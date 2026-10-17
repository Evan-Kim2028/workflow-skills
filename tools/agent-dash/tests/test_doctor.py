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
        self.assertEqual(stuck[0]["id"], "stuck-task:ws:WS9:WS9.1")
        self.assertEqual(stuck[0]["scope"], "ws:WS9")
        self.assertEqual(stuck[0]["ws"], "WS9")
        self.assertEqual(stuck[0]["severity"], "warn")
        self.assertIn("WS9.1", stuck[0]["detail"])
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
        self.assertEqual(found[0]["id"], "done-before-deps:ws:WS9:WS9.2")
        self.assertIn("Second (WS9.2)", found[0]["title"])
        self.assertIn("First (WS9.1)", found[0]["title"])
        self.assertIn("WS9.1", found[0]["detail"])

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
        self.assertEqual(found[0]["id"], "orphan-worker:global:ws1-old")
        self.assertIn("ws1-old", found[0]["detail"])
        self.assertNotIn("--model", json.dumps(found[0]))

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
        found = {item["id"]: item for item in result["problems"] if item["code"] == "model-policy"}
        self.assertEqual(
            set(found),
            {"model-policy:global:ws1-bad-effort", "model-policy:global:ws1-bare-high",
             "model-policy:global:ws1-fast-id"},
        )
        self.assertIn("ws1-bad-effort", found["model-policy:global:ws1-bad-effort"]["detail"])

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
        self.assertEqual(found[0]["id"], "over-cap:global:muse")
        self.assertIn("3", found[0]["detail"])
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


if __name__ == "__main__":
    unittest.main()
