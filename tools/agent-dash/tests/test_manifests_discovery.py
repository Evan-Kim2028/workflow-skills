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
        self.store = MemStore()

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
