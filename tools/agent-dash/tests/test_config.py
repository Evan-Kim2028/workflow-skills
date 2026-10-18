"""Config defaults. No personal sessions."""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_dash import default_cfg, load_cfg
from dash import dataroot
from dash.locate import layered_path, scratch_roots
from dash.sources.hub import poll as hub_poll
from dash.store import Store


class ConfigTests(unittest.TestCase):
    def test_code_default_scratch_is_empty(self):
        self.assertEqual(default_cfg()["roots"]["scratchpads"], [])
        self.assertEqual(default_cfg()["hub_dir"], "")
        self.assertEqual(scratch_roots({}), [])
        self.assertEqual(scratch_roots({"roots": {"scratchpads": ""}}), [])
        self.assertEqual(scratch_roots({"roots": {"scratchpads": []}}), [])

    def test_scratch_glob_returns_directories_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            one = Path(tmp) / "one"
            two = Path(tmp) / "two"
            one.mkdir()
            two.mkdir()
            (Path(tmp) / "notes.txt").write_text("x", encoding="utf-8")
            found = scratch_roots({"roots": {"scratchpads": [str(Path(tmp) / "*")]}})
            self.assertEqual(set(found), {str(one), str(two)})

    def test_local_file_replaces_example(self):
        self.addCleanup(os.environ.__setitem__, "AGENT_DASH_IGNORE_LOCAL", "1")
        os.environ["AGENT_DASH_IGNORE_LOCAL"] = ""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "map.example.json").write_text(
                json.dumps({"exact": {"a": "WS1"}, "regex": [], "sessions": {}}),
                encoding="utf-8",
            )
            (base / "map.local.json").write_text(
                json.dumps({"exact": {"a": "WS9"}, "regex": [], "sessions": {}}),
                encoding="utf-8",
            )
            found = layered_path("map", "AGENT_DASH_MAP_UNUSED_FOR_TEST", base)
            data = json.loads(found.read_text(encoding="utf-8"))
            self.assertEqual(data["exact"]["a"], "WS9")

    def test_example_config_loads_and_env_wins(self):
        cfg = load_cfg()
        pads = cfg["roots"]["scratchpads"]
        self.assertTrue(any("agent-scratch" in str(item) for item in pads))
        self.assertEqual(cfg["host"], "127.0.0.1")
        with tempfile.TemporaryDirectory() as tmp:
            key = "AGENT_DASH_HUB_DIR"
            previous = os.environ.get(key)
            os.environ[key] = tmp
            try:
                overridden = load_cfg()
            finally:
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous
            self.assertEqual(overridden["hub_dir"], tmp)

    def test_data_root_default_env_and_state_fallback(self):
        self.assertEqual(default_cfg()["data_root"], default_cfg()["state_dir"])
        with tempfile.TemporaryDirectory() as tmp:
            key = "AGENT_DASH_DATA_ROOT"
            previous = os.environ.get(key)
            os.environ[key] = tmp
            try:
                overridden = load_cfg()
            finally:
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous
            self.assertEqual(overridden["data_root"], tmp)
            self.assertEqual(overridden["state_dir"], tmp)
            self.assertEqual(dataroot.data_root(overridden), Path(tmp))
            self.assertEqual(dataroot.state_dir(overridden), Path(tmp))
            # This module points AGENT_DASH_WORKSTREAMS at the fixtures;
            # clear it to see the data-root default.
            with mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": ""}):
                self.assertEqual(dataroot.workstreams_dir(overridden), Path(tmp) / "workstreams")

    def test_unset_hub_poll_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                hub_poll(store, {}, 1)
                hub_poll(store, {"hub_dir": str(Path(tmp) / "missing")}, 1)
                self.assertEqual(store.query_workstreams(set()), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
