"""Linear GraphQL client: auth header, error codes, key redaction, rate parsing, idempotent ensures.

Every test uses a fake transport that records each request and returns canned
JSON. Nothing here calls the live Linear API. The one default-transport test
patches the module opener (lc._OPENER.open).
"""

from __future__ import annotations

import io
import json
import re
import sys
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import linear_client as lc  # noqa: E402

KEY = "lin_api_SECRETKEY_0123456789abcdef"


def _ok(data: dict, headers: dict | None = None) -> tuple:
    return 200, headers or {}, json.dumps({"data": data}).encode("utf-8")


def _err(status: int, errors: list, headers: dict | None = None) -> tuple:
    return status, headers or {}, json.dumps({"errors": errors}).encode("utf-8")


def _not_found() -> tuple:
    return _err(200, [{"message": "Entity not found"}])


class FakeTransport:
    """Records each request body and headers, then returns the next canned reply."""

    def __init__(self, replies: list | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict] = []

    def __call__(self, body: bytes, headers: dict) -> tuple:
        self.calls.append({"raw": body, "body": json.loads(body.decode("utf-8")), "headers": dict(headers)})
        if not self.replies:
            return _ok({})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _client(replies: list | None = None) -> tuple[lc.Client, FakeTransport]:
    fake = FakeTransport(replies)
    return lc.Client(KEY, transport=fake), fake


class AuthHeaderTests(unittest.TestCase):
    def test_key_is_sent_raw_without_bearer(self) -> None:
        client, fake = _client([_ok({"x": 1})])
        client.query("query Q { x }")
        headers = fake.calls[0]["headers"]
        self.assertEqual(headers["Authorization"], KEY)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertFalse(headers["Authorization"].startswith("Bearer"))

    def test_body_is_json_with_query_and_variables(self) -> None:
        client, fake = _client([_ok({})])
        client.query("query Q($a: Int) { x }", {"a": 2})
        self.assertEqual(fake.calls[0]["body"], {"query": "query Q($a: Int) { x }", "variables": {"a": 2}})

    def test_default_transport_posts_with_raw_key_header(self) -> None:
        response = mock.MagicMock()
        response.status = 200
        response.headers = {}
        response.read1.side_effect = [b'{"data": {"ok": true}}', b""]
        context = mock.MagicMock()
        context.__enter__.return_value = response
        with mock.patch.object(lc._OPENER, "open", return_value=context) as open_call:
            data = lc.Client(KEY).query("query Q { ok }")
        request = open_call.call_args.args[0]
        self.assertEqual(data, {"ok": True})
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), KEY)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(open_call.call_args.kwargs["timeout"], 20.0)


class ErrorMappingTests(unittest.TestCase):
    def test_status_and_graphql_errors_map_to_codes(self) -> None:
        cases = [
            ("AUTH 401", (401, {}, b""), "AUTH"),
            ("AUTH 403", (403, {}, b""), "AUTH"),
            ("RATELIMITED 429", (429, {}, b""), "RATELIMITED"),
            (
                "RATELIMITED ext",
                _err(400, [{"message": "Rate limit exceeded", "extensions": {"code": "RATELIMITED"}}]),
                "RATELIMITED",
            ),
            (
                "AUTH ext",
                _err(400, [{"message": "bad", "extensions": {"code": "AUTHENTICATION_ERROR"}}]),
                "AUTH",
            ),
            ("NOT_FOUND", _not_found(), "NOT_FOUND"),
            ("GRAPHQL", _err(200, [{"message": "Field x is invalid"}]), "GRAPHQL"),
            ("HTTP 500", (500, {}, b"oops"), "HTTP"),
        ]
        for label, reply, code in cases:
            with self.subTest(label):
                client, _ = _client([reply])
                with self.assertRaises(lc.LinearError) as caught:
                    client.query("query Q { x }")
                self.assertEqual(caught.exception.code, code)
                self.assertIsInstance(caught.exception.message, str)

    def test_transport_failures_map_to_network(self) -> None:
        for exc in (urllib.error.URLError("connection refused"), TimeoutError("timed out")):
            with self.subTest(type(exc).__name__):
                client, _ = _client([exc])
                with self.assertRaises(lc.LinearError) as caught:
                    client.query("query Q { x }")
                self.assertEqual(caught.exception.code, "NETWORK")

    def test_default_transport_http_error_is_mapped(self) -> None:
        error = urllib.error.HTTPError("https://api.linear.app/graphql", 401, "Unauthorized", {}, io.BytesIO(b""))
        with mock.patch.object(lc._OPENER, "open", side_effect=error):
            with self.assertRaises(lc.LinearError) as caught:
                lc.Client(KEY).query("query Q { x }")
        self.assertEqual(caught.exception.code, "AUTH")

    def test_default_transport_timeout_is_network(self) -> None:
        with mock.patch.object(lc._OPENER, "open", side_effect=TimeoutError("slow")):
            with self.assertRaises(lc.LinearError) as caught:
                lc.Client(KEY).query("query Q { x }")
        self.assertEqual(caught.exception.code, "NETWORK")


