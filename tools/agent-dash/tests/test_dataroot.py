"""Data-root resolution: env, then config, then the default. No live state."""

import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dash import dataroot


def _clean_env():
    """Clear the data env vars, returning a patcher the caller stops."""
    patcher = mock.patch.dict(
        os.environ,
        {
            "AGENT_DASH_DATA_ROOT": "",
            "AGENT_DASH_STATE_DIR": "",
            "AGENT_DASH_WORKSTREAMS": "",
            "AGENT_DASH_HUB_DIR": "",
        },
    )
    patcher.start()
    return patcher


class DataRootTests(unittest.TestCase):
    def setUp(self):
        self._env = _clean_env()
        self.addCleanup(self._env.stop)

    def test_default_root_is_todays_state_dir(self):
        self.assertEqual(dataroot.default_root(), Path.home() / ".local" / "share" / "agent-dash")
        self.assertEqual(dataroot.data_root(None), dataroot.default_root())
        self.assertEqual(dataroot.data_root({}), dataroot.default_root())

    def test_env_wins_over_config_and_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AGENT_DASH_DATA_ROOT": tmp}):
                self.assertEqual(dataroot.data_root({"data_root": "/other"}), Path(tmp))

    def test_config_data_root_used_when_env_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dataroot.data_root({"data_root": tmp}), Path(tmp))

    def test_state_dir_falls_back_to_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dataroot.state_dir({"data_root": tmp}), Path(tmp))
            self.assertEqual(
                dataroot.state_dir({"data_root": tmp, "state_dir": str(Path(tmp) / "state")}),
                Path(tmp) / "state",
            )
            with mock.patch.dict(os.environ, {"AGENT_DASH_STATE_DIR": str(Path(tmp) / "env")}):
                self.assertEqual(dataroot.state_dir({"data_root": tmp}), Path(tmp) / "env")

    def test_workstreams_dir_default_and_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dataroot.workstreams_dir({"data_root": tmp}), Path(tmp) / "workstreams")
            other = str(Path(tmp) / "bundles")
            with mock.patch.dict(os.environ, {"AGENT_DASH_WORKSTREAMS": other}):
                self.assertEqual(dataroot.workstreams_dir({"data_root": tmp}), Path(other))

    def test_hub_dir_empty_unless_configured_or_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dataroot.hub_dir({"data_root": tmp}), "")
            hub = Path(tmp) / "hub"
            hub.mkdir()
            self.assertEqual(dataroot.hub_dir({"data_root": tmp}), str(hub))
            with mock.patch.dict(os.environ, {"AGENT_DASH_HUB_DIR": str(Path(tmp) / "elsewhere")}):
                self.assertEqual(dataroot.hub_dir({"data_root": tmp}), str(Path(tmp) / "elsewhere"))

    def test_project_file_lives_in_the_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                dataroot.project_file({"data_root": tmp}), Path(tmp) / "project.json"
            )

    def test_data_root_hash_is_opaque_and_stable(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first = dataroot.data_root_hash(None, one)
            self.assertEqual(len(first), 12)
            self.assertEqual(first, dataroot.data_root_hash(None, one))
            self.assertNotEqual(first, dataroot.data_root_hash(None, two))
            self.assertNotIn(one, first)

    def test_any_local_path_works_including_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            spaced = str(Path(tmp) / "my cloud folder")
            os.makedirs(spaced)
            self.assertEqual(dataroot.data_root({"data_root": spaced}), Path(spaced))
            self.assertEqual(dataroot.project_file({"data_root": spaced}), Path(spaced) / "project.json")

    def test_local_storage_roundtrip(self):
        store = dataroot.LocalStorage()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.json"
            self.assertFalse(store.exists(path))
            store.write_text(path, '{"name": "Demo project"}')
            self.assertTrue(store.exists(path))
            self.assertEqual(store.read_text(path), '{"name": "Demo project"}')
            self.assertEqual([item.name for item in store.list(tmp)], ["project.json"])
            tmp_path = Path(tmp) / "project.json.tmp"
            store.write_text(tmp_path, "{}")
            store.replace(tmp_path, path)
            self.assertEqual(store.read_text(path), "{}")
            self.assertFalse(tmp_path.exists())


if __name__ == "__main__":
    unittest.main()
