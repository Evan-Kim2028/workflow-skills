"""``scratch gc`` safety: what it removes, what it keeps, and that dry run removes nothing."""

import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from dash import scratchdir, scratchgc

NOW = time.time()
OLD = NOW - 30 * 86400
_REAL_OPEN_IN = scratchgc._proc_open_in


def age(path: Path, when: float) -> None:
    for p in [path, *path.rglob("*")]:
        os.utime(p, (when, when), follow_symlinks=False)


class GcBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "scratch"
        self.hub_run = self.base / "hub" / "runs"
        self.hub_run.mkdir(parents=True)
        self.old_root = os.environ.get(scratchdir.ROOT_ENV)
        os.environ[scratchdir.ROOT_ENV] = str(self.root)
        # Default: no live launchers, nothing open. Tests set these to model a live run.
        self.open_flag = False
        self.real_open = False

        def open_check(folder):
            return _REAL_OPEN_IN(folder) if self.real_open else self.open_flag

        live_patch = mock.patch("dash.scratchgc.live_labels", return_value=set())
        open_patch = mock.patch("dash.scratchgc._proc_open_in", side_effect=open_check)
        self.live = live_patch.start()
        self.open_in = open_patch.start()
        self.addCleanup(live_patch.stop)
        self.addCleanup(open_patch.stop)

    def tearDown(self):
        if self.old_root is None:
            os.environ.pop(scratchdir.ROOT_ENV, None)
        else:
            os.environ[scratchdir.ROOT_ENV] = self.old_root
        self.tmp.cleanup()

    def make(self, label, *, age_when=OLD, run_dir=None, body=b"x" * 2048):
        run = run_dir if run_dir is not None else self.hub_run / label
        run.mkdir(parents=True, exist_ok=True)
        folder = scratchdir.ensure(label, run, root=self.root, now=NOW)
        (folder / "env").mkdir(exist_ok=True)
        (folder / "env" / "data.bin").write_bytes(body)
        age(folder, age_when)
        return folder

    def row(self, folder, now=NOW, days=7):
        return scratchgc.assess(folder, now, days, scratchgc.live_labels(now))


