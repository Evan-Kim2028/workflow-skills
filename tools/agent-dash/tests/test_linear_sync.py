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
        self.fail_link_urls: set[str] = set()

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

    def find_relation(self, blocker_issue_id, blocked_issue_id):
        return None

    def issue_exists(self, issue_id):
        return issue_id in self.issues

    def comment(self, issue_id, body):
        self.calls.append(("comment", issue_id, body))

    def update_project_meta(self, project_id, meta):
        self.calls.append(("project_meta", project_id, dict(meta)))

    def update_milestone_meta(self, milestone_id, project_id, description, target_date):
        self.calls.append(("milestone_meta", milestone_id, description, target_date))

    def set_issue_meta(self, issue_id, add_labels, priority):
        self.calls.append(("issue_meta", issue_id, list(add_labels), priority))

    def create_link(self, project_id, url, title):
        if url in self.fail_link_urls:
            raise LinearError("GRAPHQL", "boom")
        self.calls.append(("link", project_id, url, title))

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
        self.assertEqual([call[1] for call in creates], ["ENG-1", "ENG-2", "ENG-3", "ENG-4"])
        self.assertEqual(len(self.fake.calls_named("milestone")), 2)
        ids = {key: self._link_record()["tasks"][key]["id"] for key in ("WS9.1", "WS9.2", "WS9.3")}
        self.assertEqual(
            sorted(call[1:] for call in self.fake.calls_named("blocks")),
            sorted([(ids["WS9.1"], ids["WS9.2"]), (ids["WS9.2"], ids["WS9.3"])]),
        )
        record = self._link_record()
        self.assertEqual(sorted(record["relations"]), ["WS9.1>WS9.2", "WS9.2>WS9.3"])
        self.assertEqual(set(record["tasks"]), {"WS9.1", "WS9.2", "WS9.3", "WS9.4"})
        self.assertEqual(record["approvals"], {})
        self.assertEqual(record["linked_at"], self.now)
        self.assertEqual(record["synced_at"], self.now)
        self.assertIsNone(record["last_error"])
        self.assertTrue(record["spec_hash"])
        self.assertEqual(record["milestones"].keys(), {"Shape", "Build"})

        workspace = linear.read_workspace()
        self.assertEqual(set(workspace["states"]), {"Todo", "In Progress", "In Review", "Awaiting prod", "Done", "Canceled"})
        self.assertEqual(workspace["approval_label"], "label-approval")
        self.assertIn(("ensure_state", "Awaiting prod", "started", "#a855f7"), self.fake.calls)

    def test_pending_approvals_get_no_linear_issue(self):
        """Approvals are a local decision log: linking never asks the owner through Linear."""
        self._link()
        self.assertNotIn(linear.stable_id("approval", "WS9", "A1"), self.fake.issues)
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

    def test_legacy_approval_outbox_row_drains_without_a_linear_write(self):
        """A row from before approvals stopped mirroring drains as a skip: its Linear issue, if any, is left alone."""
        self._link()
        linear.enqueue("WS9", "approval", "A1", root=self.root, now=self.now)
        self.fake.calls.clear()
        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        self.assertEqual([call for call in self.fake.calls if call[0] == "state"], [])
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
        self.assertEqual(len(self.fake.issues), 4)

    def test_train_refs_lists_linked_members(self):
        self._link()
        train = PLAN["trains"][0]
        self.assertEqual(train_refs("WS9", train, self.root), "Fixes ENG-1, ENG-2")
        self.assertIsNone(train_refs("WS9", {"members": []}, self.root))
        with self._quiet():
            unlink("WS9", root=self.root)
        self.assertIsNone(train_refs("WS9", train, self.root))



class RecordingFake(FakeLinear):
    """FakeLinear plus issues deleted in Linear and blocks relations with ids."""

    def __init__(self):
        super().__init__()
        self.deleted: set[str] = set()
        self.relations: dict[str, tuple[str, str]] = {}
        self.relation_seq = 0

    def set_issue_state(self, issue_id, state_id):
        if issue_id in self.deleted:
            self.calls.append(("state", issue_id, state_id))
            raise LinearError("NOT_FOUND", "Entity not found")
        super().set_issue_state(issue_id, state_id)

    def comment(self, issue_id, body):
        if issue_id in self.deleted:
            raise LinearError("NOT_FOUND", "Entity not found")
        super().comment(issue_id, body)

    def _row(self, issue):
        row = super()._row(issue)
        row["state"]["id"] = issue["state_id"]
        return row

    def issues_updated_since(self, project_id, since_iso):
        return [row for row in super().issues_updated_since(project_id, since_iso) if row["id"] not in self.deleted]

    def ensure_blocks(self, blocker_issue_id, blocked_issue_id):
        super().ensure_blocks(blocker_issue_id, blocked_issue_id)
        self.relation_seq += 1
        relation_id = f"rel-{self.relation_seq}"
        self.relations[relation_id] = (blocker_issue_id, blocked_issue_id)
        return relation_id

    def delete_relation(self, relation_id):
        self.calls.append(("delete_relation", relation_id))
        self.relations.pop(relation_id, None)

    def find_relation(self, blocker_issue_id, blocked_issue_id):
        for relation_id, edge in self.relations.items():
            if edge == (blocker_issue_id, blocked_issue_id):
                return relation_id
        return None

    def issue_exists(self, issue_id):
        return super().issue_exists(issue_id) and issue_id not in self.deleted


