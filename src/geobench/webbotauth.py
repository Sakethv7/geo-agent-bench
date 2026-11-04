"""Web Bot Auth signatures (ADR-021): sign requests with Ed25519 and verify them.

Follows the IETF Web Bot Auth drafts and RFC 9421 (HTTP message signatures). The draft ids I read are
in DRAFT. Headers: Signature-Agent, Signature-Input, Signature. Signature-Agent is a Structured Fields
dictionary, sig1="https://agent.example/.well-known/http-message-signatures-directory", and the covered
component is "signature-agent";key="sig1". In the working-group draft the value is the URL of the key
set. In the earlier -02 draft it was an origin and the verifier added the well-known path. A value with no
path is still read that way. The legacy quoted string is still accepted. A member with a parameter other
than type=directory is ignored. The verifier only records a
verdict. It never blocks and never raises.
"""
import base64
import hashlib
import ipaddress
import json
import re
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

DRAFT = (
    "draft-ietf-webbotauth-httpsig-protocol-01 (2026-10-29). Still accepted: the origin form of "
    "draft-meunier-webbotauth-httpsig-protocol-02 and the legacy quoted string"
)
DIRECTORY_PATH = "/.well-known/http-message-signatures-directory"
DIRECTORY_TYPE = "application/http-message-signatures-directory+json"
TAG = "web-bot-auth"
COMPONENTS = ("@authority", "@method", "@path", "signature-agent")
DIRECTORY_TTL = 300
DIRECTORY_TIMEOUT = 2.0
DIRECTORY_MAX_BYTES = 64_000
CLOCK_SKEW = 60

SIGNATURE_ONLY_TOKENS = ("geobench-local",)  # our own bot name. No address range can prove it, only a signature


def claims_signature_only_bot(user_agent: str | None) -> bool:
    return any(t in (user_agent or "").lower() for t in SIGNATURE_ONLY_TOKENS)


VERDICTS = ("absent", "valid", "invalid", "expired", "unknown_key", "directory_unreachable")


@dataclass
class Verdict:
    verdict: str
    keyid: str | None = None
    agent_url: str | None = None
    created: int | None = None
    reason: str | None = None


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def raw_public(key: Ed25519PrivateKey | Ed25519PublicKey) -> bytes:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return public.public_bytes_raw()


def thumbprint(x: str) -> str:
    """RFC 7638 thumbprint of an Ed25519 JWK."""
    canonical = json.dumps({"crv": "Ed25519", "kty": "OKP", "x": x}, separators=(",", ":"), sort_keys=True)
    return _b64u(hashlib.sha256(canonical.encode()).digest())


def public_jwk(key: Ed25519PrivateKey) -> dict:
    return {"kty": "OKP", "crv": "Ed25519", "x": _b64u(raw_public(key))}


def directory_document(keys: list[Ed25519PrivateKey]) -> dict:
    return {"keys": [public_jwk(k) for k in keys]}


def _agent_members(raw: str) -> dict[str, str]:
    """The members of a Signature-Agent dictionary: {label: url}. The legacy quoted string gets the label ''.
    A member with a parameter other than type=directory (for example type=jwks_uri) is left out."""
    value = raw.strip()
    members = {label: url for label, url, extra in re.findall(r'(\w+)="([^"]*)"((?:;\w+=[\w-]+)*)', value)
               if extra in ("", ";type=directory")}
    if members:
        return members
    legacy = re.fullmatch(r'"(.*)"', value)
    return {"": legacy.group(1) if legacy else value}


def _agent_member_serialization(raw: str, key: str) -> str:
    """How RFC 9421 serializes one dictionary member for a covered component: the value with its parameters."""
    for label, url, extra in re.findall(r'(\w+)="([^"]*)"((?:;\w+=[\w-]+)*)', raw.strip()):
        if label == key and extra in ("", ";type=directory"):
            return f'"{url}"{extra}'
    return '""'


