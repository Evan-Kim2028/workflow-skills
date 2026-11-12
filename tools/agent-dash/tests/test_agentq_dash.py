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
            "Share of each Morph-enforced limit, % of limit",
            "Active runs over time, with caps",
            "Tokens per 5 minutes, millions",
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


class EnforcedLimitsChartTest(unittest.TestCase):
    def test_main_billed_chart_plots_only_enforced_limits(self) -> None:
        html = dash.PAGE_HTML
        start = html.index('registerChart("c-billed"')
        block = html[start:html.index('registerChart("c-workers"')]
        self.assertIn("pct_total", block)
        self.assertIn("pct_ds_uncached", block)
        self.assertNotIn("pct_ds_output", block)
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
IDLE_LIVE = {"queued": {"urgent": 0, "normal": 0, "backlog": 0}}


def five_minutes(**fields) -> dict:
    """Minute rows for the five complete minutes before UNTIL, each with the same fields."""
    return {UNTIL - 60 * k: fields for k in range(1, 6)}


def verdict_for(raw, *, live=IDLE_LIVE, laptop=CALM, kimi_waiting=False, budget_429s=None,
                jobs_queued=0, ds_p50_5m_ms=None, ds_p50_base_ms=None):
    return dash.verdict(
        raw, UNTIL, LIMITS_NOW, live, kimi_waiting=kimi_waiting, budget_429s=budget_429s or {},
        laptop=laptop, jobs_queued=jobs_queued, ds_p50_5m_ms=ds_p50_5m_ms, ds_p50_base_ms=ds_p50_base_ms,
    )


class LimitedByTest(unittest.TestCase):
    def test_morph_limit_when_an_enforced_budget_averages_90_percent(self) -> None:
        result = verdict_for(five_minutes(total=1_000_000, ds_uncached=380_000))
        self.assertEqual(result["key"], "morph_limit")
        self.assertEqual(result["chip"], "Morph limit")
        self.assertIn("DeepSeek uncached input averaged 95% of its limit over the last 5 minutes", result["text"])

    def test_a_single_peak_minute_with_a_low_average_is_not_morph_limit(self) -> None:
        raw = five_minutes(total=1_000_000, ds_uncached=40_000)
        raw[UNTIL - 120] = {"total": 1_000_000, "ds_uncached": 507_000}
        result = verdict_for(raw)
        self.assertNotEqual(result["key"], "morph_limit")
        self.assertNotIn("peak minute 127%", result["text"])

    def test_peak_minute_is_context_in_the_sentence_when_morph_limit_holds_on_average(self) -> None:
        raw = five_minutes(total=1_000_000, ds_uncached=370_000)
        raw[UNTIL - 120] = {"total": 1_000_000, "ds_uncached": 507_000}
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

    def test_kimi_uncached_counts_only_while_kimi_jobs_wait(self) -> None:
        raw = five_minutes(total=0, kimi_uncached=24_290)
        self.assertNotEqual(verdict_for(raw, kimi_waiting=False)["key"], "morph_limit")
        waiting = verdict_for(raw, kimi_waiting=True)
        self.assertEqual(waiting["key"], "morph_limit")
        self.assertIn("Kimi uncached averaged 100%", waiting["text"])

    def test_morph_slow_when_deepseek_p50_doubles_and_proxy_queue_is_empty(self) -> None:
        result = verdict_for(five_minutes(total=1_000_000), ds_p50_5m_ms=12_000, ds_p50_base_ms=5_000)
        self.assertEqual(result["key"], "morph_slow")
        self.assertEqual(result["chip"], "Morph slow")

    def test_morph_slow_needs_an_empty_proxy_queue(self) -> None:
        queued = {"queued": {"urgent": 0, "normal": 3, "backlog": 0}}
        result = verdict_for(five_minutes(total=1_000_000), live=queued, ds_p50_5m_ms=12_000, ds_p50_base_ms=5_000)
        self.assertNotEqual(result["key"], "morph_slow")

    def test_laptop_when_load_per_cpu_is_over_1_5(self) -> None:
        result = verdict_for(five_minutes(total=1_000_000), laptop=HOT)
        self.assertEqual(result["key"], "laptop")
        self.assertIn("load 44.0 on 15 CPUs", result["text"])

    def test_laptop_when_memory_free_is_under_25_percent(self) -> None:
        low_memory = dict(CALM, memory_free_pct=20.0)
        self.assertEqual(verdict_for(five_minutes(total=1_000_000), laptop=low_memory)["key"], "laptop")

    def test_demand_when_pool_is_idle_and_nothing_waits(self) -> None:
        result = verdict_for(five_minutes(total=1_000_000))
        self.assertEqual(result["key"], "demand")
        self.assertEqual(result["chip"], "Demand")

    def test_demand_needs_no_ready_job(self) -> None:
        self.assertEqual(verdict_for(five_minutes(total=1_000_000), jobs_queued=2)["key"], "near_capacity")

    def test_near_capacity_otherwise(self) -> None:
        result = verdict_for(five_minutes(total=8_800_000))
        self.assertEqual(result["key"], "near_capacity")
        self.assertEqual(result["chip"], "Near capacity")


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
        self.assertIn(summary["verdict"]["key"], {"morph_limit", "morph_slow", "laptop", "demand", "near_capacity"})
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
