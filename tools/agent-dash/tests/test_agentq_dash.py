import calendar
from collections import Counter
import io
import re
import hashlib
import shutil
import subprocess
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agentq"))

from proxy import PROXY_MINUTES_DDL

import dash

NOW = float(calendar.timegm((2026, 11, 12, 12, 0, 30)))
UNTIL = int(NOW // 60) * 60
JOB_DDL = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY, state TEXT NOT NULL, lane TEXT NOT NULL, tier TEXT NOT NULL,
    kind TEXT NOT NULL, verdict TEXT, submitted_at REAL NOT NULL, started_at REAL, finished_at REAL,
    label TEXT NOT NULL DEFAULT '', detail TEXT, res TEXT NOT NULL DEFAULT '', pid INTEGER
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

    def test_queued_jobs_are_split_by_tier_in_the_sample(self) -> None:
        conn = sqlite3.connect(self.home / "agentq.db")
        conn.execute(
            "INSERT INTO jobs (id, state, lane, tier, kind, verdict, submitted_at, started_at, finished_at, label) "
            "VALUES (?, 'queued', 'normal', 'pi-kimi', 'review', NULL, ?, NULL, NULL, 'kimi-wait')",
            ("k" * 12, NOW - 5),
        )
        conn.commit()
        conn.close()
        row = dash.sample_once(self.home, ps_runner=lambda: PS_FIXTURE, now=NOW)
        self.assertEqual((row["queued_jobs"], row["ds_queued"], row["kimi_queued"]), (2, 1, 1))
        conn = sqlite3.connect(self.home / "dash.db")
        stored = conn.execute("SELECT queued_jobs, ds_queued, kimi_queued FROM samples").fetchall()
        conn.close()
        self.assertEqual(stored, [(2, 1, 1)])

    def test_old_samples_without_the_split_show_no_queued_value_and_the_table_is_migrated(self) -> None:
        dash_db = self.home / "dash.db"
        conn = sqlite3.connect(dash_db)
        conn.execute("CREATE TABLE samples (ts REAL PRIMARY KEY, ds_workers INT, kimi_workers INT, "
                     "queue_workers INT, direct_workers INT, queued_jobs INT)")
        conn.execute("INSERT INTO samples VALUES (?, 1, 0, 0, 0, 3)", (NOW - 600,))
        conn.commit()
        conn.close()
        old = dash.read_samples(dash_db, NOW - 3600)
        self.assertIsNone(old[0]["ds_queued"])
        self.assertEqual(old[0]["queued_jobs"], 3)
        dash.sample_once(self.home, ps_runner=lambda: PS_FIXTURE, now=NOW)
        conn = sqlite3.connect(dash_db)
        columns = [r[1] for r in conn.execute("PRAGMA table_info(samples)")]
        conn.close()
        self.assertIn("ds_queued", columns)
        new = dash.read_samples(dash_db, NOW - 3600)
        self.assertEqual([s["queued_jobs"] for s in new], [3, 1])
        self.assertEqual(new[1]["ds_queued"], 1)

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
            "Top consumers, ",
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
        self.assertEqual(sorted(data["source_age_s"]), ["laptop", "live"])
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

    def test_poll_interval_follows_the_range_in_the_served_html(self) -> None:
        self.assertIn("var POLL_MS_BY_HOURS = { 1: 5000, 6: 5000, 24: 15000, 168: 60000 };", dash.PAGE_HTML)
        self.assertNotIn("POLL_MS = 30000", dash.PAGE_HTML)


RUN_LAST_TS = NOW - 30


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
                ("a" * 12, "morph-dsv41flash-1017", NOW - 600, RUN_LAST_TS, 4, 4, 1, 1000, 3000, 500, 800, 4000),
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
        self.assertEqual(run["duration_s"], RUN_LAST_TS - (NOW - 600))

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

    def test_runs_active_in_window_are_distinct_jobs_with_activity_in_range(self) -> None:
        kpis = self.summary["run_kpis"]
        self.assertEqual(kpis["active_in_window"], 4)
        self.assertEqual(kpis["active_lifetime_tokens"], 4500 + 5200 + 150 + 400)
        self.assertEqual(kpis["median_tokens_per_run"], (400 + 4500) / 2)
        self.assertEqual(kpis["median_requests_per_run"], 3.0)

    def test_finished_counts_queue_verdicts_in_the_selected_range(self) -> None:
        self.assertEqual(self.summary["range_totals"]["finished"], {"PASS": 1, "PARTIAL": 0, "FAIL": 1})

    def test_top_consumers_rank_range_runs_with_share_of_range(self) -> None:
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
            self.assertEqual(summary["run_kpis"]["active_in_window"], 0)
            self.assertEqual(summary["range_totals"]["finished"], {"PASS": 1, "PARTIAL": 0, "FAIL": 1})
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

    def test_the_deepseek_limit_pairs_with_the_deepseek_in_flight_count_not_the_total(self) -> None:
        live = {"ok": True, "budgets": {}, "inflight_limit": 2, "inflight_in_flight": 3,
                "upstream": {"in_flight": {"morph-dsv41flash-1017": 3, "morph-kimik31": 7}}}
        perf = self.summary(live)["perf_kpis"]
        self.assertEqual(perf["in_flight_now"], 10)
        self.assertEqual(perf["ds_in_flight_now"], 3)
        self.assertEqual(perf["inflight_limit"], 2)

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


class RangeAverageLineTest(unittest.TestCase):
    """The dotted average is the mean of the chart's own minute points over the selected range, idle minutes as zero."""

    AGES = (30 * 60, 2 * 3600, 10 * 3600, 3 * 24 * 3600)
    TOTALS = (2_000_000, 4_000_000, 6_000_000, 8_000_000)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name) / "agentq"
        home.mkdir()
        conn = sqlite3.connect(home / "agentq.db")
        conn.execute(PROXY_MINUTES_DDL)
        conn.executemany(
            f"INSERT INTO proxy_minutes ({MINUTE_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(minute_at(age), "morph-dsv41flash-1017", 1, 1, 0, total, 0, 0, 0, 0, 0, 0.9)
             for age, total in zip(self.AGES, self.TOTALS)],
        )
        conn.commit()
        conn.close()
        self.home = home

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def expected(self, hours: int) -> float:
        """Independent of the code under test: the sum of the in-range totals over the minutes from the first recorded one."""
        in_range = [(age, total) for age, total in zip(self.AGES, self.TOTALS) if age <= hours * 3600]
        minutes = max(age for age, _ in in_range) // 60
        return round(sum(total for _, total in in_range) / 10_000_000 / minutes, 4)

    def test_average_line_is_the_range_mean_of_the_minute_points_for_each_range(self) -> None:
        for hours in (1, 6, 24, 168):
            with self.subTest(hours=hours):
                summary = dash.build_summary(self.home, hours, now=NOW, fetch_live=no_proxy)
                self.assertEqual(summary["range_avg_pct_total"], self.expected(hours))

    def test_each_range_gives_a_different_average(self) -> None:
        values = [dash.build_summary(self.home, hours, now=NOW, fetch_live=no_proxy)["range_avg_pct_total"] for hours in (1, 6, 24, 168)]
        self.assertEqual(len(set(values)), 4, values)

    def test_total_pool_tile_uses_the_same_average_as_the_chart(self) -> None:
        for hours in (1, 6):
            summary = dash.build_summary(self.home, hours, now=NOW, fetch_live=no_proxy)
            self.assertEqual(summary["limits"]["total"]["range_avg_frac"], summary["range_avg_pct_total"])

    def test_idle_minutes_inside_the_range_count_as_zero(self) -> None:
        points = [{"t": 0, "pct_total": 0.6}, {"t": 180, "pct_total": 0.3}]
        self.assertEqual(dash.range_average_pct(points, 240), round((0.6 + 0 + 0 + 0.3) / 4, 4))

    def test_no_minute_points_means_no_average_line(self) -> None:
        self.assertIsNone(dash.range_average_pct([], 600))