class KeyRedactionTests(unittest.TestCase):
    def _assert_no_key(self, exc: BaseException) -> None:
        self.assertNotIn(KEY, str(exc))
        self.assertNotIn(KEY, repr(exc))
        self.assertNotIn(KEY, getattr(exc, "message", ""))

    def test_key_echoed_in_graphql_message_is_scrubbed(self) -> None:
        client, _ = _client([_err(200, [{"message": f"bad credential {KEY}"}])])
        with self.assertRaises(lc.LinearError) as caught:
            client.query("query Q { x }")
        self._assert_no_key(caught.exception)
        self.assertIn("[redacted]", caught.exception.message)

    def test_key_in_transport_exception_is_not_surfaced(self) -> None:
        client, _ = _client([OSError(f"socket for {KEY} dropped")])
        with self.assertRaises(lc.LinearError) as caught:
            client.query("query Q { x }")
        self._assert_no_key(caught.exception)

    def test_auth_failure_message_has_no_key(self) -> None:
        client, _ = _client([(401, {}, f"echo {KEY}".encode("utf-8"))])
        with self.assertRaises(lc.LinearError) as caught:
            client.query("query Q { x }")
        self._assert_no_key(caught.exception)

    def test_default_transport_error_has_no_key(self) -> None:
        with mock.patch.object(lc._OPENER, "open", side_effect=urllib.error.URLError(f"no route {KEY}")):
            with self.assertRaises(lc.LinearError) as caught:
                lc.Client(KEY).query("query Q { x }")
        self._assert_no_key(caught.exception)

    def test_key_is_not_in_request_body(self) -> None:
        client, fake = _client([_ok({})])
        client.query("query Q { x }", {"k": "v"})
        self.assertNotIn(KEY, fake.calls[0]["raw"].decode("utf-8"))


class RateTests(unittest.TestCase):
    def test_last_rate_is_parsed_case_insensitively(self) -> None:
        client, _ = _client(
            [
                _ok(
                    {},
                    {
                        "X-RateLimit-Requests-Remaining": "1499",
                        "x-ratelimit-complexity-remaining": "9000",
                        "X-RateLimit-Requests-Reset": "1700000000000",
                    },
                )
            ]
        )
        client.query("query Q { x }")
        self.assertEqual(
            client.last_rate,
            {"requests_remaining": 1499, "complexity_remaining": 9000, "reset": 1700000000000},
        )

    def test_absent_or_bad_headers_keep_previous_values(self) -> None:
        client, _ = _client(
            [
                _ok({}, {"X-RateLimit-Requests-Remaining": "10"}),
                _ok({}, {"X-RateLimit-Requests-Remaining": "not-a-number"}),
                _ok({}),
            ]
        )
        client.query("query Q { x }")
        client.query("query Q { x }")
        client.query("query Q { x }")
        self.assertEqual(client.last_rate, {"requests_remaining": 10})


class TeamTests(unittest.TestCase):
    def test_team_returns_id_and_states(self) -> None:
        reply = _ok(
            {
                "teams": {
                    "nodes": [
                        {
                            "id": "t1",
                            "key": "ENG",
                            "states": {"nodes": [{"id": "s1", "name": "Todo", "type": "unstarted"}]},
                        }
                    ]
                }
            }
        )
        client, fake = _client([reply])
        team = client.team("ENG")
        self.assertEqual(team, {"id": "t1", "key": "ENG", "states": [{"id": "s1", "name": "Todo", "type": "unstarted"}]})
        self.assertIn("teams(filter:", fake.calls[0]["body"]["query"])
        self.assertEqual(fake.calls[0]["body"]["variables"], {"key": "ENG"})

    def test_missing_team_is_not_found(self) -> None:
        client, _ = _client([_ok({"teams": {"nodes": []}})])
        with self.assertRaises(lc.LinearError) as caught:
            client.team("NOPE")
        self.assertEqual(caught.exception.code, "NOT_FOUND")


