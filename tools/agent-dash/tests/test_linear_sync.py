import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import bundles, errors, linear, linear_sync
from dash.linear_client import LinearError
from dash.linear_sync import link, linked_bundles, status, sync, train_refs, unlink

PLAN = {
    "workstream": "WS9",
    "dag": {
        "cols": ["Shape", "Build"],
        "groups": [
            {
                "id": "g1",
                "col": 0,
                "title": "Shape",
                "tasks": [
                    {"id": "WS9.1", "title": "First task", "status": "todo", "deps": [], "size": "S", "files": ["a.py"], "test_cmd": "python3 -m unittest x"},
                    {"id": "WS9.2", "title": "Second task", "status": "wip", "deps": ["WS9.1"]},
                ],
            },
            {
                "id": "g2",
                "col": 1,
                "title": "Build",
                "tasks": [
                    {"id": "WS9.3", "title": "Third task", "status": "todo", "deps": ["WS9.2"]},
                    {"id": "WS9.4", "title": "Fourth task", "status": "deferred", "deps": [], "moved_to": "WS8", "note": "moved"},
                ],
            },
        ],
        "edges": [],
    },
    "trains": [{"id": "t1", "title": "Doctor cut", "members": ["WS9.1", "WS9.2"], "status": "planned"}],
}

APPROVALS = {
    "ws": "WS9",
    "decided_at": None,
    "decided_by": None,
    "items": [
        {"id": "A1", "step": "Deploy tier", "status": "pending", "reversible": True, "conditions": "Prod window"},
        {"id": "A2", "step": "Old gate", "status": "approved", "reversible": False, "conditions": ""},
    ],
}


