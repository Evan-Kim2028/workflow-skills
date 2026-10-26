"""Automatic worktree cleanup for workstream lanes: temp git repos prove every case.

Covers the required cases on throwaway repos under one directory per
test (no shared-temp globs, no home directory, no network: ``fetch``,
``resolve_repos`` and ``pr_merged`` are patched out): a merged branch
is removed, dirty, unpushed, ignored-file, and live-process worktrees
are kept, allowlisted generated output is deleted first and the
worktree removed, a failed process scan keeps the worktree, the main
checkout is never touched, a ``ws task done`` status move triggers the
gc, a failing gc hook keeps the status change at exit 0 with the
warning off stdout under ``--json``, a refused ``git branch -d`` is
reported not raised, ``--no-gc`` skips the hook, and ``--dry-run``
removes nothing and never fetches. Never touches the live checkout.
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault(
    "AGENT_DASH_WORKSTREAMS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"),
)
import contextlib
import io
import json
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import bundles, worktrees, wsgc
from dash.bundles import cmd_ws

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


def make_repo(parent):
    """Temp repo under ``parent`` on main with one commit and a local origin/main ref."""
    repo = Path(tempfile.mkdtemp(prefix="dash-gc-src-", dir=str(parent)))
    git(repo, "init", "-b", "main")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base commit")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def fake_resolve_repos(ws, bundle_dir, repo_names):
    """Only the bundle's own absolute ``repos`` entries. No home dir, no network."""
    repos: list[str] = []
    if bundle_dir is not None:
        try:
            meta = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        entries = meta.get("repos") if isinstance(meta, dict) else None
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, str) and os.path.isabs(entry):
                    repos.append(os.path.realpath(entry))
    return sorted(set(repos))


def lane_path(repo, slug):
    """Sibling worktree path matching the lane slug convention."""
    return repo.parent / ("lor-wt-" + slug)


def add_lane(repo, slug, branch):
    """Fresh worktree of ``repo`` at a slug-convention path. Returns its path."""
    path = lane_path(repo, slug)
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


def make_bundle(ws, repo, trains):
    """Fresh bundle naming train branches and pointing ``repos`` at ``repo``."""
    assert cmd_ws(["new", ws, "Gc lane " + ws]) == 0
    root = Path(os.environ["AGENT_DASH_WORKSTREAMS"])
    plan_path = root / ws / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    tasks = plan["dag"]["groups"][0]["tasks"]
    known = {task["id"] for task in tasks if isinstance(task, dict)}
    for _train_id, _branch, members in trains:
        for member in members:
            if member not in known:
                tasks.append({"id": member, "title": "Task " + member, "status": "todo"})
                known.add(member)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    for train_id, branch, members in trains:
        assert bundles.set_train(ws, train_id, ws + " " + train_id, branch=branch, add=members) == 0
    root = Path(os.environ["AGENT_DASH_WORKSTREAMS"])
    meta_path = root / ws / "bundle.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["repos"] = [str(repo)]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def log_rows(ws):
    """Parsed ``(action, path, branch)`` rows from the bundle gc log."""
    root = Path(os.environ["AGENT_DASH_WORKSTREAMS"])
    log_path = root / ws / "worktree-gc.log"
    if not log_path.is_file():
        return []
    rows = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        assert len(parts) == 5, "gc log line has ISO time, path, branch, action, reason: " + line
        rows.append({"time": parts[0], "path": parts[1], "branch": parts[2], "action": parts[3], "reason": parts[4]})
    return rows


