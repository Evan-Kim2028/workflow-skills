"""Source cadence policy: interval floor, per-source override, idle mode, concurrency cap.

Pinned here: no source polls more often than ``poll_interval_floor_s``; a
``poll_intervals`` entry overrides a source by name (the floor still applies);
idle mode (no request or open stream for ``idle_after_s``) multiplies every
gap by ``idle_factor``; the first request after idle arms a wake that polls
every source at once without waiting; an open SSE stream blocks idle; at most
``max_concurrent_polls`` poll threads run at once; /api/health reports the new
per-source and top-level fields; every accepted connection counts as activity.
"""

from __future__ import annotations

import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import socketserver
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_dash  # noqa: E402
from dash.server import DashServer  # noqa: E402
from dash.sources import Registry, _set_next_due, effective_interval_s, max_concurrent_polls  # noqa: E402

T0 = 1_792_000_000.0


class _Source:
    """A fake source: records each poll start, optionally blocks inside the poll."""

    def __init__(self, name, interval, hook=None):
        self.NAME = name
        self.INTERVAL_S = interval
        self.calls = []
        self._hook = hook

    def poll(self, store, cfg, now):
        self.calls.append(now)
        if self._hook is not None:
            self._hook()


def _join_all(registry):
    for state in registry.state.values():
        thread = state.get("thread")
        if thread is not None:
            thread.join(5)


class FloorAndOverrideTests(unittest.TestCase):
    def test_floor_holds_back_a_fast_source(self):
        src = _Source("fast", 2)
        reg = Registry([src])
        cfg = {"poll_interval_floor_s": 15}
        reg.tick(None, cfg, T0)
        reg.tick(None, cfg, T0 + 5)
        self.assertEqual(src.calls, [T0])
        reg.tick(None, cfg, T0 + 15)
        self.assertEqual(src.calls, [T0, T0 + 15])

    def test_without_the_floor_key_the_declared_interval_holds(self):
        src = _Source("fast", 2)
        reg = Registry([src])
        reg.tick(None, {}, T0)
        reg.tick(None, {}, T0 + 2)
        self.assertEqual(src.calls, [T0, T0 + 2])

    def test_per_source_override_replaces_the_declared_interval(self):
        src = _Source("pi", 2)
        reg = Registry([src])
        cfg = {"poll_interval_floor_s": 15, "poll_intervals": {"pi": 30}}
        reg.tick(None, cfg, T0)
        reg.tick(None, cfg, T0 + 20)
        self.assertEqual(src.calls, [T0])
        reg.tick(None, cfg, T0 + 30)
        self.assertEqual(src.calls, [T0, T0 + 30])

    def test_override_below_the_floor_is_raised_to_the_floor(self):
        src = _Source("pi", 60)
        cfg = {"poll_interval_floor_s": 15, "poll_intervals": {"pi": 5}}
        self.assertEqual(effective_interval_s(src, cfg), 15.0)
        other = _Source("other", 60)
        self.assertEqual(effective_interval_s(other, cfg), 60.0)


