import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash.pricing import cost
from dash.sources.hub import poll as hub_poll
from dash.sources.process import apply_liveness, label_from_args, parse_etime, parse_ps, poll as process_poll, reconcile_running
from dash.store import Store

HUB = "00000000-0000-0000-0000-000000000001"


def memory(path: Path) -> Store:
    return Store(path / "dash.sqlite")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_sample_run_index_exists(self):
        names = {
            row[0]
            for row in self.store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        self.assertIn("idx_samples_run", names)

    def test_wal_and_offsets(self):
        mode = self.store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(str(mode).lower(), "wal")
        self.assertIsNone(self.store.get_offset("/tmp/a"))
        self.store.set_offset("/tmp/a", 7, 10, 10, 1.0)
        self.assertEqual(self.store.get_offset("/tmp/a"), (7, 10))
        seq = self.store.data_seq
        view = self.store.view_seq
        self.store.set_offset("/tmp/a", 7, 10, 12, 2.0)
        self.assertEqual(self.store.data_seq, seq)
        self.store.set_offset("/tmp/a", 8, 0, 4, 3.0)
        self.assertEqual(self.store.get_offset("/tmp/a"), (8, 0))
        # Read cursors are invisible to the page: they move the poll
        # sequence (liveness) but never the data or view sequences, so
        # an offset write alone keeps the snapshot cache hot.
        self.assertEqual(self.store.data_seq, seq)
        self.assertEqual(self.store.view_seq, view)

    def test_attribution_and_out_growing(self):
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws7-landing",
                "source": "muse",
                "tier": "muse",
                "model": "muse-spark-1.3-contributor",
                "label": "ws7-landing",
                "session_id": "child",
                "status": "running",
                "started_at": 10,
                "out_bytes": 10,
            }
        )
        snap = self.store.snapshot(20, "all")
        run = snap["running"][0]
        self.assertEqual(run["workstream"], "WS7")
        self.assertEqual(run["ws_confidence"], "regex")
        self.assertFalse(run["out_growing"])
        self.assertEqual(run["elapsed_s"], 10)
        self.store.upsert_run({"run_id": "muse:s:ws7-landing", "out_bytes": 25})
        snap = self.store.snapshot(30, "all")
        self.assertTrue(snap["running"][0]["out_growing"])
        self.store.upsert_run({"run_id": "muse:s:ws7-landing", "out_bytes": 25})
        snap = self.store.snapshot(40, "all")
        self.assertFalse(snap["running"][0]["out_growing"])

    def test_long_label_is_dropped(self):
        self.store.upsert_run(
            {
                "run_id": "r",
                "label": "p" * 500,
                "session_id": HUB,
                "status": "running",
                "started_at": 1,
            }
        )
        run = self.store.snapshot(2)["running"][0]
        self.assertIsNone(run["label"])
        self.assertEqual(run["workstream"], "WS1")
        self.assertEqual(run["ws_confidence"], "session-default")

    def test_child_label_miss_stays_blank(self):
        self.store.upsert_run(
            {
                "run_id": "r",
                "label": "hello",
                "session_id": HUB,
                "parent_session_id": "parent",
                "status": "running",
                "started_at": 1,
            }
        )
        run = self.store.snapshot(2)["running"][0]
        self.assertIsNone(run["workstream"])
        self.assertIsNone(run["ws_confidence"])

    def test_usage_last_wins_and_buckets(self):
        self.store.upsert_run(
            {
                "run_id": "claude-main:s:main",
                "source": "claude-main",
                "tier": "opus",
                "model": "claude-opus-5-5",
                "label": "ws1-12",
                "session_id": "s",
                "status": "done",
                "started_at": 1,
                "ended_at": 50,
            }
        )
        self.store.add_usage(
            {
                "ts": 100,
                "session_id": "s",
                "run_id": "claude-main:s:main",
                "model": "claude-opus-5-5",
                "tool": "claude",
                "message_id": "m1",
                "input": 1000,
                "output": 10,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 4,
            }
        )
        self.store.add_usage(
            {
                "ts": 110,
                "session_id": "s",
                "run_id": "claude-main:s:main",
                "model": "claude-opus-5-5",
                "tool": "claude",
                "message_id": "m1",
                "input": 2000,
                "output": 20,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 5,
            }
        )
        self.store.add_usage(
            {
                "ts": 3700,
                "session_id": "s",
                "run_id": "claude-main:s:main",
                "model": "claude-opus-5-5",
                "tool": "claude",
                "message_id": "m2",
                "input": 1,
                "output": 1,
                "cache_read": 1,
                "cache_write": 1,
                "reasoning": 0,
            }
        )
        count = self.store._conn.execute("SELECT COUNT(*) AS n FROM usage_events").fetchone()["n"]
        self.assertEqual(count, 2)
        stored = self.store._conn.execute(
            "SELECT est_cost_usd FROM usage_events WHERE message_id='m1'"
        ).fetchone()["est_cost_usd"]
        self.assertAlmostEqual(stored, cost("claude-opus-5-5", 2000, 20, 0, 0))
        hours = self.store.query_usage("model", "hour", None, None)
        self.assertEqual([(row["t"], row["key"], row["input"]) for row in hours], [(0, "opus", 2000), (3600, "opus", 1)])
        days = self.store.query_usage("model", "day", "WS1", None)
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0]["input"], 2001)
        by_ws = self.store.query_usage("workstream", "hour", None, None)
        self.assertEqual(by_ws[0]["key"], "WS1")
        seq = self.store.data_seq
        self.store.add_usage(
            {
                "ts": 110,
                "session_id": "s",
                "run_id": "claude-main:s:main",
                "model": "claude-opus-5-5",
                "tool": "claude",
                "message_id": "m1",
                "input": 2000,
                "output": 20,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 5,
            }
        )
        self.assertEqual(self.store.data_seq, seq)

    def test_unpriced_usage(self):
        self.store.upsert_run(
            {
                "run_id": "glm:s:rev",
                "source": "glm",
                "tier": "glm",
                "model": "glm-unlisted",
                "label": "glm-review",
                "status": "done",
                "started_at": 1,
                "ended_at": 2,
            }
        )
        self.store.add_usage(
            {
                "ts": 1,
                "session_id": "s",
                "run_id": "glm:s:rev",
                "model": "glm-unlisted",
                "tool": "glm",
                "message_id": "m",
                "input": 50,
                "output": 5,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 3,
            }
        )
        snap = self.store.snapshot(10)
        self.assertEqual(snap["totals"]["unpriced_runs"], 1)
        self.assertEqual(snap["totals"]["tokens"]["input"], 50)
        self.assertEqual(snap["totals"]["tokens"]["reasoning"], 3)
        self.assertEqual(snap["totals"]["cost_usd"], 0.0)
        self.assertIsNone(snap["recent"][0]["cost_usd"])
        buckets = self.store.query_usage("model", "hour", None, None)
        self.assertEqual(buckets[0]["key"], "glm-unlisted")
        self.assertIsNone(buckets[0]["cost_usd"])

    def test_zero_token_unpriced_model_is_ignored(self):
        def add(model, message, tokens):
            self.store.add_usage(
                {
                    "ts": 1,
                    "session_id": "s-synth",
                    "run_id": "claude-main:s-synth:marker",
                    "model": model,
                    "tool": "claude",
                    "message_id": message,
                    "input": tokens,
                    "output": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "reasoning": 0,
                }
            )

        add("<synthetic>", "m-zero", 0)
        self.assertEqual(self.store.unpriced_models(10, 0, 100), [])
        snap = self.store.snapshot(10)
        self.assertEqual(snap["totals"]["unpriced_models"], [])
        self.assertEqual(snap["totals"]["unpriced_tokens"], 0)

        add("<synthetic>", "m-one", 1)
        self.assertEqual(
            self.store.unpriced_models(10, 0, 100),
            [{"model": "<synthetic>", "rows": 2, "tokens": 1}],
        )
        snap = self.store.snapshot(10)
        self.assertEqual(snap["totals"]["unpriced_models"], ["<synthetic>"])
        self.assertEqual(snap["totals"]["unpriced_tokens"], 1)

    def test_priced_model_never_unpriced(self):
        self.store.add_usage(
            {
                "ts": 1,
                "session_id": "s-priced",
                "run_id": "claude-main:s-priced:r",
                "model": "claude-sonnet-5-5",
                "tool": "claude",
                "message_id": "m-priced",
                "input": 5,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 0,
            }
        )
        self.assertEqual(self.store.unpriced_models(10, 0, 100), [])

    def test_missing_field_nulls_cost(self):
        self.store.upsert_run(
            {
                "run_id": "r",
                "source": "claude-main",
                "tier": "opus",
                "model": "claude-opus-5-5",
                "label": "ws1-12",
                "status": "done",
                "started_at": 1,
                "ended_at": 2,
            }
        )
        self.store.add_usage(
            {
                "ts": 1,
                "session_id": "s",
                "run_id": "r",
                "model": "claude-opus-5-5",
                "tool": "claude",
                "message_id": "m",
                "output": 10,
                "cache_read": 0,
                "cache_write": 0,
            }
        )
        run = self.store.snapshot(5)["recent"][0]
        self.assertIsNone(run["cost_usd"])
        self.assertIsNone(run["tokens"]["input"])
        self.assertEqual(self.store.snapshot(5)["totals"]["unpriced_runs"], 1)

    def test_snapshot_keys_are_closed(self):
        now = 10_000.0
        self.store.upsert_run(
            {"run_id": "h", "tier": "haiku", "label": "ws1-12", "status": "running", "started_at": now - 21 * 60}
        )
        self.store.upsert_run(
            {
                "run_id": "dead",
                "tier": "haiku",
                "label": "ws3-x",
                "status": "died_early",
                "started_at": now - 700,
                "ended_at": now - 600,
            }
        )
        snap = self.store.snapshot(now)
        self.assertEqual(
            set(snap),
            {"now", "seq", "data_seq", "view_seq", "last_data_ts", "counts", "running", "recent", "recent_shown", "recent_total", "recent_limit", "recent_offset", "totals", "prev_24h", "totals_all_time", "last_activity", "by_workstream"},
        )
        self.assertEqual(snap["counts"]["live_agents"], 0)
        recent_ids = [run["run_id"] for run in snap["recent"]]
        self.assertEqual(recent_ids, ["dead"])

    def test_recent_cap(self):
        now = 5_000.0
        for index in range(60):
            self.store.upsert_run(
                {
                    "run_id": f"r{index}",
                    "label": "ws1-12",
                    "status": "done",
                    "started_at": now - 100 - index,
                    "ended_at": now - index,
                }
            )
        recent = self.store.snapshot(now)["recent"]
        self.assertEqual(len(recent), 50)
        self.assertEqual(recent[0]["run_id"], "r0")

    def test_recent_truncation_counts(self):
        now = 5_000.0
        for index in range(60):
            self.store.upsert_run(
                {
                    "run_id": f"t{index}",
                    "label": "ws1-12",
                    "status": "done",
                    "started_at": now - 100 - index,
                    "ended_at": now - index,
                }
            )
        snap = self.store.snapshot(now)
        self.assertEqual(snap["recent_shown"], 50)
        self.assertEqual(snap["recent_total"], 60)
        self.assertEqual(len(snap["recent"]), 50)

    def test_snapshot_uses_one_usage_scan(self):
        now = 9_000.0
        for index in range(8):
            self.store.upsert_run(
                {
                    "run_id": f"u{index}",
                    "source": "muse",
                    "tier": "muse",
                    "label": f"ws{(index % 4) + 1}-task",
                    "session_id": f"sess-{index}",
                    "status": "done",
                    "started_at": now - 200,
                    "ended_at": now - 100,
                }
            )
            self.store.add_usage(
                {
                    "ts": now - 150,
                    "session_id": f"sess-{index}",
                    "run_id": f"u{index}",
                    "model": "claude-sonnet-4-5",
                    "tool": "muse",
                    "message_id": f"m{index}",
                    "input": 100,
                    "output": 50,
                    "cache_read": 0,
                    "cache_write": 0,
                }
            )
        self.store._usage_cache = None
        calls = []
        orig = self.store._usage_by_run_uncached

        def counting():
            calls.append(1)
            return orig()

        self.store._usage_by_run_uncached = counting
        try:
            before = self.store.snapshot(now, "all")
            self.assertLessEqual(len(calls), 1, f"snapshot scanned usage {len(calls)}x")
            report = self.store.reconcile_numbers(now, now - 86400, now, processes=[])
            self.assertLessEqual(len(calls), 2, f"snapshot+reconcile scanned usage {len(calls)}x")
        finally:
            self.store._usage_by_run_uncached = orig
        after = self.store.snapshot(now, "all")
        self.assertEqual(before["counts"], after["counts"])
        self.assertEqual(before["totals"], after["totals"])
        self.assertEqual(
            [run["run_id"] for run in before["recent"]],
            [run["run_id"] for run in after["recent"]],
        )

    def test_reconcile_needs_no_per_workstream_snapshot(self):
        now = 11_000.0
        for index in range(4):
            self.store.upsert_run(
                {
                    "run_id": f"w{index}",
                    "source": "muse",
                    "tier": "muse",
                    "model": "muse-spark-1.3-contributor",
                    "label": f"ws{index + 1}-task",
                    "status": "done",
                    "started_at": now - 200,
                    "ended_at": now - 100,
                }
            )
        orig = self.store.snapshot
        self.store.snapshot = lambda *a, **k: (_ for _ in ()).throw(AssertionError("reconcile re-ran snapshot per workstream"))
        try:
            report = self.store.reconcile_numbers(now, now - 86400, now, processes=[])
        finally:
            self.store.snapshot = orig
        self.assertEqual(report["mismatches"], [])

    def test_reconcile_flags_recent_truncation(self):
        now = 13_000.0
        for index in range(55):
            self.store.upsert_run(
                {
                    "run_id": f"g{index}",
                    "source": "muse",
                    "tier": "muse",
                    "model": "muse-spark-1.3-contributor",
                    "label": "ws1-task",
                    "status": "done",
                    "started_at": now - 300,
                    "ended_at": now - 100,
                }
            )
        report = self.store.reconcile_numbers(now, now - 86400, now, processes=[])
        self.assertFalse(
            any("recent shows 50 of" in item for item in report["mismatches"]),
            report["mismatches"],
        )
        self.assertEqual(report["recent_shown"], 50)
        self.assertEqual(report["recent_total"], 55)

    def test_snapshot_cache_between_writes(self):
        now = 15_000.0
        self.store.upsert_run(
            {
                "run_id": "c1",
                "source": "muse",
                "tier": "muse",
                "model": "muse-spark-1.3-contributor",
                "label": "ws1-task",
                "status": "done",
                "started_at": now - 200,
                "ended_at": now - 100,
            }
        )
        first = self.store.snapshot_cached(now, "all")
        seq = self.store._seq
        second = self.store.snapshot_cached(now + 2, "all")
        self.assertEqual(first["seq"], seq)
        self.assertEqual(second["seq"], seq)
        self.assertEqual(
            [run["run_id"] for run in first["recent"]],
            [run["run_id"] for run in second["recent"]],
        )
        self.assertEqual(first["counts"], second["counts"])
        self.store.upsert_run(
            {
                "run_id": "c2",
                "source": "muse",
                "tier": "muse",
                "model": "muse-spark-1.3-contributor",
                "label": "ws2-task",
                "status": "done",
                "started_at": now - 200,
                "ended_at": now - 50,
            }
        )
        third = self.store.snapshot_cached(now + 4, "all")
        self.assertGreater(third["seq"], seq)
        self.assertEqual(third["recent_total"], first["recent_total"] + 1)

    def test_snapshot_benchmark(self):
        import time as _t

        now = 17_000.0
        for index in range(2000):
            self.store.upsert_run(
                {
                    "run_id": f"b{index}",
                    "source": "muse",
                    "tier": "muse",
                    "label": f"ws{(index % 8) + 1}-bench",
                    "session_id": f"bsess-{index % 500}",
                    "status": "done",
                    "started_at": now - 300,
                    "ended_at": now - 100,
                }
            )
        for index in range(2000):
            self.store.add_usage(
                {
                    "ts": now - 150,
                    "session_id": f"bsess-{index % 500}",
                    "run_id": f"b{index}",
                    "model": "claude-sonnet-4-5",
                    "tool": "muse",
                    "message_id": f"bm{index}",
                    "input": 100,
                    "output": 50,
                    "cache_read": 0,
                    "cache_write": 0,
                }
            )
        self.store._usage_cache = None
        start = _t.time()
        snap = self.store.snapshot(now, "all", usage_group="model")
        snap_dt = _t.time() - start
        start = _t.time()
        report = self.store.reconcile_numbers(now, now - 86400, now, processes=[])
        rec_dt = _t.time() - start
        self.assertLess(snap_dt, 2.0, f"snapshot {snap_dt:.2f}s over 2s for 2k runs")
        self.assertLess(rec_dt, 2.0, f"reconcile {rec_dt:.2f}s over 2s for 2k runs")
        self.assertIn("recent_shown", snap)

    def test_workflow_collapse(self):
        now = 100.0
        self.store.upsert_run(
            {
                "run_id": "claude-workflow:s:wf_f3bb66d3-492",
                "source": "claude-workflow",
                "tier": "sonnet",
                "label": "review",
                "session_id": "s",
                "status": "done",
                "started_at": 10,
                "ended_at": 40,
                "out_bytes": 10,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "claude-workflow:s:wf_f3bb66d3-492:a1",
                "source": "claude-workflow",
                "tier": "sonnet",
                "label": "a1",
                "session_id": "s",
                "status": "running",
                "started_at": 12,
                "out_bytes": 5,
            }
        )
        self.store.add_usage(
            {
                "ts": 12,
                "session_id": "s",
                "run_id": "claude-workflow:s:wf_f3bb66d3-492:a1",
                "model": "claude-sonnet-4-5",
                "tool": "claude",
                "message_id": "m",
                "input": 100,
                "output": 4,
                "cache_read": 0,
                "cache_write": 0,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "claude-main:s:main",
                "source": "claude-main",
                "tier": "opus",
                "label": "ws1-12",
                "session_id": "s",
                "status": "done",
                "started_at": 1,
                "ended_at": 2,
            }
        )
        runs = self.store.query_runs(now, None, None, None, 100)
        self.assertEqual([run["run_id"] for run in runs], ["claude-main:s:main", "claude-workflow:s:wf_f3bb66d3-492"])
        workflow = runs[1]
        self.assertEqual(workflow["children"], 1)
        self.assertEqual(workflow["status"], "running")
        self.assertIsNone(workflow["ended_at"])
        self.assertEqual(workflow["out_bytes"], 15)
        self.assertEqual(workflow["tokens"]["input"], 100)
        self.assertNotIn("children", runs[0])

    def test_concurrency(self):
        self.store.upsert_run(
            {
                "run_id": "a",
                "source": "grok",
                "tier": "grok",
                "model": "grok-4.7-high",
                "label": "dash-a",
                "status": "done",
                "started_at": 0,
                "ended_at": 100,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "b",
                "source": "muse",
                "tier": "muse",
                "model": "muse-spark-1.3-contributor",
                "label": "ws7-notes",
                "status": "done",
                "started_at": 50,
                "ended_at": 150,
            }
        )
        points = self.store.query_concurrency(300, None, 0, 50)
        by_t = {point["t"]: point for point in points}
        self.assertEqual(by_t[0]["grok"], 1)
        self.assertEqual(by_t[50]["grok"], 1)
        self.assertEqual(by_t[50]["muse"], 1)
        self.assertEqual(by_t[100]["grok"], 0)
        self.assertEqual(by_t[100]["muse"], 1)
        self.assertEqual(by_t[150]["muse"], 0)

    def test_sessions_and_workstreams(self):
        now = time.time()
        self.store.upsert_session(
            {"session_id": HUB, "tool": "claude", "cwd": "/tmp/demo", "started_at": now - 100, "status": "busy"}
        )
        self.store.upsert_run(
            {
                "run_id": "r",
                "source": "claude-main",
                "tier": "opus",
                "model": "claude-opus-5-5",
                "session_id": HUB,
                "status": "running",
                "started_at": now - 100,
            }
        )
        self.store.add_usage(
            {
                "ts": now - 50,
                "session_id": HUB,
                "run_id": "r",
                "model": "claude-opus-5-5",
                "tool": "claude",
                "message_id": "m",
                "input": 100,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
            }
        )
        self.store.set_ws_counts(
            [{"ws": "WS1", "name": "Identity", "backlog": 14, "todo": 0, "doing": 0, "review": 0, "done": 0, "cancelled": 0}]
        )
        sessions = self.store.query_sessions()
        self.assertEqual(sessions[0]["session_id"], HUB)
        self.assertEqual(sessions[0]["workstream"], "WS1")
        self.assertEqual(sessions[0]["runs"], 1)
        self.assertEqual(sessions[0]["tokens"], 100)
        boards = self.store.query_workstreams({"WS7"})
        ws1 = next(row for row in boards if row["ws"] == "WS1")
        self.assertEqual(ws1["name"], "Identity")
        self.assertEqual(ws1["tk"]["backlog"], 14)
        self.assertEqual(ws1["live_agents"], 0)
        self.assertEqual(ws1["finished_in_range"], 0)
        self.assertEqual(ws1["tokens"], 100)
        self.assertFalse(ws1["plan"])

    def test_sample_retention(self):
        now = 100_000.0
        self.store.add_process_sample(
            {"ts": now - 49 * 3600, "pid": 1, "comm": "claude", "rss_kb": 1, "elapsed_s": 1, "label": None}
        )
        self.store.add_process_sample(
            {"ts": now, "pid": 2, "comm": "claude", "rss_kb": 2, "elapsed_s": 1, "label": None}
        )
        count = self.store._conn.execute("SELECT COUNT(*) AS n FROM process_samples").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_ws_filter(self):
        now = 50.0
        self.store.upsert_run(
            {"run_id": "a", "label": "ws7-landing", "status": "running", "started_at": 1}
        )
        self.store.upsert_run(
            {"run_id": "b", "label": "ws1-12", "status": "running", "started_at": 1}
        )
        snap = self.store.snapshot(now, "WS7")
        self.assertEqual([run["run_id"] for run in snap["running"]], ["a"])
        self.assertEqual([row["ws"] for row in snap["by_workstream"]], ["WS7"])

    def test_scoped_counts_exclude_children_and_main_sessions(self):
        now = time.time()
        self.store.upsert_run(
            {
                "run_id": "grok:parent:ws1-parent",
                "source": "grok",
                "tier": "grok",
                "label": "ws1-parent",
                "session_id": "parent-chat",
                "status": "running",
                "started_at": now - 100,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "grok:child:child",
                "source": "grok",
                "tier": "grok",
                "session_id": "child-chat",
                "parent_session_id": "parent-chat",
                "status": "running",
                "started_at": now - 90,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "grok:parent:parent",
                "source": "grok",
                "tier": "grok",
                "session_id": "parent-chat",
                "status": "running",
                "started_at": now - 100,
            }
        )
        self.store.add_usage(
            {
                "ts": now - 80,
                "session_id": "child-chat",
                "run_id": "grok:child:child",
                "model": "grok-4",
                "tool": "cursor",
                "message_id": "m1",
                "input": 5,
                "output": 1,
                "cache_read": 0,
                "cache_write": 0,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "claude-main:s:main",
                "source": "claude-main",
                "tier": "opus",
                "label": "ws1-schema",
                "session_id": "s",
                "status": "running",
                "started_at": now - 50,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "muse:m:ws7-plan",
                "source": "muse",
                "tier": "muse",
                "label": "ws7-plan",
                "status": "done",
                "verdict": "PASS",
                "started_at": now - 200,
                "ended_at": now - 30,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "grok:old:ws2-old",
                "source": "grok",
                "tier": "grok",
                "label": "ws2-old",
                "status": "done",
                "verdict": "PARTIAL",
                "started_at": now - 3 * 86400,
                "ended_at": now - 2 * 86400,
            }
        )
        snap = self.store.snapshot(now, "all")
        self.assertNotIn("caps", snap)
        counts = snap["counts"]
        self.assertEqual(counts["live_agents"], 1)
        self.assertEqual(counts["live_by_tool"], {"grok": 1, "muse": 0, "glm": 0, "claude": 0, "deepseek": 0, "codex": 0, "other": 0})
        self.assertEqual(counts["finished_today"], 1)
        self.assertEqual(counts["finished_in_range"], 1)
        self.assertEqual(counts["finished_by_verdict"], {"ok": 1})
        running = [run["run_id"] for run in snap["running"]]
        self.assertIn("grok:parent:ws1-parent", running)
        self.assertIn("claude-main:s:main", running)
        self.assertNotIn("grok:child:child", running)
        self.assertNotIn("grok:parent:parent", running)
        parent = next(run for run in snap["running"] if run["run_id"] == "grok:parent:ws1-parent")
        self.assertEqual(parent["tokens"]["input"], 5)
        ws1 = self.store.snapshot(now, "WS1")
        self.assertEqual(ws1["counts"]["live_agents"], 1)
        self.assertEqual(ws1["counts"]["finished_today"], 0)
        ws7 = self.store.snapshot(now, "WS7")
        self.assertEqual(ws7["counts"]["live_agents"], 0)
        self.assertEqual(ws7["counts"]["finished_today"], 1)
        ranged = self.store.snapshot(now, "all", since=now - 10)
        self.assertEqual(ranged["counts"]["finished_in_range"], 0)

    def test_killed_runs_count_as_died_not_none(self):
        """Runs that never finish (killed / died_early) land in the
        finished_by_verdict "died" bucket even when they carry a verdict,
        so the dashboard colours died apart from blocked."""
        now = time.time()
        self.store.upsert_run(
            {
                "run_id": "grok:k:ws1-doomed",
                "source": "grok",
                "tier": "grok",
                "label": "ws1-doomed",
                "status": "killed",
                "verdict": "BLOCKED",
                "started_at": now - 200,
                "ended_at": now - 30,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "claude:d:ws1-gone",
                "source": "claude-subagent",
                "tier": "haiku",
                "label": "ws1-gone",
                "status": "died_early",
                "started_at": now - 200,
                "ended_at": now - 30,
            }
        )
        snap = self.store.snapshot(now, "all")
        counts = snap["counts"]
        self.assertEqual(counts["finished_in_range"], 2)
        self.assertEqual(counts["finished_by_verdict"], {"died": 2})

    def test_null_started_at_is_not_written_over_a_known_start(self):
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws2-notes",
                "source": "muse",
                "tier": "muse",
                "label": "ws2-notes",
                "status": "running",
                "started_at": 1792167605.0,
                "clock_source": "manifest",
            }
        )
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws2-notes",
                "source": "muse",
                "started_at": None,
                "status": "running",
            }
        )
        row = self.store.get_run("muse:s:ws2-notes")
        self.assertEqual(row["started_at"], 1792167605.0)
        self.assertEqual(row["clock_source"], "manifest")

    def test_elapsed_is_null_when_the_start_is_unknown(self):
        self.store.upsert_run(
            {
                "run_id": "muse:s:bare",
                "source": "muse",
                "tier": "muse",
                "model": "muse-spark-1.3-contributor",
                "label": "bare",
                "status": "unknown",
            }
        )
        runs = self.store.query_runs(1000.0, None, None, None, 10)
        self.assertEqual(len(runs), 1)
        self.assertIsNone(runs[0]["started_at"])
        self.assertIsNone(runs[0]["elapsed_s"])
        self.assertIsNone(runs[0]["context_tokens"])

    def test_query_runs_limit_keeps_the_newest(self):
        for i in range(5):
            self.store.upsert_run(
                {
                    "run_id": f"muse:s:run{i}",
                    "source": "muse",
                    "tier": "muse",
                    "model": "muse-spark-1.3-contributor",
                    "label": f"run{i}",
                    "status": "done",
                    "started_at": 1000.0 + i,
                    "ended_at": 1000.5 + i,
                }
            )
        runs = self.store.query_runs(2000.0, None, None, None, 3)
        self.assertEqual([run["label"] for run in runs], ["run2", "run3", "run4"])

    def test_context_tokens_are_latest_and_excluded_from_totals(self):
        now = 1_700_000_000.0
        run_id = "grok:chat:ws1-47-extract-rebase"
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": "grok",
                "tier": "grok",
                "model": "grok-4.7-high",
                "label": "ws1-47-extract-rebase",
                "session_id": "chat",
                "status": "running",
                "started_at": now - 10,
            }
        )
        self.store.add_usage(
            {
                "ts": now - 5,
                "session_id": "chat",
                "run_id": run_id,
                "model": "grok-4",
                "tool": "grok-context",
                "message_id": "ctx-1",
                "input": 1000,
                "output": 500000,
            }
        )
        self.store.add_usage(
            {
                "ts": now - 1,
                "session_id": "chat",
                "run_id": run_id,
                "model": "grok-4",
                "tool": "grok-context",
                "message_id": "ctx-2",
                "input": 113622,
                "output": 500000,
            }
        )
        snap = self.store.snapshot(now, "all")
        run = snap["running"][0]
        self.assertEqual(run["context_tokens"], 113622)
        self.assertEqual(run["tokens"]["input"], 0)
        self.assertEqual(snap["totals"]["tokens"]["input"], 0)
        listed = self.store.query_runs(now, None, None, None, 10)
        self.assertEqual(listed[0]["context_tokens"], 113622)
        buckets = self.store.query_usage("tool", "hour", None, None)
        self.assertEqual(sum(row["input"] for row in buckets), 0)

    def test_reassign_keeps_the_labeled_start_and_raises_the_end(self):
        src = "muse:s:s"
        dst = "muse:s:ws2-notes"
        self.store.upsert_run(
            {
                "run_id": dst,
                "source": "muse",
                "tier": "muse",
                "label": "ws2-notes",
                "session_id": "s",
                "status": "done",
                "started_at": 1792167605.0,
                "ended_at": 1792167605.0,
                "clock_source": "manifest",
            }
        )
        self.store.upsert_run(
            {
                "run_id": src,
                "source": "muse",
                "tier": "muse",
                "session_id": "s",
                "status": "unknown",
                "started_at": None,
                "ended_at": 1792167600.0,
            }
        )
        self.store.add_usage(
            {
                "ts": 1792167848.6732,
                "session_id": "s",
                "run_id": src,
                "tool": "muse",
                "message_id": "late",
                "input": 12,
                "output": 1,
            }
        )
        self.assertTrue(self.store.reassign_run(src, dst))
        kept = self.store.get_run(dst)
        self.assertIsNone(self.store.get_run(src))
        self.assertEqual(kept["started_at"], 1792167605.0)
        self.assertEqual(kept["ended_at"], 1792167848.6732)


