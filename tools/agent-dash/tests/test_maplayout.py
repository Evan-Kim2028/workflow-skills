"""Golden layouts and lint thresholds for the plan-progress map standard."""
import json
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import maplayout

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def tiny_plan(**over) -> dict:
    """A minimal clean plan: 2 staged columns, short ids, no warnings."""
    body = {
        "ws": "WS9",
        "cols": ["Shape", "Build"],
        "groups": [
            {
                "id": "g1",
                "col": 0,
                "title": "First stage group",
                "tasks": [
                    {"id": "WS9.1", "title": "Task WS9.1", "status": "done"},
                    {"id": "WS9.2", "title": "Task WS9.2", "status": "todo", "deps": ["WS9.1"]},
                ],
            },
            {
                "id": "g2",
                "col": 1,
                "title": "Second stage group",
                "tasks": [
                    {"id": "WS9.3", "title": "Task WS9.3", "status": "todo", "deps": ["WS9.2"]},
                    {"id": "WS9.4", "title": "Task WS9.4", "status": "todo", "deps": ["WS9.3"]},
                ],
            },
        ],
        "edges": [["g1", "g2"]],
        "phases": [{"name": "Shape", "goal": "Shape it.", "gate": "Shaped."}],
        "trains": [
            {"id": "t1", "title": "First train", "members": ["WS9.2", "WS9.3"], "status": "planned"},
        ],
    }
    body.update(over)
    return body


def big_group_plan(n: int, status: str = "done") -> dict:
    body = tiny_plan()
    body["groups"] = [
        {
            "id": "g1",
            "col": 0,
            "title": "Big group",
            "tasks": [{"id": f"WS9.{pos}", "title": f"Task {pos}", "status": status} for pos in range(1, n + 1)],
        }
    ]
    body["edges"] = []
    return body


