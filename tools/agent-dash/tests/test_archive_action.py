"""Archive/restore from the page: POST /api/workstreams/archive|restore.

Spins the real server over a temp bundle root holding synthetic done
bundles, then drives the endpoints the page uses: the archive-then-
restore round trip (frozen summary present, `state=archived` lists it on
the very next GET), the fail-closed guards (cross-site Origin, missing
action header, read-only switch), the house error shape (unknown 404,
program kind and double archive 409, bad bodies 400), atomic writes
(no temp files left), and the privacy pin (the closing-report body
never reaches any payload).
"""

import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import http.client
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_dash import default_cfg  # noqa: E402
from dash import actions as actions_mod  # noqa: E402
from dash import errors  # noqa: E402
from dash import lifecycle  # noqa: E402
from dash.bundles import cmd_ws  # noqa: E402
from dash.server import DashServer  # noqa: E402
from dash.sources import Registry  # noqa: E402
from dash.store import Store  # noqa: E402

REPORT_SENTINEL = "ARCHIVE_PIN_REPORT_BODY_sentinel_3d9a51"


def _finish_plan(root: Path, ws: str) -> None:
    plan_path = root / ws / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    for group in plan["dag"]["groups"]:
        for task in group["tasks"]:
            task["status"] = "done"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")


def _closing_report(root: Path, ws: str, body: str = "The sample lane shipped.") -> None:
    docs = root / ws / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "closing-report.md").write_text(f"# Closing report\n\n{body}\n", encoding="utf-8")


def _done_bundle(root: Path, ws: str, title: str) -> None:
    assert cmd_ws(["new", ws, title]) == 0
    _finish_plan(root, ws)
    _closing_report(root, ws, f"{title} shipped. {REPORT_SENTINEL}")
    assert cmd_ws(["state", ws, "done"]) == 0