class UpsertIssueTests(unittest.TestCase):
    def test_update_path_never_sends_state(self) -> None:
        reply = _ok({"issueUpdate": {"success": True, "issue": {"id": "i1", "identifier": "ENG-1", "url": "https://l/1"}}})
        client, fake = _client([reply])
        result = client.upsert_issue("i1", "t1", "p1", "m1", "Title", "Body", state_id="s1", label_ids=["l1"])
        self.assertEqual(result, {"id": "i1", "identifier": "ENG-1", "url": "https://l/1"})
        self.assertEqual(len(fake.calls), 1)
        body = fake.calls[0]["body"]
        self.assertIn("issueUpdate(id:", body["query"])
        self.assertNotIn("issueCreate", body["query"])
        self.assertEqual(body["variables"]["id"], "i1")
        self.assertEqual(body["variables"]["input"]["title"], "Title")
        self.assertNotIn("stateId", body["variables"]["input"])
        self.assertNotIn("teamId", body["variables"]["input"])

    def test_create_on_not_found_uses_client_id_and_state(self) -> None:
        created = _ok({"issueCreate": {"success": True, "issue": {"id": "i2", "identifier": "ENG-2", "url": "https://l/2"}}})
        client, fake = _client([_not_found(), created])
        result = client.upsert_issue("i2", "t1", "p1", "m1", "Title", "Body", state_id="s1", label_ids=["l1"])
        self.assertEqual(result["identifier"], "ENG-2")
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("issueUpdate(id:", fake.calls[0]["body"]["query"])
        create = fake.calls[1]["body"]
        self.assertIn("issueCreate(input:", create["query"])
        payload = create["variables"]["input"]
        self.assertEqual(payload["id"], "i2")
        self.assertEqual(payload["teamId"], "t1")
        self.assertEqual(payload["stateId"], "s1")
        self.assertEqual(payload["labelIds"], ["l1"])
        self.assertEqual(payload["projectMilestoneId"], "m1")

    def test_other_errors_are_not_swallowed(self) -> None:
        client, fake = _client([_err(200, [{"message": "Field is invalid"}])])
        with self.assertRaises(lc.LinearError) as caught:
            client.upsert_issue("i3", "t1", "p1", None, "T", "D")
        self.assertEqual(caught.exception.code, "GRAPHQL")
        self.assertEqual(len(fake.calls), 1)


class PaginationTests(unittest.TestCase):
    def test_issues_updated_since_follows_two_pages(self) -> None:
        page1 = _ok(
            {
                "issues": {
                    "nodes": [{"id": "a"}, {"id": "b"}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                }
            }
        )
        page2 = _ok({"issues": {"nodes": [{"id": "c"}], "pageInfo": {"hasNextPage": False, "endCursor": None}}})
        client, fake = _client([page1, page2])
        issues = client.issues_updated_since("p1", "2026-10-20T00:00:00.000Z")
        self.assertEqual([issue["id"] for issue in issues], ["a", "b", "c"])
        self.assertEqual(len(fake.calls), 2)
        first, second = fake.calls[0]["body"], fake.calls[1]["body"]
        self.assertIn("orderBy: updatedAt", first["query"])
        self.assertIn("pageInfo { hasNextPage endCursor }", first["query"])
        self.assertEqual(first["variables"]["first"], 100)
        self.assertIsNone(first["variables"]["after"])
        self.assertEqual(first["variables"]["filter"]["project"], {"id": {"eq": "p1"}})
        self.assertEqual(first["variables"]["filter"]["updatedAt"], {"gt": "2026-10-20T00:00:00.000Z"})
        self.assertEqual(second["variables"]["after"], "c1")

    def test_no_since_omits_updated_filter(self) -> None:
        client, fake = _client([_ok({"issues": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}})])
        self.assertEqual(client.issues_updated_since("p1", None), [])
        self.assertNotIn("updatedAt", fake.calls[0]["body"]["variables"]["filter"])