class MapLayoutGoldenTest(unittest.TestCase):
    def test_ws1_shape_stays_compact_and_calm(self):
        plan = load("map-WS1.json")
        result = maplayout.layout(plan)
        self.assertEqual(result["ws"], "WS1")
        self.assertEqual(result["density"], "compact")
        self.assertFalse(result["ultra"])
        self.assertEqual(result["live_tasks"], 77)
        self.assertEqual(result["deferred_tasks"], 15)
        self.assertEqual(result["arrows"]["mode"], "on-focus")
        self.assertEqual(result["arrows"]["task_edges"], 154)
        self.assertEqual(result["arrows"]["group_edges"], 20)
        self.assertEqual(result["arrows"]["reason"], "154 task deps: arrows on focus")
        self.assertEqual(result["arrows"]["fan_in"], [{"id": "109", "deps": 10}, {"id": "47", "deps": 8}])
        self.assertEqual([col["state"] for col in result["columns"]], ["full"] * 5)
        self.assertIsNone(result["folded_rail"])
        for group in result["groups"]:
            self.assertEqual(group["state"], "open", group["id"])
            self.assertIsNone(group["more_token"], group["id"])
        tokens = {group["id"]: group["deferred_token"] for group in result["groups"]}
        self.assertEqual(tokens["g11"], "↷7 deferred")
        self.assertEqual(tokens["g14"], "↷6 deferred")
        self.assertEqual(tokens["g7"], "↷1 deferred")
        self.assertEqual(tokens["g10"], "↷1 deferred")
        self.assertEqual(result["trains"]["tagged"], [])
        self.assertEqual(result["trains"]["untagged"], [])
        self.assertIsNone(result["trains"]["caption"])
        self.assertEqual(result["labels"]["shortened"], 0)
        self.assertEqual(result["labels"]["collisions"], [])
        self.assertEqual(result["phases_line"], "")
        self.assertEqual(result["balance"]["tallest"]["tasks"], 28)
        self.assertEqual(result["balance"]["shortest"]["tasks"], 10)
        self.assertEqual([warn["code"] for warn in maplayout.map_warnings(plan)], ["MAP108"])

    def test_ws7_shape_collapses_everywhere(self):
        plan = load("map-WS7.json")
        result = maplayout.layout(plan)
        self.assertEqual(result["density"], "compact")
        self.assertTrue(result["ultra"])
        self.assertEqual(result["live_tasks"], 101)
        self.assertEqual(result["deferred_tasks"], 16)
        self.assertEqual(result["arrows"]["mode"], "on-focus")
        self.assertEqual(result["arrows"]["task_edges"], 126)
        states = {group["id"]: group["state"] for group in result["groups"]}
        self.assertTrue(states)
        self.assertEqual(set(states.values()), {"collapsed-done"})
        rows = {group["id"]: group for group in result["groups"]}
        self.assertEqual(rows["g8"]["more_token"], "+31 done")
        self.assertEqual(rows["g8"]["deferred_token"], "↷12 deferred")
        self.assertEqual(rows["g8"]["shown_done"], 4)
        self.assertEqual(len(rows["g8"]["hidden_done_ids"]), 31)
        self.assertEqual(rows["g4"]["more_token"], "+23 done")
        self.assertEqual(rows["g4"]["deferred_token"], "↷1 deferred")
        self.assertEqual(
            [warn["code"] for warn in maplayout.map_warnings(plan)],
            ["MAP101", "MAP101", "MAP104", "MAP106", "MAP108", "MAP110", "MAP110", "MAP110", "MAP110"],
        )

    def test_ws8_shape_slims_done_columns_and_untags_solo_trains(self):
        plan = load("map-WS8.json")
        result = maplayout.layout(plan)
        self.assertEqual(result["density"], "compact")
        self.assertFalse(result["ultra"])
        self.assertEqual(result["live_tasks"], 69)
        self.assertEqual(result["arrows"]["mode"], "on-focus")
        self.assertEqual(result["arrows"]["task_edges"], 101)
        self.assertEqual(result["arrows"]["fan_in"], [{"id": "WS8.47", "deps": 10}])
        self.assertEqual(
            [col["state"] for col in result["columns"]],
            ["full", "slim", "full", "slim", "slim", "full", "full"],
        )
        self.assertIsNone(result["folded_rail"])
        rows = {group["id"]: group for group in result["groups"]}
        guard = rows["g1"]
        self.assertEqual(guard["state"], "collapsed-done")
        self.assertEqual(guard["more_token"], "+7 done")
        self.assertEqual(guard["hidden_done"], 7)
        self.assertEqual(len(guard["chips"]), 8)
        self.assertEqual(
            [(chip["id"], chip["kind"]) for chip in guard["chips"]],
            [
                ("WS8.59", "gate"),
                ("WS8.60", "gate"),
                ("WS8.55", "task"),
                ("WS8.3", "task"),
                ("WS8.52", "task"),
                ("WS8.53", "task"),
                ("WS8.56", "task"),
                ("WS8.57", "task"),
            ],
        )
        by_id = {chip["id"]: chip for chip in guard["chips"]}
        self.assertEqual(by_id["WS8.52"]["label"], "52")
        self.assertTrue(by_id["WS8.52"]["tooltip"].startswith("WS8.52 · "))
        for slim in ("g2", "g4", "g5"):
            self.assertEqual(rows[slim]["state"], "slim")
            self.assertEqual(rows[slim]["chips"], [])
        self.assertEqual(result["trains"]["tagged"], [])
        self.assertEqual(len(result["trains"]["untagged"]), 21)
        self.assertEqual(result["trains"]["caption"], "21 single-task trains untagged (MAP107)")
        self.assertEqual(result["trains"]["legend"], [])
        self.assertEqual(result["trains"]["more_trains"], 0)
        self.assertEqual(result["labels"]["shortened"], 70)
        self.assertTrue(result["phases_line"].startswith("1 Stage 1"))
        self.assertAlmostEqual(result["balance"]["ratio"], 20 / 3)
        codes = [warn["code"] for warn in maplayout.map_warnings(plan)]
        self.assertEqual(codes[0], "MAP101")
        self.assertIn("MAP102", codes)
        self.assertIn("MAP104", codes)
        self.assertIn("MAP106", codes)
        self.assertEqual(codes.count("MAP107"), 21)
        self.assertEqual(codes[-1], "MAP108")

    def test_layout_is_deterministic(self):
        plan = load("map-WS8.json")
        first = json.dumps(maplayout.layout(plan), sort_keys=True)
        second = json.dumps(maplayout.layout(plan), sort_keys=True)
        self.assertEqual(first, second)
        third = json.dumps(maplayout.map_warnings(plan), sort_keys=True)
        fourth = json.dumps(maplayout.map_warnings(plan), sort_keys=True)
        self.assertEqual(third, fourth)