def _signature_agent_value(raw: str, key: str | None = None) -> str:
    """The agent URL: the member named by key, else the first member."""
    members = _agent_members(raw)
    return members[key] if key in members else next(iter(members.values()))


Component = tuple[str, str | None]  # (name, key parameter)


def _component_id(component: Component) -> str:
    name, key = component
    return f'"{name}"' + (f';key="{key}"' if key else "")


def _base(components: list[Component], authority: str, method: str, path: str, agent_header: str, params: str) -> bytes:
    lines = []
    for name, key in components:
        if name == "signature-agent":
            value = agent_header.strip() if key is None else _agent_member_serialization(agent_header, key)
        else:
            value = {"@authority": authority, "@method": method.upper(), "@path": path}[name]
        lines.append(f"{_component_id((name, key))}: {value}")
    inner = " ".join(_component_id(c) for c in components)
    lines.append(f'"@signature-params": ({inner}){params}')
    return "\n".join(lines).encode()


def sign_headers(key: Ed25519PrivateKey, agent_url: str, authority: str, now: float | None = None, ttl: int = 300,
                 form: str = "url") -> dict[str, str]:
    """Headers for one request. form is url (working-group -01: the key-set URL), origin (-02: an origin with no
    trailing slash), origin_typed (-02 with the explicit type=directory parameter) or legacy (the old quoted string)."""
    created = int(now if now is not None else time.time())
    keyid = thumbprint(public_jwk(key)["x"])
    legacy = form == "legacy"
    if form == "url":
        agent_url = directory_url(agent_url)
    elif form in ("origin", "origin_typed"):
        agent_url = agent_url.rstrip("/")
    agent_header = f'"{agent_url}"' if legacy else f'sig1="{agent_url}"' + (";type=directory" if form == "origin_typed" else "")
    params = f';created={created};expires={created + ttl};keyid="{keyid}";alg="ed25519";tag="{TAG}"'
    components: list[Component] = [("@authority", None), ("signature-agent", None if legacy else "sig1")]
    signature = key.sign(_base(components, authority, "GET", "", agent_header, params))
    inner = " ".join(_component_id(c) for c in components)
    return {"Signature-Agent": agent_header, "Signature-Input": f"sig1=({inner}){params}",
            "Signature": f"sig1=:{base64.b64encode(signature).decode()}:"}


def _parse_input(value: str) -> tuple[str, list[Component], str, dict[str, str]]:
    match = re.fullmatch(r"(\w+)=\(([^)]*)\)(.*)", value.strip())
    if not match:
        raise ValueError("bad Signature-Input")
    label, inner, params = match.groups()
    components = [(name, (re.search(r'key="([^"]*)"', extra) or [None, None])[1])
                  for name, extra in re.findall(r'"([^"]+)"((?:;\w+(?:="[^"]*")?)*)', inner)]
    pairs = dict(re.findall(r';(\w+)=("[^"]*"|[^;]*)', params))
    return label, components, params, {k: v.strip('"') for k, v in pairs.items()}


def _parse_signature(value: str, label: str) -> bytes:
    match = re.fullmatch(rf"{re.escape(label)}=:([A-Za-z0-9+/=]+):", value.strip())
    if not match:
        raise ValueError("bad Signature")
    return base64.b64decode(match.group(1))


