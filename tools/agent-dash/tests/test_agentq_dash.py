import calendar
import re
import hashlib
import shutil
import subprocess
import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agentq"))

from proxy import PROXY_MINUTES_DDL

import dash

NOW = float(calendar.timegm((2026, 11, 12, 12, 0, 30)))
UNTIL = int(NOW // 60) * 60
JOB_DDL = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY, state TEXT NOT NULL, lane TEXT NOT NULL, tier TEXT NOT NULL,
    kind TEXT NOT NULL, verdict TEXT, submitted_at REAL NOT NULL, started_at REAL, finished_at REAL,
    label TEXT NOT NULL DEFAULT '', detail TEXT, res TEXT NOT NULL DEFAULT ''
)
"""
PROXY_JOBS_DDL = """
CREATE TABLE proxy_jobs (
    job TEXT NOT NULL, model TEXT NOT NULL, first_ts REAL, last_ts REAL,
    req INT, ok INT, r429 INT, input INT, cache_read INT, output INT,
    wait_ms_total INT, upstream_ms_total INT,
    PRIMARY KEY (job, model)
)
"""
MINUTE_COLUMNS = "minute, model, req, ok, r429, input, cache_read, output, uncached_est, wait_p50_ms, wait_p95_ms, target"
PS_FIXTURE = """\
  101 /usr/bin/python3 -u /x/launch_pi.py --model morph-dsv41flash-1017 AGENTQ_JOB=0123456789ab PATH=/bin
  102 /usr/bin/python3 -u /x/launch_pi.py --tier pi-kimi PATH=/bin
  103 /usr/bin/python3 -u /x/launch_pi.py --tier pi-deepseek-flash PATH=/bin
  104 /usr/bin/python3 /x/unrelated.py pi-kimi
