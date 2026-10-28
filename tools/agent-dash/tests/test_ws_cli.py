import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import bundles, errors, linear
from dash.bundles import cmd_ws, naming_issues, set_task, validate_bundle


def _add_tasks(root: Path, ws: str, ids: list) -> None:
    plan_path = root / ws / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    tasks = plan["dag"]["groups"][0]["tasks"]
    for tid in ids:
        if tid not in [task["id"] for task in tasks]:
            tasks.append({"id": tid, "title": f"Task {tid}", "status": "todo"})
    plan_path.write_text(json.dumps(plan), encoding="utf-8")


def _plan(root: Path, ws: str) -> dict:
    return json.loads((root / ws / "plan.json").read_text(encoding="utf-8"))


class WsCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old
        self.tmp.cleanup()

    def test_new_validates_and_lists(self):
        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)
        self.assertEqual(cmd_ws(["validate", "WS9"]), 0)
        root = Path(self.tmp.name) / "WS9"
        self.assertTrue((root / "plan.json").is_file())
        self.assertTrue((root / "layout.json").is_file())
        prompt = (root / "START-PROMPT.md").read_text(encoding="utf-8")
        self.assertIn("WS9", prompt)
        self.assertIn("Example workstream", prompt)
        self.assertFalse((root / "plan.json.tmp").exists())
        program = root.parent / "OVERVIEW"
        program.mkdir()
        (program / "bundle.json").write_text(
            json.dumps({"id": "OVERVIEW", "title": "Overview", "short": "Program", "order": 50, "kind": "program"}),
            encoding="utf-8",
        )
        (program / "plan.json").write_text(
            json.dumps({"narrative": {"thesis": "One program.", "end_state": "Clients share one API.", "phases": []}}),
            encoding="utf-8",
        )
        self.assertEqual(validate_bundle("OVERVIEW"), [])
        listed = cmd_ws(["list"])
        self.assertEqual(listed, 0)

    def test_new_state_flag_writes_field(self):
        self.assertEqual(cmd_ws(["new", "WS4", "Sample", "--state", "planned"]), 0)
        meta = json.loads((Path(self.tmp.name) / "WS4" / "bundle.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["state"], "planned")
        self.assertIsInstance(meta["state_changed_at"], float)
        self.assertEqual(validate_bundle("WS4"), [])
        self.assertEqual(cmd_ws(["new", "WS5", "Other", "--state", "bogus"]), 2)
        self.assertFalse((Path(self.tmp.name) / "WS5").exists())

    def test_naming_rule(self):
        task = "TU-" + "12"
        self.assertTrue(naming_issues("ADR 0119 picks the id"))
        self.assertFalse(naming_issues("the canonical card id (ADR 0119) picks the id"))
        self.assertTrue(naming_issues(f'<section class="pl-sec lay"><h2>what {task} retires</h2>'))
        self.assertFalse(naming_issues(f"<p>the read-path migration ({task}) is done.</p>"))
        self.assertFalse(naming_issues("note #6"))
        self.assertFalse(naming_issues("serial #143629585"))
        self.assertTrue(naming_issues("see #1402"))
        self.assertFalse(naming_issues("the chain-sensor fix (#1402)"))
        self.assertFalse(naming_issues("color:#000"))
        bare = Path(self.tmp.name) / "WS4"
        self.assertEqual(cmd_ws(["new", "WS4", "Sample"]), 0)
        plan_path = bare / "plan.json"
        text = plan_path.read_text(encoding="utf-8")
        text = text.replace(
            "One sentence on why this workstream exists.",
            "ADR 0099 scopes the log.",
        )
        plan_path.write_text(text, encoding="utf-8")
        issues = validate_bundle("WS4")
        self.assertTrue(any("ADR 0099" in item for item in issues))
        plan_path.write_text(
            text.replace("ADR 0099 scopes the log.", "the ingest log (ADR 0099) scopes the log."),
            encoding="utf-8",
        )
        self.assertEqual(validate_bundle("WS4"), [])

    def test_task_deps_validate(self):
        self.assertEqual(cmd_ws(["new", "WS5", "Deps"]), 0)
        path = Path(self.tmp.name) / "WS5" / "plan.json"
        plan = json.loads(path.read_text(encoding="utf-8"))
        tasks = [task for group in plan["dag"]["groups"] for task in group["tasks"]]
        first = tasks[0]["id"]
        tasks[-1]["deps"] = [first] if tasks[-1]["id"] != first else []
        path.write_text(json.dumps(plan), encoding="utf-8")
        self.assertEqual(validate_bundle("WS5"), [])
        tasks[0]["deps"] = ["WS5.missing", first]
        path.write_text(json.dumps(plan), encoding="utf-8")
        issues = validate_bundle("WS5")
        self.assertTrue(any("unknown task WS5.missing" in item for item in issues))
        self.assertTrue(any("waits on itself" in item for item in issues))
        tasks[0]["deps"] = "nope"
        path.write_text(json.dumps(plan), encoding="utf-8")
        self.assertTrue(any("deps must be a list" in item for item in validate_bundle("WS5")))

    def test_task_kind_and_dep_notes_validate(self):
        self.assertEqual(cmd_ws(["new", "WS6", "Gates"]), 0)
        _add_tasks(Path(self.tmp.name), "WS6", ["WS6.2"])
        path = Path(self.tmp.name) / "WS6" / "plan.json"
        plan = json.loads(path.read_text(encoding="utf-8"))
        tasks = [task for group in plan["dag"]["groups"] for task in group["tasks"]]
        first = tasks[0]["id"]
        tasks[0]["kind"] = "gate"
        tasks[-1]["deps"] = [first]
        tasks[-1]["dep_notes"] = {first: "start only after the signal"}
        path.write_text(json.dumps(plan), encoding="utf-8")
        self.assertEqual(validate_bundle("WS6"), [])
        tasks[0]["kind"] = "signal"
        path.write_text(json.dumps(plan), encoding="utf-8")
        self.assertTrue(any('kind \'signal\'' in item for item in validate_bundle("WS6")))
        tasks[0]["kind"] = "gate"
        tasks[-1]["dep_notes"] = {"WS6.missing": "start only after the signal"}
        path.write_text(json.dumps(plan), encoding="utf-8")
        issues = validate_bundle("WS6")
        self.assertTrue(any("dep_notes for WS6.missing is not one of its deps" in item for item in issues))
        tasks[-1]["dep_notes"] = "nope"
        path.write_text(json.dumps(plan), encoding="utf-8")
        self.assertTrue(any("dep_notes must be an object" in item for item in validate_bundle("WS6")))
        tasks[-1]["dep_notes"] = {first: ""}
        path.write_text(json.dumps(plan), encoding="utf-8")
        self.assertTrue(any("must be a non-empty string" in item for item in validate_bundle("WS6")))
        tasks[-1]["dep_notes"] = {first: "x" * 281}
        path.write_text(json.dumps(plan), encoding="utf-8")
        self.assertTrue(any("must be a short string" in item for item in validate_bundle("WS6")))

    def test_task_updates_atomically(self):
        self.assertEqual(cmd_ws(["new", "WS3", "Third"]), 0)
        self.assertEqual(set_task("WS3", "WS3.1", "done", None, None), 0)
        plan = json.loads((Path(self.tmp.name) / "WS3" / "plan.json").read_text(encoding="utf-8"))
        task = plan["dag"]["groups"][0]["tasks"][0]
        self.assertEqual(task["status"], "done")
        self.assertNotIn("moved_to", task)
        self.assertEqual(set_task("WS3", "WS3.1", "deferred", "WS6", "waiting on the cutover"), 0)
        plan = json.loads((Path(self.tmp.name) / "WS3" / "plan.json").read_text(encoding="utf-8"))
        task = plan["dag"]["groups"][0]["tasks"][0]
        self.assertEqual(task["status"], "deferred")
        self.assertEqual(task["moved_to"], "WS6")
        self.assertEqual(task["note"], "waiting on the cutover")
        self.assertFalse((Path(self.tmp.name) / "WS3" / "plan.json.tmp").exists())
        self.assertEqual(set_task("WS3", "WS3.1", "todo", None, None), 0)
        plan = json.loads((Path(self.tmp.name) / "WS3" / "plan.json").read_text(encoding="utf-8"))
        task = plan["dag"]["groups"][0]["tasks"][0]
        self.assertEqual(task["status"], "todo")
        self.assertNotIn("moved_to", task)
        self.assertNotIn("note", task)
        self.assertEqual(set_task("WS3", "missing", "done", None, None), 3)
        self.assertEqual(cmd_ws(["task", "WS3", "WS3.1", "nope"]), 2)

    def test_done_and_deferred_need_force_to_reopen(self):
        self.assertEqual(cmd_ws(["new", "WS8", "Guard"]), 0)
        self.assertEqual(set_task("WS8", "WS8.1", "done", None, None), 0)
        self.assertEqual(cmd_ws(["task", "WS8", "WS8.1", "wip"]), 2)
        self.assertEqual(cmd_ws(["task", "WS8", "WS8.1", "pr"]), 2)
        plan = _plan(Path(self.tmp.name), "WS8")
        self.assertEqual(plan["dag"]["groups"][0]["tasks"][0]["status"], "done")
        self.assertEqual(cmd_ws(["task", "WS8", "WS8.1", "wip", "--force"]), 0)
        plan = _plan(Path(self.tmp.name), "WS8")
        self.assertEqual(plan["dag"]["groups"][0]["tasks"][0]["status"], "wip")
        self.assertEqual(set_task("WS8", "WS8.1", "deferred", "WS6", "waiting on the cutover"), 0)
        self.assertEqual(cmd_ws(["task", "WS8", "WS8.1", "wip"]), 2)
        self.assertEqual(cmd_ws(["task", "WS8", "WS8.1", "pr"]), 2)
        self.assertEqual(cmd_ws(["task", "WS8", "WS8.1", "wip", "--force"]), 0)
        plan = _plan(Path(self.tmp.name), "WS8")
        self.assertEqual(plan["dag"]["groups"][0]["tasks"][0]["status"], "wip")
        self.assertEqual(set_task("WS8", "WS8.1", "done", None, None), 0)
        self.assertEqual(cmd_ws(["task", "WS8", "WS8.1", "todo"]), 0)

    def test_concurrent_task_writes_keep_every_update(self):
        import subprocess

        self.assertEqual(cmd_ws(["new", "WS9", "Race"]), 0)
        ids = [f"WS9.{n}" for n in range(1, 101)]
        _add_tasks(Path(self.tmp.name), "WS9", ids)
        root = str(Path(__file__).resolve().parents[1])
        worker = (
            "import sys; sys.path.insert(0, %r); "
            "from dash.bundles import set_task; "
            "[set_task('WS9', tid, 'done', None, None) for tid in %r]"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", worker % (root, ids[:50])],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ),
            subprocess.Popen(
                [sys.executable, "-c", worker % (root, ids[50:])],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ),
        ]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=120), 0)
        plan = _plan(Path(self.tmp.name), "WS9")
        statuses = {task["id"]: task["status"] for group in plan["dag"]["groups"] for task in group["tasks"]}
        missing = [tid for tid in ids if statuses.get(tid) != "done"]
        self.assertEqual(missing, [])

    def test_task_appends_history(self):
        from dash.bundles import append_history

        self.assertEqual(cmd_ws(["new", "WS3", "Third"]), 0)
        hist = Path(self.tmp.name) / "WS3" / "history.jsonl"
        self.assertEqual(set_task("WS3", "WS3.1", "pr", None, None), 0)
        lines = hist.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        first = json.loads(lines[0])
        self.assertEqual(first["task"], "WS3.1")
        self.assertEqual(first["from"], "todo")
        self.assertEqual(first["to"], "pr")
        self.assertIsInstance(first["ts"], float)
        self.assertEqual(set_task("WS3", "WS3.1", "pr", None, None), 0)
        self.assertEqual(len(hist.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(set_task("WS3", "WS3.1", "done", None, None), 0)
        lines = hist.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        second = json.loads(lines[1])
        self.assertEqual((second["from"], second["to"]), ("pr", "done"))
        self.assertGreaterEqual(second["ts"], first["ts"])
        append_history("WS3", "WS3.1", "done", "done", root=Path(self.tmp.name), now=1234.0)
        rows = [json.loads(line) for line in hist.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[-1]["ts"], 1234.0)

    def test_approvals_schema_cli_and_naming(self):
        from io import StringIO
        from contextlib import redirect_stdout

        self.assertEqual(cmd_ws(["new", "WS9", "Example workstream"]), 0)
        self.assertEqual(cmd_ws(["approve", "WS9", "sample-load", "pending"]), 2)
        root = Path(self.tmp.name) / "WS9"
        self.assertFalse((root / "approvals.json").exists())
        self.assertEqual(
            cmd_ws(["approve", "WS9", "sample-load", "pending", "--step", "Load the sample set before cutover", "--reversible", "no", "--conditions", "The sample set is on disk."]),
            0,
        )
        self.assertFalse((root / "approvals.json.tmp").exists())
        data = json.loads((root / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual(data["ws"], "WS9")
        self.assertIsNone(data["decided_at"])
        self.assertIsNone(data["decided_by"])
        self.assertEqual(data["items"][0]["status"], "pending")
        self.assertIs(data["items"][0]["reversible"], False)
        self.assertEqual(validate_bundle("WS9"), [])
        self.assertEqual(cmd_ws(["approve", "WS9", "sample-load", "approved", "--note", "Owner signed off.", "--by", "owner"]), 0)
        data = json.loads((root / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual(data["items"][0]["status"], "approved")
        self.assertEqual(data["items"][0]["note"], "Owner signed off.")
        self.assertEqual(data["decided_by"], "owner")
        self.assertTrue(data["decided_at"])
        self.assertFalse((root / "approvals.json.tmp").exists())
        buf = StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cmd_ws(["approvals", "WS9"]), 0)
        text = buf.getvalue()
        self.assertIn("STATUS", text)
        self.assertIn("sample-load", text)
        self.assertIn("one-way", text)
        self.assertEqual(cmd_ws(["approve", "WS9", "route-name", "denied", "--step", "Keep the public route name", "--reversible", "yes", "--conditions", "Clients already call this route."]), 0)
        broken = json.loads((root / "approvals.json").read_text(encoding="utf-8"))
        broken["items"][0]["status"] = "maybe"
        broken["items"][0]["reversible"] = "no"
        (root / "approvals.json").write_text(json.dumps(broken), encoding="utf-8")
        issues = validate_bundle("WS9")
        self.assertTrue(any("status" in item for item in issues))
        self.assertTrue(any("reversible" in item for item in issues))
        (root / "approvals.json").write_text(
            "\n".join([
                "{",
                '  "ws": "WS9",',
                '  "decided_at": null,',
                '  "decided_by": null,',
                '  "items": [{',
                '    "id": "bare",',
                '    "step": "ADR 0099 scopes the log",',
                '    "status": "pending",',
                '    "reversible": true,',
                '    "conditions": "None."',
                "  }]",
                "}",
                "",
            ]),
            encoding="utf-8",
        )
        issues = validate_bundle("WS9")
        self.assertTrue(any("ADR 0099" in item for item in issues))
        self.assertEqual(cmd_ws(["approvals", "NOPE"]), 3)


class WsTrainCliTest(unittest.TestCase):
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

    def test_template_has_empty_trains(self):
        self.assertEqual(cmd_ws(["new", "WS4", "Sample"]), 0)
        plan = _plan(self.root, "WS4")
        self.assertEqual(plan["trains"], [])
        prompt = (self.root / "WS4" / "START-PROMPT.md").read_text(encoding="utf-8")
        self.assertIn("ws train", prompt)
        self.assertEqual(validate_bundle("WS4"), [])

    def test_create_needs_title(self):
        self.assertEqual(cmd_ws(["new", "WS4", "Sample"]), 0)
        self.assertEqual(cmd_ws(["train", "WS4", "T1"]), 2)
        self.assertEqual(_plan(self.root, "WS4")["trains"], [])
        self.assertEqual(cmd_ws(["train", "WS4", "T1", "--title", "read-path cutover"]), 0)
        trains = _plan(self.root, "WS4")["trains"]
        self.assertEqual(len(trains), 1)
        self.assertEqual(trains[0]["id"], "T1")
        self.assertEqual(trains[0]["title"], "read-path cutover")
        self.assertEqual(trains[0]["status"], "planned")
        self.assertEqual(trains[0]["members"], [])
        self.assertFalse((self.root / "WS4" / "plan.json.tmp").exists())
        self.assertEqual(validate_bundle("WS4"), [])

    def test_add_move_remove(self):
        from io import StringIO
        from contextlib import redirect_stdout

        self.assertEqual(cmd_ws(["new", "WS4", "Sample"]), 0)
        self.assertEqual(cmd_ws(["train", "WS4", "T1", "--title", "first train", "--add", "WS4.1"]), 0)
        self.assertEqual(_plan(self.root, "WS4")["trains"][0]["members"], ["WS4.1"])
        buf = StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cmd_ws(["train", "WS4", "T2", "--title", "second train", "--add", "WS4.1"]), 0)
        self.assertIn("moved WS4.1 from T1 to T2", buf.getvalue())
        trains = {row["id"]: row for row in _plan(self.root, "WS4")["trains"]}
        self.assertEqual(trains["T1"]["members"], [])
        self.assertEqual(trains["T2"]["members"], ["WS4.1"])
        self.assertEqual(cmd_ws(["train", "WS4", "T2", "--remove", "WS4.1"]), 0)
        trains = {row["id"]: row for row in _plan(self.root, "WS4")["trains"]}
        self.assertEqual(trains["T2"]["members"], [])
        self.assertEqual(validate_bundle("WS4"), [])

    def test_fields(self):
        self.assertEqual(cmd_ws(["new", "WS4", "Sample"]), 0)
        self.assertEqual(
            cmd_ws([
                "train", "WS4", "T1", "--title", "read-path cutover",
                "--branch", "train/read-path", "--status", "assembling",
                "--add", "WS4.1", "--pr", "5357",
                "--shared", "api/x.py,api/y.py", "--gate", "pytest api/tests/test_x.py",
                "--note", "cut at planning time",
            ]),
            0,
        )
        train = _plan(self.root, "WS4")["trains"][0]
        self.assertEqual(train["branch"], "train/read-path")
        self.assertEqual(train["status"], "assembling")
        self.assertEqual(train["pr"], 5357)
        self.assertEqual(train["shared_files"], ["api/x.py", "api/y.py"])
        self.assertEqual(train["gate"], "pytest api/tests/test_x.py")
        self.assertEqual(train["note"], "cut at planning time")
        self.assertEqual(validate_bundle("WS4"), [])
        self.assertEqual(cmd_ws(["train", "WS4", "T1", "--status", "pr"]), 0)
        self.assertEqual(_plan(self.root, "WS4")["trains"][0]["status"], "pr")

    def test_trains_table(self):
        from io import StringIO
        from contextlib import redirect_stdout

        self.assertEqual(cmd_ws(["new", "WS4", "Sample"]), 0)
        buf = StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cmd_ws(["trains", "WS4"]), 0)
        self.assertIn("(no trains)", buf.getvalue())
        self.assertEqual(cmd_ws(["task", "WS4", "WS4.1", "pr"]), 0)
        self.assertEqual(
            cmd_ws(["train", "WS4", "T1", "--title", "read-path cutover", "--add", "WS4.1", "--shared", "api/x.py"]),
            0,
        )
        buf = StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cmd_ws(["trains", "WS4"]), 0)
        text = buf.getvalue()
        self.assertIn("TRAIN", text)
        self.assertIn("T1", text)
        self.assertIn("WS4.1(pr)", text)
        self.assertEqual(cmd_ws(["trains", "NOPE"]), 3)

    def test_rejects_bad_values(self):
        self.assertEqual(cmd_ws(["new", "WS4", "Sample"]), 0)
        self.assertEqual(cmd_ws(["train", "WS4", "T1", "--title", "cutover", "--status", "open"]), 2)
        self.assertEqual(cmd_ws(["train", "WS4", "T1", "--title", "cutover", "--branch", "main"]), 2)
        self.assertEqual(cmd_ws(["train", "WS4", "T 1", "--title", "cutover"]), 2)
        self.assertEqual(cmd_ws(["train", "WS4", "T1", "--title", "cutover", "--pr", "many"]), 2)
        self.assertEqual(cmd_ws(["train", "WS4", "T1", "--title", "cutover", "--add", "WS4.missing"]), 3)
        self.assertEqual(cmd_ws(["train", "NOPE", "T1", "--title", "cutover"]), 3)
        self.assertEqual(cmd_ws(["train", "WS4"]), 2)
        self.assertEqual(_plan(self.root, "WS4")["trains"], [])

    def test_rejects_ninth_member(self):
        self.assertEqual(cmd_ws(["new", "WS4", "Sample"]), 0)
        ids = [f"WS4.{n}" for n in range(1, 10)]
        _add_tasks(self.root, "WS4", ids)
        self.assertEqual(
            cmd_ws(["train", "WS4", "T1", "--title", "big train", "--add", ",".join(ids)]),
            2,
        )
        self.assertEqual(_plan(self.root, "WS4")["trains"], [])
        self.assertEqual(
            cmd_ws(["train", "WS4", "T1", "--title", "big train", "--add", ",".join(ids[:8])]),
            0,
        )
        self.assertEqual(cmd_ws(["train", "WS4", "T1", "--add", ids[8]]), 2)
        self.assertEqual(len(_plan(self.root, "WS4")["trains"][0]["members"]), 8)

    def test_validation_issues(self):
        self.assertEqual(cmd_ws(["new", "WS4", "Sample"]), 0)
        plan_path = self.root / "WS4" / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["trains"] = [
            {"id": "T1", "title": "first train", "members": ["WS4.1"], "status": "planned"},
            {"id": "T1", "title": "duplicate key", "members": [], "status": "planned"},
            {"id": "T2", "title": "second train", "members": ["WS4.1"], "status": "flying"},
            {"id": "T3", "title": "third train", "members": ["WS4.missing"], "status": "planned", "branch": "main"},
        ]
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        issues = validate_bundle("WS4")
        self.assertTrue(any("duplicate train id T1" in item for item in issues))
        self.assertTrue(any("task WS4.1 is in two trains" in item for item in issues))
        self.assertTrue(any("status 'flying'" in item for item in issues))
        self.assertTrue(any("unknown task WS4.missing" in item for item in issues))
        self.assertTrue(any('branch must start with "train/"' in item for item in issues))
        plan["trains"] = [
            {"id": "T9", "title": "the read-path decision (ADR 0123) is done", "members": [], "status": "planned"},
        ]
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        self.assertFalse(any("ADR 0123" in item for item in validate_bundle("WS4")))
        plan["trains"] = [{"id": "T9", "title": "ADR 0123 scopes the log", "members": [], "status": "planned"}]
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        self.assertTrue(any("ADR 0123" in item for item in validate_bundle("WS4")))
        _add_tasks(self.root, "WS4", [f"WS4.{n}" for n in range(2, 10)])
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["trains"] = [
            {"id": "T1", "title": "big train", "members": [f"WS4.{n}" for n in range(1, 10)], "status": "planned"}
        ]
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        self.assertIn("train T1 has 9 members; split it (limit 8)", validate_bundle("WS4"))


class OrchestrateCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name
        self.old_url = os.environ.get("AGENT_DASH_URL")
        os.environ["AGENT_DASH_URL"] = "http://127.0.0.1:1"
        self.root = Path(self.tmp.name)
        self.assertEqual(cmd_ws(["new", "WS9", "Sample"]), 0)
        plan_path = self.root / "WS9" / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["dag"]["groups"] = [
            {
                "id": "g",
                "col": 0,
                "title": "Group",
                "tasks": [
                    {"id": "WS9.0", "title": "Freeze signal is in", "status": "todo", "kind": "gate",
                     "signal": "the freeze lifts", "confirmed_at": "2020-01-01T00:00:00Z", "fresh_hours": 24},
                    {"id": "WS9.1", "title": "First task", "status": "done"},
                    {"id": "WS9.2", "title": "Second task", "status": "todo", "deps": ["WS9.1"], "files": ["api/x.py"]},
                    {"id": "WS9.3", "title": "Third task", "status": "todo", "deps": ["WS9.2"], "files": ["api/x.py"]},
                    {"id": "WS9.4", "title": "Fourth task", "status": "wip", "deps": ["WS9.1"], "files": ["web/y.py"]},
                    {"id": "WS9.5", "title": "Fifth task", "status": "pr", "deps": ["WS9.1"], "files": ["web/y.py"]},
                    {"id": "WS9.6", "title": "Handle the freeze signal", "status": "deferred",
                     "moved_to": "WS8", "note": "waits on the freeze", "received_as": "WS8.1"},
                    {"id": "WS9.7", "title": "Abandoned probe", "status": "deferred"},
                ],
            }
        ]
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        other = self.root / "WS8"
        other.mkdir()
        (other / "bundle.json").write_text(
            json.dumps({"id": "WS8", "title": "Other", "short": "Other", "order": 8, "kind": "workstream"}),
            encoding="utf-8",
        )
        (other / "plan.json").write_text(
            json.dumps(
                {
                    "dag": {"cols": ["Build"], "groups": [{"id": "g", "col": 0, "title": "Group", "tasks": [
                        {"id": "WS8.1", "title": "Handle the freeze signal", "status": "todo"},
                    ]}], "edges": []},
                    "narrative": {"thesis": "One.", "end_state": "Done.", "phases": []},
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(validate_bundle("WS9"), [])

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

    def _json(self, argv):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cmd_ws(argv)
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())

    def test_next_lists_ranked_ready_and_warnings(self):
        payload = self._json(["next", "WS9", "--json"])
        self.assertEqual(payload["ws"], "WS9")
        self.assertEqual([row["id"] for row in payload["ready"]], ["WS9.2", "WS9.0"])
        row = payload["ready"][0]
        self.assertEqual(row["unblocks"], ["WS9.3"])
        self.assertIsNone(row["live_run"])
        self.assertEqual(payload["live"], "unreachable")
        self.assertEqual({row["id"] for row in payload["stalled"]}, {"WS9.4", "WS9.5"})
        self.assertEqual(cmd_ws(["next", "WS9", "--limit", "1"]), 0)
        limited = self._json(["next", "WS9", "--limit", "1", "--json"])
        self.assertEqual(len(limited["ready"]), 1)

    def test_next_exit_codes(self):
        self.assertEqual(cmd_ws(["next", "NOPE"]), 3)
        self.assertEqual(cmd_ws(["next"]), 2)
        self.assertEqual(cmd_ws(["next", "WS9", "--limit", "0"]), 2)
        self.assertEqual(cmd_ws(["next", "WS9", "--limit", "x"]), 2)

    def test_waves_reports_levels_and_path(self):
        payload = self._json(["waves", "WS9", "--json"])
        self.assertEqual(payload["ws"], "WS9")
        self.assertEqual(payload["sizes"], [4, 2, 1, 1])
        self.assertEqual(payload["critical_path"], ["WS9.1", "WS9.2", "WS9.3"])
        self.assertEqual(payload["max_parallel"], 4)
        self.assertEqual(payload["best_span_tasks"], 3)
        self.assertEqual(cmd_ws(["waves", "WS9"]), 0)
        self.assertEqual(cmd_ws(["waves"]), 2)
        self.assertEqual(cmd_ws(["waves", "NOPE"]), 3)

    def test_overlap_declared_all_and_cross(self):
        payload = self._json(["overlap", "WS9", "--json"])
        self.assertEqual(payload["scope"], "wip/pr")
        self.assertEqual(len(payload["pairs"]), 1)
        self.assertEqual(payload["pairs"][0]["source"], "declared")
        scoped = self._json(["overlap", "WS9", "--all", "--json"])
        self.assertEqual(scoped["scope"], "next wave")
        crossed = self._json(["overlap", "--cross", "--json"])
        self.assertTrue(crossed["cross"])
        self.assertEqual(cmd_ws(["overlap", "WS9"]), 0)
        self.assertEqual(cmd_ws(["overlap"]), 2)
        self.assertEqual(cmd_ws(["overlap", "NOPE"]), 3)

    def test_gates_flags_stale(self):
        payload = self._json(["gates", "WS9", "--json"])
        (gate,) = payload["gates"]
        self.assertEqual(gate["id"], "WS9.0")
        self.assertTrue(gate["stale"])
        self.assertEqual(gate["signal"], "the freeze lifts")
        self.assertEqual(cmd_ws(["gates", "WS9"]), 0)
        self.assertEqual(cmd_ws(["gates"]), 2)
        self.assertEqual(cmd_ws(["gates", "NOPE"]), 3)

    def test_deferred_tracks_receiving_end(self):
        payload = self._json(["deferred", "WS9", "--json"])
        rows = {row["id"]: row for row in payload["deferred"]}
        self.assertEqual(rows["WS9.6"]["received"], "WS8.1")
        self.assertFalse(rows["WS9.6"]["orphan"])
        self.assertTrue(rows["WS9.7"]["orphan"])
        rolled = self._json(["deferred", "--json"])
        self.assertTrue(any(row["id"] == "WS9.6" for row in rolled["deferred"]))
        self.assertEqual(cmd_ws(["deferred", "WS9"]), 0)
        self.assertEqual(cmd_ws(["deferred"]), 0)
        self.assertEqual(cmd_ws(["deferred", "NOPE"]), 3)

    def test_invalid_plan_is_invalid_data(self):
        (self.root / "WS9" / "plan.json").write_text("{nope", encoding="utf-8")
        self.assertEqual(cmd_ws(["waves", "WS9"]), 4)
        self.assertEqual(cmd_ws(["next", "WS9"]), 4)

    def test_new_task_fields_validate(self):
        plan_path = self.root / "WS9" / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        tasks = plan["dag"]["groups"][0]["tasks"]
        tasks[1]["files"] = "api/x.py"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        self.assertTrue(any("files" in issue for issue in validate_bundle("WS9")))
        tasks[1].pop("files")
        tasks[0]["confirmed_at"] = "not a date"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        self.assertTrue(any("confirmed_at" in issue for issue in validate_bundle("WS9")))
        tasks[0]["confirmed_at"] = "2020-01-01T00:00:00Z"
        tasks[0]["fresh_hours"] = -1
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        self.assertTrue(any("fresh_hours" in issue for issue in validate_bundle("WS9")))
        tasks[0]["fresh_hours"] = 24
        tasks[6]["received_as"] = ""
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        self.assertTrue(any("received_as" in issue for issue in validate_bundle("WS9")))


class WsLayoutCliTest(unittest.TestCase):
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

    def _write_plan(self, ws: str, dag: dict, trains=None) -> None:
        from dash.bundles import bundle_dir

        path = bundle_dir(ws)
        assert path is not None
        plan = json.loads((path / "plan.json").read_text(encoding="utf-8"))
        plan["dag"] = dag
        plan["trains"] = trains if trains is not None else []
        (path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

    def _rich_dag(self) -> dict:
        return {
            "cols": ["Shape", "Build", "Prove"],
            "groups": [
                {
                    "id": "g1",
                    "col": 0,
                    "title": "First stage group",
                    "tasks": [
                        {"id": f"WS9.{pos}", "title": f"Task {pos}", "status": "done"}
                        for pos in range(1, 16)
                    ],
                },
                {
                    "id": "g2",
                    "col": 1,
                    "title": "Second stage group",
                    "tasks": [
                        {"id": "WS9.16", "title": "Task 16", "status": "todo", "deps": ["WS9.1"]},
                        {"id": "WS9.17", "title": "Task 17", "status": "todo", "deps": ["WS9.16"]},
                    ],
                },
                {
                    "id": "g3",
                    "col": 2,
                    "title": "Third stage group",
                    "tasks": [
                        {"id": "WS9.18", "title": "Task 18", "status": "todo", "deps": ["WS9.17"]},
                        {"id": "WS9.19", "title": "Task 19", "status": "todo", "deps": ["WS9.18"]},
                    ],
                },
            ],
            "edges": [["g1", "g2"], ["g2", "g3"]],
        }

    def test_template_has_zero_map_warnings(self):
        from dash.bundles import plan_map_warnings

        self.assertEqual(cmd_ws(["new", "WS9", "Sample"]), 0)
        self.assertEqual(plan_map_warnings("WS9"), [])
        self.assertEqual(cmd_ws(["validate", "WS9"]), 0)

    def test_layout_json_matches_layout_object(self):
        import contextlib
        import io

        from dash import maplayout
        from dash.bundles import bundle_plan_view

        self.assertEqual(cmd_ws(["new", "WS9", "Sample"]), 0)
        self._write_plan("WS9", self._rich_dag())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cmd_ws(["layout", "WS9", "--json"]), 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["ws"], "WS9")
        body = bundle_plan_view("WS9")
        assert body is not None
        self.assertEqual(payload["layout"], maplayout.layout(body))
        self.assertEqual(payload["map_warnings"], maplayout.map_warnings(body))
        self.assertEqual(payload["layout"]["density"], "comfortable")
        codes = [warn["code"] for warn in payload["map_warnings"]]
        self.assertIn("MAP101", codes)

    def test_layout_human_names_modes_and_warnings(self):
        import contextlib
        import io

        self.assertEqual(cmd_ws(["new", "WS9", "Sample"]), 0)
        self._write_plan(
            "WS9",
            self._rich_dag(),
            trains=[{"id": "solo", "title": "Solo train", "members": ["WS9.1"], "status": "planned"}],
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cmd_ws(["layout", "WS9"]), 0)
        text = buf.getvalue()
        self.assertIn("comfortable density", text)
        self.assertIn("arrows all", text)
        self.assertIn("labels:", text)
        self.assertIn("warning MAP101:", text)
        self.assertIn("warning MAP107:", text)
        self.assertIn("Fix:", text)

    def test_layout_exit_codes(self):
        self.assertEqual(cmd_ws(["layout", "NOPE"]), 3)
        self.assertEqual(cmd_ws(["layout"]), 2)
        self.assertEqual(cmd_ws(["new", "WS9", "Sample"]), 0)
        (self.root / "WS9" / "plan.json").write_text("{nope", encoding="utf-8")
        self.assertEqual(cmd_ws(["layout", "WS9"]), 4)

    def test_validate_warnings_keep_exit_zero(self):
        import contextlib
        import io

        self.assertEqual(cmd_ws(["new", "WS9", "Sample"]), 0)
        self._write_plan(
            "WS9",
            self._rich_dag(),
            trains=[{"id": "solo", "title": "Solo train", "members": ["WS9.1"], "status": "planned"}],
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cmd_ws(["validate", "WS9"]), 0)
        text = buf.getvalue()
        self.assertIn("warnings:", text)
        self.assertIn("warning MAP101:", text)
        self.assertIn("warning MAP107:", text)
        self.assertIn("OK WS9", text)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cmd_ws(["validate", "WS9", "--json"]), 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["issues"], [])
        codes = [warn["code"] for warn in payload["warnings"]]
        self.assertIn("MAP101", codes)
        self.assertIn("MAP107", codes)

    def test_validate_errors_still_fail_with_warnings(self):
        import contextlib
        import io

        self.assertEqual(cmd_ws(["new", "WS9", "Sample"]), 0)
        plan_path = self.root / "WS9" / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["dag"] = self._rich_dag()
        plan["dag"]["groups"][0]["tasks"][0]["deps"] = ["WS9.missing"]
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cmd_ws(["validate", "WS9"]), 4)
        self.assertIn("warning MAP101:", buf.getvalue())

    def test_layout_failure_warns_and_keeps_exit_zero(self):
        import contextlib
        import io
        from unittest import mock

        self.assertEqual(cmd_ws(["new", "WS9", "Sample"]), 0)
        boom = ValueError("layout boom")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("dash.maplayout.layout", side_effect=boom), mock.patch("dash.maplayout.map_warnings", side_effect=boom):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(cmd_ws(["layout", "WS9"]), 0)
                self.assertEqual(cmd_ws(["validate", "WS9"]), 0)
            js = io.StringIO()
            with contextlib.redirect_stdout(js), contextlib.redirect_stderr(err):
                self.assertEqual(cmd_ws(["layout", "WS9", "--json"]), 0)
        self.assertIn("warning: map layout unavailable (ValueError)", err.getvalue())
        payload = json.loads(js.getvalue())
        self.assertIsNone(payload["layout"])
        self.assertEqual(payload["map_warnings"], [])



class WsLinearCliTest(unittest.TestCase):
    """Linear hooks on the ws write path, the ws linear CLI and ws trains refs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workstreams"
        self.root.mkdir()
        self.env_old = {name: os.environ.get(name) for name in ("AGENT_DASH_WORKSTREAMS", "AGENT_DASH_DATA_ROOT")}
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.root)
        os.environ["AGENT_DASH_DATA_ROOT"] = str(Path(self.tmp.name) / "data")
        bundle = self.root / "WS9"
        bundle.mkdir()
        (bundle / "bundle.json").write_text(json.dumps({"id": "WS9", "title": "Notes cutover", "state": "active"}), encoding="utf-8")
        plan = {
            "workstream": "WS9",
            "dag": {
                "cols": ["Shape"],
                "groups": [
                    {
                        "id": "g1",
                        "col": 0,
                        "title": "Shape",
                        "tasks": [
                            {"id": "WS9.1", "title": "First", "status": "todo", "deps": []},
                            {"id": "WS9.2", "title": "Second", "status": "todo", "deps": []},
                        ],
                    }
                ],
                "edges": [],
            },
            "trains": [],
        }
        (bundle / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        (bundle / "approvals.json").write_text(
            json.dumps({"ws": "WS9", "decided_at": None, "decided_by": None, "items": []}), encoding="utf-8"
        )

    def tearDown(self):
        for name, value in self.env_old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def _link_file(self, tasks=None):
        record = {
            "version": 1,
            "team_key": "ENG",
            "project": {"id": "p", "url": "https://linear.app/project/p", "name": "Notes cutover"},
            "milestones": {},
            "tasks": tasks or {},
            "approvals": {},
            "relations": [],
            "spec_hash": "x",
            "cursor": None,
            "linked_at": 1.0,
            "pushed_at": 1.0,
            "synced_at": 1.0,
            "last_error": None,
        }
        linear.write_link("WS9", record, self.root)

    def _outbox(self):
        path = self.root / "WS9" / linear.OUTBOX_FILE
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def _history(self):
        return [json.loads(line) for line in (self.root / "WS9" / "history.jsonl").read_text(encoding="utf-8").splitlines()]

    def _run(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            rc = cmd_ws(argv)
        return rc, out.getvalue()

    def test_local_set_task_enqueues_only_when_linked(self):
        with contextlib.redirect_stdout(io.StringIO()):
            set_task("WS9", "WS9.1", "wip", None, None, root=self.root)
        self.assertEqual(self._outbox(), [])
        self._link_file()
        with contextlib.redirect_stdout(io.StringIO()):
            set_task("WS9", "WS9.1", "pr", None, None, root=self.root)
        self.assertEqual([(row["kind"], row["key"]) for row in self._outbox()], [("task", "WS9.1")])

    def test_linear_origin_sets_history_by_and_does_not_enqueue(self):
        self._link_file()
        with contextlib.redirect_stdout(io.StringIO()):
            set_task("WS9", "WS9.1", "wip", None, None, root=self.root)
            self.assertEqual(self._history()[-1].get("by"), None)
            set_task("WS9", "WS9.2", "done", None, None, root=self.root, force=True, origin="linear")
        last = self._history()[-1]
        self.assertEqual(last["by"], "linear")
        self.assertEqual(last["task"], "WS9.2")
        self.assertEqual([row["key"] for row in self._outbox()], ["WS9.1"])

    def test_set_approval_queues_no_linear_issue_when_linked(self):
        """Approvals are a decision log: a logged step never creates a Linear ask."""
        self._link_file()
        with contextlib.redirect_stdout(io.StringIO()):
            bundles.set_approval("WS9", "A1", "pending", "Deploy tier", True, "Prod window", None, root=self.root)
            bundles.set_approval("WS9", "A1", "approved", "Deploy tier", True, "Prod window", None, root=self.root)
        self.assertEqual(self._outbox(), [])

    def test_rollback_and_decider_are_recorded_per_item(self):
        self._link_file()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = bundles.set_approval("WS9", "A1", "approved", "Deploy tier", True, "Prod window", None,
                                      root=self.root, rollback="revert commit abc123", by="lead")
        self.assertEqual(rc, 0)
        data = json.loads((self.root / "WS9" / "approvals.json").read_text(encoding="utf-8"))
        item = data["items"][0]
        self.assertEqual(item["rollback"], "revert commit abc123")
        self.assertEqual(item["decided_by"], "lead")
        self.assertTrue(item["decided_at"])
        self.assertEqual(data["decided_by"], "lead")

    def test_pending_item_has_no_decider_or_time(self):
        with contextlib.redirect_stdout(io.StringIO()):
            bundles.set_approval("WS9", "A1", "pending", "Deploy tier", True, "Prod window", None, root=self.root)
        item = json.loads((self.root / "WS9" / "approvals.json").read_text(encoding="utf-8"))["items"][0]
        self.assertEqual(item["status"], "pending")
        self.assertNotIn("decided_at", item)
        self.assertNotIn("decided_by", item)

    def test_ws_approve_flags_rollback_and_by(self):
        self._link_file()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self._run(["approve", "WS9", "A1", "approved", "--step", "Deploy tier", "--reversible", "yes",
                                        "--conditions", "Window open", "--rollback", "revert", "--by", "lead"])[0], 0)
        item = json.loads((self.root / "WS9" / "approvals.json").read_text(encoding="utf-8"))["items"][0]
        self.assertEqual(item["rollback"], "revert")
        self.assertEqual(item["decided_by"], "lead")

    def test_decided_by_defaults_to_lead_and_names_linear_for_linear_origin(self):
        self._link_file()
        with contextlib.redirect_stdout(io.StringIO()):
            bundles.set_approval("WS9", "A1", "approved", "Deploy tier", True, "Prod window", None, root=self.root)
        data = json.loads((self.root / "WS9" / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual(data["decided_by"], "lead")
        with contextlib.redirect_stdout(io.StringIO()):
            bundles.set_approval("WS9", "A2", "denied", "Canary", False, "", None, root=self.root, origin="linear")
        data = json.loads((self.root / "WS9" / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual(data["decided_by"], "linear")

    def test_ws_linear_usage_errors(self):
        self.assertEqual(self._run(["linear"])[0], errors.USAGE)
        self.assertEqual(self._run(["linear", "link"])[0], errors.USAGE)
        rc, _ = self._run(["linear", "link", "WS9"])
        self.assertEqual(rc, errors.USAGE)
        rc, _ = self._run(["linear", "bogus", "WS9"])
        self.assertEqual(rc, errors.USAGE)
        rc, _ = self._run(["linear", "status", "WS9", "--team", "ENG"])
        self.assertEqual(rc, errors.USAGE)
        rc, _ = self._run(["linear", "status", "NOPE"])
        self.assertEqual(rc, errors.NOT_FOUND)

    def test_ws_trains_prints_refs_when_linked(self):
        rc, _ = self._run(["train", "WS9", "t1", "--title", "Doctor cut", "--add", "WS9.1,WS9.2"])
        self.assertEqual(rc, 0)
        rc, plain = self._run(["trains", "WS9"])
        self.assertEqual(rc, 0)
        self.assertNotIn("LINEAR", plain)
        self._link_file({"WS9.1": {"id": "a", "identifier": "ENG-1", "url": "u"}, "WS9.2": {"id": "b", "identifier": "ENG-2", "url": "u"}})
        rc, linked = self._run(["trains", "WS9"])
        self.assertEqual(rc, 0)
        self.assertIn("LINEAR", linked.splitlines()[0])
        self.assertIn("Fixes ENG-1, ENG-2", linked)


class WsLinearCommentCliTest(unittest.TestCase):
    """``ws linear comment <WS> <TASK-ID> <TEXT...>``: queue an agent comment for the next sync."""

    setUp = WsLinearCliTest.setUp
    tearDown = WsLinearCliTest.tearDown
    _link_file = WsLinearCliTest._link_file
    _outbox = WsLinearCliTest._outbox
    _run = WsLinearCliTest._run

    def test_comment_queues_when_linked(self):
        self._link_file()
        rc, out = self._run(["linear", "comment", "WS9", "WS9.1", "verdict", "PASS", "PR", "#7"])
        self.assertEqual(rc, errors.OK)
        self.assertEqual(out, "queued comment for WS9.1; it posts on the next sync\n")
        self.assertEqual([(row["kind"], row["key"], row["body"]) for row in self._outbox()], [("comment", "WS9.1", "verdict PASS PR #7")])

    def test_refuses_when_unlinked(self):
        rc, out = self._run(["linear", "comment", "WS9", "WS9.1", "hello"])
        self.assertEqual(rc, errors.USAGE)
        self.assertIn("not linked", out)
        self.assertEqual(self._outbox(), [])

    def test_refuses_unknown_task(self):
        self._link_file()
        rc, out = self._run(["linear", "comment", "WS9", "WS9.99", "hello"])
        self.assertEqual(rc, errors.NOT_FOUND)
        self.assertIn("WS9.99", out)
        self.assertEqual(self._outbox(), [])

    def test_refuses_empty_and_too_long_text(self):
        self._link_file()
        self.assertEqual(self._run(["linear", "comment", "WS9", "WS9.1"])[0], errors.USAGE)
        rc, out = self._run(["linear", "comment", "WS9", "WS9.1", "   "])
        self.assertEqual(rc, errors.USAGE)
        self.assertIn("empty", out)
        rc, out = self._run(["linear", "comment", "WS9", "WS9.1", "x" * 2001])
        self.assertEqual(rc, errors.USAGE)
        self.assertIn("2001", out)
        self.assertEqual(self._outbox(), [])

    def test_refuses_unknown_bundle(self):
        self._link_file()
        self.assertEqual(self._run(["linear", "comment", "NOPE", "WS9.1", "hi"])[0], errors.NOT_FOUND)
        self.assertEqual(self._outbox(), [])


class WsLinearCommentTextTest(unittest.TestCase):
    """Comment text after ``<WS> <TASK-ID>`` is verbatim: option-looking words are never parsed."""

    setUp = WsLinearCliTest.setUp
    tearDown = WsLinearCliTest.tearDown
    _link_file = WsLinearCliTest._link_file
    _outbox = WsLinearCliTest._outbox
    _run = WsLinearCliTest._run

    def _bodies(self):
        return [row["body"] for row in self._outbox()]

    def test_trailing_json_stays_in_text(self):
        self._link_file()
        rc, _ = self._run(["linear", "comment", "WS9", "WS9.1", "fine", "text", "--json"])
        self.assertEqual(rc, errors.OK)
        self.assertEqual(self._bodies(), ["fine text --json"])

    def test_team_flag_stays_in_text(self):
        self._link_file()
        rc, _ = self._run(["linear", "comment", "WS9", "WS9.1", "deploy", "with", "--team", "X"])
        self.assertEqual(rc, errors.OK)
        self.assertEqual(self._bodies(), ["deploy with --team X"])

    def test_dry_run_stays_in_text(self):
        self._link_file()
        rc, _ = self._run(["linear", "comment", "WS9", "WS9.1", "--dry-run", "kept"])
        self.assertEqual(rc, errors.OK)
        self.assertEqual(self._bodies(), ["--dry-run kept"])

    def test_double_dash_ends_options_and_is_dropped(self):
        self._link_file()
        rc, _ = self._run(["linear", "comment", "WS9", "WS9.1", "--", "-- leading dash text"])
        self.assertEqual(rc, errors.OK)
        self.assertEqual(self._bodies(), ["-- leading dash text"])

    def test_leading_dash_text_after_double_dash(self):
        self._link_file()
        rc, _ = self._run(["linear", "comment", "WS9", "WS9.1", "--", "-x", "--json"])
        self.assertEqual(rc, errors.OK)
        self.assertEqual(self._bodies(), ["-x --json"])


if __name__ == "__main__":
    unittest.main()
