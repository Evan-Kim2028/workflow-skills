"""Replay DeepSeek output admission under the mean and the percentile estimate.

Read-only. Point --db at a copy of the agentq store. The governor charges each admitted request its estimate and
the minute's ledger is the sum of those charges. A minute is under-charged when the estimate says the minute
stayed within the free allowance while the output that actually came back exceeded it: the proxy admitted what
it believed was free, and Morph refused the rest.

Request stream. The proxy logs one ``request_output`` line per completion (from this change on). When those lines
cover the window, the replay uses each request as it happened. Otherwise it uses the store's per-minute totals,
which give only each minute's request count and output sum, so each minute's requests are replayed at that
minute's mean. That loses the spread between requests inside a minute, so the percentile is understated there.

Usage:
    python3 -I -m agentq.output_estimate_replay --db <copy>/agentq.db --hours 6 --pct 80
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

DS_MODEL_LIKE = "morph-dsv41flash%"
MINUTE_FORMAT = "%Y-%m-%dT%H:%M"
ALLOWANCE = 70_000
OUTPUT_WINDOW = 200
MEAN_WINDOW = 20
REQUEST_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}):\d{2}Z \S+ request_output model=morph-dsv41flash\S* output=(\d+)"
)


@dataclass(frozen=True)
class Request:
    minute: str
    output: float


def load_store_minutes(db_path: Path) -> list[tuple[str, int, int]]:
    """(minute, requests, output) per minute for the DeepSeek models, in time order."""
    connection = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT minute, SUM(req), SUM(output) FROM proxy_minutes WHERE model LIKE ? "
            "GROUP BY minute ORDER BY minute",
            (DS_MODEL_LIKE,),
        ).fetchall()
    finally:
        connection.close()
    return [(minute, int(req or 0), int(out or 0)) for minute, req, out in rows if (req or 0) > 0]


def load_logged_requests(log_paths: list[Path]) -> list[Request]:
    """Each logged DeepSeek completion, in log order, from the ``request_output`` lines."""
    found = []
    for path in log_paths:
        for line in Path(path).read_text(errors="replace").splitlines():
            match = REQUEST_LINE.match(line)
            if match:
                found.append(Request(minute=match.group(1), output=float(match.group(2))))
    return found


def synthetic_requests(minutes: list[tuple[str, int, int]]) -> list[Request]:
    """Each minute's requests, all at that minute's mean output."""
    stream = []
    for minute, requests, output in minutes:
        stream.extend(Request(minute=minute, output=output / requests) for _ in range(requests))
    return stream


def nearest_rank(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def estimate(history: deque, pct: float | None) -> float:
    """The mean of the last MEAN_WINDOW completions, or the nearest-rank percentile of the whole output window."""
    if not history:
        return 800.0
    if pct is None:
        recent = list(history)[-MEAN_WINDOW:]
        return sum(recent) / len(recent)
    return nearest_rank(list(history), pct)


def simulate(stream: list[Request], pct: float | None, start: str, window: int) -> dict[str, tuple[float, float]]:
    """Per minute from start on: (estimated output charged, actual output). Each request's estimate uses the history
    of completions before it, as the governor does at admission."""
    history: deque = deque(maxlen=window)
    charged: dict[str, float] = defaultdict(float)
    actual: dict[str, float] = defaultdict(float)
    for request in stream:
        if request.minute >= start:
            charged[request.minute] += estimate(history, pct)
            actual[request.minute] += request.output
        history.append(request.output)
    return {minute: (charged[minute], actual[minute]) for minute in actual}


def summarise(results: dict[str, tuple[float, float]], allowance: float) -> dict:
    over = [m for m, (est, out) in results.items() if out > allowance]
    under = [m for m in over if results[m][0] <= allowance]
    shortfall = sum(max(0.0, out - est) for _, (est, out) in results.items() if out > allowance >= est)
    charged = sum(est for est, _ in results.values())
    actual = sum(out for _, out in results.values())
    return {
        "minutes": len(results),
        "charged_over_actual": round(charged / actual, 3) if actual else None,
        "minutes_over_allowance": len(over),
        "over_admitted_minutes": len(under),
        "over_admitted_tokens": round(shortfall),
        "first_over_admitted": sorted(under)[0] if under else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", required=True, type=Path, help="a copy of agentq.db")
    parser.add_argument("--log", action="append", default=[], type=Path, help="proxy log file; repeat for rotated logs")
    parser.add_argument("--hours", type=float, default=6.0, help="count minutes in this many hours ending at the last minute")
    parser.add_argument("--pct", type=float, default=80.0, help="percentile for the reserve-budget estimate")
    parser.add_argument("--allowance", type=float, default=ALLOWANCE, help="free output per minute")
    parser.add_argument("--window", type=int, default=OUTPUT_WINDOW, help="completions the percentile is taken over")
    args = parser.parse_args(argv)

    minutes = load_store_minutes(args.db)
    if not minutes:
        print("no DeepSeek minutes in the store", file=sys.stderr)
        return 1
    last = minutes[-1][0]
    start = (datetime.strptime(last, MINUTE_FORMAT) - timedelta(hours=args.hours)).strftime(MINUTE_FORMAT)
    logged = load_logged_requests(args.log)
    stream = logged if logged else synthetic_requests(minutes)
    mode = "per-request log" if logged else "per-minute means (no request_output lines in the logs)"
    print(f"stream={mode} window={start}..{last} allowance={args.allowance:,.0f}")
    for label, pct in (("mean (last 20)", None), (f"p{args.pct:g} (last {args.window})", args.pct)):
        summary = summarise(simulate(stream, pct, start, args.window), args.allowance)
        print(f"{label}: " + " ".join(f"{key}={value}" for key, value in summary.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
