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
        mismatch = [item for item in result["problems"] if item["code"] == "reconcile-mismatch"]
        self.assertTrue(
            any("unseen worker: ws1-ghost" in item["message"] for item in mismatch),
            [item["message"] for item in mismatch],
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
        unhealthy = [item for item in result["problems"] if item["code"] == "dashboard-unhealthy"]
        self.assertEqual(len(unhealthy), 1)
        self.assertIn("mystery-1", unhealthy[0]["message"])

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
        unhealthy = [item for item in result["problems"] if item["code"] == "dashboard-unhealthy"]
        self.assertEqual(len(unhealthy), 1)
        self.assertIn("muse", unhealthy[0]["message"])

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
                          "reconcile", "page-check", "config", "bundles", "checkout", "python"):
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
        dirty = [item for item in result["problems"] if item["code"] == "checkout-dirty"]
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
        self.assertTrue(payload["checks"])


if __name__ == "__main__":
    unittest.main()
