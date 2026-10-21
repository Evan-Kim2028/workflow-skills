import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import linear
from dash.linear import (
    APPROVAL_STATE,
    STATE_FOR_STATUS,
    approval_for_state,
    drain_outbox,
    enqueue,
    has_pending,
    is_linked,
    link_path,
    page_link,
    read_link,
    read_outbox,
    render_approval,
    render_issue,
    spec_hash,
    stable_id,
    stamp,
    status_for_state,
    sync_state,
    task_links,
    write_link,
)

STATE_TYPE_FOR_STATUS = {
    "todo": "unstarted",
    "wip": "started",
    "pr": "started",
    "prog": "started",
    "done": "completed",
    "deferred": "canceled",
}

SAMPLE_TASK = {
    "id": "WS4.classify",
    "title": "Run, step, and backend classifier",
    "status": "todo",
    "deps": ["WS4.replay"],
    "size": "M",
    "files": ["doctor/src/lor_doctor/modes/run.py", "doctor/tests/test_run.py"],
    "acceptance": {
        "metric": "all verdict classes on fixtures",
        "probe": "fixture Dagster plus PG state classifies",
        "threshold": "15 of 15 fixture cases correct",
        "tolerance": "exact",
    },
    "rollback": "revert commit",
    "deploy_tier": "code-only",
    "approval": "none (read-only)",
    "dep_notes": {"WS4.replay": "pack fixtures carry spec-shaped baselines"},
    "wave": "C1",
    "test_cmd": "`uv run pytest doctor/tests/test_run.py -q`",
    "phase": "A",
    "confidence": "medium",
}

SAMPLE_APPROVAL = {
    "id": "deploy-gate",
    "step": "Enable the new gate on prod",
    "status": "pending",
    "reversible": True,
    "conditions": "only after the dry run passes",
    "note": "owner asked for it",
}


def _bundle(root: Path, ws: str = "WS9") -> Path:
    bundle = root / ws
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.json").write_text(
        json.dumps({"id": ws, "title": "Example", "short": "Ex", "order": 1, "kind": "workstream"}),
        encoding="utf-8",
    )
    (bundle / "plan.json").write_text(
        json.dumps({"dag": {"cols": ["Shape"], "groups": [], "edges": []}, "narrative": {"phases": []}}),
        encoding="utf-8",
    )
    return bundle


def _link_data() -> dict:
    return {
        "version": 1,
        "team_key": "ENG",
        "project": {"id": "p1", "url": "https://linear.app/x/project/ex", "name": "Notes cutover"},
        "milestones": {"Shape": "m1"},
        "tasks": {
            "WS9.1": {"id": "i1", "identifier": "ENG-12", "url": "https://linear.app/x/issue/ENG-12"},
        },
        "approvals": {},
        "relations": [],
        "spec_hash": "abc",
        "cursor": None,
        "linked_at": 1.0,
        "pushed_at": 2.0,
        "synced_at": 3.0,
        "last_error": None,
    }


class LinearTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bundle = _bundle(self.root)
        self.old_data_root = os.environ.get("AGENT_DASH_DATA_ROOT")
        os.environ["AGENT_DASH_DATA_ROOT"] = str(self.root / "data")

    def tearDown(self):
        if self.old_data_root is None:
            os.environ.pop("AGENT_DASH_DATA_ROOT", None)
        else:
            os.environ["AGENT_DASH_DATA_ROOT"] = self.old_data_root
        self.tmp.cleanup()


class StableIdTest(unittest.TestCase):
    def test_same_parts_give_same_id(self):
        self.assertEqual(stable_id("project", "WS9"), stable_id("project", "WS9"))

    def test_different_parts_give_different_ids(self):
        self.assertNotEqual(stable_id("project", "WS9"), stable_id("project", "WS10"))
        self.assertNotEqual(stable_id("milestone", "WS9", "Shape"), stable_id("milestone", "WS9", "Other"))

    def test_is_version_4_uuid(self):
        value = stable_id("milestone", "WS9", "Shape")
        parsed = uuid.UUID(value)
        self.assertEqual(parsed.version, 4)
        self.assertEqual(str(parsed), value)
        self.assertEqual(parsed.variant, uuid.RFC_4122)