class ArchiveActionTest(unittest.TestCase):
    def setUp(self):
        self.old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        self.old_ro = os.environ.get("AGENT_DASH_READONLY")
        self.bundles = tempfile.TemporaryDirectory()
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.bundles.name
        os.environ.pop("AGENT_DASH_READONLY", None)
        self.root = Path(self.bundles.name)
        self.state = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.state.name) / "dash.sqlite")
        self.server = DashServer(
            self.store, Registry([]), "127.0.0.1", 0, cfg=default_cfg()
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.stop.set()
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.store.close()
        self.bundles.cleanup()
        self.state.cleanup()
        if self.old_ws is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old_ws
        if self.old_ro is None:
            os.environ.pop("AGENT_DASH_READONLY", None)
        else:
            os.environ["AGENT_DASH_READONLY"] = self.old_ro

    def _post(self, path, body, origin=None, action_header=True, host=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            headers = {"Content-Type": "application/json"}
            if action_header:
                headers["X-AgentDash-Action"] = "1"
            if origin is not None:
                headers["Origin"] = origin
            if host is not None:
                headers["Host"] = host
            payload = raw if raw is not None else json.dumps(body).encode()
            conn.request("POST", path, body=payload, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()

    def test_round_trip_then_restore(self):
        _done_bundle(self.root, "WS1", "Alpha work")
        seq_before = self.store.seq
        status, body = self._post(
            "/api/workstreams/archive", {"ws": "WS1"}, origin=self.origin
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(
            body, {"ok": True, "ws": "WS1", "state": "archived", "archived_at": body["archived_at"]}
        )
        self.assertIsInstance(body["archived_at"], (int, float))
        summary = json.loads((self.root / "WS1" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["ws"], "WS1")
        self.assertEqual(summary["state"], "archived")
        self.assertGreater(self.store.seq, seq_before)
        status, listed = self._get("/api/workstreams?state=archived")
        self.assertEqual(status, 200)
        self.assertIn("WS1", [row["ws"] for row in listed["workstreams"]])
        status, current = self._get("/api/workstreams")
        self.assertEqual(status, 200)
        self.assertNotIn("WS1", [row["ws"] for row in current["workstreams"]])
        status, body = self._post(
            "/api/workstreams/restore", {"ws": "WS1"}, origin=self.origin
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "done")
        self.assertIsNone(body["archived_at"])
        meta = json.loads((self.root / "WS1" / "bundle.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["state"], "done")
        self.assertIsNone(meta["archived_at"])
        self.assertTrue((self.root / "WS1" / "summary.json").is_file())

    def test_second_restore_is_409(self):
        _done_bundle(self.root, "WS1", "Alpha work")
        self.assertEqual(
            self._post("/api/workstreams/archive", {"ws": "WS1"}, origin=self.origin)[0], 200
        )
        self.assertEqual(
            self._post("/api/workstreams/restore", {"ws": "WS1"}, origin=self.origin)[0], 200
        )
        status, body = self._post(
            "/api/workstreams/restore", {"ws": "WS1"}, origin=self.origin
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "not_eligible")

    def test_double_archive_is_409_not_a_crash(self):
        _done_bundle(self.root, "WS1", "Alpha work")
        self.assertEqual(
            self._post("/api/workstreams/archive", {"ws": "WS1"}, origin=self.origin)[0], 200
        )
        status, body = self._post(
            "/api/workstreams/archive", {"ws": "WS1"}, origin=self.origin
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "not_eligible")
        self.assertIn("hint", body)
        meta = json.loads((self.root / "WS1" / "bundle.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["state"], "archived")

    def test_unknown_ws_is_404(self):
        for path in ("/api/workstreams/archive", "/api/workstreams/restore"):
            status, body = self._post(path, {"ws": "WS9"}, origin=self.origin)
            self.assertEqual(status, 404, body)
            self.assertEqual(body["code"], "not_found")
            self.assertIn("hint", body)

    def test_program_kind_is_409(self):
        prog = self.root / "PROG1"
        prog.mkdir()
        (prog / "bundle.json").write_text(
            json.dumps({"id": "PROG1", "title": "Program lane", "kind": "program"}),
            encoding="utf-8",
        )
        status, body = self._post(
            "/api/workstreams/archive", {"ws": "PROG1"}, origin=self.origin
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(body["code"], "not_eligible")

    def test_overview_never_archives(self):
        over = self.root / "OVERVIEW"
        over.mkdir()
        (over / "bundle.json").write_text(
            json.dumps({"id": "OVERVIEW", "title": "Overview", "kind": "program"}),
            encoding="utf-8",
        )
        status, body = self._post(
            "/api/workstreams/archive", {"ws": "OVERVIEW"}, origin=self.origin
        )
        self.assertEqual(status, 409, body)

    def test_unfinished_plan_is_409(self):
        assert cmd_ws(["new", "WS4", "Half done"]) == 0
        _closing_report(self.root, "WS4")
        status, body = self._post(
            "/api/workstreams/archive", {"ws": "WS4"}, origin=self.origin
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(body["code"], "not_eligible")

    def test_bad_bodies_are_400(self):
        for raw in (b"{}", b'{"ws": ""}', b"not json", b"[1]", b'{"ws": "WS1", "note": "x"}'):
            status, body = self._post(
                "/api/workstreams/archive", None, origin=self.origin, raw=raw
            )
            self.assertEqual(status, 400, (raw, body))
            self.assertEqual(body["code"], "bad_request")

    def test_empty_body_is_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(
                "POST",
                "/api/workstreams/archive",
                body=b"",
                headers={"Content-Type": "application/json", "X-AgentDash-Action": "1", "Origin": self.origin},
            )
            response = conn.getresponse()
            status, body = response.status, json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()
        self.assertEqual(status, 400, body)
        self.assertEqual(body["code"], "bad_request")

    def test_bad_ws_shape_is_400(self):
        status, body = self._post(
            "/api/workstreams/archive", {"ws": "../x"}, origin=self.origin
        )
        self.assertEqual(status, 400, body)

    def test_cross_site_origin_is_403(self):
        _done_bundle(self.root, "WS1", "Alpha work")
        status, body = self._post(
            "/api/workstreams/archive", {"ws": "WS1"}, origin="http://evil.example"
        )
        self.assertEqual(status, 403, body)
        self.assertEqual(body["code"], "forbidden")
        meta = json.loads((self.root / "WS1" / "bundle.json").read_text(encoding="utf-8"))
        self.assertNotEqual(meta.get("state"), "archived")

    def test_foreign_host_is_403(self):
        _done_bundle(self.root, "WS1", "Alpha work")
        status, body = self._post(
            "/api/workstreams/archive", {"ws": "WS1"}, origin=self.origin, host="evil.example"
        )
        self.assertEqual(status, 403, body)

    def test_missing_header_is_403(self):
        _done_bundle(self.root, "WS1", "Alpha work")
        status, body = self._post(
            "/api/workstreams/archive", {"ws": "WS1"}, origin=self.origin, action_header=False
        )
        self.assertEqual(status, 403, body)
        self.assertEqual(body["code"], "forbidden")
        meta = json.loads((self.root / "WS1" / "bundle.json").read_text(encoding="utf-8"))
        self.assertNotEqual(meta.get("state"), "archived")

    def test_no_origin_still_works(self):
        _done_bundle(self.root, "WS1", "Alpha work")
        status, body = self._post("/api/workstreams/archive", {"ws": "WS1"})
        self.assertEqual(status, 200, body)

    def test_read_only_disables_the_action(self):
        _done_bundle(self.root, "WS1", "Alpha work")
        os.environ["AGENT_DASH_READONLY"] = "1"
        try:
            status, body = self._post(
                "/api/workstreams/archive", {"ws": "WS1"}, origin=self.origin
            )
            self.assertEqual(status, 403, body)
            self.assertEqual(body["code"], "read_only")
            status, project = self._get("/api/project")
            self.assertEqual(status, 200)
            self.assertEqual(project["actions"], {"archive": False})
            status, state = self._get("/api/state")
            self.assertEqual(status, 200)
            self.assertEqual(state["actions"], {"archive": False})
        finally:
            os.environ.pop("AGENT_DASH_READONLY", None)
        status, project = self._get("/api/project")
        self.assertEqual(project["actions"], {"archive": True})
        status, state = self._get("/api/state")
        self.assertEqual(state["actions"], {"archive": True})

    def test_other_posts_stay_405(self):
        status, body = self._post("/api/workstreams", {"ws": "WS1"}, origin=self.origin)
        self.assertEqual(status, 405)

    def test_atomic_writes_leave_no_temp_files(self):
        _done_bundle(self.root, "WS1", "Alpha work")
        self.assertEqual(
            self._post("/api/workstreams/archive", {"ws": "WS1"}, origin=self.origin)[0], 200
        )
        self.assertEqual(
            self._post("/api/workstreams/restore", {"ws": "WS1"}, origin=self.origin)[0], 200
        )
        leftovers = list((self.root / "WS1").glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_report_body_never_reaches_payloads(self):
        _done_bundle(self.root, "WS1", "Alpha work")
        status, body = self._post(
            "/api/workstreams/archive", {"ws": "WS1"}, origin=self.origin
        )
        self.assertEqual(status, 200)
        self.assertNotIn(REPORT_SENTINEL, json.dumps(body))
        for path in (
            "/api/archive",
            "/api/workstreams?state=archived",
            "/api/bundles",
            "/api/bundle?ws=WS1",
            "/api/project",
            "/api/state",
        ):
            status, payload = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertNotIn(REPORT_SENTINEL, json.dumps(payload), path)

    def test_write_failure_is_500_house_shape(self):
        _done_bundle(self.root, "WS1", "Alpha work")
        with mock.patch(
            "dash.bundles._atomic_write", side_effect=OSError("denied")
        ), self.assertLogs("agent_dash.actions", level="WARNING") as logs:
            status, body = self._post(
                "/api/workstreams/archive", {"ws": "WS1"}, origin=self.origin
            )
        self.assertEqual(status, 500, body)
        self.assertEqual(body["code"], "write_failed")
        self.assertIn("error", body)
        self.assertIn("hint", body)
        self.assertEqual(body["ws"], "WS1")
        dumped = json.dumps(body)
        self.assertNotIn(self.bundles.name, dumped)
        self.assertNotIn("/", dumped)
        records = logs.records
        self.assertTrue(records, "one WARNING line is logged")
        rendered = [record.getMessage() for record in records]
        self.assertTrue(
            any("WS1" in line and "OSError" in line for line in rendered),
            rendered,
        )
        for record in records:
            self.assertIsNone(record.exc_info, "no traceback from the handler")
            self.assertNotIn(self.bundles.name, record.getMessage())
        meta = json.loads((self.root / "WS1" / "bundle.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["state"], "done")
        status, health = self._get("/api/health")
        self.assertEqual(status, 200)

    def test_stale_state_is_409_changed(self):
        _done_bundle(self.root, "WS1", "Alpha work")

        def _flip(ws, path):
            meta_path = path / "bundle.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["state"] = "paused"
            meta["state_changed_at"] = 1700000000.0
            meta_path.write_text(json.dumps(meta), encoding="utf-8")

        lifecycle._ARCHIVE_PRE_LOCK_HOOK = _flip
        try:
            status, body = self._post(
                "/api/workstreams/archive", {"ws": "WS1"}, origin=self.origin
            )
        finally:
            lifecycle._ARCHIVE_PRE_LOCK_HOOK = None
        self.assertEqual(status, 409, body)
        self.assertEqual(body["code"], "changed")
        self.assertIn("retry", body["hint"].lower())
        meta = json.loads((self.root / "WS1" / "bundle.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["state"], "paused")
        self.assertFalse((self.root / "WS1" / "summary.json").exists())

    def test_rewritten_plan_is_409_changed(self):
        _done_bundle(self.root, "WS1", "Alpha work")

        def _touch_plan(ws, path):
            plan_path = path / "plan.json"
            plan_path.write_text(
                plan_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            stamp = plan_path.stat().st_mtime_ns
            bumped = stamp + 5_000_000_000
            import os as _os

            _os.utime(plan_path, ns=(bumped, bumped))

        lifecycle._ARCHIVE_PRE_LOCK_HOOK = _touch_plan
        try:
            status, body = self._post(
                "/api/workstreams/archive", {"ws": "WS1"}, origin=self.origin
            )
        finally:
            lifecycle._ARCHIVE_PRE_LOCK_HOOK = None
        self.assertEqual(status, 409, body)
        self.assertEqual(body["code"], "changed")
        meta = json.loads((self.root / "WS1" / "bundle.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["state"], "done")
        self.assertFalse((self.root / "WS1" / "summary.json").exists())

    def test_case_alias_is_404(self):
        _done_bundle(self.root, "WS1", "Alpha work")
        status, body = self._post(
            "/api/workstreams/archive", {"ws": "ws1"}, origin=self.origin
        )
        self.assertEqual(status, 404, body)
        self.assertEqual(body["code"], "not_found")
        meta = json.loads((self.root / "WS1" / "bundle.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["state"], "done")

    def test_symlink_alias_is_404(self):
        _done_bundle(self.root, "WS1", "Alpha work")
        (self.root / "wsalias").symlink_to(self.root / "WS1", target_is_directory=True)
        status, body = self._post(
            "/api/workstreams/archive", {"ws": "wsalias"}, origin=self.origin
        )
        self.assertEqual(status, 404, body)
        self.assertEqual(body["code"], "not_found")
        meta = json.loads((self.root / "WS1" / "bundle.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["state"], "done")
        self.assertFalse((self.root / "WS1" / "summary.json").exists())

    def test_dot_ws_is_400(self):
        for dot in (".", ".."):
            status, body = self._post(
                "/api/workstreams/archive", {"ws": dot}, origin=self.origin
            )
            self.assertEqual(status, 400, (dot, body))
            self.assertEqual(body["code"], "bad_request")
            self.assertEqual(body["error"], "bad ws")

    def test_unreadable_plan_names_plan_json(self):
        _done_bundle(self.root, "WS1", "Alpha work")
        (self.root / "WS1" / "plan.json").write_text("not json", encoding="utf-8")
        # The plans-map fallback would otherwise resolve its own WS1 sample
        # plan; pin it to missing so the corrupt bundle plan is decisive.
        with mock.patch(
            "dash.bundles.resolve_plan", return_value=(None, None)
        ):
            status, body = self._post(
                "/api/workstreams/archive", {"ws": "WS1"}, origin=self.origin
            )
        self.assertEqual(status, 500, body)
        self.assertEqual(body["code"], "invalid_data")
        self.assertIn("plan.json", body["error"])
        self.assertNotIn("bundle.json", body["error"])

    def test_lifecycle_result_names_the_failed_file(self):
        status, body = actions_mod._lifecycle_result(
            None, "WS1", "archive", errors.INVALID_DATA, errors.plan_unreadable("WS1")
        )
        self.assertEqual(status, 500)
        self.assertEqual(body["code"], "invalid_data")
        self.assertIn("plan.json", body["error"])
        status, body = actions_mod._lifecycle_result(
            None,
            "WS1",
            "restore",
            errors.INVALID_DATA,
            "bundle.json for WS1 is unreadable. Run: python3 agent_dash.py ws validate WS1",
        )
        self.assertEqual(status, 500)
        self.assertIn("bundle.json", body["error"])

    def test_readonly_string_forms(self):
        for value in ("True", "YES", "on", "1", "true", "yes", "ON", "Yes", True, 1):
            self.assertFalse(
                actions_mod.actions_enabled({"readonly": value}), value
            )
        for value in ("false", "0", "off", "no", "", None, False, 0):
            self.assertTrue(
                actions_mod.actions_enabled({"readonly": value}), value
            )
        self.assertTrue(actions_mod.actions_enabled({}))

    def test_wrong_content_type_is_415(self):
        _done_bundle(self.root, "WS1", "Alpha work")
        for headers in (
            {"X-AgentDash-Action": "1", "Origin": self.origin},
            {
                "Content-Type": "text/plain",
                "X-AgentDash-Action": "1",
                "Origin": self.origin,
            },
        ):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                conn.request(
                    "POST",
                    "/api/workstreams/archive",
                    body=json.dumps({"ws": "WS1"}).encode(),
                    headers=headers,
                )
                response = conn.getresponse()
                status, body = response.status, json.loads(
                    response.read().decode("utf-8")
                )
            finally:
                conn.close()
            self.assertEqual(status, 415, (headers, body))
            self.assertEqual(body["code"], "unsupported_media_type")
            self.assertIn("hint", body)
        meta = json.loads((self.root / "WS1" / "bundle.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["state"], "done")

    def test_duplicate_keys_are_400(self):
        _done_bundle(self.root, "WS1", "Alpha work")
        status, body = self._post(
            "/api/workstreams/archive",
            None,
            origin=self.origin,
            raw=b'{"ws": "WS1", "ws": "WS1"}',
        )
        self.assertEqual(status, 400, body)
        self.assertEqual(body["code"], "bad_request")

    def test_body_read_timeout_is_bounded_and_restored(self):
        raw = b'{"ws": "WS1"}'

        class _Conn:
            def __init__(self):
                self.calls = []
                self.timeout = None

            def gettimeout(self):
                return self.timeout

            def settimeout(self, value):
                self.calls.append(value)
                self.timeout = value

        class _Handler:
            def __init__(self, raw):
                self.headers = {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(raw)),
                }
                self.rfile = io.BytesIO(raw)
                self.connection = _Conn()

        handler = _Handler(raw)
        self.assertEqual(actions_mod.read_ws_body(handler), "WS1")
        self.assertEqual(
            handler.connection.calls, [actions_mod._BODY_READ_TIMEOUT_S, None]
        )

        class _NoConn:
            def __init__(self, raw):
                self.headers = {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(raw)),
                }
                self.rfile = io.BytesIO(raw)

        self.assertEqual(actions_mod.read_ws_body(_NoConn(raw)), "WS1")


if __name__ == "__main__":
    unittest.main()
