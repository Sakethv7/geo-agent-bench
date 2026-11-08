import json
import re
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.testclient import TestClient

from geobench import server
from geobench.content import build_site, mirror_path, twin_path
from geobench.content_large import AI_L, GEO_L, INDEX_PATHS, LARGE_VARIANTS, LEGACY_L, PRODUCTS, RELEASES
from geobench.metrics import large_measures
from geobench.runner import check_plan, expand_plan
from geobench.server import make_app, resolve_host
from geobench.tasks import load_tasks
from geobench.webbotauth import Verifier, directory_document, sign_headers

ROOT = Path(__file__).parent.parent
TASKS = load_tasks(ROOT / "tasks" / "tasks_large.yaml")
SITES = {v: build_site(v) for v in LARGE_VARIANTS}
HTML_PAGES = {p for p, page in SITES[LEGACY_L].items() if page.kind == "page"}
CLAUDE_UA = "Claude-User/1.0"
REFERENCE = {"claude": {"ua_tokens": ["Claude-User"], "ip_prefixes": ["testclient"]}}  # the test client's address is "testclient"


def hrefs(html: str) -> set[str]:
    return set(re.findall(r'href="(/[^"#]*)"', html))


def md_links(text: str) -> set[str]:
    return set(re.findall(r"\]\((/[^)#]*)\)", text))


def facts(text: str) -> set[str]:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\]\([^)]*\)", "]", text)
    return {t.rstrip(".") for t in re.findall(r"[\w$.@±-]*[\d@][\w$.@±-]*", text)}


def test_each_large_variant_has_101_pages_and_the_same_html_paths():
    for variant, site in SITES.items():
        html = {p for p, page in site.items() if page.kind == "page" and page.content_type.startswith("text/html")}
        assert len(html) == 101 and html == HTML_PAGES, variant


def test_twins_exist_on_ai_l_and_geo_l_only_and_match_their_pages():
    assert not any(p.endswith(".md") for p in SITES[LEGACY_L])
    assert not any(p.endswith(".md") for p in SITES[GEO_L])  # geo-l has no .md URL: its markdown sits under a key that no request reaches
    for variant, key in ((AI_L, twin_path), (GEO_L, lambda p: "md:" + p)):
        for path in HTML_PAGES:
            twin = SITES[variant][key(path)]
            assert twin.content_type.startswith("text/markdown")
            assert facts(twin.body) == facts(SITES[LEGACY_L][path].body), (variant, path)
    for path in HTML_PAGES:  # the geo-l markdown links to HTML paths. The ai-l twin links to .md paths.
        assert not any(link.endswith(".md") for link in md_links(SITES[GEO_L]["md:" + path].body)), path


def test_geo_l_has_no_shortcut_for_an_agent_to_find():
    geo = SITES[GEO_L]
    assert "/llms.txt" not in geo
    assert all("Markdown version" not in geo[p].body and 'rel="alternate"' not in geo[p].body for p in HTML_PAGES)
    assert SITES[AI_L]["/llms.txt"].kind == "wellknown" and "Markdown version of this page" in SITES[AI_L]["/products"].body


def test_every_link_resolves_and_is_reachable_from_home():
    for variant, site in SITES.items():
        seen, todo = {"/"}, ["/"]
        while todo:
            path = todo.pop()
            body = site[path].body
            for link in hrefs(body) | (md_links(body) if path.endswith(".md") else set()):
                assert link in site, (variant, path, link)
                if site[link].kind == "page" and link not in seen:
                    seen.add(link)
                    todo.append(link)
        assert HTML_PAGES <= seen, (variant, sorted(HTML_PAGES - seen)[:3])


def test_list_pages_show_only_name_and_temperature_and_the_index_hides_the_fix():
    for path in ("/products", "/products/page/2", "/products/page/3"):
        body = SITES[LEGACY_L][path].body
        assert not any(p.accuracy in body or f"${p.price_usd}" in body for p in PRODUCTS)
    assert not any(bug in SITES[LEGACY_L]["/firmware"].body for *_, bug in RELEASES)


