"""Laptop headroom: sampler parsing, the CPU EWMA, sampling cadence, persisted state and the bars."""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentq import headroom as H  # noqa: E402

PROC_STAT = """cpu  4000 100 2000 30000 500 40 10 20 0 0
cpu0 1000 25 500 7500 120 10 2 5 0 0
intr 12345 0 0
"""

MEMINFO = """MemTotal:       50331648 kB
MemFree:          812344 kB
MemAvailable:   22020096 kB
SwapTotal:       8388608 kB
SwapFree:        1048576 kB
"""

SWAPUSAGE = "total = 8192.00M  used = 6920.44M  free = 1271.56M  (encrypted)\n"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class ProcStatTests(unittest.TestCase):
    def test_busy_excludes_idle_and_iowait_and_guest_is_not_double_counted(self):
        busy, total = H.parse_proc_stat(PROC_STAT)
        self.assertEqual(total, 4000 + 100 + 2000 + 30000 + 500 + 40 + 10 + 20)
        self.assertEqual(busy, total - 30000 - 500)

    def test_missing_aggregate_line_is_unreadable(self):
        self.assertIsNone(H.parse_proc_stat("cpu0 1 2 3 4\n"))

    def test_darwin_array_sums_four_states_per_cpu(self):
        values = [10, 5, 80, 5, 20, 10, 60, 10]
        self.assertEqual(H.darwin_ticks(values), (10 + 5 + 5 + 20 + 10 + 10, 100 + 100))


class SwapTests(unittest.TestCase):
    def test_swapusage_used_fraction(self):
        self.assertAlmostEqual(H.parse_swapusage(SWAPUSAGE), 6920.44 / 8192.0)

    def test_gigabyte_units_convert(self):
        self.assertAlmostEqual(H.parse_swapusage("total = 2.00G  used = 1.00G  free = 1.00G"), 0.5)

    def test_no_configured_swap_is_zero_and_garbage_is_unknown(self):
        self.assertEqual(H.parse_swapusage("total = 0.00M  used = 0.00M  free = 0.00M"), 0.0)
        self.assertIsNone(H.parse_swapusage("nothing useful"))

    def test_meminfo_swap_and_memory(self):
        fields = H.parse_meminfo(MEMINFO)
        self.assertAlmostEqual(H.swap_frac_from_meminfo(fields), (8388608 - 1048576) / 8388608)
        self.assertAlmostEqual(H.memory_free_from_meminfo(fields), 100.0 * 22020096 / 50331648)
        self.assertIsNone(H.memory_free_from_meminfo({}))


class CpuFractionAndSmoothingTests(unittest.TestCase):
    def test_fraction_is_the_busy_share_of_the_interval(self):
        self.assertAlmostEqual(H.cpu_fraction((100, 1000), (400, 1600)), 0.5)

    def test_no_baseline_or_no_elapsed_ticks_is_unknown(self):
        self.assertIsNone(H.cpu_fraction(None, (1, 2)))
        self.assertIsNone(H.cpu_fraction((1, 2), None))
        self.assertIsNone(H.cpu_fraction((5, 10), (5, 10)))

    def test_counter_reset_restarts_the_baseline(self):
        self.assertIsNone(H.cpu_fraction((500, 1000), (100, 1200)))

    def test_smoothing_starts_at_the_first_value(self):
        self.assertEqual(H.smooth(None, None, 0.7, 1000.0, 60.0), 0.7)

    def test_smoothing_moves_by_one_minus_exp_of_elapsed_over_tau(self):
        value = H.smooth(0.0, 1000.0, 1.0, 1060.0, 60.0)
        self.assertAlmostEqual(value, 1.0 - math.exp(-1.0))

    def test_same_instant_keeps_the_previous_value(self):
        self.assertAlmostEqual(H.smooth(0.4, 1000.0, 0.9, 1000.0, 60.0), 0.4)


