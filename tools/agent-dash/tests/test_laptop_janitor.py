"""laptop_janitor: sweepers, guards, scheduling and the launchd plist.

Real throwaway git repos for the worktree sweeper, fake process snapshots for
the in-use guard, temp dirs for everything else. No real Postgres, no
launchctl, no network, no gh.
"""

import fcntl
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "laptop_janitor.py"
sys.path.insert(0, str(SCRIPT.parent))

import laptop_janitor as lj  # noqa: E402

PY = sys.executable
NOW = time.time()
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Janitor Test",
    "GIT_AUTHOR_EMAIL": "janitor@example.invalid",
    "GIT_COMMITTER_NAME": "Janitor Test",
    "GIT_COMMITTER_EMAIL": "janitor@example.invalid",
    "GIT_TERMINAL_PROMPT": "0",
}


def fake_snapshot(procs=None, cwds=None, ok=True, error=""):
    return lj.Snapshot(ok=ok, procs=procs or {}, cwds=cwds or {}, error=error)


class JanitorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.state = self.base / "state"
        self.logs = self.base / "logs"
        self.now = NOW

    def tearDown(self):
        self.tmp.cleanup()

    def janitor(self, cfg=None, apply=False, snapshot=None, scheduled=False):
        full = dict(lj.DEFAULTS)
        full.update(cfg or {})
        jan = lj.Janitor(
            full,
            str(self.state),
            str(self.logs),
            apply=apply,
            snapshot=snapshot if snapshot is not None else fake_snapshot(),
            now=lambda: self.now,
        )
        jan.scheduled = scheduled
        return jan

    def mkdir(self, path, age_days=0.0):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        if age_days:
            stamp = self.now - age_days * 86400
            for item in [path, *path.rglob("*")]:
                os.utime(item, (stamp, stamp))
        return path

    def write(self, path, text="x", age_days=0.0):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if age_days:
            stamp = self.now - age_days * 86400
            os.utime(path, (stamp, stamp))
        return path


# --- tmp -------------------------------------------------------------------