class StatusMappingTest(unittest.TestCase):
    def test_every_status_maps_to_its_state_name(self):
        for status, name in STATE_FOR_STATUS.items():
            self.assertEqual(status_for_state(name, STATE_TYPE_FOR_STATUS[status]), status)

    def test_every_state_name_maps_back_to_status(self):
        for status, name in STATE_FOR_STATUS.items():
            self.assertEqual(STATE_FOR_STATUS[status_for_state(name, None)], name)

    def test_state_type_fallback_for_unknown_names(self):
        self.assertEqual(status_for_state("Weird", "triage"), "todo")
        self.assertEqual(status_for_state("Weird", "backlog"), "todo")
        self.assertEqual(status_for_state("Weird", "unstarted"), "todo")
        self.assertEqual(status_for_state("Weird", "started"), "wip")
        self.assertEqual(status_for_state("Weird", "completed"), "done")
        self.assertEqual(status_for_state("Weird", "canceled"), "deferred")
        self.assertEqual(status_for_state("Weird", "duplicate"), "deferred")

    def test_backlog_and_duplicate_names(self):
        self.assertEqual(status_for_state("Backlog", "backlog"), "todo")
        self.assertEqual(status_for_state("Duplicate", "canceled"), "deferred")

    def test_unknown_name_and_type_default_to_todo(self):
        self.assertEqual(status_for_state(None, None), "todo")
        self.assertEqual(status_for_state("Weird", "mystery"), "todo")


class ApprovalMappingTest(unittest.TestCase):
    def test_round_trip_through_state_names(self):
        self.assertEqual(APPROVAL_STATE, {"pending": "Todo", "approved": "Done", "denied": "Canceled"})

    def test_state_type_to_approval(self):
        self.assertEqual(approval_for_state("completed"), "approved")
        self.assertEqual(approval_for_state("canceled"), "denied")
        self.assertEqual(approval_for_state("duplicate"), "denied")
        self.assertEqual(approval_for_state("unstarted"), "pending")
        self.assertEqual(approval_for_state("started"), "pending")
        self.assertEqual(approval_for_state(None), "pending")


class RenderIssueTest(unittest.TestCase):
    def test_deterministic(self):
        first = render_issue("WS4", SAMPLE_TASK, "Shape", "train-a")
        second = render_issue("WS4", dict(SAMPLE_TASK), "Shape", "train-a")
        self.assertEqual(first, second)

    def test_title_and_header(self):
        issue = render_issue("WS4", SAMPLE_TASK, "Shape", None)
        self.assertEqual(issue["title"], "Run, step, and backend classifier")
        self.assertTrue(issue["description"].startswith("Task `WS4.classify` in workstream WS4"))

    def test_full_task_fields_present(self):
        text = render_issue("WS4", SAMPLE_TASK, "Shape", "train-a")["description"]
        for expected in (
            "- Section: Shape",
            "- Wave: C1",
            "- Size: M",
            "- Train: train-a",
            "- Metric: all verdict classes on fixtures",
            "- Probe: fixture Dagster plus PG state classifies",
            "- Threshold: 15 of 15 fixture cases correct",
            "- Tolerance: exact",
            "- `doctor/src/lor_doctor/modes/run.py`",
            "- `doctor/tests/test_run.py`",
            "`uv run pytest doctor/tests/test_run.py -q`",
            "## Rollback\n\nrevert commit",
            "## Deploy tier\n\ncode-only",
            "## Approval\n\nnone (read-only)",
            "- `WS4.replay`: pack fixtures carry spec-shaped baselines",
        ):
            self.assertIn(expected, text)
        self.assertTrue(text.rstrip().endswith(
            "_Spec lives in the agent-dash plan; edits here are overwritten on the next sync. Status syncs both ways._"
        ))

    def test_absent_fields_are_skipped(self):
        text = render_issue("WS9", {"id": "WS9.1", "title": "Bare", "status": "todo"}, None, None)["description"]
        for absent in ("Section", "Wave", "Size", "Train", "## Acceptance", "## Files", "## Test command", "## Rollback", "## Deploy tier", "## Approval", "## Depends on"):
            self.assertNotIn(absent, text)
        self.assertIn("Task `WS9.1` in workstream WS9", text)

    def test_empty_values_are_skipped(self):
        task = {"id": "WS9.1", "title": "Empty", "files": [], "acceptance": {"metric": ""}, "rollback": "", "deps": []}
        text = render_issue("WS9", task, None, None)["description"]
        self.assertNotIn("## Acceptance", text)
        self.assertNotIn("## Files", text)
        self.assertNotIn("## Rollback", text)
        self.assertNotIn("## Depends on", text)

    def test_dep_without_note_is_bare_bullet(self):
        task = {"id": "WS9.2", "title": "Dep", "deps": ["WS9.1"]}
        text = render_issue("WS9", task, None, None)["description"]
        self.assertIn("## Depends on\n\n- `WS9.1`\n", text)


