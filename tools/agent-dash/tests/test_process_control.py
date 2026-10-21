"""Process control: pidfile ownership, pidfile-less discovery, state-dir guard.

Stub health servers on free ports stand in for dashboards; short-lived
sleepers with dashboard-shaped argv stand in for server processes.
Never touches port 8765 or the live state dir: every case rebinds the
state dir and the port, and refuses to run on 8765.
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))

import contextlib
import http.server
import io
import json
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_dash
from agent_dash import main
from dash.server import state_dir_hash

LIVE_PORT = 8765


def free_port():
    """A free 127.0.0.1 port that is never the live one."""
    while True:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        if port != LIVE_PORT:
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


class ControlMixin:
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
        assert agent_dash.PORT != LIVE_PORT
        agent_dash.bind_state()
        self.addCleanup(self.restore_temp_state)
        self.procs = []
        self.stub = None

    def restore_temp_state(self):
        for proc in getattr(self, "procs", []):
            try:
                proc.kill()
            except OSError:
                pass
        if getattr(self, "stub", None) is not None:
            stop_stub(*self.stub)
            self.stub = None
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

    def sleeper(self, argv0="agent_dash.py run", seconds=60):
        """A live process whose ps command line looks like the dashboard."""
        proc = subprocess.Popen(
            ["bash", "-c", f"exec -a '{argv0}' sleep {seconds}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        return proc

    def dead_pid(self):
        """A pid that exited (unlikely to be reused inside one case)."""
        proc = subprocess.Popen(
            ["bash", "-c", "exit 0"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        self.procs.append(proc)
        return proc.pid

    def wait_gone(self, pid, timeout_s=10.0):
        """True once ``pid`` has exited (reaping our own children: an
        unreaped zombie still answers ``kill``)."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    return True
            except (ChildProcessError, InterruptedError, OSError):
                pass
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.05)
        return False

    def serve_stub(self, payload):
        server, thread = start_stub(payload)
        self.stub = (server, thread)
        agent_dash.PORT = server.server_address[1]
        assert agent_dash.PORT != LIVE_PORT
        return server

    def current_hash(self):
        return state_dir_hash(agent_dash.STATE_DIR)


class PidfileOwnershipTest(ControlMixin, unittest.TestCase):
    """Only the pidfile owner removes it (outage 1: a restart racing a
    start left the live dashboard running with no pidfile)."""

    def setUp(self):
        self.bind_temp_state()

    def test_old_server_exit_keeps_new_pidfile(self):
        old = self.sleeper()
        agent_dash._write_pidfile(old.pid)
        new = self.sleeper()
        agent_dash._write_pidfile(new.pid)
        old.kill()
        self.assertTrue(self.wait_gone(old.pid))
        # The old server's serve-finally path runs with the old pid.
        agent_dash._release_pidfile(old.pid)
        self.assertEqual(agent_dash.read_pid(), new.pid)

    def test_owner_removes_own_pidfile(self):
        gone = self.dead_pid()
        agent_dash._write_pidfile(gone)
        agent_dash._release_pidfile(gone)
        self.assertFalse(agent_dash.PIDFILE.exists())

    def test_stop_keeps_replaced_pidfile(self):
        """stop reads a dead pid; a new server replaces the entry before
        the unlink. The unlink must not erase the new server."""
        first = self.dead_pid()
        second = self.dead_pid()
        agent_dash._write_pidfile(second)
        calls = {"reads": 0}

        def flapping_read():
            calls["reads"] += 1
            return first if calls["reads"] == 1 else second

        with mock.patch.object(agent_dash, "read_pid", side_effect=flapping_read):
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["stop"])
        self.assertEqual(code, 1)
        self.assertIn("stopped", out.getvalue())
        self.assertEqual(agent_dash.PIDFILE.read_text(encoding="utf-8").strip(), str(second))

    def test_stop_clears_dead_pidfile(self):
        gone = self.dead_pid()
        agent_dash._write_pidfile(gone)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["stop"])
        self.assertEqual(code, 1)
        self.assertIn("stopped", out.getvalue())
        self.assertFalse(agent_dash.PIDFILE.exists())