def public_agent_url_ok(url: str) -> bool:
    """https, a public host, no userinfo. Every resolved address must be global."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
    except OSError:
        return False
    return bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)


def directory_url(agent_url: str) -> str:
    """The URL of the key set. A value with no path is an origin, so the well-known path is added (-02 form).
    Any other value is the key-set URL itself (working-group -01 form), without its query."""
    parsed = urlparse(agent_url)
    if parsed.path in ("", "/"):
        return agent_url.rstrip("/") + DIRECTORY_PATH
    return parsed._replace(query="", fragment="").geturl()


def fetch_directory_http(agent_url: str) -> dict | None:
    """Fetch the key directory. No redirects. At most 64 KB. None on any failure."""
    url = directory_url(agent_url)
    try:
        with httpx.Client(follow_redirects=False, timeout=DIRECTORY_TIMEOUT) as client, client.stream("GET", url) as response:
            if response.status_code != 200:
                return None
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > DIRECTORY_MAX_BYTES:
                    return None
        document = json.loads(body)
        return document if isinstance(document, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


class Verifier:
    def __init__(self, fetch: Callable[[str], dict | None] = fetch_directory_http,
                 url_ok: Callable[[str], bool] = public_agent_url_ok, clock: Callable[[], float] = time.time):
        self.fetch, self.url_ok, self.clock = fetch, url_ok, clock
        self.cache: dict[str, tuple[float, dict | None]] = {}

    def _directory(self, agent_url: str) -> dict | None:
        hit = self.cache.get(agent_url)
        if hit and self.clock() - hit[0] < DIRECTORY_TTL:
            return hit[1]
        document = self.fetch(agent_url)
        if document is not None:  # failures are not cached, so a fixed directory works at once
            self.cache[agent_url] = (self.clock(), document)
        return document

    def verify(self, headers: Mapping[str, str], authority: str, method: str = "GET", path: str = "") -> Verdict | None:
        """None when the request carries no signature header at all."""
        lower = {k.lower(): v for k, v in headers.items()}
        names = ("signature-agent", "signature-input", "signature")
        if not any(n in lower for n in names):
            return None
        if not all(n in lower for n in names):
            return Verdict("absent", reason="incomplete_headers")
        agent_url = _signature_agent_value(lower["signature-agent"])
        try:
            return self._verify(lower, agent_url, authority, method, path)
        except Exception as e:  # the verifier never raises into the request path
            return Verdict("invalid", agent_url=agent_url, reason=f"verifier_error:{type(e).__name__}")

    def _verify(self, lower: dict, agent_url: str, authority: str, method: str, path: str) -> Verdict:
        try:
            label, components, params_text, params = _parse_input(lower["signature-input"])
            signature = _parse_signature(lower["signature"], label)
        except ValueError as e:
            return Verdict("invalid", agent_url=agent_url, reason=str(e))
        base = Verdict("invalid", params.get("keyid"), agent_url, int(params["created"]) if params.get("created", "").isdigit() else None)
        if not self.url_ok(agent_url):
            return Verdict("invalid", base.keyid, agent_url, base.created, "agent_url_not_allowed")
        if params.get("tag") != TAG or params.get("alg", "ed25519") != "ed25519" or any(n not in COMPONENTS for n, _ in components):
            return Verdict("invalid", base.keyid, agent_url, base.created, "unsupported_parameters")
        signed_agent = next((k for n, k in components if n == "signature-agent"), None)
        if signed_agent is not None and signed_agent not in _agent_members(lower["signature-agent"]):
            return Verdict("invalid", base.keyid, agent_url, base.created, "signed_member_missing")
        agent_url = _signature_agent_value(lower["signature-agent"], signed_agent)  # the member that the signature covers
        document = self._directory(agent_url)
        if document is None:
            return Verdict("directory_unreachable", base.keyid, agent_url, base.created)
        keys = {thumbprint(k["x"]): k["x"] for k in document.get("keys", []) if k.get("kty") == "OKP" and k.get("crv") == "Ed25519" and "x" in k}
        if params.get("keyid") not in keys:
            return Verdict("unknown_key", base.keyid, agent_url, base.created)
        now = self.clock()
        expires = params.get("expires", "")
        if base.created is None or not expires.isdigit() or now > int(expires) or base.created > now + CLOCK_SKEW:
            return Verdict("expired", base.keyid, agent_url, base.created, "time_window")
        signed = _base(components, authority, method, path, lower["signature-agent"].strip(), params_text)
        try:
            Ed25519PublicKey.from_public_bytes(_b64u_decode(keys[params["keyid"]])).verify(signature, signed)
        except InvalidSignature:
            return Verdict("invalid", base.keyid, agent_url, base.created, "bad_signature")
        return Verdict("valid", base.keyid, agent_url, base.created)
