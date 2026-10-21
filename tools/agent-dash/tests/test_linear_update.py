"""Linear standup update: body sections and limits, health precedence, dry run, recording, the daily rule, client request shapes.

Every test uses a fake client or a fake transport. Nothing here calls the live Linear API.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import bundles, errors, linear, linear_update  # noqa: E402
from dash import linear_client as lc  # noqa: E402
from dash import linear_mcp  # noqa: E402
from dash.linear_client import LinearError  # noqa: E402
from dash.sources import linear as source  # noqa: E402
from tests.test_linear_client import KEY, FakeTransport, _ok  # noqa: E402

NOW = datetime(2026, 10, 21, 10, 0).timestamp()
PROJECT_ID = "11111111-2222-3333-4444-555555555555"
ISSUE_URL = "https://linear.app/acme/issue/ENG-2"

PLAN = {
    "workstream": "WS9",
    "narrative": {"thesis": "Move the notes store to Postgres."},
    "dag": {
        "cols": ["Build"],
        "groups": [
            {
                "id": "g1",
                "col": 0,
                "title": "Build",
                "tasks": [
                    {"id": "WS9.1", "title": "Draft the schema", "status": "done", "deps": []},
                    {"id": "WS9.2", "title": "Wire the loader", "status": "wip", "deps": ["WS9.1"]},
                    {"id": "WS9.3", "title": "Review the loader", "status": "pr", "deps": ["WS9.2"]},
                    {"id": "WS9.4", "title": "Load to production", "status": "prog", "deps": ["WS9.3"]},
                    {"id": "WS9.5", "title": "Write the docs", "status": "todo", "deps": []},
                    {"id": "WS9.6", "title": "Old idea", "status": "deferred", "deps": [], "moved_to": "WS8", "note": "moved"},
                    {"id": "WS9.7", "title": "Ship it", "status": "todo", "deps": ["WS9.4"]},
                ],
            }
        ],
        "edges": [],
    },
}

APPROVALS = {
    "ws": "WS9",
    "decided_at": None,
    "decided_by": None,
    "items": [
        {"id": "A1", "step": "Approve the production load", "status": "pending", "reversible": True, "conditions": ""},
        {"id": "A2", "step": "Old gate", "status": "approved", "reversible": False, "conditions": ""},
    ],
}


class FakeClient:
    """Records post_update calls; raises ``error`` when set."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple] = []

    def recent_updates(self, project_id, since_iso):
        return []

    def post_update(self, project_id, body, health):
        self.calls.append((project_id, body, health))
        if self.error is not None:
            raise self.error
        return "upd-1"


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "workstreams"
        self.root.mkdir()
        self.env_old = {name: os.environ.get(name) for name in ("AGENT_DASH_DATA_ROOT", linear.ENV_KEY)}
        os.environ["AGENT_DASH_DATA_ROOT"] = str(self.base / "data")
        os.environ[linear.ENV_KEY] = "lin_test_key"
        self.plan = json.loads(json.dumps(PLAN))
        self.history: list[dict] = []
        self.approvals = json.loads(json.dumps(APPROVALS))
        self.link = {
            "project": {"id": PROJECT_ID, "url": "https://linear.app/acme/project/p"},
            "tasks": {"WS9.2": {"identifier": "ENG-2", "url": ISSUE_URL}},
            "synced_at": NOW,
        }
        self._write()

    def tearDown(self):
        for name, value in self.env_old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def _write(self, linked: bool = True) -> None:
        bundle = self.root / "WS9"
        bundle.mkdir(exist_ok=True)
        (bundle / "bundle.json").write_text(json.dumps({"id": "WS9", "title": "Notes cutover", "state": "active"}), encoding="utf-8")
        (bundle / "plan.json").write_text(json.dumps(self.plan), encoding="utf-8")
        (bundle / "approvals.json").write_text(json.dumps(self.approvals), encoding="utf-8")
        (bundle / "history.jsonl").write_text("".join(json.dumps(row) + "\n" for row in self.history), encoding="utf-8")
        if linked:
            linear.write_link("WS9", dict(self.link), self.root)
        elif linear.link_path("WS9", self.root).is_file():
            linear.link_path("WS9", self.root).unlink()

    def _link(self) -> dict:
        return linear.read_link("WS9", self.root)

    def _update(self, now: float = NOW) -> dict:
        self._write()
        return linear_update.build_update("WS9", root=self.root, now=now)

    def _post(self, client, **kwargs) -> tuple[int, str]:
        self._write()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = linear_update.post("WS9", root=self.root, client=client, now=kwargs.pop("now", NOW), **kwargs)
        return rc, out.getvalue()


