import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_dash
from agent_dash import default_cfg, main
from dash.doctor import format_human, run_doctor
from dash.server import DashServer
from dash.sources import Registry
from dash.store import Store


def _cfg(**over):
    cfg = default_cfg()
    cfg.update(over)
    return cfg


def _start_server(store):
    server = DashServer(store, Registry([]), "127.0.0.1", 0, cfg=default_cfg())
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, thread


def _stop_server(server, thread):
    server.stop.set()
    server.shutdown()
    thread.join(3)
    server.server_close()


class DoctorDashboardTest(unittest.TestCase):
    def setUp(self):
        # Isolate from machine-global ps state: the live listing is empty
        # here unless a test stubs it otherwise (same as test_server.py).
        self._live_patch = mock.patch("dash.server._live_workers", return_value=[])
        self._live_patch.start()
        self.addCleanup(self._live_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.server, self.thread = _start_server(self.store)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name

    def tearDown(self):
        _stop_server(self.server, self.thread)
        self.store.close()
        if self.old_ws is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old_ws
        self.tmp.cleanup()

    def test_dashboard_up_and_reconcile_clean(self):
        cfg = _cfg(state_dir=self.tmp.name)
        result = run_doctor(cfg, self.url)
        dashboard = next(item for item in result["checks"] if item["name"] == "dashboard")
        self.assertTrue(dashboard["ok"])
        reconcile = next(item for item in result["checks"] if item["name"] == "reconcile")
        self.assertTrue(reconcile["ok"])

    def test_dashboard_down_names_run_command(self):
        cfg = _cfg(state_dir=self.tmp.name)
        result = run_doctor(cfg, "http://127.0.0.1:1")
        self.assertFalse(result["ok"])
        down = [item for item in result["problems"] if item["code"] == "dashboard-down"]
        self.assertEqual(len(down), 1)
        self.assertEqual(down[0]["command"], "python3 agent_dash.py run")

    def test_reconcile_mismatch_is_a_problem(self):
        self.store.upsert_run(
            {
                "run_id": "muse:dead:ws9-stale",
                "source": "muse",
                "tier": "muse",
                "label": "ws9-WS9.1-stale",
                "session_id": "s1",
                "status": "running",
                "pid": 999999,
                "started_at": time.time() - 60,
            }
        )
        cfg = _cfg(state_dir=self.tmp.name)
        result = run_doctor(cfg, self.url)
        self.assertFalse(result["ok"])
        codes = [item["code"] for item in result["problems"]]
        self.assertIn("reconcile-mismatch", codes)
        mismatch = next(item for item in result["problems"] if item["code"] == "reconcile-mismatch")
        self.assertIn("python3 agent_dash.py check", mismatch["command"])

    def test_unseen_worker_is_a_problem(self):
        ghosts = [{"pid": 4242, "comm": "cursor-agent", "label": "ws1-ghost"}]
        cfg = _cfg(state_dir=self.tmp.name)
        with mock.patch("dash.server._live_workers", return_value=ghosts):
            result = run_doctor(cfg, self.url)
        self.assertFalse(result["ok"])
        unseen = [item for item in result["problems"] if item["code"] == "unseen-worker"]
        self.assertTrue(
            any("ws1-ghost" in item["message"] for item in unseen),
            [item["message"] for item in result["problems"]],
        )

    def test_health_warnings_are_problems(self):
        cfg = _cfg(state_dir=self.tmp.name)
        health = {
            "ok": True,
            "sources": [],
            "scratch_roots": ["/tmp/roots"],
            "run_dirs": ["/tmp/far-away"],
            "warnings": ["live worker manifest outside roots.scratchpads: ws1-x in /tmp/far-away"],
        }
        with mock.patch("dash.doctor._get", side_effect=[health, {"mismatches": [], "rows": []}]):
            result = run_doctor(cfg, self.url)
        run_dirs = next(item for item in result["checks"] if item["name"] == "run-dirs")
        self.assertFalse(run_dirs["ok"])
        outside = [item for item in result["problems"] if item["code"] == "run-dir-outside-roots"]
        self.assertEqual(len(outside), 1)
        self.assertIn("roots.scratchpads", outside[0]["command"])

    def test_unpriced_health_names_models_not_sources(self):
        cfg = _cfg(state_dir=self.tmp.name)
        health = {
            "ok": False,
            "sources": [],
            "unpriced_models": [{"model": "mystery-1", "rows": 3, "tokens": 900}],
        }
        with mock.patch("dash.doctor._get", side_effect=[health, {"mismatches": [], "rows": []}]):
            result = run_doctor(cfg, self.url)
        dashboard = next(item for item in result["checks"] if item["name"] == "dashboard")
        self.assertFalse(dashboard["ok"])
        self.assertIn("mystery-1", dashboard["detail"])
        unpriced = [item for item in result["problems"] if item["code"] == "unpriced-model"]
        self.assertEqual(len(unpriced), 1)
        self.assertIn("mystery-1", unpriced[0]["message"])
        self.assertNotIn("warning", unpriced[0])

    def test_stalled_health_names_sources(self):
        cfg = _cfg(state_dir=self.tmp.name)
        health = {
            "ok": False,
            "sources": [{"name": "muse", "last_poll": 1.0, "last_error": None, "lag_s": 9999.0, "stalled": True}],
        }
        with mock.patch("dash.doctor._get", side_effect=[health, {"mismatches": [], "rows": []}]):
            result = run_doctor(cfg, self.url)
        dashboard = next(item for item in result["checks"] if item["name"] == "dashboard")
        self.assertFalse(dashboard["ok"])
        self.assertIn("muse", dashboard["detail"])
        stalled = [item for item in result["problems"] if item["code"] == "source-stalled"]
        self.assertEqual(len(stalled), 1)
        self.assertIn("muse", stalled[0]["message"])

    def test_run_dirs_txt_registry_covers_manifest(self):
        cfg = _cfg(state_dir=self.tmp.name)
        outside = Path(self.tmp.name) / "far-away"
        outside.mkdir()
        (outside / "muse-sessions.tsv").write_text("label\tsession\tepoch\n", encoding="utf-8")
        (Path(self.tmp.name) / "run-dirs.txt").write_text(str(outside) + "\n", encoding="utf-8")
        with mock.patch("dash.doctor._home_scratch", return_value=str(Path(self.tmp.name) / "noscratch")):
            with mock.patch("dash.doctor._scanned_dirs", return_value={str(outside)}):
                result = run_doctor(cfg, "http://127.0.0.1:1")
        self.assertFalse(any(item["code"] == "manifest-outside-roots" for item in result["problems"]))

    def test_result_shape(self):
        cfg = _cfg(state_dir=self.tmp.name)
        result = run_doctor(cfg, self.url)
        self.assertIn("ok", result)
        self.assertIn("problems", result)
        self.assertIn("checks", result)
        names = [item["name"] for item in result["checks"]]
        for expected in ("dashboard", "sources", "run-dirs", "scratch-roots", "manifest-coverage", "pricing",
                          "reconcile", "page-check", "config", "bundles", "checkout", "build", "python"):
            self.assertIn(expected, names)
        for item in result["checks"]:
            self.assertIn("ok", item)
            self.assertIn("detail", item)
        for item in result["problems"]:
            self.assertIn("code", item)
            self.assertIn("message", item)
            self.assertIn("command", item)
        self.assertTrue(format_human(result))


class DoctorFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.root / "ws")
        (self.root / "ws").mkdir()

    def tearDown(self):
        if self.old_ws is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old_ws
        self.tmp.cleanup()

    def _cfg(self):
        cfg = _cfg(state_dir=str(self.root / "state"))
        (self.root / "state").mkdir(exist_ok=True)
        return cfg

    def test_scratch_root_missing_and_unreadable(self):
        cfg = self._cfg()
        cfg["roots"]["scratchpads"] = [str(self.root / "nope")]
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.home / "agent-scratch")):
            result = run_doctor(cfg, "http://127.0.0.1:1")
        missing = [item for item in result["problems"] if item["code"] == "scratch-root-missing"]
        self.assertEqual(len(missing), 1)
        self.assertIn("roots.scratchpads", missing[0]["command"])

    def test_scratch_root_ok(self):
        cfg = self._cfg()
        present = self.root / "runs"
        present.mkdir()
        cfg["roots"]["scratchpads"] = [str(present)]
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.home / "agent-scratch")):
            result = run_doctor(cfg, "http://127.0.0.1:1")
        scratch = next(item for item in result["checks"] if item["name"] == "scratch-roots")
        self.assertTrue(scratch["ok"])

    def test_manifest_outside_roots_then_registry_covers_it(self):
        cfg = self._cfg()
        runs = self.root / "runs"
        runs.mkdir()
        cfg["roots"]["scratchpads"] = [str(runs)]
        outside = self.root / "far-away"
        outside.mkdir()
        (outside / "muse-sessions.tsv").write_text("label\tsession\tepoch\n", encoding="utf-8")
        (self.root / "state" / "run-dirs.json").write_text(json.dumps([str(outside)]), encoding="utf-8")
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.home / "agent-scratch")):
            result = run_doctor(cfg, "http://127.0.0.1:1")
        found = [item for item in result["problems"] if item["code"] == "manifest-outside-roots"]
        self.assertEqual(len(found), 1)
        self.assertIn(str(outside), found[0]["message"])
        self.assertIn("roots.scratchpads", found[0]["command"])
        inside = runs / "sub"
        inside.mkdir()
        (inside / "muse-sessions.tsv").write_text("label\tsession\tepoch\n", encoding="utf-8")
        (self.root / "state" / "run-dirs.json").write_text(json.dumps([str(inside)]), encoding="utf-8")
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.home / "agent-scratch")):
            covered = run_doctor(cfg, "http://127.0.0.1:1")
        self.assertFalse(any(item["code"] == "manifest-outside-roots" for item in covered["problems"]))

    def test_unpriced_model_from_fake_db(self):
        cfg = self._cfg()
        store = Store(self.root / "state" / "dash.sqlite")
        try:
            store.add_usage(
                {
                    "ts": time.time(),
                    "session_id": "s",
                    "run_id": "generic:s:x",
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
        finally:
            store.close()
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.home / "agent-scratch")):
            result = run_doctor(cfg, "http://127.0.0.1:1")
        unpriced = [item for item in result["problems"] if item["code"] == "unpriced-model"]
        self.assertEqual(len(unpriced), 1)
        self.assertIn("no-such-model-xyz", unpriced[0]["message"])
        self.assertIn("python3 agent_dash.py reprice", unpriced[0]["command"])

    def _add_fake_usage(self, model, tokens):
        store = Store(self.root / "state" / "dash.sqlite")
        try:
            store.add_usage(
                {
                    "ts": time.time(),
                    "session_id": "s",
                    "run_id": f"generic:s:{model}",
                    "model": model,
                    "tool": "generic",
                    "message_id": f"m-{model}",
                    "input": tokens,
                    "output": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "reasoning": 0,
                }
            )
        finally:
            store.close()

    def test_zero_token_rateless_model_is_not_unpriced(self):
        cfg = self._cfg()
        self._add_fake_usage("<synthetic>", 0)
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.home / "agent-scratch")):
            result = run_doctor(cfg, "http://127.0.0.1:1")
        self.assertFalse([item for item in result["problems"] if item["code"] == "unpriced-model"])
        pricing = next(item for item in result["checks"] if item["name"] == "pricing")
        self.assertTrue(pricing["ok"])

    def test_billed_rateless_model_is_critical(self):
        cfg = self._cfg()
        self._add_fake_usage("no-such-model-xyz", 10)
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.home / "agent-scratch")):
            result = run_doctor(cfg, "http://127.0.0.1:1")
        unpriced = [item for item in result["problems"] if item["code"] == "unpriced-model"]
        self.assertEqual(len(unpriced), 1)
        self.assertNotIn("warning", unpriced[0])
        self.assertFalse(result["ok"])
        pricing = next(item for item in result["checks"] if item["name"] == "pricing")
        self.assertFalse(pricing["ok"])

    def test_priced_model_never_alerts(self):
        cfg = self._cfg()
        self._add_fake_usage("claude-fable-5-1", 10)
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.home / "agent-scratch")):
            result = run_doctor(cfg, "http://127.0.0.1:1")
        self.assertFalse([item for item in result["problems"] if item["code"] == "unpriced-model"])
        pricing = next(item for item in result["checks"] if item["name"] == "pricing")
        self.assertTrue(pricing["ok"])

    def test_config_parses_and_flags_unknown_keys(self):
        cfg = self._cfg()
        bad = self.root / "bad.json"
        bad.write_text("{nope", encoding="utf-8")
        odd = self.root / "odd.json"
        odd.write_text(json.dumps({"roots": {"scratchpads": [], "mystery": 1}, "frobnicate": True}), encoding="utf-8")
        with mock.patch("dash.doctor._config_files", return_value=[bad, odd]):
            with mock.patch("dash.doctor._home_scratch", return_value=str(self.home / "agent-scratch")):
                result = run_doctor(cfg, "http://127.0.0.1:1")
        codes = [item["code"] for item in result["problems"]]
        self.assertIn("config-unparseable", codes)
        self.assertIn("config-unknown-key", codes)
        unknown = [item for item in result["problems"] if item["code"] == "config-unknown-key"]
        self.assertTrue(any("frobnicate" in item["message"] for item in unknown))
        self.assertTrue(any("mystery" in item["message"] for item in unknown))

    def test_invalid_bundle_is_a_problem(self):
        from dash.bundles import cmd_ws

        self.assertEqual(cmd_ws(["new", "WS9", "Example"]), 0)
        plan = self.root / "ws" / "WS9" / "plan.json"
        data = json.loads(plan.read_text(encoding="utf-8"))
        data["dag"]["groups"][0]["tasks"][0]["status"] = "doing"
        plan.write_text(json.dumps(data), encoding="utf-8")
        cfg = self._cfg()
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.home / "agent-scratch")):
            result = run_doctor(cfg, "http://127.0.0.1:1")
        failing = [item for item in result["problems"] if item["code"] == "invalid-bundle"]
        self.assertEqual(len(failing), 1)
        self.assertIn("WS9", failing[0]["message"])
        self.assertEqual(failing[0]["command"], "python3 agent_dash.py ws validate WS9")

    def test_missing_node_is_a_problem(self):
        cfg = self._cfg()
        with mock.patch("shutil.which", return_value=None):
            with mock.patch("dash.doctor._home_scratch", return_value=str(self.home / "agent-scratch")):
                result = run_doctor(cfg, "http://127.0.0.1:1")
        missing = [item for item in result["problems"] if item["code"] == "page-check-no-node"]
        self.assertEqual(len(missing), 1)
        self.assertIn("python3 agent_dash.py check --page", missing[0]["command"])

    def test_dirty_checkout_is_warning_only(self):
        cfg = self._cfg()
        proc = mock.Mock(returncode=0, stdout=" M tools/agent-dash/dash/state.py\n")
        with mock.patch("subprocess.run", return_value=proc):
            with mock.patch("dash.doctor._home_scratch", return_value=str(self.home / "agent-scratch")):
                result = run_doctor(cfg, "http://127.0.0.1:1")
        dirty = [item for item in result["problems"] if item["code"] == "dashboard-dirty-checkout"]
        self.assertEqual(len(dirty), 1)
        self.assertTrue(dirty[0]["warning"])

    def test_doctor_is_read_only(self):
        import sqlite3

        cfg = self._cfg()
        db = self.root / "state" / "dash.sqlite"
        store = Store(db)
        store.close()
        before = sorted(path.name for path in (self.root / "state").iterdir() if "-shm" not in path.name and "-wal" not in path.name)
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.home / "agent-scratch")):
            run_doctor(cfg, "http://127.0.0.1:1")
        after = sorted(path.name for path in (self.root / "state").iterdir() if "-shm" not in path.name and "-wal" not in path.name)
        self.assertEqual(before, after)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(rows, 0)