class MapLayoutThresholdTest(unittest.TestCase):
    def test_density_boundaries(self):
        self.assertEqual(maplayout.layout(big_group_plan(40, "todo"))["density"], "comfortable")
        self.assertEqual(maplayout.layout(big_group_plan(41, "todo"))["density"], "compact")
        self.assertFalse(maplayout.layout(big_group_plan(80, "todo"))["ultra"])
        self.assertTrue(maplayout.layout(big_group_plan(81, "todo"))["ultra"])
        self.assertEqual(maplayout.layout(tiny_plan(), density="comfortable")["density"], "comfortable")
        with self.assertRaises(ValueError):
            maplayout.layout(tiny_plan(), density="airy")

    def test_arrows_boundary(self):
        body = tiny_plan()
        body["groups"][0]["tasks"][1]["deps"] = [f"WS9.{pos}" for pos in range(100, 100)]
        self.assertEqual(maplayout.layout(body)["arrows"]["mode"], "all")
        body["groups"][1]["tasks"][0]["deps"] = [f"dep-{pos}" for pos in range(41)]
        result = maplayout.layout(body)
        self.assertEqual(result["arrows"]["mode"], "on-focus")
        self.assertEqual(result["arrows"]["task_edges"], 42)

    def test_collapse_boundary(self):
        self.assertEqual(maplayout.layout(big_group_plan(12))["groups"][0]["state"], "open")
        collapsed = maplayout.layout(big_group_plan(13))["groups"][0]
        self.assertEqual(collapsed["state"], "collapsed-done")
        self.assertEqual(collapsed["more_token"], "+9 done")
        self.assertEqual(collapsed["shown_done"], 4)
        self.assertEqual(len(collapsed["chips"]), 4)

    def test_ultra_collapses_small_groups(self):
        result = maplayout.layout(big_group_plan(81, "todo"))
        self.assertTrue(result["ultra"])
        self.assertEqual(result["groups"][0]["state"], "collapsed-done")

    def test_fan_in_boundary(self):
        body = tiny_plan()
        body["groups"][1]["tasks"][0]["deps"] = [f"dep-{pos}" for pos in range(6)]
        self.assertEqual(maplayout.layout(body)["arrows"]["fan_in"], [])
        body["groups"][1]["tasks"][0]["deps"] = [f"dep-{pos}" for pos in range(7)]
        result = maplayout.layout(body)
        self.assertEqual(result["arrows"]["fan_in"], [{"id": "WS9.3", "deps": 7}])
        chips = {chip["id"]: chip for group in result["groups"] for chip in group["chips"]}
        self.assertEqual(chips["WS9.3"]["fan_in"], 7)

    def test_chip_label_cap_and_prefix_strip(self):
        self.assertEqual(maplayout.short_label("WS8.52"), "52")
        self.assertEqual(maplayout.short_label("74b"), "74b")
        self.assertEqual(maplayout.capped_label("WS8.52"), "52")
        long_id = "op-rp-manifest-dryrun"
        self.assertEqual(len(maplayout.capped_label(long_id)), 14)
        self.assertTrue(maplayout.capped_label(long_id).endswith("…"))

    def test_label_collisions_disambiguated(self):
        body = tiny_plan()
        body["groups"][0]["tasks"] = [
            {"id": "averylongtaskname-aaa", "title": "Aaa", "status": "todo"},
            {"id": "averylongtaskname-bbb", "title": "Bbb", "status": "todo"},
        ]
        body["groups"][1]["tasks"] = []
        result = maplayout.layout(body)
        chips = [chip for group in result["groups"] for chip in group["chips"]]
        labels = [chip["label"] for chip in chips]
        self.assertEqual(len(set(labels)), 2)
        self.assertEqual(len(result["labels"]["collisions"]), 1)
        self.assertEqual(
            result["labels"]["collisions"][0]["ids"],
            ["averylongtaskname-aaa", "averylongtaskname-bbb"],
        )

    def test_fold_trailing_columns_into_rail(self):
        body = tiny_plan()
        body["cols"] = ["One", "Two", "Three", "Four", "Five", "Six"]
        body["groups"] = [
            {
                "id": f"g{pos}",
                "col": pos - 1,
                "title": f"Stage group {pos}",
                "tasks": [{"id": f"WS9.{pos}", "title": f"Task {pos}", "status": "todo"}],
            }
            for pos in range(1, 7)
        ]
        body["edges"] = []
        body["trains"] = []
        result = maplayout.layout(body)
        states = [col["state"] for col in result["columns"]]
        self.assertEqual(states, ["full"] * 5 + ["folded"])
        self.assertEqual(result["folded_rail"]["label"], "+1 later stages")
        self.assertEqual([entry["title"] for entry in result["folded_rail"]["columns"]], ["Six"])

    def test_unmet_gate_blocks_the_fold(self):
        body = tiny_plan()
        body["cols"] = ["One", "Two", "Three", "Four", "Five", "Six"]
        body["groups"] = [
            {
                "id": f"g{pos}",
                "col": pos - 1,
                "title": f"Stage group {pos}",
                "tasks": [{"id": f"WS9.{pos}", "title": f"Task {pos}", "status": "todo"}],
            }
            for pos in range(1, 7)
        ]
        body["groups"][5]["tasks"].append(
            {"id": "WS9.0", "title": "Freeze gate", "status": "todo", "kind": "gate", "signal": "the freeze lifts"}
        )
        body["edges"] = []
        body["trains"] = []
        result = maplayout.layout(body)
        self.assertEqual([col["state"] for col in result["columns"]], ["full"] * 6)
        self.assertIsNone(result["folded_rail"])