class ReconcileTest(unittest.TestCase):
    """One source of truth: ws rows plus unassigned equal the all-scope numbers,
    and each ws row equals that workstream tab's snapshot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))
        self.now = 1_761_999_960.0
        self.since = self.now - 12 * 3600
        self.until = self.now

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _run(self, run_id, source, tier, label, status, started, ended, session=None):
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": source,
                "tier": tier,
                "model": "claude-sonnet-5-5",
                "label": label,
                "session_id": session or run_id,
                "status": status,
                "started_at": started,
                "ended_at": ended,
            }
        )

    def _usage(self, ts, session, run_id, message, tokens):
        self.store.add_usage(
            {
                "ts": ts,
                "session_id": session,
                "run_id": run_id,
                "model": "claude-sonnet-5-5",
                "tool": "claude",
                "message_id": message,
                "input": tokens,
                "output": tokens // 2,
                "cache_read": 0,
                "cache_write": 0,
            }
        )

    def seed(self):
        now = self.now
        self._run("grok:a:ws1-a", "grok", "grok", "ws1-a", "running", now - 100, None)
        self._run("muse:b:ws1-b", "muse", "muse", "ws1-b", "running", now - 200, None)
        self._run("muse:c:ws1-c", "muse", "muse", "ws1-c", "done", now - 5000, now - 4000, session="s-c")
        self._usage(now - 4100, "s-c", "muse:c:ws1-c", "m1", 100)
        self._run("muse:old:ws1-old", "muse", "muse", "ws1-old", "done", now - 3 * 86400 - 500, now - 3 * 86400, session="s-old")
        self._usage(now - 3 * 86400, "s-old", "muse:old:ws1-old", "m1", 700)
        self._run("claude-sub:d:ws2-d", "claude-subagent", "sonnet", "ws2-d", "running", now - 50, None)
        self._run("grok:e:ws2-e", "grok", "grok", "ws2-e", "done", now - 9000, now - 8000, session="s-e")
        self._usage(now - 8100, "s-e", "grok:e:ws2-e", "m1", 200)
        self._run("glm:f:ws3-f", "glm", "glm", "ws3-f", "done", now - 7000, now - 6000, session="s-f")
        self._usage(now - 6100, "s-f", "glm:f:ws3-f", "m1", 300)
        self._run("muse:g:helper", "muse", "muse", "helper", "running", now - 70, None)
        self._run("muse:h:cleanup", "muse", "muse", "cleanup", "done", now - 3000, now - 2000, session="s-h")
        self._usage(now - 2100, "s-h", "muse:h:cleanup", "m1", 400)
        self._run("claude-main:lead:main", "claude-main", "opus", "ws1-lead", "running", now - 900, None)
        self._run("dagster:job:nightly", "dagster", "dagster", "nightly-job", "running", now - 800, None)

    def test_rows_plus_unassigned_equal_all(self):
        self.seed()
        roll = self.store.ws_rollup(self.now, self.since, self.until)
        everything = list(roll["rows"]) + [roll["unassigned"]]
        self.assertEqual({row["ws"] for row in roll["rows"]}, {"WS1", "WS2", "WS3"})
        self.assertEqual(roll["unassigned"]["ws"], "unassigned")
        for metric in ("live_agents", "finished_in_range", "finished_today"):
            self.assertEqual(sum(row[metric] for row in everything), roll["all"]["counts"][metric])
        self.assertEqual(
            sum(row["tokens"] for row in everything),
            sum(roll["all"]["totals"]["tokens"][key] for key in ("input", "output", "cache_read", "cache_write")),
        )
        self.assertAlmostEqual(
            sum(row["cost_usd"] for row in everything), roll["all"]["totals"]["cost_usd"], places=6
        )
        by_ws = {row["ws"]: row for row in everything}
        self.assertEqual(by_ws["WS1"]["live_agents"], 2)
        self.assertEqual(by_ws["WS2"]["live_agents"], 1)
        self.assertEqual(by_ws["WS3"]["live_agents"], 0)
        self.assertEqual(by_ws["unassigned"]["live_agents"], 1)
        self.assertEqual(roll["all"]["counts"]["live_agents"], 4)
        self.assertEqual(by_ws["WS1"]["finished_in_range"], 1)
        self.assertEqual(roll["all"]["counts"]["finished_in_range"], 4)

    def test_each_row_equals_its_tab(self):
        self.seed()
        roll = self.store.ws_rollup(self.now, self.since, self.until)
        for row in list(roll["rows"]) + [roll["unassigned"]]:
            tab = self.store.snapshot(self.now, row["ws"], self.since, self.until)
            for metric in ("live_agents", "finished_in_range", "finished_today"):
                self.assertEqual(row[metric], tab["counts"][metric], f"{row['ws']} {metric}")
            tab_tokens = sum(tab["totals"]["tokens"][key] for key in ("input", "output", "cache_read", "cache_write"))
            self.assertEqual(row["tokens"], tab_tokens, row["ws"])
            self.assertAlmostEqual(row["cost_usd"], tab["totals"]["cost_usd"], places=6, msg=row["ws"])

    def test_window_excludes_old_finished_and_old_tokens(self):
        self.seed()
        wide = self.store.snapshot(self.now, "all", self.since, self.until)
        narrow = self.store.snapshot(self.now, "all", self.now - 3600, self.until)
        self.assertGreater(wide["counts"]["finished_in_range"], narrow["counts"]["finished_in_range"])
        wide_tokens = sum(wide["totals"]["tokens"][k] for k in ("input", "output", "cache_read", "cache_write"))
        narrow_tokens = sum(narrow["totals"]["tokens"][k] for k in ("input", "output", "cache_read", "cache_write"))
        self.assertGreater(wide_tokens, narrow_tokens)
        self.assertEqual(narrow["counts"]["finished_in_range"], 1)

    def test_snapshot_totals_equal_usage_cells(self):
        self.seed()
        snap = self.store.snapshot(self.now, "all", self.since, self.until)
        cells = self.store.query_usage("workstream", "hour", None, self.since, self.until)
        for key in ("input", "output", "cache_read", "cache_write", "reasoning"):
            self.assertEqual(sum(cell[key] for cell in cells), snap["totals"]["tokens"][key])
        priced = [cell["priced_cost_usd"] if cell["priced_cost_usd"] is not None else cell["cost_usd"] for cell in cells]
        priced = [value for value in priced if value is not None]
        self.assertAlmostEqual(sum(priced), snap["totals"]["cost_usd"], places=6)

    def test_reconcile_has_no_mismatches(self):
        self.seed()
        report = self.store.reconcile_numbers(self.now, self.since, self.until)
        self.assertEqual(report["mismatches"], [])
        self.assertEqual(report["all"]["live_agents"], 4)
        self.assertEqual({row["ws"] for row in report["rows"]}, {"WS1", "WS2", "WS3"})

    def test_all_time_covers_tokens_outside_the_window(self):
        self.seed()
        narrow = self.since
        snap = self.store.snapshot(self.now, "all", narrow, self.until)
        windowed = sum(snap["totals"]["tokens"][key] for key in ("input", "output", "cache_read", "cache_write"))
        timeless = sum(snap["totals_all_time"]["tokens"][key] for key in ("input", "output", "cache_read", "cache_write"))
        # The old WS1 run (700 tokens in, half out) sits outside the 12 h window.
        self.assertEqual(windowed, 150 + 300 + 450 + 600)
        self.assertEqual(timeless, windowed + 1050)
        self.assertEqual(
            set(snap["totals_all_time"]["tokens"]),
            {"input", "output", "cache_read", "cache_write", "reasoning"},
        )

    def test_all_time_rows_plus_unassigned_equal_all(self):
        self.seed()
        report = self.store.reconcile_numbers(self.now, self.since, self.until)
        self.assertEqual(report["mismatches"], [])
        everything = list(report["rows"]) + [report["unassigned"]]
        self.assertEqual(
            sum(row["tokens_all_time"] for row in everything),
            report["all_time"]["tokens"],
        )
        self.assertAlmostEqual(
            sum(row["cost_usd_all_time"] for row in everything),
            report["all_time"]["cost_usd"],
            places=6,
        )
        by_ws = {row["ws"]: row for row in everything}
        # Old tokens outside the window still count all time.
        self.assertEqual(by_ws["WS1"]["tokens_all_time"], by_ws["WS1"]["tokens"] + 1050)
        for row in everything:
            tab = self.store.snapshot(self.now, row["ws"], self.since, self.until)
            tab_time = sum(tab["totals_all_time"]["tokens"][key] for key in ("input", "output", "cache_read", "cache_write"))
            self.assertEqual(row["tokens_all_time"], tab_time, row["ws"])
            self.assertAlmostEqual(
                row["cost_usd_all_time"], tab["totals_all_time"]["cost_usd"], places=6, msg=row["ws"]
            )

    def test_zero_window_still_names_last_run_and_all_time(self):
        self.seed()
        snap = self.store.snapshot(self.now, "WS3", self.now - 60, self.now)
        windowed = sum(snap["totals"]["tokens"][key] for key in ("input", "output", "cache_read", "cache_write"))
        self.assertEqual(windowed, 0)
        timeless = sum(snap["totals_all_time"]["tokens"][key] for key in ("input", "output", "cache_read", "cache_write"))
        self.assertEqual(timeless, 450)
        self.assertEqual(snap["last_activity"], self.now - 6000)
        self.assertEqual(snap["by_workstream"][0]["tokens_all_time"], 450)

    def test_no_unassigned_row_when_everything_is_attributed(self):
        self._run("muse:a:ws1-a", "muse", "muse", "ws1-a", "running", self.now - 100, None)
        snap = self.store.snapshot(self.now, "all", self.since, self.until)
        self.assertEqual([row["ws"] for row in snap["by_workstream"]], ["WS1"])
        report = self.store.reconcile_numbers(self.now, self.since, self.until)
        self.assertEqual(report["mismatches"], [])
        self.assertEqual(report["unassigned"]["tokens"], 0)

    def test_board_only_workstream_stays_visible(self):
        self.seed()
        self.store.set_ws_counts([{"ws": "WS9", "name": "Quiet", "backlog": 1, "todo": 0, "doing": 0, "review": 0, "done": 0, "cancelled": 0}])
        boards = self.store.query_workstreams({"WS9"}, self.since, self.until, now=self.now)
        quiet = next(row for row in boards if row["ws"] == "WS9")
        self.assertEqual(quiet["live_agents"], 0)
        self.assertEqual(quiet["tokens"], 0)
        self.assertTrue(quiet["plan"])
        mine = {row["ws"]: row for row in boards if row["ws"] in ("WS1", "unassigned")}
        self.assertEqual(mine["WS1"]["live_agents"], 2)
        self.assertEqual(mine["unassigned"]["live_agents"], 1)


class Prev24hTest(unittest.TestCase):
    """Snapshot prev_24h: the 24 h before the window, like with like.

    Tokens and cost come from the same hourly bucket cells over the
    earlier window (same priced-cost fallback, so the same unpriced
    rule); finished is the same finished-in-range count for that
    window, agent runs only; live_agents is the point-in-time overlap
    at the window start, not a peak.
    """

    MODEL = "claude-sonnet-5-5"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))
        self.now = 1_761_999_960.0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _run(self, run_id, source, tier, label, status, started, ended, session=None):
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": source,
                "tier": tier,
                "model": self.MODEL,
                "label": label,
                "session_id": session or run_id,
                "status": status,
                "started_at": started,
                "ended_at": ended,
            }
        )

    def _usage(self, ts, session, run_id, message, tokens, model=None):
        self.store.add_usage(
            {
                "ts": ts,
                "session_id": session,
                "run_id": run_id,
                "model": model or self.MODEL,
                "tool": "muse",
                "message_id": message,
                "input": tokens,
                "output": tokens // 2,
                "cache_read": 0,
                "cache_write": 0,
            }
        )

    def seed(self):
        now = self.now
        hour = 3600
        # Windowed WS7 run: 1500 billed tokens in the window.
        self._run("muse:win:ws7-win", "muse", "muse", "ws7-win", "done",
                  now - 2 * hour, now - hour, session="s-win")
        self._usage(now - hour - 100, "s-win", "muse:win:ws7-win", "m-win", 1000)
        # Previous-window WS7 run: 3000 billed tokens before the window.
        self._run("muse:prev:ws7-prev", "muse", "muse", "ws7-prev", "done",
                  now - 32 * hour, now - 30 * hour, session="s-prev")
        self._usage(now - 31 * hour, "s-prev", "muse:prev:ws7-prev", "m-prev", 2000)
        # Unpriced model in the previous window: tokens count, cost does not.
        self._run("muse:prevu:ws7-unpriced", "muse", "muse", "ws7-unpriced", "done",
                  now - 33 * hour, now - 31 * hour, session="s-prevu")
        self._usage(now - 32 * hour, "s-prevu", "muse:prevu:ws7-unpriced",
                    "m-prevu", 100, model="no-such-model-xyz")
        # Previous-window WS1 run: 600 billed tokens, ended before the start.
        self._run("muse:old:ws1-old", "muse", "muse", "ws1-old", "done",
                  now - 42 * hour, now - 40 * hour, session="s-old")
        self._usage(now - 41 * hour, "s-old", "muse:old:ws1-old", "m-old", 400)
        # Unassigned previous-window run: 90 billed tokens.
        self._run("muse:u:helper", "muse", "muse", "helper", "done",
                  now - 34 * hour, now - 33 * hour, session="s-u")
        self._usage(now - 33 * hour - 100, "s-u", "muse:u:helper", "m-u", 60)
        # WS1 runner live across the window start, kept fresh by a sample.
        self._run("muse:run:ws1-live", "muse", "muse", "ws1-live", "running",
                  now - 50 * hour, None, session="s-run")
        self.store.add_process_sample(
            {"ts": now - 60, "pid": 4242, "run_id": "muse:run:ws1-live", "label": "ws1-live"}
        )
        # WS7 run spanning the window start: live then, finished in the window.
        self._run("muse:long:ws7-long", "muse", "muse", "ws7-long", "done",
                  now - 50 * hour, now - hour, session="s-long")
        # A lead session, a shell row, and a Dagster job overlapping the
        # previous window are never agents: out of finished and live counts.
        self._run("claude-main:lead:ws7-lead", "claude-main", "opus", "ws7-lead", "running",
                  now - 50 * hour, None, session="lead")
        self.store.add_process_sample(
            {"ts": now - 60, "pid": 4243, "run_id": "claude-main:lead:ws7-lead", "label": "ws7-lead"}
        )
        self._run("shell:exec:ws1-sh", "shell", "generic", "ws1-sh", "done",
                  now - 33 * hour, now - 32 * hour, session="s-sh")
        self.store.upsert_run(
            {
                "run_id": "dagster:job:prev",
                "source": "dagster",
                "model": "nightly",
                "label": "ws1-backfill",
                "session_id": "dagster:job:prev",
                "status": "finished",
                "started_at": now - 33 * hour,
                "ended_at": now - 32 * hour,
                "clock_source": "dagster",
            }
        )

    def test_exact_values(self):
        self.seed()
        snap = self.store.snapshot(self.now, "all")
        prev = snap["prev_24h"]
        self.assertEqual(set(prev), {"tokens", "cost_usd", "finished", "live_agents"})
        self.assertEqual(prev["tokens"], 3000 + 150 + 600 + 90)
        self.assertAlmostEqual(
            prev["cost_usd"],
            cost(self.MODEL, 2000, 1000, 0, 0)
            + cost(self.MODEL, 400, 200, 0, 0)
            + cost(self.MODEL, 60, 30, 0, 0),
            places=6,
        )
        # Four finished agent runs; the shell row and the Dagster job do not count.
        self.assertEqual(prev["finished"], 4)
        # The WS1 runner and the spanning WS7 run; the lead session does not count.
        self.assertEqual(prev["live_agents"], 2)
        # The window itself is untouched by the earlier data.
        self.assertEqual(snap["counts"]["finished_in_range"], 2)
        self.assertEqual(snap["counts"]["live_agents"], 1)

    def test_scope_filter(self):
        self.seed()
        ws7 = self.store.snapshot(self.now, "WS7")["prev_24h"]
        self.assertEqual(ws7["tokens"], 3000 + 150)
        self.assertEqual(ws7["finished"], 2)
        self.assertEqual(ws7["live_agents"], 1)
        ws1 = self.store.snapshot(self.now, "WS1")["prev_24h"]
        self.assertEqual(ws1["tokens"], 600)
        self.assertEqual(ws1["finished"], 1)
        self.assertEqual(ws1["live_agents"], 1)
        unassigned = self.store.snapshot(self.now, "unassigned")["prev_24h"]
        self.assertEqual(unassigned["tokens"], 90)
        self.assertEqual(unassigned["finished"], 1)
        self.assertEqual(unassigned["live_agents"], 0)

    def test_rows_plus_unassigned_equal_all(self):
        self.seed()
        roll = self.store.ws_rollup(self.now)
        everything = list(roll["rows"]) + [roll["unassigned"]]
        self.assertEqual(
            sum(row["prev_24h"]["tokens"] for row in everything),
            roll["all"]["prev_24h"]["tokens"],
        )
        self.assertAlmostEqual(
            sum(row["prev_24h"]["cost_usd"] for row in everything),
            roll["all"]["prev_24h"]["cost_usd"],
            places=6,
        )
        for row in everything:
            tab = self.store.snapshot(self.now, row["ws"])
            self.assertEqual(row["prev_24h"], tab["prev_24h"], row["ws"])
        report = self.store.reconcile_numbers(self.now, processes=[])
        self.assertEqual(report["mismatches"], [])
        self.assertEqual(report["all"]["prev_24h"], self.store.snapshot(self.now, "all")["prev_24h"])

    def test_prev_mismatch_reported_like_the_others(self):
        import copy

        from dash.store import _scope_tab

        self.seed()
        now = self.now
        with self.store._lock:
            shown_all, parents = self.store._shown_locked(now)
            roll = self.store._summarize_locked(shown_all, parents, now, None, None, None)
            doctored = copy.deepcopy(roll)
            doctored["rows"][0]["prev_24h"]["tokens"] += 1
            mismatches = self.store._mismatches_locked(
                doctored, shown_all, now, lambda ws: _scope_tab(doctored, ws), []
            )
        self.assertTrue(
            any(item.startswith("prev_24h_tokens: workstream rows sum to") for item in mismatches),
            mismatches,
        )
        with self.store._lock:
            shown_all, parents = self.store._shown_locked(now)
            roll = self.store._summarize_locked(shown_all, parents, now, None, None, None)
            doctored = copy.deepcopy(roll)
            doctored["rows"][0]["prev_24h"]["cost_usd"] += 1.0
            mismatches = self.store._mismatches_locked(
                doctored, shown_all, now, lambda ws: _scope_tab(doctored, ws), []
            )
        self.assertTrue(
            any(item.startswith("prev_24h_cost_usd: workstream rows sum to") for item in mismatches),
            mismatches,
        )


class ProcessParseTest(unittest.TestCase):
    def test_etime_and_label_and_ps_line(self):
        self.assertEqual(parse_etime("00:05"), 5)
        self.assertEqual(parse_etime("01:02:03"), 3723)
        self.assertEqual(parse_etime("1-02:03:04"), 93784)
        self.assertEqual(label_from_args("cursor-agent -p --model grok [label: dash-preview] rest"), "dash-preview")
        self.assertIsNone(label_from_args("claude"))
        self.assertIsNone(label_from_args("[label: " + ("x" * 90) + "]"))
        rows = parse_ps("  10  1  10  200 00:10 claude\n  11  10  10  50 00:02 node\n")
        self.assertEqual(rows[0]["comm"], "claude")
        self.assertEqual(rows[0]["ppid"], 1)
        self.assertEqual(rows[1]["comm"], "node")

    def test_reconcile_ends_ghosts_and_links_shadow(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                store.upsert_run(
                    {
                        "run_id": "grok:chat:chat",
                        "source": "grok",
                        "session_id": "chat",
                        "status": "running",
                        "started_at": 100,
                    }
                )
                store.upsert_run(
                    {
                        "run_id": "grok:chat:dash-a",
                        "source": "grok",
                        "label": "dash-a",
                        "session_id": "chat",
                        "status": "running",
                        "pid": 9,
                        "started_at": 100,
                    }
                )
                store.upsert_run(
                    {
                        "run_id": "muse:s:ws7-plan",
                        "source": "muse",
                        "label": "ws7-plan",
                        "status": "running",
                        "pid": 3,
                        "started_at": 50,
                    }
                )
                processes = [{"pid": 9, "comm": "cursor-agent", "started_at": 90, "label": "dash-a"}]
                ended = reconcile_running(store, 200, processes)
                shadow = store.get_run("grok:chat:chat")
                labeled = store.get_run("grok:chat:dash-a")
                muse = store.get_run("muse:s:ws7-plan")
            finally:
                store.close()
        self.assertEqual(ended, 2)
        self.assertEqual(shadow["status"], "unknown")
        self.assertEqual(shadow["parent_session_id"], "grok:chat:dash-a")
        self.assertEqual(shadow["ended_at"], 100)
        self.assertEqual(labeled["status"], "running")
        self.assertEqual(muse["status"], "died_early")
        self.assertEqual(muse["ended_at"], 50)

    def test_reconcile_moves_orphan_usage_and_deletes_the_row(self):
        sid = "ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF"
        lower = sid.lower()
        labeled = "muse:%s:ws2-notes" % sid
        orphan = "muse:%s:%s" % (lower, lower)
        now = 1_700_000_100.0
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                store.upsert_run(
                    {
                        "run_id": labeled,
                        "source": "muse",
                        "tier": "muse",
                        "label": "ws2-notes",
                        "session_id": sid,
                        "status": "unknown",
                        "started_at": now - 50,
                    }
                )
                store.upsert_run(
                    {
                        "run_id": orphan,
                        "source": "muse",
                        "tier": "muse",
                        "session_id": lower,
                        "model": "muse-spark-1.3",
                        "status": "unknown",
                        "started_at": now - 80,
                        "clock_source": "muse_log",
                    }
                )
                store.add_usage(
                    {
                        "ts": now - 40,
                        "session_id": lower,
                        "run_id": orphan,
                        "model": "muse-spark-1.3",
                        "tool": "muse",
                        "message_id": "early",
                        "input": 50,
                        "output": 4,
                        "cache_read": 1,
                        "cache_write": 0,
                        "reasoning": 2,
                    }
                )
                ended = reconcile_running(store, now, [])
                usage = store._conn.execute(
                    "SELECT run_id, input_tokens FROM usage_events"
                ).fetchone()
                buckets = store._conn.execute(
                    "SELECT workstream, input_tokens FROM usage_buckets"
                ).fetchall()
                kept = store.get_run(labeled)
                gone = store.get_run(orphan)
            finally:
                store.close()
        self.assertEqual(ended, 1)
        self.assertIsNone(gone)
        self.assertEqual(usage["run_id"], labeled)
        self.assertEqual(usage["input_tokens"], 50)
        self.assertEqual(kept["model"], "muse-spark-1.3")
        self.assertEqual(kept["started_at"], now - 50)
        self.assertEqual(kept["status"], "done")
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0]["workstream"], "WS2")
        self.assertEqual(buckets[0]["input_tokens"], 50)

    def test_muse_shadow_tokens_roll_onto_labeled_run(self):
        now = time.time()
        sid = "11111111-1111-4111-8111-111111111111"
        labeled = "muse:%s:ws2-WS2.14-cards-list" % sid
        shadow = "muse:%s:%s" % (sid, sid)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                store.upsert_run(
                    {
                        "run_id": labeled,
                        "source": "muse",
                        "tier": "muse",
                        "label": "ws2-WS2.14-cards-list",
                        "session_id": sid,
                        "parent_session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "status": "running",
                        "pid": os.getpid(),
                        "started_at": now - 100,
                    }
                )
                store.upsert_run(
                    {
                        "run_id": shadow,
                        "source": "muse",
                        "tier": "muse",
                        "session_id": sid,
                        "status": "unknown",
                        "parent_session_id": labeled,
                        "started_at": now - 100,
                        "ended_at": now - 90,
                    }
                )
                store.add_usage(
                    {
                        "ts": now - 80,
                        "session_id": sid,
                        "run_id": shadow,
                        "model": "muse-spark-1.3",
                        "tool": "muse",
                        "message_id": "shadow",
                        "input": 50,
                        "output": 4,
                        "cache_read": 7,
                        "cache_write": 0,
                    }
                )
                store.add_usage(
                    {
                        "ts": now - 70,
                        "session_id": sid,
                        "run_id": labeled,
                        "model": "muse-spark-1.3",
                        "tool": "muse",
                        "message_id": "labeled",
                        "input": 5,
                        "output": 1,
                        "cache_read": 0,
                        "cache_write": 0,
                    }
                )
                snap = store.snapshot(now, "WS2")
            finally:
                store.close()
        running = snap["running"]
        self.assertEqual([run["run_id"] for run in running], [labeled])
        self.assertEqual(running[0]["workstream"], "WS2")
        self.assertEqual(running[0]["tokens"]["input"], 55)
        self.assertEqual(running[0]["tokens"]["output"], 5)
        self.assertEqual(running[0]["tokens"]["cache_read"], 7)
        self.assertEqual(snap["counts"]["live_by_tool"]["muse"], 1)

    def test_live_poll_stores_no_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                process_poll(store, {}, 1_700_000_000.0)
                rows = store._conn.execute("SELECT * FROM process_samples").fetchall()
                for row in rows:
                    self.assertIn(row["comm"], {"cursor-agent", "claude", "grok", "muse", "muse-bin"})
                    for value in row:
                        if isinstance(value, str):
                            if len(value) > 80 or "\n" in value or "[label:" in value:
                                raise AssertionError("process sample stored argv or a prompt")
            finally:
                store.close()

    def test_reconcile_raises_a_premature_end_to_last_usage(self):
        sid = "d9000a2a-f4c1-45bd-ac06-b65dd4e8d579"
        label = "ws2-WS2.14-prod-columns"
        run_id = "muse:%s:%s" % (sid, label)
        start = 1792167605.0
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                store.upsert_run(
                    {
                        "run_id": run_id,
                        "source": "muse",
                        "tier": "muse",
                        "label": label,
                        "session_id": sid,
                        "status": "done",
                        "started_at": start,
                        "ended_at": start,
                        "clock_source": "manifest",
                    }
                )
                store.add_usage(
                    {
                        "ts": 1792167848.6732,
                        "session_id": sid,
                        "run_id": run_id,
                        "tool": "muse",
                        "message_id": "last",
                        "input": 10,
                        "output": 1,
                    }
                )
                reconcile_running(store, 1792168000.0, [])
                row = store.get_run(run_id)
                view = store.query_runs(1792169000.0, "WS2", None, None, 10)
            finally:
                store.close()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["started_at"], start)
        self.assertEqual(row["ended_at"], 1792167848.6732)
        self.assertAlmostEqual(view[0]["elapsed_s"], 1792167848.6732 - start)

    def test_reconcile_closes_a_dead_muse_and_fills_the_start(self):
        sid = "971a8090-feb6-46fc-80f4-f458a744b07a"
        label = "ws2-WS2.17-drop-gate-rev1"
        run_id = "muse:%s:%s" % (sid, label)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                store.upsert_run(
                    {
                        "run_id": run_id,
                        "source": "muse",
                        "tier": "muse",
                        "label": label,
                        "session_id": sid,
                        "status": "running",
                        "pid": 999999,
                    }
                )
                store.add_usage(
                    {
                        "ts": 1792167818.0,
                        "session_id": sid,
                        "run_id": run_id,
                        "tool": "muse",
                        "message_id": "first",
                        "input": 4,
                        "output": 1,
                    }
                )
                store.add_usage(
                    {
                        "ts": 1792167946.0,
                        "session_id": sid,
                        "run_id": run_id,
                        "tool": "muse",
                        "message_id": "last",
                        "input": 4,
                        "output": 1,
                    }
                )
                ended = reconcile_running(store, 1792168000.0, [])
                row = store.get_run(run_id)
            finally:
                store.close()
        self.assertEqual(ended, 1)
        self.assertEqual(row["status"], "died_early")
        self.assertEqual(row["started_at"], 1792167818.0)
        self.assertEqual(row["ended_at"], 1792167946.0)
        self.assertIsNone(row["pid"])


class HubTest(unittest.TestCase):
    def test_fixture_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            issues = root / "issues"
            issues.mkdir()
            (issues / "a.md").write_text("---\nstatus: backlog\nworkstream: WS1\n---\n", encoding="utf-8")
            (issues / "b.md").write_text("---\nstatus: done\nworkstream: WS1\n---\n", encoding="utf-8")
            (issues / "c.md").write_text("no frontmatter\n", encoding="utf-8")
            (root / "workstreams.tsv").write_text("code\tname\tgoal\texit\nWS1\tIdentity\tg\te\n", encoding="utf-8")
            store = Store(root / "dash.sqlite")
            try:
                hub_poll(
                    store,
                    {"issues_dir": str(issues), "workstreams_tsv": str(root / "workstreams.tsv")},
                    1,
                )
                rows = store.query_workstreams(set())
                self.assertEqual(rows[0]["name"], "Identity")
                self.assertEqual(rows[0]["tk"]["backlog"], 1)
                self.assertEqual(rows[0]["tk"]["done"], 1)
            finally:
                store.close()

    @unittest.skipUnless(os.environ.get("AGENT_DASH_SMOKE") == "1", "set AGENT_DASH_SMOKE=1 to read a local hub tracker")
    def test_live_board_smoke(self):
        hub = os.environ.get("AGENT_DASH_HUB_DIR")
        if not hub:
            self.skipTest("AGENT_DASH_HUB_DIR is not set")
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                hub_poll(store, {"hub_dir": hub}, 1)
                rows = store.query_workstreams(set())
            finally:
                store.close()
        self.assertTrue(rows)


class SnapshotUsageTest(unittest.TestCase):
    """snapshot(usage_group=...) carries the chart buckets in the same
    response, read under the same lock pass as the totals, so the bucket
    token sum equals the totals by construction."""

    TOKEN_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))
        self.now = 1_761_999_960.0
        self.since = self.now - 12 * 3600
        self.until = self.now

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def seed(self):
        now = self.now
        self.store.upsert_run(
            {
                "run_id": "muse:a:ws1-a",
                "source": "muse",
                "tier": "muse",
                "model": "claude-sonnet-4-5",
                "label": "ws1-a",
                "session_id": "sess-a",
                "status": "done",
                "started_at": now - 5000,
                "ended_at": now - 4000,
            }
        )
        self.store.add_usage(
            {
                "ts": now - 4100,
                "session_id": "sess-a",
                "run_id": "muse:a:ws1-a",
                "model": "claude-sonnet-4-5",
                "tool": "muse",
                "message_id": "m1",
                "input": 100,
                "output": 50,
                "cache_read": 10,
                "cache_write": 5,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "grok:b:ws2-b",
                "source": "grok",
                "tier": "grok",
                "model": "grok-4-7",
                "label": "ws2-b",
                "session_id": "sess-b",
                "status": "done",
                "started_at": now - 9000,
                "ended_at": now - 8000,
            }
        )
        self.store.add_usage(
            {
                "ts": now - 8100,
                "session_id": "sess-b",
                "run_id": "grok:b:ws2-b",
                "model": "grok-4-7",
                "tool": "grok",
                "message_id": "m1",
                "input": 200,
                "output": 100,
                "cache_read": 0,
                "cache_write": 0,
            }
        )

    def assert_buckets_match_totals(self, snap, scope):
        self.assertIn("usage", snap)
        self.assertTrue(snap["usage"], scope)
        for key in self.TOKEN_KEYS:
            self.assertEqual(
                sum(cell[key] for cell in snap["usage"]),
                snap["totals"]["tokens"][key],
                f"{scope} {key}",
            )
        priced = [
            cell["priced_cost_usd"] if cell["priced_cost_usd"] is not None else cell["cost_usd"]
            for cell in snap["usage"]
        ]
        priced = [value for value in priced if value is not None]
        self.assertAlmostEqual(sum(priced), snap["totals"]["cost_usd"], places=6, msg=scope)

    def test_model_buckets_sum_to_totals(self):
        self.seed()
        for ws in ("all", "WS1"):
            snap = self.store.snapshot(self.now, ws, self.since, self.until, usage_group="model")
            self.assert_buckets_match_totals(snap, f"model/{ws}")
        scoped = self.store.snapshot(self.now, "WS1", self.since, self.until, usage_group="model")
        self.assertEqual({cell["key"] for cell in scoped["usage"]}, {"sonnet"})

    def test_workstream_buckets_sum_to_totals(self):
        self.seed()
        snap = self.store.snapshot(self.now, "all", self.since, self.until, usage_group="workstream")
        self.assert_buckets_match_totals(snap, "workstream/all")
        self.assertEqual({cell["key"] for cell in snap["usage"]}, {"WS1", "WS2"})

    def test_tool_buckets_sum_to_totals(self):
        self.seed()
        snap = self.store.snapshot(self.now, "all", self.since, self.until, usage_group="tool")
        self.assert_buckets_match_totals(snap, "tool/all")
        self.assertEqual({cell["key"] for cell in snap["usage"]}, {"muse", "grok"})

    def test_no_group_means_no_usage_key(self):
        self.seed()
        snap = self.store.snapshot(self.now, "all", self.since, self.until)
        self.assertNotIn("usage", snap)

    def test_bad_group_raises(self):
        self.seed()
        with self.assertRaises(ValueError):
            self.store.snapshot(self.now, "all", self.since, self.until, usage_group="day")


class StaleRunningTest(unittest.TestCase):
    """One stale row is excluded from every view at once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))
        self.now = time.time()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_stale_dead_pid_excluded_everywhere(self):
        now = self.now
        live_id = "muse:live:ws1-live"
        stale_id = "muse:stale:ws1-stale"
        self.store.upsert_run(
            {
                "run_id": live_id,
                "source": "muse",
                "tier": "muse",
                "model": "muse-spark-1.3-contributor",
                "label": "ws1-live",
                "session_id": "live-session",
                "status": "running",
                "pid": os.getpid(),
                "started_at": now - 60,
            }
        )
        self.store.upsert_run(
            {
                "run_id": stale_id,
                "source": "muse",
                "tier": "muse",
                "model": "muse-spark-1.3-contributor",
                "label": "ws1-stale",
                "session_id": "stale-session",
                "status": "running",
                "pid": 999999,
                "started_at": now - 60,
            }
        )
        stale = self.store.stale_running(now)
        self.assertEqual([run["run_id"] for run, _reason in stale], [stale_id])
        self.assertIn("dead pid", stale[0][1])
        snap = self.store.snapshot(now, "all")
        self.assertEqual([run["run_id"] for run in snap["running"]], [live_id])
        self.assertEqual(snap["counts"]["live_agents"], 1)
        boards = self.store.query_workstreams(set(), now - 3600, now, now=now)
        ws1 = next(row for row in boards if row["ws"] == "WS1")
        self.assertEqual(ws1["live_agents"], 1)
        runs = self.store.query_runs(now, None, None, None, 100)
        self.assertEqual([run["run_id"] for run in runs], [live_id])
        points = self.store.query_concurrency(now, None, now - 60, 60)
        self.assertEqual(points[-1]["muse"], 1)
        report = self.store.reconcile_numbers(now, now - 3600, now)
        self.assertTrue(
            any(stale_id in item for item in report["mismatches"]),
            report["mismatches"],
        )

    def test_stale_dagster_absent_excluded(self):
        now = self.now
        live_short = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        stale_short = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        self.store.upsert_run(
            {
                "run_id": "dagster:" + live_short,
                "source": "dagster",
                "model": "test_job_alpha",
                "label": "test_job_alpha",
                "status": "running",
                "started_at": now - 100,
                "clock_source": "dagster",
            }
        )
        self.store.upsert_run(
            {
                "run_id": "dagster:" + stale_short,
                "source": "dagster",
                "model": "test_job_beta",
                "label": "test_job_beta",
                "status": "running",
                "started_at": now - 100,
                "clock_source": "dagster",
            }
        )
        self.store.set_dagster_active([live_short], now, False)
        stale = self.store.stale_running(now)
        self.assertEqual(
            [run["run_id"] for run, _reason in stale], ["dagster:" + stale_short]
        )
        snap = self.store.snapshot(now, "all")
        self.assertEqual([run["run_id"] for run in snap["running"]], [])
        self.assertEqual(snap["counts"]["prod_jobs"], 1)
        report = self.store.reconcile_numbers(now, now - 3600, now)
        self.assertTrue(
            any("dagster:" + stale_short in item for item in report["mismatches"])
        )

    def test_finished_prod_jobs_are_a_count_not_a_list(self):
        now = self.now
        self.store.upsert_run(
            {
                "run_id": "dagster:job:done",
                "source": "dagster",
                "model": "nightly",
                "label": "ws7-backfill",
                "status": "finished",
                "started_at": now - 200,
                "ended_at": now - 100,
                "clock_source": "dagster",
            }
        )
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws7-x",
                "source": "muse",
                "tier": "muse",
                "model": "muse-spark-1.3-contributor",
                "label": "ws7-x",
                "status": "done",
                "started_at": now - 200,
                "ended_at": now - 100,
            }
        )
        snap = self.store.snapshot(now, "WS7")
        self.assertEqual([run["run_id"] for run in snap["recent"]], ["muse:s:ws7-x"])
        self.assertEqual(snap["recent_total"], 1)
        self.assertEqual(snap["counts"]["finished_in_range"], 1)
        self.assertEqual(snap["counts"]["prod_jobs_finished"], 1)
        runs = self.store.query_runs(now, "WS7", None, None, 50)
        self.assertEqual([run["run_id"] for run in runs], ["muse:s:ws7-x"])
        with_jobs = self.store.query_runs(now, "WS7", None, None, 50, include_jobs=True)
        self.assertEqual(
            {run["run_id"] for run in with_jobs}, {"muse:s:ws7-x", "dagster:job:done"}
        )

    def test_stale_idle_claude_main_excluded(self):
        now = self.now
        run_id = "claude-main:sess:main"
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": "claude-main",
                "tier": "opus",
                "label": "ws1-idle",
                "session_id": "sess",
                "status": "running",
                "started_at": now - 7 * 3600,
            }
        )
        stale = self.store.stale_running(now)
        self.assertEqual([run["run_id"] for run, _reason in stale], [run_id])
        snap = self.store.snapshot(now, "all")
        self.assertEqual(snap["running"], [])
        report = self.store.reconcile_numbers(now, now - 3600, now)
        self.assertTrue(any(run_id in item for item in report["mismatches"]))

    def test_idle_lead_alive_pid_is_not_a_mismatch(self):
        """An alive-but-idle lead is intended state: hidden, not a mismatch."""
        now = self.now
        run_id = "claude-main:lead:main"
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": "claude-main",
                "tier": "opus",
                "label": "ws1-lead",
                "session_id": "lead",
                "status": "running",
                "pid": os.getpid(),
                "started_at": now - 7 * 3600,
            }
        )
        alive = lambda pid: True  # noqa: E731
        self.assertEqual(self.store.stale_running(now, pid_alive_fn=alive), [])
        snap = self.store.snapshot(now, "all")
        self.assertEqual(snap["running"], [])
        self.assertEqual(snap["counts"]["live_agents"], 0)
        report = self.store.reconcile_numbers(now, now - 3600, now)
        self.assertFalse(any(run_id in item for item in report["mismatches"]))
        dead = self.store.stale_running(now, pid_alive_fn=lambda pid: False)
        self.assertEqual([run["run_id"] for run, _reason in dead], [run_id])
        self.assertIn("dead pid", dead[0][1])
        reconcile_running(self.store, now, processes=[])
        self.assertEqual(self.store.get_run(run_id)["status"], "running")
        self.store.add_usage(
            {
                "ts": now,
                "session_id": "lead",
                "run_id": run_id,
                "model": "claude-opus-5-5",
                "tool": "claude",
                "message_id": "m-again",
                "input": 10,
                "output": 5,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 0,
            }
        )
        self.assertEqual(self.store.stale_running(now, pid_alive_fn=alive), [])
        snap = self.store.snapshot(now, "all")
        self.assertEqual([run["run_id"] for run in snap["running"]], [run_id])
        self.assertEqual(snap["counts"]["live_agents"], 0)
        report = self.store.reconcile_numbers(now, now - 3600, now)
        self.assertFalse(any(run_id in item for item in report["mismatches"]))


