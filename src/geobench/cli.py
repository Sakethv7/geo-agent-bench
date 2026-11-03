import argparse
import json
import os
import secrets
import sys
from datetime import date
from pathlib import Path

import httpx
import uvicorn
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from geobench.account_auth import config_dir
from geobench.agents import make_agent
from geobench.content import manifest
from geobench.identity import identity_table
from geobench.interceptor import load_reference
from geobench.metrics import build_gaps, build_results, build_summary
from geobench.parse_logs import assign_requests, load_jsonl, parse_ts
from geobench.probe import run_probe
from geobench.runner import RunnerStop, check_gap, check_plan, run_plan
from geobench.server import make_app
from geobench.strangers import label, load_ranges, spoof_table, stranger_table
from geobench.tasks import load_tasks
from geobench.webbotauth import directory_document, thumbprint, public_jwk

NOTE = "Rankings hold for the run date and agent version only."


def cmd_serve(args: argparse.Namespace) -> int:
    admin_ips = frozenset(args.admin_ip or ["127.0.0.1", "::1"])
    directory_path = Path(args.directory)
    directory = json.loads(directory_path.read_text()) if directory_path.exists() else None
    app = make_app(Path(args.log), default_variant=args.default_variant, admin_ips=admin_ips,
                   mode=args.mode, reference=load_reference(Path(args.identity)), domain=args.domain, directory=directory)
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False)
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    print(json.dumps(manifest(args.variant), indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    plan = yaml.safe_load(Path(args.plan).read_text())
    tasks = load_tasks(Path(args.tasks))
    check_gap(plan)
    try:
        with httpx.Client(timeout=20) as http:
            done = run_plan(plan, tasks, Path(args.runs), http)
    except RunnerStop as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code
    print(f"{done} runs written to {args.runs}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    plan = yaml.safe_load(Path(args.plan).read_text())
    check_plan(plan)
    check_gap(plan)
    try:
        with httpx.Client(timeout=20) as http:
            report = run_probe(args.agent, plan, make_agent(plan["agents"][args.agent]), http, Path(args.requests), Path(args.identity))
    except RunnerStop as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code
    print(json.dumps(report, indent=2))
    if not report["fetch_observed"]:
        print("error: no fetch observed. Do not use this agent until you know why.", file=sys.stderr)
        return 5
    print("Compare user_agents and ips with the vendor's published values by hand.")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    runs = load_jsonl(Path(args.runs))
    requests = load_jsonl(Path(args.requests))
    tasks = load_tasks(Path(args.tasks))
    reference = load_reference(Path(args.identity)) or None
    results = build_results(runs, assign_requests(runs, requests), tasks, reference)
    summary = build_summary(results)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results.to_csv(out / "results.csv", index=False)
    summary.to_csv(out / "summary.csv", index=False)
    build_gaps(summary).to_csv(out / "gaps.csv", index=False)
    print(f"wrote {len(results)} runs, {len(summary)} summary rows to {out}")
    print(NOTE)
    return 0


def cmd_strangers(args: argparse.Namespace) -> int:
    ranges = load_ranges(Path(args.ranges), date.today())
    since = parse_ts(args.since) if args.since else None
    requests = load_jsonl(Path(args.requests))
    rows = label(requests, ranges, set(args.exclude_ip or []), since)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stranger_table(rows).to_csv(out / "strangers.csv", index=False)
    spoofs = spoof_table(rows)
    spoofs.to_csv(out / "spoofs.csv", index=False)
    dropped = sum(1 for r in requests if not r.get("run_token")) - len(rows)
    print(f"wrote {len(rows)} stranger requests ({dropped} dropped: excluded IPs or before --since), {len(spoofs)} spoofing IPs to {out}")
    print(f"Verdicts hold for the vendor lists fetched on {ranges['fetched']}.")
    return 0


def cmd_identity(args: argparse.Namespace) -> int:
    ranges = load_ranges(Path(args.ranges), date.today())
    table = identity_table(load_jsonl(Path(args.requests)), ranges, set(args.exclude_ip or []))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    table.to_csv(out / "identity.csv", index=False)
    print(f"wrote {len(table)} identity rows to {out}")
    print(f"IP verdicts hold for the vendor lists fetched on {ranges['fetched']}.")
    return 0


def cmd_botkey(args: argparse.Namespace) -> int:
    """Make the signing key and the user secret once. The private key and the secret stay out of the repo."""
    folder = config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    key_path, secret_path = folder / "bot_key.pem", folder / "user_secret"
    if key_path.exists():
        print(f"error: {key_path} exists. Delete it yourself if you want a new key", file=sys.stderr)
        return 2
    key = Ed25519PrivateKey.generate()
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(key_path, 0o600)
    if not secret_path.exists():
        secret_path.write_text(secrets.token_urlsafe(32) + "\n")
        os.chmod(secret_path, 0o600)
    out = Path(args.directory)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(directory_document([key])))
    print(f"key id {thumbprint(public_jwk(key)['x'])}. Public directory written to {out}. Private files are in {folder}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="geobench")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve")
    serve.add_argument("--log", default="runs/requests.jsonl")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--default-variant", default=None, help="variant for hosts such as localhost")
    serve.add_argument("--domain", default=None, help="parent domain. Turns on run hosts: <token>.<variant>.<domain>")
    serve.add_argument("--mode", choices=["record", "enforce"], default="record")
    serve.add_argument("--identity", default="runs/identity_reference.json")
    serve.add_argument("--directory", default="runs/bot_directory.json", help="Web Bot Auth key directory served on the bot host")
    serve.add_argument("--admin-ip", action="append", help="IP allowed to call /__health and /__time. Repeat for more.")
    serve.set_defaults(func=cmd_serve)

    man = sub.add_parser("manifest")
    man.add_argument("--variant", required=True)
    man.set_defaults(func=cmd_manifest)

    run = sub.add_parser("run")
    run.add_argument("--plan", default="plans/pilot-1.yaml")
    run.add_argument("--tasks", default="tasks/tasks.yaml")
    run.add_argument("--runs", default="runs/runs.jsonl")
    run.set_defaults(func=cmd_run)

    probe = sub.add_parser("probe")
    probe.add_argument("--agent", required=True)
    probe.add_argument("--plan", default="plans/pilot-1.yaml")
    probe.add_argument("--requests", default="runs/requests.jsonl")
    probe.add_argument("--identity", default="runs/identity_reference.json")
    probe.set_defaults(func=cmd_probe)

    report = sub.add_parser("report")
    report.add_argument("--runs", default="runs/runs.jsonl")
    report.add_argument("--requests", default="runs/requests.jsonl")
    report.add_argument("--tasks", default="tasks/tasks.yaml")
    report.add_argument("--identity", default="runs/identity_reference.json")
    report.add_argument("--out", default="runs")
    report.set_defaults(func=cmd_report)

    strangers = sub.add_parser("strangers")
    strangers.add_argument("--requests", default="runs/requests.jsonl")
    strangers.add_argument("--ranges", default="runs/vendor_ranges.json")
    strangers.add_argument("--exclude-ip", action="append", help="our own IP. Repeat for more.")
    strangers.add_argument("--since", default=None, help="ISO time. Drop requests before it.")
    strangers.add_argument("--out", default="runs/strangers")
    strangers.set_defaults(func=cmd_strangers)

    identity = sub.add_parser("identity")
    identity.add_argument("--requests", default="runs/requests.jsonl")
    identity.add_argument("--ranges", default="runs/vendor_ranges.json")
    identity.add_argument("--exclude-ip", action="append", help="IP to drop, for example the VM's own address. Repeat for more.")
    identity.add_argument("--out", default="runs/identity")
    identity.set_defaults(func=cmd_identity)

    botkey = sub.add_parser("botkey")
    botkey.add_argument("--directory", default="runs/bot_directory.json")
    botkey.set_defaults(func=cmd_botkey)

    args = parser.parse_args()
    try:
        sys.exit(args.func(args))
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
