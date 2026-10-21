"""Self-monitoring: perfmon rings, the /api/health perf block, doctor perf checks."""

import json
import logging
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

from dash import perfmon
from dash.doctor import collect_problems
from dash.server import _health_body, _record_http
from dash.singleflight import SingleFlight
from dash.store import Store

BUDGETS = {
    "endpoints": {
        "snapshot": {"warm_ms": 300},
        "health": {"warm_ms": 100},
    },
    "lock_wait_p95_ms": 200,
    "source_lag_polls": 3,
    "db_warn_mb": 500,
    "db_crit_mb": 1000,
}


def tool_root_with_budgets(tmp):
    root = Path(tmp) / "toolroot"
    root.mkdir()
    (root / "perf_budgets.json").write_text(json.dumps(BUDGETS), encoding="utf-8")
    return root


def health_with(perf):
    return {"ok": True, "sources": [], "perf": perf}


class PerfMonitorTest(unittest.TestCase):
    def setUp(self):
        self.mon = perfmon.PerfMonitor()

    def test_endpoint_window_summary(self):
        for ms in (10, 20, 30, 40, 100):
            self.mon.record_request("/api/snapshot", ms)
        snap = self.mon.snapshot()
        summary = snap["endpoints"]["/api/snapshot"]
        self.assertEqual(summary["count"], 5)
        self.assertEqual(summary["p50"], 30)
        self.assertEqual(summary["max"], 100)
        self.assertGreaterEqual(summary["p95"], 40)

    def test_window_expiry(self):
        old = time.time() - 700.0
        self.mon._endpoints.setdefault("/api/x", perfmon._Ring()).add(5.0, now=old)
        self.mon.record_request("/api/x", 7.0)
        summary = self.mon.snapshot()["endpoints"]["/api/x"]
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["p50"], 7.0)

    def test_lock_records_outermost_only(self):
        lock = perfmon.TimedRLock(self.mon)
        with lock:
            with lock:
                pass
        snap = self.mon.snapshot()
        self.assertEqual(snap["lock"]["acquisitions"], 1)
        self.assertIsNotNone(snap["lock"]["wait_p50_ms"])
        self.assertIsNotNone(snap["lock"]["hold_p50_ms"])

    def test_lock_still_mutual(self):
        lock = perfmon.TimedRLock(self.mon)
        held = []
        with lock:
            held.append(lock.acquire(blocking=False))
        self.assertEqual(held, [True])

    def test_poll_lag_breach_counts_consecutive(self):
        base = time.time()
        with mock.patch.object(perfmon.time, "time") as clock:
            clock.side_effect = [base, base + 10.0, base + 10.0 + 10.0]
            self.mon.record_poll("muse", 0.2, 2.0)
            self.mon.record_poll("muse", 0.2, 2.0)
            self.mon.record_poll("muse", 0.2, 2.0)
        summary = self.mon.snapshot()["sources"]["muse"]
        self.assertEqual(summary["polls"], 3)
        self.assertEqual(summary["lag_breaches"], 2)

    def test_singleflight_stats(self):
        flight = SingleFlight()
        flight.do(("a", 1), lambda: 1)
        stats = flight.stats()
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(stats["shared"], 0)
        self.assertEqual(stats["hit_ratio"], 0.0)

    def test_snapshot_shape(self):
        snap = self.mon.snapshot()
        for key in ("endpoints", "lock", "sources", "singleflight", "caches", "threads", "cpu"):
            self.assertIn(key, snap)
        self.assertIn("pct_5m", snap["cpu"])

    def test_recording_never_raises(self):
        self.mon.record_request(None, float("nan"))
        self.mon.record_lock(float("nan"), float("nan"))
        self.mon.record_poll(None, float("nan"))
        self.mon.record_cache("x", None)
        self.mon.snapshot()


class HealthBlockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        perfmon.get().reset()
        self.addCleanup(perfmon.get().reset)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _server(self):
        server = SimpleNamespace(
            registry=SimpleNamespace(
                health=lambda now, cfg, **kw: {"ok": True, "sources": []},
                source_states=lambda now: [],
            ),
            store=self.store,
            cfg={},
            flight=SingleFlight(),
            build={"build_id": "test", "started_at": 1.0},
        )
        server.disconnects_1h = lambda: 0
        return server

    def test_perf_block_is_additive(self):
        perfmon.get().record_request("/api/snapshot", 12.0)
        body = _health_body(self._server(), time.time(), lambda now: {"summary": {}})
        for key in ("ok", "sources", "pricing", "build_id", "started_at", "state_hash"):
            self.assertIn(key, body)
        perf = body["perf"]
        self.assertEqual(perf["endpoints"]["/api/snapshot"]["count"], 1)
        for key in ("lock", "sources", "singleflight", "caches", "threads", "cpu"):
            self.assertIn(key, perf)

    def test_slow_request_logs_once_with_ws_only(self):
        from urllib.parse import urlparse

        with self.assertLogs("agent_dash.server", level="WARNING") as logs:
            _record_http(
                self._server(), urlparse("/api/snapshot?ws=WS1&since=1&usage_group=model"), 2500.0
            )
        self.assertEqual(len(logs.records), 1)
        line = logs.records[0].getMessage()
        self.assertIn("/api/snapshot", line)
        self.assertIn("WS1", line)
        self.assertNotIn("usage_group", line)
        with self.assertNoLogs("agent_dash.server", level="WARNING"):
            _record_http(self._server(), urlparse("/api/snapshot?ws=all"), 12.0)


class DoctorPerfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = tool_root_with_budgets(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _collect(self, perf, store=None):
        cfg = {"doctor": {}}
        return collect_problems(
            store, cfg, time.time(), health=health_with(perf), tool_root=self.root
        )

    def test_quiet_perf_is_clean(self):
        result = self._collect(
            {
                "endpoints": {"/api/snapshot": {"count": 9, "p50": 8, "p95": 20, "max": 40}},
                "lock": {"wait_p95_ms": 3.0},
                "sources": {"muse": {"lag_breaches": 0, "lag_s": 1.0}},
                "cpu": {"pct_5m": 12.0, "pct_1m": 10.0},
            }
        )
        codes = [item["code"] for item in result["problems"]]
        for code in ("server-cpu-bound", "slow-endpoint", "lock-contention", "source-lagging"):
            self.assertNotIn(code, codes)

    def test_cpu_warn_then_critical(self):
        warm = self._collect({"cpu": {"pct_5m": 85.0, "pct_1m": 40.0}})
        hit = [item for item in warm["problems"] if item["code"] == "server-cpu-bound"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["severity"], "warn")
        hot = self._collect({"cpu": {"pct_5m": 92.0, "pct_1m": 95.0}})
        hit = [item for item in hot["problems"] if item["code"] == "server-cpu-bound"]
        self.assertEqual(hit[0]["severity"], "critical")

    def test_slow_endpoint_names_budget_measurement_hint(self):
        result = self._collect(
            {"endpoints": {"/api/snapshot": {"count": 30, "p50": 200, "p95": 900, "max": 1200}}}
        )
        hit = [item for item in result["problems"] if item["code"] == "slow-endpoint"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["severity"], "critical")
        for token in ("/api/snapshot", "300", "900", "docs/perf.md"):
            self.assertIn(token, hit[0]["detail"])

    def test_lock_contention_threshold(self):
        result = self._collect({"lock": {"wait_p95_ms": 350.0}})
        hit = [item for item in result["problems"] if item["code"] == "lock-contention"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["severity"], "warn")
        self.assertIn("200", hit[0]["detail"])

    def test_source_lagging_threshold(self):
        result = self._collect(
            {"sources": {"cursor": {"lag_breaches": 4, "lag_s": 30.0}}}
        )
        hit = [item for item in result["problems"] if item["code"] == "source-lagging"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["severity"], "warn")
        self.assertIn("cursor", hit[0]["title"])

    def test_db_growth_small_is_clean_and_huge_is_critical(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                result = collect_problems(
                    store, {"doctor": {}}, time.time(),
                    health=health_with({}), tool_root=self.root,
                )
                codes = [item["code"] for item in result["problems"]]
                self.assertNotIn("db-growth", codes)
            finally:
                store.close()

    def _db_growth_with(self, size_mb, free_gb=100.0):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                store.add_usage(
                    {
                        "ts": time.time(), "session_id": "s", "run_id": "muse:s1:x",
                        "model": "m", "tool": "muse", "message_id": "m-1",
                        "input": 1000, "output": 0, "cache_read": 0, "cache_write": 0,
                        "reasoning": 0,
                    }
                )
                with mock.patch("dash.doctor._db_size_mb", return_value=size_mb), \
                        mock.patch("dash.doctor._free_disk_gb", return_value=free_gb):
                    result = self._collect({}, store=store)
            finally:
                store.close()
        hits = [item for item in result["problems"] if item["code"] == "db-growth"]
        self.assertEqual(len(hits), 1)
        return hits[0]

    def test_db_growth_soft_budget_and_projection_are_warn(self):
        hit = self._db_growth_with(305.0)
        self.assertEqual(hit["severity"], "warn")
        self.assertIn("305 MB", hit["detail"])
        self.assertIn("compact old usage rows hourly", hit["detail"])
        self.assertEqual(hit["fix_command"], "python3 agent_dash.py db rollup --dry-run")
        self.assertNotIn("critical", hit["detail"])

    def test_db_growth_over_hard_cap_is_critical(self):
        hit = self._db_growth_with(2150.0)
        self.assertEqual(hit["severity"], "critical")
        self.assertIn("hard cap", hit["detail"])
        self.assertEqual(hit["fix_command"], "python3 agent_dash.py db rollup --dry-run")

    def test_db_growth_low_free_disk_is_critical(self):
        hit = self._db_growth_with(305.0, free_gb=3.0)
        self.assertEqual(hit["severity"], "critical")
        self.assertIn("3.0 GB free", hit["detail"])

    def test_db_growth_hard_cap_reads_budget_override(self):
        budgets = dict(BUDGETS, db_hard_cap_mb=4000)
        (self.root / "perf_budgets.json").write_text(json.dumps(budgets), encoding="utf-8")
        hit = self._db_growth_with(2150.0)
        self.assertEqual(hit["severity"], "warn")

    def test_missing_budgets_skips(self):
        cfg = {"doctor": {}}
        result = collect_problems(
            None, cfg, time.time(),
            health=health_with(
                {"endpoints": {"/api/snapshot": {"count": 3, "p50": 1, "p95": 99999, "max": 99999}}}
            ),
            tool_root=Path(self.tmp.name) / "empty-root",
        )
        codes = [item["code"] for item in result["problems"]]
        self.assertNotIn("slow-endpoint", codes)


if __name__ == "__main__":
    unittest.main()
