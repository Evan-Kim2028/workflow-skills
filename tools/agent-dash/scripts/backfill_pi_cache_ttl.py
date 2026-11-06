#!/usr/bin/env python3
"""Backfill the pi Haiku cache-write split and price the rows left unpriced.

Usage:
  python3 scripts/backfill_pi_cache_ttl.py [--db PATH] [--apply]
  python3 scripts/backfill_pi_cache_ttl.py [--db PATH] --undo UNDO_FILE

Why: pi rows for the two Haiku names carry no 5m/1h split (rows written
before the parser carried ``cacheWrite1h``). With cache writes > 0 and two
different write rates, ``pricing._write_charge`` cannot price them, so
``est_cost_usd`` stays NULL. The catalog rows fix the rate side; this
script fixes the split side and prices the rows.

Target: ``usage_events`` rows with ``tool = 'pi'``, model in
``anthropic/claude-haiku-5-5`` or ``claude-haiku-5-5``, and
``est_cost_usd IS NULL``. For those with ``cache_write > 0`` and both split
columns NULL, the split becomes ``cache_write_5m = cache_write`` and
``cache_write_1h = 0`` (pi's rule when the field is absent on Anthropic).
Every target row is then priced by ``pricing.reprice_null_costs``, which
writes through ``store.write_costs`` so the hourly buckets update.

Dry run is the default: the database is opened read-only, the same pricing
pass runs in memory, and nothing is written. The dry run exits 3 when it
finds zero rows while unpriced Haiku rows exist (the selector no longer
matches the data). ``--apply`` writes, first saving the prior values of
every touched row to ``backfill-pi-cache-ttl-<epoch>.undo.json`` beside the
database. It exits 4 if any target row is still unpriced afterwards. A
second ``--apply`` finds zero targets and changes nothing (idempotent).

Rollback: ``--undo <file>`` restores the saved ``est_cost_usd``,
``cache_write_5m`` and ``cache_write_1h`` values for every touched row and
rebuilds the affected hourly buckets. Use it, not hand SQL: rows that had a
split before the run keep it. By hand, for a row whose split was NULL, the
equivalent is ``UPDATE usage_events SET est_cost_usd=NULL,
cache_write_5m=NULL, cache_write_1h=NULL WHERE tool='pi' AND session_id=?
AND message_id=?``, and the hourly buckets then need the rebuild that
``--undo`` runs.

Memory: reads only the Haiku pi rows (a few thousand at most). No network.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dash import pricing  # noqa: E402
from dash.store import Store  # noqa: E402
from dash.store.util import _ESTIMATE_TOOL  # noqa: E402

TOOL = "pi"
HAIKU_MODELS = ("anthropic/claude-haiku-5-5", "claude-haiku-5-5")
ROW_COLUMNS = (
    "tool, session_id, message_id, ts, model, input_tokens, output_tokens, "
    "cache_read, cache_write, cache_write_5m, cache_write_1h, reasoning"
)

EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_DRY_RUN_EMPTY = 3
EXIT_STILL_UNPRICED = 4


def default_db() -> Path:
    state = os.environ.get("AGENT_DASH_STATE_DIR") or str(Path.home() / ".local" / "share" / "agent-dash")
    return Path(os.path.expanduser(state)) / "dash.sqlite"


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _marks(values) -> str:
    return ",".join("?" for _ in values)


def target_rows(conn: sqlite3.Connection) -> list[dict]:
    """Haiku pi rows still missing a cost, with the split fill applied in memory."""
    cur = conn.execute(
        f"""SELECT {ROW_COLUMNS}, est_cost_usd FROM usage_events
            WHERE tool = ? AND model IN ({_marks(HAIKU_MODELS)}) AND est_cost_usd IS NULL
            ORDER BY ts IS NULL, ts, session_id, message_id""",
        (TOOL, *HAIKU_MODELS),
    )
    names = [col[0] for col in cur.description]
    out = []
    for values in cur.fetchall():
        item = dict(zip(names, values))
        item["prev_cache_write_5m"] = item["cache_write_5m"]
        item["prev_cache_write_1h"] = item["cache_write_1h"]
        item["split_filled"] = False
        if (item["cache_write"] or 0) > 0 and item["cache_write_5m"] is None and item["cache_write_1h"] is None:
            item["cache_write_5m"] = item["cache_write"]
            item["cache_write_1h"] = 0
            item["split_filled"] = True
        out.append(item)
    return out


def unpriced_haiku(conn: sqlite3.Connection) -> int:
    return conn.execute(
        f"""SELECT COUNT(*) FROM usage_events
            WHERE model IN ({_marks(HAIKU_MODELS)}) AND est_cost_usd IS NULL
              AND COALESCE(tool, '') != ?""",
        (*HAIKU_MODELS, _ESTIMATE_TOOL),
    ).fetchone()[0]


class _Feed:
    """The two store calls ``reprice_null_costs`` makes, over the planned rows.

    ``writer`` is the real store on ``--apply``. On a dry run it is None,
    so the updates are only collected and nothing is written.
    """

    def __init__(self, rows: list[dict], writer=None):
        self._rows = rows
        self._writer = writer
        self.updates: list = []

    def usage_events_missing_cost(self, limit: int = 0) -> list[dict]:
        return list(self._rows)

    def write_costs(self, updates, hours=None) -> None:
        self.updates = list(updates)
        if self._writer is not None:
            self._writer.write_costs(updates, hours=hours)


def _price(rows: list[dict], writer=None) -> dict:
    feed = _Feed(rows, writer)
    summary = pricing.reprice_null_costs(feed, limit=len(rows) + 1)
    summary["updates"] = feed.updates
    return summary


def _report(rows: list[dict], summary: dict, unpriced: int, apply: bool) -> None:
    """Print the rows touched and the USD priced, per model."""
    model_of = {(r["tool"], r["session_id"], r["message_id"]): r["model"] for r in rows}
    by_model: dict = {}
    for row in rows:
        by_model.setdefault(row["model"], {"rows": 0, "priced": 0, "usd": 0.0})["rows"] += 1
    total = 0.0
    for est, _five, _one, tool, session_id, message_id in summary["updates"]:
        slot = by_model[model_of[(tool, session_id, message_id)]]
        slot["priced"] += 1
        slot["usd"] += est
        total += est
    mode = "applied" if apply else "dry run"
    print(f"pi Haiku cache-TTL backfill ({mode})")
    print(f"  target rows (pi, Haiku names, est_cost_usd NULL): {len(rows)}")
    print(f"  split filled (cache_write > 0, split NULL -> 5m=cache_write, 1h=0): {sum(1 for r in rows if r['split_filled'])}")
    print(f"  priced: {summary['priced']}  still unpriced: {summary['still_null']}")
    for model in sorted(by_model):
        slot = by_model[model]
        print(f"  {model}: {slot['rows']} rows, {slot['priced']} priced, ${slot['usd']:.6f}")
    print(f"  total USD priced: {total:.6f}")
    print(f"  unpriced Haiku rows in database (any tool, before this run): {unpriced}")


def _write_undo(db: Path, rows: list[dict]) -> Path:
    path = db.with_name("backfill-pi-cache-ttl-%d.undo.json" % int(time.time()))
    saved = [
        {
            "tool": r["tool"],
            "session_id": r["session_id"],
            "message_id": r["message_id"],
            "ts": r["ts"],
            "est_cost_usd": None,
            "cache_write_5m": r["prev_cache_write_5m"],
            "cache_write_1h": r["prev_cache_write_1h"],
        }
        for r in rows
    ]
    path.write_text(json.dumps({"db": str(db), "rows": saved}, indent=1), encoding="utf-8")
    return path


def run(db: Path, apply: bool) -> int:
    if not db.is_file():
        print(f"no database at {db}. Pass --db PATH to a dash.sqlite copy.", file=sys.stderr)
        return EXIT_BAD_INPUT
    conn = _connect_ro(db)
    try:
        rows = target_rows(conn)
        unpriced = unpriced_haiku(conn)
    finally:
        conn.close()
    if not apply:
        summary = _price(rows)
        _report(rows, summary, unpriced, apply=False)
        if not rows and unpriced:
            print("dry run found zero rows, but unpriced Haiku rows exist: the selector no longer matches.")
            return EXIT_DRY_RUN_EMPTY
        return EXIT_OK
    if not rows:
        print(f"nothing to do: no pi Haiku rows without a cost in {db}")
        return EXIT_OK
    undo = _write_undo(db, rows)
    store = Store(str(db))
    try:
        summary = _price(rows, writer=store)
    finally:
        store.close()
    _report(rows, summary, unpriced, apply=True)
    print(f"undo file: {undo}")
    if summary["still_null"]:
        print(f"{summary['still_null']} target rows are still unpriced; check the catalog rows.")
        return EXIT_STILL_UNPRICED
    return EXIT_OK


def undo(db: Path, undo_file: Path) -> int:
    data = json.loads(Path(undo_file).read_text(encoding="utf-8"))
    rows = data.get("rows") or []
    hours = {int(float(r["ts"]) // 3600) * 3600 for r in rows if r.get("ts") is not None}
    store = Store(str(db))
    try:
        with store._lock:
            store._conn.execute("BEGIN IMMEDIATE")
            try:
                store._conn.executemany(
                    """UPDATE usage_events SET est_cost_usd=?, cache_write_5m=?, cache_write_1h=?
                       WHERE tool=? AND session_id=? AND message_id=?""",
                    [
                        (r["est_cost_usd"], r["cache_write_5m"], r["cache_write_1h"], r["tool"], r["session_id"], r["message_id"])
                        for r in rows
                    ],
                )
                store._conn.execute("COMMIT")
            except Exception:
                store._conn.execute("ROLLBACK")
                raise
        store.write_costs([], hours=hours)
    finally:
        store.close()
    print(f"restored {len(rows)} rows from {undo_file}")
    return EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=None, help="dash.sqlite path (default: the state dir's)")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--undo", type=Path, default=None, help="restore rows from an undo file")
    args = parser.parse_args(argv)
    db = args.db or default_db()
    if args.undo is not None:
        return undo(db, args.undo)
    return run(db, args.apply)


if __name__ == "__main__":
    sys.exit(main())
