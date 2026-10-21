"""Every endpoint the pages poll must support ETag/304.

The exec and plan views refetch their endpoints on every tick and every
tab switch. Without a working conditional round-trip each of those
refetches ships a full body; with one an unchanged endpoint costs
headers only. This test pins the round-trip per endpoint: a first GET
returns 200 with an ETag, and a second GET with If-None-Match returns
304 with an empty body. An endpoint added to the pages without ETag
support fails here until it gains it.
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

from agent_dash import default_cfg
from dash.server import DashServer
from dash.sources import Registry
from dash.store import Store


def _start(store):
    server = DashServer(store, Registry([]), "127.0.0.1", 0, cfg=default_cfg())
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, thread


def _stop(server, thread):
    server.stop.set()
    server.shutdown()
    thread.join(3)
    server.server_close()


def _get(port, path, etag=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        return response.status, response.getheader("ETag"), response.read()
    finally:
        conn.close()


# (name, path): every endpoint the exec/plan pages poll on an idle
# dashboard, with the same parameters the page sends.
ENDPOINTS = [
    ("snapshot", "/api/snapshot?ws=all&since=1756684800"),
    ("runs", "/api/runs?since=1756684800&limit=5000"),
    ("concurrency", "/api/concurrency?since=1756684800&step=60"),
    ("bundles", "/api/bundles"),
    ("workstreams", "/api/workstreams?since=1756684800"),
    ("workstreams-all", "/api/workstreams?since=1756684800&state=all"),
    ("project", "/api/project"),
    ("plan", "/api/plan?ws=WS9"),
    ("approvals", "/api/approvals?ws=all"),
    ("usage", "/api/usage?group=workstream&since=1756684800"),
    ("ws-progress", "/api/ws/progress?ws=all"),
    ("archive", "/api/archive"),
    ("project-phases", "/api/project/phases?scope=all"),
]

# Live endpoints: they emit an ETag and answer 304 when nothing changed,
# but their bodies carry per-request clocks (health's perf CPU counters,
# doctor's check durations, reconcile's window edges), so a 304 is not
# guaranteed and the test only pins the ETag's presence.
LIVE_ENDPOINTS = [
    ("health", "/api/health"),
    ("doctor", "/api/doctor"),
    ("reconcile", "/api/reconcile?hours=24"),
]

_PLAN_DOC = {
    "dag": {
        "cols": ["Shape"],
        "groups": [
            {"id": "g", "col": 0, "title": "Group", "tasks": [
                {"id": "WS9.1", "title": "First task", "status": "done"},
            ]},
        ],
        "edges": [],
    },
    "narrative": {"phases": []},
}


class ETagTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        maps = Path(self.tmp.name) / "maps"
        maps.mkdir()
        (maps / "ws9.json").write_text(json.dumps(_PLAN_DOC), encoding="utf-8")
        (maps / "plans.json").write_text(json.dumps({"WS9": "ws9.json"}), encoding="utf-8")
        self._old_plans = os.environ.get("AGENT_DASH_PLANS")
        os.environ["AGENT_DASH_PLANS"] = str(maps / "plans.json")
        self.server, self.thread = _start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        _stop(self.server, self.thread)
        self.store.close()
        if self._old_plans is None:
            os.environ.pop("AGENT_DASH_PLANS", None)
        else:
            os.environ["AGENT_DASH_PLANS"] = self._old_plans
        self.tmp.cleanup()

    def test_every_polled_endpoint_supports_conditional_get(self):
        failures = []
        for name, path in ENDPOINTS:
            with self.subTest(endpoint=name):
                status, etag, _body = _get(self.port, path)
                if status != 200:
                    failures.append(f"{name}: first GET status {status}, want 200")
                    continue
                if not etag:
                    failures.append(f"{name}: 200 without an ETag header")
                    continue
                status2, _etag2, body2 = _get(self.port, path, etag=etag)
                if status2 != 304:
                    failures.append(f"{name}: conditional GET status {status2}, want 304")
                elif body2:
                    failures.append(f"{name}: 304 with {len(body2)} body bytes, want empty")
        self.assertEqual(failures, [], "endpoints without a working ETag/304 round-trip:\n" + "\n".join(failures))

    def test_live_endpoints_emit_etag(self):
        for name, path in LIVE_ENDPOINTS:
            with self.subTest(endpoint=name):
                status, etag, _body = _get(self.port, path)
                self.assertEqual(status, 200, f"{name}: first GET status {status}")
                self.assertTrue(etag, f"{name}: 200 without an ETag header")


if __name__ == "__main__":
    unittest.main()
