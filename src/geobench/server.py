"""Test site. One process serves every variant by Host header and writes one
JSON Lines log line per request. Caddy sits in front for HTTPS and sets
X-Forwarded-For. We trust that header only when the peer is local."""
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote_plus

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from geobench.content import BUILDERS, ORDERS_PATH, Page, build_site, orders_html, twin_path
from geobench.interceptor import Decision, RequestInfo, decide
from geobench.mcp import SURFACES, handle_message
from geobench.oauth import OAuthServer, load_user_secret
from geobench.webbotauth import DIRECTORY_PATH, DIRECTORY_TYPE, Verifier, claims_signature_only_bot

LOCAL_PEERS = {"127.0.0.1", "::1"}
TOKEN_RE = re.compile(r"^[a-z0-9]{8}$")
BOT_HOST = "bot"  # one fixed host that serves only the key directory (ADR-021)
OAUTH_PATHS = ("/oauth/authorize", "/oauth/consent", "/oauth/token", "/.well-known/oauth-protected-resource", "/.well-known/oauth-authorization-server")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for")
    if peer in LOCAL_PEERS and forwarded:
        return forwarded.split(",")[0].strip()
    return peer


def negotiated_twin(site: dict[str, Page], variant: str, path: str, accept: str) -> Page | None:
    """On ai, a page request whose Accept header names text/markdown gets the markdown twin (ADR-014).
    Quality values are ignored."""
    if variant != "ai" or "text/markdown" not in accept.lower():
        return None
    page = site.get(path)
    if page is None or page.kind != "page" or not page.content_type.startswith("text/html"):
        return None
    return site.get(twin_path(path))


def resolve_host(host: str, domain: str | None, default_variant: str | None) -> tuple[str, str | None] | None:
    """Return (variant, run_token) for a host we serve, or None.

    With a domain, the host is <variant>.<domain> (no token) or <token>.<variant>.<domain>.
    Without a domain, the first label is the variant, as before."""
    name = host.split(":")[0].lower()
    if domain is None:
        label = name.split(".")[0]
        variant = label if label in BUILDERS or label == BOT_HOST else default_variant
        return (variant, None) if variant else None
    suffix = f".{domain}"
    if not name.endswith(suffix):
        return None
    labels = name[: -len(suffix)].split(".")
    if len(labels) == 1 and (labels[0] in BUILDERS or labels[0] == BOT_HOST):
        return labels[0], None
    if len(labels) == 2 and TOKEN_RE.match(labels[0]) and labels[1] in BUILDERS:
        return labels[1], labels[0]
    return None