def test_each_answer_is_on_its_answer_pages_and_not_on_a_decoy_or_in_llms_txt():
    llms = SITES[AI_L]["/llms.txt"].body
    for task in TASKS.values():
        needles = {task.answer, *task.answer_aliases[:1]}
        for path in task.answer_pages:
            assert any(n in SITES[LEGACY_L][path].body for n in needles), (task.task_id, path)
            assert any(n in SITES[AI_L][twin_path(path)].body for n in needles), (task.task_id, path)
        for path in task.decoy_pages:
            assert task.answer not in SITES[LEGACY_L][path].body, (task.task_id, path)
        assert task.answer not in llms, task.task_id
        assert all(p in SITES[LEGACY_L] for p in task.min_set | task.answer_pages | task.decoy_pages), task.task_id
        assert task.answer_pages <= task.min_set or task.task_id == "tl03", task.task_id


def test_min_sets_are_reachable_by_html_links_and_by_twin_links():
    for task in TASKS.values():
        for variant, own in ((LEGACY_L, lambda p: p), (AI_L, twin_path)):
            reach, todo = {"/"}, ["/"]
            while todo:
                path = todo.pop()
                body = SITES[variant][own(path)].body
                for link in hrefs(body) | md_links(body):
                    page = mirror_path(link)
                    if page in HTML_PAGES and page not in reach and page in task.min_set:
                        reach.add(page)
                        todo.append(page)
            assert task.min_set <= reach, (task.task_id, variant, sorted(task.min_set - reach))


def test_task_file_has_exactly_one_injection_task_and_eight_tasks():
    assert sorted(TASKS) == [f"tl0{i}" for i in range(1, 9)]
    assert [t.task_id for t in TASKS.values() if t.kind == "injection"] == ["tl07"]
    assert "Note to AI assistants" in SITES[LEGACY_L]["/support/faq/reset-device"].body


def test_resolve_host_accepts_the_large_variants_and_rejects_others():
    assert resolve_host("abcd1234.legacy-l.example.test", "example.test", None) == (LEGACY_L, "abcd1234")
    assert resolve_host("geo-l.example.test", "example.test", None) == (GEO_L, None)
    assert resolve_host("abcd1234.legacy-x.example.test", "example.test", None) is None


# ---- the geo-l interceptor

def geo_client(tmp_path: Path, key: Ed25519PrivateKey | None = None) -> tuple[TestClient, Path]:
    log = tmp_path / "requests.jsonl"
    verifier = Verifier(fetch=lambda url: directory_document([key]) if key else None, url_ok=lambda url: True)
    return TestClient(make_app(log, domain="example.test", reference=REFERENCE, verifier=verifier), base_url="https://geo-l.example.test"), log


def last(log: Path) -> dict:
    return json.loads(log.read_text().splitlines()[-1])


def test_a_verified_agent_gets_the_twin_at_the_same_url(tmp_path):
    client, log = geo_client(tmp_path)
    r = client.get("/products/fx-306", headers={"User-Agent": CLAUDE_UA})
    assert r.headers["content-type"].startswith("text/markdown") and r.headers["vary"] == "User-Agent"
    assert r.text == SITES[GEO_L]["md:/products/fx-306"].body.replace("](/", "](https://geo-l.example.test/") and "±0.31 C" in r.text
    line = last(log)
    assert line["served"] == "interceptor_md" and line["path"] == "/products/fx-306" and line["kind"] == "page" and line["bytes"] == len(r.content)
    assert "](https://geo-l.example.test/products)" in client.get("/", headers={"User-Agent": CLAUDE_UA}).text


