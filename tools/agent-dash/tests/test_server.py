import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import http.client
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_dash
from agent_dash import default_cfg, is_dash, load_cfg, main, reconcile_if_due, run_backfill
from dash.plans import PlanNotFound, load as load_plan, mapping as plan_mapping
from dash.server import DashServer, PLACEHOLDER
from dash.sources import Registry
from dash.store import Store


def start(store, index=None, registry=None, interval=2.0):
    registry = registry if registry is not None else Registry([])
    server = DashServer(store, registry, "127.0.0.1", 0, index_path=index, stream_interval=interval, cfg=default_cfg())
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, thread


def stop(server, thread):
    server.stop.set()
    server.shutdown()
    thread.join(3)
    server.server_close()


def get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        return response.status, response.getheader("Content-Type"), body
    finally:
        conn.close()


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.server, self.thread = start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()

    def test_binds_loopback_and_serves_routes(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, content_type, body = get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn(b"Agent execution", body)
        status, content_type, body = get(self.port, "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        payload = json.loads(body)
        self.assertEqual(payload["running"], [])
        self.assertNotIn("caps", payload)
        self.assertEqual(payload["counts"]["live_agents"], 0)
        status, _, body = get(self.port, "/api/health")
        health = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        self.assertEqual(health["sources"], [])
        status, _, body = get(self.port, "/api/plan?ws=NOPE")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "plan not found")
        status, _, body = get(self.port, "/api/usage?group=nope")
        self.assertEqual(status, 400)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request("POST", "/api/snapshot")
            response = conn.getresponse()
            self.assertEqual(response.status, 405)
            response.read()
        finally:
            conn.close()

    def test_placeholder_when_index_missing(self):
        stop(self.server, self.thread)
        missing = Path(self.tmp.name) / "missing.html"
        self.server, self.thread = start(self.store, index=missing)
        self.port = self.server.server_address[1]
        status, _, body = get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, PLACEHOLDER)

    def test_snapshot_and_runs_roundtrip(self):
        now = time.time()
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws7-landing",
                "source": "muse",
                "tier": "muse",
                "label": "ws7-landing",
                "status": "running",
                "started_at": now - 5,
            }
        )
        status, _, body = get(self.port, "/api/snapshot?ws=WS7")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["running"][0]["workstream"], "WS7")
        self.assertEqual(payload["running"][0]["label"], "ws7-landing")
        status, _, body = get(self.port, "/api/runs?limit=10")
        runs = json.loads(body)["runs"]
        self.assertEqual(runs[0]["run_id"], "muse:s:ws7-landing")

    def test_snapshot_carries_truncation_and_freshness(self):
        status, _, body = get(self.port, "/api/snapshot?ws=all")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        for key in ("recent_shown", "recent_total", "data_seq", "last_data_ts"):
            self.assertIn(key, payload, f"snapshot misses {key}")
        self.assertEqual(payload["recent_shown"], len(payload["recent"]))
        self.assertGreaterEqual(payload["recent_total"], len(payload["recent"]))
        first = dict(payload)
        status, _, body = get(self.port, "/api/snapshot?ws=all")
        second = json.loads(body)
        self.assertEqual(first["seq"], second["seq"])
        self.assertEqual(first["counts"], second["counts"])

    def test_reconcile_carries_recent_counts(self):
        status, _, body = get(self.port, "/api/reconcile?hours=12")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("recent_shown", payload)
        self.assertIn("recent_total", payload)
        self.assertLessEqual(payload["recent_shown"], 50)

    def test_plan_smoke(self):
        mapping = plan_mapping()
        status, _, body = get(self.port, "/api/plan?ws=WS7")
        self.assertEqual(status, 200)
        plan = json.loads(body)
        raw = json.loads(Path(mapping["WS7"]).read_text(encoding="utf-8"))
        self.assertEqual(plan["ws"], "WS7")
        self.assertEqual(plan["cols"], raw["dag"]["cols"])
        self.assertEqual(plan["groups"][0]["id"], raw["dag"]["groups"][0]["id"])
        self.assertEqual(plan["edges"], raw["dag"]["edges"])
        self.assertEqual(
            plan["phases"],
            [{"name": item.get("name"), "goal": item.get("goal"), "gate": item.get("gate")} for item in raw["narrative"]["phases"]],
        )
        self.assertNotIn("decisions", plan)
        self.assertNotIn("motivation", plan)
        status, _, body = get(self.port, "/api/plan?ws=WS2")
        ws2 = json.loads(body)
        raw2 = json.loads(Path(mapping["WS2"]).read_text(encoding="utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(ws2["cols"], raw2["dag"]["cols"])

    def test_workstreams_carry_plan_progress(self):
        from dash.bundles import resolve_plan

        self.store.set_ws_counts(
            [
                {"ws": "WS1", "name": "Read path", "backlog": 0, "todo": 0, "doing": 0, "review": 0, "done": 0, "cancelled": 0},
                {"ws": "WS9", "name": "No plan", "backlog": 0, "todo": 0, "doing": 0, "review": 0, "done": 0, "cancelled": 0},
            ]
        )
        status, _, body = get(self.port, "/api/workstreams")
        self.assertEqual(status, 200)
        rows = {row["ws"]: row for row in json.loads(body)["workstreams"]}
        _source, expected = resolve_plan("WS1")
        self.assertIsNotNone(expected)
        self.assertEqual(rows["WS1"]["progress"], expected["progress"])
        self.assertEqual(set(rows["WS1"]["progress"]), {"done", "total", "counts", "pct"})
        self.assertIsNone(rows["WS9"]["progress"])

    def test_stream_tick(self):
        stop(self.server, self.thread)
        self.server, self.thread = start(self.store, interval=30)
        self.port = self.server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request("GET", "/api/stream")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("text/event-stream", response.getheader("Content-Type"))
        buf = b""
        while b"\n\n" not in buf:
            chunk = response.read(1)
            if not chunk:
                break
            buf += chunk
        self.assertIn(b"event: tick\n", buf)
        data = json.loads(buf.split(b"data: ", 1)[1].split(b"\n", 1)[0])
        self.assertTrue({"seq", "running"} <= set(data), data.keys())
        for key in ("now", "data_seq", "last_data_ts", "last_poll", "sources"):
            self.assertIn(key, data, f"tick payload misses freshness field {key}")
        conn.close()


class PlanAndBackfillTest(unittest.TestCase):
    def test_missing_plan_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plans.json"
            path.write_text(json.dumps({"WS9": str(Path(tmp) / "gone.json")}), encoding="utf-8")
            with self.assertRaises(PlanNotFound):
                load_plan("WS9", path)
            with self.assertRaises(PlanNotFound):
                load_plan("WS1", path)

    def test_backfill_stops_when_idle(self):
        class Chunk:
            NAME = "chunk"
            INTERVAL_S = 1

            def __init__(self):
                self.n = 0

            def poll(self, store, cfg, now):
                self.assert_session(cfg)
                if self.n < 3:
                    store.set_offset(f"p{self.n}", 1, self.n, 1, now)
                    self.n += 1

            @staticmethod
            def assert_session(cfg):
                if cfg.get("backfill_session") != "abc":
                    raise AssertionError("backfill session was not passed to the source")

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                passes = run_backfill(store, Registry([Chunk()]), default_cfg(), "abc")
            finally:
                store.close()
        self.assertEqual(passes, 5)

    def test_source_error_is_isolated(self):
        class Bad:
            NAME = "bad"
            INTERVAL_S = 1

            def poll(self, store, cfg, now):
                raise RuntimeError("boom")

        registry = Registry([Bad()])
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                registry.tick(store, {}, 10, force=True)
                health = registry.health(12)
            finally:
                store.close()
        self.assertFalse(health["ok"])
        self.assertEqual(health["sources"][0]["last_error"], "RuntimeError: boom")
        self.assertEqual(health["sources"][0]["lag_s"], 2)

    def test_cli_guards(self):
        self.assertEqual(main(["--help"]), 0)
        self.assertEqual(main(["backfill"]), 2)
        self.assertEqual(main(["nope"]), 2)
        self.assertFalse(is_dash(os.getpid()))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"host": "bad-host", "caps": {"grok": 3}}), encoding="utf-8")
            cfg = load_cfg(path)
        self.assertEqual(cfg["host"], "127.0.0.1")
        self.assertEqual(cfg["port"], 8765)
        self.assertNotIn("caps", cfg)

    def test_reconcile_runs_immediately_then_every_60s(self):
        import agent_dash

        calls = []
        orig = agent_dash.reconcile_running

        def fake(store, now, processes=None):
            calls.append(now)
            return 0

        agent_dash.reconcile_running = fake
        try:
            last = reconcile_if_due(None, 1000.0, None)
            self.assertEqual(last, 1000.0)
            last = reconcile_if_due(None, 1059.0, last)
            self.assertEqual(last, 1000.0)
            self.assertEqual(calls, [1000.0])
            last = reconcile_if_due(None, 1060.0, last)
            self.assertEqual(calls, [1000.0, 1060.0])
            self.assertEqual(last, 1060.0)
        finally:
            agent_dash.reconcile_running = orig