def make_app(
    log_path: Path,
    default_variant: str | None = None,
    admin_ips: frozenset[str] = frozenset(LOCAL_PEERS),
    mode: str = "record",
    reference: dict | None = None,
    domain: str | None = None,
    directory: dict | None = None,
    user_secret: str | None = None,
    verifier: Verifier | None = None,
    oauth: OAuthServer | None = None,
    signature_policy: str = "record",
) -> Starlette:
    """mode is "record" (interceptor labels, never blocks) or "enforce".
    signature_policy is "record" (a signature verdict is only logged) or "enforce". Enforce answers 403 on the account host
    to a request that claims our own bot name without a valid signature (ADR-026). Nothing else is ever blocked by it.
    domain turns on run hosts (<token>.<variant>.<domain>). See docs/api.md, per-run host names."""
    sites = {v: build_site(v) for v in BUILDERS}
    sites[BOT_HOST] = {DIRECTORY_PATH: Page("directory", json.dumps(directory), DIRECTORY_TYPE)} if directory else {}
    verifier = verifier or Verifier()
    oauth = oauth or OAuthServer(user_secret if user_secret is not None else load_user_secret())
    reference = reference or {}
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(request: Request, variant: str, run_token: str | None, status: int, size: int, kind: str, decision: Decision, negotiated: bool, extra: dict | None = None) -> None:
        line = {
            "ts": now_iso(),
            "host": request.headers.get("host", "").split(":")[0],
            "run_token": run_token,
            "variant": variant,
            "method": request.method,
            "path": request.url.path,
            "query": request.url.query or None,
            "status": status,
            "bytes": size,
            "ua": request.headers.get("user-agent"),
            "ip": client_ip(request),
            "accept": request.headers.get("accept"),
            "referer": request.headers.get("referer"),
            "kind": kind,
            "negotiated": negotiated,
            "interceptor": {"mode": mode, "class": decision.klass, "decision": decision.action, "agent": decision.agent},
        } | (extra or {})
        with log_path.open("a") as f:
            f.write(json.dumps(line) + "\n")

    def admin(request: Request, path: str) -> Response:
        if client_ip(request) not in admin_ips:
            return PlainTextResponse("Not found", status_code=404)
        if path == "/__health":
            return JSONResponse({"ok": True, "mode": mode})
        return JSONResponse({"now": now_iso()})

    def scheme_of(request: Request) -> str:
        peer = request.client.host if request.client else ""
        forwarded = request.headers.get("x-forwarded-proto")
        return forwarded if peer in LOCAL_PEERS and forwarded in ("http", "https") else request.url.scheme

    async def mcp_response(request: Request, surface: str, run_token: str | None) -> tuple[Response, str, dict]:
        if surface not in SURFACES or (surface == "poisoned" and run_token is None):
            return PlainTextResponse("Not found", status_code=404), "unknown", {}
        if request.method != "POST":
            return PlainTextResponse("Method not allowed", status_code=405, headers={"Allow": "POST"}), "mcp", {}
        try:
            message = json.loads(await request.body())
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400), "mcp", {}
        outcome = handle_message(surface, message)
        response = Response(status_code=202) if outcome.body is None else JSONResponse(outcome.body, status_code=outcome.status)
        return response, "mcp", {"mcp": outcome.info}

    def oauth_response(request: Request, path: str, host: str, body: bytes) -> tuple[Response, dict]:
        scheme = scheme_of(request)
        if path == ORDERS_PATH:
            status, headers, info = oauth.protect(request.headers.get("authorization"), host, scheme)
            response = Response(orders_html(), media_type="text/html; charset=utf-8") if status == 200 else PlainTextResponse("Unauthorized" if status == 401 else "Forbidden", status_code=status, headers=headers)
            return response, {"oauth": info}
        if path == "/.well-known/oauth-protected-resource":
            status, headers, data, info = oauth.resource_metadata(host, scheme)
        elif path == "/.well-known/oauth-authorization-server":
            status, headers, data, info = oauth.server_metadata(host, scheme)
        elif path == "/oauth/authorize":
            status, headers, data, info = oauth.authorize(dict(request.query_params), host)
        else:
            try:
                parsed = json.loads(body) if path == "/oauth/consent" else dict(x.split("=", 1) for x in body.decode().split("&") if "=" in x)
            except ValueError:
                parsed = {}
            if request.method != "POST":
                return PlainTextResponse("Method not allowed", status_code=405), {"oauth": {"endpoint": path.rsplit("/", 1)[-1], "result": "bad_method"}}
            if path == "/oauth/consent":
                status, headers, data, info = oauth.consent({k.lower(): v for k, v in request.headers.items()}, parsed if isinstance(parsed, dict) else {}, host)
            else:
                status, headers, data, info = oauth.token({k: unquote_plus(v) for k, v in parsed.items()}, host)
        return JSONResponse(data, status_code=status, headers=headers), {"oauth": info}

    async def handle(request: Request) -> Response:
        path = request.url.path
        if path in ("/__health", "/__time"):
            return admin(request, path)
        host = request.headers.get("host", "")
        resolved = resolve_host(host, domain, default_variant)
        if resolved is None:
            return PlainTextResponse("Misdirected request", status_code=421)
        variant, run_token = resolved
        extra: dict = {}
        verdict = await asyncio.to_thread(verifier.verify, dict(request.headers), host.lower(), request.method, path)
        if verdict is not None:
            extra["sig"] = {"verdict": verdict.verdict, "keyid": verdict.keyid, "agent_url": verdict.agent_url, "created": verdict.created, "reason": verdict.reason}
        page: Page | None = sites[variant].get(path)
        kind = page.kind if page else "unknown"
        twin = negotiated_twin(sites[variant], variant, path, request.headers.get("accept", ""))
        info = RequestInfo(host, path, request.method, request.headers.get("user-agent", ""), client_ip(request), dict(request.headers))
        decision = decide(info, mode, reference)
        if (signature_policy == "enforce" and variant == "account" and claims_signature_only_bot(request.headers.get("user-agent"))
                and (verdict is None or verdict.verdict != "valid")):
            decision = Decision("block", "unsigned_claimed_bot", None, "signature required")
        if decision.action == "block":
            response: Response = PlainTextResponse("Forbidden", status_code=403)
        elif variant == "native" and path.startswith("/mcp/"):
            response, kind, more = await mcp_response(request, path[len("/mcp/"):].strip("/"), run_token)
            extra |= more
        elif variant == "account" and (path in OAUTH_PATHS or path == ORDERS_PATH):
            response, more = oauth_response(request, path, host.lower(), await request.body())
            kind = "protected" if path == ORDERS_PATH else "oauth"
            extra |= more
        elif page is None:
            response = PlainTextResponse("Not found", status_code=404)
        elif request.method == "POST":
            response = PlainTextResponse("Thanks", status_code=200) if kind == "form" else PlainTextResponse("Method not allowed", status_code=405)
        elif twin is not None:
            response = Response(twin.body, media_type=twin.content_type, headers={"Vary": "Accept"})
        else:
            response = Response(page.body, media_type=page.content_type, headers={"Vary": "Accept"} if variant == "ai" else None)
        log(request, variant, run_token, response.status_code, len(response.body), kind, decision, twin is not None and response.status_code == 200, extra)
        return response

    return Starlette(routes=[Route("/{path:path}", handle, methods=["GET", "HEAD", "POST"])])