class HourlyAverageTest(unittest.TestCase):
    """Hourly averages of the chart's minute points: Eastern hours, idle minutes as zero, the hour in progress."""

    def points(self, start: int, minutes: int, value) -> list[dict]:
        return [{"t": start + 60 * i, "pct_total": value(i)} for i in range(minutes)]

    def test_hours_start_on_the_eastern_clock_hour_across_both_dst_changes(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        start = calendar.timegm((2026, 3, 7, 12, 0, 0))
        until = start + 96 * 3600
        rows = dash.hourly_averages(self.points(start, 96 * 60, lambda i: 0.5), until)
        self.assertEqual(len(rows), 96)
        eastern = ZoneInfo("America/New_York")
        for row in rows:
            moment = datetime.fromtimestamp(row["t"], eastern)
            self.assertEqual((moment.minute, moment.second), (0, 0), row)
            self.assertEqual(row["label"], moment.strftime("%H") + " ET")
        spring_day = [row for row in rows if datetime.fromtimestamp(row["t"], eastern).date().isoformat() == "2026-03-08"]
        labels = [row["label"] for row in spring_day]
        self.assertEqual(len(spring_day), 23, "the spring-forward day is 23 Eastern hours long")
        self.assertNotIn("02 ET", labels)
        self.assertEqual(labels.count("03 ET"), 1)

    def test_fall_back_repeats_the_01_hour_as_two_distinct_rows(self) -> None:
        start = calendar.timegm((2026, 11, 1, 4, 0, 0))
        rows = dash.hourly_averages(self.points(start, 180, lambda i: 0.2), start + 3 * 3600)
        ones = [row for row in rows if row["label"] == "01 ET"]
        self.assertEqual(len(ones), 2)
        self.assertNotEqual(ones[0]["t"], ones[1]["t"])

    def test_the_minute_before_midnight_eastern_belongs_to_the_previous_hour(self) -> None:
        midnight = calendar.timegm((2026, 11, 13, 5, 0, 0))
        points = [{"t": midnight - 60, "pct_total": 0.9}, {"t": midnight, "pct_total": 0.1}]
        rows = dash.hourly_averages(points, midnight + 3600)
        self.assertEqual([row["label"] for row in rows], ["23 ET", "00 ET"])
        self.assertEqual((rows[0]["minutes"], rows[0]["avg"]), (1, 0.9))
        self.assertEqual((rows[1]["minutes"], rows[1]["avg"]), (60, round(0.1 / 60, 4)))

    def test_idle_minutes_inside_a_complete_hour_count_as_zero(self) -> None:
        hour = calendar.timegm((2026, 11, 13, 15, 0, 0))
        rows = dash.hourly_averages(self.points(hour, 30, lambda i: 0.6), hour + 3660)
        self.assertEqual(rows[0]["minutes"], 60)
        self.assertEqual(rows[0]["avg"], 0.3)
        self.assertFalse(rows[0]["partial"])

    def test_the_hour_in_progress_averages_the_minutes_so_far_and_is_partial(self) -> None:
        hour = calendar.timegm((2026, 11, 13, 15, 0, 0))
        rows = dash.hourly_averages(self.points(hour, 10, lambda i: 0.8), hour + 600)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["partial"])
        self.assertEqual((rows[0]["minutes"], rows[0]["avg"]), (10, 0.8))

    def test_the_first_hour_counts_from_the_first_recorded_minute(self) -> None:
        hour = calendar.timegm((2026, 11, 13, 15, 0, 0))
        rows = dash.hourly_averages(self.points(hour + 1800, 30, lambda i: 0.5), hour + 3600)
        self.assertEqual((rows[0]["minutes"], rows[0]["avg"]), (30, 0.5))

    def test_an_empty_range_has_no_hours(self) -> None:
        self.assertEqual(dash.hourly_averages([], 1000), [])

    def test_a_one_hour_range_has_at_most_two_hours_and_the_summary_carries_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(Path(tmp))
            summary = dash.build_summary(home, 1, now=NOW, fetch_live=no_proxy)
        rows = summary["hourly_avg_pct_total"]
        self.assertGreaterEqual(len(rows), 1)
        self.assertLessEqual(len(rows), 2)
        self.assertEqual(rows[-1]["partial"], rows[-1]["t"] + 3600 > NOW // 60 * 60)
        self.assertEqual(set(rows[0]), {"t", "label", "minutes", "avg", "partial"})


class HourlyStepPageTest(unittest.TestCase):
    def test_the_page_builds_vertical_steps_and_keeps_each_minute_with_its_hour(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        html = dash.PAGE_HTML
        snippet = html[html.index("  function completeHourFor("):html.index("  function endSeries(")]
        script = snippet + """
var rows = [
  {t: 0, label: "00 ET", avg: 0.2, minutes: 60, partial: false},
  {t: 3600, label: "01 ET", avg: 0.6, minutes: 10, partial: true}
];
var points = [{t: 3540, pct_total: 0.1}, {t: 3600, pct_total: 0.7}, {t: 3660, pct_total: 0.7}];
console.log(JSON.stringify(withHourly(points, rows)));
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True)
        out = json.loads(result.stdout)
        self.assertEqual([p["t"] for p in out], [3540, 3600, 3600, 3660])
        self.assertEqual([p["hourly"] for p in out], [0.2, 0.2, None, None])
        self.assertNotIn("hourlyPartial", out[0])
        self.assertEqual([p["pct_total"] for p in out], [0.1, 0.7, 0.7, 0.7])


class EnforcedLimitsChartTest(unittest.TestCase):
    def test_main_billed_chart_has_the_per_minute_line_and_an_hourly_step_average(self) -> None:
        html = dash.PAGE_HTML
        start = html.index('registerChart("c-billed"')
        block = html[start:html.index('registerChart("c-workers"')]
        self.assertEqual(block.count('key: "pct_'), 1)
        self.assertNotIn("pct_total_avg5", html)
        self.assertNotIn("5-min average", html)
        self.assertIn('{ key: "pct_total", name: "Per minute", color: COLORS.s1, width: 1.25, opacity: 0.4 }', block)
        self.assertIn('{ key: "hourly", name: "Hourly average", color: COLORS.s3, width: 2.5 }', block)
        self.assertNotIn("hourlyPartial", html)
        self.assertNotIn("This hour so far", html)
        self.assertIn('"Hourly average"', html)
        self.assertNotIn("notes:", block)
        self.assertIn('"Hourly average" + (lastComplete ? " · last full hour " + lastComplete.label + ": " + Math.round(lastComplete.avg * 100) + "%" : "")', html)
        self.assertNotIn("range_avg_pct_total", block)
        self.assertIn('Morph limit (10M/min)', block)
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
CALM = {"load1": 2.0, "cpus": 15, "load_per_cpu": 2.0 / 15, "memory_free_pct": 60.0, "cpu_busy": 0.30, "swap_frac": 0.10}
HOT = {"load1": 44.0, "cpus": 15, "load_per_cpu": 44.0 / 15, "memory_free_pct": 60.0, "cpu_busy": 0.95, "swap_frac": 0.10}
def live_queued(total=0, output=0, uncached=0, uncached_ds=0) -> dict:
    """A proxy status whose budgets hold the given number of waiting requests each, all in the normal lane."""
    waits = {"total": total, "output": output, "uncached": uncached, "uncached_ds": uncached_ds}
    return {"budgets": {name: {"queued": {"urgent": 0, "normal": n, "backlog": 0}} for name, n in waits.items()}}


IDLE_LIVE = live_queued()


def five_minutes(**fields) -> dict:
    """Minute rows for the five complete minutes before UNTIL, each with the same fields."""
    return {UNTIL - 60 * k: fields for k in range(1, 6)}


def verdict_for(raw, *, live=IDLE_LIVE, laptop=CALM, kimi_jobs_waiting=0, budget_429s=None,
                ds_jobs_ready=0, slow=None):
    return dash.verdict(
        raw, UNTIL, LIMITS_NOW, live, kimi_jobs_waiting=kimi_jobs_waiting, budget_429s=budget_429s or {},
        laptop=laptop, ds_jobs_ready=ds_jobs_ready, slow=slow,
    )


def slow_ttfb(limit=28):
    """A ttfb_slow result: five-minute median 11.2 s against a 5.0 s baseline."""
    return {"ttfb_ms": 11200, "baseline_ms": 5000, "slow_minutes": 4, "minutes": 5, "limit": limit}



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

    def test_morph_slow_when_first_byte_is_slow_and_the_proxy_holds_nothing_for_a_budget(self) -> None:
        result = verdict_for(five_minutes(total=1_000_000), slow=slow_ttfb())
        self.assertEqual(result["key"], "morph_slow")
        self.assertEqual(result["chip"], "Morph slow")
        self.assertEqual(
            result["text"],
            "Morph is slow: first byte 11.2 s vs 5.0 s usual; requests above 28 in flight wait at the proxy.",
        )

    def test_morph_slow_names_the_would_be_limit_in_shadow(self) -> None:
        result = verdict_for(five_minutes(total=1_000_000), live=dict(IDLE_LIVE, inflight_mode="shadow"), slow=slow_ttfb())
        self.assertIn("(advisory in-flight limit 28, not enforced)", result["text"])

    def test_morph_slow_needs_an_empty_budget_queue(self) -> None:
        result = verdict_for(five_minutes(total=1_000_000), live=live_queued(output=3), slow=slow_ttfb())
        self.assertNotEqual(result["key"], "morph_slow")

    def test_a_waiting_kimi_request_does_not_block_morph_slow(self) -> None:
        live = live_queued(uncached=1)
        result = verdict_for(five_minutes(total=1_000_000), live=live, kimi_jobs_waiting=1, slow=slow_ttfb())
        self.assertEqual(result["key"], "morph_slow")

    def test_ttfb_slow_needs_three_of_five_minutes_at_1_8_times_the_baseline(self) -> None:
        raw = {UNTIL - 60 * k: {"ds_ttfb_p50_ms": 10_000 if k <= 3 else 4_000} for k in range(1, 6)}
        self.assertEqual(dash.ttfb_slow(raw, UNTIL, 5_000, 28)["slow_minutes"], 3)
        raw[UNTIL - 60 * 3] = {"ds_ttfb_p50_ms": 4_000}
        self.assertIsNone(dash.ttfb_slow(raw, UNTIL, 5_000, 28))

    def test_a_429_minute_stops_morph_slow(self) -> None:
        result = verdict_for(five_minutes(total=1_000_000), slow=slow_ttfb(), budget_429s={"total": {UNTIL - 60}})
        self.assertNotEqual(result["key"], "morph_slow")

    def test_laptop_pressure_without_a_broker_hold_is_context_not_the_headline(self) -> None:
        busy = dict(CALM, cpu_busy=0.95)
        result = verdict_for(five_minutes(total=1_000_000), laptop=busy)
        self.assertEqual(result["key"], "headroom")
        self.assertEqual(
            result["secondary"],
            "Laptop pressure, not holding DeepSeek jobs: CPU 95% busy against the 92% light bar.",
        )

    def test_the_laptop_verdict_needs_a_broker_hold_on_a_ready_deepseek_job(self) -> None:
        result = dash.verdict(
            five_minutes(total=1_000_000), UNTIL, LIMITS_NOW, IDLE_LIVE, kimi_jobs_waiting=0, budget_429s={},
            laptop=CALM, ds_jobs_ready=1, slow=None,
            ds_holds=["hold light: cpu 95% busy >= 92%"],
        )
        self.assertEqual(result["key"], "held_laptop")
        self.assertEqual(result["chip"], "Laptop")
        self.assertEqual(
            result["text"],
            "1 light job waiting for laptop headroom (CPU 95% busy, limit 92%).",
        )

    def test_a_ready_job_held_on_a_proxy_budget_is_not_a_laptop_verdict_even_under_pressure(self) -> None:
        result = dash.verdict(
            five_minutes(total=1_000_000), UNTIL, LIMITS_NOW, IDLE_LIVE, kimi_jobs_waiting=0, budget_429s={},
            laptop=dict(CALM, cpu_busy=0.99), ds_jobs_ready=1, slow=None,
            ds_holds=["hold pi-deepseek-flash: proxy total waiting 9 > 8"],
        )
        self.assertNotEqual(result["key"], "held_laptop")
        self.assertIn("CPU 99% busy", result["secondary"])

    def test_memory_and_swap_pressure_are_context_against_the_light_bars(self) -> None:
        low_memory = verdict_for(five_minutes(total=1_000_000), laptop=dict(CALM, memory_free_pct=15.0))
        self.assertIn("15% memory free against the 20% light floor", low_memory["secondary"])
        swapping = verdict_for(five_minutes(total=1_000_000), laptop=dict(CALM, swap_frac=0.90))
        self.assertIn("swap 90% used against the 90% bar", swapping["secondary"])
        self.assertNotEqual(swapping["key"], "laptop")

    def test_laptop_bars_default_to_the_brokers_and_read_broker_json(self) -> None:
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        from agentq import broker
        for name, default in dash.LAPTOP_BAR_DEFAULTS.items():
            self.assertEqual(broker.DEFAULT_LAPTOP[name], default, name)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "broker.json").write_text(json.dumps({"laptop": {"tests_max_cpu_busy": 0.7}}), encoding="utf-8")
            bars = dash.laptop_bars(home)
            self.assertEqual(bars["tests_max_cpu_busy"], 0.7)
            self.assertEqual(bars["max_swap_frac"], 0.90)

    def test_laptop_reading_takes_fresh_gauge_state_and_drops_stale_measures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "slots").mkdir()
            state = {"free": 44.0, "free_until": NOW + 10, "cpu_busy": 0.66, "cpu_until": NOW + 25,
                     "swap": 0.2, "swap_until": NOW - 1}
            (home / "slots" / "gauge.json").write_text(json.dumps(state), encoding="utf-8")
            reading = dash.laptop_reading(home, NOW, memory=lambda now: 99.0)
            self.assertEqual(reading["memory_free_pct"], 44.0)
            self.assertEqual(reading["cpu_busy"], 0.66)
            self.assertIsNone(reading["swap_frac"])
            state["free_until"] = NOW - 1
            (home / "slots" / "gauge.json").write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(dash.laptop_reading(home, NOW, memory=lambda now: 99.0)["memory_free_pct"], 99.0)

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
        self.assertEqual(result["chip"], "Proxy waiting")
        self.assertEqual(result["text"], "DeepSeek requests are waiting at the proxy on Total pool while the pool is at 41%.")

    def test_proxy_pacing_names_deepseek_uncached_input_when_that_budget_waits(self) -> None:
        result = verdict_for(five_minutes(total=4_100_000), live=live_queued(uncached_ds=3))
        self.assertEqual(result["key"], "proxy_pacing")
        self.assertIn("waiting at the proxy on DeepSeek uncached input", result["text"])

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
            laptop=CALM, ds_jobs_ready=2, slow=None, ds_holds=["", None],
        )
        self.assertEqual(result["key"], "launching")
        self.assertEqual(result["chip"], "Launching")
        self.assertEqual(result["text"], "Launching: 2 DeepSeek jobs are ready; the broker is starting them.")

    def test_held_jobs_on_the_laptop_gate_and_ssh_slots_name_the_laptop_chip(self) -> None:
        details = ["hold tests: cpu 90% busy >= 80%"] * 4 + ["hold ssh: slots full"] * 2
        result = dash.verdict(
            five_minutes(total=4_100_000), UNTIL, LIMITS_NOW, IDLE_LIVE, kimi_jobs_waiting=0, budget_429s={},
            laptop=CALM, ds_jobs_ready=6, slow=None,
            ds_holds=details, ssh_in_use=8,
        )
        self.assertEqual(result["key"], "held_laptop")
        self.assertEqual(result["chip"], "Laptop")
        self.assertEqual(
            result["text"],
            "4 test jobs waiting for laptop headroom (CPU 90% busy, limit 80%).",
        )
        self.assertEqual(result["secondary"], "Also: 2 jobs waiting for a production ssh slot (8 in use).")

    def test_all_jobs_held_on_ssh_slots_name_the_ssh_chip(self) -> None:
        result = dash.verdict(
            five_minutes(total=4_100_000), UNTIL, LIMITS_NOW, IDLE_LIVE, kimi_jobs_waiting=0, budget_429s={},
            laptop=CALM, ds_jobs_ready=2, slow=None,
            ds_holds=["hold ssh: slots full", "hold ssh: slots full"], ssh_in_use=8,
        )
        self.assertEqual(result["key"], "held_ssh")
        self.assertEqual(result["chip"], "SSH slots")
        self.assertEqual(result["text"], "2 jobs waiting for a production ssh slot (8 in use).")

    def test_held_jobs_waiting_on_a_workspace_or_the_proxy_name_their_chip(self) -> None:
        workspace = dash.verdict(
            five_minutes(total=4_100_000), UNTIL, LIMITS_NOW, IDLE_LIVE, kimi_jobs_waiting=0, budget_429s={},
            laptop=CALM, ds_jobs_ready=1, slow=None,
            ds_holds=["hold pi-deepseek-flash: workspace busy"],
        )
        self.assertEqual(workspace["chip"], "Workspace")
        self.assertEqual(workspace["text"], "1 write job waiting for its workspace.")
        held_proxy = dash.verdict(
            five_minutes(total=4_100_000), UNTIL, LIMITS_NOW, IDLE_LIVE, kimi_jobs_waiting=0, budget_429s={},
            laptop=CALM, ds_jobs_ready=1, slow=None,
            ds_holds=["hold pi-deepseek-flash: proxy total waiting 3 > 0"],
        )
        self.assertEqual(held_proxy["key"], "held_proxy")
        self.assertEqual(held_proxy["chip"], "Proxy waiting")
        self.assertEqual(
            held_proxy["text"],
            "1 DeepSeek job waiting to launch behind 3 requests waiting at the proxy on the Morph total pool.",
        )

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

    def test_laptop_room_is_the_light_cpu_bar_less_busy_scaled_per_worker(self) -> None:
        raw = five_minutes(total=1_000_000, ds_uncached=100_000)
        laptop = dict(CALM, cpu_busy=0.50)
        result = self.room_for(raw, workers=10, laptop=laptop)
        self.assertEqual(result["candidates"]["laptop"], round((0.92 - 0.50) * 10 / 0.50, 2))

    def test_laptop_room_ignores_load_and_is_absent_without_a_cpu_reading(self) -> None:
        raw = five_minutes(total=1_000_000, ds_uncached=100_000)
        high_load = dict(CALM, load1=60.0, cpu_busy=0.30)
        self.assertNotIn("laptop", self.room_for(raw, workers=10, laptop=dict(high_load, cpu_busy=None))["candidates"])
        self.assertEqual(self.room_for(raw, workers=10, laptop=high_load)["candidates"]["laptop"], round((0.92 - 0.30) * 10 / 0.30, 2))

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
        self.assertIn(summary["verdict"]["key"], {"morph_limit", "morph_slow", "laptop", "near_capacity", "proxy_pacing", "launching", "headroom", "held_pacing", "held_laptop", "held_ssh", "held_slots", "held_proxy", "held_workspace", "held_tier", "held_other"})
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
            found = dash.budget_429_minutes(dash.read_calib_429s(home), UNTIL)
        self.assertEqual(found, {"total": {UNTIL - 60}})

    def test_a_calib_429_written_just_before_a_rotation_is_still_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            logs = home / "logs"
            logs.mkdir()
            live = logs / "proxy.err.log"
            this = dash.minute_stamp(UNTIL - 60)
            live.write_text(
                f"{this}:30Z agentq-proxy calib minute={this} budget=uncached_ds sec=30.0\n",
                encoding="utf-8",
            )
            # RotatingLog rotates by renaming the live file to .1 and starting an empty one.
            os.replace(live, live.with_name(live.name + ".1"))
            live.write_text("", encoding="utf-8")
            read = dash.read_calib_429s(home)
            found = dash.budget_429_minutes(dash.read_calib_429s(home), UNTIL)
        self.assertEqual(read["counts"], Counter({("uncached_ds", UNTIL - 60): 1}))
        self.assertEqual(found, {"uncached_ds": {UNTIL - 60}})

    def test_the_live_file_is_read_after_the_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            logs = home / "logs"
            logs.mkdir()
            live = logs / "proxy.err.log"
            old = dash.minute_stamp(UNTIL - 180)
            this = dash.minute_stamp(UNTIL - 60)
            live.with_name(live.name + ".1").write_text(
                f"{old}:30Z agentq-proxy calib minute={old} budget=total sec=30.0\n",
                encoding="utf-8",
            )
            live.write_text(
                f"{this}:30Z agentq-proxy request job=a1b2c3d4e5f0 status=200\n"
                f"{this}:31Z agentq-proxy calib minute={this} budget=uncached_ds sec=31.0\n",
                encoding="utf-8",
            )
            read = dash.read_calib_429s(home)
        self.assertEqual(read["counts"], Counter({("total", UNTIL - 180): 1, ("uncached_ds", UNTIL - 60): 1}))
        self.assertTrue(read["truncated"], "the older backups were not read, so the window is partial")


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
        self.assertNotIn("offScale", block)
        self.assertIn('note.textContent = c.label + " \\u2191 (off scale)";', html)
        self.assertIn("height: PAIR_LAYOUT.height", block)
        self.assertIn("margin: PAIR_LAYOUT.margin", block)

    def test_off_scale_notices_are_in_the_legend_row_not_the_plot(self) -> None:
        html = dash.PAGE_HTML
        self.assertNotIn("(cfg.offScale || [])", html)
        self.assertNotIn("y: m.t + 13", html)
        self.assertIn('$("lg-workers").appendChild(note);', html)

    def test_every_remaining_in_plot_label_has_a_surface_halo(self) -> None:
        html = dash.PAGE_HTML
        start = html.index("var refPlaced")
        block = html[start:html.index("if (cfg.kind === \"bar\")", start)]
        self.assertIn("paint-order: stroke", block)
        self.assertIn("stroke: var(--surface)", block)
        token = html[html.index("function drawTokenCombo"):]
        self.assertIn("paint-order: stroke", token[:token.index("\n  }\n")] if "\n  }\n" in token else token[:4000])


class SmallChartRowTest(unittest.TestCase):
    SMALL = ("c-kimi", "c-dsu", "c-429", "c-wait")

    def test_small_charts_read_the_selected_range_not_a_fixed_hour(self) -> None:
        html = dash.PAGE_HTML
        self.assertNotIn("var recent = d.recent", html)
        self.assertNotIn("recentMinutes", html)
        for chart_id in self.SMALL:
            with self.subTest(chart=chart_id):
                start = html.index(f'registerChart("{chart_id}"')
                end = html.index("var upPoints", start) if chart_id == "c-wait" else html.index("registerChart(", start + 1)
                block = html[start:end]
                self.assertIn("smallChart(", block)
                self.assertIn("var smallChart = function", html)
                self.assertIn("points: minutes", html)
                self.assertIn("bucket_s: bucket", html)
                self.assertIn("x0: win.start", html)

    def test_small_charts_share_one_plot_layout_and_three_ticks(self) -> None:
        html = dash.PAGE_HTML
        self.assertIn("var smallLayout = { height: 200, margin: { l: 48, r: 12, t: 26, b: 24 } };", html)
        self.assertIn("xSteps: 2", html)
        self.assertIn("{ v: 1 }", html)
        self.assertNotIn('label: "limit"', html)
        self.assertIn('id="kimi-limit"', html)
        self.assertIn('id="dsu-limit"', html)
        self.assertIn("var smallPctAxis = { min: 0, max: 2, clip: 2, ticks: [0, 1, 2], fmt: pctFmt };", html)

    def test_small_cards_have_no_legend_and_the_rejection_subtitle_is_short(self) -> None:
        html = dash.PAGE_HTML
        grid = html[html.index('<section class="grid3">'):html.index('aria-labelledby="h-upstream"')]
        self.assertNotIn('class="legend"', grid)
        self.assertIn("Morph rejections on this limit, ", html)


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
        self.assertIn("Morph rejections on this limit, ", html)

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
        self.assertEqual(dash.budget_rejections({"counts": Counter(), "truncated": False}, "uncached_ds", 0, UNTIL),
                         {"count": None, "partial": False})
        self.assertEqual(dash.budget_rejections(None, "uncached_ds", 0, UNTIL), {"count": None, "partial": False})

    def test_truncated_log_tail_marks_the_count_partial(self) -> None:
        read = {"counts": Counter({("uncached_ds", UNTIL - 60): 1}), "truncated": True}
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


class FillerHeadlineTest(unittest.TestCase):
    FILL = {"enabled": True, "target_billed_per_min": 8_000_000, "low_frac": 0.9}

    def headline(self, ds_total, filler_running, fill=FILL, ds_jobs_ready=0, ds_waiting=None, kimi_total=0):
        live = IDLE_LIVE if ds_waiting is None else live_queued(ds_waiting)
        return dash.verdict(
            five_minutes(total=ds_total + kimi_total, ds_total=ds_total), UNTIL, LIMITS_NOW, live,
            kimi_jobs_waiting=0, budget_429s={}, laptop=CALM, ds_jobs_ready=ds_jobs_ready,
            slow=None, filler_running=filler_running, fill=fill,
        )

    def test_near_target_with_filler_running_says_holding_near_target(self) -> None:
        result = self.headline(7_800_000, 6)
        self.assertEqual(result["key"], "filling")
        self.assertEqual(result["chip"], "Near target")
        self.assertEqual(result["text"], "Holding near target: 6 filler runs filling the gap.")

    def test_without_filler_running_the_headline_is_unchanged(self) -> None:
        self.assertEqual(self.headline(7_800_000, 0)["key"], "headroom")

    def test_below_the_low_mark_is_not_held_by_fill(self) -> None:
        self.assertEqual(self.headline(5_000_000, 3)["key"], "headroom")

    def test_kimi_traffic_does_not_make_deepseek_look_near_target(self) -> None:
        self.assertEqual(self.headline(5_000_000, 3, kimi_total=2_500_000)["key"], "headroom")

    def test_fill_disabled_never_claims_the_headline(self) -> None:
        self.assertEqual(self.headline(7_800_000, 3, fill=dict(self.FILL, enabled=False))["key"], "headroom")

    def test_real_deepseek_work_waiting_keeps_its_own_headline(self) -> None:
        self.assertEqual(self.headline(7_800_000, 3, ds_jobs_ready=2)["key"], "launching")

    def test_proxy_waits_keep_the_proxy_pacing_headline(self) -> None:
        self.assertEqual(self.headline(7_800_000, 3, ds_waiting=4)["key"], "proxy_pacing")

    def test_read_fill_defaults_when_broker_json_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fill = dash.read_fill(Path(tmp))
        self.assertEqual(fill, {"enabled": False, "target_billed_per_min": 8_000_000.0, "low_frac": 0.9})
RANGE_AGES = (30 * 60, 2 * 3600, 10 * 3600, 3 * 24 * 3600)


def range_fixture(tmp: Path) -> Path:
    """Proxy minutes and queue verdicts at four ages, so each range step adds new minutes and new verdicts."""
    home = tmp / "agentq"
    home.mkdir()
    conn = sqlite3.connect(home / "agentq.db")
    conn.execute(PROXY_MINUTES_DDL)
    conn.executemany(f"INSERT INTO proxy_minutes ({MINUTE_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        (minute_at(age), "morph-dsv41flash-1017", 1, 1, index + 1, 2_000_000 * (index + 1), 0, 0, 0, 100 * (index + 1), 1000 * (index + 1), 0.9)
        for index, age in enumerate(RANGE_AGES)
    ])
    conn.execute(JOB_DDL)
    jobs = []
    for index, age in enumerate(RANGE_AGES):
        for verdict in VERDICTS:
            jobs.append((f"{index}{verdict}".ljust(12, "x")[:12], "done", "normal", "pi-deepseek-flash", "read", verdict,
                         NOW - age - 60, NOW - age - 50, NOW - age - 10, verdict.lower()))
    conn.executemany(
        "INSERT INTO jobs (id, state, lane, tier, kind, verdict, submitted_at, started_at, finished_at, label) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)", jobs,
    )
    conn.commit()
    conn.close()
    return home


VERDICTS = ("PASS", "PARTIAL", "FAIL")


class QueuedSplitChartTest(unittest.TestCase):
    def test_worker_chart_draws_queued_as_areas_behind_the_solid_run_lines(self) -> None:
        html = dash.PAGE_HTML
        start = html.index('registerChart("c-workers"')
        block = html[start:html.index("var mix = d.token_mix", start)]
        self.assertIn('{ key: "dsq", name: "DeepSeek queued", color: COLORS.s3, stacked: true, fillOpacity: 0.32 }', block)
        self.assertIn('{ key: "kimiq", name: "Kimi queued", color: COLORS.kimiQueue, stacked: true, fillOpacity: 0.3 }', block)
        self.assertIn('stack: ["dsq", "kimiq"]', block)
        self.assertIn('{ key: "queuedTotal", name: "Queued, total", color: COLORS.muted, tooltipOnly: true }', block)
        self.assertIn('kimiQueue: "#8a6cc0"', html)
        self.assertIn("queuedTotal: split ? w.ds_queued + w.kimi_queued : null", html)
        self.assertIn("cfg.series.filter(function (s) { return !s.stacked && !s.tooltipOnly; })", html)
        self.assertIn("var stackKeys = cfg.stack || [];", html)
        self.assertIn('{ key: "ds", name: "DeepSeek runs", color: COLORS.s1 }', block)
        self.assertIn('{ key: "kimi", name: "Kimi runs", color: COLORS.s2 }', block)
        self.assertNotIn('dash: "4 3"', block)
        self.assertNotIn("unsplit", block.lower())
        self.assertIn("dsq: w.ds_queued", html)
        self.assertNotIn("Queued jobs", block)
        self.assertIn("stroke-dasharray", html)

    def test_old_format_store_still_draws_runs_across_the_whole_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(Path(tmp))
            conn = sqlite3.connect(home / "dash.db")
            conn.execute("CREATE TABLE samples (ts REAL PRIMARY KEY, ds_workers INT, kimi_workers INT, "
                         "queue_workers INT, direct_workers INT, queued_jobs INT)")
            conn.executemany("INSERT INTO samples VALUES (?, 3, 1, 0, 0, 2)",
                             [(NOW - age,) for age in range(3500, 0, -60)])
            conn.commit()
            conn.close()
            workers = dash.build_summary(home, 1, now=NOW, fetch_live=no_proxy)["workers"]
        self.assertEqual(len(workers), 59)
        self.assertTrue(all(w["ds_workers"] == 3 and w["kimi_workers"] == 1 for w in workers))
        self.assertTrue(all(w["ds_queued"] is None and "queued_unsplit" not in w for w in workers))

    def test_summary_workers_follow_the_selected_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(Path(tmp))
            conn = sqlite3.connect(home / "dash.db")
            conn.execute(dash.SAMPLES_DDL)
            conn.executemany(
                "INSERT INTO samples (ts, ds_workers, kimi_workers, queue_workers, direct_workers, queued_jobs, ds_queued, kimi_queued) "
                "VALUES (?, 1, 0, 0, 0, 2, 1, 1)",
                [(NOW - 2 * 3600,), (NOW - 600,)],
            )
            conn.commit()
            conn.close()
            one = dash.build_summary(home, 1, now=NOW, fetch_live=no_proxy)["workers"]
            six = dash.build_summary(home, 6, now=NOW, fetch_live=no_proxy)["workers"]
        self.assertEqual(len(one), 1)
        self.assertEqual(len(six), 2)


class RangeConsistencyTest(unittest.TestCase):
    """Every field the page labels with the selected range must move when the range moves, and the Now fields must not."""

    RANGE_FIELDS = {
        "tokens_total": lambda s: s["range_totals"]["tokens"]["total"],
        "r429": lambda s: s["range_totals"]["r429"],
        "requests": lambda s: s["range_totals"]["requests"],
        "finished_pass": lambda s: s["range_totals"]["finished"]["PASS"],
        "finished_fail": lambda s: s["range_totals"]["finished"]["FAIL"],
        "finished_partial": lambda s: s["range_totals"]["finished"]["PARTIAL"],
        "total_pool_range_avg": lambda s: s["limits"]["total"]["range_avg_frac"],
        "ds_uncached_range_avg": lambda s: s["limits"]["uncached_ds"]["range_avg_frac"],
        "morph_wait_p95": lambda s: s["perf_kpis"]["wait_p95_ms"],
        "morph_wait_p50": lambda s: s["perf_kpis"]["wait_p50_ms"],
        "minute_rows": lambda s: len(s["minutes"]),
    }
    NOW_FIELDS = {
        "tokens_5m": lambda s: s["activity"]["tokens_5m"]["tokens"],
        "tokens_15m": lambda s: s["activity"]["tokens_15m"]["tokens"],
        "verdict": lambda s: s["verdict"]["key"],
        "waiting_now": lambda s: s["capacity"]["waiting_now"],
    }

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = range_fixture(Path(self.tmp.name))
        self.summaries = {
            hours: dash.build_summary(self.home, hours, now=NOW, fetch_live=proxy_status, laptop=CALM)
            for hours in dash.RANGE_HOURS
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_every_range_bound_field_changes_with_the_range(self) -> None:
        for name, field in self.RANGE_FIELDS.items():
            values = [field(self.summaries[hours]) for hours in dash.RANGE_HOURS]
            with self.subTest(field=name):
                self.assertGreater(len(set(map(repr, values))), 1, values)
                self.assertNotEqual(values[0], values[-1], values)

    def test_now_fields_do_not_follow_the_range(self) -> None:
        for name, field in self.NOW_FIELDS.items():
            values = {repr(field(self.summaries[hours])) for hours in dash.RANGE_HOURS}
            with self.subTest(field=name):
                self.assertEqual(len(values), 1, values)

    def test_one_hour_range_counts_only_the_last_hour(self) -> None:
        one = self.summaries[1]
        self.assertEqual(one["range_totals"]["tokens"]["total"], 2_000_000)
        self.assertEqual(one["range_totals"]["finished"], {"PASS": 1, "PARTIAL": 1, "FAIL": 1})
        self.assertEqual(one["bucket_s"], dash.BUCKET_S[1])
        self.assertEqual(self.summaries[168]["range_totals"]["tokens"]["total"], 2_000_000 * (1 + 2 + 3 + 4))
        self.assertEqual(self.summaries[168]["range_totals"]["finished"], {verdict: 4 for verdict in VERDICTS})

    def test_range_average_counts_from_the_first_recorded_minute(self) -> None:
        one = self.summaries[1]
        self.assertEqual(one["limits"]["total"]["range_avg_frac"], round(2_000_000 / 30 / dash.LIMITS["total"], 4))
        self.assertEqual(one["limits"]["total"]["avg_5m_frac"], 0.0)


class SummaryCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.tmp.name))
        self.now = 1000.0
        self.cache = dash.SummaryCache(self.home, fetch_live=no_proxy, clock=lambda: self.now)

    def tearDown(self) -> None:
        self.cache.stop()
        self.cache.wait_for_computes()
        self.tmp.cleanup()

    def counting_build(self):
        return mock.patch.object(dash, "build_summary", wraps=dash.build_summary)

    def test_first_request_computes_and_later_requests_serve_the_stored_bytes(self) -> None:
        with self.counting_build() as build:
            first, generated = self.cache.get(1)
            second, generated_again = self.cache.get(1)
        self.assertEqual(build.call_count, 1)
        self.assertIs(first, second)
        self.assertEqual(generated, generated_again)
        self.assertEqual(json.loads(first)["generated_at"], generated)
        self.assertEqual(json.loads(first)["source_age_s"], {"live": None, "laptop": None})
        self.assertEqual(json.loads(first)["cadence_s"], dash.RANGE_CADENCE_S[1])

    def test_worker_recomputes_each_range_on_its_cadence_while_active(self) -> None:
        with self.counting_build() as build:
            self.cache.touch()
            self.cache.tick()
            self.cache.wait_for_computes()
            self.assertEqual(build.call_count, 4)
            self.now += 4
            self.cache.tick()
            self.cache.wait_for_computes()
            self.assertEqual(build.call_count, 4)
            self.now += 1
            self.cache.tick()
            self.cache.wait_for_computes()
            self.assertEqual(build.call_count, 6)
            self.now += 10
            self.cache.touch()
            self.cache.tick()
            self.cache.wait_for_computes()
            self.assertEqual(build.call_count, 9)

    def test_worker_is_idle_once_nobody_has_asked_for_IDLE_AFTER_S(self) -> None:
        with self.counting_build() as build:
            self.cache.touch()
            self.cache.tick()
            self.cache.wait_for_computes()
            before = build.call_count
            self.now += dash.IDLE_AFTER_S + 1
            self.cache.tick()
            self.cache.wait_for_computes()
            self.assertEqual(build.call_count, before)
            self.assertFalse(self.cache.active())

    def test_a_request_after_idle_serves_the_stored_bytes_at_once(self) -> None:
        with self.counting_build() as build:
            self.cache.touch()
            self.cache.tick()
            self.cache.wait_for_computes()
            self.now += 10 * 3600
            body, generated = self.cache.get(1)
        self.assertEqual(build.call_count, 4)
        self.assertLess(generated, self.now)
        self.assertIsNotNone(body)

    def test_a_slow_reader_serves_its_last_value_with_its_age(self) -> None:
        reader = dash.BackgroundReader("probe", lambda: {"ok": True}, lambda: True, clock=lambda: self.now)
        self.assertIsNone(reader.latest())
        reader.refresh()
        self.now += 7
        self.assertEqual(reader.latest(), {"ok": True})
        self.assertEqual(reader.age(self.now), 7.0)

    def test_a_partial_body_never_replaces_a_full_one(self) -> None:
        full = dash.Stored(b"full", 100.0, False, 1)
        self.cache.bodies[6] = full
        self.assertIs(self.cache._store(6, dash.Stored(b"partial", 200.0, True, 2)), full)
        self.assertEqual(self.cache.bodies[6], full)

    def test_a_newer_partial_replaces_an_older_partial(self) -> None:
        self.cache.bodies[6] = dash.Stored(b"old", 100.0, True, 1)
        newer = dash.Stored(b"new", 200.0, True, 2)
        self.assertIs(self.cache._store(6, newer), newer)

    def test_a_newer_full_replaces_any_body(self) -> None:
        self.cache.bodies[6] = dash.Stored(b"partial", 200.0, True, 2)
        newer_full = dash.Stored(b"full", 300.0, False, 3)
        self.assertIs(self.cache._store(6, newer_full), newer_full)

    def test_an_older_full_never_replaces_a_newer_full(self) -> None:
        """The review case: a full compute that started first but finished late, over a newer body."""
        self.cache.bodies[6] = dash.Stored(b"newer", 200.0, False, 5)
        older_full = dash.Stored(b"older", 100.0, False, 4)
        self.assertIs(self.cache._store(6, older_full), self.cache.bodies[6])
        self.assertEqual(self.cache.bodies[6].body, b"newer")

    def test_an_older_full_never_replaces_a_newer_partial(self) -> None:
        self.cache.bodies[6] = dash.Stored(b"partial", 200.0, True, 5)
        self.cache._store(6, dash.Stored(b"full", 100.0, False, 4))
        self.assertTrue(self.cache.bodies[6].partial)
        self.assertEqual(self.cache.bodies[6].body, b"partial")

    def test_the_generation_is_taken_when_each_compute_starts(self) -> None:
        self.cache._start_compute(6)
        self.cache.wait_for_computes()
        first = self.cache.bodies[6].generation
        self.cache._start_compute(6)
        self.cache.wait_for_computes()
        self.assertGreater(self.cache.bodies[6].generation, first)


class BlockedReaderTest(unittest.TestCase):
    """A proxy status that never answers, or a locked agentq.db, must not hold up a request."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.release.set()
        self.cache.stop()
        self.cache.wait_for_computes()
        self.tmp.cleanup()

    def test_request_returns_under_a_second_while_the_proxy_status_read_is_blocked(self) -> None:
        self.release = threading.Event()

        def never_answers():
            self.release.wait(60)
            raise OSError("released")

        self.cache = dash.SummaryCache(self.home, fetch_live=never_answers)
        self.cache.start()
        started = time.monotonic()
        body, _ = self.cache.get(6)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, elapsed)
        self.assertIsNone(json.loads(body)["live"])

    def test_request_returns_within_its_wait_while_agentq_db_is_locked(self) -> None:
        self.release = threading.Event()
        self.cache = dash.SummaryCache(self.home, fetch_live=no_proxy)
        lock = sqlite3.connect(self.home / "agentq.db", isolation_level=None, timeout=5)
        lock.execute("BEGIN EXCLUSIVE")
        try:
            started = time.monotonic()
            body, _ = self.cache.get(6)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.5, elapsed)
            first = json.loads(body)
            self.assertTrue(first.get("warming_up") or first["partial"])
            self.cache.wait_for_computes(10)
            summary = json.loads(self.cache.get(6)[0])
        finally:
            lock.execute("ROLLBACK")
            lock.close()
        self.assertTrue(summary["partial"])
        self.assertTrue(any("time budget" in message for message in summary["errors"].values()), summary["errors"])
        self.assertEqual(summary["hours"], 6)

    def test_a_partial_summary_is_replaced_by_the_full_compute(self) -> None:
        self.release = threading.Event()
        self.cache = dash.SummaryCache(self.home, fetch_live=no_proxy)
        lock = sqlite3.connect(self.home / "agentq.db", isolation_level=None, timeout=5)
        lock.execute("BEGIN EXCLUSIVE")
        try:
            self.cache.get(6)
            self.cache.wait_for_computes(10)
            partial = json.loads(self.cache.get(6)[0])
        finally:
            lock.execute("ROLLBACK")
            lock.close()
        self.assertTrue(partial["partial"])
        self.cache._start_compute(6)
        self.cache.wait_for_computes()
        self.assertFalse(json.loads(self.cache.get(6)[0])["partial"])