class RenderApprovalTest(unittest.TestCase):
    def test_title_and_fields(self):
        issue = render_approval("WS9", SAMPLE_APPROVAL)
        self.assertEqual(issue["title"], "Approve: Enable the new gate on prod")
        text = issue["description"]
        self.assertIn("Approval `deploy-gate` for workstream WS9", text)
        self.assertIn("- Reversible: yes", text)
        self.assertIn("- Conditions: only after the dry run passes", text)
        self.assertIn("## Note\n\nowner asked for it", text)

    def test_irreversible_and_absent_fields(self):
        item = {"id": "x", "step": "Drop table", "reversible": False, "conditions": ""}
        text = render_approval("WS9", item)["description"]
        self.assertIn("- Reversible: no", text)
        self.assertNotIn("Conditions", text)
        self.assertNotIn("## Note", text)

    def test_deterministic(self):
        self.assertEqual(render_approval("WS9", SAMPLE_APPROVAL), render_approval("WS9", dict(SAMPLE_APPROVAL)))


class SpecHashTest(unittest.TestCase):
    def test_stable_across_key_order(self):
        a = [{"title": "T", "description": "D"}]
        b = [{"description": "D", "title": "T"}]
        self.assertEqual(spec_hash(a), spec_hash(b))

    def test_changes_with_content(self):
        base = [render_issue("WS4", SAMPLE_TASK, "Shape", None)]
        changed = dict(SAMPLE_TASK, title="Different title")
        self.assertNotEqual(spec_hash(base), spec_hash([render_issue("WS4", changed, "Shape", None)]))

    def test_is_hex_sha256(self):
        value = spec_hash([])
        self.assertEqual(len(value), 64)
        int(value, 16)


class LinkFileTest(LinearTestBase):
    def test_round_trip_with_root(self):
        data = _link_data()
        write_link("WS9", data, root=self.root)
        self.assertEqual(read_link("WS9", root=self.root), data)
        self.assertEqual(link_path("WS9", root=self.root), self.bundle.resolve() / "linear.json")
        self.assertFalse((self.bundle / "linear.json.tmp").exists())

    def test_is_linked(self):
        self.assertFalse(is_linked("WS9", root=self.root))
        write_link("WS9", _link_data(), root=self.root)
        self.assertTrue(is_linked("WS9", root=self.root))
        self.assertFalse(is_linked("WS404", root=self.root))

    def test_read_link_none_when_unlinked(self):
        self.assertIsNone(read_link("WS9", root=self.root))

    def test_write_link_rejects_unknown_bundle(self):
        with self.assertRaises(ValueError):
            write_link("WS404", _link_data(), root=self.root)

    def test_link_path_none_for_unknown_bundle(self):
        self.assertIsNone(link_path("WS404", root=self.root))

    def test_workspace_round_trip_uses_data_root(self):
        self.assertIsNone(linear.read_workspace())
        linear.write_workspace({"team": {"id": "t1", "key": "ENG"}})
        self.assertEqual(linear.read_workspace(), {"team": {"id": "t1", "key": "ENG"}})
        self.assertEqual(linear.workspace_path(), self.root / "data" / "linear.json")

    def test_api_key_from_env(self):
        old = os.environ.get("LINEAR_API_KEY")
        try:
            os.environ["LINEAR_API_KEY"] = "  "
            self.assertIsNone(linear.api_key())
            os.environ["LINEAR_API_KEY"] = "lin_test"
            self.assertEqual(linear.api_key(), "lin_test")
        finally:
            if old is None:
                os.environ.pop("LINEAR_API_KEY", None)
            else:
                os.environ["LINEAR_API_KEY"] = old


