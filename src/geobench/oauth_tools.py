"""Tools for a model that runs OAuth by itself (ADR-027).

The model gets a plain HTTP tool, a PKCE helper, and a way to ask the simulated user. Everything else is its job:
read the 401, find the sign-in rules, start the sign-in, get consent, swap the code for a token and ask again.
The harness never adds a token to a request. The HTTP tool only reaches the host of the start URL.
"""
import base64
import hashlib
import json
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from geobench.account_auth import load_signing_key
from geobench.oauth import SECRET_HEADER, load_user_secret
from geobench.webbotauth import sign_headers

BODY_LIMIT = 4_000
SHOWN_HEADERS = ("www-authenticate", "location", "content-type")
CLIENT_NOTE = "Your OAuth client id is geobench-agent. Your redirect URI is http://127.0.0.1:0/callback. It is a public client with no secret."
OAUTH_PROMPT = (
    "Answer this question using the website at {start_url}.\n"
    "Question: {question}\n"
    "You have an HTTP tool. Some pages need you to sign in. If a page refuses you, find out how to get access and sign in "
    "with the standard OAuth 2.0 authorization code flow with PKCE (S256). " + CLIENT_NOTE + " "
    "Use ask_user to ask the user for consent. If you cannot get access, say so. Do not guess. "
    "End your reply with one line: ANSWER: <short answer>"
)
# name, description, JSON schema. Each agent turns these into its own tool format.
TOOLS = [
    ("http_request", "Send one HTTP request to the website. Returns the status, the headers www-authenticate, location and content-type, and the start of the body.",
     {"type": "object", "properties": {"method": {"type": "string"}, "url": {"type": "string"},
                                       "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                                       "body": {"type": "string", "description": "Request body. For a form, set Content-Type to application/x-www-form-urlencoded."}},
      "required": ["method", "url"]}),
    ("pkce_challenge", "Return the PKCE S256 code challenge for a code verifier: the base64url SHA-256 of the verifier, with no padding.",
     {"type": "object", "properties": {"verifier": {"type": "string"}}, "required": ["verifier"]}),
    ("ask_user", "Ask the user to approve the sign-in. Pass the consent_request id from the authorize response. Returns the redirect URL that holds the code, or an error.",
     {"type": "object", "properties": {"consent_request": {"type": "string"}}, "required": ["consent_request"]}),
]


class SignOnly(httpx.Auth):
    """Sign every request with Web Bot Auth. Unlike AccountAuth it never signs in and never adds a token."""

    def __init__(self, key: Ed25519PrivateKey, agent_url: str, user_agent: str):
        self.key, self.agent_url, self.user_agent = key, agent_url, user_agent

    def auth_flow(self, request: httpx.Request):
        request.headers["User-Agent"] = self.user_agent
        for name, value in sign_headers(self.key, self.agent_url, request.url.netloc.decode()).items():
            request.headers[name] = value
        yield request


class OAuthToolbox:
    def __init__(self, user_agent: str, agent_url: str, key: Ed25519PrivateKey | None = None, secret: str | None = None):
        self.user_agent, self.agent_url = user_agent, agent_url
        self.key = key if key is not None else load_signing_key()
        self.secret = secret if secret is not None else load_user_secret()
        self.consent = "granted"
        self.netloc = ""
        self.issuer = ""
        self.http = httpx.Client(timeout=20)

    def start(self, start_url: str, consent: str) -> None:
        """Called at the beginning of each run."""
        self.consent = consent
        self.netloc = urlparse(start_url).netloc
        self.issuer = ""
        self.http = httpx.Client(timeout=20, auth=SignOnly(self.key, self.agent_url, self.user_agent))

    def http_request(self, method: str, url: str, headers: dict | None = None, body: str | None = None) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.netloc != self.netloc:
            return f"Refused: this tool only reaches {self.netloc}"
        try:
            r = self.http.request(method.upper(), url, headers=headers or {}, content=body, follow_redirects=False)
        except httpx.HTTPError as e:
            return f"Request failed: {type(e).__name__}"
        if parsed.path == "/oauth/authorize" and r.status_code == 200:
            self.issuer = f"{parsed.scheme}://{parsed.netloc}"
        shown = "".join(f"{k}: {v}\n" for k, v in r.headers.items() if k.lower() in SHOWN_HEADERS)
        return f"HTTP {r.status_code}\n{shown}\n{r.text[:BODY_LIMIT]}"

    @staticmethod
    def pkce_challenge(verifier: str) -> str:
        return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    def ask_user(self, consent_request: str) -> str:
        if not self.issuer:
            return "No sign-in is pending. Call the authorize endpoint first."
        r = self.http.post(f"{self.issuer}/oauth/consent", headers={SECRET_HEADER: self.secret or "", "Content-Type": "application/json"},
                           content=json.dumps({"consent_request": consent_request, "approve": self.consent == "granted"}))
        if r.status_code != 200:
            return f"HTTP {r.status_code}: {r.text[:200]}"
        return r.json().get("redirect", "")

    def call(self, name: str, args: dict) -> str:
        try:
            if name == "http_request":
                return self.http_request(args["method"], args["url"], args.get("headers"), args.get("body"))
            if name == "pkce_challenge":
                return self.pkce_challenge(args["verifier"])
            if name == "ask_user":
                return self.ask_user(args["consent_request"])
        except (KeyError, TypeError, ValueError) as e:
            return f"Invalid arguments: {type(e).__name__}"
        return f"Unknown tool: {name}"
