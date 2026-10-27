"""Static assets must survive a page-load burst while the server folds.

A dashboard page load fires ~45 requests (scripts, CSS, APIs) in one
burst. The server used to listen with the socketserver default backlog
of 5, so while the one accept thread stalled behind a snapshot fold
the kernel RST the excess connections: page scripts died with
ERR_CONNECTION_RESET, their globals (warmTabs, ADM, openPlan, FIX)
stayed undefined, and the inline script threw before the body rendered.
This test pins the behavior: a burst of parallel static GETs issued
while snapshot folds keep the server busy must all answer 200 with
byte-identical bodies, never a dropped connection.
"""
import http.client
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

from agent_dash import default_cfg
from dash.server import DashServer
from dash.sources import Registry
from dash.store import Store

STATIC_ROOT = Path(__file__).resolve().parents[1] / "static"

# A spread of the scripts and styles a page load fetches: the core files
# whose loss breaks the inline script (api.js holds warmTabs, state.js
# holds openPlan, el.js holds FIX), plus cards down the tail of the burst.
STATIC_PATHS = [
    "/static/format.js",
    "/static/tokencache.js",
    "/static/tabs.js",
    "/static/models.js",
    "/static/core/el.js",
    "/static/core/state.js",
    "/static/core/api.js",
    "/static/core/cache.js",
    "/static/cards/tiles.js",
    "/static/cards/timeline.js",
    "/static/cards/plan-progress.js",
    "/static/plan.css",
]


def _start(store):
    server = DashServer(store, Registry([]), "127.0.0.1", 0, cfg=default_cfg())
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, thread


def _stop(server, thread):
    server.stop.set()
    server.shutdown()
    thread.join(5)
    server.server_close()


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


class StaticBurstTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        now = time.time()
        for k in range(3000):
            self.store.upsert_run({
                "run_id": "burst-seed:%d" % k,
                "source": "muse",
                "tier": "muse",
                "model": "muse-1",
                "label": "seed-%04d" % k,
                "status": "running" if k % 2 else "done",
                "started_at": now - (3000 - k),
                "elapsed_s": 10,
                "out_bytes": 100 + k,
                "out_growing": False,
                "tokens": {"input": 100, "output": 50, "cache_read": 0, "cache_write": 0},
            })
        self.server, self.thread = _start(self.store)
        self.port = self.server.server_address[1]

    def tearDown(self):
        _stop(self.server, self.thread)
        self.store.close()
        self.tmp.cleanup()

    def test_static_burst_under_fold_load(self):
        # Cold folds keep the accept thread busy: every since value is a
        # distinct cache key, so each hammer request recomputes instead of
        # 304ing off the warm cache.
        stop = threading.Event()

        def hammer(seed):
            n = 0
            while not stop.is_set() and n < 12:
                try:
                    _get(self.port, "/api/snapshot?ws=all&since=%d" % (1756684800 - seed * 100000 - n))
                except Exception:
                    pass
                n += 1

        hammers = [threading.Thread(target=hammer, args=(i,), daemon=True) for i in range(6)]
        for thread in hammers:
            thread.start()
        try:
            failures = []
            lock = threading.Lock()

            def burst(path):
                try:
                    status, body = _get(self.port, path)
                except Exception as exc:
                    with lock:
                        failures.append("%s: %s: %s" % (path, type(exc).__name__, exc))
                    return
                if status != 200:
                    with lock:
                        failures.append("%s: status %s, want 200" % (path, status))
                    return
                want = (STATIC_ROOT / path[len("/static/"):]).read_bytes()
                if body != want:
                    with lock:
                        failures.append("%s: %d body bytes, want %d" % (path, len(body), len(want)))

            threads = []
            for _ in range(7):
                for path in STATIC_PATHS:
                    thread = threading.Thread(target=burst, args=(path,), daemon=True)
                    threads.append(thread)
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
        finally:
            stop.set()
            for thread in hammers:
                thread.join(60)
        self.assertEqual(failures, [], "static requests lost under burst:\n" + "\n".join(failures[:10]))


if __name__ == "__main__":
    unittest.main()
