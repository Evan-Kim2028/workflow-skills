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
from unittest import mock
import http.client
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_dash
from agent_dash import default_cfg, is_dash, load_cfg, main, reconcile_if_due, run_backfill
from dash.plans import PlanNotFound, load as load_plan, mapping as plan_mapping
from dash.server import DashServer, PLACEHOLDER, ROOT
from dash.sources import Registry
from dash.store import Store


_KEPT_PORT = os.environ.get("AGENT_DASH_PORT")


def setUpModule():
    # main() re-reads AGENT_DASH_PORT on every call, so a trial port
    # inherited from a deploy would clobber the in-process test ports.
    # The tests choose their own ports; the env override stays out.
    os.environ.pop("AGENT_DASH_PORT", None)


def tearDownModule():
    if _KEPT_PORT is None:
        os.environ.pop("AGENT_DASH_PORT", None)
    else:
        os.environ["AGENT_DASH_PORT"] = _KEPT_PORT


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

    def test_problem_docs_serve_text_and_refuse_traversal(self):
        status, content_type, body = get(self.port, "/docs/problems/dep-cycle.md")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/plain; charset=utf-8")
        self.assertEqual(body, (ROOT / "docs" / "problems" / "dep-cycle.md").read_bytes())
        for path in ("/docs/problems/../CONTRACT.md", "/docs/problems/nope.md"):
            status, content_type, body = get(self.port, path)
            self.assertEqual(status, 404, path)
            self.assertIn("application/json", content_type)
            self.assertEqual(json.loads(body)["error"], "not found")

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
                "model": "muse-spark-1.3-contributor",
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

    def test_runs_and_snapshot_carry_outcome_class(self):
        """Every /api/runs row and snapshot row carries the one server
        derivation (run.outcome_class plus run.outcome_reason), and the
        finished buckets count classes, never raw verdicts: a killed row
        with a BLOCKED verdict reads died, a quota row reads died with
        its reason, a CANCELED job reads cancelled."""
        now = time.time()
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws7-doomed",
                "source": "muse",
                "tier": "muse",
                "label": "ws7-doomed",
                "status": "killed",
                "verdict": "BLOCKED",
                "started_at": now - 200,
                "ended_at": now - 100,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "glm:s:ws7-quota",
                "source": "glm",
                "tier": "glm",
                "label": "ws7-quota",
                "status": "died_early",
                "verdict": "QUOTA",
                "started_at": now - 200,
                "ended_at": now - 100,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws7-pass",
                "source": "muse",
                "tier": "muse",
                "label": "ws7-pass",
                "status": "done",
                "verdict": "PASS",
                "started_at": now - 200,
                "ended_at": now - 100,
            }
        )
        status, _, body = get(self.port, "/api/runs?limit=10")
        self.assertEqual(status, 200)
        by_id = {row["run_id"]: row for row in json.loads(body)["runs"]}
        self.assertEqual(by_id["muse:s:ws7-doomed"]["outcome_class"], "died")
        self.assertIn("SIGTERM", by_id["muse:s:ws7-doomed"]["outcome_reason"])
        self.assertEqual(by_id["glm:s:ws7-quota"]["outcome_class"], "died")
        self.assertIn("quota", by_id["glm:s:ws7-quota"]["outcome_reason"])
        self.assertEqual(by_id["muse:s:ws7-pass"]["outcome_class"], "ok")
        status, _, body = get(self.port, "/api/snapshot?ws=all")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        recent = {row["run_id"]: row for row in payload["recent"]}
        self.assertEqual(recent["muse:s:ws7-doomed"]["outcome_class"], "died")
        self.assertEqual(recent["glm:s:ws7-quota"]["outcome_class"], "died")
        self.assertEqual(
            payload["counts"]["finished_by_verdict"], {"died": 2, "ok": 1}
        )

    def test_agent_areas_hold_no_prod_jobs(self):
        """Production jobs are a count, never a list: the snapshot running
        and recent lists and /api/runs (default) hold no dagster row, while
        counts.prod_jobs_finished names the finished jobs and
        ?include=jobs brings their rows back for tools that need them."""
        now = time.time()
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws7-x",
                "source": "muse",
                "tier": "muse",
                "model": "muse-spark-1.3-contributor",
                "label": "ws7-x",
                "status": "done",
                "started_at": now - 200,
                "ended_at": now - 100,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "dagster:job:backfill",
                "source": "dagster",
                "model": "backfill",
                "label": "ws7-backfill",
                "status": "finished",
                "started_at": now - 200,
                "ended_at": now - 100,
                "clock_source": "dagster",
            }
        )
        status, _, body = get(self.port, "/api/snapshot?ws=WS7")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        for row in payload["running"] + payload["recent"]:
            self.assertNotEqual(row["source"], "dagster", row["run_id"])
        self.assertEqual(payload["counts"]["finished_in_range"], 1)
        self.assertEqual(payload["recent_total"], 1)
        self.assertEqual(payload["counts"]["prod_jobs_finished"], 1)
        status, _, body = get(self.port, "/api/runs?ws=WS7&limit=50")
        self.assertEqual(status, 200)
        runs = json.loads(body)["runs"]
        self.assertEqual([row["run_id"] for row in runs], ["muse:s:ws7-x"])
        status, _, body = get(self.port, "/api/runs?ws=WS7&limit=50&include=jobs")
        self.assertEqual(status, 200)
        with_jobs = json.loads(body)["runs"]
        self.assertEqual(
            {row["run_id"] for row in with_jobs}, {"muse:s:ws7-x", "dagster:job:backfill"}
        )
        status, _, _ = get(self.port, "/api/runs?include=nope")
        self.assertEqual(status, 400)

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

    def test_plan_survives_layout_failure(self):
        with mock.patch("dash.maplayout.layout", side_effect=ValueError("layout boom")), mock.patch.dict("dash.server._PLAN_MAP_CACHE", clear=True):
            with self.assertLogs("agent_dash.maplayout", level="WARNING"):
                status, _, body = get(self.port, "/api/plan?ws=WS7")
        self.assertEqual(status, 200)
        plan = json.loads(body)
        self.assertEqual(plan["ws"], "WS7")
        self.assertIsNone(plan["layout"])
        self.assertEqual(plan["map_warnings"], [])

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


class ProjectEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "dataroot")
        os.makedirs(self.root, exist_ok=True)
        self._env = mock.patch.dict(
            os.environ,
            {
                "AGENT_DASH_DATA_ROOT": self.root,
                "AGENT_DASH_STATE_DIR": os.path.join(self.tmp.name, "state"),
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.server, self.thread = start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()

    def _write_project(self, data):
        path = os.path.join(self.root, "project.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    def test_missing_project_reads_null_name(self):
        status, _, body = get(self.port, "/api/project")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIsNone(payload["name"])
        self.assertEqual(payload["phases"], [])
        self.assertEqual(payload["issues"], [])
        self.assertEqual(len(payload["data_root_hash"]), 12)
        self.assertEqual(set(payload["counts"]), {"planned", "active", "paused", "done", "archived", "dropped"})
        self.assertIn("tokens", payload["totals_all_time"])
        self.assertNotIn(self.root, body.decode("utf-8", "replace"))

    def test_present_project_computes_phase_states(self):
        self._write_project(
            {
                "name": "Demo project",
                "short": "demo",
                "description": "Seeded.",
                "goals": ["Ship the first cut"],
                "started": "2026-10-15",
                "target": None,
                "phases": [
                    {"id": "p1", "title": "First cut", "summary": "", "gate": ["WS1"]},
                ],
                "links": [],
                "owner_notes": "",
            }
        )
        status, _, body = get(self.port, "/api/project")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["name"], "Demo project")
        self.assertEqual(payload["issues"], [])
        # Fixture bundles carry no lifecycle state yet, so members read active.
        self.assertEqual(payload["phases"][0]["state"], "active")
        self.assertEqual(payload["phases"][0]["members"], [{"ws": "WS1", "state": "active"}])
        self.assertGreaterEqual(payload["counts"]["active"], 1)
        self.assertNotIn(self.root, body.decode("utf-8", "replace"))

    def test_invalid_project_serves_issues(self):
        with open(os.path.join(self.root, "project.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        status, _, body = get(self.port, "/api/project")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIsNone(payload["name"])
        self.assertTrue(payload["issues"])


class ProjectsRegistryEndpointTest(unittest.TestCase):
    """GET /api/projects: registry rows plus probes, never another root."""

    def setUp(self):
        from dash import projects as projects_mod

        projects_mod.clear_cache()
        self.addCleanup(projects_mod.clear_cache)
        self.tmp = tempfile.TemporaryDirectory()
        self.root_a = os.path.join(self.tmp.name, "a-data")
        self.root_b = os.path.join(self.tmp.name, "b-data")
        os.makedirs(self.root_a, exist_ok=True)
        os.makedirs(self.root_b, exist_ok=True)
        self.reg = os.path.join(self.tmp.name, "projects.json")
        self.own_root = os.path.join(self.tmp.name, "own-data")
        os.makedirs(self.own_root, exist_ok=True)
        self._env = mock.patch.dict(
            os.environ,
            {
                "AGENT_DASH_PROJECTS": self.reg,
                "AGENT_DASH_PROJECT_ID": "aaa",
                "AGENT_DASH_DATA_ROOT": self.own_root,
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.server, self.thread = start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()

    def _write_registry(self, rows):
        import time as _time

        with open(self.reg, "w", encoding="utf-8") as fh:
            json.dump(
                {"projects": [
                    {"id": pid, "name": name, "data_root": root,
                     "port": port, "added_at": int(_time.time())}
                    for pid, name, root, port in rows
                ]},
                fh,
            )

    def _silent_port(self):
        import socket as _socket

        sock = _socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.assertNotEqual(port, 8765)
        return port

    def test_lists_registry_with_current_and_no_paths(self):
        port_a, port_b = self._silent_port(), self._silent_port()
        self._write_registry([
            ("aaa", "Alpha project", self.root_a, port_a),
            ("bbb", "Beta project", self.root_b, port_b),
        ])
        status, _, body = get(self.port, "/api/projects")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["current"], "aaa")
        rows = {row["id"]: row for row in payload["projects"]}
        self.assertEqual(set(rows), {"aaa", "bbb"})
        for row in rows.values():
            self.assertEqual(set(row), {"id", "name", "port", "running", "current"}, row)
        self.assertTrue(rows["aaa"]["current"])
        self.assertFalse(rows["bbb"]["current"])
        # The serving instance reads running even when its own probe
        # is slow: current implies running by construction.
        self.assertTrue(rows["aaa"]["running"])
        self.assertFalse(rows["bbb"]["running"])
        text = body.decode("utf-8", "replace")
        self.assertNotIn(self.root_a, text)
        self.assertNotIn(self.root_b, text)
        self.assertNotIn("data_root", text)

    def test_no_registry_returns_just_this_instance(self):
        status, _, body = get(self.port, "/api/projects")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["projects"]), 1)
        row = payload["projects"][0]
        self.assertEqual(row["id"], "aaa")
        self.assertTrue(row["current"])
        self.assertTrue(row["running"])
        self.assertNotIn("data_root", body.decode("utf-8", "replace"))

    def test_instance_identity_on_project_and_health(self):
        status, _, body = get(self.port, "/api/project")
        self.assertEqual(json.loads(body)["id"], "aaa")
        status, _, body = get(self.port, "/api/health")
        self.assertEqual(json.loads(body)["project_id"], "aaa")


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
        self.assertEqual(main(["check", "--url", "http://127.0.0.1:1", "--hours", "12"]), 5)
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

    def test_restart_succeeds_when_health_reports_problems(self):
        import io
        import time
        from contextlib import redirect_stderr, redirect_stdout

        agent_dash.is_dash = lambda pid: True
        agent_dash.bind_state()
        store = Store(agent_dash.DBFILE)
        now = time.time()
        try:
            store.upsert_run(
                {
                    "run_id": "generic:u1:ws9-unpriced",
                    "source": "generic",
                    "tier": "generic",
                    "model": "demo-model",
                    "label": "ws9-unpriced",
                    "status": "done",
                    "started_at": now - 100,
                    "ended_at": now - 50,
                }
            )
            store.add_usage(
                {
                    "ts": now - 60,
                    "session_id": "u1",
                    "run_id": "generic:u1:ws9-unpriced",
                    "model": "demo-model",
                    "tool": "generic",
                    "message_id": "m1",
                    "input": 100,
                    "output": 50,
                    "cache_read": 0,
                    "cache_write": 0,
                }
            )
        finally:
            store.close()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["restart"])
        self.assertEqual(code, 0)
        self.assertIn("restarted (health reports problems", out.getvalue())
        self.assertIn("doctor", out.getvalue())
        self.assertTrue(agent_dash._health_answers())
        self.assertFalse(agent_dash._health_ok())
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


class IgnoreWorkstreamsServerTest(unittest.TestCase):
    """Ignored ids leave the workstream list and the per-workstream
    breakdown, fold into Other where charts sum, and keep the totals."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.now = time.time()
        self.since = int(self.now - 12 * 3600)
        for label, ws, inp, out in (("ws9-a", "WS9", 100, 50), ("ws345-a", "WS345", 200, 100)):
            run_id = "muse:a:%s" % label
            self.store.upsert_run(
                {
                    "run_id": run_id,
                    "source": "muse",
                    "tier": "muse",
                    "model": "claude-sonnet-4-5",
                    "label": label,
                    "session_id": "sess-%s" % label,
                    "status": "done",
                    "started_at": self.now - 5000,
                    "ended_at": self.now - 4000,
                }
            )
            with self.store._lock:
                self.store._conn.execute(
                    "UPDATE runs SET workstream=? WHERE run_id=?", (ws, run_id)
                )
                self.store._conn.commit()
            self.store.add_usage(
                {
                    "ts": self.now - 4100,
                    "session_id": "sess-%s" % label,
                    "run_id": run_id,
                    "model": "claude-sonnet-4-5",
                    "tool": "muse",
                    "message_id": "m-%s" % label,
                    "input": inp,
                    "output": out,
                    "cache_read": 10,
                    "cache_write": 5,
                }
            )
        (Path(self.tmp.name) / "project.json").write_text(
            json.dumps({"name": "Probe project", "ignore_workstreams": ["WS345"]}),
            encoding="utf-8",
        )
        self._env = mock.patch.dict(os.environ, {"AGENT_DASH_DATA_ROOT": self.tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
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

    def test_workstreams_endpoint_drops_ignored(self):
        body = self.fetch("/api/workstreams")
        ids = [row["ws"] for row in body["workstreams"]]
        self.assertIn("WS9", ids)
        self.assertNotIn("WS345", ids)

    def test_snapshot_drops_breakdown_folds_other_keeps_totals(self):
        payload = self.fetch(f"/api/snapshot?ws=all&since={self.since}&usage_group=workstream")
        self.assertNotIn("WS345", [row["ws"] for row in payload["by_workstream"]])
        self.assertIn("WS9", [row["ws"] for row in payload["by_workstream"]])
        keys = [cell["key"] for cell in payload["usage"]]
        self.assertNotIn("WS345", keys)
        self.assertIn("Other", keys)
        other = sum(
            (cell["input"] or 0) + (cell["output"] or 0)
            + (cell["cache_read"] or 0) + (cell["cache_write"] or 0)
            for cell in payload["usage"] if cell["key"] == "Other"
        )
        self.assertEqual(other, 200 + 100 + 10 + 5)
        billed = payload["totals"]["tokens"]
        total = billed["input"] + billed["output"] + billed["cache_read"] + billed["cache_write"]
        self.assertEqual(total, (100 + 50 + 10 + 5) + (200 + 100 + 10 + 5))
        buckets = sum(
            (cell["input"] or 0) + (cell["output"] or 0)
            + (cell["cache_read"] or 0) + (cell["cache_write"] or 0)
            for cell in payload["usage"]
        )
        self.assertEqual(buckets, total)

    def test_usage_endpoint_folds_ignored(self):
        body = self.fetch("/api/usage?group=workstream")
        keys = [cell["key"] for cell in body["buckets"]]
        self.assertNotIn("WS345", keys)
        self.assertIn("Other", keys)


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


class Prev24hEndpointTest(unittest.TestCase):
    """Snapshot and state carry prev_24h per scope; reconcile checks it."""

    MODEL = "claude-sonnet-5-5"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.now = time.time()
        self._run("muse:win:ws7-win", "ws7-win", "done", self.now - 7200, self.now - 3600, "s-win")
        self._usage(self.now - 3500, "s-win", "muse:win:ws7-win", "m-win", 1000)
        self._run("muse:prev:ws7-prev", "ws7-prev", "done", self.now - 32 * 3600, self.now - 30 * 3600, "s-prev")
        self._usage(self.now - 30 * 3600 + 60, "s-prev", "muse:prev:ws7-prev", "m-prev", 2000)
        self._run("muse:prev:ws1-prev", "ws1-prev", "done", self.now - 42 * 3600, self.now - 40 * 3600, "s-old")
        self._usage(self.now - 41 * 3600, "s-old", "muse:prev:ws1-prev", "m-old", 400)
        self.server, self.thread = start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()

    def _run(self, run_id, label, status, started, ended, session):
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": "muse",
                "tier": "muse",
                "model": self.MODEL,
                "label": label,
                "session_id": session,
                "status": status,
                "started_at": started,
                "ended_at": ended,
            }
        )

    def _usage(self, ts, session, run_id, message, tokens):
        self.store.add_usage(
            {
                "ts": ts,
                "session_id": session,
                "run_id": run_id,
                "model": self.MODEL,
                "tool": "muse",
                "message_id": message,
                "input": tokens,
                "output": tokens // 2,
                "cache_read": 0,
                "cache_write": 0,
            }
        )

    def fetch(self, path):
        status, _, body = get(self.port, path)
        self.assertEqual(status, 200, path)
        return json.loads(body)

    def test_snapshot_carries_prev_24h_with_scope_filter(self):
        payload = self.fetch("/api/snapshot?ws=all")
        self.assertEqual(payload["prev_24h"]["tokens"], 3000 + 600)
        self.assertEqual(payload["prev_24h"]["finished"], 2)
        self.assertEqual(payload["prev_24h"]["live_agents"], 0)
        scoped = self.fetch("/api/snapshot?ws=WS7")
        self.assertEqual(scoped["prev_24h"]["tokens"], 3000)
        self.assertEqual(scoped["prev_24h"]["finished"], 1)
        self.assertEqual(len(scoped["by_workstream"]), 1)
        self.assertEqual(scoped["by_workstream"][0]["prev_24h"], scoped["prev_24h"])

    def test_state_totals_and_rows_carry_prev_24h(self):
        payload = self.fetch("/api/state")
        self.assertEqual(payload["totals"]["prev_24h"]["tokens"], 3000 + 600)
        by_id = {row["id"]: row for row in payload["workstreams"]}
        self.assertEqual(by_id["WS7"]["prev_24h"]["tokens"], 3000)
        self.assertEqual(by_id["WS1"]["prev_24h"]["finished"], 1)

    def test_reconcile_carries_prev_24h_without_mismatch(self):
        payload = self.fetch("/api/reconcile?hours=24")
        self.assertEqual(payload["all"]["prev_24h"]["tokens"], 3000 + 600)
        self.assertFalse(
            [item for item in payload["mismatches"] if "prev_24h" in item],
            payload["mismatches"],
        )
class BuildInfoEndpointTest(unittest.TestCase):
    """Safe deploy: /api/health and /api/state carry the captured build."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.server, self.thread = start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()

    def test_health_carries_captured_build(self):
        status, _, body = get(self.port, "/api/health")
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertEqual(health["build_id"], self.server.build["build_id"])
        self.assertEqual(health["started_at"], self.server.build["started_at"])
        self.assertTrue(health["build_id"])

    def test_state_version_carries_build(self):
        status, _, body = get(self.port, "/api/state")
        self.assertEqual(status, 200)
        version = json.loads(body)["version"]
        self.assertEqual(version["build_id"], self.server.build["build_id"])
        self.assertEqual(version["started_at"], self.server.build["started_at"])
        self.assertTrue(version["tool"])
        self.assertIn("schema", version)

    def test_explicit_build_is_served_verbatim(self):
        stop(self.server, self.thread)
        from dash.server import DashServer

        self.server = DashServer(
            self.store,
            Registry([]),
            "127.0.0.1",
            0,
            cfg=default_cfg(),
            build={"build_id": "cafef00d", "started_at": 1700000001.5},
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.port = self.server.server_address[1]
        status, _, body = get(self.port, "/api/health")
        health = json.loads(body)
        self.assertEqual(health["build_id"], "cafef00d")
        self.assertEqual(health["started_at"], 1700000001.5)
        status, _, body = get(self.port, "/api/state")
        version = json.loads(body)["version"]
        self.assertEqual(version["build_id"], "cafef00d")
        self.assertEqual(version["started_at"], 1700000001.5)


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
                "model": "muse-spark-1.3-contributor",
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


class DoctorApiTest(unittest.TestCase):
    def setUp(self):
        from unittest import mock

        self._live_patch = mock.patch("dash.server._live_workers", return_value=[])
        self._live_patch.start()
        self.addCleanup(self._live_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.now = time.time()
        self.store.doctor_snapshot(
            [
                {
                    "id": "unpriced-model:global:mystery-9", "code": "unpriced-model",
                    "severity": "critical", "scope": "global", "ws": None,
                    "title": "Model mystery-9 has usage but no price",
                    "detail": "model 'mystery-9' has 3 usage rows (900 tokens in 24 h) with no price",
                    "fix_command": "add a row to tools/agent-dash/catalog/prices.json, then run: python3 agent_dash.py reprice",
                    "safe_auto": True,
                },
                {
                    "id": "stuck-task:ws:WS9:WS9.1", "code": "stuck-task",
                    "severity": "warn", "scope": "ws:WS9", "ws": "WS9",
                    "title": "Task WS9.1 is stuck in wip with no live agent",
                    "detail": "task WS9.1 in WS9 has status wip with no live agent",
                    "fix_command": "python3 agent_dash.py ws plan WS9",
                    "safe_auto": False,
                },
            ],
            self.now,
        )
        self.server, self.thread = start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()

    def test_doctor_shape_exact(self):
        status, _, body = get(self.port, "/api/doctor")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(
            set(payload),
            {"generated_at", "next_run_at", "interval_s", "summary", "problems", "resolved_24h"},
        )
        self.assertEqual(
            payload["summary"], {"critical": 1, "warn": 1, "info": 0, "regressed": 0}
        )
        self.assertEqual(payload["interval_s"], 300.0)
        self.assertEqual(len(payload["problems"]), 2)
        for item in payload["problems"]:
            self.assertEqual(
                set(item),
                {"id", "code", "severity", "scope", "ws", "title", "detail",
                 "fix", "first_seen", "last_seen", "count", "regressed"},
            )
            self.assertEqual(
                set(item["fix"]), {"command", "safe_auto", "doc"}
            )
        by_id = {item["id"]: item for item in payload["problems"]}
        self.assertTrue(by_id["unpriced-model:global:mystery-9"]["fix"]["safe_auto"])
        self.assertEqual(
            by_id["stuck-task:ws:WS9:WS9.1"]["fix"]["doc"], "docs/problems/stuck-task.md"
        )

    def test_doctor_filters(self):
        status, _, body = get(self.port, "/api/doctor?ws=WS9")
        ids = {item["id"] for item in json.loads(body)["problems"]}
        self.assertEqual(ids, {"unpriced-model:global:mystery-9", "stuck-task:ws:WS9:WS9.1"})
        status, _, body = get(self.port, "/api/doctor?ws=WS1")
        ids = {item["id"] for item in json.loads(body)["problems"]}
        self.assertEqual(ids, {"unpriced-model:global:mystery-9"})
        status, _, body = get(self.port, "/api/doctor?severity=critical")
        ids = {item["id"] for item in json.loads(body)["problems"]}
        self.assertEqual(ids, {"unpriced-model:global:mystery-9"})
        status, _, body = get(self.port, "/api/doctor?severity=bogus")
        self.assertEqual(status, 400)
        status, _, _ = get(self.port, "/api/doctor?ws=..")
        self.assertEqual(status, 400)

    def test_doctor_never_runs_inline(self):
        status, _, before = get(self.port, "/api/doctor")
        first = json.loads(before)["generated_at"]
        status, _, _ = get(self.port, "/api/doctor?refresh=1")
        self.assertEqual(status, 200)
        status, _, after = get(self.port, "/api/doctor")
        self.assertEqual(json.loads(after)["generated_at"], first)

    def test_doctor_empty_before_first_run(self):
        stop(self.server, self.thread)
        fresh = Store(Path(self.tmp.name) / "empty.sqlite")
        self.addCleanup(fresh.close)
        self.server, self.thread = start(fresh)
        self.port = self.server.server_address[1]
        status, _, body = get(self.port, "/api/doctor")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIsNone(payload["generated_at"])
        self.assertEqual(payload["problems"], [])
        self.assertEqual(
            payload["summary"], {"critical": 0, "warn": 0, "info": 0, "regressed": 0}
        )

    def test_health_carries_doctor_and_fails_on_critical(self):
        status, _, body = get(self.port, "/api/health")
        health = json.loads(body)
        self.assertIn("doctor", health)
        self.assertEqual(health["doctor"]["summary"]["critical"], 1)
        self.assertFalse(health["ok"])
        self.store.doctor_snapshot([], time.time())
        # A background write keeps the TTL generation within the window,
        # then the invalidated entry recomputes.
        status, _, body = get(self.port, "/api/health")
        health = json.loads(body)
        self.assertEqual(health["doctor"]["summary"]["critical"], 1)
        self.server.respcache.invalidate_all()
        status, _, body = get(self.port, "/api/health")
        health = json.loads(body)
        self.assertEqual(health["doctor"]["summary"]["critical"], 0)

    def test_state_problems_match_doctor_ids(self):
        status, _, body = get(self.port, "/api/doctor")
        doctor_ids = [item["id"] for item in json.loads(body)["problems"]]
        status, _, body = get(self.port, "/api/state")
        state_ids = [item["id"] for item in json.loads(body)["problems"]]
        self.assertEqual(state_ids, doctor_ids)
        for item in json.loads(body)["problems"]:
            self.assertIn("message", item)
            self.assertIn("command", item)


class ArchiveReportResolutionTest(unittest.TestCase):
    """Archive cards name the closing report by basename, wherever it lives.

    ``ws archive`` requires ``docs/closing-report.md``; the card must find it
    there with no ``reports/`` dir, and must never surface a path or body.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _archived(self, ws, summary=None):
        bundle = self.root / ws
        bundle.mkdir(parents=True)
        (bundle / "bundle.json").write_text(json.dumps(
            {"id": ws, "title": "Probe " + ws, "state": "archived", "archived_at": 100}),
            encoding="utf-8")
        if summary is not None:
            (bundle / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        return bundle

    def _report(self, ws):
        from dash.workstates import archive_cards, all_bundle_meta

        cards = archive_cards(all_bundle_meta(self.root), self.root)
        return next(card["report"] for card in cards if card["id"] == ws)

    def test_docs_closing_report_without_reports_dir(self):
        bundle = self._archived("WS51", {"ws": "WS51", "closing_report": True})
        (bundle / "docs").mkdir()
        (bundle / "docs" / "closing-report.md").write_text("SECRET BODY\n", encoding="utf-8")
        self.assertFalse((bundle / "reports").exists())
        self.assertEqual(self._report("WS51"), "closing-report.md")

    def test_frozen_name_wins_when_file_exists(self):
        bundle = self._archived("WS52", {"ws": "WS52", "closing_report": True,
                                        "closing_report_name": "final.md"})
        (bundle / "docs").mkdir()
        (bundle / "docs" / "final.md").write_text("body\n", encoding="utf-8")
        (bundle / "docs" / "closing-report.md").write_text("body\n", encoding="utf-8")
        self.assertEqual(self._report("WS52"), "final.md")

    def test_frozen_name_without_file_falls_back(self):
        bundle = self._archived("WS53", {"ws": "WS53", "closing_report": True,
                                        "closing_report_name": "gone.md"})
        (bundle / "docs").mkdir()
        (bundle / "docs" / "closing-report.md").write_text("body\n", encoding="utf-8")
        self.assertEqual(self._report("WS53"), "closing-report.md")

    def test_frozen_name_is_reduced_to_basename(self):
        self._archived("WS54", {"ws": "WS54", "closing_report_name": "../../secret/closing.md"})
        self.assertIsNone(self._report("WS54"))

    def test_reports_glob_still_resolves(self):
        bundle = self._archived("WS55")
        (bundle / "reports").mkdir()
        (bundle / "reports" / "2026-10-17-closing.md").write_text("body\n", encoding="utf-8")
        self.assertEqual(self._report("WS55"), "2026-10-17-closing.md")

    def test_neither_gives_no_report(self):
        self._archived("WS56", {"ws": "WS56", "closing_report": False})
        self.assertIsNone(self._report("WS56"))


class WorkstreamStatesApiTest(unittest.TestCase):
    """Lifecycle states on the API: scoped workstream rows plus /api/archive.

    Synthetic bundles cover every state (WS41 planned, WS42 active, WS43
    paused, WS44 done, WS45+WS47 archived, WS46 dropped), each with one
    priced run in the store.
    """

    MODEL = "claude-sonnet-5-5"
    BUNDLES = {
        "WS41": {"state": "planned", "depends_on": ["WS42"], "milestone": "Part 2"},
        "WS42": {"state": "active"},
        "WS43": {"state": "paused"},
        "WS44": {"state": "done"},
        "WS45": {"state": "archived", "archived_at": "old"},
        "WS46": {"state": "dropped", "archived_at": "old"},
        "WS47": {"state": "archived", "archived_at": "new"},
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.wsroot = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.wsroot)
        for ws, fields in self.BUNDLES.items():
            bundle = {"id": ws, "title": "Probe %s" % ws, "short": ws,
                      "order": int(ws[2:]), "kind": "workstream"}
            if fields["state"] != "active":
                bundle["state"] = fields["state"]
            bundle["state_changed_at"] = int(self.now) - 50
            if "archived_at" in fields:
                bundle["archived_at"] = int(self.now) - (1000 if fields["archived_at"] == "old" else 100)
            if "depends_on" in fields:
                bundle["depends_on"] = fields["depends_on"]
            if "milestone" in fields:
                bundle["milestone"] = fields["milestone"]
            dest = os.path.join(self.wsroot, ws)
            os.makedirs(dest)
            with open(os.path.join(dest, "bundle.json"), "w") as fh:
                json.dump(bundle, fh)
        for ws in ("WS45", "WS47"):
            summary = {"tasks_done": 5, "tasks_total": 5, "runs": 9,
                       "tokens": 1500, "cost_usd": 0.01, "trains_merged": 1}
            if ws == "WS45":
                summary["report"] = "reports/2026-10-18-closing.md"
            with open(os.path.join(self.wsroot, ws, "summary.json"), "w") as fh:
                json.dump(summary, fh)
        reports = os.path.join(self.wsroot, "WS47", "reports")
        os.makedirs(reports)
        with open(os.path.join(reports, "2026-10-17-closing.md"), "w") as fh:
            fh.write("# closing notes for probe WS47\n")
        self._env = mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": self.wsroot})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._live = mock.patch("dash.server._live_workers", return_value=[])
        self._live.start()
        self.addCleanup(self._live.stop)
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        for ws in sorted(self.BUNDLES):
            num = ws[2:]
            self.store.upsert_run(
                {
                    "run_id": "muse:s%d:ws%s-probe" % (int(num), num),
                    "source": "muse",
                    "tier": "muse",
                    "model": self.MODEL,
                    "label": "ws%s-WS%s.1-probe" % (num, num),
                    "session_id": "sess-%s" % num,
                    "status": "done",
                    "started_at": self.now - 7200,
                    "ended_at": self.now - 3600,
                }
            )
            self.store.add_usage(
                {
                    "ts": self.now - 3500,
                    "session_id": "sess-%s" % num,
                    "run_id": "muse:s%d:ws%s-probe" % (int(num), num),
                    "model": self.MODEL,
                    "tool": "muse",
                    "message_id": "m-%s" % num,
                    "input": 1000,
                    "output": 500,
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

    def _rows(self, path):
        status, _, body = get(self.port, path)
        self.assertEqual(status, 200, path)
        return json.loads(body)["workstreams"]

    def test_default_scope_drops_archived_and_dropped(self):
        rows = {row["ws"]: row for row in self._rows("/api/workstreams")}
        self.assertEqual(
            sorted(rows),
            ["WS41", "WS42", "WS43", "WS44"],
        )
        self.assertEqual(rows["WS41"]["state"], "planned")
        self.assertEqual(rows["WS41"]["depends_on"], ["WS42"])
        self.assertEqual(rows["WS41"]["milestone"], "Part 2")
        self.assertEqual(rows["WS42"]["state"], "active")
        self.assertEqual(rows["WS43"]["state"], "paused")
        self.assertEqual(rows["WS44"]["state"], "done")
        for row in rows.values():
            for key in ("state_changed_at", "archived_at", "depends_on", "milestone",
                        "live_agents", "tokens", "cost_usd", "tokens_all_time",
                        "cost_usd_all_time", "last_activity", "progress", "ws_confidence"):
                self.assertIn(key, row, key)
            self.assertEqual(row["tokens"], 1500)
        self.assertIsNone(rows["WS42"]["archived_at"])
        self.assertIsNotNone(rows["WS41"]["state_changed_at"])

    def test_state_param_overrides_scope(self):
        rows = {row["ws"]: row for row in self._rows("/api/workstreams?state=all")}
        self.assertEqual(
            sorted(rows),
            ["WS41", "WS42", "WS43", "WS44", "WS45", "WS46", "WS47"],
        )
        self.assertEqual(rows["WS45"]["state"], "archived")
        self.assertEqual(rows["WS46"]["state"], "dropped")
        self.assertIsNotNone(rows["WS45"]["archived_at"])
        only = {row["ws"] for row in self._rows("/api/workstreams?state=archived")}
        self.assertEqual(only, {"WS45", "WS47"})
        subset = {row["ws"] for row in self._rows("/api/workstreams?state=done,planned")}
        self.assertEqual(subset, {"WS41", "WS44"})

    def test_bad_state_is_400(self):
        status, _, body = get(self.port, "/api/workstreams?state=sleeping")
        self.assertEqual(status, 400)
        self.assertIn("state", json.loads(body)["hint"])

    def test_archive_cards_newest_first_with_totals(self):
        status, _, body = get(self.port, "/api/archive")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual([card["id"] for card in payload["archive"]], ["WS47", "WS45"])
        older = payload["archive"][1]
        self.assertEqual(older["tasks_done"], 5)
        self.assertEqual(older["report"], "2026-10-18-closing.md")
        self.assertEqual(older["title"], "Probe WS45")
        self.assertIsNotNone(older["archived_at"])
        newer = payload["archive"][0]
        self.assertEqual(newer["report"], "2026-10-17-closing.md")
        totals = payload["totals"]
        self.assertEqual(totals["workstreams"], 2)
        self.assertEqual(totals["tokens"], 3000)
        self.assertEqual(totals["runs"], 2)
        self.assertGreater(totals["cost_usd"], 0)

    def test_reconcile_carries_archived_aggregate(self):
        status, _, body = get(self.port, "/api/reconcile?hours=24")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        archived = payload["archived"]
        self.assertEqual(sorted(archived["workstreams"]), ["WS45", "WS46", "WS47"])
        self.assertEqual(archived["tokens"], 4500)
        self.assertEqual(archived["tokens_all_time"], 4500)
        visible = [row for row in payload["rows"] if row["ws"] not in set(archived["workstreams"])]
        self.assertEqual(
            sum(row["tokens"] for row in visible) + archived["tokens"],
            payload["all"]["tokens"],
        )
        self.assertFalse(
            [item for item in payload["mismatches"] if item.startswith("archived")],
            payload["mismatches"],
        )

    def test_state_rows_carry_lifecycle_and_project_placeholder(self):
        status, _, body = get(self.port, "/api/state")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["project"], {"name": None})
        by_id = {row["id"]: row for row in payload["workstreams"]}
        self.assertEqual(by_id["WS45"]["state"], "archived")
        self.assertEqual(by_id["WS42"]["state"], "active")
        self.assertEqual(
            sorted(payload["totals"]["archived"]["workstreams"]),
            ["WS45", "WS46", "WS47"],
        )


class DecisionsRisksApiTest(unittest.TestCase):
    """Decisions and risks endpoints over a synthetic hub dir, read-only.
    WS41 is active with a stale plan gate; WS42 is planned waiting on
    WS41; the hub holds two decisions and one filed risk.
    """
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.wsroot = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.wsroot)
        for ws, state in (("WS41", "active"), ("WS42", "planned")):
            dest = os.path.join(self.wsroot, ws)
            os.makedirs(dest)
            bundle = {"id": ws, "title": "Probe %s" % ws, "short": ws,
                      "order": int(ws[2:]), "kind": "workstream",
                      "state_changed_at": int(self.now) - 50}
            if state != "active":
                bundle["state"] = state
            if ws == "WS42":
                bundle["depends_on"] = ["WS41"]
            with open(os.path.join(dest, "bundle.json"), "w") as fh:
                json.dump(bundle, fh)
        plan = {
            "dag": {
                "cols": ["Build"],
                "groups": [
                    {"id": "build", "col": 0, "title": "Build", "tasks": [
                        {"id": "WS41.1", "title": "Wait for the freeze",
                         "status": "todo", "deps": [], "kind": "gate",
                         "signal": "freeze lifts", "fresh_hours": 1}
                    ]},
                ],
                "edges": [],
            },
            "narrative": {"thesis": "t", "end_state": "e", "phases": []},
        }
        with open(os.path.join(self.wsroot, "WS41", "plan.json"), "w") as fh:
            json.dump(plan, fh)
        self.hub = os.path.join(self.tmp.name, "hub")
        os.makedirs(self.hub)
        with open(os.path.join(self.hub, "decisions.tsv"), "w") as fh:
            fh.write("date\tid\tdecision\tstatus\n"
                     "2026-10-14\tD-1\told choice\taccepted\n"
                     "2026-10-16\tD-2\tnew choice\tproposed\n")
        with open(os.path.join(self.hub, "risks.tsv"), "w") as fh:
            fh.write("id\ttitle\tseverity\nR-1\tfiled risk\tinfo\n")
        self._env = mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": self.wsroot})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._live = mock.patch("dash.server._live_workers", return_value=[])
        self._live.start()
        self.addCleanup(self._live.stop)
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        cfg = default_cfg()
        cfg["hub_dir"] = self.hub
        registry = Registry([])
        server = DashServer(self.store, registry, "127.0.0.1", 0, cfg=cfg)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        self.server, self.thread = server, thread
        self.port = self.server.server_address[1]
    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()
    def test_decisions_newest_first_with_counts(self):
        status, _, body = get(self.port, "/api/decisions")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual([row["id"] for row in payload["decisions"]], ["D-2", "D-1"])
        self.assertEqual(payload["decisions"][0]["title"], "new choice")
        self.assertEqual(payload["counts"], {"proposed": 1, "accepted": 1})
        self.assertEqual(payload["source"], {"present": True})
    def test_risks_computed_then_filed(self):
        status, _, body = get(self.port, "/api/risks")
        self.assertEqual(status, 200)
        risks = json.loads(body)["risks"]
        by_id = {risk["id"]: risk for risk in risks}
        self.assertIn("gates:WS41:WS41.1", by_id)
        self.assertIn("dependency:WS42", by_id)
        self.assertEqual(risks[-1]["id"], "R-1")
        for risk in risks:
            for key in ("id", "title", "severity", "source", "ws", "since"):
                self.assertIn(key, risk, risk.get("id"))
            if risk["source"] != "file":
                self.assertTrue(risk["title"].startswith(risk["ws"]), risk["title"])
    def test_state_carries_decisions_and_risks(self):
        status, _, body = get(self.port, "/api/state")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["decisions"], {"total": 2, "open": 1})
        self.assertEqual(payload["risks"]["total"], 3)
        self.assertEqual(
            sum(payload["risks"]["by_severity"].values()),
            payload["risks"]["total"],
        )
class FeedEndpointTest(unittest.TestCase):
    """GET /api/project/feed over synthetic bundles: history moves,
    decided approvals, archive lines, run summaries, and limit rules."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.wsroot = os.path.join(self.tmp.name, "ws")
        minute = int((self.now - 600) // 60) * 60
        histories = {
            "WS51": [
                {"ts": minute + 1, "task": "WS51.1", "from": "wip", "to": "done"},
                {"ts": minute + 2, "task": "WS51.2", "from": "wip", "to": "done"},
                {"ts": self.now - 9000},
                {"task": "WS51.9", "to": "done"},
                {"kind": "state", "ts": self.now - 100, "ws": "WS51",
                 "from": "planned", "to": "active"},
            ],
            "WS52": [
                {"ts": minute + 3, "task": "WS52.1", "from": "pr", "to": "done"},
                {"kind": "state", "ts": self.now - 50, "ws": "WS52",
                 "from": "done", "to": "archived"},
            ],
        }
        for ws, rows in histories.items():
            dest = os.path.join(self.wsroot, ws)
            os.makedirs(dest)
            with open(os.path.join(dest, "bundle.json"), "w") as fh:
                json.dump(
                    {"id": ws, "title": "Probe %s" % ws, "short": ws,
                     "order": 51, "kind": "workstream", "state": "active"},
                    fh,
                )
            with open(os.path.join(dest, "history.jsonl"), "w") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")
        with open(os.path.join(self.wsroot, "WS51", "approvals.json"), "w") as fh:
            json.dump(
                {"ws": "WS51", "decided_at": "2026-04-02T15:10:00Z",
                 "decided_by": "owner",
                 "items": [{"id": "go", "step": "Ship the probe",
                            "status": "approved", "reversible": True,
                            "conditions": "Seeded."}]},
                fh,
            )
        self._env = mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": self.wsroot})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws52-probe",
                "source": "muse",
                "tier": "muse",
                "label": "ws52-WS52.1-probe",
                "status": "done",
                "verdict": "PASS",
                "started_at": self.now - 400,
                "ended_at": self.now - 300,
            }
        )
        self.server, self.thread = start(self.store)
        self.port = self.server.server_address[1]
    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()
    def fetch(self, path):
        status, content_type, body = get(self.port, path)
        return status, content_type, json.loads(body)
    def test_feed_shape_order_and_collapse(self):
        status, content_type, payload = self.fetch("/api/project/feed")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        self.assertEqual(payload["limit"], 50)
        kinds = [line["kind"] for line in payload["feed"]]
        self.assertIn("tasks", kinds)
        self.assertIn("approval", kinds)
        self.assertIn("archive", kinds)
        self.assertIn("run", kinds)
        stamps = [line["ts"] for line in payload["feed"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertEqual(payload["total"], len(payload["feed"]))
        bulk = [line for line in payload["feed"] if line["kind"] == "tasks"][0]
        self.assertEqual(
            bulk["text"],
            "3 tasks moved to done across Probe WS51 (WS51) (2), Probe WS52 (WS52) (1)",
        )
        for line in payload["feed"]:
            for key in ("ts", "day", "ws", "name", "kind", "text", "target"):
                self.assertIn(key, line, key)
            self.assertNotIn(line["kind"], ("doctor", "health"))
            self.assertNotIn("doctor", line["text"].lower())
            self.assertNotIn("health", line["text"].lower())
        self.assertIsNotNone(payload["earliest_ts"])
    def test_feed_limit_rules(self):
        _, _, payload = self.fetch("/api/project/feed?limit=2")
        self.assertEqual(payload["limit"], 2)
        self.assertEqual(len(payload["feed"]), 2)
        _, _, payload = self.fetch("/api/project/feed?limit=500")
        self.assertEqual(payload["limit"], 200)
        status, _, body = get(self.port, "/api/project/feed?limit=0")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "bad_request")
        status, _, _ = get(self.port, "/api/project/feed?limit=nope")
        self.assertEqual(status, 400)
    def test_feed_empty_root(self):
        with tempfile.TemporaryDirectory() as empty:
            with mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": empty}):
                status, _, payload = self.fetch("/api/project/feed")
        self.assertEqual(status, 200)
        kinds = [line["kind"] for line in payload["feed"]]
        self.assertNotIn("task", kinds)
        self.assertNotIn("tasks", kinds)
        self.assertNotIn("state", kinds)
        self.assertNotIn("approval", kinds)
        self.assertIn("run", kinds)
        self.assertIn("first-run", kinds)
    def test_feed_empty_root_and_store(self):
        store = Store(Path(self.tmp.name) / "empty.sqlite")
        server, thread = start(store)
        try:
            with tempfile.TemporaryDirectory() as empty:
                with mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": empty}):
                    status, _, body = get(server.server_address[1], "/api/project/feed")
        finally:
            stop(server, thread)
            store.close()
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["feed"], [])
        self.assertEqual(payload["total"], 0)
        self.assertIsNone(payload["earliest_ts"])
class ProjectGraphApiTest(unittest.TestCase):
    """GET /api/project/graph over synthetic bundles: every edge kind, a
    cycle, stale gates, moved tasks, and archived nodes in one payload."""
    MODEL = "claude-sonnet-5-5"
    def _bundle(self, ws, state="active", depends_on=None, milestone=None):
        bundle = {"id": ws, "title": "Probe %s" % ws, "short": ws,
                  "order": int(ws[2:]), "kind": "workstream"}
        if state != "active":
            bundle["state"] = state
        bundle["state_changed_at"] = int(self.now) - 50
        if depends_on:
            bundle["depends_on"] = depends_on
        if milestone:
            bundle["milestone"] = milestone
        dest = os.path.join(self.wsroot, ws)
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "bundle.json"), "w") as fh:
            json.dump(bundle, fh)
    def _plan(self, ws, tasks):
        doc = {"dag": {"cols": ["Build"], "groups": [
            {"id": "build", "col": 0, "title": "Build", "tasks": tasks}]},
               "narrative": {"thesis": "probe", "end_state": "done",
                             "phases": []}, "trains": []}
        with open(os.path.join(self.wsroot, ws, "plan.json"), "w") as fh:
            json.dump(doc, fh)
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.wsroot = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.wsroot)
        self._bundle("WS51", state="planned", depends_on=["WS52"], milestone="Part 2")
        self._bundle("WS52")
        self._bundle("WS53", state="done")
        self._bundle("WS54", state="planned", depends_on=["WS53"])
        self._bundle("WS55")
        self._bundle("WS56")
        self._bundle("WS57", depends_on=["WS58"])
        self._bundle("WS58", depends_on=["WS57"])
        self._bundle("WS59", state="archived")
        self._plan("WS51", [
            {"id": "WS51.1", "title": "First probe task", "status": "todo", "deps": []},
        ])
        self._plan("WS54", [
            {"id": "WS54.G1", "title": "Signal batch is merged", "status": "todo",
             "kind": "gate", "signal": "WS52 signal batch is merged",
             "confirmed_at": "2020-01-01T00:00:00Z", "fresh_hours": 24},
            {"id": "WS54.G2", "title": "Handoff is complete", "status": "done",
             "kind": "gate", "signal": "WS53 handoff is complete"},
        ])
        self._plan("WS55", [
            {"id": "WS55.1", "title": "Shared probe task", "status": "deferred",
             "moved_to": "WS52", "received_as": "WS52.9", "note": "handed over"},
        ])
        self._plan("WS56", [
            {"id": "WS56.1", "title": "Reconcile the cold archive", "status": "deferred",
             "moved_to": "WS52", "note": "nobody picked this up"},
        ])
        self._plan("WS52", [
            {"id": "WS52.9", "title": "Shared probe task follow-up", "status": "todo", "deps": []},
        ])
        self._env = mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": self.wsroot})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._live = mock.patch("dash.server._live_workers", return_value=[])
        self._live.start()
        self.addCleanup(self._live.stop)
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.store.upsert_run(
            {"run_id": "muse:s51:ws51-probe", "source": "muse", "tier": "muse",
             "model": self.MODEL, "label": "ws51-WS51.1-probe",
             "session_id": "sess-51", "status": "done",
             "started_at": self.now - 7200, "ended_at": self.now - 3600}
        )
        self.server, self.thread = start(self.store)
        self.port = self.server.server_address[1]
    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()
    def test_graph_payload_shape(self):
        status, ctype, body = get(self.port, "/api/project/graph")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        payload = json.loads(body)
        self.assertEqual(set(payload), {"nodes", "edges", "cycles", "generated_at"})
        nodes = {node["id"]: node for node in payload["nodes"]}
        self.assertEqual(sorted(nodes),
                         ["WS51", "WS52", "WS53", "WS54", "WS55", "WS56", "WS57", "WS58", "WS59"])
        for node in nodes.values():
            self.assertEqual(set(node),
                             {"id", "title", "state", "progress", "live_agents", "milestone", "depth"})
        self.assertEqual(nodes["WS51"]["state"], "planned")
        self.assertEqual(nodes["WS51"]["milestone"], "Part 2")
        self.assertEqual(nodes["WS53"]["state"], "done")
        self.assertEqual(nodes["WS59"]["state"], "archived")
        self.assertEqual(nodes["WS51"]["progress"]["done"], 0)
        self.assertEqual(nodes["WS51"]["progress"]["total"], 1)
    def test_graph_edges_and_states(self):
        status, _, body = get(self.port, "/api/project/graph")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        by_key = {(edge["from"], edge["to"], edge["kind"]): edge for edge in payload["edges"]}
        for edge in payload["edges"]:
            self.assertEqual(set(edge) >= {"from", "to", "kind", "status", "condition"}, True)
            self.assertIn(edge["kind"], ("dependency", "gate", "moved"))
            self.assertIn(edge["status"], ("met", "waiting", "stale"))
        self.assertEqual(by_key[("WS52", "WS51", "dependency")]["status"], "waiting")
        self.assertEqual(by_key[("WS53", "WS54", "dependency")]["status"], "met")
        self.assertEqual(by_key[("WS52", "WS54", "gate")]["status"], "stale")
        self.assertEqual(by_key[("WS53", "WS54", "gate")]["status"], "met")
        self.assertEqual(by_key[("WS55", "WS52", "moved")]["status"], "met")
        self.assertEqual(by_key[("WS56", "WS52", "moved")]["status"], "waiting")
    def test_graph_reports_cycles_and_layers(self):
        status, _, body = get(self.port, "/api/project/graph")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn(["WS57", "WS58"], payload["cycles"])
        depths = {node["id"]: node["depth"] for node in payload["nodes"]}
        self.assertEqual(depths["WS53"], 0)
        self.assertEqual(depths["WS55"], 0)
        self.assertEqual(depths["WS56"], 0)
        self.assertEqual(depths["WS52"], 1)
        self.assertEqual(depths["WS51"], 2)
        self.assertEqual(depths["WS54"], 2)
    def test_graph_carries_no_paths_or_bodies(self):
        status, _, body = get(self.port, "/api/project/graph")
        self.assertEqual(status, 200)
        text = body.decode("utf-8", "replace")
        self.assertNotIn(self.wsroot, text)
        self.assertNotIn(".md", text)
        self.assertNotIn("reports/", text)