class StalledTest(unittest.TestCase):
    """A live process with no output is stuck, not live."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))
        self.now = time.time()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _running(self, run_id, source="muse", pid=True, started_ago=7200, **over):
        row = {
            "run_id": run_id,
            "source": source,
            "tier": source,
            "label": run_id.split(":")[-1],
            "status": "running",
            "pid": os.getpid() if pid else None,
            "started_at": self.now - started_ago,
        }
        row.update(over)
        self.store.upsert_run(row)
        return row

    def _heartbeat(self, run_id, ts=None):
        self.store.add_usage(
            {
                "ts": self.now if ts is None else ts,
                "session_id": "sess",
                "run_id": run_id,
                "model": "muse-spark-1.3",
                "tool": "muse",
                "message_id": "m-%s" % run_id,
                "input": 1,
                "output": 1,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 0,
            }
        )

    def test_quiet_alive_worker_reads_stalled(self):
        now = self.now
        run_id = "muse:s:ws1-stuck"
        self._running(run_id)
        stalled = self.store._stalled_locked(now, [dict(self.store.get_run(run_id))])
        self.assertIn(run_id, stalled)
        self.assertGreater(stalled[run_id], 1800)
        snap = self.store.snapshot(now, "all")
        row = next(run for run in snap["running"] if run["run_id"] == run_id)
        self.assertTrue(row["stalled"])
        self.assertEqual(row["outcome_class"], "stalled")
        self.assertIn("no output", row["outcome_reason"])
        self.assertEqual(snap["counts"]["live_agents"], 0)
        self.assertEqual(snap["counts"]["stalled"], 1)

    def test_fresh_output_reads_live(self):
        now = self.now
        run_id = "muse:s:ws1-live"
        self._running(run_id)
        self._heartbeat(run_id)
        stalled = self.store._stalled_locked(now, [dict(self.store.get_run(run_id))])
        self.assertEqual(stalled, {})
        snap = self.store.snapshot(now, "all")
        self.assertEqual(snap["counts"]["live_agents"], 1)
        self.assertEqual(snap["counts"]["stalled"], 0)

    def test_fresh_out_file_reads_live(self):
        now = self.now
        out = os.path.join(self.tmp.name, "out-ws1-fresh.md")
        with open(out, "w") as fh:
            fh.write("working\n")
        run_id = "muse:s:ws1-fresh"
        self._running(run_id, out_path=out)
        stalled = self.store._stalled_locked(now, [dict(self.store.get_run(run_id))])
        self.assertEqual(stalled, {})

    def test_dead_pid_is_never_stalled(self):
        now = self.now
        run_id = "muse:s:ws1-dead"
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": "muse",
                "tier": "muse",
                "label": "ws1-dead",
                "status": "running",
                "pid": 99999999,
                "started_at": now - 7200,
            }
        )
        stalled = self.store._stalled_locked(now, [dict(self.store.get_run(run_id))])
        self.assertEqual(stalled, {})

    def test_leads_and_jobs_are_exempt(self):
        now = self.now
        lead = "claude-main:sess:main"
        self._running(lead, source="claude-main")
        job = "dagster:job:stuck"
        self.store.upsert_run(
            {
                "run_id": job,
                "source": "dagster",
                "label": "stuck",
                "status": "running",
                "pid": os.getpid(),
                "started_at": now - 7200,
            }
        )
        rows = [dict(self.store.get_run(lead)), dict(self.store.get_run(job))]
        self.assertEqual(self.store._stalled_locked(now, rows), {})

    def test_reused_pid_reads_stale_not_idle(self):
        now = self.now
        run_id = "claude-main:old:main"
        self._running(run_id, source="claude-main", started_ago=45 * 3600)
        self.store.add_process_sample(
            {
                "ts": now,
                "pid": os.getpid(),
                "comm": "claude",
                "rss_kb": 10,
                "elapsed_s": 5.0,
                "label": "main",
                "run_id": "other:new:row",
                "model": None,
            }
        )
        stale = self.store.stale_running(now, pid_alive_fn=lambda pid: True)
        self.assertEqual([run["run_id"] for run, _reason in stale], [run_id])
        self.assertIn("reused", stale[0][1])

    def test_idle_shell_close_reads_died(self):
        from dash.outcomes import outcome_class

        now = self.now
        run_id = "shell:s:ws1-quiet"
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": "shell",
                "label": "ws1-quiet",
                "status": "running",
                "started_at": now - 7 * 3600,
            }
        )
        ended = reconcile_running(self.store, now, processes=[])
        self.assertGreaterEqual(ended, 1)
        row = self.store.get_run(run_id)
        self.assertEqual(row["status"], "died_early")
        self.assertEqual(row["verdict"], "IDLE")
        self.assertEqual(outcome_class(row), "died")

    def test_views_carry_outcome_class_and_reason(self):
        from dash.outcomes import outcome_class as cls

        now = self.now
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws1-doomed",
                "source": "muse",
                "tier": "muse",
                "label": "ws1-doomed",
                "status": "killed",
                "started_at": now - 200,
                "ended_at": now - 30,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "dagster:job:cancelled",
                "source": "dagster",
                "label": "cancelled",
                "status": "finished",
                "verdict": "CANCELED",
                "started_at": now - 200,
                "ended_at": now - 30,
            }
        )
        rows = self.store.query_runs(now, "all", None, None, 50, include_jobs=True)
        by_id = {run["run_id"]: run for run in rows}
        doomed = by_id["muse:s:ws1-doomed"]
        self.assertEqual(doomed["outcome_class"], "died")
        self.assertIn("SIGTERM", doomed["outcome_reason"])
        cancelled = by_id["dagster:job:cancelled"]
        self.assertEqual(cancelled["outcome_class"], "cancelled")
        snap = self.store.snapshot(now, "all")
        recent = {run["run_id"]: run for run in snap["recent"]}
        self.assertEqual(recent["muse:s:ws1-doomed"]["outcome_class"], "died")
        for run in list(by_id.values()) + list(recent.values()):
            self.assertEqual(run["outcome_class"], cls(run))


class LaneAndUnseenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _lane(self, now, worker=True):
        self.store.upsert_run(
            {
                "run_id": "claude-workflow:lead:wf_1/build",
                "source": "claude-workflow",
                "tier": "haiku",
                "label": "ws9-WS9.1-build",
                "session_id": "lead",
                "status": "running",
                "started_at": now - 100,
            }
        )
        if worker:
            self.store.upsert_run(
                {
                    "run_id": "glm:sid1:ws9-WS9.1-build",
                    "source": "glm",
                    "tier": "glm",
                    "model": "glm-5.3",
                    "label": "ws9-WS9.1-build",
                    "session_id": "sid1",
                    "status": "running",
                    "started_at": now - 90,
                }
            )

    def test_wrapper_and_live_worker_count_once(self):
        now = time.time()
        self._lane(now)
        snap = self.store.snapshot(now, "all")
        self.assertEqual(snap["counts"]["live_agents"], 1)
        self.assertEqual(snap["counts"]["live_by_tool"]["grok"], 1)
        self.assertEqual(snap["counts"]["live_by_tool"]["glm"], 0)
        self.assertEqual(snap["counts"]["live_by_tool"]["claude"], 0)
        running = snap["running"]
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0]["run_id"], "glm:sid1:ws9-WS9.1-build")
        # The worker names its wrapper as its parent: via the workflow step.
        self.assertEqual(
            running[0]["parent_session_id"], "claude-workflow:lead:wf_1/build"
        )
        rows = self.store.query_workstreams({"WS1", "WS9"}, None, None)
        ws9 = [row for row in rows if row["ws"] == "WS9"]
        self.assertEqual(len(ws9), 1)
        self.assertEqual(ws9[0]["live_agents"], 1)
        report = self.store.reconcile_numbers(now, None, None, processes=[])
        self.assertEqual(report["all"]["live_agents"], 1)
        self.assertEqual(report["mismatches"], [])

    def test_wrapper_without_worker_counts_itself(self):
        now = time.time()
        self._lane(now, worker=False)
        snap = self.store.snapshot(now, "all")
        self.assertEqual(snap["counts"]["live_agents"], 1)
        self.assertEqual(
            [run["run_id"] for run in snap["running"]],
            ["claude-workflow:lead:wf_1/build"],
        )

    def test_wrapper_stays_visible_in_runs(self):
        now = time.time()
        self._lane(now)
        runs = self.store.query_runs(now, None, None, None, 100)
        ids = {run["run_id"] for run in runs}
        self.assertIn("glm:sid1:ws9-WS9.1-build", ids)
        self.assertIn("claude-workflow:lead:wf_1/build", ids)
        wrapper = [run for run in runs if run["source"] == "claude-workflow"][0]
        self.assertTrue(wrapper.get("lane_shadow"))

    def test_unseen_worker_mismatch(self):
        now = time.time()
        self._lane(now)
        procs = [
            {
                "pid": 4242,
                "comm": "grok",
                "started_at": now - 10,
                "label": "ws9-WS9.9-ghost",
                "prompt_label": None,
                "session_id": None,
            }
        ]
        report = self.store.reconcile_numbers(now, None, None, processes=procs)
        self.assertIn(
            "unseen worker: ws9-WS9.9-ghost (grok pid 4242)", report["mismatches"]
        )
        # A live worker with a running row is seen: no mismatch for it.
        seen = [
            {
                "pid": 4243,
                "comm": "cursor-agent",
                "started_at": now - 10,
                "label": "ws9-WS9.1-build",
                "prompt_label": None,
                "session_id": None,
            }
        ]
        report = self.store.reconcile_numbers(now, None, None, processes=seen)
        self.assertNotIn("unseen worker", "\n".join(report["mismatches"]))
        # One mismatch per label even when launcher and child share it.
        both = seen + [
            {
                "pid": 4244,
                "comm": "python3",
                "started_at": now - 9,
                "label": "ws9-WS9.9-ghost",
                "prompt_label": None,
                "session_id": None,
            },
            procs[0],
        ]
        report = self.store.reconcile_numbers(now, None, None, processes=both)
        ghost = [item for item in report["mismatches"] if "ws9-WS9.9-ghost" in item]
        self.assertEqual(len(ghost), 1)

    def test_unseen_worker_grace_for_young_process(self):
        now = time.time()
        self._lane(now)
        young = [
            {
                "pid": 4245,
                "comm": "cursor-agent",
                "started_at": now - 1,
                "label": "ws9-WS9.9-new",
                "prompt_label": None,
                "session_id": None,
            }
        ]
        report = self.store.reconcile_numbers(now, None, None, processes=young)
        self.assertNotIn("unseen worker", "\n".join(report["mismatches"]))
        # No age and no sighting: still a mismatch, as before.
        ageless = [dict(young[0], started_at=None)]
        report = self.store.reconcile_numbers(now, None, None, processes=ageless)
        self.assertIn(
            "unseen worker: ws9-WS9.9-new (cursor-agent pid 4245)",
            report["mismatches"],
        )

    def test_unseen_worker_grace_for_first_sighting(self):
        now = time.time()
        self._lane(now)
        label = "ws9-WS9.9-ramp"
        # A 13-second-old worker this dashboard only just sampled (ramp-up)
        # is not yet a mismatch.
        self.store.add_process_sample(
            {
                "ts": now - 1,
                "pid": 4250,
                "comm": "cursor-agent",
                "rss_kb": 1024,
                "elapsed_s": 13.0,
                "label": label,
                "run_id": None,
                "model": None,
            }
        )
        ramp = [
            {
                "pid": 4250,
                "comm": "cursor-agent",
                "started_at": now - 13,
                "label": label,
                "prompt_label": None,
                "session_id": None,
            }
        ]
        report = self.store.reconcile_numbers(now, None, None, processes=ramp)
        self.assertNotIn("unseen worker", "\n".join(report["mismatches"]))
        # Sampled long ago and still no row: a real mismatch.
        old = "ws9-WS9.9-stale"
        for ts in (now - 60, now - 30):
            self.store.add_process_sample(
                {
                    "ts": ts,
                    "pid": 4251,
                    "comm": "cursor-agent",
                    "rss_kb": 1024,
                    "elapsed_s": 60.0,
                    "label": old,
                    "run_id": None,
                    "model": None,
                }
            )
        stale = [dict(ramp[0], pid=4251, started_at=now - 60, label=old)]
        report = self.store.reconcile_numbers(now, None, None, processes=stale)
        self.assertIn(
            "unseen worker: ws9-WS9.9-stale (cursor-agent pid 4251)",
            report["mismatches"],
        )

    def test_ended_at_is_last_seen_alive_not_start(self):
        now = 1_792_000_000.0
        self.store.upsert_run(
            {
                "run_id": "glm:sid9:ws9-WS9.2-plan",
                "source": "glm",
                "tier": "glm",
                "label": "ws9-WS9.2-plan",
                "session_id": "sid9",
                "status": "running",
                "started_at": 1_792_000_100.0,
            }
        )
        self.store.add_process_sample(
            {
                "ts": 1_792_000_400.0,
                "pid": 4242,
                "comm": "grok",
                "rss_kb": 10,
                "elapsed_s": 300.0,
                "label": "ws9-WS9.2-plan",
                "run_id": "glm:sid9:ws9-WS9.2-plan",
            }
        )
        ended = reconcile_running(self.store, now, processes=[])
        self.assertEqual(ended, 1)
        row = self.store.get_run("glm:sid9:ws9-WS9.2-plan")
        self.assertEqual(row["status"], "died_early")
        self.assertEqual(row["ended_at"], 1_792_000_400.0)
class GracePeriodTest(unittest.TestCase):
    """A dead-pid row within two poll intervals of its source is not stale."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))
        self.now = time.time()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def dead_row(self, run_id, started_ago, usage_ago=None):
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": "muse",
                "tier": "muse",
                "model": "muse-spark-1.3-contributor",
                "label": "ws9-grace",
                "session_id": "grace-session",
                "status": "running",
                "pid": 999999,
                "started_at": self.now - started_ago,
            }
        )
        if usage_ago is not None:
            self.store.add_usage(
                {
                    "ts": self.now - usage_ago,
                    "session_id": "grace-session",
                    "run_id": run_id,
                    "model": "muse-spark-1.3-contributor",
                    "tool": "muse",
                    "message_id": "m1",
                    "input": 10,
                    "output": 5,
                    "cache_read": 0,
                    "cache_write": 0,
                    "reasoning": 0,
                }
            )

    def test_fresh_dead_pid_is_not_stale(self):
        self.dead_row("muse:grace:fresh", 2)
        stale = self.store.stale_running(self.now, pid_alive_fn=lambda pid: False)
        self.assertEqual(stale, [])
        snap = self.store.snapshot(self.now, "all")
        self.assertEqual([run["run_id"] for run in snap["running"]], ["muse:grace:fresh"])
        report = self.store.reconcile_numbers(self.now, self.now - 3600, self.now)
        self.assertFalse(any("muse:grace:fresh" in item for item in report["mismatches"]))

    def test_recent_usage_is_not_stale(self):
        self.dead_row("muse:grace:usage", 100, usage_ago=1)
        stale = self.store.stale_running(self.now, pid_alive_fn=lambda pid: False)
        self.assertEqual(stale, [])

    def test_old_dead_pid_still_fails(self):
        self.dead_row("muse:grace:old", 10)
        stale = self.store.stale_running(self.now, pid_alive_fn=lambda pid: False)
        self.assertEqual([run["run_id"] for run, _reason in stale], ["muse:grace:old"])
        self.assertIn("dead pid", stale[0][1])
        report = self.store.reconcile_numbers(self.now, self.now - 3600, self.now)
        self.assertTrue(any("muse:grace:old" in item for item in report["mismatches"]))


class BackfillGrokModelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))
        self.now = time.time()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_run(self, run_id, model=None):
        row = {
            "run_id": run_id,
            "source": "grok",
            "tier": "grok",
            "label": "ws6-backfill",
            "session_id": "chat",
            "status": "done",
            "started_at": self.now - 100,
            "ended_at": self.now - 50,
        }
        if model is not None:
            row["model"] = model
        self.store.upsert_run(row)

    def add_usage(self, run_id, model, ago, message="m1"):
        self.store.add_usage(
            {
                "ts": self.now - ago,
                "session_id": "chat",
                "run_id": run_id,
                "model": model,
                "tool": "cursor",
                "message_id": message,
                "input": 10,
                "output": 5,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 0,
            }
        )

    def test_latest_usage_model_fills_null(self):
        self.add_run("grok:chat:ws6-backfill")
        self.add_usage("grok:chat:ws6-backfill", "grok-old", 90, "m1")
        self.add_usage("grok:chat:ws6-backfill", "grok-4.7-high", 80, "m2")
        self.assertEqual(self.store.backfill_grok_models(self.now), 1)
        self.assertEqual(self.store.get_run("grok:chat:ws6-backfill")["model"], "grok-4.7-high")

    def test_no_usage_stays_null(self):
        self.add_run("grok:chat:ws6-backfill")
        self.assertEqual(self.store.backfill_grok_models(self.now), 0)
        self.assertIsNone(self.store.get_run("grok:chat:ws6-backfill")["model"])

    def test_known_model_is_untouched(self):
        self.add_run("grok:chat:ws6-backfill", model="composer-2.5")
        self.add_usage("grok:chat:ws6-backfill", "grok-4.7-high", 80)
        self.assertEqual(self.store.backfill_grok_models(self.now), 0)
        self.assertEqual(self.store.get_run("grok:chat:ws6-backfill")["model"], "composer-2.5")

    def test_backfill_is_throttled(self):
        self.add_run("grok:chat:ws6-backfill")
        self.add_usage("grok:chat:ws6-backfill", "grok-4.7-high", 80)
        self.assertEqual(self.store.backfill_grok_models(self.now), 1)
        self.add_run("grok:other:ws6-backfill")
        self.add_usage("grok:other:ws6-backfill", "grok-4.7-high", 80)
        self.assertEqual(self.store.backfill_grok_models(self.now), 0)


