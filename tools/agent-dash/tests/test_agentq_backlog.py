"""agentq backlog templates: CLI round-trip, due logic, one job at a time, the Morph gate,
the label, the read-only preamble, the seed script and the status line.

Offline. AGENTQ_HOME is a temp folder in every test. The proxy status is a dict the
test sets, and the broker never launches a worker: the launch pool is a future that
never completes, so a submitted job stays queued or launching.
"""

import concurrent.futures
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentq import backlog as backlog_lib  # noqa: E402
from agentq import backlog_seed  # noqa: E402
from agentq import broker as B  # noqa: E402
from agentq import cli, db  # noqa: E402


class Clock:
    def __init__(self, t=100000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeLabeler:
    """Builds the same shape of label as the real helper, and records each call."""

    def __init__(self, refuse=False):
        self.refuse = refuse
        self.calls = []

    def __call__(self, kind, slug):
        self.calls.append((kind, slug))
        if self.refuse:
            return None
        mark = "review" if kind == "review" else "exec"
        return f"hub-{mark}-{slug}"


class NeverPool:
    """A launch pool whose launches never finish, so submitted jobs stay in the launch step."""

    def submit(self, fn, *args):
        return concurrent.futures.Future()


def proxy_doc(total=0, output=0, uncached=0):
    def lanes(n):
        return {"urgent": 0, "normal": n, "backlog": 0}

    return {
        "ok": True,
        "saturated": False,
        "budgets": {
            "total": {"queued": lanes(total)},
            "output": {"queued": lanes(output)},
            "uncached": {"queued": lanes(uncached)},
        },
    }


def idle():
    return proxy_doc()


class BacklogTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.workspace = self.tmp / "ws"
        self.workspace.mkdir()
        self.env = {"AGENTQ_HOME": str(self.home), "AGENTQ_SESSION": "sess-a", "PATH": os.environ["PATH"]}
        self.prompt = self.tmp / "brief.md"
        self.prompt.write_text("Look at the queue and report.\n", encoding="utf-8")
        self.clock = Clock()

    def run_cli(self, argv, now=None):
        out = io.StringIO()
        code = cli.main(argv, env=self.env, out=out, now=now or self.clock)
        return code, out.getvalue()

    def add_template(self, name="nightly", every="6h", *extra):
        code, text = self.run_cli(
            ["backlog", "add", name, str(self.prompt), "--every", every, "--workspace", str(self.workspace), *extra]
        )
        self.assertEqual(code, 0, text)
        return text

    def broker(self, labeler=None, pool=None, proxy=None):
        return B.Broker(self.home, launcher=mock.Mock(), now=self.clock, proxy=proxy or (lambda: None),
                        gauges=mock.Mock(read=lambda ttl: (90.0, 0.1)),
                        launch_pool=pool or NeverPool(), labeler=labeler or FakeLabeler())

    def conn(self):
        conn = db.connect(self.home)
        self.addCleanup(conn.close)
        return conn

    def jobs(self):
        return db.select_jobs(self.conn())

    def template(self, name="nightly"):
        return backlog_lib.get(self.conn(), name)


class DurationTests(unittest.TestCase):
    def test_parse_accepts_minutes_hours_days_and_seconds(self):
        self.assertEqual(backlog_lib.parse_duration("30m"), 1800)
        self.assertEqual(backlog_lib.parse_duration("6h"), 21600)
        self.assertEqual(backlog_lib.parse_duration("1d"), 86400)
        self.assertEqual(backlog_lib.parse_duration("90s"), 90)

    def test_parse_rejects_other_text_and_zero(self):
        for bad in ("6 hours", "soon", "0h", "", "-1h", "6w"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                backlog_lib.parse_duration(bad)

    def test_format_picks_the_largest_exact_unit(self):
        self.assertEqual(backlog_lib.format_duration(21600), "6h")
        self.assertEqual(backlog_lib.format_duration(1800), "30m")
        self.assertEqual(backlog_lib.format_duration(86400), "1d")
        self.assertEqual(backlog_lib.format_duration(90), "90s")


class DueTests(unittest.TestCase):
    def template(self, **fields):
        base = {"enabled": 1, "every_s": 3600.0, "last_submitted": None}
        return {**base, **fields}

    def test_a_template_that_never_ran_is_due(self):
        self.assertTrue(backlog_lib.is_due(self.template(), 500.0))

    def test_due_exactly_when_the_interval_has_passed(self):
        t = self.template(last_submitted=1000.0)
        self.assertFalse(backlog_lib.is_due(t, 1000.0 + 3599))
        self.assertTrue(backlog_lib.is_due(t, 1000.0 + 3600))

    def test_a_paused_template_is_never_due(self):
        self.assertFalse(backlog_lib.is_due(self.template(enabled=0), 500.0))

    def test_the_kind_default_tier_applies_when_no_tier_is_set(self):
        self.assertEqual(backlog_lib.tier_for({"tier": None, "kind": "read"}), "pi-deepseek-flash")
        self.assertEqual(backlog_lib.tier_for({"tier": None, "kind": "review"}), "pi-kimi")
        self.assertEqual(backlog_lib.tier_for({"tier": "x", "kind": "review"}), "x")


class TemplateCliTests(BacklogTestCase):
    def test_add_ls_pause_resume_rm_round_trip(self):
        text = self.add_template("nightly", "6h")
        self.assertIn("ANSWER: added backlog nightly: every 6h", text)
        copied = self.home / "backlog" / "nightly.md"
        self.assertEqual(copied.read_text(encoding="utf-8"), self.prompt.read_text(encoding="utf-8"))

        code, text = self.run_cli(["backlog", "ls"])
        self.assertEqual(code, 0)
        self.assertIn("nightly", text)
        self.assertIn("enabled", text)
        self.assertIn("ANSWER: 1 backlog template(s), 1 enabled", text)

        code, text = self.run_cli(["backlog", "pause", "nightly"])
        self.assertEqual((code, text.strip()), (0, "ANSWER: paused backlog nightly"))
        self.assertEqual(self.template()["enabled"], 0)
        code, text = self.run_cli(["backlog", "ls"])
        self.assertIn("paused", text)
        self.assertIn("next paused", text)

        code, text = self.run_cli(["backlog", "resume", "nightly"])
        self.assertEqual(code, 0)
        self.assertEqual(self.template()["enabled"], 1)

        code, text = self.run_cli(["backlog", "rm", "nightly"])
        self.assertEqual(code, 0)
        self.assertIn("past jobs stay", text)
        self.assertIsNone(self.template())
        self.assertFalse(copied.exists())
        code, text = self.run_cli(["backlog", "rm", "nightly"])
        self.assertEqual(code, 1)
        self.assertIn("no backlog template nightly", text)

    def test_add_takes_kind_resource_tier_and_max_runtime(self):
        self.add_template("reviewer", "1d", "--kind", "review", "--res", "tests", "--tier", "pi-kimi",
                          "--max-runtime", "2h")
        row = self.template("reviewer")
        self.assertEqual((row["kind"], row["res"], row["tier"]), ("review", "tests", "pi-kimi"))
        self.assertEqual(row["every_s"], 86400)
        self.assertEqual(row["max_runtime_s"], 7200)

    def test_bad_input_is_refused_with_a_reason(self):
        self.add_template("nightly", "6h")
        cases = {
            "Bad Name": ["backlog", "add", "Bad Name", str(self.prompt), "--every", "6h"],
            "soon": ["backlog", "add", "soonish", str(self.prompt), "--every", "soon"],
            "missing prompt": ["backlog", "add", "other", str(self.tmp / "nope.md"), "--every", "6h"],
            "duplicate": ["backlog", "add", "nightly", str(self.prompt), "--every", "6h"],
            "bad max-runtime": ["backlog", "add", "other", str(self.prompt), "--every", "6h", "--max-runtime", "x"],
        }
        for label, argv in cases.items():
            with self.subTest(label):
                code, text = self.run_cli(argv)
                self.assertEqual(code, 1)
                self.assertTrue(text.startswith("ANSWER: "), text)
        self.assertIsNone(self.template("other"))

    def test_pause_of_an_unknown_template_is_a_negative_fact(self):
        code, text = self.run_cli(["backlog", "pause", "ghost"])
        self.assertEqual(code, 1)
        self.assertIn("no backlog template ghost", text)


class SubmitTests(BacklogTestCase):
    def setUp(self):
        super().setUp()
        self.add_template("nightly", "6h")

    def test_a_due_template_submits_one_backlog_job(self):
        labeler = FakeLabeler()
        broker = self.broker(labeler=labeler)
        submitted = broker.submit_backlog(self.conn(), self.clock.t, idle())
        self.assertEqual(len(submitted), 1)
        job = self.jobs()[0]
        self.assertEqual(job["lane"], "backlog")
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["kind"], "read")
        self.assertEqual(job["tier"], "pi-deepseek-flash")
        self.assertEqual(job["workspace"], str(self.workspace))
        self.assertEqual((job["session"], job["harness"]), ("backlog", "backlog"))
        self.assertEqual(labeler.calls, [("read", "backlog-nightly")])
        self.assertEqual(self.template()["last_submitted"], self.clock.t)

    def test_the_label_comes_from_the_real_label_helper(self):
        broker = self.broker(labeler=B.label_for)
        broker.submit_backlog(self.conn(), self.clock.t, idle())
        job = self.jobs()[0]
        self.assertEqual(job["label"], B.label_for("read", "backlog-nightly"))
        self.assertIn(job["label"], backlog_lib.job_labels("nightly"))
        self.assertTrue(job["label"].startswith("hub-exec-backlog-nightly"))

    def test_a_review_template_takes_the_review_mark_and_tier(self):
        self.add_template("reviewer", "6h", "--kind", "review")
        self.broker().submit_backlog(self.conn(), self.clock.t, idle())
        by_label = {job["label"]: job for job in self.jobs()}
        self.assertIn("hub-review-backlog-reviewer", by_label)
        self.assertEqual(by_label["hub-review-backlog-reviewer"]["tier"], "pi-kimi")

    def test_the_read_preamble_is_prepended_to_the_brief(self):
        self.broker().submit_backlog(self.conn(), self.clock.t, idle())
        text = Path(self.jobs()[0]["prompt_path"]).read_text(encoding="utf-8")
        self.assertTrue(text.startswith(backlog_lib.PREAMBLE))
        self.assertIn("This is a recurring low-priority backlog job.", text)
        self.assertIn("do not open PRs or push branches", text)
        self.assertTrue(text.endswith("Look at the queue and report.\n"))

    def test_a_write_template_gets_no_preamble(self):
        self.add_template("writer", "6h", "--kind", "write")
        self.broker().submit_backlog(self.conn(), self.clock.t, idle())
        writer = [job for job in self.jobs() if job["label"].endswith("backlog-writer")][0]
        self.assertEqual(Path(writer["prompt_path"]).read_text(encoding="utf-8"), "Look at the queue and report.\n")

    def test_a_job_that_is_not_yet_due_is_not_resubmitted(self):
        broker = self.broker()
        broker.submit_backlog(self.conn(), self.clock.t, idle())
        self.clock.t += 6 * 3600 - 1
        self.assertEqual(broker.submit_backlog(self.conn(), self.clock.t, idle()), [])
        self.assertEqual(len(self.jobs()), 1)

    def test_one_instance_at_a_time_while_the_job_is_queued(self):
        broker = self.broker()
        broker.submit_backlog(self.conn(), self.clock.t, idle())
        self.clock.t += 6 * 3600 + 1
        self.assertEqual(broker.submit_backlog(self.conn(), self.clock.t, idle()), [])
        self.assertEqual(len(self.jobs()), 1)

    def test_one_instance_at_a_time_while_the_job_is_running(self):
        broker = self.broker()
        broker.submit_backlog(self.conn(), self.clock.t, idle())
        db.transition(self.conn(), self.jobs()[0]["id"], ("queued",), self.clock.t, "launch", "t",
                      state="running", started_at=self.clock.t)
        self.clock.t += 6 * 3600 + 1
        self.assertEqual(broker.submit_backlog(self.conn(), self.clock.t, idle()), [])

    def test_the_next_job_is_submitted_once_the_last_one_has_ended(self):
        broker = self.broker()
        broker.submit_backlog(self.conn(), self.clock.t, idle())
        job_id = self.jobs()[0]["id"]
        db.transition(self.conn(), job_id, ("queued",), self.clock.t, "finish", "verdict PASS",
                      state="done", verdict="PASS", finished_at=self.clock.t)
        self.clock.t += 6 * 3600
        self.assertEqual(len(broker.submit_backlog(self.conn(), self.clock.t, idle())), 1)
        self.assertEqual(len(self.jobs()), 2)

    def test_a_paused_template_submits_nothing(self):
        self.run_cli(["backlog", "pause", "nightly"])
        self.assertEqual(self.broker().submit_backlog(self.conn(), self.clock.t, idle()), [])
        self.assertEqual(self.jobs(), [])

    def test_a_queued_proxy_request_on_the_tier_budget_holds_the_submission(self):
        broker = self.broker()
        self.assertEqual(broker.submit_backlog(self.conn(), self.clock.t, proxy_doc(total=1)), [])
        self.assertEqual(broker.submit_backlog(self.conn(), self.clock.t, proxy_doc(output=1)), [])
        self.assertEqual(self.jobs(), [])
        self.assertEqual(len(broker.submit_backlog(self.conn(), self.clock.t, idle())), 1)

    def test_a_review_template_waits_on_the_uncached_budget_of_its_tier(self):
        self.add_template("reviewer", "6h", "--kind", "review")
        broker = self.broker()
        broker.submit_backlog(self.conn(), self.clock.t, proxy_doc(uncached=2))
        self.assertEqual([job["label"] for job in self.jobs()], ["hub-exec-backlog-nightly"])
        self.assertEqual(len(broker.submit_backlog(self.conn(), self.clock.t, idle())), 1)
        self.assertIn("hub-review-backlog-reviewer", [job["label"] for job in self.jobs()])

    def test_no_proxy_status_means_no_headroom_to_read(self):
        self.assertEqual(self.broker().submit_backlog(self.conn(), self.clock.t, None), [])
        self.assertEqual(self.jobs(), [])
        self.assertIsNone(self.template()["last_submitted"])

    def test_a_refused_label_is_retried_after_five_minutes_not_every_tick(self):
        labeler = FakeLabeler(refuse=True)
        broker = self.broker(labeler=labeler)
        self.assertEqual(broker.submit_backlog(self.conn(), self.clock.t, idle()), [])
        self.assertEqual(broker.submit_backlog(self.conn(), self.clock.t + 10, idle()), [])
        self.assertEqual(len(labeler.calls), 1)
        labeler.refuse = False
        self.clock.t += B.BACKLOG_RETRY_S
        self.assertEqual(len(broker.submit_backlog(self.conn(), self.clock.t, idle())), 1)
        self.assertEqual(len(labeler.calls), 2)

    def test_tick_runs_the_submission_after_the_launches(self):
        broker = self.broker(proxy=idle)
        with mock.patch.object(B, "write_heartbeat"):
            broker.tick()
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.jobs()[0]["lane"], "backlog")


