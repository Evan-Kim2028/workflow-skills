"""Linear over MCP: session handling, SSE and JSON replies, adopt-by-marker, the Awaiting prod emulation, relations, error mapping.

Every test talks to a fake MCP server bound to 127.0.0.1. The server keeps
issues, projects, milestones, labels and relations in memory and implements the
tool subset that ``dash.linear_mcp.McpClient`` calls. Nothing here reaches
mcp.linear.app.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import linear, linear_sync  # noqa: E402
from dash.linear_client import LinearError  # noqa: E402
from dash.linear_mcp import AWAITING, McpClient  # noqa: E402

TOKEN = "tok-SECRET-0123456789"
TEAM_KEY = "ENG"
TEAM_ID = str(uuid.UUID("5f0c2a7e-3b1d-4c8e-9a61-2d7f4e8b9c10"))
STATES = [
    ("Todo", "unstarted"),
    ("In Progress", "started"),
    ("In Review", "started"),
    ("Done", "completed"),
    ("Canceled", "canceled"),
]
TOOLS = [
    "list_teams",
    "list_issue_statuses",
    "list_issue_labels",
    "create_issue_label",
    "list_projects",
    "save_project",
    "list_milestones",
    "save_milestone",
    "save_issue",
    "get_issue",
    "list_issues",
    "save_comment",
]
DESC_TASK_1 = "Task `WS4.1` in workstream WS4\n\n- Section: Build\n\n_Spec lives in the agent-dash plan._\n"
DESC_TASK_2 = "Task `WS4.2` in workstream WS4\n\n_Spec lives in the agent-dash plan._\n"


class ToolError(Exception):
    """A tool failure reported in-band with isError true."""


class FakeMcp:
    """In-memory Linear MCP server with session, SSE and failure switches."""

    def __init__(self) -> None:
        self.token = TOKEN
        self.sse = False
        self.session: str | None = None
        self.session_seq = 0
        self.initialize_count = 0
        self.requests: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.next_status: int | None = None
        self.error_text: str | None = None
        self.clock = 0
        self.issue_seq = 0
        self.issues: dict[str, dict] = {}
        self.projects: dict[str, dict] = {}
        self.milestones: dict[str, dict] = {}
        self.labels: dict[str, str] = {}
        self.project_labels: dict[str, str] = {}
        self.project_states = ("Planned", "In Progress", "Completed", "Canceled")
        self.comments: list[tuple[str, str]] = []
        self.states = {name: {"id": str(uuid.uuid4()), "name": name, "type": kind} for name, kind in STATES}
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                status, headers, payload = fake.handle_http(self.path, dict(self.headers.items()), body)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/mcp"

    def expire_session(self) -> None:
        self.session_seq += 1
        self.session = f"expired-{self.session_seq}"

    def handle_http(self, path: str, raw_headers: dict, body: bytes) -> tuple[int, dict, bytes]:
        headers = {str(key).lower(): value for key, value in raw_headers.items()}
        if path == "/redirect":
            return 302, {"Location": "/mcp"}, b""
        if path != "/mcp":
            return 404, {}, b"not here"
        if headers.get("authorization") != f"Bearer {self.token}":
            return 401, {}, b"unauthorized"
        message = json.loads(body.decode("utf-8"))
        method = message.get("method")
        request_id = message.get("id")
        session_header = headers.get("mcp-session-id")
        self.requests.append(
            {"method": method, "session": session_header, "protocol": headers.get("mcp-protocol-version")}
        )
        if method == "initialize":
            self.initialize_count += 1
            self.session_seq += 1
            self.session = f"sess-{self.session_seq}"
            reply = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}},
            }
            return self._reply(reply, {"Mcp-Session-Id": self.session})
        if session_header != self.session:
            return 404, {}, b"session not found"
        if request_id is None:
            return 202, {}, b""
        if method == "tools/list":
            return self._reply({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": n} for n in TOOLS]}}, {})
        if method != "tools/call":
            return self._reply({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}, {})
        if self.next_status is not None:
            status, self.next_status = self.next_status, None
            return status, {}, b""
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        self.calls.append((name, arguments))
        try:
            value = self._tool(name, arguments)
            text = value if isinstance(value, str) else json.dumps(value)
            result = {"content": [{"type": "text", "text": text}]}
        except ToolError as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        if self.error_text is not None:
            result = {"content": [{"type": "text", "text": self.error_text}], "isError": True}
            self.error_text = None
        return self._reply({"jsonrpc": "2.0", "id": request_id, "result": result}, {})

    def _reply(self, reply: dict, extra: dict) -> tuple[int, dict, bytes]:
        if self.sse:
            payload = f"event: message\ndata: {json.dumps(reply)}\n\n".encode("utf-8")
            return 200, {"Content-Type": "text/event-stream", **extra}, payload
        return 200, {"Content-Type": "application/json", **extra}, json.dumps(reply).encode("utf-8")

    def _stamp(self) -> str:
        self.clock += 1
        return f"2026-10-21T10:00:00.{self.clock:03d}Z"

    def _find_issue(self, ident: str) -> dict:
        if ident in self.issues:
            return self.issues[ident]
        for issue in self.issues.values():
            if issue["identifier"] == ident:
                return issue
        raise ToolError(f"Could not find issue {ident}")

    def _project(self, ident: str) -> dict:
        for project in self.projects.values():
            if ident in (project["uuid"], project["name"]):
                return project
        raise ToolError(f"Could not find project \"{ident}\"")

    def _state_name(self, ident: str) -> str:
        for name, state in self.states.items():
            if ident in (name, state["id"]):
                return name
        raise ToolError(f"Could not find state {ident}")

    def _view(self, issue: dict, fields: list[str] | None) -> dict:
        full = {
            "uuid": issue["uuid"],
            "id": issue["identifier"],
            "title": issue["title"],
            "url": issue["url"],
            "description": issue["description"],
            "status": issue["status"],
            "statusType": self.states[issue["status"]]["type"],
            "labels": list(issue["labels"]),
            "updatedAt": issue["updatedAt"],
            "project": issue["project"],
            "projectMilestone": issue["milestone"],
            "priority": issue.get("priority", 0),
        }
        if not fields:
            return full
        return {key: full.get(key) for key in fields}

    def _tool(self, name: str, args: dict) -> object:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            raise ToolError(f"Unknown tool {name}")
        return handler(args)

    def _t_list_teams(self, args: dict) -> dict:
        query = args.get("query")
        teams = [{"id": TEAM_ID, "key": TEAM_KEY, "name": "Engineering"}]
        if query:
            teams = [team for team in teams if query in (team["key"], team["name"])]
        return {"teams": teams, "hasNextPage": False}

    def _check_team(self, ident: object) -> None:
        if ident not in (TEAM_KEY, TEAM_ID):
            raise ToolError("Could not find team")

    def _t_list_issue_statuses(self, args: dict) -> list:
        self._check_team(args.get("team"))
        return list(self.states.values())

    def _t_list_issue_labels(self, args: dict) -> dict:
        rows = [{"id": lid, "name": name} for lid, name in self.labels.items()]
        if args.get("name"):
            rows = [row for row in rows if row["name"] == args["name"]]
        return {"labels": rows, "hasNextPage": False}

    def _t_create_issue_label(self, args: dict) -> dict:
        name = args["name"]
        if name in self.labels.values():
            raise ToolError(f"Label {name} already exists")
        lid = str(uuid.uuid4())
        self.labels[lid] = name
        return {"id": lid, "name": name}

    def _t_list_projects(self, args: dict) -> dict:
        self._check_team(args.get("team"))
        rows = list(self.projects.values())
        if args.get("query"):
            rows = [row for row in rows if args["query"].lower() in row["name"].lower()]
        return {"projects": rows, "hasNextPage": False}

    def _t_save_project(self, args: dict) -> dict:
        if args.get("id"):
            project = self._project(args["id"])
            if "name" in args:
                project["name"] = args["name"]
            if "description" in args:
                project["description"] = args["description"]
            for initiative in args.get("addInitiatives") or []:
                project["initiatives"].append(initiative)
            self._apply_project_meta(project, args)
            return {"uuid": project["uuid"], "name": project["name"], "url": project["url"]}
        pid = str(uuid.uuid4())
        slug = args["name"].lower().replace(" ", "-")
        project = {
            "uuid": pid,
            "name": args["name"],
            "url": f"https://linear.app/example/project/{slug}-{pid[:6]}",
            "description": args.get("description", ""),
            "initiatives": [],
            "links": [],
        }
        self.projects[pid] = project
        return {"id": pid, "url": project["url"], "name": project["name"]}

    def _apply_project_meta(self, project: dict, args: dict) -> None:
        """Store the metadata fields save_project accepts; a status name outside the workspace's list is refused."""
        for key, field in (("summary", "summary"), ("lead", "lead"), ("priority", "priority"), ("startDate", "startDate"), ("targetDate", "targetDate")):
            if key in args:
                project[field] = args[key]
        if "labels" in args:
            project["labels"] = list(args["labels"])
        for link in args.get("links") or []:
            project["links"].append(dict(link))
        if "state" in args:
            if args["state"] not in self.project_states:
                raise ToolError(f"Could not find project status {args['state']}")
            project["status"] = args["state"]

    def _t_get_project(self, args: dict) -> dict:
        project = self._project(args["query"])
        return {"uuid": project["uuid"], "name": project["name"], "url": project["url"], "labels": [{"name": n} for n in project.get("labels", [])]}

    def _t_list_project_labels(self, args: dict) -> dict:
        rows = [{"id": lid, "name": name} for lid, name in self.project_labels.items()]
        if args.get("name"):
            rows = [row for row in rows if row["name"] == args["name"]]
        return {"labels": rows, "hasNextPage": False}

    def _t_save_project_label(self, args: dict) -> dict:
        lid = str(uuid.uuid4())
        self.project_labels[lid] = args["name"]
        return {"id": lid, "name": args["name"]}

    def _t_list_milestones(self, args: dict) -> dict:
        self._project(args["project"])
        rows = [m for m in self.milestones.values() if m["project"] == args["project"]]
        return {"milestones": [{"id": m["id"], "name": m["name"]} for m in rows]}

    def _t_save_milestone(self, args: dict) -> dict:
        self._project(args["project"])
        if args.get("id"):
            milestone = self.milestones[args["id"]]
            milestone["name"] = args.get("name", milestone["name"])
            for key in ("description", "targetDate"):
                if key in args:
                    milestone[key] = args[key]
            return {"id": milestone["id"], "name": milestone["name"]}
        mid = str(uuid.uuid4())
        self.milestones[mid] = {"id": mid, "name": args["name"], "project": args["project"]}
        return {"id": mid, "name": args["name"]}

    def _label_id(self, name: str) -> str:
        for lid, label in self.labels.items():
            if label == name:
                return lid
        raise ToolError(f"Label {name} not found")

    def _t_save_issue(self, args: dict) -> object:
        if "id" in args:
            issue = self._find_issue(args["id"])
            if "title" in args:
                issue["title"] = args["title"]
            if "description" in args:
                issue["description"] = args["description"]
            if "project" in args:
                issue["project"] = self._project(args["project"])["uuid"]
            if "milestone" in args:
                issue["milestone"] = args["milestone"]
            if "state" in args:
                issue["status"] = self._state_name(args["state"])
            if "labels" in args:
                issue["labels"] = [name for name in args["labels"] if self._label_id(name)]
            for name in args.get("addLabels") or []:
                self._label_id(name)
                if name not in issue["labels"]:
                    issue["labels"].append(name)
            if "priority" in args:
                issue["priority"] = args["priority"]
            for name in args.get("removeLabels") or []:
                if name not in issue["labels"]:
                    raise ToolError(f"Label {name} is not on the issue")
                issue["labels"].remove(name)
            for ident in args.get("blockedBy") or []:
                blocker = self._find_issue(ident)["uuid"]
                if blocker not in issue["blockedBy"]:
                    issue["blockedBy"].append(blocker)
            for ident in args.get("removeBlockedBy") or []:
                blocker = self._find_issue(ident)["uuid"]
                if blocker not in issue["blockedBy"]:
                    raise ToolError("Could not find blocking relation")
                issue["blockedBy"].remove(blocker)
            issue["updatedAt"] = self._stamp()
            return {"id": issue["identifier"], "url": issue["url"]}
        if not args.get("team") or not args.get("title"):
            raise ToolError("team and title are required to create an issue")
        self._check_team(args["team"])
        self.issue_seq += 1
        iid = str(uuid.uuid4())
        identifier = f"{TEAM_KEY}-{self.issue_seq}"
        issue = {
            "uuid": iid,
            "identifier": identifier,
            "url": f"https://linear.app/example/issue/{identifier}/x",
            "title": args["title"],
            "description": args.get("description", ""),
            "status": "Todo",
            "labels": [],
            "project": None,
            "milestone": None,
            "blockedBy": [],
            "updatedAt": self._stamp(),
        }
        self.issues[iid] = issue
        if args.get("project"):
            issue["project"] = self._project(args["project"])["uuid"]
        if args.get("milestone"):
            issue["milestone"] = args["milestone"]
        if args.get("state"):
            issue["status"] = self._state_name(args["state"])
        for name in args.get("labels") or []:
            self._label_id(name)
            issue["labels"].append(name)
        return f"Created {identifier}: {issue['url']}"

    def _t_get_issue(self, args: dict) -> dict:
        issue = self._find_issue(args["id"])
        view = self._view(issue, args.get("fields"))
        if args.get("includeRelations"):
            view["blockedBy"] = [{"uuid": u, "id": self.issues[u]["identifier"]} for u in issue["blockedBy"]]
        return view

    def _t_list_issues(self, args: dict) -> dict:
        if args.get("project"):
            project = self._project(args["project"])
            rows = [issue for issue in self.issues.values() if issue["project"] == project["uuid"]]
        else:
            rows = list(self.issues.values())
        if args.get("query"):
            needle = args["query"].lower()
            rows = [i for i in rows if needle in (i["title"] + "\n" + i["description"]).lower()]
        if args.get("updatedAt"):
            rows = [i for i in rows if i["updatedAt"] > args["updatedAt"]]
        rows.sort(key=lambda i: i["updatedAt"])
        start = int(args.get("cursor") or 0)
        limit = int(args.get("limit") or 50)
        page = rows[start : start + limit]
        more = start + limit < len(rows)
        out = {"issues": [self._view(i, args.get("fields")) for i in page], "hasNextPage": more}
        if more:
            out["cursor"] = str(start + limit)
        return out

    def _t_save_comment(self, args: dict) -> dict:
        self._find_issue(args["issueId"])
        self.comments.append((args["issueId"], args["body"]))
        return {"id": str(uuid.uuid4())}


class McpTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeMcp()
        self.fake.start()
        self.addCleanup(self.fake.stop)

    def client(self, token: str = TOKEN, endpoint: str | None = None) -> McpClient:
        return McpClient(token, endpoint=endpoint or self.fake.url, timeout=5, deadline=5, team_key=TEAM_KEY)

    def issues_named(self, marker: str) -> list[dict]:
        return [i for i in self.fake.issues.values() if i["description"].startswith(marker)]


class McpSessionTest(McpTestBase):
    def test_session_id_and_protocol_header_are_sent_after_initialize(self) -> None:
        client = self.client()
        client.team(TEAM_KEY)
        methods = [r["method"] for r in self.fake.requests]
        self.assertEqual(methods[:2], ["initialize", "notifications/initialized"])
        self.assertIsNone(self.fake.requests[0]["session"])
        later = self.fake.requests[2:]
        self.assertTrue(later)
        for request in later:
            self.assertEqual(request["session"], self.fake.session)
            self.assertEqual(request["protocol"], "2025-06-18")
        self.assertEqual(self.fake.initialize_count, 1)

    def test_reinitializes_once_when_session_expires(self) -> None:
        client = self.client()
        client.team(TEAM_KEY)
        self.fake.expire_session()
        states = client.team(TEAM_KEY)["states"]
        self.assertIn("Todo", {state["name"] for state in states})
        self.assertEqual(self.fake.initialize_count, 2)

    def test_sse_reply_is_parsed(self) -> None:
        self.fake.sse = True
        info = self.client().team(TEAM_KEY)
        self.assertEqual(info["key"], TEAM_KEY)
        self.assertEqual(info["id"], TEAM_ID)

    def test_json_reply_is_parsed(self) -> None:
        info = self.client().team(TEAM_KEY)
        self.assertEqual({s["name"] for s in info["states"]}, {name for name, _kind in STATES})

    def test_last_rate_stays_empty(self) -> None:
        client = self.client()
        client.team(TEAM_KEY)
        self.assertEqual(client.last_rate, {})