class CalibReadTimingTest(unittest.TestCase):
    """The 429 reader on 200k calib lines across the live log and its backup: cold, incremental, rotated, replaced."""

    @staticmethod
    def calib_line(i: int) -> str:
        minute = dash.minute_stamp(UNTIL - 60 * (i % 120))
        budget = "uncached_ds" if i % 2 == 0 else "None"
        return f"2026-11-13T09:00:{i % 60:02d}Z agentq-proxy calib minute={minute} budget={budget} sec=30.0\n"

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = Path(cls.tmp.name)
        logs = cls.home / "logs"
        logs.mkdir()
        (logs / "proxy.err.log.1").write_text("".join(cls.calib_line(i) for i in range(100_000)), encoding="utf-8")
        (logs / "proxy.err.log").write_text("".join(cls.calib_line(i) for i in range(100_000, 200_000)), encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def setUp(self) -> None:
        dash._CALIB_FILES.clear()

    def test_cold_read_of_200k_lines_across_live_and_backup_is_under_half_a_second(self) -> None:
        started = time.perf_counter()
        read = dash.read_calib_429s(self.home)
        elapsed = time.perf_counter() - started
        self.assertIsNotNone(read)
        self.assertGreater(sum(read["counts"].values()), 0)
        self.assertLess(elapsed, 0.5, f"cold read took {elapsed:.3f} s")

    def test_incremental_read_after_appends_is_under_twenty_milliseconds(self) -> None:
        dash.read_calib_429s(self.home)
        logs = self.home / "logs"
        before = sum(dash.read_calib_429s(self.home)["counts"].values())
        with open(logs / "proxy.err.log", "a", encoding="utf-8") as handle:
            handle.write("".join(self.calib_line(200_000 + i) for i in range(100)))
        started = time.perf_counter()
        after = dash.read_calib_429s(self.home)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.02, f"incremental read took {elapsed * 1000:.1f} ms")
        self.assertEqual(sum(after["counts"].values()) - before, 50)

    def test_only_appended_bytes_are_parsed_after_the_first_read(self) -> None:
        dash.read_calib_429s(self.home)
        appended = "".join(self.calib_line(300_000 + i) for i in range(10))
        with open(self.home / "logs" / "proxy.err.log", "a", encoding="utf-8") as handle:
            handle.write(appended)
        with mock.patch.object(dash, "_parse_calib", wraps=dash._parse_calib) as parse:
            dash.read_calib_429s(self.home)
        self.assertEqual(sum(len(call.args[1]) for call in parse.call_args_list), len(appended.encode("utf-8")))

    def test_a_rotated_file_keeps_its_counts_without_being_read_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "logs").mkdir()
            live = home / "logs" / "proxy.err.log"
            live.write_text("".join(self.calib_line(i) for i in range(1000)), encoding="utf-8")
            before = dash.read_calib_429s(home)["counts"].copy()
            os.replace(live, live.with_name(live.name + ".1"))
            fresh = "".join(self.calib_line(i) for i in range(1000, 1010))
            live.write_text(fresh, encoding="utf-8")
            with mock.patch.object(dash, "_parse_calib", wraps=dash._parse_calib) as parse:
                after = dash.read_calib_429s(home)["counts"]
        self.assertEqual(sum(after.values()), sum(before.values()) + 5)
        self.assertEqual(sum(len(call.args[1]) for call in parse.call_args_list), len(fresh.encode("utf-8")))

    def test_a_file_replaced_under_the_same_path_is_parsed_afresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "logs").mkdir()
            live = home / "logs" / "proxy.err.log"
            minute = dash.minute_stamp(UNTIL - 60)
            live.write_text(f"{minute}:30Z agentq-proxy calib minute={minute} budget=total sec=1\n" * 3, encoding="utf-8")
            self.assertEqual(sum(dash.read_calib_429s(home)["counts"].values()), 3)
            live.unlink()
            live.write_text(f"{minute}:30Z agentq-proxy calib minute={minute} budget=total sec=1\n" * 5, encoding="utf-8")
            self.assertEqual(sum(dash.read_calib_429s(home)["counts"].values()), 5)