class StatusTests(BacklogTestCase):
    def test_status_counts_templates_and_open_template_jobs(self):
        self.add_template("nightly", "6h")
        self.add_template("other", "1d")
        self.run_cli(["backlog", "pause", "other"])
        self.broker().submit_backlog(self.conn(), self.clock.t, idle())
        out = io.StringIO()
        with mock.patch.object(B, "proxy_status", return_value=None):
            cli.main(["status"], env=self.env, out=out, now=self.clock)
        text = out.getvalue()
        self.assertIn("backlog templates: 1 enabled of 2; template jobs open: 1", text)


class SchemaUpgradeTests(BacklogTestCase):
    def test_a_version_two_store_gains_the_template_table(self):
        conn = db.connect(self.home)
        conn.execute("DROP TABLE backlog_templates")
        conn.execute("PRAGMA user_version = 2")
        conn.close()
        conn = db.connect(self.home)
        self.addCleanup(conn.close)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertIn("backlog_templates", tables)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_a_fresh_store_has_the_template_table_and_no_template(self):
        conn = db.connect(self.home)
        self.addCleanup(conn.close)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(backlog_templates)")}
        self.assertEqual(columns, {
            "name", "prompt_path", "kind", "res", "tier", "workspace", "every_s",
            "max_runtime_s", "enabled", "last_submitted", "created_at",
        })  # fmt: skip
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM backlog_templates").fetchone()[0], 0)

    def test_an_upgraded_store_keeps_its_jobs_and_its_templates_are_empty(self):
        self.run_cli(["submit", str(self.prompt), "--label", "hub-exec-kept", "--workspace", str(self.workspace)])
        conn = db.connect(self.home)
        conn.execute("DROP TABLE backlog_templates")
        conn.execute("PRAGMA user_version = 2")
        conn.close()
        self.assertEqual([job["label"] for job in self.jobs()], ["hub-exec-kept"])
        self.assertEqual(backlog_lib.select_all(self.conn()), [])