class McpErrorTest(McpTestBase):
    def test_rejected_token_maps_to_auth_and_never_leaks(self) -> None:
        with self.assertRaises(LinearError) as caught:
            self.client(token="wrong-token-XYZ").team(TEAM_KEY)
        self.assertEqual(caught.exception.code, "AUTH")
        self.assertNotIn("wrong-token-XYZ", str(caught.exception))

    def test_rate_limit_maps_to_ratelimited(self) -> None:
        client = self.client()
        client.team(TEAM_KEY)
        self.fake.next_status = 429
        with self.assertRaises(LinearError) as caught:
            client.issue_exists(str(uuid.uuid4()))
        self.assertEqual(caught.exception.code, "RATELIMITED")

    def test_token_echoed_in_a_tool_error_is_redacted(self) -> None:
        client = self.client()
        client.team(TEAM_KEY)
        self.fake.error_text = f"Internal failure while reading token {TOKEN}"
        with self.assertRaises(LinearError) as caught:
            client.issue_exists(str(uuid.uuid4()))
        self.assertEqual(caught.exception.code, "GRAPHQL")
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIn("[redacted]", caught.exception.message)

    def test_missing_issue_reads_as_not_found_and_false(self) -> None:
        client = self.client()
        self.assertFalse(client.issue_exists(str(uuid.uuid4())))

    def test_redirect_is_refused(self) -> None:
        with self.assertRaises(LinearError) as caught:
            self.client(endpoint=self.fake.url.replace("/mcp", "/redirect")).team(TEAM_KEY)
        self.assertEqual(caught.exception.code, "HTTP")
        self.assertIn("redirect refused", caught.exception.message)

    def test_non_https_remote_endpoint_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            McpClient(TOKEN, endpoint="http://example.com/mcp")