class EnsureTests(unittest.TestCase):
    def test_ensure_label_finds_existing_without_create(self) -> None:
        client, fake = _client([_ok({"issueLabels": {"nodes": [{"id": "L1", "name": "approval"}]}})])
        self.assertEqual(client.ensure_label("t1", "approval"), "L1")
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("issueLabels(filter:", fake.calls[0]["body"]["query"])

    def test_ensure_label_creates_when_missing(self) -> None:
        created = _ok({"issueLabelCreate": {"success": True, "issueLabel": {"id": "L2"}}})
        client, fake = _client([_ok({"issueLabels": {"nodes": []}}), created])
        self.assertEqual(client.ensure_label("t1", "approval"), "L2")
        self.assertEqual(len(fake.calls), 2)
        body = fake.calls[1]["body"]
        self.assertIn("issueLabelCreate(input:", body["query"])
        self.assertEqual(body["variables"]["input"], {"teamId": "t1", "name": "approval"})

    def test_ensure_state_matches_by_id_then_creates_with_id(self) -> None:
        states = {"states": {"nodes": [{"id": "s1", "name": "Todo", "type": "unstarted"}]}}
        created = _ok({"workflowStateCreate": {"success": True, "workflowState": {"id": "s9"}}})
        client, fake = _client([_ok({"team": states}), _ok({"team": states}), created])
        self.assertEqual(client.ensure_state("t1", "s1", "Todo", "unstarted", "#aaa"), "s1")
        self.assertEqual(client.ensure_state("t1", "s9", "Awaiting prod", "started", "#a855f7"), "s9")
        body = fake.calls[2]["body"]
        self.assertIn("workflowStateCreate(input:", body["query"])
        self.assertEqual(body["variables"]["input"]["id"], "s9")
        self.assertEqual(body["variables"]["input"]["type"], "started")

    def test_ensure_initiative_finds_or_creates(self) -> None:
        found = _ok({"initiatives": {"nodes": [{"id": "I1", "name": "Example program", "url": "u1"}]}})
        client, fake = _client([found])
        self.assertEqual(
            client.ensure_initiative("Example program"),
            {"id": "I1", "name": "Example program", "url": "u1"},
        )
        self.assertEqual(len(fake.calls), 1)
        created = _ok({"initiativeCreate": {"success": True, "initiative": {"id": "I2", "name": "New", "url": "u2"}}})
        client, fake = _client([_ok({"initiatives": {"nodes": []}}), created])
        self.assertEqual(client.ensure_initiative("New")["id"], "I2")
        self.assertEqual(fake.calls[1]["body"]["variables"]["input"], {"name": "New"})

    def test_ensure_project_updates_existing(self) -> None:
        reply = _ok({"projectUpdate": {"success": True, "project": {"id": "p1", "name": "Notes cutover", "url": "pu"}}})
        client, fake = _client([reply])
        result = client.ensure_project("p1", "Notes cutover", "t1", "desc")
        self.assertEqual(result, {"id": "p1", "url": "pu", "name": "Notes cutover"})
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("projectUpdate(id:", fake.calls[0]["body"]["query"])

    def test_ensure_project_creates_with_client_id_on_not_found(self) -> None:
        created = _ok({"projectCreate": {"success": True, "project": {"id": "p1", "name": "Notes cutover", "url": "pc"}}})
        client, fake = _client([_not_found(), created])
        result = client.ensure_project("p1", "Notes cutover", "t1", "desc")
        self.assertEqual(result["url"], "pc")
        body = fake.calls[1]["body"]
        self.assertIn("projectCreate(input:", body["query"])
        self.assertEqual(body["variables"]["input"]["id"], "p1")
        self.assertEqual(body["variables"]["input"]["teamIds"], ["t1"])

    def test_ensure_milestone_reuses_existing_id(self) -> None:
        listing = _ok({"project": {"projectMilestones": {"nodes": [{"id": "m1", "name": "Wave 1"}]}}})
        client, fake = _client([listing])
        self.assertEqual(client.ensure_milestone("m1", "p1", "Wave 1", 1.0), "m1")
        self.assertEqual(len(fake.calls), 1)

    def test_ensure_milestone_creates_when_missing(self) -> None:
        created = _ok({"projectMilestoneCreate": {"success": True, "projectMilestone": {"id": "m2"}}})
        client, fake = _client([_ok({"project": {"projectMilestones": {"nodes": []}}}), created])
        self.assertEqual(client.ensure_milestone("m2", "p1", "Wave 2", 2.0), "m2")
        payload = fake.calls[1]["body"]["variables"]["input"]
        self.assertEqual(payload, {"id": "m2", "projectId": "p1", "name": "Wave 2", "sortOrder": 2.0})

    def test_add_project_to_initiative_tolerates_duplicate(self) -> None:
        client, fake = _client([_err(200, [{"message": "Initiative already contains this project"}])])
        client.add_project_to_initiative("I1", "p1")
        self.assertIn("initiativeToProjectCreate(input:", fake.calls[0]["body"]["query"])
        self.assertEqual(fake.calls[0]["body"]["variables"]["input"], {"initiativeId": "I1", "projectId": "p1"})

    def test_add_project_to_initiative_raises_other_errors(self) -> None:
        client, _ = _client([_err(200, [{"message": "Forbidden"}])])
        with self.assertRaises(lc.LinearError) as caught:
            client.add_project_to_initiative("I1", "p1")
        self.assertEqual(caught.exception.code, "GRAPHQL")

    def test_ensure_blocks_tolerates_duplicate_relation(self) -> None:
        client, fake = _client([_err(200, [{"message": "Relation already exists"}])])
        client.ensure_blocks("i-blocker", "i-blocked")
        body = fake.calls[0]["body"]
        self.assertIn("issueRelationCreate(input:", body["query"])
        self.assertEqual(
            body["variables"]["input"],
            {"issueId": "i-blocker", "relatedIssueId": "i-blocked", "type": "blocks"},
        )

    def test_ensure_blocks_raises_other_errors(self) -> None:
        client, _ = _client([_err(200, [{"message": "Issue is archived"}])])
        with self.assertRaises(lc.LinearError):
            client.ensure_blocks("a", "b")

    def test_comment_and_state_use_expected_operations(self) -> None:
        client, fake = _client([_ok({"commentCreate": {"success": True}}), _ok({"issueUpdate": {"success": True}})])
        client.comment("i1", "hello")
        client.set_issue_state("i1", "s1")
        self.assertIn("commentCreate(input:", fake.calls[0]["body"]["query"])
        self.assertEqual(fake.calls[0]["body"]["variables"], {"input": {"issueId": "i1", "body": "hello"}})
        self.assertIn("issueUpdate(id:", fake.calls[1]["body"]["query"])
        self.assertEqual(fake.calls[1]["body"]["variables"], {"id": "i1", "input": {"stateId": "s1"}})