class BodyTest(UpdateTest):
    def test_body_has_every_section_in_order(self):
        body = self._update()["body"]
        heads = [line for line in body.splitlines() if line.startswith("**") and not line.startswith("**Progress")]
        self.assertEqual(
            heads,
            [
                "**Changed since the last update**",
                "**In progress now**",
                "**Next up**",
                "**Needs a decision**",
            ],
        )
        self.assertTrue(body.startswith("Move the notes store to Postgres.\n\n**Progress:**"))

    def test_progress_counts_exclude_deferred_tasks(self):
        body = self._update()["body"]
        self.assertIn(
            "**Progress:** 1 of 6 tasks done (17%). In progress: 1. In review: 1. Waiting on a production step: 1.",
            body,
        )

    def test_purpose_falls_back_to_bundle_title(self):
        del self.plan["narrative"]
        self.assertTrue(self._update()["body"].startswith("Notes cutover\n"))

    def test_tracker_summary_wins_over_thesis(self):
        self.plan["tracker"] = {"summary": "Plain summary for non-engineers."}
        self.assertTrue(self._update()["body"].startswith("Plain summary for non-engineers.\n"))

    def test_task_titles_with_linear_links_and_no_bare_ids(self):
        body = self._update()["body"]
        self.assertIn(f"- Wire the loader ([ENG-2]({ISSUE_URL}))", body)
        self.assertNotIn("WS9.2", body)

    def test_identifier_without_url_keeps_plain_identifier(self):
        self.link["tasks"] = {"WS9.2": {"identifier": "ENG-2", "url": None}}
        self.assertIn("- Wire the loader (ENG-2)", self._update()["body"])

    def test_in_progress_and_next_up_lists(self):
        body = self._update()["body"]
        self.assertIn("**In progress now**\n- Wire the loader", body)
        self.assertIn("**Next up**\n- Write the docs", body)

    def test_pending_approvals_are_listed_by_step(self):
        body = self._update()["body"]
        self.assertIn("**Needs a decision**\n- Approve the production load", body)
        self.assertNotIn("Old gate", body)

    def test_empty_sections_say_so(self):
        self.approvals["items"] = []
        self.plan["dag"]["groups"][0]["tasks"] = [{"id": "WS9.1", "title": "Only", "status": "done", "deps": []}]
        body = self._update()["body"]
        self.assertIn("**In progress now**\n- None.", body)
        self.assertIn("**Next up**\n- Nothing is ready.", body)
        self.assertIn("**Needs a decision**\n- Nothing waits on a decision.", body)


class ChangedTest(UpdateTest):
    def _row(self, ts: float, task: str = "WS9.2", to: str = "wip", frm: str | None = "todo", by: str | None = None) -> dict:
        row = {"ts": ts, "task": task, "to": to}
        if frm is not None:
            row["from"] = frm
        if by is not None:
            row["by"] = by
        return row

    def test_changes_within_a_day_are_listed_and_older_ones_are_not(self):
        self.history = [self._row(NOW - 3600, task="WS9.2", to="wip"), self._row(NOW - 2 * 86400, task="WS9.5", to="done")]
        body = self._update()["body"]
        self.assertIn(f"- Wire the loader ([ENG-2]({ISSUE_URL})): todo -> wip", body)
        self.assertNotIn("- Write the docs: ", body)

    def test_linear_rows_are_marked_in_linear(self):
        self.history = [self._row(NOW - 60, by="linear")]
        self.assertIn("todo -> wip (in Linear)", self._update()["body"])

    def test_since_last_update_at_replaces_the_day_window(self):
        self.link["last_update_at"] = NOW - 600
        self.history = [self._row(NOW - 3600), self._row(NOW - 300, task="WS9.5", to="wip")]
        body = self._update()["body"]
        self.assertIn("- Write the docs: todo -> wip", body)
        self.assertNotIn("- Wire the loader (", body.split("**In progress now**")[0])

    def test_changed_list_stops_at_ten_and_counts_the_rest(self):
        self.history = [self._row(NOW - 60 - i, task="WS9.5", to=f"s{i}") for i in range(12)]
        body = self._update()["body"]
        section = body.split("**Changed since the last update**\n")[1].split("\n\n")[0]
        self.assertEqual(section.count("\n") + 1, 11)
        self.assertTrue(section.endswith("- and 2 more"))

    def test_no_changes_says_so(self):
        self.assertIn("**Changed since the last update**\n- Nothing changed.", self._update()["body"])


