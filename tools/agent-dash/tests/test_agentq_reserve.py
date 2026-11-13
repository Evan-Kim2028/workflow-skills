import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentq import reserve_replay
from agentq.proxy import (
    CAPACITY,
    DEFAULT_CONFIG,
    EMPTY,
    PROXY_MINUTES_DDL,
    RESERVE,
    Entry,
    Governor,
    Reserve,
    merge_config,
    minute_epoch,
    minute_stamp,
    prefix_blocks,
    tokens_for_chars,
)

BASE = 1_800_000_000.0
MODEL_OUT = "morph-dsv41flash-1017"
MODEL_KIMI = "morph-kimik31"


def quiet(event, **fields):
    return None


class ReserveMathTest(unittest.TestCase):
    def test_usage_within_the_free_allowance_costs_no_reserve(self) -> None:
        reserve = Reserve(100, wall=0.0)
        reserve.sync(0.0, "m0", used=50, free=60)
        self.assertEqual(reserve.level, 100)
        self.assertEqual(reserve.drained, 0)

    def test_usage_past_the_allowance_drains_the_reserve(self) -> None:
        reserve = Reserve(100, wall=0.0)
        reserve.sync(0.0, "m0", used=80, free=60)
        self.assertEqual(reserve.level, 80)

    def test_a_refunded_charge_gives_its_overflow_back(self) -> None:
        reserve = Reserve(100, wall=0.0)
        reserve.sync(0.0, "m0", used=80, free=60)
        reserve.sync(0.0, "m0", used=50, free=60)
        self.assertEqual(reserve.level, 100)

    def test_refill_is_linear_over_refill_seconds_and_never_exceeds_capacity(self) -> None:
        reserve = Reserve(100, refill_s=1800.0, wall=0.0)
        reserve.sync(0.0, "m0", used=100, free=0)
        self.assertEqual(reserve.level, 0)
        reserve.refill(900.0)
        self.assertAlmostEqual(reserve.level, 50.0)
        reserve.refill(10_000.0)
        self.assertEqual(reserve.level, 100)

    def test_a_new_minute_clears_the_drain_but_keeps_the_level(self) -> None:
        reserve = Reserve(100, refill_s=1800.0, wall=0.0)
        reserve.sync(0.0, "m0", used=80, free=60)
        reserve.sync(60.0, "m1", used=0, free=60)
        self.assertEqual(reserve.drained, 0)
        self.assertAlmostEqual(reserve.level, 80 + 60 * 100 / 1800)

    def test_admission_overflow_counts_only_what_is_past_the_allowance(self) -> None:
        reserve = Reserve(100, wall=0.0)
        reserve.sync(0.0, "m0", used=50, free=60)
        self.assertEqual(reserve.admission_overflow(50, 30, 60), 20)
        self.assertEqual(reserve.admission_overflow(50, 5, 60), 0)

    def test_covers_needs_the_overflow_in_the_reserve_and_an_oversized_one_takes_a_full_reserve(self) -> None:
        reserve = Reserve(100, wall=0.0, level=10)
        self.assertFalse(reserve.covers(20))
        self.assertTrue(reserve.covers(10))
        full = Reserve(100, wall=0.0)
        self.assertTrue(full.covers(500))

    def test_seconds_until_covered_follows_the_refill_rate(self) -> None:
        reserve = Reserve(100, refill_s=1800.0, wall=0.0, level=0)
        self.assertAlmostEqual(reserve.seconds_until_covered(10), 180.0)

    def test_empty_zeroes_the_level(self) -> None:
        reserve = Reserve(100, wall=0.0)
        reserve.empty()
        self.assertEqual(reserve.level, 0)

    def test_restore_decays_by_elapsed_time_and_keeps_the_drain_only_in_the_same_minute(self) -> None:
        reserve = Reserve(1000, refill_s=1800.0, wall=0.0)
        reserve.sync(0.0, "m0", used=800, free=600)
        saved = reserve.snapshot()
        restored = Reserve(1000, refill_s=1800.0, wall=0.0)
        restored.restore(saved, now=300.0, minute="m0")
        self.assertAlmostEqual(restored.level, 800 + 1000 * 300 / 1800)
        self.assertEqual(restored.drained, 200)
        other = Reserve(1000, refill_s=1800.0, wall=0.0)
        other.restore(saved, now=300.0, minute="m1")
        self.assertEqual(other.drained, 0)


class GovernorReserveTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "agentq"
        self.home.mkdir()
        self.wall = [BASE + 10.0]
        self.gov = self.make_governor()

    def make_governor(self, **overrides) -> Governor:
        config = merge_config(DEFAULT_CONFIG, {"start_target": 1.0, **overrides})
        return Governor(self.home, config, clock=lambda: self.wall[0], wall=lambda: self.wall[0], log=quiet)

    def ticket(self, estimate: int, *, lane: str = "normal", budget: str = "output", model: str = MODEL_OUT):
        ticket = self.gov.make_ticket(
            job=f"job-{lane}-{estimate}", lane=lane, model=model, length=4, blocks=prefix_blocks("xxxx"),
            max_tokens=None, now=self.wall[0],
        )
        ticket.estimates = {budget: estimate}
        return ticket

    def occupy(self, budget: str, amount: int, lane: str = "normal") -> None:
        self.gov.budgets[budget].entries.append(
            Entry(at=self.gov.clock(), amount=amount, lane=lane, wall_at=self.wall[0])
        )

    def output_level(self) -> float:
        self.gov.sync_reserve(self.gov.budgets["output"])
        return self.gov.budgets["output"].reserve.level

    def test_only_budgets_with_a_reserve_carry_one(self) -> None:
        self.assertIsNotNone(self.gov.budgets["output"].reserve)
        self.assertIsNotNone(self.gov.budgets["uncached_ds"].reserve)
        self.assertIsNone(self.gov.budgets["total"].reserve)
        self.assertIsNone(self.gov.budgets["requests"].reserve)

    def test_default_budgets_match_morph_published_limits(self) -> None:
        budgets = DEFAULT_CONFIG["budgets"]
        self.assertEqual((budgets["output"]["limit"], budgets["output"]["reserve"]), (70_000, 210_000))
        self.assertEqual((budgets["kimi_output"]["limit"], budgets["kimi_output"]["reserve"]), (6_162, 18_486))
        self.assertEqual((budgets["uncached"]["limit"], budgets["uncached"]["reserve"]), (24_290, 72_870))
        self.assertEqual((budgets["uncached_ds"]["limit"], budgets["uncached_ds"]["reserve"]), (400_000, 1_200_000))
        self.assertEqual(budgets["requests"]["limit"], 1_000)
        self.assertEqual(DEFAULT_CONFIG["reserve_refill_s"], 1800.0)
        for name in ("output", "kimi_output", "uncached", "uncached_ds", "requests"):
            self.assertEqual(budgets[name]["window_mode"], "calendar")

    def test_a_reserve_on_a_rolling_budget_is_refused_at_startup(self) -> None:
        with self.assertRaises(ValueError):
            self.make_governor(budgets={"output": {"window_mode": "rolling", "reserve": 1000}})

    def test_free_allowance_is_free_and_the_overflow_drains_the_reserve(self) -> None:
        self.occupy("output", 60_000)
        self.assertEqual(self.output_level(), 210_000)
        self.occupy("output", 20_000)
        self.assertEqual(self.output_level(), 200_000)

    def test_backlog_waits_at_the_allowance_and_never_spends_the_reserve(self) -> None:
        self.occupy("output", 60_000)
        backlog = self.ticket(20_000, lane="backlog")
        self.gov.enqueue(backlog)
        self.gov.pump()
        self.assertFalse(backlog.holds)
        self.assertEqual(backlog.last_refusal, RESERVE)
        self.assertEqual(self.output_level(), 210_000)

    def test_normal_work_spends_the_reserve_past_the_allowance(self) -> None:
        normal = self.ticket(100_000)
        self.gov.enqueue(normal)
        self.gov.pump()
        self.assertTrue(normal.holds)
        self.assertEqual(self.output_level(), 180_000)

    def test_urgent_work_spends_the_reserve_too(self) -> None:
        urgent = self.ticket(100_000, lane="urgent")
        self.gov.enqueue(urgent)
        self.gov.pump()
        self.assertTrue(urgent.holds)
        self.assertEqual(self.output_level(), 180_000)

    def test_normal_work_waits_when_the_reserve_cannot_pay_and_does_not_raise_the_target(self) -> None:
        self.gov.budgets["output"].reserve.empty()
        normal = self.ticket(100_000)
        self.gov.enqueue(normal)
        self.gov.pump()
        self.assertFalse(normal.holds)
        self.assertEqual(normal.last_refusal, EMPTY)
        self.assertFalse(self.gov.budgets["output"].saturated)
        self.assertNotEqual(normal.last_refusal, CAPACITY)

    def test_the_reserve_refills_over_thirty_minutes(self) -> None:
        normal = self.ticket(100_000)
        self.gov.enqueue(normal)
        self.gov.pump()
        self.assertEqual(self.output_level(), 180_000)
        self.wall[0] += 1800.0
        self.assertEqual(self.output_level(), 210_000)

    def test_a_429_naming_the_budget_zeroes_its_reserve(self) -> None:
        ticket = self.ticket(1_000)
        self.gov.learn_from_429(ticket, "Rate limit exceeded. Limit: 70000 output tokens/minute.")
        self.assertEqual(self.output_level(), 0)

    def test_a_429_naming_another_budget_leaves_the_reserve_alone(self) -> None:
        ticket = self.ticket(1_000)
        self.gov.learn_from_429(ticket, "Rate limit exceeded. Limit: 24290 uncached input tokens/minute.")
        self.assertEqual(self.output_level(), 210_000)

    def test_the_reserve_survives_a_restart_and_refills_for_the_time_away(self) -> None:
        self.gov.enqueue(self.ticket(170_000))
        self.gov.pump()
        self.assertEqual(self.output_level(), 110_000)
        self.gov.save_state()
        self.wall[0] += 300.0
        restarted = self.make_governor()
        restarted.sync_reserve(restarted.budgets["output"])
        self.assertAlmostEqual(restarted.budgets["output"].reserve.level, 110_000 + 210_000 * 300 / 1800, delta=1)

    def test_a_restart_inside_the_same_minute_does_not_drain_twice(self) -> None:
        self.gov.enqueue(self.ticket(170_000))
        self.gov.pump()
        self.gov.save_state()
        restarted = self.make_governor()
        restarted.sync_reserve(restarted.budgets["output"])
        self.assertEqual(restarted.budgets["output"].reserve.level, 110_000)

    def test_a_state_file_without_reserves_starts_the_reserve_empty(self) -> None:
        (self.home / "proxy_state.json").write_text(
            json.dumps({"saved_at": self.wall[0], "budgets": {"output": {"limit": 70_000, "window_used": 0}}})
        )
        restarted = self.make_governor()
        self.assertEqual(restarted.budgets["output"].reserve.level, 0)

    def test_kimi_output_and_uncached_budgets_apply_to_every_kimi_model(self) -> None:
        for model in (MODEL_KIMI, "morph-kimik3"):
            names = {b.name for b in self.gov.budgets.values() if b.applies(model)}
            self.assertIn("kimi_output", names)
            self.assertIn("uncached", names)
            self.assertNotIn("output", names)
        self.assertEqual(self.gov.budgets["kimi_output"].limit, 6_162)

    def test_requests_budget_admits_one_per_request_and_waits_at_the_limit(self) -> None:
        ticket = self.ticket(1, budget="requests")
        self.assertEqual(self.gov.make_ticket(
            job="r", lane="normal", model=MODEL_OUT, length=4, blocks=prefix_blocks("xxxx"),
            max_tokens=None, now=self.wall[0],
        ).estimates["requests"], 1)
        self.occupy("requests", 1_000)
        self.gov.enqueue(ticket)
        self.gov.pump()
        self.assertFalse(ticket.holds)
        self.assertEqual(ticket.last_refusal, CAPACITY)

    def test_status_reports_free_allowance_reserve_and_capacity(self) -> None:
        self.occupy("output", 60_000)
        self.occupy("output", 20_000)
        status = self.gov.status()
        output = status["budgets"]["output"]
        self.assertEqual(output["free"], 70_000)
        self.assertEqual(output["used_60s"], 80_000)
        self.assertEqual(output["reserve"]["level"], 200_000)
        self.assertEqual(output["reserve"]["capacity"], 210_000)
        self.assertEqual(output["reserve"]["refill_per_min"], 7_000)
        self.assertNotIn("reserve", status["budgets"]["total"])
        self.assertIn("uncached_estimate", status)

    def test_uncached_ratio_learns_the_actual_share_and_scales_admission(self) -> None:
        raw = self.gov.make_ticket(
            job="u", lane="normal", model=MODEL_OUT, length=4096, blocks=prefix_blocks("A" * 4096),
            max_tokens=None, now=self.wall[0],
        )
        self.assertEqual(raw.uncached_est, tokens_for_chars(4096))
        for _ in range(200):
            self.gov.learn_uncached(MODEL_OUT, 1000, 500)
        ratio = self.gov.uncached_factor(MODEL_OUT)
        self.assertAlmostEqual(ratio, 0.5, delta=0.02)
        self.assertEqual(self.gov.uncached_ratio["morph-dsv41flash"]["samples"], 200)

    def test_uncached_ratio_stays_within_its_clamp(self) -> None:
        for _ in range(400):
            self.gov.learn_uncached(MODEL_OUT, 1000, 0)
        self.assertGreaterEqual(self.gov.uncached_factor(MODEL_OUT), 0.25)

    def test_the_learned_ratio_reaches_the_uncached_admission_estimate(self) -> None:
        for _ in range(200):
            self.gov.learn_uncached(MODEL_OUT, 1000, 500)
        ticket = self.gov.make_ticket(
            job="u2", lane="normal", model=MODEL_OUT, length=4096, blocks=prefix_blocks("B" * 4096),
            max_tokens=None, now=self.wall[0],
        )
        self.assertEqual(ticket.estimates["uncached_ds"], round(ticket.uncached_est * self.gov.uncached_factor(MODEL_OUT)))
        self.assertLess(ticket.estimates["uncached_ds"], ticket.uncached_est)


    def test_a_reserve_budget_target_climbs_no_higher_than_the_morph_limit(self) -> None:
        output = self.gov.budgets["output"]
        output.target = 0.98
        for _ in range(20):
            output.saturated = True
            self.wall[0] += 60.0
            self.gov.tick()
        self.assertEqual(output.target, 1.0)

    def test_capacity_headline_carries_the_live_reserves(self) -> None:
        from agentq.dash import capacity_headline

        live = {"budgets": {
            "output": {"reserve": {"level": 140_000, "capacity": 210_000}},
            "uncached_ds": {"reserve": {"level": 900_000, "capacity": 1_200_000}},
            "total": {},
        }}
        headline = capacity_headline({}, live)
        self.assertEqual(headline["reserves"], {
            "ds_output": {"level": 140_000, "capacity": 210_000},
            "ds_uncached": {"level": 900_000, "capacity": 1_200_000},
        })
        self.assertEqual(capacity_headline({}, None)["reserves"], {})