@unittest.skipUnless(shutil.which("lsof"), "lsof is required for listener discovery")
class ListenerDiscoveryTest(ControlMixin, unittest.TestCase):
    """lsof finds the listener read-only; the command line must name the
    dashboard server (agent_dash.py and run)."""

    def setUp(self):
        self.bind_temp_state()

    def test_listener_pid_finds_stub_process(self):
        server = self.serve_stub({"ok": True})
        self.assertEqual(agent_dash._listener_pid(server.server_address[1]), os.getpid())

    def test_listener_pid_empty_on_free_port(self):
        self.assertIsNone(agent_dash._listener_pid(agent_dash.PORT))

    def test_serves_dashboard_matches_server_argv(self):
        run_proc = self.sleeper("agent_dash.py run")
        plain_proc = self.sleeper("sleep")
        restart_proc = self.sleeper("agent_dash.py restart")
        self.assertTrue(agent_dash._serves_dashboard(run_proc.pid))
        self.assertFalse(agent_dash._serves_dashboard(plain_proc.pid))
        self.assertFalse(agent_dash._serves_dashboard(restart_proc.pid))
        self.assertFalse(agent_dash._serves_dashboard(self.dead_pid()))


class PidfileLessStatusTest(ControlMixin, unittest.TestCase):
    """status finds a pidfile-less dashboard and repairs only that case."""

    def setUp(self):
        self.bind_temp_state()
        if shutil.which("lsof") is None:
            self.skipTest("lsof is required for listener discovery")

    def _dashboard(self, **extra):
        """A stub health payload plus a live dashboard-shaped process."""
        proc = self.sleeper("agent_dash.py run")
        payload = {
            "ok": True,
            "build_id": "cafef00d",
            "started_at": 1700000001.5,
            "state_hash": self.current_hash(),
        }
        payload.update(extra)
        self.serve_stub(payload)
        listener = mock.patch.object(agent_dash, "_listener_pid", return_value=proc.pid)
        listener.start()
        self.addCleanup(listener.stop)
        return proc

    def test_status_reports_pidfile_less_dashboard(self):
        proc = self._dashboard()
        self.assertFalse(agent_dash.PIDFILE.exists())
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["status"])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn(f"running (pidfile missing, pid {proc.pid})", text)
        self.assertIn("status --repair", text)

    def test_status_json_marks_pidfile_missing(self):
        proc = self._dashboard()
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["status", "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["running"])
        self.assertEqual(payload["pid"], proc.pid)
        self.assertTrue(payload["pidfile_missing"])
        self.assertFalse(payload["repaired"])

    def test_status_repair_rewrites_pidfile(self):
        proc = self._dashboard()
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["status", "--repair"])
        self.assertEqual(code, 0)
        self.assertIn(f"pidfile rewritten for pid {proc.pid}", out.getvalue())
        self.assertEqual(agent_dash.read_pid(), proc.pid)
        # A second status goes through the pidfile, not the repair path.
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["status"])
        self.assertEqual(code, 0)
        self.assertNotIn("pidfile missing", out.getvalue())

    def test_status_repair_only_in_that_case(self):
        proc = self.sleeper("agent_dash.py run")
        agent_dash._write_pidfile(proc.pid)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["status", "--repair"])
        self.assertEqual(code, 0)
        self.assertNotIn("rewritten", out.getvalue())
        self.assertEqual(agent_dash.read_pid(), proc.pid)

    def test_status_repair_without_dashboard_writes_nothing(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["status", "--repair"])
        self.assertEqual(code, 1)
        self.assertIn("stopped", out.getvalue())
        self.assertFalse(agent_dash.PIDFILE.exists())