class OutboxTest(LinearTestBase):
    def test_enqueue_is_noop_when_unlinked(self):
        enqueue("WS9", "task", "WS9.1", root=self.root, now=1.0)
        self.assertFalse((self.bundle / "linear-outbox.jsonl").exists())
        self.assertEqual(read_outbox("WS9", root=self.root), [])

    def test_enqueue_appends_when_linked(self):
        write_link("WS9", _link_data(), root=self.root)
        enqueue("WS9", "task", "WS9.1", root=self.root, now=1.0)
        enqueue("WS9", "approval", "deploy-gate", root=self.root, now=2.0)
        rows = read_outbox("WS9", root=self.root)
        self.assertEqual([{k: v for k, v in row.items() if k != "id"} for row in rows], [
            {"ts": 1.0, "kind": "task", "key": "WS9.1"},
            {"ts": 2.0, "kind": "approval", "key": "deploy-gate"},
        ])
        self.assertEqual(len({row["id"] for row in rows}), 2)

    def test_read_outbox_skips_malformed_lines(self):
        write_link("WS9", _link_data(), root=self.root)
        (self.bundle / "linear-outbox.jsonl").write_text(
            '{"ts": 1.0, "kind": "task", "key": "WS9.1"}\nnot json\n{"ts": 2.0, "kind": "bogus", "key": "x"}\n',
            encoding="utf-8",
        )
        rows = read_outbox("WS9", root=self.root)
        self.assertEqual([{k: v for k, v in row.items() if k != "id"} for row in rows], [{"ts": 1.0, "kind": "task", "key": "WS9.1"}])
        self.assertTrue(rows[0]["id"])

    def test_drain_removes_exactly_the_given_ids(self):
        write_link("WS9", _link_data(), root=self.root)
        for ts in (1.0, 2.0, 3.0):
            enqueue("WS9", "task", f"WS9.{int(ts)}", root=self.root, now=ts)
        rows = read_outbox("WS9", root=self.root)
        drain_outbox("WS9", {rows[0]["id"], rows[1]["id"]}, root=self.root)
        self.assertEqual([row["key"] for row in read_outbox("WS9", root=self.root)], ["WS9.3"])
        drain_outbox("WS9", {rows[2]["id"]}, root=self.root)
        self.assertEqual(read_outbox("WS9", root=self.root), [])

    def test_drain_keeps_a_row_appended_with_an_equal_timestamp(self):
        write_link("WS9", _link_data(), root=self.root)
        enqueue("WS9", "task", "WS9.1", root=self.root, now=5.0)
        flushed = {row["id"] for row in read_outbox("WS9", root=self.root)}
        enqueue("WS9", "task", "WS9.2", root=self.root, now=5.0)
        drain_outbox("WS9", flushed, root=self.root)
        self.assertEqual([row["key"] for row in read_outbox("WS9", root=self.root)], ["WS9.2"])

    def test_legacy_rows_without_id_get_a_stable_derived_id(self):
        write_link("WS9", _link_data(), root=self.root)
        (self.bundle / "linear-outbox.jsonl").write_text('{"ts": 1.0, "kind": "task", "key": "WS9.1"}\n', encoding="utf-8")
        first = read_outbox("WS9", root=self.root)[0]["id"]
        self.assertEqual(read_outbox("WS9", root=self.root)[0]["id"], first)
        drain_outbox("WS9", {first}, root=self.root)
        self.assertEqual(read_outbox("WS9", root=self.root), [])

    def test_has_pending_matches_kind_and_key(self):
        self.assertFalse(has_pending("WS9", "task", "WS9.1", root=self.root))
        write_link("WS9", _link_data(), root=self.root)
        enqueue("WS9", "task", "WS9.1", root=self.root, now=1.0)
        self.assertTrue(has_pending("WS9", "task", "WS9.1", root=self.root))
        self.assertFalse(has_pending("WS9", "approval", "WS9.1", root=self.root))
        self.assertFalse(has_pending("WS9", "task", "WS9.2", root=self.root))
        drain_outbox("WS9", {row["id"] for row in read_outbox("WS9", root=self.root)}, root=self.root)
        self.assertFalse(has_pending("WS9", "task", "WS9.1", root=self.root))

    def test_unlink_then_enqueue_leaves_no_outbox(self):
        write_link("WS9", _link_data(), root=self.root)
        (self.bundle / "linear.json").unlink()
        (self.bundle / "linear-outbox.jsonl").unlink(missing_ok=True)
        enqueue("WS9", "task", "WS9.1", root=self.root, now=1.0)
        self.assertFalse((self.bundle / "linear-outbox.jsonl").exists())

    def test_drain_on_unlinked_bundle_is_safe(self):
        drain_outbox("WS9", 1.0, root=self.root)
        self.assertFalse((self.bundle / "linear-outbox.jsonl").exists())


class LinkShapesTest(LinearTestBase):
    def test_unlinked_shapes(self):
        self.assertIsNone(sync_state("WS9", root=self.root))
        self.assertIsNone(page_link("WS9", root=self.root))
        self.assertEqual(task_links("WS9", root=self.root), {})

    def test_sync_state_shape(self):
        write_link("WS9", _link_data(), root=self.root)
        enqueue("WS9", "task", "WS9.1", root=self.root, now=1.0)
        self.assertEqual(sync_state("WS9", root=self.root), {
            "url": "https://linear.app/x/project/ex",
            "synced_at": 3.0,
            "last_error": None,
            "outbox": 1,
            "project": "Notes cutover",
        })

    def test_page_link_shape(self):
        write_link("WS9", _link_data(), root=self.root)
        self.assertEqual(page_link("WS9", root=self.root), {
            "url": "https://linear.app/x/project/ex",
            "synced_at": 3.0,
        })

    def test_task_links_shape(self):
        write_link("WS9", _link_data(), root=self.root)
        self.assertEqual(task_links("WS9", root=self.root), {
            "WS9.1": {"identifier": "ENG-12", "url": "https://linear.app/x/issue/ENG-12"},
        })