class RelationAndPagingTests(unittest.TestCase):
    def test_ensure_blocks_returns_relation_id(self) -> None:
        reply = _ok({"issueRelationCreate": {"success": True, "issueRelation": {"id": "rel-1"}}})
        client, fake = _client([reply])
        self.assertEqual(client.ensure_blocks("a", "b"), "rel-1")
        self.assertIn("issueRelation { id }", fake.calls[0]["body"]["query"])

    def test_ensure_blocks_returns_none_on_duplicate(self) -> None:
        client, _ = _client([_err(200, [{"message": "Relation already exists"}])])
        self.assertIsNone(client.ensure_blocks("a", "b"))

    def test_delete_relation_sends_id_and_tolerates_not_found(self) -> None:
        client, fake = _client([_ok({"issueRelationDelete": {"success": True}}), _not_found()])
        client.delete_relation("rel-9")
        self.assertIn("issueRelationDelete(id:", fake.calls[0]["body"]["query"])
        self.assertEqual(fake.calls[0]["body"]["variables"], {"id": "rel-9"})
        client.delete_relation("rel-gone")
        self.assertEqual(len(fake.calls), 2)

    def test_delete_relation_raises_other_errors(self) -> None:
        client, _ = _client([_err(200, [{"message": "Field is invalid"}])])
        with self.assertRaises(lc.LinearError) as caught:
            client.delete_relation("rel-1")
        self.assertEqual(caught.exception.code, "GRAPHQL")

    def test_issues_query_requests_state_id(self) -> None:
        client, fake = _client([_ok({"issues": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}})])
        client.issues_updated_since("p1", None)
        self.assertIn("state { id name type }", fake.calls[0]["body"]["query"])

    def test_repeated_cursor_raises(self) -> None:
        page = {"issues": {"nodes": [{"id": "a"}], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}}
        client, fake = _client([_ok(page), _ok(page), _ok(page)])
        with self.assertRaises(lc.LinearError) as caught:
            client.issues_updated_since("p1", None)
        self.assertEqual(caught.exception.code, "GRAPHQL")
        self.assertEqual(len(fake.calls), 2)

    def test_page_cap_raises_after_fifty_pages(self) -> None:
        replies = [
            _ok({"issues": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": f"c{index}"}}})
            for index in range(lc.MAX_PAGES + 5)
        ]
        client, fake = _client(replies)
        with self.assertRaises(lc.LinearError) as caught:
            client.issues_updated_since("p1", None)
        self.assertEqual(caught.exception.code, "GRAPHQL")
        self.assertEqual(len(fake.calls), lc.MAX_PAGES)

class _LocalServer:
    """A 127.0.0.1 HTTP server on a free port. ``hits`` records each POST it receives."""

    def __init__(self, respond) -> None:
        handler = type(
            "Handler",
            (BaseHTTPRequestHandler,),
            {"do_POST": _post_hook(respond), "log_message": lambda self, *args: None},
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.server.hits = []
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/graphql"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _post_hook(respond):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.hits.append(dict(self.headers.items()))
        respond(self)

    return do_POST


def _json_reply(data: dict):
    def respond(handler) -> None:
        raw = json.dumps({"data": data}).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    return respond


class TransportHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.servers: list[_LocalServer] = []

    def tearDown(self) -> None:
        for server in self.servers:
            server.close()

    def _serve(self, respond) -> _LocalServer:
        server = _LocalServer(respond)
        self.servers.append(server)
        return server

    def test_redirect_is_refused_and_target_receives_nothing(self) -> None:
        target = self._serve(_json_reply({"ok": True}))

        def redirect(handler) -> None:
            handler.send_response(302)
            handler.send_header("Location", target.url)
            handler.send_header("Content-Length", "0")
            handler.end_headers()

        origin = self._serve(redirect)
        client = lc.Client(KEY, endpoint=origin.url)
        with self.assertRaises(lc.LinearError) as caught:
            client.query("query Q { ok }")
        self.assertEqual(caught.exception.code, "HTTP")
        self.assertEqual(caught.exception.message, "redirect refused")
        self.assertEqual(target.server.hits, [])
        self.assertEqual(len(origin.server.hits), 1)

    def test_trickling_body_hits_total_deadline(self) -> None:
        def trickle(handler) -> None:
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", "100000")
            handler.end_headers()
            try:
                for _ in range(50):
                    handler.wfile.write(b" ")
                    handler.wfile.flush()
                    time.sleep(0.2)
            except OSError:
                pass

        server = self._serve(trickle)
        client = lc.Client(KEY, endpoint=server.url, timeout=5.0, deadline=1.0)
        started = time.monotonic()
        with self.assertRaises(lc.LinearError) as caught:
            client.query("query Q { ok }")
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "NETWORK")
        self.assertEqual(caught.exception.message, "deadline exceeded")
        self.assertLess(elapsed, 1.0 + 2.0)

    def test_oversized_body_is_refused(self) -> None:
        size = lc.MAX_BODY_BYTES + 1

        def oversized(handler) -> None:
            handler.send_response(200)
            handler.send_header("Content-Length", str(size))
            handler.end_headers()
            try:
                handler.wfile.write(b"x" * size)
            except OSError:
                pass

        server = self._serve(oversized)
        with self.assertRaises(lc.LinearError) as caught:
            lc.Client(KEY, endpoint=server.url).query("query Q { ok }")
        self.assertEqual(caught.exception.code, "HTTP")
        self.assertEqual(caught.exception.message, "response too large")

    def test_non_https_endpoint_is_rejected_at_construction(self) -> None:
        for endpoint in ("http://example.com/graphql", "ftp://127.0.0.1/graphql", "https:///graphql"):
            with self.subTest(endpoint):
                with self.assertRaises(ValueError):
                    lc.Client(KEY, endpoint=endpoint)

    def test_https_and_localhost_http_endpoints_are_accepted(self) -> None:
        lc.Client(KEY, endpoint="https://api.linear.app/graphql")
        server = self._serve(_json_reply({"ok": True}))
        self.assertTrue(server.url.startswith("http://127.0.0.1:"))
        self.assertEqual(lc.Client(KEY, endpoint=server.url).query("query Q { ok }"), {"ok": True})

    def test_key_never_appears_in_raised_messages(self) -> None:
        leaking = self._serve(lambda handler: handler.send_response(500) or handler.end_headers() or handler.wfile.write(f"bad {KEY}".encode("utf-8")))
        cases = [
            ("http 500 echo", lambda: lc.Client(KEY, endpoint=leaking.url).query("query Q { ok }")),
            ("refused", lambda: lc.Client(KEY, endpoint="http://127.0.0.1:1/graphql").query("query Q { ok }")),
        ]
        for label, call in cases:
            with self.subTest(label):
                with self.assertRaises(lc.LinearError) as caught:
                    call()
                self.assertNotIn(KEY, str(caught.exception))
                self.assertNotIn(KEY, repr(caught.exception))
        scrubbed = lc._transport_failure(OSError(f"socket for {KEY} dropped"), KEY)
        self.assertNotIn(KEY, scrubbed.message)
        self.assertIn("[redacted]", scrubbed.message)


class LookupAndStreamTests(unittest.TestCase):
    def test_find_relation_returns_the_blocks_id_to_the_blocked_issue(self) -> None:
        reply = _ok(
            {
                "issue": {
                    "relations": {
                        "nodes": [
                            {"id": "rel-other", "type": "related", "relatedIssue": {"id": "i-blocked"}},
                            {"id": "rel-wrong", "type": "blocks", "relatedIssue": {"id": "i-else"}},
                            {"id": "rel-hit", "type": "blocks", "relatedIssue": {"id": "i-blocked"}},
                        ]
                    }
                }
            }
        )
        client, fake = _client([reply])
        self.assertEqual(client.find_relation("i-blocker", "i-blocked"), "rel-hit")
        body = fake.calls[0]["body"]
        self.assertIn("relations(", body["query"])
        self.assertIn("relatedIssue { id }", body["query"])
        self.assertEqual(body["variables"], {"id": "i-blocker"})

    def test_find_relation_returns_none_when_absent_or_blocker_gone(self) -> None:
        client, _ = _client([_ok({"issue": {"relations": {"nodes": []}}})])
        self.assertIsNone(client.find_relation("i-blocker", "i-blocked"))
        client, _ = _client([_not_found()])
        self.assertIsNone(client.find_relation("i-blocker", "i-blocked"))

    def test_find_relation_raises_other_errors(self) -> None:
        client, _ = _client([_err(200, [{"message": "Field is invalid"}])])
        with self.assertRaises(lc.LinearError) as caught:
            client.find_relation("a", "b")
        self.assertEqual(caught.exception.code, "GRAPHQL")

    def test_issue_exists_is_true_for_data_and_false_for_not_found(self) -> None:
        client, fake = _client([_ok({"issue": {"id": "i-1"}}), _not_found(), _ok({"issue": None})])
        self.assertTrue(client.issue_exists("i-1"))
        self.assertIn("issue(id:", fake.calls[0]["body"]["query"])
        self.assertEqual(fake.calls[0]["body"]["variables"], {"id": "i-1"})
        self.assertFalse(client.issue_exists("i-gone"))
        self.assertFalse(client.issue_exists("i-null"))

    def test_issue_exists_reraises_other_errors(self) -> None:
        client, _ = _client([_err(429, [])])
        with self.assertRaises(lc.LinearError) as caught:
            client.issue_exists("i-1")
        self.assertEqual(caught.exception.code, "RATELIMITED")

    def test_stream_without_read_is_a_network_error(self) -> None:
        class Unreadable:
            pass

        with self.assertRaises(lc.LinearError) as caught:
            lc._read_capped(Unreadable(), time.monotonic() + 5)
        self.assertEqual(caught.exception.code, "NETWORK")
        self.assertNotIn(KEY, caught.exception.message)

    def test_stream_without_read_through_transport_is_a_network_error(self) -> None:
        class Opened:
            status = 200
            headers: dict = {}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        client = lc.Client(KEY, transport=lc._urllib_transport("https://api.linear.app/graphql", 5.0, 5.0, KEY))
        with mock.patch.object(lc._OPENER, "open", return_value=Opened()):
            with self.assertRaises(lc.LinearError) as caught:
                client.query("query Q { ok }")
        self.assertEqual(caught.exception.code, "NETWORK")



class RouteTransport:
    """Answers each GraphQL operation from a handler table, by operation name, and records every request."""

    def __init__(self, handlers: dict) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, body: bytes, headers: dict) -> tuple:
        payload = json.loads(body.decode("utf-8"))
        name = re.search(r"\b(?:query|mutation)\s+(\w+)", payload["query"]).group(1)
        self.calls.append((name, payload["variables"]))
        handler = self.handlers.get(name)
        data = handler(payload["variables"]) if handler is not None else {}
        return _ok(data)

    def inputs(self, name: str) -> list[dict]:
        return [variables.get("input", variables) for op, variables in self.calls if op == name]


class MetaClientTests(unittest.TestCase):
    def _client(self, handlers: dict) -> tuple[lc.Client, RouteTransport]:
        transport = RouteTransport(handlers)
        return lc.Client(KEY, transport=transport), transport

    def test_project_meta_maps_summary_and_content_and_resolves_ids(self) -> None:
        handlers = {
            "ProjectStatuses": lambda v: {"projectStatuses": {"nodes": [{"id": "st-wip", "name": "In Progress"}]}},
            "UserByName": lambda v: {"users": {"nodes": [{"id": "user-evan", "name": "Evan Kim", "displayName": "evan"}]}},
            "ProjectDetail": lambda v: {"project": {"labels": {"nodes": [{"id": "pl-keep", "name": "Manual"}]}}},
            "ProjectLabelByName": lambda v: {"projectLabels": {"nodes": []}},
            "ProjectLabelCreate": lambda v: {"projectLabelCreate": {"success": True, "projectLabel": {"id": "pl-new"}}},
            "ProjectUpdate": lambda v: {"projectUpdate": {"success": True, "project": {"id": v["id"], "name": "x", "url": "u"}}},
            "EntityExternalLinkCreate": lambda v: {"entityExternalLinkCreate": {"success": True}},
        }
        client, transport = self._client(handlers)
        client.update_project_meta(
            "proj-1",
            {
                "summary": "Short",
                "description": "Long",
                "lead": "Evan Kim",
                "priority": 2,
                "start_date": "2026-10-21",
                "target_date": "2026-12-01",
                "labels": ["Infrastructure"],
                "links": [{"title": "Dashboard", "url": "https://example.test/d"}],
                "status": "In Progress",
            },
        )
        update = transport.inputs("ProjectUpdate")[0]
        self.assertEqual(
            update,
            {
                "description": "Short",
                "content": "Long",
                "priority": 2,
                "startDate": "2026-10-21",
                "targetDate": "2026-12-01",
                "leadId": "user-evan",
                "labelIds": ["pl-keep", "pl-new"],
                "statusId": "st-wip",
            },
        )
        self.assertEqual(
            transport.inputs("EntityExternalLinkCreate"),
            [{"projectId": "proj-1", "url": "https://example.test/d", "label": "Dashboard"}],
        )

    def test_unknown_status_and_lead_are_skipped_not_failed(self) -> None:
        handlers = {
            "ProjectStatuses": lambda v: {"projectStatuses": {"nodes": [{"id": "st-wip", "name": "In Progress"}]}},
            "UserByName": lambda v: {"users": {"nodes": []}},
            "ProjectUpdate": lambda v: {"projectUpdate": {"success": True, "project": {"id": "p", "name": "x", "url": "u"}}},
        }
        client, transport = self._client(handlers)
        client.update_project_meta("proj-1", {"summary": "Still set", "status": "Paused", "lead": "Nobody"})
        self.assertEqual(transport.inputs("ProjectUpdate"), [{"description": "Still set"}])

    def test_milestone_meta_sends_only_set_fields(self) -> None:
        client, transport = self._client({"ProjectMilestoneUpdate": lambda v: {"projectMilestoneUpdate": {"success": True}}})
        client.update_milestone_meta("ms-1", "proj-1", "Gate", None)
        client.update_milestone_meta("ms-1", "proj-1", None, None)
        self.assertEqual(transport.inputs("ProjectMilestoneUpdate"), [{"description": "Gate"}])

    def test_issue_meta_appends_labels_and_sets_priority(self) -> None:
        handlers = {
            "IssueLabels": lambda v: {"issue": {"team": {"id": "team-1"}, "labels": {"nodes": [{"id": "lab-keep", "name": "Manual"}]}}},
            "LabelByName": lambda v: {"issueLabels": {"nodes": {"Infrastructure": [{"id": "lab-infra", "name": "Infrastructure"}], "Manual": [{"id": "lab-keep", "name": "Manual"}]}.get(v["name"], [])}},
            "IssueUpdate": lambda v: {"issueUpdate": {"success": True, "issue": {"id": v["id"], "identifier": "ENG-1", "url": "u"}}},
        }
        client, transport = self._client(handlers)
        client.set_issue_meta("issue-1", ["Infrastructure", "Manual"], 2)
        self.assertEqual(transport.inputs("IssueUpdate"), [{"labelIds": ["lab-keep", "lab-infra"], "priority": 2}])

    def test_issue_meta_with_nothing_to_change_sends_no_update(self) -> None:
        handlers = {
            "IssueLabels": lambda v: {"issue": {"team": {"id": "team-1"}, "labels": {"nodes": [{"id": "lab-keep", "name": "Manual"}]}}},
            "LabelByName": lambda v: {"issueLabels": {"nodes": [{"id": "lab-keep", "name": "Manual"}]}},
        }
        client, transport = self._client(handlers)
        client.set_issue_meta("issue-1", ["Manual"], None)
        self.assertEqual([name for name, _ in transport.calls], ["IssueLabels", "LabelByName"])


if __name__ == "__main__":
    unittest.main()
