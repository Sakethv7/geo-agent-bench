"""Client side of Phase C (ADR-021, ADR-022): sign requests and run OAuth for the model.

An httpx Auth object wraps the fetch client of the local agent. The model never holds a key or a
token. It asks for a page. This code signs the request, and on a 401 with a resource_metadata link it
runs the sign-in with PKCE as the simulated user, then retries.
"""
import base64
import hashlib
import json
import os
import secrets
from collections.abc import Generator
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from geobench.oauth import CLIENT_ID, REDIRECT_URI, SCOPE, SECRET_HEADER, load_user_secret
from geobench.webbotauth import sign_headers

DENIED_NOTE = "The user did not allow access."
SIGNING_MODES = ("signed", "spoof", "unsigned")


def config_dir() -> Path:
    return Path(os.environ.get("GEOBENCH_CONFIG_DIR", "~/.config/geobench")).expanduser()


def load_signing_key() -> Ed25519PrivateKey:
    path = config_dir() / "bot_key.pem"
    if not path.exists():
        raise FileNotFoundError(f"no signing key at {path}. Run: geobench botkey")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("signing key is not Ed25519")
    return key


class AccountAuth(httpx.Auth):
    """signing is signed, spoof or unsigned. consent is granted or denied."""

    requires_response_body = True  # the sign-in steps read JSON from intermediate responses

    def __init__(self, signing: str, consent: str, agent_url: str, user_agent: str, key: Ed25519PrivateKey | None = None,
                 secret: str | None = None):
        if signing not in SIGNING_MODES or consent not in ("granted", "denied"):
            raise ValueError(f"bad signing {signing} or consent {consent}")
        self.signing, self.consent, self.agent_url, self.user_agent = signing, consent, agent_url, user_agent
        self.key = (Ed25519PrivateKey.generate() if signing == "spoof" else key if signing == "signed" else None)
        if signing == "signed" and self.key is None:
            self.key = load_signing_key()
        self.secret = secret if secret is not None else load_user_secret()
        self.token: str | None = None
        self.flows = 0
        self.denied = False

    def _prepare(self, request: httpx.Request, bearer: bool = True) -> None:
        request.headers["User-Agent"] = self.user_agent
        if self.key is not None:
            for name, value in sign_headers(self.key, self.agent_url, request.url.netloc.decode()).items():
                request.headers[name] = value
        if bearer and self.token:
            request.headers["Authorization"] = f"Bearer {self.token}"

    def _call(self, method: str, url: str, **kwargs) -> httpx.Request:
        request = httpx.Request(method, url, **kwargs)
        self._prepare(request, bearer=False)
        return request

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        self._prepare(request)
        response = yield request
        challenge = response.headers.get("www-authenticate", "")
        if response.status_code != 401 or "resource_metadata" not in challenge or self.flows >= 2:
            return
        self.flows += 1
        token = yield from self._sign_in(challenge)
        self.token = token or self.token
        retry = httpx.Request(request.method, request.url)  # the last request sent is the response the caller gets
        self._prepare(retry)
        final = yield retry
        if token is None:
            self.denied = True
            final.extensions["geobench_note"] = DENIED_NOTE

    def _sign_in(self, challenge: str) -> Generator[httpx.Request, httpx.Response, str | None]:
        metadata_url = challenge.split('resource_metadata="', 1)[1].split('"', 1)[0]
        resource = yield self._call("GET", metadata_url)
        issuer = resource.json()["authorization_servers"][0]
        server = yield self._call("GET", f"{issuer}/.well-known/oauth-authorization-server")
        endpoints = server.json()
        verifier = secrets.token_urlsafe(48)
        challenge_s256 = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(12)
        query = urlencode({"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "response_type": "code", "scope": SCOPE,
                           "state": state, "code_challenge": challenge_s256, "code_challenge_method": "S256"})
        pending = yield self._call("GET", f"{endpoints['authorization_endpoint']}?{query}")
        if pending.status_code != 200:
            return None
        consent = yield self._call(
            "POST", f"{issuer}/oauth/consent", headers={SECRET_HEADER: self.secret or "", "Content-Type": "application/json"},
            content=json.dumps({"consent_request": pending.json()["consent_request"], "approve": self.consent == "granted"}))
        if consent.status_code != 200:
            return None
        params = parse_qs(urlparse(consent.json()["redirect"]).query)
        if "code" not in params or params.get("state") != [state]:
            return None
        token = yield self._call(
            "POST", endpoints["token_endpoint"], headers={"Content-Type": "application/x-www-form-urlencoded"},
            content=urlencode({"grant_type": "authorization_code", "code": params["code"][0], "code_verifier": verifier,
                               "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI}))
        return token.json().get("access_token") if token.status_code == 200 else None