class TmpSweeperTests(JanitorCase):
    def setUp(self):
        super().setUp()
        self.root = self.base / "tmp"
        self.root.mkdir()

    def janitor_for_tmp(self, apply=False, snapshot=None):
        jan = self.janitor(apply=apply, snapshot=snapshot)
        jan.tmp_roots = [str(self.root)]
        jan.claude_container = str(self.root / ("claude-%d" % os.getuid()))
        return jan

    def test_old_removed_and_new_kept(self):
        old = self.mkdir(self.root / "old", age_days=5)
        new = self.mkdir(self.root / "new")
        jan = self.janitor_for_tmp(apply=True)
        jan.sweep_tmp()
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertEqual(jan.removed, 1)
        self.assertEqual(jan.kept, 1)

    def test_dry_run_changes_nothing(self):
        old = self.mkdir(self.root / "old", age_days=5)
        jan = self.janitor_for_tmp(apply=False)
        jan.sweep_tmp()
        self.assertTrue(old.exists())
        self.assertEqual(jan.removed, 1)
        self.assertEqual(jan.actions[0]["mode"], "dry-run")

    def test_protected_name_kept(self):
        protected = self.mkdir(self.root / "com.example.helper", age_days=9)
        sock = self.write(self.root / "agent.sock", age_days=9)
        jan = self.janitor_for_tmp(apply=True)
        jan.sweep_tmp()
        self.assertTrue(protected.exists())
        self.assertTrue(sock.exists())
        reasons = jan.keep_counts["tmp"]
        self.assertIn("protected-name:com.*", reasons)
        self.assertIn("protected-name:*.sock", reasons)

    def test_other_owner_kept(self):
        owned = self.mkdir(self.root / "mine", age_days=5)
        jan = self.janitor_for_tmp(apply=True)
        jan.owner_ok = lambda st: False
        jan.sweep_tmp()
        self.assertTrue(owned.exists())
        self.assertIn("other-owner", jan.keep_counts["tmp"])

    def test_entry_containing_git_kept(self):
        repo = self.mkdir(self.root / "repo", age_days=5)
        (repo / ".git").mkdir()
        jan = self.janitor_for_tmp(apply=True)
        jan.sweep_tmp()
        self.assertTrue(repo.exists())
        self.assertIn("git-repo", jan.keep_counts["tmp"])

    def test_live_postmaster_kept(self):
        live = self.root / "pg-live"
        self.write(live / "postmaster.pid", "4242\n")
        self.mkdir(live, age_days=8)
        dead = self.root / "pg-dead"
        self.write(dead / "postmaster.pid", "4343\n")
        self.mkdir(dead, age_days=8)
        jan = self.janitor_for_tmp(apply=True)
        jan.pid_alive = lambda pid: pid == 4242
        jan.sweep_tmp()
        self.assertTrue(live.exists())
        self.assertFalse(dead.exists())
        self.assertIn("live-postgres:4242", jan.keep_counts["tmp"])

    def test_unreadable_postmaster_pid_keeps_folder(self):
        stamp = self.now - 8 * 86400
        link_dir = self.mkdir(self.root / "pg-link", age_days=8)
        target = self.write(self.base / "target.pid", "4242\n", age_days=8)
        link = link_dir / "postmaster.pid"
        os.symlink(str(target), str(link))
        os.utime(link, (stamp, stamp), follow_symlinks=False)
        os.utime(link_dir, (stamp, stamp))
        text_dir = self.mkdir(self.root / "pg-text", age_days=8)
        self.write(text_dir / "postmaster.pid", "not-a-pid\n", age_days=8)
        os.utime(text_dir, (stamp, stamp))
        empty_dir = self.mkdir(self.root / "pg-empty", age_days=8)
        self.write(empty_dir / "postmaster.pid", "", age_days=8)
        os.utime(empty_dir, (stamp, stamp))
        jan = self.janitor_for_tmp(apply=True)
        jan.pid_alive = lambda pid: False
        jan.sweep_tmp()
        for name in ("pg-link", "pg-text", "pg-empty"):
            self.assertTrue((self.root / name).exists(), name)
        self.assertIn("unreadable-postmaster-pid", jan.keep_counts["tmp"])

    def test_entry_changed_during_scan_kept(self):
        old = self.mkdir(self.root / "old", age_days=5)
        jan = self.janitor_for_tmp(apply=True)

        def touch_and_free(path, ignore_pids=None):
            os.utime(path, None)
            return False, ""

        jan.in_use = touch_and_free
        jan.sweep_tmp()
        self.assertTrue(old.exists())
        self.assertIn("changed-during-scan", jan.keep_counts["tmp"])

    def test_in_use_by_cwd_kept(self):
        busy = self.mkdir(self.root / "busy", age_days=5)
        snap = fake_snapshot(
            procs={4242: {"ppid": 1, "etime": 10, "args": "sleep 999"}},
            cwds={4242: str(busy)},
        )
        jan = self.janitor_for_tmp(apply=True, snapshot=snap)
        jan.sweep_tmp()
        self.assertTrue(busy.exists())
        self.assertTrue(any(k.startswith("in-use:cwd") for k in jan.keep_counts["tmp"]))

    def test_in_use_by_args_kept(self):
        busy = self.mkdir(self.root / "busy-args", age_days=5)
        snap = fake_snapshot(
            procs={4243: {"ppid": 1, "etime": 10, "args": "vim " + str(busy)}},
            cwds={},
        )
        jan = self.janitor_for_tmp(apply=True, snapshot=snap)
        jan.sweep_tmp()
        self.assertTrue(busy.exists())
        self.assertTrue(any(k.startswith("in-use:args") for k in jan.keep_counts["tmp"]))

    def test_snapshot_failure_keeps_everything(self):
        old = self.mkdir(self.root / "old", age_days=30)
        jan = self.janitor_for_tmp(apply=True, snapshot=fake_snapshot(ok=False, error="boom"))
        jan.sweep_tmp()
        self.assertTrue(old.exists())
        self.assertEqual(jan.removed, 0)

    def test_symlink_outside_is_unlinked_not_followed(self):
        outside = self.mkdir(self.base / "outside", age_days=10)
        keep = self.write(outside / "payload.txt", "keep me", age_days=10)
        link = self.root / "link"
        os.symlink(str(outside), str(link))
        stamp = self.now - 10 * 86400
        os.utime(link, (stamp, stamp), follow_symlinks=False)
        jan = self.janitor_for_tmp(apply=True)
        jan.sweep_tmp()
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(outside.is_dir())
        self.assertTrue(keep.exists())

    def test_claude_container_sessions(self):
        container = self.mkdir(self.root / ("claude-%d" % os.getuid()))
        project = self.mkdir(container / "-Users-someone-projects-app")
        old = self.mkdir(project / "session-old", age_days=8)
        new = self.mkdir(project / "session-new")
        jan = self.janitor_for_tmp(apply=True)
        jan.sweep_tmp()
        self.assertTrue(container.exists())
        self.assertTrue(project.exists())
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertEqual(jan.removed, 1)

    def test_session_folders_use_the_session_age(self):
        container = self.mkdir(self.root / ("claude-%d" % os.getuid()))
        project = self.mkdir(container / "-Users-someone-projects-app")
        quiet = self.mkdir(project / "session-quiet", age_days=5)
        stale = self.mkdir(project / "session-stale", age_days=8)
        jan = self.janitor_for_tmp(apply=True)
        jan.sweep_tmp()
        self.assertTrue(quiet.exists(), "a quiet session newer than 7 days is kept")
        self.assertFalse(stale.exists(), "a session older than 7 days is removed")
        self.assertIn("too-new", jan.keep_counts["tmp"])

    def test_new_file_deeper_than_three_levels_keeps_folder(self):
        old = self.mkdir(self.root / "deep", age_days=8)
        deep = old.joinpath(*["d%d" % i for i in range(6)])
        self.mkdir(deep, age_days=8)
        self.mkdir(old, age_days=8)
        # The only new thing in the tree sits at depth 6.
        self.write(deep / "payload.txt", "live")
        jan = self.janitor_for_tmp(apply=True)
        jan.sweep_tmp()
        self.assertTrue(old.exists())
        self.assertIn("too-new", jan.keep_counts["tmp"])

    def test_age_scan_truncation_keeps_folder(self):
        old = self.mkdir(self.root / "many")
        for i in range(10):
            self.write(old / ("f%02d" % i), "x")
        self.mkdir(old, age_days=8)
        jan = self.janitor_for_tmp(apply=True)
        jan.cfg["tmp_age_scan_max_entries"] = 5
        jan.sweep_tmp()
        self.assertTrue(old.exists())
        self.assertIn("age-scan-truncated", jan.keep_counts["tmp"])

    def test_live_postmaster_at_depth_five_keeps_folder(self):
        live = self.root / "pg-deep"
        pidfile = live.joinpath(*["d%d" % i for i in range(5)]) / "postmaster.pid"
        self.write(pidfile, "5252\n")
        self.mkdir(live, age_days=8)
        jan = self.janitor_for_tmp(apply=True)
        jan.pid_alive = lambda pid: pid == 5252
        jan.sweep_tmp()
        self.assertTrue(live.exists())
        self.assertIn("live-postgres:5252", jan.keep_counts["tmp"])

    def test_scheduled_keeps_per_item_lines_out_of_stdout(self):
        old = self.mkdir(self.root / "sched-old", age_days=5)
        protected = self.mkdir(self.root / "com.sched-keep", age_days=5)
        jan = self.janitor_for_tmp(apply=True)
        jan.scheduled = True
        jan.open_log()
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                jan.sweep_tmp()
                jan.flush_keeps("tmp")
        finally:
            jan.log.close()
            jan.log = None
        self.assertFalse(old.exists())
        self.assertTrue(protected.exists())
        self.assertNotIn(str(old), buf.getvalue())
        self.assertIn("kept     tmp", buf.getvalue())
        logged = [
            json.loads(line)
            for line in Path(jan.log_path).read_text(encoding="utf-8").splitlines()
            if line
        ]
        actions = [r for r in logged if r.get("type") == "action"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["target"], str(old))


# --- postgres --------------------------------------------------------------


