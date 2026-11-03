"""Run a plan one run at a time. Run times are written in the server clock, so
the parser can match requests to runs without a clock correction."""
import json
import random
import secrets
import ssl
import string
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from geobench.agents import Agent, AgentError, Budget, BudgetExceeded, describe, make_agent
from geobench.metrics import final_answer
from geobench.parse_logs import PAD, TAIL, load_jsonl, parse_ts
from geobench.tasks import Task

MAX_CLOCK_OFFSET_MS = 2000
TOKEN_CHARS = string.ascii_lowercase + string.digits
TOKEN_LENGTH = 8
CERT_MIN_DAYS = 14


class RunnerStop(Exception):
    """The batch cannot continue. code is the CLI exit code."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def check_plan(plan: dict) -> None:
    """Refuse to start while any value still says TODO."""
    used = {f"{a}.{k}": v for a in plan["agent_names"] for k, v in plan["agents"][a].items()}
    todo = [k for k, v in {**plan, **used}.items() if v == "TODO"]
    if todo:
        raise ValueError(f"plan has TODO values: {', '.join(todo)}")
    if host_mode(plan) not in {"shared", "per_run"}:
        raise ValueError(f"host_mode must be shared or per_run, not {host_mode(plan)}")


def check_gap(plan: dict) -> None:
    """Run windows must not overlap: the quiet gap has to cover the tail plus the start pad."""
    minimum = (TAIL + PAD).total_seconds()
    if plan["quiet_gap_seconds"] < minimum:
        raise ValueError(f"quiet_gap_seconds {plan['quiet_gap_seconds']} is below {minimum:.0f}")


def host_mode(plan: dict) -> str:
    return plan.get("host_mode", "shared")


def base_url(plan: dict, variant: str) -> str:
    return f"{plan['scheme']}://{variant}.{plan['domain']}"


def run_base(plan: dict, variant: str, token: str | None) -> str:
    """Base URL of one run. A token gives the run its own host: <token>.<variant>.<domain>."""
    if token is None:
        return base_url(plan, variant)
    return f"{plan['scheme']}://{token}.{variant}.{plan['domain']}"


def new_token(used: set[str], tries: int = 5) -> str:
    for _ in range(tries):
        token = "".join(secrets.choice(TOKEN_CHARS) for _ in range(TOKEN_LENGTH))
        if token not in used:
            return token
    raise RunnerStop(f"could not make an unused run token in {tries} tries", 3)


def describe_failure(e: Exception) -> str:
    text = str(e).lower()
    if "certificate" in text or "ssl" in text or "tls" in text:
        return "the TLS check failed"
    if any(w in text for w in ("name or service not known", "nodename nor servname", "getaddrinfo", "name resolution")):
        return "the name does not resolve"
    return type(e).__name__


def certificate_days(response: httpx.Response, now: float | None = None) -> float | None:
    """Days until the certificate of this connection expires, or None if the connection does not say."""
    stream = response.extensions.get("network_stream")
    ssl_object = stream.get_extra_info("ssl_object") if stream else None
    cert = ssl_object.getpeercert() if ssl_object else None
    if not cert or "notAfter" not in cert:
        return None
    return (ssl.cert_time_to_seconds(cert["notAfter"]) - (time.time() if now is None else now)) / 86400


def precheck(http: httpx.Client, base: str) -> None:
    """Stop the batch if the run host does not answer, or its certificate is close to expiry."""
    host = base.split("://", 1)[1]
    try:
        response = http.get(f"{base}/__health")  # the CLI client has a 20 second timeout
    except httpx.HTTPError as e:
        raise RunnerStop(f"run host {host} failed the check: {describe_failure(e)}", 3) from e
    if response.status_code != 200:
        raise RunnerStop(f"run host {host} failed the check: status {response.status_code}", 3)
    if base.startswith("https://"):
        days = certificate_days(response)
        if days is None:
            raise RunnerStop(f"run host {host}: cannot read the certificate date", 3)
        if days < CERT_MIN_DAYS:
            raise RunnerStop(f"certificate for {host} expires in {days:.0f} days", 3)


def local_precheck(http: httpx.Client, cfg: dict) -> None:
    """Before a local run: the model server answers and serves the model in the plan (logic_flow Phase A, step 2)."""
    url = f"{cfg['base_url'].rstrip('/')}/models"
    try:
        response = http.get(url)
        response.raise_for_status()
        ids = {m.get("id") for m in response.json().get("data", [])}
    except (httpx.HTTPError, ValueError) as e:
        raise RunnerStop(f"local model server failed the check: {describe_failure(e)}", 3) from e
    if cfg["model"] not in ids:
        raise RunnerStop(f"local model server does not serve {cfg['model']} (it serves {sorted(i for i in ids if i)})", 3)


def expand_plan(plan: dict) -> list[dict]:
    cells = [
        {"agent": a, "variant": v, "task_id": t, "rep": r}
        for a in plan["agent_names"] for v in plan["variants"] for t in plan["tasks"] for r in range(1, plan["reps"] + 1)
    ]
    random.Random(plan["order_seed"]).shuffle(cells)
    return [{"run_id": f"{plan['plan_id']}-{i:04d}", **c} for i, c in enumerate(cells)]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def server_clock(http: httpx.Client, base: str) -> tuple[float, str]:
    """Return (offset in ms, server mode). offset = server time minus local time."""
    t0 = datetime.now(timezone.utc)
    try:
        health = http.get(f"{base}/__health")
        now = http.get(f"{base}/__time")
        health.raise_for_status()
        now.raise_for_status()
    except httpx.HTTPError as e:
        raise RunnerStop(f"test site unreachable: {type(e).__name__}", 3) from e
    t1 = datetime.now(timezone.utc)
    midpoint = t0 + (t1 - t0) / 2
    offset = (parse_ts(now.json()["now"]) - midpoint).total_seconds() * 1000
    if abs(offset) > MAX_CLOCK_OFFSET_MS:
        raise RunnerStop(f"clock offset {offset:.0f} ms is above {MAX_CLOCK_OFFSET_MS} ms", 4)
    return offset, health.json()["mode"]


def run_one(cell: dict, plan: dict, task: Task, agent: Agent, http: httpx.Client, token: str | None = None) -> dict:
    """token is the run token. In per_run mode the run gets its own host and run_plan supplies the token."""
    cfg = plan["agents"][cell["agent"]]
    per_run = host_mode(plan) == "per_run"
    if per_run and token is None:
        raise ValueError("host_mode per_run needs a run token")
    base = run_base(plan, cell["variant"], token if per_run else None)
    if per_run:
        precheck(http, base)
    if cfg["kind"] == "openai_compat":
        local_precheck(http, cfg)
    offset, mode = server_clock(http, base)
    seed = cell["rep"] if cfg["kind"] == "openai_compat" else None
    skew = timedelta(milliseconds=offset)
    start = datetime.now(timezone.utc) + skew
    record = {
        "run_id": cell["run_id"], "plan_id": plan["plan_id"], "agent": cell["agent"], **describe(cfg),
        "variant": cell["variant"], "task_id": cell["task_id"], "rep": cell["rep"], "seed": seed,
        "run_host": base.split("://", 1)[1] if per_run else None, "run_token": token if per_run else None,
        "date": start.date().isoformat(), "start": iso(start), "interceptor": mode,
        "retries": 0, "api_tokens": None, "claimed_fetches": [], "error": None, "answer_raw": None, "answer": None,
    }
    budget = Budget(**plan["budget"])
    try:
        start_url = f"{base}/" if per_run else f"{base}/?r={cell['run_id']}"
        result = agent(task.question, start_url, seed, budget)
        record |= {"state": "answered", "answer_raw": result.text, "answer": final_answer(result.text),
                   "api_tokens": result.api_tokens, "retries": result.retries, "claimed_fetches": result.claimed_fetches}
    except BudgetExceeded as e:
        record |= {"state": "timeout", "error": str(e)}
    except AgentError as e:
        record |= {"state": "error", "error": str(e)}
    record["end"] = iso(datetime.now(timezone.utc) + skew)
    record["clock_offset_ms"] = round(offset)
    return record


def run_plan(
    plan: dict, tasks: dict[str, Task], runs_path: Path, http: httpx.Client,
    agents: dict[str, Agent] | None = None, sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Execute every run not yet in runs.jsonl. Returns the number of runs done."""
    check_plan(plan)
    agents = agents or {name: make_agent(plan["agents"][name]) for name in plan["agent_names"]}
    existing = load_jsonl(runs_path) if runs_path.exists() else []
    done = {r["run_id"] for r in existing}
    used_tokens = {r["run_token"] for r in existing if r.get("run_token")}
    runs_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    for cell in expand_plan(plan):
        if cell["run_id"] in done:
            continue
        token = new_token(used_tokens) if host_mode(plan) == "per_run" else None
        if token:
            used_tokens.add(token)
        record = run_one(cell, plan, tasks[cell["task_id"]], agents[cell["agent"]], http, token)
        with runs_path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        count += 1
        print(f"{record['run_id']} {record['agent']} {record['variant']} {record['task_id']} -> {record['state']}", flush=True)
        sleep(plan["quiet_gap_seconds"])
    return count