class ReconcileEndpointTest(unittest.TestCase):
    def setUp(self):
        from unittest import mock

        # Isolate from machine-global ps state: the live listing is empty
        # here unless a test stubs it otherwise.
        self._live_patch = mock.patch("dash.server._live_workers", return_value=[])
        self._live_patch.start()
        self.addCleanup(self._live_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        now = time.time()
        self.store.upsert_run(
            {
                "run_id": "muse:a:ws1-a",
                "source": "muse",
                "tier": "muse",
                "model": "claude-sonnet-5-5",
                "label": "ws1-a",
                "status": "running",
                "started_at": now - 100,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "muse:b:helper",
                "source": "muse",
                "tier": "muse",
                "model": "claude-sonnet-5-5",
                "label": "helper",
                "session_id": "sess-b",
                "status": "done",
                "started_at": now - 3000,
                "ended_at": now - 2000,
            }
        )
        self.store.add_usage(
            {
                "ts": now - 2100,
                "session_id": "sess-b",
                "run_id": "muse:b:helper",
                "model": "claude-sonnet-5-5",
                "tool": "claude",
                "message_id": "m1",
                "input": 400,
                "output": 20,
                "cache_read": 0,
                "cache_write": 0,
            }
        )
        self.server, self.thread = start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()

    def test_reconcile_endpoint(self):
        status, _, body = get(self.port, "/api/reconcile?hours=12")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["mismatches"], [])
        self.assertEqual(payload["all"]["live_agents"], 1)
        self.assertEqual(payload["all"]["finished_in_range"], 1)
        self.assertEqual(payload["unassigned"]["tokens"], 420)
        self.assertEqual(payload["unassigned"]["finished_in_range"], 1)
        self.assertEqual([row["ws"] for row in payload["rows"]], ["WS1"])
        status, _, body = get(self.port, "/api/reconcile?hours=nope")
        self.assertEqual(status, 400)

    def test_check_command_passes(self):
        self.assertEqual(main(["check", "--url", f"http://127.0.0.1:{self.port}", "--hours", "12"]), 0)
        self.assertEqual(main(["check", "--url", "http://127.0.0.1:1", "--hours", "12"]), 2)
        self.assertEqual(main(["check", "--hours", "nope"]), 2)

    def test_reconcile_reports_unseen_worker(self):
        from unittest import mock

        ghosts = [
            {
                "pid": 4242,
                "comm": "cursor-agent",
                "started_at": time.time() - 10,
                "label": "ws1-ghost",
                "prompt_label": None,
                "session_id": None,
            }
        ]
        with mock.patch("dash.server._live_workers", return_value=ghosts):
            status, _, body = get(self.port, "/api/reconcile?hours=12")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(
            any(
                item == "unseen worker: ws1-ghost (cursor-agent pid 4242)"
                for item in payload["mismatches"]
            ),
            payload["mismatches"],
        )
        with mock.patch("dash.server._live_workers", return_value=ghosts):
            self.assertEqual(
                main(["check", "--url", f"http://127.0.0.1:{self.port}", "--hours", "12"]), 1
            )

    def test_health_reports_scratch_roots_and_run_dirs(self):
        from dash.sources import Registry

        rundir = Path(self.tmp.name) / "runs" / "ws1"
        rundir.mkdir(parents=True)
        (rundir / "glm-sessions.tsv").write_text(
            "ws1-ghost\t11111111-1111-4111-8111-111111111111\t1700000000\tglm-5.3\n",
            encoding="utf-8",
        )
        state = Path(self.tmp.name) / "state"
        state.mkdir()
        (state / "run-dirs.txt").write_text(str(rundir) + "\n", encoding="utf-8")
        cfg = default_cfg()
        cfg["roots"]["scratchpads"] = []
        cfg["state_dir"] = str(state)
        ghosts = [{"pid": 4242, "comm": "grok", "started_at": 1.0, "label": "ws1-ghost"}]
        body = Registry([]).health(time.time(), cfg, processes=ghosts)
        self.assertEqual(body["scratch_roots"], [])
        self.assertEqual(body["run_dirs"], [str(rundir)])
        self.assertEqual(len(body["warnings"]), 1)
        self.assertIn("ws1-ghost", body["warnings"][0])
        self.assertIn(str(rundir), body["warnings"][0])
        # A configured root covering the run folder silences the warning.
        cfg["roots"]["scratchpads"] = [str(rundir)]
        body = Registry([]).health(time.time(), cfg, processes=ghosts)
        self.assertEqual(body["warnings"], [])

    def test_reconcile_reports_stale_running(self):
        self.store.upsert_run(
            {
                "run_id": "muse:stale:ws1-stale",
                "source": "muse",
                "tier": "muse",
                "label": "ws1-stale",
                "session_id": "stale-session",
                "status": "running",
                "pid": 999999,
                "started_at": time.time() - 60,
            }
        )
        status, _, body = get(self.port, "/api/reconcile?hours=12")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(
            any("stale running" in item for item in payload["mismatches"]),
            payload["mismatches"],
        )
        self.assertEqual(
            main(["check", "--url", f"http://127.0.0.1:{self.port}", "--hours", "12"]), 1
        )

    def test_check_page_skips_without_node(self):
        from unittest import mock

        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(main(["check", "--page", "--url", f"http://127.0.0.1:{self.port}"]), 0)
        self.assertEqual(main(["check", "--page", "--hours", "nope"]), 2)

    def test_check_command_fails_on_mismatch(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        doc = json.dumps(
            {
                "since": 1,
                "until": 2,
                "rows": [{"ws": "WS1", "live_agents": 1, "finished_in_range": 0, "tokens": 5, "cost_usd": 0.0}],
                "unassigned": {"ws": "unassigned", "live_agents": 0, "finished_in_range": 0, "tokens": 0, "cost_usd": 0.0},
                "mismatches": ["tokens: workstream rows sum to 5, overview shows 6"],
            }
        ).encode()

        class Stub(BaseHTTPRequestHandler):
            def do_GET(self):
                body = doc
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        server = HTTPServer(("127.0.0.1", 0), Stub)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            self.assertEqual(main(["check", "--url", f"http://127.0.0.1:{port}"]), 1)
        finally:
            server.shutdown()
            thread.join(3)
            server.server_close()


def free_port():
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class StopRestartTest(unittest.TestCase):
    """`stop` waits for the exit and the free port; `restart` is stop then
    run plus a health wait. Never touches the live :8765 port."""

    def setUp(self):
        import subprocess

        self.subprocess = subprocess
        self.tmp = tempfile.TemporaryDirectory()
        self.keep_state = os.environ.get("AGENT_DASH_STATE_DIR")
        os.environ["AGENT_DASH_STATE_DIR"] = self.tmp.name
        self.keep = (
            agent_dash.STATE_DIR,
            agent_dash.PIDFILE,
            agent_dash.LOGFILE,
            agent_dash.DBFILE,
            agent_dash.PORT,
            agent_dash.is_dash,
        )
        agent_dash.PORT = free_port()
        self.addCleanup(self.restore)
        self.procs = []

    def restore(self):
        for proc in self.procs:
            try:
                proc.kill()
            except OSError:
                pass
        (
            agent_dash.STATE_DIR,
            agent_dash.PIDFILE,
            agent_dash.LOGFILE,
            agent_dash.DBFILE,
            agent_dash.PORT,
            agent_dash.is_dash,
        ) = self.keep
        if self.keep_state is None:
            os.environ.pop("AGENT_DASH_STATE_DIR", None)
        else:
            os.environ["AGENT_DASH_STATE_DIR"] = self.keep_state
        self.tmp.cleanup()

    def sleeper(self):
        proc = self.subprocess.Popen(
            ["bash", "-c", "exec -a agent_dash.py sleep 30"],
            stdin=self.subprocess.DEVNULL,
            stdout=self.subprocess.DEVNULL,
            stderr=self.subprocess.DEVNULL,
        )
        self.procs.append(proc)
        return proc

    def test_stop_waits_for_exit(self):
        proc = self.sleeper()
        self.assertTrue(is_dash(proc.pid))
        agent_dash.bind_state()
        agent_dash.PIDFILE.write_text(str(proc.pid) + "\n", encoding="utf-8")
        self.assertEqual(main(["stop"]), 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(proc.pid, 0)
        self.assertFalse(agent_dash.PIDFILE.exists())

    def test_stop_returns_1_while_the_port_is_bound(self):
        import socket

        blocker = socket.socket()
        blocker.bind(("127.0.0.1", agent_dash.PORT))
        blocker.listen(1)
        try:
            proc = self.sleeper()
            agent_dash.bind_state()
            agent_dash.PIDFILE.write_text(str(proc.pid) + "\n", encoding="utf-8")
            self.assertEqual(main(["stop"]), 1)
            self.assertFalse(agent_dash.PIDFILE.exists())
        finally:
            blocker.close()

    def test_restart_starts_and_passes_health(self):
        agent_dash.is_dash = lambda pid: True
        self.assertEqual(main(["restart"]), 0)
        self.assertTrue(agent_dash._health_ok())
        self.assertEqual(main(["stop"]), 0)
        self.assertTrue(agent_dash._port_free())


class SnapshotUsageEndpointTest(unittest.TestCase):
    """GET /api/snapshot?usage_group= carries the hourly chart buckets for
    the same scope and window in the same response; they sum to the totals."""

    TOKEN_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.now = time.time()
        self.since = int(self.now - 12 * 3600)
        self.store.upsert_run(
            {
                "run_id": "muse:a:ws1-a",
                "source": "muse",
                "tier": "muse",
                "model": "claude-sonnet-4-5",
                "label": "ws1-a",
                "session_id": "sess-a",
                "status": "done",
                "started_at": self.now - 5000,
                "ended_at": self.now - 4000,
            }
        )
        self.store.add_usage(
            {
                "ts": self.now - 4100,
                "session_id": "sess-a",
                "run_id": "muse:a:ws1-a",
                "model": "claude-sonnet-4-5",
                "tool": "muse",
                "message_id": "m1",
                "input": 100,
                "output": 50,
                "cache_read": 10,
                "cache_write": 5,
            }
        )
        self.server, self.thread = start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()

    def fetch(self, path):
        status, _, body = get(self.port, path)
        self.assertEqual(status, 200, path)
        return json.loads(body)

    def assert_buckets_match_totals(self, payload, scope):
        self.assertIn("usage", payload)
        self.assertTrue(payload["usage"], scope)
        for key in self.TOKEN_KEYS:
            self.assertEqual(
                sum(cell[key] for cell in payload["usage"]),
                payload["totals"]["tokens"][key],
                f"{scope} {key}",
            )

    def test_snapshot_usage_sums_for_model_and_workstream(self):
        for group in ("model", "workstream"):
            payload = self.fetch(f"/api/snapshot?ws=all&since={self.since}&usage_group={group}")
            self.assert_buckets_match_totals(payload, f"{group}/all")
        scoped = self.fetch(f"/api/snapshot?ws=WS1&since={self.since}&usage_group=model")
        self.assert_buckets_match_totals(scoped, "model/WS1")
        tab = self.fetch(f"/api/snapshot?ws=WS1&since={self.since}")
        self.assertEqual(scoped["totals"], tab["totals"])

    def test_snapshot_without_group_omits_usage(self):
        payload = self.fetch(f"/api/snapshot?ws=all&since={self.since}")
        self.assertNotIn("usage", payload)

    def test_snapshot_bad_group_is_400(self):
        status, _, _ = get(self.port, "/api/snapshot?usage_group=nope")
        self.assertEqual(status, 400)


class UnpricedAlertTest(unittest.TestCase):
    """Item 7: a model with windowed usage and no rate fails health and
    reconcile loudly, and the snapshot totals name the excluded tokens."""

    MODEL = "unpriced-test-model"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.now = time.time()
        self.store.upsert_run(
            {
                "run_id": "muse:u:ws1-u",
                "source": "muse",
                "tier": "muse",
                "model": self.MODEL,
                "label": "ws1-u",
                "session_id": "sess-u",
                "status": "done",
                "started_at": self.now - 5000,
                "ended_at": self.now - 4000,
            }
        )
        self.store.add_usage(
            {
                "ts": self.now - 4100,
                "session_id": "sess-u",
                "run_id": "muse:u:ws1-u",
                "model": self.MODEL,
                "tool": "muse",
                "message_id": "m1",
                "input": 100,
                "output": 200,
                "cache_read": 0,
                "cache_write": 0,
            }
        )
        self.server, self.thread = start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()

    def test_health_fails_on_unpriced_windowed_usage(self):
        status, _, body = get(self.port, "/api/health")
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertFalse(health["ok"])
        self.assertEqual(
            health["unpriced_models"],
            [{"model": self.MODEL, "rows": 1, "tokens": 300}],
        )
        self.assertIn("disconnects_1h", health)
        self.assertEqual(health["disconnects_1h"], 0)

    def test_health_passes_without_unpriced_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                server, thread = start(store)
                try:
                    status, _, body = get(server.server_address[1], "/api/health")
                finally:
                    stop(server, thread)
            finally:
                store.close()
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertTrue(health["ok"])
        self.assertNotIn("unpriced_models", health)

    def test_reconcile_flags_unpriced_model(self):
        status, _, body = get(self.port, "/api/reconcile?hours=24")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn(
            f"unpriced model: {self.MODEL} (300 tokens in 24 h)",
            payload["mismatches"],
        )

    def test_snapshot_totals_name_unpriced_tokens(self):
        status, _, body = get(self.port, "/api/snapshot?ws=all")
        self.assertEqual(status, 200)
        totals = json.loads(body)["totals"]
        self.assertEqual(totals["unpriced_tokens"], 300)
        self.assertEqual(totals["unpriced_models"], [self.MODEL])


class DisconnectSilenceTest(unittest.TestCase):
    """Item 9: BrokenPipe/ConnectionReset are counted, not printed. Real
    errors still log."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.server, self.thread = start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()

    def _handler(self):
        from dash.server import Handler

        handler = Handler.__new__(Handler)
        handler.server = self.server
        handler.command = "GET"
        handler.requestline = "GET /api/snapshot HTTP/1.0"
        handler.request_version = "HTTP/1.0"
        return handler

    def test_bytes_swallows_broken_pipe_and_counts(self):
        handler = self._handler()

        class Dead:
            def write(self, data):
                raise BrokenPipeError(32, "Broken pipe")

            def flush(self):
                pass

        handler.wfile = Dead()
        handler._bytes(200, b"x", "text/plain")
        self.assertEqual(self.server.disconnects_1h(), 1)

    def test_handle_error_counts_reset_without_traceback(self):
        import contextlib
        import io

        err = io.StringIO()
        try:
            raise ConnectionResetError(54, "Connection reset by peer")
        except ConnectionResetError:
            with contextlib.redirect_stderr(err):
                self.server.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(self.server.disconnects_1h(), 1)

    def test_handle_error_still_logs_real_errors(self):
        import contextlib
        import io

        err = io.StringIO()
        try:
            raise ValueError("boom")
        except ValueError:
            with contextlib.redirect_stderr(err):
                self.server.handle_error(None, ("127.0.0.1", 1))
        self.assertIn("ValueError", err.getvalue())
        self.assertEqual(self.server.disconnects_1h(), 0)


def _replay_bundle(root: Path) -> None:
    ws = root / "WS9"
    ws.mkdir()
    (ws / "bundle.json").write_text(
        json.dumps({"id": "WS9", "title": "Example", "short": "Ex", "order": 1, "kind": "workstream"}),
        encoding="utf-8",
    )
    (ws / "plan.json").write_text(
        json.dumps(
            {
                "dag": {
                    "cols": ["Shape", "Build"],
                    "groups": [
                        {
                            "id": "g",
                            "col": 0,
                            "title": "Group",
                            "tasks": [
                                {"id": "1", "title": "First task", "status": "done", "deps": []},
                                {"id": "2", "title": "Second task", "status": "done", "deps": ["1"]},
                                {"id": "3", "title": "Third task", "status": "done", "deps": ["2"]},
                                {"id": "4", "title": "Later task", "status": "todo", "deps": ["3"]},
                            ],
                        }
                    ],
                    "edges": [],
                },
                "narrative": {"phases": []},
            }
        ),
        encoding="utf-8",
    )


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name
        _replay_bundle(Path(self.tmp.name))
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.store.upsert_run(
            {
                "run_id": "muse:s1:ws9-1-first",
                "source": "muse",
                "tier": "muse",
                "label": "ws9-1-first",
                "status": "done",
                "verdict": "PASS",
                "started_at": 1000.0,
                "ended_at": 1100.0,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "muse:s2:ws9-1-second",
                "source": "muse",
                "tier": "muse",
                "label": "ws9-1-second",
                "status": "done",
                "started_at": 1200.0,
                "ended_at": 1300.0,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "grok:s3:ws9-2-x",
                "source": "grok",
                "tier": "grok",
                "label": "ws9-2-x",
                "status": "done",
                "verdict": "PASS",
                "started_at": 1400.0,
                "ended_at": 1500.0,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "muse:s4:ws8-1-x",
                "source": "muse",
                "tier": "muse",
                "label": "ws8-1-x",
                "status": "done",
                "started_at": 900.0,
                "ended_at": 950.0,
            }
        )
        self.server, self.thread = start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old
        self.tmp.cleanup()

    def replay(self):
        status, _, body = get(self.port, "/api/replay?ws=WS9")
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_observed_times_come_from_runs(self):
        payload = self.replay()
        one = payload["tasks"]["1"]
        self.assertEqual(one["first_active"], 1000.0)
        self.assertEqual(one["done_at"], 1300.0)
        self.assertFalse(one["inferred"])
        self.assertEqual([run["label"] for run in one["runs"]], ["ws9-1-first", "ws9-1-second"])
        self.assertEqual({run["family"] for run in one["runs"]}, {"muse"})
        for run in one["runs"]:
            self.assertEqual(
                set(run),
                {"start", "end", "family", "label", "verdict", "source", "status", "tokens", "clock"},
            )
        self.assertEqual(one["runs"][0]["verdict"], "PASS")
        self.assertEqual(one["runs"][0]["source"], "muse")
        self.assertEqual(one["runs"][0]["status"], "done")
        self.assertIsInstance(one["runs"][0]["tokens"], int)
        two = payload["tasks"]["2"]
        self.assertEqual((two["first_active"], two["done_at"], two["inferred"]), (1400.0, 1500.0, False))

    def test_other_workstream_runs_do_not_leak_in(self):
        payload = self.replay()
        labels = [event["label"] for event in payload["events"] if event["kind"] == "active"]
        self.assertNotIn("ws8-1-x", labels)

    def test_inferred_done_uses_the_deps(self):
        payload = self.replay()
        three = payload["tasks"]["3"]
        self.assertEqual(three["runs"], [])
        self.assertIsNone(three["first_active"])
        self.assertEqual(three["done_at"], 1500.0)
        self.assertTrue(three["inferred"])
        four = payload["tasks"]["4"]
        self.assertIsNone(four["done_at"])
        self.assertFalse(four["inferred"])

    def test_events_sorted_and_counts(self):
        payload = self.replay()
        stamps = [(event["t"], event["kind"]) for event in payload["events"]]
        self.assertEqual(stamps, sorted(stamps, key=lambda row: (row[0], 0 if row[1] == "active" else 1)))
        self.assertEqual(payload["span"], {"start": 1000.0, "end": 1500.0})
        self.assertEqual(payload["observed"], 2)
        self.assertEqual(payload["inferred"], 1)

    def test_recorded_history_wins(self):
        (Path(self.tmp.name) / "WS9" / "history.jsonl").write_text(
            json.dumps({"ts": 9999.0, "task": "3", "from": "todo", "to": "done"}) + "\n", encoding="utf-8"
        )
        payload = self.replay()
        three = payload["tasks"]["3"]
        self.assertEqual(three["done_at"], 9999.0)
        self.assertFalse(three["inferred"])
        self.assertEqual(payload["span"]["end"], 9999.0)
        self.assertEqual(payload["observed"], 3)
        self.assertEqual(payload["inferred"], 0)

    def test_done_prefers_first_pass_before_review_only(self):
        """A passing run followed only by review runs ends the task: the
        done moment is the PASS end, not the later review end. Task 1 keeps
        the last end because an implementation run follows its PASS."""
        self.store.upsert_run(
            {
                "run_id": "grok:s5:ws9-2-review",
                "source": "grok",
                "tier": "grok",
                "label": "ws9-2-review round",
                "status": "done",
                "verdict": "APPROVE",
                "started_at": 1550.0,
                "ended_at": 1600.0,
            }
        )
        payload = self.replay()
        one = payload["tasks"]["1"]
        self.assertEqual(one["done_at"], 1300.0)
        two = payload["tasks"]["2"]
        self.assertEqual(two["done_at"], 1500.0)
        self.assertFalse(two["inferred"])

    def test_replay_errors(self):
        status, _, body = get(self.port, "/api/replay")
        self.assertEqual(status, 400)
        status, _, _ = get(self.port, "/api/replay?ws=..")
        self.assertEqual(status, 400)
        status, _, body = get(self.port, "/api/replay?ws=WS404")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "plan not found")


if __name__ == "__main__":
    unittest.main()
