import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash.attribution import (
    attribute,
    clean_label,
    dominant_workstream,
    infer_from_cwd,
    is_explicit,
    resolve_lead,
    ws_from_bundle_path,
)

HUB = "00000000-0000-0000-0000-000000000001"
PLAN = "00000000-0000-0000-0000-000000000002"


class AttributionTest(unittest.TestCase):
    def test_regex_rules(self):
        self.assertEqual(attribute("ws7-landing"), ("WS7", "regex"))
        self.assertEqual(attribute("ws2-3"), ("WS2", "regex"))
        self.assertEqual(attribute("ws1-12"), ("WS1", "regex"))
        self.assertEqual(attribute("WS1.4"), ("WS1", "regex"))
        self.assertEqual(attribute("dash-preview"), ("TOOLS", "regex"))
        self.assertEqual(attribute("g-review"), ("WS1", "regex"))

    def test_label_beats_session(self):
        self.assertEqual(attribute("ws7-landing", HUB), ("WS7", "regex"))

    def test_session_defaults(self):
        self.assertEqual(attribute(None, HUB), ("WS1", "session-default"))
        self.assertEqual(attribute("", PLAN), ("PLANNING", "session-default"))
        self.assertEqual(attribute("hello", HUB), ("WS1", "session-default"))

    def test_child_does_not_inherit_session(self):
        self.assertEqual(attribute("hello", HUB, parent_session_id="parent"), (None, None))
        self.assertEqual(attribute(None, HUB, parent_session_id="parent"), (None, None))
        self.assertEqual(attribute("ws2-3", HUB, parent_session_id="parent"), ("WS2", "regex"))

    def test_blank(self):
        self.assertEqual(attribute("nope-label"), (None, None))
        self.assertEqual(attribute(None, "not-a-session"), (None, None))

    def test_exact_beats_regex(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.json"
            path.write_text(
                json.dumps({"exact": {"ws7-landing": "WS9"}, "regex": [["^ws(\\d+)", "WS{1}"]], "sessions": {}}),
                encoding="utf-8",
            )
            self.assertEqual(attribute("ws7-landing", path=path), ("WS9", "label"))
            self.assertEqual(attribute("ws2-3", path=path), ("WS2", "regex"))

    def test_reloads_on_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.json"
            path.write_text(json.dumps({"exact": {}, "regex": [], "sessions": {}}), encoding="utf-8")
            self.assertEqual(attribute("zzz", path=path), (None, None))
            path.write_text(
                json.dumps({"exact": {"zzz": "WS3"}, "regex": [], "sessions": {}}),
                encoding="utf-8",
            )
            os.utime(path, (time.time() + 5, time.time() + 5))
            self.assertEqual(attribute("zzz", path=path), ("WS3", "label"))

    def test_clean_label_drops_prompt_sized_text(self):
        self.assertIsNone(clean_label("x" * 500))
        self.assertIsNone(clean_label("has\nnewline"))
        self.assertEqual(clean_label("  ws7-landing  "), "ws7-landing")


class InheritanceTest(unittest.TestCase):
    def test_explicit_beats_parent_workstream(self):
        self.assertEqual(
            attribute("ws2-3", HUB, parent_session_id="p1", parent_workstream="WS7"),
            ("WS2", "regex"),
        )
        self.assertEqual(
            attribute(None, HUB, parent_session_id=HUB, parent_workstream="WS7"),
            ("WS7", "inherited"),
        )

    def test_child_inherits_parent_workstream(self):
        self.assertEqual(
            attribute("helper", "unknown-sid", parent_session_id="p1", parent_workstream="WS4"),
            ("WS4", "inherited"),
        )
        self.assertEqual(
            attribute(None, "unknown-sid", parent_session_id="p1", parent_workstream="WS4"),
            ("WS4", "inherited"),
        )

    def test_child_without_parent_workstream_falls_back(self):
        self.assertEqual(
            attribute("helper", HUB, parent_session_id="parent"), (None, None)
        )
        self.assertEqual(
            attribute("helper", HUB, parent_session_id=HUB), ("WS1", "session-default")
        )

    def test_is_explicit(self):
        for confidence in ("label", "regex", "session-default"):
            self.assertTrue(is_explicit(confidence))
        for confidence in ("inherited", "inferred", None, "", "other"):
            self.assertFalse(is_explicit(confidence))


class DominantWorkstreamTest(unittest.TestCase):
    def test_clear_majority_wins(self):
        self.assertEqual(dominant_workstream(["WS7"] * 3), "WS7")
        self.assertEqual(dominant_workstream(["WS7"] * 2 + ["WS1"]), "WS7")
        self.assertEqual(dominant_workstream(["WS7"] * 3 + ["WS1"] * 2), "WS7")

    def test_needs_three_runs(self):
        self.assertIsNone(dominant_workstream(["WS7"] * 2))
        self.assertIsNone(dominant_workstream(["WS7"]))
        self.assertIsNone(dominant_workstream([]))

    def test_ties_stay_unassigned(self):
        self.assertIsNone(dominant_workstream(["WS7"] * 2 + ["WS1"] * 2))
        self.assertIsNone(dominant_workstream(["WS7", "WS1", "WS2"]))

    def test_split_field_stays_unassigned(self):
        self.assertIsNone(dominant_workstream(["WS7"] * 2 + ["WS1"] * 2 + ["WS2"]))

    def test_blanks_ignored(self):
        self.assertEqual(dominant_workstream(["WS7"] * 3 + [None, "", "  "]), "WS7")
        self.assertIsNone(dominant_workstream([None, None, None]))


class InferFromCwdTest(unittest.TestCase):
    def test_branch_prefix_and_path_segment(self):
        self.assertEqual(infer_from_cwd("/repo/wt-ws2/notes"), "WS2")
        self.assertEqual(infer_from_cwd("ws4/thing"), "WS4")
        self.assertEqual(infer_from_cwd("/repo/train-ws2-x"), "WS2")

    def test_train_branch_names_win(self):
        branches = {"train/notes-cutover": "WS4"}
        self.assertEqual(
            infer_from_cwd("/scratch/train-notes-cutover", branches), "WS4"
        )
        self.assertEqual(
            infer_from_cwd("/scratch/train/notes-cutover", branches), "WS4"
        )

    def test_no_false_positives(self):
        self.assertIsNone(infer_from_cwd("/repo/wt-w2/scratch"))
        self.assertIsNone(infer_from_cwd("/repo/main-checkout"))
        self.assertIsNone(infer_from_cwd(None))
        self.assertIsNone(infer_from_cwd(""))


class BundlePathTest(unittest.TestCase):
    def test_bundle_dir_maps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(ws_from_bundle_path(str(root / "WS4" / "plan.json"), root), "WS4")
            self.assertEqual(ws_from_bundle_path(str(root / "TOOLS" / "x.md"), root), "TOOLS")

    def test_non_workstream_dir_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(ws_from_bundle_path(str(root / "OVERVIEW" / "x.md"), root))
            self.assertIsNone(ws_from_bundle_path("/elsewhere/WS4/plan.json", root))
            self.assertIsNone(ws_from_bundle_path(None, root))
            self.assertIsNone(ws_from_bundle_path("", root))


class ResolveLeadTest(unittest.TestCase):
    def test_workers_beat_cwd_beats_hint(self):
        self.assertEqual(
            resolve_lead(["WS7"] * 3, "/repo/wt-ws2/x", "WS4"), ("WS7", "inferred")
        )
        self.assertEqual(
            resolve_lead(["WS7", "WS1"], "/repo/wt-ws2/x", "WS4"), ("WS2", "inferred")
        )
        self.assertEqual(
            resolve_lead(["WS7", "WS1"], "/repo/main", "WS4"), ("WS4", "inferred")
        )

    def test_nothing_qualifies(self):
        self.assertEqual(resolve_lead([], "/repo/main", None), (None, None))
        self.assertEqual(resolve_lead(["WS7", "WS1"], "/repo/main", "nope"), (None, None))


if __name__ == "__main__":
    unittest.main()