class GcSafetyTests(GcBase):
    def test_finished_old_folder_is_reclaimable(self):
        folder = self.make("ws4-plan-done")
        verdict = self.row(folder)
        self.assertEqual(verdict["action"], "reclaim", verdict)

    def test_dry_run_removes_nothing(self):
        folder = self.make("ws4-plan-done")
        report = scratchgc.gc(self.root, 7, apply=False, now=NOW)
        self.assertEqual(report["reclaimable_count"], 1)
        self.assertGreater(report["reclaimable"], 0)
        self.assertEqual(report["removed_count"], 0)
        self.assertTrue(folder.is_dir())

    def test_apply_removes_then_is_idempotent(self):
        folder = self.make("ws4-plan-done")
        first = scratchgc.gc(self.root, 7, apply=True, now=NOW)
        self.assertEqual(first["removed_count"], 1)
        self.assertFalse(folder.exists())
        second = scratchgc.gc(self.root, 7, apply=True, now=NOW)
        self.assertEqual(second["reclaimable_count"], 0)
        self.assertEqual(second["removed_count"], 0)

    def test_live_launcher_keeps_its_folder(self):
        folder = self.make("ws4-plan-live")
        self.live.return_value = {"ws4-plan-live"}
        verdict = self.row(folder)
        self.assertEqual(verdict["action"], "keep")
        self.assertEqual(verdict["reason"], "live launcher")
        report = scratchgc.gc(self.root, 7, apply=True, now=NOW)
        self.assertTrue(folder.is_dir())
        self.assertEqual(report["removed_count"], 0)

    def test_running_pid_keeps_its_folder(self):
        run = self.hub_run / "ws4-plan-running"
        (run / "running").mkdir(parents=True)
        (run / "running" / "ws4-plan-running").write_text(f"{os.getpid()}\tmodel\n", encoding="utf-8")
        folder = self.make("ws4-plan-running", run_dir=run)
        verdict = self.row(folder)
        self.assertEqual(verdict["action"], "keep")
        self.assertEqual(verdict["reason"], "run still running")

    def test_pid_file_with_dead_pid_is_finished(self):
        run = self.hub_run / "ws4-plan-dead"
        run.mkdir(parents=True)
        (run / "ws4-plan-dead.pid").write_text("999999\n", encoding="utf-8")
        folder = self.make("ws4-plan-dead", run_dir=run)
        with mock.patch("dash.scratchgc._pid_alive", return_value=False):
            self.assertEqual(self.row(folder)["action"], "reclaim")

    def test_unreadable_run_state_keeps(self):
        run = self.hub_run / "ws4-plan-bad"
        (run / "running").mkdir(parents=True)
        (run / "running" / "ws4-plan-bad").write_text("not-a-pid\n", encoding="utf-8")
        folder = self.make("ws4-plan-bad", run_dir=run)
        verdict = self.row(folder)
        self.assertEqual(verdict["action"], "keep")
        self.assertEqual(verdict["reason"], "run state unreadable")

    def test_open_file_keeps_its_folder(self):
        folder = self.make("ws4-plan-open")
        self.open_flag = True
        verdict = self.row(folder)
        self.assertEqual(verdict["action"], "keep")
        self.assertEqual(verdict["reason"], "in use")

    def test_real_open_handle_is_seen(self):
        folder = self.make("ws4-plan-real-open")
        held = open(folder / "env" / "data.bin", "rb")
        self.addCleanup(held.close)
        self.real_open = True
        if _REAL_OPEN_IN(str(folder)) is None:
            self.skipTest("no way to list open files on this host")
        self.assertEqual(self.row(folder)["reason"], "in use")

    def test_open_check_that_cannot_run_keeps(self):
        folder = self.make("ws4-plan-unknown")
        self.open_in.side_effect = lambda folder: None
        self.assertEqual(self.row(folder)["reason"], "open-handle check did not run")

    def test_live_listing_that_cannot_run_keeps_everything(self):
        folder = self.make("ws4-plan-nolive")
        self.live.return_value = None
        self.assertEqual(self.row(folder)["reason"], "live-process check did not run")
        report = scratchgc.gc(self.root, 7, apply=True, now=NOW)
        self.assertEqual(report["removed_count"], 0)
        self.assertTrue(folder.is_dir())

    def test_recent_files_keep_the_folder(self):
        folder = self.make("ws4-plan-recent", age_when=NOW - 2 * 86400)
        verdict = self.row(folder)
        self.assertEqual(verdict["action"], "keep")
        self.assertIn("under 7 days", verdict["reason"])

    def test_folder_without_owner_marker_is_kept(self):
        folder = self.root / "WS4" / "ws4-notes-stranger"
        (folder / "env").mkdir(parents=True)
        (folder / "env" / "data.bin").write_bytes(b"x")
        age(folder, OLD)
        verdict = self.row(folder)
        self.assertEqual(verdict["action"], "keep")
        self.assertIn("no owner marker", verdict["reason"])

    def test_owner_marker_for_another_label_is_kept(self):
        folder = self.make("ws4-plan-mixed")
        (folder / scratchdir.OWNER_FILE).write_text('{"label": "someone-else"}', encoding="utf-8")
        self.assertIn("another run", self.row(folder)["reason"])

    def test_symlink_folders_are_never_followed_or_removed(self):
        target = self.base / "precious"
        target.mkdir()
        (target / "keep.txt").write_text("keep", encoding="utf-8")
        link_ws = self.root / "WS9"
        link_ws.mkdir(parents=True)
        (link_ws / "ws9-link").symlink_to(target, target_is_directory=True)
        report = scratchgc.gc(self.root, 0, apply=True, now=NOW)
        self.assertEqual(report["removed_count"], 0)
        self.assertTrue((target / "keep.txt").exists())

    def test_a_folder_that_cannot_be_walked_is_kept(self):
        folder = self.make("ws4-plan-walk")
        with mock.patch("dash.scratchgc._walk", return_value=(0, 0.0, False)):
            self.assertEqual(self.row(folder)["reason"], "cannot read the folder")

    def test_zero_day_cutoff_still_checks_live_state(self):
        folder = self.make("ws4-plan-zero-live", age_when=NOW)
        self.live.return_value = {"ws4-plan-zero-live"}
        report = scratchgc.gc(self.root, 0, apply=True, now=NOW)
        self.assertTrue(folder.is_dir())
        self.assertEqual(report["removed_count"], 0)

    def test_recheck_before_removal_keeps_a_run_that_started(self):
        folder = self.make("ws4-plan-race")
        calls = {"n": 0}

        def live_later(now=None):
            calls["n"] += 1
            return set() if calls["n"] == 1 else {"ws4-plan-race"}

        self.live.side_effect = live_later
        report = scratchgc.gc(self.root, 7, apply=True, now=NOW)
        self.assertTrue(folder.is_dir())
        self.assertEqual(report["removed_count"], 0)

    def test_gc_prints_a_summary(self):
        folder = self.make("ws4-plan-print")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scratchgc.cmd_scratch(["gc", "--older-than-days", "7", "--apply"])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("reclaim  WS4/ws4-plan-print", out)
        self.assertIn("removed 1 folder(s)", out)
        self.assertFalse(folder.exists())


