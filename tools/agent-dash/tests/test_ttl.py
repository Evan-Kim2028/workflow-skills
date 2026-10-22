"""TTL coalescing (stale-while-revalidate) for the heavy read endpoints.

Spins the real server and drives the freshness contract end to end:
same body and ETag within the window (with the true generated_at and
an age header for debugging), recompute after the window or an
explicit invalidation, stale-while-revalidate serving during a
recompute, the hard 5 s max staleness, one compute under thread
fan-in, POST user actions showing immediately, the longer pinned
window TTL, and response_ttl_s=0 disabling the cache (legacy path,
byte-identical). Plus the fragment-cache property: cached run views
(and their serialisation) equal fresh computation, fixed seed.
"""

import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import http.client
import json
import random
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_dash import default_cfg  # noqa: E402
from dash import respcache as respcache_mod  # noqa: E402
from dash.server import DashServer  # noqa: E402
from dash.sources import Registry  # noqa: E402
from dash.store import Store  # noqa: E402


def _seed_runs(store, now, count=40, seed=7):
    rng = random.Random(seed)
    sources = ["muse", "cursor", "codex", "claude", "grok", "dagster"]
    statuses = ["done", "done", "done", "running", "queued", "killed", "unknown"]
    for i in range(count):
        source = rng.choice(sources)
        status = rng.choice(statuses)
        started = now - rng.randint(60, 200000)
        ended = None if status in ("running", "queued") else started + rng.randint(10, 3600)
        row = {
            "run_id": f"{source}:ttl:{i}",
            "source": source,
            "label": f"ws{(i % 4) + 1}-{i} ünïcodé \U0001f600",
            "session_id": f"sess-{i % 6}",
            "status": status,
            "started_at": started,
            "ended_at": ended,
            "model": rng.choice(["claude-sonnet-4-5", "grok-4-7", None]),
            "verdict": rng.choice(["PASS", "PARTIAL", None]),
        }
        if rng.random() < 0.3:
            row["pid"] = 1000 + i
        store.upsert_run(row)
        if rng.random() < 0.7:
            store.add_usage(
                {
                    "ts": (ended or now) - 5,
                    "session_id": row["session_id"],
                    "message_id": f"m-{i}",
                    "run_id": row["run_id"],
                    "model": row.get("model") or "unknown",
                    "tool": source,
                    "input": rng.randint(0, 10000),
                    "output": rng.randint(0, 2000),
                }
            )


class TtlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.now = time.time()
        _seed_runs(self.store, self.now)
        self.cfg = default_cfg()
        self.server = DashServer(
            self.store, Registry([]), "127.0.0.1", 0, cfg=self.cfg
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.stop.set()
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def _get(self, path, tag=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            headers = {}
            if tag is not None:
                headers["If-None-Match"] = tag
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_same_body_and_etag_within_window(self):
        status, heads, first = self._get("/api/snapshot?ws=all")
        self.assertEqual(status, 200)
        tag = heads.get("ETag")
        self.assertTrue(tag, "snapshot has no ETag")
        body = json.loads(first)
        self.assertIn("generated_at", body, "TTL body carries the true generated_at")
        self.assertIn("X-AgentDash-Age-Ms", heads, "TTL serve carries an age header")
        status, heads2, second = self._get("/api/snapshot?ws=all", tag)
        self.assertEqual(status, 304, "same generation 304s within the window")
        self.assertEqual(second, b"")
        status, heads3, third = self._get("/api/snapshot?ws=all")
        self.assertEqual(status, 200)
        self.assertEqual(heads3.get("ETag"), tag, "ETag stays stable until recompute")
        volatile = {"now", "seq", "data_seq", "view_seq", "last_data_ts"}
        first_body = {key: value for key, value in json.loads(first).items() if key not in volatile}
        third_body = {key: value for key, value in json.loads(third).items() if key not in volatile}
        self.assertEqual(third_body, first_body, "sums stay frozen until recompute")

    def test_write_then_recompute_after_invalidate(self):
        status, heads, first = self._get("/api/runs?limit=100")
        self.assertEqual(status, 200)
        tag = heads.get("ETag")
        self.assertTrue(tag)
        self.store.upsert_run(
            {
                "run_id": "muse:ttl:new",
                "source": "muse",
                "label": "ws1-new-run",
                "session_id": "sess-new",
                "status": "done",
                "started_at": self.now - 50,
                "ended_at": self.now - 10,
            }
        )
        self.store.add_usage(
            {
                "ts": self.now - 5,
                "session_id": "sess-new",
                "message_id": "m-new",
                "run_id": "muse:ttl:new",
                "model": "claude-sonnet-4-5",
                "tool": "muse",
                "input": 100,
                "output": 10,
            }
        )
        status, _, empty = self._get("/api/runs?limit=100", tag)
        self.assertEqual(status, 304, "writes within the TTL keep the stable generation")
        self.assertEqual(empty, b"")
        self.server.respcache.invalidate_all()
        status, heads2, second = self._get("/api/runs?limit=100", tag)
        self.assertEqual(status, 200, "invalidation recomputes")
        self.assertNotEqual(heads2.get("ETag"), tag)
        labels = [row["label"] for row in json.loads(second)["runs"]]
        self.assertIn("ws1-new-run", labels, "the write shows after invalidation")

    def test_ttl_expiry_recomputes(self):
        self.cfg["response_ttl_s"] = 0.05
        status, heads, _ = self._get("/api/concurrency?since=1")
        self.assertEqual(status, 200)
        tag = heads.get("ETag")
        time.sleep(0.15)
        status, heads2, _ = self._get("/api/concurrency?since=1", tag)
        self.assertEqual(status, 200, "an expired generation recomputes")
        self.assertNotEqual(heads2.get("ETag"), tag)

    def test_zero_disables(self):
        self.cfg["response_ttl_s"] = 0
        status, heads, first = self._get("/api/snapshot?ws=all")
        self.assertEqual(status, 200)
        body = json.loads(first)
        self.assertNotIn("generated_at", body, "legacy path adds no freshness fields")
        self.assertNotIn("X-Agentdash-Age-Ms", heads)
        tag = heads.get("ETag")
        self.store.upsert_run(
            {
                "run_id": "muse:ttl:zero",
                "source": "muse",
                "label": "ws1-zero-run",
                "session_id": "sess-zero",
                "status": "done",
                "started_at": self.now - 50,
                "ended_at": self.now - 10,
            }
        )
        status, heads2, second = self._get("/api/snapshot?ws=all", tag)
        self.assertEqual(status, 200, "ttl=0 retags on every data write")
        self.assertNotEqual(second, first)

    def test_explicit_window_keeps_longer_ttl(self):
        self.assertEqual(
            respcache_mod.ttl_for(
                {"response_ttl_s": 0.05}, "snapshot",
                {"since": self.now - 100, "until": self.now},
            ),
            10.0,
            "explicit from/to pins the 10 s window TTL",
        )
        self.cfg["response_ttl_s"] = 0.05
        path = f"/api/snapshot?ws=all&since={int(self.now - 100)}&until={int(self.now)}"
        status, heads, _ = self._get(path)
        self.assertEqual(status, 200)
        tag = heads.get("ETag")
        self.store.upsert_run(
            {
                "run_id": "muse:ttl:pinned",
                "source": "muse",
                "label": "ws1-pinned-run",
                "session_id": "sess-pinned",
                "status": "done",
                "started_at": self.now - 50,
                "ended_at": self.now - 10,
            }
        )
        time.sleep(0.15)
        status, _, empty = self._get(path, tag)
        self.assertEqual(status, 304, "a pinned window outlives the default TTL")
        self.assertEqual(empty, b"")

    def test_swr_serves_stale_during_recompute(self):
        cache = respcache_mod.ResponseCache()
        calls = []
        release = threading.Event()

        def slow():
            calls.append(1)
            release.wait(5.0)
            return {"n": len(calls)}

        payload, etag, _, kind = cache.get_or_compute(("k",), "snapshot", 0.05, slow)
        self.assertEqual(kind, "fresh")
        first_tag = etag or cache.cached_etag(("k",))
        time.sleep(0.1)
        bg = threading.Thread(
            target=lambda: cache.get_or_compute(("k",), "snapshot", 0.05, slow)
        )
        bg.start()
        time.sleep(0.15)
        started = time.perf_counter()
        payload2, etag2, _, kind2 = cache.get_or_compute(("k",), "snapshot", 0.05, slow)
        elapsed = time.perf_counter() - started
        release.set()
        bg.join(5.0)
        self.assertEqual(kind2, "stale", "readers get the previous result, not a wait")
        self.assertLess(elapsed, 1.0, "stale serve never blocks on the recompute")
        self.assertEqual(payload2, payload)
        self.assertEqual(cache.cached_etag(("k",)), etag2 or first_tag)
        self.assertEqual(len(calls), 2, "one background recompute, not one per reader")

    def test_many_threads_one_compute(self):
        cache = respcache_mod.ResponseCache()
        calls = []
        lock = threading.Lock()

        def compute():
            with lock:
                calls.append(1)
            time.sleep(0.2)
            return {"v": 1}

        results, errors = [], []

        def worker():
            try:
                results.append(cache.get_or_compute(("c",), "runs", 30.0, compute))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10.0)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 16)
        self.assertEqual(len(calls), 1, "16 threads, one compute")
        self.assertTrue(all(item[0]["v"] == 1 for item in results))

    def test_hard_max_staleness_waits(self):
        cache = respcache_mod.ResponseCache()
        payload, _, _, _ = cache.get_or_compute(("s",), "usage", 60.0, lambda: {"v": 1})
        self.assertEqual(payload["v"], 1, "compute result served (plus generated_at)")
        with cache._lock:
            entry = cache._entries[("s",)]
            entry.generated_at -= 10.0
        release = threading.Event()

        def slow():
            release.wait(5.0)
            return {"v": 2}

        bg: list = []
        worker = threading.Thread(
            target=lambda: bg.append(cache.get_or_compute(("s",), "usage", 0.05, slow))
        )
        worker.start()
        time.sleep(0.15)
        started = time.perf_counter()
        _, _, _, kind = cache.get_or_compute(("s",), "usage", 0.05, slow)
        elapsed = time.perf_counter() - started
        release.set()
        worker.join(5.0)
        self.assertEqual(kind, "fresh", "past max staleness, readers wait for the recompute")
        self.assertGreaterEqual(elapsed, 0.1)
        self.assertTrue(bg and bg[0][0]["v"] == 2)

    def test_key_quantizes_windows(self):
        key = lambda params: respcache_mod.canonical_key("snapshot", params, None)
        self.assertEqual(
            key({"ws": "all", "since": 100.5}),
            key({"ws": "all", "since": 115.5}),
            "sliding since shares a key within the grid",
        )
        self.assertNotEqual(
            key({"ws": "all", "since": 100.5}),
            key({"ws": "all", "since": 200.5}),
        )
        self.assertNotEqual(key({"ws": "all"}), key({"ws": "ws1"}))

    def test_effective_ttl_desynchronises_keys(self):
        first = respcache_mod.effective_ttl(("snapshot", ()), 1.5)
        self.assertEqual(first, respcache_mod.effective_ttl(("snapshot", ()), 1.5))
        seen = {
            respcache_mod.effective_ttl((f"endpoint-{i}", ()), 1.5)
            for i in range(12)
        }
        self.assertGreater(len(seen), 1, "keys spread their expiries")
        for value in seen:
            self.assertGreaterEqual(value, 1.5 * 0.8)
            self.assertLessEqual(value, 1.5 * 1.2)

    def test_readonly_switch_changes_key(self):
        from dash.server import _ttl_viewer_params  # noqa: E402

        with mock.patch.dict(os.environ, {"AGENT_DASH_READONLY": "1"}):
            ro = _ttl_viewer_params(self.server)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_DASH_READONLY", None)
            rw = _ttl_viewer_params(self.server)
        self.assertNotEqual(
            respcache_mod.canonical_key("project", ro, None),
            respcache_mod.canonical_key("project", rw, None),
            "the read-only toggle must retag project/state at once",
        )


