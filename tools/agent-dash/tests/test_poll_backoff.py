"""Dormant backoff for idle sources (stage 5).

A source that declares ``signal_paths`` and found nothing new for
``BACKOFF_AFTER`` polls stops polling at its interval: the gap doubles up to
``BACKOFF_MAX_S``. A stat-only wake check polls at once when a signal
changes, and the gap cap is a fallback poll. Sources without signal paths
keep their cadence. Pinned here: wake, fallback, reset on a write, busy,
the stall limit, and a store without a write counter.
"""

from __future__ import annotations

import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import sources as S  # noqa: E402
from dash.sources import Registry  # noqa: E402

T0 = 1_792_000_000.0


class CountingStore:
    """Stands in for Store: ``write_count`` moves only on a real write."""

    def __init__(self):
        self.n = 0
        self.path = "mem"

    def write_count(self):
        return self.n


def make_source(name, path, *, writes=None, busy=False, interval=2):
    """A fake source whose poll writes one row when ``writes`` says so."""
    mod = types.SimpleNamespace()
    mod.NAME = name
    mod.INTERVAL_S = interval
    mod.calls = []
    mod.last_active = busy
    state = {"writes": writes}

    def poll(store, cfg, now):
        mod.calls.append(now)
        if state["writes"] and state["writes"](len(mod.calls)):
            store.n += 1

    mod.poll = poll
    if path is not None:
        mod.signal_paths = lambda cfg: [path]
    return mod


def run_ticks(reg, store, cfg, start, end, step=0.5):
    now = start
    while now <= end + 1e-9:
        reg.tick(store, cfg, now)
        now += step


class BackoffRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sig = os.path.join(self.tmp.name, "root")
        Path(self.sig).mkdir()
        self.cfg = {"poll_budget_s": 5}

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_without_signal_paths_keeps_its_cadence(self):
        src = make_source("plain", None)
        reg = Registry([src])
        run_ticks(reg, CountingStore(), self.cfg, T0, T0 + 30)
        self.assertEqual(len(src.calls), 16)  # every 2 s, nothing else
        self.assertEqual(reg.state["plain"]["idle"], 0)

    def test_idle_source_backs_off_and_polls_less(self):
        src = make_source("idle", self.sig, writes=lambda k: False)
        reg = Registry([src])
        store = CountingStore()
        run_ticks(reg, store, self.cfg, T0, T0 + 120)
        # Base cadence for 3 empty polls, then the gap doubles: 4, 8, 16 s.
        self.assertLess(len(src.calls), 20)
        gaps = [b - a for a, b in zip(src.calls, src.calls[1:])]
        self.assertEqual(gaps[:5], [2.0, 2.0, 4.0, 8.0, 16.0])

    def test_gap_is_capped_at_backoff_max(self):
        src = make_source("capped", self.sig, writes=lambda k: False)
        reg = Registry([src])
        run_ticks(reg, CountingStore(), self.cfg, T0, T0 + 1200, step=1.0)
        gaps = [b - a for a, b in zip(src.calls, src.calls[1:])]
        self.assertEqual(max(gaps), S.BACKOFF_MAX_S)

    def test_signal_change_wakes_a_dormant_source_within_one_check(self):
        src = make_source("wake", self.sig, writes=lambda k: False)
        reg = Registry([src])
        store = CountingStore()
        run_ticks(reg, store, self.cfg, T0, T0 + 200)
        before = len(src.calls)
        # Touch the root after the source went dormant (adds an entry).
        Path(self.sig, "new-entry").write_text("x")
        touched = T0 + 200.5
        run_ticks(reg, store, self.cfg, touched, touched + 10)
        woke = [t for t in src.calls[before:]]
        self.assertTrue(woke, "dormant source did not wake on a signal change")
        self.assertLessEqual(woke[0] - touched, 2.0 + 0.5)

    def test_run_started_while_dormant_is_seen_within_one_check(self):
        """A new run folder (a changed signal) is polled at once, not at the gap."""
        src = make_source("late", self.sig, writes=lambda k: False)
        reg = Registry([src])
        store = CountingStore()
        run_ticks(reg, store, self.cfg, T0, T0 + 300)
        self.assertGreater(reg.state["late"]["idle"], S.BACKOFF_AFTER)
        Path(self.sig, "run-folder").mkdir()
        t = T0 + 300.5
        run_ticks(reg, store, self.cfg, t, t + 6)
        self.assertTrue(any(c >= t and c - t <= 2.5 for c in src.calls), src.calls[-3:])

    def test_fallback_poll_runs_at_the_gap_cap_without_any_signal(self):
        src = make_source("quiet", self.sig, writes=lambda k: False)
        reg = Registry([src])
        store = CountingStore()
        run_ticks(reg, store, self.cfg, T0, T0 + 900, step=1.0)
        self.assertLessEqual(max(b - a for a, b in zip(src.calls, src.calls[1:])), S.BACKOFF_MAX_S + 1)

    def test_a_write_resets_the_backoff(self):
        src = make_source("reset", self.sig, writes=lambda k: k <= 10)
        reg = Registry([src])
        store = CountingStore()
        run_ticks(reg, store, self.cfg, T0, T0 + 60)
        # Polls 1-9 wrote nothing and 10 wrote a row, so idle counts the
        # empty polls after poll 10 only.
        self.assertEqual(reg.state["reset"]["idle"], len(src.calls) - 10)

    def test_busy_source_never_backs_off(self):
        src = make_source("busy", self.sig, writes=lambda k: False, busy=True)
        reg = Registry([src])
        run_ticks(reg, CountingStore(), self.cfg, T0, T0 + 60)
        self.assertEqual(len(src.calls), 31)  # 0, 2, ..., 60 s
        self.assertEqual(reg.state["busy"]["idle"], 0)

    def test_store_without_write_counter_never_backs_off(self):
        src = make_source("nocount", self.sig, writes=lambda k: False)
        reg = Registry([src])
        plain = types.SimpleNamespace(path="mem")  # no write_count attribute
        run_ticks(reg, plain, self.cfg, T0, T0 + 60)
        self.assertEqual(len(src.calls), 31)  # 0, 2, ..., 60 s

    def test_failed_poll_resets_the_backoff(self):
        src = make_source("fails", self.sig, writes=lambda k: False)
        reg = Registry([src])
        store = CountingStore()
        run_ticks(reg, store, self.cfg, T0, T0 + 60)
        self.assertGreater(reg.state["fails"]["idle"], 0)

        def boom(store, cfg, now):
            raise RuntimeError("nope")

        src.poll = boom
        reg.state["fails"]["sig_at"] = None
        reg.state["fails"]["last_poll"] = None
        reg.tick(store, self.cfg, T0 + 500)
        self.assertEqual(reg.state["fails"]["idle"], 0)

    def test_dormant_source_is_not_reported_stalled_inside_the_gap(self):
        src = make_source("calm", self.sig, writes=lambda k: False)
        reg = Registry([src])
        reg._born = T0 - 10_000
        store = CountingStore()
        run_ticks(reg, store, self.cfg, T0, T0 + 200)
        now = T0 + 200
        health = reg.health(now + 60, cfg=None)
        row = [r for r in health["sources"] if r["name"] == "calm"][0]
        self.assertNotEqual(row["state"], "stalled")

    def test_live_source_still_reads_stalled_when_silent(self):
        src = make_source("gone", None)
        reg = Registry([src])
        reg._born = T0 - 10_000
        reg.tick(CountingStore(), self.cfg, T0)
        health = reg.health(T0 + 100, cfg=None)
        row = [r for r in health["sources"] if r["name"] == "gone"][0]
        self.assertEqual(row["state"], "stalled")


class StoreWriteCounterTests(unittest.TestCase):
    """The runner's "new row" signal: real Store writes, not offset stamps."""

    def test_run_write_counts_and_offset_and_repeat_do_not(self):
        from dash.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "dash.sqlite"))
            base = store.write_count()
            store.set_offset("/x/file", 1, 10, 10, 1.0)
            self.assertEqual(store.write_count(), base, "offset stamps are not data writes")
            row = {"run_id": "t:1", "source": "t", "tier": "t", "status": "running",
                   "started_at": T0, "label": "L1", "session_id": "s1", "tool": "t"}
            store.upsert_run(row)
            after_first = store.write_count()
            self.assertGreater(after_first, base)
            store.upsert_run(row)  # identical: no change, no write
            self.assertEqual(store.write_count(), after_first)