class WsGcTest(unittest.TestCase):
    DIR = os.path.dirname(os.path.abspath(__file__))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AGENT_DASH_WORKSTREAMS")
        os.environ["AGENT_DASH_WORKSTREAMS"] = self.tmp.name
        # One directory per test: every repo and lane path lives under it,
        # so cleanup is just dropping the directory. No globs, no rm -rf.
        self.case = Path(self.tmp.name) / self._testMethodName
        self.case.mkdir(parents=True, exist_ok=True)
        self.repos: list[Path] = []
        pr_patcher = mock.patch.object(wsgc, "pr_merged", return_value=False)
        pr_patcher.start()
        self.pr_patcher = pr_patcher
        self.addCleanup(pr_patcher.stop)
        fetch_patcher = mock.patch.object(wsgc, "fetch_repo")
        self.fetch_mock = fetch_patcher.start()
        self.addCleanup(fetch_patcher.stop)
        resolve_patcher = mock.patch.object(wsgc, "resolve_repos", side_effect=fake_resolve_repos)
        resolve_patcher.start()
        self.addCleanup(resolve_patcher.stop)
        self.addCleanup(self._cleanup)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old
        self.tmp.cleanup()

    def _cleanup(self):
        for repo in self.repos:
            if not Path(repo).is_dir():
                continue
            try:
                listed = subprocess.run(
                    ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                paths = [line[len("worktree "):] for line in listed.stdout.splitlines() if line.startswith("worktree ")]
                for path in paths[1:]:
                    subprocess.run(
                        ["git", "-C", str(repo), "worktree", "remove", "--force", path],
                        capture_output=True,
                        timeout=30,
                    )
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _repo(self):
        repo = make_repo(self.case)
        self.repos.append(repo)
        return repo

    def _finished_lane(self, ws):
        """Create one clean, merged, finished lane eligible for gc."""
        repo = self._repo()
        branch = "train/" + ws.lower() + "-one"
        make_bundle(ws, repo, [("T1", branch, [ws + ".1"])])
        path = add_lane(repo, ws.lower() + "-one", branch)
        commit_in(path, "lane.txt")
        merge_into_main(repo, branch)
        self.assertEqual(bundles.set_task(ws, ws + ".1", "done", None, None), 0)
        return repo, path, branch

    def test_merged_branch_removed(self):
        """A finished, merged lane worktree is removed and its branch deleted."""
        repo = self._repo()
        ws = "WS90"
        make_bundle(ws, repo, [("T1", "train/ws90-one", [ws + ".1"])])
        path = add_lane(repo, "ws90-one", "train/ws90-one")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "train/ws90-one")
        self.assertEqual(bundles.set_task(ws, ws + ".1", "done", None, None), 0)
        self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertFalse(Path(path).exists())
        self.assertEqual(git(repo, "branch", "--list", "train/ws90-one"), "")
        rows = log_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "remove")
        self.assertIn("train/ws90-one", rows[0]["branch"])

    def test_dirty_kept(self):
        """Uncommitted changes keep an otherwise removable worktree."""
        repo = self._repo()
        ws = "WS91"
        make_bundle(ws, repo, [("T1", "train/ws91-one", [ws + ".1"])])
        path = add_lane(repo, "ws91-one", "train/ws91-one")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "train/ws91-one")
        self.assertEqual(bundles.set_task(ws, ws + ".1", "done", None, None), 0)
        (Path(path) / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
        self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertTrue(Path(path).exists())
        self.assertNotEqual(git(repo, "branch", "--list", "train/ws91-one"), "")
        rows = log_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "keep")
        self.assertIn("dirty", rows[0]["reason"])

    def test_unpushed_kept(self):
        """A finished task keeps the worktree when its branch is unpushed and unmerged."""
        repo = self._repo()
        ws = "WS92"
        make_bundle(ws, repo, [("T1", "train/ws92-one", [ws + ".1"])])
        path = add_lane(repo, "ws92-one", "train/ws92-one")
        commit_in(path, "lane.txt")
        self.assertEqual(bundles.set_task(ws, ws + ".1", "done", None, None), 0)
        self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertTrue(Path(path).exists())
        rows = log_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "keep")
        self.assertIn("unpushed", rows[0]["reason"])

    def test_main_checkout_never_touched(self):
        """The main checkout is kept even when its branch belongs to the workstream."""
        repo = self._repo()
        ws = "WS93"
        make_bundle(ws, repo, [("T1", "train/ws93-one", [ws + ".1"])])
        git(repo, "checkout", "-b", "ws93/mainlane")
        try:
            self.assertEqual(bundles.set_task(ws, ws + ".1", "done", None, None), 0)
            self.assertEqual(cmd_ws(["gc", ws]), 0)
            self.assertTrue(repo.is_dir())
            self.assertNotEqual(git(repo, "branch", "--list", "ws93/mainlane"), "")
        finally:
            git(repo, "checkout", "main")
        rows = log_rows(ws)
        self.assertTrue(any(row["action"] == "keep" and "main checkout" in row["reason"] for row in rows))

    def test_task_status_trigger(self):
        """``ws task done`` runs the gc; ``--no-gc`` skips it."""
        repo = self._repo()
        ws = "WS94"
        make_bundle(
            ws,
            repo,
            [
                ("T1", "train/ws94-one", [ws + ".1"]),
                ("T2", "train/ws94-two", [ws + ".2"]),
            ],
        )
        first = add_lane(repo, "ws94-one", "train/ws94-one")
        commit_in(first, "one.txt")
        merge_into_main(repo, "train/ws94-one")
        second = add_lane(repo, "ws94-two", "train/ws94-two")
        commit_in(second, "two.txt")
        self.assertEqual(cmd_ws(["task", ws, ws + ".1", "done"]), 0)
        self.assertFalse(Path(first).exists())
        self.assertTrue(Path(second).exists())
        self.assertEqual(cmd_ws(["task", ws, ws + ".2", "done", "--no-gc"]), 0)
        self.assertTrue(Path(second).exists())
        rows = log_rows(ws)
        by_path = {row["path"]: row for row in rows}
        self.assertEqual(by_path[os.path.realpath(first)]["action"], "remove")

    def test_dry_run_removes_nothing(self):
        """``--dry-run`` lists the worktree, removes nothing, fetches nothing, logs nothing."""
        repo = self._repo()
        ws = "WS95"
        make_bundle(ws, repo, [("T1", "train/ws95-one", [ws + ".1"])])
        path = add_lane(repo, "ws95-one", "train/ws95-one")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "train/ws95-one")
        self.assertEqual(bundles.set_task(ws, ws + ".1", "done", None, None), 0)
        self.assertEqual(cmd_ws(["gc", ws, "--dry-run"]), 0)
        self.assertTrue(Path(path).exists())
        self.assertNotEqual(git(repo, "branch", "--list", "train/ws95-one"), "")
        self.fetch_mock.assert_not_called()
        live, removable = wsgc.dry_run_summary(ws)
        self.assertEqual((live, removable), (1, 1))
        self.fetch_mock.assert_not_called()
        root = Path(self.tmp.name)
        self.assertFalse((root / ws / "worktree-gc.log").exists())

    def test_nonmatching_path_kept(self):
        """A belonging branch at a non-slug path is never removed."""
        repo = self._repo()
        ws = "WS96"
        make_bundle(ws, repo, [("T1", "train/ws96-one", [ws + ".1"])])
        plan_path = Path(self.tmp.name) / ws / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["trains"].append(
            {"id": "T9", "title": "plain lane", "branch": "ws96/plain", "members": [ws + ".1"], "status": "planned"}
        )
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        plain = repo.parent / "ws96-plain"
        git(repo, "worktree", "add", str(plain), "-b", "ws96/plain")
        commit_in(plain, "lane.txt")
        merge_into_main(repo, "ws96/plain")
        self.assertEqual(bundles.set_task(ws, ws + ".1", "done", None, None), 0)
        self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertTrue(Path(plain).exists())
        rows = log_rows(ws)
        self.assertTrue(any(row["action"] == "keep" and "slug convention" in row["reason"] for row in rows))

    def test_ignored_secret_env_kept(self):
        """An ignored ``secret.env`` outside the allowlist keeps the worktree."""
        repo = self._repo()
        ws = "WS97"
        make_bundle(ws, repo, [("T1", "train/ws97-one", [ws + ".1"])])
        path = add_lane(repo, "ws97-one", "train/ws97-one")
        (Path(path) / ".gitignore").write_text("secret.env\n", encoding="utf-8")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "train/ws97-one")
        self.assertEqual(bundles.set_task(ws, ws + ".1", "done", None, None), 0)
        (Path(path) / "secret.env").write_text("s3cr3t\n", encoding="utf-8")
        self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertTrue(Path(path).exists())
        self.assertNotEqual(git(repo, "branch", "--list", "train/ws97-one"), "")
        rows = log_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "keep")
        self.assertIn("ignored files present", rows[0]["reason"])
        self.assertIn("secret.env", rows[0]["reason"])

    def test_nested_benchmarks_and_venv_removed(self):
        """Nested benchmark output and a venv are deleted before removing the worktree."""
        repo = self._repo()
        ws = "WS106"
        make_bundle(ws, repo, [("T1", "train/ws106-one", [ws + ".1"])])
        path = add_lane(repo, "ws106-one", "train/ws106-one")
        (Path(path) / ".gitignore").write_text(".benchmarks/\n.venv/\n", encoding="utf-8")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "train/ws106-one")
        self.assertEqual(bundles.set_task(ws, ws + ".1", "done", None, None), 0)
        benchmark = Path(path) / "api" / ".benchmarks" / "x.json"
        benchmark.parent.mkdir(parents=True)
        benchmark.write_text("{}\n", encoding="utf-8")
        venv_bin = Path(path) / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").write_text("binary\n", encoding="utf-8")
        self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertFalse(Path(path).exists())
        self.assertEqual(git(repo, "branch", "--list", "train/ws106-one"), "")
        rows = log_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "remove")

    def test_allowlisted_venv_only_removed(self):
        """An ignored ``.venv`` alone does not keep the worktree; it is deleted first."""
        repo = self._repo()
        ws = "WS98"
        make_bundle(ws, repo, [("T1", "train/ws98-one", [ws + ".1"])])
        path = add_lane(repo, "ws98-one", "train/ws98-one")
        (Path(path) / ".gitignore").write_text(".venv/\n", encoding="utf-8")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "train/ws98-one")
        self.assertEqual(bundles.set_task(ws, ws + ".1", "done", None, None), 0)
        venv_bin = Path(path) / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").write_text("binary\n", encoding="utf-8")
        self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertFalse(Path(path).exists())
        self.assertEqual(git(repo, "branch", "--list", "train/ws98-one"), "")
        rows = log_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "remove")

    def test_live_process_kept(self):
        """A process sitting in the worktree keeps it."""
        repo = self._repo()
        ws = "WS99"
        make_bundle(ws, repo, [("T1", "train/ws99-one", [ws + ".1"])])
        path = add_lane(repo, "ws99-one", "train/ws99-one")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "train/ws99-one")
        self.assertEqual(bundles.set_task(ws, ws + ".1", "done", None, None), 0)
        proc = subprocess.Popen(["sleep", "60"], cwd=str(path))
        try:
            self.assertEqual(cmd_ws(["gc", ws]), 0)
            self.assertTrue(Path(path).exists())
            self.assertNotEqual(git(repo, "branch", "--list", "train/ws99-one"), "")
            rows = log_rows(ws)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["action"], "keep")
            self.assertIn("live worker", rows[0]["reason"])
        finally:
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=30)

    def test_process_check_failure_kept(self):
        """When the process scan cannot run, the worktree is kept, not removed."""
        repo = self._repo()
        ws = "WS100"
        make_bundle(ws, repo, [("T1", "train/ws100-one", [ws + ".1"])])
        path = add_lane(repo, "ws100-one", "train/ws100-one")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "train/ws100-one")
        self.assertEqual(bundles.set_task(ws, ws + ".1", "done", None, None), 0)
        with mock.patch.object(wsgc, "snapshot_processes", return_value=None):
            self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertTrue(Path(path).exists())
        self.assertNotEqual(git(repo, "branch", "--list", "train/ws100-one"), "")
        rows = log_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "keep")
        self.assertEqual(rows[0]["reason"], wsgc.LIVE_CHECK_FAILED)

    def test_cwd_source_failure_kept(self):
        """A failed cwd scan keeps the worktree even when the command scan works."""
        ws = "WS103"
        repo, path, branch = self._finished_lane(ws)
        pid = 123456
        with mock.patch.object(worktrees, "_proc_pids", return_value=[pid]), \
                mock.patch.object(worktrees, "_cwd_table", return_value=None), \
                mock.patch.object(worktrees, "_command_table", return_value={pid: "sleep 60"}):
            self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertTrue(Path(path).exists())
        self.assertNotEqual(git(repo, "branch", "--list", branch), "")
        rows = log_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "keep")
        self.assertEqual(rows[0]["reason"], wsgc.LIVE_CHECK_FAILED)

    def test_command_source_failure_kept(self):
        """A failed command scan keeps the worktree even when cwd scanning works."""
        ws = "WS104"
        repo, path, branch = self._finished_lane(ws)
        pid = 123456
        with mock.patch.object(worktrees, "_proc_pids", return_value=[pid]), \
                mock.patch.object(worktrees, "_cwd_table", return_value={pid: str(path)}), \
                mock.patch.object(worktrees, "_command_table", return_value=None):
            self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertTrue(Path(path).exists())
        self.assertNotEqual(git(repo, "branch", "--list", branch), "")
        rows = log_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "keep")
        self.assertEqual(rows[0]["reason"], wsgc.LIVE_CHECK_FAILED)

    def test_empty_successful_process_tables_do_not_block_gc(self):
        """Successful empty cwd and command scans allow normal gc decisions."""
        ws = "WS105"
        _repo, path, _branch = self._finished_lane(ws)
        with mock.patch.object(worktrees, "_proc_pids", return_value=[123456]), \
                mock.patch.object(worktrees, "_cwd_table", return_value={}), \
                mock.patch.object(worktrees, "_command_table", return_value={}):
            self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertFalse(Path(path).exists())
        rows = log_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "remove")

    def test_task_gc_failure_keeps_status(self):
        """A failing gc hook keeps the status change at exit 0, warning off stdout under --json."""
        repo = self._repo()
        ws = "WS101"
        make_bundle(ws, repo, [("T1", "train/ws101-one", [ws + ".1"])])
        path = add_lane(repo, "ws101-one", "train/ws101-one")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "train/ws101-one")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(wsgc, "gc_after_task", side_effect=RuntimeError("boom")):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cmd_ws(["task", ws, ws + ".1", "done", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(wsgc.task_statuses(ws).get(ws + ".1"), "done")
        self.assertTrue(Path(path).exists())
        self.assertNotIn("warning", out.getvalue())
        self.assertIn("warning", err.getvalue())

    def squash_merge_into_main(self, repo, branch):
        """Squash-merge ``branch`` into main and move origin/main.

        The branch tip stays off origin/main, like a GitHub squash
        merge whose remote branch was deleted.
        """
        git(repo, "checkout", "main")
        git(repo, "merge", "--squash", branch)
        git(repo, "commit", "-m", "squash merge " + branch)
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    def test_squash_merged_pr_removed(self):
        """A squash-merged branch with a deleted remote is removed via its merged PR."""
        repo = self._repo()
        ws = "WS110"
        make_bundle(ws, repo, [("T1", "train/ws110-one", [ws + ".1"])])
        path = add_lane(repo, "ws110-one", "train/ws110-one")
        commit_in(path, "lane.txt")
        self.squash_merge_into_main(repo, "train/ws110-one")
        with mock.patch.object(wsgc, "pr_merged", return_value=True):
            _bundle_dir, _repos, preview = wsgc.collect(ws, fetch=False)
            self.assertEqual(len(preview), 1)
            self.assertEqual(preview[0]["action"], "remove")
            self.assertIn("pull request merged", preview[0]["reason"])
            self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertFalse(Path(path).exists())
        # The tip is not an ancestor, so ``git branch -d`` refuses and
        # the branch stays while the worktree goes.
        self.assertNotEqual(git(repo, "branch", "--list", "train/ws110-one"), "")
        rows = log_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "remove")
        self.assertIn("removed worktree", rows[0]["reason"])
        self.assertIn("refused", rows[0]["reason"])

    def test_gh_failure_keeps(self):
        """When the gh merge check cannot run, the worktree is kept fail-closed."""
        repo = self._repo()
        ws = "WS111"
        make_bundle(ws, repo, [("T1", "train/ws111-one", [ws + ".1"])])
        path = add_lane(repo, "ws111-one", "train/ws111-one")
        commit_in(path, "lane.txt")
        with mock.patch.object(wsgc, "pr_merged", return_value=None):
            self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertTrue(Path(path).exists())
        self.assertNotEqual(git(repo, "branch", "--list", "train/ws111-one"), "")
        rows = log_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "keep")
        self.assertEqual(rows[0]["reason"], "merge check did not run; kept")
        with mock.patch.object(wsgc.shutil, "which", return_value=None):
            self.assertIsNone(wsgc._gh_merged_head_shas(str(repo), "train/ws111-one"))

    def test_pr_merged_sha_logic(self):
        """pr_merged counts only merged PRs whose head contains the local tip."""
        repo = self._repo()
        git(repo, "checkout", "-b", "feature/sha")
        (repo / "feat.txt").write_text("v1\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "feat v1")
        tip = git(repo, "rev-parse", "feature/sha")
        git(repo, "checkout", "-b", "feature/sha-child")
        (repo / "child.txt").write_text("child\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "child commit")
        child = git(repo, "rev-parse", "feature/sha-child")
        git(repo, "checkout", "feature/sha")
        bindir = self.case / "fakebin-sha"
        bindir.mkdir(parents=True, exist_ok=True)
        gh = bindir / "gh"
        gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "print(json.dumps([{\"number\": 7, \"headRefOid\": os.environ.get(\"GH_HEAD_SHA\", \"\")}]))\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        real_pr = self.pr_patcher.temp_original
        self.pr_patcher.stop()
        try:
            env = dict(os.environ, PATH=str(bindir) + ":" + os.environ.get("PATH", ""))
            with mock.patch.dict(os.environ, env):
                with mock.patch.dict(os.environ, {"GH_HEAD_SHA": tip}):
                    self.assertTrue(real_pr(str(repo), "feature/sha"))
                with mock.patch.dict(os.environ, {"GH_HEAD_SHA": child}):
                    self.assertTrue(real_pr(str(repo), "feature/sha"))
                with mock.patch.dict(os.environ, {"GH_HEAD_SHA": "0" * 40}):
                    self.assertFalse(real_pr(str(repo), "feature/sha"))
        finally:
            self.pr_patcher.start()
        faildir = self.case / "fakebin-fail"
        faildir.mkdir(parents=True, exist_ok=True)
        failgh = faildir / "gh"
        failgh.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n", encoding="utf-8")
        failgh.chmod(0o755)
        self.pr_patcher.stop()
        try:
            env = dict(os.environ, PATH=str(faildir) + ":" + os.environ.get("PATH", ""))
            with mock.patch.dict(os.environ, env):
                self.assertIsNone(real_pr(str(repo), "feature/sha"))
        finally:
            self.pr_patcher.start()

    def test_content_equal_fallback_removes(self):
        """A squash merge without PR proof is removed when contents match origin/main."""
        repo = self._repo()
        ws = "WS112"
        make_bundle(ws, repo, [("T1", "train/ws112-one", [ws + ".1"])])
        path = add_lane(repo, "ws112-one", "train/ws112-one")
        commit_in(path, "lane.txt")
        self.squash_merge_into_main(repo, "train/ws112-one")
        _bundle_dir, _repos, preview = wsgc.collect(ws, fetch=False)
        self.assertEqual(len(preview), 1)
        self.assertEqual(preview[0]["action"], "remove")
        self.assertIn("branch content matches", preview[0]["reason"])
        self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertFalse(Path(path).exists())
        rows = log_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "remove")

    def test_nested_worktrees_removed(self):
        """Nested doctor worktrees under .worktrees/ are force-removed with the parent."""
        repo = self._repo()
        ws = "WS113"
        make_bundle(ws, repo, [("T1", "train/ws113-one", [ws + ".1"])])
        path = add_lane(repo, "ws113-one", "train/ws113-one")
        (Path(path) / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "train/ws113-one")
        self.assertEqual(bundles.set_task(ws, ws + ".1", "done", None, None), 0)
        nested = Path(path) / ".worktrees" / "doctor-one"
        git(repo, "worktree", "add", str(nested), "-b", "nested/doctor-one")
        self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertFalse(Path(path).exists())
        self.assertFalse(nested.exists())
        listed = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotIn(str(nested), listed.stdout)
        rows = log_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "remove")

    def test_nested_worktree_failure_keeps(self):
        """The parent is kept when a nested worktree removal fails."""
        repo = self._repo()
        ws = "WS114"
        make_bundle(ws, repo, [("T1", "train/ws114-one", [ws + ".1"])])
        path = add_lane(repo, "ws114-one", "train/ws114-one")
        (Path(path) / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "train/ws114-one")
        self.assertEqual(bundles.set_task(ws, ws + ".1", "done", None, None), 0)
        ghost = str(Path(path) / ".worktrees" / "ghost")
        with mock.patch.object(wsgc, "_nested_worktrees", return_value=[ghost]):
            self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertTrue(Path(path).exists())
        rows = log_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "keep")
        self.assertIn("nested worktree remove refused", rows[0]["reason"])
        self.assertIn("kept", rows[0]["reason"])

    def test_regenerable_allowlist_entries(self):
        """Every new allowlist entry is deleted first and the worktree removed."""
        entries = [
            ("parquet", "*.parquet\n", "api/tests/fixtures/pkmn_card_language_map.parquet"),
            ("agent-results", "data/agent-results/\n", "data/agent-results/summary.json"),
            ("sqlite", "*.sqlite\n", "data/state/session.sqlite"),
            ("worktrees-file", ".worktrees/\n", ".worktrees/scratch.txt"),
            ("venv", ".venv/\n", ".venv/bin/python"),
            ("node-modules", "node_modules/\n", "node_modules/pkg/index.js"),
            ("pytest-cache", ".pytest_cache/\n", ".pytest_cache/CACHEDIR.TAG"),
            ("pycache", "__pycache__/\n", "pkg/__pycache__/mod.pyc"),
            ("ruff", ".ruff_cache/\n", ".ruff_cache/CACHEDIR.TAG"),
            ("mypy", ".mypy_cache/\n", ".mypy_cache/3.13/base.meta.json"),
        ]
        for index, (suffix, ignore, rel) in enumerate(entries):
            with self.subTest(entry=suffix):
                repo = self._repo()
                ws = "WS%d" % (120 + index)
                slug = "ws%d-one" % (120 + index)
                branch = "train/ws%d-one" % (120 + index)
                make_bundle(ws, repo, [("T1", branch, [ws + ".1"])])
                path = add_lane(repo, slug, branch)
                (Path(path) / ".gitignore").write_text(ignore, encoding="utf-8")
                commit_in(path, "lane.txt")
                merge_into_main(repo, branch)
                self.assertEqual(bundles.set_task(ws, ws + ".1", "done", None, None), 0)
                target = Path(path) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("generated\n", encoding="utf-8")
                self.assertEqual(cmd_ws(["gc", ws]), 0)
                self.assertFalse(Path(path).exists(), "allowlist entry kept the worktree: " + suffix)
                self.assertEqual(git(repo, "branch", "--list", branch), "")
                rows = log_rows(ws)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["action"], "remove")

    def test_branch_delete_refusal_reported(self):
        """A refused ``git branch -d`` is reported on the row, never raised."""
        repo = self._repo()
        ws = "WS102"
        make_bundle(ws, repo, [("T1", "train/ws102-one", [ws + ".1"])])
        path = add_lane(repo, "ws102-one", "train/ws102-one")
        commit_in(path, "lane.txt")
        # Advance origin/main past main without moving main: the branch
        # counts as merged, but ``git branch -d`` still refuses it.
        git(repo, "checkout", "--detach")
        git(repo, "merge", "--no-ff", "train/ws102-one", "-m", "merge train/ws102-one")
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(repo, "checkout", "main")
        self.assertEqual(bundles.set_task(ws, ws + ".1", "done", None, None), 0)
        self.assertEqual(cmd_ws(["gc", ws]), 0)
        self.assertFalse(Path(path).exists())
        self.assertNotEqual(git(repo, "branch", "--list", "train/ws102-one"), "")
        rows = log_rows(ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "remove")
        self.assertIn("removed worktree", rows[0]["reason"])
        self.assertIn("refused", rows[0]["reason"])


if __name__ == "__main__":
    unittest.main()
