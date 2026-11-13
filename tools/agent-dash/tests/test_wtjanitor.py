"""Worktree disk janitor: temp repos prove reaps, artifact deletes and every guard.

Each test builds throwaway git repos under a temp dir (never the live
checkout) and asserts what the janitor frees: merged lanes are reaped,
stale unmerged lanes lose only their regenerable folders, recent or busy
worktrees and git-tracked artifact names are kept, disk pressure lowers
the artifact age, and a dry-run deletes nothing.
"""
import contextlib
import io
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dash import worktrees, wtjanitor  # noqa: E402
import agent_dash  # noqa: E402
from test_worktrees import (  # noqa: E402
    add_worktree,
    commit_in,
    git,
    make_repo,
    merge_into_main,
)


def make_old_tree(path, hours=30.0):
    """Backdate every file and folder under ``path`` except .git."""
    stamp = time.time() - hours * 3600.0
    for root, dirs, files in os.walk(str(path)):
        if ".git" in dirs:
            dirs.remove(".git")
        for name in list(dirs) + list(files):
            target = os.path.join(root, name)
            if os.path.islink(target):
                continue
            try:
                os.utime(target, (stamp, stamp))
            except OSError:
                pass
    os.utime(str(path), (stamp, stamp))


def add_venv(path, name=".venv", size=8192):
    """Create a fake dependency checkout at ``path/name`` with one blob."""
    target = Path(path) / name
    (target / "lib").mkdir(parents=True, exist_ok=True)
    (target / "lib" / "blob.bin").write_bytes(b"x" * size)
    return target


