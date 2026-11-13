"""Read-only report on the DeepSeek in-flight limiter: throughput and latency by arm.

The denominator is calendar minutes, not stored rows. The window is the complete UTC minutes in
[until - hours, until), where until is the start of the current minute, so the minute in progress is
never counted. A minute with no DeepSeek row is an idle minute: it counts in its arm's minutes with zero
requests and zero tokens. Its arm comes from the stored row when there is one, otherwise from the A/B
schedule, so an idle minute in an off-arm period still counts against the off arm.

The window starts at the first minute that carries a limiter arm. Minutes before it were never
instrumented, so they are left out rather than counted as idle. Latency medians use only minutes with a row.
"""

from __future__ import annotations

import calendar
import statistics
import sqlite3
import time
from pathlib import Path
from typing import Callable

from agentq.proxy import minute_epoch, minute_stamp

REPORT_COLUMNS = (
    "inflight_limit", "inflight_mode", "inflight_arm", "inflight_over", "in_flight_max",
    "ttfb_p50_ms", "upstream_p50_ms", "ttfb_le_p50_ms", "ttfb_gt_p50_ms",
)

MINUTE_SQL = """
SELECT minute,
       SUM(COALESCE(input, 0) + COALESCE(cache_read, 0) + COALESCE(output, 0)) AS billed,
       SUM(COALESCE(req, 0)) AS req,
       {columns}
FROM proxy_minutes
WHERE model LIKE 'morph-dsv%' AND minute >= ? AND minute < ?
GROUP BY minute
ORDER BY minute
"""


def _select_columns(present: set[str]) -> str:
    parts = []
    for name in REPORT_COLUMNS:
        if name in present:
            agg = "SUM" if name == "inflight_over" else "MAX"
            parts.append(f"{agg}({name}) AS {name}")
        else:
            parts.append(f"NULL AS {name}")
    return ",\n       ".join(parts)


def read_minutes(db_path: Path, since: float, until: float) -> tuple[dict[str, dict], str | None]:
    """DeepSeek rows in [since, until) keyed by minute stamp, and the first minute stamp that carries an arm.

    Read-only. A missing store gives no rows and no instrumented minute.
    """
    if not Path(db_path).exists():
        return {}, None
    connection = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        present = {row["name"] for row in connection.execute("PRAGMA table_info(proxy_minutes)")}
        rows = connection.execute(
            MINUTE_SQL.format(columns=_select_columns(present)), (minute_stamp(since), minute_stamp(until))
        ).fetchall()
        first = None
        if "inflight_arm" in present:
            first = connection.execute("SELECT MIN(minute) FROM proxy_minutes WHERE inflight_arm IS NOT NULL").fetchone()[0]
    finally:
        connection.close()
    return {row["minute"]: dict(row) for row in rows}, first


def _median(values: list) -> float | None:
    present = [float(v) for v in values if v is not None]
    return statistics.median(present) if present else None


def summarize(minutes: list[float], rows: dict[str, dict], schedule: Callable[[float], str]) -> dict[str, dict]:
    """Per arm over every calendar minute in ``minutes`` (epoch seconds, each the start of a complete minute).

    Requests, tokens and the over-limit share divide by all the arm's minutes, idle ones included. Latency
    and limit medians use only the arm's minutes that have a row.
    """
    arms: dict[str, list[dict | None]] = {}
    for t in minutes:
        row = rows.get(minute_stamp(t))
        arm = (row or {}).get("inflight_arm") or schedule(t)
        arms.setdefault(arm, []).append(row)
    summary = {}
    for arm, slots in sorted(arms.items()):
        count = len(slots)
        active = [r for r in slots if r is not None]
        summary[arm] = {
            "minutes": count,
            "active_minutes": len(active),
            "idle_minutes": count - len(active),
            "billed_per_min": round(sum(r["billed"] or 0 for r in active) / count),
            "req_per_min": round(sum(r["req"] or 0 for r in active) / count, 1),
            "ttfb_p50_ms": _median([r["ttfb_p50_ms"] for r in active]),
            "upstream_p50_ms": _median([r["upstream_p50_ms"] for r in active]),
            "in_flight_max": _median([r["in_flight_max"] for r in active]),
            "limit_median": _median([r["inflight_limit"] for r in active]),
            "over_limit_minute_frac": round(sum(1 for r in active if (r.get("inflight_over") or 0) > 0) / count, 3),
            "ttfb_le_p50_ms": _median([r["ttfb_le_p50_ms"] for r in active]),
            "ttfb_gt_p50_ms": _median([r["ttfb_gt_p50_ms"] for r in active]),
        }
    return summary


def _cell(value, fmt: str = "{:,.0f}") -> str:
    return "-" if value is None else fmt.format(value)


def format_table(summary: dict[str, dict]) -> str:
    header = (
        f"{'arm':<8} {'min':>5} {'idle':>5} {'billed/min':>12} {'req/min':>8} {'ttfb p50':>9} "
        f"{'upstream p50':>13} {'in-flight':>10} {'limit':>6} {'over-limit':>11} {'ttfb<=lim':>10} {'ttfb>lim':>9}"
    )
    lines = [header]
    for arm, s in summary.items():
        lines.append(
            f"{arm:<8} {s['minutes']:>5} {s['idle_minutes']:>5} {_cell(s['billed_per_min']):>12} "
            f"{_cell(s['req_per_min'], '{:.1f}'):>8} {_cell(s['ttfb_p50_ms']):>9} {_cell(s['upstream_p50_ms']):>13} "
            f"{_cell(s['in_flight_max']):>10} {_cell(s['limit_median']):>6} "
            f"{_cell(s['over_limit_minute_frac'] * 100, '{:.0f}%'):>11} "
            f"{_cell(s['ttfb_le_p50_ms']):>10} {_cell(s['ttfb_gt_p50_ms']):>9}"
        )
    return "\n".join(lines)


def report(db_path: Path, hours: float, schedule: Callable[[float], str], now: float | None = None) -> tuple[str, dict[str, dict]]:
    """The table text and the per-arm summary for the complete UTC minutes of the last ``hours``.

    ``schedule`` maps an epoch minute to its arm when no row records one (the proxy's A/B schedule).
    """
    until = (time.time() if now is None else now) // 60 * 60
    since = until - hours * 3600
    rows, first = read_minutes(db_path, since, until)
    if first is None and not rows:
        return "no DeepSeek minutes in the window", {}
    start = since
    if first is not None:
        start = max(since, float(minute_epoch(first)))
    minutes = [float(t) for t in range(int(start), int(until), 60)]
    if not minutes:
        return "no complete instrumented minutes in the window", {}
    summary = summarize(minutes, rows, schedule)
    header = f"window {minute_stamp(minutes[0])} to {minute_stamp(minutes[-1] + 60)} UTC, {len(minutes)} complete minutes"
    if start > since:
        header += f" (instrumented from {minute_stamp(start)}; earlier minutes excluded)"
    return header + "\n" + format_table(summary), summary
