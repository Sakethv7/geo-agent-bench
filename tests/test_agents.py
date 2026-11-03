import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from geobench.agents import (
    AgentError, Budget, BudgetExceeded, LOCAL_UA, claude_agent, describe, fetch_url, openai_compat_agent,
)

CFG = {"kind": "openai_compat", "base_url": "http://llm.test/v1", "model": "qwen-x", "model_file": "qwen-x-4bit",
       "runtime": "lmstudio-mlx", "temperature": 0.7}


def tool_call(url, call_id="c1"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "fetch_url", "arguments": json.dumps({"url": url})}}]}


def llm_client(replies, seen):
    """A client that answers LLM calls from `replies` and page fetches with a fixed body."""
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            seen["bodies"].append(json.loads(request.content))
            message = queue.pop(0) if len(queue) > 1 else queue[0]
            return httpx.Response(200, json={"choices": [{"message": message}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        seen["fetch"].append((str(request.url), request.headers.get("user-agent")))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return httpx.Response(200, text="<p>Maximum operating temperature: 85 C</p>")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_local_loop_fetches_then_answers():
    seen = {"bodies": [], "fetch": []}
    http = llm_client([tool_call("http://legacy.test/products"), {"role": "assistant", "content": "ANSWER: 85 C"}], seen)
    result = openai_compat_agent(CFG, http)("What?", "http://legacy.test/", 3, Budget())
    assert result.text == "ANSWER: 85 C"
    assert result.claimed_fetches == ["http://legacy.test/products"]
    assert result.api_tokens == {"in": 20, "out": 10}
    assert seen["fetch"] == [("http://legacy.test/products", LOCAL_UA)]
    assert seen["bodies"][0]["seed"] == 3 and seen["bodies"][0]["temperature"] == 0.7
    assert "Maximum operating" in seen["bodies"][1]["messages"][-1]["content"]


def test_local_loop_stops_at_max_requests():
    seen = {"bodies": [], "fetch": []}
    http = llm_client([tool_call("http://legacy.test/x")], seen)  # always asks for another fetch
    with pytest.raises(BudgetExceeded):
        openai_compat_agent(CFG, http)("What?", "http://legacy.test/", None, Budget(max_requests=2))
    assert len(seen["fetch"]) == 2


def test_local_loop_wraps_llm_http_errors():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(AgentError):
        openai_compat_agent(CFG, http)("What?", "http://legacy.test/", None, Budget())


def test_local_loop_survives_bad_tool_arguments():
    bad = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "fetch_url", "arguments": "not json"}}]}
    seen = {"bodies": [], "fetch": []}
    http = llm_client([bad, {"role": "assistant", "content": "ANSWER: x"}], seen)
    result = openai_compat_agent(CFG, http)("What?", "http://legacy.test/", None, Budget())
    assert result.text == "ANSWER: x" and result.claimed_fetches == [] and seen["fetch"] == []


def test_fetch_url_rules():
    seen = {"bodies": [], "fetch": []}
    http = llm_client([{"content": ""}], seen)
    assert fetch_url(http, "ftp://x/y") == "Invalid URL"
    assert "85 C" in fetch_url(http, "http://legacy.test/private/archive/")  # robots off by default
    assert fetch_url(http, "http://legacy.test/private/archive/", obey_robots=True) == "Blocked by robots.txt"
    assert "85 C" in fetch_url(http, "http://legacy.test/products", obey_robots=True)
    not_found = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert fetch_url(not_found, "http://legacy.test/x") == "HTTP 404"


def test_describe_marks_local_harness_and_claude():
    assert describe(CFG)["harness"]["obey_robots"] is False and describe(CFG)["model_file"] == "qwen-x-4bit"
    assert describe({"kind": "claude", "model": "claude-opus-5-5"})["harness"] is None


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = self

    def with_options(self, **kw):
        return self

    def create(self, **kw):
        self.calls.append(kw)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def response(stop, blocks, tin=100, tout=20):
    return SimpleNamespace(stop_reason=stop, content=blocks, usage=SimpleNamespace(input_tokens=tin, output_tokens=tout))


FETCH = SimpleNamespace(type="server_tool_use", name="web_fetch", input={"url": "https://legacy.example.test/products"})
TEXT = SimpleNamespace(type="text", text="ANSWER: 85 C")


def test_claude_resumes_after_pause_turn_and_sums_tokens():
    fake = FakeClient([response("pause_turn", [FETCH]), response("end_turn", [TEXT])])
    result = claude_agent({"model": "claude-opus-5-5"}, fake)("What?", "https://legacy.example.test/", None, Budget(max_requests=7))
    assert result.text == "ANSWER: 85 C" and result.api_tokens == {"in": 200, "out": 40}
    assert result.claimed_fetches == ["https://legacy.example.test/products"]
    assert fake.calls[0]["tools"] == [{"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 7}]
    assert fake.calls[1]["messages"][-1]["role"] == "assistant"


def test_claude_refusal_and_api_errors_become_agent_errors():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    run = lambda fake: claude_agent({"model": "m"}, fake)("What?", "https://x/", None, Budget())
    with pytest.raises(AgentError, match="refusal"):
        run(FakeClient([response("refusal", [])]))
    with pytest.raises(AgentError):
        run(FakeClient([anthropic.APIConnectionError(request=request)]))
    with pytest.raises(BudgetExceeded):
        run(FakeClient([anthropic.APITimeoutError(request=request)]))


def test_claude_refusal_message_carries_the_category_when_the_api_gives_one():
    run = lambda fake: claude_agent({"model": "m"}, fake)("What?", "https://x/", None, Budget())
    as_object = response("refusal", [])
    as_object.stop_details = SimpleNamespace(category="cyber")
    with pytest.raises(AgentError) as e:
        run(FakeClient([as_object]))
    assert str(e.value) == "refusal: cyber"
    as_dict = response("refusal", [])
    as_dict.stop_details = {"category": "bio"}
    with pytest.raises(AgentError) as e:
        run(FakeClient([as_dict]))
    assert str(e.value) == "refusal: bio"
    with pytest.raises(AgentError) as e:
        run(FakeClient([response("refusal", [])]))
    assert str(e.value) == "refusal"
