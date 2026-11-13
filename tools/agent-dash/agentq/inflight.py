"""Read-only report on the DeepSeek in-flight limiter: throughput and latency by arm.

Reads proxy_minutes for DeepSeek rows, groups each complete UTC minute by the arm it fell in
(``enforce``, ``off``, ``shadow``), and summarises each arm. Minutes written before the limiter
existed, or minutes with no DeepSeek traffic, have no arm and are reported as ``none``. Nothing is written.
"""

from __future__ import annotations

import statistics
import sqlite3
import time
from pathlib import Path

from agentq.proxy import minute_stamp

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


def read_minutes(db_path: Path, since: float, until: float) -> list[dict]:
    """DeepSeek minute rows in [since, until) epoch seconds, one per minute. Read-only; missing store gives []."""
    if not Path(db_path).exists():
        return []
    connection = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        present = {row["name"] for row in connection.execute("PRAGMA table_info(proxy_minutes)")}
        sql = MINUTE_SQL.format(columns=_select_columns(present))
        return [dict(row) for row in connection.execute(sql, (minute_stamp(since), minute_stamp(until)))]
    finally:
        connection.close()


def _median(values: list) -> float | None:
    present = [float(v) for v in values if v is not None]
    return statistics.median(present) if present else None


def summarize(minutes: list[dict]) -> dict[str, dict]:
    """Per arm: minute count, mean billed tokens and requests per minute, median first-byte and upstream
    p50, mean peak in flight, the share of minutes where demand was above the limit, and the first-byte
    median split by requests sent above or at the limit."""
    arms: dict[str, list[dict]] = {}
    for row in minutes:
        arms.setdefault(row.get("inflight_arm") or "none", []).append(row)
    summary = {}
    for arm, rows in sorted(arms.items()):
        count = len(rows)
        over_minutes = sum(1 for r in rows if (r.get("inflight_over") or 0) > 0)
        summary[arm] = {
            "minutes": count,
            "billed_per_min": round(sum(r["billed"] or 0 for r in rows) / count),
            "req_per_min": round(sum(r["req"] or 0 for r in rows) / count, 1),
            "ttfb_p50_ms": _median([r["ttfb_p50_ms"] for r in rows]),
            "upstream_p50_ms": _median([r["upstream_p50_ms"] for r in rows]),
            "in_flight_max": _median([r["in_flight_max"] for r in rows]),
            "limit_median": _median([r["inflight_limit"] for r in rows]),
            "over_limit_minute_frac": round(over_minutes / count, 3),
            "ttfb_le_p50_ms": _median([r["ttfb_le_p50_ms"] for r in rows]),
            "ttfb_gt_p50_ms": _median([r["ttfb_gt_p50_ms"] for r in rows]),
        }
    return summary


def _cell(value, fmt: str = "{:,.0f}") -> str:
    return "-" if value is None else fmt.format(value)


def format_table(summary: dict[str, dict]) -> str:
    header = (
        f"{'arm':<8} {'min':>5} {'billed/min':>12} {'req/min':>8} {'ttfb p50':>9} "
        f"{'upstream p50':>13} {'in-flight':>10} {'limit':>6} {'over-limit':>11} {'ttfb<=lim':>10} {'ttfb>lim':>9}"
    )
    lines = [header]
    for arm, s in summary.items():
        lines.append(
            f"{arm:<8} {s['minutes']:>5} {_cell(s['billed_per_min']):>12} {_cell(s['req_per_min'], '{:.1f}'):>8} "
            f"{_cell(s['ttfb_p50_ms']):>9} {_cell(s['upstream_p50_ms']):>13} {_cell(s['in_flight_max']):>10} "
            f"{_cell(s['limit_median']):>6} {_cell(s['over_limit_minute_frac'] * 100, '{:.0f}%'):>11} "
            f"{_cell(s['ttfb_le_p50_ms']):>10} {_cell(s['ttfb_gt_p50_ms']):>9}"
        )
    return "\n".join(lines)


def report(db_path: Path, hours: float, now: float | None = None) -> tuple[str, dict[str, dict]]:
    """The table text and the per-arm summary for the last ``hours`` complete minutes."""
    until = (time.time() if now is None else now) // 60 * 60
    summary = summarize(read_minutes(db_path, until - hours * 3600, until))
    if not summary:
        return "no DeepSeek minutes in the window", summary
    return format_table(summary), summary
