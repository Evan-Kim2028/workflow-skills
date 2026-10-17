import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash.state import SCHEMA_VERSION, build_state, tool_version
from dash.store import Store


class StubRegistry:
    def __init__(self, sources):
        self._sources = sources

    def health(self, now, cfg=None):
        body = {
            "ok": not any(item.get("last_error") for item in self._sources),
            "sources": [
                {
                    "name": item["name"],
                    "last_poll": item.get("last_poll"),
                    "last_error": item.get("last_error"),
                    "lag_s": None if item.get("last_poll") is None else max(0.0, now - item["last_poll"]),
                }
                for item in self._sources
            ],
        }
        if cfg is not None:
            body.update({"scratch_roots": [], "run_dirs": [], "warnings": []})
        return body


def _store(tmp: str) -> Store:
    return Store(Path(tmp) / "dash.sqlite")


class StateShapeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_shape_and_versions(self):
        now = time.time()
        registry = StubRegistry([{"name": "generic", "last_poll": now, "last_error": None}])
        payload = build_state(self.store, registry, {}, now=now)
        self.assertEqual(payload["schema"], SCHEMA_VERSION)
        self.assertEqual(payload["version"]["schema"], SCHEMA_VERSION)
        self.assertTrue(payload["version"]["tool"])
        self.assertEqual(tool_version(), payload["version"]["tool"])
        self.assertEqual(payload["now"], now)
        self.assertEqual(payload["window"], {"since": now - 24 * 3600, "until": now, "hours": 24.0})
        self.assertIn("health", payload)
        self.assertIn("workstreams", payload)
        self.assertIn("unassigned", payload)
        self.assertIn("running", payload)
        self.assertIn("totals", payload)
        self.assertIn("totals_all_time", payload)
        self.assertIn("reconcile", payload)
        self.assertIn("problems", payload)
        self.assertEqual(payload["reconcile"], {"ok": True, "mismatches": []})
        self.assertEqual(payload["problems"], [])
        self.assertEqual(payload["running"], {"count": 0, "by_source": {}})

    def test_workstream_rows_carry_progress_and_window_numbers(self):
        now = time.time()
        self.store.upsert_run(
            {
                "run_id": "generic:abc:ws9-WS9.1-slug",
                "source": "generic",
                "tier": "generic",
                "label": "ws9-WS9.1-slug",
                "session_id": "abc",
                "status": "done",
                "started_at": now - 100,
                "ended_at": now - 50,
            }
        )
        payload = build_state(self.store, StubRegistry([]), {}, now=now)
        ids = [row["id"] for row in payload["workstreams"]]
        self.assertIn("WS9", ids)
        row = next(item for item in payload["workstreams"] if item["id"] == "WS9")
        for key in ("id", "title", "plan", "progress", "live_agents", "tokens", "cost_usd",
                    "tokens_all_time", "cost_usd_all_time", "last_activity"):
            self.assertIn(key, row)
        for item in payload["problems"]:
            self.assertIn("code", item)
            self.assertIn("message", item)
            self.assertIn("command", item)

    def test_one_store_pass(self):
        calls = []
        real = Store._summarize_locked

        def spy(locked_self, *args, **kwargs):
            calls.append(1)
            return real(locked_self, *args, **kwargs)

        Store._summarize_locked = spy
        try:
            build_state(self.store, StubRegistry([]), {}, now=time.time())
        finally:
            Store._summarize_locked = real
        self.assertEqual(len(calls), 1)

    def test_verdict_matches_reconcile(self):
        now = time.time()
        self.store.upsert_run(
            {
                "run_id": "muse:dead:ws9-stale",
                "source": "muse",
                "tier": "muse",
                "label": "ws9-WS9.1-stale",
                "session_id": "s1",
                "status": "running",
                "pid": 999999,
                "started_at": now - 60,
            }
        )
        payload = build_state(self.store, StubRegistry([]), {}, now=now)
        expected = self.store.reconcile_numbers(now)
        self.assertEqual(payload["reconcile"]["mismatches"], expected["mismatches"])
        self.assertFalse(payload["reconcile"]["ok"])

    def test_health_carries_run_dir_fields(self):
        now = time.time()
        payload = build_state(self.store, StubRegistry([]), {}, now=now)
        for key in ("scratch_roots", "run_dirs", "warnings"):
            self.assertIn(key, payload["health"])

    def test_unseen_worker_in_reconcile_and_problems(self):
        now = time.time()
        ghosts = [{"pid": 4242, "comm": "cursor-agent", "label": "ws1-ghost"}]
        payload = build_state(self.store, StubRegistry([]), {}, now=now, processes=ghosts)
        expected = self.store.reconcile_numbers(now, processes=ghosts)
        self.assertEqual(payload["reconcile"]["mismatches"], expected["mismatches"])
        self.assertTrue(
            any(item == "unseen worker: ws1-ghost (cursor-agent pid 4242)" for item in payload["reconcile"]["mismatches"]),
            payload["reconcile"]["mismatches"],
        )
        problems = [item for item in payload["problems"] if item["code"] == "reconcile-mismatch"]
        self.assertTrue(
            any("unseen worker: ws1-ghost" in item["message"] for item in problems),
            [item["message"] for item in problems],
        )
        self.assertTrue(all("python3 agent_dash.py check" in item["command"] for item in problems))


class StateProblemsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_source_error_and_stale_source(self):
        now = time.time()
        registry = StubRegistry(
            [
                {"name": "generic", "last_poll": now - 5, "last_error": None},
                {"name": "muse", "last_poll": now - 5, "last_error": "boom"},
                {"name": "quiet", "last_poll": None, "last_error": None},
            ]
        )
        payload = build_state(self.store, registry, {}, now=now)
        codes = [item["code"] for item in payload["problems"]]
        self.assertIn("source-error", codes)
        self.assertIn("source-stale", codes)
        failing = next(item for item in payload["problems"] if item["code"] == "source-error")
        self.assertIn("muse", failing["message"])
        self.assertIn("python3 agent_dash.py status", failing["command"])

    def test_stale_running_commands(self):
        now = time.time()
        self.store.upsert_run(
            {
                "run_id": "muse:dead:ws9-stale",
                "source": "muse",
                "tier": "muse",
                "label": "ws9-WS9.1-stale",
                "session_id": "s1",
                "status": "running",
                "pid": 999999,
                "started_at": now - 60,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "generic:old:g9",
                "source": "generic",
                "tier": "generic",
                "label": "ws9-WS9.2-old",
                "session_id": "s2",
                "status": "running",
                "started_at": now - 7 * 3600,
            }
        )
        payload = build_state(self.store, StubRegistry([]), {}, now=now)
        stale = [item for item in payload["problems"] if item["code"] == "stale-running"]
        self.assertEqual(len(stale), 2)
        by_run = {item["message"]: item["command"] for item in stale}
        muse_cmd = next(cmd for msg, cmd in by_run.items() if "muse:dead" in msg)
        generic_cmd = next(cmd for msg, cmd in by_run.items() if "generic:old" in msg)
        self.assertEqual(muse_cmd, "python3 agent_dash.py check")
        self.assertIn("python3 agent_dash.py report finish --label ws9-WS9.2-old", generic_cmd)
        self.assertFalse(any(item["code"] == "reconcile-mismatch" and "stale running" in item["message"] for item in payload["problems"]))

    def test_unpriced_model(self):
        now = time.time()
        self.store.add_usage(
            {
                "ts": now,
                "session_id": "s",
                "run_id": "generic:s:ws9-WS9.1-x",
                "model": "no-such-model-xyz",
                "tool": "generic",
                "message_id": "m1",
                "input": 10,
                "output": 5,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 0,
            }
        )
        payload = build_state(self.store, StubRegistry([]), {}, now=now)
        unpriced = [item for item in payload["problems"] if item["code"] == "unpriced-model"]
        self.assertTrue(unpriced)
        self.assertIn("no-such-model-xyz", unpriced[0]["message"])
        self.assertIn("catalog/prices.json", unpriced[0]["command"])


if __name__ == "__main__":
    unittest.main()