class MergeGrokManifestsTest(unittest.TestCase):
    LABEL = "ws6-c11-coverage"
    CHAT_UUID = "c11c11c1-1c11-4c11-8c11-c11c11c11c11"
    PAD_UUID = "d22d22d2-2d22-4d22-8d22-d22d22d22d22"
    CHAT = "grok:%s:ws6-c11-coverage" % CHAT_UUID
    MANIFEST = "grok:%s:cursor/ws6-c11-coverage@1792246363" % PAD_UUID

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))
        self.now = 1792246400.0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_pair(self, chat_model="composer-2.5", manifest_model=None, chat_out="/tmp/t.jsonl"):
        # Production order: the launcher writes the TSV row first, the chat
        # row is discovered after. (A manifest row upserted while its twin
        # is already stored folds at ingest instead of inserting.)
        row = {
            "run_id": self.MANIFEST,
            "source": "grok",
            "tier": "grok",
            "label": self.LABEL,
            "session_id": self.PAD_UUID,
            "status": "unknown",
            "started_at": 1792246363.0,
            "clock_source": "manifest",
            "verdict": "PASS",
            "out_path": "/tmp/cursor/out-ws6-c11-coverage.md",
        }
        if manifest_model is not None:
            row["model"] = manifest_model
        self.store.upsert_run(row)
        self.store.upsert_run(
            {
                "run_id": self.CHAT,
                "source": "grok",
                "tier": "grok",
                "label": self.LABEL,
                "session_id": self.CHAT_UUID,
                "status": "done",
                "model": chat_model,
                "started_at": 1792246364.332,
                "ended_at": 1792246390.0,
                "clock_source": "transcript",
                "out_path": chat_out,
            }
        )

    def test_manifest_folds_into_chat(self):
        self.add_pair()
        self.store.add_usage(
            {
                "ts": 1792246370.0,
                "session_id": self.PAD_UUID,
                "run_id": self.MANIFEST,
                "model": "composer-2.5",
                "tool": "cursor",
                "message_id": "m1",
                "input": 10,
                "output": 5,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 0,
            }
        )
        self.assertEqual(self.store.merge_grok_manifests(), 1)
        self.assertIsNone(self.store.get_run(self.MANIFEST))
        chat = self.store.get_run(self.CHAT)
        self.assertEqual(chat["started_at"], 1792246363.0)
        self.assertEqual(chat["clock_source"], "manifest")
        self.assertEqual(chat["model"], "composer-2.5")
        self.assertEqual(chat["verdict"], "PASS")
        self.assertEqual(chat["out_path"], "/tmp/t.jsonl")
        self.assertEqual(chat["status"], "done")
        moved = self.store._conn.execute(
            "SELECT run_id FROM usage_events WHERE message_id='m1'"
        ).fetchone()
        self.assertEqual(moved["run_id"], self.CHAT)

    def test_manifest_out_path_fills_a_gap(self):
        self.add_pair(chat_out=None)
        self.assertEqual(self.store.merge_grok_manifests(), 1)
        chat = self.store.get_run(self.CHAT)
        self.assertEqual(chat["out_path"], "/tmp/cursor/out-ws6-c11-coverage.md")

    def test_manifest_model_fills_a_gap(self):
        self.add_pair(chat_model=None, manifest_model="grok-4.7-high")
        self.assertEqual(self.store.merge_grok_manifests(), 1)
        self.assertEqual(self.store.get_run(self.CHAT)["model"], "grok-4.7-high")

    def test_row_without_twin_stays(self):
        self.store.upsert_run(
            {
                "run_id": self.MANIFEST,
                "source": "grok",
                "tier": "grok",
                "label": self.LABEL,
                "status": "unknown",
                "started_at": 1792246363.0,
                "clock_source": "manifest",
            }
        )
        self.assertEqual(self.store.merge_grok_manifests(), 0)
        self.assertIsNotNone(self.store.get_run(self.MANIFEST))

    def test_twin_outside_the_window_stays(self):
        self.add_pair()
        self.store.upsert_run({"run_id": self.CHAT, "started_at": 1792246364.332 + 3600})
        self.assertEqual(self.store.merge_grok_manifests(), 0)
        self.assertIsNotNone(self.store.get_run(self.MANIFEST))
        report = self.store.reconcile_numbers(self.now, self.now - 3600, self.now)
        self.assertTrue(
            any("duplicate run" in item for item in report["mismatches"]),
            report["mismatches"],
        )


class DuplicateMismatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))
        self.now = 1792246400.0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, run_id, source, label, start, end, clock="transcript", parent=None):
        row = {
            "run_id": run_id,
            "source": source,
            "label": label,
            "status": "done",
            "started_at": start,
            "ended_at": end,
            "clock_source": clock,
        }
        if parent is not None:
            row["parent_session_id"] = parent
        self.store.upsert_run(row)

    def duplicates(self):
        report = self.store.reconcile_numbers(self.now, self.now - 3600, self.now)
        return [item for item in report["mismatches"] if "duplicate run" in item]

    def test_merged_pair_is_silent(self):
        self.add(
            "grok:parent:cursor/ws6-dup@1792000001",
            "grok",
            "ws6-dup",
            1792000001.0,
            1792000100.0,
            clock="manifest",
        )
        self.add("grok:chat:ws6-dup", "grok", "ws6-dup", 1792000000.0, 1792000100.0)
        self.assertEqual(self.store.merge_grok_manifests(), 1)
        self.assertEqual(self.duplicates(), [])

    def test_two_chat_rows_are_not_a_duplicate(self):
        self.add("grok:aaa:ws6-probe", "grok", "ws6-probe", 1792000000.0, 1792000900.0)
        self.add("grok:bbb:ws6-probe", "grok", "ws6-probe", 1792000500.0, 1792000600.0)
        self.assertEqual(self.duplicates(), [])

    def test_non_overlapping_twins_are_silent(self):
        self.add("grok:chat:ws6-seq", "grok", "ws6-seq", 1792000000.0, 1792000100.0)
        self.add(
            "grok:parent:cursor/ws6-seq@1792005000",
            "grok",
            "ws6-seq",
            1792005000.0,
            1792005100.0,
            clock="manifest",
        )
        self.assertEqual(self.duplicates(), [])

    def test_shadow_link_is_silent(self):
        self.add("grok:chat:ws6-shadow", "grok", "ws6-shadow", 1792000000.0, 1792000100.0)
        self.add(
            "grok:parent:cursor/ws6-shadow@1792000001",
            "grok",
            "ws6-shadow",
            1792000001.0,
            1792000100.0,
            clock="manifest",
            parent="grok:chat:ws6-shadow",
        )
        self.assertEqual(self.duplicates(), [])