class PressureTests(unittest.TestCase):
    BARS = dict(max_cpu_busy=0.80, min_free_pct=35.0, max_swap_frac=0.90)

    def test_clear_reading_holds_nothing(self):
        self.assertIsNone(H.pressure(H.Headroom(60.0, 0.5, 0.2, 9.0), **self.BARS))

    def test_each_bar_names_its_measure_with_the_live_number(self):
        self.assertEqual(H.pressure(H.Headroom(60.0, 0.84, 0.2, None), **self.BARS),
                         ("cpu busy >= 80%", "cpu 84% busy >= 80%"))
        self.assertEqual(H.pressure(H.Headroom(31.0, 0.1, 0.2, None), **self.BARS),
                         ("memory free < 35%", "memory free 31% < 35%"))
        self.assertEqual(H.pressure(H.Headroom(60.0, 0.1, 0.92, None), **self.BARS),
                         ("swap used >= 90%", "swap 92% used >= 90%"))

    def test_bar_is_inclusive_for_cpu_and_swap_and_strict_for_memory(self):
        self.assertIsNotNone(H.pressure(H.Headroom(60.0, 0.80, 0.0, None), **self.BARS))
        self.assertIsNotNone(H.pressure(H.Headroom(60.0, 0.0, 0.90, None), **self.BARS))
        self.assertIsNone(H.pressure(H.Headroom(35.0, 0.0, 0.0, None), **self.BARS))

    def test_unreadable_measures_never_hold(self):
        self.assertIsNone(H.pressure(H.Headroom(None, None, None, 99.0), **self.BARS))

    def test_first_breach_in_cpu_memory_swap_order_wins(self):
        breach = H.pressure(H.Headroom(10.0, 0.99, 0.99, None), **self.BARS)
        self.assertEqual(breach[0], "cpu busy >= 80%")


class GaugesTests(unittest.TestCase):
    def gauges(self, ticks, clock, *, home=None, memory=42.0, swap=0.25, sample_s=10.0, tau_s=60.0):
        feed = iter(ticks)
        return H.Gauges(home, settings={"memory_ttl_s": 15.0, "sample_s": sample_s, "tau_s": tau_s},
                        memory=lambda: memory, ticks=lambda: next(feed), swap=lambda: swap,
                        loadavg=lambda: (30.0, 0.0, 0.0), cpus=lambda: 15, clock=clock)

    def test_first_read_has_no_cpu_busy_yet_then_one_sample_per_interval(self):
        clock = Clock(1000.0)
        gauges = self.gauges([(0, 100), (50, 200), (60, 300)], clock)
        first = gauges.read()
        self.assertIsNone(first.cpu_busy)
        self.assertEqual((first.free_pct, first.swap_frac), (42.0, 0.25))
        self.assertAlmostEqual(first.load_per_cpu, 2.0)
        clock.t = 1005.0
        self.assertIsNone(gauges.read().cpu_busy)
        clock.t = 1010.0
        self.assertAlmostEqual(gauges.read().cpu_busy, 0.5)

    def test_cpu_busy_is_smoothed_across_samples(self):
        clock = Clock(0.0)
        ticks = [(0, 100), (100, 200), (100, 300)]
        gauges = self.gauges(ticks, clock)
        gauges.read()
        clock.t = 10.0
        first = gauges.read().cpu_busy
        self.assertAlmostEqual(first, 1.0)
        clock.t = 20.0
        second = gauges.read().cpu_busy
        alpha = 1.0 - math.exp(-10.0 / 60.0)
        self.assertAlmostEqual(second, first + alpha * (0.0 - first))

    def test_state_persists_under_home_and_a_new_reader_continues_from_it(self):
        clock = Clock(0.0)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            writer = self.gauges([(0, 100), (50, 200)], clock, home=home)
            writer.read()
            clock.t = 10.0
            writer_reading = writer.read()
            saved = json.loads((home / H.GAUGE_FILE).read_text(encoding="utf-8"))
            self.assertEqual(saved["ticks"], [50, 200])
            self.assertAlmostEqual(saved["cpu_busy"], writer_reading.cpu_busy)

            reader = self.gauges([(100, 300)], Clock(20.0), home=home)
            self.assertAlmostEqual(reader.read().cpu_busy, writer_reading.cpu_busy + (1.0 - math.exp(-10 / 60.0)) * (0.5 - writer_reading.cpu_busy))

    def test_reading_from_state_drops_each_measure_past_its_until_time(self):
        state = {"free": 40.0, "free_until": 1005.0, "cpu_busy": 0.7, "cpu_until": 900.0,
                 "swap": 0.3, "swap_until": 1000.0}
        self.assertEqual(H.reading_from_state(state, 1000.0), H.Headroom(40.0, None, None, None))

    def test_settings_come_from_the_broker_config_shape(self):
        cfg = {"adaptive": {"gauge_ttl_s": 15.0}, "laptop": {"sample_s": 5.0, "ewma_s": 30.0}}
        self.assertEqual(H.settings(cfg), {"memory_ttl_s": 15.0, "sample_s": 5.0, "tau_s": 30.0})

    def test_unknown_platform_reads_no_cpu_or_swap(self):
        self.assertIsNone(H.read_cpu_ticks("freebsd14"))
        self.assertIsNone(H.read_swap_frac(platform="freebsd14"))
        self.assertIsNone(H.memory_free_pct(platform="freebsd14"))


SETTINGS = {"memory_ttl_s": 15.0, "sample_s": 10.0, "tau_s": 60.0}


