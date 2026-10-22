"""Run-to-task matching: compile-once regex rules, live taskmap edits, and canonical labels.

Pins the task-map hot path that the dashboard hits per run (a taskmap
with many ``/regex/`` rules compiles each rule at most once across many
``task_of`` calls, first-match order and case handling are unchanged,
and an edited taskmap file takes effect without a restart), plus the
canonical-label rule in dash/taskmatch.py (exact task id, round suffix,
longest dash-bounded prefix, no match, cross-workstream refusal) and
that the existing taskmap and built-in mappings still win when they
name a plan task.
"""
import json
import os
import re
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dash import sections  # noqa: E402
from dash import taskmatch  # noqa: E402
from dash.phases import resolve_task  # noqa: E402
from dash.taskmatch import task_of  # noqa: E402

class CompileOnceTest(unittest.TestCase):
    def test_thousand_rules_compile_at_most_once_over_two_thousand_calls(self):
        tag = uuid.uuid4().hex[:8]
        table = {f"/^ws11-{tag}-{i:04d}-/": f"WS11.T{i}" for i in range(1000)}
        table["/^never-matches-zzz$/"] = "WS11.NEVER"
        labels = [f"ws11-{tag}-{i % 1000:04d}-run{i}" for i in range(2000)]
        real_compile = re._compile
        seen = []

        def counting(pattern, *args, **kwargs):
            if isinstance(pattern, str) and tag in pattern:
                seen.append(pattern)
            return real_compile(pattern, *args, **kwargs)

        with mock.patch.object(re, "_compile", side_effect=counting):
            results = [task_of(label, "WS11", {"WS11": table}, ()) for label in labels]
        self.assertLessEqual(len(seen), 1000)
        self.assertEqual(len(set(seen)), len(seen))
        self.assertGreater(len(seen), 0)
        self.assertEqual(results[0], "WS11.T0")
        self.assertEqual(results[999], "WS11.T999")
        self.assertEqual(results[1000], "WS11.T0")

    def test_first_match_wins_and_case_insensitive(self):
        taskmap = {"WS1": {"/RUN-A/": "first", "/run/": "second"}}
        self.assertEqual(task_of("run-a-1", "WS1", taskmap, ()), "first")
        self.assertEqual(task_of("RUN-B", "WS1", taskmap, ()), "second")

    def test_bad_regex_is_skipped(self):
        taskmap = {"WS1": {"/([/": "broken", "/ok/": "fine"}}
        self.assertEqual(task_of("ok-label", "WS1", taskmap, ()), "fine")

    def test_plan_id_substring_longest_wins_with_cached_index(self):
        ids = ["WS9.s", "WS9.s-tbench", "WS9.x"]
        self.assertEqual(task_of("ws9.s-tbench-run", "WS9", {}, ids), "WS9.s-tbench")
        self.assertEqual(task_of("ws9.s-tbench-run", "WS9", {}, ids), "WS9.s-tbench")
        self.assertIsNone(task_of("ws9.sx", "WS9", {}, ids))




class TaskmapEditTest(unittest.TestCase):
    def test_editing_taskmap_file_changes_result_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "taskmap.json"
            path.write_text(json.dumps({"WS5": {"/^alpha/": "WS5.A"}}), encoding="utf-8")
            old_env = os.environ.get("AGENT_DASH_TASKMAP")
            os.environ["AGENT_DASH_TASKMAP"] = str(path)
            try:
                first = sections._taskmap_cached()
                self.assertEqual(task_of("alpha-1", "WS5", first, ()), "WS5.A")
                path.write_text(json.dumps({"WS5": {"/^beta/": "WS5.B"}}), encoding="utf-8")
                second = sections._taskmap_cached()
                self.assertEqual(task_of("beta-1", "WS5", second, ()), "WS5.B")
                self.assertIsNone(task_of("alpha-1", "WS5", second, ()))
            finally:
                if old_env is None:
                    os.environ.pop("AGENT_DASH_TASKMAP", None)
                else:
                    os.environ["AGENT_DASH_TASKMAP"] = old_env




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