class CursorDormantTests(unittest.TestCase):
    def setUp(self):
        import dash.sources.cursor as cursor

        self.cursor = cursor
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(os.path.join(self.home, "chats", "bucketA", "1" * 8 + "-1111-1111-1111-111111111111"))
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state)
        self.cfg = {"cursor_home": self.home, "state_dir": self.state, "poll_budget_s": 5}
        self._orig = {k: getattr(cursor, k) for k in ("live_cursor_pids", "_poll")}
        cursor.live_cursor_pids = lambda: set()
        self.polls = []
        cursor._poll = lambda store, cfg, now: self.polls.append(now)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(self.cursor, k, v)
        self.tmp.cleanup()

    def test_signal_paths_are_roots_buckets_and_usage_file(self):
        # Buckets come from the last discovery walk; run one here.
        self.cursor._discover(self.home, 10**6, 10**6)
        paths = self.cursor.signal_paths(self.cfg)
        self.assertIn(os.path.join(self.home, "chats"), paths)
        self.assertIn(os.path.join(self.home, "chats", "bucketA"), paths)
        self.assertIn(os.path.join(self.state, self.cursor.USAGE_FILE), paths)

    def test_dormant_cursor_enters_the_entry_point_only_on_the_gap(self):
        """Dormant cursor makes no poll call (so no walk, no offset query).

        Past the gap cap too: cursor has no fallback poll, its signals cover it.
        """
        reg = Registry([self.cursor])
        store = CountingStore()
        run_ticks(reg, store, self.cfg, T0, T0 + 200)
        self.assertTrue(reg.state["cursor"]["idle"] >= S.BACKOFF_AFTER)
        before = len(self.polls)
        run_ticks(reg, store, self.cfg, T0 + 200.5, T0 + 1000)
        self.assertEqual(len(self.polls), before)
        self.assertFalse(S._is_stalled(self.cursor, reg.state["cursor"], T0 + 1000, T0))

    def test_hook_line_wakes_cursor(self):
        reg = Registry([self.cursor])
        store = CountingStore()
        run_ticks(reg, store, self.cfg, T0, T0 + 200)
        before = len(self.polls)
        with open(os.path.join(self.state, self.cursor.USAGE_FILE), "a") as fh:
            fh.write('{"chat_id": "x"}\n')
        t = T0 + 200.5
        run_ticks(reg, store, self.cfg, t, t + 5)
        self.assertGreater(len(self.polls), before)

    def test_live_process_wakes_cursor_without_a_file_change(self):
        reg = Registry([self.cursor])
        store = CountingStore()
        run_ticks(reg, store, self.cfg, T0, T0 + 200)
        before = len(self.polls)
        self.cursor.live_cursor_pids = lambda: {4242}
        t = T0 + 200.5
        run_ticks(reg, store, self.cfg, t, t + 5)
        self.assertGreater(len(self.polls), before)


class GrokDormantTests(unittest.TestCase):
    def test_signal_paths_include_roots_and_known_pads(self):
        from dash.sources import grok_cli

        with tempfile.TemporaryDirectory() as tmp:
            pad = os.path.join(tmp, "pad")
            os.makedirs(pad)
            grok_cli._KNOWN_PADS = {pad}
            cfg = {"roots": {"grok_sessions": tmp, "grok_log": os.path.join(tmp, "log")}}
            paths = grok_cli.signal_paths(cfg)
            self.assertIn(tmp, paths)
            self.assertIn(pad, paths)
            self.assertIn(os.path.join(pad, "glm-sessions.tsv"), paths)
            grok_cli._KNOWN_PADS = set()


if __name__ == "__main__":
    unittest.main()


class HealthStoreRefreshTests(unittest.TestCase):
    def test_due_on_first_check_and_by_key_and_age(self):
        from dash import server as V

        due = V.health_store_refresh_due
        lo, hi = V.HEALTH_STORE_MIN_AGE_S, V.HEALTH_STORE_MAX_AGE_S
        self.assertTrue(due((1, 1, 1), None, None, 0.0))
        # Unchanged key: only the max age triggers a recompute.
        self.assertFalse(due((1, 1, 1), (1, 1, 1), 0.0, hi - 0.1))
        self.assertTrue(due((1, 1, 1), (1, 1, 1), 0.0, hi))
        # Moved key (live writer): the min age bounds the recompute rate.
        self.assertFalse(due((2, 1, 1), (1, 1, 1), 0.0, lo - 0.1))
        self.assertTrue(due((2, 1, 1), (1, 1, 1), 0.0, lo))
        self.assertLess(lo, hi)

