"""Dashboard evolution: new workstreams appear on their own, lifecycle stays honest.

Synthetic bundles and runs only. No live data, no fixtures with private ids.
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash.store import Store


def _cfg(state_dir):
    import agent_dash

    cfg = agent_dash.default_cfg()
    cfg.update({"state_dir": str(state_dir)})
    cfg["roots"]["scratchpads"] = []
    return cfg


def _make_bundle(wsroot, ws, state="active", title=None, tasks=None):
    from dash.bundles import cmd_ws

    assert cmd_ws(["new", ws, title or ("Probe %s" % ws)]) == 0
    if state != "active":
        assert cmd_ws(["state", ws, state]) == 0
    if tasks is not None:
        plan_path = Path(wsroot) / ws / "plan.json"
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        data["dag"]["groups"] = [
            {"id": "build", "col": 0, "title": "Build", "tasks": tasks}
        ]
        plan_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return Path(wsroot) / ws


def _finish_all_tasks(wsroot, ws):
    plan_path = Path(wsroot) / ws / "plan.json"
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    for group in data["dag"]["groups"]:
        for task in group["tasks"]:
            task["status"] = "done"
    plan_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class BundleFallbackTest(unittest.TestCase):
    def test_planned_active_done_bundles_gain_zero_rows(self):
        from dash.bundles import cmd_ws
        from dash.workstates import bundle_fallback_rows, filter_rows

        with tempfile.TemporaryDirectory() as tmp:
            wsroot = Path(tmp) / "ws"
            wsroot.mkdir()
            with mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": str(wsroot)}):
                self.assertEqual(cmd_ws(["new", "WS90", "Planned lane", "--state", "planned"]), 0)
                self.assertEqual(cmd_ws(["new", "WS91", "Active lane"]), 0)
                self.assertEqual(cmd_ws(["new", "WS92", "Done lane"]), 0)
                self.assertEqual(cmd_ws(["state", "WS92", "done"]), 0)
                rows = bundle_fallback_rows(set(), plan_ids={"WS90", "WS91", "WS92"})
                by_id = {row["ws"]: row for row in rows}
                self.assertEqual(sorted(by_id), ["WS90", "WS91", "WS92"])
                self.assertEqual(by_id["WS90"]["state"], "planned")
                self.assertEqual(by_id["WS91"]["state"], "active")
                self.assertEqual(by_id["WS92"]["state"], "done")
                for row in rows:
                    self.assertEqual(row["live_agents"], 0)
                    self.assertEqual(row["tokens"], 0)
                    self.assertIsNone(row["last_activity"])
                    self.assertTrue(row["plan"])
                from dash.workstates import all_bundle_meta

                meta = all_bundle_meta()
                kept = {row["ws"] for row in filter_rows(rows, None, meta)}
                self.assertIn("WS90", kept)
                self.assertIn("WS91", kept)
                self.assertIn("WS92", kept)

    def test_program_and_overview_never_gain_rows(self):
        from dash.workstates import bundle_fallback_rows

        bundles = [
            {"id": "OVERVIEW", "title": "Overview", "kind": "program", "state": "active",
             "state_changed_at": None, "archived_at": None, "depends_on": [], "milestone": None},
            {"id": "TOOLS", "title": "Tools", "kind": "program", "state": "active",
             "state_changed_at": None, "archived_at": None, "depends_on": [], "milestone": None},
            {"id": "WS93", "title": "Real", "kind": "workstream", "state": "planned",
             "state_changed_at": None, "archived_at": None, "depends_on": [], "milestone": None},
        ]
        rows = bundle_fallback_rows(set(), bundles=bundles, plan_ids={"WS93"})
        self.assertEqual([row["ws"] for row in rows], ["WS93"])


class WorkstreamsApiIncludesPlannedNoRunsTest(unittest.TestCase):
    def test_planned_bundle_with_no_runs_is_served(self):
        import threading
        from dash.bundles import cmd_ws
        from dash.server import DashServer
        from dash.sources import Registry

        with tempfile.TemporaryDirectory() as tmp:
            wsroot = Path(tmp) / "ws"
            wsroot.mkdir()
            with mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": str(wsroot)}):
                self.assertEqual(cmd_ws(["new", "WS90", "Planned lane", "--state", "planned"]), 0)
                store = Store(Path(tmp) / "dash.sqlite")
                try:
                    server = DashServer(store, Registry([]), "127.0.0.1", 0, cfg=_cfg(tmp))
                    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
                    thread.start()
                    try:
                        import urllib.request

                        url = f"http://127.0.0.1:{server.server_address[1]}/api/workstreams"
                        with urllib.request.urlopen(url, timeout=5) as response:
                            payload = json.loads(response.read().decode("utf-8"))
                        rows = {row["ws"]: row for row in payload["workstreams"]}
                        self.assertIn("WS90", rows)
                        self.assertEqual(rows["WS90"]["state"], "planned")
                    finally:
                        server.stop.set()
                        server.shutdown()
                        thread.join(3)
                        server.server_close()
                finally:
                    store.close()


class StateIncludesPlannedNoRunsTest(unittest.TestCase):
    def test_state_workstreams_include_planned_no_runs(self):
        from dash.bundles import cmd_ws
        from dash.state import build_state
        from dash.sources import Registry

        with tempfile.TemporaryDirectory() as tmp:
            wsroot = Path(tmp) / "ws"
            wsroot.mkdir()
            with mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": str(wsroot)}):
                self.assertEqual(cmd_ws(["new", "WS90", "Planned lane", "--state", "planned"]), 0)
                store = Store(Path(tmp) / "dash.sqlite")
                try:
                    payload = build_state(store, Registry([]), cfg=_cfg(tmp), now=time.time())
                    rows = {row["id"]: row for row in payload["workstreams"]}
                    self.assertIn("WS90", rows)
                    self.assertEqual(rows["WS90"]["state"], "planned")
                finally:
                    store.close()


class PlannedLiveDoctorTest(unittest.TestCase):
    def test_planned_with_runs_names_activate_command(self):
        from dash.bundles import cmd_ws
        from dash.doctor import collect_problems

        with tempfile.TemporaryDirectory() as tmp:
            wsroot = Path(tmp) / "ws"
            wsroot.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            old = os.environ.get("AGENT_DASH_WORKSTREAMS")
            os.environ["AGENT_DASH_WORKSTREAMS"] = str(wsroot)
            try:
                self.assertEqual(cmd_ws(["new", "WS90", "Planned lane", "--state", "planned"]), 0)
                store = Store(state / "dash.sqlite")
                try:
                    now = time.time()
                    store.upsert_run(
                        {
                            "run_id": "muse:s1:ws90-first",
                            "source": "muse",
                            "tier": "muse",
                            "label": "ws90-WS90.1-first",
                            "session_id": "s1",
                            "status": "done",
                            "started_at": now - 3600,
                            "ended_at": now - 3500,
                        }
                    )
                    with mock.patch("dash.doctor._home_scratch", return_value=str(Path(tmp) / "noscratch")):
                        result = collect_problems(
                            store, _cfg(state), now,
                            health={"ok": True, "sources": []}, processes=[],
                        )
                    hits = [item for item in result["problems"] if item["code"] == "planned-live"]
                    self.assertEqual(len(hits), 1)
                    self.assertEqual(hits[0]["ws"], "WS90")
                    self.assertEqual(hits[0]["fix_command"], "python3 agent_dash.py ws state WS90 active")
                    self.assertIn("first agent", hits[0]["detail"])
                finally:
                    store.close()
            finally:
                if old is None:
                    os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
                else:
                    os.environ["AGENT_DASH_WORKSTREAMS"] = old


class UnplannedDoctorTest(unittest.TestCase):
    def test_runs_without_bundle_name_ws_new_per_id(self):
        from dash.bundles import cmd_ws
        from dash.doctor import collect_problems

        with tempfile.TemporaryDirectory() as tmp:
            wsroot = Path(tmp) / "ws"
            wsroot.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            old = os.environ.get("AGENT_DASH_WORKSTREAMS")
            os.environ["AGENT_DASH_WORKSTREAMS"] = str(wsroot)
            # Hermetic data root: no project.json here, so the ignore list
            # reads empty and the ids under test keep firing even when a
            # live project.json carries its own list.
            old_root = os.environ.get("AGENT_DASH_DATA_ROOT")
            os.environ["AGENT_DASH_DATA_ROOT"] = str(Path(tmp))
            try:
                self.assertEqual(cmd_ws(["new", "WS1", "Real work"]), 0)
                store = Store(state / "dash.sqlite")
                try:
                    now = time.time()
                    for ws in ("WS345", "PLANNING", "TOOLS"):
                        store.upsert_run(
                            {
                                "run_id": "muse:%s:probe" % ws.lower(),
                                "source": "muse",
                                "tier": "muse",
                                "label": "%s-probe" % ws.lower(),
                                "session_id": "s-%s" % ws.lower(),
                                "status": "done",
                                "started_at": now - 7200,
                                "ended_at": now - 3600,
                            }
                        )
                        # Attribute the run to the unplanned id directly.
                        with store._lock:
                            store._conn.execute(
                                "UPDATE runs SET workstream=? WHERE run_id=?", (ws, "muse:%s:probe" % ws.lower())
                            )
                            store._conn.commit()
                    with mock.patch("dash.doctor._home_scratch", return_value=str(Path(tmp) / "noscratch")):
                        result = collect_problems(
                            store, _cfg(state), now,
                            health={"ok": True, "sources": []}, processes=[],
                        )
                    hits = {item["ws"]: item for item in result["problems"] if item["code"] == "unplanned-activity"}
                    self.assertEqual(sorted(hits), ["PLANNING", "TOOLS", "WS345"])
                    for ws, item in hits.items():
                        self.assertIn("python3 agent_dash.py ws new %s" % ws, item["fix_command"])
                        self.assertIn("Active without a plan", item["title"])
                    self.assertIn("Shared tooling", hits["TOOLS"]["fix_command"])
                    self.assertIn("Planning lane", hits["PLANNING"]["fix_command"])
                finally:
                    store.close()
            finally:
                if old is None:
                    os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
                else:
                    os.environ["AGENT_DASH_WORKSTREAMS"] = old
                if old_root is None:
                    os.environ.pop("AGENT_DASH_DATA_ROOT", None)
                else:
                    os.environ["AGENT_DASH_DATA_ROOT"] = old_root


class DoneNagActiveHundredTest(unittest.TestCase):
    def test_active_at_100_nags_after_7_days(self):
        from dash.bundles import cmd_ws
        from dash.doctor import collect_problems

        with tempfile.TemporaryDirectory() as tmp:
            wsroot = Path(tmp) / "ws"
            wsroot.mkdir()
            state = Path(tmp) / "state"
            state.mkdir()
            old = os.environ.get("AGENT_DASH_WORKSTREAMS")
            os.environ["AGENT_DASH_WORKSTREAMS"] = str(wsroot)
            try:
                _make_bundle(
                    str(wsroot), "WS1", state="active", title="Sample lane",
                    tasks=[{"id": "WS1.1", "title": "First task", "status": "todo"}],
                )
                _finish_all_tasks(str(wsroot), "WS1")
                bundle_path = Path(wsroot) / "WS1"
                old_ts = time.time() - 8 * 86400
                history = bundle_path / "history.jsonl"
                history.write_text(
                    json.dumps({"ts": old_ts, "task": "WS1.1", "from": "todo", "to": "done"}) + "\n",
                    encoding="utf-8",
                )
                plan_path = bundle_path / "plan.json"
                os.utime(plan_path, (old_ts, old_ts))
                store = Store(state / "dash.sqlite")
                try:
                    now = time.time()
                    with mock.patch("dash.doctor._home_scratch", return_value=str(Path(tmp) / "noscratch")):
                        result = collect_problems(
                            store, _cfg(state), now,
                            health={"ok": True, "sources": []}, processes=[],
                        )
                    hits = [item for item in result["problems"] if item["code"] == "done-not-archived" and item["ws"] == "WS1"]
                    self.assertEqual(len(hits), 1)
                    self.assertEqual(hits[0]["fix_command"], "python3 agent_dash.py ws archive WS1")
                    self.assertIn("100%", hits[0]["title"])
                finally:
                    store.close()
            finally:
                if old is None:
                    os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
                else:
                    os.environ["AGENT_DASH_WORKSTREAMS"] = old


class PageShelfRulesTest(unittest.TestCase):
    STATIC = Path(__file__).resolve().parent.parent / "static"

    def _text(self, *parts):
        return (self.STATIC.joinpath(*parts)).read_text(encoding="utf-8")

    def test_planned_shelf_merges_bundle_list(self):
        planned = self._text("cards", "planned.js")
        self.assertIn("state.bundles", planned)
        home = self._text("cards", "project-home.js")
        self.assertIn("shelfRows", home)
        self.assertIn("state.bundles", home)

    def test_current_shows_first_agent_hint(self):
        cards = self._text("cards", "ws-cards.js")
        self.assertIn("first agent started", cards)
        self.assertIn("planned", cards)

    def test_ready_to_archive_chip_with_copy_command(self):
        cards = self._text("cards", "ws-cards.js")
        self.assertIn("Ready to archive", cards)
        self.assertIn("python3 agent_dash.py ws archive", cards)
        home = self._text("cards", "project-home.js")
        self.assertIn("Ready to archive", home)

    def test_unplanned_strip_without_warning_wording(self):
        home = self._text("cards", "project-home.js")
        self.assertIn("Unplanned activity", home)
        self.assertIn("Active without a plan", home)
        self.assertIn("python3 agent_dash.py ws new", home)
        for bad in ("warn", "critical", "health"):
            section = home[home.index("unplannedStrip"):home.index("unplannedStrip") + 2000]
            self.assertNotIn(bad, section.lower())

    def test_current_caps_with_show_more(self):
        cards = self._text("cards", "ws-cards.js")
        self.assertIn("discloseRows(rows, capKey,", cards)
        self.assertIn("discloseButton(info,", cards)


if __name__ == "__main__":
    unittest.main()
