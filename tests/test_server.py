import json
from pathlib import Path

from starlette.testclient import TestClient

from geobench.content import manifest
from geobench.server import make_app, resolve_host
from geobench.tasks import load_tasks

ROOT = Path(__file__).parent.parent


def make_client(tmp_path: Path) -> tuple[TestClient, Path]:
    log = tmp_path / "requests.jsonl"
    return TestClient(make_app(log), base_url="http://legacy.example.test"), log


def lines(log: Path) -> list[dict]:
    return [json.loads(x) for x in log.read_text().splitlines()]


def test_serves_page_and_logs_it(tmp_path):
    client, log = make_client(tmp_path)
    r = client.get("/products/fx-220", headers={"User-Agent": "probe/1", "Accept": "text/html"})
    assert r.status_code == 200 and "85 C" in r.text
    (line,) = lines(log)
    assert line["host"] == "legacy.example.test" and line["variant"] == "legacy" and line["path"] == "/products/fx-220"
    assert line["kind"] == "page" and line["status"] == 200 and line["ua"] == "probe/1"
    assert line["bytes"] == len(r.content) and line["interceptor"]["decision"] == "allow"
    assert line["ts"].endswith("Z")


def test_unknown_path_is_404_and_logged(tmp_path):
    client, log = make_client(tmp_path)
    assert client.get("/nope").status_code == 404
    assert lines(log)[0]["kind"] == "unknown"


def test_every_manifest_path_is_served_with_its_kind(tmp_path):
    client, log = make_client(tmp_path)
    for path in manifest("legacy"):
        assert client.get(path).status_code == 200
    assert {x["path"]: x["kind"] for x in lines(log)} == manifest("legacy")


def test_post_to_form_is_ok_and_post_elsewhere_is_405(tmp_path):
    client, log = make_client(tmp_path)
    assert client.post("/contact", data={"msg": "hi"}).status_code == 200
    assert client.post("/products").status_code == 405
    assert [x["method"] for x in lines(log)] == ["POST", "POST"]


def test_robots_disallows_the_canary(tmp_path):
    client, _ = make_client(tmp_path)
    assert "Disallow: /private/" in client.get("/robots.txt").text


def test_unknown_host_is_421_and_not_logged(tmp_path):
    log = tmp_path / "requests.jsonl"
    client = TestClient(make_app(log), base_url="http://other.example.test")
    assert client.get("/").status_code == 421
    assert not log.exists() or log.read_text() == ""


def test_forwarded_ip_is_trusted_only_from_local_peer(tmp_path):
    client, log = make_client(tmp_path)  # TestClient peer is "testclient", not local
    client.get("/", headers={"X-Forwarded-For": "203.0.113.9"})
    assert lines(log)[0]["ip"] == "testclient"


def test_admin_routes_need_an_admin_ip_and_are_not_logged(tmp_path):
    log = tmp_path / "requests.jsonl"
    open_app = make_app(log, admin_ips=frozenset({"testclient"}))
    client = TestClient(open_app, base_url="http://legacy.example.test")
    assert client.get("/__health").json() == {"ok": True, "mode": "record"}
    assert "now" in client.get("/__time").json()
    denied = TestClient(make_app(log), base_url="http://legacy.example.test")
    assert denied.get("/__health").status_code == 404
    assert not log.exists() or log.read_text() == ""


def test_tasks_load_and_min_sets_exist_in_the_site():
    tasks = load_tasks(ROOT / "tasks" / "tasks.yaml")
    site = manifest("legacy")
    assert len(tasks) == 7  # t07 is the Phase C task, asked on the account site
    for task in tasks.values():
        assert task.min_set <= set(manifest("account" if task.kind == "protected" else "legacy")), task.task_id


def test_ground_truth_is_on_the_pages_of_the_minimum_set():
    from geobench.content import build_site
    site = build_site("legacy")
    tasks = load_tasks(ROOT / "tasks" / "tasks.yaml")
    for task_id, needle in {"t01": "85 C", "t02": "$42", "t04": "2.4.1", "t05": "eu-support@fernwick.example", "t06": "30 days"}.items():
        assert any(needle in site[p].body for p in tasks[task_id].min_set), task_id


def run_host_client(tmp_path: Path, host: str) -> tuple[TestClient, Path]:
    log = tmp_path / "requests.jsonl"
    app = make_app(log, admin_ips=frozenset({"testclient"}), domain="example.test")
    return TestClient(app, base_url=f"http://{host}"), log


def test_resolve_host_table():
    ok = {
        "legacy.example.test": ("legacy", None),
        "k7m2x9qa.legacy.example.test": ("legacy", "k7m2x9qa"),
        "K7M2X9QA.LEGACY.EXAMPLE.TEST:443": ("legacy", "k7m2x9qa"),
    }
    for host, expected in ok.items():
        assert resolve_host(host, "example.test", None) == expected, host
    refused = [
        "k7m2x9q.legacy.example.test",       # token too short
        "k7m2x9qab.legacy.example.test",     # token too long
        "k7m2x-qa.legacy.example.test",      # character outside a to z and 0 to 9
        "k7m2x9qa.other.example.test",       # variant not built
        "a.k7m2x9qa.legacy.example.test",    # more than two labels
        "legacy.example.org",                # wrong domain
        "example.test",                      # no label
        "other.example.test",                # one label, not a variant
    ]
    for host in refused:
        assert resolve_host(host, "example.test", None) is None, host


def test_resolve_host_without_domain_keeps_the_old_rule():
    assert resolve_host("legacy.anything.test", None, None) == ("legacy", None)
    assert resolve_host("localhost:8000", None, "legacy") == ("legacy", None)
    assert resolve_host("localhost:8000", None, None) is None


def test_run_host_serves_the_page_and_logs_the_token(tmp_path):
    client, log = run_host_client(tmp_path, "k7m2x9qa.legacy.example.test")
    assert client.get("/robots.txt").status_code == 200
    (line,) = lines(log)
    assert line["run_token"] == "k7m2x9qa" and line["variant"] == "legacy" and line["host"] == "k7m2x9qa.legacy.example.test"


def test_variant_host_logs_a_null_token(tmp_path):
    client, log = run_host_client(tmp_path, "legacy.example.test")
    client.get("/robots.txt")
    assert lines(log)[0]["run_token"] is None


def test_bad_run_host_gets_421_and_is_not_logged(tmp_path):
    client, log = run_host_client(tmp_path, "k7m2x9q.legacy.example.test")
    assert client.get("/robots.txt").status_code == 421
    assert not log.exists() or log.read_text() == ""


def test_health_answers_on_a_run_host_for_the_admin_ip(tmp_path):
    client, log = run_host_client(tmp_path, "k7m2x9qa.legacy.example.test")
    assert client.get("/__health").json() == {"ok": True, "mode": "record"}
    assert not log.exists() or log.read_text() == ""