class DoctorCliTest(unittest.TestCase):
    def test_cli_json_shape_and_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_state = os.environ.get("AGENT_DASH_STATE_DIR")
            old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
            os.environ["AGENT_DASH_STATE_DIR"] = tmp
            os.environ["AGENT_DASH_WORKSTREAMS"] = tmp
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = main(["doctor", "--url", "http://127.0.0.1:1", "--json"])
            finally:
                if old_state is None:
                    os.environ.pop("AGENT_DASH_STATE_DIR", None)
                else:
                    os.environ["AGENT_DASH_STATE_DIR"] = old_state
                if old_ws is None:
                    os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
                else:
                    os.environ["AGENT_DASH_WORKSTREAMS"] = old_ws
        self.assertEqual(code, 1)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["problems"])
        for key in ("generated_at", "next_run_at", "interval_s", "summary", "resolved_24h"):
            self.assertIn(key, payload)
        self.assertIn("critical", payload["summary"])
        for item in payload["problems"]:
            for key in ("id", "code", "severity", "scope", "ws", "title", "detail",
                        "fix", "first_seen", "last_seen", "count", "regressed"):
                self.assertIn(key, item, f"problem misses {key}")
            for key in ("command", "safe_auto", "doc"):
                self.assertIn(key, item["fix"])

    def test_cli_rejects_bad_severity(self):
        self.assertEqual(main(["doctor", "--severity", "nope"]), 2)


class WorkstreamHygieneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wsroot = self.root / "ws"
        self.wsroot.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.wsroot)
        self.store = Store(self.state / "dash.sqlite")
        self.now = 1_700_000_000.0
        self.health = {"ok": True, "sources": []}

    def tearDown(self):
        self.store.close()
        if self.old_ws is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old_ws
        self.tmp.cleanup()

    def _cfg(self):
        cfg = _cfg(state_dir=str(self.state))
        cfg["roots"]["scratchpads"] = []
        return cfg

    def _bundle(self, ws="WS9", tasks=None, trains=None):
        from dash.bundles import cmd_ws

        self.assertEqual(cmd_ws(["new", ws, "Example"]), 0)
        plan = self.wsroot / ws / "plan.json"
        data = json.loads(plan.read_text(encoding="utf-8"))
        if tasks is not None:
            data["dag"]["groups"] = [
                {"id": "build", "col": 0, "title": "Build", "tasks": tasks}
            ]
        if trains is not None:
            data["trains"] = trains
        plan.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return self.wsroot / ws

    def _collect(self, **over):
        from dash.doctor import collect_problems

        params = {"health": self.health, "processes": []}
        params.update(over)
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.root / "noscratch")):
            return collect_problems(self.store, self._cfg(), self.now, **params)

    def _codes(self, result):
        return [(item["code"], item["id"], item["scope"], item["ws"]) for item in result["problems"]]

    def test_starting_source_emits_no_stalled_problem(self):
        self.health = {
            "ok": True,
            "sources": [
                {"name": "muse", "last_poll": None, "last_error": None, "lag_s": None,
                 "stalled": False, "state": "starting", "started_at": self.now - 5,
                 "interval_s": 2},
            ],
        }
        result = self._collect()
        codes = [item["code"] for item in result["problems"]]
        self.assertNotIn("source-stalled", codes)
        sources = next(item for item in result["checks"] if item["name"] == "sources")
        self.assertTrue(sources["ok"])

    def test_slow_source_is_a_warn_problem(self):
        self.health = {
            "ok": True,
            "sources": [
                {"name": "dagster", "last_poll": self.now - 1, "last_error": None, "lag_s": 1.0,
                 "stalled": False, "state": "slow", "started_at": self.now - 900,
                 "interval_s": 60, "last_duration_s": 16.2},
            ],
            "slow_sources": 1,
        }
        result = self._collect()
        slow = [item for item in result["problems"] if item["code"] == "source-slow"]
        self.assertEqual(len(slow), 1)
        self.assertEqual(slow[0]["severity"], "warn")
        self.assertIn("16.2", slow[0]["detail"])
        slow_check = next(item for item in result["checks"] if item["name"] == "slow-sources")
        self.assertFalse(slow_check["ok"])

    def test_dagster_closeout_skip_is_a_warn_problem(self):
        self.health = {
            "ok": True,
            "sources": [],
            "dagster": {"skipped_closeouts": 2},
        }
        result = self._collect()
        skipped = [item for item in result["problems"] if item["code"] == "dagster-closeout-skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["severity"], "warn")
        self.assertIn("2", skipped[0]["detail"])

    def test_stuck_task_and_live_agent_clears_it(self):
        self._bundle(tasks=[
            {"id": "WS9.1", "title": "Do the thing", "status": "wip", "deps": []},
        ])
        (self.wsroot / "WS9" / "history.jsonl").write_text(
            json.dumps({"ts": self.now - 7 * 3600, "task": "WS9.1", "from": "todo", "to": "wip"}) + "\n",
            encoding="utf-8",
        )
        result = self._collect()
        stuck = [item for item in result["problems"] if item["code"] == "stuck-task"]
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0]["id"], "stuck-task:ws:WS9")
        self.assertEqual(stuck[0]["scope"], "ws:WS9")
        self.assertEqual(stuck[0]["ws"], "WS9")
        self.assertEqual(stuck[0]["severity"], "warn")
        self.assertIn("WS9.1", stuck[0]["detail"])
        self.assertIn("1 task", stuck[0]["title"])
        self.assertEqual(stuck[0]["fix_command"], "python3 agent_dash.py doctor --ws WS9 --all")
        live = [{"pid": 4242, "comm": "cursor-agent", "label": "ws9-WS9.1-slug",
                 "started_at": self.now - 60}]
        again = self._collect(processes=live)
        self.assertFalse([item for item in again["problems"] if item["code"] == "stuck-task"])

    def test_live_run_row_clears_stuck(self):
        self._bundle(tasks=[
            {"id": "WS9.1", "title": "Do the thing", "status": "wip", "deps": []},
        ])
        (self.wsroot / "WS9" / "history.jsonl").write_text(
            json.dumps({"ts": self.now - 7 * 3600, "task": "WS9.1", "from": "todo", "to": "wip"}) + "\n",
            encoding="utf-8",
        )
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws9-WS9.1-x",
                "source": "muse",
                "tier": "muse",
                "label": "ws9-WS9.1-x",
                "session_id": "s",
                "status": "running",
                "started_at": self.now - 100,
            }
        )
        result = self._collect()
        self.assertFalse([item for item in result["problems"] if item["code"] == "stuck-task"])

    def test_done_before_deps(self):
        self._bundle(tasks=[
            {"id": "WS9.1", "title": "First", "status": "todo", "deps": []},
            {"id": "WS9.2", "title": "Second", "status": "done", "deps": ["WS9.1"]},
        ])
        result = self._collect()
        found = [item for item in result["problems"] if item["code"] == "done-before-deps"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "done-before-deps:ws:WS9")
        self.assertIn("1 task", found[0]["title"])
        self.assertIn("WS9", found[0]["title"])
        self.assertIn("Second (WS9.2)", found[0]["detail"])
        self.assertIn("First (WS9.1)", found[0]["detail"])
        self.assertEqual(found[0]["fix_command"], "python3 agent_dash.py doctor --ws WS9 --all")

    def test_gate_blocking(self):
        self._bundle(tasks=[
            {"id": "WS9.1", "title": "Wait for freeze", "status": "todo", "kind": "gate", "deps": []},
            {"id": "WS9.2", "title": "Publish", "status": "wip", "deps": ["WS9.1"]},
        ])
        result = self._collect()
        found = [item for item in result["problems"] if item["code"] == "gate-blocking"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "gate-blocking:ws:WS9:WS9.1")
        self.assertIn("WS9.2", found[0]["detail"])

    def test_dep_cycle(self):
        self._bundle(tasks=[
            {"id": "WS9.1", "title": "First", "status": "todo", "deps": ["WS9.2"]},
            {"id": "WS9.2", "title": "Second", "status": "todo", "deps": ["WS9.1"]},
        ])
        result = self._collect()
        found = [item for item in result["problems"] if item["code"] == "dep-cycle"]
        self.assertEqual(len(found), 1)
        self.assertIn("WS9.1", found[0]["detail"])
        self.assertIn("WS9.2", found[0]["detail"])

    def test_invalid_bundle_and_bare_citation_split(self):
        self._bundle(tasks=[
            {"id": "WS9.1", "title": "ADR 0098", "status": "doing", "deps": []},
        ])
        result = self._collect()
        codes = [item["code"] for item in result["problems"]]
        self.assertIn("invalid-bundle", codes)
        self.assertIn("bare-citation", codes)
        bare = next(item for item in result["problems"] if item["code"] == "bare-citation")
        self.assertEqual(bare["scope"], "ws:WS9")

    def test_approval_stale(self):
        from dash.bundles import cmd_ws

        self._bundle()
        self.assertEqual(
            cmd_ws(["approve", "WS9", "cutover", "pending", "--step", "Publish it",
                    "--reversible", "no", "--conditions", "File is on disk."]),
            0,
        )
        old = self.now - 4 * 86400
        os.utime(self.wsroot / "WS9" / "approvals.json", (old, old))
        with mock.patch("time.time", return_value=self.now):
            result = self._collect()
        found = [item for item in result["problems"] if item["code"] == "approval-stale"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "approval-stale:ws:WS9:cutover")
        self.assertIn("python3 agent_dash.py ws approvals WS9", found[0]["fix_command"])

    def test_train_stuck(self):
        self._bundle(
            tasks=[{"id": "WS9.1", "title": "First", "status": "done", "deps": []}],
            trains=[{"id": "T1", "title": "cutover", "members": ["WS9.1"],
                     "status": "assembling", "branch": "train/cutover"}],
        )
        old = self.now - 25 * 3600
        os.utime(self.wsroot / "WS9" / "plan.json", (old, old))
        result = self._collect()
        found = [item for item in result["problems"] if item["code"] == "train-stuck"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "train-stuck:ws:WS9:T1")


class LifecycleDoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wsroot = self.root / "ws"
        self.wsroot.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.wsroot)
        self.store = Store(self.state / "dash.sqlite")
        self.now = 1_700_000_000.0
        self.codes = (
            "done-not-archived", "active-idle", "planned-blocked",
            "planned-ready", "phase-slipping",
        )

    def tearDown(self):
        self.store.close()
        if self.old_ws is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old_ws
        self.tmp.cleanup()

    def _cfg(self, **doctor):
        cfg = _cfg(state_dir=str(self.state))
        cfg["roots"]["scratchpads"] = []
        if doctor:
            cfg["doctor"] = dict(doctor)
        return cfg

    def _bundle(self, ws, title, tasks, state=None, state_changed_at=None,
                depends_on=None, milestone=None, history=None, plan_age_days=30):
        from dash.bundles import cmd_ws

        self.assertEqual(cmd_ws(["new", ws, title]), 0)
        root = self.wsroot / ws
        plan = root / "plan.json"
        data = json.loads(plan.read_text(encoding="utf-8"))
        data["dag"]["groups"] = [
            {"id": "build", "col": 0, "title": "Build", "tasks": tasks}
        ]
        plan.write_text(json.dumps(data, indent=2), encoding="utf-8")
        meta = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
        if state is not None:
            meta["state"] = state
        if state_changed_at is not None:
            meta["state_changed_at"] = state_changed_at
        if depends_on is not None:
            meta["depends_on"] = depends_on
        if milestone is not None:
            meta["milestone"] = milestone
        (root / "bundle.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        if history:
            (root / "history.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in history), encoding="utf-8"
            )
        old = self.now - plan_age_days * 86400
        os.utime(plan, (old, old))
        return root

    def _collect(self, cfg=None, **over):
        from dash.doctor import collect_problems

        params = {"health": {"ok": True, "sources": []}, "processes": []}
        params.update(over)
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.root / "noscratch")):
            return collect_problems(self.store, cfg or self._cfg(), self.now, **params)

    def _problems(self, result, code):
        return [item for item in result["problems"] if item["code"] == code]

    def test_done_state_fires_warn(self):
        self._bundle(
            "WS41", "North Meadow",
            tasks=[{"id": "WS41.1", "title": "First", "status": "done", "deps": []}],
            state="done", state_changed_at=self.now - 8 * 86400,
        )
        found = self._problems(self._collect(), "done-not-archived")
        self.assertEqual(len(found), 1)
        item = found[0]
        self.assertEqual(item["id"], "done-not-archived:ws:WS41")
        self.assertEqual(item["scope"], "ws:WS41")
        self.assertEqual(item["ws"], "WS41")
        self.assertEqual(item["severity"], "warn")
        self.assertLess(item["title"].index("North Meadow"), item["title"].index("WS41"))
        self.assertIn("8.0 days", item["detail"])
        self.assertIn("docs/closing-report.md", item["detail"])
        self.assertEqual(item["fix_command"], "python3 agent_dash.py ws archive WS41")

    def test_done_state_silent_inside_nag(self):
        self._bundle(
            "WS41", "North Meadow",
            tasks=[{"id": "WS41.1", "title": "First", "status": "done", "deps": []}],
            state="done", state_changed_at=self.now - 2 * 86400,
        )
        self.assertFalse(self._problems(self._collect(), "done-not-archived"))

    def test_done_nag_threshold_edge(self):
        self._bundle(
            "WS41", "North Meadow",
            tasks=[{"id": "WS41.1", "title": "First", "status": "done", "deps": []}],
            state="done", state_changed_at=self.now - 7 * 86400,
        )
        self.assertFalse(self._problems(self._collect(), "done-not-archived"))
        meta = json.loads((self.wsroot / "WS41" / "bundle.json").read_text(encoding="utf-8"))
        meta["state_changed_at"] = self.now - 7 * 86400 - 120
        (self.wsroot / "WS41" / "bundle.json").write_text(json.dumps(meta), encoding="utf-8")
        found = self._problems(self._collect(), "done-not-archived")
        self.assertEqual(len(found), 1)

    def test_done_nag_config_override(self):
        self._bundle(
            "WS41", "North Meadow",
            tasks=[{"id": "WS41.1", "title": "First", "status": "done", "deps": []}],
            state="done", state_changed_at=self.now - 8 * 86400,
        )
        self.assertFalse(
            self._problems(self._collect(self._cfg(archive_nag_days=30)), "done-not-archived")
        )

    def test_active_complete_plan_fires(self):
        self._bundle(
            "WS41", "North Meadow",
            tasks=[
                {"id": "WS41.1", "title": "First", "status": "done", "deps": []},
                {"id": "WS41.2", "title": "Moved", "status": "deferred", "deps": []},
            ],
            state="active",
            history=[{"ts": self.now - 8 * 86400, "task": "WS41.1",
                      "from": "pr", "to": "done"}],
        )
        found = self._problems(self._collect(), "done-not-archived")
        self.assertEqual(len(found), 1)
        self.assertIn("100%", found[0]["title"])

    def test_active_incomplete_plan_silent_for_nag(self):
        self._bundle(
            "WS41", "North Meadow",
            tasks=[
                {"id": "WS41.1", "title": "First", "status": "done", "deps": []},
                {"id": "WS41.2", "title": "Second", "status": "todo", "deps": []},
            ],
            state="active",
            history=[{"ts": self.now - 8 * 86400, "task": "WS41.1",
                      "from": "pr", "to": "done"}],
        )
        self.assertFalse(self._problems(self._collect(), "done-not-archived"))

    def test_archived_never_nags(self):
        self._bundle(
            "WS41", "North Meadow",
            tasks=[{"id": "WS41.1", "title": "First", "status": "done", "deps": []}],
            state="archived", state_changed_at=self.now - 60 * 86400,
        )
        self.assertFalse(self._problems(self._collect(), "done-not-archived"))

    def test_active_idle_fires_warn(self):
        self._bundle(
            "WS41", "North Meadow",
            tasks=[{"id": "WS41.1", "title": "First", "status": "todo", "deps": []}],
            state="active",
            history=[{"ts": self.now - 4 * 86400, "task": "WS41.1",
                      "from": "todo", "to": "wip"}],
        )
        found = self._problems(self._collect(), "active-idle")
        self.assertEqual(len(found), 1)
        item = found[0]
        self.assertEqual(item["id"], "active-idle:ws:WS41")
        self.assertEqual(item["severity"], "warn")
        self.assertLess(item["title"].index("North Meadow"), item["title"].index("WS41"))
        self.assertIn("4.0 days", item["detail"])
        self.assertEqual(item["fix_command"], "python3 agent_dash.py ws next WS41")

    def test_active_idle_silent_when_recent(self):
        self._bundle(
            "WS41", "North Meadow",
            tasks=[{"id": "WS41.1", "title": "First", "status": "todo", "deps": []}],
            state="active",
            history=[{"ts": self.now - 3600, "task": "WS41.1",
                      "from": "todo", "to": "wip"}],
        )
        self.assertFalse(self._problems(self._collect(), "active-idle"))

    def test_active_idle_silent_when_paused_or_nothing_open(self):
        self._bundle(
            "WS41", "North Meadow",
            tasks=[{"id": "WS41.1", "title": "First", "status": "todo", "deps": []}],
            state="paused",
            history=[{"ts": self.now - 10 * 86400, "task": "WS41.1",
                      "from": "todo", "to": "wip"}],
        )
        self._bundle(
            "WS42", "South Field",
            tasks=[{"id": "WS42.1", "title": "First", "status": "done", "deps": []}],
            state="active",
            history=[{"ts": self.now - 10 * 86400, "task": "WS42.1",
                      "from": "pr", "to": "done"}],
        )
        self.assertFalse(self._problems(self._collect(), "active-idle"))

    def test_active_idle_config_override(self):
        self._bundle(
            "WS41", "North Meadow",
            tasks=[{"id": "WS41.1", "title": "First", "status": "todo", "deps": []}],
            state="active",
            history=[{"ts": self.now - 4 * 86400, "task": "WS41.1",
                      "from": "todo", "to": "wip"}],
        )
        self.assertFalse(
            self._problems(self._collect(self._cfg(active_idle_days=10)), "active-idle")
        )

    def test_absent_state_reads_as_active(self):
        self._bundle(
            "WS41", "North Meadow",
            tasks=[{"id": "WS41.1", "title": "First", "status": "todo", "deps": []}],
            milestone="Alpha",
            history=[{"ts": self.now - 4 * 86400, "task": "WS41.1",
                      "from": "todo", "to": "wip"}],
        )
        found = self._problems(self._collect(), "active-idle")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["ws"], "WS41")

    def test_planned_blocked_on_dependency(self):
        self._bundle(
            "WS41", "North Meadow",
            tasks=[{"id": "WS41.1", "title": "First", "status": "done", "deps": []}],
            state="active",
            history=[{"ts": self.now - 3600, "task": "WS41.1",
                      "from": "pr", "to": "done"}],
        )
        self._bundle(
            "WS42", "South Field",
            tasks=[{"id": "WS42.1", "title": "First", "status": "todo", "deps": []}],
            state="planned", depends_on=["WS41"],
        )
        found = self._problems(self._collect(), "planned-blocked")
        self.assertEqual(len(found), 1)
        item = found[0]
        self.assertEqual(item["id"], "planned-blocked:ws:WS42")
        self.assertEqual(item["severity"], "info")
        self.assertLess(item["title"].index("South Field"), item["title"].index("WS42"))
        self.assertIn("North Meadow (WS41)", item["detail"])
        self.assertEqual(item["fix_command"], "python3 agent_dash.py ws gates WS42")
        self.assertFalse(self._problems(self._collect(), "planned-ready"))

    def test_planned_blocked_on_unknown_dependency(self):
        self._bundle(
            "WS42", "South Field",
            tasks=[{"id": "WS42.1", "title": "First", "status": "todo", "deps": []}],
            state="planned", depends_on=["WS99"],
        )
        found = self._problems(self._collect(), "planned-blocked")
        self.assertEqual(len(found), 1)
        self.assertIn("unknown workstream (WS99)", found[0]["detail"])

    def test_planned_blocked_on_stale_gate(self):
        self._bundle(
            "WS41", "North Meadow",
            tasks=[{"id": "WS41.1", "title": "First", "status": "done", "deps": []}],
            state="done", state_changed_at=self.now - 3600,
        )
        self._bundle(
            "WS42", "South Field",
            tasks=[
                {"id": "WS42.g", "title": "Freeze lifts", "status": "todo",
                 "kind": "gate", "deps": [], "signal": "site freeze",
                 "confirmed_at": "2020-01-01T00:00:00Z", "fresh_hours": 24},
                {"id": "WS42.1", "title": "First", "status": "todo", "deps": []},
            ],
            state="planned", depends_on=["WS41"],
        )
        found = self._problems(self._collect(), "planned-blocked")
        self.assertEqual(len(found), 1)
        self.assertIn("WS42.g", found[0]["detail"])
        self.assertIn("site freeze", found[0]["detail"])

    def test_planned_ready_when_clear(self):
        self._bundle(
            "WS41", "North Meadow",
            tasks=[{"id": "WS41.1", "title": "First", "status": "done", "deps": []}],
            state="done", state_changed_at=self.now - 3600,
        )
        self._bundle(
            "WS42", "South Field",
            tasks=[{"id": "WS42.1", "title": "First", "status": "todo", "deps": []}],
            state="planned", depends_on=["WS41"],
        )
        found = self._problems(self._collect(), "planned-ready")
        self.assertEqual(len(found), 1)
        item = found[0]
        self.assertEqual(item["id"], "planned-ready:ws:WS42")
        self.assertEqual(item["severity"], "info")
        self.assertIn("ready to start", item["title"])
        self.assertEqual(item["fix_command"], "python3 agent_dash.py ws state WS42 active")
        self.assertFalse(self._problems(self._collect(), "planned-blocked"))

    def test_planned_ready_with_no_dependencies(self):
        self._bundle(
            "WS42", "South Field",
            tasks=[{"id": "WS42.1", "title": "First", "status": "todo", "deps": []}],
            state="planned",
        )
        found = self._problems(self._collect(), "planned-ready")
        self.assertEqual(len(found), 1)

    def test_phase_slipping_after_finished_phase(self):
        (self.wsroot / "project.json").write_text(json.dumps({
            "name": "Demo project",
            "phases": [{"name": "Alpha"}, {"name": "Beta"}],
        }), encoding="utf-8")
        self._bundle(
            "WS51", "Alpha One",
            tasks=[{"id": "WS51.1", "title": "First", "status": "done", "deps": []}],
            state="done", state_changed_at=self.now - 10 * 86400, milestone="Alpha",
        )
        self._bundle(
            "WS53", "Beta One",
            tasks=[{"id": "WS53.1", "title": "First", "status": "todo", "deps": []}],
            state="planned", milestone="Beta",
        )
        found = self._problems(self._collect(), "phase-slipping")
        self.assertEqual(len(found), 1)
        item = found[0]
        self.assertEqual(item["id"], "phase-slipping:global:after:Alpha")
        self.assertEqual(item["scope"], "global")
        self.assertEqual(item["severity"], "info")
        self.assertIn("Alpha", item["title"])
        self.assertIn("Beta", item["title"])
        self.assertEqual(item["fix_command"], "python3 agent_dash.py project show")

    def test_phase_slipping_silent_when_next_started(self):
        (self.wsroot / "project.json").write_text(json.dumps({
            "name": "Demo project",
            "phases": [{"name": "Alpha"}, {"name": "Beta"}],
        }), encoding="utf-8")
        self._bundle(
            "WS51", "Alpha One",
            tasks=[{"id": "WS51.1", "title": "First", "status": "done", "deps": []}],
            state="done", state_changed_at=self.now - 10 * 86400, milestone="Alpha",
        )
        self._bundle(
            "WS53", "Beta One",
            tasks=[{"id": "WS53.1", "title": "First", "status": "todo", "deps": []}],
            state="active", milestone="Beta",
            history=[{"ts": self.now - 3600, "task": "WS53.1",
                      "from": "todo", "to": "wip"}],
        )
        self.assertFalse(self._problems(self._collect(), "phase-slipping"))

    def test_phase_slipping_config_override(self):
        (self.wsroot / "project.json").write_text(json.dumps({
            "name": "Demo project",
            "phases": [{"name": "Alpha"}, {"name": "Beta"}],
        }), encoding="utf-8")
        self._bundle(
            "WS51", "Alpha One",
            tasks=[{"id": "WS51.1", "title": "First", "status": "done", "deps": []}],
            state="done", state_changed_at=self.now - 10 * 86400, milestone="Alpha",
        )
        self._bundle(
            "WS53", "Beta One",
            tasks=[{"id": "WS53.1", "title": "First", "status": "todo", "deps": []}],
            state="planned", milestone="Beta",
        )
        self.assertFalse(
            self._problems(self._collect(self._cfg(phase_slip_days=30)), "phase-slipping")
        )

    def test_phase_target_passed_fires(self):
        (self.wsroot / "project.json").write_text(json.dumps({
            "name": "Demo project",
            "phases": [{"name": "Gamma", "target": "2023-01-01",
                        "workstreams": ["WS54"]}],
        }), encoding="utf-8")
        self._bundle(
            "WS54", "Gamma One",
            tasks=[{"id": "WS54.1", "title": "First", "status": "todo", "deps": []}],
            state="active",
            history=[{"ts": self.now - 3600, "task": "WS54.1",
                      "from": "todo", "to": "wip"}],
        )
        found = self._problems(self._collect(), "phase-slipping")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "phase-slipping:global:target:Gamma")
        self.assertIn("Gamma One (WS54)", found[0]["detail"])

    def test_phase_target_future_silent(self):
        (self.wsroot / "project.json").write_text(json.dumps({
            "name": "Demo project",
            "phases": [{"name": "Gamma", "target": "2099-01-01",
                        "workstreams": ["WS54"]}],
        }), encoding="utf-8")
        self._bundle(
            "WS54", "Gamma One",
            tasks=[{"id": "WS54.1", "title": "First", "status": "todo", "deps": []}],
            state="active",
            history=[{"ts": self.now - 3600, "task": "WS54.1",
                      "from": "todo", "to": "wip"}],
        )
        self.assertFalse(self._problems(self._collect(), "phase-slipping"))

    def test_no_lifecycle_data_is_silent(self):
        from dash.bundles import cmd_ws

        self.assertEqual(cmd_ws(["new", "WS41", "North Meadow"]), 0)
        # `ws new` now stamps state and state_changed_at; strip them so the
        # bundle matches a pre-lifecycle one with no explicit lifecycle keys.
        path = self.wsroot / "WS41" / "bundle.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        for key in ("state", "state_changed_at"):
            meta.pop(key, None)
        path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        result = self._collect()
        for code in self.codes:
            self.assertFalse(
                [item for item in result["problems"] if item["code"] == code],
                f"{code} fired without lifecycle data",
            )
            check = next(item for item in result["checks"] if item["name"] == code)
            self.assertTrue(check["ok"])
            self.assertIn("no lifecycle data", check["detail"])

    def test_one_group_per_code_and_scope(self):
        for ws, title in (("WS41", "North Meadow"), ("WS42", "South Field")):
            self._bundle(
                ws, title,
                tasks=[{"id": f"{ws}.1", "title": "First", "status": "done", "deps": []}],
                state="done", state_changed_at=self.now - 8 * 86400,
            )
        found = self._problems(self._collect(), "done-not-archived")
        self.assertEqual(len(found), 2)
        self.assertEqual(
            {item["id"] for item in found},
            {"done-not-archived:ws:WS41", "done-not-archived:ws:WS42"},
        )
        self.assertEqual({item["scope"] for item in found}, {"ws:WS41", "ws:WS42"})


class FleetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.root / "ws")
        (self.root / "ws").mkdir()
        self.store = Store(self.state / "dash.sqlite")
        self.now = 1_700_000_000.0

    def tearDown(self):
        self.store.close()
        if self.old_ws is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old_ws
        self.tmp.cleanup()

    def _collect(self, **over):
        from dash.doctor import collect_problems

        cfg = _cfg(state_dir=str(self.state))
        cfg["roots"]["scratchpads"] = []
        params = {"health": {"ok": True, "sources": []}, "processes": [],
                  "argv_tokens": {}, "ppids": {}, "pgrep_counts": {},
                  "workers_registry": {"workers": {}}}
        params.update(over)
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.root / "noscratch")):
            return collect_problems(self.store, cfg, self.now, **params)

    def test_orphan_reparented(self):
        procs = [{"pid": 9, "comm": "muse", "label": "ws1-old",
                  "started_at": self.now - 3600}]
        result = self._collect(processes=procs, ppids={9: 1})
        found = [item for item in result["problems"] if item["code"] == "orphan-worker"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "orphan-worker:global")
        self.assertIn("ws1-old", found[0]["detail"])
        self.assertNotIn("--model", json.dumps(found[0]))
        self.assertEqual(found[0]["fix_command"], "python3 agent_dash.py doctor --all")

    def test_orphan_row_ended(self):
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws1-old",
                "source": "muse",
                "tier": "muse",
                "label": "ws1-old",
                "session_id": "s",
                "status": "done",
                "started_at": self.now - 7200,
                "ended_at": self.now - 3700,
            }
        )
        procs = [{"pid": 9, "comm": "muse", "label": "ws1-old",
                  "started_at": self.now - 3700}]
        result = self._collect(processes=procs, ppids={9: 4242})
        found = [item for item in result["problems"] if item["code"] == "orphan-worker"]
        self.assertEqual(len(found), 1)

    def test_young_process_is_not_orphan(self):
        procs = [{"pid": 9, "comm": "muse", "label": "ws1-new",
                  "started_at": self.now - 60}]
        result = self._collect(processes=procs, ppids={9: 1})
        self.assertFalse([item for item in result["problems"] if item["code"] == "orphan-worker"])

    def test_model_policy_live(self):
        procs = [
            {"pid": 7, "comm": "muse", "label": "ws1-bad-effort", "started_at": self.now - 60},
            {"pid": 8, "comm": "cursor-agent", "label": "ws1-bare-high", "started_at": self.now - 60},
            {"pid": 9, "comm": "cursor-agent", "label": "ws1-fast-id",
             "started_at": self.now - 60},
            {"pid": 10, "comm": "cursor-agent", "label": "ws1-good", "started_at": self.now - 60},
        ]
        tokens = {
            7: {"model": "muse-spark-1.3-contributor", "reasoning_effort": "low"},
            8: {"model": "grok-4.7-high", "reasoning_effort": None},
            9: {"model": "grok-4.7-high-fast", "reasoning_effort": None},
            10: {"model": "grok-4.7[context=500k,reasoning_effort=xhigh,fast=false]",
                 "reasoning_effort": None},
        }
        result = self._collect(processes=procs, argv_tokens=tokens)
        found = [item for item in result["problems"] if item["code"] == "model-policy"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "model-policy:global")
        self.assertIn("3 workers", found[0]["title"])
        member_ids = {member["id"] for member in found[0]["items"]}
        self.assertEqual(member_ids, {"ws1-bad-effort", "ws1-bare-high", "ws1-fast-id"})
        self.assertIn("ws1-bad-effort", found[0]["detail"])

    def test_model_policy_reason_units(self):
        from dash.doctor import model_policy_reason

        policy = {"muse_reasoning_effort": "xhigh", "grok_require_bracketed_500k": True}
        self.assertIsNone(model_policy_reason("muse", "m", "xhigh", policy))
        self.assertTrue(model_policy_reason("muse", "m", "low", policy))
        self.assertTrue(model_policy_reason("muse", "m", None, policy))
        self.assertIsNone(
            model_policy_reason("cursor", "grok-4.7[context=500k,reasoning_effort=xhigh,fast=false]", None, policy)
        )
        self.assertTrue(model_policy_reason("cursor", "grok-4.7-high", None, policy))
        self.assertTrue(model_policy_reason("cursor", "grok-4.7-high-fast", None, policy))

    def test_over_cap(self):
        registry = {"workers": {"muse": {"cap": 1, "pgrep": "[m]use", "kind": "muse"}}}
        result = self._collect(workers_registry=registry, pgrep_counts={"[m]use": 3})
        found = [item for item in result["problems"] if item["code"] == "over-cap"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "over-cap:global")
        self.assertIn("3", found[0]["detail"])
        self.assertEqual(found[0]["fix_command"], "python3 agent_dash.py doctor --all")
        calm = self._collect(workers_registry=registry, pgrep_counts={"[m]use": 1})
        self.assertFalse([item for item in calm["problems"] if item["code"] == "over-cap"])


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.now = 1_700_000_000.0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _fresh(self, pid, code="stuck-task", severity="warn", scope="ws:WS9", ws="WS9"):
        return {
            "id": pid, "code": code, "severity": severity, "scope": scope, "ws": ws,
            "title": f"title {pid}", "detail": f"detail {pid}",
            "fix_command": "python3 agent_dash.py check", "safe_auto": False,
        }

    def test_resolve_and_regress_cycle(self):
        first = self.store.doctor_snapshot(
            [self._fresh("stuck-task:ws:WS9:WS9.1"),
             self._fresh("unpriced-model:global:m", "unpriced-model", "critical", "global", None)],
            self.now,
        )
        self.assertEqual(first["summary"], {"critical": 1, "warn": 1, "info": 0, "regressed": 0})
        self.assertEqual(first["problems"][0]["code"], "unpriced-model")
        resolved = self.store.doctor_snapshot([self._fresh("unpriced-model:global:m", "unpriced-model", "critical", "global", None)], self.now + 10)
        self.assertEqual(resolved["summary"], {"critical": 1, "warn": 0, "info": 0, "regressed": 0})
        self.assertEqual(len(resolved["resolved_24h"]), 1)
        self.assertEqual(resolved["resolved_24h"][0]["id"], "stuck-task:ws:WS9:WS9.1")
        back = self.store.doctor_snapshot([self._fresh("stuck-task:ws:WS9:WS9.1")], self.now + 20)
        self.assertEqual(back["summary"]["regressed"], 1)
        self.assertTrue(back["problems"][0]["regressed"])
        self.assertEqual(back["problems"][0]["count"], 2)
        self.assertEqual([entry["id"] for entry in back["resolved_24h"]], ["unpriced-model:global:m"])

    def test_resolved_rows_retained_14_days(self):
        self.store.doctor_snapshot([self._fresh("a:global", "reconcile-mismatch", "critical", "global", None)], self.now)
        self.store.doctor_snapshot([], self.now + 10)
        conn = self.store._conn
        conn.execute(
            "UPDATE doctor_problems SET resolved_at=? WHERE id=?", (self.now - 15 * 86400, "a:global")
        )
        conn.commit()
        payload = self.store.doctor_snapshot([], self.now + 20)
        self.assertEqual(payload["resolved_24h"], [])
        rows = conn.execute("SELECT id FROM doctor_problems").fetchall()
        self.assertEqual(rows, [])

    def test_ws_and_severity_filters(self):
        self.store.doctor_snapshot(
            [
                self._fresh("unpriced-model:global:m", "unpriced-model", "critical", "global", None),
                self._fresh("stuck-task:ws:WS9:WS9.1"),
                self._fresh("dashboard-dirty-checkout:global", "dashboard-dirty-checkout", "info", "global", None),
            ],
            self.now,
        )
        ws9 = self.store.doctor_latest(self.now, ws="WS9")
        self.assertEqual({item["id"] for item in ws9["problems"]},
                         {"unpriced-model:global:m", "stuck-task:ws:WS9:WS9.1",
                          "dashboard-dirty-checkout:global"})
        self.assertEqual(ws9["summary"], {"critical": 1, "warn": 1, "info": 1, "regressed": 0})
        crit = self.store.doctor_latest(self.now, min_severity="critical")["problems"]
        self.assertEqual([item["code"] for item in crit], ["unpriced-model"])
        warns = self.store.doctor_latest(self.now, min_severity="warn")["problems"]
        self.assertEqual({item["code"] for item in warns}, {"unpriced-model", "stuck-task"})
        self.assertEqual(self.store.doctor_latest(self.now)["generated_at"], self.now)


class FixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.store = Store(self.state / "dash.sqlite")
        self.cfg = _cfg(state_dir=str(self.state))
        self.cfg["roots"]["scratchpads"] = []

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_dry_run_lists_safe_fixes(self):
        from agent_dash import cmd_doctor_fix

        payload = {
            "problems": [
                {
                    "id": "manifest-outside-roots:global:x", "code": "manifest-outside-roots",
                    "severity": "info", "scope": "global", "ws": None,
                    "title": "t", "detail": "d",
                    "fix": {"command": "add the path", "safe_auto": True, "doc": "docs/problems/x.md"},
                    "first_seen": 1, "last_seen": 1, "count": 1, "regressed": False,
                }
            ]
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cmd_doctor_fix(self.store, self.cfg, payload, False, False)
        self.assertEqual(code, 0)
        self.assertIn("dry run", buf.getvalue())
        self.assertIn("would apply", buf.getvalue())

    def test_apply_registers_run_dir(self):
        from dash.doctor import apply_safe_fixes

        rundir = self.root / "far-away"
        rundir.mkdir()
        (rundir / "muse-sessions.tsv").write_text("label\tsession\tepoch\n", encoding="utf-8")
        problems = [
            {
                "id": "x", "code": "manifest-outside-roots", "severity": "info",
                "scope": "global", "ws": None, "title": "t",
                "detail": f"live worker manifest at {rundir} (muse-sessions.tsv) is outside the scanned roots",
                "fix": {"command": "add the path", "safe_auto": True, "doc": ""},
                "first_seen": 1, "last_seen": 1, "count": 1, "regressed": False,
            }
        ]
        messages = apply_safe_fixes(self.store, self.cfg, problems)
        self.assertTrue(any("registered run dir" in line for line in messages), messages)
        self.assertIn(str(rundir), (self.state / "run-dirs.txt").read_text(encoding="utf-8"))

    def test_apply_adds_scratch_root_with_backup(self):
        from dash.doctor import apply_safe_fixes

        rundir = self.root / "far-away"
        rundir.mkdir()
        (self.state / "run-dirs.txt").write_text(str(rundir) + "\n", encoding="utf-8")
        fake_config = self.root / "config.local.json"
        fake_config.write_text(json.dumps({"roots": {"scratchpads": []}}), encoding="utf-8")
        problems = [
            {
                "id": "x", "code": "run-dir-outside-roots", "severity": "info",
                "scope": "global", "ws": None, "title": "t",
                "detail": f"live worker manifest outside roots.scratchpads: ws1-x in {rundir}",
                "fix": {"command": "add the path", "safe_auto": True, "doc": ""},
                "first_seen": 1, "last_seen": 1, "count": 1, "regressed": False,
            }
        ]
        with mock.patch("dash.doctor._local_config_path", return_value=fake_config):
            messages = apply_safe_fixes(self.store, self.cfg, problems)
        self.assertTrue(any("backup kept" in line for line in messages), messages)
        data = json.loads(fake_config.read_text(encoding="utf-8"))
        self.assertIn(str(rundir), data["roots"]["scratchpads"])
        backups = list(self.root.glob("config.local.json.bak-*"))
        self.assertEqual(len(backups), 1)
        with mock.patch("dash.doctor._local_config_path", return_value=fake_config):
            again = apply_safe_fixes(self.store, self.cfg, problems)
        self.assertTrue(any("already in config.local.json" in line for line in again), again)

    def test_apply_runs_prune(self):
        from dash.doctor import apply_safe_fixes

        victim = self.root / "gone.jsonl"
        victim.write_text("x\n", encoding="utf-8")
        st = victim.stat()
        self.store.set_offset(str(victim), st.st_ino, 1, 1, st.st_mtime)
        victim.unlink()
        messages = apply_safe_fixes(self.store, self.cfg, [])
        self.assertTrue(any("pruned 1 dead offset row" in line for line in messages), messages)


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_run_once_persists_and_latest_serves(self):
        from dash.doctor import DoctorScheduler

        class Registry:
            state = {}

            def health(self, now, cfg=None):
                return {"ok": True, "sources": []}

        sched = DoctorScheduler(self.store, Registry(), _cfg(state_dir=self.tmp.name))
        with mock.patch("dash.doctor._home_scratch", return_value=str(Path(self.tmp.name) / "noscratch")):
            with mock.patch("dash.sources.process.live_worker_processes", return_value=[]):
                payload = sched.run_once(1_700_000_000.0)
        self.assertEqual(payload["generated_at"], 1_700_000_000.0)
        self.assertEqual(payload["interval_s"], 300.0)
        served = sched.latest(1_700_000_100.0)
        self.assertEqual(
            [item["id"] for item in served["problems"]],
            [item["id"] for item in payload["problems"]],
        )
        self.assertEqual(served["next_run_at"], 1_700_000_000.0 + 300.0)

    def test_full_poll_done_matches_starting_definition(self):
        from dash.doctor import DoctorScheduler
        from dash.sources import source_state

        class FakeSource:
            NAME = "muse"
            INTERVAL_S = 2

        class FakeRegistry:
            def __init__(self):
                self.state = {"muse": {"last_poll": None}}

        sched = DoctorScheduler(self.store, FakeRegistry(), _cfg(state_dir=self.tmp.name))
        born = time.time()
        self.assertFalse(sched._full_poll_done())
        self.assertEqual(
            source_state(FakeSource(), {"last_poll": None}, born + 1, born), "starting"
        )
        sched.registry.state["muse"]["last_poll"] = born + 1
        self.assertTrue(sched._full_poll_done())

    def test_background_thread_runs_after_first_delay(self):
        import dash.doctor as doctor_mod

        from dash.doctor import DoctorScheduler

        class Registry:
            state = {}

            def health(self, now, cfg=None):
                return {"ok": True, "sources": []}

        sched = DoctorScheduler(self.store, Registry(), _cfg(state_dir=self.tmp.name))
        old_first, old_wait = doctor_mod.FIRST_RUN_DELAY_S, doctor_mod.FULL_POLL_WAIT_S
        doctor_mod.FIRST_RUN_DELAY_S, doctor_mod.FULL_POLL_WAIT_S = 0.1, 0.2
        try:
            sched.start()
            deadline = time.time() + 15
            while sched.store.doctor_latest(time.time())["generated_at"] is None:
                if time.time() > deadline:
                    break
                time.sleep(0.1)
        finally:
            sched.stop()
            doctor_mod.FIRST_RUN_DELAY_S, doctor_mod.FULL_POLL_WAIT_S = old_first, old_wait
        self.assertIsNotNone(sched.store.doctor_latest(time.time())["generated_at"])


class DoctorReportCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keep = {
            key: os.environ.get(key)
            for key in ("AGENT_DASH_STATE_DIR", "AGENT_DASH_WORKSTREAMS")
        }
        os.environ["AGENT_DASH_STATE_DIR"] = self.tmp.name
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name
        store = Store(Path(self.tmp.name) / "dash.sqlite")
        try:
            store.doctor_snapshot(
                [
                    {
                        "id": "stuck-task:ws:WS9:WS9.1", "code": "stuck-task",
                        "severity": "warn", "scope": "ws:WS9", "ws": "WS9",
                        "title": "Task WS9.1 is stuck", "detail": "no live agent",
                        "fix_command": "python3 agent_dash.py ws plan WS9",
                        "safe_auto": False,
                    }
                ],
                time.time(),
            )
        finally:
            store.close()

    def tearDown(self):
        for key, value in self.keep.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_report_text_and_md(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["doctor", "report", "--since", "24h"]), 0)
        self.assertIn("new: 1", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["doctor", "report", "--since", "24h", "--md"]), 0)
        self.assertIn("## Doctor report", buf.getvalue())
        self.assertIn("### New (1)", buf.getvalue())

    def test_report_rejects_bad_since(self):
        self.assertEqual(main(["doctor", "report", "--since", "never"]), 2)

    def test_fix_dry_run(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["doctor", "--url", "http://127.0.0.1:1", "--fix"]), 0)
        self.assertIn("dry run", buf.getvalue())


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.now = 1_700_000_000.0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _fresh(self, pid, code="stuck-task", severity="warn"):
        return {
            "id": pid, "code": code, "severity": severity, "scope": "ws:WS9", "ws": "WS9",
            "title": f"title {pid}", "detail": f"detail {pid}",
            "fix_command": "python3 agent_dash.py check", "safe_auto": False,
        }

    def test_digest_counts(self):
        from dash.doctor import format_report

        self.store.doctor_snapshot(
            [self._fresh("stuck-task:ws:WS9:WS9.1"), self._fresh("train-stuck:ws:WS9:T1", "train-stuck")],
            self.now,
        )
        self.store.doctor_snapshot([self._fresh("stuck-task:ws:WS9:WS9.1")], self.now + 10)
        report = self.store.doctor_report(self.now - 100, self.now + 10)
        self.assertEqual(report["open_by_severity"], {"critical": 0, "warn": 1, "info": 0})
        self.assertEqual(report["open_by_ws"], {"WS9": 1})
        self.assertEqual(len(report["new"]), 2)
        self.assertEqual(len(report["resolved"]), 1)
        text = format_report(report, md=False)
        self.assertIn("new: 2", text)
        md = format_report(report, md=True)
        self.assertIn("## Doctor report", md)
        self.assertIn("### Resolved (1)", md)
        self.assertIn("### New (2)", md)


