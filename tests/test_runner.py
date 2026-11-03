import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from geobench.agents import AgentError, AgentResult, BudgetExceeded
from geobench.metrics import build_results
from geobench.parse_logs import assign_requests, load_jsonl
from geobench.probe import analyze, ip_prefix, run_probe
from geobench import runner
from geobench.runner import (
    RunnerStop, certificate_days, check_gap, check_plan, expand_plan, local_precheck, new_token, precheck, run_plan, server_clock,
)
from geobench.server import make_app
from geobench.tasks import load_tasks

ROOT = Path(__file__).parent.parent
TASKS = load_tasks(ROOT / "tasks" / "tasks.yaml")
LOCAL_CFG = {"kind": "openai_compat", "base_url": "http://llm.test/v1", "model": "m", "model_file": "f", "runtime": "r"}


@pytest.fixture(autouse=True)
def no_local_precheck(monkeypatch, request):
    """The run-flow tests use a fake local agent with no model server. local_precheck has its own tests."""
    if "local_precheck" not in request.node.name:
        monkeypatch.setattr(runner, "local_precheck", lambda http, cfg: None)


def plan(**over):
    base = {"plan_id": "p", "domain": "example.test", "scheme": "http", "variants": ["legacy"], "agent_names": ["fake"],
            "tasks": ["t01"], "reps": 2, "order_seed": 1, "quiet_gap_seconds": 0,
            "budget": {"max_requests": 40, "max_seconds": 600}, "agents": {"fake": LOCAL_CFG}}
    return base | over


def site(tmp_path):
    log = tmp_path / "requests.jsonl"
    return TestClient(make_app(log, admin_ips=frozenset({"testclient"})), base_url="http://legacy.example.test"), log


def walker(client, paths, ua="fake/1"):
    def agent(question, start_url, seed, budget):
        for p in paths:
            client.get(p, headers={"User-Agent": ua})
        return AgentResult("ANSWER: 85 C", {"in": 1, "out": 1}, 0, [f"http://legacy.example.test{p}" for p in paths])
    return agent


def test_expand_plan_is_deterministic_and_complete():
    p = plan(agent_names=["a", "b"], tasks=["t01", "t02"], reps=3)
    runs = expand_plan(p)
    assert len(runs) == 2 * 1 * 2 * 3 and len({r["run_id"] for r in runs}) == len(runs)
    assert runs == expand_plan(p) and runs != expand_plan(plan(agent_names=["a", "b"], tasks=["t01", "t02"], reps=3, order_seed=2))


def test_check_plan_refuses_todo_in_used_values_only():
    with pytest.raises(ValueError, match="domain"):
        check_plan(plan(domain="TODO"))
    with pytest.raises(ValueError, match="fake.model"):
        check_plan(plan(agents={"fake": LOCAL_CFG | {"model": "TODO"}}))
    check_plan(plan(agents={"fake": LOCAL_CFG, "other": LOCAL_CFG | {"model": "TODO"}}))


def test_full_run_writes_records_that_the_parser_can_match(tmp_path):
    client, log = site(tmp_path)
    runs_path = tmp_path / "runs.jsonl"
    # One run only: with a quiet gap of 0 the windows of two runs would overlap, which a real plan forbids.
    done = run_plan(plan(reps=1), TASKS, runs_path, client, {"fake": walker(client, ["/", "/products", "/products/fx-220", "/warranty"])}, lambda s: None)
    assert done == 1
    runs = load_jsonl(runs_path)
    assert {r["state"] for r in runs} == {"answered"} and runs[0]["interceptor"] == "record"
    assert runs[0]["answer"] == "85 C" and runs[0]["seed"] in (1, 2) and runs[0]["agent_version"] == "m"
    row = build_results(runs, assign_requests(runs, load_jsonl(log)), TASKS).iloc[0]
    assert (row["success"], row["page_fetches"], row["overfetch_ratio"]) == (1, 4, 0.25)
    assert row["flags"] == ""


def test_rerun_resumes_and_skips_finished_runs(tmp_path):
    client, _ = site(tmp_path)
    runs_path = tmp_path / "runs.jsonl"
    agents = {"fake": walker(client, ["/"])}
    assert run_plan(plan(), TASKS, runs_path, client, agents, lambda s: None) == 2
    assert run_plan(plan(), TASKS, runs_path, client, agents, lambda s: None) == 0
    assert len(load_jsonl(runs_path)) == 2