class IdleModeTests(unittest.TestCase):
    IDLE_CFG = {"idle_after_s": 300, "idle_factor": 8}

    def test_idle_multiplies_the_gap_after_idle_after_s(self):
        src = _Source("fast", 2)
        reg = Registry([src])
        store = object()
        reg.note_activity(T0)
        reg.tick(store, self.IDLE_CFG, T0)
        self.assertEqual(src.calls, [T0])

        reg.tick(store, self.IDLE_CFG, T0 + 301)
        self.assertEqual(src.calls, [T0, T0 + 301])
        _join_all(reg)
        rows = reg.health(T0 + 301)["sources"]
        self.assertTrue(rows[0]["idle"])
        self.assertEqual(rows[0]["effective_interval_s"], 16.0)

        reg.tick(store, self.IDLE_CFG, T0 + 310)
        self.assertEqual(len(src.calls), 2)
        reg.tick(store, self.IDLE_CFG, T0 + 317)
        self.assertEqual(src.calls, [T0, T0 + 301, T0 + 317])

    def test_a_recent_request_keeps_the_normal_cadence(self):
        src = _Source("fast", 2)
        reg = Registry([src])
        reg.note_activity(T0)
        reg.tick(None, self.IDLE_CFG, T0)
        reg.note_activity(T0 + 200)
        reg.tick(None, self.IDLE_CFG, T0 + 202)
        self.assertEqual(src.calls, [T0, T0 + 202])
        self.assertFalse(reg.idle(T0 + 202))

    def test_first_request_after_idle_polls_every_source_at_once(self):
        slow = _Source("slow", 1000)
        other = _Source("other", 1000)
        reg = Registry([slow, other])
        reg.note_activity(T0)
        reg.tick(None, self.IDLE_CFG, T0)
        _join_all(reg)
        self.assertEqual(len(slow.calls) + len(other.calls), 2)

        reg.tick(None, self.IDLE_CFG, T0 + 400)
        _join_all(reg)
        self.assertEqual(len(slow.calls) + len(other.calls), 2)
        self.assertTrue(reg.idle(T0 + 400))

        reg.note_activity(T0 + 401)
        health = reg.health(T0 + 401)
        self.assertTrue(health["wake_pending"])
        reg.tick(None, self.IDLE_CFG, T0 + 402)
        _join_all(reg)
        self.assertEqual(len(slow.calls), 2)
        self.assertEqual(len(other.calls), 2)
        self.assertFalse(reg.health(T0 + 402)["wake_pending"])

    def test_wake_survives_a_cap_that_defers_a_source(self):
        gate = {"armed": False}
        release = threading.Event()

        def hold():
            if gate["armed"]:
                release.wait(5)

        first = _Source("first", 1000, hook=hold)
        second = _Source("second", 1000, hook=hold)
        reg = Registry([first, second])
        cfg = {"idle_after_s": 300, "idle_factor": 8, "max_concurrent_polls": 1, "poll_budget_s": 0.2}
        reg.note_activity(T0)
        reg.tick(None, cfg, T0)
        _join_all(reg)
        reg.tick(None, cfg, T0 + 400)
        _join_all(reg)
        self.assertEqual(len(first.calls) + len(second.calls), 2)

        reg.note_activity(T0 + 401)
        gate["armed"] = True
        reg.tick(None, cfg, T0 + 402)
        self.assertEqual(len(first.calls) + len(second.calls), 3)
        self.assertTrue(reg.health(T0 + 402)["wake_pending"])
        release.set()
        _join_all(reg)
        reg.tick(None, cfg, T0 + 403)
        _join_all(reg)
        self.assertEqual(len(first.calls) + len(second.calls), 4)
        self.assertFalse(reg.health(T0 + 403)["wake_pending"])

    def test_open_stream_blocks_idle_until_it_closes(self):
        src = _Source("fast", 2)
        reg = Registry([src])
        reg.note_activity(T0)
        reg.tick(None, self.IDLE_CFG, T0)
        reg.stream_opened(T0 + 1)
        self.assertFalse(reg.idle(T0 + 1000))
        reg.stream_closed(T0 + 1000)
        self.assertFalse(reg.idle(T0 + 1000))
        self.assertTrue(reg.idle(T0 + 1300))

    def test_idle_mode_is_off_without_the_key(self):
        src = _Source("fast", 2)
        reg = Registry([src])
        reg.note_activity(T0)
        reg.tick(None, {}, T0)
        self.assertFalse(reg.idle(T0 + 10_000))


class ConcurrencyCapTests(unittest.TestCase):
    def test_at_most_max_concurrent_polls_run_at_once(self):
        release = threading.Event()
        sources = [_Source(f"s{i}", 2, hook=lambda: release.wait(5)) for i in range(5)]
        reg = Registry(sources)
        cfg = {"max_concurrent_polls": 3, "poll_budget_s": 0.2}
        reg.tick(None, cfg, T0)
        self.assertEqual(sum(len(s.calls) for s in sources), 3)
        self.assertEqual(reg.health(T0)["polls_running"], 3)
        self.assertEqual(reg.health(T0)["max_concurrent_polls"], 3)

        release.set()
        _join_all(reg)
        for step in range(1, 4):
            reg.tick(None, cfg, T0 + 10 * step)
            _join_all(reg)
        self.assertTrue(all(s.calls for s in sources))

    def test_slots_go_to_the_source_polled_longest_ago(self):
        release = threading.Event()
        stale = _Source("stale", 2, hook=lambda: release.wait(5))
        recent = _Source("recent", 2)
        reg = Registry([recent, stale])
        reg.state["stale"]["last_poll"] = T0 - 100
        reg.state["recent"]["last_poll"] = T0 - 50
        cfg = {"max_concurrent_polls": 1, "poll_budget_s": 0.2}
        reg.tick(None, cfg, T0)
        self.assertEqual(stale.calls, [T0])
        self.assertEqual(recent.calls, [])
        release.set()
        _join_all(reg)

    def test_a_running_poll_is_never_started_twice(self):
        release = threading.Event()
        src = _Source("slow", 2, hook=lambda: release.wait(5))
        reg = Registry([src])
        cfg = {"poll_budget_s": 0.2}
        reg.tick(None, cfg, T0)
        reg.tick(None, cfg, T0 + 10)
        self.assertEqual(len(src.calls), 1)
        release.set()
        _join_all(reg)


