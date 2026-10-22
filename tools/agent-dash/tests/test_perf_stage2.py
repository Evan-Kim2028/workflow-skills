"""Stage-2 performance regressions: incremental folds stay exact.

Pins the steady-state CPU work: the per-run usage fold refolds only
written runs and always equals a full rebuild, no matter the
interleaving (fixed seed); grouped pricing matches per-event pricing
on edge shapes; the phase cache invalidates on rule-file and label
changes; single-flight shares one computation; classification never
holds the facade lock; snapshot totals always equal the usage buckets
they ride with.
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault(
    "AGENT_DASH_WORKSTREAMS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"),
)

import random
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import phases as phases_mod
from dash.pricing import cost as price_cost
from dash.singleflight import SingleFlight
from dash.store import Store
from dash.store.usage import _fold_events


def memory(tmpdir) -> Store:
    return Store(Path(tmpdir) / "dash.sqlite")


def usage_row(session, message, run, model="claude-sonnet-4-5", tool="muse", **tokens):
    row = {
        "ts": 1000.0,
        "session_id": session,
        "message_id": message,
        "run_id": run,
        "model": model,
        "tool": tool,
    }
    row.update({"input": 100, "output": 50, "cache_read": 0, "cache_write": 0, **tokens})
    return row


class IncrementalFoldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_incremental_equals_full_after_interleaved_writes(self):
        """Fixed-seed interleaving: inserts, upserts, and cost rewrites."""
        rng = random.Random(20261019)
        runs = [f"run-{i}" for i in range(12)]
        keys = []
        for step in range(300):
            op = rng.random()
            run = rng.choice(runs)
            if op < 0.55 or not keys:
                session, message = f"s-{step}", f"m-{step}"
                keys.append((session, message, run))
                self.store.add_usage(
                    usage_row(
                        session, message, run,
                        input=rng.choice([0, 10, 100, 5000]),
                        output=rng.choice([0, 5, 50]),
                        cache_read=rng.choice([0, 0, 1000]),
                        cache_write=rng.choice([0, 0, 0, 40]),
                        model=rng.choice(["claude-sonnet-4-5", "grok-4.7", "unknown-model-zzz"]),
                    )
                )
            elif op < 0.8:
                session, message, run = rng.choice(keys)
                self.store.add_usage(
                    usage_row(session, message, run, input=rng.choice([1, 999]))
                )
            else:
                sample = rng.sample(keys, min(len(keys), 5))
                self.store.write_costs(
                    [(1.5, None, None, "muse", session, message) for session, message, _run in sample]
                )
            if step % 25 == 0:
                self.assertEqual(
                    self.store._usage_by_run(),
                    self.store._usage_by_run_uncached(),
                    f"diverged at step {step}",
                )
        self.assertEqual(
            self.store._usage_by_run(), self.store._usage_by_run_uncached()
        )

    def test_merge_moves_refold(self):
        self.store.upsert_run({"run_id": "src", "source": "muse", "label": "a",
                               "status": "running", "started_at": 10})
        self.store.upsert_run({"run_id": "dst", "source": "muse", "label": "a",
                               "status": "running", "started_at": 9})
        self.store.add_usage(usage_row("s", "m", "src", input=300))
        before = self.store._usage_by_run()
        self.assertIn("src", before)
        self.assertTrue(self.store.reassign_run("src", "dst"))
        after = self.store._usage_by_run()
        self.assertEqual(after, self.store._usage_by_run_uncached())
        self.assertNotIn("src", after)
        self.assertEqual(after["dst"][0]["input"], 300)

    def test_fold_result_is_a_copy(self):
        self.store.add_usage(usage_row("s", "m", "r1", input=100))
        first = self.store._usage_by_run()
        first["r1"] = ("corrupted", 0.0, False)
        self.assertEqual(self.store._usage_by_run()["r1"][0]["input"], 100)


class GroupedPricingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _events(self, rng, n):
        events = []
        for _ in range(n):
            model = rng.choice(["claude-sonnet-4-5", "grok-4.7", "deepseek-chat", "no-such-model"])
            missing = rng.random() < 0.15
            events.append(
                {
                    "model": model,
                    "input_tokens": None if missing and rng.random() < 0.5 else rng.choice([0, 10, 2000]),
                    "output_tokens": None if missing and rng.random() < 0.5 else rng.choice([0, 5, 500]),
                    "cache_read": rng.choice([0, 100]),
                    "cache_write": rng.choice([0, 0, 30]),
                    "cache_write_5m": None,
                    "cache_write_1h": None,
                    "reasoning": rng.choice([None, 0, 12]),
                    "ts": 1792400000.0 + rng.choice([0, 100, 7200]),
                }
            )
        return events

    def test_grouped_matches_per_event(self):
        """Grouped fold equals per-event pricing on tricky shapes."""
        rng = random.Random(77)
        for trial in range(30):
            events = self._events(rng, rng.choice([1, 2, 5, 40]))
            grouped = _fold_events(events, _memo={})
            expected_costs, unpriced = [], False
            for event in events:
                priced = price_cost(
                    event["model"], event["input_tokens"], event["output_tokens"],
                    event["cache_read"], event["cache_write"],
                    cache_write_5m=event["cache_write_5m"],
                    cache_write_1h=event["cache_write_1h"],
                    reasoning=event["reasoning"], ts=event["ts"],
                )
                expected_costs.append(priced)
                if priced is None and any(
                    event[name] is not None
                    for name in ("input_tokens", "output_tokens", "cache_read", "cache_write")
                ):
                    unpriced = True
            if not events:
                expected = (grouped[0], 0.0, False)
            elif unpriced or all(item is None for item in expected_costs):
                expected = (grouped[0], None, True)
            else:
                expected = (grouped[0], sum(item for item in expected_costs if item is not None), False)
            self.assertEqual(grouped[1] is None, expected[1] is None, f"trial {trial}")
            if grouped[1] is not None:
                self.assertAlmostEqual(grouped[1], expected[1], places=9, msg=f"trial {trial}")
            self.assertEqual(grouped[2], expected[2], f"trial {trial}")


class PhaseCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(self.tmp.name)
        self.store.upsert_run({"run_id": "r-plan", "source": "muse", "label": "ws9-plan-alpha",
                               "status": "done", "started_at": 10, "ended_at": 20,
                               "workstream": "WS9"})
        self.store.upsert_run({"run_id": "r-exec", "source": "muse", "label": "fixing the build",
                               "status": "done", "started_at": 30, "ended_at": 40,
                               "workstream": "WS9"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _inputs(self, rules):
        return ("digest-for-test", "taskmap-for-test", None, id(rules))

    def test_label_change_reclassifies(self):
        raw = [dict(row) for row in self.store._all_runs()]
        rules, _digest, _version = phases_mod.load_rules(local=phases_mod.local_path())
        got = self.store._classify_runs_cached(raw, {}, {}, rules, self._inputs(rules))
        first = dict(got)
        self.assertEqual(first["r-plan"][0], "planning")
        raw[1]["label"] = "ws9-plan-beta"
        got = self.store._classify_runs_cached(raw, {}, {}, rules, self._inputs(rules))
        self.assertEqual(got["r-exec"][0], "planning")
        self.assertEqual(got["r-plan"], first["r-plan"])

    def test_rule_change_retags(self):
        raw = [dict(row) for row in self.store._all_runs()]
        rules, _digest, _version = phases_mod.load_rules(local=phases_mod.local_path())
        before = self.store._classify_runs_cached(raw, {}, {}, rules, ("digest-a", "tm", None))
        other = [dict(entry) for entry in rules]
        after = self.store._classify_runs_cached(raw, {}, {}, other, ("digest-b", "tm", None))
        self.assertEqual(before, after)
        self.assertIsNot(before, after)

    def test_matches_fresh_classify(self):
        raw = [dict(row) for row in self.store._all_runs()]
        rules, _digest, _version = phases_mod.load_rules(local=phases_mod.local_path())
        cached = self.store._classify_runs_cached(raw, {}, {}, rules, self._inputs(rules))
        fresh = phases_mod.classify_runs(raw, taskmap={}, plan_tasks={}, rules=rules)
        self.assertEqual(cached, fresh)


class SingleFlightTest(unittest.TestCase):
    def test_concurrent_callers_share_one_computation(self):
        flight = SingleFlight()
        calls = []
        gate = threading.Event()

        def slow():
            calls.append(1)
            gate.wait(timeout=10)
            return {"payload": [1, 2, 3]}

        results = [None] * 8

        def worker(index):
            results[index] = flight.do("key", slow)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        time.sleep(0.2)
        self.assertEqual(flight.in_flight(), 1)
        gate.set()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(calls), 1)
        for result in results:
            self.assertIs(result, results[0])
        self.assertEqual(flight.in_flight(), 0)

    def test_error_reaches_waiters_and_retries(self):
        flight = SingleFlight()
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise ValueError("boom")
            return "ok"

        with self.assertRaises(ValueError):
            flight.do("k", flaky)
        self.assertEqual(flight.do("k", flaky), "ok")
        self.assertEqual(len(attempts), 2)


class LockBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(self.tmp.name)
        self.store.upsert_run({"run_id": "r1", "source": "muse", "label": "ws9-plan-x",
                               "status": "done", "started_at": 10, "ended_at": 20,
                               "workstream": "WS9"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_write_proceeds_while_classifying(self):
        """Classification runs outside the facade lock: a writer blocked
        inside classify_run must not stop a concurrent usage write."""
        self.store.add_usage(usage_row("s0", "m0", "r1", input=10))
        entered = threading.Event()
        release = threading.Event()
        real_classify = phases_mod.classify_run

        def gated(run, ctx=None, *, rules=None):
            entered.set()
            self.assertTrue(release.wait(timeout=10), "classify gate timed out")
            return real_classify(run, ctx, rules=rules)

        with mock.patch.object(phases_mod, "classify_run", side_effect=gated):
            worker = threading.Thread(target=self.store.by_phase_all_time)
            worker.start()
            self.assertTrue(entered.wait(timeout=10), "classify never started")
            done = threading.Event()

            def writer():
                self.store.add_usage(usage_row("s", "m", "r1", input=50))
                done.set()

            writer_thread = threading.Thread(target=writer)
            writer_thread.start()
            self.assertTrue(done.wait(timeout=10), "usage write blocked during classify")
            release.set()
            worker.join(timeout=15)
            writer_thread.join(timeout=10)
        self.assertIn("r1", self.store._usage_by_run())

    def test_snapshot_totals_match_buckets(self):
        now = 2000.0
        self.store.add_usage(usage_row("s", "m", "r1", input=100, output=10, ts=1500.0))
        snap = self.store.snapshot(now, "all", 1000.0, None, usage_group="model")
        self.assertIn("usage", snap)
        bucket_tokens = sum(
            cell["input"] + cell["output"] + cell["cache_read"] + cell["cache_write"]
            for cell in snap["usage"]
        )
        totals = snap["totals"]["tokens"]
        self.assertEqual(
            bucket_tokens,
            totals["input"] + totals["output"] + totals["cache_read"] + totals["cache_write"],
        )


if __name__ == "__main__":
    unittest.main()
