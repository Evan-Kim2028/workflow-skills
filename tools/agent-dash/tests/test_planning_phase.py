import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault(
    "AGENT_DASH_WORKSTREAMS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"),
)

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dash import orchestrate, planrender, plans, wsprogress
from dash.bundles import validate_bundle

MODEL = "claude-sonnet-5-5"


class PlanProgressTests(unittest.TestCase):
    def test_planning_group_reported(self):
        groups = [
            {"id": "plan", "phase": "planning", "tasks": [
                {"id": "P1", "title": "Plan a", "status": "done"},
                {"id": "P2", "title": "Plan b", "status": "done"},
            ]},
            {"id": "exec", "phase": "execution", "tasks": [
                {"id": "E1", "title": "Exec a", "status": "todo"},
                {"id": "E2", "title": "Exec b", "status": "todo"},
                {"id": "E3", "title": "Exec c", "status": "todo"},
            ]},
        ]
        result = plans.plan_progress(groups)
        self.assertEqual(result["done"], 0)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["pct"], 0)
        self.assertEqual(result["planning"], {"done": 2, "total": 2, "pct": 100})

    def test_no_planning_group_has_no_key(self):
        groups = [
            {"id": "exec", "tasks": [
                {"id": "E1", "title": "Exec a", "status": "todo"},
            ]},
        ]
        result = plans.plan_progress(groups)
        self.assertNotIn("planning", result)


class ValidatorTests(unittest.TestCase):
    def _make_bundle(self, root, phase):
        ws = root / "WS9"
        ws.mkdir()
        (ws / "bundle.json").write_text(
            json.dumps({"id": "WS9", "title": "Example", "short": "Ex", "order": 1, "kind": "workstream"}),
            encoding="utf-8",
        )
        (ws / "plan.json").write_text(
            json.dumps({
                "dag": {"cols": ["Shape"], "groups": [
                    {"id": "g", "col": 0, "title": "Group", "phase": phase,
                     "tasks": [{"id": "WS9.1", "title": "First", "status": "todo"}]},
                ], "edges": []},
                "narrative": {"phases": []},
            }),
            encoding="utf-8",
        )
        return ws

    def test_planning_and_execution_pass(self):
        for phase in ("planning", "execution"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._make_bundle(root, phase)
                self.assertEqual(validate_bundle("WS9", root), [])

    def test_design_yields_exact_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_bundle(root, "design")
            issues = validate_bundle("WS9", root)
            self.assertEqual(issues, ['plan.json: group g phase design is not "planning" or "execution"'])


class OrchestrateTests(unittest.TestCase):
    def _body(self):
        return {
            "groups": [
                {"id": "plan", "phase": "planning", "tasks": [
                    {"id": "P1", "title": "Plan", "status": "todo"},
                ]},
                {"id": "exec", "phase": "execution", "tasks": [
                    {"id": "E1", "title": "Exec", "status": "todo", "deps": ["P1"]},
                ]},
            ],
            "dag": {"groups": [
                {"id": "plan", "phase": "planning", "tasks": [
                    {"id": "P1", "title": "Plan", "status": "todo"},
                ]},
                {"id": "exec", "phase": "execution", "tasks": [
                    {"id": "E1", "title": "Exec", "status": "todo", "deps": ["P1"]},
                ]},
            ]},
        }

    def test_execution_task_ready(self):
        body = self._body()
        ready = orchestrate.ready_queue(body)
        self.assertEqual([row["id"] for row in ready], ["E1"])

    def test_waves_no_planning_ids(self):
        body = self._body()
        ids = [tid for wave in orchestrate.waves(body) for tid in wave]
        self.assertNotIn("P1", ids)
        self.assertIn("E1", ids)

    def test_critical_path_no_planning_ids(self):
        body = self._body()
        self.assertNotIn("P1", orchestrate.critical_path(body))

    def test_max_parallel_counts_execution_only(self):
        body = self._body()
        self.assertEqual(orchestrate.max_parallel(body), 1)


class WsProgressTests(unittest.TestCase):
    def setUp(self):
        wsprogress.clear_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.wsroot = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.wsroot)
        self._env = mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": self.wsroot})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(wsprogress.clear_cache)
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()
        import sys as _sys
        from pathlib import Path as _Path
        from dash.store import Store as _Store
        self.store = _Store(Path(self.tmp.name) / "dash.sqlite")
        self.addCleanup(self.store.close)

    def make_bundle(self, ws, tasks, history=None):
        dest = os.path.join(self.wsroot, ws)
        os.makedirs(dest, exist_ok=True)
        meta = {"id": ws, "title": "Probe %s" % ws, "kind": "workstream"}
        with open(os.path.join(dest, "bundle.json"), "w") as fh:
            json.dump(meta, fh)
        groups = []
        # planning group
        planning_tasks = [t for t in tasks if t.get("_planning")]
        exec_tasks = [t for t in tasks if not t.get("_planning")]
        if planning_tasks:
            groups.append({"id": "plan", "col": 0, "title": "Plan", "phase": "planning",
                           "tasks": [{k: v for k, v in t.items() if k != "_planning"} for t in planning_tasks]})
        if exec_tasks:
            groups.append({"id": "exec", "col": 0, "title": "Exec", "phase": "execution",
                           "tasks": [{k: v for k, v in t.items() if k != "_planning"} for t in exec_tasks]})
        with open(os.path.join(dest, "plan.json"), "w") as fh:
            json.dump({"dag": {"cols": ["Build"], "groups": groups, "edges": []}, "narrative": {"phases": []}}, fh)
        if history:
            with open(os.path.join(dest, "history.jsonl"), "w") as fh:
                for row in history:
                    fh.write(json.dumps(row) + "\n")

    def test_done_planning_task_does_not_move_series(self):
        self.make_bundle(
            "WP1",
            [{"id": "WP1.P1", "title": "Plan task", "status": "done", "_planning": True},
             {"id": "WP1.E1", "title": "Exec task", "status": "todo", "_planning": False}],
            [{"ts": self.now - 3600, "task": "WP1.P1", "from": "todo", "to": "done"}],
        )
        payload = wsprogress.build_wsprogress(self.store, "WP1", self.now)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["done"], 0)
        self.assertEqual(payload["dated"], 0)
        for pt in payload["points"]:
            self.assertEqual(pt["done"], 0)