class MapWarningsTest(unittest.TestCase):
    def codes(self, body: dict) -> list[str]:
        return [warn["code"] for warn in maplayout.map_warnings(body)]

    def test_clean_plan_has_no_warnings(self):
        self.assertEqual(maplayout.map_warnings(tiny_plan()), [])

    def test_warning_shape(self):
        body = tiny_plan()
        body["cols"] = ["One", "Two", "Three", "Four", "Five", "Six"]
        (warn,) = [warn for warn in maplayout.map_warnings(body) if warn["code"] == "MAP102"]
        self.assertEqual(set(warn), {"code", "severity", "title", "detail", "fix"})
        self.assertEqual(warn["severity"], "info")
        self.assertIn("6", warn["title"])
        self.assertTrue(warn["fix"])

    def test_map101_big_group(self):
        warns = maplayout.map_warnings(big_group_plan(15))
        self.assertEqual([warn["code"] for warn in warns], ["MAP101"])
        self.assertIn("g1", warns[0]["title"])
        self.assertEqual(maplayout.map_warnings(big_group_plan(14)), [])

    def test_map103_hollow_column(self):
        body = tiny_plan()
        for task in body["groups"][1]["tasks"]:
            task["status"] = "deferred"
            task["moved_to"] = "WS8"
            task["note"] = "waiting on the freeze"
        codes = self.codes(body)
        self.assertIn("MAP103", codes)

    def test_map104_unbalanced_columns(self):
        body = tiny_plan()
        body["groups"][0]["tasks"] = [
            {"id": f"WS9.{pos}", "title": f"Task {pos}", "status": "todo"} for pos in range(50)
        ]
        body["groups"][1]["tasks"] = [{"id": "WS9.99", "title": "Task 99", "status": "todo"}]
        self.assertIn("MAP104", self.codes(body))
        self.assertNotIn("MAP104", self.codes(tiny_plan()))

    def test_map105_bare_id_title(self):
        body = tiny_plan()
        body["groups"][1]["title"] = "g-ship"
        self.assertIn("MAP105", self.codes(body))
        body["groups"][1]["title"] = "Ship"
        self.assertNotIn("MAP105", self.codes(body))
        body["groups"][1]["title"] = "g2"
        self.assertIn("MAP105", self.codes(body))

    def test_map106_uncovered_stage(self):
        body = tiny_plan()
        body["cols"] = ["Shape", "Build", "Prove"]
        body["groups"][1]["col"] = 2
        body["edges"] = []
        self.assertIn("MAP106", self.codes(body))
        body["edges"] = [["g1", "g2"]]
        self.assertNotIn("MAP106", self.codes(body))

    def test_map107_solo_train(self):
        body = tiny_plan()
        body["trains"] = [{"id": "solo", "title": "Solo", "members": ["WS9.1"], "status": "planned"}]
        self.assertIn("MAP107", self.codes(body))

    def test_map108_many_edges_is_info(self):
        body = tiny_plan()
        body["groups"][1]["tasks"][0]["deps"] = [f"dep-{pos}" for pos in range(41)]
        (warn,) = [warn for warn in maplayout.map_warnings(body) if warn["code"] == "MAP108"]
        self.assertEqual(warn["severity"], "info")

    def test_map109_singleton_groups(self):
        body = tiny_plan()
        body["groups"] = [
            {"id": f"g{pos}", "col": 0, "title": f"Stage group {pos}",
             "tasks": [{"id": f"WS9.{pos}", "title": f"Task {pos}", "status": "todo"}]}
            for pos in range(1, 5)
        ]
        body["edges"] = []
        self.assertIn("MAP109", self.codes(body))

    def test_map110_long_suffix(self):
        body = tiny_plan()
        body["groups"][1]["tasks"][0]["id"] = "op-rp-manifest-dryrun"
        codes = self.codes(body)
        self.assertIn("MAP110", codes)

    def test_map111_deferred_share(self):
        body = tiny_plan()
        body["groups"][0]["tasks"] = [
            {"id": "WS9.1", "title": "Task 1", "status": "done"},
            {"id": "WS9.2", "title": "Task 2", "status": "deferred", "moved_to": "WS8", "note": "waiting"},
            {"id": "WS9.3", "title": "Task 3", "status": "deferred", "moved_to": "WS8", "note": "waiting"},
        ]
        body["groups"][1]["tasks"] = []
        self.assertIn("MAP111", self.codes(body))