class McpMethodsTest(McpTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.client_one = self.client()
        self.info = self.client_one.team(TEAM_KEY)
        self.team_id = self.info["id"]
        self.state_ids = {state["name"]: state["id"] for state in self.info["states"]}
        self.project_id = self.client_one.ensure_project(
            linear.stable_id("project", "WS4"), "Example cutover", self.team_id, "Workstream WS4"
        )["id"]

    def test_ensure_state_returns_real_ids_and_synthetic_awaiting(self) -> None:
        self.assertEqual(self.client_one.ensure_state(self.team_id, "x", "Todo", "unstarted", "#fff"), self.state_ids["Todo"])
        self.assertEqual(
            self.client_one.ensure_state(self.team_id, "x", AWAITING, "started", "#a855f7"),
            "label:Awaiting prod",
        )

    def test_upsert_creates_then_updates_the_same_real_issue(self) -> None:
        first = self.client_one.upsert_issue(
            linear.stable_id("task", "WS4", "WS4.1"), self.team_id, self.project_id, None,
            "First", DESC_TASK_1, state_id=self.state_ids["Todo"],
        )
        self.assertEqual(set(first), {"id", "identifier", "url"})
        self.assertTrue(first["identifier"].startswith("ENG-"))
        second = self.client_one.upsert_issue(first["id"], self.team_id, self.project_id, None, "First 2", DESC_TASK_1)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.fake.issues), 1)
        self.assertEqual(self.fake.issues[first["id"]]["title"], "First 2")

    def test_adopts_by_marker_when_linear_json_is_lost(self) -> None:
        first = self.client_one.upsert_issue(
            linear.stable_id("task", "WS4", "WS4.1"), self.team_id, self.project_id, None, "First", DESC_TASK_1
        )
        fresh = self.client()
        again = fresh.upsert_issue(
            linear.stable_id("task", "WS4", "WS4.1"), self.team_id, self.project_id, None, "First", DESC_TASK_1
        )
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self.issues_named("Task `WS4.1`")), 1)

    def test_awaiting_prod_round_trip_through_label(self) -> None:
        ref = self.client_one.upsert_issue(
            linear.stable_id("task", "WS4", "WS4.1"), self.team_id, self.project_id, None,
            "First", DESC_TASK_1, state_id=self.state_ids["Todo"],
        )
        awaiting = self.client_one.ensure_state(self.team_id, "x", AWAITING, "started", "#a855f7")
        self.client_one.set_issue_state(ref["id"], awaiting)
        issue = self.fake.issues[ref["id"]]
        self.assertEqual(issue["status"], "In Progress")
        self.assertEqual(issue["labels"], [AWAITING])
        self.assertIn(AWAITING, self.fake.labels.values())
        changes = self.client_one.issues_updated_since(self.project_id, None)
        self.assertEqual(changes[0]["id"], ref["id"])
        self.assertEqual(changes[0]["state"], {"id": "label:Awaiting prod", "name": "Awaiting prod", "type": "started"})
        self.client_one.set_issue_state(ref["id"], self.state_ids["Done"])
        self.assertEqual(issue["status"], "Done")
        self.assertEqual(issue["labels"], [])
        changes = self.client_one.issues_updated_since(self.project_id, None)
        self.assertEqual(changes[0]["state"]["name"], "Done")
        self.assertEqual(changes[0]["state"]["type"], "completed")

    def test_issues_updated_since_filters_by_cursor(self) -> None:
        first = self.client_one.upsert_issue(linear.stable_id("task", "WS4", "A"), self.team_id, self.project_id, None, "A", DESC_TASK_1)
        cursor = self.fake.issues[first["id"]]["updatedAt"]
        self.client_one.upsert_issue(linear.stable_id("task", "WS4", "B"), self.team_id, self.project_id, None, "B", DESC_TASK_2)
        newer = self.client_one.issues_updated_since(self.project_id, cursor)
        self.assertEqual([change["identifier"] for change in newer], [self.fake.issues[k]["identifier"] for k in self.fake.issues if k != first["id"]])

    def test_relation_add_find_and_remove_by_pair(self) -> None:
        a = self.client_one.upsert_issue(linear.stable_id("task", "WS4", "WS4.1"), self.team_id, self.project_id, None, "A", DESC_TASK_1)
        b = self.client_one.upsert_issue(linear.stable_id("task", "WS4", "WS4.2"), self.team_id, self.project_id, None, "B", DESC_TASK_2)
        pair = self.client_one.ensure_blocks(a["id"], b["id"])
        self.assertEqual(pair, f"pair:{a['id']}>{b['id']}")
        self.assertEqual(self.fake.issues[b["id"]]["blockedBy"], [a["id"]])
        self.assertEqual(self.client_one.find_relation(a["id"], b["id"]), pair)
        self.client_one.delete_relation(pair)
        self.assertEqual(self.fake.issues[b["id"]]["blockedBy"], [])
        self.assertIsNone(self.client_one.find_relation(a["id"], b["id"]))
        self.client_one.delete_relation(pair)

    def test_ensure_project_adopts_by_exact_name(self) -> None:
        again = self.client().ensure_project(linear.stable_id("project", "WS4-other"), "Example cutover", self.team_id, "d")
        self.assertEqual(again["id"], self.project_id)
        self.assertEqual(sum(1 for p in self.fake.projects.values() if p["name"] == "Example cutover"), 1)

    def test_ensure_milestone_adopts_by_name(self) -> None:
        first = self.client_one.ensure_milestone(linear.stable_id("milestone", "WS4", "Shape"), self.project_id, "Shape", 0.0)
        second = self.client().ensure_milestone(linear.stable_id("milestone", "WS4", "Other"), self.project_id, "Shape", 0.0)
        self.assertEqual(second, first)
        self.assertEqual(len(self.fake.milestones), 1)

    def test_initiative_is_none_and_linking_is_a_noop(self) -> None:
        initiative = self.client_one.ensure_initiative("Initiative")
        self.assertEqual(initiative, {"id": None, "name": "Initiative", "url": None})
        before = len(self.fake.calls)
        self.client_one.add_project_to_initiative(initiative["id"], self.project_id)
        self.assertEqual(len(self.fake.calls), before)

    def test_label_and_comment_and_exists(self) -> None:
        label_id = self.client_one.ensure_label(self.team_id, "approval")
        self.assertEqual(self.client_one.ensure_label(self.team_id, "approval"), label_id)
        ref = self.client_one.upsert_issue(
            linear.stable_id("approval", "WS4", "A1"), self.team_id, self.project_id, None,
            "Approve", "Approval `A1` for workstream WS4\n", label_ids=[label_id],
        )
        self.assertEqual(self.fake.issues[ref["id"]]["labels"], ["approval"])
        self.client_one.comment(ref["id"], "hello")
        self.assertEqual(self.fake.comments, [(ref["id"], "hello")])
        self.assertTrue(self.client_one.issue_exists(ref["id"]))