class FinishedRunTests(GcBase):
    def test_finished_run_idle_two_hours_is_reclaimed_with_the_flag(self):
        folder = self.make("ws4-plan-idle2h", age_when=NOW - 2 * 3600)
        verdict = scratchgc.assess(folder, NOW, 7, scratchgc.live_labels(NOW), 1)
        self.assertEqual(verdict["action"], "reclaim", verdict)
        self.assertEqual(verdict["reason"], "finished run, idle over 1 h")
        report = scratchgc.gc(self.root, 7, apply=True, now=NOW, finished_after_hours=1)
        self.assertEqual(report["removed_count"], 1)
        self.assertFalse(folder.exists())

    def test_finished_run_idle_two_hours_is_kept_without_the_flag(self):
        folder = self.make("ws4-plan-idle2h-off", age_when=NOW - 2 * 3600)
        verdict = self.row(folder)
        self.assertEqual(verdict["action"], "keep", verdict)
        self.assertIn("under 7 days", verdict["reason"])
        report = scratchgc.gc(self.root, 7, apply=True, now=NOW)
        self.assertEqual(report["removed_count"], 0)
        self.assertTrue(folder.is_dir())

    def test_finished_run_idle_thirty_minutes_is_kept(self):
        folder = self.make("ws4-plan-idle30m", age_when=NOW - 30 * 60)
        verdict = scratchgc.assess(folder, NOW, 7, scratchgc.live_labels(NOW), 1)
        self.assertEqual(verdict["action"], "keep", verdict)
        self.assertIn("under 7 days", verdict["reason"])

    def test_live_run_idle_two_hours_is_kept(self):
        folder = self.make("ws4-plan-idle2h-live", age_when=NOW - 2 * 3600)
        self.live.return_value = {"ws4-plan-idle2h-live"}
        verdict = scratchgc.assess(folder, NOW, 7, scratchgc.live_labels(NOW), 1)
        self.assertEqual(verdict["action"], "keep", verdict)
        self.assertEqual(verdict["reason"], "live launcher")
        report = scratchgc.gc(self.root, 7, apply=True, now=NOW, finished_after_hours=1)
        self.assertEqual(report["removed_count"], 0)
        self.assertTrue(folder.is_dir())

    def test_finished_after_hours_flag_is_parsed(self):
        folder = self.make("hub-exec-flag", age_when=NOW - 2 * 3600)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scratchgc.cmd_scratch(["gc", "--finished-after-hours", "1", "--apply"])
        self.assertEqual(code, 0)
        self.assertIn("finished runs idle over 1 h", buf.getvalue())
        self.assertFalse(folder.exists())

    def test_finished_after_hours_rejects_a_bad_value(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(scratchgc.cmd_scratch(["gc", "--finished-after-hours", "soon"]), 2)


class UnmarkedWorkerTests(GcBase):
    def unmarked(self, label, *, age_when=OLD, ws="hub"):
        folder = self.root / ws / label
        (folder / "env").mkdir(parents=True)
        (folder / "env" / "data.bin").write_bytes(b"x" * 1024)
        age(folder, age_when)
        return folder

    def test_unmarked_worker_folder_stale_six_hours_is_reclaimed(self):
        folder = self.unmarked("hub-exec-foo", age_when=NOW - 6 * 3600)
        verdict = scratchgc.assess(folder, NOW, 5 / 24, scratchgc.live_labels(NOW))
        self.assertEqual(verdict["action"], "reclaim", verdict)
        self.assertEqual(verdict["reason"], "unmarked worker folder, stale")
        report = scratchgc.gc(self.root, 5 / 24, apply=True, now=NOW)
        self.assertEqual(report["removed_count"], 1)
        self.assertFalse(folder.exists())

    def test_unmarked_worker_folder_two_hours_old_is_kept(self):
        folder = self.unmarked("hub-exec-foo", age_when=NOW - 2 * 3600)
        verdict = scratchgc.assess(folder, NOW, 5 / 24, scratchgc.live_labels(NOW))
        self.assertEqual(verdict["action"], "keep", verdict)
        self.assertIn("under 0.208333 days", verdict["reason"])
        report = scratchgc.gc(self.root, 5 / 24, apply=True, now=NOW)
        self.assertEqual(report["removed_count"], 0)
        self.assertTrue(folder.is_dir())

    def test_unmarked_hand_made_folder_is_kept(self):
        folder = self.unmarked("serving-history-plan", age_when=NOW - 86400)
        verdict = scratchgc.assess(folder, NOW, 5 / 24, scratchgc.live_labels(NOW))
        self.assertEqual(verdict["action"], "keep", verdict)
        self.assertIn("no owner marker", verdict["reason"])
        report = scratchgc.gc(self.root, 5 / 24, apply=True, now=NOW)
        self.assertEqual(report["removed_count"], 0)
        self.assertTrue(folder.is_dir())

    def test_unmarked_postgres_data_folder_is_kept(self):
        folder = self.unmarked("hub-exec-pgdata-x", age_when=NOW - 86400)
        verdict = scratchgc.assess(folder, NOW, 5 / 24, scratchgc.live_labels(NOW))
        self.assertEqual(verdict["action"], "keep", verdict)
        self.assertIn("no owner marker", verdict["reason"])
        report = scratchgc.gc(self.root, 5 / 24, apply=True, now=NOW)
        self.assertEqual(report["removed_count"], 0)
        self.assertTrue(folder.is_dir())

    def test_unmarked_worker_folder_with_a_live_launcher_is_kept(self):
        folder = self.unmarked("hub-exec-busy", age_when=NOW - 86400)
        self.live.return_value = {"hub-exec-busy"}
        verdict = scratchgc.assess(folder, NOW, 5 / 24, scratchgc.live_labels(NOW))
        self.assertEqual(verdict["action"], "keep", verdict)
        self.assertEqual(verdict["reason"], "live launcher")

    def test_unmarked_worker_folder_in_use_is_kept(self):
        folder = self.unmarked("hub-exec-open", age_when=NOW - 86400)
        self.open_flag = True
        verdict = scratchgc.assess(folder, NOW, 5 / 24, scratchgc.live_labels(NOW))
        self.assertEqual(verdict["action"], "keep", verdict)
        self.assertEqual(verdict["reason"], "in use")

    def test_unmarked_worker_folder_is_kept_when_the_live_check_cannot_run(self):
        folder = self.unmarked("hub-exec-nolive", age_when=NOW - 86400)
        self.live.return_value = None
        verdict = scratchgc.assess(folder, NOW, 5 / 24, scratchgc.live_labels(NOW))
        self.assertEqual(verdict["action"], "keep", verdict)
        self.assertEqual(verdict["reason"], "live-process check did not run")

    def test_unmarked_worker_folder_is_kept_without_an_age_cutoff(self):
        folder = self.unmarked("hub-exec-noage", age_when=NOW - 86400)
        verdict = scratchgc.assess(folder, NOW, None, scratchgc.live_labels(NOW))
        self.assertEqual(verdict["action"], "keep", verdict)
        self.assertIn("no owner marker", verdict["reason"])

    def test_unreadable_owner_marker_keeps_the_folder(self):
        folder = self.unmarked("hub-exec-foo", age_when=NOW - 6 * 3600)
        (folder / scratchdir.OWNER_FILE).write_text("{not json", encoding="utf-8")
        age(folder, NOW - 6 * 3600)
        verdict = scratchgc.assess(folder, NOW, 5 / 24, scratchgc.live_labels(NOW))
        self.assertEqual(verdict["action"], "keep", verdict)
        self.assertEqual(verdict["reason"], "owner marker unreadable")
        report = scratchgc.gc(self.root, 5 / 24, apply=True, now=NOW)
        self.assertEqual(report["removed_count"], 0)
        self.assertTrue(folder.is_dir())


class CliFlagTests(GcBase):
    def test_non_finite_flags_are_rejected(self):
        for flag in ("--finished-after-hours", "--older-than-days"):
            for value in ("nan", "inf", "-inf"):
                with self.subTest(flag=flag, value=value):
                    err = io.StringIO()
                    with redirect_stderr(err):
                        code = scratchgc.cmd_scratch(["gc", flag, value])
                    self.assertEqual(code, 2)
                    self.assertIn("finite", err.getvalue())


class WorkstreamHelperTests(GcBase):
    def test_workstream_live_refuses_on_a_live_launcher(self):
        self.make("ws4-plan-busy")
        self.live.return_value = {"ws4-plan-busy"}
        verifiable, blockers = scratchgc.workstream_live("WS4", self.root, NOW)
        self.assertTrue(verifiable)
        self.assertIn("ws4-plan-busy", blockers)

    def test_workstream_live_counts_other_live_labels_of_the_same_ws(self):
        self.live.return_value = {"ws4-exec-new-lane"}
        verifiable, blockers = scratchgc.workstream_live("WS4", self.root, NOW)
        self.assertTrue(verifiable)
        self.assertEqual(blockers, ["ws4-exec-new-lane"])

    def test_workstream_live_unverifiable_when_listing_fails(self):
        self.live.return_value = None
        self.assertEqual(scratchgc.workstream_live("WS4", self.root, NOW), (False, []))

    def test_remove_workstream_removes_only_that_workstream(self):
        mine = self.make("ws4-plan-old")
        other = self.make("ws5-plan-old")
        out = scratchgc.remove_workstream("WS4", self.root, NOW)
        self.assertEqual(out["removed"], ["ws4-plan-old"])
        self.assertGreater(out["freed"], 0)
        self.assertFalse(mine.exists())
        self.assertTrue(other.exists())

    def test_remove_workstream_keeps_a_live_folder(self):
        busy = self.make("ws4-plan-busy2")
        self.live.return_value = {"ws4-plan-busy2"}
        out = scratchgc.remove_workstream("WS4", self.root, NOW)
        self.assertEqual(out["removed"], [])
        self.assertTrue(busy.is_dir())
        self.assertTrue(any("live launcher" in k for k in out["kept"]))


if __name__ == "__main__":
    unittest.main()
