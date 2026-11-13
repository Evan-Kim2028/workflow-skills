"""agentq fill controller: rate and desired-worker math, hysteresis, the plan and its gates, launch pacing,
the store readers, the filler generator (targets, cursor, skip-unchanged, labels, pause) and the broker
wiring. Offline. AGENTQ_HOME is a temp folder; nothing launches a worker and git is never run for real.
"""

import concurrent.futures
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentq import broker as B  # noqa: E402
from agentq import cli, db  # noqa: E402
from agentq import fill as F  # noqa: E402
from agentq import filler as Fl  # noqa: E402
from agentq import headroom as H  # noqa: E402

LAPTOP = B.DEFAULT_LAPTOP
CALM = H.Headroom(90.0, 0.2, 0.1, 0.2)


def reading(free=90.0, cpu=0.2, swap=0.1, load=0.2):
    """A headroom reading with calm values for anything not named."""
    return H.Headroom(free, cpu, swap, load)

DS_MODEL = "morph-dsv41flash-1017"
NOW = 100000.0
LAST_MINUTE = int(NOW // 60) * 60 - 60


def stamp(ts):
    return F.minute_stamp(ts)


FILL = F.section({"enabled": True}, F.DEFAULT_FILL)
PACING = F.section(None, F.DEFAULT_PACING)


def status_doc(blocked=0):
    return {
        "ok": True,
        "budgets": {
            "total": {"queued": {"urgent": 0, "normal": 0, "backlog": 0}, "blocked_by_family": {"morph-dsv41flash": blocked}},
        },
    }


class HysteresisTests(unittest.TestCase):
    def test_hysteresis_fills_below_low_stops_above_high_and_holds_between(self):
        target, low, high = 8_000_000, 0.9, 0.97
        self.assertTrue(F.filling_state(False, 7_000_000, target, low, high))
        self.assertFalse(F.filling_state(True, 7_900_000, target, low, high))
        self.assertTrue(F.filling_state(True, 7_500_000, target, low, high))
        self.assertFalse(F.filling_state(False, 7_500_000, target, low, high))


def tick(ctl, now, billed, active, *, fill=FILL, real=0, ready_real=0, queued_filler=0, gate=None, paused=False):
    return ctl.tick(now, fill=fill, paused=paused, billed_avg=billed, active=active, ready_real=ready_real,
                    queued_filler=queued_filler, gate=gate)


class LaptopGateTests(unittest.TestCase):
    def test_laptop_cpu_memory_and_swap_hold_at_the_filler_bars(self):
        self.assertIn("memory free 10% < 20%", F.gate_reason(status_doc(), 0, reading(free=10.0), LAPTOP, FILL))
        self.assertIn("cpu 80% busy >= 75%", F.gate_reason(status_doc(), 0, reading(cpu=0.80), LAPTOP, FILL))
        self.assertIn("swap 88% used >= 85%", F.gate_reason(status_doc(), 0, reading(swap=0.88), LAPTOP, FILL))

    def test_readings_inside_the_filler_bars_and_unreadable_gauges_do_not_hold(self):
        self.assertIsNone(F.gate_reason(status_doc(), 0, reading(cpu=0.74, swap=0.84), LAPTOP, FILL))
        self.assertIsNone(F.gate_reason(status_doc(), 0, reading(None, None, None, 9.0), LAPTOP, FILL))
        self.assertIsNone(F.gate_reason(status_doc(), 0, None, LAPTOP, FILL))


def run(ctl, *, seconds, billed_fn, start_filler, real_fn=lambda t: 0, life=None, fill=FILL, gate_fn=lambda t: None):
    """Drive the controller tick by tick. A filler step starts at once and its workers live ``life`` seconds
    (forever when None). ``real_fn(t)`` gives the real DeepSeek count at second t. Returns (peak busy workers,
    final busy workers, plans) where busy = filler + real."""
    launches, plans, peak, t = [], [], 0, 0
    final = start_filler
    while t < seconds:
        filler = start_filler + sum(n for lt, n in launches if life is None or t - lt < life)
        real = real_fn(t)
        busy = filler + real
        plan = tick(ctl, NOW + t, billed_fn(filler, real), busy, fill=fill, queued_filler=0, gate=gate_fn(t))
        if plan.submit:
            launches.append((t, plan.submit))
        plans.append(plan)
        if plan.submit:
            peak = max(peak, busy + plan.submit)
        final = busy + plan.submit
        t += 2
    return peak, final, plans


class ControllerTests(unittest.TestCase):
    def test_disabled_and_paused_never_step(self):
        self.assertEqual(tick(F.FillController(), NOW, 1e6, 10, fill=dict(FILL, enabled=False)).key, "disabled")
        self.assertEqual(tick(F.FillController(), NOW, 1e6, 10, paused=True).key, "paused")

    def test_an_empty_pool_fills_from_zero_when_the_gates_are_open(self):
        ctl = F.FillController()
        peak, final, plans = run(ctl, seconds=2 * 3600, billed_fn=lambda f, r: 150_000.0 * (f + r),
                                 start_filler=0)
        self.assertEqual(plans[0].key, "stepping")
        self.assertGreaterEqual(150_000.0 * final, 0.9 * 8_000_000)

    def test_steps_are_at_most_step_workers_and_at_least_step_interval_apart(self):
        ctl = F.FillController()
        peak, final, plans = run(ctl, seconds=3600, billed_fn=lambda f, r: 0.0, start_filler=0)
        stepping = [i for i, p in enumerate(plans) if p.key == "stepping"]
        self.assertTrue(all(p.submit <= 4 for p in plans))
        gaps = [(b - a) * 2 for a, b in zip(stepping, stepping[1:])]
        self.assertTrue(all(g >= 120 for g in gaps))

    def test_a_queued_step_must_launch_before_the_next_one(self):
        ctl = F.FillController()
        self.assertEqual(tick(ctl, NOW, 0.0, 0).key, "stepping")
        self.assertEqual(tick(ctl, NOW + 200, 0.0, 0, queued_filler=4).key, "step-wait")

    def test_realistic_churn_with_good_gains_reaches_the_target_band(self):
        ctl = F.FillController()
        churn = lambda t: 50 + (t // 300) % 2  # noqa: E731  real 50 <-> 51 every 300 s
        billed = lambda f, r: 120_000.0 * (f + r)  # noqa: E731
        peak, final, plans = run(ctl, seconds=3 * 3600, billed_fn=billed, start_filler=0, real_fn=churn)
        self.assertGreaterEqual(120_000.0 * final, 0.9 * 8_000_000)
        self.assertLessEqual(peak, 64)

    def test_the_high_mark_stops_steps(self):
        plan = tick(F.FillController(), NOW, 8_000_000, 60)
        self.assertEqual((plan.filling, plan.submit, plan.key), (False, 0, "high-mark"))

    def test_hysteresis_keeps_filling_between_the_marks(self):
        fresh = F.FillController()
        self.assertEqual(tick(fresh, NOW, 7_500_000, 40).key, "high-mark")
        ctl = F.FillController()
        self.assertEqual(tick(ctl, NOW, 7_000_000, 40).key, "stepping")
        self.assertEqual(tick(ctl, NOW + 200, 7_500_000, 44).key, "stepping")
        self.assertEqual(tick(ctl, NOW + 400, 7_800_000, 44).key, "high-mark")

    def test_real_work_ready_goes_first(self):
        self.assertEqual(tick(F.FillController(), NOW, 1e6, 10, ready_real=2).key, "real-ready")

    def test_any_gate_holds_filler_with_its_reason(self):
        plan = tick(F.FillController(), NOW, 1e6, 10, gate="Morph slow: in-flight limit 20 reached")
        self.assertEqual((plan.submit, plan.key), (0, "gate"))
        self.assertIn("in-flight limit 20", plan.reason)

    def test_max_workers_is_never_exceeded(self):
        ctl = F.FillController()
        peak, final, plans = run(ctl, seconds=3 * 3600, billed_fn=lambda f, r: 0.0, start_filler=0,
                                 real_fn=lambda t: 60)
        self.assertLessEqual(peak, 64)
        self.assertLessEqual(final, 64)

    def test_filler_fills_to_max_workers_when_the_pool_stays_empty_of_billing(self):
        ctl = F.FillController()
        peak, final, plans = run(ctl, seconds=3 * 3600, billed_fn=lambda f, r: 0.0, start_filler=0)
        self.assertEqual(peak, 64)
        self.assertEqual(plans[-1].key, "at-max")

    def test_a_held_gate_never_grows_the_pool(self):
        ctl = F.FillController()
        held = [tick(ctl, NOW + 2 * i, 0.0, 0, gate="laptop gate: load 2.00 per CPU") for i in range(300)]
        self.assertTrue(all(p.submit == 0 and p.key == "gate" for p in held))
        self.assertIsNone(ctl.last_step_at)


class GateSimulationTest(unittest.TestCase):
    """The real gate_reason feeds the controller over simulated time: filler holds while a gate is closed and
    grows again once it opens."""

    def closed_between(self, start, end, reason):
        return lambda t: reason if start <= t < end else None

    def test_morph_slow_in_flight_limit_binding_holds_filler_then_resumes(self):
        status = {"budgets": {"total": {"queued": {}}}, "inflight_limit": 20, "inflight_max": 96,
                  "inflight_enforcing": True, "inflight_queued": 5, "inflight_in_flight": 20, "inflight_mode": "enforce"}
        reason = F.gate_reason(status, 0, None, LAPTOP, FILL)
        self.assertIn("in-flight limit 20", reason)
        billed = lambda f, r: 120_000.0 * (f + r)  # noqa: E731
        plans_held = run(F.FillController(), seconds=3600, billed_fn=billed, start_filler=0,
                         gate_fn=self.closed_between(0, 3600, reason))[2]
        self.assertTrue(all(p.submit == 0 and p.key == "gate" for p in plans_held))
        peak, final, plans = run(F.FillController(), seconds=3 * 3600, billed_fn=billed, start_filler=0,
                                 gate_fn=self.closed_between(0, 1800, reason))
        self.assertEqual(sum(p.submit for p in plans[:900]), 0)
        self.assertGreater(sum(p.submit for p in plans[900:]), 0)
        self.assertGreaterEqual(120_000.0 * final, 0.9 * 8_000_000)

    def test_a_deepseek_429_holds_filler_for_its_window(self):
        reason = F.gate_reason(status_doc(), 2, None, LAPTOP, FILL)
        self.assertIn("429", reason)
        peak, final, plans = run(F.FillController(), seconds=1800, billed_fn=lambda f, r: 0.0, start_filler=0,
                                 gate_fn=self.closed_between(0, 600, reason))
        self.assertEqual(sum(p.submit for p in plans[:300]), 0)
        self.assertGreater(sum(p.submit for p in plans[300:]), 0)

    def test_a_laptop_past_the_filler_bars_holds_filler(self):
        reason = F.gate_reason(status_doc(), 0, reading(cpu=0.9), LAPTOP, FILL)
        self.assertIn("laptop gate", reason)
        peak, final, plans = run(F.FillController(), seconds=1200, billed_fn=lambda f, r: 0.0, start_filler=0,
                                 gate_fn=self.closed_between(0, 1200, reason))
        self.assertEqual(sum(p.submit for p in plans), 0)
        self.assertEqual(final, 0)


class ProxyUnavailableTests(unittest.TestCase):
    def test_no_status_and_an_empty_status_are_unavailable(self):
        self.assertFalse(F.proxy_available(None))
        self.assertFalse(F.proxy_available({}))
        self.assertTrue(F.proxy_available(status_doc()))

    def test_filler_is_held_with_the_proxy_unavailable_reason(self):
        for status in (None, {}):
            self.assertEqual(F.gate_reason(status, 0, CALM, LAPTOP, FILL), "proxy status unavailable; filler held")


class PacingTests(unittest.TestCase):
    def test_the_default_launch_cost_is_40k_against_80_percent_of_400k(self):
        self.assertEqual(F.pacing_allowance(0, 0, PACING, None), (8, 40_000))
        self.assertEqual(F.pacing_allowance(300_000, 0, PACING, None), (0, 40_000))

    def test_each_pending_launch_takes_its_cost_out_of_the_allowance(self):
        self.assertEqual(F.pacing_allowance(0, 3, PACING, None), (5, 40_000))
        self.assertEqual(F.pacing_allowance(200_000, 1, PACING, None), (2, 40_000))

    def test_a_measured_cost_replaces_the_default(self):
        self.assertEqual(F.pacing_allowance(0, 0, PACING, 80_000), (4, 80_000))

    def test_over_the_cap_allows_nothing(self):
        self.assertEqual(F.pacing_allowance(400_000, 0, PACING, None)[0], 0)


class SectionAndWorkerTests(unittest.TestCase):
    def test_unknown_keys_and_bad_values_are_ignored(self):
        out = F.section({"enabled": 1, "max_workers": "12", "low_frac": "x", "nope": 3}, F.DEFAULT_FILL)
        self.assertIs(out["enabled"], True)
        self.assertEqual(out["max_workers"], 12)
        self.assertEqual(out["low_frac"], F.DEFAULT_FILL["low_frac"])
        self.assertNotIn("nope", out)

    def test_direct_workers_are_deepseek_launches_without_a_queue_job_env(self):
        ps = "\n".join([
            "1 python3 launch_pi.py pi-deepseek-flash lbl ws",
            "2 python3 launch_pi.py pi-deepseek-flash lbl ws AGENTQ_JOB=0123456789ab",
            "3 python3 launch_pi.py pi-kimi lbl ws",
            "4 python3 other.py pi-deepseek-flash",
        ])
        self.assertEqual(F.direct_ds_workers(ps), 1)
        self.assertIsNone(F.direct_ds_workers(None))


class PendingWhileUnreachableTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = db.connect(Path(tmp.name) / "home")
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "CREATE TABLE proxy_minutes (minute TEXT, model TEXT, input INT, cache_read INT, output INT, r429 INT,"
            " PRIMARY KEY (minute, model))"
        )
        self.conn.execute(
            "CREATE TABLE proxy_jobs (job TEXT, model TEXT, req INT, input INT, first_ts REAL, last_ts REAL,"
            " PRIMARY KEY (job, model))"
        )
        db.insert_job(self.conn, {
            "id": "old", "lane": "normal", "kind": "read", "res": "light", "tier": "pi-deepseek-flash",
            "label": "x", "workspace": "/tmp", "session": "sess", "harness": "pi", "prompt_path": "/tmp/p",
        }, NOW)
        self.conn.execute("UPDATE jobs SET state = 'running', started_at = ? WHERE id = 'old'", (NOW - 3600,))

    def test_a_requestless_launch_older_than_the_grace_counts_only_while_unreachable(self):
        self.assertEqual(F.pending_launches(self.conn, NOW, 120), 0)
        self.assertEqual(F.pending_launches(self.conn, NOW, None), 1)

    def test_pacing_inputs_drop_the_grace_only_when_the_proxy_is_unavailable(self):
        pacing = F.section(None, F.DEFAULT_PACING)
        self.assertEqual(F.pacing_inputs(self.conn, NOW, status_doc(), pacing)[2], 0)
        self.assertEqual(F.pacing_inputs(self.conn, NOW, None, pacing)[2], 1)

    def test_unreachable_pacing_uses_the_store_last_known_minute(self):
        self.conn.execute("INSERT INTO proxy_minutes VALUES (?,?,?,?,?,?)",
                          (stamp(NOW - 120), DS_MODEL, 300_000, 0, 0, 0))
        self.assertEqual(F.current_uncached(None, self.conn, NOW), (300_000, "store"))


class StoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.conn = db.connect(self.home)
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "CREATE TABLE proxy_minutes (minute TEXT, model TEXT, input INT, cache_read INT, output INT, r429 INT,"
            " PRIMARY KEY (minute, model))"
        )
        self.conn.execute(
            "CREATE TABLE proxy_jobs (job TEXT, model TEXT, req INT, input INT, first_ts REAL, last_ts REAL,"
            " PRIMARY KEY (job, model))"
        )

    def minute(self, ts, input_, cache_read, output, r429=0, model=DS_MODEL):
        self.conn.execute("INSERT INTO proxy_minutes VALUES (?,?,?,?,?,?)",
                          (stamp(ts), model, input_, cache_read, output, r429))

    def test_minute_rows_sum_billed_and_skip_other_models(self):
        self.minute(LAST_MINUTE, 100_000, 50_000, 20_000)
        self.minute(LAST_MINUTE, 1_000_000, 0, 0, model="morph-kimik31")
        rows = F.minute_rows(self.conn, NOW, 5)
        self.assertEqual(rows[stamp(LAST_MINUTE)], {"input": 100_000, "billed": 170_000})

    def test_current_uncached_prefers_the_live_proxy_budget_then_the_current_minute_row(self):
        status = {"budgets": {"uncached_ds": {"used_60s": 350_000}}}
        self.assertEqual(F.current_uncached(status, self.conn, NOW), (350_000, "proxy"))
        self.minute(int(NOW // 60) * 60, 120_000, 0, 0)
        self.assertEqual(F.current_uncached(None, self.conn, NOW), (120_000, "store"))

    def test_pending_launches_are_running_ds_jobs_with_no_proxy_row_yet(self):
        self.insert_job("seen", "running", "normal", "pi-deepseek-flash", "x")
        self.insert_job("unseen", "running", "normal", "pi-deepseek-flash", "y")
        self.insert_job("old", "running", "normal", "pi-deepseek-flash", "z")
        self.conn.execute("UPDATE jobs SET started_at = ? WHERE id IN ('seen', 'unseen')", (NOW - 10,))
        self.conn.execute("UPDATE jobs SET started_at = ? WHERE id = 'old'", (NOW - 3600,))
        self.conn.execute("INSERT INTO proxy_jobs VALUES ('seen', ?, 1, 5, ?, ?)", (DS_MODEL, NOW - 9, NOW - 9))
        self.assertEqual(F.pending_launches(self.conn, NOW, 120), 1)

    def test_pending_launches_without_a_proxy_table_count_every_recent_start(self):
        self.conn.execute("DROP TABLE proxy_jobs")
        self.insert_job("a", "running", "normal", "pi-deepseek-flash", "x")
        self.conn.execute("UPDATE jobs SET started_at = ? WHERE id = 'a'", (NOW - 5,))
        self.assertEqual(F.pending_launches(self.conn, NOW, 120), 1)

    def test_recent_429_counts_the_overlapping_minutes(self):
        self.minute(NOW - 30, 0, 0, 0, r429=2)
        self.minute(NOW - 600, 0, 0, 0, r429=9)
        self.assertEqual(F.recent_429(self.conn, NOW, 120), 2)

    def test_job_counts_split_real_filler_and_running(self):
        self.insert_job("a", "queued", "normal", "pi-deepseek-flash", "hub-exec-x")
        self.insert_job("b", "queued", "backlog", "pi-deepseek-flash", "hub-exec-backlog-filler-r-m")
        self.insert_job("c", "running", "backlog", "pi-deepseek-flash", "hub-exec-backlog-filler-r-n")
        self.insert_job("d", "running", "normal", "pi-kimi", "hub-exec-k")
        counts = F.job_counts(self.conn, NOW)
        self.assertEqual(counts, {"running_ds": 1, "ready_real": 1, "queued_filler": 1, "running_filler": 1})

    def insert_job(self, job_id, state, lane, tier, label):
        db.insert_job(self.conn, {
            "id": job_id, "lane": lane, "kind": "read", "res": "light", "tier": tier, "label": label,
            "workspace": "/tmp/ws", "session": "backlog", "harness": "backlog", "prompt_path": "/tmp/p",
        }, NOW)
        self.conn.execute("UPDATE jobs SET state = ? WHERE id = ?", (state, job_id))

    def test_measured_first_request_is_a_high_percentile_of_single_request_jobs(self):
        pacing = F.section({"min_measured_jobs": 4}, F.DEFAULT_PACING)
        self.assertIsNone(F.measured_first_request(self.conn, NOW, pacing))
        for i, value in enumerate((20_000, 30_000, 60_000, 90_000)):
            self.conn.execute("INSERT INTO proxy_jobs VALUES (?,?,?,?,?,?)",
                              (f"j{i}", DS_MODEL, 1, value, NOW - 10, NOW - 5))
        self.assertEqual(F.measured_first_request(self.conn, NOW, pacing), 60_000)

    def test_decide_reads_the_store_and_plans_a_fill(self):
        self.minute(LAST_MINUTE, 100_000, 0, 0)
        status = {"budgets": {"uncached_ds": {"used_60s": 100_000}}}
        decision = F.decide(
            self.conn, NOW, controller=F.FillController(), fill=FILL, pacing=PACING, status=status, ps_text="",
            laptop=CALM, laptop_cfg=LAPTOP, paused=False, measured=None,
        )
        self.assertEqual((decision.plan.submit, decision.plan.key), (4, "stepping"))
        self.assertEqual(decision.billed_avg, 20_000)
        self.assertEqual(decision.direct_ds, 0)
        self.assertEqual(decision.pacing_current, 100_000)
        self.assertEqual(decision.pacing_source, "proxy")
        self.assertEqual(decision.pacing_left, 5)


class FillerTargetTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.ws = self.tmp / "repo"
        for module in ("pkg/a", "pkg/b", "tools/c", "pkg/__pycache__"):
            (self.ws / module).mkdir(parents=True)
        self.home = self.tmp / "home"
        self.conn = db.connect(self.home)
        self.addCleanup(self.conn.close)

    def config(self, **repo):
        return {"filler": {"report_dir": str(self.tmp / "reports"), "max_runtime": "10m",
                           "repos": {"alpha": {"workspace": str(self.ws), **repo}}}}

    def test_roots_expand_to_module_folders_and_skip_caches(self):
        targets, max_runtime = Fl.load_targets(self.config(roots=["pkg", "tools"]))
        self.assertEqual([t.module for t in targets], ["pkg/a", "pkg/b", "tools/c"])
        self.assertEqual(max_runtime, 600)

    def test_explicit_modules_are_used_as_given(self):
        targets, _ = Fl.load_targets(self.config(modules=["tools/c"]))
        self.assertEqual([t.key for t in targets], ["alpha/tools/c"])

    def test_no_filler_key_means_no_targets(self):
        self.assertEqual(Fl.load_targets({})[0], [])

    def test_a_malformed_section_is_refused(self):
        with self.assertRaises(ValueError):
            Fl.load_targets(self.config())
        with self.assertRaises(ValueError):
            Fl.load_targets({"filler": {"repos": {"x": {"workspace": str(self.ws), "roots": ["pkg"]}}}})

    def test_report_path_and_labels_follow_the_spec(self):
        target = Fl.load_targets(self.config(modules=["pkg/a"]))[0][0]
        self.assertEqual(target.report_path, str(self.tmp.resolve() / "reports" / "filler" / "alpha" / "pkg-a.md"))
        self.assertEqual(Fl.label_slug(target), "backlog-filler-alpha-pkg-a")
        self.assertTrue(Fl.is_filler_label("hub-exec-backlog-filler-alpha-pkg-a"))
        self.assertFalse(Fl.is_filler_label("hub-exec-backlog-nightly"))

    def test_rotation_skips_unchanged_modules_and_open_jobs(self):
        targets, _ = Fl.load_targets(self.config(modules=["pkg/a", "pkg/b"]))
        heads = {"alpha/pkg/a": "c1", "alpha/pkg/b": "c2"}
        head_of = lambda t: heads[t.key]  # noqa: E731
        first = Fl.next_target(self.conn, targets, head_of)
        self.assertEqual((first[0].key, first[1]), ("alpha/pkg/a", "c1"))
        Fl.record_submission(self.conn, first[0], "c1", "job-a", NOW)
        second = Fl.next_target(self.conn, targets, head_of)
        self.assertEqual(second[0].key, "alpha/pkg/b")
        Fl.record_submission(self.conn, second[0], "c2", "job-b", NOW)
        self.assertIsNone(Fl.next_target(self.conn, targets, head_of))
        self.assertEqual(Fl.state_get(self.conn, "cursor"), "alpha/pkg/b")

    def test_a_target_with_a_queued_job_is_not_picked_again(self):
        targets, _ = Fl.load_targets(self.config(modules=["pkg/a"]))
        head_of = lambda t: "c1"  # noqa: E731
        Fl.record_submission(self.conn, targets[0], "c1", "job-a", NOW)
        db.insert_job(self.conn, {
            "id": "job-a", "lane": "backlog", "kind": "read", "res": "light", "tier": "pi-deepseek-flash",
            "label": "x", "workspace": "/tmp", "session": "backlog", "harness": "backlog", "prompt_path": "/tmp/p",
        }, NOW)
        self.assertIsNone(Fl.next_target(self.conn, targets, head_of))

    def test_an_unknown_head_is_skipped(self):
        targets, _ = Fl.load_targets(self.config(modules=["pkg/a"]))
        self.assertIsNone(Fl.next_target(self.conn, targets, lambda t: None))

    def test_the_prompt_carries_its_placeholders_filled(self):
        target = Fl.load_targets(self.config(modules=["pkg/a"]))[0][0]
        text = Fl.prompt_body(target, "abc123")
        for placeholder in ("{module}", "{repo}", "{workspace}", "{report_path}", "{commit}"):
            self.assertNotIn(placeholder, text)
        self.assertIn("abc123", text)
        self.assertIn("VERDICT:", text)

    def test_pause_flag_round_trips(self):
        self.assertFalse(Fl.paused(self.conn))
        Fl.set_paused(self.conn, True)
        self.assertTrue(Fl.paused(self.conn))


class BrokerWiringTest(unittest.TestCase):
    """The broker submits and holds filler on a real store. The process table, git and the proxy are stubbed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.ws = self.tmp / "repo"
        for module in ("pkg/a", "pkg/b"):
            (self.ws / module).mkdir(parents=True)
        self.prompt = self.tmp / "brief.md"
        self.prompt.write_text("Look and report.\n", encoding="utf-8")
        self.home.mkdir()
        self.env = {"AGENTQ_HOME": str(self.home), "AGENTQ_SESSION": "sess-a", "PATH": os.environ["PATH"]}
        self.write_json("broker.json", {"fill": {"enabled": True}, "pacing": {"enabled": False}})
        self.write_json("backlog-seed.json", {"filler": {
            "report_dir": str(self.tmp / "reports"),
            "repos": {"alpha": {"workspace": str(self.ws), "modules": ["pkg/a", "pkg/b"]}},
        }})
        self.conn = db.connect(self.home)
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "CREATE TABLE proxy_minutes (minute TEXT, model TEXT, input INT, cache_read INT, output INT, r429 INT,"
            " PRIMARY KEY (minute, model))"
        )
        for k in range(1, 6):
            self.conn.execute("INSERT INTO proxy_minutes VALUES (?,?,?,?,?,?)",
                              (stamp(NOW - 60 * k), DS_MODEL, 0, 0, 0, 0))
        self.ps = ""
        self.status = status_doc()
        self.labeler = lambda kind, slug: f"hub-exec-{slug}"

    def write_json(self, name, data):
        (self.home / name).write_text(json.dumps(data), encoding="utf-8")

    def set_billed(self, per_minute):
        self.conn.execute("UPDATE proxy_minutes SET cache_read = ?", (per_minute,))

    def broker(self):
        gauges = mock.Mock(read=lambda: H.Headroom(90.0, 0.1, 0.1, None))
        broker = B.Broker(self.home, launcher=mock.Mock(), now=lambda: NOW, proxy=lambda: self.status,
                          gauges=gauges, launch_pool=NeverPool(), labeler=self.labeler)
        return broker

    def tick(self, broker):
        with mock.patch.object(F, "process_table", return_value=self.ps), \
                mock.patch.object(Fl, "module_head", side_effect=lambda t: "c-" + t.module.replace("/", "-")):
            return broker.tick()

    def log_text(self):
        path = self.home / "logs" / "broker.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def deepseek_jobs(self):
        return [job for job in db.select_jobs(self.conn) if job["tier"] == "pi-deepseek-flash"]

    def filler_jobs(self):
        return [job for job in self.deepseek_jobs() if Fl.is_filler_label(job["label"])]

    def test_a_low_average_submits_filler_one_per_target(self):
        self.set_billed(1_000_000)
        self.tick(self.broker())
        labels = sorted(job["label"] for job in self.filler_jobs())
        self.assertEqual(labels, ["hub-exec-backlog-filler-alpha-pkg-a", "hub-exec-backlog-filler-alpha-pkg-b"])
        job = self.filler_jobs()[0]
        self.assertEqual((job["lane"], job["kind"], job["res"], job["session"]), ("backlog", "read", "light", "backlog"))
        self.assertIn("filler alpha/pkg/a submitted", self.log_text())

    def test_fill_disabled_submits_nothing_and_reads_no_process_table(self):
        self.write_json("broker.json", {"fill": {"enabled": False}})
        self.set_billed(1_000_000)
        with mock.patch.object(F, "process_table") as table:
            self.tick(self.broker())
            table.assert_not_called()
        self.assertEqual(self.filler_jobs(), [])

    def test_ready_real_work_holds_filler_even_when_it_cannot_launch(self):
        self.set_billed(1_000_000)
        self.write_json("broker.json", {"fill": {"enabled": True}, "pacing": {"enabled": False},
                                        "tiers": {"pi-deepseek-flash": 0}})
        db.insert_job(self.conn, {
            "id": "real", "lane": "normal", "kind": "read", "res": "light", "tier": "pi-deepseek-flash",
            "label": "hub-exec-real", "workspace": str(self.ws), "session": "sess-a", "harness": "pi",
            "prompt_path": str(self.prompt),
        }, NOW)
        self.tick(self.broker())
        self.assertEqual(self.filler_jobs(), [])
        self.assertEqual(db.get_job(self.conn, "real")["state"], "queued")

    def test_a_deepseek_gate_keeps_filler_out(self):
        self.set_billed(1_000_000)
        self.status = status_doc(blocked=3)
        self.tick(self.broker())
        self.assertEqual(self.filler_jobs(), [])

    def test_pause_stops_filler_and_holds_the_queued_filler(self):
        self.set_billed(1_000_000)
        broker = self.broker()
        self.tick(broker)
        queued = len(self.filler_jobs())
        self.assertGreater(queued, 0)
        out = io.StringIO()
        code = cli.main(["backlog", "pause"], env=self.env, out=out, now=lambda: NOW)
        self.assertEqual(code, 0)
        self.assertIn("paused all", out.getvalue())
        self.tick(broker)
        self.assertEqual(len(self.filler_jobs()), queued)
        for job in self.filler_jobs():
            self.assertEqual(job["state"], "queued")
            self.assertIn("filler paused", db.get_job(self.conn, job["id"])["detail"])

    def test_the_fill_decision_is_logged_once_per_change_of_reason(self):
        self.set_billed(1_000_000)
        broker = self.broker()
        self.tick(broker)
        self.tick(broker)
        self.tick(broker)
        self.assertEqual(self.log_text().count("fill: "), 2)

    def test_uncached_pacing_holds_a_deepseek_launch_and_lets_one_urgent_through(self):
        self.write_json("broker.json", {"fill": {"enabled": False}, "pacing": {"enabled": True}})
        self.status = {"budgets": {"uncached_ds": {"used_60s": 390_000}}}
        for job_id, lane in (("normal-1", "normal"), ("urgent-1", "urgent")):
            db.insert_job(self.conn, {
                "id": job_id, "lane": lane, "kind": "read", "res": "light", "tier": "pi-deepseek-flash",
                "label": f"hub-exec-{job_id}", "workspace": str(self.ws), "session": "sess-a", "harness": "pi",
                "prompt_path": str(self.prompt),
            }, NOW)
        broker = self.broker()
        self.tick(broker)
        self.assertIn("urgent-1", broker._inflight)
        self.assertNotIn("normal-1", broker._inflight)
        normal = db.get_job(self.conn, "normal-1")
        self.assertEqual(normal["state"], "queued")
        self.assertIn("proxy uncached pacing", normal["detail"])
        self.assertIn("390,000 uncached this minute (proxy)", normal["detail"])


class ProxyDownBrokerTest(BrokerWiringTest):
    def test_with_no_proxy_status_filler_is_held_and_the_log_says_why(self):
        self.set_billed(1_000_000)
        self.status = None
        self.tick(self.broker())
        self.assertEqual(self.filler_jobs(), [])
        self.assertIn("proxy status unavailable", self.log_text())

    def test_unreachable_proxy_keeps_one_urgent_launch_and_paces_normal_jobs_on_store_numbers(self):
        self.write_json("broker.json", {"fill": {"enabled": False}, "pacing": {"enabled": True}})
        self.status = None
        self.conn.execute("INSERT INTO proxy_minutes VALUES (?,?,?,?,?,?)",
                          (stamp(NOW), DS_MODEL, 390_000, 0, 0, 0))
        for job_id, lane in (("normal-1", "normal"), ("urgent-1", "urgent")):
            db.insert_job(self.conn, {
                "id": job_id, "lane": lane, "kind": "read", "res": "light", "tier": "pi-deepseek-flash",
                "label": f"hub-exec-{job_id}", "workspace": str(self.ws), "session": "sess-a", "harness": "pi",
                "prompt_path": str(self.prompt),
            }, NOW)
        broker = self.broker()
        self.tick(broker)
        self.assertIn("urgent-1", broker._inflight)
        self.assertNotIn("normal-1", broker._inflight)
        normal = db.get_job(self.conn, "normal-1")
        self.assertIn("proxy uncached pacing", normal["detail"])
        self.assertIn("390,000 uncached this minute (store)", normal["detail"])


class NeverPool:
    def submit(self, fn, *args):
        return concurrent.futures.Future()


if __name__ == "__main__":
    unittest.main()


class InflightFillGateTest(unittest.TestCase):
    """Filler waits while DeepSeek is at the in-flight limit and the limit is below its maximum."""

    def status(self, **fields) -> dict:
        base = {"inflight_limit": 28, "inflight_max": 96, "inflight_enforcing": True,
                "inflight_queued": 0, "inflight_in_flight": 28, "inflight_mode": "enforce"}
        return base | fields

    def test_enforced_limit_holding_deepseek_requests_holds_filler(self) -> None:
        self.assertEqual(F.inflight_gate(self.status(inflight_queued=3)), "Morph slow: in-flight limit 28 reached")

    def test_enforced_limit_with_nothing_held_does_not_hold_filler(self) -> None:
        self.assertIsNone(F.inflight_gate(self.status(inflight_queued=0)))

    def test_shadow_limit_is_named_as_would_be_when_deepseek_is_at_it(self) -> None:
        reason = F.inflight_gate(self.status(inflight_enforcing=False, inflight_mode="shadow", inflight_queued=0))
        self.assertEqual(reason, "Morph slow: in-flight limit 28 reached (shadow: would limit to 28)")

    def test_shadow_below_the_limit_does_not_hold_filler(self) -> None:
        self.assertIsNone(F.inflight_gate(self.status(inflight_enforcing=False, inflight_in_flight=10)))

    def test_a_limit_at_its_maximum_never_holds_filler_here(self) -> None:
        self.assertIsNone(F.inflight_gate(self.status(inflight_limit=96, inflight_queued=4)))

    def test_no_limit_means_no_gate(self) -> None:
        self.assertIsNone(F.inflight_gate(self.status(inflight_limit=None)))

    def test_gate_reason_reports_the_limit_after_the_429_check(self) -> None:
        calm = H.Headroom(free_pct=60.0, cpu_busy=0.3, swap_frac=0.1, load_per_cpu=0.1)
        laptop_cfg = {"fill_max_cpu_busy": 0.9, "fill_min_free_pct": 10.0, "fill_max_swap_frac": 0.9}
        status = self.status(inflight_queued=2, budgets={"total": {"blocked_by_family": {}, "queued": {}}})
        self.assertEqual(F.gate_reason(status, 0, calm, laptop_cfg, F.DEFAULT_FILL), "Morph slow: in-flight limit 28 reached")
        self.assertEqual(
            F.gate_reason(status, 3, calm, laptop_cfg, F.DEFAULT_FILL),
            "Morph gate: 3 DeepSeek 429s in the last 120 s",
        )
