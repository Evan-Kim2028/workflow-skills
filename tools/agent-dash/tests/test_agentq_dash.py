import calendar
import hashlib
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
    label TEXT NOT NULL DEFAULT ''
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
            "Total pool and DeepSeek output, % of limit",
            "Active runs over time, with caps",
            "Tokens per minute, millions",
            "Runs",
            "Top consumers today",
            "Upstream 429s per minute",
            "Wait p95 per minute",
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

    def test_token_mix_splits_each_minute_into_three_series(self) -> None:
        by_minute = {row["minute"]: row for row in self.summary["token_mix"]}
        row = by_minute[minute_at(60)]
        self.assertEqual(row["cache_read"], 1000)
        self.assertEqual(row["uncached"], 100 + 2429)
        self.assertEqual(row["output"], 7000 + 100)

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


if __name__ == "__main__":
    unittest.main()


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
