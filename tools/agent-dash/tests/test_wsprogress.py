"""Per-workstream progress over time: done-% steps, prog band, milestones.

Seeds synthetic bundles (invented BX ids) in a temp workstreams dir plus
runs in a temp store, then pins the builder contract: step points from
the shared replay done rule, milestone first-cross times (reached and
unreached), the recorded prog band, runs started per hour from the
store's run rows, honest gap labels (never interpolated), the backfilled
task list, cache invalidation by history mtime and store seq, the HTTP
endpoint shape, the ws-progress fixture shape, and the card wiring
(registry scope, page tags, chart rules).
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault(
    "AGENT_DASH_WORKSTREAMS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fixtures",
        "bundles",
    ),
)

import http.client
import json
import math
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

from dash import wsprogress  # noqa: E402
from dash.replay import ReplayNotFound  # noqa: E402
from dash.server import DashServer  # noqa: E402
from dash.sources import Registry  # noqa: E402
from dash.store import Store  # noqa: E402
from agent_dash import default_cfg  # noqa: E402

MODEL = "claude-sonnet-5-5"
HOUR = 3600


class ProgressCase(unittest.TestCase):
    """Temp bundles + temp store behind AGENT_DASH_WORKSTREAMS."""

    def setUp(self):
        wsprogress.clear_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.wsroot = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.wsroot)
        self._env = mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": self.wsroot})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(wsprogress.clear_cache)
        self.now = time.time()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.addCleanup(self.store.close)
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        wsprogress.clear_cache()

    def make_bundle(self, ws, tasks, history=None):
        dest = os.path.join(self.wsroot, ws)
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "bundle.json"), "w") as fh:
            json.dump({"id": ws, "title": "Probe %s" % ws, "kind": "workstream"}, fh)
        groups = [{"id": "g", "col": 0, "title": "Build", "tasks": tasks}]
        with open(os.path.join(dest, "plan.json"), "w") as fh:
            json.dump(
                {"dag": {"cols": ["Build"], "groups": groups, "edges": []},
                 "narrative": {"phases": []}},
                fh,
            )
        if history:
            with open(os.path.join(dest, "history.jsonl"), "w") as fh:
                for row in history:
                    fh.write(json.dumps(row) + "\n")

    def add_run(self, label, ws, started_at, ended_at=None):
        """A finished run pinned to ``ws`` (neutral labels keep the store's
        label attribution from moving it elsewhere)."""
        run_id = "muse:seed:%s:%d" % (label, int(started_at))
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": "muse",
                "tier": "muse",
                "model": MODEL,
                "label": label,
                "session_id": "seed-%s" % label,
                "status": "done",
                "started_at": started_at,
                "ended_at": started_at + 60 if ended_at is None else ended_at,
            }
        )
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE runs SET workstream=? WHERE run_id=?", (ws, run_id)
            )
            self.store._conn.commit()


class BuilderTests(ProgressCase):
    def test_steps_and_milestones(self):
        self.make_bundle(
            "BX1",
            [{"id": "BX1.%d" % i, "title": "Task %d" % i,
              "status": "done" if i <= 2 else "todo"} for i in range(1, 6)],
            [{"ts": self.now - 7200, "task": "BX1.1", "from": "todo", "to": "done"},
             {"ts": self.now - 3600, "task": "BX1.2", "from": "todo", "to": "done"}],
        )
        payload = wsprogress.build_wsprogress(self.store, "BX1", self.now)
        self.assertEqual(payload["now"], self.now)
        self.assertEqual(payload["ws"], "BX1")
        self.assertEqual(payload["total"], 5)
        self.assertEqual(payload["done"], 2)
        self.assertEqual([pt["done"] for pt in payload["points"]], [1, 2])
        self.assertEqual(payload["points"][0]["t"], self.now - 7200)
        # done 2 of 5 is 40%: the 20% mark is crossed at the first task.
        by_pct = {mark["pct"]: mark for mark in payload["milestones"]}
        self.assertEqual(len(payload["milestones"]), 5)
        self.assertTrue(by_pct[20]["reached"])
        self.assertEqual(by_pct[20]["t"], self.now - 7200)
        self.assertAlmostEqual(by_pct[20]["elapsed"], 0.0)
        self.assertTrue(by_pct[40]["reached"])
        self.assertEqual(by_pct[40]["t"], self.now - 3600)
        self.assertFalse(by_pct[60]["reached"])
        self.assertIsNone(by_pct[60]["t"])
        self.assertFalse(by_pct[100]["reached"])

    def test_prog_band_is_recorded(self):
        self.make_bundle(
            "BX1",
            [{"id": "BX1.1", "title": "First", "status": "done"},
             {"id": "BX1.2", "title": "Second", "status": "prog"},
             {"id": "BX1.3", "title": "Third", "status": "todo"}],
            [{"ts": self.now - 5400, "task": "BX1.2", "from": "todo", "to": "prog"},
             {"ts": self.now - 3600, "task": "BX1.1", "from": "todo", "to": "done"}],
        )
        payload = wsprogress.build_wsprogress(self.store, "BX1", self.now)
        prog_by_t = {pt["t"]: pt["prog"] for pt in payload["points"]}
        self.assertEqual(prog_by_t[self.now - 5400], 1)
        self.assertEqual(prog_by_t[self.now - 3600], 1)
        # A task with no history row never reads as prog.
        self.make_bundle(
            "BX2",
            [{"id": "BX2.1", "title": "First", "status": "prog"}],
            [{"ts": self.now - 100, "task": "BX2.9", "from": "todo", "to": "done"}],
        )
        other = wsprogress.build_wsprogress(self.store, "BX2", self.now)
        self.assertTrue(all(pt["prog"] == 0 for pt in other["points"]))

    def test_reopened_tasks_keep_their_resolved_moment(self):
        # The series follows the shared replay rule: a recorded done
        # transition fixes the task's done moment even when the task is
        # later reopened, so the curve is non-decreasing and agrees with
        # the replay task by task.
        self.make_bundle(
            "BX1",
            [{"id": "BX1.1", "title": "First", "status": "todo"}],
            [{"ts": self.now - 7200, "task": "BX1.1", "from": "todo", "to": "done"},
             {"ts": self.now - 3600, "task": "BX1.1", "from": "done", "to": "todo"}],
        )
        payload = wsprogress.build_wsprogress(self.store, "BX1", self.now)
        self.assertEqual(payload["done"], 0)
        self.assertEqual(payload["points"][-1]["done"], 1)
        self.assertEqual(payload["points"][-1]["t"], self.now - 7200)

    def test_runs_per_hour(self):
        self.make_bundle(
            "BX1",
            [{"id": "BX1.1", "title": "First", "status": "todo"}],
        )
        base = int(self.now // HOUR) * HOUR
        self.add_run("probe-task-a", "BX1", base - 2 * HOUR + 10)
        self.add_run("probe-task-b", "BX1", base - 2 * HOUR + 20)
        self.add_run("probe-task-c", "BX1", base - 1 * HOUR + 30)
        payload = wsprogress.build_wsprogress(self.store, "BX1", self.now)
        bars = {row["t"]: row["n"] for row in payload["runs_per_hour"]}
        self.assertEqual(bars[base - 2 * HOUR], 2)
        self.assertEqual(bars[base - 1 * HOUR], 1)
        self.assertEqual(payload["runs_total"], 3)
        self.assertEqual(payload["undated_runs"], 0)

    def test_undated_gap(self):
        self.make_bundle(
            "BX1",
            [{"id": "BX1.1", "title": "First", "status": "done"},
             {"id": "BX1.2", "title": "Second", "status": "todo"}],
        )
        payload = wsprogress.build_wsprogress(self.store, "BX1", self.now)
        self.assertEqual(payload["dated"], 0)
        self.assertEqual(payload["undated"], 1)
        self.assertIn("1 done task not dated", payload["gap"])

    def test_backfilled_tasks_listed(self):
        self.make_bundle(
            "BX1",
            [{"id": "BX1.1", "title": "First", "status": "done"}],
            [{"ts": self.now - 100, "task": "BX1.1", "to": "done",
              "backfill": "run manifest end"}],
        )
        payload = wsprogress.build_wsprogress(self.store, "BX1", self.now)
        self.assertEqual(payload["backfilled"], ["BX1.1"])
        self.assertEqual(payload["undated"], 0)

    def test_cache_invalidates_on_history_and_store(self):
        self.make_bundle(
            "BX1",
            [{"id": "BX1.1", "title": "First", "status": "todo"},
             {"id": "BX1.2", "title": "Second", "status": "todo"}],
            [{"ts": self.now - 7200, "task": "BX1.1", "from": "todo", "to": "done"}],
        )
        first = wsprogress.build_wsprogress(self.store, "BX1", self.now)
        again = wsprogress.build_wsprogress(self.store, "BX1", self.now)
        self.assertIs(first, again)
        self.add_run("probe-task-a", "BX1", self.now - 3700)
        rebuilt = wsprogress.build_wsprogress(self.store, "BX1", self.now)
        self.assertIsNot(first, rebuilt)
        self.assertEqual(rebuilt["runs_total"], 1)
        path = os.path.join(self.wsroot, "BX1", "history.jsonl")
        with open(path, "a") as fh:
            fh.write(json.dumps({"ts": self.now - 100, "task": "BX1.2",
                                 "from": "todo", "to": "done"}) + "\n")
        os.utime(path, (self.now + 5, self.now + 5))
        moved = wsprogress.build_wsprogress(self.store, "BX1", self.now)
        self.assertEqual(moved["points"][-1]["done"], 2)

    def test_no_plan_raises(self):
        with self.assertRaises(ReplayNotFound):
            wsprogress.build_wsprogress(self.store, "BX404", self.now)


class EndpointTests(ProgressCase):
    def _serve(self):
        server = DashServer(self.store, Registry([]), "127.0.0.1", 0, cfg=default_cfg())
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        return server.server_address[1]

    def _get(self, port, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_progress_endpoint(self):
        self.make_bundle(
            "BX1",
            [{"id": "BX1.1", "title": "First", "status": "done"},
             {"id": "BX1.2", "title": "Second", "status": "todo"}],
            [{"ts": self.now - 3600, "task": "BX1.1", "from": "todo", "to": "done"}],
        )
        self.add_run("probe-task-a", "BX1", self.now - 3700)
        port = self._serve()
        status, body = self._get(port, "/api/ws/progress?ws=BX1")
        self.assertEqual(status, 200)
        payload = json.loads(body)["wsprogress"]
        self.assertEqual(payload["ws"], "BX1")
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["done"], 1)
        self.assertTrue(payload["points"])
        self.assertEqual(len(payload["milestones"]), 5)
        self.assertEqual(payload["runs_total"], 1)

    def test_progress_endpoint_errors(self):
        port = self._serve()
        status, _body = self._get(port, "/api/ws/progress")
        self.assertEqual(status, 400)
        status, _body = self._get(port, "/api/ws/progress?ws=..")
        self.assertEqual(status, 400)
        status, body = self._get(port, "/api/ws/progress?ws=BX404")
        self.assertEqual(status, 404)
        self.assertIn("plan_not_found", body.decode("utf-8"))


class FixtureTests(unittest.TestCase):
    def test_ws_progress_fixture_mirrors_endpoint_shape(self):
        for name in ("ws-progress-WS9.json", "ws-progress-WS1.json"):
            doc = json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))
            self.assertTrue(doc["_note"].strip())
            body = doc["wsprogress"]
            self.assertEqual(body["ws"], name[len("ws-progress-"):-len(".json")])
            points = body["points"]
            self.assertEqual([pt["t"] for pt in points], sorted(pt["t"] for pt in points))
            for pt in points:
                self.assertGreaterEqual(pt["done"], 0)
                self.assertLessEqual(pt["done"], body["total"])
                self.assertGreaterEqual(pt["prog"], 0)
                self.assertAlmostEqual(pt["pct"], round(pt["done"] / body["total"] * 1000) / 10)
            self.assertEqual([mark["pct"] for mark in body["milestones"]],
                             [20, 40, 60, 80, 100])
            for mark in body["milestones"]:
                need = math.ceil(body["total"] * mark["pct"] / 100)
                self.assertEqual(mark["done"], need)
                self.assertEqual(mark["reached"], mark["t"] is not None)
            hours = body["runs_per_hour"]
            self.assertEqual([row["t"] for row in hours], sorted(row["t"] for row in hours))
            for row in hours:
                self.assertEqual(row["t"] % HOUR, 0)
            self.assertEqual(body["runs_total"], sum(row["n"] for row in hours))
            self.assertEqual(body["dated"] + body["undated"], body["done"])


class CardWiringTests(unittest.TestCase):
    def _card(self):
        return (ROOT / "static" / "cards" / "progress.js").read_text(encoding="utf-8")

    def test_registry_scope_and_order(self):
        registry = (ROOT / "static" / "cards" / "registry.js").read_text(encoding="utf-8")
        self.assertIn('"progress"', registry)
        self.assertIn('scope: "workstream"', registry)
        self.assertIn("progressSpec", registry)
        # Below the planning-versus-execution card on the workstream tab.
        self.assertLess(registry.index('"phases-ws"'), registry.index('"progress"'))

    def test_page_tags(self):
        page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<link rel="stylesheet" href="/static/cards/progress.css">', page)
        self.assertIn('<script src="/static/cards/progress.js"></script>', page)
        self.assertLess(
            page.index("/static/cards/progress.js"), page.index("/static/cards/registry.js")
        )

    def test_chart_rules(self):
        src = self._card()
        for token in ("axes(", "timeTicks(", "fmtN", "fmtPct", "fmtDur", "fmtDate",
                      "data-key", "data-v", "data-t", "data-milestone",
                      "request failed", "data-failed-scope", "tabindex",
                      "role: \"img\"", "aria-label"):
            self.assertIn(token, src, "progress.js lost %r" % token)
        for bad in (".toFixed(", "toLocaleString(", "doctor", "health", "warn"):
            self.assertNotIn(bad, src, "progress.js must not contain %r" % bad)
        for fn in ("progressSpec", "progressCard", "progChartA", "progChartB",
                   "progLegendTip", "progMileRows", "progStepRows", "progBarRows"):
            self.assertIn("function %s(" % fn, src)


if __name__ == "__main__":
    unittest.main()
