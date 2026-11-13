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
from collections import deque
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


class RateAndTargetTests(unittest.TestCase):
    def test_per_worker_rate_is_billed_over_active_and_clamped(self):
        self.assertEqual(F.per_worker_rate(9_000_000, 50, FILL), (180_000.0, "measured"))
        self.assertEqual(F.per_worker_rate(1_000_000, 50, FILL), (100_000.0, "measured"))
        self.assertEqual(F.per_worker_rate(50_000_000, 50, FILL), (250_000.0, "measured"))

    def test_no_active_history_uses_the_default_rate(self):
        self.assertEqual(F.per_worker_rate(9_000_000, None, FILL), (175_000.0, "default"))
        self.assertEqual(F.per_worker_rate(9_000_000, 0, FILL), (175_000.0, "default"))

    def test_desired_workers_rounds_up_and_respects_the_cap(self):
        self.assertEqual(F.desired_workers(8_000_000, 175_000, 64), 46)
        self.assertEqual(F.desired_workers(8_000_000, 100_000, 64), 64)

    def test_hysteresis_fills_below_low_stops_above_high_and_holds_between(self):
        target, low, high = 8_000_000, 0.9, 0.97
        self.assertTrue(F.filling_state(False, 7_000_000, target, low, high))
        self.assertFalse(F.filling_state(True, 7_900_000, target, low, high))
        self.assertTrue(F.filling_state(True, 7_500_000, target, low, high))
        self.assertFalse(F.filling_state(False, 7_500_000, target, low, high))


class PlanTests(unittest.TestCase):
    def plan(self, **overrides):
        args = dict(enabled=True, paused=False, avg_billed=5_000_000, was_filling=False, fill=FILL,
                    rate=175_000, running_ds=10, ready_real=0, queued_filler=0, gate=None)
        args.update(overrides)
        return F.plan_fill(**args)

    def test_low_average_submits_up_to_the_per_tick_limit(self):
        plan = self.plan()
        self.assertTrue(plan.filling)
        self.assertEqual(plan.submit, FILL["max_launch_per_tick"])
        self.assertEqual(plan.key, "submitting")

    def test_queued_filler_counts_against_the_gap(self):
        self.assertEqual(self.plan(queued_filler=40).submit, 0)
        self.assertEqual(self.plan(queued_filler=40).key, "queued-covers")

    def test_ready_real_work_goes_first(self):
        plan = self.plan(ready_real=2)
        self.assertEqual((plan.submit, plan.key), (0, "real-ready"))

    def test_each_gate_holds_the_filler(self):
        plan = self.plan(gate="Morph gate: 3 DeepSeek requests waiting at the proxy")
        self.assertEqual((plan.submit, plan.key), (0, "gate"))

    def test_disabled_and_paused_never_fill(self):
        self.assertEqual(self.plan(enabled=False).key, "disabled")
        self.assertFalse(self.plan(paused=True).filling)
        self.assertEqual(self.plan(paused=True).submit, 0)

    def test_above_the_high_mark_the_filler_stops(self):
        plan = self.plan(avg_billed=8_000_000, was_filling=True)
        self.assertFalse(plan.filling)
        self.assertEqual(plan.key, "high-mark")

    def test_a_full_gap_submits_nothing(self):
        self.assertEqual(self.plan(running_ds=60, avg_billed=7_000_000).submit, 0)


class GateTests(unittest.TestCase):
    def test_no_proxy_status_holds(self):
        self.assertIsNotNone(F.gate_reason(None, 0, (90.0, 0.1), FILL))

    def test_blocked_deepseek_requests_hold(self):
        self.assertIn("waiting at the proxy", F.gate_reason(status_doc(blocked=2), 0, (90.0, 0.1), FILL))

    def test_a_recent_deepseek_429_holds(self):
        self.assertIn("429", F.gate_reason(status_doc(), 1, (90.0, 0.1), FILL))

    def test_laptop_memory_and_load_hold_but_unreadable_gauges_do_not(self):
        self.assertIn("memory", F.gate_reason(status_doc(), 0, (10.0, 0.1), FILL))
        self.assertIn("load", F.gate_reason(status_doc(), 0, (90.0, 1.5), FILL))
        self.assertIsNone(F.gate_reason(status_doc(), 0, (None, None), FILL))

    def test_clear_gates_are_open(self):
        self.assertIsNone(F.gate_reason(status_doc(), 0, (90.0, 0.2), FILL))

    def test_ds_blocked_falls_back_to_the_queue_for_an_old_proxy(self):
        old = {"budgets": {"total": {"queued": {"urgent": 0, "normal": 2, "backlog": 1}}}}
        self.assertEqual(F.ds_blocked(old), 3)


class PacingTests(unittest.TestCase):
    def test_the_default_launch_cost_is_40k_against_80_percent_of_400k(self):
        self.assertEqual(F.pacing_allowance(0, 0, PACING, None), (8, 40_000))
        self.assertEqual(F.pacing_allowance(300_000, 0, PACING, None), (0, 40_000))

    def test_recent_launches_each_take_their_cost_out_of_the_allowance(self):
        self.assertEqual(F.pacing_allowance(0, 3, PACING, None), (5, 40_000))

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

    def test_active_average_keeps_only_the_window(self):
        samples = deque()
        F.active_average(samples, 0.0, 10, 300)
        self.assertEqual(F.active_average(samples, 400.0, 20, 300), 20)


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
        self.assertEqual(F.last_minute_input(self.conn, NOW), 100_000)

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
        samples = deque()
        decision = F.decide(
            self.conn, NOW, fill=FILL, pacing=PACING, status=status_doc(), ps_text="",
            laptop=(90.0, 0.2), was_filling=False, samples=samples, recent_launches=0, measured=None, paused=False,
        )
        self.assertEqual(decision.plan.submit, FILL["max_launch_per_tick"])
        self.assertEqual(decision.billed_avg, 20_000)
        self.assertEqual(decision.direct_ds, 0)
        self.assertEqual(decision.last_input, 100_000)
        self.assertEqual(decision.rate_source, "default")
        self.assertEqual(len(samples), 1)


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
        return B.Broker(self.home, launcher=mock.Mock(), now=lambda: NOW, proxy=lambda: self.status,
                        gauges=gauges, launch_pool=NeverPool(), labeler=self.labeler)

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
        self.assertEqual(self.log_text().count("fill: "), 1)

    def test_uncached_pacing_holds_a_deepseek_launch_and_lets_one_urgent_through(self):
        self.write_json("broker.json", {"fill": {"enabled": False}, "pacing": {"enabled": True}})
        self.conn.execute("UPDATE proxy_minutes SET input = 390000 WHERE minute = ?", (stamp(NOW - 60),))
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


class NeverPool:
    def submit(self, fn, *args):
        return concurrent.futures.Future()


if __name__ == "__main__":
    unittest.main()
