import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash.pricing import cost
from dash.sources.hub import poll as hub_poll
from dash.sources.process import label_from_args, parse_etime, parse_ps, poll as process_poll, reconcile_running
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

    def test_wal_and_offsets(self):
        mode = self.store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(str(mode).lower(), "wal")
        self.assertIsNone(self.store.get_offset("/tmp/a"))
        self.store.set_offset("/tmp/a", 7, 10, 10, 1.0)
        self.assertEqual(self.store.get_offset("/tmp/a"), (7, 10))
        seq = self.store.data_seq
        self.store.set_offset("/tmp/a", 7, 10, 12, 2.0)
        self.assertEqual(self.store.data_seq, seq)
        self.store.set_offset("/tmp/a", 8, 0, 4, 3.0)
        self.assertEqual(self.store.get_offset("/tmp/a"), (8, 0))
        self.assertGreater(self.store.data_seq, seq)

    def test_attribution_and_out_growing(self):
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws7-landing",
                "source": "muse",
                "tier": "muse",
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
        self.assertEqual(buckets[0]["key"], "glm")
        self.assertIsNone(buckets[0]["cost_usd"])

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
        self.assertEqual(set(snap), {"now", "seq", "counts", "running", "recent", "totals", "by_workstream"})
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
        self.assertEqual(counts["live_by_tool"], {"grok": 1, "muse": 0, "glm": 0, "claude": 0})
        self.assertEqual(counts["finished_today"], 1)
        self.assertEqual(counts["finished_in_range"], 1)
        self.assertEqual(counts["finished_by_verdict"], {"PASS": 1})
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
                "label": "bare",
                "status": "unknown",
            }
        )
        runs = self.store.query_runs(1000.0, None, None, None, 10)
        self.assertEqual(len(runs), 1)
        self.assertIsNone(runs[0]["started_at"])
        self.assertIsNone(runs[0]["elapsed_s"])
        self.assertIsNone(runs[0]["context_tokens"])

    def test_context_tokens_are_latest_and_excluded_from_totals(self):
        now = 1_700_000_000.0
        run_id = "grok:chat:ws1-47-extract-rebase"
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": "grok",
                "tier": "grok",
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
                "model": "claude-sonnet-4-5",
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
                "model": "claude-sonnet-4-5",
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
        self.assertEqual(muse["status"], "done")
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
                        "pid": 42,
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
        self.assertEqual(row["status"], "done")
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


if __name__ == "__main__":
    unittest.main()
