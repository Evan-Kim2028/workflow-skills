"""Stat-based discovery for manifests: settled folders are not re-listed.

The folder cache trusts a directory's (inode, mtime_ns) to say whether its
entries changed. These tests pin that assumption on this filesystem, and that
new TSVs, new nested run folders and in-place TSV appends are still seen on
the next poll, while a settled folder costs no per-name checks.
"""

from __future__ import annotations

import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import rundirs  # noqa: E402
from dash.sources import manifests  # noqa: E402

PARENT = "cccccccc-cccc-cccc-cccc-cccccccccccc"
MUSE = "11111111-1111-1111-1111-111111111111"
GLM = "22222222-2222-2222-2222-222222222222"
NOW = 1_792_000_000


class MemStore:
    def __init__(self):
        self.offsets = {}
        self.runs = {}

    def get_offset(self, path):
        return self.offsets.get(path)

    def set_offset(self, path, inode, offset, size, mtime):
        self.offsets[path] = (inode, offset)

    def upsert_run(self, row):
        self.runs[row["run_id"]] = row


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


class DirMtimeAssumptionTests(unittest.TestCase):
    """The folder cache's premise: entry changes move the directory mtime."""

    def test_create_rename_and_delete_move_the_directory_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = os.stat(tmp).st_mtime_ns
            time.sleep(0.01)
            _write(os.path.join(tmp, "a.tsv"), "x\n")
            created = os.stat(tmp).st_mtime_ns
            self.assertNotEqual(before, created)
            time.sleep(0.01)
            tmp_name = os.path.join(tmp, "a.tsv.tmp")
            _write(tmp_name, "y\n")
            os.replace(tmp_name, os.path.join(tmp, "a.tsv"))
            renamed = os.stat(tmp).st_mtime_ns
            self.assertNotEqual(created, renamed)
            time.sleep(0.01)
            os.remove(os.path.join(tmp, "a.tsv"))
            self.assertNotEqual(renamed, os.stat(tmp).st_mtime_ns)


class FolderDiscoveryTests(unittest.TestCase):
    def setUp(self):
        manifests.reset_state()
        self.emitted = []
        self._orig_emit = manifests._emit_manifest

        def record(store, parent, scratch, source, directory, default_tier, row, *a, **k):
            self.emitted.append(row["label"])
            return self._orig_emit(store, parent, scratch, source, directory, default_tier, row, *a, **k)

        manifests._emit_manifest = record
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        # Layout the walk expects: <root>/<encoded>/<session uuid>/scratchpad.
        self.scratch = os.path.join(self.root, "enc", PARENT, "scratchpad")
        os.makedirs(self.scratch)
        # Bundle run roots are discovered from the workstreams dir, so point
        # it at the temp tree: the machine's real bundles stay unread.
        self._ws_env = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = os.path.join(self.root, "workstreams")
        self.store = MemStore()

    def tearDown(self):
        if self._ws_env is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self._ws_env
        manifests._emit_manifest = self._orig_emit
        manifests.reset_state()
        self.tmp.cleanup()

    def _cfg(self):
        return {
            "scratch_root": self.root,
            "state_dir": os.path.join(self.root, "state"),
            "dir_scan_s": 0,
            "live_window_s": 36 * 3600,
            "read_budgets": {"per_file": 1024 * 1024, "per_tick": 1024 * 1024},
        }

    def test_new_tsv_file_is_seen_on_the_next_poll(self):
        manifests.poll(self.store, self._cfg(), NOW)
        self.assertEqual(self.emitted, [])
        _write(os.path.join(self.scratch, "muse-sessions.tsv"), f"alpha\t{MUSE}\t1700000000\n")
        manifests.poll(self.store, self._cfg(), NOW + 2)
        self.assertIn("alpha", self.emitted)

    def test_in_place_append_is_seen_on_the_next_poll(self):
        path = os.path.join(self.scratch, "muse-sessions.tsv")
        _write(path, f"alpha\t{MUSE}\t1700000000\n")
        manifests.poll(self.store, self._cfg(), NOW)
        with open(path, "a") as fh:
            fh.write(f"beta\t{MUSE}\t1700000001\n")
        manifests.poll(self.store, self._cfg(), NOW + 2)
        self.assertIn("beta", self.emitted)

    def test_new_nested_run_folder_is_seen_on_the_next_poll(self):
        _write(os.path.join(self.scratch, "muse-sessions.tsv"), f"alpha\t{MUSE}\t1700000000\n")
        manifests.poll(self.store, self._cfg(), NOW)
        child = os.path.join(self.scratch, "grokb")
        _write(os.path.join(child, "glm-sessions.tsv"), f"rev\t{GLM}\t1700000002\tglm-5.3\n")
        manifests.poll(self.store, self._cfg(), NOW + 2)
        self.assertIn("rev", self.emitted)

    def test_settled_folder_costs_no_name_checks(self):
        _write(os.path.join(self.scratch, "muse-sessions.tsv"), f"alpha\t{MUSE}\t1700000000\n")
        manifests.poll(self.store, self._cfg(), NOW)
        manifests.poll(self.store, self._cfg(), NOW + 2)
        self.emitted.clear()
        with mock.patch.object(manifests, "_is_file", wraps=manifests._is_file) as is_file:
            manifests.poll(self.store, self._cfg(), NOW + 4)
        # Only the present TSV is opened (a stat inside _load_tsv, not _is_file);
        # no absent manifest name is probed on an unchanged folder.
        self.assertEqual(is_file.call_count, 0)
        self.assertEqual(self.emitted, ["alpha"])

    def test_folder_state_recomputed_only_when_the_folder_changes(self):
        _write(os.path.join(self.scratch, "muse-sessions.tsv"), f"alpha\t{MUSE}\t1700000000\n")
        first = manifests._folder_state(self.scratch)
        self.assertEqual(first[0], frozenset({"muse-sessions.tsv"}))
        with mock.patch.object(manifests, "_is_file", side_effect=AssertionError("re-checked")):
            self.assertEqual(manifests._folder_state(self.scratch), first)

    def test_missing_folder_has_no_state(self):
        self.assertIsNone(manifests._folder_state(os.path.join(self.root, "nope")))


