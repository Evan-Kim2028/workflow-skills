"""Thinking levels: the launcher's ``--thinking`` flag (and that pi accepts it), the broker's per-tier level with a
per-job override, the level recorded per run, the schema upgrade, and the read-only ``agentq thinking report``.

Offline. AGENTQ_HOME is a temp folder; no worker is launched (the broker's launch pool only records the call).
"""

import concurrent.futures
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from agentq import broker as B  # noqa: E402
from agentq import cli, db  # noqa: E402
from agentq import thinking_report as TR  # noqa: E402
from agentq.proxy import PROXY_JOBS_DDL  # noqa: E402

_DASH_BEFORE = {name for name in sys.modules if name == "dash" or name.startswith("dash.")}
import launch_pi as LP  # noqa: E402  (imports the dash package; not left cached for other test modules)
for _name in [n for n in sys.modules if (n == "dash" or n.startswith("dash.")) and n not in _DASH_BEFORE]:
    del sys.modules[_name]

NOW = 100000.0


class LaunchPiArgvTests(unittest.TestCase):
    def argv(self, **kw):
        return LP.build_argv("lbl", "/ws", "/p.md", "/out", "morph-dsv41flash-1017", None, **kw)

    def test_the_flag_is_passed_before_the_brief_when_a_level_is_set(self):
        argv = self.argv(thinking="high")
        at = argv.index("--thinking")
        self.assertEqual(argv[at:at + 2], ["--thinking", "high"])
        self.assertLess(at, argv.index(LP.BRIEF_MESSAGE))

    def test_no_flag_at_all_when_no_level_is_set(self):
        self.assertNotIn("--thinking", self.argv())
        self.assertNotIn("--thinking", self.argv(thinking=None))

    def test_the_resume_argv_carries_the_level_too(self):
        argv = LP.build_resume_argv("/s.jsonl", "/out", "m", "/p.md", None, thinking="low")
        self.assertEqual(argv[argv.index("--thinking"):argv.index("--thinking") + 2], ["--thinking", "low"])
        self.assertNotIn("--thinking", LP.build_resume_argv("/s.jsonl", "/out", "m", "/p.md", None))

    def test_only_pi_levels_reach_the_flag(self):
        self.assertEqual(LP.thinking_level({"AGENTQ_THINKING": "high"}), "high")
        self.assertEqual(LP.thinking_level({"AGENTQ_THINKING": " low "}), "low")
        self.assertIsNone(LP.thinking_level({"AGENTQ_THINKING": "turbo"}))
        self.assertIsNone(LP.thinking_level({"AGENTQ_THINKING": ""}))
        self.assertIsNone(LP.thinking_level({}))

    def test_the_level_is_recorded_in_the_run_folder_and_nothing_is_written_when_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            LP.write_thinking_level(tmp, "high")
            self.assertEqual((Path(tmp) / "thinking-level").read_text(encoding="utf-8"), "high\n")
            with tempfile.TemporaryDirectory() as other:
                LP.write_thinking_level(other, None)
                self.assertFalse((Path(other) / "thinking-level").exists())

    @unittest.skipUnless(shutil.which("pi"), "pi is not installed here")
    def test_the_real_pi_accepts_the_composed_flags(self):
        argv = self.argv(thinking="high")
        proc = subprocess.run(["pi", *argv[1:-1], "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--thinking", proc.stdout + proc.stderr)


class RecordingPool:
    """A launch pool that records each submitted start and never runs it."""

    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        self.calls.append(args)
        return concurrent.futures.Future()


class BrokerThinkingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.ws = Path(tmp.name) / "ws"
        self.ws.mkdir()
        self.prompt = Path(tmp.name) / "brief.md"
        self.prompt.write_text("brief\n", encoding="utf-8")
        self.pool = RecordingPool()
        self.conn = db.connect(self.home)
        self.addCleanup(self.conn.close)

    def config(self, data):
        (self.home / "broker.json").write_text(json.dumps(data), encoding="utf-8")

    def job(self, job_id, tier, thinking=None):
        db.insert_job(self.conn, {
            "id": job_id, "lane": "normal", "kind": "read", "res": "light", "tier": tier, "label": f"hub-exec-{job_id}",
            "workspace": str(self.ws), "session": "sess", "harness": "pi", "prompt_path": str(self.prompt),
            **({"thinking": thinking} if thinking is not None else {}),
        }, NOW)
        return db.get_job(self.conn, job_id)

    def launch(self, job):
        broker = B.Broker(self.home, launcher=mock.Mock(), now=lambda: NOW, proxy=lambda: None,
                          gauges=mock.Mock(read=lambda ttl: (90.0, 0.1)), launch_pool=self.pool,
                          labeler=lambda kind, slug: "x")
        broker._launch(self.conn, job, NOW)
        _, _, env = self.pool.calls[-1]
        return env

    def used(self, job_id):
        return db.get_job(self.conn, job_id)["thinking_used"]

    def test_the_deepseek_tier_runs_at_its_configured_level(self):
        self.config({"tiers_thinking": {"pi-deepseek-flash": "high"}})
        env = self.launch(self.job("ds1", "pi-deepseek-flash"))
        self.assertEqual(env.get("AGENTQ_THINKING"), "high")
        self.assertEqual(self.used("ds1"), "high")

    def test_kimi_is_untouched_unless_it_is_configured(self):
        self.config({"tiers_thinking": {"pi-deepseek-flash": "high"}})
        env = self.launch(self.job("k1", "pi-kimi"))
        self.assertNotIn("AGENTQ_THINKING", env)
        self.assertIsNone(self.used("k1"))

    def test_a_configured_kimi_tier_gets_its_own_level(self):
        self.config({"tiers_thinking": {"pi-kimi": "low"}})
        env = self.launch(self.job("k2", "pi-kimi"))
        self.assertEqual(env.get("AGENTQ_THINKING"), "low")

    def test_a_job_override_wins_over_the_tier_level(self):
        self.config({"tiers_thinking": {"pi-deepseek-flash": "high"}})
        env = self.launch(self.job("ds2", "pi-deepseek-flash", thinking="low"))
        self.assertEqual(env.get("AGENTQ_THINKING"), "low")
        self.assertEqual(self.used("ds2"), "low")

    def test_an_invalid_override_falls_back_to_the_tier_level(self):
        self.config({"tiers_thinking": {"pi-deepseek-flash": "high"}})
        env = self.launch(self.job("ds3", "pi-deepseek-flash", thinking="turbo"))
        self.assertEqual(env.get("AGENTQ_THINKING"), "high")

    def test_with_no_configuration_real_work_runs_at_pi_default(self):
        env = self.launch(self.job("ds4", "pi-deepseek-flash"))
        self.assertNotIn("AGENTQ_THINKING", env)
        self.assertIsNone(self.used("ds4"))

    def test_a_stale_inherited_level_never_reaches_a_job_that_has_none(self):
        with mock.patch.dict(os.environ, {"AGENTQ_THINKING": "max"}):
            env = self.launch(self.job("ds5", "pi-deepseek-flash"))
        self.assertNotIn("AGENTQ_THINKING", env)

    def test_broker_json_drops_unknown_levels(self):
        self.config({"tiers_thinking": {"pi-deepseek-flash": "turbo", "pi-kimi": "low"}})
        self.assertEqual(B.load_config(self.home)["tiers_thinking"], {"pi-kimi": "low"})
        self.config({"tiers_thinking": "high"})
        self.assertEqual(B.load_config(self.home)["tiers_thinking"], {})


class SchemaUpgradeThinkingTests(unittest.TestCase):
    def test_a_version_six_store_gains_the_thinking_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            conn = db.connect(home)
            conn.execute("ALTER TABLE jobs DROP COLUMN thinking")
            conn.execute("ALTER TABLE jobs DROP COLUMN thinking_used")
            conn.execute("PRAGMA user_version = 6")
            conn.close()
            conn = db.connect(home)
            self.addCleanup(conn.close)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
            self.assertIn("thinking", columns)
            self.assertIn("thinking_used", columns)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)

    def test_the_upgrade_is_safe_to_rerun_over_a_store_that_has_the_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            conn = db.connect(home)
            conn.execute("PRAGMA user_version = 6")
            conn.close()
            conn = db.connect(home)
            self.addCleanup(conn.close)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)


class ThinkingReportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.conn = db.connect(self.home)
        self.addCleanup(self.conn.close)
        self.conn.execute(PROXY_JOBS_DDL)
        self.id = 0

    def job(self, tier, level, state, verdict, output, turns, billed, submitted=NOW, wall=60):
        self.id += 1
        jid = f"j{self.id}"
        db.insert_job(self.conn, {
            "id": jid, "lane": "normal", "kind": "read", "res": "light", "tier": tier, "label": f"hub-exec-{jid}",
            "workspace": "/ws", "session": "s", "harness": "pi", "prompt_path": "/p",
        }, submitted)
        started = submitted + 5
        self.conn.execute(
            "UPDATE jobs SET state = ?, verdict = ?, thinking_used = ?, started_at = ?, finished_at = ? WHERE id = ?",
            (state, verdict, level, started, started + wall, jid))
        if output is not None:
            self.conn.execute("INSERT INTO proxy_jobs (job, model, first_ts, last_ts, req, ok, r429, input, cache_read, output)"
                              " VALUES (?, 'morph-dsv41flash-1017', 0, 0, ?, ?, 0, ?, ?, ?)",
                              (jid, turns, turns, billed - output, 0, output))
        return jid

    def test_the_report_groups_deepseek_jobs_by_level_with_verdicts_and_turn_output(self):
        self.job("pi-deepseek-flash", "high", "done", "PASS", output=9000, turns=10, billed=50000)
        self.job("pi-deepseek-flash", "high", "done", "PASS", output=7000, turns=5, billed=30000)
        self.job("pi-deepseek-flash", "high", "done", "FAIL", output=3000, turns=3, billed=20000)
        self.job("pi-deepseek-flash", "medium", "done", "PARTIAL", output=4000, turns=4, billed=25000)
        self.job("pi-deepseek-flash", None, "done", "PASS", output=2000, turns=2, billed=9000)
        self.job("pi-deepseek-flash", "high", "running", None, output=None, turns=None, billed=None)
        self.job("pi-kimi", "high", "done", "PASS", output=1, turns=1, billed=1)
        lines, answer = TR.report_text(self.home)
        text = "\n".join(lines)
        self.assertIn("level high: 4 jobs, 3 finished, 1 not finished", text)
        self.assertIn("verdicts: PASS 2, PARTIAL 0, FAIL 1, BLOCKED 0, none 0", text)
        self.assertIn("level medium: 1 jobs, 1 finished", text)
        self.assertIn("level default: 1 jobs, 1 finished", text)
        self.assertIn("output tokens per model turn", text)
        self.assertNotIn("pi-kimi", text)
        self.assertEqual(answer, "ANSWER: 6 DeepSeek job(s) across 3 thinking level(s)")

    def test_output_per_turn_is_output_over_model_turns_pooled(self):
        self.job("pi-deepseek-flash", "high", "done", "PASS", output=9000, turns=10, billed=50000)
        self.job("pi-deepseek-flash", "high", "done", "PASS", output=6000, turns=5, billed=30000)
        summary = TR.summarize(TR.collect(self.conn))
        self.assertEqual(summary["high"]["output_per_turn"], 15000 / 15)
        self.assertEqual(summary["high"]["output"], 7500.0)

    def test_the_pass_rate_has_a_wilson_interval(self):
        for _ in range(8):
            self.job("pi-deepseek-flash", "high", "done", "PASS", output=1, turns=1, billed=1)
        for _ in range(2):
            self.job("pi-deepseek-flash", "high", "done", "FAIL", output=1, turns=1, billed=1)
        low, high = TR.summarize(TR.collect(self.conn))["high"]["pass_interval"]
        self.assertAlmostEqual(low, 0.4902, places=3)
        self.assertAlmostEqual(high, 0.9433, places=3)

    def test_the_since_filter_counts_only_later_jobs(self):
        self.job("pi-deepseek-flash", "high", "done", "PASS", output=1, turns=1, billed=1, submitted=NOW)
        self.job("pi-deepseek-flash", "high", "done", "PASS", output=1, turns=1, billed=1, submitted=NOW + 3600)
        lines, _ = TR.report_text(self.home, since="1970-01-02T04:30:00Z")
        self.assertIn("level high: 1 jobs", "\n".join(lines))

    def test_the_report_writes_nothing_to_the_store(self):
        self.job("pi-deepseek-flash", "high", "done", "PASS", output=1, turns=1, billed=1)
        self.conn.commit()
        path = self.home / "agentq.db"
        before = path.read_bytes()
        TR.report_text(self.home)
        self.assertEqual(path.read_bytes(), before)

    def test_an_empty_store_says_so(self):
        lines, answer = TR.report_text(self.home)
        self.assertEqual(lines[-1], "no DeepSeek job has run yet")
        self.assertIn("0 DeepSeek job(s)", answer)

    def test_a_missing_store_and_a_bad_since_are_refused(self):
        with self.assertRaisesRegex(ValueError, "no agentq store"):
            TR.report_text(Path(tempfile.mkdtemp()) / "nope")
        with self.assertRaisesRegex(ValueError, "ISO time"):
            TR.report_text(self.home, since="yesterday")

    def test_the_cli_prints_the_report_and_an_answer(self):
        self.job("pi-deepseek-flash", "high", "done", "PASS", output=1, turns=1, billed=1)
        self.conn.commit()
        env = {"AGENTQ_HOME": str(self.home), "PATH": os.environ["PATH"]}
        out = io.StringIO()
        code = cli.main(["thinking", "report"], env=env, out=out, now=lambda: NOW)
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("level high: 1 jobs", text)
        self.assertIn("ANSWER: 1 DeepSeek job(s) across 1 thinking level(s)", text)


if __name__ == "__main__":
    unittest.main()
