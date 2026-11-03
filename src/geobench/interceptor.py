"""Interceptor: identify, classify, decide. No side effects, no network.

Identity reference comes from runs/identity_reference.json, written by the
identity probe: {agent: {"ua_tokens": [...], "ip_prefixes": [...]}}.

Classes
  verified_agent  user agent token and IP prefix both match a reference entry
  claimed_agent   user agent matches a known agent token, IP does not verify
  unknown         anything else

Policy in enforce mode: block claimed_agent (a spoof or an unlisted IP),
allow the rest. In record mode every decision is allow. The "markdown" action
from api.md waits for a variant that has markdown twins.
"""
import json
from dataclasses import dataclass
from pathlib import Path

from geobench.parse_logs import KNOWN_AGENT_TOKENS


@dataclass
class RequestInfo:
    host: str
    path: str
    method: str
    ua: str
    ip: str
    headers: dict[str, str]


@dataclass
class Decision:
    action: str            # "allow" | "block"
    klass: str             # "verified_agent" | "claimed_agent" | "unknown"
    agent: str | None
    reason: str


def load_reference(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _match_agent(ua: str, reference: dict) -> str | None:
    ua = ua.lower()
    for agent, ref in reference.items():
        if any(t.lower() in ua for t in ref["ua_tokens"]):
            return agent
    return None


def decide(req: RequestInfo, mode: str, reference: dict) -> Decision:
    try:
        agent = _match_agent(req.ua, reference)
        if agent and any(req.ip.startswith(p) for p in reference[agent]["ip_prefixes"]):
            return Decision("allow", "verified_agent", agent, "ua and ip match reference")
        claimed = agent or next((t for t in KNOWN_AGENT_TOKENS if t in req.ua.lower()), None)
        if claimed:
            action = "block" if mode == "enforce" else "allow"
            return Decision(action, "claimed_agent", agent, "ua names an agent, ip not verified")
        return Decision("allow", "unknown", None, "no agent token")
    except Exception as e:  # decide never raises
        return Decision("allow", "unknown", None, f"error:{type(e).__name__}")
