import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
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

from agent_dash import default_cfg, is_dash, load_cfg, main, run_backfill
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
        self.assertIn("seq", data)
        self.assertIn("running", data)
        self.assertIn("alerts_count", data)
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


if __name__ == "__main__":
    unittest.main()
