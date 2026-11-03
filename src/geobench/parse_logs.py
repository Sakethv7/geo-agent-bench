"""Match logged requests to runs.

A request with a run token belongs to the run that has that token, at any time. If its time is
outside the run window, the run gets the flag outside_window. A token that is in no run record
leaves the request unassigned and voids no run.

A request without a token belongs to a run that has no token (shared mode) when its time is inside
[start - pad, end + tail] and its variant (written by the server) equals the run's variant. Runs are serial,
so windows do not overlap. A run with a token never takes a tokenless request: scanners and crawlers
on the bare variant host would otherwise land in it. A request that fits no run is kept as unassigned.
If it looks like an agent, it voids the nearest earlier tokenless run on the same variant.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

TAIL = timedelta(seconds=30)
PAD = timedelta(seconds=2)  # tolerance for clock-offset jitter at the start of a run
DEFAULT_RUN_LENGTH = timedelta(seconds=600)
KNOWN_AGENT_TOKENS = (
    "claude", "anthropic", "gpt", "openai", "oai-searchbot", "gemini", "googleagent",
    "google-extended", "cursor", "perplexity", "geobench-local",
)


@dataclass
class Assignment:
    by_run: dict[str, list[dict]] = field(default_factory=dict)
    unassigned: list[dict] = field(default_factory=list)
    invalid_runs: set[str] = field(default_factory=set)
    outside_window: set[str] = field(default_factory=set)


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def looks_like_agent(ua: str | None) -> bool:
    return bool(ua) and any(token in ua.lower() for token in KNOWN_AGENT_TOKENS)


def _window(run: dict) -> tuple[datetime, datetime]:
    start = parse_ts(run["start"])
    end = parse_ts(run["end"]) if run.get("end") else start + DEFAULT_RUN_LENGTH
    return start - PAD, end + TAIL


def _find_run(runs: list[dict], request: dict) -> dict | None:
    ts = parse_ts(request["ts"])
    hits = [
        r for r in runs
        if r["variant"] == request["variant"] and _window(r)[0] <= ts <= _window(r)[1]
    ]
    return max(hits, key=lambda r: r["start"]) if hits else None


def _nearest_earlier(runs: list[dict], request: dict) -> dict | None:
    ts = parse_ts(request["ts"])
    earlier = [r for r in runs if r["variant"] == request["variant"] and parse_ts(r["start"]) <= ts]
    return max(earlier, key=lambda r: r["start"]) if earlier else None


def assign_requests(runs: list[dict], requests: list[dict]) -> Assignment:
    result = Assignment(by_run={r["run_id"]: [] for r in runs})
    by_token = {r["run_token"]: r for r in runs if r.get("run_token")}
    window_runs = [r for r in runs if not r.get("run_token")]
    for request in sorted(requests, key=lambda r: r["ts"]):
        token = request.get("run_token")
        if token:
            owner = by_token.get(token)
            if owner is None or owner["variant"] != request["variant"]:
                result.unassigned.append(request)
                continue
            result.by_run[owner["run_id"]].append(request)
            low, high = _window(owner)
            if not low <= parse_ts(request["ts"]) <= high:
                result.outside_window.add(owner["run_id"])
            continue
        run = _find_run(window_runs, request)
        if run is not None:
            result.by_run[run["run_id"]].append(request)
            continue
        result.unassigned.append(request)
        if looks_like_agent(request.get("ua")):
            nearest = _nearest_earlier(window_runs, request)
            if nearest is not None:
                result.invalid_runs.add(nearest["run_id"])
    return result
