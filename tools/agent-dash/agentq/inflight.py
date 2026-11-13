"""Read-only report on the DeepSeek in-flight limiter: throughput and latency by arm.

The unit of account is the calendar minute the proxy recorded (the ``inflight_minutes`` table). The proxy
writes that record on every flush, with the mode, arm and limit it was actually running, so an arm is
never inferred from configuration or from traffic after the fact.

The window is the complete UTC minutes in [until - hours, until), where until is the start of the minute in
progress. A window minute with no record was not recorded (before instrumentation, or the proxy was down).
It is excluded, and the report prints how many were excluded. A recorded minute with no DeepSeek request is
idle: it counts in its arm's minutes with zero requests and tokens. Traffic rows for minutes with no record
are ignored.
"""

from __future__ import annotations

import statistics
import sqlite3
import time
from pathlib import Path

from agentq.proxy import minute_stamp

TRAFFIC_COLUMNS = (
    "inflight_over", "in_flight_max", "ttfb_p50_ms", "upstream_p50_ms", "ttfb_le_p50_ms", "ttfb_gt_p50_ms",
)

TRAFFIC_SQL = """
SELECT minute,
       SUM(COALESCE(input, 0) + COALESCE(cache_read, 0) + COALESCE(output, 0)) AS billed,
       SUM(COALESCE(req, 0)) AS req,
       {columns}
FROM proxy_minutes
WHERE model LIKE 'morph-dsv%' AND minute >= ? AND minute < ?
GROUP BY minute
ORDER BY minute
"""

RECORD_SQL = """
SELECT minute, mode, arm, inflight_limit, enforcing
FROM inflight_minutes
WHERE minute >= ? AND minute < ?
"""


def _select_columns(present: set[str]) -> str:
    parts = []
    for name in TRAFFIC_COLUMNS:
        if name in present:
            agg = "SUM" if name == "inflight_over" else "MAX"
            parts.append(f"{agg}({name}) AS {name}")
        else:
            parts.append(f"NULL AS {name}")
    return ",\n       ".join(parts)


def _connect(db_path: Path) -> sqlite3.Connection | None:
    if not Path(db_path).exists():
        return None
    connection = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    return connection


def read_records(db_path: Path, since: float, until: float) -> dict[str, dict]:
    """The limiter's per-minute records in [since, until), keyed by minute stamp. Read-only; no store gives {}."""
    connection = _connect(db_path)
    if connection is None:
        return {}
    try:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inflight_minutes'"
        ).fetchone() is None:
            return {}
        rows = connection.execute(RECORD_SQL, (minute_stamp(since), minute_stamp(until))).fetchall()
    finally:
        connection.close()
    return {row["minute"]: dict(row) for row in rows}


def read_traffic(db_path: Path, since: float, until: float) -> dict[str, dict]:
    """DeepSeek traffic rows in [since, until), one per minute, keyed by minute stamp. Read-only."""
    connection = _connect(db_path)
    if connection is None:
        return {}
    try:
        present = {row["name"] for row in connection.execute("PRAGMA table_info(proxy_minutes)")}
        if not present:
            return {}
        rows = connection.execute(
            TRAFFIC_SQL.format(columns=_select_columns(present)), (minute_stamp(since), minute_stamp(until))
        ).fetchall()
    finally:
        connection.close()
    return {row["minute"]: dict(row) for row in rows}


def _median(values: list) -> float | None:
    present = [float(v) for v in values if v is not None]
    return statistics.median(present) if present else None


def summarize(minutes: list[str], records: dict[str, dict], traffic: dict[str, dict]) -> dict[str, dict]:
    """Per arm over the recorded minutes in ``minutes`` (minute stamps). The arm, mode and limit come from the
    record only. Requests and tokens divide by every recorded minute of the arm, idle ones included. Latency and
    in-flight medians use the minutes that had a DeepSeek request."""
    arms: dict[str, list[str]] = {}
    for stamp in minutes:
        arms.setdefault(records[stamp]["arm"], []).append(stamp)
    summary = {}
    for arm, stamps in sorted(arms.items()):
        count = len(stamps)
        rows = [traffic[s] for s in stamps if s in traffic and (traffic[s].get("req") or 0) > 0]
        summary[arm] = {
            "minutes": count,
            "active_minutes": len(rows),
            "idle_minutes": count - len(rows),
            "billed_per_min": round(sum(r["billed"] or 0 for r in rows) / count),
            "req_per_min": round(sum(r["req"] or 0 for r in rows) / count, 1),
            "ttfb_p50_ms": _median([r["ttfb_p50_ms"] for r in rows]),
            "upstream_p50_ms": _median([r["upstream_p50_ms"] for r in rows]),
            "in_flight_max": _median([r["in_flight_max"] for r in rows]),
            "limit_median": _median([records[s]["inflight_limit"] for s in stamps]),
            "over_limit_minute_frac": round(sum(1 for r in rows if (r.get("inflight_over") or 0) > 0) / count, 3),
            "ttfb_le_p50_ms": _median([r["ttfb_le_p50_ms"] for r in rows]),
            "ttfb_gt_p50_ms": _median([r["ttfb_gt_p50_ms"] for r in rows]),
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


def report(db_path: Path, hours: float, now: float | None = None) -> tuple[str, dict[str, dict]]:
    """The table text and per-arm summary for the complete UTC minutes of the last ``hours``, from the records."""
    until = (time.time() if now is None else now) // 60 * 60
    since = until - hours * 3600
    calendar = [minute_stamp(t) for t in range(int(since), int(until), 60)]
    records = read_records(db_path, since, until)
    recorded = [stamp for stamp in calendar if stamp in records]
    if not recorded:
        return f"no recorded minutes in the window ({len(calendar)} calendar minutes excluded)", {}
    excluded = len(calendar) - len(recorded)
    traffic = read_traffic(db_path, since, until)
    summary = summarize(recorded, records, traffic)
    header = (
        f"window {recorded[0]} to {minute_stamp(until)} UTC: {len(recorded)} recorded minutes; "
        f"{excluded} minutes excluded (no record: before instrumentation or the proxy was down)"
    )
    return header + "\n" + format_table(summary), summary
