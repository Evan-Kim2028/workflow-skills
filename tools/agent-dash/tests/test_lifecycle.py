import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import http.server
import json
import os
import threading
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash.bundles import cmd_new, cmd_ws, list_bundles, validate_bundle
from dash.lifecycle import (
    CLOSING_REPORT,
    DEFAULT_LIST_STATES,
    STATES,
    closing_report_path,
    freeze_summary,
    legal_moves,
)


def _meta(root: Path, ws: str) -> dict:
    return json.loads((root / ws / "bundle.json").read_text(encoding="utf-8"))


def _history(root: Path, ws: str) -> list:
    hist = root / ws / "history.jsonl"
    if not hist.is_file():
        return []
    return [json.loads(line) for line in hist.read_text(encoding="utf-8").splitlines() if line.strip()]


def _finish_plan(root: Path, ws: str) -> None:
    plan_path = root / ws / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    for group in plan["dag"]["groups"]:
        for task in group["tasks"]:
            task["status"] = "done"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")


def _closing_report(root: Path, ws: str) -> None:
    docs = root / ws / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "closing-report.md").write_text("# Closing report\n\nThe sample lane shipped.\n", encoding="utf-8")


class LifecycleFieldsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name
        self.root = Path(self.tmp.name)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old
        self.tmp.cleanup()

    def test_missing_state_defaults_to_active(self):
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)
        meta_path = self.root / "WS9" / "bundle.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        del meta["state"]
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        self.assertEqual(validate_bundle("WS9"), [])
        rows = {item["id"]: item for item in list_bundles(self.root)}
        self.assertEqual(rows["WS9"]["state"], "active")

    def test_new_state_flag(self):
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream", "--state", "planned"]), 0)
        self.assertEqual(_meta(self.root, "WS9")["state"], "planned")
        self.assertIsInstance(_meta(self.root, "WS9")["state_changed_at"], float)
        self.assertEqual(validate_bundle("WS9"), [])
        self.assertEqual(cmd_new("WS8", "Other", state="bogus"), 2)
        self.assertEqual(cmd_ws(["new", "WS7", "Third", "--state", "bogus"]), 2)
        self.assertFalse((self.root / "WS7").exists())

    def test_invalid_fields_fail_validate_with_next_command(self):
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)
        meta_path = self.root / "WS9" / "bundle.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["state"] = "flying"
        meta["depends_on"] = ["WS404"]
        meta["milestone"] = 7
        meta["summary"] = "done-ish"
        meta["state_changed_at"] = "yesterday"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        issues = validate_bundle("WS9")
        self.assertTrue(any("state 'flying'" in item for item in issues))
        self.assertTrue(any("WS404" in item for item in issues))
        self.assertTrue(any("milestone" in item for item in issues))
        self.assertTrue(any("summary" in item for item in issues))
        self.assertTrue(any("state_changed_at" in item for item in issues))
        self.assertTrue(all("Run: python3 agent_dash.py" in item for item in issues))
        self.assertEqual(cmd_ws(["validate", "WS9"]), 4)

    def test_depends_on_overview_rejected(self):
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)
        meta_path = self.root / "WS9" / "bundle.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["depends_on"] = ["OVERVIEW"]
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        self.assertTrue(any("OVERVIEW" in item for item in validate_bundle("WS9")))
        self.assertEqual(cmd_ws(["validate", "WS9"]), 4)

    def test_valid_depends_on_and_milestone_pass(self):
        self.assertEqual(cmd_ws(["new", "WS8", "Upstream lane"]), 0)
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)
        meta_path = self.root / "WS9" / "bundle.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["depends_on"] = ["WS8"]
        meta["milestone"] = "Part 2"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        self.assertEqual(validate_bundle("WS9"), [])
        rows = {item["id"]: item for item in list_bundles(self.root)}
        self.assertEqual(rows["WS9"]["depends_on"], ["WS8"])
        self.assertEqual(rows["WS9"]["milestone"], "Part 2")


class WsStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name
        self.root = Path(self.tmp.name)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old
        self.tmp.cleanup()

    def test_legal_path_and_history(self):
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream", "--state", "planned"]), 0)
        self.assertEqual(cmd_ws(["state", "WS9", "active", "--note", "kicked off"]), 0)
        meta = _meta(self.root, "WS9")
        self.assertEqual(meta["state"], "active")
        self.assertIsInstance(meta["state_changed_at"], float)
        rows = _history(self.root, "WS9")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "state")
        self.assertEqual((rows[0]["ws"], rows[0]["from"], rows[0]["to"]), ("WS9", "planned", "active"))
        self.assertEqual(rows[0]["note"], "kicked off")
        self.assertEqual(cmd_ws(["state", "WS9", "paused"]), 0)
        self.assertEqual(cmd_ws(["state", "WS9", "active"]), 0)
        self.assertEqual(cmd_ws(["state", "WS9", "done"]), 0)
        self.assertEqual(_meta(self.root, "WS9")["state"], "done")
        self.assertEqual(len(_history(self.root, "WS9")), 4)

    def test_illegal_move_names_legal_moves(self):
        from io import StringIO
        from contextlib import redirect_stdout

        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream", "--state", "planned"]), 0)
        buf = StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cmd_ws(["state", "WS9", "done"]), 2)
        self.assertIn("illegal move WS9 planned -> done", buf.getvalue())
        self.assertIn("active", buf.getvalue())
        self.assertEqual(_meta(self.root, "WS9")["state"], "planned")
        self.assertEqual(_history(self.root, "WS9"), [])
        with redirect_stdout(StringIO()):
            self.assertEqual(cmd_ws(["state", "WS9", "flying"]), 2)

    def test_reopen_done_needs_force_and_note(self):
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)
        self.assertEqual(cmd_ws(["state", "WS9", "done"]), 0)
        self.assertEqual(cmd_ws(["state", "WS9", "active"]), 2)
        self.assertEqual(cmd_ws(["state", "WS9", "active", "--force"]), 2)
        self.assertEqual(
            cmd_ws(["state", "WS9", "active", "--force", "--note", "the freeze thawed"]), 0
        )
        self.assertEqual(_meta(self.root, "WS9")["state"], "active")

    def test_noop_move_writes_nothing(self):
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)
        self.assertEqual(cmd_ws(["state", "WS9", "active"]), 0)
        self.assertEqual(_history(self.root, "WS9"), [])
        self.assertEqual(cmd_ws(["state", "NOPE", "active"]), 3)

    def test_restore_only_leaves_archived(self):
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)
        self.assertEqual(cmd_ws(["state", "WS9", "done"]), 0)
        self.assertEqual(cmd_ws(["state", "WS9", "archived"]), 0)
        meta = _meta(self.root, "WS9")
        self.assertEqual(meta["state"], "archived")
        self.assertIsInstance(meta["archived_at"], float)
        self.assertEqual(cmd_ws(["state", "WS9", "done"]), 2)
        self.assertEqual(cmd_ws(["restore", "WS9"]), 0)
        meta = _meta(self.root, "WS9")
        self.assertEqual(meta["state"], "done")
        self.assertIsNone(meta["archived_at"])
        self.assertEqual(cmd_ws(["restore", "WS9"]), 2)


class WsArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name
        self.old_url = os.environ.get("AGENT_DASH_URL")
        os.environ["AGENT_DASH_URL"] = "http://127.0.0.1:1"
        self.root = Path(self.tmp.name)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old
        if self.old_url is None:
            os.environ.pop("AGENT_DASH_URL", None)
        else:
            os.environ["AGENT_DASH_URL"] = self.old_url
        self.tmp.cleanup()

    def test_archive_refuses_incomplete_plan(self):
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)
        _closing_report(self.root, "WS9")
        self.assertEqual(cmd_ws(["state", "WS9", "done"]), 0)
        self.assertEqual(cmd_ws(["archive", "WS9"]), 2)
        self.assertEqual(_meta(self.root, "WS9")["state"], "done")
        self.assertEqual(cmd_ws(["archive", "WS9", "--force"]), 2)
        self.assertEqual(
            cmd_ws(["archive", "WS9", "--force", "--note", "archiving the sample early"]), 0
        )
        self.assertEqual(_meta(self.root, "WS9")["state"], "archived")

    def test_archive_requires_closing_report(self):
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)
        _finish_plan(self.root, "WS9")
        self.assertEqual(cmd_ws(["state", "WS9", "done"]), 0)
        self.assertEqual(cmd_ws(["archive", "WS9"]), 2)
        self.assertFalse((self.root / "WS9" / "summary.json").exists())

    def test_archive_freezes_summary(self):
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)
        _finish_plan(self.root, "WS9")
        self.assertEqual(cmd_ws(["state", "WS9", "done"]), 0)
        _closing_report(self.root, "WS9")
        self.assertEqual(cmd_ws(["archive", "WS9"]), 0)
        meta = _meta(self.root, "WS9")
        self.assertEqual(meta["state"], "archived")
        self.assertIsInstance(meta["archived_at"], float)
        summary = json.loads((self.root / "WS9" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["ws"], "WS9")
        self.assertEqual(summary["tasks"]["done"], summary["tasks"]["total"])
        self.assertGreater(summary["tasks"]["total"], 0)
        self.assertTrue(summary["closing_report"])
        self.assertIsNone(summary["usage"]["tokens_all_time"])
        self.assertIsNone(summary["usage"]["cost_usd_all_time"])
        self.assertFalse(summary["usage"]["reachable"])
        self.assertIn("127.0.0.1:1", summary["usage"]["note"])
        self.assertIsNotNone(summary["finished_at"])
        rows = _history(self.root, "WS9")
        self.assertEqual(rows[-1]["kind"], "state")
        self.assertEqual((rows[-1]["from"], rows[-1]["to"]), ("done", "archived"))
        self.assertEqual(cmd_ws(["archive", "WS9"]), 2)

    def test_archive_fetches_usage_when_reachable(self):
        payload = {"workstreams": [{"ws": "WS9", "tokens_all_time": 42, "cost_usd_all_time": 1.5}]}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            from dash.lifecycle import fetch_workstream_usage

            usage = fetch_workstream_usage(f"http://127.0.0.1:{server.server_address[1]}", "WS9")
            self.assertTrue(usage["reachable"])
            self.assertEqual(usage["tokens_all_time"], 42)
            self.assertEqual(usage["cost_usd_all_time"], 1.5)
            missing = fetch_workstream_usage(f"http://127.0.0.1:{server.server_address[1]}", "WS404")
            self.assertTrue(missing["reachable"])
            self.assertIsNone(missing["tokens_all_time"])
        finally:
            server.shutdown()
            thread.join(3)
            server.server_close()

    def test_restore_keeps_summary(self):
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)
        _finish_plan(self.root, "WS9")
        self.assertEqual(cmd_ws(["state", "WS9", "done"]), 0)
        _closing_report(self.root, "WS9")
        self.assertEqual(cmd_ws(["archive", "WS9"]), 0)
        self.assertEqual(cmd_ws(["restore", "WS9"]), 0)
        self.assertTrue((self.root / "WS9" / "summary.json").is_file())
        rows = _history(self.root, "WS9")
        self.assertEqual((rows[-1]["kind"], rows[-1]["from"], rows[-1]["to"]), ("state", "archived", "done"))


class WsListTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name
        self.root = Path(self.tmp.name)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old
        self.tmp.cleanup()

    def _ids(self, *args):
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cmd_ws(["list", *args]), 0)
        lines = [line for line in buf.getvalue().splitlines() if line and not line.startswith("ID\t")]
        return [line.split("\t")[0] for line in lines]

    def test_default_hides_archived_and_dropped(self):
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)
        self.assertEqual(cmd_ws(["new", "WS8", "Older lane", "--state", "planned"]), 0)
        self.assertEqual(cmd_ws(["state", "WS9", "done"]), 0)
        self.assertEqual(cmd_ws(["state", "WS9", "archived"]), 0)
        self.assertEqual(self._ids(), ["WS8"])
        self.assertEqual(self._ids("--state", "archived"), ["WS9"])
        self.assertEqual(self._ids("--state", "all"), ["WS9", "WS8"])
        self.assertEqual(self._ids("--state", "planned,done"), ["WS8"])

    def test_list_json_rows(self):
        from io import StringIO
        from contextlib import redirect_stdout

        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)
        buf = StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cmd_ws(["list", "--json"]), 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(len(payload["workstreams"]), 1)
        row = payload["workstreams"][0]
        self.assertEqual(row["id"], "WS9")
        self.assertEqual(row["state"], "active")
        self.assertEqual(row["progress"]["total"], 1)
        self.assertIsInstance(row["state_changed_at"], float)
        buf = StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cmd_ws(["list", "--state", "flying"]), 2)
        self.assertIn("unknown state", buf.getvalue())


class PureHelpersTest(unittest.TestCase):
    def test_legal_moves(self):
        self.assertEqual(legal_moves("planned"), ["active"])
        self.assertEqual(set(legal_moves("active")), {"paused", "done", "dropped"})
        self.assertEqual(set(legal_moves("done")), {"archived", "active"})
        self.assertEqual(legal_moves("archived"), [])
        self.assertEqual(legal_moves("dropped"), [])
        self.assertEqual(legal_moves("flying"), [])
        self.assertTrue(legal_moves.__doc__)

    def test_closing_report_path(self):
        self.assertEqual(
            closing_report_path(Path("/tmp/bundles/WS9")),
            Path("/tmp/bundles/WS9") / CLOSING_REPORT,
        )
        self.assertEqual(CLOSING_REPORT, Path("docs/closing-report.md"))
        self.assertTrue(closing_report_path.__doc__)

    def test_freeze_summary(self):
        summary = freeze_summary(
            "WS9",
            started_at=1000.0,
            finished_at=4600.0,
            progress={"done": 2, "total": 2, "counts": {"todo": 0, "done": 2}, "pct": 100},
            trains=[{"id": "T1", "status": "merged"}, {"id": "T2", "status": "planned"}],
            approvals={"items": [
                {"id": "a", "status": "approved"},
                {"id": "b", "status": "pending"},
            ]},
            usage={"tokens_all_time": 10, "cost_usd_all_time": 0.1, "reachable": True, "note": None},
            closing_report=True,
            note=None,
        )
        self.assertEqual(summary["ws"], "WS9")
        self.assertEqual(summary["duration_s"], 3600.0)
        self.assertEqual(summary["tasks"]["done"], 2)
        self.assertEqual(summary["trains"], {"total": 2, "by_status": {"merged": 1, "planned": 1}})
        self.assertEqual(summary["approvals"]["approved"], 1)
        self.assertEqual(summary["approvals"]["total"], 2)
        self.assertTrue(freeze_summary.__doc__)
        self.assertEqual(STATES, ("planned", "active", "paused", "done", "archived", "dropped"))
        self.assertEqual(
            set(DEFAULT_LIST_STATES), {"active", "paused", "planned", "done"}
        )


class ReplayIgnoresStateLinesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name
        self.root = Path(self.tmp.name)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old
        self.tmp.cleanup()

    def test_state_lines_do_not_break_replay(self):
        from dash.replay import read_history

        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)
        self.assertEqual(cmd_ws(["task", "WS9", "WS9.1", "done"]), 0)
        self.assertEqual(cmd_ws(["state", "WS9", "done"]), 0)
        rows = read_history("WS9")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task"], "WS9.1")
        self.assertEqual(rows[0]["to"], "done")
        raw = (self.root / "WS9" / "history.jsonl").read_text(encoding="utf-8")
        self.assertIn('"kind": "state"', raw)


if __name__ == "__main__":
    unittest.main()