"""


def minute_at(seconds_before_until: int) -> str:
    return dash.minute_stamp(UNTIL - seconds_before_until)


def make_home(tmp: Path, *, minutes=True, jobs=True) -> Path:
    home = tmp / "agentq"
    home.mkdir()
    if minutes or jobs:
        conn = sqlite3.connect(home / "agentq.db")
        if minutes:
            conn.execute(PROXY_MINUTES_DDL)
            rows = [
                (minute_at(60), "morph-dsv41flash-1017", 1, 1, 0, 100, 0, 7000, 0, 300, 500, 0.9),
                (minute_at(60), "morph-kimik31", 1, 1, 1, 2429, 1000, 100, 2429, 1500, 2000, 0.9),
                (minute_at(120), "morph-dsv41flash-1017", 1, 1, 0, 0, 0, 500000, 0, 100, 900, 0.9),
                (minute_at(10 * 60), "morph-dsv41flash-1017", 1, 0, 3, 0, 0, 0, 0, 0, 0, 0.9),
                (minute_at(3 * 60), "morph-dsv41flash-1017", 1, 1, 0, 0, 0, 5, 0, 10, 20, 0.9),
                (dash.minute_stamp(UNTIL), "morph-dsv41flash-1017", 9, 9, 9, 999999, 0, 999999, 0, 99, 99, 0.9),
                (minute_at(61 * 60), "morph-kimik31", 1, 1, 10, 0, 0, 0, 0, 0, 0, 1.0),
            ]
            conn.executemany(f"INSERT INTO proxy_minutes ({MINUTE_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        if jobs:
            conn.execute(JOB_DDL)
            conn.executemany(
                "INSERT INTO jobs (id, state, lane, tier, kind, verdict, submitted_at, started_at, finished_at, label) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    ("a" * 12, "queued", "normal", "pi-deepseek-flash", "read", None, NOW - 30, None, None, "queued-read"),
                    ("b" * 12, "running", "urgent", "pi-kimi", "review", None, NOW - 90, NOW - 80, None, "review-kimi"),
                    ("c" * 12, "done", "normal", "pi-deepseek-flash", "write", "PASS", NOW - 600, NOW - 580, NOW - 500, "write-ds"),
                ],
            )
        conn.commit()
        conn.close()
    return home


def no_proxy(): 
    raise OSError("proxy not running")


def proxy_status():
    return {"ok": True, "budgets": {
        "total": {"limit": 10_000_000, "target_frac": 0.9},
        "output": {"limit": 70_000, "target_frac": 1.0},
        "uncached": {"limit": 24_290, "target_frac": 1.18},
    }}


class SummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_percentages_use_the_five_complete_minutes_and_the_documented_limits(self) -> None:
        summary = dash.build_summary(self.home, 1, now=NOW, fetch_live=proxy_status)
        limits = summary["limits"]
        # total: (7000+100+2429+1000+100 at 11:59 ... see fixture) over 5 complete minutes
        billed_total = (100 + 7000) + (2429 + 1000 + 100) + 500000
        self.assertEqual(limits["total"]["avg_5m_frac"], round(billed_total / 5 / 10_000_000, 4))
        self.assertEqual(limits["uncached"]["avg_5m_frac"], round(2429 / 5 / 24_290, 4))
        self.assertEqual(limits["uncached"]["avg_5m_frac"], 0.02)
        self.assertEqual(limits["output"]["avg_5m_frac"], round((7000 + 500000) / 5 / 70_000, 4))
        self.assertGreater(limits["output"]["avg_5m_frac"], 1.0)
        self.assertEqual(limits["total"]["target_frac"], 0.9)
        self.assertEqual(limits["uncached"]["target_frac"], 1.18)

    def test_current_minute_is_excluded_from_minutes_and_rates(self) -> None:
        summary = dash.build_summary(self.home, 1, now=NOW, fetch_live=proxy_status)
        self.assertEqual(summary["window"]["end"], UNTIL)
        self.assertNotIn(UNTIL, [p["t"] for p in summary["minutes"]])
        self.assertTrue(all(p["total"] != 999999 for p in summary["minutes"]))

    def test_minute_points_sum_models_and_compute_fractions(self) -> None:
        summary = dash.build_summary(self.home, 1, now=NOW, fetch_live=proxy_status)
        by_minute = {p["minute"]: p for p in summary["minutes"]}
        eleven_fifty_nine = by_minute[minute_at(60)]
        self.assertEqual(eleven_fifty_nine["total"], 100 + 7000 + 2429 + 1000 + 100)
        self.assertEqual(eleven_fifty_nine["ds_output"], 7000)
        self.assertEqual(eleven_fifty_nine["kimi_uncached"], 2429)
        self.assertEqual(eleven_fifty_nine["r429"], 1)
        self.assertEqual(eleven_fifty_nine["wait_p95_ms"], 2000)
        self.assertEqual(eleven_fifty_nine["pct_ds_output"], round(7000 / 70_000, 4))
        empty = by_minute[minute_at(4 * 60)]
        self.assertEqual(empty["total"], 0)
        self.assertIsNone(empty["wait_p95_ms"])

    def test_rate_429s_cover_the_last_sixty_complete_minutes(self) -> None:
        summary = dash.build_summary(self.home, 6, now=NOW, fetch_live=proxy_status)
        # 11:59 kimi 1, 11:57 ds 3; the 10:59-minute row (61 min old) is outside the window.
        self.assertEqual(summary["r429_60m"], 1 + 3)

    def test_jobs_counts_and_recent_list(self) -> None:
        summary = dash.build_summary(self.home, 1, now=NOW, fetch_live=proxy_status)
        jobs = summary["jobs"]
        self.assertEqual(jobs["by_state"], {"queued": 1, "running": 1, "done": 1})
        self.assertEqual(jobs["by_tier"]["pi-kimi"], {"running": 1})
        self.assertEqual(len(jobs["recent"]), 3)
        self.assertEqual(jobs["recent"][0]["state"], "queued")
        self.assertEqual(summary["live"]["budgets"]["uncached"]["target_frac"], 1.18)

    def test_caps_come_from_broker_json_with_defaults(self) -> None:
        self.assertEqual(dash.read_caps(self.home), {"deepseek": 64, "kimi": 2})
        (self.home / "broker.json").write_text(json.dumps({"tiers": {"pi-deepseek-flash": 48, "pi-kimi": "x"}}))
        self.assertEqual(dash.read_caps(self.home), {"deepseek": 48, "kimi": 2})

    def test_hours_outside_the_range_fall_back_to_default(self) -> None:
        self.assertEqual(dash.build_summary(self.home, 3, now=NOW, fetch_live=no_proxy)["hours"], 6)
        self.assertEqual(dash.build_summary(self.home, 24, now=NOW, fetch_live=no_proxy)["hours"], 24)

    def test_unreachable_proxy_gives_null_live_and_an_error(self) -> None:
        summary = dash.build_summary(self.home, 1, now=NOW, fetch_live=no_proxy)
        self.assertIsNone(summary["live"])
        self.assertIn("live", summary["errors"])
        self.assertIsNone(summary["limits"]["total"]["target_frac"])
        self.assertGreater(summary["limits"]["total"]["avg_5m_frac"], 0)

    def test_real_fetch_of_a_closed_port_degrades(self) -> None:
        with self.assertRaises(OSError):
            dash.fetch_live_status("http://127.0.0.1:1/agentq/status", timeout=0.5)
        summary = dash.build_summary(self.home, 1, now=NOW, fetch_live=lambda: dash.fetch_live_status("http://127.0.0.1:1/x", 0.5))
        self.assertIsNone(summary["live"])
        self.assertIn("live", summary["errors"])


class MissingDataTest(unittest.TestCase):
    def test_missing_database_degrades_to_empty_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "empty"
            home.mkdir()
            summary = dash.build_summary(home, 1, now=NOW, fetch_live=no_proxy)
            self.assertEqual(summary["minutes"], [])
            self.assertIsNone(summary["jobs"])
            self.assertEqual(summary["workers"], [])
            self.assertIn("minutes", summary["errors"])
            self.assertIn("jobs", summary["errors"])
            self.assertEqual(summary["limits"]["total"]["avg_5m_frac"], 0.0)
            self.assertNotIn("workers", summary["errors"])
            self.assertFalse((home / "agentq.db").exists(), "summary must not create agentq.db")

    def test_database_without_proxy_table_degrades_only_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(Path(tmp), minutes=False)
            summary = dash.build_summary(home, 1, now=NOW, fetch_live=no_proxy)
            self.assertEqual(summary["minutes"], [])
            self.assertEqual(summary["jobs"]["by_state"], {"queued": 1, "running": 1, "done": 1})
            self.assertEqual(summary["jobs"]["recent"][0]["state"], "queued")


class SamplerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ps_fixture_is_counted_by_tier_and_queue_or_direct(self) -> None:
        counts = dash.parse_workers(PS_FIXTURE)
        self.assertEqual(counts, {"ds_workers": 2, "kimi_workers": 1, "queue_workers": 1, "direct_workers": 2})

    def test_sample_is_written_to_dash_db_and_agentq_db_is_untouched(self) -> None:
        before = hashlib.sha256((self.home / "agentq.db").read_bytes()).hexdigest()
        row = dash.sample_once(self.home, ps_runner=lambda: PS_FIXTURE, now=NOW)
        self.assertEqual(row["ds_workers"], 2)
        self.assertEqual(row["queued_jobs"], 1)
        conn = sqlite3.connect(self.home / "dash.db")
        stored = conn.execute("SELECT ts, ds_workers, kimi_workers, queue_workers, direct_workers, queued_jobs FROM samples").fetchall()
        conn.close()
        self.assertEqual(stored, [(NOW, 2, 1, 1, 2, 1)])
        after = hashlib.sha256((self.home / "agentq.db").read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_samples_older_than_seven_days_are_pruned(self) -> None:
        dash.sample_once(self.home, ps_runner=lambda: PS_FIXTURE, now=NOW - dash.SAMPLE_KEEP_S - 100)
        dash.sample_once(self.home, ps_runner=lambda: PS_FIXTURE, now=NOW)
        conn = sqlite3.connect(self.home / "dash.db")
        ts = [r[0] for r in conn.execute("SELECT ts FROM samples ORDER BY ts")]
        conn.close()
        self.assertEqual(ts, [NOW])

    def test_failed_ps_writes_no_sample(self) -> None:
        def broken() -> str:
            raise RuntimeError("ps exploded")

        with self.assertRaises(RuntimeError):
            dash.sample_once(self.home, ps_runner=broken, now=NOW)
        self.assertFalse((self.home / "dash.db").exists())

    def test_missing_agentq_db_still_records_worker_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            row = dash.sample_once(home, ps_runner=lambda: PS_FIXTURE, now=NOW)
            self.assertIsNone(row["queued_jobs"])
            self.assertEqual(dash.read_samples(home / "dash.db", 0)[0]["ds_workers"], 2)

    def test_summary_reads_back_recorded_samples(self) -> None:
        dash.sample_once(self.home, ps_runner=lambda: PS_FIXTURE, now=NOW - 120)
        summary = dash.build_summary(self.home, 1, now=NOW, fetch_live=no_proxy)
        self.assertEqual(len(summary["workers"]), 1)
        self.assertEqual(summary["workers"][0]["kimi_workers"], 1)


class ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.tmp.name))
        self.server = dash.make_server(self.home, 0, fetch_live=no_proxy)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def get(self, path: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, response.getheader("Content-Type"), response.read()
        finally:
            conn.close()

    def test_root_serves_the_page_with_the_chart_titles(self) -> None:
        status, ctype, body = self.get("/")
        html = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/html"))
        self.assertIn("<!DOCTYPE html>", html)
        for title in (
            "Morph total-token limit used per minute",
            "Active runs over time, with caps",
            "Tokens billed per 5 min",
            "Runs",
            "Top consumers today",
            "Morph 429s",
            "Proxy wait p95",
            "Recent jobs",
            "Minute data (table view)",
        ):
            self.assertIn(title, html)
        self.assertNotIn("<script src=", html)
        self.assertNotIn("http://cdn", html)

    def test_summary_endpoint_returns_json_with_the_documented_keys(self) -> None:
        status, ctype, body = self.get("/api/summary?hours=1")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "application/json")
        data = json.loads(body)
        self.assertEqual(sorted(data["recent"]), ["ds_uncached_rejections", "minutes", "window"])
        self.assertEqual(data["recent"]["window"]["end"] - data["recent"]["window"]["start"], 3600)
        for key in (
            "live", "limits", "capacity", "minutes", "token_mix", "jobs", "workers", "caps", "runs",
            "run_kpis", "perf_kpis", "top_consumers", "errors", "r429_60m", "window",
        ):
            self.assertIn(key, data)
        self.assertEqual(data["hours"], 1)
        self.assertIsNone(data["live"])

    def test_unknown_path_is_404(self) -> None:
        status, _, _ = self.get("/nope")
        self.assertEqual(status, 404)


class PlistAndPageTest(unittest.TestCase):
    def test_plist_uses_placeholders_and_keeps_alive(self) -> None:
        import plistlib

        plist_path = Path(__file__).resolve().parents[1] / "agentq" / "launchd" / "com.agentq.dash.plist"
        plist = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(plist["Label"], "com.agentq.dash")
        self.assertTrue(plist["RunAtLoad"])
        self.assertTrue(plist["KeepAlive"])
        self.assertEqual(plist["ProcessType"], "Background")
        self.assertEqual(plist["Nice"], 10)
        self.assertEqual(plist["ProgramArguments"][0], "__PYTHON__")
        self.assertIn("__HOME__", plist["StandardOutPath"])
        self.assertTrue(plist["StandardOutPath"].endswith("logs/dash.log"))

    def test_page_script_has_no_obvious_syntax_break(self) -> None:
        self.assertIn("visibilitychange", dash.PAGE_HTML)
        self.assertIn("api/summary", dash.PAGE_HTML)

    def test_poll_interval_is_thirty_seconds_in_the_served_html(self) -> None:
        self.assertIn("var POLL_MS = 30000;", dash.PAGE_HTML)
        self.assertNotIn("POLL_MS = 15000", dash.PAGE_HTML)


DAY_START = dash.day_start(NOW)
RUN_TODAY = max(NOW - 30, DAY_START)


def make_runs_home(tmp: Path, *, proxy_jobs=True) -> Path:
    home = make_home(tmp)
    conn = sqlite3.connect(home / "agentq.db")
    conn.execute("DROP TABLE jobs")
    conn.execute(JOB_DDL)
    conn.executemany(
        "INSERT INTO jobs (id, state, lane, tier, kind, verdict, submitted_at, started_at, finished_at, label) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("a" * 12, "queued", "normal", "pi-deepseek-flash", "read", None, NOW - 900, None, None, "hub-exec-agentq-18f40a1d"),
            ("b" * 12, "running", "urgent", "pi-kimi", "review", None, NOW - 900, NOW - 880, None, "hub-review-agentq-460a92e5"),
            ("c" * 12, "done", "normal", "pi-deepseek-flash", "write", "PASS", NOW - 900, NOW - 880, NOW - 200, "write-ds"),
            ("d" * 12, "done", "normal", "pi-deepseek-flash", "write", "FAIL", NOW - 900, NOW - 880, NOW - 300, "write-fail"),
            ("e" * 12, "done", "normal", "pi-kimi", "review", "PARTIAL", NOW - 3 * 86400, NOW - 3 * 86400, NOW - 2 * 86400, "old-partial"),
        ],
    )
    if proxy_jobs:
        conn.execute(PROXY_JOBS_DDL)
        conn.executemany(
            "INSERT INTO proxy_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("a" * 12, "morph-dsv41flash-1017", NOW - 600, RUN_TODAY, 4, 4, 1, 1000, 3000, 500, 800, 4000),
                ("b" * 12, "morph-kimik31", NOW - 900, NOW - 100, 10, 10, 2, 5000, 0, 200, 2000, 20000),
                ("c" * 12, "morph-dsv41flash-1017", NOW - 300, NOW - 200, 1, 1, 0, 100, 0, 50, 10, 200),
                ("none", "morph-dsv41flash-1017", NOW - 60, NOW - 50, 2, 2, 0, 300, 0, 100, 40, 1000),
                ("hub-direct-launch-x", "morph-kimik31", NOW - 9 * 86400, NOW - 8 * 86400, 1, 1, 0, 50, 0, 5, 5, 100),
            ],
        )
    conn.commit()
    conn.close()
    return home


def live_with_queue():
    status = proxy_status()
    status["queued"] = {"urgent": 0, "normal": 2, "backlog": 1}
    return status


class RunsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_runs_home(Path(self.tmp.name))
        self.summary = dash.build_summary(self.home, 1, now=NOW, fetch_live=live_with_queue)
        self.runs = {run["job"]: run for run in self.summary["runs"]}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_queue_run_takes_its_label_from_the_jobs_table(self) -> None:
        run = self.runs["a" * 12]
        self.assertEqual(run["run"], "hub-exec-agentq-18f40a1d")
        self.assertEqual(run["status"], "queued")
        self.assertEqual(run["tier"], "pi-deepseek-flash")
        self.assertEqual(run["total_tokens"], 1000 + 3000 + 500)
        self.assertEqual(run["cache_read_pct"], 0.75)
        self.assertEqual(run["avg_wait_s"], 0.2)
        self.assertEqual(run["avg_upstream_s"], 1.0)
        self.assertEqual(run["duration_s"], RUN_TODAY - (NOW - 600))

    def test_direct_launch_keeps_its_job_string_and_active_state(self) -> None:
        run = self.runs["none"]
        self.assertEqual(run["run"], dash.UNTAGGED_LABEL)
        self.assertEqual(run["job"], "none")
        self.assertFalse(run["queue"])
        self.assertEqual(run["status"], "active")
        self.assertIsNone(run["verdict"])

    def test_untagged_row_is_flagged_and_listed_last(self) -> None:
        self.assertEqual(self.runs["none"]["run"], "untagged (pre-tagging workers)")
        self.assertTrue(self.runs["none"]["untagged"])
        self.assertFalse(self.runs["a" * 12]["untagged"])
        self.assertEqual([run["untagged"] for run in self.summary["runs"]].count(True), 1)
        self.assertEqual(self.summary["runs"][-1]["job"], "none")

    def test_queue_status_comes_from_jobs_not_from_proxy_recency(self) -> None:
        self.assertEqual(self.runs["b" * 12]["status"], "running")
        self.assertEqual(self.runs["b" * 12]["verdict"], None)
        self.assertEqual(self.runs["c" * 12]["verdict"], "PASS")

    def test_run_kpis_count_active_runs_against_caps(self) -> None:
        kpis = self.summary["run_kpis"]
        self.assertEqual(kpis["active_deepseek"], 1)
        self.assertEqual(kpis["active_kimi"], 1)
        self.assertEqual(kpis["cap_deepseek"], 64)
        self.assertEqual(kpis["cap_kimi"], 2)

    def test_runs_seen_today_are_distinct_jobs_with_activity_today(self) -> None:
        kpis = self.summary["run_kpis"]
        self.assertEqual(kpis["seen_today"], 4)
        self.assertEqual(kpis["tokens_today"], 4500 + 5200 + 150 + 400)
        self.assertEqual(kpis["median_tokens_per_run"], (400 + 4500) / 2)
        self.assertEqual(kpis["median_requests_per_run"], 3.0)

    def test_finished_today_counts_queue_verdicts_on_the_local_day(self) -> None:
        self.assertEqual(self.summary["run_kpis"]["finished_today"], {"PASS": 1, "PARTIAL": 0, "FAIL": 1})

    def test_top_consumers_rank_today_runs_with_share_of_today(self) -> None:
        top = self.summary["top_consumers"]
        self.assertEqual([row["job"] for row in top], ["b" * 12, "a" * 12, "none", "c" * 12])
        self.assertAlmostEqual(top[0]["share"], 5200 / (4500 + 5200 + 150 + 400))
        self.assertEqual(sum(row["share"] for row in top), 1.0)

    def test_perf_kpis_from_minutes_and_active_runs(self) -> None:
        perf = self.summary["perf_kpis"]
        self.assertAlmostEqual(perf["wait_p50_ms"], (300 + 1500 + 100 + 10 + 0) / 5)
        self.assertEqual(perf["wait_p95_ms"], 2000)
        self.assertEqual(perf["upstream_median_ms"], 750.0)
        self.assertEqual(perf["r429_rate_60m"], 0.8)
        self.assertEqual(perf["requests_per_min_5m"], 0.8)

    def test_capacity_headline_names_the_binding_limit(self) -> None:
        capacity = self.summary["capacity"]
        self.assertEqual(capacity["binding"], "output")
        self.assertEqual(capacity["binding_label"], "DeepSeek output")
        self.assertEqual(capacity["used_frac"], self.summary["limits"]["output"]["avg_5m_frac"])
        self.assertEqual(self.summary["limits"]["output"]["last_min_frac"], 0.1)
        self.assertEqual(capacity["waiting_now"], 3)
        self.assertEqual(capacity["waiting_by_lane"], {"urgent": 0, "normal": 2, "backlog": 1})

    def test_token_mix_uses_five_minute_buckets_and_keeps_every_token(self) -> None:
        mix = self.summary["token_mix"]
        self.assertEqual(self.summary["token_bucket_s"], 300)
        self.assertTrue(all(row["t"] % 300 == 0 for row in mix))
        for key, source in (("cache_read", "cache_read"), ("uncached", "input"), ("output", "output")):
            self.assertEqual(sum(row[key] for row in mix), sum(p[source] for p in self.summary["minutes"]))

    def test_direct_run_past_the_seven_day_lookback_is_absent(self) -> None:
        self.assertNotIn("hub-direct-launch-x", self.runs)


class CapacityHeadlineTest(unittest.TestCase):
    def test_binding_limit_is_the_maximum_five_minute_fraction(self) -> None:
        limits = {
            "total": {"avg_5m_frac": 0.95},
            "output": {"avg_5m_frac": 0.9},
            "uncached": {"avg_5m_frac": 0.2},
        }
        headline = dash.capacity_headline(limits, None)
        self.assertEqual(headline["binding"], "total")
        self.assertEqual(headline["used_frac"], 0.95)
        self.assertIsNone(headline["waiting_now"])

    def test_tie_goes_to_the_first_limit_in_order(self) -> None:
        limits = {"total": {"avg_5m_frac": 0.5}, "output": {"avg_5m_frac": 0.5}, "uncached": {"avg_5m_frac": 0.1}}
        self.assertEqual(dash.capacity_headline(limits, None)["binding"], "total")


class MissingRunsTableTest(unittest.TestCase):
    def test_missing_proxy_jobs_degrades_only_the_runs_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_runs_home(Path(tmp), proxy_jobs=False)
            summary = dash.build_summary(home, 1, now=NOW, fetch_live=no_proxy)
            self.assertIn("runs", summary["errors"])
            self.assertNotIn("minutes", summary["errors"])
            self.assertNotIn("jobs", summary["errors"])
            self.assertEqual(summary["runs"], [])
            self.assertEqual(summary["top_consumers"], [])
            self.assertEqual(summary["run_kpis"]["seen_today"], 0)
            self.assertEqual(summary["run_kpis"]["finished_today"], {"PASS": 1, "PARTIAL": 0, "FAIL": 1})
            self.assertIsNone(summary["perf_kpis"]["upstream_median_ms"])
            self.assertEqual(summary["jobs"]["by_state"], {"queued": 1, "running": 1, "done": 3})
            self.assertTrue(summary["minutes"])

    def test_page_and_summary_still_serve_when_proxy_jobs_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_runs_home(Path(tmp), proxy_jobs=False)
            server = dash.make_server(home, 0, fetch_live=no_proxy)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                conn.request("GET", "/api/summary?hours=1")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("runs", json.loads(response.read())["errors"])
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class IncompleteMinuteTest(unittest.TestCase):
    """Only complete minutes feed the tiles and series; the current minute and unflushed minutes never read as zero."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def delete_minute(self, seconds_before_until: int) -> None:
        conn = sqlite3.connect(self.home / "agentq.db")
        conn.execute("DELETE FROM proxy_minutes WHERE minute = ?", (minute_at(seconds_before_until),))
        conn.commit()
        conn.close()

    def summary(self) -> dict:
        return dash.build_summary(self.home, 1, now=NOW, fetch_live=proxy_status)

    def test_last_minute_tiles_read_the_latest_complete_minute_not_the_current_one(self) -> None:
        limits = self.summary()["limits"]
        self.assertEqual(limits["total"]["last_min_frac"], dash.fraction(100 + 7000 + 2429 + 1000 + 100, 10_000_000))
        self.assertEqual(limits["output"]["last_min_frac"], dash.fraction(7000, 70_000))

    def test_last_minute_falls_back_one_minute_when_the_latest_complete_minute_has_no_row(self) -> None:
        self.delete_minute(60)
        limits = self.summary()["limits"]
        self.assertEqual(limits["output"]["last_min_frac"], dash.fraction(500_000, 70_000))
        self.assertEqual(limits["total"]["last_min_frac"], dash.fraction(500_000, 10_000_000))

    def test_last_minute_tiles_are_none_when_neither_recent_minute_has_a_row(self) -> None:
        self.delete_minute(60)
        self.delete_minute(120)
        limits = self.summary()["limits"]
        self.assertIsNone(limits["output"]["last_min_frac"])
        self.assertIsNone(limits["total"]["last_min_frac"])

    def test_series_ends_at_the_last_minute_with_a_row_and_keeps_idle_gaps_as_zero(self) -> None:
        self.delete_minute(60)
        minutes = self.summary()["minutes"]
        self.assertEqual(minutes[-1]["minute"], minute_at(120))
        by_minute = {p["minute"]: p for p in minutes}
        self.assertNotIn(minute_at(60), by_minute)
        self.assertEqual(by_minute[minute_at(4 * 60)]["total"], 0)

    def test_deepseek_output_rate_averages_five_complete_minutes(self) -> None:
        # 11:59 = 7000, 11:58 = 500000, 11:57 = 5, 11:56 and 11:55 have no rows; the current minute is excluded.
        self.assertEqual(self.summary()["ds_output_5m_per_min"], (7000 + 500_000 + 5) / 5)


