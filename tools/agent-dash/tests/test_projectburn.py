"""Project burn-up: recorded task moves vs plan totals, cost/token burn.

Seeds synthetic bundles (invented BX ids) in a temp workstreams dir plus
usage rows in a temp store, then pins the builder contract: cumulative
series from history with reverse moves, honest gap labels (never
interpolated), plan totals excluding deferred tasks, usage sums equal to
the all-scope totals for the same days, cache invalidation by history
mtime and store seq, the HTTP endpoint shape, the burnup.json fixture
shape, and the card wiring (registry scope/slot, page tags, chart rules).
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

from dash import projectburn  # noqa: E402
from dash.server import DashServer  # noqa: E402
from dash.sources import Registry  # noqa: E402
from dash.store import Store  # noqa: E402
from agent_dash import default_cfg  # noqa: E402

MODEL = "claude-sonnet-5-5"
DAY = 86400


def _day_ago(now, n):
    return int(now // DAY) * DAY - n * DAY


class BurnupCase(unittest.TestCase):
    """Temp bundles + temp store behind AGENT_DASH_WORKSTREAMS."""

    def setUp(self):
        projectburn.clear_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.wsroot = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.wsroot)
        self._env = mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": self.wsroot})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(projectburn.clear_cache)
        self.now = time.time()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.addCleanup(self.store.close)
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        projectburn.clear_cache()

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

    def add_run(self, ws_label, ws, ts, inp=100, out=50, model=MODEL):
        run_id = "muse:seed:%s:%d" % (ws_label, int(ts))
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": "muse",
                "tier": "muse",
                "model": model,
                "label": ws_label,
                "workstream": ws,
                "session_id": "seed-%s" % ws_label,
                "status": "done",
                "started_at": ts - 60,
                "ended_at": ts,
            }
        )
        self.store.add_usage(
            {
                "ts": ts,
                "session_id": "seed-%s" % ws_label,
                "run_id": run_id,
                "model": model,
                "tool": "muse",
                "message_id": "m-%s" % ws_label,
                "input": inp,
                "output": out,
                "cache_read": 10,
                "cache_write": 5,
            }
        )


class BuilderTests(BurnupCase):
    def test_cumulative_from_history(self):
        self.make_bundle(
            "BX1",
            [{"id": "BX1.1", "title": "First", "status": "done"},
             {"id": "BX1.2", "title": "Second", "status": "done"},
             {"id": "BX1.3", "title": "Third", "status": "todo"}],
            [{"ts": _day_ago(self.now, 3) + 60, "task": "BX1.1", "from": "todo", "to": "done"},
             {"ts": _day_ago(self.now, 1) + 60, "task": "BX1.2", "from": "todo", "to": "done"}],
        )
        payload = projectburn.build_burnup(self.store, self.now)
        row = payload["tasks"]["by_workstream"]["BX1"]
        self.assertEqual(row["cumulative"][-1], 2)
        self.assertEqual(row["recorded"], 2)
        self.assertEqual(row["plan_total"], 3)
        self.assertEqual(payload["tasks"]["plan_total"], 3)
        # The project line is the sum of recorded moves over time.
        for pos in range(len(payload["days"])):
            expect = sum(
                item["cumulative"][pos]
                for item in payload["tasks"]["by_workstream"].values()
            )
            self.assertEqual(payload["tasks"]["project"][pos], expect)

    def test_reverse_moves_decrement(self):
        self.make_bundle(
            "BX1",
            [{"id": "BX1.1", "title": "First", "status": "todo"}],
            [{"ts": _day_ago(self.now, 3) + 60, "task": "BX1.1", "from": "todo", "to": "done"},
             {"ts": _day_ago(self.now, 1) + 60, "task": "BX1.1", "from": "done", "to": "todo"}],
        )
        payload = projectburn.build_burnup(self.store, self.now)
        row = payload["tasks"]["by_workstream"]["BX1"]
        self.assertEqual(row["recorded"], 0)
        self.assertEqual(row["cumulative"][-1], 0)
        self.assertGreater(max(row["cumulative"]), 0)

    def test_gap_labelling_no_history(self):
        self.make_bundle(
            "BX1",
            [{"id": "BX1.1", "title": "First", "status": "done"}],
            [{"ts": _day_ago(self.now, 2) + 60, "task": "BX1.1", "from": "todo", "to": "done"}],
        )
        self.make_bundle(
            "BX2",
            [{"id": "BX2.1", "title": "First", "status": "done"},
             {"id": "BX2.2", "title": "Second", "status": "todo"}],
        )
        payload = projectburn.build_burnup(self.store, self.now)
        gap = payload["tasks"]["by_workstream"]["BX2"]["gap"]
        first = payload["first_recorded"]
        expect = "no history before %s: 1 done task not dated" % projectburn._day_label(
            projectburn._utc_day(first)
        )
        self.assertEqual(gap, expect)
        self.assertEqual(payload["tasks"]["by_workstream"]["BX2"]["cumulative"][-1], 0)

    def test_gap_labelling_partial_history(self):
        self.make_bundle(
            "BX1",
            [{"id": "BX1.1", "title": "First", "status": "done"},
             {"id": "BX1.2", "title": "Second", "status": "done"}],
            [{"ts": _day_ago(self.now, 2) + 60, "task": "BX1.1", "from": "todo", "to": "done"}],
        )
        payload = projectburn.build_burnup(self.store, self.now)
        row = payload["tasks"]["by_workstream"]["BX1"]
        self.assertEqual(row["recorded"], 1)
        self.assertEqual(row["undated"], 1)
        self.assertIn("1 done task not dated", row["gap"])
        self.assertTrue(row["gap"].startswith("no history before "))

    def test_plan_totals_exclude_deferred(self):
        self.make_bundle(
            "BX1",
            [{"id": "BX1.1", "title": "First", "status": "done"},
             {"id": "BX1.9", "title": "Moved", "status": "deferred", "moved_to": "BX2"}],
        )
        payload = projectburn.build_burnup(self.store, self.now)
        self.assertEqual(payload["tasks"]["by_workstream"]["BX1"]["plan_total"], 1)
        self.assertEqual(payload["tasks"]["plan_total"], 1)

    def test_usage_sums_equal_totals(self):
        """Reconcile-style: per-day sums equal the all-scope totals."""
        self.make_bundle("BX1", [{"id": "BX1.1", "title": "First", "status": "todo"}])
        self.make_bundle("BX2", [{"id": "BX2.1", "title": "First", "status": "todo"}])
        self.add_run("ws1-BX1.1-x", "BX1", self.now - 3700, inp=100, out=50)
        self.add_run("ws2-BX2.1-x", "BX2", self.now - 7300, inp=200, out=80)
        payload = projectburn.build_burnup(self.store, self.now)
        days = payload["days"]
        since = days[0]
        snap = self.store.snapshot(self.now, "all", since, None)
        totals = snap["totals"]["tokens"]
        billed = totals["input"] + totals["output"] + totals["cache_read"] + totals["cache_write"]
        self.assertEqual(sum(payload["tokens"]["project"]), billed)
        self.assertAlmostEqual(sum(payload["cost"]["project"]), snap["totals"]["cost_usd"], delta=0.01)
        buckets = self.store.query_usage("workstream", "day", None, since, None)
        bucket_tokens = sum(
            (cell.get("input") or 0) + (cell.get("output") or 0)
            + (cell.get("cache_read") or 0) + (cell.get("cache_write") or 0)
            for cell in buckets
        )
        self.assertEqual(sum(payload["tokens"]["project"]), bucket_tokens)

    def test_cache_invalidates_on_history_and_store(self):
        self.make_bundle(
            "BX1",
            [{"id": "BX1.1", "title": "First", "status": "todo"}],
            [{"ts": _day_ago(self.now, 2) + 60, "task": "BX1.1", "from": "todo", "to": "prog"}],
        )
        first = projectburn.build_burnup(self.store, self.now)
        again = projectburn.build_burnup(self.store, self.now)
        self.assertIs(first, again)
        # A store write moves the sequence: the payload rebuilds.
        self.add_run("ws1-BX1.1-x", "BX1", self.now - 3700)
        rebuilt = projectburn.build_burnup(self.store, self.now)
        self.assertIsNot(first, rebuilt)
        self.assertGreater(sum(rebuilt["tokens"]["project"]), 0)
        # A new history move changes the series once the file mtime moves.
        path = os.path.join(self.wsroot, "BX1", "history.jsonl")
        with open(path, "a") as fh:
            fh.write(json.dumps({"ts": _day_ago(self.now, 0) + 60, "task": "BX1.1",
                                 "from": "prog", "to": "done"}) + "\n")
        os.utime(path, (self.now + 5, self.now + 5))
        moved = projectburn.build_burnup(self.store, self.now)
        self.assertEqual(moved["tasks"]["by_workstream"]["BX1"]["recorded"], 1)

    def test_empty_project(self):
        payload = projectburn.build_burnup(self.store, self.now)
        self.assertEqual(payload["days"], [projectburn._utc_day(self.now)])
        self.assertEqual(payload["tasks"]["project"], [0])
        self.assertEqual(payload["tasks"]["plan_total"], 0)
        self.assertIsNone(payload["first_recorded"])


class EndpointTests(BurnupCase):
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

    def test_burnup_endpoint(self):
        self.make_bundle(
            "BX1",
            [{"id": "BX1.1", "title": "First", "status": "done"}],
            [{"ts": _day_ago(self.now, 1) + 60, "task": "BX1.1", "from": "todo", "to": "done"}],
        )
        self.add_run("ws1-BX1.1-x", "BX1", self.now - 3700)
        port = self._serve()
        status, body = self._get(port, "/api/project/burnup")
        self.assertEqual(status, 200)
        payload = json.loads(body)["burnup"]
        self.assertEqual(payload["day"], "UTC")
        self.assertTrue(payload["days"])
        self.assertIn("BX1", payload["workstreams"])
        self.assertIn("tasks", payload)
        self.assertIn("cost", payload)
        self.assertIn("tokens", payload)
        self.assertEqual(
            payload["tasks"]["by_workstream"]["BX1"]["recorded"], 1
        )


class FixtureTests(unittest.TestCase):
    def test_burnup_fixture_mirrors_endpoint_shape(self):
        doc = json.loads((ROOT / "fixtures" / "burnup.json").read_text(encoding="utf-8"))
        self.assertTrue(doc["_note"].strip())
        body = doc["burnup"]
        days = body["days"]
        self.assertTrue(len(days) >= 2)
        self.assertEqual(days, sorted(days))
        for day in days:
            self.assertEqual(day % DAY, 0)
        self.assertEqual(body["day"], "UTC")
        by_ws = body["tasks"]["by_workstream"]
        for ws in body["workstreams"]:
            self.assertIn(ws, by_ws)
            self.assertEqual(len(by_ws[ws]["cumulative"]), len(days))
        for pos in range(len(days)):
            self.assertEqual(
                body["tasks"]["project"][pos],
                sum(item["cumulative"][pos] for item in by_ws.values()),
            )
            self.assertAlmostEqual(
                body["cost"]["project"][pos],
                round(sum(item[pos] for item in body["cost"]["by_workstream"].values()), 4),
            )
            self.assertEqual(
                body["tokens"]["project"][pos],
                sum(item[pos] for item in body["tokens"]["by_workstream"].values()),
            )
        self.assertEqual(
            body["tasks"]["plan_total"],
            sum((item["plan_total"] or 0) for item in by_ws.values()),
        )
        gaps = [item["gap"] for item in by_ws.values() if item["gap"]]
        self.assertTrue(gaps)
        self.assertTrue(all("not dated" in gap for gap in gaps))


class CardWiringTests(unittest.TestCase):
    def _card(self):
        return (ROOT / "static" / "cards" / "burnup.js").read_text(encoding="utf-8")

    def test_registry_scope_and_slot(self):
        registry = (ROOT / "static" / "cards" / "registry.js").read_text(encoding="utf-8")
        self.assertIn('"burnup"', registry)
        self.assertIn('scope: "project"', registry)
        self.assertIn('slot: "exec-burnup"', registry)
        self.assertIn("burnupSpec", registry)

    def test_page_tags(self):
        page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<link rel="stylesheet" href="/static/cards/burnup.css">', page)
        self.assertIn('<script src="/static/cards/burnup.js"></script>', page)
        self.assertLess(
            page.index("/static/cards/burnup.js"), page.index("/static/cards/registry.js")
        )

    def test_chart_rules(self):
        src = self._card()
        for token in ("axes(", "burnupDayTicks(", "fmtN", "fmtUsd", "data-key", "data-v",
                      "data-t", "request failed", "data-failed-scope",
                      "no history before", "burnup-gap", "aria-pressed"):
            self.assertIn(token, src, "burnup.js lost %r" % token)
        for bad in (".toFixed(", "toLocaleString(", "doctor", "health", "warn"):
            self.assertNotIn(bad, src, "burnup.js must not contain %r" % bad)
        for fn in ("burnupSpec", "burnupCard", "burnupTasks", "burnupStacks"):
            self.assertIn("function %s(" % fn, src)


class IgnoreTests(BurnupCase):
    def add_attributed_run(self, ws_label, ws, ts, inp=100, out=50):
        """Like add_run, but pins the run row to ``ws`` directly.

        Label rules only map ws<number> lanes, so bundle ids like BX1
        and lane ids like WS345 need the explicit row for usage tests.
        The usage lands after the pin, so the hour rebuild keeps it.
        """
        run_id = "muse:seed:%s:%d" % (ws_label, int(ts))
        self.store.upsert_run(
            {
                "run_id": run_id,
                "source": "muse",
                "tier": "muse",
                "model": MODEL,
                "label": ws_label,
                "session_id": "seed-%s" % ws_label,
                "status": "done",
                "started_at": ts - 60,
                "ended_at": ts,
            }
        )
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE runs SET workstream=? WHERE run_id=?", (ws, run_id)
            )
            self.store._conn.commit()
        self.store.add_usage(
            {
                "ts": ts,
                "session_id": "seed-%s" % ws_label,
                "run_id": run_id,
                "model": MODEL,
                "tool": "muse",
                "message_id": "m-%s" % ws_label,
                "input": inp,
                "output": out,
                "cache_read": 10,
                "cache_write": 5,
            }
        )

    def test_ignored_usage_folds_into_other(self):
        self.make_bundle(
            "BX1",
            [{"id": "BX1.1", "title": "First", "status": "done"}],
            [{"ts": _day_ago(self.now, 1) + 60, "task": "BX1.1", "from": "todo", "to": "done"}],
        )
        day_ts = _day_ago(self.now, 1) + 3600
        self.add_attributed_run("bx1-BX1.1-x", "BX1", day_ts)
        self.add_attributed_run("ws345-probe", "WS345", day_ts, inp=200, out=100)
        (Path(self.tmp.name) / "project.json").write_text(
            json.dumps({"name": "Probe project", "ignore_workstreams": ["WS345"]}),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"AGENT_DASH_DATA_ROOT": self.tmp.name}):
            payload = projectburn.build_burnup(self.store, self.now)
        # The ignored id leaves every lane and order list ...
        self.assertNotIn("WS345", payload["workstreams"])
        self.assertNotIn("WS345", payload["tasks"]["by_workstream"])
        self.assertNotIn("WS345", payload["cost"]["by_workstream"])
        self.assertNotIn("WS345", payload["tokens"]["by_workstream"])
        self.assertNotIn("Other", payload["tasks"]["by_workstream"])
        # ... while its usage folds into one Other series that sums.
        token_ws = payload["tokens"]["by_workstream"]
        self.assertIn("Other", token_ws)
        self.assertEqual(sum(token_ws["Other"]), 200 + 100 + 10 + 5)
        self.assertIn("Other", payload["cost"]["by_workstream"])
        project_total = sum(payload["tokens"]["project"])
        split_total = sum(sum(series) for series in token_ws.values())
        self.assertEqual(split_total, project_total)
        self.assertEqual(project_total, (100 + 50 + 10 + 5) + (200 + 100 + 10 + 5))
        # The task lane only ever knew the bundled workstream.
        self.assertEqual(sorted(payload["tasks"]["by_workstream"]), ["BX1"])
        self.assertEqual(payload["tasks"]["by_workstream"]["BX1"]["recorded"], 1)


if __name__ == "__main__":
    unittest.main()
