import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash.bundles import cmd_ws, naming_issues, set_task, validate_bundle


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
        self.assertEqual(set_task("WS3", "missing", "done", None, None), 2)
        self.assertEqual(cmd_ws(["task", "WS3", "WS3.1", "nope"]), 2)

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
        self.assertEqual(cmd_ws(["approve", "WS9", "sample-load", "approved", "--note", "Owner signed off."]), 0)
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
        self.assertEqual(cmd_ws(["approvals", "NOPE"]), 2)


if __name__ == "__main__":
    unittest.main()