CALIB_DDL = """
CREATE TABLE proxy_calib (
    minute TEXT PRIMARY KEY, req INT, prompt_est INT, max_tokens INT, billed INT,
    first_429_s REAL, r429 INT
)
"""


class BucketAndTotalsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def raw_minutes(self, hours: int) -> list[dict]:
        since = UNTIL - hours * 3600
        return dash.minute_series(dash.read_minute_rows(self.home / "agentq.db", since, UNTIL), since, UNTIL)

    def test_24h_uses_five_minute_buckets_that_keep_every_token(self) -> None:
        summary = dash.build_summary(self.home, 24, now=NOW, fetch_live=proxy_status)
        self.assertEqual(summary["bucket_s"], 300)
        self.assertTrue(all(p["t"] % 300 == 0 for p in summary["minutes"]))
        self.assertEqual(sum(p["total"] for p in summary["minutes"]), sum(p["total"] for p in self.raw_minutes(24)))
        self.assertEqual(sum(p["r429"] for p in summary["minutes"]), sum(p["r429"] for p in self.raw_minutes(24)))

    def test_7d_uses_thirty_minute_buckets_and_averages_percent_over_members(self) -> None:
        summary = dash.build_summary(self.home, 168, now=NOW, fetch_live=proxy_status)
        self.assertEqual(summary["bucket_s"], 1800)
        self.assertTrue(all(p["t"] % 1800 == 0 for p in summary["minutes"]))
        members = [p for p in self.raw_minutes(168) if p["t"] // 1800 * 1800 == UNTIL // 1800 * 1800 - 1800 * 2]
        bucket = next(p for p in summary["minutes"] if p["t"] == UNTIL // 1800 * 1800 - 1800 * 2)
        self.assertEqual(bucket["minutes"], len(members))
        self.assertAlmostEqual(bucket["pct_ds_output"], sum(m["pct_ds_output"] for m in members) / len(members), places=3)
        self.assertEqual(bucket["total"], sum(m["total"] for m in members))

    def test_wait_p95_bucket_is_the_worst_minute(self) -> None:
        points = [
            {"t": 600, "wait_p95_ms": 900, **{k: 0 for k in dash.BUCKET_SUM_KEYS}, **{k: 0.0 for k in dash.PCT_KEYS}, "minute": "x"},
            {"t": 660, "wait_p95_ms": 2500, **{k: 0 for k in dash.BUCKET_SUM_KEYS}, **{k: 0.0 for k in dash.PCT_KEYS}, "minute": "x"},
            {"t": 720, "wait_p95_ms": None, **{k: 0 for k in dash.BUCKET_SUM_KEYS}, **{k: 0.0 for k in dash.PCT_KEYS}, "minute": "x"},
        ]
        bucket = dash.bucket_minutes(points, 300)
        self.assertEqual(len(bucket), 1)
        self.assertEqual(bucket[0]["wait_p95_ms"], 2500)

    def test_worker_samples_average_per_bucket_and_skip_missing_queue_counts(self) -> None:
        samples = [
            {"ts": 600.0, "ds_workers": 2, "kimi_workers": 0, "queue_workers": 1, "direct_workers": 1, "queued_jobs": None},
            {"ts": 660.0, "ds_workers": 4, "kimi_workers": 1, "queue_workers": 1, "direct_workers": 3, "queued_jobs": 6},
        ]
        rows = dash.bucket_workers(samples, 1800)
        self.assertEqual(rows, [{"ts": 0.0, "ds_workers": 3.0, "kimi_workers": 0.5, "queue_workers": 1.0,
                                 "direct_workers": 2.0, "queued_jobs": 6.0}])

    def test_cumulative_token_mix_is_a_running_sum_over_the_range(self) -> None:
        summary = dash.build_summary(self.home, 6, now=NOW, fetch_live=proxy_status)
        mix = summary["token_mix"]
        running = 0
        for row in mix:
            running += row["cache_read"] + row["uncached"] + row["output"]
            self.assertEqual(row["cumulative"], running)
        self.assertEqual(mix[-1]["cumulative"], sum(p["total"] for p in summary["minutes"]))

    def test_range_totals_sum_the_range_and_count_runs_and_verdicts(self) -> None:
        totals = dash.build_summary(self.home, 1, now=NOW, fetch_live=proxy_status)["range_totals"]
        # Inside the last hour (current minute excluded): requests 1+1+1+1+1 and 429s 0+1+0+3+0.
        self.assertEqual(totals["requests"], 5)
        self.assertEqual(totals["r429"], 4)
        self.assertEqual(totals["ds_output"], 7000 + 500000 + 5)
        self.assertEqual(totals["finished"], {"PASS": 1, "PARTIAL": 0, "FAIL": 0})
        self.assertEqual(totals["tokens"]["output"], 7000 + 100 + 500000 + 5)

    def test_runs_started_counts_runs_whose_start_is_inside_the_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_runs_home(Path(tmp))
            totals = dash.build_summary(home, 1, now=NOW, fetch_live=live_with_queue)["range_totals"]
            self.assertEqual(totals["runs_started"], 4)

    def test_admitted_total_is_used_where_calib_has_the_minute(self) -> None:
        conn = sqlite3.connect(self.home / "agentq.db")
        conn.execute(CALIB_DDL)
        conn.execute("INSERT INTO proxy_calib (minute, billed) VALUES (?, ?)", (minute_at(60), 4_000_000))
        conn.commit()
        conn.close()
        minutes = {p["minute"]: p for p in dash.build_summary(self.home, 1, now=NOW, fetch_live=proxy_status)["minutes"]}
        admitted = minutes[minute_at(60)]
        self.assertEqual(admitted["total_source"], "admitted")
        self.assertEqual(admitted["pct_total"], dash.fraction(4_000_000, 10_000_000))
        completed = minutes[minute_at(120)]
        self.assertEqual(completed["total_source"], "completed")
        self.assertEqual(completed["pct_total"], dash.fraction(500_000, 10_000_000))

    def test_missing_calib_table_falls_back_to_completion_totals(self) -> None:
        summary = dash.build_summary(self.home, 1, now=NOW, fetch_live=proxy_status)
        self.assertIn("admitted", summary["errors"])
        minute = next(p for p in summary["minutes"] if p["minute"] == minute_at(60))
        self.assertEqual(minute["total_source"], "completed")
        self.assertEqual(minute["pct_total"], dash.fraction(minute["total"], 10_000_000))


LOG_LINES = """\
2026-11-12T10:00:00Z agentq-proxy request job=aaaaaaaaaaaa model=m lane=urgent status=200
continuation for the first row
2026-11-12T10:00:01Z agentq-proxy request job=bbbbbbbbbbbb Authorization: Bearer sk-secret
  Authorization header continuation that must go
2026-11-12T10:00:02Z agentq-proxy retry job=cccccccccccc api-key=sk-hidden attempt=2
2026-11-12T10:00:03Z agentq-proxy done ok=1 status=200
"""


class LogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "agentq"
        (self.home / "logs").mkdir(parents=True)
        (self.home / "logs" / "proxy.err.log").write_text(LOG_LINES, encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_rows_are_newest_first_with_fields_and_continuations(self) -> None:
        body = dash.read_log(self.home, "proxy")
        self.assertEqual([r["event"] for r in body["rows"]], ["done", "request"])
        newest, first = body["rows"]
        self.assertEqual(newest["time"], "2026-11-12T10:00:03Z")
        self.assertEqual(newest["fields"], [{"k": "ok", "v": "1"}, {"k": "status", "v": "200"}])
        self.assertEqual(first["fields"][0], {"k": "job", "v": "aaaaaaaaaaaa"})
        self.assertEqual(first["continuation"], ["continuation for the first row"])

    def test_secret_lines_and_their_continuations_are_dropped(self) -> None:
        body = dash.read_log(self.home, "proxy")
        dumped = json.dumps(body)
        self.assertNotIn("Authorization", dumped)
        self.assertNotIn("api-key", dumped)
        self.assertNotIn("sk-secret", dumped)
        self.assertNotIn("must go", dumped)
        self.assertEqual(body["redacted_lines"], 3)
        self.assertEqual(body["total"], 2)

    def test_pages_hold_one_hundred_lines_newest_first(self) -> None:
        lines = "".join(f"2026-11-12T10:{i // 60:02d}:{i % 60:02d}Z agentq-proxy tick n={i}\n" for i in range(250))
        (self.home / "logs" / "proxy.err.log").write_text(lines, encoding="utf-8")
        first = dash.read_log(self.home, "proxy", 1)
        self.assertEqual((first["pages"], first["total"], len(first["rows"])), (3, 250, 100))
        self.assertEqual(first["rows"][0]["fields"][0]["v"], "249")
        last = dash.read_log(self.home, "proxy", 3)
        self.assertEqual(len(last["rows"]), 50)
        self.assertEqual(last["rows"][-1]["fields"][0]["v"], "0")
        clamped = dash.read_log(self.home, "proxy", 99)
        self.assertEqual(clamped["page"], 3)

    def test_filter_text_applies_before_paging(self) -> None:
        body = dash.read_log(self.home, "proxy", 1, "AAAAAAAAAAAA")
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["rows"][0]["event"], "request")
        self.assertEqual(dash.read_log(self.home, "proxy", 1, "cccccccccccc")["total"], 0)
        self.assertEqual(dash.read_log(self.home, "proxy", 1, "zzz-no-match")["rows"], [])

    def test_only_fixed_source_names_are_accepted(self) -> None:
        with self.assertRaises(ValueError):
            dash.read_log(self.home, "../../etc/passwd")
        server = dash.make_server(self.home, 0, fetch_live=no_proxy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            for path in ("/api/log?source=../x", "/api/log?source=proxy&page=abc", "/api/summary?hours=3"):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", path)
                self.assertEqual(conn.getresponse().status, 400, path)
                conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/summary?hours=168")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["bucket_s"], 1800)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_a_log_that_links_outside_the_logs_folder_is_refused(self) -> None:
        outside = Path(self.tmp.name) / "outside.log"
        outside.write_text("2026-11-12T10:00:00Z leak x=1\n", encoding="utf-8")
        link = self.home / "logs" / "broker.log"
        link.symlink_to(outside)
        body = dash.read_log(self.home, "broker")
        self.assertEqual(body["rows"], [])
        self.assertIn("outside", body["error"])

    def test_missing_file_is_an_empty_page_with_an_error(self) -> None:
        body = dash.read_log(self.home, "broker")
        self.assertEqual(body["rows"], [])
        self.assertEqual(body["error"], "log file not found")

    def test_tail_read_is_capped_and_drops_the_partial_first_line(self) -> None:
        path = self.home / "logs" / "proxy.err.log"
        path.write_text("".join(f"2026-11-12T10:00:{i:02d}Z agentq-proxy tick n={i}\n" for i in range(60)), encoding="utf-8")
        data, truncated = dash.read_log_tail(path, max_bytes=200)
        self.assertTrue(truncated)
        self.assertLessEqual(len(data), 200)
        self.assertTrue(data.startswith(b"2026-11-12T10:00:"))
        full, truncated_full = dash.read_log_tail(path)
        self.assertFalse(truncated_full)
        self.assertEqual(full.count(b"\n"), 60)


class TokenChartTest(unittest.TestCase):
    """The tokens-billed chart: one bar series, a Morph limit line per bucket, a secondary cumulative axis, and the mix caption."""

    @staticmethod
    def chart_block(html: str, chart_id: str) -> str:
        start = html.index(f'registerChart("{chart_id}"')
        return html[start:html.index("registerChart(", start + 1)]

    def summary(self, hours: int) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(Path(tmp))
            return dash.build_summary(home, hours, now=NOW, fetch_live=proxy_status, laptop=CALM)

    def test_tokens_chart_is_one_total_bar_series_and_the_cumulative_line(self) -> None:
        html = dash.PAGE_HTML
        block = html[html.index('registerChart("c-tokens"'):html.index("}, drawTokenCombo);")]
        self.assertIn('series: [{ key: "total", name: "Tokens billed", color: COLORS.s1 }]', block)
        self.assertEqual(block.count('key: "'), 1)
        self.assertNotIn("cache_read", block)
        self.assertNotIn("COLORS.s3", block)
        self.assertIn('{ name: "Cumulative (right axis)", color: COLORS.muted }', dash.PAGE_HTML)

    def test_limit_line_is_ten_million_per_minute_times_the_bucket_minutes(self) -> None:
        six = self.summary(6)
        self.assertEqual(six["token_limit"], {"per_minute": 10_000_000, "bucket_minutes": 5, "value": 50_000_000})
        week = self.summary(168)
        self.assertEqual(week["token_bucket_s"], 1800)
        self.assertEqual(week["token_limit"]["value"], 10_000_000 * 30)

    def test_left_axis_covers_the_limit_and_1_1_times_the_largest_bucket(self) -> None:
        for hours in (6, 168):
            summary = self.summary(hours)
            with self.subTest(hours=hours):
                axis = summary["token_axes"]["left"]
                peak = max(row["total"] for row in summary["token_mix"])
                self.assertGreaterEqual(axis["max"], summary["token_limit"]["value"] * 1.05)
                self.assertGreaterEqual(axis["max"], peak * 1.1)
                self.assertEqual(axis["ticks"][0], 0)
                self.assertEqual(axis["ticks"][-1], axis["max"])
                self.assertIn(len(axis["ticks"]), (4, 5, 6))

    def test_nice_axis_steps_are_round_and_cover_the_peak(self) -> None:
        self.assertEqual(dash.nice_axis(52_500_000), {"max": 60_000_000, "ticks": [0, 20_000_000, 40_000_000, 60_000_000]})
        self.assertEqual(dash.nice_axis(315_000_000)["ticks"], [0, 100_000_000, 200_000_000, 300_000_000, 400_000_000])
        cumulative = dash.nice_axis(1_300_000_000)
        self.assertEqual(cumulative["ticks"], [0, 500_000_000, 1_000_000_000, 1_500_000_000])
        self.assertEqual(dash.nice_axis(0)["max"], 1)

    def test_mix_shares_sum_to_one_and_match_the_range_totals(self) -> None:
        for hours in (6, 168):
            summary = self.summary(hours)
            totals = summary["token_totals"]
            with self.subTest(hours=hours):
                self.assertEqual(totals["total"], sum(row["total"] for row in summary["token_mix"]))
                self.assertAlmostEqual(sum(totals["shares"].values()), 1.0, places=9)
                self.assertEqual(totals["shares"]["output"], totals["output"] / totals["total"])

    def test_mix_shares_are_none_when_nothing_was_billed(self) -> None:
        totals = dash.token_totals([])
        self.assertEqual(totals["total"], 0)
        self.assertEqual(totals["shares"], {"cache_read": None, "uncached": None, "output": None})

    def test_tick_labels_have_no_trailing_point_zero(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        match = re.search(r"\n  function fmtTokens\(n\) \{.*?\n  \}\n", dash.PAGE_HTML, re.S)
        self.assertIsNotNone(match)
        values = [0, 500_000, 2_000_000, 2_500_000, 20_000_000, 60_000_000, 500_000_000, 1_000_000_000, 1_500_000_000, 300_000_000]
        script = match.group(0) + "\nconsole.log(JSON.stringify(%s.map(fmtTokens)));" % json.dumps(values)
        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "fmt.js"
            script_path.write_text(script, encoding="utf-8")
            out = subprocess.run([node, str(script_path)], capture_output=True, text=True, timeout=30, check=True)
        labels = json.loads(out.stdout)
        self.assertEqual(labels, ["0", "500K", "2M", "2.5M", "20M", "60M", "500M", "1B", "1.5B", "300M"])
        self.assertFalse([label for label in labels if ".0" in label])

    def test_page_titles_the_chart_by_bucket_and_states_the_mix_caption_slot(self) -> None:
        html = dash.PAGE_HTML
        self.assertIn('"Tokens billed per " + tunit', html)
        self.assertIn('setText("tok-mix", mixCaption(d.token_totals));', html)
        self.assertIn('id="tok-mix"', html)
        self.assertIn('rightLabel: "cumulative"', html)
        self.assertIn('"stroke-width": 1.5', html)
        self.assertIn('group: "capacity", bucket_s: tb', html)


if __name__ == "__main__":
    unittest.main()


OLD_MINUTES_DDL = """
CREATE TABLE proxy_minutes (
    minute TEXT NOT NULL, model TEXT NOT NULL,
    req INT, ok INT, r429 INT,
    input INT, cache_read INT, output INT, uncached_est INT,
    wait_p50_ms INT, wait_p95_ms INT, target REAL,
    PRIMARY KEY (minute, model)
)
"""
LATENCY_ROW_COLUMNS = (
    "minute, model, req, ok, r429, input, cache_read, output, uncached_est, wait_p50_ms, wait_p95_ms, target, "
    "upstream_ms_sum, upstream_n, upstream_p50_ms, upstream_p95_ms, ttfb_p50_ms, in_flight_max"
)


def write_latency_home(tmp: Path, rows: list[tuple]) -> Path:
    home = make_home(tmp, minutes=False, jobs=False)
    connection = sqlite3.connect(home / "agentq.db")
    connection.execute(PROXY_MINUTES_DDL)
    connection.executemany(f"INSERT INTO proxy_minutes ({LATENCY_ROW_COLUMNS}) VALUES ({','.join('?' * 18)})", rows)
    connection.commit()
    connection.close()
    return home


def latency_row(minute: str, model: str, n: int, p50: int | None, p95: int | None) -> tuple:
    total_ms = None if p50 is None else p50 * n
    return (minute, model, n, n, 0, 0, 0, 0, 0, 0, 0, 0.9, total_ms, n if p50 is not None else None, p50, p95, None, 1)


class UpstreamLatencyDashTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.rows = [
            latency_row(minute_at(60), "morph-dsv41flash-1017", 2, 1000, 2000),
            latency_row(minute_at(60), "morph-kimik31", 4, 500, 900),
            latency_row(minute_at(120), "morph-dsv41flash-1017", 3, None, None),
            latency_row(minute_at(180), "morph-dsv41flash-1017", 2, 300, 700),
        ]
        self.home = write_latency_home(Path(self.tmp.name), self.rows)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def summary(self, live=None, hours: int = 24) -> dict:
        return dash.build_summary(self.home, hours, now=NOW, fetch_live=lambda: live)

    def point(self, minutes_ago: int, home: Path | None = None) -> dict:
        since = UNTIL - 24 * 3600
        points = dash.minute_series(dash.read_minute_rows((home or self.home) / "agentq.db", since, UNTIL), since, UNTIL)
        return next(p for p in points if p["t"] == UNTIL - minutes_ago * 60)

    def test_minute_point_combines_models_by_request_weighted_p50_and_worst_p95(self) -> None:
        point = self.point(1)
        self.assertEqual(point["upstream_p50_ms"], round((1000 * 2 + 500 * 4) / 6))
        self.assertEqual(point["upstream_p95_ms"], 2000)
        self.assertEqual(point["upstream_n"], 6)

    def test_rows_without_latency_give_null_points_not_zeros(self) -> None:
        point = self.point(2)
        self.assertIsNone(point["upstream_p50_ms"])
        self.assertIsNone(point["upstream_p95_ms"])

    def test_bucket_averages_p50_and_takes_the_worst_p95(self) -> None:
        points = [
            {"t": 600, "wait_p95_ms": None, "upstream_p50_ms": 1000, "upstream_p95_ms": 2000,
             **{k: 0 for k in dash.BUCKET_SUM_KEYS}, **{k: 0.0 for k in dash.PCT_KEYS}, "minute": "x"},
            {"t": 660, "wait_p95_ms": None, "upstream_p50_ms": 500, "upstream_p95_ms": 900,
             **{k: 0 for k in dash.BUCKET_SUM_KEYS}, **{k: 0.0 for k in dash.PCT_KEYS}, "minute": "x"},
            {"t": 720, "wait_p95_ms": None, "upstream_p50_ms": None, "upstream_p95_ms": None,
             **{k: 0 for k in dash.BUCKET_SUM_KEYS}, **{k: 0.0 for k in dash.PCT_KEYS}, "minute": "x"},
        ]
        bucket = dash.bucket_minutes(points, 300)[0]
        self.assertEqual(bucket["upstream_p50_ms"], 750)
        self.assertEqual(bucket["upstream_p95_ms"], 2000)

    def test_perf_kpi_is_request_weighted_over_minutes_that_have_latency(self) -> None:
        perf = self.summary()["perf_kpis"]
        self.assertEqual(perf["upstream_response_p50_ms"], round((4000 + 600) / 8))
        self.assertEqual(perf["upstream_response_p95_ms"], 2000)

    def test_in_flight_now_sums_the_live_status_across_models(self) -> None:
        live = {"ok": True, "budgets": {}, "upstream": {"in_flight": {"morph-dsv41flash-1017": 3, "morph-kimik31": 1}}}
        self.assertEqual(self.summary(live)["perf_kpis"]["in_flight_now"], 4)

    def test_in_flight_now_is_null_without_a_live_status(self) -> None:
        self.assertIsNone(self.summary(None)["perf_kpis"]["in_flight_now"])

    def test_old_schema_without_latency_columns_still_charts_the_counts(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        home = make_home(tmp, minutes=False, jobs=False)
        connection = sqlite3.connect(home / "agentq.db")
        connection.execute(OLD_MINUTES_DDL)
        connection.execute(
            "INSERT INTO proxy_minutes (minute, model, req, ok, r429, input, cache_read, output, uncached_est, "
            "wait_p50_ms, wait_p95_ms, target) VALUES (?,?,5,5,1,100,0,50,0,200,800,0.9)",
            (minute_at(60), "morph-dsv41flash-1017"),
        )
        connection.commit()
        connection.close()
        summary = dash.build_summary(home, 24, now=NOW, fetch_live=no_proxy)
        self.assertNotIn("minutes", summary["errors"])
        point = self.point(1, home)
        self.assertEqual(point["req"], 5)
        self.assertEqual(point["wait_p95_ms"], 800)
        self.assertIsNone(point["upstream_p50_ms"])
        self.assertIsNone(summary["perf_kpis"]["upstream_response_p50_ms"])
        self.assertIsNone(summary["perf_kpis"]["upstream_response_p95_ms"])

    def test_page_has_the_paired_capacity_charts_the_response_chart_and_the_tiles(self) -> None:
        for marker in (
            "Capacity vs workers",
            'id="c-billed"',
            'id="c-workers"',
            'id="c-upstream"',
            'id="p-morph"',
            'id="p-inflight"',
            "Morph response time per request (s), p50 and p95",
            "function compact(",
        ):
            self.assertIn(marker, dash.PAGE_HTML)


class YAxisLabelTest(unittest.TestCase):
    LABELS = {
        "c-billed": 'labelled(pctAxis, "% of 10M tokens/min")',
        "c-workers": 'labelled({ min: 0, max: axis.max, ticks: axis.ticks, fmt: intFmt }, "runs / jobs")',
        "c-kimi": 'labelled(smallPctAxis, "% of 24,290 tokens/min")',
        "c-dsu": 'labelled(smallPctAxis, "% of 400k tokens/min")',
        "c-429": 'labelled(niceTicks(Math.max(maxR, 1), 2), "429s per minute")',
        "c-wait": 'labelled({ min: 0, max: waitTop, ticks: [0, waitTop / 2, waitTop], fmt: waitFmt }, "seconds")',
        "c-upstream": 'labelled(niceTicks(Math.max(maxU, 1), 3), "seconds per request")',
    }

    def config_block(self, chart_id: str) -> str:
        html = dash.PAGE_HTML
        start = html.index(f'registerChart("{chart_id}"')
        end = html.find("registerChart(", start + 1)
        return html[start:end if end != -1 else len(html)]

    def test_every_registered_line_and_bar_chart_has_a_y_label(self) -> None:
        html = dash.PAGE_HTML
        registered = set(re.findall(r'registerChart\("(c-[a-z0-9-]+)"', html))
        self.assertEqual(registered, set(self.LABELS) | {"c-tokens"})
        for chart_id, label in self.LABELS.items():
            with self.subTest(chart=chart_id):
                self.assertIn(label, self.config_block(chart_id))

    def test_the_label_is_drawn_above_the_plot_in_the_muted_tick_class(self) -> None:
        html = dash.PAGE_HTML
        self.assertIn('if (cfg.y.label) svgEl("text", { x: m.l, y: 12, class: "tick" }', html)
        self.assertIn('svgEl("text", { x: m.l, y: 12, class: "tick" }, svg).textContent = cfg.yLabel;', html)
        self.assertIn('svgEl("text", { x: W - 2, y: 12, "text-anchor": "end", class: "tick" }, svg).textContent = cfg.rightLabel;', html)
        self.assertIn('yLabel: "tokens per " + tunit,', html)

    def test_pair_layout_is_shared_by_both_capacity_charts_and_reserves_the_label_row(self) -> None:
        html = dash.PAGE_HTML
        self.assertIn("var PAIR_LAYOUT = { height: 260, margin: { l: 64, r: 16, t: 26, b: 24 } };", html)
        for chart_id in ("c-billed", "c-workers", "c-upstream"):
            with self.subTest(chart=chart_id):
                self.assertIn('margin: PAIR_LAYOUT.margin, group: "capacity"', self.config_block(chart_id))
        self.assertEqual(html.count("margin: PAIR_LAYOUT.margin"), 3)


class TrailingAverageTest(unittest.TestCase):
    def test_five_minute_trailing_mean_with_gaps_before_the_first_full_window(self) -> None:
        points = [{"pct_total": v} for v in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7)]
        dash.add_trailing_average(points, "pct_total", "pct_total_avg5", 5)
        self.assertEqual([p["pct_total_avg5"] for p in points[:4]], [None, None, None, None])
        self.assertEqual([p["pct_total_avg5"] for p in points[4:]], [0.3, 0.4, 0.5])

    def test_bucketed_points_average_the_minute_averages_they_contain(self) -> None:
        minutes = [dash.minute_point(60 * i, {"total": (i + 1) * 1_000_000}) for i in range(10)]
        dash.add_trailing_average(minutes, "pct_total", "pct_total_avg5", 5)
        buckets = dash.bucket_minutes(minutes, 300)
        self.assertEqual([b["pct_total_avg5"] for b in buckets], [0.3, 0.6])


class EnforcedLimitsChartTest(unittest.TestCase):
    def test_main_billed_chart_has_the_per_minute_line_and_its_five_minute_average(self) -> None:
        html = dash.PAGE_HTML
        start = html.index('registerChart("c-billed"')
        block = html[start:html.index('registerChart("c-workers"')]
        self.assertEqual(block.count('key: "pct_'), 2)
        self.assertIn('{ key: "pct_total", name: "Per minute", color: COLORS.s1, width: 1.25, opacity: 0.4 }', block)
        self.assertIn('{ key: "pct_total_avg5", name: "5-min average", color: COLORS.s2, width: 2.5 }', block)
        self.assertIn("Morph limit (10M/min)", block)
        self.assertNotIn("pct_ds_uncached", block)
        self.assertNotIn("70k", block)

    def test_ds_uncached_share_is_of_the_400k_limit(self) -> None:
        point = dash.minute_point(0, {"ds_uncached": 200_000})
        self.assertEqual(point["pct_ds_uncached"], 0.5)

    def test_page_has_at_most_twelve_kpi_tiles(self) -> None:
        import re
        tiles = re.findall(r'class="kpi(?: head)?"', dash.PAGE_HTML)
        self.assertLessEqual(len(tiles), 12)


LIMITS_NOW = {"total": 10_000_000, "output": 70_000, "uncached": 24_290, "uncached_ds": 400_000}
CALM = {"load1": 2.0, "cpus": 15, "load_per_cpu": 2.0 / 15, "memory_free_pct": 60.0}
HOT = {"load1": 44.0, "cpus": 15, "load_per_cpu": 44.0 / 15, "memory_free_pct": 40.0}
def live_queued(total=0, output=0, uncached=0, uncached_ds=0) -> dict:
    """A proxy status whose budgets hold the given number of waiting requests each, all in the normal lane."""
    waits = {"total": total, "output": output, "uncached": uncached, "uncached_ds": uncached_ds}
    return {"budgets": {name: {"queued": {"urgent": 0, "normal": n, "backlog": 0}} for name, n in waits.items()}}


IDLE_LIVE = live_queued()


def five_minutes(**fields) -> dict:
    """Minute rows for the five complete minutes before UNTIL, each with the same fields."""
    return {UNTIL - 60 * k: fields for k in range(1, 6)}


def verdict_for(raw, *, live=IDLE_LIVE, laptop=CALM, kimi_jobs_waiting=0, budget_429s=None,
                ds_jobs_ready=0, ds_p50_5m_ms=None, ds_p50_base_ms=None):
    return dash.verdict(
        raw, UNTIL, LIMITS_NOW, live, kimi_jobs_waiting=kimi_jobs_waiting, budget_429s=budget_429s or {},
        laptop=laptop, ds_jobs_ready=ds_jobs_ready, ds_p50_5m_ms=ds_p50_5m_ms, ds_p50_base_ms=ds_p50_base_ms,
    )


class LimitedByTest(unittest.TestCase):
    def test_morph_limit_when_the_total_pool_averages_90_percent(self) -> None:
        result = verdict_for(five_minutes(total=9_500_000))
        self.assertEqual(result["key"], "morph_limit")
        self.assertEqual(result["chip"], "Morph limit")
        self.assertIn("Total pool averaged 95% of its limit over the last 5 minutes", result["text"])

    def test_deepseek_uncached_average_alone_is_not_morph_limit(self) -> None:
        result = verdict_for(five_minutes(total=1_000_000, ds_uncached=380_000))
        self.assertNotEqual(result["key"], "morph_limit")
        self.assertNotIn("uncached", result["text"])

    def test_morph_limit_when_deepseek_uncached_gets_429s_in_two_minutes(self) -> None:
        raw = five_minutes(total=1_000_000, ds_uncached=40_000)
        result = verdict_for(raw, budget_429s={"uncached_ds": {UNTIL - 60, UNTIL - 180}})
        self.assertEqual(result["key"], "morph_limit")
        self.assertIn("DeepSeek uncached input returned 429s in 2 of the last 5 minutes", result["text"])

    def test_one_deepseek_uncached_429_minute_is_not_enough(self) -> None:
        result = verdict_for(five_minutes(total=1_000_000), budget_429s={"uncached_ds": {UNTIL - 60}})
        self.assertNotEqual(result["key"], "morph_limit")

    def test_a_single_peak_minute_with_a_low_average_is_not_morph_limit(self) -> None:
        raw = five_minutes(total=1_000_000, ds_uncached=40_000)
        raw[UNTIL - 120] = {"total": 1_000_000, "ds_uncached": 507_000}
        result = verdict_for(raw)
        self.assertNotEqual(result["key"], "morph_limit")
        self.assertNotIn("peak minute 127%", result["text"])

    def test_peak_minute_is_context_in_the_sentence_when_morph_limit_holds_on_average(self) -> None:
        raw = five_minutes(total=9_000_000)
        raw[UNTIL - 120] = {"total": 12_700_000}
        result = verdict_for(raw)
        self.assertEqual(result["key"], "morph_limit")
        self.assertIn("peak minute 127%", result["text"])

    def test_morph_limit_when_an_enforced_budget_had_429s_in_two_minutes(self) -> None:
        raw = five_minutes(total=1_000_000)
        result = verdict_for(raw, budget_429s={"total": {UNTIL - 60, UNTIL - 180}})
        self.assertEqual(result["key"], "morph_limit")
        self.assertIn("Total pool returned 429s in 2 of the last 5 minutes", result["text"])

    def test_one_429_minute_is_not_enough(self) -> None:
        result = verdict_for(five_minutes(total=1_000_000), budget_429s={"total": {UNTIL - 60}})
        self.assertNotEqual(result["key"], "morph_limit")

    def test_saturated_kimi_never_makes_the_headline_morph_limit(self) -> None:
        raw = five_minutes(total=5_000_000, kimi_uncached=24_290)
        for waiting in (0, 1):
            with self.subTest(kimi_jobs_waiting=waiting):
                result = verdict_for(raw, kimi_jobs_waiting=waiting, budget_429s={"uncached": {UNTIL - 60, UNTIL - 180}})
                self.assertNotEqual(result["key"], "morph_limit")
                self.assertNotIn("Kimi", result["text"])
        self.assertEqual(verdict_for(raw, kimi_jobs_waiting=0)["key"], "headroom")

    def test_kimi_waits_are_only_the_secondary_line(self) -> None:
        result = verdict_for(five_minutes(total=5_000_000, kimi_uncached=24_290), kimi_jobs_waiting=1)
        self.assertEqual(result["key"], "headroom")
        self.assertEqual(result["secondary"], "Kimi reviews waiting: 1 (Kimi's own 24,290/min uncached limit).")
        self.assertIsNone(verdict_for(five_minutes(total=5_000_000))["secondary"])

    def test_morph_slow_when_deepseek_p50_doubles_and_proxy_queue_is_empty(self) -> None:
        result = verdict_for(five_minutes(total=1_000_000), ds_p50_5m_ms=12_000, ds_p50_base_ms=5_000)
        self.assertEqual(result["key"], "morph_slow")
        self.assertEqual(result["chip"], "Morph slow")

    def test_morph_slow_needs_an_empty_proxy_queue(self) -> None:
        result = verdict_for(five_minutes(total=1_000_000), live=live_queued(output=3), ds_p50_5m_ms=12_000, ds_p50_base_ms=5_000)
        self.assertNotEqual(result["key"], "morph_slow")

    def test_a_waiting_kimi_request_does_not_block_morph_slow(self) -> None:
        live = live_queued(uncached=1)
        result = verdict_for(five_minutes(total=1_000_000), live=live, kimi_jobs_waiting=1,
                             ds_p50_5m_ms=12_000, ds_p50_base_ms=5_000)
        self.assertEqual(result["key"], "morph_slow")

    def test_laptop_when_load_per_cpu_is_over_1_5(self) -> None:
        result = verdict_for(five_minutes(total=1_000_000), laptop=HOT)
        self.assertEqual(result["key"], "laptop")
        self.assertIn("load 44.0 on 15 CPUs", result["text"])

    def test_laptop_when_memory_free_is_under_25_percent(self) -> None:
        low_memory = dict(CALM, memory_free_pct=20.0)
        self.assertEqual(verdict_for(five_minutes(total=1_000_000), laptop=low_memory)["key"], "laptop")

    def test_near_capacity_when_the_pool_averages_85_percent(self) -> None:
        result = verdict_for(five_minutes(total=8_500_000))
        self.assertEqual(result["key"], "near_capacity")
        self.assertEqual(result["chip"], "Near capacity")
        self.assertEqual(result["text"], "Near capacity: pool at 85% of Morph's 10M/min limit.")

    def test_near_capacity_wins_over_proxy_pacing(self) -> None:
        result = verdict_for(five_minutes(total=8_800_000), live=live_queued(total=2))
        self.assertEqual(result["key"], "near_capacity")

    def test_proxy_pacing_names_the_deepseek_budget_with_waiting_requests(self) -> None:
        result = verdict_for(five_minutes(total=4_100_000), live=live_queued(total=2))
        self.assertEqual(result["key"], "proxy_pacing")
        self.assertEqual(result["chip"], "Proxy pacing")
        self.assertEqual(result["text"], "Proxy pacing: DeepSeek requests are waiting on Total pool while the pool is at 41%.")

    def test_proxy_pacing_names_deepseek_uncached_input_when_that_budget_waits(self) -> None:
        result = verdict_for(five_minutes(total=4_100_000), live=live_queued(uncached_ds=3))
        self.assertEqual(result["key"], "proxy_pacing")
        self.assertIn("waiting on DeepSeek uncached input", result["text"])

    def test_a_waiting_kimi_request_is_not_deepseek_pacing(self) -> None:
        live = live_queued(total=1, uncached=1)
        result = verdict_for(five_minutes(total=4_100_000), live=live, kimi_jobs_waiting=1)
        self.assertNotEqual(result["key"], "proxy_pacing")

    def test_launching_when_deepseek_jobs_are_ready_and_nothing_waits_at_the_proxy(self) -> None:
        result = verdict_for(five_minutes(total=4_100_000), ds_jobs_ready=3)
        self.assertEqual(result["key"], "launching")
        self.assertEqual(result["chip"], "Launching")
        self.assertEqual(result["text"], "Launching: 3 DeepSeek jobs are ready; the broker is starting them.")

    def test_launching_when_the_queued_details_are_empty(self) -> None:
        result = dash.verdict(
            five_minutes(total=4_100_000), UNTIL, LIMITS_NOW, IDLE_LIVE, kimi_jobs_waiting=0, budget_429s={},
            laptop=CALM, ds_jobs_ready=2, ds_p50_5m_ms=None, ds_p50_base_ms=None, ds_holds=["", None],
        )
        self.assertEqual(result["key"], "launching")
        self.assertEqual(result["chip"], "Launching")
        self.assertEqual(result["text"], "Launching: 2 DeepSeek jobs are ready; the broker is starting them.")

    def test_held_jobs_on_the_laptop_gate_and_ssh_slots_name_the_laptop_chip(self) -> None:
        details = ["hold tests: load 1.40 per cpu >= 1"] * 4 + ["hold ssh: slots full"] * 2
        result = dash.verdict(
            five_minutes(total=4_100_000), UNTIL, LIMITS_NOW, IDLE_LIVE, kimi_jobs_waiting=0, budget_429s={},
            laptop=CALM, ds_jobs_ready=6, ds_p50_5m_ms=None, ds_p50_base_ms=None,
            ds_holds=details, ssh_in_use=8,
        )
        self.assertEqual(result["key"], "held_laptop")
        self.assertEqual(result["chip"], "Laptop")
        self.assertEqual(
            result["text"],
            "6 DeepSeek jobs held for laptop headroom: 4 test jobs (load 1.40 per CPU, limit 1.0); "
            "2 jobs waiting for a production ssh slot (8 in use).",
        )

    def test_all_jobs_held_on_ssh_slots_name_the_ssh_chip(self) -> None:
        result = dash.verdict(
            five_minutes(total=4_100_000), UNTIL, LIMITS_NOW, IDLE_LIVE, kimi_jobs_waiting=0, budget_429s={},
            laptop=CALM, ds_jobs_ready=2, ds_p50_5m_ms=None, ds_p50_base_ms=None,
            ds_holds=["hold ssh: slots full", "hold ssh: slots full"], ssh_in_use=8,
        )
        self.assertEqual(result["key"], "held_ssh")
        self.assertEqual(result["chip"], "SSH slots")
        self.assertEqual(result["text"], "2 jobs waiting for a production ssh slot (8 in use).")

    def test_held_jobs_waiting_on_a_workspace_or_the_proxy_name_their_chip(self) -> None:
        workspace = dash.verdict(
            five_minutes(total=4_100_000), UNTIL, LIMITS_NOW, IDLE_LIVE, kimi_jobs_waiting=0, budget_429s={},
            laptop=CALM, ds_jobs_ready=1, ds_p50_5m_ms=None, ds_p50_base_ms=None,
            ds_holds=["hold pi-deepseek-flash: workspace busy"],
        )
        self.assertEqual(workspace["chip"], "Workspace")
        self.assertEqual(workspace["text"], "1 write job waiting for its workspace.")
        held_proxy = dash.verdict(
            five_minutes(total=4_100_000), UNTIL, LIMITS_NOW, IDLE_LIVE, kimi_jobs_waiting=0, budget_429s={},
            laptop=CALM, ds_jobs_ready=1, ds_p50_5m_ms=None, ds_p50_base_ms=None,
            ds_holds=["hold pi-deepseek-flash: proxy total waiting 3 > 0"],
        )
        self.assertEqual(held_proxy["key"], "held_proxy")
        self.assertEqual(held_proxy["chip"], "Proxy pacing")

    def test_headroom_when_the_pool_is_below_85_and_nothing_waits(self) -> None:
        result = verdict_for(five_minutes(total=4_100_000))
        self.assertEqual(result["key"], "headroom")
        self.assertEqual(result["chip"], "Headroom")
        self.assertEqual(result["text"], "Headroom: pool at 41% of Morph's limit; room for more work.")
        self.assertIsNone(result["secondary"])

    def test_live_case_kimi_review_held_by_its_own_budget_is_headroom_with_a_kimi_line(self) -> None:
        live = live_queued(total=1, uncached=1)
        result = verdict_for(five_minutes(total=4_100_000), live=live, kimi_jobs_waiting=1, ds_jobs_ready=0)
        self.assertEqual(result["key"], "headroom")
        self.assertEqual(result["text"], "Headroom: pool at 41% of Morph's limit; room for more work.")
        self.assertEqual(result["secondary"], "Kimi reviews waiting: 1 (Kimi's own 24,290/min uncached limit).")

    def test_headroom_when_the_proxy_is_unreachable_and_no_deepseek_job_is_ready(self) -> None:
        result = verdict_for(five_minutes(total=4_100_000), live=None)
        self.assertEqual(result["key"], "headroom")

    def test_proxy_waiting_by_budget_takes_kimi_out_of_the_total_queue(self) -> None:
        live = live_queued(total=2, output=1, uncached=2, uncached_ds=4)
        self.assertEqual(dash.proxy_waiting_by_budget(live), {"total": 0, "output": 1, "uncached_ds": 4})
        self.assertIsNone(dash.proxy_waiting_by_budget(None))

    def test_proxy_waiting_by_budget_reads_deepseek_family_counts_when_the_proxy_reports_them(self) -> None:
        def budget(ds, kimi):
            return {
                "queued": {"urgent": 0, "normal": ds + kimi, "backlog": 0},
                "queued_by_family": {"morph-dsv41flash": ds, "morph-kimik31": kimi, "other": 0},
            }

        live = {"budgets": {
            "total": budget(ds=3, kimi=19),
            "output": budget(ds=1, kimi=0),
            "uncached": budget(ds=0, kimi=19),
            "uncached_ds": budget(ds=4, kimi=0),
        }}
        self.assertEqual(dash.proxy_waiting_by_budget(live), {"total": 3, "output": 1, "uncached_ds": 4})


class RoomForWorkersTest(unittest.TestCase):
    def room_for(self, raw, *, workers, laptop=CALM):
        return dash.room(raw, UNTIL, LIMITS_NOW, active_ds_workers=workers, laptop=laptop)

    def test_bounded_by_morph_uncached_input(self) -> None:
        raw = five_minutes(total=1_000_000, ds_uncached=300_000)
        result = self.room_for(raw, workers=50)
        self.assertEqual(result["bound"], "uncached_ds")
        self.assertEqual(result["workers"], 13)
        self.assertEqual(result["text"], "Room for about 13 more busy DeepSeek workers (bounded by Morph uncached input).")

    def test_bounded_by_morph_total(self) -> None:
        raw = five_minutes(total=9_000_000, ds_uncached=0)
        result = self.room_for(raw, workers=50)
        self.assertEqual(result["bound"], "total")
        self.assertEqual(result["workers"], 2)
        self.assertIn("bounded by Morph total", result["text"])

    def test_bounded_by_laptop_cpu_leaves_no_room(self) -> None:
        raw = five_minutes(total=1_000_000, ds_uncached=100_000)
        result = self.room_for(raw, workers=50, laptop=HOT)
        self.assertEqual(result["bound"], "laptop")
        self.assertEqual(result["workers"], 0)
        self.assertEqual(result["text"], "No room; adding workers will only queue (bounded by laptop CPU).")

    def test_no_active_workers_means_room_is_unknown(self) -> None:
        result = self.room_for(five_minutes(total=1_000_000), workers=None)
        self.assertIsNone(result["workers"])
        self.assertIn("Room unknown", result["text"])


class ActivityWindowTest(unittest.TestCase):
    def test_five_and_fifteen_minute_totals_with_deltas(self) -> None:
        raw = {UNTIL - 60 * k: {"total": 100, "req": 2} for k in range(1, 6)}
        raw.update({UNTIL - 60 * k: {"total": 50, "req": 1} for k in range(6, 11)})
        five = dash.window_tokens(raw, UNTIL, 5)
        self.assertEqual((five["tokens"], five["requests"], five["previous_tokens"]), (500, 10, 250))
        self.assertEqual(five["delta"], 1.0)
        fifteen = dash.window_tokens(raw, UNTIL, 15)
        self.assertEqual(fifteen["tokens"], 750)
        self.assertIsNone(fifteen["delta"])

    def test_live_limits_override_the_constants(self) -> None:
        live = {"budgets": {"uncached_ds": {"limit": 400_000}, "total": {"limit": 9_000_000}}}
        resolved = dash.resolve_limits(live)
        self.assertEqual(resolved["uncached_ds"], 400_000)
        self.assertEqual(resolved["total"], 9_000_000)
        self.assertEqual(dash.resolve_limits(None)["uncached"], 24_290)


class SummaryVerdictTest(unittest.TestCase):
    def test_summary_carries_verdict_room_activity_and_five_minute_token_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(Path(tmp))
            summary = dash.build_summary(home, 6, now=NOW, fetch_live=proxy_status, laptop=CALM)
        self.assertIn(summary["verdict"]["key"], {"morph_limit", "morph_slow", "laptop", "near_capacity", "proxy_pacing", "launching", "headroom", "held_laptop", "held_ssh", "held_proxy", "held_workspace", "held_other"})
        self.assertIn("text", summary["room"])
        self.assertIn("tokens_5m", summary["activity"])
        self.assertEqual(summary["token_bucket_s"], 300)
        self.assertEqual({p["t"] % 300 for p in summary["token_mix"]}, {0})


class BudgetRateLimitLogTest(unittest.TestCase):
    def test_calib_lines_give_the_minutes_each_budget_was_429d(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "logs").mkdir()
            this = dash.minute_stamp(UNTIL - 60)
            old = dash.minute_stamp(UNTIL - 3600)
            (home / "logs" / "proxy.err.log").write_text(
                f"{this}:30Z agentq-proxy calib minute={this} budget=total sec=30.0\n"
                f"{old}:30Z agentq-proxy calib minute={old} budget=total sec=30.0\n"
                f"{this}:31Z agentq-proxy calib minute={this} budget=None sec=31.0\n",
                encoding="utf-8",
            )
            found = dash.budget_429_minutes(home, UNTIL)
        self.assertEqual(found, {"total": {UNTIL - 60}})


class RunsAxisTest(unittest.TestCase):
    CAPS = {"deepseek": 96, "kimi": 2}

    def workers(self, ds: int, kimi: int = 0, queued: int = 0) -> list[dict]:
        return [{"ts": 0.0, "ds_workers": ds, "kimi_workers": kimi, "queued_jobs": queued}]

    def test_axis_top_follows_the_data_not_the_96_cap(self) -> None:
        axis = dash.runs_axis(self.workers(ds=55, kimi=15, queued=3), self.CAPS)
        self.assertEqual(axis["max"], 75)
        self.assertEqual(axis["ticks"], [0, 25, 50, 75])

    def test_axis_has_a_floor_of_ten_and_integer_ticks(self) -> None:
        empty = dash.runs_axis([], self.CAPS)
        self.assertEqual(empty["max"], 10)
        self.assertEqual(empty["ticks"], [0, 5, 10])
        for peak in (1, 7, 13, 40, 97, 310):
            axis = dash.runs_axis(self.workers(ds=peak), self.CAPS)
            self.assertGreaterEqual(axis["max"], peak * 1.15)
            self.assertEqual(axis["ticks"][0], 0)
            self.assertEqual(axis["ticks"][-1], axis["max"])
            self.assertTrue(all(isinstance(t, int) for t in axis["ticks"]), peak)
            self.assertTrue(2 <= len(axis["ticks"]) - 1 <= 5, peak)

    def test_cap_above_the_axis_is_off_scale_and_cap_inside_is_on_scale(self) -> None:
        axis = dash.runs_axis(self.workers(ds=55, kimi=15), self.CAPS)
        by_label = {cap["label"]: cap for cap in axis["caps"]}
        self.assertEqual(by_label["DeepSeek cap 96"]["on_scale"], False)
        self.assertEqual(by_label["Kimi cap 2"]["on_scale"], True)
        inside = dash.runs_axis(self.workers(ds=55), {"deepseek": 64, "kimi": 2})
        self.assertTrue(all(cap["on_scale"] for cap in inside["caps"]))
        self.assertEqual(inside["max"], 75)

    def test_summary_carries_the_runs_axis_for_the_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(Path(tmp))
            summary = dash.build_summary(home, 1, now=NOW, fetch_live=lambda: None)
        self.assertEqual(set(summary["runs_axis"]), {"max", "ticks", "caps"})
        self.assertEqual(summary["runs_axis"]["max"], dash.runs_axis(summary["workers"], summary["caps"])["max"])

    def test_workers_chart_reads_the_axis_and_labels_off_scale_caps(self) -> None:
        html = dash.PAGE_HTML
        start = html.index('registerChart("c-workers"')
        block = html[start:html.index("var mix =", start)]
        self.assertIn('labelled({ min: 0, max: axis.max, ticks: axis.ticks, fmt: intFmt }, "runs / jobs")', block)
        self.assertNotIn("niceTicks(Math.max(caps.deepseek", block)
        self.assertIn("c.on_scale", block)
        self.assertIn("(off scale)", block)
        self.assertIn("height: PAIR_LAYOUT.height", block)
        self.assertIn("margin: PAIR_LAYOUT.margin", block)

    def test_draw_chart_places_off_scale_labels_top_right_without_a_line(self) -> None:
        html = dash.PAGE_HTML
        start = html.index("(cfg.offScale || []).forEach")
        block = html[start:html.index("var refPlaced", start)]
        self.assertIn('"text-anchor": "end"', block)
        self.assertIn("y: m.t + 13", block)
        self.assertNotIn('"stroke-dasharray"', block)


class SmallChartRowTest(unittest.TestCase):
    SMALL = ("c-kimi", "c-dsu", "c-429", "c-wait")

    def test_small_charts_read_the_last_hour_not_the_selected_range(self) -> None:
        html = dash.PAGE_HTML
        self.assertIn("var recent = d.recent", html)
        self.assertIn("recentMinutes", html)
        for chart_id in self.SMALL:
            with self.subTest(chart=chart_id):
                start = html.index(f'registerChart("{chart_id}"')
                end = html.index("var upPoints", start) if chart_id == "c-wait" else html.index("registerChart(", start + 1)
                block = html[start:end]
                self.assertIn("smallChart(", block)
                self.assertNotIn("minutes", block.replace("recentMinutes", ""))

    def test_small_charts_share_one_plot_layout_and_three_ticks(self) -> None:
        html = dash.PAGE_HTML
        self.assertIn("var smallLayout = { height: 200, margin: { l: 48, r: 12, t: 26, b: 24 } };", html)
        self.assertIn("xSteps: 2", html)
        self.assertIn('{ v: 1, label: "limit" }', html)
        self.assertIn("var smallPctAxis = { min: 0, max: 2, clip: 2, ticks: [0, 1, 2], fmt: pctFmt };", html)

    def test_small_cards_have_no_legend_and_the_rejection_subtitle_is_short(self) -> None:
        html = dash.PAGE_HTML
        grid = html[html.index('<section class="grid3">'):html.index('aria-labelledby="h-upstream"')]
        self.assertNotIn('class="legend"', grid)
        self.assertIn("Morph rejections on this limit, last 60 min: ", html)


class CapacityPairTest(unittest.TestCase):
    def test_paired_charts_share_one_layout_object_for_height_and_margins(self) -> None:
        html = dash.PAGE_HTML
        self.assertIn("var PAIR_LAYOUT = { height: 260, margin: { l: 64, r: 16, t: 26, b: 24 } };", html)
        for name, end in (("c-billed", "c-workers"), ("c-workers", "var mix =")):
            start = html.index(f'registerChart("{name}"')
            block = html[start:html.index(end, start)]
            self.assertIn("height: PAIR_LAYOUT.height", block, name)
            self.assertIn("margin: PAIR_LAYOUT.margin", block, name)

    def test_response_time_chart_uses_the_pair_gutters_so_its_x_scale_matches(self) -> None:
        html = dash.PAGE_HTML
        start = html.index('registerChart("c-upstream"')
        self.assertIn("margin: PAIR_LAYOUT.margin", html[start:html.index("setText(\"up-note\"", start)])

    def test_shared_note_sits_under_both_columns_and_the_old_claim_is_gone(self) -> None:
        html = dash.PAGE_HTML
        self.assertIn('class="note-line pair-note"', html)
        self.assertNotIn("lines are comparable", html)
        self.assertLess(html.index('id="c-workers"'), html.index('class="note-line pair-note"'))

    def test_deepseek_uncached_chart_sits_next_to_kimi_with_a_rejection_subtitle(self) -> None:
        html = dash.PAGE_HTML
        self.assertIn("DeepSeek uncached (est.)", html)
        self.assertLess(html.index('id="c-kimi"'), html.index('id="c-dsu"'))
        self.assertIn('id="dsu-sub"', html)
        self.assertIn("rejections not recorded", html)
        self.assertIn("Morph rejections on this limit, last 60 min: ", html)

    def test_response_time_chart_sits_below_the_small_charts_and_above_runs(self) -> None:
        html = dash.PAGE_HTML
        self.assertLess(html.index('id="c-wait"'), html.index('id="c-upstream"'))
        self.assertLess(html.index('id="c-upstream"'), html.index('aria-labelledby="h-runs"'))

    def test_activity_tiles_carry_per_minute_subtext(self) -> None:
        html = dash.PAGE_HTML
        self.assertIn('id="a5-min"', html)
        self.assertIn('id="a15-min"', html)
        self.assertIn("last full minute: ", html)
        self.assertIn('"avg/min: "', html)


class RejectionAndPerMinuteValuesTest(unittest.TestCase):
    def test_rejections_count_uncached_ds_calib_lines_inside_the_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "logs").mkdir()
            this = dash.minute_stamp(UNTIL - 60)
            old = dash.minute_stamp(UNTIL - 3600)
            (home / "logs" / "proxy.err.log").write_text(
                f"{this}:30Z agentq-proxy calib minute={this} budget=uncached_ds sec=30.0\n"
                f"{this}:40Z agentq-proxy calib minute={this} budget=uncached_ds sec=40.0\n"
                f"{old}:30Z agentq-proxy calib minute={old} budget=uncached_ds sec=30.0\n"
                f"{this}:31Z agentq-proxy calib minute={this} budget=total sec=31.0\n",
                encoding="utf-8",
            )
            read = dash.read_calib_429s(home)
        rejected = dash.budget_rejections(read, "uncached_ds", UNTIL - 1800, UNTIL)
        self.assertEqual(rejected, {"count": 2, "partial": False})

    def test_no_stated_budget_lines_means_rejections_are_not_recorded(self) -> None:
        self.assertEqual(dash.budget_rejections({"events": [], "truncated": False}, "uncached_ds", 0, UNTIL),
                         {"count": None, "partial": False})
        self.assertEqual(dash.budget_rejections(None, "uncached_ds", 0, UNTIL), {"count": None, "partial": False})

    def test_truncated_log_tail_marks_the_count_partial(self) -> None:
        read = {"events": [("uncached_ds", UNTIL - 60)], "truncated": True}
        self.assertEqual(dash.budget_rejections(read, "uncached_ds", UNTIL - 3600, UNTIL),
                         {"count": 1, "partial": True})

    def test_pool_per_minute_prefers_admitted_minutes_and_averages_over_the_window(self) -> None:
        raw = five_minutes(total=1_000_000)
        admitted = {UNTIL - 60: 4_000_000}
        per_minute = dash.pool_per_minute(raw, admitted, UNTIL)
        self.assertEqual(per_minute["last_full_minute"], 4_000_000)
        self.assertEqual(per_minute["last_full_minute_frac"], 0.4)
        self.assertEqual(per_minute["avg_5m"], 1_600_000)
        self.assertEqual(per_minute["avg_15m"], round((4_000_000 + 4 * 1_000_000) / 15))