def test_run_refuses_to_mix_plan_ids_before_calling_an_agent(tmp_path):
    client, _ = site(tmp_path)
    runs_path = tmp_path / "runs.jsonl"
    original = '{"run_id":"research-0000","plan_id":"research","run_token":"abcd1234"}\n'
    runs_path.write_text(original)
    called = []

    def agent(question, start_url, seed, budget):
        called.append(True)
        return AgentResult("ANSWER: 85 C", None, 0, [])

    with pytest.raises(ValueError, match=r"contains records from plan\(s\) research; refusing to append plan portfolio"):
        run_plan(plan(plan_id="portfolio", reps=1), TASKS, runs_path, client, {"fake": agent}, lambda s: None)
    assert called == [] and runs_path.read_text() == original


def test_failure_states_are_recorded_not_retried(tmp_path):
    client, _ = site(tmp_path)
    calls = []

    def flaky(question, start_url, seed, budget):
        calls.append(1)
        raise (BudgetExceeded("max_requests") if len(calls) == 1 else AgentError("boom"))

    runs_path = tmp_path / "runs.jsonl"
    run_plan(plan(), TASKS, runs_path, client, {"fake": flaky}, lambda s: None)
    runs = load_jsonl(runs_path)
    assert [r["state"] for r in runs] == ["timeout", "error"] and len(calls) == 2
    assert runs[1]["error"] == "boom" and runs[0]["answer"] is None
    assert build_results(runs, assign_requests(runs, []), TASKS)["success"].sum() == 0


def test_cache_suspected_when_agent_claims_a_fetch_the_server_never_saw(tmp_path):
    client, log = site(tmp_path)
    runs_path = tmp_path / "runs.jsonl"

    def liar(question, start_url, seed, budget):
        client.get("/")
        return AgentResult("ANSWER: 85 C", None, 0, ["http://legacy.example.test/", "http://legacy.example.test/products/fx-220"])

    run_plan(plan(reps=1), TASKS, runs_path, client, {"fake": liar}, lambda s: None)
    runs = load_jsonl(runs_path)
    assert "cache_suspected" in build_results(runs, assign_requests(runs, load_jsonl(log)), TASKS).iloc[0]["flags"]


def test_unreachable_site_stops_with_exit_code_3():
    down = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("no"))))
    with pytest.raises(RunnerStop) as e:
        server_clock(down, "http://legacy.example.test")
    assert e.value.code == 3


def test_clock_offset_above_two_seconds_stops_with_exit_code_4():
    def handler(request):
        if request.url.path == "/__health":
            return httpx.Response(200, json={"ok": True, "mode": "record"})
        later = datetime.now(timezone.utc) + timedelta(seconds=10)
        return httpx.Response(200, json={"now": later.isoformat().replace("+00:00", "Z")})

    with pytest.raises(RunnerStop) as e:
        server_clock(httpx.Client(transport=httpx.MockTransport(handler)), "http://legacy.example.test")
    assert e.value.code == 4


def test_ip_prefix_handles_v4_and_v6():
    assert ip_prefix("20.171.206.5") == "20.171.206."
    assert ip_prefix("2001:db8:1:2:3:4:5:6") == "2001:db8:1:2"


def req(ts, path, ua="Claude-User/1.0", ip="203.0.113.9", kind="probe"):
    return {"ts": ts, "path": path, "ua": ua, "ip": ip, "kind": kind}


W = [("2026-11-09T14:00:00.000Z", "2026-11-09T14:00:10.000Z"), ("2026-11-09T14:01:00.000Z", "2026-11-09T14:01:10.000Z")]


def test_analyze_reads_identity_and_detects_cache():
    reqs = [req("2026-11-09T14:00:05.000Z", "/probe/a"), req("2026-11-09T14:01:05.000Z", "/probe/b")]
    claimed = [["https://h/probe/a"], ["https://h/probe/a", "https://h/probe/b"]]
    out = analyze(W, reqs, claimed)
    assert out["fetch_observed"] and out["user_agents"] == ["Claude-User/1.0"] and out["ips"] == ["203.0.113.9"]
    assert out["cache_suspected"] is True
    reqs.append(req("2026-11-09T14:01:06.000Z", "/probe/a"))
    assert analyze(W, reqs, claimed)["cache_suspected"] is False