class StateDirGuardTest(ControlMixin, unittest.TestCase):
    """stop/restart refuse a foreign state dir; trials can never signal
    live (outage 2: a worker's test stopped the live dashboard)."""

    def setUp(self):
        self.bind_temp_state()
        if shutil.which("lsof") is None:
            self.skipTest("lsof is required for listener discovery")

    def _dashboard(self, state_hash=None, build_id="cafef00d"):
        """A stub health server plus a live dashboard-shaped process.

        The stub holds the port, so ``stop`` reports "port still bound";
        refusal tests use this (nothing is signaled). Acting tests use
        ``_mocked_dashboard`` so the port is free and ``stop`` can exit 0.
        """
        proc = self.sleeper("agent_dash.py run")
        payload = {"ok": True, "build_id": build_id, "started_at": 1700000001.5}
        if state_hash is not None:
            payload["state_hash"] = state_hash
        self.serve_stub(payload)
        listener = mock.patch.object(agent_dash, "_listener_pid", return_value=proc.pid)
        listener.start()
        self.addCleanup(listener.stop)
        return proc

    def _mocked_dashboard(self, state_hash=None, build_id="cafef00d"):
        """A dashboard found by discovery with nothing bound to the port.

        ``_probe_health`` and ``_listener_pid`` are stubbed, but the
        process, its command-line check, the signal, and the port-free
        check are all real.
        """
        proc = self.sleeper("agent_dash.py run")
        health = {"ok": True, "build_id": build_id, "started_at": 1700000001.5}
        if state_hash is not None:
            health["state_hash"] = state_hash
        probe = mock.patch.object(agent_dash, "_probe_health", return_value=dict(health))
        probe.start()
        self.addCleanup(probe.stop)
        listener = mock.patch.object(agent_dash, "_listener_pid", return_value=proc.pid)
        listener.start()
        self.addCleanup(listener.stop)
        return proc, health

    def _stopping_line(self, pid, state_hash, build_id="cafef00d"):
        return (
            f"stopping dashboard pid {pid} on port {agent_dash.PORT} "
            f"(state {state_hash}, build {build_id})"
        )

    def test_stop_acts_on_port_dashboard_and_announces(self):
        proc, _health = self._mocked_dashboard(state_hash=self.current_hash())
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["stop"])
        self.assertEqual(code, 0)
        self.assertIn(self._stopping_line(proc.pid, self.current_hash()), out.getvalue())
        self.assertTrue(self.wait_gone(proc.pid))

    def test_stop_refuses_foreign_state_dir(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        foreign = state_dir_hash(other.name)
        proc = self._dashboard(state_hash=foreign)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["stop"])
        self.assertEqual(code, 1)
        # Refused before acting: the process lives and nothing was printed.
        try:
            os.kill(proc.pid, 0)
        except OSError:
            self.fail("foreign dashboard was signaled without --yes-live")
        self.assertNotIn("stopping dashboard", out.getvalue())
        refusal = err.getvalue()
        self.assertIn(f"dashboard pid {proc.pid}", refusal)
        self.assertIn(foreign, refusal)
        self.assertIn(self.current_hash(), refusal)
        self.assertIn("--yes-live", refusal)

    def test_stop_yes_live_overrides_foreign_state(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        foreign = state_dir_hash(other.name)
        proc, _health = self._mocked_dashboard(state_hash=foreign)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["stop", "--yes-live"])
        self.assertEqual(code, 0)
        self.assertIn(self._stopping_line(proc.pid, foreign), out.getvalue())
        self.assertTrue(self.wait_gone(proc.pid))

    def test_stop_unknown_state_hash_refuses(self):
        proc = self._dashboard()
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["stop"])
        self.assertEqual(code, 1)
        try:
            os.kill(proc.pid, 0)
        except OSError:
            self.fail("dashboard with unknown state was signaled without --yes-live")
        proc.kill()
        self.assertTrue(self.wait_gone(proc.pid))
        # Part one's stub still holds the old port: move to a free one so
        # the override stop can verify the port is free afterwards.
        agent_dash.PORT = free_port()
        proc, _health = self._mocked_dashboard()
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["stop", "--yes-live"])
        self.assertEqual(code, 0)
        self.assertIn(
            f"stopping dashboard pid {proc.pid} on port {agent_dash.PORT} (state unknown, build cafef00d)",
            out.getvalue(),
        )
        self.assertTrue(self.wait_gone(proc.pid))

    def test_restart_refuses_foreign_state_dir(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        proc = self._dashboard(state_hash=state_dir_hash(other.name))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["restart"])
        self.assertEqual(code, 1)
        self.assertIn("restart failed: old server still running", err.getvalue())
        self.assertIn("--yes-live", err.getvalue())
        try:
            os.kill(proc.pid, 0)
        except OSError:
            self.fail("foreign dashboard was signaled by restart without --yes-live")

    def test_restart_finds_port_dashboard(self):
        # The forked child keeps the test runner's argv, so its ps line
        # never names agent_dash.py: like StopRestartTest, tests call the
        # child the dashboard directly.
        dash_mock = mock.patch.object(agent_dash, "is_dash", return_value=True)
        dash_mock.start()
        self.addCleanup(dash_mock.stop)
        proc = self.sleeper("agent_dash.py run")
        health = {
            "ok": True,
            "build_id": "cafef00d",
            "started_at": 1700000001.5,
            "state_hash": self.current_hash(),
        }
        self.assertFalse(agent_dash.PIDFILE.exists())
        try:
            with mock.patch.object(
                agent_dash, "_dashboard_on_port", side_effect=[(proc.pid, health), (None, None)]
            ):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = main(["restart"])
            self.assertEqual(code, 0)
            self.assertIn("restarted", out.getvalue())
            self.assertTrue(self.wait_gone(proc.pid))
            # The restarted server wrote a fresh pidfile for its own pid.
            fresh = agent_dash.read_pid()
            self.assertIsNotNone(fresh)
            self.assertNotEqual(fresh, proc.pid)
        finally:
            with redirect_stdout(io.StringIO()):
                main(["stop"])
        self.assertTrue(agent_dash._port_free())

    def test_default_state_noninteractive_needs_flag(self):
        keep_env = os.environ.pop("AGENT_DASH_STATE_DIR", None)

        def restore_state_env():
            if keep_env is None:
                os.environ.pop("AGENT_DASH_STATE_DIR", None)
            else:
                os.environ["AGENT_DASH_STATE_DIR"] = keep_env

        self.addCleanup(restore_state_env)
        with mock.patch.object(agent_dash, "bind_state", lambda *args, **kwargs: None):
            with mock.patch.object(agent_dash, "_stdin_is_tty", return_value=False):
                proc = self.sleeper("agent_dash.py run")
                agent_dash._write_pidfile(proc.pid)
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = main(["stop"])
                self.assertEqual(code, 1)
                self.assertIn(
                    "Never run stop or restart in tests without AGENT_DASH_STATE_DIR and AGENT_DASH_PORT.",
                    err.getvalue(),
                )
                try:
                    os.kill(proc.pid, 0)
                except OSError:
                    self.fail("live dashboard was signaled without --yes-live")
                out = io.StringIO()
                with redirect_stdout(out):
                    code = main(["stop", "--yes-live"])
                self.assertEqual(code, 0)
                self.assertTrue(self.wait_gone(proc.pid))
            with mock.patch.object(agent_dash, "_stdin_is_tty", return_value=True):
                proc = self.sleeper("agent_dash.py run")
                agent_dash._write_pidfile(proc.pid)
                with redirect_stdout(io.StringIO()):
                    code = main(["stop"])
                self.assertEqual(code, 0)
                self.assertTrue(self.wait_gone(proc.pid))


