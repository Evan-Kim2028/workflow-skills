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
        self.assertEqual(payload["running"]["count"], 0)
        self.assertEqual(payload["running"]["by_source"], {})
        self.assertEqual(payload["running"]["prod_jobs"], 0)
        self.assertEqual(payload["running"]["prod_jobs_finished"], 0)
        self.assertEqual(payload["totals"]["prod_jobs"], 0)
        self.assertEqual(payload["totals"]["prod_jobs_finished"], 0)

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

    def test_unseen_worker_in_reconcile_but_not_in_problems_until_a_run(self):
        now = time.time()
        ghosts = [{"pid": 4242, "comm": "cursor-agent", "label": "ws1-ghost"}]
        payload = build_state(self.store, StubRegistry([]), {}, now=now, processes=ghosts)
        expected = self.store.reconcile_numbers(now, processes=ghosts)
        self.assertEqual(payload["reconcile"]["mismatches"], expected["mismatches"])
        self.assertTrue(
            any(item == "unseen worker: ws1-ghost (cursor-agent pid 4242)" for item in payload["reconcile"]["mismatches"]),
            payload["reconcile"]["mismatches"],
        )
        self.assertEqual(payload["problems"], [])


class StateProblemsTest(unittest.TestCase):
    """``/api/state`` problems are the persisted doctor list (same ids)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _seed(self, now):
        self.store.doctor_snapshot(
            [
                {
                    "id": "source-error:global:muse", "code": "source-error",
                    "severity": "critical", "scope": "global", "ws": None,
                    "title": "Source muse is failing",
                    "detail": "source 'muse' last poll failed: boom",
                    "fix_command": "python3 agent_dash.py status",
                    "safe_auto": False,
                },
                {
                    "id": "unpriced-model:global:no-such-model-xyz", "code": "unpriced-model",
                    "severity": "critical", "scope": "global", "ws": None,
                    "title": "Model no-such-model-xyz has usage but no price",
                    "detail": "model 'no-such-model-xyz' has 1 usage rows (15 tokens in 24 h)",
                    "fix_command": "add a row to tools/agent-dash/catalog/prices.json, then run: python3 agent_dash.py reprice",
                    "safe_auto": True,
                },
            ],
            now,
        )

    def test_problems_match_doctor_ids(self):
        now = time.time()
        self._seed(now)
        payload = build_state(self.store, StubRegistry([]), {}, now=now)
        expected = [item["id"] for item in self.store.doctor_latest(now)["problems"]]
        self.assertEqual([item["id"] for item in payload["problems"]], expected)
        for item in payload["problems"]:
            self.assertIn("code", item)
            self.assertIn("message", item)
            self.assertIn("command", item)
            self.assertIn("fix", item)
        failing = next(item for item in payload["problems"] if item["code"] == "source-error")
        self.assertIn("muse", failing["message"])
        self.assertIn("python3 agent_dash.py status", failing["command"])
        unpriced = next(item for item in payload["problems"] if item["code"] == "unpriced-model")
        self.assertIn("no-such-model-xyz", unpriced["message"])
        self.assertIn("catalog/prices.json", unpriced["command"])

    def test_no_run_means_no_problems(self):
        now = time.time()
        payload = build_state(self.store, StubRegistry([]), {}, now=now)
        self.assertEqual(payload["problems"], [])

    def test_reconcile_still_live_without_a_doctor_run(self):
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
        self.assertFalse(payload["reconcile"]["ok"])
        self.assertEqual(payload["problems"], [])


if __name__ == "__main__":
    unittest.main()
