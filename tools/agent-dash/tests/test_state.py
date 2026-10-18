import json
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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

    def test_prev_24h_rides_totals_unassigned_and_rows(self):
        now = time.time()
        self.store.upsert_run(
            {
                "run_id": "muse:win:ws7-win",
                "source": "muse",
                "tier": "muse",
                "model": "claude-sonnet-5-5",
                "label": "ws7-win",
                "session_id": "s-win",
                "status": "done",
                "started_at": now - 7200,
                "ended_at": now - 3600,
            }
        )
        self.store.add_usage(
            {
                "ts": now - 3500,
                "session_id": "s-win",
                "run_id": "muse:win:ws7-win",
                "model": "claude-sonnet-5-5",
                "tool": "muse",
                "message_id": "m-win",
                "input": 1000,
                "output": 500,
                "cache_read": 0,
                "cache_write": 0,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "muse:prev:ws7-prev",
                "source": "muse",
                "tier": "muse",
                "model": "claude-sonnet-5-5",
                "label": "ws7-prev",
                "session_id": "s-prev",
                "status": "done",
                "started_at": now - 32 * 3600,
                "ended_at": now - 30 * 3600,
            }
        )
        self.store.add_usage(
            {
                "ts": now - 30 * 3600 + 60,
                "session_id": "s-prev",
                "run_id": "muse:prev:ws7-prev",
                "model": "claude-sonnet-5-5",
                "tool": "muse",
                "message_id": "m-prev",
                "input": 2000,
                "output": 1000,
                "cache_read": 0,
                "cache_write": 0,
            }
        )
        payload = build_state(self.store, StubRegistry([]), {}, now=now)
        self.assertEqual(payload["totals"]["prev_24h"]["tokens"], 3000)
        self.assertEqual(payload["totals"]["prev_24h"]["finished"], 1)
        self.assertEqual(payload["totals"]["prev_24h"]["live_agents"], 0)
        by_id = {row["id"]: row for row in payload["workstreams"]}
        self.assertEqual(by_id["WS7"]["prev_24h"]["finished"], 1)
        self.assertEqual(
            payload["unassigned"]["prev_24h"],
            {"tokens": 0, "cost_usd": 0.0, "finished": 0, "live_agents": 0},
        )

    def test_decisions_and_risks_blocks(self):
        now = time.time()
        hub = os.path.join(self.tmp.name, "hub")
        os.makedirs(hub)
        with open(os.path.join(hub, "decisions.tsv"), "w") as fh:
            fh.write(
                "date\tid\tdecision\tstatus\n"
                "2026-10-14\tD-1\told choice\taccepted\n"
                "2026-10-16\tD-2\tnew choice\tproposed\n"
            )
        with mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": os.path.join(self.tmp.name, "ws")}):
            os.makedirs(os.path.join(self.tmp.name, "ws"), exist_ok=True)
            payload = build_state(
                self.store, StubRegistry([]), {"hub_dir": hub}, now=now
            )
        self.assertEqual(payload["decisions"], {"total": 2, "open": 1})
        self.assertEqual(payload["risks"], {"total": 0, "by_severity": {}})

    def test_no_hub_means_empty_decisions_and_risks(self):
        empty_ws = os.path.join(self.tmp.name, "empty-ws")
        os.makedirs(empty_ws, exist_ok=True)
        with mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": empty_ws}):
            payload = build_state(self.store, StubRegistry([]), {}, now=time.time())
        self.assertEqual(payload["decisions"], {"total": 0, "open": 0})
        self.assertEqual(payload["risks"], {"total": 0, "by_severity": {}})

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

    def test_grouped_problem_keeps_ids_and_carries_items(self):
        now = time.time()
        self.store.doctor_snapshot(
            [
                {
                    "id": "dep-jump:ws:WS9", "code": "dep-jump",
                    "severity": "warn", "scope": "ws:WS9", "ws": "WS9",
                    "title": "Example (WS9): 2 tasks started before a dependency finished",
                    "detail": "Thing 2 (WS9.2); Thing 3 (WS9.3)",
                    "fix_command": "python3 agent_dash.py ws next WS9",
                    "safe_auto": False,
                    "items": [
                        {"title": "Thing 2", "id": "WS9.2", "since": now - 10},
                        {"title": "Thing 3", "id": "WS9.3", "since": now - 20},
                    ],
                },
            ],
            now,
        )
        payload = build_state(self.store, StubRegistry([]), {}, now=now)
        expected = [item["id"] for item in self.store.doctor_latest(now)["problems"]]
        self.assertEqual([item["id"] for item in payload["problems"]], expected)
        group = payload["problems"][0]
        self.assertEqual(group["id"], "dep-jump:ws:WS9")
        self.assertEqual(len(group["items"]), 2)
        self.assertEqual(group["items_total"], 2)
        self.assertIn("WS9.2", group["message"])

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


class StateLifecycleTest(unittest.TestCase):
    """`/api/state` carries the lifecycle state, the archived aggregate,
    and the project placeholder."""

    MODEL = "claude-sonnet-5-5"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _store(self.tmp.name)
        self.wsroot = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.wsroot)
        self._env = mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": self.wsroot})
        self._env.start()
        self.addCleanup(self._env.stop)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _bundle(self, ws, **fields):
        bundle = {"id": ws, "title": "Probe %s" % ws, "short": ws,
                  "order": 41, "kind": "workstream"}
        bundle.update(fields)
        dest = os.path.join(self.wsroot, ws)
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "bundle.json"), "w") as fh:
            json.dump(bundle, fh)

    def _run(self, now, ws, num):
        self.store.upsert_run(
            {
                "run_id": "muse:s%d:ws%d-probe" % (num, num),
                "source": "muse",
                "tier": "muse",
                "model": self.MODEL,
                "label": "ws%d-%s.1-probe" % (num, ws),
                "session_id": "sess-%d" % num,
                "status": "done",
                "started_at": now - 7200,
                "ended_at": now - 3600,
            }
        )
        self.store.add_usage(
            {
                "ts": now - 3500,
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

    def test_project_placeholder_and_empty_archived(self):
        now = time.time()
        payload = build_state(self.store, StubRegistry([]), {}, now=now)
        self.assertEqual(payload["project"], {"name": None})
        self.assertEqual(
            payload["totals"]["archived"],
            {"tokens": 0, "cost_usd": 0.0, "tokens_all_time": 0,
             "cost_usd_all_time": 0.0, "workstreams": []},
        )

    def test_rows_carry_state_and_archived_holds_archived_work(self):
        now = time.time()
        self._bundle("WS41", state="active")
        self._bundle("WS42", state="archived", state_changed_at=int(now) - 10,
                     archived_at=int(now) - 5)
        self._run(now, "WS41", 41)
        self._run(now, "WS42", 42)
        payload = build_state(self.store, StubRegistry([]), {}, now=now)
        by_id = {row["id"]: row for row in payload["workstreams"]}
        self.assertEqual(by_id["WS41"]["state"], "active")
        self.assertEqual(by_id["WS42"]["state"], "archived")
        archived = payload["totals"]["archived"]
        self.assertEqual(archived["workstreams"], ["WS42"])
        self.assertEqual(archived["tokens"], 1500)
        self.assertEqual(archived["tokens_all_time"], 1500)
        visible = [row for row in payload["workstreams"] if row["id"] != "WS42"]
        self.assertEqual(
            sum(row["tokens"] for row in visible) + archived["tokens"],
            sum(row["tokens"] for row in payload["workstreams"]),
        )


if __name__ == "__main__":
    unittest.main()