class DeletedAndRemovedTest(LinearSyncTest):
    def setUp(self):
        super().setUp()
        self.fake = RecordingFake()
        self._link()

    def _edit_plan(self, edit) -> None:
        plan = self._plan()
        edit(plan)
        (self.root / "WS9" / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

    def test_deleted_issue_detaches_and_later_syncs_do_not_recreate(self):
        ident = self._ident("WS9.3")
        issue_id = linear.stable_id("task", "WS9", "WS9.3")
        self.fake.deleted.add(issue_id)
        with self._quiet():
            bundles.set_task("WS9", "WS9.3", "pr", None, None, root=self.root)

        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        self.assertEqual(result["flushed"], 1)
        record = self._link_record()
        self.assertEqual(record["detached"], ["task:WS9.3"])
        self.assertNotIn("WS9.3", record["tasks"])
        self.assertEqual(linear.read_outbox("WS9", self.root), [])

        self.fake.calls.clear()
        self._edit_plan(lambda plan: next(t for g in plan["dag"]["groups"] for t in g["tasks"] if t["id"] == "WS9.3").update(title="Third task, renamed"))
        later = self._sync(self.now + 2)
        self.assertIsNone(later["error"])
        self.assertFalse([c for c in self.fake.calls if c[0] in ("create", "update") and c[1] == ident])
        self.assertFalse([c for c in self.fake.calls if c[0] == "state" and c[1] == issue_id])
        self.assertNotIn("WS9.3", self._link_record()["tasks"])
        self.assertEqual(self._sync(self.now + 3)["error"], None)

    def test_renamed_awaiting_prod_state_still_maps_to_prog_by_id(self):
        state_id = self.fake.team_states.pop("Awaiting prod")[0]
        self.fake.team_states["Ready for prod"] = (state_id, "started")
        self.fake.linear_move(self._ident("WS9.2"), "Ready for prod")

        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self._task("WS9.2")["status"], "prog")

    def test_removed_dependency_deletes_its_relation(self):
        edge = "WS9.2>WS9.3"
        relation_id = self._link_record()["relations"][edge]
        self.assertIsNotNone(relation_id)
        self._edit_plan(lambda plan: next(t for g in plan["dag"]["groups"] for t in g["tasks"] if t["id"] == "WS9.3").update(deps=[]))

        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        self.assertIn(("delete_relation", relation_id), self.fake.calls)
        self.assertNotIn(relation_id, self.fake.relations)
        relations = self._link_record()["relations"]
        self.assertNotIn(edge, relations)
        self.assertIn("WS9.1>WS9.2", relations)

    def test_removed_task_is_canceled_once_with_comment_and_leaves_mapping(self):
        issue_id = linear.stable_id("task", "WS9", "WS9.4")

        def drop(plan):
            build = plan["dag"]["groups"][1]
            build["tasks"] = [t for t in build["tasks"] if t["id"] != "WS9.4"]

        self._edit_plan(drop)
        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        self.assertEqual([c for c in self.fake.calls if c[0] == "state" and c[1] == issue_id], [("state", issue_id, "s-canceled")])
        removed = [c for c in self.fake.calls if c[0] == "comment" and c[1] == issue_id and c[2] == "Removed from the plan."]
        self.assertEqual(len(removed), 1)
        self.assertNotIn("WS9.4", self._link_record()["tasks"])
        self.assertNotIn("WS9.4", linear.task_links("WS9", self.root))

        self.fake.calls.clear()
        self.assertIsNone(self._sync(self.now + 2)["error"])
        self.assertFalse([c for c in self.fake.calls if c[0] in ("state", "comment") and c[1] == issue_id])

    def test_legacy_relation_list_migrates_to_null_ids(self):
        from dash import linear_sync as sync_module

        record = {"relations": ["WS9.1>WS9.2", "WS9.2>WS9.3"]}
        self.assertEqual(sync_module._relation_map(record), {"WS9.1>WS9.2": None, "WS9.2>WS9.3": None})
        self.assertEqual(record["relations"], {"WS9.1>WS9.2": None, "WS9.2>WS9.3": None})

