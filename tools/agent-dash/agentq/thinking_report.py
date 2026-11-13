"""Thinking-level report for DeepSeek jobs (``agentq thinking report``).

Groups the DeepSeek jobs by the thinking level each run was launched with (``jobs.thinking_used``; a run from before
levels were recorded reads as ``default``). Per level it reports finished jobs, the verdict mix, output tokens per job
and per model turn, billed tokens per job, wall time, and a Wilson 95% interval for the PASS rate. Output and billed
tokens come from ``proxy_jobs``; a model turn is one proxied request. The store is opened read-only.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

from agentq import db

DEEPSEEK_TIER = "pi-deepseek-flash"
Z95 = 1.959963984540054
FINISHED = ("done", "failed")
VERDICTS = ("PASS", "PARTIAL", "FAIL", "BLOCKED")
DEFAULT_LEVEL = "default"
LEVEL_ORDER = {level: i for i, level in enumerate(db.THINKING_LEVELS)}


def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """The Wilson score interval for a proportion, or None with no trials."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def parse_since(text: str | None) -> float | None:
    """Epoch seconds for an ISO time (a naive one is UTC). None when none is given. Raises ValueError when unreadable."""
    if not text:
        return None
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"--since needs an ISO time such as 2026-11-13T12:00:00Z, not {text!r}") from exc
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def connect_read_only(home) -> sqlite3.Connection:
    path = Path(home) / "agentq.db"
    if not path.is_file():
        raise ValueError(f"no agentq store at {path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _proxy(conn) -> dict[str, dict]:
    """job id -> output tokens, billed tokens and model turns (proxied requests), summed over models."""
    try:
        rows = conn.execute(
            """SELECT job, COALESCE(SUM(output), 0) AS output, COALESCE(SUM(req), 0) AS turns,
                      COALESCE(SUM(COALESCE(input, 0) + COALESCE(cache_read, 0) + COALESCE(output, 0)), 0) AS billed
               FROM proxy_jobs WHERE job IN (SELECT id FROM jobs WHERE tier = ?) GROUP BY job""",
            (DEEPSEEK_TIER,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["job"]: {"output": int(row["output"]), "turns": int(row["turns"]), "billed": int(row["billed"])}
            for row in rows}


def collect(conn, since: float | None = None) -> list[dict]:
    """One record per DeepSeek job submitted at or after ``since``."""
    rows = conn.execute(
        """SELECT id, state, verdict, thinking_used, started_at, finished_at FROM jobs
           WHERE tier = ? AND (? IS NULL OR submitted_at >= ?) ORDER BY submitted_at, rowid""",
        (DEEPSEEK_TIER, since, since),
    ).fetchall()
    totals = _proxy(conn)
    records = []
    for row in rows:
        totals_for = totals.get(row["id"], {})
        verdict = (row["verdict"] or "none").upper()
        wall = None
        if row["started_at"] is not None and row["finished_at"] is not None:
            wall = float(row["finished_at"]) - float(row["started_at"])
        records.append({
            "level": row["thinking_used"] or DEFAULT_LEVEL, "state": row["state"],
            "finished": row["state"] in FINISHED,
            "verdict": verdict if verdict in VERDICTS else "none",
            "output": totals_for.get("output"), "turns": totals_for.get("turns"),
            "billed": totals_for.get("billed"), "wall": wall,
        })
    return records


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def summarize(records: list[dict]) -> dict[str, dict]:
    """Per-level figures, levels in pi's order (default last)."""
    levels: dict[str, list[dict]] = {}
    for record in records:
        levels.setdefault(record["level"], []).append(record)
    summary = {}
    for level in sorted(levels, key=lambda name: LEVEL_ORDER.get(name, len(LEVEL_ORDER))):
        jobs = levels[level]
        done = [j for j in jobs if j["finished"]]
        mix = {v: sum(1 for j in done if j["verdict"] == v) for v in (*VERDICTS, "none")}
        turns_total = sum(j["turns"] or 0 for j in done)
        output_total = sum(j["output"] or 0 for j in done if j["turns"])
        summary[level] = {
            "jobs": len(jobs), "finished": len(done), "running": len(jobs) - len(done),
            "verdicts": mix, "pass_rate": (mix["PASS"] / len(done)) if done else None,
            "pass_interval": wilson(mix["PASS"], len(done)),
            "output": _median([j["output"] for j in done]),
            "output_per_turn": (output_total / turns_total) if turns_total else None,
            "turns": _median([j["turns"] for j in done]),
            "billed": _median([j["billed"] for j in done]),
            "wall": _median([j["wall"] for j in done]),
        }
    return summary


def _fmt(value, pattern="{:,.0f}"):
    return "-" if value is None else pattern.format(value)


def report_lines(summary: dict[str, dict], since: str | None) -> list[str]:
    header = "DeepSeek jobs by thinking level" + (f", submitted since {since}" if since else "")
    if not summary:
        return [header, "no DeepSeek job has run yet"]
    lines = [header, ""]
    for level, s in summary.items():
        interval = s["pass_interval"]
        ci = "-" if interval is None else f"{interval[0] * 100:.0f}% to {interval[1] * 100:.0f}%"
        rate = "-" if s["pass_rate"] is None else f"{s['pass_rate'] * 100:.0f}%"
        lines += [
            f"level {level}: {s['jobs']} jobs, {s['finished']} finished, {s['running']} not finished",
            "  verdicts: " + ", ".join(f"{v} {n}" for v, n in s["verdicts"].items()),
            f"  PASS rate {rate} (95% Wilson interval {ci})",
            f"  output tokens per job (median) {_fmt(s['output'])}; output tokens per model turn "
            f"{_fmt(s['output_per_turn'], '{:,.0f}')}; model turns per job (median) {_fmt(s['turns'], '{:.0f}')}",
            f"  billed tokens per job (median) {_fmt(s['billed'])}; "
            f"wall time per job (median) {_fmt(s['wall'] and s['wall'] / 60, '{:.1f}')} min",
            "",
        ]
    return lines


def report_text(home, since: str | None = None) -> tuple[list[str], str]:
    """(report lines, one-line answer). Read-only: the store is opened with mode=ro and nothing is written."""
    epoch = parse_since(since)
    conn = connect_read_only(home)
    try:
        records = collect(conn, epoch)
    finally:
        conn.close()
    summary = summarize(records)
    total = sum(s["jobs"] for s in summary.values())
    return report_lines(summary, since), f"ANSWER: {total} DeepSeek job(s) across {len(summary)} thinking level(s)"
