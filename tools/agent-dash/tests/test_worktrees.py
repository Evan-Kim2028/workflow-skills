"""Safe worktree reaper: temp git repos prove every keep and the one reap.

Covers the five lane cases on throwaway repos: a fresh-from-main
worktree is kept, a merged worktree with commits is removed, and dirty,
in-use, and young worktrees are kept. Never touches the live checkout.
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault(
    "AGENT_DASH_WORKSTREAMS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"),
)

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_dash
from dash import worktrees
from dash.worktrees import cmd_worktrees

GIT_IDENTITY = ["-c", "user.name=agent-dash-test", "-c", "user.email=agent-dash-test@example.invalid"]


def git(repo, *args):
    """Run git in ``repo`` and return stripped stdout. Raises on failure."""
    proc = subprocess.run(
        ["git", *GIT_IDENTITY, "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.stdout.strip()


def make_repo():
    """Temp repo on main with one commit and a local origin/main ref."""
    repo = Path(tempfile.mkdtemp(prefix="dash-reap-src-"))
    git(repo, "init", "-b", "main")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base commit")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def add_worktree(repo, name, branch):
    """Fresh worktree of ``repo`` on a new branch. Returns its path."""
    path = repo.parent / (repo.name + "-" + name)
    git(repo, "worktree", "add", str(path), "-b", branch)
    return path


def commit_in(path, filename, body="work\n"):
    """Commit one file inside the worktree at ``path``."""
    (Path(path) / filename).write_text(body, encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "lane commit " + filename)


def merge_into_main(repo, branch):
    """Merge ``branch`` into main with a merge commit and move origin/main."""
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", branch, "-m", "merge " + branch)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


def make_old(path, hours=5):
    """Backdate a worktree directory past the default minimum age."""
    stamp = time.time() - hours * 3600.0
    os.utime(path, (stamp, stamp))


def rows_by_path(rows):
    """Decision rows keyed by worktree path string."""
    return {row["path"]: row for row in rows}


class ReapDecisionsTest(unittest.TestCase):
    def setUp(self):
        self.repos = []
        self.procs = []
        patcher = mock.patch.object(worktrees, "gh_pr_merged", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for proc in self.procs:
            try:
                proc.terminate()
            except OSError:
                pass
        for proc in self.procs:
            try:
                proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for repo in self.repos:
            subprocess.run(["rm", "-rf", str(repo)], check=False, timeout=30)
            sibling = repo.parent / (repo.name + "-wt")
            subprocess.run(["rm", "-rf", str(sibling)], check=False, timeout=30)

    def _repo(self):
        repo = make_repo()
        self.repos.append(repo)
        return repo

    def test_fresh_from_main_is_kept(self):
        """A worktree just created from main has no commits of its own."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-fresh")
        make_old(path)
        top, rows = worktrees.collect(str(repo), worktrees.DEFAULT_MIN_AGE_S, set(), time.time())
        row = rows_by_path(rows)[os.path.realpath(path)]
        self.assertEqual(row["decision"], "keep")
        self.assertIn("fresh from main", row["reason"])
        self.assertEqual(
            cmd_worktrees(["reap", "--repo", str(repo), "--min-age", "0s"]),
            0,
        )
        self.assertTrue(Path(path).exists())

    def test_merged_with_commits_is_removed(self):
        """Dry-run lists the merged worktree; apply removes it and the branch."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-merged")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "lane-merged")
        make_old(path)
        top, rows = worktrees.collect(str(repo), worktrees.DEFAULT_MIN_AGE_S, set(), time.time())
        row = rows_by_path(rows)[os.path.realpath(path)]
        self.assertEqual(row["decision"], "reap")
        self.assertEqual(
            cmd_worktrees(["reap", "--repo", str(repo), "--min-age", "0s"]),
            0,
        )
        self.assertTrue(Path(path).exists())
        self.assertEqual(
            cmd_worktrees(["reap", "--repo", str(repo), "--min-age", "0s", "--apply"]),
            0,
        )
        self.assertFalse(Path(path).exists())
        self.assertEqual(git(repo, "branch", "--list", "lane-merged"), "")

    def test_dirty_is_kept(self):
        """Uncommitted changes keep an otherwise reapable worktree."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-dirty")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "lane-dirty")
        make_old(path)
        (Path(path) / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
        top, rows = worktrees.collect(str(repo), worktrees.DEFAULT_MIN_AGE_S, set(), time.time())
        row = rows_by_path(rows)[os.path.realpath(path)]
        self.assertEqual(row["decision"], "keep")
        self.assertIn("dirty", row["reason"])
        self.assertEqual(
            cmd_worktrees(["reap", "--repo", str(repo), "--min-age", "0s", "--apply"]),
            0,
        )
        self.assertTrue(Path(path).exists())

    def test_in_use_cwd_is_kept(self):
        """A live process sitting in the worktree blocks the reap."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-busy")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "lane-busy")
        make_old(path)
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=str(path),
        )
        self.procs.append(proc)
        time.sleep(1.0)
        self.assertIsNone(proc.poll())
        top, rows = worktrees.collect(str(repo), worktrees.DEFAULT_MIN_AGE_S, set(), time.time())
        row = rows_by_path(rows)[os.path.realpath(path)]
        self.assertEqual(row["decision"], "keep")
        self.assertIn("cwd", row["reason"])
        self.assertEqual(
            cmd_worktrees(["reap", "--repo", str(repo), "--min-age", "0s", "--apply"]),
            0,
        )
        self.assertTrue(Path(path).exists())

    def test_young_is_kept(self):
        """A merged worktree younger than the minimum age waits."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-young")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "lane-young")
        top, rows = worktrees.collect(str(repo), worktrees.DEFAULT_MIN_AGE_S, set(), time.time())
        row = rows_by_path(rows)[os.path.realpath(path)]
        self.assertEqual(row["decision"], "keep")
        self.assertIn("younger", row["reason"])

    def test_skip_list_is_kept(self):
        """A skip entry by branch or by path keeps the worktree."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-skipped")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "lane-skipped")
        make_old(path)
        top, rows = worktrees.collect(
            str(repo), 0.0, {"lane-skipped", os.path.realpath(path)}, time.time()
        )
        row = rows_by_path(rows)[os.path.realpath(path)]
        self.assertEqual(row["decision"], "keep")
        self.assertIn("skip", row["reason"])

    def test_merged_pull_request_counts_as_merged(self):
        """A merged pull request proves the work even off the main line."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-pr")
        commit_in(path, "lane.txt")
        make_old(path)
        with mock.patch.object(worktrees, "gh_pr_merged", return_value=True):
            top, rows = worktrees.collect(str(repo), worktrees.DEFAULT_MIN_AGE_S, set(), time.time())
        row = rows_by_path(rows)[os.path.realpath(path)]
        self.assertEqual(row["decision"], "reap")

    def _detached(self, repo, name):
        """Detached worktree at the current main tip. Returns its path."""
        path = repo.parent / (repo.name + "-" + name)
        git(repo, "worktree", "add", "--detach", str(path), "HEAD")
        return path

    def test_detached_on_main_is_removed(self):
        """A detached HEAD whose commit is on origin/main is reaped, and no branch is deleted."""
        repo = self._repo()
        path = self._detached(repo, "det-main")
        make_old(path)
        top, rows = worktrees.collect(str(repo), worktrees.DEFAULT_MIN_AGE_S, set(), time.time())
        row = rows_by_path(rows)[os.path.realpath(path)]
        self.assertEqual(row["decision"], "reap")
        self.assertIn("ancestor of origin/main", row["reason"])
        self.assertEqual(
            cmd_worktrees(["reap", "--repo", str(repo), "--min-age", "0s", "--apply"]),
            0,
        )
        self.assertFalse(Path(path).exists())
        self.assertEqual(git(repo, "branch", "--list").count("\n"), 0)

    def test_detached_with_own_commit_is_kept(self):
        """A detached HEAD carrying a commit main lacks is kept."""
        repo = self._repo()
        path = self._detached(repo, "det-work")
        commit_in(path, "lane.txt")
        make_old(path)
        top, rows = worktrees.collect(str(repo), worktrees.DEFAULT_MIN_AGE_S, set(), time.time())
        row = rows_by_path(rows)[os.path.realpath(path)]
        self.assertEqual(row["decision"], "keep")
        self.assertIn("not on origin/main", row["reason"])
        self.assertEqual(
            cmd_worktrees(["reap", "--repo", str(repo), "--min-age", "0s", "--apply"]),
            0,
        )
        self.assertTrue(Path(path).exists())

    def test_dirty_detached_is_kept(self):
        """Uncommitted changes keep a detached worktree on main."""
        repo = self._repo()
        path = self._detached(repo, "det-dirty")
        make_old(path)
        (Path(path) / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
        top, rows = worktrees.collect(str(repo), worktrees.DEFAULT_MIN_AGE_S, set(), time.time())
        row = rows_by_path(rows)[os.path.realpath(path)]
        self.assertEqual(row["decision"], "keep")
        self.assertIn("dirty", row["reason"])

    def test_young_detached_is_kept(self):
        """A detached worktree on main younger than the minimum age waits."""
        repo = self._repo()
        path = self._detached(repo, "det-young")
        top, rows = worktrees.collect(str(repo), worktrees.DEFAULT_MIN_AGE_S, set(), time.time())
        row = rows_by_path(rows)[os.path.realpath(path)]
        self.assertEqual(row["decision"], "keep")
        self.assertIn("younger", row["reason"])

    def test_durations_and_main_wiring(self):
        """Duration parsing, table shape, and the agent_dash entry point."""
        self.assertEqual(worktrees.parse_duration("3h"), 10800.0)
        self.assertEqual(worktrees.parse_duration("90m"), 5400.0)
        self.assertEqual(worktrees.parse_duration("30s"), 30.0)
        self.assertEqual(worktrees.parse_duration("2d"), 172800.0)
        self.assertIsNone(worktrees.parse_duration("soon"))
        table = worktrees.format_table(
            [{"path": "/tmp/wt", "branch": "lane-x", "decision": "keep", "reason": "young"}]
        )
        self.assertIn("path", table.splitlines()[0])
        self.assertIn("lane-x", table)
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-wired")
        self.assertEqual(agent_dash.main(["worktrees", "reap", "--repo", str(repo)]), 0)
        self.assertEqual(agent_dash.main(["worktrees"]), 2)
        self.assertEqual(cmd_worktrees(["reap"]), 2)


class ProcScanCapTest(unittest.TestCase):
    def test_capped_scan_keeps_every_candidate(self):
        """Hitting MAX_PIDS marks the scan partial, so no worktree is reaped."""
        path = "/tmp/agent-dash-capped-worktree"
        with mock.patch.object(worktrees, "MAX_PIDS", 1):
            pids, partial = worktrees._proc_scan()
            self.assertEqual(len(pids), 1)
            self.assertTrue(partial)
            busy = worktrees.busy_map([path])
        self.assertEqual(set(busy), {path})
        self.assertIn("incomplete", busy[path])

    def test_failed_pid_listing_keeps_every_candidate(self):
        """A ps failure or timeout with no /proc is an unknown scan, never an empty one."""
        path = "/tmp/agent-dash-unscanned-worktree"
        failures = [OSError("ps missing"), subprocess.TimeoutExpired("ps", 1)]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(worktrees.os, "listdir", side_effect=OSError("no /proc")), \
                    mock.patch.object(worktrees.subprocess, "run", side_effect=failure):
                pids, partial = worktrees._proc_scan()
                self.assertEqual(pids, [])
                self.assertTrue(partial)
                busy = worktrees.busy_map([path])
            self.assertEqual(set(busy), {path})
        nonzero = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(worktrees.os, "listdir", side_effect=OSError("no /proc")), \
                mock.patch.object(worktrees.subprocess, "run", return_value=nonzero):
            self.assertEqual(worktrees._proc_scan(), ([], True))

    def test_uncapped_scan_is_not_partial(self):
        """A normal scan reports itself complete."""
        _pids, partial = worktrees._proc_scan()
        self.assertFalse(partial)


class MergedPrLookupTest(unittest.TestCase):
    def setUp(self):
        worktrees._MERGED_HEADS.clear()
        self.addCleanup(worktrees._MERGED_HEADS.clear)

    def _fake_gh(self, stdout="", returncode=0):
        return mock.patch.object(
            worktrees.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=""),
        )

    def test_one_bulk_call_per_repo_answers_every_branch(self):
        """Many branch checks cost one gh call, and a branch matches by its head name."""
        payload = '[{"headRefName": "lane-a"}, {"headRefName": "lane-b"}]'
        with self._fake_gh(payload) as run:
            self.assertTrue(worktrees.gh_pr_merged("/repo/x", "lane-a"))
            self.assertFalse(worktrees.gh_pr_merged("/repo/x", "lane-c"))
            self.assertTrue(worktrees.gh_pr_merged("/repo/x", "lane-b"))
        self.assertEqual(run.call_count, 1)
        self.assertIn("1000", run.call_args.args[0])

    def test_failed_lookup_reads_as_no_merged_pull_requests(self):
        """A failed gh call keeps the worktree; it is not read as merged."""
        with self._fake_gh("", returncode=1):
            self.assertFalse(worktrees.gh_pr_merged("/repo/y", "lane-a"))
        with mock.patch.object(worktrees.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 60)):
            worktrees._MERGED_HEADS.clear()
            self.assertFalse(worktrees.gh_pr_merged("/repo/y", "lane-a"))


if __name__ == "__main__":
    unittest.main()