class StampTest(LinearTestBase):
    def test_zero_when_absent(self):
        self.assertEqual(stamp("WS9", root=self.root), (0, 0))
        self.assertEqual(stamp("WS404", root=self.root), (0, 0))

    def test_changes_when_linear_json_changes(self):
        write_link("WS9", _link_data(), root=self.root)
        before = stamp("WS9", root=self.root)
        self.assertNotEqual(before, (0, 0))
        data = _link_data()
        data["last_error"] = "RATELIMITED: retry later"
        write_link("WS9", data, root=self.root)
        self.assertNotEqual(stamp("WS9", root=self.root), before)


class ApiKeyShapeTest(LinearTestBase):
    def setUp(self):
        super().setUp()
        self.old_key = os.environ.get(linear.ENV_KEY)

    def tearDown(self):
        if self.old_key is None:
            os.environ.pop(linear.ENV_KEY, None)
        else:
            os.environ[linear.ENV_KEY] = self.old_key
        super().tearDown()

    def test_rejects_embedded_whitespace_and_control_characters(self):
        for bad in ("lin_a b", "lin_a\nb", "lin_a\tb", "lin_a\rb", "lin_\x7fb", "lin_\u00e9"):
            os.environ[linear.ENV_KEY] = bad
            self.assertIsNone(linear.api_key(), repr(bad))

    def test_strips_ends_and_keeps_a_clean_key(self):
        os.environ[linear.ENV_KEY] = "  lin_ok123  \n"
        self.assertEqual(linear.api_key(), "lin_ok123")
        os.environ[linear.ENV_KEY] = "   "
        self.assertIsNone(linear.api_key())


class WriteUniqueTest(LinearTestBase):
    def test_parallel_write_link_calls_all_land_as_valid_json(self):
        import threading

        errors_seen: list[BaseException] = []

        def writer(n: int) -> None:
            try:
                for i in range(20):
                    data = _link_data()
                    data["synced_at"] = float(n * 100 + i)
                    write_link("WS9", data, root=self.root)
            except BaseException as exc:  # noqa: BLE001 - collected and asserted below
                errors_seen.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors_seen, [])
        record = json.loads((self.bundle / "linear.json").read_text(encoding="utf-8"))
        self.assertIn("synced_at", record)
        self.assertEqual([p.name for p in self.bundle.iterdir() if p.name.endswith(".tmp")], [])


class UrlFilterTest(LinearTestBase):
    def _set(self, project_url, task_url):
        data = _link_data()
        data["project"]["url"] = project_url
        data["tasks"]["WS9.1"]["url"] = task_url
        write_link("WS9", data, root=self.root)

    def test_javascript_and_http_urls_are_dropped(self):
        for bad in ("javascript:alert(1)", "http://linear.app/x", ""):
            self._set(bad, bad)
            self.assertIsNone(page_link("WS9", root=self.root)["url"], bad)
            row = task_links("WS9", root=self.root)["WS9.1"]
            self.assertIsNone(row["url"], bad)
            self.assertEqual(row["identifier"], "ENG-12")

    def test_https_url_is_kept(self):
        self._set("https://linear.app/x/project/ex", "https://linear.app/x/issue/ENG-12")
        self.assertEqual(page_link("WS9", root=self.root)["url"], "https://linear.app/x/project/ex")
        self.assertEqual(task_links("WS9", root=self.root)["WS9.1"]["url"], "https://linear.app/x/issue/ENG-12")


class TestCommandRenderTest(unittest.TestCase):
    def test_single_command_is_wrapped_once(self) -> None:
        text = render_issue("WS4", {"id": "WS4.a", "title": "A", "test_cmd": "`make test`"}, None, None)["description"]
        self.assertIn("## Test command\n\n`make test`", text)

    def test_mixed_prose_with_inline_code_is_kept_as_written(self) -> None:
        raw = "`make test` (exit 0), plus `make lint`."
        text = render_issue("WS4", {"id": "WS4.a", "title": "A", "test_cmd": raw}, None, None)["description"]
        self.assertIn("## Test command\n\n" + raw, text)
        self.assertNotIn("`.`", text)


if __name__ == "__main__":
    unittest.main()
