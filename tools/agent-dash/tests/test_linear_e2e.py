"""Linear link and sync end to end: the real HTTP client against an in-process fake server.

The unit tests in test_linear_sync.py fake the client object. These tests build the
real dash.linear_client.Client with an http endpoint on 127.0.0.1 and drive
linear_sync.link, sync and unlink through urllib, real JSON parsing and the real
GraphQL documents. FakeLinear is a ThreadingHTTPServer that dispatches on the
operation name in each document and keeps Linear-shaped objects in memory. It
pages issues two at a time so the pull walks several pages. Nothing here calls the
live Linear API.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import bundles, errors, linear, linear_sync
from dash import linear_client as lc

WS = "WS4"
TEAM_KEY = "ENG"
FAKE_KEY = "lin_api_e2e_fake_0123456789abcdef"
RATE_HEADERS = {"X-RateLimit-Requests-Remaining": "1400", "X-RateLimit-Complexity-Remaining": "9000"}
WRITE_OPS = {
    "ProjectCreate",
    "ProjectMilestoneCreate",
    "IssueCreate",
    "WorkflowStateCreate",
    "LabelCreate",
    "InitiativeCreate",
    "IssueRelationCreate",
    "CommentCreate",
}
DEFAULT_STATES = (
    ("Backlog", "backlog"),
    ("Todo", "unstarted"),
    ("In Progress", "started"),
    ("In Review", "started"),
    ("Done", "completed"),
    ("Canceled", "canceled"),
)

PLAN = {
    "workstream": WS,
    "dag": {
        "cols": ["Shape", "Build"],
        "groups": [
            {
                "id": "g1",
                "col": 0,
                "title": "Shape",
                "tasks": [
                    {"id": "WS4.1", "title": "Inventory the notes schema", "status": "todo", "deps": [], "size": "S", "files": ["notes/schema.py"], "test_cmd": "python3 -m unittest tests.test_schema -v"},
                    {"id": "WS4.2", "title": "Draft the cutover plan", "status": "todo", "deps": ["WS4.1"]},
                ],
            },
            {
                "id": "g2",
                "col": 1,
                "title": "Build",
                "tasks": [
                    {"id": "WS4.3", "title": "Backfill the notes table", "status": "todo", "deps": ["WS4.2"]},
                    {"id": "WS4.4", "title": "Switch the read path", "status": "todo", "deps": ["WS4.1"]},
                ],
            },
        ],
        "edges": [],
    },
    "trains": [{"id": "t1", "title": "Schema cut", "members": ["WS4.1", "WS4.2"], "status": "planned"}],
}

APPROVALS = {
    "ws": WS,
    "decided_at": None,
    "decided_by": None,
    "items": [
        {"id": "A1", "step": "Deploy tier", "status": "pending", "reversible": True, "conditions": "Prod window"},
        {"id": "A2", "step": "Old gate", "status": "approved", "reversible": False, "conditions": ""},
    ],
}


class GqlError(Exception):
    """A GraphQL error returned inside a 200 response, as Linear does."""

    def __init__(self, message: str, code: str = "GRAPHQL") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class FakeLinear:
    """In-process Linear GraphQL server. One lock serializes every request."""

    def __init__(self, key: str, page_size: int = 2) -> None:
        self.key = key
        self.page_size = page_size
        self.lock = threading.RLock()
        self.clock = 0
        self.teams: dict[str, dict] = {}
        self.states: dict[str, dict] = {}
        self.labels: dict[str, dict] = {}
        self.initiatives: dict[str, dict] = {}
        self.project_links: set[tuple[str, str]] = set()
        self.projects: dict[str, dict] = {}
        self.milestones: dict[str, dict] = {}
        self.issues: dict[str, dict] = {}
        self.relations: dict[str, tuple[str, str, str]] = {}
        self.comments: list[dict] = []
        self.ops: list[str] = []
        self.rate_limit_next = 0
        self.error_next: str | None = None
        self.during_issues: Callable[[], None] | None = None
        self.issue_seq = 0
        team_id = "team-eng"
        self.teams[team_id] = {"id": team_id, "key": TEAM_KEY, "states": []}
        for name, kind in DEFAULT_STATES:
            self._add_state(team_id, f"state-{name.lower().replace(' ', '-')}", name, kind)
        self._ops = {attr[len("op_"):]: getattr(self, attr) for attr in dir(self) if attr.startswith("op_")}
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(size)
                if self.headers.get("Authorization") != fake.key:
                    self._reply(401, {"errors": [{"message": "Authentication required"}]}, {})
                    return
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except ValueError:
                    self._reply(400, {"errors": [{"message": "request body is not JSON"}]}, {})
                    return
                status, body, headers = fake.handle(payload)
                self._reply(status, body, headers)

            def _reply(self, status: int, body: dict, headers: dict) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="fake-linear", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()

    @property
    def endpoint(self) -> str:
        assert self.httpd is not None
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/graphql"

    def handle(self, payload: object) -> tuple[int, dict, dict]:
        """Dispatch one parsed GraphQL request. Returns (status, body, headers)."""
        document = payload.get("query") if isinstance(payload, dict) else None
        variables = (payload.get("variables") if isinstance(payload, dict) else None) or {}
        match = re.search(r"\b(?:query|mutation)\s+(\w+)", document or "")
        op = match.group(1) if match else ""
        with self.lock:
            self.ops.append(op)
            if self.rate_limit_next:
                self.rate_limit_next -= 1
                limited = {"message": "You are being rate limited", "extensions": {"type": "ratelimit", "code": "RATELIMITED"}}
                return 400, {"errors": [limited]}, RATE_HEADERS
            if self.error_next is not None:
                message, self.error_next = self.error_next, None
                return 200, {"data": None, "errors": [{"message": message}]}, RATE_HEADERS
            handler = self._ops.get(op)
            if handler is None:
                return 400, {"errors": [{"message": f"unsupported operation {op!r}"}]}, RATE_HEADERS
            try:
                data = handler(variables)
            except GqlError as exc:
                error = {"message": exc.message, "extensions": {"code": exc.code}}
                return 200, {"data": None, "errors": [error]}, RATE_HEADERS
        return 200, {"data": data}, RATE_HEADERS

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4()}"

    def _stamp(self) -> str:
        self.clock += 1
        moment = datetime(2026, 10, 21, tzinfo=timezone.utc) + timedelta(seconds=self.clock)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _add_state(self, team_id: str, state_id: str, name: str, kind: str) -> None:
        self.states[state_id] = {"id": state_id, "name": name, "type": kind, "teamId": team_id}
        self.teams[team_id]["states"].append(state_id)

    def _state_node(self, state_id: str) -> dict:
        state = self.states[state_id]
        return {"id": state["id"], "name": state["name"], "type": state["type"]}

    def _team_states(self, team: dict) -> dict:
        return {"nodes": [self._state_node(sid) for sid in team["states"]]}

    def _team(self, team_id: object) -> dict:
        team = self.teams.get(str(team_id))
        if team is None:
            raise GqlError("Entity not found")
        return team

    def op_TeamByKey(self, v: dict) -> dict:
        nodes = [
            {"id": team["id"], "key": team["key"], "states": self._team_states(team)}
            for team in self.teams.values()
            if team["key"] == v.get("key")
        ]
        return {"teams": {"nodes": nodes}}

    def op_TeamStates(self, v: dict) -> dict:
        return {"team": {"states": self._team_states(self._team(v.get("id")))}}

    def op_WorkflowStateCreate(self, v: dict) -> dict:
        data = v["input"]
        if data["id"] in self.states:
            raise GqlError("Workflow state already exists")
        self._add_state(self._team(data["teamId"])["id"], data["id"], data["name"], data["type"])
        return {"workflowStateCreate": {"success": True, "workflowState": {"id": data["id"]}}}

    def op_LabelByName(self, v: dict) -> dict:
        nodes = [
            {"id": label["id"], "name": label["name"]}
            for label in self.labels.values()
            if label["teamId"] == v.get("teamId") and label["name"] == v.get("name")
        ]
        return {"issueLabels": {"nodes": nodes}}

    def op_LabelCreate(self, v: dict) -> dict:
        data = v["input"]
        label_id = self._new_id("label")
        self.labels[label_id] = {"id": label_id, "name": data["name"], "teamId": data["teamId"]}
        return {"issueLabelCreate": {"success": True, "issueLabel": {"id": label_id}}}

    def op_InitiativeByName(self, v: dict) -> dict:
        nodes = [dict(item) for item in self.initiatives.values() if item["name"] == v.get("name")]
        return {"initiatives": {"nodes": nodes}}

    def op_InitiativeCreate(self, v: dict) -> dict:
        data = v["input"]
        initiative_id = self._new_id("initiative")
        item = {"id": initiative_id, "name": data["name"], "url": f"https://linear.app/example/initiative/{initiative_id[-8:]}"}
        self.initiatives[initiative_id] = item
        return {"initiativeCreate": {"success": True, "initiative": dict(item)}}

    def _project_node(self, project: dict) -> dict:
        return {"id": project["id"], "name": project["name"], "url": project["url"]}

    def op_ProjectUpdate(self, v: dict) -> dict:
        project = self.projects.get(v.get("id"))
        if project is None:
            raise GqlError("Entity not found")
        for field in ("name", "description"):
            if field in v["input"]:
                project[field] = v["input"][field]
        return {"projectUpdate": {"success": True, "project": self._project_node(project)}}

    def op_ProjectCreate(self, v: dict) -> dict:
        data = v["input"]
        project_id = data.get("id") or self._new_id("project")
        if project_id in self.projects:
            raise GqlError("Project already exists")
        self._team(data["teamIds"][0])
        self.projects[project_id] = {
            "id": project_id,
            "name": data["name"],
            "description": data.get("description", ""),
            "url": f"https://linear.app/example/project/{project_id[-8:]}",
        }
        return {"projectCreate": {"success": True, "project": self._project_node(self.projects[project_id])}}

    def op_InitiativeToProjectCreate(self, v: dict) -> dict:
        data = v["input"]
        if data["initiativeId"] not in self.initiatives or data["projectId"] not in self.projects:
            raise GqlError("Entity not found")
        edge = (data["initiativeId"], data["projectId"])
        if edge in self.project_links:
            raise GqlError("Initiative to project link already exists")
        self.project_links.add(edge)
        return {"initiativeToProjectCreate": {"success": True}}

    def op_ProjectMilestones(self, v: dict) -> dict:
        project = self.projects.get(v.get("id"))
        if project is None:
            raise GqlError("Entity not found")
        nodes = [
            {"id": item["id"], "name": item["name"]}
            for item in self.milestones.values()
            if item["projectId"] == project["id"]
        ]
        return {"project": {"projectMilestones": {"nodes": nodes}}}

    def op_ProjectMilestoneCreate(self, v: dict) -> dict:
        data = v["input"]
        if data["projectId"] not in self.projects:
            raise GqlError("Entity not found")
        milestone_id = data.get("id") or self._new_id("milestone")
        if milestone_id in self.milestones:
            raise GqlError("Milestone already exists")
        self.milestones[milestone_id] = {
            "id": milestone_id,
            "name": data["name"],
            "projectId": data["projectId"],
            "sortOrder": data.get("sortOrder"),
        }
        return {"projectMilestoneCreate": {"success": True, "projectMilestone": {"id": milestone_id}}}

    def _apply_issue_input(self, issue: dict, data: dict) -> None:
        if "stateId" in data:
            if data["stateId"] not in self.states:
                raise GqlError("Entity not found")
            issue["stateId"] = data["stateId"]
        for field in ("title", "description", "projectId", "labelIds"):
            if field in data:
                issue[field] = data[field]
        if "projectMilestoneId" in data:
            issue["milestoneId"] = data["projectMilestoneId"]

    def _issue_ref(self, issue: dict) -> dict:
        return {"id": issue["id"], "identifier": issue["identifier"], "url": issue["url"]}

    def op_IssueCreate(self, v: dict) -> dict:
        data = v["input"]
        self._team(data["teamId"])
        issue_id = data.get("id") or self._new_id("issue")
        if issue_id in self.issues:
            raise GqlError("Issue already exists")
        self.issue_seq += 1
        identifier = f"{TEAM_KEY}-{self.issue_seq}"
        issue = {
            "id": issue_id,
            "identifier": identifier,
            "url": f"https://linear.app/example/issue/{identifier}",
            "teamId": data["teamId"],
            "stateId": next(iter(self.teams[data["teamId"]]["states"])),
            "title": "",
            "description": "",
            "projectId": None,
            "milestoneId": None,
            "labelIds": [],
        }
        self._apply_issue_input(issue, data)
        issue["updatedAt"] = self._stamp()
        self.issues[issue_id] = issue
        return {"issueCreate": {"success": True, "issue": self._issue_ref(issue)}}

    def op_IssueUpdate(self, v: dict) -> dict:
        issue = self.issues.get(v.get("id"))
        if issue is None:
            raise GqlError("Entity not found")
        self._apply_issue_input(issue, v["input"])
        issue["updatedAt"] = self._stamp()
        return {"issueUpdate": {"success": True, "issue": self._issue_ref(issue)}}

    def op_IssueRelationCreate(self, v: dict) -> dict:
        data = v["input"]
        if data["issueId"] not in self.issues or data["relatedIssueId"] not in self.issues:
            raise GqlError("Entity not found")
        edge = (data["issueId"], data["relatedIssueId"], data["type"])
        if edge in self.relations.values():
            raise GqlError("Issue relation already exists")
        relation_id = self._new_id("relation")
        self.relations[relation_id] = edge
        return {"issueRelationCreate": {"success": True, "issueRelation": {"id": relation_id}}}

    def op_IssueRelationDelete(self, v: dict) -> dict:
        if self.relations.pop(v.get("id"), None) is None:
            raise GqlError("Entity not found")
        return {"issueRelationDelete": {"success": True}}

    def op_CommentCreate(self, v: dict) -> dict:
        data = v["input"]
        if data["issueId"] not in self.issues:
            raise GqlError("Entity not found")
        self.comments.append({"issueId": data["issueId"], "body": data["body"]})
        return {"commentCreate": {"success": True}}

    def op_ProjectIssues(self, v: dict) -> dict:
        hook, self.during_issues = self.during_issues, None
        if hook is not None:
            hook()
        flt = v.get("filter") or {}
        project_id = ((flt.get("project") or {}).get("id") or {}).get("eq")
        since = (flt.get("updatedAt") or {}).get("gt")
        rows = sorted(
            (
                issue
                for issue in self.issues.values()
                if issue["projectId"] == project_id and (since is None or issue["updatedAt"] > since)
            ),
            key=lambda issue: issue["updatedAt"],
        )
        start = int(v.get("after") or 0)
        first = min(int(v.get("first") or 50), self.page_size)
        page = rows[start : start + first]
        end = start + len(page)
        nodes = [
            {
                "id": issue["id"],
                "identifier": issue["identifier"],
                "url": issue["url"],
                "updatedAt": issue["updatedAt"],
                "state": {
                    "id": issue["stateId"],
                    "name": self.states[issue["stateId"]]["name"],
                    "type": self.states[issue["stateId"]]["type"],
                },
            }
            for issue in page
        ]
        more = end < len(rows)
        return {"issues": {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": str(end)}}}

    def set_state(self, identifier: str, state_name: str) -> None:
        """Move an issue to a state by name, as a person in Linear would."""
        with self.lock:
            issue = self.by_identifier(identifier)
            issue["stateId"] = next(sid for sid, state in self.states.items() if state["name"] == state_name)
            issue["updatedAt"] = self._stamp()

    def delete_issue(self, identifier: str) -> None:
        """Delete an issue in Linear, cascading its relations as Linear does."""
        with self.lock:
            issue = self.by_identifier(identifier)
            del self.issues[issue["id"]]
            for relation_id in [rid for rid, edge in self.relations.items() if issue["id"] in edge[:2]]:
                del self.relations[relation_id]

    def by_identifier(self, identifier: str) -> dict:
        return next(issue for issue in self.issues.values() if issue["identifier"] == identifier)

    def state_named(self, name: str) -> dict | None:
        return next((state for state in self.states.values() if state["name"] == name), None)

    def counts(self) -> dict:
        with self.lock:
            return {
                "projects": len(self.projects),
                "milestones": len(self.milestones),
                "issues": len(self.issues),
                "relations": len(self.relations),
                "comments": len(self.comments),
                "labels": len(self.labels),
                "initiatives": len(self.initiatives),
                "project_links": len(self.project_links),
                "states": len(self.states),
            }


def _write_bundle(root: Path) -> None:
    bundle = root / WS
    bundle.mkdir(parents=True)
    (bundle / "bundle.json").write_text(json.dumps({"id": WS, "title": "Notes cutover", "state": "active"}), encoding="utf-8")
    (bundle / "plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
    (bundle / "approvals.json").write_text(json.dumps(APPROVALS), encoding="utf-8")


class LinearE2ETest(unittest.TestCase):
    """Real Client over HTTP, real files in a temp data root, one fake server per test."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "workstreams"
        self.root.mkdir()
        _write_bundle(self.root)
        env = mock.patch.dict(
            os.environ,
            {
                "AGENT_DASH_DATA_ROOT": str(self.base / "data"),
                linear.ENV_KEY: FAKE_KEY,
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.fake = FakeLinear(FAKE_KEY)
        self.fake.start()
        self.addCleanup(self.fake.stop)
        self.client = lc.Client(FAKE_KEY, endpoint=self.fake.endpoint)
        self.now = 1000.0
        self.captured: list[str] = []

    def _run(self, fn, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = fn(*args, **kwargs)
        self.captured.extend([out.getvalue(), err.getvalue()])
        return result

    def _link(self, client=None, now=None) -> None:
        rc = self._run(
            linear_sync.link, WS, TEAM_KEY, root=self.root, client=client or self.client, now=self.now if now is None else now
        )
        self.assertEqual(rc, 0)

    def _sync(self, client=None, now=None) -> dict:
        return self._run(
            linear_sync.sync, WS, root=self.root, client=client or self.client, now=self.now if now is None else now
        )

    def _link_record(self) -> dict:
        return linear.read_link(WS, self.root)

    def _ident(self, task_id: str) -> str:
        return self._link_record()["tasks"][task_id]["identifier"]

    def _issue_id(self, task_id: str) -> str:
        return self._link_record()["tasks"][task_id]["id"]

    def _task_status(self, task_id: str) -> str:
        plan = json.loads((self.root / WS / "plan.json").read_text(encoding="utf-8"))
        for group in plan["dag"]["groups"]:
            for task in group["tasks"]:
                if task["id"] == task_id:
                    return task["status"]
        raise KeyError(task_id)

    def _history(self) -> list[dict]:
        path = self.root / WS / "history.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def _approval_status(self, item_id: str) -> str:
        data = json.loads((self.root / WS / "approvals.json").read_text(encoding="utf-8"))
        return next(item["status"] for item in data["items"] if item["id"] == item_id)

    def _set_local(self, task_id: str, status: str) -> None:
        self._run(bundles.set_task, WS, task_id, status, None, None, root=self.root)


class LinkEndToEndTest(LinearE2ETest):
    def test_link_creates_one_project_two_milestones_four_tasks_relations_and_one_approval(self) -> None:
        self._link()
        counts = self.fake.counts()
        self.assertEqual(counts["projects"], 1)
        self.assertEqual(counts["milestones"], 2)
        self.assertEqual(counts["issues"], 5)
        self.assertEqual(counts["relations"], 3)
        self.assertEqual(counts["initiatives"], 1)
        self.assertEqual(counts["project_links"], 1)

        record = self._link_record()
        self.assertEqual(set(record["tasks"]), {"WS4.1", "WS4.2", "WS4.3", "WS4.4"})
        self.assertEqual(set(record["approvals"]), {"A1"})
        expected = {
            (self._issue_id("WS4.1"), self._issue_id("WS4.2"), "blocks"),
            (self._issue_id("WS4.2"), self._issue_id("WS4.3"), "blocks"),
            (self._issue_id("WS4.1"), self._issue_id("WS4.4"), "blocks"),
        }
        self.assertEqual(set(self.fake.relations.values()), expected)
        ids_by_pair = {edge[:2]: rid for rid, edge in self.fake.relations.items()}
        self.assertEqual(
            record["relations"],
            {
                "WS4.1>WS4.2": ids_by_pair[(self._issue_id("WS4.1"), self._issue_id("WS4.2"))],
                "WS4.2>WS4.3": ids_by_pair[(self._issue_id("WS4.2"), self._issue_id("WS4.3"))],
                "WS4.1>WS4.4": ids_by_pair[(self._issue_id("WS4.1"), self._issue_id("WS4.4"))],
            },
        )

        workspace = linear.read_workspace()
        awaiting = self.fake.state_named("Awaiting prod")
        self.assertIsNotNone(awaiting)
        self.assertEqual(awaiting["type"], "started")
        self.assertEqual(workspace["states"]["Awaiting prod"], awaiting["id"])
        label_ids = [label["id"] for label in self.fake.labels.values() if label["name"] == "approval"]
        self.assertEqual(label_ids, [workspace["approval_label"]])
        approval = self.fake.issues[record["approvals"]["A1"]["id"]]
        self.assertEqual(approval["labelIds"], label_ids)
        self.assertEqual(self.fake.states[approval["stateId"]]["name"], "Todo")

    def test_second_link_and_sync_create_nothing(self) -> None:
        self._link()
        before = self.fake.counts()
        self.fake.ops.clear()

        self._link(now=self.now + 1)
        result = self._sync(now=self.now + 2)

        self.assertIsNone(result["error"])
        self.assertEqual(self.fake.counts(), before)
        self.assertFalse(set(self.fake.ops) & WRITE_OPS, self.fake.ops)
        self.assertEqual(result["pushed"], 0)


class SyncEndToEndTest(LinearE2ETest):
    def setUp(self) -> None:
        super().setUp()
        self._link()

    def test_local_wip_moves_issue_to_in_progress(self) -> None:
        self._set_local("WS4.3", "wip")
        result = self._sync(now=self.now + 1)

        self.assertIsNone(result["error"])
        self.assertEqual(result["flushed"], 1)
        self.assertEqual(self.fake.by_identifier(self._ident("WS4.3"))["stateId"], self.fake.state_named("In Progress")["id"])
        self.assertEqual(linear.read_outbox(WS, self.root), [])

    def test_linear_done_applies_locally_with_history_and_clears_outbox(self) -> None:
        self.fake.set_state(self._ident("WS4.4"), "Done")
        result = self._sync(now=self.now + 1)

        self.assertIsNone(result["error"])
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self._task_status("WS4.4"), "done")
        last = self._history()[-1]
        self.assertEqual(last["by"], "linear")
        self.assertEqual(last["task"], "WS4.4")
        self.assertEqual(last["to"], "done")
        self.assertEqual(linear.read_outbox(WS, self.root), [])

    def test_approval_done_in_linear_becomes_approved(self) -> None:
        self.fake.set_state(self._link_record()["approvals"]["A1"]["identifier"], "Done")
        result = self._sync(now=self.now + 1)

        self.assertIsNone(result["error"])
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self._approval_status("A1"), "approved")
        decided = json.loads((self.root / WS / "approvals.json").read_text(encoding="utf-8"))
        self.assertEqual(decided["decided_by"], "linear")
        self.assertEqual(linear.read_outbox(WS, self.root), [])

    def test_rate_limit_sets_last_error_and_next_sync_recovers(self) -> None:
        self.fake.rate_limit_next = 1
        result = self._sync(now=self.now + 50)

        self.assertIsNotNone(result["error"])
        self.assertIn("RATELIMITED", result["error"])
        record = self._link_record()
        self.assertIn("RATELIMITED", record["last_error"])
        self.assertEqual(record["synced_at"], self.now)

        result = self._sync(now=self.now + 60)
        self.assertIsNone(result["error"])
        record = self._link_record()
        self.assertIsNone(record["last_error"])
        self.assertEqual(record["synced_at"], self.now + 60)

    def test_wrong_key_is_an_auth_error_and_is_recorded(self) -> None:
        bad = lc.Client("lin_api_wrong_key", endpoint=self.fake.endpoint)
        result = self._sync(client=bad, now=self.now + 5)

        self.assertIn("AUTH", result["error"])
        self.assertIn("AUTH", self._link_record()["last_error"])
        self.assertEqual(self._link_record()["synced_at"], self.now)

    def test_key_never_reaches_disk_or_output(self) -> None:
        self._set_local("WS4.2", "wip")
        self._sync(now=self.now + 1)
        self.fake.error_next = f"token {FAKE_KEY} was refused"
        result = self._sync(now=self.now + 2)

        self.assertIn("[redacted]", result["error"])
        self.assertNotIn(FAKE_KEY, self._link_record()["last_error"])
        leaks = []
        for path in self.base.rglob("*"):
            if path.is_file() and FAKE_KEY.encode("utf-8") in path.read_bytes():
                leaks.append(str(path))
        self.assertEqual(leaks, [])
        self.assertFalse(any(FAKE_KEY in text for text in self.captured))

    def test_unlink_then_link_adopts_existing_issues(self) -> None:
        before_ids = {key: self._issue_id(key) for key in ("WS4.1", "WS4.2", "WS4.3", "WS4.4")}
        before = self.fake.counts()

        self._run(linear_sync.unlink, WS, root=self.root)
        self.assertFalse(linear.is_linked(WS, self.root))
        self._link(now=self.now + 10)

        self.assertEqual(self.fake.counts(), before)
        self.assertEqual({key: self._issue_id(key) for key in before_ids}, before_ids)


class DeletedAndMidPullEndToEndTest(LinearE2ETest):
    def setUp(self) -> None:
        super().setUp()
        self._link()

    def test_issue_deleted_in_linear_is_detached_and_never_recreated(self) -> None:
        ident = self._ident("WS4.3")
        self.fake.delete_issue(ident)
        issues_after_delete = self.fake.counts()["issues"]
        self.assertEqual(self.fake.counts()["relations"], 2)

        self._set_local("WS4.3", "wip")
        result = self._sync(now=self.now + 1)

        self.assertIsNone(result["error"])
        record = self._link_record()
        self.assertIn("task:WS4.3", record["detached"])
        self.assertNotIn("WS4.3", record["tasks"])
        self.assertIsNone(record["last_error"])
        self.assertEqual(linear.read_outbox(WS, self.root), [])

        self.fake.ops.clear()
        self._set_local("WS4.4", "wip")
        self.assertIsNone(self._sync(now=self.now + 2)["error"])
        self._link(now=self.now + 3)
        self.assertIsNone(self._sync(now=self.now + 4)["error"])

        self.assertNotIn("IssueCreate", self.fake.ops)
        self.assertEqual(self.fake.counts()["issues"], issues_after_delete)
        with self.assertRaises(StopIteration):
            self.fake.by_identifier(ident)
        self.assertEqual(self.fake.counts()["relations"], 2)
        self.assertEqual(self._task_status("WS4.3"), "wip")

    def test_local_write_during_issues_pull_survives_and_reaches_linear(self) -> None:
        ident = self._ident("WS4.3")
        self.fake.set_state(ident, "In Review")

        def local_write() -> None:
            bundles.set_task(WS, "WS4.3", "wip", None, None, root=self.root, origin="local")

        self.fake.during_issues = local_write
        first = self._sync(now=self.now + 1)

        self.assertIsNone(first["error"])
        self.assertEqual(first["applied"], 0)
        self.assertEqual(self._task_status("WS4.3"), "wip")
        self.assertEqual(len(linear.read_outbox(WS, self.root)), 1)

        second = self._sync(now=self.now + 2)
        self.assertIsNone(second["error"])
        self.assertEqual(self._task_status("WS4.3"), "wip")
        self.assertEqual(self.fake.by_identifier(ident)["stateId"], self.fake.state_named("In Progress")["id"])
        self.assertEqual(linear.read_outbox(WS, self.root), [])


if __name__ == "__main__":
    unittest.main()