class LimitTest(UpdateTest):
    def test_in_progress_stops_at_ten_and_counts_the_rest(self):
        tasks = [{"id": f"WS9.{100 + i}", "title": f"Active {i}", "status": "wip", "deps": []} for i in range(12)]
        self.plan["dag"]["groups"][0]["tasks"] = tasks
        body = self._update()["body"]
        section = body.split("**In progress now**\n")[1].split("\n\n")[0]
        self.assertEqual(section.count("\n") + 1, 11)
        self.assertTrue(section.endswith("- and 2 more"))

    def test_next_up_is_the_first_five_ready_tasks(self):
        tasks = [{"id": f"WS9.{200 + i}", "title": f"Ready {i}", "status": "todo", "deps": []} for i in range(7)]
        self.plan["dag"]["groups"][0]["tasks"] = tasks
        body = self._update()["body"]
        section = body.split("**Next up**\n")[1].split("\n\n")[0]
        self.assertEqual(section.count("\n") + 1, 5)


class HealthTest(UpdateTest):
    def test_default_is_on_track(self):
        self.assertEqual(self._update()["health"], "onTrack")

    def test_plan_value_is_used_when_valid(self):
        self.plan["tracker"] = {"update": {"health": "atRisk"}}
        self.assertEqual(self._update()["health"], "atRisk")

    def test_invalid_plan_value_falls_back(self):
        self.plan["tracker"] = {"update": {"health": "fine"}}
        self.assertEqual(self._update()["health"], "onTrack")

    def test_flag_overrides_plan(self):
        self.plan["tracker"] = {"update": {"health": "atRisk"}}
        client = FakeClient()
        rc, _out = self._post(client, health="off-track")
        self.assertEqual(rc, errors.OK)
        self.assertEqual(client.calls[0][2], "offTrack")

    def test_plan_value_is_sent_without_flag(self):
        self.plan["tracker"] = {"update": {"health": "atRisk"}}
        client = FakeClient()
        self._post(client)
        self.assertEqual(client.calls[0][2], "atRisk")

    def test_unknown_flag_is_usage_and_posts_nothing(self):
        client = FakeClient()
        rc, out = self._post(client, health="green")
        self.assertEqual(rc, errors.USAGE)
        self.assertEqual(client.calls, [])
        self.assertIn("on-track, at-risk, off-track", out)


