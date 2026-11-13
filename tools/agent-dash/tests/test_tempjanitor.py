"""Temp janitor: temp trees on disk, fake lsof, ps and liveness.

Each test builds a throwaway temp root, ages its files, and runs the janitor
against a FakeHost whose lsof snapshot, process table and live pids the test
sets. No real /private/tmp or user temp dir is read or changed.
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import tempjanitor  # noqa: E402

DAY = 86400.0
MIN_AGE = 5 * 3600.0


def age_tree(path, days=10.0):
    """Backdate every file and directory under ``path``, including ``path``."""
    stamp = time.time() - days * DAY
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files + dirs:
            os.utime(os.path.join(root, name), (stamp, stamp), follow_symlinks=False)
    os.utime(path, (stamp, stamp))


def lsof_text(cwd_pid=None, cwd=None, open_file=None):
    lines = []
    if cwd_pid is not None:
        lines += [f"p{cwd_pid}", "fcwd", f"n{cwd}"]
    if open_file is not None:
        lines += [f"p{cwd_pid or 9}", "f4", f"n{open_file}"]
    return "\n".join(lines) + "\n"


class FakeHost(tempjanitor.TempHost):
    """Machine access replaced by test data. ``lsof_seq`` gives one answer per snapshot."""

    def __init__(self, root, lsof=None, ps="", alive=(), lsof_seq=None):
        super().__init__()
        self._root = root
        self._lsof = lsof
        self._lsof_seq = list(lsof_seq) if lsof_seq is not None else None
        self._ps = ps
        self._alive = set(alive)
        self.du_calls = []

    def roots(self):
        return (self._root,)

    def lsof_text(self):
        if self._lsof_seq is not None:
            return self._lsof_seq.pop(0) if len(self._lsof_seq) > 1 else self._lsof_seq[0]
        return self._lsof

    def ps_text(self):
        return self._ps

    def alive(self, pid):
        return pid in self._alive

    def du_bytes(self, path):
        self.du_calls.append(path)
        return 4096


def pg_ps(pid, datadir):
    return f"{pid} 1 {os.getuid()} 2-00:00:00 /opt/homebrew/bin/postgres -D {datadir} -p 5432\n"


class TempJanitorTests(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="tempjanitor-"))
        self.outside = os.path.realpath(tempfile.mkdtemp(prefix="tempjanitor-outside-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)

    def make(self, name, aged=True, files=("data/file.bin",)):
        path = os.path.join(self.root, name)
        for rel in files:
            full = os.path.join(path, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            Path(full).write_text("x" * 100)
        if aged:
            age_tree(path)
        return path

    def rows_by_name(self, host, min_age=MIN_AGE):
        rows, _ = tempjanitor.plan(host, min_age)
        return {os.path.basename(r["path"]): r for r in rows}

    def test_young_entry_is_kept(self):
        self.make("young", aged=False)
        row = self.rows_by_name(FakeHost(self.root, lsof=lsof_text()))["young"]
        self.assertEqual(row["action"], "keep")
        self.assertEqual(row["reason"], "changed within --min-age")

    def test_entry_with_process_cwd_is_kept(self):
        target = self.make("worker-scratch")
        host = FakeHost(self.root, lsof=lsof_text(cwd_pid=501, cwd=os.path.join(target, "sub")))
        row = self.rows_by_name(host)["worker-scratch"]
        self.assertEqual(row["reason"], "open file or cwd inside")

    def test_entry_with_open_file_is_kept(self):
        target = self.make("held")
        host = FakeHost(self.root, lsof=lsof_text(open_file=os.path.join(target, "data", "file.bin")))
        self.assertEqual(self.rows_by_name(host)["held"]["reason"], "open file or cwd inside")

    def test_pg_dir_with_live_postmaster_is_kept(self):
        pgdir = self.make("pg-live", files=("data/PG_VERSION",))
        datadir = os.path.join(pgdir, "data")
        Path(datadir, "postmaster.pid").write_text("4242\n" + datadir + "\n")
        age_tree(pgdir)
        host = FakeHost(self.root, lsof=lsof_text(), ps="", alive=[4242])
        self.assertEqual(self.rows_by_name(host)["pg-live"]["reason"], "postmaster.pid names a live pid")

    def test_running_postgres_data_dir_inside_is_kept(self):
        pgdir = self.make("pg-running", files=("data/PG_VERSION",))
        datadir = os.path.join(pgdir, "data")
        host = FakeHost(self.root, lsof=lsof_text(), ps=pg_ps(77, datadir))
        self.assertEqual(self.rows_by_name(host)["pg-running"]["reason"], "running postgres data dir inside")

    def test_git_checkout_and_linked_worktree_are_kept(self):
        repo = self.make("checkout", files=("README.md",))
        os.makedirs(os.path.join(repo, ".git"))
        age_tree(repo)
        worktree = self.make("linked", files=("src/app.py",))
        Path(worktree, ".git").write_text("gitdir: /elsewhere\n")
        age_tree(worktree)
        rows = self.rows_by_name(FakeHost(self.root, lsof=lsof_text()))
        self.assertEqual(rows["checkout"]["reason"], "git checkout or worktree")
        self.assertEqual(rows["linked"]["reason"], "git checkout or worktree")

    def test_system_and_session_names_are_kept(self):
        for name in ("com.apple.launchd.abc", "launchd-123.sock", ".X11-unix", "TemporaryItems",
                     "claude-session-fixture", "agent.sock"):
            self.make(name)
        rows = self.rows_by_name(FakeHost(self.root, lsof=lsof_text()))
        for name in ("com.apple.launchd.abc", "launchd-123.sock", "TemporaryItems", "claude-session-fixture", "agent.sock"):
            self.assertEqual(rows[name]["reason"], "allowlisted name", name)

    def test_stale_pg_dir_is_removed_on_apply_only(self):
        pgdir = self.make("pg-stale", files=("data/PG_VERSION", "data/base/1/1234"))
        host = FakeHost(self.root, lsof=lsof_text(), ps="")
        out = io.StringIO()
        with mock.patch.object(tempjanitor, "TempHost", return_value=host), contextlib.redirect_stdout(out):
            self.assertEqual(tempjanitor.cmd_temp(["--min-age", "5h"]), 0)
        self.assertTrue(os.path.isdir(pgdir), "dry run must not remove")
        self.assertIn("would remove", out.getvalue())
        self.assertIn("ANSWER: 1 of 1 temp entries would be removed", out.getvalue())

        host = FakeHost(self.root, lsof=lsof_text(), ps="")
        out = io.StringIO()
        with mock.patch.object(tempjanitor, "TempHost", return_value=host), contextlib.redirect_stdout(out):
            self.assertEqual(tempjanitor.cmd_temp(["--min-age", "5h", "--apply"]), 0)
        self.assertFalse(os.path.exists(pgdir))
        self.assertIn("ANSWER: removed 1 of 1 temp entries", out.getvalue())

    def test_symlink_is_never_followed_or_removed(self):
        keep_me = os.path.join(self.outside, "precious")
        Path(keep_me).mkdir()
        Path(keep_me, "file").write_text("keep")
        age_tree(self.outside)
        link = os.path.join(self.root, "link")
        os.symlink(keep_me, link)
        rows = tempjanitor.plan(FakeHost(self.root, lsof=lsof_text()), MIN_AGE)[0]
        self.assertFalse(any(os.path.basename(r["path"]) == "link" for r in rows))
        host = FakeHost(self.root, lsof=lsof_text())
        with mock.patch.object(tempjanitor, "TempHost", return_value=host), contextlib.redirect_stdout(io.StringIO()):
            tempjanitor.cmd_temp(["--apply"])
        self.assertTrue(os.path.isfile(os.path.join(keep_me, "file")))

    def test_lsof_failure_removes_nothing(self):
        pgdir = self.make("pg-stale2", files=("data/PG_VERSION",))
        host = FakeHost(self.root, lsof=None, ps="")
        err = io.StringIO()
        with mock.patch.object(tempjanitor, "TempHost", return_value=host), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(tempjanitor.cmd_temp(["--apply"]), 1)
        self.assertTrue(os.path.isdir(pgdir))
        self.assertIn("lsof did not run", err.getvalue())

    def test_ps_failure_removes_nothing(self):
        pgdir = self.make("pg-stale3", files=("data/PG_VERSION",))
        host = FakeHost(self.root, lsof=lsof_text(), ps=None)
        with mock.patch.object(tempjanitor, "TempHost", return_value=host), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tempjanitor.cmd_temp(["--apply"]), 1)
        self.assertTrue(os.path.isdir(pgdir))

    def test_entry_that_becomes_busy_before_removal_is_kept(self):
        pgdir = self.make("pg-busy", files=("data/PG_VERSION",))
        host = FakeHost(self.root, lsof_seq=[lsof_text(), lsof_text(cwd_pid=9, cwd=pgdir)], ps="")
        out = io.StringIO()
        with mock.patch.object(tempjanitor, "TempHost", return_value=host), contextlib.redirect_stdout(out):
            self.assertEqual(tempjanitor.cmd_temp(["--apply"]), 0)
        self.assertTrue(os.path.isdir(pgdir))
        self.assertIn("kept 1: kept on re-check: open file or cwd inside", out.getvalue())

    def test_walk_bound_keeps(self):
        self.make("huge", files=tuple(f"d/{i}" for i in range(20)))
        with mock.patch.object(tempjanitor, "MAX_WALK_ENTRIES", 5):
            row = self.rows_by_name(FakeHost(self.root, lsof=lsof_text()))["huge"]
        self.assertEqual(row["reason"], "walk bound reached")

    def test_bad_duration_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tempjanitor.cmd_temp(["--min-age", "soon"]), 2)


if __name__ == "__main__":
    unittest.main()
