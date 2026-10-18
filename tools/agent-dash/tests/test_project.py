"""Project descriptor: init/show/set, validation, and the /api/project payload."""

import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dash import errors, project
from dash.store import Store


def _clean_env(root: str):
    """Point every data env var at a temp tree. Blank hub means unset."""
    patcher = mock.patch.dict(
        os.environ,
        {
            "AGENT_DASH_DATA_ROOT": root,
            "AGENT_DASH_STATE_DIR": os.path.join(root, "state"),
            "AGENT_DASH_WORKSTREAMS": os.path.join(root, "workstreams"),
            "AGENT_DASH_HUB_DIR": "",
        },
    )
    patcher.start()
    return patcher


def _bundle(ws_root: Path, ws: str, state: str | None = None) -> None:
    path = ws_root / ws
    path.mkdir(parents=True, exist_ok=True)
    meta = {"id": ws, "title": f"{ws} title", "short": ws, "order": 10, "kind": "workstream"}
    if state is not None:
        meta["state"] = state
    (path / "bundle.json").write_text(json.dumps(meta), encoding="utf-8")


class ProjectCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "dataroot")
        self._env = _clean_env(self.root)
        self.addCleanup(self._env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_init_creates_template_and_dirs(self):
        self.assertEqual(project.cmd_project(["init", "Demo project", "--data-root", self.root]), errors.OK)
        dest = Path(self.root) / "project.json"
        self.assertTrue(dest.is_file())
        self.assertTrue((Path(self.root) / "workstreams").is_dir())
        data = json.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "Demo project")
        self.assertEqual(data["short"], "demo-project")
        self.assertEqual(data["target"], None)
        self.assertEqual(data["phases"], [])

    def test_init_refuses_to_overwrite(self):
        self.assertEqual(project.cmd_project(["init", "Demo project", "--data-root", self.root]), errors.OK)
        self.assertEqual(project.cmd_project(["init", "Other name", "--data-root", self.root]), errors.USAGE)
        data = json.loads((Path(self.root) / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "Demo project")

    def test_init_needs_a_name(self):
        self.assertEqual(project.cmd_project(["init"]), errors.USAGE)
        self.assertEqual(project.cmd_project(["nope"]), errors.USAGE)

    def test_show_missing_file_is_fine(self):
        self.assertEqual(project.cmd_project(["show", "--data-root", self.root, "--json"]), errors.OK)

    def test_show_and_set_roundtrip(self):
        self.assertEqual(project.cmd_project(["init", "Demo project", "--data-root", self.root]), errors.OK)
        self.assertEqual(
            project.cmd_project(["set", "description", "What ships.", "--data-root", self.root]),
            errors.OK,
        )
        self.assertEqual(
            project.cmd_project(["set", "target", "Gate freeze", "--data-root", self.root]),
            errors.OK,
        )
        data = json.loads((Path(self.root) / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(data["description"], "What ships.")
        self.assertEqual(data["target"], "Gate freeze")
        self.assertEqual(project.cmd_project(["show", "--data-root", self.root]), errors.OK)

    def test_set_target_empty_clears_to_null(self):
        self.assertEqual(project.cmd_project(["init", "Demo project", "--data-root", self.root]), errors.OK)
        self.assertEqual(
            project.cmd_project(["set", "target", "Gate freeze", "--data-root", self.root]),
            errors.OK,
        )
        self.assertEqual(project.cmd_project(["set", "target", "", "--data-root", self.root]), errors.OK)
        data = json.loads((Path(self.root) / "project.json").read_text(encoding="utf-8"))
        self.assertIsNone(data["target"])

    def test_set_rejects_bad_field_empty_name_and_missing_file(self):
        self.assertEqual(
            project.cmd_project(["set", "bogus", "x", "--data-root", self.root]),
            errors.USAGE,
        )
        self.assertEqual(
            project.cmd_project(["set", "name", "x", "--data-root", self.root]),
            errors.NOT_FOUND,
        )
        self.assertEqual(project.cmd_project(["init", "Demo project", "--data-root", self.root]), errors.OK)
        self.assertEqual(
            project.cmd_project(["set", "name", "  ", "--data-root", self.root]),
            errors.INVALID_DATA,
        )

    def test_show_flags_invalid_data(self):
        self.assertEqual(project.cmd_project(["init", "Demo project", "--data-root", self.root]), errors.OK)
        dest = Path(self.root) / "project.json"
        dest.write_text(json.dumps({"name": "", "goals": "not-a-list"}), encoding="utf-8")
        self.assertEqual(project.cmd_project(["show", "--data-root", self.root]), errors.INVALID_DATA)


class ProjectValidationTests(unittest.TestCase):
    def test_clean_template_validates(self):
        self.assertEqual(project.validate_project(project._template("Demo project")), [])

    def test_issues_name_the_field_before_the_value(self):
        issues = project.validate_project({"name": "", "goals": "nope", "phases": [{"id": "p1"}]})
        self.assertTrue(any('"name"' in issue for issue in issues), issues)
        self.assertTrue(any('"goals"' in issue for issue in issues), issues)
        self.assertTrue(any("p1" in issue and "phase" in issue for issue in issues), issues)

    def test_duplicate_phase_ids_and_bad_gate(self):
        data = {
            "name": "Demo project",
            "phases": [
                {"id": "p1", "title": "First cut", "gate": ["WS1"]},
                {"id": "p1", "title": "Second cut", "gate": {"bogus": True}},
            ],
        }
        issues = project.validate_project(data)
        self.assertTrue(any("duplicate" in issue for issue in issues), issues)
        self.assertTrue(any('"gate"' in issue for issue in issues), issues)

    def test_phase_state_rollup(self):
        states = {"WS1": "done", "WS2": "archived"}
        self.assertEqual(project.phase_state(["WS1", "WS2"], states), "done")
        self.assertEqual(project.phase_state(["WS1", "WS9"], {**states, "WS9": "active"}), "active")
        self.assertEqual(project.phase_state(["WS9"], {"WS9": "planned"}), "planned")
        self.assertEqual(project.phase_state(["WS9"], {"WS9": "dropped"}), "dropped")
        self.assertEqual(project.phase_state([], states), "planned")
        self.assertEqual(project.phase_state(["WS1", "WS9"], {**states, "WS9": "planned"}), "active")

    def test_phase_members_list_id_and_text(self):
        self.assertEqual(project.phase_members({"gate": ["WS1", "WS2"]}), ["WS1", "WS2"])
        self.assertEqual(project.phase_members({"gate": "WS1"}), ["WS1"])
        self.assertEqual(project.phase_members({"gate": "contract frozen, parity on"}), [])
        self.assertEqual(project.phase_members({}), [])

    def test_bundle_state_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            self.assertEqual(project.bundle_state("WS9", ws_root), "planned")
            _bundle(ws_root, "WS1")
            self.assertEqual(project.bundle_state("WS1", ws_root), "active")
            _bundle(ws_root, "WS2", "done")
            self.assertEqual(project.bundle_state("WS2", ws_root), "done")
            _bundle(ws_root, "WS3", "bogus")
            self.assertEqual(project.bundle_state("WS3", ws_root), "active")


class ProjectPayloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "dataroot")
        self._env = _clean_env(self.root)
        self.addCleanup(self._env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")
        self.addCleanup(self.store.close)

    def test_missing_file_payload_falls_back(self):
        payload = project.build_payload(self.store, {"data_root": self.root}, self.root)
        self.assertIsNone(payload["name"])
        self.assertEqual(payload["phases"], [])
        self.assertEqual(payload["issues"], [])
        self.assertEqual(len(payload["data_root_hash"]), 12)
        self.assertEqual(set(payload["counts"]), set(project.LIFECYCLE_STATES))
        self.assertIn("tokens", payload["totals_all_time"])

    def test_phases_compute_state_and_counts(self):
        ws_root = Path(self.root) / "workstreams"
        _bundle(ws_root, "WS1", "done")
        _bundle(ws_root, "WS2")
        dest = Path(self.root) / "project.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            json.dumps(
                {
                    "name": "Demo project",
                    "short": "demo",
                    "description": "Seeded.",
                    "goals": ["Ship the first cut"],
                    "started": "2026-10-15",
                    "target": None,
                    "phases": [
                        {"id": "p1", "title": "First cut", "summary": "", "gate": ["WS1"]},
                        {"id": "p2", "title": "Second cut", "summary": "", "gate": ["WS1", "WS2"]},
                        {"id": "p3", "title": "Later", "summary": "", "gate": "gate text here"},
                    ],
                    "links": [],
                    "owner_notes": "",
                }
            ),
            encoding="utf-8",
        )
        payload = project.build_payload(self.store, {"data_root": self.root}, self.root)
        self.assertEqual(payload["name"], "Demo project")
        by_id = {phase["id"]: phase for phase in payload["phases"]}
        self.assertEqual(by_id["p1"]["state"], "done")
        self.assertEqual(by_id["p2"]["state"], "active")
        self.assertEqual(by_id["p3"]["state"], "planned")
        self.assertEqual(by_id["p1"]["members"], [{"ws": "WS1", "state": "done"}])
        self.assertEqual(payload["counts"]["done"], 1)
        self.assertEqual(payload["counts"]["active"], 1)
        self.assertEqual(payload["issues"], [])

    def test_totals_reuse_the_snapshot_summary(self):
        now = 1_761_999_960.0
        self.store.upsert_run(
            {
                "run_id": "muse:s:ws1-a",
                "source": "muse",
                "tier": "muse",
                "label": "ws1-a",
                "status": "done",
                "started_at": now - 200,
                "ended_at": now - 100,
            }
        )
        self.store.add_usage(
            {
                "ts": now - 150,
                "session_id": "s",
                "run_id": "muse:s:ws1-a",
                "model": "muse-1",
                "tool": "muse",
                "message_id": "m1",
                "input": 400,
                "output": 20,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 0,
            }
        )
        payload = project.build_payload(self.store, {"data_root": self.root}, self.root, now=now)
        tokens = payload["totals_all_time"]["tokens"]
        self.assertEqual(tokens["input"] + tokens["output"], 420)
        snapshot = self.store.snapshot_cached(now, "all")
        self.assertEqual(payload["totals_all_time"], snapshot["totals_all_time"])

    def test_no_filesystem_path_leaks(self):
        payload = project.build_payload(self.store, {"data_root": self.root}, self.root)
        blob = json.dumps(payload)
        self.assertNotIn(self.root, blob)
        self.assertNotIn(self.tmp.name, blob)


if __name__ == "__main__":
    unittest.main()
