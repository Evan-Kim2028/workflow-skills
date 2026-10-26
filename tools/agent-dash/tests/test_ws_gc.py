"""Automatic worktree cleanup for workstream lanes: temp git repos prove every case.

Covers the six required cases on throwaway repos: a merged branch is
removed, dirty and unpushed worktrees are kept, the main checkout is
never touched, a ``ws task done`` status move triggers the gc,
``--no-gc`` skips it, and ``--dry-run`` removes nothing. Never touches
the live checkout.
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault(
    "AGENT_DASH_WORKSTREAMS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"),
)
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash import bundles, wsgc
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


def make_repo():
    """Temp repo on main with one commit and a local origin/main ref."""
    repo = Path(tempfile.mkdtemp(prefix="dash-gc-src-"))
    git(repo, "init", "-b", "main")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base commit")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


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
        self.repos: list[Path] = []
        patcher = mock.patch.object(wsgc, "pr_merged", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._cleanup)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AGENT_DASH_WORKSTREAMS", None)
        else:
            os.environ["AGENT_DASH_WORKSTREAMS"] = self.old
        self.tmp.cleanup()

    def _cleanup(self):
        for repo in self.repos:
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
            subprocess.run(["rm", "-rf", str(repo)], check=False, timeout=30)
            for child in repo.parent.glob("lor-wt-*"):
                subprocess.run(["rm", "-rf", str(child)], check=False, timeout=30)
            for child in repo.parent.glob("ws9*-plain"):
                subprocess.run(["rm", "-rf", str(child)], check=False, timeout=30)

    def _repo(self):
        repo = make_repo()
        self.repos.append(repo)
        return repo

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
        """``--dry-run`` lists the worktree, removes nothing, and logs nothing."""
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


if __name__ == "__main__":
    unittest.main()
