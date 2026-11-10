"""Bundle run roots: workstream ``runs`` folders register without a manifest.

A workstream bundle keeps its run folders under ``<workstreams>/<ID>/runs``.
These tests pin the discovery, the dedupe against the manual registry and
the configured roots, the symlink-escape refusal, and the mtime cache.
Every path is a temp tree; the live state and workstreams dirs are never
read.
"""

from __future__ import annotations

import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import rundirs  # noqa: E402

ENV_KEYS = ("AGENT_DASH_STATE_DIR", "AGENT_DASH_WORKSTREAMS", "AGENT_DASH_DATA_ROOT")


def _real(path) -> str:
    return os.path.realpath(str(path))


class BundleRunRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.ws = self.root / "workstreams"
        self.ws.mkdir()
        self.env = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["AGENT_DASH_STATE_DIR"] = str(self.state)
        os.environ["AGENT_DASH_WORKSTREAMS"] = str(self.ws)
        os.environ["AGENT_DASH_DATA_ROOT"] = str(self.root)
        rundirs._BUNDLE_CACHE.clear()

    def tearDown(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        rundirs._BUNDLE_CACHE.clear()
        self.tmp.cleanup()

    def _bundle(self, name, runs=True) -> Path:
        bundle = self.ws / name
        bundle.mkdir()
        if runs:
            (bundle / "runs").mkdir()
        return bundle

    def _bump(self, path: Path) -> None:
        stamp = time.time() + 5
        os.utime(path, (stamp, stamp))

    def test_bundle_runs_discovered(self):
        first = self._bundle("WS1")
        second = self._bundle("WS2")
        self.assertEqual(
            rundirs.bundle_run_roots(),
            [_real(first / "runs"), _real(second / "runs")],
        )

    def test_duplicate_paths_deduped(self):
        first = self._bundle("WS1")
        second = self._bundle("WS2", runs=False)
        (second / "runs").symlink_to(first / "runs")
        self.assertEqual(rundirs.bundle_run_roots(), [_real(first / "runs")])
        rundirs.register_run_dir(self.state, first / "runs")
        self.assertEqual(rundirs.run_dirs(), [str(first / "runs")])

    def test_missing_runs_dirs_ignored(self):
        kept = self._bundle("WS1")
        self._bundle("WS2", runs=False)
        (self.ws / "notes.txt").write_text("not a bundle\n", encoding="utf-8")
        self.assertEqual(rundirs.bundle_run_roots(), [_real(kept / "runs")])

    def test_symlink_escape_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "runs").mkdir()
        (self.ws / "WS9").symlink_to(outside)
        escaped = self._bundle("WS8", runs=False)
        (escaped / "runs").symlink_to(outside / "runs")
        kept = self._bundle("WS1")
        self.assertEqual(rundirs.bundle_run_roots(), [_real(kept / "runs")])

    def test_cache_invalidates_when_a_bundle_runs_dir_appears(self):
        first = self._bundle("WS1")
        self.assertEqual(rundirs.bundle_run_roots(), [_real(first / "runs")])
        second = self._bundle("WS2")
        self._bump(self.ws)
        self.assertEqual(
            rundirs.bundle_run_roots(),
            [_real(first / "runs"), _real(second / "runs")],
        )

    def test_cache_invalidates_when_runs_appears_in_an_existing_bundle(self):
        bundle = self._bundle("WS2", runs=False)
        self.assertEqual(rundirs.bundle_run_roots(), [])
        (bundle / "runs").mkdir()
        self._bump(bundle)
        self.assertEqual(rundirs.bundle_run_roots(), [_real(bundle / "runs")])

    def test_configured_roots_and_registered_order_unchanged(self):
        scratch = self.root / "scratch"
        scratch.mkdir()
        bundle = self._bundle("WS1")
        manual = self.root / "manual-run"
        manual.mkdir()
        rundirs.register_run_dir(self.state, manual)
        cfg = {"state_dir": str(self.state), "roots": {"scratchpads": [str(scratch)]}}
        self.assertEqual(
            rundirs.scan_roots(cfg),
            [str(scratch), str(manual), _real(bundle / "runs")],
        )
        self.assertEqual(rundirs.registered_run_dirs(cfg=cfg), [str(manual)])

    def test_bundle_root_manifests_do_not_warn(self):
        bundle = self._bundle("WS1")
        (bundle / "runs" / "ws1-exec-x-sessions.tsv").write_text(
            "ws1-exec-x\t-\t1700000000\n", encoding="utf-8"
        )
        cfg = {
            "state_dir": str(self.state),
            "roots": {"scratchpads": [str(self.root / "scratch")]},
        }
        report = rundirs.describe_roots(
            cfg, now=time.time(), processes=[{"label": "ws1-exec-x"}]
        )
        self.assertEqual(report["warnings"], [])
        self.assertIn(_real(bundle / "runs"), report["run_dirs"])

    def test_manual_run_dir_outside_roots_still_warns(self):
        manual = self.root / "manual-run"
        manual.mkdir()
        (manual / "x-sessions.tsv").write_text("x\t-\t1700000000\n", encoding="utf-8")
        rundirs.register_run_dir(self.state, manual)
        cfg = {
            "state_dir": str(self.state),
            "roots": {"scratchpads": [str(self.root / "scratch")]},
        }
        report = rundirs.describe_roots(
            cfg, now=time.time(), processes=[{"label": "x"}]
        )
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn(str(manual), report["warnings"][0])


if __name__ == "__main__":
    unittest.main()