class PlanViewShapeTest(unittest.TestCase):
    """Plan views from the plans-map and bundle sources, with optional keys missing or null."""

    def test_plans_map_minimal_view_has_no_optional_keys(self):
        view = {
            "ws": "WS1",
            "cols": ["Shape", "Build"],
            "groups": [
                {
                    "id": "g1",
                    "col": 0,
                    "title": "First stage",
                    "tasks": [
                        {"id": "WS1.1", "title": "Task one", "status": "done"},
                        {"id": "WS1.2", "title": "Task two", "status": "todo"},
                    ],
                },
                {
                    "id": "g2",
                    "col": 1,
                    "title": "Second stage",
                    "tasks": [
                        {"id": "WS1.3", "title": "Task three", "status": "todo"},
                        {"id": "WS1.4", "title": "Task four", "status": "todo"},
                    ],
                },
            ],
        }
        result = maplayout.layout(view)
        self.assertEqual(result["live_tasks"], 4)
        self.assertEqual(result["trains"]["tagged"], [])
        self.assertEqual(result["phases_line"], "")
        self.assertEqual(result["arrows"]["fan_in"], [])
        self.assertEqual(maplayout.map_warnings(view), [])
        json.dumps(result, allow_nan=False)

    def test_none_values_render_like_missing_keys(self):
        view = {
            "ws": None,
            "cols": None,
            "edges": None,
            "trains": None,
            "phases": None,
            "groups": [
                {
                    "id": "g1",
                    "col": None,
                    "title": None,
                    "tasks": [
                        {"id": None, "title": None, "status": None, "deps": None, "kind": None, "dep_notes": None},
                        {"id": "WS1.2", "status": "done", "deps": ["WS1.9"]},
                    ],
                },
                {"id": None, "tasks": None},
            ],
        }
        result = maplayout.layout(view)
        self.assertEqual(result["columns"], [])
        self.assertEqual(result["group_edges"], 0)
        self.assertIsNone(result["balance"]["ratio"])
        self.assertIsInstance(maplayout.map_warnings(view), list)
        json.dumps(result, allow_nan=False)

    def test_empty_shortest_column_keeps_ratio_json_safe(self):
        view = {
            "ws": "WS1",
            "cols": ["Full", "Empty"],
            "groups": [
                {"id": "g1", "col": 0, "title": "Stage", "tasks": [{"id": "1", "status": "todo"}, {"id": "2", "status": "todo"}]},
            ],
        }
        result = maplayout.layout(view)
        self.assertIsNone(result["balance"]["ratio"])
        self.assertEqual(result["balance"]["shortest"]["tasks"], 0)
        json.dumps(result, allow_nan=False)

    def test_empty_view_and_none_view(self):
        for view in ({}, None):
            result = maplayout.layout(view)
            json.dumps(result, allow_nan=False)
            self.assertEqual(maplayout.map_warnings(view), [])


if __name__ == "__main__":
    unittest.main()
