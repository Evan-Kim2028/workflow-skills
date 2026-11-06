"""``ws archive`` cleanup: logs archive step, scratch removal, live-run refusal, never fails the archive."""

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from dash import archivecleanup, errors, lifecycle, scratchdir

NOW = time.time()
OLD = NOW - 30 * 86400


class CleanupBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "scratch"
        self.old_root = os.environ.get(scratchdir.ROOT_ENV)
        os.environ[scratchdir.ROOT_ENV] = str(self.root)
        self.live = mock.patch("dash.scratchgc.live_labels", return_value=set())
        self.live_mock = self.live.start()
        self.addCleanup(self.live.stop)
        self.open = mock.patch("dash.scratchgc._proc_open_in", return_value=False)
        self.open.start()
        self.addCleanup(self.open.stop)

    def tearDown(self):
        if self.old_root is None:
            os.environ.pop(scratchdir.ROOT_ENV, None)
        else:
            os.environ[scratchdir.ROOT_ENV] = self.old_root
        self.tmp.cleanup()

    def folder(self, label, age=OLD):
        run = self.base / "hub" / "runs" / label
        run.mkdir(parents=True, exist_ok=True)
        path = scratchdir.ensure(label, run, root=self.root, now=NOW)
        (path / "venv").mkdir(exist_ok=True)
        (path / "venv" / "blob").write_bytes(b"x" * 4096)
        for p in [path, *path.rglob("*")]:
            os.utime(p, (age, age))
        return path


class CleanupTests(CleanupBase):
    def run_cleanup(self, ws="WS4", **kw):
        lines = []
        result = archivecleanup.cleanup_after_archive(ws, root=self.root, now=NOW, out=lines.append, **kw)
        return result, lines

    def test_logs_step_skipped_with_one_line_when_unavailable(self):
        _result, lines = self.run_cleanup(logs_available=False)
        self.assertEqual(lines[0], "WS4: skipped: logs archive is not available in this build")

    def test_logs_step_runs_the_documented_command(self):
        seen = []

        def runner(cmd):
            seen.append(cmd)
            return 0

        result, lines = self.run_cleanup(logs_available=True, logs_runner=runner)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1].endswith("agent_dash.py"), True)
        self.assertEqual(seen[0][-5:], ["logs", "archive", "--ws", "WS4", "--apply"])
        self.assertEqual(result["logs"], "logs archive applied")

    def test_logs_failure_never_fails_and_scratch_still_runs(self):
        self.folder("ws4-plan-old")
        result, lines = self.run_cleanup(logs_available=True, logs_runner=lambda cmd: 3)
        self.assertIn("logs archive exited 3", result["logs"])
        self.assertEqual(result["removed"], ["ws4-plan-old"])

    def test_logs_runner_crash_is_contained(self):
        def boom(cmd):
            raise OSError("no such service")

        result, lines = self.run_cleanup(logs_available=True, logs_runner=boom)
        self.assertIn("did not run", result["logs"])

    def test_finished_scratch_is_removed_and_reported(self):
        mine = self.folder("ws4-plan-old")
        other = self.folder("ws5-plan-old")
        result, lines = self.run_cleanup(logs_available=False)
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists())
        self.assertEqual(result["removed"], ["ws4-plan-old"])
        self.assertGreater(result["freed"], 0)
        self.assertTrue(any("scratch reclaimed" in line for line in lines), lines)

    def test_live_run_refuses_scratch_cleanup_only(self):
        mine = self.folder("ws4-plan-old")
        self.live_mock.return_value = {"ws4-exec-lane-1"}
        result, lines = self.run_cleanup(logs_available=False)
        self.assertTrue(mine.is_dir())
        self.assertTrue(result["scratch"].startswith("refused: live runs"), result)
        self.assertTrue(any("scratch cleanup refused" in line and "Archive kept" in line for line in lines))

    def test_live_run_folder_itself_is_named_as_blocker(self):
        busy = self.folder("ws4-plan-busy")
        self.live_mock.return_value = {"ws4-plan-busy"}
        result, _lines = self.run_cleanup(logs_available=False)
        self.assertTrue(busy.is_dir())
        self.assertIn("ws4-plan-busy", result["scratch"])

    def test_unverifiable_live_check_refuses(self):
        mine = self.folder("ws4-plan-old")
        self.live_mock.return_value = None
        result, lines = self.run_cleanup(logs_available=False)
        self.assertTrue(mine.is_dir())
        self.assertIn("live-process check did not run", result["scratch"])

    def test_keep_scratch_skips_removal(self):
        mine = self.folder("ws4-plan-old")
        result, lines = self.run_cleanup(logs_available=False, keep_scratch=True)
        self.assertTrue(mine.is_dir())
        self.assertEqual(result["scratch"], "kept: --keep-scratch")
        self.assertIn("WS4: scratch kept (--keep-scratch)", lines)

    def test_removal_error_is_contained(self):
        self.folder("ws4-plan-old")
        with mock.patch("dash.scratchgc.remove_workstream", side_effect=RuntimeError("disk gone")):
            result, lines = self.run_cleanup(logs_available=False)
        self.assertTrue(result["scratch"].startswith("failed"))
        self.assertTrue(any("archive kept" in line for line in lines))

    def test_workstream_live_error_is_contained(self):
        with mock.patch("dash.scratchgc.workstream_live", side_effect=ValueError("bad")):
            result, lines = self.run_cleanup(logs_available=False)
        self.assertTrue(result["scratch"].startswith("failed"))


