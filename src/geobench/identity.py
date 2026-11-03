"""Identity matrix (ADR-023). Joins the claimed identity (user agent), the IP verdict (ADR-017) and the
signature verdict (ADR-021) into one proof level per request, then counts.

Proof levels
  signature  a valid Web Bot Auth signature
  network    no signature, a claimed vendor, and the IP is inside the vendor's published ranges
  none       a claim that cannot be checked (no published ranges, or the key directory was unreachable)
  spoof      a bad signature, or a claimed vendor with an IP outside its ranges, or one of our own bot
             names without a valid signature
  anonymous  no claim and no signature
"""
import pandas as pd

from geobench.strangers import claimed_bot, verdict

SIGNATURE_ONLY = {"geobench-local": "geobench-local"}  # our own bot name has no IP ranges. Only a signature proves it
BAD_SIGNATURES = {"invalid", "expired", "unknown_key"}


def _claim(request: dict, ranges: dict) -> tuple[str | None, str]:
    """(claimed identity, IP verdict) from the user agent."""
    ua = (request.get("ua") or "").lower()
    for token, name in SIGNATURE_ONLY.items():
        if token in ua:
            return name, "signature_only"
    claim = claimed_bot(request.get("ua"), ranges)
    return (claim[0], verdict(request["ip"], claim[1])) if claim else (None, "n/a")


def proof_level(request: dict, ranges: dict) -> tuple[str, str]:
    """(claimed identity or 'other', proof level)."""
    claimed, ip_verdict = _claim(request, ranges)
    name = claimed or "other"
    signature = (request.get("sig") or {}).get("verdict", "absent")
    if signature == "valid":
        return name, "signature"
    if signature in BAD_SIGNATURES:
        return name, "spoof"
    if signature == "directory_unreachable":
        return name, "none"
    if claimed is None:
        return name, "anonymous"
    if ip_verdict == "verified":
        return name, "network"
    if ip_verdict in ("spoofed", "signature_only"):
        return name, "spoof"
    return name, "none"


def identity_table(requests: list[dict], ranges: dict, exclude_ips: set[str]) -> pd.DataFrame:
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in requests:
        if r["ip"] in exclude_ips:
            continue
        groups.setdefault(proof_level(r, ranges), []).append(r)
    rows = [
        {"claimed_identity": name, "proof_level": level, "requests": len(group), "distinct_ips": len({r["ip"] for r in group}),
         "first_seen": min(r["ts"] for r in group), "last_seen": max(r["ts"] for r in group)}
        for (name, level), group in sorted(groups.items(), key=lambda g: -len(g[1]))
    ]
    return pd.DataFrame(rows, columns=["claimed_identity", "proof_level", "requests", "distinct_ips", "first_seen", "last_seen"])