class WaitForChildTest(unittest.TestCase):
    """``_wait_for_child`` against real forked children that write, skip or never write the pidfile."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pidfile = Path(self.tmp.name) / "agent-dash.pid"
        self.children: list[int] = []
        patches = [
            mock.patch.object(agent_dash, "PIDFILE", self.pidfile),
            mock.patch.object(agent_dash, "LOGFILE", Path(self.tmp.name) / "agent-dash.log"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        for pid in self.children:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(pid, 0)
        self.tmp.cleanup()

    def _fork(self, delay_s: float, write_pidfile: bool = True, exit_code: int | None = None) -> int:
        pid = os.fork()
        if pid == 0:
            try:
                if exit_code is not None:
                    os._exit(exit_code)
                time.sleep(delay_s)
                if write_pidfile:
                    self.pidfile.write_text(f"{os.getpid()}\n", encoding="utf-8")
                time.sleep(60)
            finally:
                os._exit(0)
        self.children.append(pid)
        return pid

    def _wait(self, child: int) -> tuple[int, str]:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = agent_dash._wait_for_child(child)
        return code, err.getvalue()

    def test_child_that_records_its_pidfile_late_is_reported_running(self):
        child = self._fork(delay_s=4.0)
        with mock.patch.object(agent_dash, "CHILD_START_S", 15.0):
            code, err = self._wait(child)
        self.assertEqual(code, 0, err)
        self.assertEqual(agent_dash.read_pid(), child)

    def test_child_that_exits_before_recording_fails_at_once(self):
        child = self._fork(delay_s=0.0, exit_code=1)
        started = time.time()
        with mock.patch.object(agent_dash, "CHILD_START_S", 15.0):
            code, err = self._wait(child)
        self.assertEqual(code, 1)
        self.assertIn("failed to start", err)
        self.assertIn("exited", err)
        self.assertLess(time.time() - started, 5.0)

    def test_child_that_never_records_fails_after_the_start_bound(self):
        child = self._fork(delay_s=0.0, write_pidfile=False)
        with mock.patch.object(agent_dash, "CHILD_START_S", 1.0):
            code, err = self._wait(child)
        self.assertEqual(code, 1)
        self.assertIn("no pidfile after 1 s", err)

    def test_default_start_bound_is_longer_than_the_old_three_seconds(self):
        self.assertGreaterEqual(agent_dash.CHILD_START_S, 20.0)


if __name__ == "__main__":
    unittest.main()