class RunAliasTest(unittest.TestCase):
    LABEL = "ws6-c11-alias"
    CHAT_UUID = "c11c11c1-1c11-4c11-8c11-c11c11c11c11"
    PAD_UUID = "d22d22d2-2d22-4d22-8d22-d22d22d22d22"
    CHAT = "grok:%s:ws6-c11-alias" % CHAT_UUID
    MANIFEST = "grok:%s:cursor/ws6-c11-alias@1792246363" % PAD_UUID

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_chat(self, status="done", model="composer-2.5", out=None, start=1792246364.332):
        self.store.upsert_run(
            {
                "run_id": self.CHAT,
                "source": "grok",
                "tier": "grok",
                "label": self.LABEL,
                "session_id": self.CHAT_UUID,
                "status": status,
                "model": model,
                "started_at": start,
                "ended_at": 1792246390.0 if status != "running" else None,
                "clock_source": "transcript",
                "out_path": out,
            }
        )

    def add_manifest(self):
        self.store.upsert_run(
            {
                "run_id": self.MANIFEST,
                "source": "grok",
                "tier": "grok",
                "label": self.LABEL,
                "session_id": self.PAD_UUID,
                "status": "unknown",
                "started_at": 1792246363.0,
                "clock_source": "manifest",
                "verdict": "PASS",
                "out_path": "/tmp/cursor/out-ws6-c11-alias.md",
            }
        )

    def alias_row(self):
        return self.store._conn.execute(
            "SELECT alias_run_id, target_run_id, merged_at FROM run_aliases WHERE alias_run_id=?",
            (self.MANIFEST,),
        ).fetchone()

    def test_merge_writes_alias_with_delete(self):
        self.add_manifest()
        self.add_chat()
        self.assertEqual(self.store.merge_grok_manifests(), 1)
        self.assertIsNone(self.store.get_run(self.MANIFEST))
        hit = self.alias_row()
        self.assertIsNotNone(hit)
        self.assertEqual(hit["target_run_id"], self.CHAT)
        self.assertGreater(hit["merged_at"], 0)

    def test_reupsert_of_alias_gap_fills_target_without_resurrecting(self):
        self.add_manifest()
        self.add_chat(model=None, out=None)
        self.assertEqual(self.store.merge_grok_manifests(), 1)
        self.store.upsert_run(
            {
                "run_id": self.MANIFEST,
                "source": "grok",
                "tier": "grok",
                "label": self.LABEL,
                "session_id": self.PAD_UUID,
                "status": "done",
                "started_at": 1792246363.0,
                "ended_at": 1792246395.0,
                "clock_source": "manifest",
                "model": "grok-4.7-high",
                "verdict": "PASS",
                "out_path": "/tmp/cursor/out-ws6-c11-alias.md",
            }
        )
        self.assertIsNone(self.store.get_run(self.MANIFEST))
        chat = self.store.get_run(self.CHAT)
        self.assertEqual(chat["model"], "grok-4.7-high")
        self.assertEqual(chat["verdict"], "PASS")
        self.assertEqual(chat["out_path"], "/tmp/cursor/out-ws6-c11-alias.md")
        self.assertEqual(chat["started_at"], 1792246363.0)
        self.assertEqual(chat["status"], "done")
        self.assertEqual(chat["session_id"], self.CHAT_UUID)

    def test_reupsert_never_reopens_a_running_target(self):
        self.add_manifest()
        self.add_chat(status="running", model=None, out=None)
        self.assertEqual(self.store.merge_grok_manifests(), 1)
        self.store.upsert_run(
            {
                "run_id": self.MANIFEST,
                "source": "grok",
                "label": self.LABEL,
                "status": "done",
                "started_at": 1792246363.0,
                "ended_at": 1792246395.0,
                "clock_source": "manifest",
            }
        )
        chat = self.store.get_run(self.CHAT)
        self.assertEqual(chat["status"], "running")
        self.assertIsNone(chat["ended_at"])

    def test_ingest_folds_when_twin_already_stored(self):
        # Restart re-ingest: the chat row survived, the TSV row did not.
        self.add_chat(model=None, out=None)
        self.add_manifest()
        self.assertIsNone(self.store.get_run(self.MANIFEST))
        self.assertEqual(self.store.merge_grok_manifests(), 0)
        hit = self.alias_row()
        self.assertIsNotNone(hit)
        self.assertEqual(hit["target_run_id"], self.CHAT)
        chat = self.store.get_run(self.CHAT)
        self.assertEqual(chat["out_path"], "/tmp/cursor/out-ws6-c11-alias.md")
        self.assertEqual(chat["verdict"], "PASS")

    def test_usage_and_samples_for_alias_land_on_target(self):
        self.add_manifest()
        self.add_chat()
        self.assertEqual(self.store.merge_grok_manifests(), 1)
        self.store.add_usage(
            {
                "ts": 1792246370.0,
                "session_id": self.PAD_UUID,
                "run_id": self.MANIFEST,
                "model": "composer-2.5",
                "tool": "cursor",
                "message_id": "m-alias",
                "input": 3,
                "output": 1,
            }
        )
        self.store.add_process_sample(
            {
                "ts": 1792246371.0,
                "pid": 4242,
                "comm": "cursor-agent",
                "label": self.LABEL,
                "run_id": self.MANIFEST,
            }
        )
        usage = self.store._conn.execute(
            "SELECT run_id FROM usage_events WHERE message_id='m-alias'"
        ).fetchone()
        self.assertEqual(usage["run_id"], self.CHAT)
        sample = self.store._conn.execute(
            "SELECT run_id FROM process_samples WHERE pid=4242"
        ).fetchone()
        self.assertEqual(sample["run_id"], self.CHAT)

    def test_offsets_for_alias_share_the_target_slot(self):
        self.add_manifest()
        self.add_chat()
        self.assertEqual(self.store.merge_grok_manifests(), 1)
        self.store.set_offset(self.MANIFEST, 7, 10, 10, 1.0)
        self.assertEqual(self.store.get_offset(self.CHAT), (7, 10))
        self.assertEqual(self.store.get_offset(self.MANIFEST), (7, 10))
        self.store.set_offset(self.CHAT, 7, 20, 20, 2.0)
        self.assertEqual(self.store.get_offset(self.MANIFEST), (7, 20))

    def test_resolve_run_id(self):
        self.assertEqual(self.store.resolve_run_id("muse:s:plain"), "muse:s:plain")
        self.assertEqual(self.store.resolve_run_id(self.MANIFEST), self.MANIFEST)
        self.add_manifest()
        self.add_chat()
        self.assertEqual(self.store.merge_grok_manifests(), 1)
        self.assertEqual(self.store.resolve_run_id(self.MANIFEST), self.CHAT)
        self.assertEqual(self.store.resolve_run_id(self.CHAT), self.CHAT)

    def test_dangling_alias_falls_through_and_recovers(self):
        self.add_manifest()
        self.add_chat()
        self.assertEqual(self.store.merge_grok_manifests(), 1)
        self.store._conn.execute("DELETE FROM runs WHERE run_id=?", (self.CHAT,))
        self.store._conn.commit()
        self.add_manifest()
        self.assertIsNotNone(self.store.get_run(self.MANIFEST))
        self.assertIsNone(self.alias_row())

    def test_migration_creates_alias_table_on_old_schema(self):
        self.store.close()
        path = Path(self.tmp.name) / "dash.sqlite"
        con = sqlite3.connect(path)
        con.execute("DROP TABLE run_aliases")
        con.commit()
        con.close()
        self.store = memory(Path(self.tmp.name))
        names = {
            row[0]
            for row in self.store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("run_aliases", names)
        self.add_manifest()
        self.add_chat()
        self.assertEqual(self.store.merge_grok_manifests(), 1)
        self.assertIsNotNone(self.alias_row())

    def test_mergeable_pair_is_silent_before_the_merge_runs(self):
        self.add_manifest()
        self.add_chat()
        report = self.store.reconcile_numbers(1792246400.0, 1792242800.0, 1792246400.0)
        dups = [item for item in report["mismatches"] if "duplicate run" in item]
        self.assertEqual(dups, [])

    def test_unmergeable_overlap_still_mismatches(self):
        self.add_manifest()
        self.store.upsert_run(
            {
                "run_id": self.CHAT,
                "source": "grok",
                "tier": "grok",
                "label": self.LABEL,
                "session_id": self.CHAT_UUID,
                "status": "done",
                "started_at": 1792246363.0 + 3600,
                "ended_at": 1792246363.0 + 3700,
                "clock_source": "transcript",
            }
        )
        self.assertEqual(self.store.merge_grok_manifests(), 0)
        report = self.store.reconcile_numbers(1792246400.0 + 3700, 1792246400.0, 1792246400.0 + 3700)
        dups = [item for item in report["mismatches"] if "duplicate run" in item]
        self.assertEqual(len(dups), 1)


class MergeMuseManifestsTest(unittest.TestCase):
    """One row per muse run: manifest rows fold into their session row.

    Reproduces the live failure: the session row closed as done while the
    worker was alive, the manifest row stayed running with the process
    samples, and Running now showed no row for the label (unseen worker).
    """

    LABEL = "ws8-WS8.26-ops-matcher"
    UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    SESSION = "muse:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee:ws8-WS8.26-ops-matcher"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))
        self.now = time.time()
        self.epoch = int(self.now - 600)
        self.manifest_id = "muse:None:%s@%d" % (self.LABEL, self.epoch)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_broken_pair(self, sample_age_s=10.0, usage_age_s=120.0):
        """The exact failure: session done, manifest running with samples."""
        self.store.upsert_run(
            {
                "run_id": self.manifest_id,
                "source": "muse",
                "tier": "muse",
                "label": self.LABEL,
                "session_id": None,
                "status": "running",
                "started_at": float(self.epoch),
                "clock_source": "manifest",
                "out_path": "/tmp/pads/muse/out-%s.md" % self.LABEL,
            }
        )
        self.store.upsert_run(
            {
                "run_id": self.SESSION,
                "source": "muse",
                "tier": "muse",
                "label": self.LABEL,
                "session_id": self.UUID,
                "model": "muse-spark-1.3-contributor",
                "status": "running",
                "started_at": float(self.epoch + 2),
                "clock_source": "muse_log",
            }
        )
        self.store.add_usage(
            {
                "ts": self.now - usage_age_s,
                "session_id": self.UUID,
                "run_id": self.manifest_id,
                "model": "muse-spark-1.3-contributor",
                "tool": "muse",
                "message_id": "m-muse-merge",
                "input": 10,
                "output": 5,
            }
        )
        self.store.add_process_sample(
            {
                "ts": self.now - sample_age_s,
                "pid": 87654321,
                "comm": "muse-bin",
                "label": self.LABEL,
                "run_id": self.manifest_id,
            }
        )
        # The bug: a quiet session file closed the live session row.
        self.store.upsert_run(
            {
                "run_id": self.SESSION,
                "status": "done",
                "ended_at": float(self.epoch + 100),
                "pid": None,
            }
        )

    def restart(self):
        """Fresh Store on the same file, like a dashboard restart."""
        self.store.close()
        self.store = memory(Path(self.tmp.name))

    def reingest_manifest(self, status="running"):
        """What the restart manifest poll re-emits for the TSV row."""
        self.store.upsert_run(
            {
                "run_id": self.manifest_id,
                "source": "muse",
                "tier": "muse",
                "model": None,
                "label": self.LABEL,
                "session_id": None,
                "status": status,
                "started_at": float(self.epoch),
                "clock_source": "manifest",
                "out_bytes": 0,
                "err_bytes": 0,
                "out_path": "/tmp/pads/muse/out-%s.md" % self.LABEL,
            }
        )

    def live_proc(self):
        return {
            "pid": 87654321,
            "comm": "muse-bin",
            "started_at": self.now - 600,
            "label": self.LABEL,
            "model": "muse-spark-1.3-contributor",
            "prompt_label": self.LABEL,
            "session_id": self.UUID,
        }

    def unseen(self, store, now):
        report = store.reconcile_numbers(now, None, None, [self.live_proc()])
        return [item for item in report["mismatches"] if "unseen worker" in item]

    def duplicates(self, store, now):
        report = store.reconcile_numbers(now, None, None, [])
        return [item for item in report["mismatches"] if "duplicate run" in item]

    def test_restart_reingest_merges_to_one_running_row(self):
        self.add_broken_pair()
        self.assertIsNotNone(self.store.get_run(self.manifest_id))
        self.assertEqual(self.store.get_run(self.SESSION)["status"], "done")
        self.restart()
        self.reingest_manifest()
        now = time.time()
        self.assertIsNone(self.store.get_run(self.manifest_id))
        merged = self.store.get_run(self.SESSION)
        self.assertEqual(merged["status"], "running")
        self.assertIsNone(merged["ended_at"])
        self.assertEqual(merged["started_at"], float(self.epoch))
        self.assertEqual(merged["out_path"], "/tmp/pads/muse/out-%s.md" % self.LABEL)
        moved = self.store._conn.execute(
            "SELECT DISTINCT run_id FROM process_samples"
        ).fetchall()
        self.assertEqual([row[0] for row in moved], [self.SESSION])
        usage = self.store._conn.execute(
            "SELECT run_id FROM usage_events WHERE message_id='m-muse-merge'"
        ).fetchone()
        self.assertEqual(usage["run_id"], self.SESSION)
        self.assertEqual(self.store.resolve_run_id(self.manifest_id), self.SESSION)
        alias = self.store._conn.execute(
            "SELECT target_run_id FROM run_aliases WHERE alias_run_id=?",
            (self.manifest_id,),
        ).fetchone()
        self.assertEqual(alias["target_run_id"], self.SESSION)
        shot = self.store.snapshot(now, "all")
        labels = [row["label"] for row in shot["running"]]
        self.assertEqual(labels.count(self.LABEL), 1)
        self.assertEqual(self.unseen(self.store, now), [])
        self.assertEqual(self.duplicates(self.store, now), [])

    def test_second_restart_never_resurrects_the_manifest_row(self):
        self.add_broken_pair()
        self.restart()
        self.reingest_manifest()
        first = self.store.get_run(self.SESSION)
        self.assertEqual(first["status"], "running")
        self.restart()
        self.reingest_manifest()
        now = time.time()
        self.assertIsNone(self.store.get_run(self.manifest_id))
        self.assertEqual(self.store.get_run(self.SESSION)["status"], "running")
        self.assertEqual(self.store.merge_manifest_rows("muse", now=now), 0)
        shot = self.store.snapshot(now, "all")
        labels = [row["label"] for row in shot["running"]]
        self.assertEqual(labels.count(self.LABEL), 1)
        self.assertEqual(self.unseen(self.store, now), [])

    def test_stale_evidence_never_reopens_a_dead_row(self):
        self.add_broken_pair(sample_age_s=3600.0, usage_age_s=3600.0)
        self.restart()
        self.reingest_manifest()
        now = time.time()
        self.assertIsNone(self.store.get_run(self.manifest_id))
        self.assertEqual(self.store.get_run(self.SESSION)["status"], "done")
        self.assertEqual(self.duplicates(self.store, now), [])

    def test_glm_manifest_rows_merge_into_their_session_row(self):
        label = "ws8-glm-check"
        session = "glm:bbbbbbbb-cccc-4ddd-8eee-ffffffffffff:%s" % label
        manifest = "glm:None:%s@%d" % (label, self.epoch)
        self.store.upsert_run(
            {
                "run_id": manifest,
                "source": "glm",
                "tier": "glm",
                "label": label,
                "status": "running",
                "started_at": float(self.epoch),
                "clock_source": "manifest",
            }
        )
        self.store.upsert_run(
            {
                "run_id": session,
                "source": "glm",
                "tier": "glm",
                "label": label,
                "session_id": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
                "status": "running",
                "started_at": float(self.epoch + 2),
                "clock_source": "manifest",
            }
        )
        self.assertEqual(self.store.merge_manifest_rows("glm", now=time.time()), 1)
        self.assertIsNone(self.store.get_run(manifest))
        self.assertIsNotNone(self.store.get_run(session))
        self.assertEqual(self.store.resolve_run_id(manifest), session)
        report = self.store.reconcile_numbers(time.time(), None, None, [])
        dups = [item for item in report["mismatches"] if "duplicate run" in item]
        self.assertEqual(dups, [])

    def test_genuine_muse_twin_still_mismatches(self):
        self.store.upsert_run(
            {
                "run_id": self.manifest_id,
                "source": "muse",
                "tier": "muse",
                "label": self.LABEL,
                "status": "done",
                "started_at": float(self.epoch),
                "ended_at": float(self.epoch + 5000),
                "clock_source": "manifest",
            }
        )
        self.store.upsert_run(
            {
                "run_id": self.SESSION,
                "source": "muse",
                "tier": "muse",
                "label": self.LABEL,
                "session_id": self.UUID,
                "status": "done",
                "started_at": float(self.epoch + 3600),
                "ended_at": float(self.epoch + 3700),
                "clock_source": "muse_log",
            }
        )
        self.assertEqual(self.store.merge_manifest_rows("muse", now=time.time()), 0)
        dups = self.duplicates(self.store, time.time())
        self.assertEqual(len(dups), 1)

    def test_quiet_poll_keeps_a_sampled_row_running(self):
        self.store.upsert_run(
            {
                "run_id": self.SESSION,
                "source": "muse",
                "tier": "muse",
                "label": self.LABEL,
                "session_id": self.UUID,
                "status": "running",
                "started_at": float(self.epoch),
            }
        )
        self.store.add_process_sample(
            {
                "ts": time.time() - 5,
                "pid": 87654321,
                "comm": "muse-bin",
                "label": self.LABEL,
                "run_id": self.SESSION,
            }
        )
        run = {"run_id": self.SESSION}
        apply_liveness(self.store, run, "muse", [])
        self.assertEqual(run.get("status"), "running")
        self.assertIsNone(run.get("ended_at"))

    def test_reconcile_keeps_a_sampled_row_running(self):
        self.store.upsert_run(
            {
                "run_id": self.SESSION,
                "source": "muse",
                "tier": "muse",
                "label": self.LABEL,
                "session_id": self.UUID,
                "status": "running",
                "started_at": float(self.epoch),
            }
        )
        self.store.add_process_sample(
            {
                "ts": time.time() - 5,
                "pid": 87654321,
                "comm": "muse-bin",
                "label": self.LABEL,
                "run_id": self.SESSION,
            }
        )
        self.assertEqual(reconcile_running(self.store, time.time(), []), 0)
        self.assertEqual(self.store.get_run(self.SESSION)["status"], "running")


class MatchRunNewestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))
        self.now = time.time()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_label_match_returns_the_newest(self):
        self.store.upsert_run(
            {
                "run_id": "muse:old:ws1-x",
                "source": "muse",
                "label": "ws1-x",
                "status": "running",
                "started_at": self.now - 100,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "muse:new:ws1-x",
                "source": "muse",
                "label": "ws1-x",
                "status": "running",
                "started_at": self.now - 10,
            }
        )
        self.assertEqual(self.store.match_run(None, "ws1-x"), "muse:new:ws1-x")

    def test_single_match_still_works(self):
        self.store.upsert_run(
            {
                "run_id": "muse:only:ws1-y",
                "source": "muse",
                "label": "ws1-y",
                "status": "running",
                "started_at": self.now - 10,
            }
        )
        self.assertEqual(self.store.match_run(None, "ws1-y"), "muse:only:ws1-y")
        self.assertIsNone(self.store.match_run(None, "ws1-missing"))


class NewerOpenLabelRowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))
        self.now = time.time()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, run_id, status, started, ended=None):
        row = {
            "run_id": run_id,
            "source": "muse",
            "label": "ws1-z",
            "status": status,
            "started_at": started,
        }
        if ended is not None:
            row["ended_at"] = ended
        self.store.upsert_run(row)

    def test_newer_running_twin_found(self):
        self.add("muse:old:ws1-z", "running", self.now - 100)
        self.add("muse:new:ws1-z", "running", self.now - 10)
        twin = self.store.newer_open_label_row("ws1-z", "muse", self.now - 100, "muse:old:ws1-z")
        self.assertEqual(twin["run_id"], "muse:new:ws1-z")
        self.assertIsNone(
            self.store.newer_open_label_row("ws1-z", "muse", self.now - 10, "muse:new:ws1-z")
        )

    def test_closed_twin_is_ignored(self):
        self.add("muse:old:ws1-z", "done", self.now - 100, self.now - 90)
        self.add("muse:new:ws1-z", "done", self.now - 10, self.now - 5)
        self.assertIsNone(
            self.store.newer_open_label_row("ws1-z", "muse", self.now - 100, "muse:old:ws1-z")
        )

    def test_unknown_open_twin_counts(self):
        self.add("muse:old:ws1-z", "running", self.now - 100)
        self.add("muse:new:ws1-z", "unknown", self.now - 10)
        twin = self.store.newer_open_label_row("ws1-z", "muse", self.now - 100, "muse:old:ws1-z")
        self.assertEqual(twin["run_id"], "muse:new:ws1-z")


class PruneOffsetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_prune_deletes_only_gone_files_in_batches(self):
        live = os.path.join(self.tmp.name, "live.jsonl")
        with open(live, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        gone = [os.path.join(self.tmp.name, f"gone-{i}.jsonl") for i in range(4)]
        self.store.set_offset(live, 1, 0, 3, 1.0)
        for index, path in enumerate(gone):
            self.store.set_offset(path, 1, 0, 3, 1.0 + index)
        self.store.set_offset("muse:fresh-scan", 0, 5, 3, 2.0)
        self.store.set_offset(os.path.join(self.tmp.name, "gone#b#blob"), 1, 1, 0, 0.0)
        self.assertEqual(self.store.prune_offsets(batch=2), 1)
        self.assertEqual(self.store.prune_offsets(batch=2), 2)
        self.assertEqual(self.store.prune_offsets(batch=2), 1)
        self.assertEqual(self.store.prune_offsets(batch=2), 1)
        self.assertEqual(self.store.prune_offsets(batch=2), 0)
        paths = {
            row[0]
            for row in self.store._conn.execute("SELECT path FROM file_offsets").fetchall()
        }
        self.assertEqual(paths, {live, "muse:fresh-scan"})

    def test_prune_vacuums_once_per_pass(self):
        live = os.path.join(self.tmp.name, "live.jsonl")
        with open(live, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        for index in range(10):
            self.store.set_offset(os.path.join(self.tmp.name, f"old-{index}.jsonl"), 1, 0, 3, 1.0)
        self.store.set_offset(live, 1, 0, 3, 1.0)
        total = 0
        for _ in range(20):
            deleted = self.store.prune_offsets(batch=3)
            total += deleted
            if deleted == 0:
                break
        self.assertEqual(total, 10)
        self.assertEqual(self.store._pruned_since_vacuum, 0)
        remaining = {
            row[0]
            for row in self.store._conn.execute("SELECT path FROM file_offsets").fetchall()
        }
        self.assertEqual(remaining, {live})


class StoreInheritanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _session(self, sid):
        return self.store._one("SELECT * FROM sessions WHERE session_id=?", (sid,))

    def test_child_run_inherits_parent_session_workstream(self):
        self.store.upsert_session(
            {"session_id": "lead-1", "tool": "claude", "label": "ws7-landing", "status": "busy"}
        )
        self.store.upsert_run(
            {
                "run_id": "claude-subagent:lead-1:aaa",
                "source": "claude-subagent",
                "session_id": "lead-1",
                "parent_session_id": "lead-1",
                "label": "helper",
                "status": "running",
                "started_at": 10,
            }
        )
        run = self.store.get_run("claude-subagent:lead-1:aaa")
        self.assertEqual(run["workstream"], "WS7")
        self.assertEqual(run["ws_confidence"], "inherited")

    def test_lead_session_infers_dominant_workers(self):
        now = time.time()
        for index in range(3):
            self.store.upsert_run(
                {
                    "run_id": f"claude-subagent:lead-2:w{index}",
                    "source": "claude-subagent",
                    "session_id": "lead-2",
                    "parent_session_id": "lead-2",
                    "label": f"ws4-task-{index}",
                    "status": "done",
                    "started_at": now - 100,
                    "ended_at": now - 50,
                }
            )
        self.store.upsert_session(
            {"session_id": "lead-2", "tool": "claude", "cwd": "/repo/main", "status": "busy"}
        )
        session = self._session("lead-2")
        self.assertEqual(session["workstream"], "WS4")
        self.assertEqual(session["ws_confidence"], "inferred")

    def test_lead_run_row_infers_like_its_session(self):
        now = time.time()
        for index in range(3):
            self.store.upsert_run(
                {
                    "run_id": f"claude-subagent:lead-3:w{index}",
                    "source": "claude-subagent",
                    "session_id": "lead-3",
                    "parent_session_id": "lead-3",
                    "label": f"ws4-task-{index}",
                    "status": "done",
                    "started_at": now - 100,
                    "ended_at": now - 50,
                }
            )
        self.store.upsert_run(
            {
                "run_id": "claude-main:lead-3:main",
                "source": "claude-main",
                "session_id": "lead-3",
                "status": "running",
                "started_at": now - 200,
            }
        )
        run = self.store.get_run("claude-main:lead-3:main")
        self.assertEqual(run["workstream"], "WS4")
        self.assertEqual(run["ws_confidence"], "inferred")

    def test_split_workers_leave_lead_unassigned(self):
        now = time.time()
        labels = ["ws4-a", "ws4-b", "ws7-c", "ws7-d"]
        for index, label in enumerate(labels):
            self.store.upsert_run(
                {
                    "run_id": f"claude-subagent:lead-4:w{index}",
                    "source": "claude-subagent",
                    "session_id": "lead-4",
                    "parent_session_id": "lead-4",
                    "label": label,
                    "status": "done",
                    "started_at": now - 100,
                    "ended_at": now - 50,
                }
            )
        self.store.upsert_session(
            {"session_id": "lead-4", "tool": "claude", "cwd": "/repo/main", "status": "busy"}
        )
        session = self._session("lead-4")
        self.assertIsNone(session["workstream"])
        self.assertIsNone(session["ws_confidence"])

    def test_refresh_never_overwrites_explicit(self):
        self.store.upsert_session(
            {"session_id": "lead-5", "tool": "claude", "label": "ws7-landing", "status": "busy"}
        )
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws2-fix",
                "source": "muse",
                "session_id": "lead-5",
                "parent_session_id": "lead-5",
                "label": "ws2-fix",
                "status": "running",
                "started_at": 10,
            }
        )
        changed = self.store.refresh_workstream_inheritance(time.time())
        self.assertEqual(changed, 0)
        run = self.store.get_run("muse:s:ws2-fix")
        self.assertEqual(run["workstream"], "WS2")
        self.assertEqual(run["ws_confidence"], "regex")

    def test_refresh_recomputes_when_parent_loses_workstream(self):
        self.store.upsert_session(
            {"session_id": "lead-6", "tool": "claude", "label": "ws7-landing", "status": "busy"}
        )
        self.store.upsert_run(
            {
                "run_id": "claude-subagent:lead-6:aaa",
                "source": "claude-subagent",
                "session_id": "lead-6",
                "parent_session_id": "lead-6",
                "label": "helper",
                "status": "running",
                "started_at": 10,
            }
        )
        self.assertEqual(
            self.store.get_run("claude-subagent:lead-6:aaa")["ws_confidence"], "inherited"
        )
        self.store.upsert_session(
            {
                "session_id": "lead-6",
                "tool": "claude",
                "label": None,
                "cwd": "/repo/main",
                "status": "busy",
            }
        )
        session = self._session("lead-6")
        self.assertIsNone(session["workstream"])
        changed = self.store.refresh_workstream_inheritance(time.time() + 1000)
        self.assertGreaterEqual(changed, 1)
        run = self.store.get_run("claude-subagent:lead-6:aaa")
        self.assertIsNone(run["workstream"])
        self.assertIsNone(run["ws_confidence"])

    def test_ws_rows_expose_confidence(self):
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws7-landing",
                "source": "muse",
                "label": "ws7-landing",
                "session_id": "child",
                "status": "running",
                "started_at": 10,
            }
        )
        snap = self.store.snapshot(20, "all")
        rows = {row["ws"]: row for row in snap["by_workstream"]}
        self.assertEqual(rows["WS7"]["ws_confidence"], ["regex"])


class WorkstreamScopeStoreTest(unittest.TestCase):
    """Lifecycle scope defaults and the archived aggregate, at the store layer.

    WS41 is active, WS42 archived, WS43 dropped (bundle metadata passed
    straight in, no bundle files needed); each holds one priced run.
    """

    MODEL = "claude-sonnet-5-5"
    META = {
        "WS41": {"state": "active", "state_changed_at": 100,
                 "archived_at": None, "depends_on": [], "milestone": None},
        "WS42": {"state": "archived", "state_changed_at": 200,
                 "archived_at": 300, "depends_on": ["WS41"], "milestone": "Part 2"},
        "WS43": {"state": "dropped", "state_changed_at": 200,
                 "archived_at": 300, "depends_on": [], "milestone": None},
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))
        self.now = time.time()
        for num, ws in ((41, "WS41"), (42, "WS42"), (43, "WS43")):
            self.store.upsert_run(
                {
                    "run_id": "muse:s%d:ws%d-probe" % (num, num),
                    "source": "muse",
                    "tier": "muse",
                    "model": self.MODEL,
                    "label": "ws%d-%s.1-probe" % (num, ws),
                    "session_id": "sess-%d" % num,
                    "status": "done",
                    "started_at": self.now - 7200,
                    "ended_at": self.now - 3600,
                }
            )
            self.store.add_usage(
                {
                    "ts": self.now - 3500,
                    "session_id": "sess-%d" % num,
                    "run_id": "muse:s%d:ws%d-probe" % (num, num),
                    "model": self.MODEL,
                    "tool": "muse",
                    "message_id": "m-%d" % num,
                    "input": 1000,
                    "output": 500,
                    "cache_read": 0,
                    "cache_write": 0,
                }
            )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_rows_carry_lifecycle_fields(self):
        rows = {row["ws"]: row for row in self.store.query_workstreams(
            set(), bundle_meta=self.META, allowed_states={"active", "archived", "dropped", "planned", "paused", "done"})}
        self.assertEqual(rows["WS42"]["state"], "archived")
        self.assertEqual(rows["WS42"]["archived_at"], 300)
        self.assertEqual(rows["WS42"]["depends_on"], ["WS41"])
        self.assertEqual(rows["WS42"]["milestone"], "Part 2")
        self.assertEqual(rows["WS41"]["state"], "active")
        self.assertEqual(rows["WS41"]["depends_on"], [])
        self.assertIsNone(rows["WS41"]["milestone"])
        for row in rows.values():
            self.assertEqual(row["tokens"], 1500)

    def test_default_scope_drops_archived_and_dropped(self):
        rows = {row["ws"]: row for row in self.store.query_workstreams(set(), bundle_meta=self.META)}
        self.assertEqual(sorted(rows), ["WS41"])
        rows = {row["ws"]: row for row in self.store.query_workstreams(
            set(), bundle_meta=self.META, allowed_states={"archived"})}
        self.assertEqual(sorted(rows), ["WS42"])

    def test_no_meta_means_everything_active(self):
        rows = {row["ws"] for row in self.store.query_workstreams(set())}
        self.assertEqual(rows, {"WS41", "WS42", "WS43"})

    def test_unassigned_row_has_null_lifecycle_and_survives_scopes(self):
        self.store.upsert_run(
            {
                "run_id": "muse:lonely:helper",
                "source": "muse",
                "tier": "muse",
                "model": self.MODEL,
                "label": "lonely-helper",
                "session_id": "sess-lonely",
                "status": "done",
                "started_at": self.now - 7200,
                "ended_at": self.now - 3600,
            }
        )
        for allowed in (None, {"archived"}, {"active", "planned"}):
            rows = {row["ws"]: row for row in self.store.query_workstreams(
                set(), bundle_meta=self.META, allowed_states=allowed)}
            unassigned = rows["unassigned"]
            self.assertIsNone(unassigned["state"])
            self.assertIsNone(unassigned["archived_at"])
            self.assertEqual(unassigned["depends_on"], [])

    def test_reconcile_archived_aggregate_and_identity(self):
        body = self.store.reconcile_numbers(self.now, processes=[], archived_ws={"WS42", "WS43"})
        self.assertEqual(sorted(body["archived"]["workstreams"]), ["WS42", "WS43"])
        self.assertEqual(body["archived"]["tokens"], 3000)
        self.assertEqual(body["archived"]["tokens_all_time"], 3000)
        visible = [row for row in body["rows"] if row["ws"] not in {"WS42", "WS43"}]
        self.assertEqual(
            sum(row["tokens"] for row in visible) + body["archived"]["tokens"],
            body["all"]["tokens"],
        )
        self.assertEqual(
            sum(row["tokens_all_time"] for row in visible) + body["archived"]["tokens_all_time"],
            body["all_time"]["tokens"],
        )
        self.assertFalse(
            [item for item in body["mismatches"] if item.startswith("archived")],
            body["mismatches"],
        )

    def test_reconcile_without_archived_set_keeps_historic_shape(self):
        body = self.store.reconcile_numbers(self.now, processes=[])
        self.assertNotIn("archived", body)

    def test_archived_identity_checker_fires_on_tampered_sums(self):
        from dash.workstates import archived_aggregate, check_archived_identity

        body = self.store.reconcile_numbers(self.now, processes=[], archived_ws={"WS42"})
        good = archived_aggregate(body["rows"], {"WS42"})
        ok = check_archived_identity(
            body["rows"], body["unassigned"], good,
            body["all"]["tokens"], body["all"]["cost_usd"],
            body["all_time"]["tokens"], body["all_time"]["cost_usd"],
        )
        self.assertEqual(ok, [])
        broken = dict(good, tokens=good["tokens"] + 1,
                      tokens_all_time=good["tokens_all_time"] + 1)
        bad = check_archived_identity(
            body["rows"], body["unassigned"], broken,
            body["all"]["tokens"], body["all"]["cost_usd"],
            body["all_time"]["tokens"], body["all_time"]["cost_usd"],
        )
        self.assertTrue(any(item.startswith("archived tokens:") for item in bad), bad)
        self.assertTrue(any("tokens_all_time" in item for item in bad), bad)


if __name__ == "__main__":
    unittest.main()
