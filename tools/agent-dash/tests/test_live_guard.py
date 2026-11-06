"""The per-module live guard: isolates the env, refuses the live port or state dir."""

import os
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_guard import KEYS, LIVE_PORT, LIVE_STATE, Guard


def _clean_env():
    return {key: value for key, value in os.environ.items() if key not in KEYS}


class LiveGuardTest(unittest.TestCase):
    def test_start_isolates_every_key_and_stop_restores(self):
        with mock.patch.dict(os.environ, _clean_env(), clear=True):
            guard = Guard().start()
            try:
                state = Path(os.environ["AGENT_DASH_STATE_DIR"])
                self.assertFalse(state == LIVE_STATE or LIVE_STATE in state.parents)
                self.assertNotEqual(os.environ["AGENT_DASH_PORT"], str(LIVE_PORT))
                self.assertEqual(os.environ["AGENT_DASH_WORKSTREAMS"], str(Path(os.environ["AGENT_DASH_DATA_ROOT"]) / "workstreams"))
                tmp = guard._tmp
                self.assertTrue(os.path.isdir(tmp))
            finally:
                guard.stop()
            for key in KEYS:
                self.assertNotIn(key, os.environ)
            self.assertFalse(os.path.exists(tmp))

    def test_start_keeps_a_safe_value_the_module_already_set(self):
        custom = {"AGENT_DASH_STATE_DIR": "/tmp/custom-guard-state"}
        with mock.patch.dict(os.environ, dict(_clean_env(), **custom), clear=True):
            guard = Guard().start()
            try:
                self.assertEqual(os.environ["AGENT_DASH_STATE_DIR"], custom["AGENT_DASH_STATE_DIR"])
            finally:
                guard.stop()
            self.assertEqual(os.environ["AGENT_DASH_STATE_DIR"], custom["AGENT_DASH_STATE_DIR"])

    def test_refuses_the_live_port_and_restores_env(self):
        with mock.patch.dict(os.environ, dict(_clean_env(), AGENT_DASH_PORT=str(LIVE_PORT)), clear=True):
            with self.assertRaisesRegex(AssertionError, "live port"):
                Guard().start()
            self.assertEqual(os.environ["AGENT_DASH_PORT"], str(LIVE_PORT))

    def test_refuses_the_live_state_dir(self):
        live = str(LIVE_STATE)
        with mock.patch.dict(os.environ, dict(_clean_env(), AGENT_DASH_STATE_DIR=live), clear=True):
            with self.assertRaisesRegex(AssertionError, "live state dir"):
                Guard().start()
            self.assertEqual(os.environ["AGENT_DASH_STATE_DIR"], live)

    def test_refuses_a_state_dir_inside_the_live_one(self):
        inside = str(LIVE_STATE / "runs")
        with mock.patch.dict(os.environ, dict(_clean_env(), AGENT_DASH_STATE_DIR=inside), clear=True):
            with self.assertRaisesRegex(AssertionError, "live state dir"):
                Guard().start()

    def test_start_twice_is_an_error(self):
        with mock.patch.dict(os.environ, _clean_env(), clear=True):
            guard = Guard().start()
            try:
                with self.assertRaises(AssertionError):
                    guard.start()
            finally:
                guard.stop()


if __name__ == "__main__":
    unittest.main()