class PlanRenderTests(unittest.TestCase):
    def test_planning_section_and_not_counted(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "WS9"
        root.mkdir(parents=True)
        (root / "bundle.json").write_text(json.dumps({"id": "WS9", "title": "Example", "short": "Ex", "order": 1, "kind": "workstream"}))
        (root / "plan.json").write_text(json.dumps({
            "workstream": "WS9",
            "motivation": {"verdict": "WORTH_IT", "verdict_reason": "Because.", "today": [], "after": [], "costs": [], "not_better": []},
            "agent_friendly": {"litmus": "One command.", "litmus_check": "run", "requirements": []},
            "narrative": {"thesis": "T", "end_state": "E", "phases": [{"name": "Land", "goal": "Ship.", "gate": "Done."}]},
            "dag": {"cols": ["Now"], "groups": [
                {"id": "plan", "col": 0, "title": "Planning", "phase": "planning", "tasks": [{"id": "WS9.P1", "title": "Plan task", "status": "done"}]},
                {"id": "g1", "col": 0, "title": "Land", "phase": "execution", "tasks": [{"id": "WS9.1", "title": "Ship", "status": "todo"}]},
            ], "edges": []},
        }))
        (root / "layout.json").write_text(json.dumps({
            "why_title": "W", "why_visual": [], "end_title": "E", "end_body": [],
            "plan_title": "P", "plan_extra": [], "agent_title": "A", "agent_visual": [],
            "review_note": "R", "ref_sections": [],
        }))
        (root / "visuals.html").write_text("<header><p class=\"thesis\">t</p></header>\n")
        page = planrender.render(root)
        self.assertIn("Planning", page)
        self.assertIn("not counted", page)


if __name__ == "__main__":
    unittest.main()