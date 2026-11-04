"""Identity probe. Ask one agent to read two probe pages and record what the
test site saw: the real user agent and source IP. Two prompts make two
windows. A repeat of /probe/a in the second window that never reaches the
server means the agent's fetch tool used a cache."""
import json
import os
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from geobench.agents import Agent, AgentError, Budget
from geobench.parse_logs import load_jsonl, parse_ts
from geobench.runner import host_mode, iso, new_token, precheck, run_base, server_clock

QUESTIONS = [
    "What is the code shown on the page {base}/probe/a ?",
    "What are the codes shown on the pages {base}/probe/a and {base}/probe/b ?",
]


def ip_prefix(ip: str) -> str:
    if ":" in ip:
        return ":".join(ip.split(":")[:4])
    return ".".join(ip.split(".")[:3]) + "."


def pull_log(cmd: str | None) -> None:
    """Copy the request log from the host. cmd is an argument list as one string, run without a shell.
    $VARS in it come from the environment, so a plan names the VM as $GEOBENCH_VM and holds no real host name."""
    if cmd:
        subprocess.run(os.path.expandvars(cmd).split(), check=True)


def analyze(windows: list[tuple[str, str]], requests: list[dict], claimed: list[list[str]]) -> dict:
    """windows are (start, end) in server time, one per prompt."""
    hits, bounds = [], []
    for start, end in windows:
        lo, hi = parse_ts(start), parse_ts(end) + timedelta(seconds=30)
        bounds.append((lo, hi))
        hits.append([r for r in requests if r["kind"] == "probe" and lo <= parse_ts(r["ts"]) <= hi])
    all_hits = [r for window in hits for r in window]
    second_paths = {r["path"] for r in hits[1]}
    claimed_second_a = any(u.endswith("/probe/a") for u in claimed[1])
    window_any = [[r for r in requests if lo_hi[0] <= parse_ts(r["ts"]) <= lo_hi[1]] for lo_hi in bounds]
    return {
        "requests": len(all_hits),
        "robots_read": any(r["path"] == "/robots.txt" for window in window_any for r in window),
        "fetch_observed": bool(all_hits),
        "user_agents": sorted({r["ua"] or "" for r in all_hits}),
        "ips": sorted({r["ip"] for r in all_hits}),
        "hits_per_window": [[r["path"] for r in window] for window in hits],
        "cache_suspected": claimed_second_a and "/probe/a" not in second_paths,
    }


def run_probe(
    name: str, plan: dict, agent: Agent, http: httpx.Client, requests_path: Path, reference_path: Path,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    # One token for the whole probe: the second question repeats /probe/a, so the host must stay the same.
    per_run = host_mode(plan) == "per_run"
    base = run_base(plan, plan["variants"][0], new_token(set()) if per_run else None)
    if per_run:
        precheck(http, base)
    windows, claimed, answers = [], [], []
    for question in QUESTIONS:
        offset, _ = server_clock(http, base)
        start = datetime.now(timezone.utc) + timedelta(milliseconds=offset)
        try:
            result = agent(question.format(base=base), f"{base}/", None, Budget(**plan["budget"]))
            claimed.append(result.claimed_fetches)
            answers.append(result.text)
        except AgentError as e:
            claimed.append([])
            answers.append(f"error: {e}")
        windows.append((iso(start), iso(datetime.now(timezone.utc) + timedelta(milliseconds=offset))))
        sleep(plan["quiet_gap_seconds"])
    pull_log(plan.get("pull_cmd"))
    report = analyze(windows, load_jsonl(requests_path) if requests_path.exists() else [], claimed) | {"agent": name, "answers": answers, "windows": windows}
    if report["fetch_observed"]:
        reference = json.loads(reference_path.read_text()) if reference_path.exists() else {}
        reference[name] = {"ua_tokens": report["user_agents"], "ip_prefixes": sorted({ip_prefix(i) for i in report["ips"]})}
        reference_path.write_text(json.dumps(reference, indent=2))
    reference_path.with_name(f"probe_{name}.json").write_text(json.dumps(report, indent=2))
    return report
