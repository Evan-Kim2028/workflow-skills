"""Builder tests for the project roadmap timeline (dash/projecttimeline.py).

Pure data in, payload out: dates come only from recorded evidence
(mapped runs, history moves, state moves), dateless lanes read
``before_history`` (except planned estimates), and lanes order
topological by depends_on, then start, grouped Done / Active / Planned.
"""

import json
import os
import tempfile
import unittest

from dash import projecttimeline as timeline

NOW = 1_762_000_000.0


def bundle(ws, **fields):
    order = fields.pop("order", None)
    if order is None:
        try:
            order = int(ws[2:])
        except ValueError:
            order = 0
    base = {
        "id": ws,
        "title": "Probe %s" % ws,
        "short": ws,
        "order": order,
        "kind": "workstream",
        "state": "active",
        "state_changed_at": NOW - 50,
        "depends_on": [],
        "milestone": None,
    }
    base.update(fields)
    return base


class LaneDatesTest(unittest.TestCase):
    def test_started_is_earliest_of_run_and_history(self):
        dates = timeline.lane_dates(
            [{"ts": NOW - 500, "task": "WS42.1", "from": "todo", "to": "wip"}],
            first_run_at=NOW - 900,
            last_run_at=NOW - 100,
        )
        self.assertEqual(dates["started_at"], NOW - 900)
        self.assertEqual(dates["last_active_at"], NOW - 100)
        self.assertIsNone(dates["done_at"])
        self.assertFalse(dates["done_inferred"])

    def test_started_from_history_when_no_runs(self):
        dates = timeline.lane_dates(
            [{"ts": NOW - 500, "task": "WS42.1", "from": "todo", "to": "wip"}]
        )
        self.assertEqual(dates["started_at"], NOW - 500)
        self.assertEqual(dates["last_active_at"], NOW - 500)

    def test_unknown_when_no_evidence(self):
        dates = timeline.lane_dates([], None, None)
        self.assertIsNone(dates["started_at"])
        self.assertIsNone(dates["last_active_at"])
        self.assertIsNone(dates["done_at"])
        self.assertFalse(dates["has_history"])

    def test_state_move_dates_done_without_inference(self):
        dates = timeline.lane_dates(
            [
                {"ts": NOW - 500, "task": "WS42.1", "from": "todo", "to": "done"},
                {"kind": "state", "ts": NOW - 50, "ws": "WS42", "from": "active", "to": "done"},
            ],
            progress={"done": 3, "total": 3, "pct": 100},
        )
        self.assertEqual(dates["done_at"], NOW - 50)
        self.assertFalse(dates["done_inferred"])

    def test_last_task_done_is_inferred_at_full_plan(self):
        dates = timeline.lane_dates(
            [
                {"ts": NOW - 500, "task": "WS42.1", "from": "todo", "to": "done"},
                {"ts": NOW - 100, "task": "WS42.2", "from": "pr", "to": "done"},
            ],
            progress={"done": 2, "total": 2, "pct": 100},
        )
        self.assertEqual(dates["done_at"], NOW - 100)
        self.assertTrue(dates["done_inferred"])

    def test_no_inferred_done_below_full_plan(self):
        dates = timeline.lane_dates(
            [{"ts": NOW - 100, "task": "WS42.1", "from": "todo", "to": "done"}],
            progress={"done": 1, "total": 2, "pct": 50},
        )
        self.assertIsNone(dates["done_at"])
        self.assertFalse(dates["done_inferred"])

    def test_bad_rows_are_ignored(self):
        dates = timeline.lane_dates(
            ["junk", {"no": "ts"}, {"ts": "soon", "task": "WS42.1", "to": "done"}, None],
            first_run_at=True,
        )
        self.assertIsNone(dates["started_at"])
        self.assertIsNone(dates["done_at"])


class LaneForTest(unittest.TestCase):
    def test_before_history_band_for_dateless_active(self):
        lane = timeline.lane_for(bundle("WS42"), [], {"done": 0, "total": 2, "pct": 0}, {})
        self.assertTrue(lane["before_history"])
        self.assertTrue(lane["has_history"] is False)
        self.assertIsNone(lane["estimate_days"])

    def test_planned_lane_is_an_estimate_not_a_band(self):
        lane = timeline.lane_for(
            bundle("WS41", state="planned", depends_on=["WS42"]),
            [],
            {"done": 0, "total": 3, "pct": 0},
            {},
            states={"WS41": "planned", "WS42": "active"},
        )
        self.assertFalse(lane["before_history"])
        self.assertEqual(lane["estimate_days"], 7)
        self.assertEqual(lane["blocked_by"], ["WS42"])

    def test_estimate_scales_with_open_tasks(self):
        lane = timeline.lane_for(
            bundle("WS41", state="planned"),
            [],
            {"done": 1, "total": 11, "pct": 9},
            {},
        )
        self.assertEqual(lane["estimate_days"], 20)

    def test_done_dep_is_not_blocking(self):
        lane = timeline.lane_for(
            bundle("WS41", state="planned", depends_on=["WS42", "WS43", "WS99"]),
            [],
            None,
            None,
            states={"WS41": "planned", "WS42": "done", "WS43": "archived"},
        )
        self.assertEqual(lane["blocked_by"], ["WS99"])

    def test_unknown_state_reads_active(self):
        lane = timeline.lane_for(bundle("WS42", state="sleeping"), [], None, None)
        self.assertEqual(lane["state"], "active")
        self.assertEqual(lane["group"], "active")


