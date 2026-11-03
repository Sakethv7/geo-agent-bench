"""Stranger report (ADR-017). Every logged request without a run token, grouped by the bot its
user agent claims and by whether its IP is inside that vendor's published ranges.

Verdicts
  verified      claims a known bot, IP inside the bot's published ranges
  spoofed       claims a known bot, IP outside them
  unverifiable  claims a known bot that publishes no ranges
  n/a           claims no known bot
"""
import ipaddress
import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from geobench.content import CANARIES
from geobench.parse_logs import parse_ts

MAX_RANGE_AGE_DAYS = 30
DISALLOWED = ("/private/", "/backup/")  # the Disallow lines of robots.txt on both variants


def load_ranges(path: Path, today: date) -> dict:
    """Raises ValueError for a missing or old file. The CLI turns that into exit code 2."""
    if not path.exists():
        raise ValueError(f"ranges file not found: {path}")
    ranges = json.loads(path.read_text())
    age = (today - date.fromisoformat(ranges["fetched"])).days
    if age > MAX_RANGE_AGE_DAYS:
        raise ValueError(f"ranges file is {age} days old. Download the vendor lists again")
    return ranges


def claimed_bot(ua: str | None, ranges: dict) -> tuple[str, list[str]] | None:
    """(bot token, cidrs) for the first known token in the user agent, or None."""
    text = (ua or "").lower()
    for vendor in ranges["vendors"].values():
        for token in vendor["tokens"]:
            if token.lower() in text:
                return token, vendor["cidrs"]
    return None


def verdict(ip: str, cidrs: list[str]) -> str:
    if not cidrs:
        return "unverifiable"
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return "spoofed"
    return "verified" if any(address in ipaddress.ip_network(c) for c in cidrs) else "spoofed"


def label(requests: list[dict], ranges: dict, exclude_ips: set[str], since: datetime | None) -> list[dict]:
    """Tokenless requests from strangers, each with claimed and verdict added."""
    rows = []
    for r in requests:
        if r.get("run_token") or r["ip"] in exclude_ips or (since and parse_ts(r["ts"]) < since):
            continue
        claim = claimed_bot(r.get("ua"), ranges)
        claimed, v = (claim[0], verdict(r["ip"], claim[1])) if claim else ("other", "n/a")
        rows.append(r | {"claimed": claimed, "verdict": v})
    return rows


def stranger_table(rows: list[dict]) -> pd.DataFrame:
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["claimed"], r["verdict"]), []).append(r)
    table = []
    for (claimed, v), group in sorted(groups.items(), key=lambda g: -len(g[1])):
        table.append({
            "claimed": claimed, "verdict": v, "requests": len(group), "ips": len({r["ip"] for r in group}),
            "first_seen": min(r["ts"] for r in group), "last_seen": max(r["ts"] for r in group),
            "robots_read": sum(r["path"] == "/robots.txt" for r in group),
            "disallowed_hits": sum(r["path"].startswith(DISALLOWED) for r in group),
            "exposed_hits": sum(r["path"] in CANARIES for r in group),
            "secret_probes": sum(r["path"].startswith("/.") for r in group),
            "trap_hits": sum(r.get("kind") == "trap" for r in group),
            "inject_hits": sum(r.get("kind") == "inject" for r in group),
            "markdown_accept": sum("text/markdown" in (r.get("accept") or "").lower() for r in group),
        })
    return pd.DataFrame(table)


def spoof_table(rows: list[dict]) -> pd.DataFrame:
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        if r["verdict"] == "spoofed":
            groups.setdefault((r["ip"], r["claimed"]), []).append(r)
    table = [
        {"ip": ip, "claimed": claimed, "requests": len(group),
         "first_seen": min(r["ts"] for r in group), "last_seen": max(r["ts"] for r in group),
         "top_paths": " ".join(p for p, _ in Counter(r["path"] for r in group).most_common(5))}
        for (ip, claimed), group in sorted(groups.items(), key=lambda g: -len(g[1]))
    ]
    return pd.DataFrame(table, columns=["ip", "claimed", "requests", "first_seen", "last_seen", "top_paths"])
