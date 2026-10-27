"""Tests for scripts/launch_cmd.py: when an attempt counts as a usable report.

Synthetic results only: no model calls, no subprocesses.
"""
import importlib.util
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import tempfile
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

SPEC = importlib.util.spec_from_file_location("launch_cmd", ROOT / "scripts" / "launch_cmd.py")
launch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launch)

SUCCESS = {"type": "result", "subtype": "success"}


class TierTests(unittest.TestCase):
    def test_deepseek_models_map_to_deepseek_tier(self):
        self.assertEqual(launch.dash_tier("deepseek/deepseek-v4.1-flash"), "commandcode-deepseek")
        self.assertEqual(launch.dash_tier("deepseek/deepseek-v4-pro"), "commandcode-deepseek")

    def test_beam_model_maps_to_beam_tier(self):
        self.assertEqual(launch.dash_tier("reflection/beam:free"), "commandcode-beam")

    def test_other_models_fall_back_to_commandcode(self):
        self.assertEqual(launch.dash_tier("poolside/laguna-s-2.1-free"), "commandcode")
        self.assertEqual(launch.dash_tier(None), "commandcode")


class PickTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.outdir = os.path.join(self._tmp.name, "cmd")
        os.makedirs(self.outdir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_pick_is_the_deepseek_flash_build(self):
        model, _wait = launch.pick(self.outdir, "ws6-plan-pick")
        self.assertEqual(model, "deepseek/deepseek-v4.1-flash")

    def test_pin_model_skips_rotation(self):
        with unittest.mock.patch.dict(os.environ, {"CMD_PIN_MODEL": "reflection/beam:free"}):
            model, _wait = launch.pick(self.outdir, "ws6-plan-pin")
        self.assertEqual(model, "reflection/beam:free")

    def test_cooled_model_is_skipped(self):
        launch.cool(self.outdir, "deepseek/deepseek-v4.1-flash")
        model, _wait = launch.pick(self.outdir, "ws6-plan-cool")
        self.assertEqual(model, "deepseek/deepseek-v4-pro")

    def test_record_writes_the_model_tier_cell(self):
        launch.record(self.outdir, ["ws6-plan-rec", "deepseek/deepseek-v4.1-flash",
                                    "sid-1", 1792000000, 1792000100, 10, 2, "ok",
                                    launch.dash_tier("deepseek/deepseek-v4.1-flash")])
        text = open(os.path.join(self.outdir, "cmd-sessions.tsv"), encoding="utf-8").read()
        self.assertTrue(text.rstrip("\n").endswith("\tcommandcode-deepseek"))

    def test_main_registers_the_outdir(self):
        state = os.path.join(self._tmp.name, "state")
        with unittest.mock.patch.dict(os.environ, {"AGENT_DASH_STATE_DIR": state}):
            launch.register_run_dir(Path(self.outdir))
        registered = open(os.path.join(state, "run-dirs.txt"), encoding="utf-8").read()
        self.assertIn(self.outdir, registered)


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
