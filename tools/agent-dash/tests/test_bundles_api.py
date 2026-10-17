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

from agent_dash import default_cfg
from dash.server import DashServer
from dash.sources import Registry
from dash.store import Store

ROOT = Path(__file__).resolve().parents[1]


def start(store, index):
    server = DashServer(store, Registry([]), "127.0.0.1", 0, index_path=index, stream_interval=30, cfg=default_cfg())
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
        return response.status, response.getheader("Content-Type"), response.read()
    finally:
        conn.close()


class BundlesApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name
        root = Path(self.tmp.name)
        overview = root / "OVERVIEW"
        overview.mkdir()
        (overview / "bundle.json").write_text(
            json.dumps({"id": "OVERVIEW", "title": "Overview", "short": "Program", "order": 9, "kind": "program"}),
            encoding="utf-8",
        )
        (overview / "plan.json").write_text(
            json.dumps({"narrative": {"thesis": "One program.", "end_state": "One API.", "phases": []}}),
            encoding="utf-8",
        )
        (overview / "page.html").write_text("<p>OVERVIEW-PAGE</p>", encoding="utf-8")
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
                        "cols": ["Shape"],
                        "groups": [{"id": "g", "col": 0, "title": "Group", "tasks": [{"id": "WS9.1", "title": "First task", "status": "todo"}]}],
                        "edges": [],
                    },
                    "narrative": {"phases": []},
                }
            ),
            encoding="utf-8",
        )
        (ws / "page.html").write_text("<p>WS9-PAGE</p>", encoding="utf-8")
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws9-probe",
                "source": "muse",
                "tier": "muse",
                "label": "ws9-probe",
                "status": "running",
                "started_at": time.time(),
            }
        )
        self.server, self.thread = start(self.store, ROOT / "static" / "index.html")
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old
        self.tmp.cleanup()

    def test_bundle_routes_and_traversal(self):
        status, content_type, body = get(self.port, "/api/bundles")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual([item["id"] for item in payload["bundles"]], ["OVERVIEW", "WS9"])
        status, _, body = get(self.port, "/api/bundle?ws=WS9")
        self.assertEqual(status, 200)
        bundle = json.loads(body)
        self.assertEqual(bundle["bundle"]["title"], "Example")
        self.assertEqual(bundle["plan"]["dag"]["groups"][0]["tasks"][0]["id"], "WS9.1")
        self.assertIsNone(bundle["start_prompt"])
        prompt_path = Path(self.tmp.name) / "WS9" / "START-PROMPT.md"
        prompt_path.write_text("You are the WS9 (Example) lead.\n", encoding="utf-8")
        status, _, body = get(self.port, "/api/bundle?ws=WS9")
        self.assertEqual(json.loads(body)["start_prompt"], "You are the WS9 (Example) lead.\n")
        status, _, body = get(self.port, "/api/plan?ws=WS9")
        self.assertEqual(json.loads(body)["start_prompt"], "You are the WS9 (Example) lead.\n")
        status, content_type, body = get(self.port, "/bundle/WS9/page.html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn(b"WS9-PAGE", body)
        status, _, _ = get(self.port, "/bundle/WS9/plan.json")
        self.assertEqual(status, 404)
        status, _, _ = get(self.port, "/bundle/../page.html")
        self.assertEqual(status, 404)
        status, _, _ = get(self.port, "/bundle/WS9/../../agent_dash.py")
        self.assertEqual(status, 404)
        status, _, body = get(self.port, "/static/../agent_dash.py")
        self.assertEqual(status, 404)
        status, _, body = get(self.port, "/api/plan?ws=WS9")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["cols"], ["Shape"])
        self.assertEqual(json.loads(body)["groups"][0]["tasks"][0]["title"], "First task")
        status, _, body = get(self.port, "/api/plan?ws=NOPE")
        self.assertEqual(status, 404)
        status, _, body = get(self.port, "/api/bundle?ws=..")
        self.assertEqual(status, 400)
        status, content_type, body = get(self.port, "/plan")
        self.assertEqual(status, 200)
        self.assertIn(b"Execution", body)
        self.assertIn(b"/static/plan.css", body)
        status, _, body = get(self.port, "/api/workstreams")
        rows = {item["ws"]: item for item in json.loads(body)["workstreams"]}
        self.assertTrue(rows["WS9"]["plan"])
        status, content_type, body = get(self.port, "/api/plan?ws=WS7")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["ws"], "WS7")

    def test_valid_bundle_serves_empty_issues(self):
        status, _, body = get(self.port, "/api/plan?ws=WS9")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["issues"], [])
        status, _, body = get(self.port, "/api/bundle?ws=WS9")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["issues"], [])

    def test_invalid_bundle_serves_with_issues(self):
        plan_path = Path(self.tmp.name) / "WS9" / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        tasks = plan["dag"]["groups"][0]["tasks"]
        tasks[0]["kind"] = "fuzzy-kind"
        tasks[0]["status"] = "bogus-status"
        tasks[0]["deps"] = ["ZZ"]
        tasks[0]["dep_notes"] = {"ZZ": "start only after the signal"}
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        status, _, body = get(self.port, "/api/plan?ws=WS9")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["issues"])
        self.assertTrue(any("fuzzy-kind" in issue for issue in payload["issues"]))
        self.assertTrue(any("bogus-status" in issue for issue in payload["issues"]))
        self.assertEqual(payload["groups"][0]["tasks"][0]["status"], "bogus-status")
        status, _, body = get(self.port, "/api/bundle?ws=WS9")
        self.assertEqual(status, 200)
        bundle = json.loads(body)
        self.assertEqual(bundle["issues"], payload["issues"])

    def test_two_plan_sources_resolve_to_one(self):
        from dash.bundles import resolve_plan

        maps = Path(self.tmp.name) / "maps"
        maps.mkdir()
        (maps / "ws9.json").write_text(
            json.dumps(
                {
                    "dag": {
                        "cols": ["Shape"],
                        "groups": [
                            {"id": "g", "col": 0, "title": "Group", "tasks": [
                                {"id": "WS9.1", "title": "First task", "status": "done"},
                                {"id": "WS9.2", "title": "Second task", "status": "done"},
                            ]}
                        ],
                        "edges": [],
                    },
                    "narrative": {"phases": []},
                }
            ),
            encoding="utf-8",
        )
        (maps / "plans.json").write_text(json.dumps({"WS9": "ws9.json"}), encoding="utf-8")
        old = os.environ.get("AGENT_DASH_PLANS")
        os.environ["AGENT_DASH_PLANS"] = str(maps / "plans.json")
        try:
            source, body = resolve_plan("WS9")
            self.assertEqual(source, "bundle")
            self.assertEqual(body["progress"], {"done": 0, "total": 1, "counts": {"todo": 1, "wip": 0, "pr": 0, "prog": 0, "done": 0, "deferred": 0}, "pct": 0})
            status, _, payload = get(self.port, "/api/plan?ws=WS9")
            self.assertEqual(status, 200)
            plan = json.loads(payload)
            self.assertEqual(plan["progress"]["total"], 1)
            status, _, payload = get(self.port, "/api/bundle?ws=WS9")
            bundle = json.loads(payload)
            self.assertEqual(bundle["progress"], plan["progress"])
            self.assertEqual(bundle["progress"]["total"], 1)
        finally:
            if old is None:
                os.environ.pop("AGENT_DASH_PLANS", None)
            else:
                os.environ["AGENT_DASH_PLANS"] = old

    def test_approvals_routes_and_shell(self):
        ws = Path(self.tmp.name) / "WS9"
        (ws / "approvals.json").write_text(
            json.dumps(
                {
                    "ws": "WS9",
                    "decided_at": None,
                    "decided_by": None,
                    "items": [
                        {
                            "id": "sample-load",
                            "step": "Load the sample set before cutover",
                            "status": "pending",
                            "reversible": False,
                            "conditions": "The sample set is on disk.",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        status, _, body = get(self.port, "/api/bundle?ws=WS9")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["approvals"]["items"][0]["status"], "pending")
        self.assertIs(payload["approvals"]["items"][0]["reversible"], False)
        status, _, body = get(self.port, "/api/approvals?ws=WS9")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["items"][0]["id"], "sample-load")
        status, _, body = get(self.port, "/api/approvals?ws=all")
        self.assertEqual(status, 200)
        rows = {item["ws"]: item for item in json.loads(body)["approvals"]}
        self.assertIn("WS9", rows)
        self.assertIn("OVERVIEW", rows)
        self.assertEqual(rows["OVERVIEW"]["items"], [])
        status, _, _ = get(self.port, "/api/approvals?ws=..")
        self.assertEqual(status, 400)
        status, _, body = get(self.port, "/api/approvals?ws=NOPE")
        self.assertEqual(status, 404)
        status, _, home = get(self.port, "/")
        status, _, plan = get(self.port, "/plan")
        self.assertEqual(home, plan)
        self.assertIn(b"Execution", plan)
        self.assertIn(b"/static/plan.css", plan)
        self.assertIn(b"/static/tokens.css", plan)
        self.assertIn(b"pushState", plan)
        self.assertIn(b"Needs you", plan)


class TrainsApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name
        root = Path(self.tmp.name)
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
                        "cols": ["Shape"],
                        "groups": [{"id": "g", "col": 0, "title": "Group", "tasks": [
                            {"id": "WS9.1", "title": "First task", "status": "todo"},
                            {"id": "WS9.2", "title": "Second task", "status": "pr"},
                        ]}],
                        "edges": [],
                    },
                    "narrative": {"phases": []},
                    "trains": [
                        {
                            "id": "T1",
                            "title": "read-path cutover",
                            "members": ["WS9.1"],
                            "status": "planned",
                            "junk": "dropped",
                        },
                        {
                            "id": "T2",
                            "title": "second cutover",
                            "branch": "train/second",
                            "members": ["WS9.2"],
                            "status": "pr",
                            "pr": 5357,
                            "shared_files": ["api/x.py"],
                            "gate": "pytest api/tests/test_x.py",
                            "note": "(parsed)",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.server, self.thread = start(self.store, ROOT / "static" / "index.html")
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old
        self.tmp.cleanup()

    def test_trains_pass_through(self):
        from dash.bundles import validate_bundle

        self.assertEqual(validate_bundle("WS9"), [])
        status, _, body = get(self.port, "/api/bundle?ws=WS9")
        self.assertEqual(status, 200)
        trains = json.loads(body)["plan"]["trains"]
        self.assertEqual(len(trains), 2)
        first, second = trains
        self.assertEqual(first["id"], "T1")
        self.assertEqual(first["title"], "read-path cutover")
        self.assertEqual(first["members"], ["WS9.1"])
        self.assertEqual(first["status"], "planned")
        self.assertNotIn("junk", first)
        self.assertEqual(second["branch"], "train/second")
        self.assertEqual(second["pr"], 5357)
        self.assertEqual(second["shared_files"], ["api/x.py"])
        self.assertEqual(second["gate"], "pytest api/tests/test_x.py")
        status, _, body = get(self.port, "/api/plan?ws=WS9")
        self.assertEqual(status, 200)
        trains = json.loads(body)["trains"]
        self.assertEqual([row["id"] for row in trains], ["T1", "T2"])
        self.assertEqual(trains[1]["members"], ["WS9.2"])
        self.assertNotIn("junk", trains[0])

    def test_trains_validation_issues(self):
        from dash.bundles import validate_bundle

        plan_path = Path(self.tmp.name) / "WS9" / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["trains"] = [
            {"id": "T1", "title": "first", "members": ["WS9.1", "WS9.2"], "status": "planned"},
            {"id": "T2", "title": "second", "members": ["WS9.2"], "status": "bogus"},
        ]
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        issues = validate_bundle("WS9")
        self.assertTrue(any("task WS9.2 is in two trains" in item for item in issues))
        self.assertTrue(any("status 'bogus'" in item for item in issues))

    def test_plans_map_trains(self):
        from dash.plans import load as load_plan

        tmp = Path(self.tmp.name) / "maps"
        tmp.mkdir()
        (tmp / "ws9.json").write_text(
            json.dumps(
                {
                    "dag": {
                        "cols": ["Shape"],
                        "groups": [{"id": "g", "col": 0, "title": "Group", "tasks": [
                            {"id": "WS9.1", "title": "First task", "status": "todo"},
                        ]}],
                        "edges": [],
                    },
                    "narrative": {"phases": [{"name": "Shape", "goal": "Ship.", "gate": "Done."}]},
                    "trains": [
                        {"id": "T1", "title": "cutover", "members": ["WS9.1"], "status": "planned", "junk": 1},
                    ],
                }
            ),
            encoding="utf-8",
        )
        (tmp / "plans.json").write_text(json.dumps({"WS9": "ws9.json"}), encoding="utf-8")
        plan = load_plan("WS9", tmp / "plans.json")
        self.assertEqual([row["id"] for row in plan["trains"]], ["T1"])
        self.assertEqual(plan["trains"][0]["members"], ["WS9.1"])
        self.assertNotIn("junk", plan["trains"][0])
        bare = load_plan("WS9", tmp / "plans.json")
        plan_path = tmp / "ws9.json"
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        del data["trains"]
        plan_path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(load_plan("WS9", tmp / "plans.json")["trains"], [])
        self.assertEqual(bare["ws"], "WS9")


class GatesApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name
        root = Path(self.tmp.name)
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
                        "cols": ["Shape"],
                        "groups": [{"id": "g", "col": 0, "title": "Group", "tasks": [
                            {"id": "WS9.0", "title": "Upstream signal is in", "status": "done", "kind": "gate"},
                            {"id": "WS9.1", "title": "First task", "status": "todo", "deps": ["WS9.0"],
                             "dep_notes": {"WS9.0": "start only after the signal"}},
                        ]}],
                        "edges": [],
                    },
                    "narrative": {"phases": []},
                }
            ),
            encoding="utf-8",
        )
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.server, self.thread = start(self.store, ROOT / "static" / "index.html")
        self.port = self.server.server_address[1]

    def tearDown(self):
        stop(self.server, self.thread)
        self.store.close()
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old
        self.tmp.cleanup()

    def test_gate_and_dep_notes_pass_through(self):
        from dash.bundles import validate_bundle

        self.assertEqual(validate_bundle("WS9"), [])
        for route in ("/api/bundle?ws=WS9", "/api/plan?ws=WS9"):
            status, _, body = get(self.port, route)
            self.assertEqual(status, 200)
            payload = json.loads(body)
            if route.startswith("/api/bundle?"):
                tasks = payload["plan"]["dag"]["groups"][0]["tasks"]
            else:
                tasks = payload["groups"][0]["tasks"]
            rows = {task["id"]: task for task in tasks}
            self.assertEqual(rows["WS9.0"]["kind"], "gate")
            self.assertEqual(rows["WS9.1"]["deps"], ["WS9.0"])
            self.assertEqual(rows["WS9.1"]["dep_notes"], {"WS9.0": "start only after the signal"})

    def test_plans_map_gate_and_dep_notes(self):
        from dash.plans import load as load_plan

        tmp = Path(self.tmp.name) / "maps"
        tmp.mkdir()
        (tmp / "ws9.json").write_text(
            json.dumps(
                {
                    "dag": {
                        "cols": ["Shape"],
                        "groups": [{"id": "g", "col": 0, "title": "Group", "tasks": [
                            {"id": "WS9.0", "title": "Upstream signal is in", "status": "done", "kind": "gate", "junk": 1},
                            {"id": "WS9.1", "title": "First task", "status": "todo", "deps": ["WS9.0"],
                             "dep_notes": {"WS9.0": "start only after the signal"}},
                        ]}],
                        "edges": [],
                    },
                    "narrative": {"phases": []},
                }
            ),
            encoding="utf-8",
        )
        (tmp / "plans.json").write_text(json.dumps({"WS9": "ws9.json"}), encoding="utf-8")
        plan = load_plan("WS9", tmp / "plans.json")
        rows = {task["id"]: task for task in plan["groups"][0]["tasks"]}
        self.assertEqual(rows["WS9.0"]["kind"], "gate")
        self.assertNotIn("junk", rows["WS9.0"])
        self.assertEqual(rows["WS9.1"]["dep_notes"], {"WS9.0": "start only after the signal"})


if __name__ == "__main__":
    unittest.main()
