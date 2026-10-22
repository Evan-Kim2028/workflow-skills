"""Performance budgets gate: seed-shaped latency, CPU, polls, scaling.

Runs ONLY with ADASH_PERF=1 (otherwise skipped in under a second), so
the default fast suite never pays for it; scripts/gate.sh selects this
module (dotted) whenever a change touches dash/store/*, dash/server.py,
dash/sources/*, dash/phases.py, dash/pricing.py, dash/taskmatch.py or
dash/state.py. Total wall time stays under 90 s.

Seeds come from scripts/perf_seed.py (built-in profile matching the
live shape: 8.4k runs / 157k usage rows at 1x). Budgets live in
perf_budgets.json with measured baselines and the 2x headroom rule; a
failure names the endpoint, the budget, the measurement, and the
profile hint.

On cold scaling, a note on honesty: a cold open must touch every row
once, so cold endpoints scale linearly with data by construction
(measured 3.7-5.7x at 5x). The budgets file records per-scale cold
caps with that written reason. The sublinear guarantee this gate
enforces is the incremental one: a fixed-size write refolds only its
runs, so its cost is independent of total data (asserted under 2.5x).
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault(
    "AGENT_DASH_WORKSTREAMS", str(ROOT / "fixtures" / "bundles")
)

RUN_PERF = os.environ.get("ADASH_PERF") == "1"


def _budgets():
    return json.loads((ROOT / "perf_budgets.json").read_text(encoding="utf-8"))


def _hint(endpoint):
    try:
        from dash.doctor import _ENDPOINT_HINT

        return _ENDPOINT_HINT.get(endpoint, "docs/perf.md")
    except Exception:
        return "docs/perf.md"


@unittest.skipUnless(RUN_PERF, "perf gate: set ADASH_PERF=1 to run (skipped in the fast suite)")
class PerfBudgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from scripts.perf_seed import build

        cls.tmp = tempfile.TemporaryDirectory()
        cls.seed1x = Path(cls.tmp.name) / "seed1x.sqlite"
        cls.seed5x = Path(cls.tmp.name) / "seed5x.sqlite"
        build(cls.seed1x, 1, None)
        build(cls.seed5x, 5, None)
        cls.budgets = _budgets()
        cls.servers = {}

    @classmethod
    def tearDownClass(cls):
        for server, thread in cls.servers.values():
            try:
                server.stop.set()
                server.shutdown()
                thread.join(3)
                server.server_close()
            except Exception:
                pass
        cls.tmp.cleanup()

    @classmethod
    def _serve(cls, seed):
        from dash.server import DashServer
        from dash.sources import Registry
        from dash.store import Store

        key = str(seed)
        if key not in cls.servers:
            store = Store(seed)
            server = DashServer(store, Registry([]), "127.0.0.1", 0, cfg={"state_dir": cls.tmp.name})
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            thread.start()
            cls.servers[key] = (server, thread)
        return cls.servers[key][0]

    @classmethod
    def _get(cls, server, path, etag=None):
        url = f"http://127.0.0.1:{server.server_address[1]}{path}"
        headers = {"Accept-Encoding": "gzip"}
        if etag:
            headers["If-None-Match"] = etag
        request = urllib.request.Request(url, headers=headers)
        start = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                status = response.status
                out_etag = response.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = exc.code
            out_etag = exc.headers.get("ETag")
        return (time.monotonic() - start) * 1000.0, status, body, out_etag

    def _paths(self, now):
        since = now - 24 * 3600
        return {
            "/api/state": "/api/state",
            "/api/snapshot": f"/api/snapshot?ws=all&since={since}&usage_group=model",
            "/api/workstreams": f"/api/workstreams?since={since}",
            "/api/runs": f"/api/runs?since={since}&limit=5000",
            "/api/usage": f"/api/usage?group=model&bucket=hour&since={since}",
            "/api/concurrency": f"/api/concurrency?since={since}&step=60",
            "/api/project/phases?scope=24h": "/api/project/phases?scope=24h",
            "/api/health": "/api/health",
            "/api/plan?ws=WS1": "/api/plan?ws=WS1",
            "/api/doctor": "/api/doctor",
        }

    def _budget_key(self, label):
        return {
            "/api/state": "state",
            "/api/snapshot": "snapshot",
            "/api/workstreams": "workstreams",
            "/api/runs": "runs",
            "/api/usage": "usage",
            "/api/concurrency": "concurrency",
            "/api/project/phases?scope=24h": "project/phases",
            "/api/health": "health",
            "/api/plan?ws=WS1": "plan",
            "/api/doctor": "doctor",
        }[label]

    def _assert_endpoints(self, seed, cold_key):
        server = self._serve(seed)
        now = time.time()
        for label, path in self._paths(now).items():
            key = self._budget_key(label)
            entry = self.budgets["endpoints"][key]
            cold_ms, status, _, etag = self._get(server, path)
            self.assertEqual(status, 200, f"{label} cold status {status}")
            self.assertLessEqual(
                cold_ms, entry[cold_key],
                f"{label} cold {cold_ms:.0f}ms over budget {entry[cold_key]}ms"
                f" (hint: {_hint(label.split('?')[0])})",
            )
            # Warm is best-of-3 or a 304: a minute-quantum rollover between
            # the two GETs legitimately recomputes once (identity carries
            # int(now // 60)); the property is that a settled entry is fast.
            warm_ms, saw304 = None, False
            for _ in range(3):
                attempt_ms, status, _, fresh_etag = self._get(server, path, etag=etag)
                self.assertIn(status, (200, 304), f"{label} warm status {status}")
                if status == 304:
                    saw304 = True
                    warm_ms = 0.0 if warm_ms is None else min(warm_ms, 0.0)
                else:
                    warm_ms = attempt_ms if warm_ms is None else min(warm_ms, attempt_ms)
                    etag = fresh_etag or etag
                if saw304 or (warm_ms is not None and warm_ms <= entry["warm_ms"]):
                    break
            self.assertLessEqual(
                warm_ms, entry["warm_ms"],
                f"{label} warm {warm_ms:.0f}ms over budget {entry['warm_ms']}ms"
                f" (hint: {_hint(label.split('?')[0])})",
            )
            if etag:
                _, status304, _, _ = self._get(server, path, etag=etag)
                self.assertIn(
                    status304, (200, 304), f"{label} conditional status {status304}"
                )

    def test_cold_and_warm_1x(self):
        self._assert_endpoints(self.seed1x, "cold_ms")

    def test_cold_and_warm_5x(self):
        self._assert_endpoints(self.seed5x, "cold_5x_ms")

    def test_scaling_is_incremental(self):
        from dash.store import Store

        times = {}
        for name, seed in (("1x", self.seed1x), ("5x", self.seed5x)):
            store = Store(seed)
            try:
                store.snapshot(time.time(), "all")
                store.add_usage(
                    {
                        "ts": 1792456000.0,
                        "session_id": "seed-session-1",
                        "run_id": "seed-scaling-probe",
                        "model": "muse-spark-1.3-contributor",
                        "tool": "muse",
                        "message_id": "seed-scaling-m1",
                        "input": 100,
                        "output": 10,
                    }
                )
                start = time.monotonic()
                store._usage_by_run()
                times[name] = (time.monotonic() - start) * 1000.0
            finally:
                store.close()
        # Noise floor: both sides are sub-millisecond when incremental, so
        # the ratio divides by 1ms, not by timer noise. A linear refold
        # (620ms vs 3100ms) still fails both this and the absolute cap.
        ratio = times["5x"] / max(times["1x"], 1.0)
        self.assertLessEqual(
            times["5x"], 1000.0, f"5x incremental refold {times['5x']:.1f}ms over 1000ms"
        )
        self.assertLessEqual(
            ratio, self.budgets["scaling"]["incremental_max_ratio"],
            f"incremental refold scales {ratio:.1f}x (5x: {times['5x']:.1f}ms, 1x: {times['1x']:.1f}ms):"
            " a fixed-size write refolded linearly instead of incrementally",
        )

    def test_source_polls_under_budget(self):
        from dash.store import Store

        budget = self.budgets["poll_s"]
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                cfg = {
                    "roots": {
                        "claude_projects": tmp,
                        "claude_sessions": tmp,
                        "muse_sessions": tmp,
                        "grok_sessions": tmp,
                        "cursor_home": tmp,
                        "scratchpads": [],
                    }
                }
                import importlib

                slow = []
                polled = []
                for modname in ("claude", "cursor", "muse", "codex", "generic", "manifests", "hub", "process", "grok_cli", "dagster"):
                    module = importlib.import_module(f"dash.sources.{modname}")
                    poll = getattr(module, "poll", None)
                    if poll is None:
                        continue
                    polled.append(modname)
                    start = time.monotonic()
                    try:
                        poll(store, cfg, time.time())
                    except Exception:
                        pass
                    duration = time.monotonic() - start
                    if duration > budget:
                        slow.append(f"{modname} {duration:.2f}s")
                self.assertGreaterEqual(len(polled), 8, f"too few sources polled: {polled}")
                self.assertEqual(slow, [], f"source polls over the {budget}s budget: {slow}")
            finally:
                store.close()

    def test_churn_coalescing_under_budget(self):
        """Live-like churn: a writer thread lands usage on 5 live runs
        every 200 ms while two tabs poll snapshot+runs flat out for
        ~20 s. TTL coalescing must hold the recompute rate near one per
        ttl_s (not one per request), keep p95 under budget, and keep a
        hot hit ratio. Cold recompute p50s under churn stay under their
        caps (measured separately below)."""
        import random

        from dash.store import Store

        server = self._serve(self.seed1x)
        budgets = self.budgets["churn"]
        window_s = budgets["window_s"]
        store = Store(self.seed1x)
        try:
            live_ids = [
                row["run_id"]
                for row in store._conn.execute(
                    "SELECT run_id FROM runs WHERE status='running' LIMIT 5"
                ).fetchall()
            ]
            self.assertGreaterEqual(len(live_ids), 1, "seed has no live runs")
            stop = threading.Event()
            rng = random.Random(20261020)

            def churn():
                i = 0
                while not stop.wait(0.2):
                    i += 1
                    try:
                        store.add_usage(
                            {
                                "ts": 1792456000.0 + i,
                                "session_id": "seed-session-1",
                                "message_id": f"churn-{i}",
                                "run_id": rng.choice(live_ids),
                                "model": "claude-sonnet-4-5",
                                "tool": "muse",
                                "input": 100,
                                "output": 10,
                            }
                        )
                    except Exception:
                        pass

            latencies: list = []
            lat_lock = threading.Lock()

            def tab():
                now = time.time()
                since = now - 24 * 3600
                paths = [
                    f"/api/snapshot?ws=all&since={since}&usage_group=model",
                    f"/api/runs?since={since}&limit=5000",
                ]
                end = time.monotonic() + window_s
                while time.monotonic() < end and not stop.is_set():
                    for path in paths:
                        ms, status, _, _ = self._get(server, path)
                        if status == 200:
                            with lat_lock:
                                latencies.append(ms)

            def ttl_totals():
                import gzip

                _, _, health_body, _ = self._get(server, "/api/health")
                try:
                    health = json.loads(health_body)
                except (ValueError, UnicodeDecodeError):
                    health = json.loads(gzip.decompress(health_body))
                return health["perf"]["ttl"]

            writer = threading.Thread(target=churn, daemon=True)
            tabs = [threading.Thread(target=tab, daemon=True) for _ in range(2)]
            start_buckets = ttl_totals()
            t0 = time.monotonic()
            writer.start()
            for tab_thread in tabs:
                tab_thread.start()
            for tab_thread in tabs:
                tab_thread.join(window_s + 10.0)
            elapsed = time.monotonic() - t0
            stop.set()
            writer.join(5.0)
            end_buckets = ttl_totals()

            def delta(name, field):
                before = start_buckets.get(name, {}).get(field, 0)
                after = end_buckets.get(name, {}).get(field, 0)
                return after - before

            # One recompute per ttl_s per polled key, not one per
            # request: the two tabs share the snapshot and runs keys.
            for name in ("snapshot", "runs"):
                recomputes = delta(name, "recomputes")
                hits = delta(name, "hits")
                self.assertLessEqual(
                    recomputes, elapsed / budgets["recompute_min_interval_s"] + 2,
                    f"churn {name} recomputes {recomputes} in {elapsed:.0f}s:"
                    " TTL is not coalescing",
                )
                self.assertGreater(
                    hits / max(hits + recomputes, 1), budgets["hit_ratio_min"],
                    f"TTL hit ratio collapsed for {name} under churn",
                )
            self.assertGreater(len(latencies), 10, "churn tabs made no requests")
            latencies.sort()
            p95 = latencies[int(len(latencies) * 0.95)]
            self.assertLessEqual(
                p95, budgets["endpoint_p95_ms"],
                f"churn p95 {p95:.0f}ms over budget {budgets['endpoint_p95_ms']}ms",
            )
            cold = {}
            for label, path in (
                ("snapshot", f"/api/snapshot?ws=all&since={time.time() - 24 * 3600}&usage_group=model"),
                ("runs", f"/api/runs?since={time.time() - 24 * 3600}&limit=1000"),
                ("concurrency", f"/api/concurrency?since={time.time() - 24 * 3600}&step=60"),
                ("workstreams", f"/api/workstreams?since={time.time() - 24 * 3600}"),
            ):
                samples = []
                for _ in range(3):
                    server.respcache.invalidate_all()
                    ms, status, _, _ = self._get(server, path)
                    self.assertEqual(status, 200, f"{label} churn-cold status {status}")
                    samples.append(ms)
                samples.sort()
                cold[label] = samples[1]
                self.assertLessEqual(
                    cold[label], budgets[f"{label}_cold_p50_ms"],
                    f"churn cold {label} p50 {cold[label]:.0f}ms over budget"
                    f" {budgets[f'{label}_cold_p50_ms']}ms",
                )
        finally:
            store.close()

    def test_steady_cpu_under_budget(self):
        import resource

        server = self._serve(self.seed5x)
        now = time.time()
        since = now - 24 * 3600
        path = f"/api/snapshot?ws=all&since={since}&usage_group=model"
        etag = None
        cpu0 = resource.getrusage(resource.RUSAGE_SELF)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 20.0:
            _, _, _, etag = self._get(server, path, etag=etag) or (None, None, None, etag)
            time.sleep(2.0)
        cpu1 = resource.getrusage(resource.RUSAGE_SELF)
        wall = time.monotonic() - t0
        used = (cpu1.ru_utime + cpu1.ru_stime) - (cpu0.ru_utime + cpu0.ru_stime)
        pct = used / wall * 100.0
        self.assertLessEqual(
            pct, self.budgets["cpu_steady_pct"],
            f"steady CPU {pct:.1f}% over budget {self.budgets['cpu_steady_pct']}%"
            " (hint: docs/perf.md: view_seq, incremental fold)",
        )


if __name__ == "__main__":
    unittest.main()