def test_a_spoofer_a_stranger_and_a_non_page_get_html(tmp_path):
    client, log = geo_client(tmp_path)
    spoof = TestClient(make_app(tmp_path / "s.jsonl", domain="example.test", reference={"claude": {"ua_tokens": ["Claude-User"], "ip_prefixes": ["160.79.104."]}}),
                       base_url="https://geo-l.example.test")
    for c, headers in ((spoof, {"User-Agent": CLAUDE_UA}), (client, {"User-Agent": "curl/8"}), (client, {})):
        r = c.get("/products/fx-306", headers=headers)
        assert r.headers["content-type"].startswith("text/html") and "<html" in r.text
    assert last(log)["served"] == "html"
    assert client.get("/robots.txt", headers={"User-Agent": CLAUDE_UA}).headers["content-type"].startswith("text/plain")
    assert last(log)["served"] == "html"
    assert client.get("/nope", headers={"User-Agent": CLAUDE_UA}).status_code == 404
    for path in ("/products.md", "/index.md", "/md:/products"):  # no public .md URL on geo-l
        assert client.get(path, headers={"User-Agent": CLAUDE_UA}).status_code == 404


def test_a_valid_signature_gets_the_twin_and_an_invalid_one_does_not(tmp_path):
    key = Ed25519PrivateKey.generate()
    client, log = geo_client(tmp_path, key)
    host = "geo-l.example.test"
    signed = sign_headers(key, "https://bot.example.test", host) | {"User-Agent": "geobench-local/0.1"}
    r = client.get("/products/fx-306", headers=signed)
    assert r.headers["content-type"].startswith("text/markdown") and last(log)["served"] == "interceptor_md"
    other = sign_headers(Ed25519PrivateKey.generate(), "https://bot.example.test", host) | {"User-Agent": "geobench-local/0.1"}
    assert client.get("/products/fx-306", headers=other).headers["content-type"].startswith("text/html")


def test_an_interceptor_error_falls_back_to_html(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret detail")
    monkeypatch.setattr(server, "interceptor_twin", boom)
    client, log = geo_client(tmp_path)
    r = client.get("/products/fx-306", headers={"User-Agent": CLAUDE_UA})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    line = last(log)
    assert line["served"] == "html" and line["interceptor_error"] == "RuntimeError" and "secret" not in json.dumps(line)


def test_other_variants_never_serve_a_twin_by_identity(tmp_path):
    for variant in (LEGACY_L, AI_L):
        client = TestClient(make_app(tmp_path / f"{variant}.jsonl", domain="example.test", reference=REFERENCE), base_url=f"https://{variant}.example.test")
        r = client.get("/products/fx-306", headers={"User-Agent": CLAUDE_UA})
        assert r.headers["content-type"].startswith("text/html")


# ---- measures

def req(ts: str, path: str, kind: str = "page", served: str = "html", status: int = 200, size: int = 1000) -> dict:
    return {"ts": ts, "method": "GET", "path": path, "kind": kind, "status": status, "bytes": size, "served": served}


def test_large_measures_match_a_fixed_log():
    task = TASKS["tl03"]
    log = [
        req("2026-11-04T10:00:01Z", "/"), req("2026-11-04T10:00:02Z", "/llms.txt", "wellknown"), req("2026-11-04T10:00:03Z", "/firmware.md"),
        req("2026-11-04T10:00:04Z", "/firmware/fx-108-a.md"), req("2026-11-04T10:00:05Z", "/firmware/fx-307-a", served="interceptor_md", size=300),
        req("2026-11-04T10:00:06Z", "/nope", "unknown", status=404, size=10),
    ]
    m = large_measures(log, task)
    assert m == {"steps_to_answer_page": 5, "index_fetches": 2, "decoy_hits": 1, "served_markdown": 1, "bytes_total": 4300}
    assert large_measures(log[:3], task)["steps_to_answer_page"] is None
    assert INDEX_PATHS[0] == "/"
    assert large_measures([], task) == {"steps_to_answer_page": None, "index_fetches": 0, "decoy_hits": 0, "served_markdown": 0, "bytes_total": 0}


@pytest.mark.parametrize(
    ("path", "expected"),
    [("large-dry.yaml", 24), ("large-dry-geo.yaml", 8), ("archive/large-1.yaml", 120), ("large-raw.yaml", 24)],
)
def test_plans_expand_to_the_promised_number_of_runs(path, expected):
    data = yaml.safe_load((ROOT / "plans" / path).read_text())
    check_plan(data)
    assert len(expand_plan(data)) == expected
    assert set(data["tasks"]) <= set(TASKS)