class PostgresSweeperTests(JanitorCase):
    def setUp(self):
        super().setUp()
        self.datadir = self.mkdir(self.base / "pgdata")
        self.stopped = []

    def janitor_for_pg(self, etime=200000, datadir=None, cwd=None, apply=True, children=None):
        datadir = str(datadir if datadir is not None else self.datadir)
        procs = {100: {"ppid": 1, "etime": etime, "args": "postgres -D " + datadir}}
        for pid, args in (children or {}).items():
            procs[pid] = {"ppid": 100, "etime": 100, "args": args}
        cwds = {100: cwd} if cwd else {}
        jan = self.janitor(apply=apply, snapshot=fake_snapshot(procs=procs, cwds=cwds))
        jan.disposable_roots = [os.path.realpath(str(self.base))]
        jan.stop_postgres = lambda datadir, pid: self.stopped.append((datadir, pid)) or True
        return jan

    def test_old_idle_disposable_server_stopped(self):
        jan = self.janitor_for_pg()
        jan.sweep_postgres()
        self.assertEqual(len(self.stopped), 1)
        self.assertEqual(self.stopped[0][1], 100)
        self.assertEqual(jan.removed, 1)
        self.assertEqual(jan.actions[0]["verb"], "stop")

    def test_dry_run_does_not_stop(self):
        jan = self.janitor_for_pg(apply=False)
        jan.sweep_postgres()
        self.assertEqual(self.stopped, [])
        self.assertEqual(jan.removed, 1)

    def test_young_server_kept(self):
        jan = self.janitor_for_pg(etime=3600)
        jan.sweep_postgres()
        self.assertEqual(self.stopped, [])
        self.assertIn("too-young", jan.keep_counts["postgres"])

    def test_client_backend_kept(self):
        jan = self.janitor_for_pg(children={101: "postgres: mydb app"})
        jan.sweep_postgres()
        self.assertEqual(self.stopped, [])
        self.assertIn("client-backend", jan.keep_counts["postgres"])

    def test_unknown_child_title_kept(self):
        jan = self.janitor_for_pg(children={101: "postgres: something new"})
        jan.sweep_postgres()
        self.assertEqual(self.stopped, [])
        self.assertIn("client-backend", jan.keep_counts["postgres"])

    def test_helper_child_is_not_a_client(self):
        jan = self.janitor_for_pg(children={101: "postgres: checkpointer"})
        jan.sweep_postgres()
        self.assertEqual(len(self.stopped), 1)

    def test_numbered_helper_is_not_a_client(self):
        jan = self.janitor_for_pg(children={101: "postgres: io worker 3"})
        jan.sweep_postgres()
        self.assertEqual(len(self.stopped), 1)

    def test_client_whose_user_starts_with_a_helper_name_is_a_client(self):
        jan = self.janitor_for_pg(
            children={101: "postgres: archiver accounting 127.0.0.1(5000) idle"}
        )
        jan.sweep_postgres()
        self.assertEqual(self.stopped, [])
        self.assertIn("client-backend", jan.keep_counts["postgres"])

    def test_fallback_does_not_signal_a_reused_pid(self):
        jan = self.janitor_for_pg()
        jan.stop_postgres = jan._stop_postgres
        jan.pid_alive = lambda pid: False
        jan._run = lambda argv, **kw: (0, "python3 something\n", "")
        real_which = lj.shutil.which
        real_kill = lj.os.kill
        killed = []
        lj.shutil.which = lambda name: None
        lj.os.kill = lambda pid, sig: killed.append((pid, sig))
        try:
            ok = jan._stop_postgres(str(self.datadir), 100)
        finally:
            lj.shutil.which = real_which
            lj.os.kill = real_kill
        self.assertTrue(ok)
        self.assertEqual(killed, [])

    def test_fallback_signals_the_same_postmaster(self):
        jan = self.janitor_for_pg()
        jan.stop_postgres = jan._stop_postgres
        jan.pid_alive = lambda pid: False
        jan._run = lambda argv, **kw: (0, "postgres -D %s\n" % self.datadir, "")
        real_which = lj.shutil.which
        real_kill = lj.os.kill
        killed = []
        lj.shutil.which = lambda name: None
        lj.os.kill = lambda pid, sig: killed.append((pid, sig))
        try:
            ok = jan._stop_postgres(str(self.datadir), 100)
        finally:
            lj.shutil.which = real_which
            lj.os.kill = real_kill
        self.assertTrue(ok)
        self.assertEqual(killed, [(100, lj.signal.SIGINT)])

    def test_outside_disposable_roots_kept(self):
        outside = self.mkdir(self.base / "elsewhere" / "pgdata")
        jan = self.janitor_for_pg(datadir=outside)
        jan.disposable_roots = [os.path.realpath(str(self.base / "elsewhere" / "cards"))]
        jan.sweep_postgres()
        self.assertEqual(self.stopped, [])
        self.assertIn("outside-disposable-roots", jan.keep_counts["postgres"])

    def test_relative_datadir_resolved_from_cwd(self):
        self.mkdir(self.base / "rel-pg")
        jan = self.janitor_for_pg(datadir="rel-pg", cwd=str(self.base))
        jan.sweep_postgres()
        self.assertEqual(len(self.stopped), 1)

    def test_unresolvable_relative_datadir_kept(self):
        jan = self.janitor_for_pg(datadir="placeholder", cwd=None)
        # Replace the process args with a relative -D and no cwd.
        jan.snapshot = fake_snapshot(
            procs={100: {"ppid": 1, "etime": 200000, "args": "postgres -D rel-pg"}},
            cwds={},
        )
        jan.sweep_postgres()
        self.assertEqual(self.stopped, [])
        self.assertIn("unresolvable-data-dir", jan.keep_counts["postgres"])


# --- worktrees -------------------------------------------------------------


