"""Multi-project registry and launcher: add/list/remove, ports, probes, launch/stop seams.

Every case points AGENT_DASH_PROJECTS at a temp registry and uses
invented project names in temp data roots. Stub health servers on free
ports stand in for dashboards; the starter/stopper/opener seams are
stubbed so no real server or browser ever launches. Never touches port
8765, the live dashboard, or the real registry.
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))

import http.server
import io
import json
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_dash
from dash import errors, projects
from dash.projects import (
    apply_project_env,
    build_projects_payload,
    cmd_projects,
    default_port,
    entry_health,
    expected_state_hash,
    find_entry,
    load_registry,
    project_env,
    resolve_project,
)

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


def run_cmd(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cmd_projects(argv)
    return code, out.getvalue(), err.getvalue()


class RegistryMixin:
    """A temp AGENT_DASH_PROJECTS per case; restore everything after."""

    def bind_temp_registry(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reg = str(Path(self.tmp.name) / "projects.json")
        self.keep_projects = os.environ.get("AGENT_DASH_PROJECTS")
        os.environ["AGENT_DASH_PROJECTS"] = self.reg
        self.addCleanup(self.restore_registry)
        for key in ("AGENT_DASH_PROJECT_ID", "AGENT_DASH_DATA_ROOT",
                    "AGENT_DASH_STATE_DIR", "AGENT_DASH_URL"):
            if key in os.environ:
                keep = os.environ.pop(key)
                self.addCleanup(os.environ.__setitem__, key, keep)
            else:
                self.addCleanup(os.environ.pop, key, None)
        self.keep_port = os.environ.get("AGENT_DASH_PORT")
        os.environ.pop("AGENT_DASH_PORT", None)
        if self.keep_port is not None:
            self.addCleanup(os.environ.__setitem__, "AGENT_DASH_PORT", self.keep_port)

    def restore_registry(self):
        if self.keep_projects is None:
            os.environ.pop("AGENT_DASH_PROJECTS", None)
        else:
            os.environ["AGENT_DASH_PROJECTS"] = self.keep_projects

    def data_root(self, name="demo-data"):
        root = Path(self.tmp.name) / name
        root.mkdir(parents=True, exist_ok=True)
        return str(root)


class RegistryAddListRemoveTests(unittest.TestCase, RegistryMixin):
    def setUp(self):
        self.bind_temp_registry()

    def test_add_assigns_lowest_free_port(self):
        root = self.data_root("one")
        code, out, _err = run_cmd(["add", "one", "--data-root", root, "--json"])
        self.assertEqual(code, errors.OK)
        payload = json.loads(out)
        self.assertEqual(payload["port"], 8765)
        self.assertEqual(payload["data_root"], "one")
        self.assertEqual(payload["id"], "one")

    def test_second_add_takes_next_port_and_list_json_uses_basename(self):
        one = self.data_root("one")
        two = self.data_root("two")
        self.assertEqual(run_cmd(["add", "one", "--data-root", one])[0], errors.OK)
        self.assertEqual(run_cmd(["add", "two", "--data-root", two])[0], errors.OK)
        code, out, _err = run_cmd(["list", "--json"])
        self.assertEqual(code, errors.OK)
        payload = json.loads(out)
        self.assertEqual([row["port"] for row in payload["projects"]], [8765, 8766])
        for row in payload["projects"]:
            self.assertNotIn("/", row["data_root"])
            self.assertIn("running", row)
            self.assertIn("build_id", row)

    def test_list_table_shows_full_path(self):
        root = self.data_root("one")
        self.assertEqual(run_cmd(["add", "one", "--data-root", root])[0], errors.OK)
        code, out, _err = run_cmd(["list"])
        self.assertEqual(code, errors.OK)
        self.assertIn(root, out)
        self.assertIn("one", out)

    def test_add_refuses_duplicate_ids_and_ports(self):
        one = self.data_root("one")
        two = self.data_root("two")
        self.assertEqual(run_cmd(["add", "one", "--data-root", one])[0], errors.OK)
        code, _out, err = run_cmd(["add", "one", "--data-root", two])
        self.assertEqual(code, errors.USAGE)
        self.assertIn("already registered", err)
        code, _out, err = run_cmd(["add", "two", "--data-root", two, "--port", "8765"])
        self.assertEqual(code, errors.USAGE)
        self.assertIn("already registered", err)

    def test_add_needs_id_data_root_and_good_port(self):
        root = self.data_root("one")
        self.assertEqual(run_cmd(["add"])[0], errors.USAGE)
        self.assertEqual(run_cmd(["add", "one"])[0], errors.USAGE)
        self.assertEqual(run_cmd(["add", "bad id!", "--data-root", root])[0], errors.USAGE)
        self.assertEqual(run_cmd(["add", "one", "--data-root", root, "--port", "nope"])[0], errors.USAGE)

    def test_explicit_port_and_name(self):
        root = self.data_root("one")
        port = free_port()
        code, _out, _err = run_cmd(["add", "one", "--data-root", root, "--name", "One Project", "--port", str(port)])
        self.assertEqual(code, errors.OK)
        registry, _path = load_registry()
        entry = find_entry(registry, "one")
        self.assertEqual(entry["port"], port)
        self.assertEqual(entry["name"], "One Project")

    def test_add_init_writes_project_json(self):
        root = str(Path(self.tmp.name) / "fresh")
        code, _out, _err = run_cmd(["add", "fresh", "--data-root", root, "--init"])
        self.assertEqual(code, errors.OK)
        self.assertTrue((Path(root) / "project.json").is_file())

    def test_remove_is_registry_only(self):
        root = self.data_root("one")
        self.assertEqual(run_cmd(["add", "one", "--data-root", root])[0], errors.OK)
        code, _out, _err = run_cmd(["remove", "one", "--json"])
        self.assertEqual(code, errors.OK)
        registry, _path = load_registry()
        self.assertIsNone(find_entry(registry, "one"))
        self.assertTrue(Path(root).is_dir(), "remove must never touch data")
        code, _out, _err = run_cmd(["remove", "one"])
        self.assertEqual(code, errors.NOT_FOUND)

    def test_unknown_ids_are_not_found(self):
        self.assertEqual(run_cmd(["launch", "nope"])[0], errors.NOT_FOUND)
        self.assertEqual(run_cmd(["stop", "nope"])[0], errors.NOT_FOUND)
        self.assertEqual(run_cmd(["open", "nope"])[0], errors.NOT_FOUND)

    def test_default_port_skips_used(self):
        self.assertEqual(default_port({"projects": []}), 8765)
        self.assertEqual(default_port({"projects": [{"port": 8765}]}), 8766)
        self.assertEqual(
            default_port({"projects": [{"port": 8765}, {"port": 8767}]}), 8766
        )


class HealthProbeTests(unittest.TestCase, RegistryMixin):
    def setUp(self):
        self.bind_temp_registry()
        self.stubs = []
        self.addCleanup(self.close_stubs)

    def close_stubs(self):
        for server, thread in self.stubs:
            stop_stub(server, thread)
        self.stubs = []

    def test_running_only_on_matching_state_hash(self):
        root = self.data_root("one")
        port = free_port()
        self.assertEqual(run_cmd(["add", "one", "--data-root", root, "--port", str(port)])[0], errors.OK)
        registry, _path = load_registry()
        entry = find_entry(registry, "one")
        self.assertFalse(entry_health(entry)["running"])
        server, thread = start_stub({"ok": True, "state_hash": expected_state_hash(entry), "build_id": "abc1234"})
        self.stubs.append((server, thread))
        entry = dict(entry, port=server.server_address[1])
        health = entry_health(entry, projects.probe_health(server.server_address[1]))
        self.assertTrue(health["running"])
        self.assertEqual(health["build_id"], "abc1234")

    def test_other_project_on_port_reads_taken_not_running(self):
        root = self.data_root("one")
        port = free_port()
        self.assertEqual(run_cmd(["add", "one", "--data-root", root, "--port", str(port)])[0], errors.OK)
        registry, _path = load_registry()
        entry = find_entry(registry, "one")
        server, thread = start_stub({"ok": True, "state_hash": "deadbeefcafe", "build_id": "other"})
        self.stubs.append((server, thread))
        entry = dict(entry, port=server.server_address[1])
        health = entry_health(entry, projects.probe_health(server.server_address[1]))
        self.assertFalse(health["running"])
        self.assertTrue(health["port_taken"])
        code, out, _err = run_cmd(["list"])
        self.assertEqual(code, errors.OK)
        self.assertIn("stopped", out)

    def test_list_reports_port_taken_by_another(self):
        root = self.data_root("one")
        server, thread = start_stub({"ok": True, "state_hash": "deadbeefcafe", "build_id": "other"})
        self.stubs.append((server, thread))
        port = server.server_address[1]
        self.assertEqual(run_cmd(["add", "one", "--data-root", root, "--port", str(port)])[0], errors.OK)
        code, out, _err = run_cmd(["list"])
        self.assertEqual(code, errors.OK)
        self.assertIn("port taken by another", out)


class LaunchStopSeamTests(unittest.TestCase, RegistryMixin):
    def setUp(self):
        self.bind_temp_registry()
        self.stubs = []
        self.keep_starter = projects.STARTER
        self.keep_stopper = projects.STOPPER
        self.keep_opener = projects.OPENER
        self.addCleanup(self.restore_seams)
        self.addCleanup(self.close_stubs)

    def restore_seams(self):
        projects.STARTER = self.keep_starter
        projects.STOPPER = self.keep_stopper
        projects.OPENER = self.keep_opener

    def close_stubs(self):
        for server, thread in self.stubs:
            stop_stub(server, thread)
        self.stubs = []

    def test_launch_starts_and_waits_for_health(self):
        root = self.data_root("one")
        server, thread = start_stub({"ok": True, "state_hash": "pending"})
        self.stubs.append((server, thread))
        port = server.server_address[1]
        self.assertEqual(run_cmd(["add", "one", "--data-root", root, "--port", str(port)])[0], errors.OK)
        registry, _path = load_registry()
        entry = find_entry(registry, "one")
        StubHealthHandler.payload = {"ok": True, "state_hash": expected_state_hash(entry), "build_id": "abc1234"}
        calls = []
        projects.STARTER = lambda item: calls.append(dict(item)) or 0
        code, out, _err = run_cmd(["launch", "one", "--json"])
        self.assertEqual(code, errors.OK)
        payload = json.loads(out)
        self.assertTrue(payload["url"].endswith(f":{port}/"))
        self.assertEqual(len(calls), 1)
        env = projects.project_env(calls[0])
        self.assertEqual(env["AGENT_DASH_STATE_DIR"], root)
        self.assertEqual(env["AGENT_DASH_DATA_ROOT"], root)
        self.assertEqual(env["AGENT_DASH_PORT"], str(port))
        self.assertEqual(env["AGENT_DASH_PROJECT_ID"], "one")

    def test_launch_refuses_when_port_serves_another_project(self):
        root = self.data_root("one")
        server, thread = start_stub({"ok": True, "state_hash": "deadbeefcafe"})
        self.stubs.append((server, thread))
        port = server.server_address[1]
        self.assertEqual(run_cmd(["add", "one", "--data-root", root, "--port", str(port)])[0], errors.OK)
        calls = []
        projects.STARTER = lambda item: calls.append(item) or 0
        code, _out, err = run_cmd(["launch", "one"])
        self.assertEqual(code, errors.CHECK_FAILED)
        self.assertIn("already serves another project", err)
        self.assertEqual(calls, [])

    def test_run_agent_dash_passes_project_env_to_child(self):
        """Regression: the starter/stopper child must carry the project's
        data root, state dir, and port, never the default env."""
        captured = {}

        def fake_run(argv, **kwargs):
            captured.update(kwargs)

            class Proc:
                returncode = 0
                stdout = ""
                stderr = ""

            return Proc()

        with mock.patch.object(projects.subprocess, "run", fake_run):
            projects._run_agent_dash(["status"], {"AGENT_DASH_PORT": "8899", "AGENT_DASH_STATE_DIR": "/tmp/probe-a"})
        self.assertEqual(captured["env"]["AGENT_DASH_PORT"], "8899")
        self.assertEqual(captured["env"]["AGENT_DASH_STATE_DIR"], "/tmp/probe-a")

    def test_stop_of_a_does_not_touch_b(self):
        a = self.data_root("a-data")
        b = self.data_root("b-data")
        self.assertEqual(run_cmd(["add", "aaa", "--data-root", a, "--port", str(free_port())])[0], errors.OK)
        self.assertEqual(run_cmd(["add", "bbb", "--data-root", b, "--port", str(free_port())])[0], errors.OK)
        calls = []

        def fake_stopper(entry):
            calls.append(dict(entry))
            return 0, "stopped"

        projects.STOPPER = fake_stopper
        code, _out, _err = run_cmd(["stop", "aaa"])
        self.assertEqual(code, errors.OK)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["id"], "aaa")
        self.assertEqual(calls[0]["data_root"], a)
        self.assertNotIn(b, json.dumps(calls))

    def test_open_reports_url_with_stubbed_opener(self):
        root = self.data_root("one")
        port = free_port()
        self.assertEqual(run_cmd(["add", "one", "--data-root", root, "--port", str(port)])[0], errors.OK)
        seen = []
        projects.OPENER = lambda url: seen.append(url) or 0
        code, out, _err = run_cmd(["open", "one", "--json"])
        self.assertEqual(code, errors.OK)
        self.assertEqual(seen, [f"http://127.0.0.1:{port}/"])
        self.assertEqual(json.loads(out)["url"], seen[0])


class ProjectEnvTests(unittest.TestCase, RegistryMixin):
    def setUp(self):
        self.bind_temp_registry()

    def test_apply_project_env_sets_roots_port_and_url(self):
        root = self.data_root("one")
        port = free_port()
        self.assertEqual(run_cmd(["add", "one", "--data-root", root, "--port", str(port)])[0], errors.OK)
        entry, error = apply_project_env("one")
        self.assertIsNone(error)
        self.assertEqual(os.environ["AGENT_DASH_DATA_ROOT"], root)
        self.assertEqual(os.environ["AGENT_DASH_STATE_DIR"], root)
        self.assertEqual(os.environ["AGENT_DASH_PORT"], str(port))
        self.assertEqual(os.environ["AGENT_DASH_URL"], f"http://127.0.0.1:{port}/")

    def test_resolve_unknown_project(self):
        entry, error = resolve_project("nope")
        self.assertIsNone(entry)
        self.assertIn("project not found", error)

    def test_main_project_flag_routes_status(self):
        root = self.data_root("one")
        port = free_port()
        self.assertEqual(run_cmd(["add", "one", "--data-root", root, "--port", str(port)])[0], errors.OK)
        keep = (agent_dash.STATE_DIR, agent_dash.PIDFILE, agent_dash.LOGFILE, agent_dash.DBFILE, agent_dash.PORT, agent_dash.URL)
        for attr, value in zip(("STATE_DIR", "PIDFILE", "LOGFILE", "DBFILE", "PORT", "URL"), keep):
            self.addCleanup(setattr, agent_dash, attr, value)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = agent_dash.main(["--project", "one", "status", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(os.environ["AGENT_DASH_PORT"], str(port))
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["running"])

    def test_main_project_flag_unknown_id(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = agent_dash.main(["--project", "nope", "status"])
        self.assertEqual(code, errors.NOT_FOUND)
        self.assertIn("project not found", err.getvalue())

    def test_main_project_flag_needs_a_value(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = agent_dash.main(["status", "--project"])
        self.assertEqual(code, errors.USAGE)


class NoCrossReadsTests(unittest.TestCase, RegistryMixin):
    """Building /api/projects never opens another project's data root."""

    def setUp(self):
        self.bind_temp_registry()
        projects.clear_cache()
        self.addCleanup(projects.clear_cache)

    def test_payload_never_opens_other_data_root(self):
        a = self.data_root("a-data")
        b = self.data_root("b-data")
        (Path(a) / "project.json").write_text(json.dumps({"name": "Alpha project"}), encoding="utf-8")
        (Path(b) / "project.json").write_text(json.dumps({"name": "Beta project"}), encoding="utf-8")
        self.assertEqual(run_cmd(["add", "aaa", "--data-root", a, "--port", str(free_port())])[0], errors.OK)
        self.assertEqual(run_cmd(["add", "bbb", "--data-root", b, "--port", str(free_port())])[0], errors.OK)
        opened = []
        real_read_text = Path.read_text

        def recording_read_text(self, *args, **kwargs):
            opened.append(str(self))
            return real_read_text(self, *args, **kwargs)

        with mock.patch.dict(os.environ, {"AGENT_DASH_PROJECT_ID": "aaa", "AGENT_DASH_DATA_ROOT": a}):
            with mock.patch.object(Path, "read_text", recording_read_text):
                payload = build_projects_payload({"port": 8765})
        self.assertEqual({row["id"] for row in payload["projects"]}, {"aaa", "bbb"})
        for row in payload["projects"]:
            self.assertNotIn("data_root", row)
        bad = [path for path in opened if path.startswith(b + os.sep) or path == b]
        self.assertEqual(bad, [], f"opened project B's data root: {bad[:3]}")
        self.assertTrue(
            any(path.endswith("projects.json") for path in opened),
            f"the path wrapper saw no reads at all: {opened[:3]}",
        )

    def test_no_registry_returns_just_this_instance(self):
        projects.clear_cache()
        with mock.patch.dict(os.environ, {"AGENT_DASH_PROJECT_ID": "solo", "AGENT_DASH_DATA_ROOT": self.data_root("solo")}):
            payload = build_projects_payload({"port": 8765})
        self.assertEqual(len(payload["projects"]), 1)
        row = payload["projects"][0]
        self.assertEqual(row["id"], "solo")
        self.assertTrue(row["current"])
        self.assertTrue(row["running"])
        self.assertNotIn("data_root", row)

    def test_payload_cache_holds_ten_seconds(self):
        root = self.data_root("one")
        self.assertEqual(run_cmd(["add", "one", "--data-root", root, "--port", str(free_port())])[0], errors.OK)
        first = build_projects_payload({"port": 8765})
        second = build_projects_payload({"port": 8765})
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