class LinearSyncPushOverMcpTest(McpTestBase):
    def test_push_creates_then_is_idempotent_and_adopts_without_ids(self) -> None:
        client = self.client()
        info = client.team(TEAM_KEY)
        states = {state["name"]: state["id"] for state in info["states"]}
        states[AWAITING] = client.ensure_state(info["id"], "x", AWAITING, "started", "#a855f7")
        label_id = client.ensure_label(info["id"], "approval")
        project = client.ensure_project(linear.stable_id("project", "WS4"), "Example cutover", info["id"], "d")
        ws_data = {"team": {"id": info["id"], "key": TEAM_KEY}, "states": states, "approval_label": label_id}
        plan = {
            "dag": {
                "cols": ["Build"],
                "groups": [
                    {
                        "id": "g1",
                        "col": 0,
                        "title": "Build",
                        "tasks": [
                            {"id": "WS4.1", "title": "First", "status": "todo", "deps": []},
                            {"id": "WS4.2", "title": "Second", "status": "wip", "deps": ["WS4.1"]},
                        ],
                    }
                ],
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "WS4").mkdir()
            (root / "WS4" / "bundle.json").write_text(json.dumps({"id": "WS4", "title": "Example cutover"}), encoding="utf-8")
            record = {"project": project, "milestones": {}, "tasks": {}, "approvals": {}, "relations": {}, "spec_hash": None}
            count = linear_sync._push(client, "WS4", root, record, ws_data, plan, 1.0, True)
            self.assertEqual(count, 2)
            first_id = record["tasks"]["WS4.1"]["id"]
            second_id = record["tasks"]["WS4.2"]["id"]
            self.assertEqual(self.fake.issues[second_id]["blockedBy"], [first_id])
            self.assertEqual(record["relations"], {"WS4.1>WS4.2": f"pair:{first_id}>{second_id}"})
            self.assertEqual(linear_sync._push(client, "WS4", root, record, ws_data, plan, 2.0, False), 0)
            self.assertEqual(len(self.fake.issues), 2)
            record["tasks"] = {}
            record["spec_hash"] = None
            self.assertEqual(linear_sync._push(client, "WS4", root, record, ws_data, plan, 3.0, True), 2)
            self.assertEqual(len(self.fake.issues), 2)
            self.assertEqual(record["tasks"]["WS4.1"]["id"], first_id)



class McpMetaPushTest(McpTestBase):
    def _team_and_project(self):
        client = self.client()
        info = client.team(TEAM_KEY)
        project = client.ensure_project(linear.stable_id("project", "WS4"), "Example cutover", info["id"], "d")
        return client, info, project

    def _push(self, client, root, record, ws_data, plan, now, force):
        return linear_sync._push(client, "WS4", root, record, ws_data, plan, now, force)

    def test_project_meta_sets_fields_appends_labels_and_links(self) -> None:
        client, _info, project = self._team_and_project()
        client.update_project_meta(
            project["id"],
            {
                "summary": "Short",
                "description": "Long",
                "priority": 2,
                "start_date": "2026-10-21",
                "target_date": "2026-12-01",
                "labels": ["Infrastructure"],
                "links": [{"title": "Dashboard", "url": "https://example.test/d"}],
                "status": "In Progress",
            },
        )
        stored = self.fake.projects[project["id"]]
        self.assertEqual(stored["summary"], "Short")
        self.assertEqual(stored["description"], "Long")
        self.assertEqual(stored["priority"], 2)
        self.assertEqual((stored["startDate"], stored["targetDate"]), ("2026-10-21", "2026-12-01"))
        self.assertEqual(stored["labels"], ["Infrastructure"])
        self.assertEqual(stored["links"], [{"title": "Dashboard", "url": "https://example.test/d"}])
        self.assertEqual(stored["status"], "In Progress")

    def test_project_labels_and_links_are_append_only(self) -> None:
        client, _info, project = self._team_and_project()
        client.update_project_meta(project["id"], {"labels": ["Infrastructure"]})
        self.fake.projects[project["id"]]["labels"].append("Added in Linear")
        client.update_project_meta(project["id"], {"labels": ["Other"], "links": [{"title": "A", "url": "https://example.test/a"}]})
        client.update_project_meta(project["id"], {"links": [{"title": "B", "url": "https://example.test/b"}]})
        stored = self.fake.projects[project["id"]]
        self.assertEqual(stored["labels"], ["Infrastructure", "Added in Linear", "Other"])
        self.assertEqual([link["url"] for link in stored["links"]], ["https://example.test/a", "https://example.test/b"])
        self.assertIn("Other", self.fake.project_labels.values())

    def test_status_missing_from_the_workspace_is_skipped(self) -> None:
        client, _info, project = self._team_and_project()
        client.update_project_meta(project["id"], {"status": "Paused", "summary": "Still set"})
        stored = self.fake.projects[project["id"]]
        self.assertNotIn("status", stored)
        self.assertEqual(stored["summary"], "Still set")

    def test_milestone_and_issue_meta_keep_existing_labels(self) -> None:
        client, info, project = self._team_and_project()
        states = {state["name"]: state["id"] for state in info["states"]}
        milestone = client.ensure_milestone(linear.stable_id("milestone", "WS4", "Build"), project["id"], "Build", 0.0)
        client.update_milestone_meta(milestone, project["id"], "Build gate", "2026-11-01")
        self.assertEqual(self.fake.milestones[milestone]["description"], "Build gate")
        self.assertEqual(self.fake.milestones[milestone]["targetDate"], "2026-11-01")

        ref = client.upsert_issue(linear.stable_id("task", "WS4", "WS4.1"), info["id"], project["id"], None, "First", DESC_TASK_1, state_id=states["Todo"])
        client.ensure_label(info["id"], "Manual")
        client.set_issue_meta(ref["id"], ["Infrastructure"], 2)
        issue = self.fake.issues[ref["id"]]
        self.assertEqual(sorted(issue["labels"]), ["Infrastructure"])
        self.assertEqual(issue["priority"], 2)

        self.fake.issues[ref["id"]]["labels"].append("Manual")
        client.set_issue_meta(ref["id"], ["Infrastructure", "Manual"], None)
        self.assertEqual(sorted(issue["labels"]), ["Infrastructure", "Manual"])
        self.assertEqual(issue["priority"], 2)

    def test_push_sends_metadata_once_until_it_changes(self) -> None:
        client, info, project = self._team_and_project()
        states = {state["name"]: state["id"] for state in info["states"]}
        states[AWAITING] = client.ensure_state(info["id"], "x", AWAITING, "started", "#a855f7")
        label_id = client.ensure_label(info["id"], "approval")
        ws_data = {"team": {"id": info["id"], "key": TEAM_KEY}, "states": states, "approval_label": label_id}
        plan = {
            "tracker": {"summary": "Short", "issue_labels": ["Infrastructure"]},
            "dag": {
                "cols": [],
                "groups": [
                    {"id": "g1", "tasks": [{"id": "WS4.1", "title": "First", "status": "todo", "deps": []}, {"id": "WS4.2", "title": "Second", "status": "todo", "deps": ["WS4.1"]}]}
                ],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "WS4").mkdir()
            (root / "WS4" / "bundle.json").write_text(json.dumps({"id": "WS4", "title": "Example cutover"}), encoding="utf-8")
            record = {"project": project, "milestones": {}, "tasks": {}, "approvals": {}, "relations": {}, "spec_hash": None}
            self._push(client, root, record, ws_data, plan, 1.0, True)
            stored = self.fake.projects[project["id"]]
            self.assertEqual(stored["summary"], "Short")
            self.assertEqual(stored["status"], "In Progress")
            first_id = record["tasks"]["WS4.1"]["id"]
            self.assertEqual(self.fake.issues[first_id]["priority"], 2)
            self.assertEqual(sorted(self.fake.issues[first_id]["labels"]), ["Infrastructure"])
            summary_calls = lambda: [args for name, args in self.fake.calls if name == "save_project" and "summary" in args]
            self.assertEqual(len(summary_calls()), 1)

            self._push(client, root, record, ws_data, plan, 2.0, False)
            self.assertEqual(len(summary_calls()), 1)

            plan["tracker"]["summary"] = "Changed"
            self._push(client, root, record, ws_data, plan, 3.0, False)
            self.assertEqual(len(summary_calls()), 2)
            self.assertEqual(self.fake.projects[project["id"]]["summary"], "Changed")

            (root / "WS4" / "bundle.json").write_text(json.dumps({"id": "WS4", "title": "Example cutover", "state": "done"}), encoding="utf-8")
            self._push(client, root, record, ws_data, plan, 4.0, False)
            self.assertEqual(self.fake.projects[project["id"]]["status"], "Completed")



class RecentUpdatesMcpTest(unittest.TestCase):
    def test_get_status_updates_is_asked_for_project_updates_since_the_bound(self):
        from unittest import mock

        client = McpClient("tok", transport=lambda *_a, **_k: (200, {}, b""))
        rows = [
            {"id": "u1", "body": "today", "createdAt": "2026-10-21T09:00:00.000Z"},
            {"id": "u0", "body": "old", "createdAt": "2026-10-19T09:00:00.000Z"},
        ]
        with mock.patch.object(client, "_tool", return_value={"statusUpdates": rows, "hasNextPage": False}) as tool:
            out = client.recent_updates("11111111-2222-3333-4444-555555555555", "2026-10-21T00:00:00+00:00")
        name, args = tool.call_args.args
        self.assertEqual(name, "get_status_updates")
        self.assertEqual(args["type"], "project")
        self.assertEqual(args["project"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(args["createdAt"], "2026-10-21T00:00:00+00:00")
        self.assertEqual([row["id"] for row in out], ["u1"])

if __name__ == "__main__":
    unittest.main()
