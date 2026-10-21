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
        source._fail_streak = 0
        source._backoff_s = 0.0
        source._next_attempt = None
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
        self.assertIsNone(source._next_attempt)

    def test_failure_backs_off_and_later_success_resets(self):
        self.sync.return_value = {"error": "HTTP 500"}
        self._poll()
        self.assertEqual(source._fail_streak, 1)
        self.assertEqual(source._backoff_s, float(source.INTERVAL_S))
        self.assertEqual(source.health_extra()["backoff_s"], float(source.INTERVAL_S))

        # Inside the window: no sync attempt.
        self.sync.reset_mock()
        self.clock[0] += 10
        self._poll()
        self.sync.assert_not_called()

        # Window passed and the next cycle succeeds: back-off clears.
        self.sync.reset_mock()
        self.sync.return_value = {"error": None}
        self.clock[0] += source.INTERVAL_S
        self._poll()
        self.assertEqual(self.sync.call_count, 2)
        self.assertEqual(source._fail_streak, 0)
        self.assertEqual(source._backoff_s, 0.0)
        self.assertIsNone(source._next_attempt)

    def test_backoff_doubles_and_respects_max(self):
        self.sync.return_value = {"error": "boom"}
        cfg = {"linear": {"max_backoff_s": 90}}
        self._poll(cfg)
        self.assertEqual(source._backoff_s, 60.0)
        self.clock[0] += 60
        self._poll(cfg)
        self.assertEqual(source._backoff_s, 90.0)

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
        self.assertEqual(set(extra), {"linked", "backoff_s", "workstreams"})
        self.assertEqual(extra["linked"], 1)
        self.assertIsInstance(extra["backoff_s"], float)
        self.assertEqual(
            extra["workstreams"],
            {"WS9": {"synced_at": 1500.0, "last_error": None, "outbox": 1}},
        )
        self.assertNotIn(SECRET, json.dumps(extra))
        self.assertNotIn("linear.app", json.dumps(extra))


if __name__ == "__main__":
    unittest.main()
