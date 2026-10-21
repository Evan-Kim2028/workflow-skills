"""Linear GraphQL client: auth header, error codes, key redaction, rate parsing, idempotent ensures.

Every test uses a fake transport that records each request and returns canned
JSON. Nothing here calls the live Linear API. The one default-transport test
patches urllib.request.urlopen.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
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
        response.read.return_value = b'{"data": {"ok": true}}'
        context = mock.MagicMock()
        context.__enter__.return_value = response
        with mock.patch("urllib.request.urlopen", return_value=context) as urlopen:
            data = lc.Client(KEY).query("query Q { ok }")
        request = urlopen.call_args.args[0]
        self.assertEqual(data, {"ok": True})
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), KEY)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 20.0)


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
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(lc.LinearError) as caught:
                lc.Client(KEY).query("query Q { x }")
        self.assertEqual(caught.exception.code, "AUTH")

    def test_default_transport_timeout_is_network(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("slow")):
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
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError(f"no route {KEY}")):
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


if __name__ == "__main__":
    unittest.main()