def gauges_for(home, clock, *, ticks, memory=lambda: 50.0, swap=lambda: 0.1):
    return H.Gauges(home, settings=SETTINGS, memory=memory, ticks=ticks, swap=swap,
                    loadavg=lambda: (0.0, 0.0, 0.0), cpus=lambda: 15, clock=clock)


class LockedSamplingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.state_path = self.home / H.GAUGE_FILE

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_reader_whose_clock_is_older_never_overwrites_a_newer_sample(self):
        Clock11 = Clock(11.0)
        gauges_for(self.home, Clock11, ticks=lambda: (0, 100)).read()
        before = self.state_path.read_text()
        calls = []
        older = gauges_for(self.home, Clock(10.0), ticks=lambda: calls.append(1) or (5, 100),
                           memory=lambda: calls.append(2) or 1.0, swap=lambda: calls.append(3) or 0.9)
        older.read()
        self.assertEqual(calls, [])
        self.assertEqual(self.state_path.read_text(), before)

    def test_a_busy_lock_returns_the_saved_state_without_sampling_or_writing(self):
        gauges_for(self.home, Clock(0.0), ticks=lambda: (0, 100)).read()
        before = self.state_path.read_text()
        held = H._try_lock(self.home)
        self.assertIsNotNone(held)
        try:
            calls = []
            reading = gauges_for(self.home, Clock(40.0), ticks=lambda: calls.append(1) or (9, 100),
                                 memory=lambda: calls.append(2) or 1.0).read()
        finally:
            os.close(held)
        self.assertEqual(calls, [])
        self.assertEqual(self.state_path.read_text(), before)
        self.assertIsNone(reading.free_pct)

    def test_two_concurrent_readers_the_newer_sample_survives(self):
        entered, release = threading.Event(), threading.Event()

        def slow_memory():
            entered.set()
            release.wait(5)
            return 40.0

        newer = gauges_for(self.home, Clock(11.0), ticks=lambda: (0, 100), memory=slow_memory, swap=lambda: 0.1)
        older = gauges_for(self.home, Clock(10.0), ticks=lambda: (5, 100), memory=lambda: 99.0, swap=lambda: 0.9)
        worker = threading.Thread(target=newer.read)
        worker.start()
        self.assertTrue(entered.wait(5))
        older_reading = older.read()
        self.assertIsNone(older_reading.swap_frac)
        release.set()
        worker.join(5)
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual((saved["free"], saved["swap"]), (40.0, 0.1))
        older.read()
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))["swap"], 0.1)

    def test_a_failed_cpu_sample_does_not_refresh_an_old_high_reading(self):
        readings = iter([(0, 100), (90, 200), None, None, None])
        clock = Clock(0.0)
        gauges = gauges_for(self.home, clock, ticks=lambda: next(readings))
        gauges.read()
        clock.t = 10.0
        self.assertAlmostEqual(gauges.read().cpu_busy, 0.9)
        clock.t = 20.0
        self.assertAlmostEqual(gauges.read().cpu_busy, 0.9)
        clock.t = 30.0
        gauges.read()
        clock.t = 45.0
        reading = gauges.read()
        self.assertIsNone(reading.cpu_busy)
        self.assertIsNone(H.pressure(reading, max_cpu_busy=0.80, min_free_pct=35.0, max_swap_frac=0.9))
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["cpu_at"], 10.0)
        self.assertEqual(saved["cpu_until"], 40.0)


class SysctlPathTests(unittest.TestCase):
    def test_absolute_path_is_used_on_macos(self):
        with mock.patch.object(H.os.path, "exists", lambda path: path == H.SYSCTL_DARWIN):
            self.assertEqual(H.sysctl_path(), H.SYSCTL_DARWIN)

    def test_falls_back_to_a_path_lookup_elsewhere(self):
        with mock.patch.object(H.os.path, "exists", lambda path: False), \
                mock.patch.object(H.shutil, "which", lambda name: "/opt/bin/sysctl"):
            self.assertEqual(H.sysctl_path(), "/opt/bin/sysctl")


@unittest.skipUnless(sys.platform == "darwin", "runs the macOS sysctl")
class LiveSwapUnderRestrictedPathTests(unittest.TestCase):
    def test_swap_is_read_under_a_path_without_usr_sbin(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            swap = H.read_swap_frac()
        self.assertIsInstance(swap, float)
        self.assertGreaterEqual(swap, 0.0)
        self.assertLessEqual(swap, 1.0)


@unittest.skipUnless(sys.platform == "darwin", "reads the macOS host_processor_info counters")
class LiveDarwinTests(unittest.TestCase):
    def test_live_ticks_are_cumulative_and_busy_is_bounded(self):
        first = H.read_darwin_ticks()
        self.assertIsNotNone(first)
        busy, total = first
        self.assertGreater(total, 0)
        self.assertLessEqual(busy, total)


if __name__ == "__main__":
    unittest.main()