class OrchestrationChecksTest(unittest.TestCase):
    """The orchestration study's failure modes, wired into the doctor."""

    NEW_CODES = (
        "idle-ready", "dep-jump", "stale-gate", "deferred-orphan",
        "long-tail", "review-spiral", "ghost-runs", "lead-undelegated",
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wsroot = self.root / "ws"
        self.wsroot.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.wsroot)
        self.store = Store(self.state / "dash.sqlite")
        self.now = 1_700_000_000.0
        self.health = {"ok": True, "sources": []}

    def tearDown(self):
        self.store.close()
        if self.old_ws is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old_ws
        self.tmp.cleanup()

    def _cfg(self, **doctor_over):
        cfg = _cfg(state_dir=str(self.state))
        cfg["roots"]["scratchpads"] = []
        cfg.setdefault("doctor", {}).update(doctor_over)
        return cfg

    def _bundle(self, ws="WS9", tasks=None, trains=None):
        from dash.bundles import cmd_ws

        if not (self.wsroot / ws).is_dir():
            self.assertEqual(cmd_ws(["new", ws, "Example"]), 0)
        plan = self.wsroot / ws / "plan.json"
        data = json.loads(plan.read_text(encoding="utf-8"))
        if tasks is not None:
            data["dag"]["groups"] = [
                {"id": "build", "col": 0, "title": "Build", "tasks": tasks}
            ]
        if trains is not None:
            data["trains"] = trains
        plan.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return self.wsroot / ws

    def _task(self, tid, status="todo", deps=None, **extra):
        row = {"id": tid, "title": f"Task {tid}", "status": status, "deps": deps or []}
        row.update(extra)
        return row

    def _history(self, ws, *rows):
        path = self.wsroot / ws / "history.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for task, ts, frm, to in rows:
                handle.write(json.dumps({"ts": ts, "task": task, "from": frm, "to": to}) + "\n")

    def _backdate_plan(self, ws, age_s):
        old = self.now - age_s
        os.utime(self.wsroot / ws / "plan.json", (old, old))

    def _run(self, label, status="done", age_s=3600, duration_s=600, **over):
        row = {
            "run_id": f"muse:s:{label}",
            "source": "muse",
            "tier": "muse",
            "label": label,
            "session_id": "s",
            "status": status,
            "started_at": self.now - age_s,
        }
        if status in ("done", "killed", "died_early"):
            row["ended_at"] = self.now - age_s + duration_s
        row.update(over)
        self.store.upsert_run(row)
        return row

    def _collect(self, cfg=None, **over):
        from dash.doctor import collect_problems

        params = {"health": self.health, "processes": []}
        params.update(over)
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.root / "noscratch")):
            return collect_problems(self.store, cfg or self._cfg(), self.now, **params)

    def _by_code(self, result, code):
        return [item for item in result["problems"] if item["code"] == code]

    def test_doctor_cfg_orchestration_defaults_and_overrides(self):
        from dash.doctor import doctor_cfg

        cfg = doctor_cfg({})
        self.assertEqual(
            (cfg["idle_ready_minutes"], cfg["long_tail_minutes"], cfg["fix_rounds_cap"],
             cfg["recent_hours"], cfg["ghost_share"]),
            (30.0, 60.0, 2, 24.0, 0.2),
        )
        cfg = doctor_cfg({"doctor": {"idle_ready_minutes": 5, "fix_rounds_cap": 4, "ghost_share": 0.5}})
        self.assertEqual((cfg["idle_ready_minutes"], cfg["fix_rounds_cap"], cfg["ghost_share"]),
                         (5.0, 4, 0.5))
        cfg = doctor_cfg({"doctor": {"idle_ready_minutes": "nope", "fix_rounds_cap": -1, "ghost_share": 9}})
        self.assertEqual((cfg["idle_ready_minutes"], cfg["fix_rounds_cap"], cfg["ghost_share"]),
                         (30.0, 2, 0.2))

    def test_idle_ready_fires_and_names_top_tasks(self):
        self._bundle(tasks=[
            self._task("WS9.0", "todo", kind="gate"),
            self._task("WS9.1", "todo"),
            self._task("WS9.2", "todo", ["WS9.0"]),
        ])
        self._backdate_plan("WS9", 31 * 60)
        result = self._collect()
        found = self._by_code(result, "idle-ready")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "idle-ready:ws:WS9:queue")
        self.assertEqual(found[0]["scope"], "ws:WS9")
        self.assertEqual(found[0]["severity"], "warn")
        self.assertIn("WS9.1", found[0]["detail"])
        self.assertNotIn("WS9.0", found[0]["detail"].split("Top by")[1])
        self.assertEqual(found[0]["fix_command"], "python3 agent_dash.py ws next WS9")

    def test_idle_ready_quiet_when_fresh_two_agents_or_capped(self):
        self._bundle(tasks=[self._task("WS9.1", "todo")])
        result = self._collect()
        self.assertFalse(self._by_code(result, "idle-ready"))
        self._backdate_plan("WS9", 30 * 60)
        edge = self._collect()
        self.assertFalse(self._by_code(edge, "idle-ready"))
        self._run("ws9-WS9.1-a", status="running", age_s=60)
        self._run("ws9-WS9.1-b", status="running", age_s=60)
        self._backdate_plan("WS9", 31 * 60)
        busy = self._collect()
        self.assertFalse(self._by_code(busy, "idle-ready"))

    def test_idle_ready_config_override(self):
        self._bundle(tasks=[self._task("WS9.1", "todo")])
        self._backdate_plan("WS9", 10 * 60)
        quick = self._collect(cfg=self._cfg(idle_ready_minutes=5))
        self.assertEqual(len(self._by_code(quick, "idle-ready")), 1)
        slow = self._collect(cfg=self._cfg(idle_ready_minutes=60))
        self.assertFalse(self._by_code(slow, "idle-ready"))

    def test_dep_jump_warn_recent_and_info_old(self):
        self._bundle(tasks=[
            self._task("WS9.1", "todo"),
            self._task("WS9.2", "wip", ["WS9.1"]),
        ])
        self._history("WS9", ("WS9.2", self.now - 3600, "todo", "wip"))
        result = self._collect()
        found = self._by_code(result, "dep-jump")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "dep-jump:ws:WS9")
        self.assertEqual(found[0]["severity"], "warn")
        self.assertIn("WS9.1", found[0]["detail"])
        self.assertIn("1 task", found[0]["title"])
        self.assertEqual(found[0]["fix_command"], "python3 agent_dash.py ws next WS9")
        (self.wsroot / "WS9" / "history.jsonl").unlink()
        self._history("WS9", ("WS9.2", self.now - 25 * 3600, "todo", "wip"))
        old = self._collect()
        aged = self._by_code(old, "dep-jump")
        self.assertEqual(len(aged), 1)
        self.assertEqual(aged[0]["severity"], "info")

    def test_dep_jump_healthy_order_is_clean(self):
        self._bundle(tasks=[
            self._task("WS9.1", "done"),
            self._task("WS9.2", "wip", ["WS9.1"]),
        ])
        self._history(
            "WS9",
            ("WS9.1", self.now - 7200, "wip", "done"),
            ("WS9.2", self.now - 3600, "todo", "wip"),
        )
        result = self._collect()
        self.assertFalse(self._by_code(result, "dep-jump"))

    def test_stale_gate_fires_and_fresh_does_not(self):
        import datetime

        def iso(age_s):
            moment = datetime.datetime.fromtimestamp(self.now - age_s, tz=datetime.timezone.utc)
            return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

        self._bundle(tasks=[
            self._task("WS9.0", "todo", kind="gate", signal="Freeze lifts",
                       fresh_hours=24, confirmed_at=iso(25 * 3600)),
            self._task("WS9.1", "todo", ["WS9.0"]),
            self._task("WS9.9", "todo", kind="gate", signal="Standing signal"),
        ])
        result = self._collect()
        found = self._by_code(result, "stale-gate")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "stale-gate:ws:WS9:WS9.0")
        self.assertEqual(found[0]["severity"], "warn")
        self.assertIn("Freeze lifts", found[0]["detail"])
        self.assertEqual(found[0]["fix_command"], "python3 agent_dash.py ws gates WS9")

    def test_stale_gate_done_or_fresh_is_clean(self):
        import datetime

        def iso(age_s):
            moment = datetime.datetime.fromtimestamp(self.now - age_s, tz=datetime.timezone.utc)
            return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

        self._bundle(tasks=[
            self._task("WS9.0", "done", kind="gate", signal="Freeze lifts",
                       fresh_hours=24, confirmed_at=iso(25 * 3600)),
            self._task("WS9.1", "todo", kind="gate", signal="Fresh check",
                       fresh_hours=24, confirmed_at=iso(3600)),
        ])
        result = self._collect()
        self.assertFalse(self._by_code(result, "stale-gate"))

    def test_deferred_orphan_fires_and_received_does_not(self):
        self._bundle(tasks=[
            self._task("WS9.1", "done"),
            {"id": "WS9.3", "title": "Third thing", "status": "deferred",
             "deps": [], "moved_to": "WS8", "note": "waits on the freeze"},
        ])
        self._bundle(ws="WS8", tasks=[self._task("WS8.1", "todo")])
        self._run("ws9-WS9.1-x", status="done", age_s=7200, duration_s=600)
        self._history("WS9", ("WS9.3", self.now - 25 * 3600, "wip", "deferred"))
        result = self._collect()
        found = self._by_code(result, "deferred-orphan")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "deferred-orphan:ws:WS9:WS9.3")
        self.assertEqual(found[0]["severity"], "info")
        self.assertIn("WS8", found[0]["detail"])
        self.assertEqual(found[0]["fix_command"], "python3 agent_dash.py ws deferred WS9")

    def test_deferred_recent_or_received_is_clean(self):
        self._bundle(tasks=[
            {"id": "WS9.3", "title": "Third thing", "status": "deferred",
             "deps": [], "moved_to": "WS8", "note": "waits on the freeze",
             "received_as": "WS8.7"},
            self._task("WS9.1", "done"),
        ])
        self._bundle(ws="WS8", tasks=[self._task("WS8.7", "todo")])
        self._run("ws9-WS9.1-x", status="done", age_s=7200, duration_s=600)
        self._history("WS9", ("WS9.3", self.now - 3600, "wip", "deferred"))
        result = self._collect()
        self.assertFalse(self._by_code(result, "deferred-orphan"))

    def test_long_tail_fires_and_edge_is_clean(self):
        self._bundle(tasks=[
            self._task("WS9.1", "done"),
            self._task("WS9.2", "done"),
            self._task("WS9.3", "wip"),
        ])
        self._history(
            "WS9",
            ("WS9.1", self.now - 61 * 60, "wip", "done"),
            ("WS9.2", self.now - 61 * 60, "wip", "done"),
        )
        self._run("ws9-WS9.3-x", status="running", age_s=70 * 60)
        result = self._collect()
        found = self._by_code(result, "long-tail")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "long-tail:ws:WS9:WS9.3")
        self.assertEqual(found[0]["severity"], "info")
        self.assertIn("WS9.3", found[0]["title"])
        self.assertEqual(found[0]["fix_command"], "python3 agent_dash.py ws waves WS9")
        (self.wsroot / "WS9" / "history.jsonl").unlink()
        self._history(
            "WS9",
            ("WS9.1", self.now - 60 * 60, "wip", "done"),
            ("WS9.2", self.now - 60 * 60, "wip", "done"),
        )
        edge = self._collect()
        self.assertFalse(self._by_code(edge, "long-tail"))

    def test_long_tail_quiet_without_a_live_lane(self):
        self._bundle(tasks=[
            self._task("WS9.1", "done"),
            self._task("WS9.2", "done"),
            self._task("WS9.3", "wip"),
        ])
        self._history(
            "WS9",
            ("WS9.1", self.now - 61 * 60, "wip", "done"),
            ("WS9.2", self.now - 61 * 60, "wip", "done"),
        )
        result = self._collect()
        self.assertFalse(self._by_code(result, "long-tail"))

    def test_review_spiral_fires_cap_edge_and_override(self):
        self._bundle(tasks=[self._task("WS9.1", "wip")])
        self._run("ws9-WS9.1-a-fix1", age_s=7200, duration_s=600)
        self._run("ws9-WS9.1-b-fix2", age_s=5400, duration_s=600)
        two = self._collect()
        self.assertFalse(self._by_code(two, "review-spiral"))
        self._run("ws9-WS9.1-c-rev3", age_s=3600, duration_s=600)
        result = self._collect()
        found = self._by_code(result, "review-spiral")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "review-spiral:ws:WS9:WS9.1")
        self.assertEqual(found[0]["severity"], "warn")
        self.assertIn("fix1", found[0]["detail"])
        self.assertEqual(found[0]["fix_command"], "python3 agent_dash.py ws plan WS9")
        capped = self._collect(cfg=self._cfg(fix_rounds_cap=3))
        self.assertFalse(self._by_code(capped, "review-spiral"))

    def test_review_spiral_done_task_is_clean(self):
        self._bundle(tasks=[self._task("WS9.1", "done")])
        self._run("ws9-WS9.1-a-fix1", age_s=7200, duration_s=600)
        self._run("ws9-WS9.1-b-fix2", age_s=5400, duration_s=600)
        self._run("ws9-WS9.1-c-fix3", age_s=3600, duration_s=600)
        result = self._collect()
        self.assertFalse(self._by_code(result, "review-spiral"))

    def test_ghost_runs_fires_at_share_and_config(self):
        self._bundle(tasks=[self._task("WS9.1", "todo")])
        for suffix in ("a", "b", "c", "d"):
            self._run(f"ws9-WS9.1-{suffix}", age_s=3600, duration_s=600)
        self._run("ws9-random-thing", age_s=3600, duration_s=600)
        result = self._collect()
        found = self._by_code(result, "ghost-runs")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "ghost-runs:ws:WS9")
        self.assertEqual(found[0]["severity"], "info")
        self.assertIn("ws9-random-thing", found[0]["detail"])
        raised = self._collect(cfg=self._cfg(ghost_share=0.5))
        self.assertFalse(self._by_code(raised, "ghost-runs"))
        self._run("ws9-WS9.1-e", age_s=3600, duration_s=600)
        sixth = self._collect()
        self.assertFalse(self._by_code(sixth, "ghost-runs"))

    def test_lead_undelegated_fires_and_clears(self):
        self._bundle(tasks=[self._task("WS9.1", "wip")])
        self.store.upsert_session(
            {
                "session_id": "lead-1",
                "tool": "muse",
                "label": "ws9-lead",
                "started_at": self.now - 7200,
                "status": "running",
            }
        )
        self._run("ws9-WS9.1-x", status="running", age_s=3600,
                  session_id="child-1", parent_session_id="lead-1",
                  model="muse-spark-1.3-contributor")
        result = self._collect()
        found = self._by_code(result, "lead-undelegated")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "warn")
        self.assertIn("LOW-DELEGATION", found[0]["detail"])
        self.assertEqual(found[0]["fix_command"], "python3 agent_dash.py lead-profile --ws WS9")
        self._run("ws9-WS9.1-y", status="running", age_s=1800,
                  session_id="child-2", parent_session_id="lead-1",
                  model="muse-spark-1.3-contributor")
        delegating = self._collect()
        self.assertFalse(self._by_code(delegating, "lead-undelegated"))

    def test_lead_context_flag_fires(self):
        self._bundle(tasks=[self._task("WS9.1", "wip")])
        self.store.upsert_session(
            {
                "session_id": "lead-1",
                "tool": "muse",
                "label": "ws9-lead",
                "started_at": self.now - 7200,
                "status": "running",
            }
        )
        self._run("ws9-WS9.1-x", status="running", age_s=3600,
                  session_id="child-1", parent_session_id="lead-1")
        self._run("ws9-WS9.1-y", status="running", age_s=1800,
                  session_id="child-2", parent_session_id="lead-1")
        self._run("ws9-WS9.1-lead", status="done", age_s=7200, duration_s=600,
                  session_id="lead-1", run_id="muse:lead-1:ws9-lead")
        self.store.add_usage(
            {
                "ts": self.now - 600,
                "session_id": "lead-1",
                "run_id": "muse:lead-1:ws9-lead",
                "model": "test-model-xyz",
                "tool": "muse",
                "message_id": "m1",
                "input": 300000,
                "output": 100,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 0,
            }
        )
        result = self._collect()
        found = self._by_code(result, "lead-undelegated")
        self.assertEqual(len(found), 1)
        self.assertIn("CONTEXT>250k", found[0]["detail"])

    def test_lead_quiet_without_a_live_run(self):
        self._bundle(tasks=[self._task("WS9.1", "wip")])
        self.store.upsert_session(
            {
                "session_id": "lead-1",
                "tool": "muse",
                "label": "ws9-lead",
                "started_at": self.now - 7200,
                "status": "running",
            }
        )
        self._run("ws9-WS9.1-x", status="done", age_s=3600, duration_s=600,
                  session_id="child-1", parent_session_id="lead-1")
        result = self._collect()
        self.assertFalse(self._by_code(result, "lead-undelegated"))

    def test_quiet_workstream_fires_nothing_new(self):
        self._bundle(tasks=[
            self._task("WS9.1", "done"),
            self._task("WS9.2", "done", ["WS9.1"]),
        ])
        self._backdate_plan("WS9", 2 * 86400)
        result = self._collect()
        for code in self.NEW_CODES:
            self.assertFalse(
                self._by_code(result, code), f"{code} fired on a quiet workstream",
            )

    def test_empty_plan_fires_nothing_new(self):
        self._bundle(tasks=[])
        self._run("ws9-WS9.1-x", status="done", age_s=3600, duration_s=600)
        result = self._collect()
        for code in self.NEW_CODES:
            self.assertFalse(
                self._by_code(result, code), f"{code} fired with no plan tasks",
            )