class LinearAtomicityTest(LinearSyncTest):
    """Local writes and Linear pulls share the bundle lock; Linear-origin writes yield to pending local rows."""

    def setUp(self):
        super().setUp()
        self._link()

    def test_local_write_landing_during_pull_survives_the_next_sync(self):
        self.fake.linear_move(self._ident("WS9.1"), "In Progress")
        real_pull = self.fake.issues_updated_since
        fired = []

        def pull_with_local_write(project_id, since_iso):
            if not fired:
                fired.append(True)
                with self._quiet():
                    self.assertEqual(bundles.set_task("WS9", "WS9.1", "pr", None, None, root=self.root), 0)
            return real_pull(project_id, since_iso)

        self.fake.issues_updated_since = pull_with_local_write
        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        self.assertEqual(fired, [True])
        self.assertEqual(self._task("WS9.1")["status"], "pr")
        self.assertEqual(result["applied"], 0)

        result = self._sync(self.now + 2)
        self.assertIsNone(result["error"])
        issue = self.fake.issues[linear.stable_id("task", "WS9", "WS9.1")]
        self.assertEqual(issue["state_id"], "s-pr")
        self.assertEqual(self._task("WS9.1")["status"], "pr")
        self.assertEqual(linear.read_outbox("WS9", self.root), [])

    def test_linear_origin_write_is_skipped_while_local_row_pending(self):
        with self._quiet():
            bundles.set_task("WS9", "WS9.2", "done", None, None, root=self.root)
        history_before = len(self._history())
        with self._quiet() as out:
            rc = bundles.set_task("WS9", "WS9.2", "wip", None, None, root=self.root, force=True, origin="linear")
        self.assertEqual(rc, errors.SKIPPED_PENDING)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(self._task("WS9.2")["status"], "done")
        self.assertEqual(len(self._history()), history_before)
        self.assertEqual(linear_sync._apply_task("WS9", self.root, "WS9.2", {"name": "In Progress", "type": "started"}), 0)

    def test_drain_keeps_row_appended_mid_flush_with_equal_timestamp(self):
        linear.enqueue("WS9", "task", "WS9.3", root=self.root, now=self.now)
        self.fake.on_state = lambda _issue_id: linear.enqueue("WS9", "task", "WS9.3", root=self.root, now=self.now)
        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        self.assertEqual(result["flushed"], 1)
        rows = linear.read_outbox("WS9", self.root)
        self.assertEqual([(row["key"], row["ts"]) for row in rows], [("WS9.3", self.now)])

    def test_unlink_racing_enqueue_never_leaves_an_outbox_behind(self):
        import threading

        bundle = self.root / "WS9"
        record = self._link_record()
        for i in range(25):
            linear.write_link("WS9", record, self.root)
            start = threading.Barrier(2)

            def do_unlink():
                start.wait()
                with self._quiet():
                    unlink("WS9", root=self.root)

            def do_enqueue():
                start.wait()
                linear.enqueue("WS9", "task", "WS9.1", root=self.root, now=float(i))

            threads = [threading.Thread(target=do_unlink), threading.Thread(target=do_enqueue)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertFalse(linear.is_linked("WS9", self.root))
            self.assertFalse((bundle / linear.OUTBOX_FILE).exists(), f"iteration {i}")


class BrokenPlanAndRelationTest(LinearSyncTest):
    """A broken plan never mass-cancels; stale relation ids are looked up, not leaked."""

    def setUp(self):
        super().setUp()
        self.fake = RecordingFake()
        self._link()

    def _write_plan(self, plan) -> None:
        (self.root / "WS9" / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

    def _tracked(self) -> set[str]:
        return set(self._link_record()["tasks"])

    def test_missing_dag_cancels_nothing_and_records_no_error(self):
        plan = self._plan()
        del plan["dag"]
        self._write_plan(plan)
        before = self._tracked()
        self.fake.calls.clear()

        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        self.assertEqual(self._tracked(), before)
        self.assertFalse([c for c in self.fake.calls if c[0] in ("state", "comment", "delete_relation")])
        self.assertIsNone(self._link_record().get("last_error"))

    def test_non_dict_dag_and_empty_groups_cancel_nothing(self):
        for broken in ("oops", {"cols": [], "groups": []}, {"groups": "nope"}):
            with self.subTest(dag=broken):
                plan = self._plan()
                plan["dag"] = broken
                self._write_plan(plan)
                before = self._tracked()
                self.fake.calls.clear()
                self.assertIsNone(self._sync(self.now + 1)["error"])
                self.assertEqual(self._tracked(), before)
                self.assertFalse([c for c in self.fake.calls if c[0] in ("state", "comment", "delete_relation")])

    def test_empty_live_set_with_tracked_tasks_cancels_nothing(self):
        plan = self._plan()
        for group in plan["dag"]["groups"]:
            group["tasks"] = []
        self._write_plan(plan)
        before = self._tracked()
        self.fake.calls.clear()
        self.assertIsNone(self._sync(self.now + 1)["error"])
        self.assertEqual(self._tracked(), before)
        self.assertFalse([c for c in self.fake.calls if c[0] == "state"])

    def test_stale_null_relation_id_is_looked_up_and_deleted(self):
        edge = "WS9.2>WS9.3"
        blocker = linear.stable_id("task", "WS9", "WS9.2")
        blocked = linear.stable_id("task", "WS9", "WS9.3")
        rel_id = next(rid for rid, pair in self.fake.relations.items() if pair == (blocker, blocked))
        record = self._link_record()
        record["relations"][edge] = None
        linear.write_link("WS9", record, self.root)
        self._edit_plan_deps("WS9.3", [])

        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        self.assertIn(("delete_relation", rel_id), self.fake.calls)
        self.assertNotIn(rel_id, self.fake.relations)
        self.assertNotIn(edge, self._link_record()["relations"])

    def test_null_relation_with_no_live_relation_is_dropped_without_delete(self):
        edge = "WS9.2>WS9.3"
        record = self._link_record()
        record["relations"][edge] = None
        linear.write_link("WS9", record, self.root)
        blocker = linear.stable_id("task", "WS9", "WS9.2")
        blocked = linear.stable_id("task", "WS9", "WS9.3")
        for rid, pair in list(self.fake.relations.items()):
            if pair == (blocker, blocked):
                del self.fake.relations[rid]
        self._edit_plan_deps("WS9.3", [])
        self.fake.calls.clear()

        self.assertIsNone(self._sync(self.now + 1)["error"])
        self.assertFalse([c for c in self.fake.calls if c[0] == "delete_relation"])
        self.assertNotIn(edge, self._link_record()["relations"])

    def test_create_on_duplicate_stores_the_looked_up_relation_id(self):
        blocker = linear.stable_id("task", "WS9", "WS9.1")
        blocked = linear.stable_id("task", "WS9", "WS9.2")
        existing = "rel-existing"
        for rid, pair in list(self.fake.relations.items()):
            if pair == (blocker, blocked):
                del self.fake.relations[rid]
        self.fake.relations[existing] = (blocker, blocked)
        record = self._link_record()
        del record["relations"]["WS9.1>WS9.2"]
        linear.write_link("WS9", record, self.root)
        self.fake.ensure_blocks = lambda blocker_id, blocked_id: None
        self._edit_plan_title("WS9.2", "Second task, renamed")

        self.assertIsNone(self._sync(self.now + 1)["error"])
        self.assertEqual(self._link_record()["relations"]["WS9.1>WS9.2"], existing)

    def test_not_found_for_a_live_issue_is_not_detached(self):
        def misreported(issue_id, state_id):
            raise LinearError("NOT_FOUND", "Team not found")

        self.fake.set_issue_state = misreported
        with self._quiet():
            bundles.set_task("WS9", "WS9.3", "pr", None, None, root=self.root)

        result = self._sync(self.now + 1)
        self.assertIsNotNone(result["error"])
        record = self._link_record()
        self.assertNotIn("task:WS9.3", record.get("detached") or [])
        self.assertIn("WS9.3", record["tasks"])
        self.assertEqual(len(linear.read_outbox("WS9", self.root)), 1)

    def _edit_plan_deps(self, task_id, deps):
        plan = self._plan()
        next(t for g in plan["dag"]["groups"] for t in g["tasks"] if t["id"] == task_id)["deps"] = deps
        self._write_plan(plan)

    def _edit_plan_title(self, task_id, title):
        plan = self._plan()
        next(t for g in plan["dag"]["groups"] for t in g["tasks"] if t["id"] == task_id)["title"] = title
        self._write_plan(plan)


class DriftTest(LinearSyncTest):
    """Local status that differs from ``synced_status`` is sent on the next sync, with or without an outbox row."""

    def setUp(self):
        super().setUp()
        self._link()

    def _edit_plan(self, edit) -> None:
        plan = self._plan()
        edit(plan)
        (self.root / "WS9" / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

    def _state_calls(self) -> list[tuple]:
        return [call for call in self.fake.calls if call[0] == "state"]

    def test_hand_edited_plan_status_reaches_linear_without_outbox_row(self):
        def set_pr(plan):
            next(t for g in plan["dag"]["groups"] for t in g["tasks"] if t["id"] == "WS9.3")["status"] = "pr"

        self._edit_plan(set_pr)
        self.assertEqual(linear.read_outbox("WS9", self.root), [])
        self.fake.calls.clear()

        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        issue_id = linear.stable_id("task", "WS9", "WS9.3")
        self.assertEqual(self._state_calls(), [("state", issue_id, "s-pr")])
        self.assertEqual(self._link_record()["tasks"]["WS9.3"]["synced_status"], "pr")

        self.fake.calls.clear()
        self.assertIsNone(self._sync(self.now + 2)["error"])
        self.assertEqual(self._state_calls(), [])

    def test_hand_edit_wins_over_a_concurrent_linear_change(self):
        def set_pr(plan):
            next(t for g in plan["dag"]["groups"] for t in g["tasks"] if t["id"] == "WS9.3")["status"] = "pr"

        self._edit_plan(set_pr)
        self.fake.linear_move(self._ident("WS9.3"), "Done")
        self.fake.calls.clear()

        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        self.assertEqual(self._task("WS9.3")["status"], "pr")
        issue = self.fake.issues[linear.stable_id("task", "WS9", "WS9.3")]
        self.assertEqual(issue["state_id"], "s-pr")
        self.assertEqual(result["applied"], 0)
        self.assertEqual(self._link_record()["tasks"]["WS9.3"]["synced_status"], "pr")

    def test_record_without_synced_status_initializes_without_sending(self):
        record = self._link_record()
        for ref in record["tasks"].values():
            ref.pop("synced_status", None)
        linear.write_link("WS9", record, self.root)
        self.fake.calls.clear()

        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        self.assertEqual(self._state_calls(), [])
        record = self._link_record()
        self.assertEqual(record["tasks"]["WS9.1"]["synced_status"], "todo")
        self.assertEqual(record["tasks"]["WS9.2"]["synced_status"], "wip")

    def test_pull_that_applies_a_linear_change_updates_synced_status(self):
        self.fake.linear_move(self._ident("WS9.2"), "Done")
        self.fake.calls.clear()

        result = self._sync(self.now + 1)
        self.assertIsNone(result["error"])
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self._task("WS9.2")["status"], "done")
        self.assertEqual(self._link_record()["tasks"]["WS9.2"]["synced_status"], "done")

        self.fake.calls.clear()
        result = self._sync(self.now + 2)
        self.assertIsNone(result["error"])
        self.assertEqual(result["flushed"], 0)
        self.assertEqual(self._state_calls(), [])

class AgentCommentTest(LinearSyncTest):
    """Agent comments: every row posts in ts order, and none touches status or drift."""

    def setUp(self):
        super().setUp()
        self._link()
        self.issue = self._link_record()["tasks"]["WS9.1"]["id"]

    def _comments(self) -> list[tuple]:
        return [call for call in self.fake.calls_named("comment") if call[1] == self.issue]

    def test_two_comments_on_one_task_both_post_in_order(self):
        linear.enqueue_comment("WS9", "WS9.1", "second", root=self.root, now=self.now + 2)
        linear.enqueue_comment("WS9", "WS9.1", "first", root=self.root, now=self.now + 1)
        self.fake.calls.clear()
        result = self._sync()
        self.assertIsNone(result["error"])
        self.assertEqual(self._comments(), [("comment", self.issue, "first"), ("comment", self.issue, "second")])
        self.assertEqual(linear.read_outbox("WS9", self.root), [])

    def test_comments_do_not_change_status_or_drift(self):
        before = dict(self._link_record()["tasks"]["WS9.1"])
        plan_before = self._plan()
        linear.enqueue_comment("WS9", "WS9.1", "verdict PASS", root=self.root, now=self.now + 1)
        self.fake.calls.clear()
        self._sync()
        self.assertEqual(self.fake.calls_named("state"), [])
        self.assertEqual(self._plan(), plan_before)
        after = self._link_record()["tasks"]["WS9.1"]
        self.assertEqual(after.get("synced_status"), before.get("synced_status"))
        self.assertEqual(self._history(), [])

    def test_comment_posts_alongside_a_status_change(self):
        with self._quiet():
            bundles.set_task("WS9", "WS9.1", "pr", None, None, root=self.root)
        linear.enqueue_comment("WS9", "WS9.1", "PR up", root=self.root, now=self.now + 5)
        self.fake.calls.clear()
        self._sync(self.now + 6)
        self.assertEqual(self.fake.calls_named("state"), [("state", self.issue, "s-pr")])
        self.assertEqual(self._comments(), [("comment", self.issue, "PR up")])
        self.assertEqual(linear.read_outbox("WS9", self.root), [])

    def test_comment_on_unmapped_task_is_drained_without_a_post(self):
        linear.enqueue_comment("WS9", "WS9.3", "no issue yet", root=self.root, now=self.now + 1)
        self.fake.calls.clear()
        self._sync()
        self.assertEqual(self._comments(), [])
        self.assertEqual(linear.read_outbox("WS9", self.root), [])

    def test_rows_appended_during_the_flush_stay_queued(self):
        fake = self.fake
        linear.enqueue_comment("WS9", "WS9.1", "first", root=self.root, now=self.now + 1)
        original = fake.comment
        late = {"done": False}

        def comment(issue_id, body):
            original(issue_id, body)
            if not late["done"]:
                late["done"] = True
                linear.enqueue_comment("WS9", "WS9.2", "late", root=self.root, now=self.now + 9)

        fake.comment = comment
        self._sync()
        self.assertEqual([row["body"] for row in linear.read_outbox("WS9", self.root)], ["late"])
        self.assertEqual(self._comments(), [("comment", self.issue, "first")])



class MetaPushTest(LinearSyncTest):
    def setUp(self):
        super().setUp()
        self._link()
        self.fake.calls.clear()

    def _set_plan(self, edit) -> None:
        plan = self._plan()
        edit(plan)
        (self.root / "WS9" / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

    def _set_tracker(self, tracker: dict) -> None:
        self._set_plan(lambda plan: plan.__setitem__("tracker", tracker))

    def _set_state(self, state: str) -> None:
        path = self.root / "WS9" / "bundle.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["state"] = state
        path.write_text(json.dumps(meta), encoding="utf-8")

    def _project_meta_calls(self) -> list[dict]:
        return [call[2] for call in self.fake.calls_named("project_meta")]

    def test_tracker_of_keeps_valid_fields_and_drops_the_rest(self):
        plan = {
            "tracker": {
                "summary": "Short",
                "description": "Long",
                "lead": "Evan Kim",
                "labels": ["Infrastructure", "", 3, "Infrastructure"],
                "issue_labels": ["Infrastructure"],
                "priority": 2,
                "start_date": "2026-10-21",
                "target_date": "2026-13-40",
                "links": [
                    {"title": "Dashboard", "url": "https://example.test/d"},
                    {"title": "Bad", "url": "javascript:alert(1)"},
                    {"url": "https://example.test/x"},
                ],
                "milestones": {"Shape": {"description": "Gate", "target_date": None, "extra": 1}, "": {"description": "x"}},
                "update": {"health": "onTrack", "daily_hour": 25},
                "unknown": True,
            }
        }
        self.assertEqual(
            linear.tracker_of(plan),
            {
                "summary": "Short",
                "description": "Long",
                "lead": "Evan Kim",
                "labels": ["Infrastructure"],
                "issue_labels": ["Infrastructure"],
                "priority": 2,
                "start_date": "2026-10-21",
                "links": [{"title": "Dashboard", "url": "https://example.test/d"}],
                "milestones": {"Shape": {"description": "Gate"}},
                "update": {"health": "onTrack"},
            },
        )
        self.assertEqual(linear.tracker_of({}), {})
        self.assertEqual(linear.tracker_of({"tracker": "text"}), {})
        self.assertEqual(linear.tracker_of({"tracker": {"summary": "x" * 256, "priority": True}}), {})

    def test_derived_priorities_rank_critical_then_ready_then_other_open(self):
        plan = {
            "dag": {
                "cols": [],
                "groups": [
                    {
                        "id": "g1",
                        "tasks": [
                            {"id": "A", "status": "todo", "deps": []},
                            {"id": "B", "status": "todo", "deps": ["A"]},
                            {"id": "C", "status": "todo", "deps": ["B"]},
                            {"id": "D", "status": "todo", "deps": []},
                            {"id": "E", "status": "todo", "deps": ["D"]},
                            {"id": "F", "status": "done", "deps": []},
                            {"id": "G", "status": "deferred", "deps": []},
                        ],
                    }
                ],
                "edges": [],
            }
        }
        self.assertEqual(linear.derived_priorities(plan), {"A": 2, "B": 2, "C": 2, "D": 3, "E": 4})

    def test_metadata_is_pushed_once_and_again_only_when_it_changes(self):
        self._set_tracker({"summary": "One line"})
        self.assertIsNone(self._sync(self.now + 1)["error"])
        self.assertEqual([meta.get("summary") for meta in self._project_meta_calls()], ["One line"])

        self.fake.calls.clear()
        self.assertIsNone(self._sync(self.now + 2)["error"])
        self.assertEqual(self._project_meta_calls(), [])

        self._set_tracker({"summary": "Changed line"})
        self.assertIsNone(self._sync(self.now + 3)["error"])
        self.assertEqual([meta.get("summary") for meta in self._project_meta_calls()], ["Changed line"])

    def test_lifecycle_change_pushes_status_without_a_tracker(self):
        self.assertNotIn("tracker", self._plan())
        self._set_state("done")
        self.assertIsNone(self._sync(self.now + 1)["error"])
        meta = self._project_meta_calls()[0]
        self.assertEqual(meta["status"], "Completed")
        self.assertNotIn("summary", meta)
        self.assertNotIn("description", meta)

    def test_archived_state_leaves_the_project_status_alone(self):
        self._set_state("archived")
        self.assertIsNone(self._sync(self.now + 1)["error"])
        self.assertNotIn("status", self._project_meta_calls()[0])

    def test_issue_metadata_sets_derived_priority_tracker_labels_and_approval(self):
        self._set_tracker({"issue_labels": ["Infrastructure"]})
        self.assertIsNone(self._sync(self.now + 1)["error"])
        issues = {call[1]: (call[2], call[3]) for call in self.fake.calls_named("issue_meta")}
        for task in ("WS9.1", "WS9.2", "WS9.3"):
            self.assertEqual(issues[linear.stable_id("task", "WS9", task)], (["Infrastructure"], 2))
        self.assertNotIn(linear.stable_id("approval", "WS9", "A1"), issues)
        self.assertNotIn(linear.stable_id("task", "WS9", "WS9.4"), issues)
        self.assertNotIn(linear.stable_id("approval", "WS9", "A2"), issues)

    def test_milestone_metadata_goes_to_mapped_columns_only(self):
        self._set_tracker(
            {
                "milestones": {
                    "Shape": {"description": "Shape gate", "target_date": "2026-11-01"},
                    "Unmapped": {"description": "never pushed"},
                }
            }
        )
        self.assertIsNone(self._sync(self.now + 1)["error"])
        milestone_calls = self.fake.calls_named("milestone_meta")
        self.assertEqual(milestone_calls, [("milestone_meta", linear.stable_id("milestone", "WS9", "Shape"), "Shape gate", "2026-11-01")])

    def test_metadata_pass_resumes_across_cycles_without_resending_done_calls(self):
        clock = {"t": 0.0}
        for name in ("update_project_meta", "set_issue_meta", "update_milestone_meta"):
            real = getattr(self.fake, name)

            def ticking(*args, _real=real, **kwargs):
                clock["t"] += 1.0
                return _real(*args, **kwargs)

            setattr(self.fake, name, ticking)
        self._set_tracker({"summary": "Resume me", "issue_labels": ["Infrastructure"], "milestones": {"Shape": {"description": "Gate"}}})
        sent_all: list[tuple] = []
        batches: list[int] = []
        for cycle in range(20):
            clock["t"] = 0.0
            self.fake.calls.clear()
            result = sync("WS9", root=self.root, client=self.fake, now=self.now + 1 + cycle,
                          meta_budget_s=2.5, clock=lambda: clock["t"])
            self.assertIsNone(result["error"])
            sent = [call for call in self.fake.calls if call[0] in ("project_meta", "issue_meta", "milestone_meta")]
            sent_all.extend(sent)
            batches.append(len(sent))
            if not sent:
                break
        self.assertGreater(len(batches), 2)
        self.assertEqual(batches[-1], 0)
        self.assertEqual(len(sent_all), len({repr(call) for call in sent_all}))
        self.assertEqual(len([call for call in sent_all if call[0] == "project_meta"]), 1)

    def test_status_change_does_not_rewrite_the_project(self):
        with self._quiet():
            bundles.set_task("WS9", "WS9.3", "wip", None, None, root=self.root)
        self.fake.calls.clear()
        self.assertIsNone(self._sync(self.now + 1)["error"])
        self.assertEqual(self._project_meta_calls(), [])
        self.assertEqual(self.fake.calls_named("milestone_meta"), [])

    def test_unchanged_milestone_is_not_resent_when_another_changes(self):
        self._set_tracker({"milestones": {"Shape": {"description": "Shape gate", "target_date": "2026-11-01"}}})
        self.assertIsNone(self._sync(self.now + 1)["error"])
        self.fake.calls.clear()
        self._set_tracker({"milestones": {"Shape": {"description": "Shape gate", "target_date": "2026-11-01"}, "Build": {"description": "Build gate"}}})
        self.assertIsNone(self._sync(self.now + 2)["error"])
        self.assertEqual([call[2] for call in self.fake.calls_named("milestone_meta")], ["Build gate"])

    def test_failed_project_push_keeps_its_digest_and_repeats_next_cycle(self):
        real = self.fake.update_project_meta

        def refuse(project_id, meta):
            raise LinearError("GRAPHQL", "boom")

        before = self._link_record()["meta_digests"]["project"]
        self._set_tracker({"summary": "Retry me"})
        self.fake.update_project_meta = refuse
        self.assertIsNotNone(self._sync(self.now + 1)["error"])
        self.assertEqual(self._link_record()["meta_digests"]["project"], before)
        self.fake.update_project_meta = real
        self.fake.calls.clear()
        self.assertIsNone(self._sync(self.now + 2)["error"])
        self.assertEqual([meta.get("summary") for meta in self._project_meta_calls()], ["Retry me"])
        self.assertNotEqual(self._link_record()["meta_digests"]["project"], before)

    def test_links_are_created_once_each_and_recorded_as_they_succeed(self):
        link = {"title": "Dashboard", "url": "https://example.test/d"}
        self._set_tracker({"links": [link]})
        self.assertIsNone(self._sync(self.now + 1)["error"])
        project_id = self._link_record()["project"]["id"]
        self.assertEqual(self.fake.calls_named("link"), [("link", project_id, link["url"], link["title"])])
        self.assertEqual(self._link_record()["meta_links"], [link["url"]])

        self.fake.calls.clear()
        self._set_tracker({"summary": "Changed", "links": [link]})
        self.assertIsNone(self._sync(self.now + 2)["error"])
        self.assertEqual(self.fake.calls_named("link"), [])
        self.assertNotIn("links", self._project_meta_calls()[-1])

    def test_failed_link_create_keeps_the_sent_links_and_resumes_at_the_failed_one(self):
        first = {"title": "One", "url": "https://example.test/1"}
        second = {"title": "Two", "url": "https://example.test/2"}
        self._set_tracker({"links": [first, second]})
        self.fake.fail_link_urls = {second["url"]}
        self.assertIsNotNone(self._sync(self.now + 1)["error"])
        self.assertEqual(self._link_record()["meta_links"], [first["url"]])
        self.fake.fail_link_urls = set()
        self.fake.calls.clear()
        self.assertIsNone(self._sync(self.now + 2)["error"])
        self.assertEqual([call[2] for call in self.fake.calls_named("link")], [second["url"]])
        self.assertEqual(self._link_record()["meta_links"], sorted([first["url"], second["url"]]))

    def test_metadata_failure_does_not_stop_the_status_flush_or_the_pull(self):
        self._set_tracker({"summary": "Boom"})
        real = self.fake.update_project_meta

        def refuse(project_id, meta):
            raise LinearError("GRAPHQL", "boom")

        self.fake.update_project_meta = refuse
        with self._quiet():
            bundles.set_task("WS9", "WS9.3", "wip", None, None, root=self.root)
        self.fake.calls.clear()
        result = self._sync(self.now + 1)
        self.fake.update_project_meta = real
        self.assertIn("boom", result["error"])
        issue_id = linear.stable_id("task", "WS9", "WS9.3")
        self.assertEqual([call for call in self.fake.calls_named("state") if call[1] == issue_id], [("state", issue_id, "s-wip")])
        self.assertEqual(result["flushed"], 1)
        self.assertEqual(linear.read_outbox("WS9", self.root), [])
        self.assertIn("boom", self._link_record()["last_error"])

    def test_not_found_meta_call_detaches_its_task_and_the_pass_goes_on(self):
        target = self._link_record()["tasks"]["WS9.1"]["id"]
        self._set_tracker({"issue_labels": ["Infrastructure"]})
        real_set = self.fake.set_issue_meta
        real_exists = self.fake.issue_exists

        def gone(issue_id, labels, priority):
            if issue_id == target:
                raise LinearError("NOT_FOUND", "gone")
            return real_set(issue_id, labels, priority)

        self.fake.set_issue_meta = gone
        self.fake.issue_exists = lambda issue_id: issue_id != target
        self.fake.calls.clear()
        result = self._sync(self.now + 1)
        self.fake.set_issue_meta = real_set
        self.fake.issue_exists = real_exists
        self.assertIsNone(result["error"])
        self.assertIn("task:WS9.1", self._link_record()["detached"])
        sent = {call[1] for call in self.fake.calls_named("issue_meta")}
        self.assertIn(linear.stable_id("task", "WS9", "WS9.2"), sent)
        self.assertIn(linear.stable_id("task", "WS9", "WS9.3"), sent)

    def test_failed_status_send_does_not_block_comments_and_keeps_its_row(self):
        with self._quiet():
            bundles.set_task("WS9", "WS9.3", "wip", None, None, root=self.root)
            linear.enqueue_comment("WS9", "WS9.1", "Hello there", root=self.root, now=self.now)
        real = self.fake.set_issue_state
        target = linear.stable_id("task", "WS9", "WS9.3")

        def broken(issue_id, state_id):
            if issue_id == target:
                raise LinearError("GRAPHQL", "boom")
            return real(issue_id, state_id)

        self.fake.set_issue_state = broken
        self.fake.calls.clear()
        result = self._sync(self.now + 1)
        self.fake.set_issue_state = real
        self.assertIn("boom", result["error"])
        self.assertIn(("comment", linear.stable_id("task", "WS9", "WS9.1"), "Hello there"), self.fake.calls)
        self.assertEqual([(row["kind"], row["key"]) for row in linear.read_outbox("WS9", self.root)], [("task", "WS9.3")])

    def test_comment_with_no_linear_issue_is_counted_as_dropped(self):
        with self._quiet():
            linear.enqueue_comment("WS9", "WS9.99", "Not in the plan", root=self.root, now=self.now)
        self.assertIsNone(self._sync(self.now + 1)["error"])
        self.assertEqual(self._link_record()["dropped_comments"], 1)
        self.assertEqual(linear.read_outbox("WS9", self.root), [])
        with self._quiet() as out:
            linear_sync.status("WS9", root=self.root)
        self.assertIn("dropped comments: 1", out.getvalue())


class DerivedPriorityTest(unittest.TestCase):
    def test_closed_tasks_are_absent_and_do_not_shape_the_critical_path(self):
        plan = {
            "dag": {
                "groups": [
                    {
                        "tasks": [
                            {"id": "a", "status": "done", "deps": []},
                            {"id": "b", "status": "todo", "deps": ["a"]},
                            {"id": "c", "status": "wip", "deps": []},
                            {"id": "d", "status": "todo", "deps": ["c"]},
                            {"id": "e", "deps": []},
                        ]
                    }
                ]
            }
        }
        priorities = linear.derived_priorities(plan)
        self.assertNotIn("a", priorities)
        self.assertEqual(priorities["b"], linear.PRIORITY_READY)
        self.assertEqual(priorities["c"], linear.PRIORITY_CRITICAL)
        self.assertEqual(priorities["d"], linear.PRIORITY_CRITICAL)
        self.assertEqual(priorities["e"], linear.PRIORITY_OTHER)


if __name__ == "__main__":
    unittest.main()