def run_janitor(repo, *extra):
    """Run the janitor in-process. Returns exit code and captured stdout."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = wtjanitor.cmd_janitor(["--repo", str(repo), *extra])
    return code, out.getvalue()


def answer_field(text, key):
    """Value of one ``key=`` field on the ANSWER line."""
    for line in text.splitlines():
        if line.startswith("ANSWER:"):
            for token in line.split():
                if token.startswith(key + "="):
                    return token.split("=", 1)[1]
    return None


class JanitorTest(unittest.TestCase):
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
            subprocess.run(["rm", "-rf", str(repo)], check=False, timeout=60)
            sibling = repo.parent / (repo.name + "-wt")
            subprocess.run(["rm", "-rf", str(sibling)], check=False, timeout=60)

    def _repo(self):
        repo = make_repo()
        self.repos.append(repo)
        return repo

    def test_merged_old_worktree_is_reaped(self):
        """A merged, clean, old worktree goes with its branch."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-merged")
        commit_in(path, "lane.txt")
        merge_into_main(repo, "lane-merged")
        make_old_tree(path, 5)
        code, text = run_janitor(repo, "--apply")
        self.assertEqual(code, 0)
        self.assertFalse(Path(path).exists())
        self.assertEqual(git(repo, "branch", "--list", "lane-merged"), "")
        self.assertIn("reaped=", text)
        self.assertEqual(answer_field(text, "reaped"), "1")

    def test_unmerged_old_keeps_code_and_loses_venv(self):
        """An old unmerged lane keeps its commits and branch but loses .venv."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-open")
        commit_in(path, "lane.txt")
        venv = add_venv(path)
        make_old_tree(path, 30)
        code, text = run_janitor(repo, "--apply")
        self.assertEqual(code, 0)
        self.assertTrue(Path(path).exists())
        self.assertTrue((Path(path) / "lane.txt").exists())
        self.assertFalse(venv.exists())
        self.assertIn("deleted " + os.path.realpath(str(venv)), text)
        self.assertEqual(answer_field(text, "artifacts"), "1")
        self.assertNotEqual(git(repo, "branch", "--list", "lane-open"), "")
        self.assertNotEqual(answer_field(text, "freed_bytes"), "0")

    def test_recent_worktree_is_untouched(self):
        """A worktree with fresh source files keeps its artifacts."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-recent")
        venv = add_venv(path)
        code, text = run_janitor(repo, "--apply")
        self.assertEqual(code, 0)
        self.assertTrue(venv.exists())
        self.assertEqual(answer_field(text, "artifacts"), "0")

    def test_tracked_artifact_name_is_kept(self):
        """A .venv holding tracked files is never deleted."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-tracked")
        venv = Path(path) / ".venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("tracked\n", encoding="utf-8")
        git(path, "add", ".venv/pyvenv.cfg")
        git(path, "commit", "-m", "track a venv file")
        make_old_tree(path, 30)
        code, text = run_janitor(repo, "--apply")
        self.assertEqual(code, 0)
        self.assertTrue((venv / "pyvenv.cfg").exists())
        self.assertEqual(answer_field(text, "artifacts"), "0")

    def test_dist_is_deleted_only_when_gitignored(self):
        """dist/build count as artifacts only once git ignores them."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-dist")
        dist = Path(path) / "dist"
        dist.mkdir()
        (dist / "app.js").write_text("built\n", encoding="utf-8")
        make_old_tree(path, 30)
        code, text = run_janitor(repo, "--apply")
        self.assertEqual(code, 0)
        self.assertTrue(dist.exists())
        (Path(path) / ".gitignore").write_text("dist/\n", encoding="utf-8")
        make_old_tree(path, 30)
        code, text = run_janitor(repo, "--apply")
        self.assertEqual(code, 0)
        self.assertFalse(dist.exists())
        self.assertEqual(answer_field(text, "artifacts"), "1")

    def test_pressure_mode_lowers_the_artifact_age(self):
        """Below the free-space threshold an 8h-old lane loses its venv."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-pressure")
        venv = add_venv(path)
        make_old_tree(path, 8)
        with mock.patch.object(wtjanitor, "disk_free_pct", return_value=50.0):
            code, text = run_janitor(repo, "--apply")
            self.assertEqual(code, 0)
            self.assertTrue(venv.exists())
            self.assertEqual(answer_field(text, "pressure"), "0")
            with mock.patch.object(wtjanitor, "disk_free_pct", return_value=5.0):
                code, text = run_janitor(repo, "--apply", "--pressure-pct", "12")
        self.assertEqual(code, 0)
        self.assertFalse(venv.exists())
        self.assertIn("disk pressure", text)
        self.assertEqual(answer_field(text, "pressure"), "1")
        self.assertEqual(answer_field(text, "artifact_age"), str(int(wtjanitor.DEFAULT_PRESSURE_AGE_S)) + "s")

    def test_dry_run_deletes_nothing_and_reports_projection(self):
        """Without --apply the venv stays and the ANSWER line projects the bytes."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-dry")
        commit_in(path, "lane.txt")
        venv = add_venv(path)
        make_old_tree(path, 30)
        code, text = run_janitor(repo)
        self.assertEqual(code, 0)
        self.assertTrue(venv.exists())
        self.assertIn("would delete " + os.path.realpath(str(venv)), text)
        self.assertEqual(answer_field(text, "apply"), "0")
        self.assertEqual(answer_field(text, "artifacts"), "1")
        self.assertGreater(int(answer_field(text, "freed_bytes")), 0)

    def test_busy_worktree_keeps_its_artifacts(self):
        """A live process inside the worktree blocks the artifact delete."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-busy")
        commit_in(path, "lane.txt")
        venv = add_venv(path)
        make_old_tree(path, 30)
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=str(path),
        )
        self.procs.append(proc)
        time.sleep(1.0)
        self.assertIsNone(proc.poll())
        code, text = run_janitor(repo, "--apply")
        self.assertEqual(code, 0)
        self.assertTrue(venv.exists())
        self.assertEqual(answer_field(text, "artifacts"), "0")

    def test_branch_skip_protects_the_artifacts_too(self):
        """A branch-only --skip keeps the worktree and its .venv."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-skip")
        commit_in(path, "lane.txt")
        venv = add_venv(path)
        make_old_tree(path, 30)
        code, text = run_janitor(repo, "--apply", "--skip", "lane-skip")
        self.assertEqual(code, 0)
        self.assertTrue(Path(path).exists())
        self.assertTrue(venv.exists())
        self.assertEqual(answer_field(text, "artifacts"), "0")

    def test_activity_during_measurement_prevents_deletion(self):
        """A process that starts after the busy scan keeps the artifacts."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-race")
        commit_in(path, "lane.txt")
        venv = add_venv(path)
        make_old_tree(path, 30)
        real_measure = wtjanitor.measure_rows

        def measuring(rows, workers=4):
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=str(path),
            )
            self.procs.append(proc)
            time.sleep(1.0)
            self.assertIsNone(proc.poll())
            real_measure(rows, workers)

        with mock.patch.object(wtjanitor, "measure_rows", side_effect=measuring):
            code, text = run_janitor(repo, "--apply")
        self.assertEqual(code, 0)
        self.assertTrue(venv.exists())
        self.assertEqual(answer_field(text, "artifacts"), "0")
        self.assertIn("became busy", text)

    def test_source_scan_error_reads_as_unknown(self):
        """An unreadable subdirectory makes source_mtime unknown, so nothing goes."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-perm")
        commit_in(path, "lane.txt")
        sub = Path(path) / "sub"
        sub.mkdir()
        (sub / "file.txt").write_text("x", encoding="utf-8")
        venv = add_venv(path)
        make_old_tree(path, 30)
        real_scandir = os.scandir

        def fake_scandir(target):
            if os.path.realpath(str(target)) == os.path.realpath(str(sub)):
                raise PermissionError(13, "permission denied", str(target))
            return real_scandir(target)

        with mock.patch.object(wtjanitor.os, "scandir", side_effect=fake_scandir):
            self.assertIsNone(wtjanitor.source_mtime(path, time.monotonic() + 30.0))
            rows, note = wtjanitor.plan_artifacts(
                str(path), wtjanitor.DEFAULT_ARTIFACT_AGE_S, time.time(),
                time.monotonic() + 30.0, set(), "lane-perm",
            )
        self.assertEqual(rows, [])
        self.assertEqual(note, "source scan did not finish")
        self.assertTrue(venv.exists())

    def test_source_scan_deadline_overrun_reads_as_unknown(self):
        """An expired deadline keeps the worktree instead of reporting an old mtime."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-deadline")
        commit_in(path, "lane.txt")
        self.assertIsNone(wtjanitor.source_mtime(path, time.monotonic() - 1.0))

    def test_apply_cooldown_downgrades_a_second_apply_run(self):
        """--apply-cooldown runs dry after a recent apply and deletes after zero."""
        repo = self._repo()
        path = add_worktree(repo, "wt", "lane-cool")
        commit_in(path, "lane.txt")
        add_venv(path)
        make_old_tree(path, 30)
        state = Path(tempfile.mkdtemp(prefix="dash-janitor-cool-")) / "janitor_state.json"
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(state.parent)], check=False, timeout=60))
        code, text = run_janitor(repo, "--apply", "--state-file", str(state), "--apply-cooldown", "1h")
        self.assertEqual(code, 0)
        self.assertEqual(answer_field(text, "apply"), "1")
        venv = add_venv(path)
        make_old_tree(path, 30)
        code, text = run_janitor(repo, "--apply", "--state-file", str(state), "--apply-cooldown", "1h")
        self.assertEqual(code, 0)
        self.assertIn("apply cooldown", text)
        self.assertEqual(answer_field(text, "apply"), "0")
        self.assertTrue(venv.exists())
        code, text = run_janitor(repo, "--apply", "--state-file", str(state), "--apply-cooldown", "0s")
        self.assertEqual(code, 0)
        self.assertEqual(answer_field(text, "apply"), "1")
        self.assertFalse(venv.exists())

    def test_usage_and_wiring(self):
        """The janitor is reachable through agent_dash and refuses bad flags."""
        repo = self._repo()
        self.assertEqual(wtjanitor.cmd_janitor([]), 2)
        self.assertEqual(wtjanitor.cmd_janitor(["--repo", str(repo), "--artifact-age", "soon"]), 2)
        self.assertEqual(wtjanitor.cmd_janitor(["--repo", str(repo), "--pressure-pct", "101"]), 2)
        self.assertEqual(wtjanitor.cmd_janitor(["--repo", str(repo / "missing")]), 3)
        self.assertEqual(
            agent_dash.main(["worktrees", "janitor", "--repo", str(repo)]),
            0,
        )


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp(prefix="dash-janitor-state-")) / "janitor_state.json"
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.state.parent)], check=False, timeout=60))
        wtjanitor._AGENTQ_GC_SUPPORTED.clear()
        self.addCleanup(wtjanitor._AGENTQ_GC_SUPPORTED.clear)

    def _fake(self, gc_help):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            if list(args[:2]) == ["agentq", "gc"] and "--help" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout=gc_help, stderr="")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="cleaned", stderr="")

        return calls, fake_run

    def test_daily_maintenance_runs_once_per_day(self):
        """uv and agentq run at most once per 24h, tracked in the state file."""
        calls, fake_run = self._fake("usage: agentq gc [--runs-older-than DUR]\n")
        base = time.time()
        with mock.patch.object(wtjanitor.shutil, "which", side_effect=lambda name: "/usr/bin/" + name), \
                mock.patch.object(wtjanitor.subprocess, "run", side_effect=fake_run):
            first = wtjanitor.run_maintenance(self.state, True, now=base)
            second = wtjanitor.run_maintenance(self.state, True, now=base + 3600.0)
            third = wtjanitor.run_maintenance(self.state, True, now=base + 25 * 3600.0)
        self.assertEqual([row["decision"] for row in first], ["run", "run"])
        self.assertEqual([row["decision"] for row in second], ["skip", "skip"])
        self.assertEqual([row["decision"] for row in third], ["run", "run"])
        uv_calls = [call for call in calls if call == ["uv", "cache", "prune"]]
        gc_calls = [call for call in calls if call == ["agentq", *wtjanitor.AGENTQ_GC_ARGS]]
        self.assertEqual(len(uv_calls), 2)
        self.assertEqual(len(gc_calls), 2)
        help_calls = [call for call in calls if call[:3] == ["agentq", "gc", "--help"]]
        self.assertEqual(len(help_calls), 1)

    def test_dry_run_plans_without_running(self):
        """Without --apply nothing runs and nothing is recorded."""
        calls, fake_run = self._fake("usage: agentq gc [--runs-older-than DUR]\n")
        with mock.patch.object(wtjanitor.shutil, "which", side_effect=lambda name: "/usr/bin/" + name), \
                mock.patch.object(wtjanitor.subprocess, "run", side_effect=fake_run):
            rows = wtjanitor.run_maintenance(self.state, False, now=time.time())
        self.assertEqual([row["decision"] for row in rows], ["run", "run"])
        self.assertIn("would run", rows[0]["reason"])
        self.assertEqual([call for call in calls if call[0] != "agentq" or "--help" not in call], [])
        self.assertFalse(self.state.exists())

    def test_agentq_gc_is_skipped_without_the_flag(self):
        """An older agentq reads as no capability, and no gc is attempted."""
        calls, fake_run = self._fake("usage: agentq gc [--dry-run]\n")
        with mock.patch.object(wtjanitor.shutil, "which", side_effect=lambda name: "/usr/bin/" + name), \
                mock.patch.object(wtjanitor.subprocess, "run", side_effect=fake_run):
            rows = wtjanitor.run_maintenance(self.state, True, now=time.time())
        decisions = {row["action"]: row["decision"] for row in rows}
        self.assertEqual(decisions["uv_cache_prune"], "run")
        self.assertEqual(decisions["agentq_gc"], "skip")
        gc_row = [row for row in rows if row["action"] == "agentq_gc"][0]
        self.assertIn("no --runs-older-than", gc_row["reason"])
        self.assertEqual([call for call in calls if call == ["agentq", *wtjanitor.AGENTQ_GC_ARGS]], [])


if __name__ == "__main__":
    unittest.main()
