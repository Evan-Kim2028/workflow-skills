"""Run-to-task matching: compile-once regex rules and live taskmap edits.

Pins the task-map hot path that the dashboard hits per run: a taskmap
with many ``/regex/`` rules compiles each rule at most once across many
``task_of`` calls (the process-wide ``re`` cache holds 512 patterns, so
per-call compiles thrashed it), first-match order and case handling are
unchanged, and an edited taskmap file takes effect without a restart.
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import json
import re
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dash import sections  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