class GroupingTest(unittest.TestCase):
    """One problem per (code, workstream); old per-item ids are gone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wsroot = self.root / "ws"
        self.wsroot.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.wsroot)
        self.store = Store(self.state / "dash.sqlite")
        self.now = 1_700_000_000.0
        self.health = {"ok": True, "sources": []}

    def tearDown(self):
        self.store.close()
        if self.old_ws is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old_ws
        self.tmp.cleanup()

    def _cfg(self):
        cfg = _cfg(state_dir=str(self.state))
        cfg["roots"]["scratchpads"] = []
        return cfg

    def _bundle(self, ws="WS9", tasks=None):
        from dash.bundles import cmd_ws

        if not (self.wsroot / ws).is_dir():
            self.assertEqual(cmd_ws(["new", ws, "Example"]), 0)
        plan = self.wsroot / ws / "plan.json"
        data = json.loads(plan.read_text(encoding="utf-8"))
        if tasks is not None:
            data["dag"]["groups"] = [
                {"id": "build", "col": 0, "title": "Build", "tasks": tasks}
            ]
        plan.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return self.wsroot / ws

    def _collect(self, **over):
        from dash.doctor import collect_problems

        params = {"health": self.health, "processes": []}
        params.update(over)
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.root / "noscratch")):
            return collect_problems(self.store, self._cfg(), self.now, **params)

    def test_five_stuck_tasks_are_one_group(self):
        tasks = [
            {"id": f"WS9.{n}", "title": f"Thing {n}", "status": "wip", "deps": []}
            for n in range(1, 6)
        ]
        self._bundle(tasks=tasks)
        history = "\n".join(
            json.dumps({"ts": self.now - 7 * 3600, "task": f"WS9.{n}",
                        "from": "todo", "to": "wip"})
            for n in range(1, 6)
        ) + "\n"
        (self.wsroot / "WS9" / "history.jsonl").write_text(history, encoding="utf-8")
        result = self._collect()
        stuck = [item for item in result["problems"] if item["code"] == "stuck-task"]
        self.assertEqual(len(stuck), 1)
        group = stuck[0]
        self.assertEqual(group["id"], "stuck-task:ws:WS9")
        self.assertIn("Example (WS9)", group["title"])
        self.assertIn("5 tasks", group["title"])
        self.assertNotIn("WS9.1", group["title"])
        for n in (1, 2, 3):
            self.assertIn(f"Thing {n} (WS9.{n})", group["detail"])
        self.assertIn("+2 more", group["detail"])
        self.assertEqual(len(group["items"]), 5)
        for member in group["items"]:
            self.assertEqual(set(member), {"title", "id", "since"})
        self.assertEqual(
            group["fix_command"], "python3 agent_dash.py doctor --ws WS9 --all"
        )

    def test_no_per_item_ids_remain(self):
        tasks = [
            {"id": "WS9.1", "title": "First", "status": "todo", "deps": []},
            {"id": "WS9.2", "title": "Second", "status": "done", "deps": ["WS9.1"]},
            {"id": "WS9.3", "title": "Third", "status": "wip",
             "deps": ["WS9.1"]},
        ]
        self._bundle(tasks=tasks)
        (self.wsroot / "WS9" / "history.jsonl").write_text(
            json.dumps({"ts": self.now - 7 * 3600, "task": "WS9.3",
                        "from": "todo", "to": "wip"}) + "\n",
            encoding="utf-8",
        )
        result = self._collect()
        ids = [item["id"] for item in result["problems"]]
        self.assertNotIn("done-before-deps:ws:WS9:WS9.2", ids)
        self.assertIn("done-before-deps:ws:WS9", ids)
        for ident in ids:
            self.assertLessEqual(ident.count(":"), 2, ident)

    def test_ghost_group_carries_member_rows(self):
        self._bundle(tasks=[{"id": "WS9.1", "title": "Task WS9.1",
                             "status": "todo", "deps": []}])
        for suffix in ("a", "b", "c", "d"):
            self.store.upsert_run(
                {"run_id": f"muse:s:ws9-WS9.1-{suffix}", "source": "muse",
                 "tier": "muse", "label": f"ws9-WS9.1-{suffix}",
                 "session_id": "s", "status": "done",
                 "started_at": self.now - 3600, "ended_at": self.now - 3000}
            )
        self.store.upsert_run(
            {"run_id": "muse:s:ws9-random-thing", "source": "muse",
             "tier": "muse", "label": "ws9-random-thing",
             "session_id": "s", "status": "done",
             "started_at": self.now - 3600, "ended_at": self.now - 3000}
        )
        result = self._collect()
        ghosts = [item for item in result["problems"] if item["code"] == "ghost-runs"]
        self.assertEqual(len(ghosts), 1)
        self.assertEqual(ghosts[0]["id"], "ghost-runs:ws:WS9")
        self.assertTrue(ghosts[0]["items"])
        self.assertTrue(
            any(member["title"] == "ws9-random-thing" for member in ghosts[0]["items"])
        )


class MemberCapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.now = 1_700_000_000.0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_items_cap_at_20_and_all_returns_everything(self):
        members = [
            {"title": f"Thing {n}", "id": f"WS9.{n}", "since": self.now - n}
            for n in range(25)
        ]
        payload = self.store.doctor_snapshot(
            [{"id": "stuck-task:ws:WS9", "code": "stuck-task",
              "severity": "warn", "scope": "ws:WS9", "ws": "WS9",
              "title": "Example (WS9): 25 tasks stuck with no live agent",
              "detail": "many", "fix_command": "python3 agent_dash.py doctor --ws WS9 --all",
              "safe_auto": False, "items": members}],
            self.now,
        )
        self.assertEqual(payload["summary"].get("items_total"), 25)
        group = next(item for item in payload["problems"] if item["id"] == "stuck-task:ws:WS9")
        self.assertEqual(len(group["items"]), 20)
        self.assertEqual(group["items_total"], 25)
        full = self.store.doctor_latest(self.now, include_all=True)
        group_full = next(item for item in full["problems"] if item["id"] == "stuck-task:ws:WS9")
        self.assertEqual(len(group_full["items"]), 25)
        scoped = self.store.doctor_latest(self.now, ws="WS9", include_all=True)
        self.assertEqual(len(scoped["problems"][0]["items"]), 25)


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.now = 1_700_000_000.0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _fresh(self, pid, code="stuck-task", severity="warn", scope="ws:WS9", ws="WS9"):
        return {
            "id": pid, "code": code, "severity": severity, "scope": scope, "ws": ws,
            "title": f"title {pid}", "detail": f"detail {pid}",
            "fix_command": "python3 agent_dash.py check", "safe_auto": False,
        }

    def test_old_per_item_ids_resolve_on_first_grouped_run(self):
        self.store.doctor_snapshot(
            [
                self._fresh("dep-jump:ws:WS9:WS9.2", "dep-jump"),
                self._fresh("stuck-task:ws:WS9:WS9.1"),
                self._fresh("orphan-worker:global:ws1-old", "orphan-worker",
                           "warn", "global", None),
                self._fresh("model-policy:global:ws1-bad", "model-policy",
                           "warn", "global", None),
            ],
            self.now,
        )
        payload = self.store.doctor_snapshot(
            [
                {**self._fresh("dep-jump:ws:WS9", "dep-jump"),
                 "items": [{"title": "Second", "id": "WS9.2", "since": self.now - 10}]},
                {**self._fresh("stuck-task:ws:WS9"),
                 "items": [{"title": "First", "id": "WS9.1", "since": self.now - 10}]},
            ],
            self.now + 10,
        )
        ids = {item["id"] for item in payload["problems"]}
        self.assertIn("dep-jump:ws:WS9", ids)
        self.assertIn("stuck-task:ws:WS9", ids)
        self.assertNotIn("dep-jump:ws:WS9:WS9.2", ids)
        self.assertNotIn("stuck-task:ws:WS9:WS9.1", ids)
        self.assertNotIn("orphan-worker:global:ws1-old", ids)
        resolved = {entry["id"] for entry in payload["resolved_24h"]}
        self.assertIn("dep-jump:ws:WS9:WS9.2", resolved)
        self.assertIn("stuck-task:ws:WS9:WS9.1", resolved)


class DepJumpSeverityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wsroot = self.root / "ws"
        self.wsroot.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.wsroot)
        self.store = Store(self.state / "dash.sqlite")
        self.now = 1_700_000_000.0
        self.health = {"ok": True, "sources": []}

    def tearDown(self):
        self.store.close()
        if self.old_ws is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old_ws
        self.tmp.cleanup()

    def _cfg(self):
        cfg = _cfg(state_dir=str(self.state))
        cfg["roots"]["scratchpads"] = []
        return cfg

    def _bundle(self):
        from dash.bundles import cmd_ws

        self.assertEqual(cmd_ws(["new", "WS9", "Example"]), 0)
        plan = self.wsroot / "WS9" / "plan.json"
        data = json.loads(plan.read_text(encoding="utf-8"))
        data["dag"]["groups"] = [
            {"id": "build", "col": 0, "title": "Build", "tasks": [
                {"id": "WS9.1", "title": "Task WS9.1", "status": "todo", "deps": []},
                {"id": "WS9.2", "title": "Task WS9.2", "status": "wip", "deps": ["WS9.1"]},
                {"id": "WS9.3", "title": "Task WS9.3", "status": "wip", "deps": ["WS9.1"]},
            ]}
        ]
        plan.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _collect(self):
        from dash.doctor import collect_problems

        with mock.patch("dash.doctor._home_scratch", return_value=str(self.root / "noscratch")):
            return collect_problems(
                self.store, self._cfg(), self.now,
                health=self.health, processes=[])

    def test_mixed_recent_and_old_is_warn(self):
        self._bundle()
        path = self.wsroot / "WS9" / "history.jsonl"
        path.write_text(
            json.dumps({"ts": self.now - 3600, "task": "WS9.2",
                        "from": "todo", "to": "wip"}) + "\n"
            + json.dumps({"ts": self.now - 25 * 3600, "task": "WS9.3",
                          "from": "todo", "to": "wip"}) + "\n",
            encoding="utf-8",
        )
        result = self._collect()
        found = [item for item in result["problems"] if item["code"] == "dep-jump"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "warn")
        self.assertEqual(len(found[0]["items"]), 2)

    def test_all_old_is_info(self):
        self._bundle()
        path = self.wsroot / "WS9" / "history.jsonl"
        path.write_text(
            json.dumps({"ts": self.now - 25 * 3600, "task": "WS9.2",
                        "from": "todo", "to": "wip"}) + "\n"
            + json.dumps({"ts": self.now - 26 * 3600, "task": "WS9.3",
                          "from": "todo", "to": "wip"}) + "\n",
            encoding="utf-8",
        )
        result = self._collect()
        found = [item for item in result["problems"] if item["code"] == "dep-jump"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "info")


class DigestTest(unittest.TestCase):
    def _payload(self):
        def problem(pid, code, severity, ws, title, members):
            return {
                "id": pid, "code": code, "severity": severity,
                "scope": f"ws:{ws}" if ws else "global", "ws": ws,
                "title": title, "detail": "d",
                "fix": {"command": "python3 agent_dash.py check",
                        "safe_auto": False, "doc": f"docs/problems/{code}.md"},
                "first_seen": 1, "last_seen": 2, "count": 1,
                "regressed": False,
                "items_total": len(members), "items": members,
            }

        crit_members = [
            {"title": f"C member {n}", "id": f"c{n}", "since": n} for n in range(5)
        ]
        warn_members = [
            {"title": "W member a", "id": "wa", "since": 1},
            {"title": "W member b", "id": "wb", "since": 2},
        ]
        return {
            "generated_at": 10, "next_run_at": 310, "interval_s": 300,
            "summary": {"critical": 1, "warn": 1, "info": 1, "regressed": 0,
                       "items_total": 8},
            "problems": [
                problem("x:global", "dashboard-dirty-checkout", "info", None,
                        "Info group", [{"title": "i", "id": "i", "since": 1}]),
                problem("dep-jump:ws:WS9", "dep-jump", "warn", "WS9",
                        "Example (WS9): 2 tasks started before a dependency finished",
                        warn_members),
                problem("unseen-worker:global:g", "unseen-worker", "critical", None,
                        "Worker is invisible on the dashboard", crit_members),
            ],
            "resolved_24h": [],
        }

    def test_digest_sorts_and_shows_top3_and_footer(self):
        from dash.doctor import format_doctor_human

        text = format_doctor_human(self._payload())
        lines = text.splitlines()
        group_lines = [line for line in lines if line[:1] in ("✖", "⚠", "ℹ")]
        self.assertEqual(len(group_lines), 3)
        self.assertTrue(group_lines[0].startswith("✖ critical"))
        self.assertTrue(group_lines[1].startswith("⚠ warn"))
        self.assertTrue(group_lines[2].startswith("ℹ info"))
        for line in group_lines:
            parts = line.split()
            self.assertGreaterEqual(len(parts), 5)
        self.assertIn("unseen-worker", group_lines[0])
        self.assertIn("x5", group_lines[0])
        member_lines = [line for line in lines if line.startswith("  - ")]
        self.assertEqual(len(member_lines), 3 + 2)
        self.assertIn("(+2 more)", text)
        self.assertIn("3 groups, 8 items", text)
        self.assertIn("doctor --all to expand", text)
        self.assertIn("doctor --ws", text)

    def test_digest_omits_zero_count_and_caps_resolved(self):
        from dash.doctor import format_doctor_human

        payload = self._payload()
        payload["problems"].append(
            {
                "id": "idle-ready:ws:WS9:queue", "code": "idle-ready",
                "severity": "warn", "scope": "ws:WS9", "ws": "WS9",
                "title": "Example (WS9): 1 ready task with no live agent",
                "detail": "d",
                "fix": {"command": "python3 agent_dash.py ws next WS9",
                        "safe_auto": False, "doc": "docs/problems/idle-ready.md"},
                "first_seen": 1, "last_seen": 2, "count": 1,
                "regressed": False,
            }
        )
        payload["resolved_24h"] = [
            {"id": f"old:{n}", "code": "dep-jump", "title": f"old {n}",
             "resolved_at": 3}
            for n in range(25)
        ]
        text = format_doctor_human(payload)
        idle = next(
            line for line in text.splitlines() if "idle-ready" in line
        )
        self.assertNotIn("x0", idle)
        self.assertIn("resolved in the last 24 h (25):", text)
        self.assertIn("(+15 more)", text)

    def test_all_expands_every_member(self):
        from dash.doctor import format_doctor_human

        text = format_doctor_human(self._payload(), show_all=True)
        member_lines = [line for line in text.splitlines() if line.startswith("  - ")]
        self.assertEqual(len(member_lines), 5 + 2 + 1)
        self.assertNotIn("more)", text)


class UnseenGraceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wsroot = self.root / "ws"
        self.wsroot.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.wsroot)
        self.store = Store(self.state / "dash.sqlite")
        self.now = 1_700_000_000.0

    def tearDown(self):
        self.store.close()
        if self.old_ws is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old_ws
        self.tmp.cleanup()

    def _registry(self, last_poll):
        class FakeSource:
            NAME = "muse"
            INTERVAL_S = 2

        class FakeRegistry:
            def __init__(self, born, state):
                self.sources = [FakeSource()]
                self.state = state
                self._born = born

        return FakeRegistry(self.now - 1000, {"muse": {"last_poll": last_poll}})

    def _collect(self, registry):
        from dash.doctor import collect_problems

        cfg = _cfg(state_dir=str(self.state))
        cfg["roots"]["scratchpads"] = []
        procs = [{"pid": 4242, "comm": "cursor-agent", "label": "ws1-ghost",
                  "started_at": self.now - 3600}]
        with mock.patch("dash.doctor._home_scratch", return_value=str(self.root / "noscratch")):
            return collect_problems(
                self.store, cfg, self.now,
                health={"ok": True, "sources": []},
                registry=registry, processes=procs)

    def test_starting_registry_emits_no_unseen_worker(self):
        result = self._collect(self._registry(None))
        self.assertFalse(
            [item for item in result["problems"] if item["code"] == "unseen-worker"]
        )

    def test_first_poll_completed_emits_unseen_worker(self):
        result = self._collect(self._registry(self.now - 10))
        unseen = [item for item in result["problems"] if item["code"] == "unseen-worker"]
        self.assertEqual(len(unseen), 1)
        self.assertIn("ws1-ghost", unseen[0]["detail"])

    def test_health_based_rule_for_check(self):
        from dash.doctor import unseen_startup_ready

        starting = {
            "ok": True,
            "sources": [{"name": "muse", "last_poll": None, "last_error": None,
                         "lag_s": None, "state": "starting",
                         "started_at": self.now - 5, "interval_s": 2}],
        }
        self.assertFalse(unseen_startup_ready(None, starting, self.now))
        ready = {
            "ok": True,
            "sources": [{"name": "muse", "last_poll": self.now - 2, "last_error": None,
                         "lag_s": 2.0, "state": "ok",
                         "started_at": self.now - 1000, "interval_s": 2}],
        }
        self.assertTrue(unseen_startup_ready(None, ready, self.now))
class BuildChecksTest(unittest.TestCase):
    """Safe deploy: build-dirty warns on a dirty running build,
    build-behind is info when the running build is older than
    origin/main. Temp git repos stand in for the tool checkout."""

    def setUp(self):
        import subprocess

        self.subprocess = subprocess
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "tool"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        (self.repo / "file.txt").write_text("one\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "first")
        self.old = self.git("rev-parse", "HEAD")
        (self.repo / "file.txt").write_text("two\n", encoding="utf-8")
        self.git("commit", "-am", "second")
        self.new = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.new)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        proc = self.subprocess.run(
            ["git", "-c", "user.name=agent-dash-test", "-c", "user.email=agent-dash-test@example.invalid",
             "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.stdout.strip()

    def short(self, sha):
        return self.git("rev-parse", "--short", sha)

    def collect(self, health):
        from dash.doctor import collect_problems

        return collect_problems(
            None, default_cfg(), time.time(), url="http://127.0.0.1:1",
            health=health, tool_root=self.repo,
        )

    def test_dirty_build_is_a_warning(self):
        result = self.collect({"ok": True, "sources": [], "build_id": self.short(self.new) + "-dirty"})
        dirty = [item for item in result["problems"] if item["code"] == "build-dirty"]
        self.assertEqual(len(dirty), 1)
        self.assertEqual(dirty[0]["severity"], "warn")
        self.assertIn("python3 agent_dash.py deploy", dirty[0]["fix_command"])
        build = next(item for item in result["checks"] if item["name"] == "build")
        self.assertFalse(build["ok"])

    def test_clean_current_build_is_quiet(self):
        result = self.collect({"ok": True, "sources": [], "build_id": self.short(self.new)})
        self.assertFalse([item for item in result["problems"] if item["code"] in ("build-dirty", "build-behind")])
        build = next(item for item in result["checks"] if item["name"] == "build")
        self.assertTrue(build["ok"])

    def test_older_build_is_behind_but_advisory(self):
        result = self.collect({"ok": True, "sources": [], "build_id": self.short(self.old)})
        behind = [item for item in result["problems"] if item["code"] == "build-behind"]
        self.assertEqual(len(behind), 1)
        self.assertEqual(behind[0]["severity"], "info")
        self.assertIn("python3 agent_dash.py deploy", behind[0]["fix_command"])
        build = next(item for item in result["checks"] if item["name"] == "build")
        self.assertTrue(build["ok"])

    def test_unknown_build_and_down_dashboard_skip(self):
        for health in ({"ok": True, "sources": [], "build_id": "unknown"}, {"ok": True, "sources": []}, None):
            result = self.collect(health)
            self.assertFalse(
                [item for item in result["problems"] if item["code"] in ("build-dirty", "build-behind")],
                health,
            )


class IgnoreListDoctorTest(unittest.TestCase):
    """Synthetic runs for ids with no bundle, behind a temp ignore list."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wsroot = self.root / "ws"
        self.wsroot.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        self.old_root = os.environ.get("AGENT_DASH_DATA_ROOT")
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.wsroot)
        os.environ["AGENT_DASH_DATA_ROOT"] = str(self.root)
        self.store = Store(self.state / "dash.sqlite")
        self.now = 1_700_000_000.0

    def tearDown(self):
        self.store.close()
        if self.old_ws is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old_ws
        if self.old_root is None:
            os.environ.pop("AGENT_DASH_DATA_ROOT", None)
        else:
            os.environ["AGENT_DASH_DATA_ROOT"] = self.old_root
        self.tmp.cleanup()

    def _cfg(self):
        cfg = _cfg(state_dir=str(self.state))
        cfg["roots"]["scratchpads"] = []
        return cfg

    def _ignore(self, entries):
        (self.root / "project.json").write_text(
            json.dumps({"name": "Probe project", "ignore_workstreams": entries}),
            encoding="utf-8",
        )

    def _run(self, ws):
        stamp = self.now - 3600
        run_id = "muse:probe:%s" % ws.lower()
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": "muse",
                "tier": "muse",
                "label": "%s-probe" % ws.lower(),
                "session_id": "s-%s" % ws.lower(),
                "status": "done",
                "started_at": stamp - 60,
                "ended_at": stamp,
            }
        )
        # Pin the attribution directly: label rules only map ws<number>
        # lanes, so lane ids like TOOLS or PLANNING need the explicit row.
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE runs SET workstream=? WHERE run_id=?", (ws, run_id)
            )
            self.store._conn.commit()

    def _collect(self):
        from dash.doctor import collect_problems

        with mock.patch("dash.doctor._home_scratch", return_value=str(self.root / "noscratch")):
            return collect_problems(
                self.store, self._cfg(), self.now,
                health={"ok": True, "sources": []}, processes=[],
            )

    def _problems(self, result, code):
        return [item for item in result["problems"] if item["code"] == code]

    def test_ignored_ids_skip_unplanned(self):
        self._run("WS345")
        self._run("WS9")
        self._ignore(["PLANNING", "TOOLS", "WS0", "WS345"])
        result = self._collect()
        hits = {item["ws"]: item for item in self._problems(result, "unplanned-activity")}
        self.assertEqual(sorted(hits), ["WS9"])
        check = next(item for item in result["checks"] if item["name"] == "unplanned-activity")
        self.assertFalse(check["ok"])
        self.assertIn("1 open", check["detail"])

    def test_prefix_and_case_insensitive(self):
        self._run("WS345")
        self._run("planning")
        self._ignore(["ws*", "Planning"])
        result = self._collect()
        self.assertEqual(self._problems(result, "unplanned-activity"), [])
        check = next(item for item in result["checks"] if item["name"] == "unplanned-activity")
        self.assertTrue(check["ok"])

    def test_unplanned_detail_names_ignore_as_second_option(self):
        self._run("WS9")
        result = self._collect()
        hits = self._problems(result, "unplanned-activity")
        self.assertEqual(len(hits), 1)
        item = hits[0]
        self.assertIn("python3 agent_dash.py ws new WS9", item["detail"])
        self.assertIn("or ignore it: python3 agent_dash.py project ignore add WS9", item["detail"])

    def test_bundled_ignored_id_raises_no_workstream_finding(self):
        from dash.bundles import cmd_ws

        self.assertEqual(cmd_ws(["new", "WS41", "North Meadow"]), 0)
        meta = json.loads((self.wsroot / "WS41" / "bundle.json").read_text(encoding="utf-8"))
        meta["state"] = "done"
        meta["state_changed_at"] = self.now - 8 * 86400
        (self.wsroot / "WS41" / "bundle.json").write_text(json.dumps(meta), encoding="utf-8")
        (self.wsroot / "WS41" / "plan.json").write_text(
            json.dumps({"dag": {"cols": ["Build"], "groups": []}, "narrative": {"phases": []}}),
            encoding="utf-8",
        )
        before = self._problems(self._collect(), "done-not-archived")
        self.assertEqual(len(before), 1)
        self._ignore(["WS41"])
        after = self._collect()
        self.assertFalse([item for item in after["problems"] if item.get("ws") == "WS41"])

    def test_existing_finding_auto_resolves(self):
        self._run("WS345")
        first = self._collect()
        self.assertEqual(len(self._problems(first, "unplanned-activity")), 1)
        self.store.doctor_snapshot(first["problems"], self.now)
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT resolved_at FROM doctor_problems WHERE id=?",
                ("unplanned-activity:ws:WS345",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["resolved_at"])
        self._ignore(["WS345"])
        second = self._collect()
        self.assertEqual(self._problems(second, "unplanned-activity"), [])
        self.store.doctor_snapshot(second["problems"], self.now + 60)
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT resolved_at FROM doctor_problems WHERE id=?",
                ("unplanned-activity:ws:WS345",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["resolved_at"])


if __name__ == "__main__":
    unittest.main()