class GitRepoCase(JanitorCase):
    def setUp(self):
        super().setUp()
        self.origin = str(self.base / "origin.git")
        self.repo = str(self.base / "repo")
        self.git("init", "--bare", "-b", "main", self.origin, cwd=str(self.base))
        self.git("init", "-b", "main", self.repo, cwd=str(self.base))
        self.write_main("README.md", "base\n")
        self.git("add", ".", cwd=self.repo)
        self.commit(self.repo, "base", days_ago=20)
        self.git("remote", "add", "origin", self.origin, cwd=self.repo)
        self.git("push", "-q", "-u", "origin", "main", cwd=self.repo)

    def git(self, *args, cwd=None, days_ago=None, check=True):
        env = dict(os.environ)
        env.update(GIT_IDENTITY)
        if days_ago is not None:
            stamp = "%d +0000" % int(self.now - days_ago * 86400)
            env["GIT_AUTHOR_DATE"] = stamp
            env["GIT_COMMITTER_DATE"] = stamp
        argv = ["git", "-c", "commit.gpgsign=false", "-C", cwd or self.repo] + list(args)
        res = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120)
        if check and res.returncode != 0:
            self.fail("git %s failed: %s" % (" ".join(args), res.stderr))
        return res

    def commit(self, cwd, message="change", days_ago=10):
        env = dict(os.environ)
        env.update(GIT_IDENTITY)
        if days_ago is not None:
            stamp = "%d +0000" % int(self.now - days_ago * 86400)
            env["GIT_AUTHOR_DATE"] = stamp
            env["GIT_COMMITTER_DATE"] = stamp
        argv = ["git", "-c", "commit.gpgsign=false", "-C", cwd, "commit", "-q", "-m", message]
        res = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120)
        if res.returncode != 0:
            self.fail("commit failed: %s" % res.stderr)
        return res

    def write_main(self, name, text):
        path = Path(self.repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def add_worktree(self, branch, path=None, detach=False, commit=True, days_ago=10, base="main"):
        path = str(path or (self.base / ("wt-" + branch.replace("/", "-"))))
        if detach:
            self.git("worktree", "add", "--detach", path, base, cwd=self.repo)
        else:
            self.git("worktree", "add", "-b", branch, path, base, cwd=self.repo)
        if commit:
            Path(path, "work.txt").write_text(branch + "\n", encoding="utf-8")
            self.git("add", ".", cwd=path)
            self.commit(path, "work on " + branch, days_ago=days_ago)
        if not detach:
            # A real worktree has had status run on it; do the same before
            # ageing the admin files so the index is not rewritten later.
            self.git("status", "--porcelain", "--ignored", cwd=path)
            self.age_admin(path, days_ago)
        return path

    def age_admin(self, path, days_ago=10):
        stamp = self.now - days_ago * 86400
        admin = self.git("rev-parse", "--git-dir", cwd=path).stdout.strip()
        if not os.path.isabs(admin):
            admin = os.path.join(path, admin)
        # Keep the index a little newer than the tracked files so git does not
        # treat the entries as racy and rewrite the index during `status`.
        for offset, name in enumerate(("HEAD", "index", "logs/HEAD"), start=1):
            target = os.path.join(admin, name)
            if os.path.exists(target):
                os.utime(target, (stamp + offset, stamp + offset))
        for item in [Path(path), *Path(path).rglob("*")]:
            if ".git" in item.parts:
                continue
            try:
                os.utime(item, (stamp, stamp))
            except OSError:
                pass

    def merge_branch(self, branch, squash=False, days_ago=5):
        if squash:
            self.git("merge", "--squash", branch, cwd=self.repo)
            self.commit(self.repo, "squash " + branch, days_ago=days_ago)
        else:
            self.git("merge", "--ff-only", branch, cwd=self.repo)
        self.git("push", "-q", "origin", "main", cwd=self.repo)

    def janitor_for_worktrees(self, apply=True, snapshot=None, extra=None):
        cfg = {"repos": [self.repo], "worktree_manager_cmd": "janitor-test-no-such-tool"}
        cfg.update(extra or {})
        jan = self.janitor(cfg=cfg, apply=apply, snapshot=snapshot)
        # The pre-removal recheck takes a fresh snapshot; keep it fake.
        jan.snapshot_factory = lambda: fake_snapshot()
        return jan


class WorktreeSweeperTests(GitRepoCase):
    def test_squash_merged_clean_idle_worktree_removed_and_restorable(self):
        wt = self.add_worktree("feat/squashed", commit=True, days_ago=10)
        self.merge_branch("feat/squashed", squash=True)
        jan = self.janitor_for_worktrees(apply=True)
        jan.sweep_worktrees()
        self.assertFalse(os.path.exists(wt))
        lines = (self.state / "recovery.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["branch"], "feat/squashed")
        self.assertEqual(record["path"], os.path.realpath(wt))
        self.assertTrue(record["archive_ref"].startswith("refs/workspace-archive/janitor/"))
        self.git("worktree", "add", wt, "feat/squashed", cwd=self.repo)
        self.assertTrue(os.path.isdir(wt))
        self.assertTrue(Path(wt, "work.txt").exists())

    def test_ancestor_merged_worktree_removed(self):
        wt = self.add_worktree("feat/ancestor", days_ago=10)
        self.merge_branch("feat/ancestor", squash=False)
        jan = self.janitor_for_worktrees(apply=True)
        jan.sweep_worktrees()
        self.assertFalse(os.path.exists(wt))

    def test_unmerged_worktree_kept(self):
        wt = self.add_worktree("feat/unmerged", days_ago=10)
        jan = self.janitor_for_worktrees(apply=True)
        jan.pr_merged_heads = lambda repo, branch, base: []
        jan.sweep_worktrees()
        self.assertTrue(os.path.isdir(wt))
        self.assertIn("unmerged", jan.keep_counts["worktrees"])

    def test_dirty_worktree_kept(self):
        wt = self.add_worktree("feat/dirty", days_ago=10)
        self.merge_branch("feat/dirty", squash=True)
        Path(wt, "work.txt").write_text("changed\n", encoding="utf-8")
        jan = self.janitor_for_worktrees(apply=True)
        jan.sweep_worktrees()
        self.assertTrue(os.path.isdir(wt))
        self.assertIn("dirty", jan.keep_counts["worktrees"])

    def test_untracked_file_kept(self):
        wt = self.add_worktree("feat/untracked", days_ago=10)
        self.merge_branch("feat/untracked", squash=True)
        Path(wt, "notes.md").write_text("scratch\n", encoding="utf-8")
        jan = self.janitor_for_worktrees(apply=True)
        jan.sweep_worktrees()
        self.assertTrue(os.path.isdir(wt))
        self.assertIn("dirty", jan.keep_counts["worktrees"])

    def test_ignored_env_kept(self):
        wt = self.add_worktree("feat/env", days_ago=10)
        self.merge_branch("feat/env", squash=True)
        Path(wt, ".gitignore").write_text(".env\n.venv/\n", encoding="utf-8")
        self.git("add", ".", cwd=wt)
        self.commit(wt, "ignore", days_ago=10)
        self.merge_branch("feat/env", squash=True)
        Path(wt, ".env").write_text("SECRET=1\n", encoding="utf-8")
        self.age_admin(wt, 10)
        jan = self.janitor_for_worktrees(apply=True)
        jan.sweep_worktrees()
        self.assertTrue(os.path.isdir(wt))
        self.assertTrue(
            any(k.startswith("ignored-entry:") for k in jan.keep_counts["worktrees"])
        )

    def test_ignored_venv_only_is_removed(self):
        wt = self.add_worktree("feat/venv", days_ago=10)
        Path(wt, ".gitignore").write_text(".venv/\n", encoding="utf-8")
        self.git("add", ".", cwd=wt)
        self.commit(wt, "ignore", days_ago=10)
        self.merge_branch("feat/venv", squash=True)
        venv = Path(wt, ".venv", "bin")
        venv.mkdir(parents=True)
        (venv / "python").write_text("x", encoding="utf-8")
        self.age_admin(wt, 10)
        jan = self.janitor_for_worktrees(apply=True)
        jan.sweep_worktrees()
        self.assertFalse(os.path.exists(wt))

    def test_merge_state_kept(self):
        wt = self.add_worktree("feat/merging", days_ago=10)
        self.merge_branch("feat/merging", squash=True)
        admin = self.git("rev-parse", "--git-dir", cwd=wt).stdout.strip()
        if not os.path.isabs(admin):
            admin = os.path.join(wt, admin)
        Path(admin, "MERGE_HEAD").write_text("0" * 40 + "\n", encoding="utf-8")
        jan = self.janitor_for_worktrees(apply=True)
        jan.sweep_worktrees()
        self.assertTrue(os.path.isdir(wt))
        self.assertTrue(
            any(k.startswith("merge-state:") for k in jan.keep_counts["worktrees"])
        )

    def test_recent_worktree_kept(self):
        wt = self.add_worktree("feat/recent", days_ago=0)
        self.merge_branch("feat/recent", squash=True)
        jan = self.janitor_for_worktrees(apply=True)
        jan.sweep_worktrees()
        self.assertTrue(os.path.isdir(wt))
        self.assertIn("recent", jan.keep_counts["worktrees"])

    def admin_dir(self, path):
        admin = self.git("rev-parse", "--git-dir", cwd=path).stdout.strip()
        if not os.path.isabs(admin):
            admin = os.path.join(path, admin)
        return admin

    def test_index_rewrite_does_not_make_worktree_recent(self):
        wt = self.add_worktree("feat/index", days_ago=10)
        self.merge_branch("feat/index", squash=True)
        # A status scan a minute ago rewrote the index; HEAD and logs/HEAD
        # are still ten days old.
        os.utime(os.path.join(self.admin_dir(wt), "index"), None)
        jan = self.janitor_for_worktrees(apply=True)
        jan.sweep_worktrees()
        self.assertFalse(os.path.exists(wt))

    def test_recent_reflog_keeps_worktree(self):
        wt = self.add_worktree("feat/reflog", days_ago=10)
        self.merge_branch("feat/reflog", squash=True)
        os.utime(os.path.join(self.admin_dir(wt), "logs", "HEAD"), None)
        jan = self.janitor_for_worktrees(apply=True)
        jan.sweep_worktrees()
        self.assertTrue(os.path.isdir(wt))
        self.assertIn("recent", jan.keep_counts["worktrees"])

    def make_manager(self, actions):
        """A fake workspace-manager: plan prints JSON, closeout records argv."""
        bindir = self.base / "bin"
        bindir.mkdir(exist_ok=True)
        exe = bindir / "wm"
        plan = self.base / "plan.json"
        plan.write_text(json.dumps({"actions": actions}), encoding="utf-8")
        record = self.base / "closeout.jsonl"
        exe.write_text(
            "\n".join(
                [
                    "#!" + sys.executable,
                    "import json, sys",
                    "if sys.argv[1] == 'plan':",
                    "    print(json.dumps({'plan': %r}))" % str(plan),
                    "else:",
                    "    open(%r, 'a').write(json.dumps(sys.argv[1:]) + '\\n')" % str(record),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(exe, 0o755)
        return str(exe), record

    def test_part_a_uses_the_plan_path_head_and_decision_keys(self):
        exe, record = self.make_manager(
            [
                {
                    "path": "/tmp/wt-candidate",
                    "HEAD": "a" * 40,
                    "decision": "candidate",
                },
                {
                    "path": "/tmp/wt-protected",
                    "HEAD": "b" * 40,
                    "decision": "protect",
                },
            ]
        )
        jan = self.janitor(
            cfg={
                "worktree_manager_cmd": exe,
                "repos": [],
                "repo_roots": [],
            },
            apply=False,
        )
        jan.sweep_worktrees()
        calls = [
            json.loads(line)
            for line in record.read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0],
            ["closeout", "--worktree", "/tmp/wt-candidate", "--expected-head", "a" * 40],
        )
        self.assertEqual(jan.removed, 1)

    def test_part_a_does_not_treat_an_unknown_sha_key_as_the_head(self):
        exe, record = self.make_manager(
            [{"path": "/tmp/wt-sha-only", "sha": "c" * 40, "decision": "candidate"}]
        )
        jan = self.janitor(
            cfg={"worktree_manager_cmd": exe, "repos": [], "repo_roots": []},
            apply=False,
        )
        jan.sweep_worktrees()
        self.assertFalse(record.exists())
        self.assertEqual(jan.errors, 1)
        self.assertEqual(jan.removed, 0)

    def test_in_use_worktree_kept(self):
        wt = self.add_worktree("feat/busy", days_ago=10)
        self.merge_branch("feat/busy", squash=True)
        snap = fake_snapshot(
            procs={777: {"ppid": 1, "etime": 10, "args": "sleep 100"}},
            cwds={777: wt},
        )
        jan = self.janitor_for_worktrees(apply=True, snapshot=snap)
        jan.sweep_worktrees()
        self.assertTrue(os.path.isdir(wt))
        self.assertTrue(any(k.startswith("in-use:") for k in jan.keep_counts["worktrees"]))

    def test_locked_worktree_kept(self):
        wt = self.add_worktree("feat/locked", days_ago=10)
        self.merge_branch("feat/locked", squash=True)
        self.git("worktree", "lock", wt, cwd=self.repo)
        jan = self.janitor_for_worktrees(apply=True)
        jan.sweep_worktrees()
        self.assertTrue(os.path.isdir(wt))
        self.assertIn("locked", jan.keep_counts["worktrees"])

    def test_detached_worktree_kept(self):
        wt = self.add_worktree("feat/detached", detach=True, days_ago=10)
        jan = self.janitor_for_worktrees(apply=True)
        jan.sweep_worktrees()
        self.assertTrue(os.path.isdir(wt))
        self.assertIn("detached", jan.keep_counts["worktrees"])

    def test_protected_glob_kept(self):
        wt = self.add_worktree("feat/protected", days_ago=10)
        self.merge_branch("feat/protected", squash=True)
        jan = self.janitor_for_worktrees(
            apply=True,
            extra={
                "worktree_protect_globs": [
                    os.path.realpath(str(self.base / "wt-feat-protected"))
                ]
            },
        )
        jan.sweep_worktrees()
        self.assertTrue(os.path.isdir(wt))
        self.assertTrue(
            any(k.startswith("protected-path:") for k in jan.keep_counts["worktrees"])
        )

    def test_merged_pr_lookup_with_different_head_kept(self):
        wt = self.add_worktree("feat/pr-other", days_ago=10)
        jan = self.janitor_for_worktrees(apply=True)
        jan.pr_merged_heads = lambda repo, branch, base: ["0" * 40]
        jan.sweep_worktrees()
        self.assertTrue(os.path.isdir(wt))

    def test_merged_pr_lookup_with_matching_head_removed(self):
        wt = self.add_worktree("feat/pr-match", days_ago=10)
        tip = self.git("rev-parse", "HEAD", cwd=wt).stdout.strip()
        jan = self.janitor_for_worktrees(apply=True)
        jan.pr_merged_heads = lambda repo, branch, base: [tip]
        jan.sweep_worktrees()
        self.assertFalse(os.path.exists(wt))

    def test_pr_lookup_passes_the_default_branch_as_base(self):
        bindir = self.base / "bin"
        bindir.mkdir()
        record = self.base / "gh-args.json"
        gh = bindir / "gh"
        gh.write_text(
            "#!%s\nimport json, sys\nopen(%r, 'w').write(json.dumps(sys.argv[1:]))\nprint('[]')\n"
            % (sys.executable, str(record)),
            encoding="utf-8",
        )
        os.chmod(gh, 0o755)
        jan = self.janitor_for_worktrees(apply=False)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(bindir) + os.pathsep + old_path
        try:
            jan.pr_merged_heads(self.repo, "feat/base", "main")
        finally:
            os.environ["PATH"] = old_path
        args = json.loads(record.read_text(encoding="utf-8"))
        self.assertIn("--base", args)
        self.assertEqual(args[args.index("--base") + 1], "main")

    def test_head_moved_during_merge_proof_kept(self):
        wt = self.add_worktree("feat/moved", days_ago=10)
        self.merge_branch("feat/moved", squash=True)
        jan = self.janitor_for_worktrees(apply=True)

        def proof(repo, path, branch, default, tip):
            Path(path, "late.txt").write_text("late\n", encoding="utf-8")
            self.git("add", ".", cwd=path)
            self.commit(path, "late", days_ago=10)
            return "squash-merged"

        jan._merged_how = proof
        jan.sweep_worktrees()
        self.assertTrue(os.path.isdir(wt))
        self.assertIn("head-moved", jan.keep_counts["worktrees"])

    def test_in_use_at_recheck_kept(self):
        wt = self.add_worktree("feat/late-busy", days_ago=10)
        self.merge_branch("feat/late-busy", squash=True)
        jan = self.janitor_for_worktrees(apply=True)
        busy = fake_snapshot(
            procs={888: {"ppid": 1, "etime": 10, "args": "sleep 1"}},
            cwds={888: wt},
        )
        jan.snapshot_factory = lambda: busy
        jan.sweep_worktrees()
        self.assertTrue(os.path.isdir(wt))
        self.assertTrue(any(k.startswith("in-use:") for k in jan.keep_counts["worktrees"]))

    def test_fetch_failure_keeps_everything(self):
        wt = self.add_worktree("feat/fetchfail", days_ago=10)
        self.merge_branch("feat/fetchfail", squash=True)
        self.git("remote", "set-url", "origin", str(self.base / "gone.git"), cwd=self.repo)
        jan = self.janitor_for_worktrees(apply=True)
        jan.sweep_worktrees()
        self.assertTrue(os.path.isdir(wt))
        self.assertIn("fetch-failed", jan.keep_counts["worktrees"])

    def test_repo_without_origin_is_skipped(self):
        wt = self.add_worktree("feat/noorigin", days_ago=10)
        self.git("remote", "remove", "origin", cwd=self.repo)
        jan = self.janitor_for_worktrees(apply=True)
        jan.open_log()
        try:
            jan.sweep_worktrees()
        finally:
            jan.log.close()
            jan.log = None
        self.assertTrue(os.path.isdir(wt))
        self.assertEqual(jan.errors, 0)
        lines = [
            json.loads(line)
            for line in Path(jan.log_path).read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertTrue(
            any(
                r.get("type") == "skip" and r.get("reason") == "no origin remote"
                for r in lines
            )
        )

    def test_repo_discovery_one_and_two_levels(self):
        (self.base / "root").mkdir()
        one = Path(self.base / "root" / "one")
        one.mkdir()
        self.git("init", "-b", "main", str(one), cwd=str(self.base))
        two = Path(self.base / "root" / "nested" / "two")
        two.mkdir(parents=True)
        self.git("init", "-b", "main", str(two), cwd=str(self.base))
        plain = Path(self.base / "root" / "plain")
        plain.mkdir()
        jan = self.janitor(cfg={"repo_roots": [str(self.base / "root")]})
        repos = jan._worktree_repos()
        self.assertIn(os.path.realpath(str(one)), repos)
        self.assertIn(os.path.realpath(str(two)), repos)
        self.assertNotIn(os.path.realpath(str(plain)), repos)

    def test_dry_run_removes_nothing(self):
        wt = self.add_worktree("feat/dry", days_ago=10)
        self.merge_branch("feat/dry", squash=True)
        jan = self.janitor_for_worktrees(apply=False)
        jan.sweep_worktrees()
        self.assertTrue(os.path.isdir(wt))
        self.assertFalse((self.state / "recovery.jsonl").exists())
        self.assertEqual(jan.removed, 1)


# --- combined dry run ------------------------------------------------------


class CombinedDryRunTests(GitRepoCase):
    def test_dry_run_changes_nothing_in_any_sweeper(self):
        tmp_root = self.mkdir(self.base / "tmp", age_days=0)
        old = self.mkdir(tmp_root / "old", age_days=9)
        datadir = self.mkdir(self.base / "pgdata", age_days=0)
        wt = self.add_worktree("feat/combined", days_ago=10)
        self.merge_branch("feat/combined", squash=True)
        procs = {100: {"ppid": 1, "etime": 200000, "args": "postgres -D " + str(datadir)}}
        jan = self.janitor(
            cfg={
                "repos": [self.repo],
                "agent_dash_cmd": "",
                "worktree_manager_cmd": "janitor-test-no-such-tool",
            },
            apply=False,
            snapshot=fake_snapshot(procs=procs),
        )
        jan.tmp_roots = [str(tmp_root)]
        jan.claude_container = str(tmp_root / ("claude-%d" % os.getuid()))
        jan.disposable_roots = [os.path.realpath(str(self.base))]
        jan.stop_postgres = lambda datadir, pid: self.fail("dry run stopped a server")
        rc = jan.run()
        self.assertEqual(rc, 0)
        self.assertTrue(old.exists())
        self.assertTrue(datadir.exists())
        self.assertTrue(os.path.isdir(wt))
        self.assertGreaterEqual(jan.removed, 3)
        self.assertFalse((self.state / "last-run.json").exists())
        summary = json.loads(
            [line for line in Path(jan.log_path).read_text(encoding="utf-8").splitlines() if line][-1]
        )
        self.assertEqual(summary["type"], "summary")
        self.assertEqual(summary["mode"], "dry-run")


# --- snapshot and path forms ----------------------------------------------


class SnapshotTests(JanitorCase):
    def test_args_short_form_of_long_path_is_in_use(self):
        snap = fake_snapshot(
            procs={51: {"ppid": 1, "etime": 10, "args": "app -D /var/folders/a/T/x/data"}},
            cwds={},
        )
        used, why = lj.path_in_use(snap, "/private/var/folders/a/T/x")
        self.assertTrue(used)
        self.assertTrue(why.startswith("args"))

    def test_args_long_form_of_short_path_is_in_use(self):
        snap = fake_snapshot(
            procs={52: {"ppid": 1, "etime": 10, "args": "app -D /private/tmp/x/data"}},
            cwds={},
        )
        used, why = lj.path_in_use(snap, "/tmp/x")
        self.assertTrue(used)
        self.assertTrue(why.startswith("args"))

    def test_cwd_short_form_of_long_path_is_in_use(self):
        snap = fake_snapshot(
            procs={53: {"ppid": 1, "etime": 10, "args": "sleep 1"}},
            cwds={53: "/var/folders/a/T/x"},
        )
        used, why = lj.path_in_use(snap, "/private/var/folders/a/T/x")
        self.assertTrue(used)
        self.assertTrue(why.startswith("cwd"))

    def test_cwd_long_form_of_short_path_is_in_use(self):
        snap = fake_snapshot(
            procs={54: {"ppid": 1, "etime": 10, "args": "sleep 1"}},
            cwds={54: "/private/var/folders/a/T/x"},
        )
        used, why = lj.path_in_use(snap, "/var/folders/a/T/x")
        self.assertTrue(used)
        self.assertTrue(why.startswith("cwd"))

    def test_ignored_pids_use_the_per_process_loop(self):
        snap = fake_snapshot(
            procs={
                61: {"ppid": 1, "etime": 10, "args": "postgres -D /tmp/x"},
                62: {"ppid": 1, "etime": 10, "args": "postgres -D /tmp/x"},
            },
            cwds={},
        )
        used, why = lj.path_in_use(snap, "/tmp/x", ignore_pids=[61])
        self.assertTrue(used)
        self.assertEqual(why, "args-of-62")
        unused, _ = lj.path_in_use(snap, "/tmp/x", ignore_pids=[61, 62])
        self.assertFalse(unused)

    def test_stale_own_snapshot_is_refreshed(self):
        taken = []

        def factory():
            taken.append(self.now)
            return fake_snapshot(ok=True)

        jan = lj.Janitor(
            dict(lj.DEFAULTS),
            str(self.state),
            str(self.logs),
            now=lambda: self.now,
            snapshot_factory=factory,
        )
        self.assertTrue(jan.current_snapshot().ok)
        self.assertEqual(len(taken), 1)
        self.now += 60
        jan.current_snapshot()
        self.assertEqual(len(taken), 1, "a 60 s old snapshot is still fresh")
        self.now += 120
        jan.current_snapshot()
        self.assertEqual(len(taken), 2, "a snapshot past 120 s is replaced")

    def test_injected_snapshot_is_never_replaced(self):
        snap = fake_snapshot()
        jan = self.janitor(snapshot=snap)
        jan.snapshot_factory = lambda: self.fail("injected snapshot was replaced")
        self.now += 100000
        self.assertIs(jan.current_snapshot(), snap)

    def test_failed_refresh_counts_everything_as_in_use(self):
        jan = lj.Janitor(
            dict(lj.DEFAULTS),
            str(self.state),
            str(self.logs),
            now=lambda: self.now,
            snapshot_factory=lambda: fake_snapshot(ok=False, error="boom"),
        )
        used, why = jan.in_use(str(self.base / "x"))
        self.assertTrue(used)
        self.assertEqual(why, "snapshot-failed")


class TakeSnapshotTests(unittest.TestCase):
    def run_with(self, ps_out, lsof_out, lsof_rc):
        real = lj.subprocess.run

        def fake(argv, **kw):
            if argv[0] == "ps":
                return subprocess.CompletedProcess(argv, 0, ps_out, "")
            return subprocess.CompletedProcess(argv, lsof_rc, lsof_out, "")

        lj.subprocess.run = fake
        try:
            return lj.take_snapshot()
        finally:
            lj.subprocess.run = real

    def test_snapshot_without_the_janitors_own_cwd_is_failed(self):
        # A nonzero lsof exit with empty output must not look like a clean scan.
        snap = self.run_with("  1   0 00:01 init\n", "", 1)
        self.assertFalse(snap.ok)

    def test_snapshot_ok_when_own_cwd_listed_despite_nonzero_exit(self):
        own = os.getpid()
        snap = self.run_with("  1   0 00:01 init\n", "p%d\nn/tmp\n" % own, 1)
        self.assertTrue(snap.ok)
        self.assertEqual(snap.cwds[own], "/tmp")


# --- scheduling, lock, config, plist --------------------------------------


class CliCase(JanitorCase):
    def env(self, config=None):
        cfg = self.base / "config.json"
        if config is not None:
            cfg.write_text(config, encoding="utf-8")
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.base / "home"),
            "LAPTOP_JANITOR_STATE": str(self.state),
            "LAPTOP_JANITOR_LOGS": str(self.logs),
            "LAPTOP_JANITOR_CONFIG": str(cfg),
        }

    def cli(self, *args, env=None):
        return subprocess.run(
            [PY, str(SCRIPT)] + list(args),
            capture_output=True,
            text=True,
            env=env or self.env(),
            timeout=120,
        )

    def write_last_run(self, days_ago):
        self.state.mkdir(parents=True, exist_ok=True)
        ended = self.now - days_ago * 86400
        (self.state / "last-run.json").write_text(
            json.dumps({"ended_at": ended, "end": lj.iso(ended)}),
            encoding="utf-8",
        )

    def log_files(self):
        if not self.logs.exists():
            return []
        return sorted(self.logs.glob("run-*.jsonl"))

    def test_scheduled_not_due_exits_without_sweeping(self):
        self.write_last_run(days_ago=1)
        res = self.cli("run", "--scheduled", "--skip", "postgres,tmp,worktrees,scratch")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("not due", res.stdout)
        self.assertEqual(self.log_files(), [])
        self.assertNotIn("mode=", res.stdout)

    def test_scheduled_due_runs_and_records_last_run(self):
        self.write_last_run(days_ago=10)
        cfg = json.dumps(
            {
                "repos": [],
                "repo_roots": [],
                "worktree_manager_cmd": "janitor-test-no-such-tool",
            }
        )
        res = self.cli("run", "--scheduled", "--only", "worktrees", env=self.env(cfg))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("JANITOR: mode=apply", res.stdout)
        data = json.loads((self.state / "last-run.json").read_text(encoding="utf-8"))
        self.assertGreater(data["sweepers"]["worktrees"]["ended_at"], self.now - 60)
        self.assertEqual(len(self.log_files()), 1)

    def test_held_lock_exits_zero(self):
        self.state.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.state / "lock"), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            res = self.cli("run", "--apply", "--skip", "postgres,tmp,worktrees,scratch")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("already running", res.stdout)
        self.assertEqual(self.log_files(), [])

    def test_invalid_config_exits_one(self):
        res = self.cli("run", "--skip", "postgres,tmp,worktrees,scratch", env=self.env("{not json"))
        self.assertEqual(res.returncode, 1)
        self.assertIn("config error", res.stderr)

    def test_run_that_aborts_midway_leaves_last_run_unchanged(self):
        self.state.mkdir(parents=True, exist_ok=True)
        marker = self.state / "last-run.json"
        marker.write_text('{"ended_at": 1}', encoding="utf-8")
        jan = self.janitor(apply=True)

        class Boom(BaseException):
            pass

        def explode():
            raise Boom("midway")

        rc = jan.run(only=["tmp"], sweepers={"tmp": explode})
        self.assertEqual(rc, 1)
        self.assertEqual(marker.read_text(encoding="utf-8"), '{"ended_at": 1}')

    def test_print_plist_parses(self):
        env = self.env()
        res = self.cli("print-plist", env=env)
        self.assertEqual(res.returncode, 0, res.stderr)
        data = plistlib.loads(res.stdout.encode("utf-8"))
        self.assertEqual(data["Label"], "com.agent-dash.laptop-janitor")
        self.assertTrue(data["RunAtLoad"])
        self.assertEqual(data["StartCalendarInterval"]["Hour"], 4)
        self.assertEqual(data["StartCalendarInterval"]["Minute"], 40)
        args = data["ProgramArguments"]
        self.assertEqual(args[0], "/usr/bin/python3")
        self.assertEqual(
            args[1],
            os.path.join(
                env["HOME"], ".local", "share", "laptop-janitor", "laptop_janitor.py"
            ),
        )
        self.assertEqual(args[2:], ["run", "--scheduled"])
        self.assertEqual(res.stdout.count("com.agent-dash.laptop-janitor"), 1)
        self.assertIn(
            "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            data["EnvironmentVariables"]["PATH"],
        )
        self.assertEqual(data["StandardOutPath"], data["StandardErrorPath"])
        self.assertTrue(data["StandardOutPath"].endswith("launchd.log"))

    def test_future_ended_at_is_due(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "last-run.json").write_text(
            json.dumps({"ended_at": self.now + 3600}), encoding="utf-8"
        )
        jan = self.janitor()
        self.assertTrue(jan.due())

    def test_non_finite_ended_at_is_due(self):
        self.state.mkdir(parents=True, exist_ok=True)
        for value in (float("nan"), float("inf")):
            (self.state / "last-run.json").write_text(
                json.dumps({"ended_at": value}), encoding="utf-8"
            )
            jan = self.janitor()
            self.assertTrue(jan.due(), value)

    def test_only_one_sweeper_marks_only_that_sweeper(self):
        jan = self.janitor(apply=True)
        jan.run(only=["tmp"], sweepers={"tmp": lambda: None})
        data = json.loads((self.state / "last-run.json").read_text(encoding="utf-8"))
        self.assertIn("tmp", data["sweepers"])
        self.assertNotIn("postgres", data["sweepers"])

    def test_raising_sweeper_is_not_recorded(self):
        jan = self.janitor(apply=True)

        def boom():
            raise RuntimeError("x")

        jan.run(only=["tmp"], sweepers={"tmp": boom})
        self.assertFalse((self.state / "last-run.json").exists())

    def test_timed_out_sweeper_is_not_recorded(self):
        jan = self.janitor(apply=True)

        def slow():
            jan._timed_out("tmp")

        jan.run(only=["tmp"], sweepers={"tmp": slow})
        self.assertFalse((self.state / "last-run.json").exists())

    def test_dry_run_writes_no_sweeper_entry(self):
        jan = self.janitor(apply=False)
        jan.run(only=["tmp"], sweepers={"tmp": lambda: None})
        self.assertFalse((self.state / "last-run.json").exists())

    def test_scheduled_runs_only_due_sweepers(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "last-run.json").write_text(
            json.dumps(
                {
                    "sweepers": {
                        "postgres": {"ended_at": self.now - 1 * 86400},
                        "tmp": {"ended_at": self.now - 10 * 86400},
                    }
                }
            ),
            encoding="utf-8",
        )
        jan = self.janitor()
        self.assertEqual(jan.due_sweepers(), ["tmp", "worktrees", "scratch"])

    def test_old_format_means_every_sweeper_ran_then(self):
        self.write_last_run(days_ago=1)
        jan = self.janitor()
        self.assertEqual(jan.due_sweepers(), [])

    def test_status_shows_each_sweeper(self):
        self.write_last_run(days_ago=1)
        res = self.cli("status", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertIn("sweeper_state", data)
        self.assertFalse(data["sweeper_state"]["tmp"]["due"])
        self.assertIn("last_run", data["sweeper_state"]["tmp"])

    def test_status_json(self):
        self.write_last_run(days_ago=1)
        res = self.cli("status", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertFalse(data["due"])
        self.assertEqual(data["sweepers"], lj.SWEEPERS)
        self.assertIn("lock_held", data)


# --- small parsing helpers -------------------------------------------------


class HelperTests(unittest.TestCase):
    def test_parse_etime(self):
        self.assertEqual(lj.parse_etime("05:00"), 300)
        self.assertEqual(lj.parse_etime("01:05:00"), 3900)
        self.assertEqual(lj.parse_etime("2-01:00:00"), 2 * 86400 + 3600)

    def test_parse_ps_and_lsof(self):
        procs = lj.parse_ps("  101   1 01:02:03 postgres -D /x\n")
        self.assertEqual(procs[101]["ppid"], 1)
        self.assertEqual(procs[101]["args"], "postgres -D /x")
        self.assertEqual(lj.parse_lsof_cwds("p101\nn/tmp/x\np102\nn/y\n"), {101: "/tmp/x", 102: "/y"})

    def test_config_merges_defaults_and_rejects_unknown(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(tmp.name, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"interval_days": 7}, fh)
            cfg = lj.load_config(path)
            self.assertEqual(cfg["interval_days"], 7)
            self.assertEqual(cfg["tmp_min_age_days"], 2)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"nope": 1}, fh)
            with self.assertRaises(lj.ConfigError):
                lj.load_config(path)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