class WarmingPageTest(unittest.TestCase):
    def test_the_page_shows_updating_for_a_warming_body_and_keeps_the_last_figures(self) -> None:
        html = dash.PAGE_HTML
        self.assertIn("if (data.warming_up) {", html)
        self.assertIn("state.warming = true;", html)
        self.assertIn('"updating…"', html)


class RequestWaitTest(unittest.TestCase):
    """A request for a range with no body waits at most REQUEST_WAIT_S, then gets the warming body while a build runs."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.tmp.name))
        self.gate = threading.Event()
        self.cache = dash.SummaryCache(self.home, fetch_live=no_proxy)

    def tearDown(self) -> None:
        self.gate.set()
        self.cache.stop()
        self.cache.wait_for_computes(10)
        self.tmp.cleanup()

    def test_a_request_during_a_running_build_returns_the_warming_body_within_the_wait(self) -> None:
        real_build = dash.build_summary

        def slow(*args, **kwargs):
            self.gate.wait(20)
            return real_build(*args, **kwargs)

        with mock.patch.object(dash, "build_summary", side_effect=slow):
            self.cache._start_compute(6)
            started = time.monotonic()
            body, _ = self.cache.get(6)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, dash.REQUEST_WAIT_S + 0.5, elapsed)
        warming = json.loads(body)
        self.assertTrue(warming["warming_up"])
        self.assertIsNone(warming["generated_at"])
        self.gate.set()
        self.cache.wait_for_computes(10)
        self.assertNotIn("warming_up", json.loads(self.cache.get(6)[0]))

    def test_a_request_skips_the_proxy_log_so_its_build_never_parses_it(self) -> None:
        with mock.patch.object(dash, "read_calib_429s", side_effect=AssertionError("log read on a request build")):
            body, _ = self.cache.get(6)
            self.cache.wait_for_computes(10)
        summary = json.loads(self.cache.get(6)[0])
        self.assertTrue(summary["partial"])
        first = json.loads(body)
        self.assertTrue(first.get("warming_up") or first["partial"])

    def test_a_range_never_has_two_builds_at_once(self) -> None:
        real_build = dash.build_summary

        def slow(*args, **kwargs):
            self.gate.wait(20)
            return real_build(*args, **kwargs)

        with mock.patch.object(dash, "build_summary", side_effect=slow):
            self.assertTrue(self.cache._start_compute(6, request=True))
            self.assertFalse(self.cache._start_compute(6))
            self.assertFalse(self.cache._start_compute(6, force=True))
            self.gate.set()
            self.cache.wait_for_computes(10)


class WatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.tmp.name))
        self.now = 1000.0
        self.release = threading.Event()
        self.cache = dash.SummaryCache(self.home, fetch_live=no_proxy, clock=lambda: self.now)

    def tearDown(self) -> None:
        self.release.set()
        self.cache.stop()
        self.cache.wait_for_computes()
        self.tmp.cleanup()

    def test_a_range_older_than_three_cadences_is_recomputed_on_a_fresh_worker_and_logged_once(self) -> None:
        real_build = dash.build_summary
        calls = []

        def stuck_then_fine(home, hours, *args, **kwargs):
            if hours != 6:
                return real_build(home, hours, *args, **kwargs)
            calls.append(threading.current_thread().name)
            if len(calls) == 1:
                self.release.wait(30)
            return real_build(home, hours, *args, **kwargs)

        with mock.patch.object(dash, "build_summary", side_effect=stuck_then_fine), \
                mock.patch.object(dash.sys, "stderr", new=io.StringIO()) as err:
            self.cache.touch()
            self.cache._start_compute(6)
            time.sleep(0.05)
            self.cache.bodies[6] = dash.Stored(b"{}", self.now - 3 * dash.RANGE_CADENCE_S[6] - 1, False, 0)
            self.now += 1
            self.cache.tick()
            self.cache.tick()
            self.cache.wait_for_computes(10)
            fresh_generated = self.cache.bodies[6].generated_at
        self.assertEqual(len(calls), 2, calls)
        self.assertGreater(fresh_generated, self.now - 1)
        self.assertEqual(err.getvalue().count("recomputing on a fresh worker"), 1, err.getvalue())

    def test_a_fresh_range_is_not_forced(self) -> None:
        with mock.patch.object(dash, "build_summary", wraps=dash.build_summary) as build, \
                mock.patch.object(dash.sys, "stderr", new=io.StringIO()) as err:
            self.cache.touch()
            self.cache.tick()
            self.cache.wait_for_computes()
            first = build.call_count
            self.cache.tick()
            self.cache.wait_for_computes()
        self.assertEqual(build.call_count, first)
        self.assertNotIn("recomputing", err.getvalue())


class ComputeCapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.cache.stop()
        self.cache.wait_for_computes()
        self.tmp.cleanup()

    def test_a_persistently_locked_store_keeps_live_computes_within_the_cap_for_sixty_seconds(self) -> None:
        real_build = dash.build_summary

        def slow_build(*args, **kwargs):
            time.sleep(0.25)
            return real_build(*args, **kwargs)

        clock = {"now": 1000.0}
        self.cache = dash.SummaryCache(self.home, fetch_live=no_proxy, clock=lambda: clock["now"])
        lock = sqlite3.connect(self.home / "agentq.db", isolation_level=None, timeout=5)
        lock.execute("BEGIN EXCLUSIVE")
        peak = 0
        peak_per_range = 0
        try:
            with mock.patch.object(dash, "build_summary", side_effect=slow_build), \
                    mock.patch.object(dash.sys, "stderr", new=io.StringIO()):
                self.cache.touch()
                for _ in range(60):
                    self.cache.touch()
                    self.cache.tick()
                    peak = max(peak, self.cache.live_computes())
                    with self.cache.lock:
                        peak_per_range = max([peak_per_range] + [len(c) for c in self.cache.inflight.values()])
                    clock["now"] += 1.0
                    time.sleep(0.02)
                self.cache.wait_for_computes(30)
        finally:
            lock.execute("ROLLBACK")
            lock.close()
        self.assertLessEqual(peak, dash.MAX_LIVE_COMPUTES)
        self.assertLessEqual(peak_per_range, 2)
        self.assertGreater(peak, 0)

    def test_a_forced_start_never_bypasses_the_cap(self) -> None:
        self.cache = dash.SummaryCache(self.home, fetch_live=no_proxy, clock=lambda: 1000.0)
        gate = threading.Event()
        with mock.patch.object(dash, "build_summary", side_effect=lambda *a, **k: (gate.wait(10), None)[1]):
            started = [self.cache._start_compute(h) for h in dash.RANGE_HOURS]
            forced = [self.cache._start_compute(h, force=True) for h in dash.RANGE_HOURS]
            gate.set()
            self.cache.wait_for_computes(10)
        self.assertLessEqual(sum(started) + sum(forced), dash.MAX_LIVE_COMPUTES)
        self.assertFalse(all(forced))

    def test_one_ordinary_and_one_forced_compute_per_range_at_a_time(self) -> None:
        self.cache = dash.SummaryCache(self.home, fetch_live=no_proxy, clock=lambda: 1000.0)
        gate = threading.Event()
        with mock.patch.object(dash, "build_summary", side_effect=lambda *a, **k: (gate.wait(10), None)[1]):
            self.assertTrue(self.cache._start_compute(1))
            self.assertFalse(self.cache._start_compute(1))
            self.assertTrue(self.cache._start_compute(1, force=True))
            self.assertFalse(self.cache._start_compute(1, force=True))
            gate.set()
            self.cache.wait_for_computes(10)
        self.assertEqual(self.cache.live_computes(), 0)


class SamplerGapTest(unittest.TestCase):
    def test_unreadable_jobs_table_reports_queue_and_direct_as_unknown_not_direct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            row = dash.sample_once(home, ps_runner=lambda: "  101 /x/launch_pi.py --model morph-dsv41flash-1017\n", now=NOW)
            stored = sqlite3.connect(home / "dash.db").execute("SELECT ds_workers, queue_workers, direct_workers FROM samples").fetchall()
        self.assertEqual((row["ds_workers"], row["queue_workers"], row["direct_workers"]), (1, None, None))
        self.assertEqual(stored, [(1, None, None)])

    def test_readable_jobs_table_still_splits_queue_from_direct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(Path(tmp))
            row = dash.sample_once(home, ps_runner=lambda: "  101 /x/launch_pi.py --model morph-dsv41flash-1017\n  102 /x/launch_pi.py --tier pi-kimi\n", now=NOW)
        self.assertEqual((row["queue_workers"], row["direct_workers"]), (1, 1))


class RunPsTest(unittest.TestCase):
    def test_ps_is_called_without_environments_and_with_a_short_timeout(self) -> None:
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["timeout"] = kwargs.get("timeout")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch.object(dash.subprocess, "run", side_effect=fake_run):
            dash.run_ps()
        self.assertEqual(captured["args"], ["ps", "-axo", "pid=,command="])
        self.assertLessEqual(captured["timeout"], 5.0)

    def test_queue_workers_come_from_running_queue_jobs_not_the_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(Path(tmp))
            ps_without_env = "  101 /x/launch_pi.py --model morph-dsv41flash-1017\n  102 /x/launch_pi.py --tier pi-kimi\n"
            row = dash.sample_once(home, ps_runner=lambda: ps_without_env, now=NOW)
        self.assertEqual(row["queue_workers"], 1)
        self.assertEqual(row["direct_workers"], 1)


class EasternTimeTest(unittest.TestCase):
    def test_page_formats_every_time_in_america_new_york_not_the_browser_zone(self) -> None:
        html = dash.PAGE_HTML
        self.assertIn('timeZone: "America/New_York"', html)
        self.assertNotIn("getHours()", html)
        self.assertNotIn("getMinutes()", html)
        self.assertIn('"updated " + hms(data.generated_at)', html)
        self.assertIn('"Start (ET)"', html)
        self.assertIn("Time (UTC)", html)

    def test_eastern_labels_match_the_zoneinfo_reference(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        from zoneinfo import ZoneInfo
        from datetime import datetime

        html = dash.PAGE_HTML
        snippet = html[html.index("  function pad(n)"):html.index("  function pct(")]
        probes = [1794542644, 1794506750, 1794524400]
        script = snippet + "\n" + "\n".join(
            f"console.log(hm({sec}) + '|' + hms({sec}) + '|' + dateHM({sec}));" for sec in probes
        )
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(len(lines), len(probes))
        for sec, line in zip(probes, lines):
            moment = datetime.fromtimestamp(sec, ZoneInfo("America/New_York"))
            expected = "|".join([
                moment.strftime("%H:%M") + " ET",
                moment.strftime("%H:%M:%S") + " ET",
                moment.strftime("%m-%d %H:%M") + " ET",
            ])
            self.assertEqual(line, expected)

    def test_stale_data_shows_amber_past_twice_its_cadence(self) -> None:
        html = dash.PAGE_HTML
        self.assertIn("if (age > 2 * (data.cadence_s || 60))", html)
        self.assertIn('aged.textContent = " · data " + Math.round(age) + " s old";', html)
        self.assertIn(".status .aged", html)


class FaulthandlerTest(unittest.TestCase):
    def test_main_registers_a_stack_dump_on_sigusr1(self) -> None:
        import signal as signal_module

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(dash.faulthandler, "register") as register, \
                mock.patch.object(dash, "make_server") as make_server, \
                mock.patch.object(dash, "Sampler") as sampler:
            make_server.return_value.serve_forever.side_effect = KeyboardInterrupt
            make_server.return_value.server_address = ("127.0.0.1", 1)
            dash.main(["--port", "0", "--home", tmp])
            sampler.return_value.start.assert_called_once()
        register.assert_called_once()
        self.assertEqual(register.call_args.args[0], signal_module.SIGUSR1)
        self.assertTrue(register.call_args.kwargs.get("all_threads"))


class ListenBacklogTest(unittest.TestCase):
    def test_a_burst_of_concurrent_connections_is_all_accepted(self) -> None:
        self.assertGreaterEqual(dash.DashServer.request_queue_size, 32)
        with tempfile.TemporaryDirectory() as tmp:
            server = dash.make_server(make_home(Path(tmp)), 0, fetch_live=no_proxy)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            statuses, errors = [], []

            def fetch():
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
                    conn.request("GET", "/api/summary?hours=1")
                    statuses.append(conn.getresponse().status)
                    conn.close()
                except OSError as exc:
                    errors.append(exc)

            workers = [threading.Thread(target=fetch) for _ in range(16)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(30)
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(statuses, [200] * 16)


class UnlaunchedJobsTest(unittest.TestCase):
    """Jobs that failed before launching never count and never list; they collapse to one line per reason."""

    BAD_LABEL = "launch failed: STATUS: failed out= err= tier=pi-deepseek-flash reason=bad-label; 3 attempts"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.tmp.name))
        conn = sqlite3.connect(self.home / "agentq.db")
        rows = [
            (f"{n:012x}", "failed", "backlog", "pi-deepseek-flash", "write", None, NOW - 900 + n, None, NOW - 890 + n,
             f"hub-exec-backlog-filler-{n}", self.BAD_LABEL, None)
            for n in range(3)
        ]
        rows.append(("f" * 12, "failed", "backlog", "pi-deepseek-flash", "write", None, NOW - 500, None, NOW - 490,
                     "hub-exec-backlog-filler-boom", "launch failed: boom", None))
        rows.append(("e" * 12, "failed", "normal", "pi-deepseek-flash", "read", "FAIL", NOW - 400, NOW - 390, NOW - 380,
                     "launched-then-failed", "launch failed: spawn exited", 4242))
        rows.append(("d" * 12, "failed", "normal", "pi-deepseek-flash", "read", "FAIL", NOW - 300, NOW - 290, NOW - 280,
                     "ran-and-timed-out", "max runtime 2700s exceeded", 77))
        conn.executemany(
            "INSERT INTO jobs (id, state, lane, tier, kind, verdict, submitted_at, started_at, finished_at, label, detail, res, pid) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,'light',?)", rows,
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_launch_failures_are_out_of_the_counts_and_the_recent_list(self) -> None:
        jobs = dash.read_jobs(self.home / "agentq.db")
        self.assertEqual(jobs["by_state"]["failed"], 2, "only the two that launched count as failed")
        recent_ids = {row["id"] for row in jobs["recent"]}
        self.assertFalse({f"{n:012x}" for n in range(3)} & recent_ids)
        self.assertNotIn("f" * 12, recent_ids)
        self.assertIn("d" * 12, recent_ids)
        self.assertIn("e" * 12, recent_ids, "a job that got a pid launched, so it stays listed")

    def test_launch_failures_group_by_reason_with_the_fix_time(self) -> None:
        unlaunched = dash.read_jobs(self.home / "agentq.db")["unlaunched"]
        self.assertEqual(unlaunched["total"], 4)
        by_key = {group["key"]: group for group in unlaunched["groups"]}
        self.assertEqual(by_key["bad-label"]["count"], 3)
        self.assertEqual(by_key["bad-label"]["text"], "bad label")
        self.assertEqual(by_key["bad-label"]["fixed"], ", fixed 12:32 ET")
        self.assertEqual(by_key["boom"]["count"], 1)
        self.assertEqual(by_key["boom"]["fixed"], "")
        self.assertEqual(unlaunched["groups"][0]["key"], "bad-label", "the biggest group first")

    def test_the_expandable_list_names_each_hidden_job(self) -> None:
        recent = dash.read_jobs(self.home / "agentq.db")["unlaunched"]["recent"]
        self.assertEqual(len(recent), 4)
        self.assertEqual({row["reason"] for row in recent}, {"bad label", "boom"})

    def test_finished_counts_leave_launch_failures_out_even_with_a_verdict(self) -> None:
        conn = sqlite3.connect(self.home / "agentq.db")
        conn.execute("UPDATE jobs SET verdict = 'FAIL', finished_at = ? WHERE id = ?", (NOW - 100, f"{0:012x}"))
        conn.commit()
        conn.close()
        summary = dash.build_summary(self.home, 1, now=NOW, fetch_live=no_proxy)
        self.assertEqual(summary["range_totals"]["finished"]["FAIL"], 2, "the two FAILs from jobs that ran; the launch failure is out")

    def test_launch_failures_do_not_appear_in_the_runs_table(self) -> None:
        conn = sqlite3.connect(self.home / "agentq.db")
        conn.execute(PROXY_JOBS_DDL)
        conn.executemany(
            "INSERT INTO proxy_jobs (job, model, first_ts, last_ts, req, ok, r429, input, cache_read, output, wait_ms_total, upstream_ms_total) "
            "VALUES (?, 'morph-dsv41flash-1017', ?, ?, 1, 0, 0, 0, 0, 0, 0, 0)",
            [(f"{0:012x}", NOW - 300, NOW - 290), ("d" * 12, NOW - 300, NOW - 290)],
        )
        conn.commit()
        conn.close()
        runs = dash.read_run_rows(self.home / "agentq.db", NOW - 3600)
        self.assertEqual([row["job"] for row in runs], ["d" * 12])

    def test_the_page_collapses_launch_failures_and_names_backlog_jobs_as_filler(self) -> None:
        html = dash.PAGE_HTML
        self.assertIn('<div id="runs-unlaunched" class="unlaunched"></div>', html)
        self.assertIn('<div id="jobs-unlaunched" class="unlaunched"></div>', html)
        self.assertIn('" failed to launch ("', html)
        self.assertIn('\\u2014 hidden', html)
        self.assertIn('lane === "backlog" ? "backlog filler" : lane', html)
        self.assertIn("cell: function (r) { return laneLabel(r.lane); }", html)


class ClientGoneTest(unittest.TestCase):
    def test_hang_ups_are_logged_at_most_once_a_minute(self) -> None:
        class Server:
            gone_logged_at = float("-inf")

        server = Server()
        with mock.patch.object(dash.time, "time", return_value=1000.0), \
                mock.patch.object(dash.sys, "stderr", new=io.StringIO()) as err:
            dash.note_client_gone(server)
            dash.note_client_gone(server)
        self.assertEqual(err.getvalue().count("client closed the connection early"), 1)

    def test_broken_pipe_during_a_request_does_not_escape_the_handler(self) -> None:
        handler = dash.Handler.__new__(dash.Handler)
        handler.server = mock.Mock(gone_logged_at=float("-inf"))
        with mock.patch.object(dash.BaseHTTPRequestHandler, "handle_one_request", side_effect=BrokenPipeError), \
                mock.patch.object(dash.sys, "stderr", new=io.StringIO()):
            handler.handle_one_request()
        self.assertTrue(handler.close_connection)


class LifetimeRunTokensTest(unittest.TestCase):
    """proxy_jobs keeps lifetime totals per job, so runs in a window are shown with lifetime tokens and labelled so."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.tmp.name))
        conn = sqlite3.connect(self.home / "agentq.db")
        conn.execute(PROXY_JOBS_DDL)
        conn.executemany(
            "INSERT INTO proxy_jobs (job, model, first_ts, last_ts, req, ok, r429, input, cache_read, output, wait_ms_total, upstream_ms_total) "
            "VALUES (?, 'morph-dsv41flash-1017', ?, ?, 10, 10, 0, 700000, 0, 0, 0, 0)",
            [("1" * 12, NOW - 3 * 24 * 3600, NOW - 1800), ("2" * 12, NOW - 5 * 24 * 3600, NOW - 3 * 24 * 3600)],
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_run_active_in_the_hour_shows_its_lifetime_tokens_and_an_old_run_is_absent(self) -> None:
        summary = dash.build_summary(self.home, 1, now=NOW, fetch_live=no_proxy)
        jobs = {row["job"]: row for row in summary["top_consumers"]}
        self.assertIn("1" * 12, jobs)
        self.assertEqual(jobs["1" * 12]["total_tokens"], 700000)
        self.assertNotIn("2" * 12, jobs)
        self.assertEqual(summary["run_kpis"]["active_in_window"], 1)
        self.assertEqual(summary["run_kpis"]["active_lifetime_tokens"], 700000)

    def test_the_page_labels_top_consumers_as_lifetime_tokens(self) -> None:
        self.assertIn("Lifetime tokens of the ", dash.PAGE_HTML)
        self.assertIn("runs active ", dash.PAGE_HTML)


class SmallChartRateTest(unittest.TestCase):
    """Per-minute values must stay per minute at every bucket size: the 429 chart plots a mean, not a sum."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_home(Path(self.tmp.name), minutes=False, jobs=False)
        conn = sqlite3.connect(self.home / "agentq.db")
        conn.execute(PROXY_MINUTES_DDL)
        conn.executemany(
            f"INSERT INTO proxy_minutes ({MINUTE_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(minute_at(60 * k), "morph-dsv41flash-1017", 1, 1, 1, 100, 0, 0, 0, 0, 500, 0.9) for k in range(1, 121)],
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_429_rate_is_one_per_minute_at_24h_and_7d_buckets(self) -> None:
        for hours, bucket in ((24, 300), (168, 1800)):
            with self.subTest(hours=hours):
                summary = dash.build_summary(self.home, hours, now=NOW, fetch_live=no_proxy)
                self.assertEqual(summary["bucket_s"], bucket)
                full = [m for m in summary["minutes"] if m["minutes"] == bucket // 60]
                self.assertTrue(full)
                self.assertTrue(all(m["r429_per_min"] == 1.0 for m in summary["minutes"]), summary["minutes"][:3])
                self.assertTrue(all(m["r429"] == m["minutes"] for m in full))

    def test_429_chart_plots_the_per_minute_rate_and_says_so(self) -> None:
        html = dash.PAGE_HTML
        start = html.index('registerChart("c-429"')
        block = html[start:html.index("registerChart(", start + 1)]
        self.assertIn('key: "r429_per_min"', block)
        self.assertIn("429s per minute", block)

    def test_wait_p95_stays_a_worst_minute_at_7d(self) -> None:
        summary = dash.build_summary(self.home, 168, now=NOW, fetch_live=no_proxy)
        self.assertTrue(all(m["wait_p95_ms"] == 500 for m in summary["minutes"] if m["wait_p95_ms"] is not None))


class ConcurrentMigrationTest(unittest.TestCase):
    def test_writers_on_an_old_schema_db_lose_no_samples(self) -> None:
        import concurrent.futures

        with tempfile.TemporaryDirectory() as tmp:
            dash_db = Path(tmp) / "dash.db"
            conn = sqlite3.connect(dash_db)
            conn.execute("CREATE TABLE samples (ts REAL PRIMARY KEY, ds_workers INT, kimi_workers INT, "
                         "queue_workers INT, direct_workers INT, queued_jobs INT)")
            conn.commit()
            conn.close()
            rows = [
                {"ts": NOW + i, "ds_workers": 1, "kimi_workers": 0, "queue_workers": 0, "direct_workers": 0,
                 "queued_jobs": 0, "ds_queued": 0, "kimi_queued": 0}
                for i in range(12)
            ]
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                list(pool.map(lambda row: dash.write_sample(dash_db, row), rows))
            conn = sqlite3.connect(dash_db)
            count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            columns = [r[1] for r in conn.execute("PRAGMA table_info(samples)")]
            conn.close()
        self.assertEqual(count, 12)
        self.assertIn("ds_queued", columns)
        self.assertIn("kimi_queued", columns)


class LogPollingTest(unittest.TestCase):
    def test_refresh_log_stamps_its_fetch_so_the_fifteen_second_throttle_applies(self) -> None:
        html = dash.PAGE_HTML
        start = html.index("function refreshLog()")
        self.assertIn("log.fetchedAt = Date.now();", html[start:start + 200])
        self.assertIn("if (Date.now() - log.fetchedAt >= LOG_POLL_MS) refreshLog();", html)


LIVE_PACING_DETAILS = [
    "hold proxy: proxy uncached pacing: 34,977 uncached this minute (store), 0 pending launch(es) at 40,000 each, cap 320,000",
    "hold proxy: proxy uncached pacing: 120,445 uncached this minute (store), 0 pending launch(es) at 40,000 each, cap 320,000",
    "hold proxy: proxy uncached pacing: 242,985 uncached this minute (store), 0 pending launch(es) at 40,000 each, cap 320,000",
    "hold proxy: proxy uncached pacing: 184,363 uncached this minute (store), 0 pending launch(es) at 40,000 each, cap 320,000",
    "hold proxy: proxy uncached pacing: 165,352 uncached this minute (store), 0 pending launch(es) at 40,000 each, cap 320,000",
    "hold proxy: proxy uncached pacing: 32,300 uncached this minute (proxy), 0 pending launch(es) at 40,000 each, cap 320,000",
    "hold proxy: proxy uncached pacing: 207,010 uncached this minute (proxy), 0 pending launch(es) at 40,000 each, cap 320,000",
    "hold proxy: proxy uncached pacing: 98,533 uncached this minute (proxy), 0 pending launch(es) at 40,000 each, cap 320,000",
]
# The live queue held eight distinct strings; the owner's report named seventeen. The other nine follow the same template.
SYNTHESIZED_PACING_DETAILS = [
    f"hold proxy: proxy uncached pacing: {current:,} uncached this minute ({source}), 0 pending launch(es) at 40,000 each, cap 320,000"
    for current, source in (
        (11_204, "store"), (18_870, "proxy"), (26_417, "store"), (41_992, "proxy"), (57_618, "store"),
        (66_301, "proxy"), (79_455, "store"), (88_016, "proxy"), (101_739, "store"),
    )
]


class HeadlineCleanTest(unittest.TestCase):
    def verdict_with(self, details, ssh_in_use=8, **kwargs):
        return dash.verdict(
            five_minutes(total=4_100_000), UNTIL, LIMITS_NOW, IDLE_LIVE, kimi_jobs_waiting=0, budget_429s={},
            laptop=CALM, ds_jobs_ready=len(details), slow=None, ds_holds=details, ssh_in_use=ssh_in_use, **kwargs,
        )

    def test_seventeen_live_pacing_strings_make_one_clause_with_count_seventeen(self) -> None:
        details = LIVE_PACING_DETAILS + SYNTHESIZED_PACING_DETAILS
        self.assertEqual(len(set(details)), 17)
        rows = dash.hold_summary(details, 8)
        self.assertEqual([(group, count) for group, _key, count, _clause in rows], [("pacing", 17)])
        result = self.verdict_with(details)
        self.assertEqual(result["key"], "held_pacing")
        self.assertEqual(result["chip"], "Launch pacing")
        self.assertEqual(
            result["text"],
            "Launch pacing: 17 DeepSeek jobs waiting to launch under Morph's uncached-input limit "
            "(243k of 320k this minute).",
        )
        self.assertIsNone(result["secondary"])
        self.assertNotIn("proxy", result["text"].lower())

    def test_mixed_holds_put_the_biggest_first_and_never_exceed_three_clauses(self) -> None:
        details = (
            LIVE_PACING_DETAILS + SYNTHESIZED_PACING_DETAILS
            + ["hold tests: cpu 90% busy >= 80%"] * 4
            + ["hold ssh: slots full"] * 3
            + ["hold pi-deepseek-flash: workspace busy"] * 2
            + ["hold pi-deepseek-flash: proxy total waiting 3 > 2"]
            + ["hold pi-deepseek-flash: tier cap full"]
        )
        result = self.verdict_with(details)
        self.assertTrue(result["text"].startswith("Launch pacing: 17 DeepSeek jobs"))
        self.assertTrue(result["secondary"].startswith("Also: 4 test jobs waiting for laptop headroom"))
        clauses = [result["text"]] + result["secondary"][len("Also: "):].rstrip(".").split("; ")
        self.assertEqual(len(clauses), 3, clauses)
        self.assertNotIn("workspace", " ".join(clauses))
        self.assertNotIn("Worker pool", result["secondary"])

    def test_proxy_held_jobs_say_waiting_at_the_proxy_and_launch_held_jobs_do_not(self) -> None:
        proxy = self.verdict_with(["hold pi-deepseek-flash: proxy total waiting 9 > 8"])
        self.assertIn("requests waiting at the proxy", proxy["text"])
        self.assertEqual(proxy["chip"], "Proxy waiting")
        launch = self.verdict_with(LIVE_PACING_DETAILS[:1])
        self.assertIn("waiting to launch", launch["text"])
        self.assertNotIn("at the proxy", launch["text"])
        self.assertEqual(launch["chip"], "Launch pacing")

    def test_the_pacing_clause_names_the_largest_current_figure_across_the_group(self) -> None:
        result = self.verdict_with(LIVE_PACING_DETAILS[:3])
        self.assertIn("(243k of 320k this minute)", result["text"])

    def test_the_room_line_is_still_one_line(self) -> None:
        room = dash.room(five_minutes(total=1_000_000, ds_uncached=100_000), UNTIL, LIMITS_NOW,
                         active_ds_workers=10, laptop=CALM)
        self.assertNotIn("\n", room["text"])


