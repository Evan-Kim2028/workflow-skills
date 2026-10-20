"""Canonical labels: make builds them, check judges them. No live state.

The launch-gate tests for ``launch_grok.sh`` and ``report start``
stay with the lane that owns those gates; this branch carries the
``label make``/``label check`` command and the ``run_worker`` reuse
refusal only.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import labels  # noqa: E402


class MakeTests(unittest.TestCase):
    def test_canonical(self):
        self.assertEqual(
            labels.make("ws11", "plan", "scout-10-partition-cold", round=1),
            "ws11-plan-scout-10-partition-cold-r1",
        )
        self.assertEqual(labels.make("hub", "coord", "dash-gate"), "hub-coord-dash-gate")

    def test_normalises(self):
        self.assertEqual(labels.make("WS11", "plan", "Scout 10_Partition.Cold"), "ws11-plan-scout-10-partition-cold")
        self.assertEqual(labels.make("ws11", "exec", "x", round="2"), "ws11-exec-x-r2")

    def test_rejects(self):
        for args in (
            ("xx", "plan", "x"),
            ("ws0", "plan", "x"),
            ("ws11", "do", "x"),
            ("ws11", "plan", "---"),
            ("ws11", "plan", "x", 0),
        ):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    labels.make(*args)


class ParseTests(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(
            labels.parse("ws11-plan-scout-10-partition-cold-r1"),
            {"scope": "ws11", "mark": "plan", "slug": "scout-10-partition-cold", "round": 1},
        )
        self.assertEqual(
            labels.parse("hub-exec-x"),
            {"scope": "hub", "mark": "exec", "slug": "x", "round": None},
        )

    def test_rejects(self):
        for bad in ("ws11-visuals", "WS11-plan-x", "ws11_plan_x", "xx-plan-x", "", "ws11-plan-"):
            with self.subTest(bad=bad):
                self.assertIsNone(labels.parse(bad))


class CheckTests(unittest.TestCase):
    def test_convention_marks(self):
        for label, phase in (
            ("ws11-plan-x", "planning"),
            ("ws11-exec-x", "execution"),
            ("ws11-review-x", "review"),
            ("ws11-coord-x", "coordination"),
            ("hub-plan-dash-gate", "planning"),
            ("hub-exec-ops", "execution"),
            ("ws11-plan-scout-10-partition-cold-r1", "planning"),
        ):
            with self.subTest(label=label):
                got = labels.check(label, plan_ids=[])
                self.assertTrue(got["ok"], got)
                self.assertEqual(got["phase"], phase)
                self.assertEqual(got["rule"], "convention")

    def test_task_lane_is_execution(self):
        got = labels.check("ws11-pgload-hook", plan_ids=["WS11.pgload", "WS11.daily"])
        self.assertEqual((got["ok"], got["phase"], got["rule"]), (True, "execution", "task"))
        self.assertEqual(got["task"], "WS11.pgload")

    def test_task_lane_keeps_documented_spelling(self):
        # Uppercase task ids (ws4-WS4.1-slug) stay valid when the plan
        # match proves them: the match is the evidence.
        got = labels.check("ws4-WS4.1-notes-index", plan_ids=["WS4.1", "WS4.2"])
        self.assertEqual((got["ok"], got["phase"], got["rule"]), (True, "execution", "task"))

    def test_dirty_mark_and_review_get_canonical_fix(self):
        got = labels.check("ws11-PLAN-x", plan_ids=[])
        self.assertFalse(got["ok"])
        self.assertIn("use ws11-plan-x", got["fix"] or "")
        got = labels.check("WS9-F-SWITCH-FIX3", plan_ids=[])
        self.assertFalse(got["ok"])
        self.assertIn("use ws9-f-switch-fix3", got["fix"] or "")

    def test_suggested_fix_passes(self):
        for bad in (
            "ws11-scout-10-partition-cold-r1",
            "ws11-visuals",
            "hub-thing",
        ):
            with self.subTest(bad=bad):
                fix = labels.check(bad, plan_ids=[])["fix"] or ""
                match = __import__("re").search(r"use (\S+)", fix)
                self.assertTrue(match, fix)
                again = labels.check(match.group(1), plan_ids=[])
                self.assertTrue(again["ok"], (bad, fix, again))
        # A task lane without a readable plan is refused, but the fix is
        # a valid marked label.
        fix = labels.check("ws9-WS9.1-probe", plan_ids=[])["fix"] or ""
        self.assertIn("use ws9-plan-ws9-1-probe", fix)
        self.assertTrue(labels.check("ws9-plan-ws9-1-probe", plan_ids=[])["ok"])

    def test_review_affixes(self):
        for label in ("ws9-rev-s-itemkey", "ws9-f-switch-fix3", "ws9-x-check2", "ws9-x-rev-y"):
            with self.subTest(label=label):
                got = labels.check(label, plan_ids=[])
                self.assertEqual((got["ok"], got["phase"], got["rule"]), (True, "review", "review-affix"))

    def test_scopeless_review_shape_is_refused(self):
        # Legacy r-/rev- leads have no workstream, so no new run may use
        # them: the classifier still reads old ones as review.
        for label in ("r-ws9-thing", "rev-ws9-thing"):
            with self.subTest(label=label):
                got = labels.check(label, plan_ids=[])
                self.assertFalse(got["ok"], got)
                self.assertIn("label make", got["fix"] or "")

    def test_conflict_is_warning_not_refusal(self):
        got = labels.check("ws11-plan-pgload-hook", plan_ids=["WS11.pgload"])
        self.assertTrue(got["ok"])
        self.assertEqual(got["phase"], "planning")
        self.assertIn("warning", (got["warning"] or "").lower())
        self.assertIn("WS11.pgload", got["warning"] or "")

    def test_refusal_names_exact_fix(self):
        got = labels.check("ws11-scout-10-partition-cold-r1", plan_ids=[])
        self.assertFalse(got["ok"])
        self.assertIn("use ws11-plan-scout-10-partition-cold-r1", got["fix"] or "")
        got = labels.check("ws11-visuals", plan_ids=[])
        self.assertFalse(got["ok"])
        self.assertIn("use ws11-plan-visuals", got["fix"] or "")

    def test_hub_needs_mark(self):
        got = labels.check("hub-something", plan_ids=[])
        self.assertFalse(got["ok"])
        self.assertIn("use hub-plan-something", got["fix"] or "")

    def test_bad_shapes(self):
        for bad in ("", "no-scope", "xx-plan-x", "ws11-plan-", "UPPER-plan-x", "ws9-WS9.1-probe"):
            with self.subTest(bad=bad):
                got = labels.check(bad, plan_ids=[])
                self.assertFalse(got["ok"], got)
                self.assertTrue(got["fix"], got)

    def test_normalised_fix_is_canonical(self):
        got = labels.check("ws9-WS9.1-probe", plan_ids=[])
        self.assertIn("use ws9-plan-ws9-1-probe", got["fix"] or "")

    def test_ws_mismatch(self):
        got = labels.check("ws12-trim", ws="WS11", plan_ids=[])
        self.assertFalse(got["ok"])
        self.assertIn("use ws11-plan-trim", got["fix"] or "")

    def test_review_patterns_match_catalog(self):
        catalog = json.loads((ROOT / "catalog" / "phases.json").read_text(encoding="utf-8"))
        affix = next(rule for rule in catalog["rules"] if rule["id"] == "review-affix")
        self.assertEqual(list(labels.REVIEW_PATTERNS), list(affix["patterns"]))


class LegacyTests(unittest.TestCase):
    def test_hatch(self):
        old = os.environ.get(labels.LEGACY_ENV)
        try:
            os.environ.pop(labels.LEGACY_ENV, None)
            self.assertFalse(labels.legacy_allowed())
            os.environ[labels.LEGACY_ENV] = "1"
            self.assertTrue(labels.legacy_allowed())
        finally:
            if old is None:
                os.environ.pop(labels.LEGACY_ENV, None)
            else:
                os.environ[labels.LEGACY_ENV] = old


if __name__ == "__main__":
    unittest.main()