class ArchiveCommandTests(CleanupBase):
    """``ws archive`` end to end through the CLI entry point, with the archive itself stubbed."""

    def archive(self, argv, *, code=errors.OK, json_mode=False):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(lifecycle, "archive_bundle", return_value=code) as ab, \
                mock.patch("dash.archivecleanup.logs_archive_available", return_value=False), \
                redirect_stdout(out), redirect_stderr(err):
            rc = lifecycle.cmd_archive(argv, json_mode=json_mode)
        return rc, ab, out.getvalue(), err.getvalue()

    def test_successful_archive_cleans_up_its_scratch(self):
        mine = self.folder("ws4-plan-old")
        other = self.folder("ws5-plan-old")
        rc, ab, out, _err = self.archive(["WS4"])
        self.assertEqual(rc, errors.OK)
        ab.assert_called_once()
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists())
        self.assertIn("WS4: scratch reclaimed", out)

    def test_keep_scratch_flag_is_parsed_and_honoured(self):
        mine = self.folder("ws4-plan-old")
        rc, _ab, out, _err = self.archive(["WS4", "--keep-scratch"])
        self.assertEqual(rc, errors.OK)
        self.assertTrue(mine.is_dir())
        self.assertIn("scratch kept (--keep-scratch)", out)

    def test_refused_archive_runs_no_cleanup(self):
        mine = self.folder("ws4-plan-old")
        rc, _ab, out, _err = self.archive(["WS4"], code=errors.USAGE)
        self.assertEqual(rc, errors.USAGE)
        self.assertTrue(mine.is_dir())
        self.assertNotIn("scratch", out)

    def test_live_run_keeps_archive_successful_and_scratch(self):
        busy = self.folder("ws4-plan-busy")
        self.live_mock.return_value = {"ws4-plan-busy"}
        rc, _ab, out, _err = self.archive(["WS4"])
        self.assertEqual(rc, errors.OK)
        self.assertTrue(busy.is_dir())
        self.assertIn("scratch cleanup refused", out)

    def test_cleanup_crash_never_fails_the_archive(self):
        with mock.patch("dash.archivecleanup.cleanup_after_archive", side_effect=RuntimeError("boom")):
            rc, _ab, out, _err = self.archive(["WS4"])
        self.assertEqual(rc, errors.OK)
        self.assertIn("post-archive cleanup failed", out)

    def test_json_mode_keeps_stdout_valid_json(self):
        self.folder("ws4-plan-old")
        rc, _ab, out, err = self.archive(["WS4"], json_mode=True)
        self.assertEqual(rc, errors.OK)
        self.assertEqual(json.loads(out), {"ws": "WS4", "state": "archived", "ok": True})
        self.assertIn("scratch reclaimed", err)


class LogsAvailabilityTests(unittest.TestCase):
    def test_missing_module_reports_unavailable(self):
        with mock.patch("importlib.util.find_spec", return_value=None):
            self.assertFalse(archivecleanup.logs_archive_available())

    def test_present_module_reports_available(self):
        with mock.patch("importlib.util.find_spec", return_value=object()):
            self.assertTrue(archivecleanup.logs_archive_available())


if __name__ == "__main__":
    unittest.main()
