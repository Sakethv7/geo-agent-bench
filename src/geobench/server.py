"""Test site. One process serves every variant by Host header and writes one
JSON Lines log line per request. Caddy sits in front for HTTPS and sets
X-Forwarded-For. We trust that header only when the peer is local."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from geobench.content import BUILDERS, Page, build_site, twin_path
from geobench.interceptor import Decision, RequestInfo, decide

LOCAL_PEERS = {"127.0.0.1", "::1"}
TOKEN_RE = re.compile(r"^[a-z0-9]{8}$")


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
        variant = label if label in BUILDERS else default_variant
        return (variant, None) if variant else None
    suffix = f".{domain}"
    if not name.endswith(suffix):
        return None
    labels = name[: -len(suffix)].split(".")
    if len(labels) == 1 and labels[0] in BUILDERS:
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
) -> Starlette:
    """mode is "record" (interceptor labels, never blocks) or "enforce".
    domain turns on run hosts (<token>.<variant>.<domain>). See docs/api.md, per-run host names."""
    sites = {v: build_site(v) for v in BUILDERS}
    reference = reference or {}
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(request: Request, variant: str, run_token: str | None, status: int, size: int, kind: str, decision: Decision, negotiated: bool) -> None:
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
        }
        with log_path.open("a") as f:
            f.write(json.dumps(line) + "\n")

    def admin(request: Request, path: str) -> Response:
        if client_ip(request) not in admin_ips:
            return PlainTextResponse("Not found", status_code=404)
        if path == "/__health":
            return JSONResponse({"ok": True, "mode": mode})
        return JSONResponse({"now": now_iso()})

    async def handle(request: Request) -> Response:
        path = request.url.path
        if path in ("/__health", "/__time"):
            return admin(request, path)
        host = request.headers.get("host", "")
        resolved = resolve_host(host, domain, default_variant)
        if resolved is None:
            return PlainTextResponse("Misdirected request", status_code=421)
        variant, run_token = resolved
        page: Page | None = sites[variant].get(path)
        kind = page.kind if page else "unknown"
        twin = negotiated_twin(sites[variant], variant, path, request.headers.get("accept", ""))
        info = RequestInfo(host, path, request.method, request.headers.get("user-agent", ""), client_ip(request), dict(request.headers))
        decision = decide(info, mode, reference)
        if decision.action == "block":
            response: Response = PlainTextResponse("Forbidden", status_code=403)
        elif page is None:
            response = PlainTextResponse("Not found", status_code=404)
        elif request.method == "POST":
            response = PlainTextResponse("Thanks", status_code=200) if kind == "form" else PlainTextResponse("Method not allowed", status_code=405)
        elif twin is not None:
            response = Response(twin.body, media_type=twin.content_type, headers={"Vary": "Accept"})
        else:
            response = Response(page.body, media_type=page.content_type, headers={"Vary": "Accept"} if variant == "ai" else None)
        log(request, variant, run_token, response.status_code, len(response.body), kind, decision, twin is not None and response.status_code == 200)
        return response

    return Starlette(routes=[Route("/{path:path}", handle, methods=["GET", "HEAD", "POST"])])