class HealthFieldTests(unittest.TestCase):
    def test_health_reports_effective_interval_idle_and_duration(self):
        src = _Source("fast", 2)
        reg = Registry([src])
        reg._born = T0 - 3600
        cfg = {"poll_interval_floor_s": 15, "idle_after_s": 300, "idle_factor": 8, "max_concurrent_polls": 3}
        reg.note_activity(T0)
        reg.tick(None, cfg, T0)
        _join_all(reg)
        body = reg.health(T0 + 1)
        row = body["sources"][0]
        self.assertEqual(row["interval_s"], 2)
        self.assertEqual(row["effective_interval_s"], 15.0)
        self.assertFalse(row["idle"])
        self.assertIsNotNone(row["last_duration_s"])
        self.assertFalse(body["idle_mode"])
        self.assertEqual(body["polls_running"], 0)
        self.assertEqual(body["max_concurrent_polls"], 3)
        self.assertTrue(reg.health(T0 + 400)["idle_mode"])


class DutyFactorTests(unittest.TestCase):
    def _gap(self, cfg, duration, mult=1.0, interval=2):
        src = _Source("slow", interval)
        state = Registry([src]).state["slow"]
        _set_next_due(src, state, T0, duration, cfg, mult)
        return state["next_due"] - T0

    def test_gap_is_factor_times_the_last_duration(self):
        self.assertEqual(self._gap({"poll_duty_factor": 8}, 10.0), 80.0)

    def test_duty_is_one_over_the_factor(self):
        for factor in (2, 4, 8):
            gap = self._gap({"poll_duty_factor": factor}, 10.0)
            self.assertAlmostEqual(10.0 / gap, 1.0 / factor)

    def test_factor_2_and_the_absent_key_restore_the_old_gap(self):
        self.assertEqual(self._gap({"poll_duty_factor": 2}, 10.0), 20.0)
        self.assertEqual(self._gap({}, 10.0), 20.0)

    def test_factor_0_leaves_only_the_interval(self):
        self.assertEqual(self._gap({"poll_duty_factor": 0}, 10.0), 2.0)

    def test_floor_still_applies_under_the_factor(self):
        self.assertEqual(self._gap({"poll_duty_factor": 8, "poll_interval_floor_s": 15}, 1.0), 15.0)

    def test_idle_multiplier_applies_on_top_of_the_factor(self):
        self.assertEqual(self._gap({"poll_duty_factor": 8}, 10.0, mult=8.0), 640.0)

    def test_signal_driven_sources_do_not_read_the_duration(self):
        src = _Source("sig", 2)
        src.signal_paths = lambda cfg: []
        state = Registry([src]).state["sig"]
        _set_next_due(src, state, T0, 10.0, {"poll_duty_factor": 8}, 1.0)
        self.assertIsNone(state.get("next_due"))

    def test_health_reports_the_factor_and_time_to_next_start(self):
        src = _Source("fast", 2)
        reg = Registry([src])
        reg._born = T0 - 3600
        cfg = {"poll_duty_factor": 8, "poll_interval_floor_s": 15}
        reg.tick(None, cfg, T0)
        _join_all(reg)
        body = reg.health(T0 + 5)
        self.assertEqual(body["duty_factor"], 8)
        self.assertAlmostEqual(body["sources"][0]["next_due_in_s"], 10.0, places=2)
        self.assertEqual(reg.health(T0 + 100)["sources"][0]["next_due_in_s"], 0.0)

    def test_production_defaults_are_factor_8_and_two_slots(self):
        cfg = agent_dash.load_cfg()
        self.assertEqual(cfg["poll_duty_factor"], 8)
        self.assertEqual(cfg["max_concurrent_polls"], 2)
        self.assertIsNone(max_concurrent_polls({}))


class RequestActivityTests(unittest.TestCase):
    def test_every_accepted_connection_is_recorded_as_activity(self):
        reg = Registry([])
        server = DashServer(None, reg, "127.0.0.1", 0, cfg={})
        try:
            with mock.patch.object(socketserver.ThreadingMixIn, "process_request") as base:
                with mock.patch.object(reg, "note_activity") as note:
                    server.process_request("sock", ("127.0.0.1", 1))
                    note.assert_called_once()
                base.assert_called_once_with("sock", ("127.0.0.1", 1))
        finally:
            server.server_close()

    def test_a_registry_without_the_hook_is_still_served(self):
        class Bare:
            state = {}
            sources = []

        server = DashServer(None, Bare(), "127.0.0.1", 0, cfg={})
        try:
            with mock.patch.object(socketserver.ThreadingMixIn, "process_request") as base:
                server.process_request("sock", ("127.0.0.1", 1))
                base.assert_called_once()
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
