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

import contextlib
import io
import sqlite3
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
    """Backdate a worktree's whole tree past the default minimum age.

    The reaper measures the newest file anywhere under the worktree, not the worktree folder's
    own mtime, so backdating only the folder no longer makes it look old.
    """
    stamp = time.time() - hours * 3600.0
    for root, dirs, files in os.walk(path):
        for name in files + dirs:
            try:
                os.utime(os.path.join(root, name), (stamp, stamp))
            except OSError:
                pass
    os.utime(path, (stamp, stamp))


def rows_by_path(rows):
    """Decision rows keyed by worktree path string."""
    return {row["path"]: row for row in rows}


class WorktreeAgeTest(unittest.TestCase):
    """The age gate reads the newest file anywhere under the worktree."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.root)], check=False, timeout=30)

    def test_deep_recent_edit_makes_the_worktree_young(self):
        deep = self.root / "api" / "src" / "lor_api"
        deep.mkdir(parents=True)
        old = time.time() - 72 * 3600.0
        (self.root / ".git").write_text("gitdir: somewhere\n")
        os.utime(self.root / ".git", (old, old))
        touched = deep / "route.py"
        touched.write_text("x")
        age = worktrees.worktree_age_s(str(self.root))
        self.assertIsNotNone(age)
        self.assertLess(age, 60)

    def test_stale_tree_with_a_live_folder_mtime_reads_as_old(self):
        deep = self.root / "api"
        deep.mkdir()
        (deep / "route.py").write_text("x")
        old = time.time() - 72 * 3600.0
        os.utime(deep / "route.py", (old, old))
        os.utime(deep, (old, old))
        (self.root / ".git").write_text("gitdir: somewhere\n")
        os.utime(self.root / ".git", (old, old))
        os.utime(self.root, (time.time(), time.time()))
        age = worktrees.worktree_age_s(str(self.root))
        self.assertIsNotNone(age)
        self.assertGreater(age, 71 * 3600.0)

    def test_regenerable_folders_do_not_count_as_activity(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "a.py").write_text("x")
        cache = self.root / ".venv" / "lib"
        cache.mkdir(parents=True)
        (cache / "b.py").write_text("x")
        old = time.time() - 48 * 3600.0
        for target in (self.root / "src" / "a.py", self.root / "src", self.root):
            os.utime(target, (old, old))
        age = worktrees.worktree_age_s(str(self.root))
        self.assertIsNotNone(age)
        self.assertGreater(age, 47 * 3600.0)

    def test_missing_path_has_no_age(self):
        self.assertIsNone(worktrees.worktree_age_s(str(self.root / "gone")))


class ReapDecisionsTest(unittest.TestCase):
    def setUp(self):
        self.repos = []
        self.procs = []
        patcher = mock.patch.object(worktrees, "gh_pr_merged", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The agentq job guard reads the live database by default; keep the suite hermetic.
        held = mock.patch.object(worktrees, "agentq_active_workspaces", return_value={})
        held.start()
        self.addCleanup(held.stop)
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

    def test_purge_age_removes_a_dirty_unmerged_lane(self):
        """Past --purge-age a lane goes even when it never merged and is dirty."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-abandoned")
        commit_in(path, "lane.txt")
        (Path(path) / "dirty.txt").write_text("uncommitted\n")
        make_old(path, hours=6)
        archive = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(archive)], check=False)
        with mock.patch.object(worktrees, "salvage", wraps=worktrees.salvage) as spy:
            with mock.patch("dash.dataroot.state_dir", return_value=archive):
                top, rows = worktrees.collect(
                    str(repo), worktrees.DEFAULT_MIN_AGE_S, set(), time.time(), 4 * 3600.0
                )
                row = rows_by_path(rows)[os.path.realpath(path)]
                self.assertEqual(row["decision"], "reap")
                self.assertTrue(row.get("purge"))
                rows = worktrees.apply_reaps(top, rows, False)
            self.assertTrue(spy.called)
        self.assertFalse(Path(path).exists())
        saved = archive / "wt-salvage"
        folders = list(saved.rglob("status.txt"))
        self.assertTrue(folders, "the dirty tree should be archived")
        self.assertIn("dirty.txt", folders[0].read_text())
        self.assertTrue(git(repo, "branch", "--list", "lane-abandoned").strip())

    def test_purge_age_keeps_a_young_lane(self):
        """--purge-age does not shorten --min-age for a lane written just now."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-live")
        commit_in(path, "lane.txt")
        top, rows = worktrees.collect(str(repo), 4 * 3600.0, set(), time.time(), 4 * 3600.0)
        row = rows_by_path(rows)[os.path.realpath(path)]
        self.assertEqual(row["decision"], "keep")
        self.assertIn("younger", row["reason"])

    def test_purge_age_keeps_a_skip_listed_lane(self):
        """The skip list still wins over the purge cutoff."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-pinned")
        commit_in(path, "lane.txt")
        make_old(path, hours=9)
        top, rows = worktrees.collect(
            str(repo), worktrees.DEFAULT_MIN_AGE_S, {"lane-pinned"}, time.time(), 4 * 3600.0
        )
        row = rows_by_path(rows)[os.path.realpath(path)]
        self.assertEqual(row["decision"], "keep")
        self.assertIn("skip list", row["reason"])

    def test_purge_age_reaches_the_cli(self):
        """`worktrees reap --purge-age` reaps a dirty unmerged lane."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-cli-purge")
        commit_in(path, "lane.txt")
        (Path(path) / "dirty.txt").write_text("uncommitted\n")
        make_old(path, hours=8)
        archive = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(archive)], check=False)
        with mock.patch("dash.dataroot.state_dir", return_value=archive):
            self.assertEqual(
                cmd_worktrees(
                    ["reap", "--repo", str(repo), "--purge-age", "4h", "--apply"]
                ),
                0,
            )
        self.assertFalse(Path(path).exists())

    def test_reap_reports_the_disk_delta_not_only_apparent_size(self):
        """A reaped lane reports what the volume gave back, not its du size."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-delta")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "lane-delta")
        make_old(path)
        reads = iter([10_000_000_000, 10_050_000_000])
        out = io.StringIO()
        with mock.patch.object(
            worktrees, "disk_free_bytes", side_effect=lambda _p: next(reads)
        ):
            with contextlib.redirect_stdout(out):
                code = cmd_worktrees(["reap", "--repo", str(repo), "--min-age", "0s", "--apply"])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertFalse(Path(path).exists())
        self.assertIn("reclaimed on disk", text)
        self.assertIn("47.7 MB", text)

    def test_reap_reports_zero_when_clones_give_nothing_back(self):
        """Removing clone copies can free no disk space; say zero, not unknown."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-clones")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "lane-clones")
        make_old(path)
        out = io.StringIO()
        with mock.patch.object(worktrees, "disk_free_bytes", return_value=10_000_000_000):
            with contextlib.redirect_stdout(out):
                code = cmd_worktrees(["reap", "--repo", str(repo), "--min-age", "0s", "--apply"])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("0 B reclaimed on disk", text)
        self.assertIn("copy-on-write clone", text)

    def test_disk_reclaimed_treats_a_loss_as_zero(self):
        """Other writers can eat the space; a negative delta is not a reclaim."""
        self.assertEqual(worktrees.disk_reclaimed(1000, 900), 0)
        self.assertEqual(worktrees.disk_reclaimed(1000, 1000), 0)
        self.assertEqual(worktrees.disk_reclaimed(1000, 1500), 500)
        self.assertIsNone(worktrees.disk_reclaimed(None, 1500))

    def test_reap_dry_run_claims_no_disk_reclaim(self):
        """A dry run prints no reclaim figure because nothing was removed."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-dryrun")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "lane-dryrun")
        make_old(path)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmd_worktrees(["reap", "--repo", str(repo)])
        self.assertEqual(code, 0)
        self.assertNotIn("reclaimed on disk", out.getvalue())
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

    def test_deploy_release_worktree_is_always_kept(self):
        """A detached release under agent-dash/releases is kept even when its commit is on main."""
        entry = {"path": "/home/u/.local/share/agent-dash/releases/abc123", "head": "abc123", "branch": None}
        row = worktrees.decide("/repo", entry, "/repo", "main", 0.0, set(), time.time())
        self.assertEqual(row["decision"], "keep")
        self.assertIn("deploy release", row["reason"])

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