class TtlInvalidationTest(unittest.TestCase):
    """POST user actions invalidate the TTL so a tap shows immediately."""

    def setUp(self):
        pin = mock.patch.dict(os.environ, {"AGENT_DASH_URL": "http://127.0.0.1:9"})
        pin.start()
        self.addCleanup(pin.stop)
        self.old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        self.bundles = tempfile.TemporaryDirectory()
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.bundles.name
        self.root = Path(self.bundles.name)
        self.state = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.state.name) / "dash.sqlite")
        self.cfg = default_cfg()
        self.server = DashServer(
            self.store, Registry([]), "127.0.0.1", 0, cfg=self.cfg
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        from dash.bundles import cmd_ws  # noqa: E402

        self.cmd_ws = cmd_ws
        assert cmd_ws(["new", "WS1", "Alpha work"]) == 0
        plan_path = self.root / "WS1" / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        for group in plan["dag"]["groups"]:
            for task in group["tasks"]:
                task["status"] = "done"
        plan_path.write_text(json.dumps(plan))
        docs = self.root / "WS1" / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "closing-report.md").write_text("# Closing report\n\nShipped.\n")
        assert cmd_ws(["state", "WS1", "done"]) == 0

    def tearDown(self):
        self.server.stop.set()
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.store.close()
        self.bundles.cleanup()
        self.state.cleanup()
        if self.old_ws is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old_ws

    def _get(self, path, tag=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            headers = {}
            if tag is not None:
                headers["If-None-Match"] = tag
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def _post(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(
                "POST", path, body=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-AgentDash-Action": "1",
                    "Origin": self.origin,
                },
            )
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()

    def test_archive_then_lists_show_it(self):
        status, heads, first = self._get("/api/workstreams")
        self.assertEqual(status, 200)
        tag = heads.get("ETag")
        rows = {row["ws"]: row for row in json.loads(first)["workstreams"]}
        self.assertEqual(rows["WS1"]["state"], "done")
        status, _ = self._post("/api/workstreams/archive", {"ws": "WS1"})
        self.assertEqual(status, 200)
        status, heads2, second = self._get("/api/workstreams", tag)
        self.assertEqual(status, 200, "the POST invalidated the TTL: no stale 304")
        self.assertNotEqual(heads2.get("ETag"), tag)
        rows = {row["ws"]: row for row in json.loads(second)["workstreams"]}
        self.assertNotIn("WS1", rows, "archived WS1 leaves the active list at once")
        status, _, archived = self._get("/api/workstreams?state=archived")
        self.assertEqual(status, 200)
        rows = {row["ws"]: row for row in json.loads(archived)["workstreams"]}
        self.assertIn("WS1", rows)

    def test_failed_post_keeps_cache(self):
        status, heads, _ = self._get("/api/snapshot?ws=all")
        self.assertEqual(status, 200)
        tag = heads.get("ETag")
        status, _ = self._post("/api/workstreams/archive", {"ws": "NOPE"})
        self.assertEqual(status, 404)
        status, _, empty = self._get("/api/snapshot?ws=all", tag)
        self.assertEqual(status, 304, "a failed action invalidates nothing")


class FragmentPropertyTest(unittest.TestCase):
    """Cached run-view fragments equal fresh computation, fixed seed.

    Three snapshots at one fixed clock with churn writes between: the
    fragment cache must never change the output (dict and serialised
    bytes), only the cost of computing it.
    """

    def test_fragments_equal_fresh_serialisation(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "dash.sqlite")
        self.addCleanup(store.close)
        now = 1_700_000_000.0
        _seed_runs(store, now, count=120, seed=99)
        first = store.snapshot(now, "all", None, None)
        self.assertGreater(len(store._view_frags), 50, "fragments populate")
        rng = random.Random(20261019)
        for i in range(5):
            store.upsert_run(
                {
                    "run_id": f"muse:churn:{i}",
                    "source": "muse",
                    "label": f"ws{(i % 3) + 1}-churn",
                    "session_id": "sess-churn",
                    "status": "done",
                    "started_at": now - 100,
                    "ended_at": now - 10,
                }
            )
            store.add_usage(
                {
                    "ts": now - 5,
                    "session_id": "sess-churn",
                    "message_id": f"cm-{i}",
                    "run_id": f"muse:churn:{rng.randint(0, 30)}"
                    if rng.random() < 0.5 else "muse:ttl:3",
                    "model": "claude-sonnet-4-5",
                    "tool": "muse",
                    "input": 100,
                    "output": 10,
                }
            )
        warm = store.snapshot(now, "all", None, None)
        store._view_frags.clear()
        from dash.store import summarize as summarize_mod  # noqa: E402

        summarize_mod._OUTCOME_MEMO.clear()
        cold = store.snapshot(now, "all", None, None)
        self.assertEqual(warm, cold, "fragments must not change snapshot output")
        self.assertEqual(
            json.dumps(warm, separators=(",", ":"), allow_nan=False),
            json.dumps(cold, separators=(",", ":"), allow_nan=False),
            "cached fragments serialise byte-identical to fresh computation",
        )
        self.assertNotEqual(first["seq"], warm["seq"], "the churn writes landed")


class CountsEquivalenceTest(unittest.TestCase):
    """The grouped single-pass counts equal the legacy per-scope walks.

    Fixed seed: for every scope, ``_counts_many`` must return exactly
    what ``_agent_counts`` (windowed counts), the old ``_prev_24h``
    assembly (previous-window finished/live), and ``_point_live_count``
    return, plus the same running/recent/last-activity/confidence lists
    the old snapshot filters built.
    """

    def test_grouped_counts_match_legacy(self):
        from dash.store.summarize import (  # noqa: E402
            _agent_counts,
            _counts_many,
            _point_live_count,
            _prev_24h,
            _sum_cells,
        )
        from dash.store.util import UNASSIGNED, _billed_total  # noqa: E402

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "dash.sqlite")
        self.addCleanup(store.close)
        now = 1_700_000_100.0
        _seed_runs(store, now, count=120, seed=4242)
        with store._lock:
            shown, parents = store._shown_locked(now)
            live_open, _ = store._live_open_with_shadows_locked(now, shown)
            usage = store._usage_by_run()
            stalled = {
                run["run_id"]: run["stalled"]
                for run in shown
                if run.get("stalled")
            }
            range_since = now - 86400
            range_until = now
            prev_since = range_since - 86400
            prev_until = range_since
            from dash.store.summarize import _live_at_point_ids  # noqa: E402

            live_at_start = _live_at_point_ids(shown, parents, prev_until, now)
            members_by_ws: dict = {}
            for run in shown:
                members_by_ws.setdefault(
                    run.get("workstream") or UNASSIGNED, []
                ).append(run)
            if UNASSIGNED not in members_by_ws:
                members_by_ws[UNASSIGNED] = []
            all_counts, by_ws = _counts_many(
                shown, members_by_ws, parents, usage, now,
                range_since, range_until, prev_since, prev_until,
                live_open, stalled, live_at_start,
            )
            prev_cells = store._query_usage_locked(
                "workstream", "hour", None, prev_since, prev_until
            )
            prev_buckets: dict = {}
            for cell in prev_cells:
                prev_buckets.setdefault(cell["key"], []).append(cell)
            scopes = dict(members_by_ws)
            scopes["__all__"] = shown
            for ws, members in scopes.items():
                entry = all_counts if ws == "__all__" else by_ws[ws]
                legacy = _agent_counts(
                    members, parents, now, range_since, range_until,
                    live_open, stalled,
                )
                self.assertEqual(
                    entry["counts"], legacy, f"counts differ for scope {ws}"
                )
                legacy_prev = _prev_24h(
                    members, parents, now, prev_since, prev_until,
                    prev_buckets.get(ws if ws != "__all__" else None, [])
                    if ws != "__all__" else prev_cells,
                    live_open, live_at_start,
                )
                self.assertEqual(
                    entry["prev_finished"], legacy_prev["finished"],
                    f"prev finished differs for scope {ws}",
                )
                self.assertEqual(
                    entry["point_live"], legacy_prev["live_agents"],
                    f"prev live differs for scope {ws}",
                )
                self.assertEqual(
                    entry["point_live"],
                    _point_live_count(members, live_at_start),
                    f"point live differs for scope {ws}",
                )
                prev_summed = _sum_cells(
                    prev_buckets.get(ws if ws != "__all__" else None, [])
                    if ws != "__all__" else prev_cells
                )
                self.assertEqual(
                    _billed_total(prev_summed["tokens"]), legacy_prev["tokens"]
                )


if __name__ == "__main__":
    unittest.main()