class ReplayTest(unittest.TestCase):
    def test_replay_predicts_the_exhaustion_minute_and_matches_a_logged_429(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "agentq.db"
        connection = sqlite3.connect(db)
        connection.executescript(PROXY_MINUTES_DDL)
        start = minute_epoch("2026-11-13T12:00")
        for index in range(10):
            stamp = minute_stamp(start + 60 * index)
            output = 500_000 if index == 9 else 60_000
            connection.execute(
                "INSERT INTO proxy_minutes (minute, model, input, cache_read, output) VALUES (?, ?, ?, ?, ?)",
                (stamp, MODEL_OUT, 1_000, 0, output),
            )
        connection.commit()
        connection.close()
        log = Path(tmp.name) / "proxy.err.log"
        log.write_text(
            "2026-11-13T12:09:30Z agentq-proxy upstream_429 job=abc model=morph-dsv41flash-1017 attempt=1 "
            "body={ detail : Rate limit exceeded. Please slow down your requests. Limit: 70000 output tokens/minute. }\n"
        )
        last, usage = reserve_replay.load_usage(db, "2026-11-13T00:00")
        self.assertEqual(last, "2026-11-13T12:09")
        refusals = reserve_replay.load_refusals([log])
        minutes = reserve_replay.minute_range("2026-11-13T12:00", last)
        spec = reserve_replay.BUDGETS[0]
        rows = reserve_replay.replay_budget(spec, usage, minutes, 1.0)
        self.assertEqual([r.minute for r in rows if r.predicted_empty], ["2026-11-13T12:09"])
        summary = reserve_replay.summarise("output", rows, refusals, spec["kind"])
        self.assertEqual(summary["predicted_empty_minutes"], 1)
        self.assertEqual(summary["morph_429_minutes"], 1)
        self.assertEqual(summary["429_matched_by_prediction"], 1)


if __name__ == "__main__":
    unittest.main()
