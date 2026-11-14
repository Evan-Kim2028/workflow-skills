"""Unit tests for the staging A/B harness: job selection, labels, commands and results aggregation.

Offline. Jobs live in a temp sqlite file shaped like agentq.db's jobs table, the git check is a stub,
and no pi or launcher runs.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import stage_ab as SA  # noqa: E402

JOBS_DDL = (
    "CREATE TABLE jobs (id TEXT PRIMARY KEY, lane TEXT NOT NULL, state TEXT NOT NULL, kind TEXT NOT NULL, "
    "label TEXT NOT NULL, tier TEXT NOT NULL, workspace TEXT NOT NULL, prompt_path TEXT NOT NULL, "
    "submitted_at REAL NOT NULL)"
)
PROXY_DDL = (
    "CREATE TABLE proxy_jobs (job TEXT NOT NULL, model TEXT NOT NULL, first_ts REAL NOT NULL, last_ts REAL NOT NULL, "
    "req INT, ok INT, r429 INT, input INT, cache_read INT, output INT, wait_ms_total INT, upstream_ms_total INT, "
    "PRIMARY KEY (job, model))"
)


def ALWAYS_GIT(path):
    """A git-check stub that accepts every workspace."""
    return True


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stage-ab-test-")
        self.prompts = Path(self.tmp) / "prompts"
        self.prompts.mkdir()
        self.db = Path(self.tmp) / "agentq.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.execute(JOBS_DDL)
        self.conn.execute(PROXY_DDL)
        self.clock = 1000.0

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_job(self, job_id, text="Read the file and report PASS.", lane="normal", state="done", kind="read",
                prompt_exists=True, tier="pi-deepseek-flash"):
        path = self.prompts / f"{job_id}.md"
        if prompt_exists:
            path.write_text(text, encoding="utf-8")
        self.clock += 1
        self.conn.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, lane, state, kind, f"hub-exec-{job_id[:6]}", tier, "/ws/repo", str(path), self.clock),
        )
        self.conn.commit()
        return job_id

    def reader(self):
        self.conn.row_factory = sqlite3.Row
        return self.conn


class ForbiddenTermTests(unittest.TestCase):
    def test_each_listed_term_is_caught_case_insensitively(self):
        for term in ("ssh ", "psql", "lake-vps", "lor-main", "git push", "gh pr", "lor_merge", "merge",
                     "deploy", "systemctl", "--apply", "migrate"):
            self.assertIsNotNone(SA.forbidden_term(f"please {term.upper()} now"), term)

    def test_a_clean_prompt_passes(self):
        self.assertIsNone(SA.forbidden_term("Read the sweep summary and list anomalies."))


class ChooseJobsTests(StoreCase):
    def test_only_done_read_and_review_jobs_are_candidates(self):
        self.add_job("a" * 12, state="done", kind="read")
        self.add_job("b" * 12, state="failed", kind="read")
        self.add_job("c" * 12, state="done", kind="write")
        self.add_job("d" * 12, state="done", kind="review")
        choices, _ = SA.choose_jobs(self.reader(), 10, is_checkout=ALWAYS_GIT)
        self.assertEqual({c["id"] for c in choices}, {"a" * 12, "d" * 12})

    def test_forbidden_prompts_are_skipped_and_counted(self):
        self.add_job("e" * 12, text="SSH into the box and check.")
        self.add_job("f" * 12, text="Summarise the report.")
        choices, rejections = SA.choose_jobs(self.reader(), 4, is_checkout=ALWAYS_GIT)
        self.assertEqual([c["id"] for c in choices], ["f" * 12])
        self.assertEqual(rejections["prompt mentions 'ssh'"], 1)

    def test_missing_prompt_files_and_non_checkouts_are_skipped(self):
        self.add_job("1" * 12, prompt_exists=False)
        self.add_job("2" * 12)
        choices, rejections = SA.choose_jobs(self.reader(), 4, is_checkout=lambda ws: False)
        self.assertEqual(choices, [])
        self.assertEqual(rejections["prompt file missing"], 1)
        self.assertEqual(rejections["workspace is not a git checkout with origin/main"], 1)

    def test_the_most_recent_jobs_win_and_half_come_from_backlog_when_available(self):
        for n in range(3):
            self.add_job(f"n{n}".ljust(12, "0"), lane="normal")
        for n in range(3):
            self.add_job(f"b{n}".ljust(12, "0"), lane="backlog")
        choices, _ = SA.choose_jobs(self.reader(), 4, is_checkout=ALWAYS_GIT)
        lanes = [c["lane"] for c in choices]
        self.assertEqual(lanes.count("backlog"), 2)
        self.assertEqual(lanes.count("normal"), 2)
        self.assertEqual([c["id"] for c in choices if c["lane"] == "backlog"], ["b2" + "0" * 10, "b1" + "0" * 10])
        self.assertTrue(all("recent valid" in c["reason"] for c in choices))

    def test_when_backlog_is_short_other_lanes_fill_the_rest(self):
        for n in range(4):
            self.add_job(f"x{n}".ljust(12, "0"), lane="urgent")
        self.add_job("bb".ljust(12, "0"), lane="backlog")
        choices, _ = SA.choose_jobs(self.reader(), 4, is_checkout=ALWAYS_GIT)
        self.assertEqual(len(choices), 4)
        self.assertEqual(sum(c["lane"] == "backlog" for c in choices), 1)

    def test_job_ids_are_taken_in_the_order_given_and_checked(self):
        self.add_job("p" * 12)
        self.add_job("q" * 12)
        chosen = SA.choose_by_id(self.reader(), ["q" * 12, "p" * 12], ALWAYS_GIT)
        self.assertEqual([c["id"] for c in chosen], ["q" * 12, "p" * 12])
        self.assertEqual(chosen[0]["reason"], "named with --job-ids")

    def test_an_unknown_or_refused_job_id_is_an_error(self):
        self.add_job("r" * 12, text="please deploy")
        with self.assertRaises(ValueError):
            SA.choose_by_id(self.reader(), ["zzzz" * 3], ALWAYS_GIT)
        with self.assertRaises(ValueError):
            SA.choose_by_id(self.reader(), ["r" * 12], ALWAYS_GIT)


class LabelAndCommandTests(StoreCase):
    def test_labels_are_review_scoped_with_the_job_prefix_and_arm_letter(self):
        self.assertEqual(SA.build_label("control", "8fd05befad19"), "hub-review-stage-8fd05bef-c")
        self.assertEqual(SA.build_label("treatment", "8fd05befad19"), "hub-review-stage-8fd05bef-t")

    def test_proxy_keys_match_the_arm_and_job8(self):
        self.assertEqual(SA.proxy_key("control", "8fd05befad19"), "stage-control-8fd05bef")
        self.assertEqual(SA.proxy_key("treatment", "8fd05befad19"), "stage-treatment-8fd05bef")

    def test_the_launch_command_is_the_arm_launcher_with_the_tier_model_and_tier(self):
        argv = SA.build_command("/code/agent-dash", "lbl", "/wt", "/p.md", "/out",
                                "morph/morph-dsv41flash-1017", "pi-deepseek-flash")
        self.assertEqual(argv[1], "/code/agent-dash/scripts/launch_pi.py")
        self.assertEqual(argv[2:], ["lbl", "/wt", "/p.md", "/out", "--model", "morph/morph-dsv41flash-1017",
                                    "--tier", "pi-deepseek-flash"])

    def test_a_deepseek_flash_job_is_run_on_the_morph_deepseek_model(self):
        self.add_job("d1" * 6)
        choices, _ = SA.choose_jobs(self.reader(), 1, is_checkout=ALWAYS_GIT)
        self.assertEqual(choices[0]["model"], "morph/morph-dsv41flash-1017")
        argv = SA.build_command("/code/agent-dash", "lbl", "/wt", "/p.md", "/out", choices[0]["model"],
                                choices[0]["tier"])
        self.assertEqual(argv[argv.index("--model") + 1], "morph/morph-dsv41flash-1017")

    def test_a_kimi_job_gets_the_kimi_model(self):
        self.add_job("k1" * 6, tier="pi-kimi")
        choices, _ = SA.choose_jobs(self.reader(), 1, is_checkout=ALWAYS_GIT)
        self.assertEqual(choices[0]["model"], "morph/morph-kimik31")

    def test_a_tier_with_no_mapping_is_refused_not_run_on_the_default(self):
        self.add_job("t1" * 6, tier="pi-glm-flash")
        choices, rejections = SA.choose_jobs(self.reader(), 4, is_checkout=ALWAYS_GIT)
        self.assertEqual(choices, [])
        self.assertEqual(rejections["tier 'pi-glm-flash' has no model mapping"], 1)

    def test_the_env_isolates_the_queue_home_and_drops_broker_variables(self):
        base = {"PATH": "/bin", "AGENTQ_RUN": "x", "AGENTQ_THINKING": "high", "AGENTQ_JOB": "live",
                "AGENTQ_TOOL_OUTPUT_CAP": "0", "KEEP": "1"}
        env = SA.build_env(base, "/h", "stage-treatment-abcd1234", "/s", "/d")
        self.assertEqual(env["AGENTQ_HOME"], "/h")
        self.assertEqual(env["AGENTQ_JOB"], "stage-treatment-abcd1234")
        self.assertEqual(env["AGENT_SCRATCH"], "/s")
        self.assertEqual(env["AGENT_DASH_STATE_DIR"], "/d")
        self.assertEqual(env["KEEP"], "1")
        for dropped in ("AGENTQ_RUN", "AGENTQ_THINKING", "AGENTQ_TOOL_OUTPUT_CAP"):
            self.assertNotIn(dropped, env)
        self.assertEqual(base["AGENTQ_JOB"], "live")

    def test_worktrees_are_detached_at_origin_main(self):
        self.assertEqual(
            SA.worktree_command("/repo", "/cache/x"),
            ["git", "-C", "/repo", "worktree", "add", "--detach", "/cache/x", "origin/main"],
        )

    def test_read_verdict_takes_the_first_line_word(self):
        tmp = tempfile.mkdtemp(prefix="stage-ab-verdict-")
        try:
            Path(tmp, "out-lbl.md").write_text("PARTIAL\n\nrest of report PASS\n", encoding="utf-8")
            self.assertEqual(SA.read_verdict(tmp, "lbl"), "PARTIAL")
            self.assertEqual(SA.read_verdict(tmp, "missing"), "NONE")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ProxyUsageTests(StoreCase):
    def test_usage_sums_models_and_derives_context_per_request(self):
        rows = [
            {"req": 3, "input": 100, "cache_read": 900, "output": 50},
            {"req": 1, "input": 20, "cache_read": 80, "output": 10},
        ]
        totals = SA.usage_of(rows)
        self.assertEqual(totals["requests"], 4)
        self.assertEqual(totals["input"], 120)
        self.assertEqual(totals["cache_read"], 980)
        self.assertEqual(totals["output"], 60)
        self.assertEqual(totals["total"], 1160)
        self.assertEqual(totals["avg_ctx"], 275.0)

    def test_no_rows_means_zero_and_no_context(self):
        totals = SA.usage_of([])
        self.assertEqual(totals["requests"], 0)
        self.assertIsNone(totals["avg_ctx"])

    def test_proxy_rows_are_read_per_key_only(self):
        self.conn.execute("INSERT INTO proxy_jobs VALUES ('stage-control-aaaa1111','m1',1,2,3,3,0,10,20,5,0,0)")
        self.conn.execute("INSERT INTO proxy_jobs VALUES ('stage-control-aaaa1111','m2',1,2,1,1,0,5,5,1,0,0)")
        self.conn.execute("INSERT INTO proxy_jobs VALUES ('stage-treatment-aaaa1111','m1',1,2,2,2,0,7,7,7,0,0)")
        self.conn.commit()
        control = SA.usage_of(SA.proxy_rows(self.db, "stage-control-aaaa1111"))
        self.assertEqual(control["requests"], 4)
        self.assertEqual(control["total"], 10 + 20 + 5 + 5 + 5 + 1)
        self.assertEqual(SA.usage_of(SA.proxy_rows(self.db, "stage-missing"))["requests"], 0)
        self.assertEqual(SA.proxy_rows(Path(self.tmp, "nope.db"), "x"), [])


def _task(job, arm, verdict, req, inp, cr, out, wall=60.0):
    key = SA.proxy_key(arm, job)
    return {"job8": SA.job8(job), "job_id": job, "lane": "backlog", "arm": arm, "key": key,
            "verdict": verdict, "wall_s": wall, "status": "done", "label": SA.build_label(arm, job),
            "outdir": "/o", "rc": 0, "timed_out": False}, key, {"requests": req, "input": inp,
                                                                  "cache_read": cr, "output": out}


class ResultsAggregationTests(unittest.TestCase):
    def test_pairs_jobs_and_computes_deltas_totals_and_agreement(self):
        job_a, job_b = "aaaa1111" + "0" * 4, "bbbb2222" + "0" * 4
        tasks, usage = [], {}
        for job, verdicts in ((job_a, ("PASS", "PASS")), (job_b, ("PARTIAL", "PASS"))):
            for arm, verdict, req in (("control", verdicts[0], 4), ("treatment", verdicts[1], 3)):
                task, key, totals = _task(job, arm, verdict, req, 1000, 4000, 100)
                tasks.append(task)
                usage[key] = SA.usage_of([{**totals, "req": req}])
        jobs, totals = SA.compare(tasks, usage)
        self.assertEqual(len(jobs), 2)
        first = jobs[0]
        self.assertEqual(first["control"]["total"], 5100)
        self.assertEqual(first["treatment"]["total"], 5100)
        self.assertEqual(first["delta_total"], 0)
        self.assertEqual(first["delta_requests"], -1)
        self.assertTrue(first["agree"])
        self.assertFalse(jobs[1]["agree"])
        self.assertEqual(totals["control"]["runs"], 2)
        self.assertEqual(totals["control"]["requests"], 8)
        self.assertEqual(totals["treatment"]["requests"], 6)
        self.assertEqual(totals["control"]["verdicts"], {"PASS": 1, "PARTIAL": 1})
        self.assertEqual(totals["agree"], 1)
        self.assertEqual(totals["paired"], 2)

    def test_a_run_with_no_proxy_rows_counts_zero(self):
        task, _, _ = _task("cccc3333" + "0" * 4, "control", "PASS", 0, 0, 0, 0)
        jobs, totals = SA.compare([task], {})
        self.assertEqual(jobs[0]["control"]["requests"], 0)
        self.assertIsNone(jobs[0]["control"]["avg_ctx"])

    def test_results_rows_and_report_carry_the_totals(self):
        job = "dddd4444" + "0" * 4
        tasks, usage = [], {}
        for arm, req in (("control", 2), ("treatment", 2)):
            task, key, totals = _task(job, arm, "PASS", req, 100, 300, 10)
            tasks.append(task)
            usage[key] = SA.usage_of([{**totals, "req": req}])
        jobs, totals = SA.compare(tasks, usage)
        rows = SA.results_rows(jobs)
        self.assertEqual(rows[0][0], "job8")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][3], "control")
        self.assertEqual(rows[1][14], 410)
        report = SA.render_report("rid", {"control_code": "c", "treatment_code": "t", "concurrency": 2,
                                          "timeout_min": 25, "model": "m"}, jobs, totals, [], {})
        self.assertIn("| control | 1 | 2 |", report)
        self.assertIn("verdict agreement 1 of 1", report)


class DryRunCommandTests(unittest.TestCase):
    def test_the_worktree_and_launcher_commands_name_the_arm_code(self):
        argv = SA.build_command("/treat", SA.build_label("treatment", "abcdef123456"), "/wt", "/p", "/o",
                                "morph/morph-dsv41flash-1017", "pi-deepseek-flash")
        self.assertIn("/treat/scripts/launch_pi.py", argv)
        self.assertIn("hub-review-stage-abcdef12-t", argv)


if __name__ == "__main__":
    unittest.main()


class EnvPairsTest(unittest.TestCase):
    """--control-env / --treatment-env pairs become environment entries."""

    def test_pairs_parse(self):
        self.assertEqual(SA.parse_env_pairs(["A=1", "B=x=y"]), {"A": "1", "B": "x=y"})

    def test_malformed_pair_raises(self):
        with self.assertRaises(ValueError):
            SA.parse_env_pairs(["nope"])
