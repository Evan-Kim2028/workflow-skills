#!/usr/bin/env python3
"""Synthetic state DBs with the real schema and today's data shape.

Copies the distribution of runs per source, usage rows per run,
labels and workstreams from a database (``--from``, usually a copy of
the live state), anonymised, scaled by ``--scale``. Without ``--from``
it uses the built-in profile measured on the live database
2026-10-19 (8,406 runs, ~159k usage rows, 17 workstreams).

Labels, session ids, and paths are synthetic (no prompts, no bodies,
no paths cross); model names are real so pricing resolves;
workstream ids keep their WS<n> shape so attribution behaves the
same; token shapes pool from measured rows so folds cost what folds
cost. est_cost_usd uses the real catalog pricing with a memo, so
repeated shapes share one computation exactly like the server.

Builds with raw SQL in bulk transactions (fast), then opens the result
with the real Store and rebuilds every touched hourly bucket with the
real ``_rebuild_hour``, so buckets equal what the server would build.

Usage:
  python3 scripts/perf_seed.py --scale 1 --out /tmp/seed1x.sqlite
  python3 scripts/perf_seed.py --scale 5 --from /tmp/state/dash.sqlite --out /tmp/seed5x.sqlite
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Built-in profile: live database 2026-10-19. (source, tier, runs, usage_rows, tool)
SOURCES = [
    ("dagster", "LIGHT", 2702, 0, None),
    ("muse", "muse", 1877, 112885, "muse"),
    ("grok", "grok", 1229, 0, None),
    ("claude-subagent", "haiku", 632, 20000, "claude"),
    ("claude-workflow", "haiku", 505, 12000, "claude"),
    ("dagster", "HEAVY", 560, 0, None),
    ("shell", "", 228, 0, None),
    ("dagster", "INGEST", 200, 0, None),
    ("glm", "glm", 178, 5135, "glm"),
    ("dagster", "MAINT", 108, 0, None),
    ("claude-workflow", "sonnet", 98, 4000, "claude"),
    ("dagster", "SHORT", 50, 0, None),
    ("claude-main", "opus", 26, 2500, "claude"),
    ("codex-exec", "codex", 7, 532, "codex"),
    ("claude-subagent", "sonnet", 6, 358, "claude"),
    ("dagster", "", 3, 0, None),
    ("codex", "codex", 3, 0, "codex"),
    ("claude-main", "sonnet", 11, 0, "claude"),
    ("claude-main", "", 7, 0, "claude"),
    ("claude-subagent", "opus", 1, 0, "claude"),
]

# (model, weight) for usage rows, by tool.
TOOL_MODELS = {
    "muse": [("muse-spark-1.3-contributor", 100), ("muse-wonder-1.0-contributor", 3), ("muse-wonder-1.0", 1)],
    "claude": [("claude-haiku-5-5", 60), ("claude-opus-5-5", 30), ("claude-sonnet-5-5", 8), ("claude-fable-5-1", 6)],
    "glm": [("glm-5.3-flash", 6), ("glm-5.3", 4)],
    "codex": [("gpt-5-codex", 1)],
}

# Measured token shapes (input, output, cache_read, cache_write, reasoning) by tool.
TOOL_SHAPES = {
    "muse": [(36029, 186, 0, 0, 28), (2521, 340, 71537, 0, 99), (938, 692, 278897, 0, 490), (2210, 883, 95729, 0, 623), (413, 984, 256369, 0, 792), (445, 209, 106609, 0, 0), (734, 293, 147313, 0, 132), (2163, 157, 188529, 0, 51), (369, 199, 160753, 0, 32), (578, 563, 339313, 0, 318)],
    "claude": [(2, 482, 48273, 8456, 190), (2, 2179, 106821, 544, 1684), (2, 208, 754215, 1113, 0), (2, 186, 201880, 652, 0), (0, 0, 0, 0, 0)],
    "glm": [(91212, 17003, 2219584, 0, 7202), (40000, 8000, 900000, 0, 3000)],
    "codex": [(6265, 14, 12160, 0, 0), (3000, 200, 8000, 0, 100)],
}

# (workstream, weight): 17 live ids plus the unattributed share (None).
WORKSTREAMS = [
    (None, 4627), ("WS1", 888), ("WS7", 794), ("WS9", 518), ("WS8", 438),
    ("WS11", 280), ("WS4", 275), ("WS2", 168), ("TOOLS", 157), ("WS3", 98),
    ("WS12", 65), ("WS5", 36), ("WS6", 33), ("PLANNING", 26), ("WS0", 21),
    ("WS10", 5), ("WS345", 2),
]

STATUSES = [("done", 4627), ("finished", 3620), ("died_early", 93), ("unknown", 42), ("killed", 38), ("running", 11)]

TS_MIN = 1791714674.0
TS_MAX = 1792456383.0
SAMPLES_PER_RUN = 29


def _weighted(rng, pairs):
    total = sum(weight for _, weight in pairs)
    pick = rng.uniform(0, total)
    for value, weight in pairs:
        pick -= weight
        if pick <= 0:
            return value
    return pairs[-1][0]


def collect_profile(db: Path) -> dict:
    """Read the run/usage distribution out of a real database (counts only)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        sources = [
            (row[0], row[1], row[2])
            for row in con.execute("SELECT source, COALESCE(tier,''), COUNT(*) FROM runs GROUP BY 1, 2")
        ]
        usage_by_source = {}
        for tool, n in con.execute(
            "SELECT tool, COUNT(*) FROM usage_events WHERE COALESCE(tool,'') != 'grok-context' GROUP BY 1"
        ):
            usage_by_source[tool or ""] = n
        models = {}
        for tool, model, n in con.execute(
            "SELECT tool, model, COUNT(*) FROM usage_events WHERE COALESCE(tool,'') != 'grok-context' GROUP BY 1, 2"
        ):
            models.setdefault(tool or "", []).append((model, n))
        shapes = {}
        for tool, i, o, cr, cw, r in con.execute(
            "SELECT tool, input_tokens, output_tokens, cache_read, cache_write, reasoning"
            " FROM usage_events WHERE COALESCE(tool,'') != 'grok-context' ORDER BY ts"
        ):
            bucket = shapes.setdefault(tool or "", [])
            if len(bucket) < 400 and (len(bucket) < 12 or hash((i, o)) % 37 == 0):
                bucket.append((i or 0, o or 0, cr or 0, cw or 0, r or 0))
        workstreams = [
            (row[0], row[1]) for row in con.execute("SELECT workstream, COUNT(*) FROM runs GROUP BY 1")
        ]
        statuses = [(row[0], row[1]) for row in con.execute("SELECT status, COUNT(*) FROM runs GROUP BY 1")]
        span = con.execute("SELECT MIN(ts), MAX(ts) FROM usage_events WHERE ts IS NOT NULL").fetchone()
        samples = con.execute("SELECT COUNT(*) FROM process_samples").fetchone()[0]
        runs = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        con.close()
    return {
        "sources": sources,
        "usage_by_source": usage_by_source,
        "models": models,
        "shapes": shapes,
        "workstreams": workstreams,
        "statuses": statuses,
        "span": (span[0], span[1]),
        "samples_per_run": (samples / max(1, runs)),
    }


