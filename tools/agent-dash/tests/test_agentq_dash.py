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
    kind TEXT NOT NULL, verdict TEXT, submitted_at REAL NOT NULL, started_at REAL, finished_at REAL
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
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    ("a" * 12, "queued", "normal", "pi-deepseek-flash", "read", None, NOW - 30, None, None),
                    ("b" * 12, "running", "urgent", "pi-kimi", "review", None, NOW - 90, NOW - 80, None),
                    ("c" * 12, "done", "normal", "pi-deepseek-flash", "write", "PASS", NOW - 600, NOW - 580, NOW - 500),
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
            "Billed use, % of each Morph limit, per minute",
            "Workers",
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
        for key in ("live", "limits", "minutes", "jobs", "workers", "caps", "errors", "r429_60m", "window"):
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
        self.assertIn("15000", dash.PAGE_HTML)


if __name__ == "__main__":
    unittest.main()
