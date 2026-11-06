"""Scratch folder resolution, owner marker, and the scratch CLI (dash/scratchdir.py, dash/scratchgc.py)."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from dash import scratchdir, scratchgc

NOW = 1_800_000_000.0
DAY = 86400


class ScratchRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get(scratchdir.ROOT_ENV)
        os.environ.pop(scratchdir.ROOT_ENV, None)

    def tearDown(self):
        if self.old is None:
            os.environ.pop(scratchdir.ROOT_ENV, None)
        else:
            os.environ[scratchdir.ROOT_ENV] = self.old
        self.tmp.cleanup()

    def test_default_root_is_cache_dir(self):
        self.assertEqual(scratchdir.scratch_root(), Path.home() / ".cache" / "agent-dash" / "scratch")

    def test_env_overrides_root(self):
        os.environ[scratchdir.ROOT_ENV] = self.tmp.name
        self.assertEqual(scratchdir.scratch_root(), Path(self.tmp.name))

    def test_workstream_key_from_label(self):
        self.assertEqual(scratchdir.workstream_key("ws4-WS4.1-notes-index"), "WS4")
        self.assertEqual(scratchdir.workstream_key("ws12-plan-lane-1"), "WS12")
        self.assertEqual(scratchdir.workstream_key("WS7-legacy-thing"), "WS7")
        self.assertEqual(scratchdir.workstream_key("hub-plan-review"), "hub")
        self.assertEqual(scratchdir.workstream_key("random label"), "hub")
        self.assertEqual(scratchdir.workstream_key(""), "hub")

    def test_scratch_path_layout(self):
        root = Path(self.tmp.name)
        self.assertEqual(
            scratchdir.scratch_path("ws4-plan-lane-1", root),
            root / "WS4" / "ws4-plan-lane-1",
        )
        self.assertEqual(scratchdir.scratch_path("hub-plan-x", root), root / "hub" / "hub-plan-x")

    def test_unsafe_labels_are_refused(self):
        root = Path(self.tmp.name)
        for bad in ("", ".", "..", "a/b", "../x", "a b"):
            with self.assertRaises(ValueError, msg=bad):
                scratchdir.scratch_path(bad, root)


class EnsureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "scratch"
        self.run_dir = Path(self.tmp.name) / "hub" / "runs" / "ws4-plan-lane-1"

    def tearDown(self):
        self.tmp.cleanup()

    def test_ensure_creates_folder_and_owner_marker(self):
        path = scratchdir.ensure("ws4-plan-lane-1", self.run_dir, root=self.root, now=NOW)
        self.assertEqual(path, self.root / "WS4" / "ws4-plan-lane-1")
        self.assertTrue(path.is_dir())
        owner = scratchdir.read_owner(path)
        self.assertEqual(owner["label"], "ws4-plan-lane-1")
        self.assertEqual(owner["workstream"], "WS4")
        self.assertEqual(owner["run_dir"], str(self.run_dir))
        self.assertEqual(owner["created_at"], int(NOW))

    def test_ensure_is_idempotent_and_keeps_first_owner(self):
        first = scratchdir.ensure("hub-plan-x", self.run_dir, root=self.root, now=NOW)
        (first / "keep.txt").write_text("kept", encoding="utf-8")
        second = scratchdir.ensure("hub-plan-x", Path("/elsewhere"), root=self.root, now=NOW + 5)
        self.assertEqual(first, second)
        self.assertEqual((second / "keep.txt").read_text(encoding="utf-8"), "kept")
        self.assertEqual(scratchdir.read_owner(second)["run_dir"], str(self.run_dir))

    def test_read_owner_missing_or_bad(self):
        folder = self.root / "WS1" / "ws1-x"
        folder.mkdir(parents=True)
        self.assertIsNone(scratchdir.read_owner(folder))
        (folder / scratchdir.OWNER_FILE).write_text("[1, 2]", encoding="utf-8")
        self.assertIsNone(scratchdir.read_owner(folder))

    def test_marker_write_failure_still_returns_path(self):
        with mock.patch("dash.scratchdir.os.replace", side_effect=OSError("disk")):
            path = scratchdir.ensure("ws2-plan-x", self.run_dir, root=self.root, now=NOW)
        self.assertTrue(path.is_dir())
        self.assertIsNone(scratchdir.read_owner(path))


class ScratchCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "scratch"
        self.old = os.environ.get(scratchdir.ROOT_ENV)
        os.environ[scratchdir.ROOT_ENV] = str(self.root)

    def tearDown(self):
        if self.old is None:
            os.environ.pop(scratchdir.ROOT_ENV, None)
        else:
            os.environ[scratchdir.ROOT_ENV] = self.old
        self.tmp.cleanup()

    def run_cli(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scratchgc.cmd_scratch(argv)
        return code, buf.getvalue()

    def test_bad_usage_exits_2(self):
        for argv in ([], ["nope"], ["gc", "--bogus"], ["gc", "--older-than-days"], ["gc", "--older-than-days", "x"],
                     ["gc", "--older-than-days", "-1"], ["status", "--top", "x"]):
            with mock.patch("sys.stderr", new=io.StringIO()):
                code, _out = self.run_cli(argv)
            self.assertEqual(code, 2, argv)

    def test_status_on_missing_root(self):
        code, out = self.run_cli(["status"])
        self.assertEqual(code, 0)
        self.assertIn("does not exist yet", out)
        self.assertFalse(self.root.exists())

    def test_status_reports_total_and_largest(self):
        big = self.root / "WS1" / "ws1-big"
        big.mkdir(parents=True)
        (big / "blob").write_bytes(b"x" * 300_000)
        small = self.root / "hub" / "hub-small"
        small.mkdir(parents=True)
        (small / "blob").write_bytes(b"x" * 10)
        code, out = self.run_cli(["status", "--top", "1"])
        self.assertEqual(code, 0)
        self.assertIn("2 folder(s)", out)
        self.assertIn("WS1/ws1-big", out)
        self.assertNotIn("hub/hub-small", out)
        code, out = self.run_cli(["status", "--json"])
        data = json.loads(out)
        self.assertEqual(data["folders"], 2)
        self.assertEqual(data["largest"][0]["label"], "ws1-big")
        self.assertGreater(data["bytes"], 300_000)


if __name__ == "__main__":
    unittest.main()