def build(out: Path, scale: int, profile: dict | None, seed: int = 7) -> dict:
    """Write the seed database; returns counts, size, and timings."""
    from dash.pricing import cost as price_cost

    rng = random.Random(seed)
    t0 = time.time()
    if out.exists():
        out.unlink()
    from dash.store import Store

    store = Store(out)
    store.close()
    con = sqlite3.connect(str(out))
    timings = {}

    if profile is None:
        sources = SOURCES
        ws_pool = WORKSTREAMS
        status_pool = STATUSES
        ts_min, ts_max = TS_MIN, TS_MAX
        samples_per_run = SAMPLES_PER_RUN
        models_of = TOOL_MODELS
        shapes_of = TOOL_SHAPES
    else:
        usage_by_source = profile["usage_by_source"]
        tool_of_source = {"muse": "muse", "claude-main": "claude", "claude-subagent": "claude",
                          "claude-workflow": "claude", "glm": "glm", "codex": "codex", "codex-exec": "codex"}
        sources = []
        for name, tier, n in profile["sources"]:
            tool = tool_of_source.get(name)
            total_u = usage_by_source.get(tool or "", 0) if tool else 0
            total_r = sum(m for nm, _, m in profile["sources"] if tool_of_source.get(nm) == tool) if tool else 1
            sources.append((name, tier, n, int(total_u * n / max(1, total_r)) if tool else 0, tool))
        ws_pool = profile["workstreams"]
        status_pool = profile["statuses"]
        ts_min, ts_max = profile["span"]
        samples_per_run = profile["samples_per_run"]
        models_of = profile["models"]
        shapes_of = {tool: rows for tool, rows in profile["shapes"].items() if rows}

    est_memo: dict = {}

    def est(model, shape):
        key = (model, shape)
        hit = est_memo.get(key)
        if hit is None:
            i, o, cr, cw, r = shape
            try:
                hit = price_cost(model, i, o, cr, cw, reasoning=r, ts=ts_min)
            except Exception:
                hit = None
            est_memo[key] = hit
        return hit

    t1 = time.time()
    con.execute("BEGIN")
    run_rows = []
    run_counter = 0
    session_of_run = {}
    for name, tier, n_runs, n_usage, tool in sources:
        for _ in range(n_runs * scale):
            run_counter += 1
            run_id = f"seed-{name or 'none'}-{run_counter}"
            session_id = f"seed-session-{run_counter}"
            ws = _weighted(rng, ws_pool)
            label = f"{ws}-{run_counter % 97} synthetic task {run_counter % 13}" if ws else f"synthetic task {run_counter % 13}"
            status = _weighted(rng, status_pool)
            started = rng.uniform(ts_min, ts_max)
            ended = None if status == "running" else min(ts_max, started + rng.uniform(60, 7200))
            run_rows.append((run_id, name, tier, session_id, label, ws, "explicit" if ws else None, status, started, ended))
            session_of_run[run_id] = session_id
    con.executemany(
        "INSERT INTO runs(run_id, source, tier, session_id, label, workstream, ws_confidence, status, started_at, ended_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        run_rows,
    )
    con.executemany(
        "INSERT INTO sessions(session_id, tool, label, workstream, ws_confidence) VALUES(?,?,?,?,?)",
        [(session, None, None, None, None) for session in {s for s in session_of_run.values()}],
    )
    con.execute("COMMIT")
    timings["runs"] = time.time() - t1

    t1 = time.time()
    con.execute("BEGIN")
    usage_rows = []
    msg = 0
    run_ids = [row[0] for row in run_rows]
    per_run: dict[str, int] = {}
    for name, tier, n_runs, n_usage, tool in sources:
        if not tool or not n_usage:
            continue
        pool = [rid for rid in run_ids if rid.startswith(f"seed-{name}-")]
        if not pool:
            continue
        models = models_of.get(tool) or [(None, 1)]
        shapes = shapes_of.get(tool) or [(100, 10, 0, 0, 0)]
        for _ in range(n_usage * scale):
            msg += 1
            rid = rng.choice(pool)
            per_run[rid] = per_run.get(rid, 0) + 1
            model = _weighted(rng, models)
            shape = rng.choice(shapes)
            usage_rows.append((
                tool, session_of_run[rid], f"seed-m-{msg}",
                rng.uniform(ts_min, ts_max), rid, model,
                shape[0], shape[1], shape[2], shape[3], shape[4], est(model, shape),
            ))
            if len(usage_rows) >= 20000:
                con.executemany(
                    "INSERT INTO usage_events(tool, session_id, message_id, ts, run_id, model,"
                    " input_tokens, output_tokens, cache_read, cache_write, reasoning, est_cost_usd)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    usage_rows,
                )
                usage_rows = []
    if usage_rows:
        con.executemany(
            "INSERT INTO usage_events(tool, session_id, message_id, ts, run_id, model,"
            " input_tokens, output_tokens, cache_read, cache_write, reasoning, est_cost_usd)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            usage_rows,
        )
    con.execute("COMMIT")
    timings["usage"] = time.time() - t1

    t1 = time.time()
    con.execute("BEGIN")
    sample_rows = []
    # Per-run count stays constant: totals scale with the run count, so
    # scale x N means x N samples overall, not x N^2.
    for rid in run_ids:
        for _ in range(int(round(samples_per_run))):
            sample_rows.append((rng.uniform(ts_min, ts_max), 1000 + (hash(rid) % 50000), "seed", 100000, 10.0, rid))
            if len(sample_rows) >= 20000:
                con.executemany(
                    "INSERT INTO process_samples(ts, pid, comm, rss_kb, elapsed_s, run_id) VALUES(?,?,?,?,?,?)",
                    sample_rows,
                )
                sample_rows = []
    if sample_rows:
        con.executemany(
            "INSERT INTO process_samples(ts, pid, comm, rss_kb, elapsed_s, run_id) VALUES(?,?,?,?,?,?)",
            sample_rows,
        )
    con.execute("COMMIT")
    timings["samples"] = time.time() - t1
    con.close()

    t1 = time.time()
    store = Store(out)
    try:
        hours = [
            row[0] for row in store._conn.execute(
                "SELECT DISTINCT CAST(ts / 3600 AS INTEGER) * 3600 FROM usage_events WHERE ts IS NOT NULL"
            )
        ]
        for hour in hours:
            store._rebuild_hour(int(hour))
        counts = {
            "runs": store._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
            "usage": store._conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0],
            "samples": store._conn.execute("SELECT COUNT(*) FROM process_samples").fetchone()[0],
            "hours": len(hours),
        }
    finally:
        store.close()
    timings["buckets"] = time.time() - t1
    timings["total"] = time.time() - t0
    return {"counts": counts, "size_mb": out.stat().st_size / 1e6, "timings": timings}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=int, default=1)
    parser.add_argument("--out", required=True)
    parser.add_argument("--from", dest="source", default=None)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    profile = collect_profile(Path(args.source)) if args.source else None
    report = build(Path(args.out), args.scale, profile, args.seed)
    print(f"wrote {args.out}: {report['counts']} {report['size_mb']:.0f} MB timings={{" + ", ".join(f"{k}: {v:.1f}s" for k, v in report["timings"].items()) + "}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
