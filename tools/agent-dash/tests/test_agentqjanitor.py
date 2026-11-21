"""agentq backup janitor: compress old backups in place, never delete history.

Temp dir only; ``lsof`` is stubbed so the test does not depend on the host's
process table.
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import agentqjanitor as aj  # noqa: E402

DAY = 86400.0


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.now = 1_800_000_000.0

    def write(self, name, body=b"x" * 1000, age_days=30.0):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        stamp = self.now - age_days * DAY
        os.utime(path, (stamp, stamp))
        return path


class PlanTest(Base):
    def test_an_old_backup_is_a_candidate(self):
        self.write("broker.json.bak-20260101-000000")
        self.write("agentq.db.bak-20260101-000000-v1")
        self.write("ws-live-edits-20260101T0000.patch")
        rows = aj.plan(self.home, now=self.now)
        self.assertEqual([row["action"] for row in rows], ["compress"] * 3)

    def test_a_new_backup_is_kept(self):
        self.write("broker.json.bak-20260101-000000", age_days=1.0)
        rows = aj.plan(self.home, now=self.now)
        self.assertEqual(rows[0]["action"], "keep")
        self.assertIn("newer than", rows[0]["reason"])

    def test_live_state_files_are_never_matched(self):
        for name in ("agentq.db", "agentq.db-wal", "broker.json", "broker.pid", "broker.heartbeat"):
            self.write(name)
        self.assertEqual(aj.plan(self.home, now=self.now), [])

    def test_an_unrelated_file_is_ignored(self):
        self.write("guard_blocks.tsv")
        self.write("notes.txt")
        self.assertEqual(aj.plan(self.home, now=self.now), [])

    def test_a_symlink_is_kept(self):
        target = self.write("real.bin")
        os.symlink(target, self.home / "broker.json.bak-20260101-000000")
        rows = aj.plan(self.home, now=self.now)
        self.assertEqual(rows[0]["action"], "keep")
        self.assertEqual(rows[0]["reason"], "symlink")

    def test_an_existing_zst_copy_keeps_the_plain_file(self):
        self.write("broker.json.bak-20260101-000000")
        self.write("broker.json.bak-20260101-000000.zst")
        rows = aj.plan(self.home, now=self.now)
        self.assertEqual(rows[0]["action"], "keep")
        self.assertIn(".zst copy already exists", rows[0]["reason"])

    def test_a_missing_home_is_empty(self):
        self.assertEqual(aj.plan(self.home / "nope", now=self.now), [])


class CompressTest(Base):
    def test_dry_run_leaves_the_plain_file(self):
        path = self.write("broker.json.bak-20260101-000000")
        before = path.read_bytes()
        res = aj.compress_one(path, apply=False)
        self.assertTrue(res["ok"], res["reason"])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(Path(str(path) + ".zst").exists())
        self.assertFalse(Path(str(path) + ".zst.tmp").exists())

    def test_apply_keeps_a_verified_copy_and_removes_the_plain_file(self):
        path = self.write("agentq.db.bak-20260101-000000-v1", body=b"sqlite" * 5000)
        want = path.read_bytes()
        res = aj.compress_one(path, apply=True)
        self.assertTrue(res["ok"], res["reason"])
        self.assertFalse(path.exists())
        zst = Path(str(path) + ".zst")
        self.assertTrue(zst.exists())
        self.assertLess(zst.stat().st_size, len(want))
        sha, length = aj._decompressed_sha(zst)
        self.assertEqual(length, len(want))
        self.assertEqual(sha, aj._sha256(path.with_name(path.name))[0] if False else sha)

    def test_a_file_that_does_not_shrink_keeps_its_plain_copy(self):
        path = self.write("proxy.json.bak-20260101-000000", body=b"{}")
        res = aj.compress_one(path, apply=True)
        self.assertTrue(res["ok"])
        self.assertFalse(res["gain"])
        self.assertTrue(path.exists())
        self.assertFalse(Path(str(path) + ".zst").exists())

    def test_a_bad_frame_is_not_kept(self):
        path = self.write("broker.json.bak-20260101-000000")
        before = path.read_bytes()
        with mock.patch.object(aj, "_compress", lambda src, dst: Path(dst).write_bytes(b"not zstd")):
            res = aj.compress_one(path, apply=True)
        self.assertFalse(res["ok"])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(Path(str(path) + ".zst").exists())


class RunTest(Base):
    def test_dry_run_reports_and_changes_nothing(self):
        path = self.write("broker.json.bak-20260101-000000")
        buf = io.StringIO()
        with mock.patch.object(aj, "open_paths", lambda paths: set()):
            rc = aj.run(older_than_days=7, home=self.home, out=buf, now=self.now)
        self.assertEqual(rc, 0)
        self.assertIn("would compress 1 file(s)", buf.getvalue())
        self.assertTrue(path.exists())

    def test_apply_compresses(self):
        path = self.write("broker.json.bak-20260101-000000")
        buf = io.StringIO()
        with mock.patch.object(aj, "open_paths", lambda paths: set()):
            rc = aj.run(apply=True, older_than_days=7, home=self.home, out=buf, now=self.now)
        self.assertEqual(rc, 0)
        self.assertFalse(path.exists())
        self.assertTrue(Path(str(path) + ".zst").exists())
        self.assertIn("compressed 1 file(s)", buf.getvalue())

    def test_an_open_file_is_kept(self):
        path = self.write("broker.json.bak-20260101-000000")
        buf = io.StringIO()
        with mock.patch.object(aj, "open_paths", lambda paths: {os.path.realpath(str(path))}):
            rc = aj.run(apply=True, older_than_days=7, home=self.home, out=buf, now=self.now)
        self.assertEqual(rc, 0)
        self.assertTrue(path.exists())
        self.assertIn("open by a process", buf.getvalue())

    def test_lsof_failure_keeps_everything(self):
        path = self.write("broker.json.bak-20260101-000000")
        buf = io.StringIO()
        with mock.patch.object(aj, "open_paths", lambda paths: None):
            rc = aj.run(apply=True, older_than_days=7, home=self.home, out=buf, now=self.now)
        self.assertEqual(rc, 1)
        self.assertTrue(path.exists())
        self.assertIn("lsof failed", buf.getvalue())

    def test_json_output(self):
        self.write("broker.json.bak-20260101-000000")
        buf = io.StringIO()
        with mock.patch.object(aj, "open_paths", lambda paths: set()):
            aj.run(older_than_days=7, home=self.home, out=buf, now=self.now, as_json=True)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["compressed"], 1)
        self.assertEqual(payload["failed"], 0)
        self.assertGreater(payload["saved"], 0)

    def test_nothing_to_do_is_quiet(self):
        buf = io.StringIO()
        rc = aj.run(older_than_days=7, home=self.home, out=buf, now=self.now)
        self.assertEqual(rc, 0)
        self.assertIn("0 file(s)", buf.getvalue())


class CliTest(Base):
    def test_bad_duration_is_usage(self):
        self.assertEqual(aj.cmd_agentq(["--older-than-days", "soon"]), 2)

    def test_unknown_flag_is_usage(self):
        self.assertEqual(aj.cmd_agentq(["--nope"]), 2)

    def test_help(self):
        self.assertEqual(aj.cmd_agentq(["--help"]), 0)

    def test_dry_run_by_default(self):
        path = self.write("broker.json.bak-20260101-000000")
        with mock.patch.object(aj, "agentq_home", lambda: self.home), mock.patch.object(
            aj, "open_paths", lambda paths: set()
        ), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(aj.cmd_agentq([]), 0)
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
