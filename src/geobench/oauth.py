"""A small OAuth server and one protected page (ADR-022). Authorization code with PKCE, one
pre-registered client, one fake user, scope orders:read. State lives in memory. A restart ends it.

The site is both the authorization server and the resource server. Every handler returns
(status, headers, body, info). info goes into the request log. No secret ever goes into info.
"""
import base64
import hashlib
import hmac
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlencode

CLIENT_ID = "geobench-agent"
REDIRECT_URI = "http://127.0.0.1:0/callback"
SCOPE = "orders:read"
CODE_TTL = 60
TOKEN_TTL = 300
CONSENT_TTL = 120
LIVE_CAP = 100
SECRET_HEADER = "x-geobench-user"

Result = tuple[int, dict, object, dict]


def load_user_secret() -> str | None:
    """From GEOBENCH_USER_SECRET, else from <config dir>/user_secret. None means consent always fails."""
    if os.environ.get("GEOBENCH_USER_SECRET"):
        return os.environ["GEOBENCH_USER_SECRET"]
    path = Path(os.environ.get("GEOBENCH_CONFIG_DIR", "~/.config/geobench")).expanduser() / "user_secret"
    return path.read_text().strip() if path.exists() else None


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class OAuthServer:
    def __init__(self, secret: str | None, clock: Callable[[], float] = time.time):
        self.secret, self.clock = secret, clock
        self.consents: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}

    def _purge(self) -> None:
        now = self.clock()
        for store in (self.consents, self.codes, self.tokens):
            for key in [k for k, v in store.items() if v["exp"] < now]:
                del store[key]

    def _room(self, store: dict) -> bool:
        self._purge()
        return len(store) < LIVE_CAP

    @staticmethod
    def _issuer(host: str, scheme: str) -> str:
        return f"{scheme}://{host}"

    def resource_metadata(self, host: str, scheme: str) -> Result:
        issuer = self._issuer(host, scheme)
        return 200, {}, {"resource": issuer, "authorization_servers": [issuer], "scopes_supported": [SCOPE]}, {"endpoint": "resource_metadata"}

    def server_metadata(self, host: str, scheme: str) -> Result:
        issuer = self._issuer(host, scheme)
        return 200, {}, {
            "issuer": issuer, "authorization_endpoint": f"{issuer}/oauth/authorize", "token_endpoint": f"{issuer}/oauth/token",
            "code_challenge_methods_supported": ["S256"], "scopes_supported": [SCOPE], "response_types_supported": ["code"],
        }, {"endpoint": "server_metadata"}

    def authorize(self, params: dict, host: str) -> Result:
        info = {"endpoint": "authorize", "client_id": params.get("client_id"), "scope": params.get("scope")}
        problems = [
            params.get("client_id") != CLIENT_ID, params.get("redirect_uri") != REDIRECT_URI, params.get("response_type") != "code",
            params.get("scope") != SCOPE, params.get("code_challenge_method") != "S256", not params.get("code_challenge"), not params.get("state"),
        ]
        if any(problems):  # never redirect on a bad request
            return 400, {}, {"error": "invalid_request"}, info | {"result": "rejected"}
        if not self._room(self.consents):
            return 503, {}, {"error": "temporarily_unavailable"}, info | {"result": "full"}
        consent_id = secrets.token_urlsafe(16)
        self.consents[consent_id] = {"host": host, "state": params["state"], "challenge": params["code_challenge"], "exp": self.clock() + CONSENT_TTL}
        return 200, {}, {"consent_request": consent_id}, info | {"result": "pending"}

    def consent(self, headers: dict, body: dict, host: str) -> Result:
        info = {"endpoint": "consent", "client_id": CLIENT_ID, "scope": SCOPE}
        given = headers.get(SECRET_HEADER, "")
        if not self.secret or not hmac.compare_digest(given.encode(), self.secret.encode()):
            return 403, {}, {"error": "forbidden"}, info | {"result": "forbidden"}
        pending = self.consents.pop(str(body.get("consent_request")), None)
        if pending is None or pending["host"] != host or pending["exp"] < self.clock():
            return 400, {}, {"error": "invalid_request"}, info | {"result": "unknown_request"}
        if not body.get("approve"):
            query = urlencode({"error": "access_denied", "state": pending["state"]})
            return 200, {}, {"redirect": f"{REDIRECT_URI}?{query}"}, info | {"result": "denied"}
        if not self._room(self.codes):
            return 503, {}, {"error": "temporarily_unavailable"}, info | {"result": "full"}
        code = secrets.token_urlsafe(24)
        self.codes[code] = {"host": host, "challenge": pending["challenge"], "exp": self.clock() + CODE_TTL}
        return 200, {}, {"redirect": f"{REDIRECT_URI}?{urlencode({'code': code, 'state': pending['state']})}"}, info | {"result": "approved"}

    def token(self, form: dict, host: str) -> Result:
        info = {"endpoint": "token", "client_id": form.get("client_id"), "scope": SCOPE}
        entry = self.codes.pop(form.get("code", ""), None)  # one use, even when the exchange fails
        ok = (
            form.get("grant_type") == "authorization_code" and form.get("client_id") == CLIENT_ID
            and form.get("redirect_uri") == REDIRECT_URI and entry is not None and entry["host"] == host
            and entry["exp"] >= self.clock() and hmac.compare_digest(s256(form.get("code_verifier", "")), entry["challenge"])
        )
        if not ok:
            return 400, {}, {"error": "invalid_grant"}, info | {"result": "invalid_grant"}
        if not self._room(self.tokens):
            return 503, {}, {"error": "temporarily_unavailable"}, info | {"result": "full"}
        token = secrets.token_urlsafe(32)
        self.tokens[token] = {"host": host, "scope": SCOPE, "exp": self.clock() + TOKEN_TTL}
        return 200, {}, {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL, "scope": SCOPE}, info | {"result": "issued"}

    def protect(self, authorization: str | None, host: str, scheme: str, scope: str = SCOPE) -> tuple[int, dict, dict]:
        """(status, headers, info) for the protected page. 200 means serve it."""
        metadata = f'{self._issuer(host, scheme)}/.well-known/oauth-protected-resource'
        challenge = f'Bearer resource_metadata="{metadata}"'
        info = {"endpoint": "protected", "scope": scope}
        if not authorization or not authorization.lower().startswith("bearer "):
            return 401, {"WWW-Authenticate": challenge}, info | {"result": "no_token"}
        entry = self.tokens.get(authorization[7:].strip())
        if entry is None or entry["host"] != host:
            return 401, {"WWW-Authenticate": challenge}, info | {"result": "bad_token"}
        if entry["exp"] < self.clock():
            return 401, {"WWW-Authenticate": f'{challenge}, error="invalid_token"'}, info | {"result": "expired"}
        if entry["scope"] != scope:
            return 403, {"WWW-Authenticate": f'{challenge}, error="insufficient_scope", scope="{scope}"'}, info | {"result": "insufficient_scope"}
        return 200, {}, info | {"result": "ok"}
