#!/usr/bin/env python3
"""Summarise api-recorder JSONL files: per-model health across intervals.

    python3 api_recorder_report.py            # today
    python3 api_recorder_report.py --hours 6  # last N hours only
    python3 api_recorder_report.py --all      # every file on disk

Reads ~/.hermes/cache/api-recorder/YYYY-MM-DD.jsonl (one JSON object per flush
interval) and prints a table. Fails loudly if the plugin has recorded nothing —
a silent empty report reads like "everything is fine".
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BUCKETS = ("success", "empty_content", "aborted", "rate_limit",
           "stream_timeout", "client_abort", "auth", "billing", "overloaded",
           "server_error", "context_overflow", "policy_blocked", "tls_error",
           "other_error")
# Counters worth alerting on: a provider that answers 200 with nothing, a model
# that has exhausted its quota, or a hard failure class that needs a human
# (credentials, credit, a blocked model, a broken cert chain). Everything else
# is context.
ALERT = {"empty_content", "rate_limit", "stream_timeout",
         "auth", "billing", "policy_blocked", "tls_error"}
# The table has no room for 7 more columns, so the named error counters collapse
# into one cell listing only the non-zero ones. auth and billing stay separate
# entries, so they are still distinguishable at a glance.
NAMED_ERRORS = ("rate_limit", "stream_timeout", "client_abort", "auth", "billing",
                "overloaded", "server_error", "context_overflow", "policy_blocked",
                "tls_error", "other_error")
SHORT = {"empty_content": "EMPTY", "rate_limit": "429", "stream_timeout": "to",
         "client_abort": "499",
         "auth": "auth", "billing": "bill", "overloaded": "ovl",
         "server_error": "5xx", "context_overflow": "ctx", "policy_blocked": "pol",
         "tls_error": "tls", "other_error": "oth"}


def err_cell(c: dict) -> str:
    """Compact 'auth=1 pol=2' cell — only counters that actually fired."""
    parts = [f"{SHORT[n]}={c[n]}" for n in NAMED_ERRORS if c.get(n)]
    return " ".join(parts) if parts else "-"


def state_dir() -> Path:
    home = os.environ.get("HERMES_HOME") or Path.home() / ".hermes"
    return Path(home) / "cache" / "api-recorder"


def _synthetic_spans(records: list[dict], min_gap_s: int = 60) -> set[int]:
    """Indices of self-test records.

    The plugin's self-test forces a flush by resetting the interval clock, so a
    test run emits a burst of records seconds apart. Real flushes are an
    interval (default 600 s) or more apart, so a short gap is the reliable tell.

    Note: the JSONL ``interval_s`` field is NOT a duration — it is the raw
    monotonic clock, so it grows forever and cannot be range-checked.
    """
    flagged: set[int] = set()
    stamps: list[float | None] = []
    for rec in records:
        ts = rec.get("ts")
        if not ts:
            stamps.append(None)
            continue
        try:
            stamps.append(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
        except ValueError:
            stamps.append(None)
    for i in range(1, len(records)):
        prev, cur = stamps[i - 1], stamps[i]
        if prev is not None and cur is not None and 0 <= cur - prev < min_gap_s:
            # Everything in a tight cluster is a test run, including its first row.
            flagged.add(i - 1)
            flagged.add(i)
    return flagged


def load(days: int, hours: int | None) -> list[dict]:
    root = state_dir()
    if not root.is_dir():
        sys.exit(f"api-recorder state dir not found: {root}\n"
                 f"Is the plugin installed and has it flushed at least one interval?")
    files = sorted(root.glob("*.jsonl"))[-days:] if not hours else \
            sorted(root.glob("*.jsonl"))[-1:]
    records = []
    for f in files:
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append(rec)
    if hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        kept = []
        for r in records:
            ts = r.get("ts")
            if not ts:
                continue
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if when >= cutoff:
                kept.append(r)
        records = kept
    return records


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int,
                    help="only records newer than N hours (0 or negative = no limit)")
    ap.add_argument("--all", action="store_true", help="all files on disk")
    ap.add_argument("--days", type=int, default=3, help="how many day-files to read")
    ap.add_argument("--with-synthetic", action="store_true",
                    help="also count self-test records (fake models like gpt-4o/test-model)")
    args = ap.parse_args()

    hours = args.hours if (args.hours or 0) > 0 else None
    records = load(days=10**6 if args.all else args.days, hours=hours)
    if not args.with_synthetic:
        flagged = _synthetic_spans(records)
        if flagged:
            print(f"(skipping {len(flagged)} self-test record(s) — burst of "
                  f"flushes seconds apart; use --with-synthetic to include)")
        records = [r for i, r in enumerate(records) if i not in flagged]
    if not records:
        print("No records in range. The plugin may not have flushed yet "
              "(default interval is 10 minutes).")
        return 1

    agg = defaultdict(lambda: defaultdict(int))
    lat = defaultdict(list)
    first_ts = last_ts = None

    for rec in records:
        ts = rec.get("ts", "")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        for b in rec.get("buckets", []):
            key = (b.get("model") or "?", b.get("provider") or "?")
            agg[key]["total"] += b.get("total", 0)
            for name in BUCKETS:
                agg[key][name] += b.get(name, 0)
            ls = b.get("latency_s") or {}
            if ls.get("n"):
                for _ in range(int(ls["n"])):
                    # Reservoir already summarised; keep max and n, approximate mean.
                    lat[key].append(ls)

    print(f"api-recorder report  |  {len(records)} intervals  |  "
          f"{first_ts} .. {last_ts}")
    print()
    hdr = (f"{'model':<26}{'prov':<12}{'tot':>5}{'ok':>5}{'EMPTY':>7}"
           f"{'avg_s':>7}  {'errors':<46} alert")
    print(hdr)
    print("-" * len(hdr))
    flagged = 0
    for key in sorted(agg, key=lambda k: -agg[k]["total"]):
        model, prov = key
        c = agg[key]
        samples = lat.get(key) or []
        n = sum(s.get("n", 0) for s in samples)
        avg = (sum(s.get("avg", 0) * s.get("n", 0) for s in samples) / n) if n else 0.0
        mx = max((s.get("max", 0) for s in samples), default=0.0)
        bad = {k: c[k] for k in ALERT if c.get(k)}
        if bad:
            flagged += 1
        mark = " ".join(f"{SHORT[k]}={v}" for k, v in sorted(bad.items())) if bad else ""
        print(f"{model[:25]:<26}{prov[:11]:<12}{c['total']:>5}{c['success']:>5}"
              f"{c['empty_content']:>7}{avg:>7.1f}  {err_cell(c):<46} {mark}")

    totals = {k: sum(agg[key][k] for key in agg) for k in BUCKETS}
    total_calls = sum(agg[key]["total"] for key in agg)
    print()
    print(f"total calls: {total_calls}")
    for name in BUCKETS:
        if totals[name]:
            share = 100.0 * totals[name] / total_calls if total_calls else 0
            print(f"  {name:<16} {totals[name]:>6}  ({share:.1f}%)")
    if flagged:
        print(f"\n{flagged} model bucket(s) with alert-worthy counters "
              f"({', '.join(sorted(ALERT))}).")
    else:
        print("\nNo empty responses, no rate limits, no timeouts in this range.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
