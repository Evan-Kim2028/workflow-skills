"""Tests for scripts/launch_cmd.py: when an attempt counts as a usable report.

Synthetic results only: no model calls, no subprocesses.
"""
import importlib.util
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

SPEC = importlib.util.spec_from_file_location("launch_cmd", ROOT / "scripts" / "launch_cmd.py")
launch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launch)

SUCCESS = {"type": "result", "subtype": "success"}


class AttemptSucceededTests(unittest.TestCase):
    def test_short_verdict_is_a_success(self):
        self.assertTrue(launch.attempt_succeeded(SUCCESS, "PASS — 10 schemas parse."))

    def test_one_word_verdict_is_a_success(self):
        self.assertTrue(launch.attempt_succeeded(SUCCESS, "PASS"))

    def test_long_report_mentioning_rate_limits_is_a_success(self):
        report = "PASS\n" + "The conventions add rate-limit headers and a 429 error. " * 10
        self.assertTrue(launch.attempt_succeeded(SUCCESS, report))

    def test_empty_text_is_a_failure(self):
        self.assertFalse(launch.attempt_succeeded(SUCCESS, "   \n"))

    def test_short_quota_notice_is_a_failure(self):
        self.assertFalse(launch.attempt_succeeded(SUCCESS, "You have hit today's limit. Resets in 2h 5m"))

    def test_non_success_subtype_is_a_failure(self):
        self.assertFalse(launch.attempt_succeeded({"subtype": "error_max_turns"}, "PASS"))

    def test_missing_result_is_a_failure(self):
        self.assertFalse(launch.attempt_succeeded(None, "PASS"))


if __name__ == "__main__":
    unittest.main()
