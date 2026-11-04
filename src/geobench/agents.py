"""Agent adapters. Every agent is a function:

    run(question, start_url, seed, budget) -> AgentResult

Three kinds. "claude" uses Anthropic's server-side web fetch tool, so Anthropic's
servers do the fetching. "openai_compat" runs our own loop against any
OpenAI-compatible chat endpoint (LM Studio, llama.cpp) with our own fetch_url
tool, so the fetching is ours. "claude_tools" runs Claude with the same fetch_url
as a client tool, so Claude and the local model read the same raw text (ADR-015).
"""
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import anthropic
import httpx

from geobench.account_auth import AccountAuth
from geobench.oauth_tools import OAUTH_PROMPT, TOOLS as OAUTH_TOOL_DEFS, OAuthToolbox
from geobench.webbotauth import DRAFT

LOCAL_UA = "geobench-local/0.1"
FETCH_LIMIT = 60_000
STRUCTURED_FETCH_LIMIT = 8_000
TOOL_HISTORY_CHAR_BUDGET = 48_000
STRUCTURED_LINK_LIMIT = 40
LOCAL_KINDS = {"openai_compat", "openai_mcp", "openai_oauth"}  # agents that run on the laptop and need the local-server pre-check
MCP_BETA = "mcp-client-2025-11-20"
MCP_PROTOCOL = "2025-06-18"
EMPTY_NUDGE = (
    "You ended your turn without a final answer. Use the tool results above and reply now. "
    "End with one line: ANSWER: <short answer>"
)
MCP_PROMPT = (
    "Answer this question using the tools of the MCP server at {start_url}.\n"
    "Question: {question}\n"
    "Use the tools to find the facts. Base the answer only on tool results. "
    "End your reply with one line: ANSWER: <short answer>"
)
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
ROBUST_FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": (
            "Fetch a web page. The result contains bounded visible content and a LINKS section. "
            "Follow relevant same-site links before concluding that information is unavailable."
        ),
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
}
ROBUST_PROMPT = (
    "Answer this question using the website at {start_url}.\n"
    "Question: {question}\n"
    "Use fetch_url to inspect the start page and follow relevant links. Do not claim that the "
    "information is unavailable while relevant unvisited links remain. Base the answer only on "
    "fetched evidence, include a final SOURCE: <fetched URL> line, and end with "
    "ANSWER: <short answer>."
)


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


CONTEXT_OVERFLOW = "context_overflow"


class BudgetExceeded(AgentError):
    """The run hit max_requests or max_seconds. The runner records state timeout."""


Agent = Callable[[str, str, int | None, Budget], AgentResult]


@dataclass
class StructuredPage:
    content: str
    links: list[str]