def _write_bundle(root: Path) -> None:
    bundle = root / "WS9"
    bundle.mkdir(parents=True)
    (bundle / "bundle.json").write_text(json.dumps({"id": "WS9", "title": "Notes cutover", "state": "active"}), encoding="utf-8")
    (bundle / "plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
    (bundle / "approvals.json").write_text(json.dumps(APPROVALS), encoding="utf-8")


class FakeLinear:
    """In-memory stand-in for dash.linear_client.Client. Records every write."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.count = 0
        self.clock = 0
        self.team_states = {
            "Todo": ("s-todo", "unstarted"),
            "In Progress": ("s-wip", "started"),
            "In Review": ("s-pr", "started"),
            "Done": ("s-done", "completed"),
            "Canceled": ("s-canceled", "canceled"),
        }
        self.issues: dict[str, dict] = {}
        self.fail_pull: Exception | None = None
        self.on_state = None

    def _tick(self) -> str:
        self.clock += 1
        return f"2026-10-21T10:00:00.{self.clock:06d}Z"

    def _state(self, state_id: str) -> tuple[str, str]:
        for name, (sid, kind) in self.team_states.items():
            if sid == state_id:
                return name, kind
        raise KeyError(state_id)

    def _row(self, issue: dict) -> dict:
        name, kind = self._state(issue["state_id"])
        return {
            "id": issue["id"],
            "identifier": issue["identifier"],
            "url": issue["url"],
            "updatedAt": issue["updatedAt"],
            "state": {"name": name, "type": kind},
        }

    def calls_named(self, kind: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == kind]

    def by_identifier(self, identifier: str) -> dict:
        return next(issue for issue in self.issues.values() if issue["identifier"] == identifier)

    def linear_move(self, identifier: str, state_name: str) -> None:
        """Simulate a status change made in Linear."""
        issue = self.by_identifier(identifier)
        issue["state_id"] = self.team_states[state_name][0]
        issue["updatedAt"] = self._tick()

    def team(self, key: str) -> dict:
        self.calls.append(("team", key))
        return {
            "id": "team-1",
            "key": key,
            "states": [{"id": sid, "name": name, "type": kind} for name, (sid, kind) in self.team_states.items()],
        }

    def ensure_state(self, team_id, state_id, name, state_type, color):
        self.calls.append(("ensure_state", name, state_type, color))
        if name in self.team_states:
            return self.team_states[name][0]
        self.team_states[name] = (state_id, state_type)
        return state_id

    def ensure_label(self, team_id, name):
        self.calls.append(("ensure_label", name))
        return "label-approval"

    def ensure_initiative(self, name):
        self.calls.append(("ensure_initiative", name))
        return {"id": "init-1", "name": name, "url": "https://linear.app/initiative/1"}

    def ensure_project(self, project_id, name, team_id, description):
        self.calls.append(("project", name))
        return {"id": project_id, "url": "https://linear.app/project/" + project_id[:8], "name": name}

    def add_project_to_initiative(self, initiative_id, project_id):
        self.calls.append(("initiative_link", initiative_id, project_id))

    def ensure_milestone(self, milestone_id, project_id, name, sort_order):
        self.calls.append(("milestone", name, sort_order))
        return milestone_id

    def upsert_issue(self, issue_id, team_id, project_id, milestone_id, title, description, state_id=None, label_ids=None):
        issue = self.issues.get(issue_id)
        if issue is None:
            self.count += 1
            issue = {
                "id": issue_id,
                "identifier": f"ENG-{self.count}",
                "url": f"https://linear.app/team/ENG/issue/ENG-{self.count}",
                "state_id": state_id,
                "project": project_id,
            }
            self.issues[issue_id] = issue
            self.calls.append(("create", issue["identifier"], state_id))
        else:
            self.calls.append(("update", issue["identifier"]))
        issue.update(title=title, description=description, milestone=milestone_id, labels=label_ids or [])
        issue["updatedAt"] = self._tick()
        return {"id": issue_id, "identifier": issue["identifier"], "url": issue["url"]}

    def set_issue_state(self, issue_id, state_id):
        self.calls.append(("state", issue_id, state_id))
        issue = self.issues[issue_id]
        issue["state_id"] = state_id
        issue["updatedAt"] = self._tick()
        hook, self.on_state = self.on_state, None
        if hook is not None:
            hook(issue_id)

    def ensure_blocks(self, blocker_issue_id, blocked_issue_id):
        self.calls.append(("blocks", blocker_issue_id, blocked_issue_id))

    def comment(self, issue_id, body):
        self.calls.append(("comment", issue_id, body))

    def issues_updated_since(self, project_id, since_iso):
        if self.fail_pull is not None:
            raise self.fail_pull
        rows = [
            self._row(issue)
            for issue in self.issues.values()
            if issue["project"] == project_id and (since_iso is None or issue["updatedAt"] > since_iso)
        ]
        return sorted(rows, key=lambda row: row["updatedAt"])


class LinearSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "workstreams"
        self.root.mkdir()
        _write_bundle(self.root)
        self.env_old = {name: os.environ.get(name) for name in ("AGENT_DASH_DATA_ROOT", linear.ENV_KEY)}
        os.environ["AGENT_DASH_DATA_ROOT"] = str(self.base / "data")
        os.environ[linear.ENV_KEY] = "lin_test_key"
        self.fake = FakeLinear()
        self.now = 1000.0

    def tearDown(self):
        for name, value in self.env_old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def _quiet(self):
        return contextlib.redirect_stdout(io.StringIO())

    def _link(self):
        with self._quiet() as out:
            rc = link("WS9", "ENG", root=self.root, client=self.fake, now=self.now)
        self.assertEqual(rc, 0)
        return out.getvalue()

    def _sync(self, now=None):
        return sync("WS9", root=self.root, client=self.fake, now=self.now if now is None else now)

    def _plan(self) -> dict:
        return json.loads((self.root / "WS9" / "plan.json").read_text(encoding="utf-8"))

    def _task(self, task_id: str) -> dict:
        for group in self._plan()["dag"]["groups"]:
            for task in group["tasks"]:
                if task["id"] == task_id:
                    return task
        raise KeyError(task_id)

    def _link_record(self) -> dict:
        return linear.read_link("WS9", self.root)

    def _ident(self, task_id: str) -> str:
        return self._link_record()["tasks"][task_id]["identifier"]

    def _history(self) -> list[dict]:
        path = self.root / "WS9" / "history.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def _approval_status(self, item_id: str) -> str:
        data = json.loads((self.root / "WS9" / "approvals.json").read_text(encoding="utf-8"))
        return next(item["status"] for item in data["items"] if item["id"] == item_id)


class LinkTest(LinearSyncTest):
    def test_link_refuses_without_key(self):
        del os.environ[linear.ENV_KEY]
        with self._quiet() as out:
            rc = link("WS9", "ENG", root=self.root, client=self.fake, now=self.now)
        self.assertEqual(rc, errors.USAGE)
        self.assertIn(linear.ENV_KEY, out.getvalue())
        self.assertFalse(linear.is_linked("WS9", self.root))
        self.assertEqual(self.fake.calls, [])

    def test_link_creates_workspace_project_milestones_issues_and_relations(self):
        out = self._link()
        self.assertIn("https://linear.app/project/", out)

        creates = self.fake.calls_named("create")
        self.assertEqual([call[1] for call in creates], ["ENG-1", "ENG-2", "ENG-3", "ENG-4", "ENG-5"])
        self.assertEqual(len(self.fake.calls_named("milestone")), 2)
        ids = {key: self._link_record()["tasks"][key]["id"] for key in ("WS9.1", "WS9.2", "WS9.3")}
        self.assertEqual(
            sorted(call[1:] for call in self.fake.calls_named("blocks")),
            sorted([(ids["WS9.1"], ids["WS9.2"]), (ids["WS9.2"], ids["WS9.3"])]),
        )
        record = self._link_record()
        self.assertEqual(sorted(record["relations"]), ["WS9.1>WS9.2", "WS9.2>WS9.3"])
        self.assertEqual(set(record["tasks"]), {"WS9.1", "WS9.2", "WS9.3", "WS9.4"})
        self.assertEqual(set(record["approvals"]), {"A1"})
        self.assertEqual(record["linked_at"], self.now)
        self.assertEqual(record["synced_at"], self.now)
        self.assertIsNone(record["last_error"])
        self.assertTrue(record["spec_hash"])
        self.assertEqual(record["milestones"].keys(), {"Shape", "Build"})

        workspace = linear.read_workspace()
        self.assertEqual(set(workspace["states"]), {"Todo", "In Progress", "In Review", "Awaiting prod", "Done", "Canceled"})
        self.assertEqual(workspace["approval_label"], "label-approval")
        self.assertIn(("ensure_state", "Awaiting prod", "started", "#a855f7"), self.fake.calls)

    def test_only_pending_approvals_get_issues_and_the_label(self):
        self._link()
        approval_issue = self.fake.issues[linear.stable_id("approval", "WS9", "A1")]
        self.assertEqual(approval_issue["labels"], ["label-approval"])
        self.assertEqual(approval_issue["state_id"], "s-todo")
        self.assertNotIn(linear.stable_id("approval", "WS9", "A2"), self.fake.issues)

    def test_deferred_task_gets_one_comment_at_creation(self):
        self._link()
        comments = self.fake.calls_named("comment")
        self.assertEqual(len(comments), 1)
        self.assertIn("Moved to WS8", comments[0][2])

    def test_relink_and_sync_send_no_creates(self):
        self._link()
        self.fake.calls.clear()
        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        self.assertEqual(result["pushed"], 0)
        self.assertEqual(self.fake.calls_named("create"), [])

        self.fake.calls.clear()
        with self._quiet():
            rc = link("WS9", "ENG", root=self.root, client=self.fake, now=self.now + 2)
        self.assertEqual(rc, 0)
        self.assertEqual(self.fake.calls_named("create"), [])
        self.assertEqual(self.fake.calls_named("blocks"), [])
        self.assertEqual(self.fake.calls_named("comment"), [])

    def test_spec_change_repushes_once(self):
        self._link()
        plan = self._plan()
        plan["dag"]["groups"][1]["tasks"][0]["title"] = "Third task renamed"
        (self.root / "WS9" / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

        self.fake.calls.clear()
        result = self._sync(self.now + 1)
        self.assertEqual(self.fake.calls_named("create"), [])
        self.assertGreater(result["pushed"], 0)
        issue = self.fake.issues[linear.stable_id("task", "WS9", "WS9.3")]
        self.assertEqual(issue["title"], "Third task renamed")

        self.fake.calls.clear()
        self.assertEqual(self._sync(self.now + 2)["pushed"], 0)
        self.assertEqual(self.fake.calls_named("update"), [])


class SyncTest(LinearSyncTest):
    def setUp(self):
        super().setUp()
        self._link()

    def test_flush_sends_current_status_once_and_drains(self):
        with self._quiet():
            bundles.set_task("WS9", "WS9.3", "wip", None, None, root=self.root)
            bundles.set_task("WS9", "WS9.3", "pr", None, None, root=self.root)
        self.assertEqual(len(linear.read_outbox("WS9", self.root)), 2)

        self.fake.calls.clear()
        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        self.assertEqual(result["flushed"], 1)
        issue_id = linear.stable_id("task", "WS9", "WS9.3")
        states = [call for call in self.fake.calls_named("state") if call[1] == issue_id]
        self.assertEqual(states, [("state", issue_id, "s-pr")])
        self.assertEqual(linear.read_outbox("WS9", self.root), [])
        self.assertEqual(result["applied"], 0)

    def test_pull_applies_linear_change_with_history_and_no_outbox_row(self):
        self.fake.linear_move(self._ident("WS9.2"), "Done")
        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self._task("WS9.2")["status"], "done")
        last = self._history()[-1]
        self.assertEqual(last["by"], "linear")
        self.assertEqual(last["task"], "WS9.2")
        self.assertEqual(last["to"], "done")
        self.assertEqual(linear.read_outbox("WS9", self.root), [])

    def test_local_write_during_flush_is_not_lost(self):
        with self._quiet():
            bundles.set_task("WS9", "WS9.3", "wip", None, None, root=self.root)

        def concurrent(_issue_id):
            with self._quiet():
                bundles.set_task("WS9", "WS9.3", "pr", None, None, root=self.root)

        self.fake.on_state = concurrent
        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        self.assertEqual(self._task("WS9.3")["status"], "pr")
        self.assertEqual(len(linear.read_outbox("WS9", self.root)), 1)

        result = self._sync(self.now + 2)
        self.assertIsNone(result["error"])
        issue = self.fake.issues[linear.stable_id("task", "WS9", "WS9.3")]
        self.assertEqual(issue["state_id"], "s-pr")
        self.assertEqual(self._task("WS9.3")["status"], "pr")
        self.assertEqual(linear.read_outbox("WS9", self.root), [])

    def test_flush_reads_plan_after_outbox_so_a_racing_write_is_sent(self):
        with self._quiet():
            bundles.set_task("WS9", "WS9.3", "wip", None, None, root=self.root)
        real_plan = linear_sync._plan
        fired = []

        def racing_plan(ws, root):
            if sys._getframe(1).f_code.co_name == "_flush" and not fired:
                fired.append(True)
                with self._quiet():
                    bundles.set_task("WS9", "WS9.3", "pr", None, None, root=self.root)
            return real_plan(ws, root)

        linear_sync._plan = racing_plan
        try:
            result = self._sync(self.now + 1)
        finally:
            linear_sync._plan = real_plan
        self.assertIsNone(result["error"])
        self.assertEqual(fired, [True])
        issue = self.fake.issues[linear.stable_id("task", "WS9", "WS9.3")]
        self.assertEqual(issue["state_id"], "s-pr")
        self.assertEqual(len(linear.read_outbox("WS9", self.root)), 1)

    def test_pull_does_not_overwrite_pending_local_change(self):
        cursor_before = self._link_record()["cursor"]
        with self._quiet():
            bundles.set_task("WS9", "WS9.2", "done", None, None, root=self.root)
        self.fake.linear_move(self._ident("WS9.2"), "In Review")
        record = self._link_record()
        with self._quiet():
            mapped, applied = linear_sync._pull(self.fake, "WS9", self.root, record)
        self.assertEqual((mapped, applied), (1, 0))
        self.assertEqual(self._task("WS9.2")["status"], "done")
        self.assertEqual(len(linear.read_outbox("WS9", self.root)), 1)
        self.assertEqual(record["cursor"], cursor_before)

        result = self._sync(self.now + 5)
        self.assertIsNone(result["error"])
        issue = self.fake.issues[linear.stable_id("task", "WS9", "WS9.2")]
        self.assertEqual(issue["state_id"], "s-done")
        self.assertEqual(self._task("WS9.2")["status"], "done")
        self.assertEqual(linear.read_outbox("WS9", self.root), [])

    def test_pull_of_unchanged_status_is_a_noop(self):
        result = self._sync(self.now + 1)
        self.assertEqual(result["applied"], 0)
        self.assertEqual(self._history(), [])

    def test_linear_canceled_maps_to_deferred_with_note(self):
        self.fake.linear_move(self._ident("WS9.1"), "Canceled")
        result = self._sync(self.now + 1)
        self.assertEqual(result["applied"], 1)
        task = self._task("WS9.1")
        self.assertEqual(task["status"], "deferred")
        self.assertEqual(task["note"], "canceled in Linear")
        self.assertEqual(linear.read_outbox("WS9", self.root), [])

    def test_approval_done_maps_to_approved(self):
        self.fake.linear_move(self._link_record()["approvals"]["A1"]["identifier"], "Done")
        result = self._sync(self.now + 1)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self._approval_status("A1"), "approved")
        self.assertEqual(linear.read_outbox("WS9", self.root), [])

    def test_failed_sync_records_last_error_and_keeps_synced_at(self):
        self.fake.fail_pull = LinearError("RATELIMITED", "Too many requests")
        result = self._sync(self.now + 50)
        self.assertIsNotNone(result["error"])
        self.assertIn("RATELIMITED", result["error"])
        record = self._link_record()
        self.assertIn("RATELIMITED", record["last_error"])
        self.assertEqual(record["synced_at"], self.now)

        self.fake.fail_pull = LinearError("GRAPHQL", f"token {linear.api_key()} rejected")
        self._sync(self.now + 60)
        self.assertNotIn("lin_test_key", self._link_record()["last_error"])
        self.assertIn("[redacted]", self._link_record()["last_error"])

        self.fake.fail_pull = ValueError("boom")
        self.assertIn("ValueError", self._sync(self.now + 70)["error"])

        self.fake.fail_pull = None
        ok = self._sync(self.now + 100)
        self.assertIsNone(ok["error"])
        record = self._link_record()
        self.assertIsNone(record["last_error"])
        self.assertEqual(record["synced_at"], self.now + 100)

    def test_sync_on_unlinked_bundle_does_not_raise(self):
        with self._quiet():
            unlink("WS9", root=self.root)
        result = self._sync(self.now + 1)
        self.assertIn("not linked", result["error"])

    def test_status_json_reports_the_link(self):
        with self._quiet() as out:
            rc = status("WS9", root=self.root, as_json=True)
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["linked"])
        self.assertEqual(payload["linked_tasks"], 4)
        self.assertEqual(payload["outbox"], 0)
        self.assertIsNotNone(payload["url"])

    def test_linked_bundles_lists_only_linked(self):
        self.assertEqual(linked_bundles(self.root), ["WS9"])
        with self._quiet():
            unlink("WS9", root=self.root)
        self.assertEqual(linked_bundles(self.root), [])


class UnlinkAndTrainsTest(LinearSyncTest):
    def test_unlink_removes_files_and_keeps_linear_data(self):
        self._link()
        with self._quiet():
            bundles.set_task("WS9", "WS9.3", "wip", None, None, root=self.root)
        self.assertTrue((self.root / "WS9" / linear.OUTBOX_FILE).is_file())
        with self._quiet() as out:
            rc = unlink("WS9", root=self.root)
        self.assertEqual(rc, 0)
        self.assertFalse((self.root / "WS9" / linear.LINK_FILE).exists())
        self.assertFalse((self.root / "WS9" / linear.OUTBOX_FILE).exists())
        self.assertIn("Kept in Linear", out.getvalue())
        self.assertEqual(len(self.fake.issues), 5)

    def test_train_refs_lists_linked_members(self):
        self._link()
        train = PLAN["trains"][0]
        self.assertEqual(train_refs("WS9", train, self.root), "Fixes ENG-1, ENG-2")
        self.assertIsNone(train_refs("WS9", {"members": []}, self.root))
        with self._quiet():
            unlink("WS9", root=self.root)
        self.assertIsNone(train_refs("WS9", train, self.root))


if __name__ == "__main__":
    unittest.main()
