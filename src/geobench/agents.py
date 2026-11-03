"""Agent adapters. Every agent is a function:

    run(question, start_url, seed, budget) -> AgentResult

Two kinds. "claude" uses Anthropic's server-side web fetch tool, so Anthropic's
servers do the fetching. "openai_compat" runs our own loop against any
OpenAI-compatible chat endpoint (LM Studio, llama.cpp) with our own fetch_url
tool, so the fetching is ours.
"""
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import anthropic
import httpx

LOCAL_UA = "geobench-local/0.1"
FETCH_LIMIT = 60_000
PROMPT = (
    "Answer this question using the website at {start_url}.\n"
    "Question: {question}\n"
    "End your reply with one line: ANSWER: <short answer>"
)
FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": "Fetch a web page and return its text.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
}


@dataclass
class Budget:
    max_requests: int = 40
    max_seconds: int = 600


@dataclass
class AgentResult:
    text: str
    api_tokens: dict | None
    retries: int
    claimed_fetches: list[str] = field(default_factory=list)  # URLs the agent says it fetched


class AgentError(Exception):
    """Any API or tool failure. The message goes in the run record."""


class BudgetExceeded(AgentError):
    """The run hit max_requests or max_seconds. The runner records state timeout."""


Agent = Callable[[str, str, int | None, Budget], AgentResult]


def fetch_url(http: httpx.Client, url: str, obey_robots: bool = False) -> str:
    if urlparse(url).scheme not in ("http", "https"):
        return "Invalid URL"
    if obey_robots and not _robots_allow(http, url):
        return "Blocked by robots.txt"
    try:
        r = http.get(url, headers={"User-Agent": LOCAL_UA}, timeout=20, follow_redirects=True)
    except httpx.HTTPError as e:
        return f"Fetch failed: {type(e).__name__}"
    if r.status_code >= 400:
        return f"HTTP {r.status_code}"
    return r.text[:FETCH_LIMIT]


def _robots_allow(http: httpx.Client, url: str) -> bool:
    parts = urlparse(url)
    try:
        r = http.get(f"{parts.scheme}://{parts.netloc}/robots.txt", headers={"User-Agent": LOCAL_UA}, timeout=20)
    except httpx.HTTPError:
        return True
    if r.status_code != 200:
        return True
    parser = RobotFileParser()
    parser.parse(r.text.splitlines())
    return parser.can_fetch(LOCAL_UA, url)


def openai_compat_agent(cfg: dict, http: httpx.Client | None = None) -> Agent:
    http = http or httpx.Client()
    base = cfg["base_url"].rstrip("/")

    def run(question: str, start_url: str, seed: int | None, budget: Budget) -> AgentResult:
        messages = [{"role": "user", "content": PROMPT.format(start_url=start_url, question=question)}]
        deadline = time.monotonic() + budget.max_seconds
        fetched: list[str] = []
        tokens = {"in": 0, "out": 0}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BudgetExceeded("max_seconds")
            body = {"model": cfg["model"], "messages": messages, "tools": [FETCH_TOOL], "temperature": cfg.get("temperature", 0.7)}
            if seed is not None:
                body["seed"] = seed
            try:
                r = http.post(f"{base}/chat/completions", json=body, timeout=remaining)
                r.raise_for_status()
            except httpx.TimeoutException as e:
                raise BudgetExceeded("max_seconds") from e
            except httpx.HTTPError as e:
                raise AgentError(f"{type(e).__name__}: {e}") from e
            data = r.json()
            usage = data.get("usage") or {}
            tokens["in"] += usage.get("prompt_tokens", 0)
            tokens["out"] += usage.get("completion_tokens", 0)
            message = data["choices"][0]["message"]
            messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                return AgentResult(message.get("content") or "", tokens, 0, fetched)
            for call in calls:
                if len(fetched) >= budget.max_requests:
                    raise BudgetExceeded("max_requests")
                try:
                    url = json.loads(call["function"]["arguments"])["url"]
                except (KeyError, ValueError, TypeError):
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": "Invalid arguments"})
                    continue
                fetched.append(url)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": fetch_url(http, url, cfg.get("obey_robots", False))})

    return run


def claude_agent(cfg: dict, client: anthropic.Anthropic | None = None) -> Agent:
    # max_retries covers HTTP 429 and 5xx. The SDK does not report how many retries it made.
    client = client or anthropic.Anthropic(max_retries=3)

    def run(question: str, start_url: str, seed: int | None, budget: Budget) -> AgentResult:
        messages = [{"role": "user", "content": PROMPT.format(start_url=start_url, question=question)}]
        tools = [{"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": budget.max_requests}]
        deadline = time.monotonic() + budget.max_seconds
        fetched: list[str] = []
        tokens = {"in": 0, "out": 0}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BudgetExceeded("max_seconds")
            try:
                response = client.with_options(timeout=remaining).messages.create(
                    model=cfg["model"], max_tokens=16000, tools=tools, messages=messages
                )
            except anthropic.APITimeoutError as e:
                raise BudgetExceeded("max_seconds") from e
            except anthropic.APIError as e:
                raise AgentError(f"{type(e).__name__}: {e}") from e
            tokens["in"] += response.usage.input_tokens
            tokens["out"] += response.usage.output_tokens
            fetched += [b.input.get("url", "") for b in response.content if b.type == "server_tool_use" and b.name == "web_fetch"]
            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue
            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = details.get("category") if isinstance(details, dict) else getattr(details, "category", None)
                raise AgentError(f"refusal: {category}" if category else "refusal")
            return AgentResult("".join(b.text for b in response.content if b.type == "text"), tokens, 0, fetched)

    return run


KINDS = {"claude": claude_agent, "openai_compat": openai_compat_agent}


def make_agent(cfg: dict) -> Agent:
    return KINDS[cfg["kind"]](cfg)


def describe(cfg: dict) -> dict:
    """Run-record fields that identify the agent."""
    if cfg["kind"] == "claude":
        return {"agent_version": cfg["model"], "model_file": None, "runtime": "anthropic-api", "harness": None}
    return {
        "agent_version": cfg["model"], "model_file": cfg["model_file"], "runtime": cfg["runtime"],
        "harness": {"obey_robots": cfg.get("obey_robots", False), "user_agent": LOCAL_UA, "temperature": cfg.get("temperature", 0.7)},
    }
