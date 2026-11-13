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
    return ctl.tick(now, fill=fill, paused=paused, billed_avg=billed, active=active, real_running=real,
                    ready_real=ready_real, queued_filler=queued_filler, gate=gate)


def seeded(per_worker=175_000.0, active=8, ceiling=None, now=NOW):
    """A controller whose long-run rate is known: three earlier samples at ``per_worker`` billed per busy worker."""
    ctl = F.FillController(ceiling=ceiling)
    for k in range(1, 4):
        ctl.samples.append((now - 60 * k, per_worker * active, active))
    return ctl


class ControllerTests(unittest.TestCase):
    def test_disabled_and_paused_never_step(self):
        ctl = seeded()
        self.assertEqual(tick(ctl, NOW, 1e6, 10, fill=dict(FILL, enabled=False)).key, "disabled")
        self.assertEqual(tick(ctl, NOW, 1e6, 10, paused=True).key, "paused")
        self.assertIsNone(ctl.step)

    def test_an_unknown_rate_holds_everything(self):
        ctl = F.FillController()
        plan = tick(ctl, NOW, 1_000_000, 8)
        self.assertEqual((plan.submit, plan.key), (0, "rate-unknown"))
        self.assertIsNone(ctl.step)

    def test_zero_billed_ticks_with_four_busy_workers_never_grow_or_clear_a_ceiling(self):
        ctl = F.FillController(ceiling=(47, NOW))
        now = NOW
        for _ in range(int(1200 / 2)):
            plan = tick(ctl, now, 0.0, 4)
            self.assertEqual((plan.submit, plan.key), (0, "rate-unknown"))
            now += 2
        self.assertEqual(ctl.last_ceiling, 47)
        self.assertEqual(ctl.ceiling, (47, NOW))
        self.assertIsNone(ctl.step)
        self.assertIsNone(ctl.judgement)

    def test_a_low_average_starts_one_probe_of_step_workers(self):
        ctl = seeded()
        plan = tick(ctl, NOW, 1_400_000, 8)
        self.assertEqual((plan.submit, plan.key), (4, "stepping"))
        self.assertEqual(tick(ctl, NOW + 2, 1_400_000, 8).key, "step-wait")
        self.assertEqual(tick(ctl, NOW + 2, 1_400_000, 8).submit, 0)

    def test_real_work_and_gates_hold_before_a_step_starts(self):
        self.assertEqual(tick(seeded(), NOW, 1e6, 10, ready_real=2).key, "real-ready")
        self.assertEqual(tick(seeded(), NOW, 1e6, 10, gate="Morph gate: x").key, "gate")

    def test_above_the_high_mark_it_stops_and_does_not_kill(self):
        plan = tick(seeded(), NOW, 8_000_000, 60)
        self.assertEqual((plan.filling, plan.submit, plan.key), (False, 0, "high-mark"))

    def test_the_worker_cap_stops_a_probe(self):
        self.assertEqual(tick(seeded(active=64), NOW, 1e6, 64).key, "at-limit")

    def test_a_probe_that_starts_no_worker_is_void_and_records_nothing(self):
        ctl = seeded(active=47, per_worker=100_000.0)
        tick(ctl, NOW, 4_700_000, 47)
        plan = tick(ctl, NOW + 301, 4_700_000, 47)
        self.assertIsNone(ctl.new_ceiling)
        self.assertIn("void", ctl.judgement)
        self.assertEqual(plan.key, "stepping")

    def test_the_47_worker_scenario_does_not_walk_to_64(self):
        ctl = seeded(active=47, per_worker=100_000.0)
        active, now, peak = 47, NOW, 47
        for _ in range(int(3 * 3600 / 2)):
            billed = 4_700_000 + 25_000 * (active - 47)
            plan = tick(ctl, now, billed, active)
            active += plan.submit
            peak = max(peak, active)
            now += 2
        self.assertLessEqual(peak, 51)
        self.assertEqual(ctl.last_ceiling, 47)
        self.assertIn("poor", ctl.judgement)

    def test_one_worker_completions_and_poor_refills_keep_the_ceiling_at_47(self):
        ctl = seeded(active=47, per_worker=100_000.0, ceiling=(47, NOW))
        active, now = 47, NOW
        for cycle in range(int(1200 / 2)):
            if cycle % 10 == 9 and active > 0:
                active -= 1
            plan = tick(ctl, now, 4_700_000, active)
            self.assertIn(plan.key, ("refill", "at-limit"))
            active += plan.submit
            now += 2
        self.assertEqual(ctl.ceiling, (47, NOW))
        self.assertEqual(ctl.last_ceiling, 47)
        self.assertIsNone(ctl.judgement)
        self.assertEqual(active, 47)

    def test_a_real_job_count_is_a_floor_under_the_ceiling(self):
        ctl = seeded(active=50, per_worker=94_000.0, ceiling=(47, NOW))
        self.assertEqual(ctl.worker_limit(NOW, FILL, real_running=50), (50, "learned ceiling"))
        self.assertEqual(tick(ctl, NOW, 4_700_000, 50, real=50).key, "at-limit")

    def test_a_poor_probe_never_records_below_the_real_count(self):
        ctl = seeded(active=47, per_worker=100_000.0)
        tick(ctl, NOW, 4_700_000, 47, real=49)
        tick(ctl, NOW + 301, 4_700_000, 51, real=49)
        self.assertEqual(ctl.last_ceiling, 49)

    def test_good_gain_per_probe_reaches_the_target_band(self):
        ctl = seeded(active=8)
        active, now, billed = 8, NOW, 0.0
        for _ in range(int(2 * 3600 / 2)):
            billed = 175_000.0 * active
            plan = tick(ctl, now, billed, active)
            active += plan.submit
            now += 2
        self.assertGreaterEqual(billed, 0.9 * 8_000_000)
        self.assertIsNone(ctl.new_ceiling)
        self.assertIsNone(ctl.last_ceiling)

    def test_an_expired_ceiling_allows_one_probe_past_it_and_no_more(self):
        ctl = seeded(active=47, per_worker=100_000.0, ceiling=(47, NOW))
        plans = []
        now = NOW + 2
        while now < NOW + 1800 + 1200:
            plans.append(tick(ctl, now, 4_700_000, 47 + sum(p.submit for p in plans if p.key == "stepping")))
            now += 2
        probes = [p for p in plans if p.key == "stepping"]
        self.assertEqual([p.submit for p in probes], [4])
        self.assertEqual(ctl.new_ceiling[0], 47)
        self.assertEqual(ctl.last_ceiling, 47)
        self.assertEqual(plans[-1].key, "at-limit")

    def test_a_saved_ceiling_survives_a_restart(self):
        ctl = seeded(active=47, per_worker=100_000.0, ceiling=(47, NOW - 60))
        self.assertEqual(tick(ctl, NOW, 4_700_000, 47).key, "at-limit")

    def test_the_rate_judging_a_step_ignores_samples_from_the_step_itself(self):
        ctl = F.FillController()
        for k in range(10):
            ctl.samples.append((NOW - 600 + k * 30, 4_700_000.0, 47))
        ctl.samples.append((NOW + 100, 9_000_000.0, 50))
        self.assertEqual(ctl.measured_rate(NOW + 400, NOW, FILL), 100_000)

    def test_a_rate_below_the_floor_counts_as_unknown(self):
        ctl = F.FillController()
        for k in range(1, 4):
            ctl.samples.append((NOW - 60 * k, 0.0, 4))
        self.assertIsNone(ctl.measured_rate(NOW, NOW, FILL))


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
            laptop=(90.0, 0.2), paused=False, measured=None,
        )
        self.assertEqual((decision.plan.submit, decision.plan.key), (0, "rate-unknown"))
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
        gauges = mock.Mock(read=lambda ttl: (90.0, 0.1))
        broker = B.Broker(self.home, launcher=mock.Mock(), now=lambda: NOW, proxy=lambda: self.status,
                          gauges=gauges, launch_pool=NeverPool(), labeler=self.labeler)
        broker._controller = F.FillController()
        for k in range(1, 4):
            broker._controller.samples.append((NOW - 60 * k, 175_000.0 * 4, 4))
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


class NeverPool:
    def submit(self, fn, *args):
        return concurrent.futures.Future()


if __name__ == "__main__":
    unittest.main()
