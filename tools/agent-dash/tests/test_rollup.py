"""Bounded growth: the hourly rollup preserves every served aggregate.

Rolls a fixture database (old hours across workstreams, models, and
tools, plus unpriced rows, missing-token rows, estimate rows, and a
recent hour that must stay live) and proves the pages see the same
numbers before and after: integer token sums compare exactly, costs
compare after the 6-decimal money rounding every payload applies
(float sums may differ in the last ulp across regroupings — the
codebase's own byte-identity standard), unpriced flags compare
exactly. Also covers dry runs, idempotent reruns, moves after a
rollup, and workstream changes touching rolled hours.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

from dash.store import Store
from dash.store.util import _money

NOW = 1792456383.0
HOUR = 3600
OLD = [1791714700.0, 1791714700.0 + HOUR, 1791714700.0 + 2 * HOUR]
RECENT = NOW - 3600.0


def _usage(message_id, run_id, session_id, ts, model="muse-spark-1.3-contributor", tool="muse", **kw):
    row = {
        "ts": ts,
        "session_id": session_id,
        "run_id": run_id,
        "model": model,
        "tool": tool,
        "message_id": message_id,
        "input": 1000,
        "output": 100,
        "cache_read": 500,
        "cache_write": 50,
        "reasoning": 10,
    }
    row.update(kw)
    return row


def seed(store):
    store.upsert_run({"run_id": "roll-r1", "source": "muse", "tier": "muse", "model": "muse-spark-1.3-contributor", "label": "WS1-1 first", "session_id": "roll-s1", "status": "done", "started_at": OLD[0], "ended_at": OLD[0] + 100})
    store.upsert_run({"run_id": "roll-r2", "source": "claude", "tier": "haiku", "model": "claude-haiku-5-5", "label": "WS7-2 second", "session_id": "roll-s2", "status": "done", "started_at": OLD[0], "ended_at": OLD[0] + 200})
    store.upsert_run({"run_id": "roll-r3", "source": "muse", "tier": "muse", "model": "muse-spark-1.3-contributor", "label": "no ws here", "session_id": "roll-s3", "status": "running", "started_at": OLD[1]})
    n = 0
    for run_id, session_id in (("roll-r1", "roll-s1"), ("roll-r2", "roll-s2"), ("roll-r3", "roll-s3")):
        tool = "claude" if run_id == "roll-r2" else "muse"
        model = "claude-haiku-5-5" if run_id == "roll-r2" else "muse-spark-1.3-contributor"
        for hour in OLD:
            for k in range(3):
                n += 1
                store.add_usage(_usage(f"roll-m-{n}", run_id, session_id, hour + 10 * k + 1, model=model, tool=tool))
        n += 1
        store.add_usage(_usage(f"roll-m-{n}", run_id, session_id, RECENT, model=model, tool=tool))
    n += 1
    store.add_usage(_usage(f"roll-m-{n}", "roll-r1", "roll-s1", OLD[0] + 5, model="no-such-model-zzz", tool="muse"))
    n += 1
    store.add_usage(_usage(f"roll-m-{n}", "roll-r1", "roll-s1", OLD[1] + 5, input=None, output=None, cache_read=None, cache_write=None))
    n += 1
    store.add_usage(_usage(f"roll-m-{n}", "roll-r1", "roll-s1", OLD[0] + 7, model="grok-context-model", tool="grok-context", input=999999))
    n += 1
    store.add_usage(_usage(f"roll-m-{n}", "roll-r1", "roll-s1", None))
    return n


CLOCK_KEYS = {"now", "seq", "last_data_ts"}


def normalize(value):
    """Round every float to 6 decimals so served payloads compare byte-exact.

    Poll-clock fields (now, seq, last_data_ts) are supposed to move and
    compare separately: data_seq and view_seq must NOT move (asserted
    exactly), seq must advance (asserted greater).
    """
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: normalize(val) for key, val in sorted(value.items()) if key not in CLOCK_KEYS}
    if isinstance(value, (list, tuple)):
        return [normalize(val) for val in value]
    if isinstance(value, set):
        return sorted(normalize(val) for val in value)
    return value


def aggregates(store):
    """Every aggregate the pages and API serve over hour-aligned ranges.

    Result caches are cleared first, so every aggregate recomputes from
    the tables: the proof covers the data path, not a stale cache hit
    (the rollup intentionally leaves data_seq alone, so caches stay
    valid — served bytes are identical either way).
    """
    from dash import projectphases

    store._snapshot_cache.clear()
    store._rollup_cache.clear()
    projectphases._CACHE.clear()
    out = {}
    out["fold"] = store._usage_by_run()
    out["stats"] = store.usage_model_stats()
    out["unpriced"] = store.unpriced_models(NOW, NOW - 30 * 86400, NOW)
    out["buckets_all"] = store.query_usage("model", "hour", None, OLD[0] // HOUR * HOUR, NOW)
    out["buckets_day"] = store.query_usage("workstream", "day", None, OLD[0] // HOUR * HOUR, NOW)
    out["workstreams"] = store.query_workstreams([], OLD[0] // HOUR * HOUR, None, now=NOW, bundle_meta={}, allowed_states=None)
    out["snapshot"] = store.snapshot(NOW, "all", OLD[0] // HOUR * HOUR, None)
    runs, events = store.phase_events(OLD[0] // HOUR * HOUR, NOW)
    out["phase_billed"] = sum(
        int(event.get("input_tokens") or 0) + int(event.get("output_tokens") or 0)
        + int(event.get("cache_read") or 0) + int(event.get("cache_write") or 0)
        for event in events
    )
    out["phases"] = projectphases.build_phases(store, "all", NOW)
    return out


class RollupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        seed(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_dry_run_changes_nothing(self):
        before = aggregates(self.store)
        report = self.store.rollup_usage(older_than_days=5, dry_run=True, now=NOW)
        self.assertTrue(report["dry_run"])
        self.assertGreater(report["candidate_rows"], 0)
        self.assertEqual(
            self.store._conn.execute("SELECT COUNT(*) FROM usage_rollup_1h").fetchone()[0], 0
        )
        self.assertEqual(normalize(aggregates(self.store)), normalize(before))

    def test_disabled_leaves_everything(self):
        report = self.store.rollup_usage(older_than_days=0, now=NOW)
        self.assertTrue(report.get("disabled"))
        self.assertEqual(report["rolled_runs"], 0)

    def test_rollup_preserves_every_served_aggregate(self):
        before = aggregates(self.store)
        seq_before = (self.store.seq, self.store.data_seq, self.store.view_seq)
        live_before = self.store._conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        report = self.store.rollup_usage(older_than_days=5, batch_runs=100, time_budget_s=25.0, now=NOW)
        self.assertEqual(report["failed_runs"], [])
        seq_after = (self.store.seq, self.store.data_seq, self.store.view_seq)
        self.assertGreater(seq_after[0], seq_before[0])
        self.assertEqual(seq_after[1], seq_before[1])
        self.assertEqual(seq_after[2], seq_before[2])
        self.assertGreater(report["rolled_rows"], 0)
        live_after = self.store._conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        bundled = self.store._conn.execute("SELECT COALESCE(SUM(n_events), 0) FROM usage_rollup_1h").fetchone()[0]
        self.assertEqual(live_before, live_after + report["rolled_rows"])
        self.assertEqual(bundled, report["rolled_rows"])
        # Recent rows, estimate rows, and NULL-stamp rows stay live.
        remaining_models = {
            row["model"]
            for row in self.store._conn.execute("SELECT DISTINCT model FROM usage_events").fetchall()
        }
        self.assertIn("grok-context-model", remaining_models)
        self.assertEqual(normalize(aggregates(self.store)), normalize(before))

    def test_rerun_is_a_noop(self):
        self.store.rollup_usage(older_than_days=5, batch_runs=100, time_budget_s=25.0, now=NOW)
        report = self.store.rollup_usage(older_than_days=5, batch_runs=100, time_budget_s=25.0, now=NOW)
        self.assertEqual(report["rolled_runs"], 0)
        self.assertEqual(report["failed_runs"], [])

    def test_moves_and_ws_changes_after_rollup(self):
        before = aggregates(self.store)
        self.store.rollup_usage(older_than_days=5, batch_runs=100, time_budget_s=25.0, now=NOW)
        self.assertTrue(self.store.reassign_run("roll-r3", "roll-r1"))
        self.store.upsert_run({"run_id": "roll-r1", "source": "muse", "label": "WS2-9 moved"})
        after_move = aggregates(self.store)
        # Totals are conserved across the merge; attribution follows the new label.
        self.assertEqual(
            normalize(after_move["fold"]["roll-r1"][0]),
            normalize({
                key: (before["fold"]["roll-r1"][0].get(key) or 0) + (before["fold"]["roll-r3"][0].get(key) or 0)
                if (before["fold"]["roll-r1"][0].get(key) is not None or before["fold"]["roll-r3"][0].get(key) is not None)
                else None
                for key in ("input", "output", "cache_read", "cache_write", "reasoning")
            }),
        )
        self.assertEqual(
            _money((after_move["fold"]["roll-r1"][1] or 0.0)),
            _money((before["fold"]["roll-r1"][1] or 0.0) + (before["fold"]["roll-r3"][1] or 0.0)),
        )

    def test_rolled_hours_rebuild_identically(self):
        self.store.rollup_usage(older_than_days=5, batch_runs=100, time_budget_s=25.0, now=NOW)
        before = self.store.query_usage("model", "hour", None, OLD[0] // HOUR * HOUR, NOW)
        for hour in {int(OLD[0]) // HOUR * HOUR, int(OLD[1]) // HOUR * HOUR}:
            self.store._rebuild_hour(hour)
        self.assertEqual(
            normalize(self.store.query_usage("model", "hour", None, OLD[0] // HOUR * HOUR, NOW)),
            normalize(before),
        )

    def test_stats_shape(self):
        stats = self.store.db_rollup_stats(now=NOW)
        for key in ("size_mb", "runs", "usage_event_rows", "rollup_bundle_rows", "rolled_events", "buckets"):
            self.assertIn(key, stats)
        self.assertEqual(stats["rolled_events"], 0)
        self.store.rollup_usage(older_than_days=5, batch_runs=100, time_budget_s=25.0, now=NOW)
        stats = self.store.db_rollup_stats(now=NOW)
        self.assertGreater(stats["rolled_events"], 0)
        self.assertGreater(stats["size_mb"], 0)


if __name__ == "__main__":
    unittest.main()