class PostTest(UpdateTest):
    def test_dry_run_prints_body_and_posts_nothing(self):
        client = FakeClient()
        rc, out = self._post(client, dry_run=True)
        self.assertEqual(rc, errors.OK)
        self.assertEqual(client.calls, [])
        self.assertIn("**Progress:**", out)
        self.assertNotIn("last_update_at", self._link())

    def test_success_posts_project_body_and_records_last_update_at(self):
        client = FakeClient()
        rc, _out = self._post(client)
        self.assertEqual(rc, errors.OK)
        project_id, body, health = client.calls[0]
        self.assertEqual(project_id, PROJECT_ID)
        self.assertIn("**Progress:**", body)
        self.assertEqual(health, "onTrack")
        self.assertEqual(self._link()["last_update_at"], NOW)

    def test_failure_records_nothing_as_posted_and_reports_it(self):
        client = FakeClient(LinearError("NETWORK", "boom"))
        rc, out = self._post(client)
        self.assertEqual(rc, errors.UNREACHABLE)
        self.assertNotIn("last_update_at", self._link())
        self.assertIn("update not posted for WS9: NETWORK: boom", out)
        self.assertEqual(self._link()["last_error"], "NETWORK: boom")

    def test_unlinked_bundle_is_refused_without_posting(self):
        self._write(linked=False)
        client = FakeClient()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = linear_update.post("WS9", root=self.root, client=client, now=NOW)
        self.assertEqual(rc, errors.UNREACHABLE)
        self.assertEqual(client.calls, [])

    def test_missing_bundle_is_not_found(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = linear_update.post("NOPE", root=self.root, client=FakeClient(), now=NOW)
        self.assertEqual(rc, errors.NOT_FOUND)

    def test_default_client_comes_from_linear_sync(self):
        client = FakeClient()
        with mock.patch("dash.linear_sync._make_client", return_value=client) as make:
            rc, _out = self._post(None)
        make.assert_called_once()
        self.assertEqual(rc, errors.OK)
        self.assertEqual(len(client.calls), 1)


class DailyRuleTest(UpdateTest):
    def _at(self, hour: int, minute: int = 0, day: int = 21) -> float:
        return datetime(2026, 10, day, hour, minute).timestamp()

    def _daily(self, hour: int | None = 9) -> None:
        if hour is not None:
            self.plan["tracker"] = {"update": {"daily_hour": hour}}
        self._write()

    def test_before_the_hour_is_not_due(self):
        self._daily()
        self.assertFalse(linear_update.due("WS9", self._at(8, 59), root=self.root))

    def test_at_and_after_the_hour_is_due(self):
        self._daily()
        self.assertTrue(linear_update.due("WS9", self._at(9, 0), root=self.root))
        self.assertTrue(linear_update.due("WS9", self._at(17, 30), root=self.root))

    def test_already_posted_today_is_not_due(self):
        self.link["last_update_at"] = self._at(9, 5)
        self._daily()
        self.assertFalse(linear_update.due("WS9", self._at(12, 0), root=self.root))

    def test_posted_yesterday_is_due_today(self):
        self.link["last_update_at"] = self._at(9, 5, day=20)
        self._daily()
        self.assertTrue(linear_update.due("WS9", self._at(9, 30), root=self.root))

    def test_no_daily_hour_is_never_due(self):
        self._daily(hour=None)
        self.assertFalse(linear_update.due("WS9", self._at(12, 0), root=self.root))

    def test_unlinked_is_never_due(self):
        self._daily()
        self._write(linked=False)
        self.assertFalse(linear_update.due("WS9", self._at(12, 0), root=self.root))

    def test_recent_failed_attempt_waits_an_hour(self):
        self.link["update_attempt_at"] = self._at(9, 0)
        self._daily()
        self.assertFalse(linear_update.due("WS9", self._at(9, 30), root=self.root))
        self.assertTrue(linear_update.due("WS9", self._at(10, 30), root=self.root))


class SendDailyTest(UpdateTest):
    def test_send_daily_returns_the_error_text_and_never_raises(self):
        with mock.patch("dash.linear_sync._make_client", side_effect=LinearError("AUTH", "Not signed in")):
            error = linear_update.send_daily("WS9", NOW, root=self.root)
        self.assertEqual(error, "AUTH: Not signed in")
        self.assertEqual(self._link()["last_error"], "AUTH: Not signed in")

    def test_send_daily_success_returns_none(self):
        self.assertIsNone(linear_update.send_daily("WS9", NOW, root=self.root, client=FakeClient()))
        self.assertEqual(self._link()["last_update_at"], NOW)

    def test_poller_posts_only_when_due(self):
        self.plan["tracker"] = {"update": {"daily_hour": 9}}
        self._write()
        morning = datetime(2026, 10, 21, 9, 30).timestamp()
        env = mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": str(self.root)})
        with env, mock.patch.object(linear_update, "send_daily", return_value=None) as send, mock.patch("dash.sources.linear.time.time", return_value=morning):
            source._daily_update("WS9")
        send.assert_called_once_with("WS9", morning)
        early = datetime(2026, 10, 21, 7, 0).timestamp()
        with env, mock.patch.object(linear_update, "send_daily") as send, mock.patch("dash.sources.linear.time.time", return_value=early):
            source._daily_update("WS9")
        send.assert_not_called()


class CliTest(unittest.TestCase):
    def test_update_flags_reach_post(self):
        from dash import bundles as bundles_mod

        with mock.patch("dash.linear_update.post", return_value=0) as post:
            rc = bundles_mod._cmd_linear(["update", "WS9", "--health", "at-risk", "--dry-run"])
        self.assertEqual(rc, 0)
        post.assert_called_once_with("WS9", health="at-risk", dry_run=True)

    def test_update_needs_one_id(self):
        from dash import bundles as bundles_mod

        out = io.StringIO()
        with mock.patch("dash.linear_update.post") as post, contextlib.redirect_stdout(out):
            rc = bundles_mod._cmd_linear(["update"])
        self.assertEqual(rc, errors.USAGE)
        post.assert_not_called()

    def test_health_needs_a_value(self):
        from dash import bundles as bundles_mod

        out = io.StringIO()
        with mock.patch("dash.linear_update.post") as post, contextlib.redirect_stdout(out):
            rc = bundles_mod._cmd_linear(["update", "WS9", "--health"])
        self.assertEqual(rc, errors.USAGE)
        post.assert_not_called()

    def test_usage_lists_update(self):
        self.assertIn("ws linear update <ID>", bundles.WS_USAGE)


class GraphqlPostUpdateTest(unittest.TestCase):
    def test_request_shape(self):
        transport = FakeTransport([_ok({"projectUpdateCreate": {"success": True, "projectUpdate": {"id": "upd-9"}}})])
        client = lc.Client(KEY, transport=transport)
        self.assertEqual(client.post_update(PROJECT_ID, "Body text", "atRisk"), "upd-9")
        sent = transport.calls[0]["body"]
        self.assertIn("projectUpdateCreate(input: $input)", sent["query"])
        self.assertEqual(sent["variables"], {"input": {"projectId": PROJECT_ID, "body": "Body text", "health": "atRisk"}})

    def test_unsaved_payload_raises(self):
        transport = FakeTransport([_ok({"projectUpdateCreate": {"success": False, "projectUpdate": None}})])
        client = lc.Client(KEY, transport=transport)
        with self.assertRaises(LinearError):
            client.post_update(PROJECT_ID, "Body", "onTrack")


class McpPostUpdateTest(unittest.TestCase):
    def test_tool_call_shape(self):
        client = linear_mcp.McpClient("tok", transport=lambda *_a, **_k: (200, {}, b""))
        with mock.patch.object(client, "_tool", return_value={"id": "11111111-2222-3333-4444-666666666666"}) as tool:
            update_id = client.post_update(PROJECT_ID, "Body text", "offTrack")
        tool.assert_called_once_with(
            "save_status_update",
            {"type": "project", "project": PROJECT_ID, "body": "Body text", "health": "offTrack"},
        )
        self.assertEqual(update_id, "11111111-2222-3333-4444-666666666666")


class LifecycleRowTest(unittest.TestCase):
    def test_workstream_state_row_names_the_workstream(self) -> None:
        rows = [{"kind": "state", "ts": 10.0, "ws": "WS4", "from": "planned", "to": "active"}]
        lines = linear_update._changed(rows, 0.0, {}, {})
        self.assertEqual(lines[0], "- Workstream: planned -> active")



class RecentUpdatesClient(FakeClient):
    """A fake client that lists project updates and can fail the post or the listing."""

    def __init__(self, existing=(), post_error=None, list_error=None, hold=None):
        super().__init__()
        self.existing = list(existing)
        self.post_error = post_error
        self.list_error = list_error
        self.hold = hold
        self.held_at_post = None
        self.listed = []

    def recent_updates(self, project_id, since_iso):
        self.listed.append((project_id, since_iso))
        if self.list_error is not None:
            raise self.list_error
        return list(self.existing)

    def post_update(self, project_id, body, health):
        self.calls.append((project_id, body, health))
        if self.hold is not None:
            self.held_at_post = self.hold["held"]
        if self.post_error is not None:
            raise self.post_error
        return "upd-new"


def _iso(stamp):
    return datetime.fromtimestamp(stamp, UTC).isoformat()


class StandupSafetyTest(UpdateTest):
    def _purpose_line(self):
        return self._update()["body"].split("\n", 1)[0]

    def test_non_finite_stamps_are_not_stamps(self):
        self.assertIsNone(linear_update._stamp_of(float("nan")))
        self.assertIsNone(linear_update._stamp_of(float("inf")))
        self.assertIsNone(linear_update._stamp_of(10 ** 400))
        self.assertEqual(linear_update._stamp_of(5), 5.0)

    def test_nan_attempt_stamp_does_not_block_the_retry(self):
        self.link["update_attempt_at"] = float("nan")
        self.plan["tracker"] = {"update": {"daily_hour": 9}}
        self._write()
        self.assertTrue(linear_update.due("WS9", datetime(2026, 10, 21, 10, 30).timestamp(), root=self.root))

    def test_existing_update_today_with_the_same_purpose_is_reused(self):
        existing = {"id": "upd-0", "body": self._purpose_line() + "\n\nEarlier text", "createdAt": _iso(NOW - 600)}
        client = RecentUpdatesClient(existing=[existing])
        rc, _out = self._post(client)
        self.assertEqual(rc, errors.OK)
        self.assertEqual(client.calls, [])
        self.assertEqual(self._link()["last_update_at"], NOW)

    def test_yesterdays_update_with_the_same_purpose_is_not_reused(self):
        existing = {"id": "upd-0", "body": self._purpose_line() + "\n\nOld", "createdAt": _iso(datetime(2026, 10, 20, 23, 0).timestamp())}
        client = RecentUpdatesClient(existing=[existing])
        rc, _out = self._post(client)
        self.assertEqual(rc, errors.OK)
        self.assertEqual(len(client.calls), 1)

    def test_a_different_purpose_line_is_not_reused(self):
        existing = {"id": "upd-0", "body": "Something else entirely\n\nOld", "createdAt": _iso(NOW - 600)}
        client = RecentUpdatesClient(existing=[existing])
        self._post(client)
        self.assertEqual(len(client.calls), 1)

    def test_ambiguous_timeout_then_retry_does_not_repost(self):
        self._write()
        client = RecentUpdatesClient(post_error=TimeoutError("read timed out"))
        error = linear_update.send_daily("WS9", NOW, self.root, client)
        self.assertIn("TimeoutError", error)
        self.assertEqual(len(client.calls), 1)
        client.post_error = None
        client.existing = [{"id": "upd-0", "body": self._purpose_line() + "\n\nCommitted", "createdAt": _iso(NOW)}]
        self.assertIsNone(linear_update.send_daily("WS9", NOW + linear_update.RETRY_S, self.root, client))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self._link()["last_update_at"], NOW + linear_update.RETRY_S)

    def test_listing_failure_posts_nothing(self):
        client = RecentUpdatesClient(list_error=LinearError("NETWORK", "down"))
        error = linear_update.send_daily("WS9", NOW, self.root, client)
        self.assertEqual(error, "NETWORK: down")
        self.assertEqual(client.calls, [])
        self.assertEqual(self._link()["update_attempt_at"], NOW)

    def test_write_failure_after_the_post_does_not_repost_a_minute_later(self):
        self._write()
        client = RecentUpdatesClient()
        real = linear.write_link

        def flaky(ws, data, root=None):
            if data.get("last_update_at") == NOW:
                raise OSError("disk full")
            return real(ws, data, root)

        with mock.patch("dash.linear.write_link", side_effect=flaky):
            error = linear_update.send_daily("WS9", NOW, self.root, client)
        self.assertIn("disk full", error)
        self.assertEqual(len(client.calls), 1)
        client.existing = [{"id": "upd-new", "body": self._purpose_line() + "\n\nPosted", "createdAt": _iso(NOW)}]
        linear_update.send_daily("WS9", NOW + 60, self.root, client)
        self.assertEqual(len(client.calls), 1)

    def test_daily_post_runs_under_the_sync_lock(self):
        state = {"held": False}

        @contextlib.contextmanager
        def fake_lock(path):
            state["held"] = True
            try:
                yield
            finally:
                state["held"] = False

        client = RecentUpdatesClient(hold=state)
        with mock.patch("dash.linear_sync._sync_lock", fake_lock):
            self.assertIsNone(linear_update.send_daily("WS9", NOW, self.root, client))
        self.assertTrue(client.held_at_post)

    def test_manual_post_runs_under_the_sync_lock(self):
        state = {"held": False}

        @contextlib.contextmanager
        def fake_lock(path):
            state["held"] = True
            try:
                yield
            finally:
                state["held"] = False

        client = RecentUpdatesClient(hold=state)
        with mock.patch("dash.linear_sync._sync_lock", fake_lock):
            rc, _out = self._post(client)
        self.assertEqual(rc, errors.OK)
        self.assertTrue(client.held_at_post)

    def test_daily_recheck_inside_the_lock_skips_a_day_already_posted(self):
        self.link["last_update_at"] = NOW - 600
        self._write()
        client = RecentUpdatesClient()
        self.assertIsNone(linear_update.send_daily("WS9", NOW, self.root, client))
        self.assertEqual(client.calls, [])

    def test_unlinked_project_stamps_the_attempt(self):
        self.link = {k: v for k, v in self.link.items() if k != "project"}
        self._write()
        error = linear_update.send_daily("WS9", NOW, self.root, RecentUpdatesClient())
        self.assertIn("not linked", error)
        self.assertEqual(self._link()["update_attempt_at"], NOW)

    def test_plan_text_is_escaped_for_markdown(self):
        self.plan["dag"]["groups"][0]["tasks"][1]["title"] = "# Wire *the* @team\n- [loader](x) `code`"
        self.approvals["items"][0]["step"] = "> Approve_the_load\n\n@here"
        body = self._update()["body"]
        self.assertIn(r"\# Wire \*the\* \@team - \[loader\]\(x\) \`code\`", body)
        self.assertIn(r"- \> Approve\_the\_load \@here", body)
        self.assertNotRegex(body, r"(?<!\\)@")

if __name__ == "__main__":
    unittest.main()
