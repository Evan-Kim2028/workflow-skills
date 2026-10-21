"""Linear poller source: read-only and key gates, per-bundle sync, back-off, budget, health shape."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import linear  # noqa: E402
from dash.sources import linear as source  # noqa: E402

SECRET = "lin_api_secret_value_for_tests"


class LinearSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wsroot = self.root / "ws"
        self.wsroot.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {
                "LINEAR_API_KEY": SECRET,
                "AGENT_DASH_WORKSTREAMS": str(self.wsroot),
                "AGENT_DASH_DATA_ROOT": str(self.root),
            },
        )
        self.env.start()
        os.environ.pop("AGENT_DASH_READONLY", None)
        source._fail_streak.clear()
        source._backoff.clear()
        source._next_attempt.clear()
        source._rate_limit_until = None
        self.clock = [1000.0]
        self.clock_patch = mock.patch.object(source, "_clock", lambda: self.clock[0])
        self.clock_patch.start()
        self.linked_patch = mock.patch.object(source, "_linked", return_value=["WS9", "WS10"])
        self.linked = self.linked_patch.start()
        self.sync_patch = mock.patch.object(source, "_sync", return_value={"pushed": 1, "flushed": 0, "pulled": 0, "applied": 0, "error": None})
        self.sync = self.sync_patch.start()

    def tearDown(self):
        for patcher in (self.sync_patch, self.linked_patch, self.clock_patch, self.env):
            patcher.stop()
        self.tmp.cleanup()

    def _poll(self, cfg=None):
        source.poll(None, cfg if cfg is not None else {}, self.clock[0])

    def test_readonly_env_skips_every_sync(self):
        with mock.patch.dict(os.environ, {"AGENT_DASH_READONLY": "1"}):
            self._poll()
        self.sync.assert_not_called()

    def test_readonly_config_skips_every_sync(self):
        self._poll({"readonly": True})
        self.sync.assert_not_called()

    def test_missing_key_skips_every_sync(self):
        with mock.patch.dict(os.environ, {"LINEAR_API_KEY": ""}):
            self._poll()
        self.sync.assert_not_called()
        self.linked.assert_not_called()

    def test_no_linked_bundles_skips_every_sync(self):
        self.linked.return_value = []
        self._poll()
        self.sync.assert_not_called()

    def test_each_linked_bundle_is_synced(self):
        self._poll()
        self.assertEqual([c.args[0] for c in self.sync.call_args_list], ["WS9", "WS10"])
        self.assertEqual(source._next_attempt, {})

    def _results(self, results):
        """Route each bundle to its own result dict; unlisted bundles succeed."""
        ok = {"pushed": 0, "flushed": 0, "pulled": 0, "applied": 0, "error": None}
        self.sync.side_effect = lambda ws: results.get(ws, ok)

    def test_failure_backs_off_and_later_success_resets(self):
        self._results({"WS9": {"error": "HTTP 500"}})
        self._poll()
        self.assertEqual(source._fail_streak, {"WS9": 1})
        self.assertEqual(source._backoff, {"WS9": float(source.INTERVAL_S)})
        with mock.patch.object(source, "_linked_on_disk", return_value=["WS9", "WS10"]):
            extra = source.health_extra()
        self.assertEqual(extra["workstreams"]["WS9"]["backoff_s"], float(source.INTERVAL_S))
        self.assertEqual(extra["workstreams"]["WS10"]["backoff_s"], 0.0)

        # Inside the window: WS9 is not attempted again.
        self.sync.reset_mock()
        self.clock[0] += 10
        self._poll()
        self.assertEqual([c.args[0] for c in self.sync.call_args_list], ["WS10"])

        # Window passed and WS9 succeeds: its back-off clears.
        self.sync.reset_mock()
        self._results({})
        self.clock[0] += source.INTERVAL_S
        self._poll()
        self.assertEqual(self.sync.call_count, 2)
        self.assertEqual(source._fail_streak, {})
        self.assertEqual(source._backoff, {})
        self.assertEqual(source._next_attempt, {})

    def test_failing_bundle_does_not_delay_healthy_bundle(self):
        self._results({"WS9": {"error": "HTTP 500"}})
        self._poll()
        self.sync.reset_mock()
        self.clock[0] += 10
        self._poll()
        self.assertEqual([c.args[0] for c in self.sync.call_args_list], ["WS10"])
        self.assertNotIn("WS10", source._next_attempt)

    def test_backoff_doubles_and_respects_max_per_bundle(self):
        self._results({"WS9": {"error": "boom"}})
        cfg = {"linear": {"max_backoff_s": 90}}
        self._poll(cfg)
        self.assertEqual(source._backoff["WS9"], 60.0)
        self.clock[0] += 60
        self._poll(cfg)
        self.assertEqual(source._backoff["WS9"], 90.0)
        self.assertNotIn("WS10", source._backoff)

    def test_ratelimited_stops_the_loop_and_pauses_every_bundle(self):
        self._results({"WS9": {"error": "RATELIMITED: slow down"}})
        self._poll()
        self.assertEqual([c.args[0] for c in self.sync.call_args_list], ["WS9"])
        self.assertEqual(source._rate_limit_until, 1000.0 + 300.0)
        self.assertEqual(source._fail_streak, {})
        self.assertEqual(source.health_extra()["rate_limit_pause_s"], 300.0)

        self.sync.reset_mock()
        self.clock[0] += 100
        self._poll()
        self.sync.assert_not_called()

        self.clock[0] += 201
        self._results({})
        self.sync.reset_mock()
        self._poll()
        self.assertEqual([c.args[0] for c in self.sync.call_args_list], ["WS9", "WS10"])
        self.assertIsNone(source._rate_limit_until)

    def test_ratelimit_pause_uses_reset_time_when_result_has_one(self):
        import time as real_time

        self._results({"WS9": {"error": "RATELIMITED: slow down", "reset_at": real_time.time() + 120}})
        self._poll()
        self.assertAlmostEqual(source._rate_limit_until - self.clock[0], 120.0, delta=5.0)

    def test_poll_error_redacts_the_api_key(self):
        self.linked.side_effect = RuntimeError(f"bad request with {SECRET} inside")
        self._poll()
        self.assertIsNotNone(source.last_error)
        self.assertNotIn(SECRET, source.last_error)
        self.assertIn("[redacted]", source.last_error)

    def test_sync_error_text_is_redacted_in_health(self):
        self._results({"WS9": {"error": f"AUTH: key {SECRET} rejected"}})
        self._poll()
        self.assertNotIn(SECRET, json.dumps(source.health_extra()))

    def test_budget_stops_before_the_next_bundle(self):
        self.linked.return_value = ["WS1", "WS2", "WS3"]

        def slow_sync(ws):
            self.clock[0] += 10
            return {"error": None}

        self.sync.side_effect = slow_sync
        self._poll({"poll_budget_s": 15})
        self.assertEqual([c.args[0] for c in self.sync.call_args_list], ["WS1"])

    def test_health_extra_shape_with_linked_bundle(self):
        from dash.bundles import cmd_ws

        with redirect_stdout(io.StringIO()):
            self.assertEqual(cmd_ws(["new", "WS9", "Example"]), 0)
        linear.write_link(
            "WS9",
            {
                "project": {"id": "p1", "url": "https://linear.app/x/project/ex", "name": "Pipeline"},
                "synced_at": 1500.0,
                "last_error": None,
            },
        )
        linear.enqueue("WS9", "task", "WS9.1", now=1400.0)
        extra = source.health_extra()
        self.assertEqual(set(extra), {"linked", "rate_limit_pause_s", "workstreams"})
        self.assertEqual(extra["linked"], 1)
        self.assertIsInstance(extra["rate_limit_pause_s"], float)
        self.assertEqual(
            extra["workstreams"],
            {"WS9": {"synced_at": 1500.0, "last_error": None, "outbox": 1, "backoff_s": 0.0}},
        )
        self.assertNotIn(SECRET, json.dumps(extra))
        self.assertNotIn("linear.app", json.dumps(extra))


if __name__ == "__main__":
    unittest.main()
