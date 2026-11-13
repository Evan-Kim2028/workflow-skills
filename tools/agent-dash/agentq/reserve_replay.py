"""Replay DeepSeek per-minute usage through the burst reserve model and compare with Morph's 429s.

Read-only. Point --db at a copy of the agentq store, never the live file. The 429s come from the proxy
log lines ``upstream_429 ... Limit: N <kind> tokens/minute``. For each DeepSeek budget (output and uncached
input) the replay runs the same Reserve class the proxy uses, minute by minute: the free allowance is the
Morph per-minute limit (times --target), usage beyond it drains the reserve, and the reserve refills over
30 minutes. A minute is predicted to exhaust the reserve when its overflow is larger than the reserve held
at the start of the minute. Predictions are compared with the minutes Morph actually refused, within
MATCH_WINDOW_MIN minutes, since the store stamps usage at completion and Morph stamps it at the refusal.

Usage:
    python3 -I agentq/reserve_replay.py --db <copy>/agentq.db --log <logs>/proxy.err.log.1 --log <logs>/proxy.err.log
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from agentq.proxy import RESERVE_REFILL_S, Reserve, minute_epoch

DS_MODEL_LIKE = "morph-dsv41flash%"
MINUTE_FORMAT = "%Y-%m-%dT%H:%M"
MATCH_WINDOW_MIN = 2
BUDGETS = (
    {"name": "output", "limit": 70_000, "reserve": 210_000, "kind": "output"},
    {"name": "uncached_ds", "limit": 400_000, "reserve": 1_200_000, "kind": "uncached input"},
)
LIMIT_LINE = re.compile(
    r"^(\S+) .*upstream_429 .*model=morph-dsv41flash\S* .*Limit:\s*(\d+)\s+(output|uncached input) tokens/minute"
)


@dataclass(frozen=True)
class MinuteRow:
    minute: str
    used: int
    level_before: float
    level_after: float
    predicted_empty: bool


def minute_range(start: str, end: str) -> list[str]:
    """Every UTC minute from start to end inclusive, in proxy_minutes stamp format."""
    cursor = datetime.strptime(start, MINUTE_FORMAT)
    stop = datetime.strptime(end, MINUTE_FORMAT)
    out = []
    while cursor <= stop:
        out.append(cursor.strftime(MINUTE_FORMAT))
        cursor += timedelta(minutes=1)
    return out


def minute_gap(a: str, b: str) -> int:
    """Whole minutes from b to a."""
    delta = datetime.strptime(a, MINUTE_FORMAT) - datetime.strptime(b, MINUTE_FORMAT)
    return int(delta.total_seconds() // 60)


def load_usage(db_path: Path, since: str) -> tuple[str, dict[str, dict[str, int]]]:
    """The last stored DeepSeek minute, and per-minute output and uncached input summed over DeepSeek models."""
    connection = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        last = connection.execute(
            "SELECT MAX(minute) FROM proxy_minutes WHERE model LIKE ?", (DS_MODEL_LIKE,)
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT minute, SUM(output), SUM(input) FROM proxy_minutes WHERE model LIKE ? AND minute >= ? "
            "GROUP BY minute ORDER BY minute",
            (DS_MODEL_LIKE, since),
        ).fetchall()
    finally:
        connection.close()
    usage = {minute: {"output": int(out or 0), "uncached input": int(inp or 0)} for minute, out, inp in rows}
    return last, usage


def load_refusals(log_paths: list[Path]) -> dict[str, dict[str, int]]:
    """Morph's DeepSeek 429s per minute and kind, from the proxy log lines that name a Limit."""
    found: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for path in log_paths:
        for line in Path(path).read_text(errors="replace").splitlines():
            match = LIMIT_LINE.match(line)
            if match:
                found[match.group(1)[:16]][match.group(3)] += 1
    return {minute: dict(kinds) for minute, kinds in found.items()}


def replay_budget(spec: dict, usage: dict[str, dict[str, int]], minutes: list[str], target: float) -> list[MinuteRow]:
    """Run one budget's reserve over the minutes. The reserve starts full at the first minute."""
    free = spec["limit"] * target
    reserve = Reserve(spec["reserve"], refill_s=RESERVE_REFILL_S, wall=minute_epoch(minutes[0]))
    rows = []
    for minute in minutes:
        wall = minute_epoch(minute)
        used = usage.get(minute, {}).get(spec["kind"], 0)
        reserve.refill(wall)
        before = reserve.level
        predicted_empty = used - free > before
        reserve.sync(wall, minute, used, free)
        rows.append(MinuteRow(minute, used, round(before), round(reserve.level), predicted_empty))
    return rows


def summarise(name: str, rows: list[MinuteRow], refusals: dict[str, dict[str, int]], kind: str) -> dict:
    """Agreement between predicted exhaustion minutes and minutes Morph refused, within the match window."""
    actual = [r.minute for r in rows if refusals.get(r.minute, {}).get(kind)]
    predicted = [r.minute for r in rows if r.predicted_empty]

    def near(minute: str, others: list[str]) -> bool:
        return any(abs(minute_gap(minute, other)) <= MATCH_WINDOW_MIN for other in others)

    return {
        "budget": name,
        "minutes": len(rows),
        "predicted_empty_minutes": len(predicted),
        "first_predicted": predicted[0] if predicted else None,
        "morph_429_minutes": len(actual),
        "first_morph_429": actual[0] if actual else None,
        "predicted_matching_429": sum(1 for p in predicted if near(p, actual)),
        "429_matched_by_prediction": sum(1 for m in actual if near(m, predicted)),
    }


def print_table(rows: list[MinuteRow], usage: dict, refusals: dict, kind: str, start: str, end: str) -> None:
    print(f"minute  used  level_before  level_after  predicted_empty  morph_429")
    for row in rows:
        if not start <= row.minute <= end:
            continue
        refused = refusals.get(row.minute, {}).get(kind, 0)
        print(
            f"{row.minute}  {row.used:>7,}  {row.level_before:>12,}  {row.level_after:>11,}  "
            f"{'yes' if row.predicted_empty else '-':>15}  {refused or '-':>9}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", required=True, type=Path, help="a copy of agentq.db")
    parser.add_argument("--log", action="append", default=[], type=Path, help="proxy log file; repeat for rotated logs")
    parser.add_argument("--hours", type=float, default=12.0, help="replay this many hours ending at the last stored minute")
    parser.add_argument("--target", type=float, default=1.0, help="free allowance as a fraction of the Morph limit")
    parser.add_argument("--table-from", default=None, help="print per-minute rows from this minute stamp (UTC)")
    parser.add_argument("--table-to", default=None, help="print per-minute rows up to this minute stamp (UTC)")
    args = parser.parse_args(argv)

    probe_since = "0000-01-01T00:00"
    last, _ = load_usage(args.db, probe_since)
    if last is None:
        print("no DeepSeek minutes in the store", file=sys.stderr)
        return 1
    since = (datetime.strptime(last, MINUTE_FORMAT) - timedelta(hours=args.hours)).strftime(MINUTE_FORMAT)
    _, usage = load_usage(args.db, since)
    refusals = load_refusals(args.log)
    minutes = minute_range(since, last)
    for spec in BUDGETS:
        rows = replay_budget(spec, usage, minutes, args.target)
        print(" ".join(f"{k}={v}" for k, v in summarise(spec["name"], rows, refusals, spec["kind"]).items()))
        if args.table_from and args.table_to and spec["name"] == "output":
            print_table(rows, usage, refusals, spec["kind"], args.table_from, args.table_to)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
