"""Canonical run labels map to plan tasks: ws<N>-<mark>-<slug>[-rK] on the server.

Pins the canonical-label rule in dash/taskmatch.py (exact task id, round
suffix, longest dash-bounded prefix, no match, cross-workstream refusal)
and that the existing taskmap and built-in mappings still win when they
name a plan task. The page copies are pinned to this rule in
tests/test_page_rules.py.
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import taskmatch  # noqa: E402
from dash.phases import resolve_task  # noqa: E402


class CanonicalLabelTask(unittest.TestCase):
    def test_exact_task_id(self):
        self.assertEqual(taskmatch.task_of("ws4-exec-classify", "WS4", {}, ["WS4.classify"]), "WS4.classify")

    def test_round_suffix(self):
        ids = ["WS4.classify", "WS4.plan-x"]
        self.assertEqual(taskmatch.task_of("ws4-exec-classify-r2", "WS4", {}, ids), "WS4.classify")
        self.assertEqual(taskmatch.task_of("ws4-plan-classify-r12", "WS4", {}, ids), "WS4.classify")

    def test_longest_prefix_when_full_slug_is_not_a_task(self):
        self.assertEqual(
            taskmatch.task_of("ws4-exec-classify-fixtures", "WS4", {}, ["WS4.classify"]),
            "WS4.classify",
        )

    def test_full_slug_beats_prefix(self):
        ids = ["WS4.classify", "WS4.classify-fixtures"]
        self.assertEqual(
            taskmatch.task_of("ws4-exec-classify-fixtures-r3", "WS4", {}, ids),
            "WS4.classify-fixtures",
        )

    def test_case_insensitive_returns_plan_casing(self):
        self.assertEqual(taskmatch.task_of("ws4-exec-CLASSIFY", "WS4", {}, ["WS4.Classify"]), "WS4.Classify")

    def test_no_match_keeps_none(self):
        self.assertIsNone(taskmatch.task_of("ws4-exec-nothing", "WS4", {}, ["WS4.classify"]))

    def test_no_plan_ids_keeps_none(self):
        self.assertIsNone(taskmatch.task_of("ws4-exec-classify", "WS4", {}, []))

    def test_cross_workstream_refused(self):
        self.assertIsNone(taskmatch.task_of("ws4-exec-classify", "WS5", {}, ["WS5.classify", "WS4.classify"]))
        self.assertIsNone(taskmatch.task_of("ws4-exec-classify", "WS4", {}, ["WS5.classify"]))

    def test_non_mark_label_untouched(self):
        self.assertIsNone(taskmatch.task_of("ws4-build-classify", "WS4", {}, ["WS4.classify"]))

    def test_existing_taskmap_mapping_wins(self):
        taskmap = {"WS4": {"ws4-exec-classify": "WS4.other"}}
        ids = ["WS4.other", "WS4.classify"]
        self.assertEqual(taskmatch.task_of("ws4-exec-classify", "WS4", taskmap, ids), "WS4.other")

    def test_builtin_phantom_falls_through_to_canonical(self):
        self.assertEqual(taskmatch.task_of("ws7-exec-landing", "WS7", {}, ["WS7.LANDING"]), "WS7.LANDING")

    def test_phases_resolver_uses_the_rule(self):
        self.assertEqual(resolve_task("ws4-exec-classify", "WS4", {}, ["WS4.classify"]), "WS4.classify")
        self.assertIsNone(resolve_task("ws4-exec-classify", "WS5", {}, ["WS5.other"]))


if __name__ == "__main__":
    unittest.main()