class SeedTests(BacklogTestCase):
    def setUp(self):
        super().setUp()
        self.config = self.tmp / "seed.json"
        self.write_config(self.config)

    def write_config(self, path, **overrides):
        import json

        data = {
            "merged-review-lor": {"workspace": str(self.tmp / "repo-a"), "report_dir": str(self.tmp / "state-a")},
            "merged-review-ws": {"workspace": str(self.tmp / "repo-b"), "report_dir": str(self.tmp / "state-b")},
        }
        data.update(overrides)
        Path(path).write_text(json.dumps(data), encoding="utf-8")

    def seed_main(self, config=None):
        out = io.StringIO()
        argv = ["--home", str(self.home), "--config", str(config or self.config)]
        code = backlog_seed.main(argv, env={}, out=out, now=lambda: 5000.0)
        return code, out.getvalue()

    def test_the_seed_creates_the_two_review_templates_with_their_folders(self):
        code, text = self.seed_main()
        self.assertEqual(code, 0)
        self.assertIn("ANSWER: 2 template(s) created, 0 already present", text)
        conn = self.conn()
        self.assertEqual([t["name"] for t in backlog_lib.select_all(conn)], ["merged-review-lor", "merged-review-ws"])
        for template in backlog_lib.select_all(conn):
            self.assertEqual(template["kind"], "read")
            self.assertEqual(template["every_s"], 6 * 3600)
            body = Path(template["prompt_path"]).read_text(encoding="utf-8")
            self.assertNotIn("{workspace}", body)
            self.assertNotIn("{report_dir}", body)
            self.assertIn("read-only", body)
            self.assertIn("VERDICT: PASS", body)
        lor = backlog_lib.get(conn, "merged-review-lor")
        self.assertEqual(lor["workspace"], str(self.tmp / "repo-a"))
        lor_body = Path(lor["prompt_path"]).read_text(encoding="utf-8")
        self.assertIn(f"git -C {self.tmp / 'repo-a'} fetch", lor_body)
        self.assertIn(f"{self.tmp / 'state-a'}/merged-review-lor-", lor_body)
        ws = backlog_lib.get(conn, "merged-review-ws")
        self.assertEqual(ws["workspace"], str(self.tmp / "repo-b"))

    def test_the_shipped_prompts_carry_placeholders_and_no_personal_paths(self):
        for name in ("merged-review-lor.md", "merged-review-ws.md"):
            text = (backlog_seed.PROMPTS / name).read_text(encoding="utf-8")
            self.assertIn("{workspace}", text)
            self.assertIn("{report_dir}", text)
            self.assertNotIn("/Users/", text)

    def test_running_the_seed_twice_creates_no_duplicates(self):
        self.seed_main()
        code, text = self.seed_main()
        self.assertEqual(code, 0)
        self.assertIn("ANSWER: 0 template(s) created, 2 already present", text)
        self.assertEqual(len(backlog_lib.select_all(self.conn())), 2)

    def test_a_seeded_template_keeps_its_edits_on_the_second_run(self):
        self.seed_main()
        self.run_cli(["backlog", "pause", "merged-review-lor"])
        self.seed_main()
        self.assertEqual(backlog_lib.get(self.conn(), "merged-review-lor")["enabled"], 0)

    def test_a_missing_config_is_refused_and_writes_nothing(self):
        code, text = self.seed_main(config=self.tmp / "absent.json")
        self.assertEqual(code, 1)
        self.assertIn("no seed config", text)
        self.assertEqual(backlog_lib.select_all(self.conn()), [])

    def test_a_config_without_both_folders_for_a_template_is_refused(self):
        self.write_config(self.config, **{"merged-review-ws": {"workspace": str(self.tmp / "repo-b")}})
        code, text = self.seed_main()
        self.assertEqual(code, 1)
        self.assertIn("merged-review-ws", text)
        self.assertEqual(backlog_lib.select_all(self.conn()), [])


if __name__ == "__main__":
    unittest.main()
