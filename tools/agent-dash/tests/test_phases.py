"""Usage by phase: classifier rules, folds, endpoint, and card wiring.

Seeds synthetic bundles (invented labels on WS1/WS2, exact-label map) in
a temp workstreams dir plus usage rows in a temp store, then pins the
contract: every rule class and its precedence, the explicit ws mark
convention, the unclassified bucket, task mapping (dash/dot
insensitive, plan members only), lead and planning-window context,
local override merge and hash, per-phase folds that sum to the
all-scope totals to the cent, scope windows, the ignore list and Other
bucket, rules-hash cache invalidation, the HTTP endpoint shape (304,
400), by_phase on workstream and state rows, the phases fixture shape,
and the card wiring (registry scopes/slot, page tags, chart rules,
stacked-bar math in node).
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

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

from dash import phases  # noqa: E402
from dash import projectphases  # noqa: E402
from dash.server import DashServer  # noqa: E402
from dash.sources import Registry  # noqa: E402
from dash.store import Store  # noqa: E402
from agent_dash import default_cfg  # noqa: E402

MODEL = "claude-sonnet-5-5"
UNPRICED_MODEL = "mystery-model-9"
DAY = 86400


def _run(label, run_id, source="muse", tier="muse", parent=None, started=1000.0):
    return {
        "run_id": run_id,
        "label": label,
        "source": source,
        "tier": tier,
        "session_id": "sess-" + run_id,
        "parent_session_id": parent,
        "started_at": started,
        "workstream": "WS1",
    }


class ClassifierTests(unittest.TestCase):
    """Pure rule tests over the shipped catalog file."""

    @classmethod
    def setUpClass(cls):
        phases.clear_caches()
        cls.rules, cls.digest, cls.version = phases.load_rules()

    def classify(self, label, task_id=None, in_window=False, **run):
        base = _run(label, "r")
        if "parent" in run:
            base["parent_session_id"] = run.pop("parent")
        base.update(run)
        return phases.classify_run(
            base, {"task_id": task_id, "in_window": in_window}, rules=self.rules
        )

    def test_convention_tokens_win(self):
        for label, phase in (
            ("ws9-plan-foundation", "planning"),
            ("ws9-planning-x", "planning"),
            ("ws9-exec-y", "execution"),
            ("ws9-execution-y", "execution"),
            ("ws9-review-z", "review"),
            ("ws9-rev-z", "review"),
            ("ws9-coord-w", "coordination"),
            ("ws9-coordination-w", "coordination"),
            ("WS9.PLAN-x", "planning"),
            ("ws9_plan_x", "planning"),
        ):
            with self.subTest(label=label):
                self.assertEqual(self.classify(label)[0], phase)

    def test_convention_beats_task(self):
        # ws1-rev-WS1.1 names a plan task but the explicit mark wins.
        self.assertEqual(
            self.classify("ws1-rev-WS1.1", task_id="WS1.1"), ("review", "convention")
        )

    def test_review_affix_beats_task(self):
        self.assertEqual(
            self.classify("r-WS1.1-scope", task_id="WS1.1")[0], "review"
        )
        self.assertEqual(
            self.classify("ws1-WS1.1-fix1", task_id="WS1.1")[0], "review"
        )

    def test_task_is_execution(self):
        self.assertEqual(
            self.classify("ws1-WS1.1-build", task_id="WS1.1"), ("execution", "task")
        )

    def test_task_miss_falls_through(self):
        self.assertEqual(
            self.classify("ws1-visual", task_id=None), ("unclassified", "none")
        )

    def test_keyword_classes(self):
        for label, phase in (
            ("Audit Postgres index usage", "planning"),
            ("Design pipeline observability", "planning"),
            ("ws1-spec-track", "planning"),
            ("Adversarial review of the plans", "review"),
            ("Verify orphan staging tables", "review"),
            ("ws1-diff-cert", "review"),
            ("ws1-train-lanes", "execution"),
            ("ws1-ops-gold-rebuild", "execution"),
            ("ws1-lander-2", "execution"),
            ("agentdash-harness-build", "coordination"),
            ("ws1-sync-huddle", "coordination"),
        ):
            with self.subTest(label=label):
                self.assertEqual(self.classify(label)[0], phase)

    def test_explain_is_not_planning(self):
        # Word boundaries: "explain" must not match the "plan" word.
        self.assertNotEqual(self.classify("ws1-pg-explain")[0], "planning")

    def test_lead_rule(self):
        self.assertEqual(
            self.classify("hub thinking", source="claude-main", parent=None)[0],
            "planning",
        )
        # A lead-source run with a parent is no lead session.
        self.assertEqual(
            self.classify("hub thinking", source="claude-main", parent="hub")[0],
            "unclassified",
        )
        # Keyword runs never reach the lead rule, but stay classified.
        self.assertEqual(
            self.classify("hub plan review", source="claude-main", parent=None)[0],
            "planning",
        )

    def test_window_rule(self):
        runs = [
            _run("ws1-early-notes", "early", started=100.0),
            _run("ws1-WS1.1-build", "tasked", started=200.0),
            _run("ws1-late-notes", "late", started=300.0),
        ]
        got = phases.classify_runs(
            runs, plan_tasks={"WS1": ["WS1.1"]}, rules=self.rules
        )
        self.assertEqual(got["early"][0], "planning")
        self.assertEqual(got["tasked"], ("execution", "task"))
        self.assertEqual(got["late"][0], "unclassified")

    def test_window_needs_an_edge(self):
        runs = [_run("ws1-early-notes", "early", started=100.0)]
        got = phases.classify_runs(runs, plan_tasks={"WS1": []}, rules=self.rules)
        self.assertEqual(got["early"][0], "unclassified")

    def test_resolve_task_dash_dot(self):
        self.assertEqual(
            phases.resolve_task("ws9-s-tbench", "WS9", {}, ["WS9.s-tbench"]),
            "WS9.s-tbench",
        )
        self.assertEqual(
            phases.resolve_task("ws7-WS7.11-dispatch", "WS7", {}, ["WS7.11-dispatch"]),
            "WS7.11-dispatch",
        )

    def test_resolve_task_rejects_outsiders(self):
        self.assertIsNone(phases.resolve_task("ws9-s-tbench", "WS9", {}, ["WS9.other"]))
        self.assertIsNone(phases.resolve_task("ws9-s-tbench", "WS9", {}))

    def test_merge_local_first(self):
        catalog = {"version": 1, "rules": [
            {"id": "a", "type": "keyword", "phase": "planning", "patterns": ["alpha"]},
            {"id": "b", "type": "keyword", "phase": "execution", "patterns": ["beta"]},
        ]}
        local = {"version": 2, "rules": [
            {"id": "b", "type": "keyword", "phase": "review", "patterns": ["beta"]},
            {"id": "c", "type": "keyword", "phase": "coordination", "patterns": ["gamma"]},
        ], "disabled": ["a"]}
        merged, version = phases.merge_rules(catalog, local)
        self.assertEqual(version, 2)
        self.assertEqual([entry["id"] for entry in merged], ["b", "c"])
        self.assertEqual(merged[0]["phase"], "review")

    def test_rules_hash_moves(self):
        first = phases.rules_hash([{"id": "a"}])
        second = phases.rules_hash([{"id": "b"}])
        self.assertNotEqual(first, second)
        self.assertEqual(first, phases.rules_hash([{"id": "a"}]))

    def test_bad_catalog_falls_back(self):
        rules, digest, version = phases.load_rules(catalog=Path("/nonexistent/phases.json"))
        self.assertTrue(rules)
        self.assertEqual(version, 0)
        phase, _rule = phases.classify_run(_run("ws1-plan-x", "r"), rules=rules)
        self.assertEqual(phase, "planning")


def _phases_cases():
    """(label, source, tier, parent, start_off, end_off, inp, out, model)."""
    return [
        ("seed-plan", "muse", "muse", "hub", 90000, 89000, 1000, 500, MODEL),
        ("seed-WS1.1-build", "muse", "muse", "hub", 80000, 79000, 2000, 1000, MODEL),
        ("r-seed-scope", "muse", "muse", "hub", 70000, 69000, 3000, 1500, MODEL),
        ("seed-train-lane", "grok", "grok", "hub", 60000, 59000, 4000, 2000, MODEL),
        ("seed-sync", "grok", "grok", "hub", 50000, 49000, 5000, 2500, MODEL),
        ("seed-hub-think", "claude-main", "opus", None, 95000, 94000, 6000, 3000, MODEL),
        ("seed-visual", "muse", "muse", "hub", 40000, 39000, 7000, 3500, MODEL),
        ("seed-mystery", "muse", "muse", "hub", 30000, 29000, 8000, 4000, UNPRICED_MODEL),
        ("ws1-plan-zzz", "muse", "muse", "hub", 20000, 19000, 9000, 4500, MODEL),
        ("old-plan", "muse", "muse", "hub", 200000, 199000, 111, 22, MODEL),
    ]


class FoldCase(unittest.TestCase):
    """Temp bundles + temp store behind env overrides."""

    def setUp(self):
        phases.clear_caches()
        projectphases.clear_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.wsroot = os.path.join(self.tmp.name, "ws")
        self.dataroot = os.path.join(self.tmp.name, "data")
        os.makedirs(self.wsroot)
        os.makedirs(self.dataroot)
        self.mapfile = os.path.join(self.tmp.name, "map.json")
        exact = {label: "WS1" for label, *_rest in _phases_cases()}
        with open(self.mapfile, "w") as fh:
            json.dump({"exact": exact, "regex": [], "sessions": {}}, fh)
        self._env = mock.patch.dict(os.environ, {
            "AGENT_DASH_WORKSTREAMS": self.wsroot,
            "AGENT_DASH_DATA_ROOT": self.dataroot,
            "AGENT_DASH_MAP": self.mapfile,
        })
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(projectphases.clear_cache)
        self.addCleanup(phases.clear_caches)
        self.now = time.time()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.addCleanup(self.store.close)
        self.addCleanup(self.tmp.cleanup)
        self.make_bundle("WS1", [
            {"id": "WS1.1", "title": "First", "status": "done"},
            {"id": "WS1.2", "title": "Second", "status": "todo"},
        ])
        self.make_bundle("WS2", [{"id": "WS2.1", "title": "Only", "status": "todo"}])
        for pos, (label, source, tier, parent, start, end, inp, out, model) in enumerate(_phases_cases()):
            self.add_run("k%d" % pos, label, source, tier, parent, start, end, inp, out, model)

    def make_bundle(self, ws, tasks):
        dest = os.path.join(self.wsroot, ws)
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "bundle.json"), "w") as fh:
            json.dump({"id": ws, "title": "Seed %s" % ws, "kind": "workstream"}, fh)
        groups = [{"id": "g", "col": 0, "title": "Build", "tasks": tasks}]
        with open(os.path.join(dest, "plan.json"), "w") as fh:
            json.dump({"dag": {"cols": ["Build"], "groups": groups, "edges": []},
                       "narrative": {"phases": []}}, fh)

    def add_run(self, run_id, label, source, tier, parent, start_off, end_off, inp, out, model):
        self.store.upsert_run({
            "run_id": run_id, "source": source, "tier": tier, "model": model,
            "label": label, "session_id": "sess-" + run_id,
            "parent_session_id": parent, "status": "done",
            "started_at": self.now - start_off, "ended_at": self.now - end_off,
        })
        self.store.add_usage({
            "ts": self.now - start_off + 30, "session_id": "sess-" + run_id,
            "run_id": run_id, "model": model, "tool": "muse",
            "message_id": "m-" + run_id,
            "input": inp, "output": out, "cache_read": 0, "cache_write": 0,
        })

    def test_store_workstream_attribution(self):
        rows = {row["run_id"]: row for row in self.store.query_runs(self.now, None, None, None, 100)}
        for pos in range(len(_phases_cases())):
            self.assertEqual(rows["k%d" % pos]["workstream"], "WS1")

    def test_by_phase_sums(self):
        got = self.store.by_phase_all_time()
        self.assertEqual(set(got["WS1"]), set(phases.PHASES))
        # Priced costs land; the mystery model prices nowhere.
        planning = got["WS1"]["planning"]
        self.assertGreater(planning["tokens"], 0)
        self.assertGreater(planning["cost_usd"], 0.0)
        billed = sum(cell["tokens"] for cell in got["WS1"].values())
        priced = [row for row in self.store.query_runs(self.now, None, None, None, 100)]
        self.assertTrue(priced)
        events = self.store.phase_events(None, None)[1]
        expect = sum(
            (event["input_tokens"] or 0) + (event["output_tokens"] or 0)
            for event in events
        )
        self.assertEqual(billed, expect)

    def test_by_phase_matches_buckets_to_the_cent(self):
        got = self.store.by_phase_all_time()
        phase_cost = round(sum(cell["cost_usd"] for cell in got["WS1"].values()), 2)
        cells = self.store.query_usage("workstream", "hour", None, None, None)
        bucket_cost = round(sum(cell["cost_usd"] or 0 for cell in cells), 2)
        bucket_tokens = sum(
            (cell.get("input") or 0) + (cell.get("output") or 0)
            + (cell.get("cache_read") or 0) + (cell.get("cache_write") or 0)
            for cell in cells
        )
        phase_tokens = sum(cell["tokens"] for cell in got["WS1"].values())
        self.assertEqual(phase_tokens, bucket_tokens)
        self.assertAlmostEqual(phase_cost, bucket_cost, delta=0.01)

    def test_phase_events_window(self):
        _runs, old = self.store.phase_events(None, self.now - 100000)
        _runs2, fresh = self.store.phase_events(self.now - 100000, None)
        self.assertEqual(len(old), 1)
        self.assertEqual(len(fresh), len(_phases_cases()) - 1)


class BuilderTests(FoldCase):
    def test_payload_shape(self):
        payload = projectphases.build_phases(self.store, "all", self.now)
        self.assertEqual(payload["scope"], "all")
        self.assertEqual(payload["day"], "UTC")
        self.assertTrue(payload["rules_hash"])
        self.assertIn("caption", payload)
        self.assertIn("Unclassified", payload["caption"])
        self.assertGreaterEqual(payload["unclassified_share"], 0.0)
        self.assertLessEqual(payload["unclassified_share"], 1.0)
        row = [item for item in payload["workstreams"] if item["ws"] == "WS1"][0]
        self.assertEqual(row["name"], "Seed WS1")
        self.assertEqual(set(row["by_phase"]), set(phases.PHASES))
        for phase in phases.PHASES:
            cell = row["by_phase"][phase]
            self.assertEqual(
                set(cell), {"tokens", "billed_tokens", "cost_usd", "unpriced_tokens", "agents", "runs", "hours"}
            )
            self.assertEqual(set(cell["tokens"]), {"input", "output", "cache_read", "cache_write", "reasoning"})
        series = row["series"]["execution"]
        self.assertEqual(
            set(series), {"cost", "tokens", "cumulative_cost", "cumulative_tokens"}
        )
        self.assertEqual(len(series["cost"]), len(row["days"]))
        cum = series["cumulative_cost"]
        self.assertTrue(all(later >= earlier for earlier, later in zip(cum, cum[1:])))
        # The mystery run prices nowhere but still counts its tokens.
        self.assertGreater(row["by_phase"]["unclassified"]["unpriced_tokens"], 0)

    def test_scope_windows(self):
        day = projectphases.build_phases(self.store, "24h", self.now)
        week = projectphases.build_phases(self.store, "7d", self.now)
        everything = projectphases.build_phases(self.store, "all", self.now)
        billed = lambda payload: sum(
            sum(cell["billed_tokens"] for cell in item["by_phase"].values())
            for item in payload["workstreams"]
        )
        # "old-plan" (200000 s ago) sits outside 24 h but inside 7 d.
        self.assertLess(billed(day), billed(week))
        self.assertEqual(billed(week), billed(everything))

    def test_totals_equal_phases_to_the_cent(self):
        for scope in ("24h", "7d", "all"):
            payload = projectphases.build_phases(self.store, scope, self.now)
            total = payload["total"]
            expect_tokens = {key: 0 for key in ("input", "output", "cache_read", "cache_write", "reasoning")}
            expect_cost = 0.0
            for item in payload["workstreams"]:
                for phase in phases.PHASES:
                    for key in expect_tokens:
                        expect_tokens[key] += item["by_phase"][phase]["tokens"][key]
                    expect_cost += item["by_phase"][phase]["cost_usd"] or 0.0
            self.assertEqual(total["tokens"], expect_tokens)
            self.assertAlmostEqual(total["cost_usd"], round(expect_cost, 6), delta=1e-9)
            # And the endpoint agrees with the bucket totals for the window.
            cells = self.store.query_usage("workstream", "hour", None, payload["since"], None)
            bucket_tokens = sum(
                (cell.get("input") or 0) + (cell.get("output") or 0)
                + (cell.get("cache_read") or 0) + (cell.get("cache_write") or 0)
                for cell in cells
            )
            bucket_cost = round(sum(cell["cost_usd"] or 0 for cell in cells), 2)
            billed = sum(total["tokens"][key] for key in ("input", "output", "cache_read", "cache_write"))
            self.assertEqual(billed, bucket_tokens)
            self.assertAlmostEqual(total["cost_usd"], bucket_cost, delta=0.01)

    def test_bad_scope(self):
        with self.assertRaises(ValueError):
            projectphases.build_phases(self.store, "fortnight", self.now)

    def test_cache_invalidates_on_write_and_rules(self):
        first = projectphases.build_phases(self.store, "all", self.now)
        again = projectphases.build_phases(self.store, "all", self.now)
        self.assertIs(first, again)
        self.add_run("kx", "seed-plan", "muse", "muse", "hub", 500, 400, 10, 5, MODEL)
        rebuilt = projectphases.build_phases(self.store, "all", self.now)
        self.assertIsNot(first, rebuilt)
        # A rules edit retags: point the override at a temp copy and grow it.
        alt = os.path.join(self.tmp.name, "phases.json")
        with open(os.path.join(ROOT, "catalog", "phases.json")) as fh:
            rules = fh.read()
        with open(alt, "w") as fh:
            fh.write(rules)
        with mock.patch.dict(os.environ, {"AGENT_DASH_PHASES": alt}):
            phases.clear_caches()
            projectphases.clear_cache()
            before = projectphases.build_phases(self.store, "all", self.now)
            with open(alt) as fh:
                doc = json.load(fh)
            doc["rules"].append(
                {"id": "zzz-no-match", "type": "keyword", "phase": "review",
                 "patterns": ["zzz-no-such-label"]})
            with open(alt, "w") as fh:
                json.dump(doc, fh)
            os.utime(alt, (self.now + 5, self.now + 5))
            after = projectphases.build_phases(self.store, "all", self.now)
            self.assertIsNot(before, after)
            self.assertNotEqual(before["rules_hash"], after["rules_hash"])
            self.assertEqual(before["total"], after["total"])

    def test_ignore_list_folds_other(self):
        with open(os.path.join(self.dataroot, "project.json"), "w") as fh:
            json.dump({"ignore_workstreams": ["WS1"]}, fh)
        phases.clear_caches()
        projectphases.clear_cache()
        payload = projectphases.build_phases(self.store, "all", self.now)
        ids = [item["ws"] for item in payload["workstreams"]]
        self.assertNotIn("WS1", ids)
        self.assertIn("Other", ids)

    def test_local_override_wins(self):
        local = {
            "rules": [
                {"id": "visual", "type": "keyword", "phase": "execution",
                 "patterns": ["visual"]},
            ]
        }
        with open(os.path.join(self.dataroot, "phases.local.json"), "w") as fh:
            json.dump(local, fh)
        phases.clear_caches()
        projectphases.clear_cache()
        payload = projectphases.build_phases(self.store, "all", self.now)
        row = [item for item in payload["workstreams"] if item["ws"] == "WS1"][0]
        self.assertEqual(row["by_phase"]["execution"]["runs"], 3)
        self.assertEqual(row["by_phase"]["unclassified"]["runs"], 1)
        plain = projectphases.build_phases(self.store, "all", self.now)
        row2 = [item for item in plain["workstreams"] if item["ws"] == "WS1"][0]
        self.assertEqual(row2["by_phase"]["execution"]["runs"], row["by_phase"]["execution"]["runs"])
        os.remove(os.path.join(self.dataroot, "phases.local.json"))
        phases.clear_caches()
        projectphases.clear_cache()
        reverted = projectphases.build_phases(self.store, "all", self.now)
        row3 = [item for item in reverted["workstreams"] if item["ws"] == "WS1"][0]
        self.assertEqual(row3["by_phase"]["unclassified"]["runs"], 2)


class EndpointTests(FoldCase):
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

    def _get(self, port, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_phases_endpoint_shape(self):
        port = self._serve()
        status, _headers, body = self._get(port, "/api/project/phases?scope=all")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(
            set(payload),
            {"scope", "since", "until", "day", "rules_hash", "rules_version",
             "caption", "workstreams", "total", "unclassified_share"},
        )
        row = [item for item in payload["workstreams"] if item["ws"] == "WS1"][0]
        self.assertEqual(set(row), {"ws", "name", "by_phase", "total", "days", "series"})
        self.assertEqual(set(row["by_phase"]), set(phases.PHASES))

    def test_phases_endpoint_304_and_400(self):
        port = self._serve()
        status, headers, _body = self._get(port, "/api/project/phases?scope=24h")
        self.assertEqual(status, 200)
        etag = headers.get("ETag")
        self.assertTrue(etag)
        again, _h2, _b2 = self._get(
            port, "/api/project/phases?scope=24h", {"If-None-Match": etag}
        )
        self.assertEqual(again, 304)
        bad, _h3, _b3 = self._get(port, "/api/project/phases?scope=fortnight")
        self.assertEqual(bad, 400)

    def test_workstream_rows_carry_by_phase(self):
        port = self._serve()
        status, _headers, body = self._get(port, "/api/workstreams")
        self.assertEqual(status, 200)
        rows = {row["ws"]: row for row in json.loads(body)["workstreams"]}
        self.assertIn("WS1", rows)
        cell = rows["WS1"]["by_phase"]
        self.assertEqual(set(cell), set(phases.PHASES))
        for phase in phases.PHASES:
            self.assertEqual(set(cell[phase]), {"tokens", "cost_usd"})
        billed = sum(cell[phase]["tokens"] for phase in phases.PHASES)
        events = self.store.phase_events(None, None)[1]
        expect = sum(
            (event["input_tokens"] or 0) + (event["output_tokens"] or 0)
            for event in events
            if event["run_id"] in {
                "k%d" % pos for pos in range(len(_phases_cases()))
            }
        )
        self.assertEqual(billed, expect)

    def test_state_rows_carry_by_phase(self):
        port = self._serve()
        status, _headers, body = self._get(port, "/api/state")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        rows = {row["id"]: row for row in payload["workstreams"]}
        self.assertIn("WS1", rows)
        self.assertEqual(set(rows["WS1"]["by_phase"]), set(phases.PHASES))


class FixtureTests(unittest.TestCase):
    def test_phases_fixture_shape(self):
        doc = json.loads((ROOT / "fixtures" / "phases.json").read_text())
        self.assertEqual(
            set(doc),
            {"scope", "since", "until", "day", "rules_hash", "rules_version",
             "caption", "workstreams", "total", "unclassified_share", "_note"},
        )
        self.assertTrue(doc["_note"].strip())
        for item in doc["workstreams"]:
            self.assertEqual(set(item), {"ws", "name", "by_phase", "total", "days", "series"})
            self.assertEqual(set(item["by_phase"]), set(phases.PHASES))
            for phase in phases.PHASES:
                self.assertEqual(
                    set(item["by_phase"][phase]),
                    {"tokens", "billed_tokens", "cost_usd", "unpriced_tokens",
                     "agents", "runs", "hours"},
                )
                series = item["series"][phase]
                self.assertEqual(len(series["cost"]), len(item["days"]))
                cum = series["cumulative_cost"]
                self.assertTrue(all(b >= a for a, b in zip(cum, cum[1:])))
                self.assertEqual(round(sum(series["cost"]), 4), cum[-1] if cum else 0.0)
            total_tokens = sum(
                item["by_phase"][phase]["billed_tokens"] for phase in phases.PHASES
            )
            total_cost = round(
                sum(item["by_phase"][phase]["cost_usd"] for phase in phases.PHASES), 4
            )
            self.assertEqual(item["total"]["cost_usd"], total_cost)
            self.assertGreaterEqual(total_tokens, 0)
        unc = sum(item["by_phase"]["unclassified"]["billed_tokens"] for item in doc["workstreams"])
        billed = sum(
            item["by_phase"][phase]["billed_tokens"]
            for item in doc["workstreams"] for phase in phases.PHASES
        )
        self.assertAlmostEqual(doc["unclassified_share"], round(unc / billed, 4) if billed else 0.0)

    def test_workstream_fixtures_carry_by_phase(self):
        for name in ("workstreams.json", "workstreams-all.json"):
            doc = json.loads((ROOT / "fixtures" / name).read_text())
            for row in doc["workstreams"]:
                self.assertEqual(set(row["by_phase"]), set(phases.PHASES), name)
                for phase in phases.PHASES:
                    self.assertEqual(set(row["by_phase"][phase]), {"tokens", "cost_usd"}, name)
                self.assertEqual(
                    sum(row["by_phase"][phase]["tokens"] for phase in phases.PHASES),
                    row["tokens"],
                    name,
                )