ENV_KEYS = ("AGENT_DASH_STATE_DIR", "AGENT_DASH_WORKSTREAMS", "AGENT_DASH_DATA_ROOT")


class BundleRunRootTests(unittest.TestCase):
    """A run folder under a bundle's ``runs/`` is ingested without registration."""

    def setUp(self):
        manifests.reset_state()
        rundirs._BUNDLE_CACHE.clear()
        self.emitted = []
        self._orig_emit = manifests._emit_manifest

        def record(store, parent, scratch, source, directory, default_tier, row, *a, **k):
            self.emitted.append(row["label"])
            return self._orig_emit(store, parent, scratch, source, directory, default_tier, row, *a, **k)

        manifests._emit_manifest = record
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.state = os.path.join(self.root, "state")
        self.ws = os.path.join(self.root, "workstreams")
        os.makedirs(self.ws)
        self.env = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.ws
        self.store = MemStore()

    def tearDown(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        rundirs._BUNDLE_CACHE.clear()
        manifests._emit_manifest = self._orig_emit
        manifests.reset_state()
        self.tmp.cleanup()

    def _cfg(self):
        return {
            "state_dir": self.state,
            "roots": {"scratchpads": [os.path.join(self.root, "scratch")]},
            "dir_scan_s": 0,
            "live_window_s": 36 * 3600,
            "read_budgets": {"per_file": 1024 * 1024, "per_tick": 1024 * 1024},
        }

    def _run_folder(self, *parts) -> str:
        return os.path.join(self.ws, *parts)

    def test_run_folder_under_a_bundle_root_is_ingested(self):
        run = self._run_folder("WS1", "runs", "ws1-exec-alpha")
        _write(os.path.join(run, "muse-sessions.tsv"), f"ws1-exec-alpha\t{MUSE}\t1700000000\n")
        _write(os.path.join(run, "out-ws1-exec-alpha.md"), "PASS\n")
        _write(os.path.join(run, "err-ws1-exec-alpha.log"), "")
        manifests.poll(self.store, self._cfg(), NOW)
        self.assertIn("ws1-exec-alpha", self.emitted)
        self.assertEqual(self.store.runs["muse:" + MUSE + ":ws1-exec-alpha"]["verdict"], "PASS")

    def test_bundle_root_holding_a_tsv_is_ingested(self):
        run = self._run_folder("WS1", "runs")
        _write(os.path.join(run, "muse-sessions.tsv"), f"ws1-exec-beta\t{MUSE}\t1700000000\n")
        _write(os.path.join(run, "out-ws1-exec-beta.md"), "PASS\n")
        manifests.poll(self.store, self._cfg(), NOW)
        self.assertIn("ws1-exec-beta", self.emitted)

    def test_registered_flat_folder_still_ingested(self):
        flat = os.path.join(self.root, "manual-run")
        _write(os.path.join(flat, "muse-sessions.tsv"), f"manual-alpha\t{MUSE}\t1700000000\n")
        _write(os.path.join(flat, "out-manual-alpha.md"), "PASS\n")
        rundirs.register_run_dir(self.state, flat)
        manifests.poll(self.store, self._cfg(), NOW)
        self.assertIn("manual-alpha", self.emitted)

    def test_bundle_root_without_a_tsv_is_ignored(self):
        run = self._run_folder("WS1", "runs", "empty-run")
        _write(os.path.join(run, "out-empty-run.md"), "PASS\n")
        manifests.poll(self.store, self._cfg(), NOW)
        self.assertEqual(self.emitted, [])

    def test_manual_root_is_not_child_scanned(self):
        flat = os.path.join(self.root, "manual-run")
        _write(os.path.join(flat, "muse-sessions.tsv"), f"manual-alpha\t{MUSE}\t1700000000\n")
        _write(os.path.join(flat, "out-manual-alpha.md"), "PASS\n")
        child = os.path.join(flat, "nested")
        _write(os.path.join(child, "glm-sessions.tsv"), f"nested-beta\t{GLM}\t1700000001\n")
        rundirs.register_run_dir(self.state, flat)
        with mock.patch.object(
            manifests, "_flat_child_dirs", wraps=manifests._flat_child_dirs
        ) as child_dirs:
            manifests.poll(self.store, self._cfg(), NOW)
        self.assertIn("manual-alpha", self.emitted)
        self.assertNotIn("nested-beta", self.emitted)
        self.assertEqual(child_dirs.call_args_list, [])

    def test_only_bundle_roots_are_child_scanned(self):
        manual = os.path.join(self.root, "manual-run")
        _write(os.path.join(manual, "muse-sessions.tsv"), f"manual-alpha\t{MUSE}\t1700000000\n")
        rundirs.register_run_dir(self.state, manual)
        bundle = self._run_folder("WS1", "runs")
        run = os.path.join(bundle, "ws1-exec-gamma")
        _write(os.path.join(run, "muse-sessions.tsv"), f"ws1-exec-gamma\t{MUSE}\t1700000000\n")
        with mock.patch.object(
            manifests, "_flat_child_dirs", wraps=manifests._flat_child_dirs
        ) as child_dirs:
            manifests.poll(self.store, self._cfg(), NOW)
        called = [call.args[0] for call in child_dirs.call_args_list]
        self.assertIn(os.path.realpath(bundle), called)
        self.assertNotIn(os.path.realpath(manual), called)
        self.assertIn("manual-alpha", self.emitted)
        self.assertIn("ws1-exec-gamma", self.emitted)

    def test_new_bundle_run_appears_after_the_cache_bucket_advances(self):
        bundle = self._run_folder("WS1", "runs")
        first = os.path.join(bundle, "ws1-exec-one")
        _write(os.path.join(first, "muse-sessions.tsv"), f"ws1-exec-one\t{MUSE}\t1700000000\n")
        manifests.poll(self.store, self._cfg(), NOW)
        self.assertIn("ws1-exec-one", self.emitted)
        second = os.path.join(bundle, "ws1-exec-two")
        _write(os.path.join(second, "muse-sessions.tsv"), f"ws1-exec-two\t{GLM}\t1700000001\n")
        manifests.poll(self.store, self._cfg(), NOW)
        self.assertNotIn("ws1-exec-two", self.emitted)
        later = NOW + manifests.FLAT_SCAN_S
        self.assertNotEqual(int(later // manifests.FLAT_SCAN_S), int(NOW // manifests.FLAT_SCAN_S))
        manifests.poll(self.store, self._cfg(), later)
        self.assertIn("ws1-exec-two", self.emitted)

    def test_reset_state_clears_the_flat_cache(self):
        bundle = self._run_folder("WS1", "runs")
        run = os.path.join(bundle, "ws1-exec-one")
        _write(os.path.join(run, "muse-sessions.tsv"), f"ws1-exec-one\t{MUSE}\t1700000000\n")
        manifests.poll(self.store, self._cfg(), NOW)
        self.assertTrue(manifests._flat_cache)
        manifests.reset_state()
        self.assertEqual(manifests._flat_cache, {})


if __name__ == "__main__":
    unittest.main()