class TimelineEndpointTest(unittest.TestCase):
    """GET /api/project/timeline: one lane per workstream from recorded evidence.
    Synthetic bundles cover the lane shapes (WS41 planned with an unmet
    dep, WS42 active with runs plus history, WS43 done via a state move,
    WS44 active with no dates at all), each with priced runs in the store.
    """
    MODEL = "claude-sonnet-5-5"
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.wsroot = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.wsroot)
        bundles = {
            "WS41": {"state": "planned", "depends_on": ["WS42"], "milestone": "Second wave"},
            "WS42": {"state": "active"},
            "WS43": {"state": "done"},
            "WS44": {"state": "active"},
        }
        for ws, fields in bundles.items():
            bundle = {"id": ws, "title": "Probe %s" % ws, "short": ws,
                      "order": int(ws[2:]), "kind": "workstream"}
            for key in ("state", "depends_on", "milestone"):
                if key in fields:
                    bundle[key] = fields[key]
            bundle["state_changed_at"] = int(self.now) - 50
            dest = os.path.join(self.wsroot, ws)
            os.makedirs(dest)
            with open(os.path.join(dest, "bundle.json"), "w") as fh:
                json.dump(bundle, fh)
        plans = {
            "WS42": [("WS42.1", "done"), ("WS42.2", "todo")],
            "WS43": [("WS43.1", "done")],
        }
        for ws, tasks in plans.items():
            plan = {"dag": {"cols": ["Build"], "groups": [
                {"id": "build", "col": 0, "title": "Build",
                 "tasks": [{"id": tid, "title": "Probe %s" % tid, "status": status} for tid, status in tasks]}],
                "edges": []}, "narrative": {}}
            with open(os.path.join(self.wsroot, ws, "plan.json"), "w") as fh:
                json.dump(plan, fh)
        history = {
            "WS42": [
                {"ts": self.now - 4000, "task": "WS42.1", "from": "todo", "to": "done"},
                {"ts": self.now - 3000, "task": "WS42.2", "from": "todo", "to": "wip"},
            ],
            "WS43": [
                {"ts": self.now - 5000, "task": "WS43.1", "from": "todo", "to": "done"},
                {"kind": "state", "ts": self.now - 200, "ws": "WS43", "from": "active", "to": "done"},
            ],
        }
        for ws, rows in history.items():
            with open(os.path.join(self.wsroot, ws, "history.jsonl"), "w") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")
        with open(os.path.join(self.wsroot, "project.json"), "w") as fh:
            json.dump({"name": "Probe project", "started": "2025-10-01", "target": None,
                       "phases": [{"title": "First wave", "state": "done"}]}, fh)
        self._env = mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": self.wsroot})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._live = mock.patch("dash.server._live_workers", return_value=[])
        self._live.start()
        self.addCleanup(self._live.stop)
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        runs = [
            ("41", self.now - 5000, self.now - 4000),
            ("42", self.now - 7200, self.now - 3600),
            ("42b", self.now - 7100, self.now - 3500),
            ("43", self.now - 9000, self.now - 8000),
        ]
        for num, started, ended in runs:
            run_id = "muse:s%s:ws%s-probe" % (num, num)
            self.store.upsert_run(
                {
                    "run_id": run_id,
                    "source": "muse",
                    "tier": "muse",
                    "model": self.MODEL,
                    "label": "ws%s-WS%s.1-probe" % (num, num),
                    "session_id": "sess-%s" % num,
                    "status": "done",
                    "started_at": started,
                    "ended_at": ended,
                }
            )
            self.store.add_usage(
                {
                    "ts": ended - 10,
                    "session_id": "sess-%s" % num,
                    "run_id": run_id,
                    "model": self.MODEL,
                    "tool": "muse",
                    "message_id": "m-%s" % run_id,
                    "input": 1000,
                    "output": 500,
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
    def _payload(self):
        status, ctype, body = get(self.port, "/api/project/timeline")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        return json.loads(body)
    def test_lanes_come_from_recorded_evidence(self):
        payload = self._payload()
        self.assertEqual(
            payload["order"], ["WS43", "WS42", "WS44", "WS41"]
        )
        by_id = {lane["id"]: lane for lane in payload["lanes"]}
        self.assertEqual(sorted(by_id), ["WS41", "WS42", "WS43", "WS44"])
        active = by_id["WS42"]
        self.assertEqual(active["started_at"], self.now - 7200)
        self.assertIsNone(active["done_at"])
        self.assertFalse(active["before_history"])
        self.assertEqual(active["progress"], {"done": 1, "total": 2, "pct": 50})
        self.assertEqual(active["runs"], 2)
        self.assertEqual(active["tokens"], 3000)
        done = by_id["WS43"]
        self.assertEqual(done["done_at"], self.now - 200)
        self.assertFalse(done["done_inferred"])
        band = by_id["WS44"]
        self.assertTrue(band["before_history"])
        self.assertIsNone(band["started_at"])
        planned = by_id["WS41"]
        self.assertFalse(planned["before_history"])
        self.assertEqual(planned["estimate_days"], 7)
        self.assertEqual(planned["blocked_by"], ["WS42"])
        self.assertEqual(planned["milestone"], "Second wave")
        self.assertEqual(payload["history_floor"], self.now - 5000)
        self.assertEqual(payload["project"]["started"], "2025-10-01")
        self.assertIsNone(payload["project"]["target"])
        self.assertEqual(payload["project"]["phases"], [{"title": "First wave", "state": "done"}])
    def test_missing_project_file_falls_back_gracefully(self):
        os.remove(os.path.join(self.wsroot, "project.json"))
        payload = self._payload()
        self.assertEqual(payload["project"], {"started": None, "target": None, "phases": []})
        self.assertEqual(len(payload["lanes"]), 4)
class BurnupEndpointTest(unittest.TestCase):
    """GET /api/project/burnup: recorded task burn-up plus per-day cost
    and token burn by workstream on UTC days, with honest history gaps.
    Synthetic bundles use invented ids (WS48 with history, WS49 planned
    with plan-done work but no history file, so its gap is labelled).
    """
    MODEL = "claude-sonnet-5-5"
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.wsroot = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.wsroot)
        day = lambda n: int(self.now // 86400) * 86400 - n * 86400
        self._bundle(
            "WS48", "active",
            [{"id": "WS48.1", "title": "First probe task", "status": "done"},
             {"id": "WS48.2", "title": "Second probe task", "status": "todo"}],
            [{"ts": day(2) + 60, "task": "WS48.1", "from": "todo", "to": "done"}],
        )
        self._bundle(
            "WS49", "planned",
            [{"id": "WS49.1", "title": "First probe task", "status": "done"}],
            None,
        )
        self._env = mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": self.wsroot})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.store.upsert_run(
            {
                "run_id": "muse:s48:ws48-probe",
                "source": "muse",
                "tier": "muse",
                "model": self.MODEL,
                "label": "ws48-WS48.1-probe",
                "session_id": "sess-48",
                "status": "done",
                "started_at": self.now - 3700,
                "ended_at": self.now - 3600,
            }
        )
        self.store.add_usage(
            {
                "ts": self.now - 3650,
                "session_id": "sess-48",
                "run_id": "muse:s48:ws48-probe",
                "model": self.MODEL,
                "tool": "muse",
                "message_id": "m-48",
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
    def _bundle(self, ws, state, tasks, history):
        dest = os.path.join(self.wsroot, ws)
        os.makedirs(dest)
        bundle = {"id": ws, "title": "Probe %s" % ws, "short": ws,
                  "order": int(ws[2:]), "kind": "workstream", "state": state,
                  "state_changed_at": int(self.now) - 50}
        with open(os.path.join(dest, "bundle.json"), "w") as fh:
            json.dump(bundle, fh)
        with open(os.path.join(dest, "plan.json"), "w") as fh:
            json.dump(
                {"dag": {"cols": ["Build"],
                         "groups": [{"id": "g", "col": 0, "title": "Build", "tasks": tasks}],
                         "edges": []},
                 "narrative": {"phases": []}},
                fh,
            )
        if history:
            with open(os.path.join(dest, "history.jsonl"), "w") as fh:
                for row in history:
                    fh.write(json.dumps(row) + "\n")
    def test_burnup_endpoint(self):
        status, _, body = get(self.port, "/api/project/burnup")
        self.assertEqual(status, 200)
        payload = json.loads(body)["burnup"]
        self.assertEqual(payload["day"], "UTC")
        self.assertTrue(payload["days"])
        by_ws = payload["tasks"]["by_workstream"]
        self.assertEqual(by_ws["WS48"]["recorded"], 1)
        self.assertEqual(by_ws["WS48"]["plan_total"], 2)
        self.assertIn("not dated", by_ws["WS49"]["gap"])
        self.assertTrue(by_ws["WS49"]["gap"].startswith("no history before "))
        self.assertEqual(payload["tasks"]["plan_total"], 3)
        for pos in range(len(payload["days"])):
            self.assertEqual(
                payload["tasks"]["project"][pos],
                sum(item["cumulative"][pos] for item in by_ws.values()),
            )
            self.assertEqual(
                payload["tokens"]["project"][pos],
                sum(item[pos] for item in payload["tokens"]["by_workstream"].values()),
            )


def geth(port, path, headers=None):
    """GET with request headers: returns (status, headers, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        heads = {key.lower(): value for key, value in response.getheaders()}
        return response.status, heads, response.read()
    finally:
        conn.close()


class ConditionalGetTest(unittest.TestCase):
    """ETag revalidation: an unchanged entry 304s, a store write retags."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.now = time.time()
        self.store.upsert_run(
            {
                "run_id": "muse:cond:ws1-first",
                "source": "muse",
                "tier": "muse",
                "model": "claude-sonnet-4-5",
                "label": "ws1-first",
                "session_id": "sess-cond",
                "status": "done",
                "started_at": self.now - 5000,
                "ended_at": self.now - 4000,
            }
        )
        # A persisted doctor run pins generated_at/next_run_at so the
        # doctor payload is stable across requests (without one,
        # next_run_at floats on every read and nothing can 304).
        self.store.doctor_snapshot([], self.now)
        self.server, self.thread = start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()

    def fetch(self, path, tag=None):
        headers = {} if tag is None else {"If-None-Match": tag}
        return geth(self.port, path, headers)

    def test_state_304_then_new_etag_after_write(self):
        status, heads, body = self.fetch("/api/state")
        self.assertEqual(status, 200)
        first = json.loads(body)
        tag = heads.get("etag")
        self.assertTrue(tag, "state has no ETag")
        status, _, empty = self.fetch("/api/state", tag)
        self.assertEqual(status, 304, "unchanged state should 304")
        self.assertEqual(empty, b"")
        self.store.upsert_run(
            {
                "run_id": "muse:cond:ws1-second",
                "source": "muse",
                "tier": "muse",
                "model": "claude-sonnet-4-5",
                "label": "ws1-second",
                "session_id": "sess-cond",
                "status": "done",
                "started_at": self.now - 500,
                "ended_at": self.now - 100,
            }
        )
        status, _, empty = self.fetch("/api/state", tag)
        self.assertEqual(status, 304, "a data write within the TTL must keep the stable generation")
        self.assertEqual(empty, b"")
        self.server.respcache.invalidate_all()
        status, heads, body = self.fetch("/api/state")
        self.assertEqual(status, 200)
        self.assertNotEqual(heads.get("etag"), tag, "write did not retag state")
        second = json.loads(body)
        self.assertNotEqual(
            json.dumps(first["workstreams"], sort_keys=True),
            json.dumps(second["workstreams"], sort_keys=True),
            "write did not change state content",
        )
        status, _, _ = self.fetch("/api/state", tag)
        self.assertEqual(status, 200, "stale tag should refetch, not 304")

    def test_snapshot_workstreams_plan_304(self):
        since = int(self.now - 12 * 3600)
        for path in (
            f"/api/snapshot?ws=all&since={since}&usage_group=model",
            f"/api/workstreams?since={since}",
            "/api/plan?ws=WS1",
            "/api/doctor",
        ):
            status, heads, _ = self.fetch(path)
            self.assertEqual(status, 200, path)
            tag = heads.get("etag")
            self.assertTrue(tag, f"{path} has no ETag")
            status, _, empty = self.fetch(path, tag)
            self.assertEqual(status, 304, f"{path} should 304 when unchanged")
            self.assertEqual(empty, b"")

    def test_snapshot_304_after_poll_clock_bump(self):
        """A poll cycle that writes no data (process samples, counts bump
        only the poll clock) must not retag the snapshot: the client's
        If-None-Match still names the entry, so idle ticks 304. A real
        data write keeps the TTL generation (304 within the window) and
        retags to 200 once the entry is invalidated."""
        since = int(self.now - 12 * 3600)
        path = f"/api/snapshot?ws=all&since={since}&usage_group=model"
        status, heads, _ = self.fetch(path)
        self.assertEqual(status, 200)
        tag = heads.get("etag")
        self.assertTrue(tag, "snapshot has no ETag")
        seq_before = self.store.seq
        data_before = self.store.data_seq
        self.store.set_ws_counts([{"ws": "WS9", "name": "counted"}])
        self.assertEqual(
            self.store.data_seq, data_before, "the fixture bump must not move the data version"
        )
        self.assertGreater(self.store.seq, seq_before, "the fixture bump moved no clock at all")
        status, _, empty = self.fetch(path, tag)
        self.assertEqual(status, 304, "a poll-clock bump retagged the snapshot: ticks can never 304")
        self.assertEqual(empty, b"")
        self.store.upsert_run(
            {
                "run_id": "muse:cond:ws1-clock",
                "source": "muse",
                "tier": "muse",
                "model": "claude-sonnet-4-5",
                "label": "ws1-clock",
                "session_id": "sess-cond",
                "status": "done",
                "started_at": self.now - 50,
                "ended_at": self.now - 10,
            }
        )
        status, _, empty = self.fetch(path, tag)
        self.assertEqual(status, 304, "a data write within the TTL must keep the stable generation")
        self.assertEqual(empty, b"")
        self.server.respcache.invalidate_all()
        status, heads, _ = self.fetch(path, tag)
        self.assertEqual(status, 200, "an invalidated write did not retag the snapshot")
        self.assertTrue(heads.get("etag") and heads.get("etag") != tag)


class SnapshotRecentPagingTest(unittest.TestCase):
    """recent_limit/recent_offset page the finished window: the default page
    keeps the historic shape, every page carries the true total, pages join
    up newest-first, bad values are 400s, and each page 304s on its own tag."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.now = time.time()
        for i in range(55):
            self.store.upsert_run(
                {
                    "run_id": f"muse:pg:ws21-fin-{i:02d}",
                    "source": "muse",
                    "tier": "muse",
                    "model": "claude-sonnet-4-5",
                    "label": f"ws21-pg-fin-{i:02d}",
                    "status": "done",
                    "started_at": self.now - 3600 - i * 60,
                    "ended_at": self.now - 60 * (i + 1),
                }
            )
        self.server, self.thread = start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()

    def fetch(self, path, tag=None):
        headers = {} if tag is None else {"If-None-Match": tag}
        return geth(self.port, path, headers)

    def snap(self, path):
        status, _, body = self.fetch(path)
        self.assertEqual(status, 200, path)
        return json.loads(body)

    def test_default_page_is_the_historic_window(self):
        payload = self.snap("/api/snapshot?ws=all")
        self.assertEqual(len(payload["recent"]), 50)
        self.assertEqual(payload["recent_shown"], 50)
        self.assertEqual(payload["recent_total"], 55)
        self.assertEqual(payload["recent_limit"], 50)
        self.assertEqual(payload["recent_offset"], 0)
        ended = [run["ended_at"] for run in payload["recent"]]
        self.assertEqual(ended, sorted(ended, reverse=True), "pages must stay newest-first")

    def test_pages_join_up_with_a_true_total(self):
        default = self.snap("/api/snapshot?ws=all")
        want = [run["run_id"] for run in default["recent"]]
        got = []
        for offset in (0, 10, 20, 30, 40, 50):
            page = self.snap(f"/api/snapshot?ws=all&recent_limit=10&recent_offset={offset}")
            self.assertEqual(page["recent_total"], 55, f"offset {offset} lost the true total")
            self.assertEqual(page["recent_limit"], 10)
            self.assertEqual(page["recent_offset"], offset)
            self.assertEqual(page["recent_shown"], len(page["recent"]))
            got.extend(run["run_id"] for run in page["recent"])
        self.assertEqual(got[:50], want, "paged rows drifted off the default window")
        self.assertEqual(len(got), 55, "the last page never arrived")
        self.assertEqual(len(set(got)), 55, "pages overlap")
        self.assertEqual(
            [run["ended_at"] for run in default["recent"]][:1],
            [self.snap("/api/snapshot?ws=all&recent_limit=1&recent_offset=0")["recent"][0]["ended_at"]],
            "offset 0 is not the newest row",
        )

    def test_bad_page_values_are_400s(self):
        for path in (
            "/api/snapshot?ws=all&recent_limit=0",
            "/api/snapshot?ws=all&recent_limit=nope",
            "/api/snapshot?ws=all&recent_offset=-1",
            "/api/snapshot?ws=all&recent_offset=nope",
        ):
            status, _, body = self.fetch(path)
            self.assertEqual(status, 400, path)
            self.assertIn("hint", json.loads(body), f"{path} names no next step")

    def test_each_page_304s_on_its_own_tag(self):
        page_a = "/api/snapshot?ws=all&recent_limit=10&recent_offset=0"
        page_b = "/api/snapshot?ws=all&recent_limit=10&recent_offset=10"
        _, heads_a, _ = self.fetch(page_a)
        _, heads_b, _ = self.fetch(page_b)
        self.assertTrue(heads_a.get("etag"), "page A has no ETag")
        self.assertTrue(heads_b.get("etag"), "page B has no ETag")
        self.assertNotEqual(heads_a.get("etag"), heads_b.get("etag"), "one page's tag must not serve another's bytes")
        status, _, empty = self.fetch(page_a, heads_a.get("etag"))
        self.assertEqual(status, 304, "an unchanged page should 304")
        self.assertEqual(empty, b"")
        status, _, _ = self.fetch(page_b, heads_a.get("etag"))
        self.assertEqual(status, 200, "a cross-page tag must refetch, not 304")


def _section_bundle(root: Path) -> Path:
    """A WS9 bundle whose plan holds two sections: Alpha (A1, A2) and
    Beta (B1). Task ids are plan-id substrings of the seeded labels, so
    run-to-task mapping needs no taskmap file."""
    ws = root / "WS9"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "bundle.json").write_text(
        json.dumps({"id": "WS9", "title": "Nine", "kind": "workstream"}),
        encoding="utf-8",
    )
    plan = {
        "dag": {
            "cols": ["Build", "Ship"],
            "groups": [
                {
                    "id": "g1",
                    "col": 0,
                    "title": "Alpha",
                    "tasks": [
                        {"id": "A1", "title": "First", "status": "wip"},
                        {"id": "A2", "title": "Second", "status": "todo"},
                    ],
                },
                {
                    "id": "g2",
                    "col": 1,
                    "title": "Beta",
                    "tasks": [{"id": "B1", "title": "Third", "status": "todo"}],
                },
            ],
            "edges": [],
        },
        "narrative": {"phases": []},
    }
    (ws / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    return ws / "plan.json"


class RunSectionTest(unittest.TestCase):
    """Run rows carry their plan section (dash/sections.py): a run whose
    label maps to a task in section A reads section A, an unmapped run
    reads null, a plan edit invalidates the map, and the snapshot and
    runs ETags retag on a plan edit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_ws = os.environ.get("AGENT_DASH_WORKSTREAMS")
        self.root = Path(self.tmp.name) / "ws"
        self.root.mkdir()
        self.plan_path = _section_bundle(self.root)
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.root)
        self.now = time.time()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.store.upsert_run(
            {
                "run_id": "muse:sec:ws9-a1-k1",
                "source": "muse",
                "tier": "muse",
                "model": "muse-spark-1.3-contributor",
                "label": "ws9-a1-k1",
                "status": "running",
                "started_at": self.now - 5,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "muse:sec:ws9-zzz",
                "source": "muse",
                "tier": "muse",
                "model": "muse-spark-1.3-contributor",
                "label": "ws9-zzz-unmapped",
                "status": "running",
                "started_at": self.now - 4,
            }
        )
        self.store.upsert_run(
            {
                "run_id": "muse:sec:ws9-b1-old",
                "source": "muse",
                "tier": "muse",
                "model": "muse-spark-1.3-contributor",
                "label": "ws9-b1-old",
                "status": "done",
                "started_at": self.now - 200,
                "ended_at": self.now - 100,
            }
        )
        self.server, self.thread = start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        if self.old_ws is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old_ws
        self.tmp.cleanup()

    def fetch(self, path, tag=None):
        headers = {} if tag is None else {"If-None-Match": tag}
        return geth(self.port, path, headers)

    def test_running_rows_carry_section(self):
        status, _, body = self.fetch("/api/snapshot?ws=all")
        self.assertEqual(status, 200)
        rows = {row["label"]: row for row in json.loads(body)["running"]}
        self.assertEqual(rows["ws9-a1-k1"]["section"], "Alpha")
        self.assertEqual(rows["ws9-a1-k1"]["section_index"], 0)
        self.assertIsNone(rows["ws9-zzz-unmapped"]["section"])
        self.assertIsNone(rows["ws9-zzz-unmapped"]["section_index"])

    def test_recent_rows_carry_section(self):
        status, _, body = self.fetch("/api/snapshot?ws=all")
        self.assertEqual(status, 200)
        rows = {row["label"]: row for row in json.loads(body)["recent"]}
        self.assertEqual(rows["ws9-b1-old"]["section"], "Beta")
        self.assertEqual(rows["ws9-b1-old"]["section_index"], 1)

    def test_runs_endpoint_carries_section(self):
        status, _, body = self.fetch("/api/runs?limit=10")
        self.assertEqual(status, 200)
        rows = {row["label"]: row for row in json.loads(body)["runs"]}
        self.assertEqual(rows["ws9-a1-k1"]["section"], "Alpha")
        self.assertIsNone(rows["ws9-zzz-unmapped"]["section"])

    def test_plan_edit_invalidates_and_retags(self):
        _, snap_heads, _ = self.fetch("/api/snapshot?ws=all")
        _, runs_heads, _ = self.fetch("/api/runs?limit=10")
        snap_tag = snap_heads.get("etag")
        runs_tag = runs_heads.get("etag")
        self.assertTrue(snap_tag, "snapshot has no ETag")
        self.assertTrue(runs_tag, "runs has no ETag")
        status, _, empty = self.fetch("/api/snapshot?ws=all", snap_tag)
        self.assertEqual(status, 304, "an unchanged snapshot should 304")
        self.assertEqual(empty, b"")
        plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
        plan["dag"]["groups"][0]["title"] = "Alpha2"
        self.plan_path.write_text(json.dumps(plan), encoding="utf-8")
        stamp = time.time() + 5
        os.utime(self.plan_path, (stamp, stamp))
        status, _, empty = self.fetch("/api/snapshot?ws=all", snap_tag)
        self.assertEqual(status, 304, "a plan edit within the TTL must keep the stable generation")
        self.assertEqual(empty, b"")
        status, _, rempty = self.fetch("/api/runs?limit=10", runs_tag)
        self.assertEqual(status, 304, "a plan edit within the TTL must keep the stable runs generation")
        self.assertEqual(rempty, b"")
        self.server.respcache.invalidate_all()
        status, snap_new, body = self.fetch("/api/snapshot?ws=all", snap_tag)
        self.assertEqual(status, 200, "an invalidated plan edit should retag the snapshot")
        self.assertNotEqual(snap_new.get("etag"), snap_tag, "snapshot tag ignored the plan edit")
        rows = {row["label"]: row for row in json.loads(body)["running"]}
        self.assertEqual(rows["ws9-a1-k1"]["section"], "Alpha2")
        status, runs_new, body = self.fetch("/api/runs?limit=10", runs_tag)
        self.assertEqual(status, 200, "an invalidated plan edit should retag the runs list")
        self.assertNotEqual(runs_new.get("etag"), runs_tag, "runs tag ignored the plan edit")
        rows = {row["label"]: row for row in json.loads(body)["runs"]}
        self.assertEqual(rows["ws9-a1-k1"]["section"], "Alpha2")


class LinearApiTest(unittest.TestCase):
    """``GET /api/linear``: read-only, linked workstreams only, no token in the body."""
    def setUp(self):
        from dash import linearview

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wsroot = os.path.join(self.tmp.name, "ws")
        for ws, linked in (("WS4", True), ("WS9", False)):
            dest = os.path.join(self.wsroot, ws)
            os.makedirs(dest)
            with open(os.path.join(dest, "bundle.json"), "w") as fh:
                json.dump({"id": ws, "title": "Probe " + ws, "order": int(ws[2:])}, fh)
            with open(os.path.join(dest, "plan.json"), "w") as fh:
                json.dump({"dag": {"groups": []}}, fh)
            if linked:
                with open(os.path.join(dest, "linear.json"), "w") as fh:
                    json.dump({"project": {"name": "Notes", "url": "https://linear.app/example/project/notes"},
                               "tasks": {}, "approvals": {}, "synced_at": time.time(), "last_error": None,
                               "access_token": "lin_api_SECRET_TOKEN_123"}, fh)
        with open(os.path.join(self.wsroot, "WS4", "history.jsonl"), "w") as fh:
            fh.write(json.dumps({"ts": time.time() - 60, "task": "WS4.a", "from": "todo", "to": "wip", "by": "linear"}) + "\n")
        self._env = mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": self.wsroot})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._status = mock.patch("dash.linearview.linear_auth.status", return_value={"method": "key", "issuer": None, "expires_at": None})
        self._status.start()
        self.addCleanup(self._status.stop)
        linearview._CACHE.update({"key": None, "at": 0.0, "body": None})
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.addCleanup(self.store.close)
        server, thread = start(self.store)
        self.addCleanup(stop, server, thread)
        self.port = server.server_address[1]

    def test_route_lists_linked_workstreams_only(self):
        status, ctype, body = get(self.port, "/api/linear")
        self.assertEqual(status, 200)
        self.assertIn("json", ctype)
        payload = json.loads(body)
        self.assertEqual(payload["signed_in"], "key")
        self.assertEqual([row["ws"] for row in payload["workstreams"]], ["WS4"])
        row = payload["workstreams"][0]
        self.assertEqual(row["changes_24h"], {"local": 0, "linear": 1})
        self.assertEqual(row["url"], "https://linear.app/example/project/notes")
        self.assertNotIn("lin_api_SECRET_TOKEN_123", body.decode("utf-8"))

    def test_route_is_read_only(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request("POST", "/api/linear", body=b"{}", headers={"Content-Type": "application/json"})
            status = conn.getresponse().status
        finally:
            conn.close()
        self.assertNotEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