class AgentqJobGuardTest(unittest.TestCase):
    """A worktree that is the workspace of a queued or running agentq job is kept.

    A requeued job (state ``queued``, no process) keeps its worktree, and the
    retry would recreate it from ``origin/main`` if the reaper removed it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "agentq"
        self.home.mkdir()
        self.path = Path(self._tmp.name) / "wt"
        self.path.mkdir()
        patcher = mock.patch.dict(os.environ, {"AGENTQ_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # No live process holds the temp path; the scan itself is covered elsewhere.
        for name, value in (("_proc_scan", ([], False)), ("_cwd_table", {}), ("_command_table", {})):
            patcher = mock.patch.object(worktrees, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_db(self, rows):
        conn = sqlite3.connect(self.home / "agentq.db")
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT, workspace TEXT)")
        conn.executemany("INSERT INTO jobs(id, state, workspace) VALUES (?, ?, ?)", rows)
        conn.commit()
        conn.close()

    def test_queued_job_workspace_is_busy(self):
        self.make_db([("abc123", "queued", str(self.path))])
        busy = worktrees.busy_map([str(self.path)])
        self.assertIn("abc123", busy[str(self.path)])
        self.assertIn("queued or running", busy[str(self.path)])

    def test_running_job_workspace_is_busy(self):
        self.make_db([("def456", "running", str(self.path))])
        self.assertIn("def456", worktrees.busy_map([str(self.path)])[str(self.path)])

    def test_finished_job_workspace_is_not_busy(self):
        self.make_db([("aaa111", "done", str(self.path)), ("bbb222", "failed", str(self.path))])
        self.assertEqual(worktrees.busy_map([str(self.path)]), {})

    def test_missing_database_holds_nothing(self):
        self.assertEqual(worktrees.agentq_active_workspaces(), {})
        self.assertEqual(worktrees.busy_map([str(self.path)]), {})

    def test_unreadable_database_holds_nothing(self):
        (self.home / "agentq.db").write_text("not a database", encoding="utf-8")
        self.assertEqual(worktrees.agentq_active_workspaces(), {})

    def test_decide_keeps_a_worktree_a_queued_job_holds(self):
        self.make_db([("abc123", "queued", str(self.path))])
        busy = worktrees.busy_map([str(self.path)])
        row = worktrees.decide("/tmp/repo", {"path": str(self.path), "branch": "agentq/abc123"},
                               "/tmp/repo", "main", 0, set(), time.time(),
                               busy_reason=busy.get(str(self.path), ""))
        self.assertEqual(row["decision"], "keep")
        self.assertIn("abc123", row["reason"])


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