class _PageParser(HTMLParser):
    """Extract visible text, preferring <main>, plus navigation targets."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.all_text: list[str] = []
        self.main_text: list[str] = []
        self.links: list[str] = []
        self.main_depth = 0
        self.skip_depth = 0
        self.saw_main = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        if tag == "main":
            self.saw_main = True
            self.main_depth += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1
        if tag == "main" and self.main_depth:
            self.main_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        text = " ".join(data.split())
        if not text:
            return
        self.all_text.append(text)
        if self.main_depth:
            self.main_text.append(text)


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
        note = r.extensions.get("geobench_note")
        return f"HTTP {r.status_code}. {note}" if note else f"HTTP {r.status_code}"
    return r.text[:FETCH_LIMIT]


def fetch_url_structured(
    http: httpx.Client, url: str, obey_robots: bool = False, limit: int = STRUCTURED_FETCH_LIMIT,
    link_limit: int = STRUCTURED_LINK_LIMIT,
) -> StructuredPage:
    """Return compact page evidence and explicit links without benchmark ground truth."""
    raw = fetch_url(http, url, obey_robots)
    if raw.startswith(("Invalid URL", "Blocked by robots.txt", "Fetch failed:", "HTTP ")):
        return StructuredPage(raw, [])
    parser = _PageParser()
    try:
        parser.feed(raw)
    except Exception:  # malformed HTML is still useful as bounded plain text
        parser = _PageParser()
    text_parts = parser.main_text if parser.saw_main else parser.all_text
    visible = "\n".join(text_parts) if text_parts else raw
    visible = re.sub(r"[ \t]+", " ", visible).strip()[:limit]
    links = list(dict.fromkeys(urljoin(url, href) for href in parser.links))[:link_limit]
    link_text = "\n".join(f"- {link}" for link in links) or "- none"
    content = f"URL: {url}\nVISIBLE CONTENT:\n{visible}\nLINKS:\n{link_text}"
    return StructuredPage(content, links)


def _bounded_messages(messages: list[dict], char_budget: int) -> list[dict]:
    """Compact old tool bodies while preserving tool-call/result ordering."""
    remaining = char_budget
    bounded = [dict(message) for message in messages]
    for message in reversed(bounded):
        if message.get("role") != "tool" or not isinstance(message.get("content"), str):
            continue
        content = message["content"]
        if len(content) <= remaining:
            remaining -= len(content)
            continue
        url = content.splitlines()[0] if content.startswith("URL: ") else "Earlier tool result"
        message["content"] = f"{url}\n[content compacted to preserve the context budget]"
    return bounded


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
    robust = cfg.get("navigation_policy") == "structured-v1"
    site = {"http": http}  # the client that fetches pages. Phase C swaps in one that signs and signs in

    def configure(cell: dict) -> None:
        if cell.get("signing"):
            auth = AccountAuth(cell["signing"], cell["consent"], cfg["signature_agent"], LOCAL_UA)
            site["http"] = httpx.Client(auth=auth)
        else:
            site["http"] = http

    def run(question: str, start_url: str, seed: int | None, budget: Budget) -> AgentResult:
        prompt = ROBUST_PROMPT if robust else PROMPT
        tool = ROBUST_FETCH_TOOL if robust else FETCH_TOOL
        messages = [{"role": "user", "content": prompt.format(start_url=start_url, question=question)}]
        deadline = time.monotonic() + budget.max_seconds
        fetched: list[str] = []
        discovered_links: list[str] = []
        controller_retries = 0
        tokens = {"in": 0, "out": 0}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BudgetExceeded("max_seconds")
            request_messages = (
                _bounded_messages(messages, cfg.get("tool_history_char_budget", TOOL_HISTORY_CHAR_BUDGET))
                if robust else messages
            )
            body = {"model": cfg["model"], "messages": request_messages, "tools": [tool], "temperature": cfg.get("temperature", 0.7)}
            if seed is not None:
                body["seed"] = seed
            try:
                r = http.post(f"{base}/chat/completions", json=body, timeout=remaining)
                r.raise_for_status()
            except httpx.TimeoutException as e:
                raise BudgetExceeded("max_seconds") from e
            except httpx.HTTPStatusError as e:
                if _is_context_overflow(e.response):
                    raise AgentError(CONTEXT_OVERFLOW) from e
                raise AgentError(f"{type(e).__name__}: {e}") from e
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
                retry_limit = cfg.get("early_answer_retries", 1)
                stopped_at_start = not fetched or (
                    len(fetched) == 1 and fetched[0].rstrip("/") == start_url.rstrip("/") and discovered_links
                )
                if robust and stopped_at_start and controller_retries < retry_limit:
                    controller_retries += 1
                    messages.append({
                        "role": "user",
                        "content": (
                            "The answer is not yet supported: only the start page has been inspected and it "
                            "contains unvisited links. Fetch the most relevant linked page, then answer from evidence."
                        ),
                    })
                    continue
                return AgentResult(message.get("content") or "", tokens, controller_retries, fetched)
            for call in calls:
                if len(fetched) >= budget.max_requests:
                    raise BudgetExceeded("max_requests")
                try:
                    url = json.loads(call["function"]["arguments"])["url"]
                except (KeyError, ValueError, TypeError):
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": "Invalid arguments"})
                    continue
                fetched.append(url)
                if robust:
                    page = fetch_url_structured(
                        site["http"], url, cfg.get("obey_robots", False), cfg.get("fetch_limit", STRUCTURED_FETCH_LIMIT),
                        cfg.get("link_limit", STRUCTURED_LINK_LIMIT),
                    )
                    discovered_links.extend(link for link in page.links if link not in discovered_links)
                    content = page.content
                else:
                    content = fetch_url(site["http"], url, cfg.get("obey_robots", False))
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})

    run.configure = configure
    return run


def _mcp_post(http: httpx.Client, url: str, method: str, params: dict | None = None, notify: bool = False) -> dict | None:
    body = {"jsonrpc": "2.0", "method": method} | ({} if notify else {"id": 1}) | ({} if params is None else {"params": params})
    try:
        r = http.post(url, json=body, headers={"User-Agent": LOCAL_UA, "Accept": "application/json, text/event-stream"}, timeout=20)
    except httpx.HTTPError as e:
        raise AgentError(f"connector_error: {type(e).__name__}") from e
    if notify:
        return None
    if r.status_code != 200:
        raise AgentError(f"connector_error: status {r.status_code}")
    data = r.json()
    if "error" in data:
        raise AgentError(f"connector_error: {data['error'].get('message')}")
    return data["result"]


def openai_mcp_agent(cfg: dict, http: httpx.Client | None = None) -> Agent:
    """Local Qwen with a small MCP client (ADR-018). Same history limits as structured-v1."""
    http = http or httpx.Client()
    base = cfg["base_url"].rstrip("/")

    def run(question: str, start_url: str, seed: int | None, budget: Budget) -> AgentResult:
        _mcp_post(http, start_url, "initialize", {"protocolVersion": MCP_PROTOCOL, "capabilities": {}, "clientInfo": {"name": "geobench", "version": "0.1"}})
        _mcp_post(http, start_url, "notifications/initialized", notify=True)
        listed = _mcp_post(http, start_url, "tools/list", {})["tools"]
        tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["inputSchema"]}} for t in listed]
        messages = [{"role": "user", "content": MCP_PROMPT.format(start_url=start_url, question=question)}]
        deadline = time.monotonic() + budget.max_seconds
        calls_made = 0
        empty_retries = 0
        tokens = {"in": 0, "out": 0}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BudgetExceeded("max_seconds")
            body = {"model": cfg["model"], "messages": _bounded_messages(messages, cfg.get("tool_history_char_budget", TOOL_HISTORY_CHAR_BUDGET)),
                    "tools": tools, "temperature": cfg.get("temperature", 0.7)}
            if seed is not None:
                body["seed"] = seed
            try:
                r = http.post(f"{base}/chat/completions", json=body, timeout=remaining)
                r.raise_for_status()
            except httpx.TimeoutException as e:
                raise BudgetExceeded("max_seconds") from e
            except httpx.HTTPStatusError as e:
                if _is_context_overflow(e.response):
                    raise AgentError(CONTEXT_OVERFLOW) from e
                raise AgentError(f"{type(e).__name__}: {e}") from e
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
                if not (message.get("content") or "").strip() and empty_retries < cfg.get("empty_reply_retries", 0):
                    empty_retries += 1
                    messages.append({"role": "user", "content": EMPTY_NUDGE})
                    continue
                return AgentResult(message.get("content") or "", tokens, empty_retries, [])
            for call in calls:
                if calls_made >= budget.max_requests:
                    raise BudgetExceeded("max_requests")
                calls_made += 1
                try:
                    arguments = json.loads(call["function"]["arguments"] or "{}")
                    result = _mcp_post(http, start_url, "tools/call", {"name": call["function"]["name"], "arguments": arguments})
                    content = "".join(c.get("text", "") for c in result["content"])
                except (KeyError, ValueError, TypeError):
                    content = "Invalid arguments"
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})

    return run


def _is_context_overflow(response: httpx.Response) -> bool:
    """llama-server answers 400 with error type exceed_context_size_error when the prompt does not fit."""
    if response.status_code != 400:
        return False
    text = response.text.lower()
    return "exceed_context_size" in text or "context size" in text


def _refusal(response) -> AgentError:
    details = getattr(response, "stop_details", None)
    category = details.get("category") if isinstance(details, dict) else getattr(details, "category", None)
    return AgentError(f"refusal: {category}" if category else "refusal")


def claude_tools_agent(cfg: dict, client: anthropic.Anthropic | None = None, http: httpx.Client | None = None) -> Agent:
    client = client or anthropic.Anthropic(max_retries=3)
    http = http or (httpx.Client(auth=AccountAuth(cfg["signing"], "granted", cfg["signature_agent"], LOCAL_UA)) if cfg.get("signing") else httpx.Client())  # a signed arm is a verified agent to the geo-l interceptor (ADR-028)
    fn = FETCH_TOOL["function"]
    tools = [{"name": fn["name"], "description": fn["description"], "input_schema": fn["parameters"]}]

    def run(question: str, start_url: str, seed: int | None, budget: Budget) -> AgentResult:
        messages = [{"role": "user", "content": PROMPT.format(start_url=start_url, question=question)}]
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
            except anthropic.BadRequestError as e:
                if "prompt is too long" in str(e).lower():  # raw pages filled the context window
                    raise AgentError(CONTEXT_OVERFLOW) from e
                raise AgentError(f"{type(e).__name__}: {e}") from e
            except anthropic.APIError as e:
                raise AgentError(f"{type(e).__name__}: {e}") from e
            tokens["in"] += response.usage.input_tokens
            tokens["out"] += response.usage.output_tokens
            if response.stop_reason == "refusal":
                raise _refusal(response)
            calls = [b for b in response.content if b.type == "tool_use"]
            if not calls:
                return AgentResult("".join(b.text for b in response.content if b.type == "text"), tokens, 0, fetched)
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for call in calls:
                if len(fetched) >= budget.max_requests:
                    raise BudgetExceeded("max_requests")
                url = call.input.get("url") if isinstance(call.input, dict) else None
                if not isinstance(url, str):
                    results.append({"type": "tool_result", "tool_use_id": call.id, "content": "Invalid arguments"})
                    continue
                fetched.append(url)
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": fetch_url(http, url, cfg.get("obey_robots", False))})
            messages.append({"role": "user", "content": results})

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
                raise _refusal(response)
            return AgentResult("".join(b.text for b in response.content if b.type == "text"), tokens, 0, fetched)

    return run


def _oauth_toolbox(cfg: dict) -> OAuthToolbox:
    return OAuthToolbox(LOCAL_UA, cfg["signature_agent"])


def openai_oauth_agent(cfg: dict, http: httpx.Client | None = None, toolbox: OAuthToolbox | None = None) -> Agent:
    """Local Qwen runs OAuth by itself with an HTTP tool, a PKCE helper and ask_user (ADR-027)."""
    http = http or httpx.Client()
    base = cfg["base_url"].rstrip("/")
    tools = [{"type": "function", "function": {"name": n, "description": d, "parameters": sch}} for n, d, sch in OAUTH_TOOL_DEFS]
    state = {"toolbox": toolbox, "consent": "granted"}

    def configure(cell: dict) -> None:
        state["consent"] = cell.get("consent", "granted")

    def run(question: str, start_url: str, seed: int | None, budget: Budget) -> AgentResult:
        box = state["toolbox"] = state["toolbox"] or _oauth_toolbox(cfg)
        box.start(start_url, state["consent"])
        messages = [{"role": "user", "content": OAUTH_PROMPT.format(start_url=start_url, question=question)}]
        deadline = time.monotonic() + budget.max_seconds
        calls_made = 0
        empty_retries = 0
        tokens = {"in": 0, "out": 0}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BudgetExceeded("max_seconds")
            body = {"model": cfg["model"], "messages": _bounded_messages(messages, cfg.get("tool_history_char_budget", TOOL_HISTORY_CHAR_BUDGET)),
                    "tools": tools, "temperature": cfg.get("temperature", 0.7)}
            if seed is not None:
                body["seed"] = seed
            try:
                r = http.post(f"{base}/chat/completions", json=body, timeout=remaining)
                r.raise_for_status()
            except httpx.TimeoutException as e:
                raise BudgetExceeded("max_seconds") from e
            except httpx.HTTPStatusError as e:
                if _is_context_overflow(e.response):
                    raise AgentError(CONTEXT_OVERFLOW) from e
                raise AgentError(f"{type(e).__name__}: {e}") from e
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
                if not (message.get("content") or "").strip() and empty_retries < cfg.get("empty_reply_retries", 0):
                    empty_retries += 1
                    messages.append({"role": "user", "content": EMPTY_NUDGE})
                    continue
                return AgentResult(message.get("content") or "", tokens, empty_retries, [])
            for call in calls:
                if calls_made >= budget.max_requests:
                    raise BudgetExceeded("max_requests")
                calls_made += 1
                try:
                    content = box.call(call["function"]["name"], json.loads(call["function"]["arguments"] or "{}"))
                except (KeyError, ValueError, TypeError):
                    content = "Invalid arguments"
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})

    run.configure = configure
    return run


def claude_oauth_agent(cfg: dict, client: anthropic.Anthropic | None = None, toolbox: OAuthToolbox | None = None) -> Agent:
    """Claude runs OAuth by itself with the same three client tools (ADR-027)."""
    client = client or anthropic.Anthropic(max_retries=3)
    tools = [{"name": n, "description": d, "input_schema": sch} for n, d, sch in OAUTH_TOOL_DEFS]
    state = {"toolbox": toolbox, "consent": "granted"}

    def configure(cell: dict) -> None:
        state["consent"] = cell.get("consent", "granted")

    def run(question: str, start_url: str, seed: int | None, budget: Budget) -> AgentResult:
        box = state["toolbox"] = state["toolbox"] or _oauth_toolbox(cfg)
        box.start(start_url, state["consent"])
        messages = [{"role": "user", "content": OAUTH_PROMPT.format(start_url=start_url, question=question)}]
        deadline = time.monotonic() + budget.max_seconds
        calls_made = 0
        tokens = {"in": 0, "out": 0}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BudgetExceeded("max_seconds")
            try:
                response = client.with_options(timeout=remaining).messages.create(
                    model=cfg["model"], max_tokens=16000, tools=tools, messages=messages)
            except anthropic.APITimeoutError as e:
                raise BudgetExceeded("max_seconds") from e
            except anthropic.APIError as e:
                raise AgentError(f"{type(e).__name__}: {e}") from e
            tokens["in"] += response.usage.input_tokens
            tokens["out"] += response.usage.output_tokens
            if response.stop_reason == "refusal":
                raise _refusal(response)
            calls = [b for b in response.content if b.type == "tool_use"]
            if not calls:
                return AgentResult("".join(b.text for b in response.content if b.type == "text"), tokens, 0, [])
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for call in calls:
                if calls_made >= budget.max_requests:
                    raise BudgetExceeded("max_requests")
                calls_made += 1
                content = box.call(call.name, call.input if isinstance(call.input, dict) else {})
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": content})
            messages.append({"role": "user", "content": results})

    run.configure = configure
    return run


def claude_mcp_agent(cfg: dict, client: anthropic.Anthropic | None = None) -> Agent:
    """Claude through the API's MCP connector. Anthropic's servers call our MCP server (ADR-018)."""
    client = client or anthropic.Anthropic(max_retries=3)

    def run(question: str, start_url: str, seed: int | None, budget: Budget) -> AgentResult:
        messages = [{"role": "user", "content": MCP_PROMPT.format(start_url=start_url, question=question)}]
        servers = [{"type": "url", "url": start_url, "name": "fernwick"}]
        tools = [{"type": "mcp_toolset", "mcp_server_name": "fernwick"}]
        deadline = time.monotonic() + budget.max_seconds
        tokens = {"in": 0, "out": 0}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BudgetExceeded("max_seconds")
            try:
                response = client.with_options(timeout=remaining).beta.messages.create(
                    model=cfg["model"], max_tokens=16000, messages=messages, mcp_servers=servers, tools=tools, betas=[MCP_BETA])
            except anthropic.APITimeoutError as e:
                raise BudgetExceeded("max_seconds") from e
            except anthropic.BadRequestError as e:  # the connector could not reach or use our server
                raise AgentError(f"connector_error: {e}") from e
            except anthropic.APIError as e:
                raise AgentError(f"{type(e).__name__}: {e}") from e
            tokens["in"] += response.usage.input_tokens
            tokens["out"] += response.usage.output_tokens
            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue
            if response.stop_reason == "refusal":
                raise _refusal(response)
            return AgentResult("".join(b.text for b in response.content if b.type == "text"), tokens, 0, [])

    return run


KINDS = {"claude": claude_agent, "claude_tools": claude_tools_agent, "claude_mcp": claude_mcp_agent, "claude_oauth": claude_oauth_agent,
         "openai_compat": openai_compat_agent, "openai_mcp": openai_mcp_agent, "openai_oauth": openai_oauth_agent}


def make_agent(cfg: dict) -> Agent:
    return KINDS[cfg["kind"]](cfg)


def describe(cfg: dict) -> dict:
    """Run-record fields that identify the agent."""
    if cfg["kind"] == "claude":
        return {"agent_version": cfg["model"], "model_file": None, "runtime": "anthropic-api", "harness": None}
    if cfg["kind"] == "claude_tools":
        return {"agent_version": cfg["model"], "model_file": None, "runtime": "anthropic-api",
                "harness": {"obey_robots": cfg.get("obey_robots", False), "user_agent": LOCAL_UA, "temperature": None}}
    if cfg["kind"] == "claude_oauth":
        return {"agent_version": cfg["model"], "model_file": None, "runtime": "anthropic-api",
                "harness": {"tools": [n for n, _, _ in OAUTH_TOOL_DEFS], "user_agent": LOCAL_UA, "signature_draft": DRAFT, "temperature": None}}
    if cfg["kind"] == "claude_mcp":
        return {"agent_version": cfg["model"], "model_file": None, "runtime": "anthropic-api",
                "harness": {"mcp_connector_beta": MCP_BETA, "user_agent": "Anthropic connector", "temperature": None}}
    harness = {"obey_robots": cfg.get("obey_robots", False), "user_agent": LOCAL_UA, "temperature": cfg.get("temperature", 0.7)}
    if cfg["kind"] == "openai_oauth":
        harness |= {"tools": [n for n, _, _ in OAUTH_TOOL_DEFS], "signature_draft": DRAFT, "tool_history_char_budget": cfg.get("tool_history_char_budget", TOOL_HISTORY_CHAR_BUDGET),
                    "empty_reply_retries": cfg.get("empty_reply_retries", 0)}
    if cfg["kind"] == "openai_mcp":
        harness |= {"mcp_protocol": MCP_PROTOCOL, "tool_history_char_budget": cfg.get("tool_history_char_budget", TOOL_HISTORY_CHAR_BUDGET),
                    "empty_reply_retries": cfg.get("empty_reply_retries", 0)}
    if cfg.get("signing"):
        harness |= {"signature_draft": DRAFT, "signature_agent": cfg["signature_agent"]}
    if cfg.get("navigation_policy"):
        harness |= {
            "navigation_policy": cfg["navigation_policy"],
            "fetch_limit": cfg.get("fetch_limit", STRUCTURED_FETCH_LIMIT),
            "tool_history_char_budget": cfg.get("tool_history_char_budget", TOOL_HISTORY_CHAR_BUDGET),
            "link_limit": cfg.get("link_limit", STRUCTURED_LINK_LIMIT),
            "early_answer_retries": cfg.get("early_answer_retries", 1),
        }
    if cfg.get("server_args"):
        harness["server_args"] = cfg["server_args"]
    return {"agent_version": cfg["model"], "model_file": cfg["model_file"], "runtime": cfg["runtime"], "harness": harness}
