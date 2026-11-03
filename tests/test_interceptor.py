import json
from pathlib import Path

from starlette.testclient import TestClient

from geobench.interceptor import RequestInfo, decide, load_reference
from geobench.server import make_app

REF = {"claude": {"ua_tokens": ["Claude-User"], "ip_prefixes": ["203.0.113."]}}


def req(ua, ip="203.0.113.9"):
    return RequestInfo("legacy.example.test", "/", "GET", ua, ip, {})


def test_verified_agent_is_allowed_in_both_modes():
    for mode in ("record", "enforce"):
        d = decide(req("Claude-User/1.0"), mode, REF)
        assert (d.action, d.klass, d.agent) == ("allow", "verified_agent", "claude")


def test_claimed_agent_with_wrong_ip_is_blocked_only_in_enforce():
    assert decide(req("Claude-User/1.0", "198.51.100.1"), "record", REF).action == "allow"
    d = decide(req("Claude-User/1.0", "198.51.100.1"), "enforce", REF)
    assert (d.action, d.klass) == ("block", "claimed_agent")


def test_agent_token_without_reference_counts_as_claimed():
    d = decide(req("GPTBot/1.1"), "enforce", {})
    assert (d.action, d.klass) == ("block", "claimed_agent")


def test_plain_clients_are_unknown_and_allowed():
    for ua in ("curl/8.7.1", "Mozilla/5.0 Chrome/126", ""):
        d = decide(req(ua), "enforce", REF)
        assert (d.action, d.klass) == ("allow", "unknown")


def test_decide_never_raises():
    d = decide(req("Claude-User/1.0"), "enforce", {"claude": {}})
    assert d.action == "allow" and d.reason.startswith("error:")


def test_load_reference_missing_file_is_empty(tmp_path):
    assert load_reference(tmp_path / "none.json") == {}
    path = tmp_path / "ref.json"
    path.write_text(json.dumps(REF))
    assert load_reference(path) == REF


def client(tmp_path: Path, mode: str, reference: dict | None):
    log = tmp_path / "requests.jsonl"
    app = make_app(log, mode=mode, reference=reference, admin_ips=frozenset({"testclient"}))
    return TestClient(app, base_url="http://legacy.example.test"), log


def test_enforce_blocks_unverified_agent_and_logs_it(tmp_path):
    c, log = client(tmp_path, "enforce", {})
    assert c.get("/", headers={"User-Agent": "Claude-User/1.0"}).status_code == 403
    line = json.loads(log.read_text())
    assert line["interceptor"] == {"mode": "enforce", "class": "claimed_agent", "decision": "block", "agent": None}
    assert line["status"] == 403 and line["kind"] == "page"


def test_enforce_allows_verified_agent(tmp_path):
    ref = {"claude": {"ua_tokens": ["Claude-User"], "ip_prefixes": ["testclient"]}}
    c, log = client(tmp_path, "enforce", ref)
    assert c.get("/", headers={"User-Agent": "Claude-User/1.0"}).status_code == 200
    assert json.loads(log.read_text())["interceptor"]["class"] == "verified_agent"


def test_record_mode_labels_but_never_blocks(tmp_path):
    c, log = client(tmp_path, "record", {})
    assert c.get("/", headers={"User-Agent": "GPTBot/1.1"}).status_code == 200
    assert json.loads(log.read_text())["interceptor"]["class"] == "claimed_agent"


def test_health_reports_mode(tmp_path):
    c, _ = client(tmp_path, "enforce", {})
    assert c.get("/__health").json() == {"ok": True, "mode": "enforce"}