def test_analyze_reports_no_fetch():
    assert analyze(W, [req("2026-11-09T15:00:00.000Z", "/probe/a")], [[], []])["fetch_observed"] is False


def test_run_probe_writes_reference_and_report(tmp_path):
    client, log = site(tmp_path)
    ref = tmp_path / "identity_reference.json"

    def agent(question, start_url, seed, budget):
        client.get("/probe/a", headers={"User-Agent": "Claude-User/1.0"})
        return AgentResult("PROBE-A-7Q2", None, 0, ["http://legacy.example.test/probe/a"])

    report = run_probe("claude", plan(), agent, client, log, ref, lambda s: None)
    assert report["fetch_observed"] and report["cache_suspected"] is False
    saved = json.loads(ref.read_text())
    assert saved["claude"] == {"ua_tokens": ["Claude-User/1.0"], "ip_prefixes": ["testclient."]}
    assert (tmp_path / "probe_claude.json").exists()


def test_run_probe_writes_no_reference_when_nothing_was_fetched(tmp_path):
    client, log = site(tmp_path)
    ref = tmp_path / "identity_reference.json"
    report = run_probe("ghost", plan(), lambda q, u, s, b: AgentResult("I do not know", None, 0, []), client, log, ref, lambda s: None)
    assert report["fetch_observed"] is False and not ref.exists()


def test_check_gap_requires_tail_plus_pad():
    check_gap(plan(quiet_gap_seconds=32))
    with pytest.raises(ValueError, match="quiet_gap_seconds"):
        check_gap(plan(quiet_gap_seconds=30))


def per_run_site(tmp_path):
    log = tmp_path / "requests.jsonl"
    app = make_app(log, admin_ips=frozenset({"testclient"}), domain="example.test")
    return TestClient(app, base_url="http://legacy.example.test"), log


def test_per_run_mode_gives_each_run_its_own_host_and_matches_by_token(tmp_path):
    client, log = per_run_site(tmp_path)
    runs_path = tmp_path / "runs.jsonl"
    starts = []

    def agent(question, start_url, seed, budget):
        starts.append(start_url)
        client.get(start_url + "robots.txt", headers={"User-Agent": "fake/1"})
        return AgentResult("ANSWER: 85 C", None, 0, [start_url + "robots.txt"])

    # quiet gap 0: the time windows overlap, so only the token can tell the runs apart
    done = run_plan(plan(host_mode="per_run"), TASKS, runs_path, client, {"fake": agent}, lambda s: None)
    runs = load_jsonl(runs_path)
    assert done == 2 and len({r["run_token"] for r in runs}) == 2
    for run, start in zip(runs, starts):
        assert start == f"http://{run['run_token']}.legacy.example.test/" and "?r=" not in start
        assert run["run_host"] == f"{run['run_token']}.legacy.example.test"
    assignment = assign_requests(runs, load_jsonl(log))
    assert [len(assignment.by_run[r["run_id"]]) for r in runs] == [1, 1] and not assignment.unassigned


def test_shared_mode_keeps_the_old_start_url_and_null_token(tmp_path):
    client, _ = site(tmp_path)
    starts = []

    def agent(question, start_url, seed, budget):
        starts.append(start_url)
        return AgentResult("ANSWER: 85 C", None, 0, [])

    runs_path = tmp_path / "runs.jsonl"
    run_plan(plan(reps=1), TASKS, runs_path, client, {"fake": agent}, lambda s: None)
    (run,) = load_jsonl(runs_path)
    assert run["run_token"] is None and run["run_host"] is None and starts[0].endswith("/?r=p-0000")


def test_per_run_tokens_are_not_reused_across_a_resumed_batch(tmp_path):
    client, _ = per_run_site(tmp_path)
    runs_path = tmp_path / "runs.jsonl"
    agent = {"fake": lambda q, u, s, b: AgentResult("ANSWER: 85 C", None, 0, [])}
    run_plan(plan(host_mode="per_run", reps=1), TASKS, runs_path, client, agent, lambda s: None)
    run_plan(plan(host_mode="per_run", reps=2), TASKS, runs_path, client, agent, lambda s: None)
    tokens = [r["run_token"] for r in load_jsonl(runs_path)]
    assert len(tokens) == 2 and len(set(tokens)) == 2


