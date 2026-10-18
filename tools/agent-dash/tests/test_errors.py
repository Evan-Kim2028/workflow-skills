import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
import http.client
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_dash import default_cfg, main
from dash.bundles import cmd_ws
from dash.server import DashServer
from dash.sources import Registry
from dash.store import Store


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cmd_ws(argv)
    return code, out.getvalue(), err.getvalue()


def _main(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class WsErrorsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old
        self.tmp.cleanup()

    def test_task_not_found_names_plan_command(self):
        code, out, _err = _run(["task", "WS9", "WS9.99", "done"])
        self.assertEqual(code, 3)
        self.assertIn("task WS9.99 not found in WS9", out)
        self.assertIn("Run: python3 agent_dash.py ws plan WS9", out)

    def test_task_bad_status_lists_choices(self):
        code, out, _err = _run(["task", "WS9", "WS9.1", "doing"])
        self.assertEqual(code, 2)
        self.assertIn("unknown status 'doing'", out)
        self.assertIn("Use one of: todo, wip, pr, prog, done, deferred", out)

    def test_task_missing_bundle_names_list(self):
        code, out, _err = _run(["task", "NOPE", "WS9.1", "done"])
        self.assertEqual(code, 3)
        self.assertIn("bundle not found: NOPE", out)
        self.assertIn("Run: python3 agent_dash.py ws list", out)

    def test_train_bad_status_and_missing_member(self):
        code, out, _err = _run(["train", "WS9", "T1", "--title", "x", "--status", "open"])
        self.assertEqual(code, 2)
        self.assertIn("unknown train status 'open'", out)
        self.assertIn("planned, assembling, gate, pr, merged, dropped", out)
        code, out, _err = _run(["train", "WS9", "T1", "--title", "x", "--add", "WS9.99"])
        self.assertEqual(code, 3)
        self.assertIn("task WS9.99 not found in WS9", out)
        self.assertIn("Run: python3 agent_dash.py ws plan WS9", out)

    def test_approve_errors(self):
        code, out, _err = _run(["approve", "NOPE", "item", "pending", "--step", "s", "--reversible", "yes", "--conditions", "c"])
        self.assertEqual(code, 3)
        self.assertIn("Run: python3 agent_dash.py ws list", out)
        code, out, _err = _run(["approve", "WS9", "item", "maybe"])
        self.assertEqual(code, 2)
        self.assertIn("Use one of: pending, approved, denied", out)
        code, out, _err = _run(["approve", "WS9", "item", "pending"])
        self.assertEqual(code, 2)
        self.assertIn("--step", out)
        self.assertIn("python3 agent_dash.py ws approve WS9 item pending", out)

    def test_validate_not_found_and_invalid(self):
        code, out, _err = _run(["validate", "NOPE"])
        self.assertEqual(code, 3)
        self.assertIn("Run: python3 agent_dash.py ws list", out)
        plan = Path(self.tmp.name) / "WS9" / "plan.json"
        data = json.loads(plan.read_text(encoding="utf-8"))
        data["dag"]["groups"][0]["tasks"][0]["status"] = "doing"
        plan.write_text(json.dumps(data), encoding="utf-8")
        code, out, _err = _run(["validate", "WS9"])
        self.assertEqual(code, 4)
        self.assertIn("is not todo, wip, pr, prog, done, deferred", out)
        self.assertIn("python3 agent_dash.py ws validate WS9", out)

    def test_new_duplicate_names_validate(self):
        code, out, _err = _run(["new", "WS9", "Again"])
        self.assertEqual(code, 2)
        self.assertIn("already exists", out)
        self.assertIn("python3 agent_dash.py ws validate WS9", out)


class WsJsonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old
        self.tmp.cleanup()

    def _json(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cmd_ws(argv)
        self.assertEqual(err.getvalue(), "", f"human text leaked to stderr: {err.getvalue()!r}")
        return code, json.loads(out.getvalue())

    def test_validate_json_shapes(self):
        code, payload = self._json(["validate", "WS9", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload, {"ws": "WS9", "ok": True, "issues": [], "warnings": []})
        code, _out, err = _run(["validate", "NOPE", "--json"])
        self.assertEqual(code, 3)
        self.assertIn("ws list", err)

    def test_plan_json_lists_tasks(self):
        code, payload = self._json(["plan", "WS9", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["ws"], "WS9")
        tasks = [task for group in payload["groups"] for task in group["tasks"]]
        self.assertTrue(tasks)
        for task in tasks:
            self.assertIn("id", task)
            self.assertIn("title", task)
            self.assertIn("status", task)
            self.assertIn("deps", task)
        code, _out, err = _run(["plan", "NOPE", "--json"])
        self.assertEqual(code, 3)

    def test_trains_json_lists_members(self):
        self.assertEqual(cmd_ws(["train", "WS9", "T1", "--title", "cutover", "--add", "WS9.1"]), 0)
        code, payload = self._json(["trains", "WS9", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["ws"], "WS9")
        self.assertEqual(payload["trains"][0]["id"], "T1")
        self.assertIn("WS9.1", payload["trains"][0]["members"])

    def test_approvals_json_lists_items(self):
        self.assertEqual(
            cmd_ws(["approve", "WS9", "launch", "pending", "--step", "Ship it", "--reversible", "no", "--conditions", "Tests pass"]),
            0,
        )
        code, payload = self._json(["approvals", "WS9", "--json"])
        self.assertEqual(code, 0)
        items = [item for record in payload["approvals"] for item in record["items"]]
        self.assertEqual(items[0]["id"], "launch")
        self.assertEqual(items[0]["status"], "pending")


class PortAndActionArgsTest(unittest.TestCase):
    def setUp(self):
        import agent_dash

        self.dash = agent_dash
        self.keep_port = agent_dash.PORT
        self.keep_url = agent_dash.URL
        self.keep_env = os.environ.get("AGENT_DASH_PORT")
        self.keep_state = os.environ.get("AGENT_DASH_STATE_DIR")
        self.keep_url_env = os.environ.get("AGENT_DASH_URL")
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["AGENT_DASH_STATE_DIR"] = self.tmp.name
        self.addCleanup(self.restore)

    def restore(self):
        self.dash.PORT = self.keep_port
        self.dash.URL = self.keep_url
        if self.keep_env is None:
            os.environ.pop("AGENT_DASH_PORT", None)
        else:
            os.environ["AGENT_DASH_PORT"] = self.keep_env
        if self.keep_url_env is None:
            os.environ.pop("AGENT_DASH_URL", None)
        else:
            os.environ["AGENT_DASH_URL"] = self.keep_url_env
        if self.keep_state is None:
            os.environ.pop("AGENT_DASH_STATE_DIR", None)
        else:
            os.environ["AGENT_DASH_STATE_DIR"] = self.keep_state
        self.tmp.cleanup()

    def test_run_help_prints_usage_without_starting(self):
        code, out, _err = _main(["run", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("agent-dash", out)
        self.assertFalse(Path(self.tmp.name, "agent-dash.pid").exists())

    def test_run_extra_arg_is_usage_error(self):
        code, _out, err = _main(["run", "--bogus"])
        self.assertEqual(code, 2)
        self.assertIn("run takes no arguments", err)
        self.assertIn("Run: python3 agent_dash.py run", err)

    def test_stop_restart_help_and_extras(self):
        code, out, _err = _main(["stop", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("agent-dash", out)
        code, _out, err = _main(["restart", "--verbose"])
        self.assertEqual(code, 2)
        self.assertIn("restart takes no arguments", err)

    def test_port_env_rebinds_url(self):
        os.environ.pop("AGENT_DASH_URL", None)
        os.environ["AGENT_DASH_PORT"] = "8815"
        code, _out, _err = _main(["status"])
        self.assertEqual(code, 1)  # no server in the temp state dir
        self.assertEqual(self.dash.PORT, 8815)
        self.assertTrue(self.dash.URL.endswith(":8815/"))
        self.assertEqual(os.environ.get("AGENT_DASH_URL"), "http://127.0.0.1:8815/")

    def test_explicit_dash_url_wins_over_port(self):
        os.environ["AGENT_DASH_URL"] = "http://127.0.0.1:9999"
        os.environ["AGENT_DASH_PORT"] = "8815"
        code, _out, _err = _main(["status"])
        self.assertEqual(code, 1)
        self.assertEqual(self.dash.PORT, 8815)
        self.assertEqual(os.environ.get("AGENT_DASH_URL"), "http://127.0.0.1:9999")

    def test_bad_port_is_usage_error(self):
        for raw in ("bogus", "0", "99999", "-1"):
            os.environ["AGENT_DASH_PORT"] = raw
            code, _out, err = _main(["status"])
            self.assertEqual(code, 2, raw)
            self.assertIn("bad port", err, raw)
            self.assertIn("AGENT_DASH_PORT", err, raw)
        self.assertEqual(self.dash.PORT, self.keep_port)

    def test_no_port_env_leaves_port_alone(self):
        os.environ.pop("AGENT_DASH_PORT", None)
        _main(["stop", "--help"])
        self.assertEqual(self.dash.PORT, self.keep_port)
        self.assertEqual(self.dash.URL, self.keep_url)


class UnreachableTest(unittest.TestCase):
    def test_state_and_check_exit_5_with_next_command(self):
        code, _out, err = _main(["state", "--url", "http://127.0.0.1:1"])
        self.assertEqual(code, 5)
        self.assertIn("dashboard not reachable at http://127.0.0.1:1", err)
        self.assertIn("python3 agent_dash.py run", err)
        code, out, err = _main(["check", "--url", "http://127.0.0.1:1", "--json"])
        self.assertEqual(code, 5)
        payload = json.loads(out)
        self.assertEqual(payload["code"], "unreachable")
        self.assertIn("python3 agent_dash.py run", payload["hint"])
        self.assertEqual(err, "")


class ApiErrorsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.server = DashServer(self.store, Registry([]), "127.0.0.1", 0, cfg=default_cfg())
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.stop.set()
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def _get(self, path, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_error_bodies_carry_code_and_hint(self):
        status, body = self._get("/api/plan?ws=NOPE")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "plan not found")
        self.assertIn("code", body)
        self.assertIn("Run:", body["hint"])
        status, body = self._get("/api/bundle?ws=NOPE")
        self.assertEqual(status, 404)
        self.assertIn("code", body)
        self.assertIn("python3 agent_dash.py ws list", body["hint"])
        status, body = self._get("/nope")
        self.assertEqual(status, 404)
        self.assertIn("code", body)
        self.assertIn("hint", body)
        status, body = self._get("/api/snapshot?usage_group=nope")
        self.assertEqual(status, 400)
        self.assertIn("code", body)
        self.assertIn("hint", body)
        status, body = self._get("/", method="POST")
        self.assertEqual(status, 405)
        self.assertIn("agent_dash.py", body["hint"])

    def test_state_endpoint_shape(self):
        status, body = self._get("/api/state")
        self.assertEqual(status, 200)
        for key in ("schema", "version", "now", "window", "health", "workstreams",
                    "unassigned", "running", "totals", "totals_all_time", "reconcile", "problems"):
            self.assertIn(key, body)
        self.assertEqual(body["schema"], 1)


if __name__ == "__main__":
    unittest.main()
