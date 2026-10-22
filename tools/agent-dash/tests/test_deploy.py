"""Safe deploy flow: build id, mismatched-run refusal, clean releases.

Covers build_id on temp git repos (clean/dirty/non-git), run refusal
against a stub health server, and deploy/rollback against temp git
repos with stubbed start/health seams. Never touches port 8765 or the
live state dir: tests rebind the state dir and the port per case.
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))

import http.server
import io
import json
import re
import socket
import subprocess
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
from agent_dash import load_cfg, main
from dash import deploy as deploy_lib
from dash.buildinfo import build_id, build_info
from dash.deploy import DeployError

GIT_IDENTITY = ["-c", "user.name=agent-dash-test", "-c", "user.email=agent-dash-test@example.invalid"]


def git(repo, *args):
    proc = subprocess.run(
        ["git", *GIT_IDENTITY, "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.stdout.strip()


def make_repo(commits=1):
    repo = Path(tempfile.mkdtemp(prefix="dash-deploy-src-"))
    git(repo, "init", "-b", "main")
    for n in range(commits):
        (repo / f"file{n}.txt").write_text(f"content {n}\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", f"commit {n}")
    return repo


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class StubHealthHandler(http.server.BaseHTTPRequestHandler):
    payload = {"ok": True}

    def do_GET(self):
        if self.path == "/api/health" or self.path.startswith("/api/health?"):
            body = json.dumps(type(self).payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        return


def start_stub(payload):
    StubHealthHandler.payload = dict(payload)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubHealthHandler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, thread


def stop_stub(server, thread):
    server.shutdown()
    thread.join(3)
    server.server_close()


class BuildIdTest(unittest.TestCase):
    def test_clean_repo_is_short_sha(self):
        repo = make_repo()
        try:
            expected = git(repo, "rev-parse", "--short", "HEAD")
            self.assertEqual(build_id(repo), expected)
            self.assertTrue(re.fullmatch(r"[0-9a-f]+", build_id(repo)))
        finally:
            subprocess.run(["rm", "-rf", str(repo)], check=False, timeout=30)

    def test_dirty_repo_gets_suffix(self):
        repo = make_repo()
        try:
            clean = build_id(repo)
            (repo / "file0.txt").write_text("changed\n", encoding="utf-8")
            dirty = build_id(repo)
            self.assertEqual(dirty, clean + "-dirty")
            self.assertTrue(dirty.endswith("-dirty"))
        finally:
            subprocess.run(["rm", "-rf", str(repo)], check=False, timeout=30)

    def test_non_git_dir_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(build_id(tmp), "unknown")

    def test_build_info_carries_start_time(self):
        before = time.time()
        info = build_info()
        self.assertIn("build_id", info)
        self.assertGreaterEqual(info["started_at"], before)
        self.assertLessEqual(info["started_at"], time.time())


class CliStateMixin:
    """Rebind state dir + port per case; restore everything after."""

    def bind_temp_state(self):
        self.tmp_state = tempfile.TemporaryDirectory()
        self.keep_env_state = os.environ.get("AGENT_DASH_STATE_DIR")
        self.keep_env_port = os.environ.get("AGENT_DASH_PORT")
        os.environ["AGENT_DASH_STATE_DIR"] = self.tmp_state.name
        os.environ.pop("AGENT_DASH_PORT", None)
        self.keep = (
            agent_dash.STATE_DIR,
            agent_dash.PIDFILE,
            agent_dash.LOGFILE,
            agent_dash.DBFILE,
            agent_dash.PORT,
        )
        agent_dash.PORT = free_port()
        self.addCleanup(self.restore_temp_state)

    def restore_temp_state(self):
        (
            agent_dash.STATE_DIR,
            agent_dash.PIDFILE,
            agent_dash.LOGFILE,
            agent_dash.DBFILE,
            agent_dash.PORT,
        ) = self.keep
        if self.keep_env_state is None:
            os.environ.pop("AGENT_DASH_STATE_DIR", None)
        else:
            os.environ["AGENT_DASH_STATE_DIR"] = self.keep_env_state
        if self.keep_env_port is None:
            os.environ.pop("AGENT_DASH_PORT", None)
        else:
            os.environ["AGENT_DASH_PORT"] = self.keep_env_port
        self.tmp_state.cleanup()


class RunRefusalTest(CliStateMixin, unittest.TestCase):
    def setUp(self):
        self.bind_temp_state()
        self.stub = None

    def tearDown(self):
        if self.stub is not None:
            stop_stub(*self.stub)

    def _serve(self, payload):
        server, thread = start_stub(payload)
        self.stub = (server, thread)
        agent_dash.PORT = server.server_address[1]
        return server

    def test_mismatched_build_refuses_with_both_builds_and_commands(self):
        self._serve({"ok": True, "build_id": "deadbee1"})
        mine = build_info()["build_id"]
        self.assertNotEqual(mine, "deadbee1")
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", err):
            code = main(["run"])
        self.assertEqual(code, 1)
        text = err.getvalue()
        self.assertIn("run refused", text)
        self.assertIn("deadbee1", text)
        self.assertIn(mine, text)
        self.assertIn("python3 agent_dash.py restart", text)
        self.assertIn("python3 agent_dash.py status", text)
        self.assertFalse(agent_dash.PIDFILE.exists())

    def test_same_build_counts_as_running_without_fork(self):
        mine = build_info()["build_id"]
        self._serve({"ok": True, "build_id": mine, "started_at": 1700000000.0})
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["run"])
        self.assertEqual(code, 0)
        self.assertIn("running", out.getvalue())
        self.assertIn(mine, out.getvalue())
        self.assertFalse(agent_dash.PIDFILE.exists())

    def test_force_replace_restarts_on_mismatch(self):
        self._serve({"ok": True, "build_id": "deadbee1"})
        with mock.patch.object(agent_dash, "cmd_restart", return_value=0) as restart:
            code = main(["run", "--force-replace"])
        self.assertEqual(code, 0)
        restart.assert_called_once_with()

    def test_force_replace_restarts_even_with_live_pidfile(self):
        self._serve({"ok": True, "build_id": "deadbee1"})
        with mock.patch.object(agent_dash, "live_pid", return_value=4242), mock.patch.object(
            agent_dash, "cmd_restart", return_value=0
        ) as restart:
            code = main(["run", "--force-replace"])
        self.assertEqual(code, 0)
        restart.assert_called_once_with()

    def test_plain_run_prefers_live_pidfile_without_probing(self):
        with mock.patch.object(agent_dash, "live_pid", return_value=4242), mock.patch.object(
            agent_dash, "_probe_health", side_effect=AssertionError("must not probe")
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["run"])
        self.assertEqual(code, 0)
        self.assertIn("4242", out.getvalue())

    def test_run_rejects_unknown_flags(self):
        with redirect_stdout(io.StringIO()):
            code = main(["run", "--bogus"])
        self.assertEqual(code, 2)

    def test_status_prints_running_build(self):
        self._serve({"ok": True, "build_id": "cafef00d", "started_at": 1700000001.5})
        with mock.patch.object(agent_dash, "live_pid", return_value=4242):
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["status"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("cafef00d", text)
        self.assertIn("1700000001.5", text)

    def test_status_json_carries_build(self):
        self._serve({"ok": True, "build_id": "cafef00d", "started_at": 1700000001.5})
        with mock.patch.object(agent_dash, "live_pid", return_value=4242):
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["status", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["build_id"], "cafef00d")
        self.assertEqual(payload["started_at"], 1700000001.5)

    def test_status_stopped_names_local_build(self):
        with mock.patch.object(agent_dash, "live_pid", return_value=None):
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["status"])
        self.assertEqual(code, 1)
        self.assertIn("stopped", out.getvalue())
        self.assertIn(build_info()["build_id"], out.getvalue())


class YesLiveForwardTest(CliStateMixin, unittest.TestCase):
    """``--yes-live`` reaches the release restarts (state-dir guard)."""

    def setUp(self):
        self.bind_temp_state()

    def test_restart_release_appends_flag(self):
        seen = []
        with mock.patch.object(
            deploy_lib,
            "_run",
            side_effect=lambda argv, cwd, timeout_s, label: seen.append(list(argv)) or (True, ""),
        ):
            deploy_lib.restart_release("/tmp/tool-x", True)
            deploy_lib.restart_release("/tmp/tool-x")
        self.assertIn("--yes-live", seen[0])
        self.assertNotIn("--yes-live", seen[1])

    def test_cmd_deploy_forwards_yes_live(self):
        with mock.patch.object(deploy_lib, "deploy", return_value=(True, "deployed")) as dep:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["deploy", "--yes-live"]), 0)
        self.assertTrue(dep.call_args.kwargs["yes_live"])
        with mock.patch.object(deploy_lib, "deploy", return_value=(True, "deployed")) as dep:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["deploy"]), 0)
        self.assertFalse(dep.call_args.kwargs["yes_live"])

    def test_cmd_deploy_rejects_unknown_flags(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["deploy", "--bogus"]), 2)


class PortOverrideTest(CliStateMixin, unittest.TestCase):
    def setUp(self):
        self.bind_temp_state()

    def test_env_port_wins_and_bad_values_fall_back(self):
        os.environ["AGENT_DASH_PORT"] = "8819"
        try:
            self.assertEqual(agent_dash._port(), 8819)
            self.assertIn("8819", agent_dash._base_url())
        finally:
            os.environ.pop("AGENT_DASH_PORT", None)
        for bad in ("notaport", "0", "99999"):
            os.environ["AGENT_DASH_PORT"] = bad
            try:
                self.assertEqual(agent_dash._port(), agent_dash.PORT)
            finally:
                os.environ.pop("AGENT_DASH_PORT", None)


class DeployLibTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(commits=2)
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.sha1 = git(self.repo, "rev-parse", "HEAD~1")
        self.sha2 = git(self.repo, "rev-parse", "HEAD")
        self.addCleanup(self.cleanup_repo)

    def cleanup_repo(self):
        git(self.repo, "worktree", "prune")
        subprocess.run(["rm", "-rf", str(self.repo)], check=False, timeout=30)
        self.tmp.cleanup()

    def _all_ok(self):
        patches = [
            mock.patch.object(deploy_lib, "run_gate", return_value=(True, "")),
            mock.patch.object(deploy_lib, "restart_release", return_value=(True, "")),
            mock.patch.object(deploy_lib, "wait_healthy", return_value=(True, "")),
            mock.patch.object(deploy_lib, "wait_settled", return_value=(True, "")),
            mock.patch.object(deploy_lib, "run_check", return_value=(True, "")),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_resolve_bad_ref_names_the_fix(self):
        with self.assertRaises(DeployError) as ctx:
            deploy_lib.resolve_sha(self.repo, "no-such-ref")
        self.assertIn("--ref", str(ctx.exception))

    def test_ensure_creates_clean_detached_release(self):
        release, sha, expected = deploy_lib.ensure_release(self.repo, self.sha2, self.state)
        self.assertEqual(sha, self.sha2)
        self.assertEqual(release, deploy_lib.releases_dir(self.state) / self.sha2)
        self.assertEqual(git(release, "rev-parse", "HEAD"), self.sha2)
        self.assertEqual(git(release, "status", "--porcelain"), "")
        self.assertEqual(expected, git(release, "rev-parse", "--short", "HEAD"))
        again, _, _ = deploy_lib.ensure_release(self.repo, self.sha2, self.state)
        self.assertEqual(again, release)

    def test_ensure_uses_origin_ref_and_refuses_dirty(self):
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.sha2)
        release, sha, _ = deploy_lib.ensure_release(self.repo, "origin/main", self.state)
        self.assertEqual(sha, self.sha2)
        (release / "file1.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(DeployError) as ctx:
            deploy_lib.ensure_release(self.repo, "origin/main", self.state)
        self.assertIn("uncommitted", str(ctx.exception))

    def test_ensure_refuses_non_repo_path(self):
        target = deploy_lib.releases_dir(self.state) / self.sha1
        target.mkdir(parents=True)
        (target / "junk.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(DeployError):
            deploy_lib.ensure_release(self.repo, self.sha1, self.state)

    def test_deploy_success_records_current_and_previous(self):
        self._all_ok()
        ok, message = deploy_lib.deploy(self.state, self.repo, ref=self.sha1, url="http://127.0.0.1:1")
        self.assertTrue(ok, message)
        first = deploy_lib.current_release(self.state)
        self.assertIsNotNone(first)
        self.assertIsNone(deploy_lib.previous_release(self.state))
        ok, message = deploy_lib.deploy(self.state, self.repo, ref=self.sha2, url="http://127.0.0.1:1")
        self.assertTrue(ok, message)
        self.assertEqual(deploy_lib.current_release(self.state), deploy_lib.releases_dir(self.state) / self.sha2)
        self.assertEqual(deploy_lib.previous_release(self.state), first)

    def test_wait_healthy_checks_the_release_build(self):
        seen = []
        self._all_ok()
        with mock.patch.object(
            deploy_lib,
            "wait_healthy",
            side_effect=lambda url, build, timeout_s=25.0: seen.append(build) or (True, ""),
        ):
            ok, message = deploy_lib.deploy(self.state, self.repo, ref=self.sha2, url="http://127.0.0.1:1")
        self.assertTrue(ok, message)
        self.assertEqual(seen, [git(self.repo, "rev-parse", "--short", self.sha2)])

    def test_gate_failure_restarts_previous(self):
        self._all_ok()
        self.assertTrue(deploy_lib.deploy(self.state, self.repo, ref=self.sha1, url="http://127.0.0.1:1")[0])
        self.assertTrue(deploy_lib.deploy(self.state, self.repo, ref=self.sha2, url="http://127.0.0.1:1")[0])
        prev = deploy_lib.current_release(self.state)
        restarts = []
        with mock.patch.object(deploy_lib, "run_gate", return_value=(False, "gate boom")), mock.patch.object(
            deploy_lib,
            "restart_release",
            side_effect=lambda tool, yes_live=False: restarts.append((Path(tool), yes_live)) or (True, ""),
        ):
            ok, message = deploy_lib.deploy(self.state, self.repo, ref=self.sha1, url="http://127.0.0.1:1")
        self.assertFalse(ok)
        self.assertIn("gate failed", message)
        self.assertIn("gate boom", message)
        self.assertEqual(deploy_lib.current_release(self.state), prev)
        self.assertIn(deploy_lib.tool_dir_of(prev), [tool for tool, _flag in restarts])
        first = deploy_lib.releases_dir(self.state) / self.sha1
        self.assertEqual(deploy_lib.previous_release(self.state), first)

    def test_unhealthy_release_rolls_back_current(self):
        self._all_ok()
        self.assertTrue(deploy_lib.deploy(self.state, self.repo, ref=self.sha1, url="http://127.0.0.1:1")[0])
        self.assertTrue(deploy_lib.deploy(self.state, self.repo, ref=self.sha2, url="http://127.0.0.1:1")[0])
        second = deploy_lib.releases_dir(self.state) / self.sha2
        with mock.patch.object(
            deploy_lib, "wait_healthy", side_effect=[(False, "wait boom"), (True, "")]
        ):
            ok, message = deploy_lib.deploy(self.state, self.repo, ref=self.sha1, url="http://127.0.0.1:1")
        self.assertFalse(ok)
        self.assertIn("unhealthy", message)
        self.assertEqual(deploy_lib.current_release(self.state), second)
        first = deploy_lib.releases_dir(self.state) / self.sha1
        self.assertEqual(deploy_lib.previous_release(self.state), first)

    def test_rollback_switches_back(self):
        self._all_ok()
        self.assertTrue(deploy_lib.deploy(self.state, self.repo, ref=self.sha1, url="http://127.0.0.1:1")[0])
        self.assertTrue(deploy_lib.deploy(self.state, self.repo, ref=self.sha2, url="http://127.0.0.1:1")[0])
        ok, message = deploy_lib.rollback(self.state, url="http://127.0.0.1:1")
        self.assertTrue(ok, message)
        self.assertEqual(deploy_lib.current_release(self.state), deploy_lib.releases_dir(self.state) / self.sha1)
        self.assertEqual(deploy_lib.previous_release(self.state), deploy_lib.releases_dir(self.state) / self.sha2)

    def test_rollback_without_previous_fails_cleanly(self):
        ok, message = deploy_lib.rollback(self.state)
        self.assertFalse(ok)
        self.assertIn("no previous release", message)
        self.assertIn("deploy", message)

    def test_wait_settled_needs_every_source_polled_once(self):
        pending = {"ok": True, "sources": [{"name": "a", "last_poll": 1.0}, {"name": "b", "last_poll": None}]}
        ready = {"ok": True, "sources": [{"name": "a", "last_poll": 1.0}, {"name": "b", "last_poll": 2.0}]}
        with mock.patch.object(deploy_lib, "fetch_health", side_effect=[pending, ready]):
            ok, _ = deploy_lib.wait_settled("http://127.0.0.1:1", timeout_s=10.0)
        self.assertTrue(ok)
        with mock.patch.object(deploy_lib, "fetch_health", return_value=pending):
            ok, reason = deploy_lib.wait_settled("http://127.0.0.1:1", timeout_s=0.1)
        self.assertFalse(ok)
        self.assertIn("running check anyway", reason)

    def test_gate_prefers_gate_sh_and_falls_back_to_test_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp)
            (tool / "scripts").mkdir()
            (tool / "tests").mkdir()
            (tool / "scripts" / "gate.sh").write_text("exit 3\n", encoding="utf-8")
            calls = []
            with mock.patch.object(
                deploy_lib, "_run",
                side_effect=lambda argv, cwd, timeout_s, label, env=None: calls.append(argv) or (True, ""),
            ):
                ok, _ = deploy_lib.run_gate(tool)
            self.assertTrue(ok)
            self.assertEqual(calls, [["sh", str(tool / "scripts" / "gate.sh")]])
            (tool / "scripts" / "gate.sh").unlink()
            with mock.patch.object(
                deploy_lib, "_run", side_effect=[(True, ""), (False, "server boom")]
            ) as run:
                ok, reason = deploy_lib.run_gate(tool)
            self.assertFalse(ok)
            self.assertIn("server boom", reason)
            self.assertEqual(run.call_count, 2)
            self.assertIn("test_page_rules.py", run.call_args_list[0].args[0][-1])
            self.assertIn("test_server.py", run.call_args_list[1].args[0][-1])
            os.environ["AGENT_DASH_PORT"] = "8812"
            try:
                with mock.patch.object(
                    deploy_lib, "_run", side_effect=[(True, ""), (True, "")]
                ) as run2:
                    ok, _ = deploy_lib.run_gate(tool)
            finally:
                os.environ.pop("AGENT_DASH_PORT", None)
            self.assertTrue(ok)
            for call in run2.call_args_list:
                self.assertNotIn("AGENT_DASH_PORT", call.kwargs["env"])


class StateDirConfigTest(CliStateMixin, unittest.TestCase):
    def setUp(self):
        self.bind_temp_state()

    def test_state_config_wins_over_checkout_and_env_config_wins_over_all(self):
        self.addCleanup(os.environ.__setitem__, "AGENT_DASH_IGNORE_LOCAL", "1")
        os.environ["AGENT_DASH_IGNORE_LOCAL"] = ""
        marker = str(Path(self.tmp_state.name) / "hub")
        (Path(self.tmp_state.name) / "config.local.json").write_text(
            json.dumps({"hub_dir": marker}), encoding="utf-8"
        )
        cfg = load_cfg()
        self.assertEqual(cfg["hub_dir"], marker)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"hub_dir": "/tmp/env-config-wins"}, fh)
            env_path = fh.name
        os.environ["AGENT_DASH_CONFIG"] = env_path
        try:
            self.assertEqual(load_cfg()["hub_dir"], "/tmp/env-config-wins")
        finally:
            os.environ.pop("AGENT_DASH_CONFIG", None)


class PreflightTest(CliStateMixin, unittest.TestCase):
    """restart/deploy preflight: the new build opens a copy of the live
    database before the live server is signalled."""

    def setUp(self):
        self.bind_temp_state()
        agent_dash.bind_state()

    def test_missing_database_is_not_a_failure(self):
        ok, message = agent_dash.preflight_db_open()
        self.assertTrue(ok)
        self.assertIn("no database", message)

    def test_live_copy_opens_with_current_build(self):
        from dash.store import Store

        db = Path(self.tmp_state.name) / "dash.sqlite"
        store = Store(db)
        store.upsert_run({"run_id": "pre-r1", "source": "claude", "status": "done"})
        store.close()
        ok, message = agent_dash.preflight_db_open()
        self.assertTrue(ok, message)
        self.assertIn("opens", message)
        # The copy is deleted; the live database still has its row.
        leftovers = [
            p.name
            for p in Path(self.tmp_state.name).glob("adash-preflight-*")
        ]
        self.assertEqual(leftovers, [])
        check = Store(db)
        try:
            self.assertIsNotNone(
                check._conn.execute(
                    "SELECT run_id FROM runs WHERE run_id='pre-r1'"
                ).fetchone()
            )
        finally:
            check.close()

    def test_previous_schema_copy_opens_after_migration(self):
        fixture = (
            Path(__file__).resolve().parent / "fixtures" / "schema_prev.sql"
        )
        import sqlite3

        db = Path(self.tmp_state.name) / "dash.sqlite"
        conn = sqlite3.connect(str(db))
        conn.executescript(fixture.read_text(encoding="utf-8"))
        conn.commit()
        conn.close()
        ok, message = agent_dash.preflight_db_open()
        self.assertTrue(ok, message)

    def test_unreadable_database_refuses(self):
        db = Path(self.tmp_state.name) / "dash.sqlite"
        db.write_bytes(b"not a sqlite database")
        ok, message = agent_dash.preflight_db_open()
        self.assertFalse(ok)
        self.assertIn("cannot open", message)

    def test_restart_refuses_before_stopping_on_preflight_failure(self):
        db = Path(self.tmp_state.name) / "dash.sqlite"
        db.write_bytes(b"not a sqlite database")
        stopped = []
        real_stop = agent_dash.cmd_stop
        agent_dash.cmd_stop = lambda argv=None: stopped.append(argv) or 0
        try:
            with io.StringIO() as buf, redirect_stdout(buf):
                rc = agent_dash.cmd_restart([])
        finally:
            agent_dash.cmd_stop = real_stop
        self.assertEqual(rc, 1)
        self.assertEqual(stopped, [])


if __name__ == "__main__":
    unittest.main()