class OrderLanesTest(unittest.TestCase):
    def test_groups_then_dependencies_then_start(self):
        lanes = [
            {"id": "WS43", "group": "active", "depends_on": ["WS44"], "started_at": NOW - 300, "order": 43},
            {"id": "WS44", "group": "active", "depends_on": [], "started_at": NOW - 900, "order": 44},
            {"id": "WS42", "group": "done", "depends_on": [], "started_at": NOW - 100, "order": 42},
            {"id": "WS41", "group": "planned", "depends_on": ["WS44"], "started_at": None, "order": 41},
        ]
        self.assertEqual(
            timeline.order_lanes(lanes), ["WS42", "WS44", "WS43", "WS41"]
        )

    def test_band_lanes_sort_by_plan_order(self):
        lanes = [
            {"id": "WS45", "group": "active", "depends_on": [], "started_at": None, "order": 45},
            {"id": "WS43", "group": "active", "depends_on": [], "started_at": None, "order": 43},
        ]
        self.assertEqual(timeline.order_lanes(lanes), ["WS43", "WS45"])

    def test_cycle_keeps_every_lane(self):
        lanes = [
            {"id": "WS43", "group": "active", "depends_on": ["WS44"], "started_at": None, "order": 43},
            {"id": "WS44", "group": "active", "depends_on": ["WS43"], "started_at": None, "order": 44},
        ]
        self.assertEqual(sorted(timeline.order_lanes(lanes)), ["WS43", "WS44"])


class BuildTimelineTest(unittest.TestCase):
    def test_program_bundles_are_skipped(self):
        payload = timeline.build_timeline(
            [bundle("WS42"), dict(bundle("OVERVIEW"), kind="program")],
            {},
            {},
            {},
            None,
            NOW,
        )
        self.assertEqual(payload["order"], ["WS42"])
        self.assertEqual([lane["id"] for lane in payload["lanes"]], ["WS42"])

    def test_history_floor_and_project_fallback(self):
        payload = timeline.build_timeline(
            [bundle("WS42")],
            {"WS42": [{"ts": NOW - 500, "task": "WS42.1", "to": "wip"}]},
            {"WS42": {"done": 0, "total": 1, "pct": 0}},
            {"WS42": {"runs": 2, "tokens": 1500, "cost": 0.01, "first_run_at": NOW - 900, "last_run_at": NOW - 100}},
            None,
            NOW,
        )
        self.assertEqual(payload["history_floor"], NOW - 500)
        self.assertEqual(payload["project"], {"started": None, "target": None, "phases": []})
        lane = payload["lanes"][0]
        self.assertEqual(lane["started_at"], NOW - 900)
        self.assertEqual(lane["runs"], 2)
        self.assertEqual(lane["tokens"], 1500)
        self.assertEqual(lane["progress"], {"done": 0, "total": 1, "pct": 0})

    def test_project_block_passes_through(self):
        payload = timeline.build_timeline(
            [],
            {},
            {},
            {},
            {"started": "2025-10-01", "target": None, "phases": [{"title": "First wave"}]},
            NOW,
        )
        self.assertEqual(payload["project"]["started"], "2025-10-01")
        self.assertIsNone(payload["project"]["target"])
        self.assertEqual(payload["project"]["phases"], [{"title": "First wave"}])
        self.assertIsNone(payload["history_floor"])
        self.assertEqual(payload["lanes"], [])


class ReadHelpersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "ws")
        os.makedirs(os.path.join(self.root, "WS42"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_raw_history_keeps_state_lines_oldest_first(self):
        with open(os.path.join(self.root, "WS42", "history.jsonl"), "w") as fh:
            fh.write('{"kind": "state", "ts": %d, "ws": "WS42", "from": "active", "to": "done"}\n' % int(NOW - 50))
            fh.write("not json\n")
            fh.write('{"ts": %d, "task": "WS42.1", "from": "todo", "to": "done"}\n' % int(NOW - 500))
            fh.write('{"ts": "soon", "task": "WS42.2", "to": "wip"}\n')
        rows = timeline.read_history_raw("WS42", self.root)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["task"], "WS42.1")
        self.assertEqual(rows[1]["kind"], "state")

    def test_raw_history_missing_file_is_empty(self):
        self.assertEqual(timeline.read_history_raw("WS43", self.root), [])

    def test_project_reader_falls_back_to_none(self):
        self.assertIsNone(timeline.read_project(self.root))
        with open(os.path.join(self.root, "project.json"), "w") as fh:
            json.dump({"started": "2025-10-01", "target": None, "phases": [{"title": "First wave", "state": "done"}]}, fh)
        project = timeline.read_project(self.root)
        self.assertEqual(project["started"], "2025-10-01")
        self.assertIsNone(project["target"])
        self.assertEqual(project["phases"], [{"title": "First wave", "state": "done"}])
        with open(os.path.join(self.root, "project.json"), "w") as fh:
            fh.write("{broken")
        self.assertIsNone(timeline.read_project(self.root))


if __name__ == "__main__":
    unittest.main()
