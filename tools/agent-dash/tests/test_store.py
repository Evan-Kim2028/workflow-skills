import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash.sources.hub import poll as hub_poll
from dash.sources.process import label_from_args, parse_etime, parse_ps, poll as process_poll
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
        self.assertAlmostEqual(stored, (2000 * 15 + 20 * 75) / 1_000_000)
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
                "model": "glm-5.3",
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
                "model": "glm-5.3",
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

    def test_alerts_and_caps(self):
        now = 10_000.0
        self.store.upsert_run(
            {"run_id": "h", "tier": "haiku", "label": "ws1-12", "status": "running", "started_at": now - 21 * 60}
        )
        self.store.upsert_run(
            {"run_id": "s", "tier": "sonnet", "label": "ws2-3", "status": "running", "started_at": now - 44 * 60}
        )
        self.store.upsert_run(
            {"run_id": "s2", "tier": "sonnet", "label": "WS2.1", "status": "running", "started_at": now - 46 * 60}
        )
        self.store.upsert_run(
            {"run_id": "g", "tier": "glm", "label": "glm-review", "status": "running", "started_at": now - 31 * 60}
        )
        self.store.upsert_run(
            {
                "run_id": "o",
                "tier": "opus",
                "label": "hub",
                "session_id": HUB,
                "status": "running",
                "started_at": now - 3 * 3600,
            }
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
        self.store.upsert_run(
            {
                "run_id": "old",
                "tier": "haiku",
                "label": "ws3-y",
                "status": "killed",
                "started_at": now - 8000,
                "ended_at": now - 7000,
            }
        )
        for pid in range(11):
            self.store.add_process_sample(
                {"ts": now, "pid": pid, "comm": "cursor-agent", "rss_kb": 10, "elapsed_s": 1, "label": None}
            )
        for pid in range(100, 110):
            self.store.add_process_sample(
                {"ts": now, "pid": pid, "comm": "claude", "rss_kb": 10, "elapsed_s": 1, "label": None}
            )
        snap = self.store.snapshot(now)
        kinds = {(alert["kind"], alert["run_id"]) for alert in snap["alerts"]}
        self.assertIn(("long_running", "h"), kinds)
        self.assertIn(("long_running", "s2"), kinds)
        self.assertIn(("long_running", "g"), kinds)
        self.assertNotIn(("long_running", "s"), kinds)
        self.assertNotIn(("long_running", "o"), kinds)
        self.assertIn(("died_early", "dead"), kinds)
        self.assertNotIn(("killed", "old"), kinds)
        self.assertIn(("over_cap", "cap:grok"), kinds)
        self.assertNotIn(("over_cap", "cap:claude"), kinds)
        self.assertEqual(snap["caps"]["grok"], {"live": 11, "cap": 10})
        self.assertEqual(snap["caps"]["claude"], {"live": 10, "cap": None})
        self.assertEqual(snap["caps"]["muse"]["cap"], 10)
        self.assertEqual(snap["caps"]["glm"]["cap"], 5)
        recent_ids = [run["run_id"] for run in snap["recent"]]
        self.assertEqual(recent_ids, ["dead"])
        self.assertNotIn("old", recent_ids)

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
        self.store.upsert_session(
            {"session_id": HUB, "tool": "claude", "cwd": "/tmp/demo", "started_at": 5, "status": "busy"}
        )
        self.store.upsert_run(
            {
                "run_id": "r",
                "source": "claude-main",
                "tier": "opus",
                "model": "claude-opus-5-5",
                "session_id": HUB,
                "status": "running",
                "started_at": 5,
            }
        )
        self.store.add_usage(
            {
                "ts": 6,
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
        self.assertEqual(ws1["running"], 1)
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