def test_precheck_failure_stops_the_batch_and_writes_no_record(tmp_path):
    down = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("[Errno 8] nodename nor servname provided"))))
    runs_path = tmp_path / "runs.jsonl"
    with pytest.raises(RunnerStop, match="does not resolve") as e:
        run_plan(plan(host_mode="per_run"), TASKS, runs_path, down, {"fake": lambda q, u, s, b: None}, lambda s: None)
    assert e.value.code == 3 and not runs_path.exists()


def test_precheck_names_the_cause():
    def http_that(exc=None, status=200):
        def handler(request):
            if exc:
                raise exc
            return httpx.Response(status, json={"ok": True})
        return httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(RunnerStop, match="TLS check failed"):
        precheck(http_that(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] bad")), "http://t.legacy.example.test")
    with pytest.raises(RunnerStop, match="status 500"):
        precheck(http_that(status=500), "http://t.legacy.example.test")
    precheck(http_that(), "http://t.legacy.example.test")  # http: the certificate check does not apply


class FakeTls:
    def __init__(self, cert):
        self.cert = cert

    def get_extra_info(self, name):
        return self if name == "ssl_object" else None

    def getpeercert(self):
        return self.cert


def tls_client(cert):
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}, extensions={"network_stream": FakeTls(cert)})))


def test_precheck_certificate_expiry_guard():
    base = "https://t.legacy.example.test"
    precheck(tls_client({"notAfter": "Jan  1 00:00:00 2100 GMT"}), base)
    with pytest.raises(RunnerStop, match="expires in"):
        precheck(tls_client({"notAfter": "Jan  1 00:00:00 2000 GMT"}), base)
    with pytest.raises(RunnerStop, match="cannot read the certificate date"):
        precheck(tls_client({}), base)


def test_certificate_days_counts_from_the_given_time():
    response = httpx.Response(200, extensions={"network_stream": FakeTls({"notAfter": "Jan  1 00:00:00 2030 GMT"})})
    start_of_2029 = 1861920000  # 2029-01-01 00:00:00 UTC
    assert round(certificate_days(response, now=start_of_2029)) == 365
    assert certificate_days(httpx.Response(200)) is None


def test_new_token_is_8_characters_and_refuses_after_five_collisions(monkeypatch):
    token = new_token(set())
    assert len(token) == 8 and token.isalnum() and token == token.lower()
    monkeypatch.setattr("geobench.runner.secrets.choice", lambda chars: "a")
    with pytest.raises(RunnerStop, match="5 tries") as e:
        new_token({"aaaaaaaa"})
    assert e.value.code == 3


def test_check_plan_refuses_an_unknown_host_mode():
    with pytest.raises(ValueError, match="host_mode"):
        check_plan(plan(host_mode="sometimes"))
    check_plan(plan(host_mode="per_run"))


def test_run_probe_uses_one_token_host_for_both_questions(tmp_path):
    client, log = per_run_site(tmp_path)
    ref = tmp_path / "identity_reference.json"
    starts = []

    def agent(question, start_url, seed, budget):
        starts.append(start_url)
        client.get(start_url + "probe/a", headers={"User-Agent": "Claude-User/1.0"})
        return AgentResult("PROBE-A-7Q2", None, 0, [start_url + "probe/a"])

    report = run_probe("claude", plan(host_mode="per_run"), agent, client, log, ref, lambda s: None)
    assert starts[0] == starts[1] and starts[0].startswith("http://") and ".legacy.example.test/" in starts[0]
    assert report["fetch_observed"] and [x["run_token"] for x in load_jsonl(log)] == [starts[0].split("//")[1].split(".")[0]] * 2


def test_local_precheck_passes_when_the_server_serves_the_model():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"id": "m"}]})))
    local_precheck(http, LOCAL_CFG)


def test_local_precheck_stops_when_the_model_is_missing_or_the_server_is_down():
    other = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"id": "x"}]})))
    with pytest.raises(RunnerStop, match="does not serve m") as e:
        local_precheck(other, LOCAL_CFG)
    assert e.value.code == 3
    down = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused"))))
    with pytest.raises(RunnerStop, match="failed the check"):
        local_precheck(down, LOCAL_CFG)
