"""Tests for the `cg` worktree-aware codegraph shim (shims/cg).

Builds a throwaway git repo with a real-looking .codegraph index and a
linked worktree, then points CG_REAL_CODEGRAPH at a fake binary that
records argv, cwd and stdin. No real codegraph call, no dashboard state.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "shims" / "cg"
INSTALLER = ROOT / "scripts" / "install_hooks.sh"

FAKE = """#!/usr/bin/env python3
import json, os, sys
record = os.environ["CG_FAKE_RECORD"]
entry = {
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "stdin": sys.stdin.read(),
}
with open(record, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(entry) + "\\n")
sys.exit(int(os.environ.get("CG_FAKE_EXIT", "0")))
"""


class CgShimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        # Spaces exercise quoting in every path the shim passes around.
        self.main = base / "main repo"
        self.main.mkdir()
        subprocess.run(
            ["git", "init"], cwd=self.main, check=True, capture_output=True, timeout=30
        )
        (self.main / "file.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "file.py"], cwd=self.main, check=True, capture_output=True, timeout=30
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=cg test",
                "-c",
                "user.email=cg@example.invalid",
                "commit",
                "-m",
                "seed",
            ],
            cwd=self.main,
            check=True,
            capture_output=True,
            timeout=30,
        )
        # A real index: codegraph.db, not just the .gitignore a failed
        # `codegraph init` leaves in a worktree.
        (self.main / ".codegraph").mkdir()
        (self.main / ".codegraph" / "codegraph.db").write_bytes(b"fake db")
        (self.main / ".codegraph" / ".gitignore").write_text("*.db\n", encoding="utf-8")

        self.worktree = base / "linked worktree"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(self.worktree), "HEAD"],
            cwd=self.main,
            check=True,
            capture_output=True,
            timeout=30,
        )
        # A worktree often carries a bare .codegraph/ from a failed init.
        # It is not an index and must not shadow the main checkout.
        (self.worktree / ".codegraph").mkdir()
        (self.worktree / ".codegraph" / ".gitignore").write_text("*.db\n", encoding="utf-8")
        self.worktree_sub = self.worktree / "sub dir"
        self.worktree_sub.mkdir()

        # A second worktree nested under its indexed main checkout. The old
        # ancestor search found the main index from here and skipped the
        # write refusal.
        self.nested = self.main / "nested worktree"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(self.nested), "HEAD"],
            cwd=self.main,
            check=True,
            capture_output=True,
            timeout=30,
        )

        # A worktree under an unrelated indexed repository. The ancestor
        # search must not pick that repository's index.
        self.outer = base / "outer repo"
        self.outer.mkdir()
        subprocess.run(
            ["git", "init"], cwd=self.outer, check=True, capture_output=True, timeout=30
        )
        (self.outer / ".codegraph").mkdir()
        (self.outer / ".codegraph" / "codegraph.db").write_bytes(b"outer db")
        self.outer_wt = self.outer / "linked worktree"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(self.outer_wt), "HEAD"],
            cwd=self.main,
            check=True,
            capture_output=True,
            timeout=30,
        )

        # A symlinked spelling of the linked worktree.
        self.worktree_link = base / "worktree link"
        self.worktree_link.symlink_to(self.worktree, target_is_directory=True)

        # A directory whose only codegraph file is a WAL sidecar.
        self.wal_only = base / "wal only"
        (self.wal_only / ".codegraph").mkdir(parents=True)
        (self.wal_only / ".codegraph" / "codegraph.db-wal").write_bytes(b"wal")

        self.no_index = base / "no index repo"
        self.no_index.mkdir()
        subprocess.run(
            ["git", "init"], cwd=self.no_index, check=True, capture_output=True, timeout=30
        )

        self.non_git = base / "plain dir"
        self.non_git.mkdir()

        self.fake = base / "fake codegraph"
        self.fake.write_text(FAKE, encoding="utf-8")
        self.fake.chmod(0o755)
        self.record = base / "record.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cg(self, *args, cwd=None, stdin="", env=None):
        base_env = {
            **os.environ,
            "CG_REAL_CODEGRAPH": str(self.fake),
            "CG_FAKE_RECORD": str(self.record),
        }
        if env:
            base_env.update(env)
        return subprocess.run(
            [sys.executable, str(SHIM), *args],
            cwd=str(cwd or self.main),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            env=base_env,
        )

    def calls(self):
        if not self.record.exists():
            return []
        return [
            json.loads(line)
            for line in self.record.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_main_checkout_passes_through_unchanged(self):
        proc = self.run_cg("explore", "lor_merge", cwd=self.main)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"], ["explore", "lor_merge"])
        self.assertEqual(Path(calls[0]["cwd"]).resolve(), self.main.resolve())

    def test_worktree_redirect_adds_p_main(self):
        proc = self.run_cg("explore", "lor_merge", cwd=self.worktree)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("using the index at", proc.stderr)
        self.assertIn(str(self.main), proc.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"], ["explore", "-p", str(self.main.resolve()), "lor_merge"])

    def test_subdirectory_of_worktree_redirects(self):
        proc = self.run_cg("node", "Thing", cwd=self.worktree_sub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.calls()
        self.assertEqual(
            calls[0]["argv"], ["node", "-p", str(self.main.resolve()), "Thing"]
        )

    def test_caller_p_is_replaced(self):
        proc = self.run_cg(
            "explore", "-p", str(self.worktree), "lor_merge", cwd=self.worktree
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.calls()
        self.assertEqual(
            calls[0]["argv"], ["explore", "-p", str(self.main.resolve()), "lor_merge"]
        )

    def test_caller_path_equals_is_replaced(self):
        proc = self.run_cg(
            "explore", "--path=" + str(self.worktree), "lor_merge", cwd=self.worktree
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.calls()
        self.assertEqual(
            calls[0]["argv"], ["explore", "-p", str(self.main.resolve()), "lor_merge"]
        )

    def test_index_writing_subcommands_refused_in_worktree(self):
        for sub in ("init", "index", "sync", "uninit"):
            with self.subTest(sub=sub):
                proc = self.run_cg(sub, cwd=self.worktree)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("worktree is never indexed", proc.stderr)
                self.assertIn(str(self.main), proc.stderr)
                self.assertEqual(self.calls(), [])

    def test_nested_worktree_write_refused_before_index_lookup(self):
        for args in (("sync",), ("uninit", "-f"), ("init",), ("index",)):
            with self.subTest(args=args):
                proc = self.run_cg(*args, cwd=self.nested)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("worktree is never indexed", proc.stderr)
                self.assertIn(str(self.main), proc.stderr)
                self.assertEqual(self.calls(), [])

    def test_nested_worktree_read_redirects_to_main(self):
        proc = self.run_cg("explore", "needle", cwd=self.nested)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("using the index at", proc.stderr)
        self.assertEqual(
            self.calls()[0]["argv"],
            ["explore", "-p", str(self.main.resolve()), "needle"],
        )

    def test_worktree_under_unrelated_index_uses_its_own_main(self):
        proc = self.run_cg("explore", "needle", cwd=self.outer_wt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv, ["explore", "-p", str(self.main.resolve()), "needle"])
        self.assertNotIn(str(self.outer.resolve()), argv)

    def test_symlinked_worktree_is_still_a_worktree(self):
        proc = self.run_cg("sync", cwd=self.worktree_link)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(self.calls(), [])
        proc = self.run_cg("explore", "needle", cwd=self.worktree_link)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.calls()[0]["argv"],
            ["explore", "-p", str(self.main.resolve()), "needle"],
        )

    def test_self_indexed_worktree_still_refuses_writes(self):
        (self.worktree / ".codegraph" / "codegraph.db").write_bytes(b"wt db")
        proc = self.run_cg("index", cwd=self.worktree)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(self.calls(), [])

    def test_attached_short_path_after_subcommand_is_replaced(self):
        proc = self.run_cg(
            "explore", "-p" + str(self.worktree), "lor_merge", cwd=self.worktree
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.calls()[0]["argv"],
            ["explore", "-p", str(self.main.resolve()), "lor_merge"],
        )

    def test_attached_short_path_before_subcommand_is_replaced(self):
        proc = self.run_cg(
            "-p" + str(self.worktree), "explore", "lor_merge", cwd=self.worktree
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.calls()[0]["argv"],
            ["explore", "-p", str(self.main.resolve()), "lor_merge"],
        )

    def test_double_dash_path_literal_is_preserved(self):
        proc = self.run_cg("query", "--", "--path=literal", cwd=self.worktree)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.calls()[0]["argv"],
            ["query", "-p", str(self.main.resolve()), "--", "--path=literal"],
        )

    def test_double_dash_short_p_is_preserved(self):
        proc = self.run_cg("explore", "--", "-p", "literal", cwd=self.worktree)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.calls()[0]["argv"],
            ["explore", "-p", str(self.main.resolve()), "--", "-p", "literal"],
        )

    def test_double_dash_path_separated_is_preserved(self):
        proc = self.run_cg("explore", "--", "--path", "literal", cwd=self.worktree)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.calls()[0]["argv"],
            ["explore", "-p", str(self.main.resolve()), "--", "--path", "literal"],
        )

    def test_option_value_before_subcommand_is_not_the_subcommand(self):
        proc = self.run_cg("--max-files", "5", "explore", "q", cwd=self.worktree)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("using the index at", proc.stderr)
        self.assertEqual(
            self.calls()[0]["argv"],
            ["--max-files", "5", "-p", str(self.main.resolve()), "explore", "q"],
        )

    def test_wal_sidecar_alone_is_not_an_index(self):
        proc = self.run_cg("--where", cwd=self.wal_only)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(proc.stdout, "")
        proc = self.run_cg("explore", "needle", cwd=self.wal_only)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(self.calls(), [])

    def test_missing_git_degrades_to_no_index(self):
        empty = Path(self.tmp.name) / "empty path"
        empty.mkdir()
        env = {**os.environ, "PATH": str(empty)}
        proc = subprocess.run(
            [sys.executable, str(SHIM), "explore", "needle"],
            cwd=str(self.worktree),
            input="",
            capture_output=True,
            text=True,
            timeout=60,
            env={
                **env,
                "CG_REAL_CODEGRAPH": str(self.fake),
                "CG_FAKE_RECORD": str(self.record),
            },
        )
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("no CodeGraph index", proc.stderr)
        self.assertEqual(self.calls(), [])

    def test_no_index_anywhere_exits_three(self):
        proc = self.run_cg("explore", "lor_merge", cwd=self.no_index)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("no CodeGraph index", proc.stderr)
        self.assertEqual(self.calls(), [])

    def test_non_git_directory_exits_three(self):
        proc = self.run_cg("explore", "lor_merge", cwd=self.non_git)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("no CodeGraph index", proc.stderr)
        self.assertEqual(self.calls(), [])

    def test_where_in_main_checkout(self):
        proc = self.run_cg("--where", cwd=self.main)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(Path(proc.stdout.strip()).resolve(), self.main.resolve())
        self.assertEqual(self.calls(), [])

    def test_where_in_worktree_resolves_main(self):
        proc = self.run_cg("--where", cwd=self.worktree)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(Path(proc.stdout.strip()).resolve(), self.main.resolve())
        self.assertEqual(self.calls(), [])

    def test_where_without_index_exits_three(self):
        proc = self.run_cg("--where", cwd=self.no_index)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("no CodeGraph index", proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(self.calls(), [])

    def test_stdin_passes_through(self):
        payload = "a.py\nb.py\n"
        proc = self.run_cg("affected", "--stdin", cwd=self.worktree, stdin=payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.calls()
        self.assertEqual(
            calls[0]["argv"], ["affected", "-p", str(self.main.resolve()), "--stdin"]
        )
        self.assertEqual(calls[0]["stdin"], payload)

    def test_real_exit_status_is_returned(self):
        proc = self.run_cg(
            "explore", "lor_merge", cwd=self.worktree, env={"CG_FAKE_EXIT": "7"}
        )
        self.assertEqual(proc.returncode, 7)

    def test_help_passes_through_without_index(self):
        proc = self.run_cg("--help", cwd=self.non_git)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls()[0]["argv"], ["--help"])

    def test_bare_cg_passes_through(self):
        proc = self.run_cg(cwd=self.non_git)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls()[0]["argv"], [])

    def test_missing_real_binary_exits_127(self):
        empty = Path(self.tmp.name) / "empty path"
        empty.mkdir()
        env = {**os.environ, "PATH": str(empty)}
        env.pop("CG_REAL_CODEGRAPH", None)
        proc = subprocess.run(
            [sys.executable, str(SHIM), "explore", "x"],
            cwd=str(self.main),
            input="",
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        self.assertEqual(proc.returncode, 127)
        self.assertIn("no codegraph binary", proc.stderr)


class InstallerTest(unittest.TestCase):
    """--codex-shim installs the cg shim beside muse, editing no shell rc."""

    def run_installer(self, home, *args):
        # A PATH without the real muse keeps --uninstall from touching the
        # host's muse plugin list; every command the script needs is in
        # /usr/bin or /bin.
        env = {**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"}
        return subprocess.run(
            ["sh", str(INSTALLER), *args],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    def test_installs_cg_without_shell_rc_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            proc = self.run_installer(home, "--codex-shim")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            dest = home / ".local/share/agent-dash/bin/cg"
            self.assertTrue(dest.is_file())
            self.assertTrue(os.access(dest, os.X_OK))
            self.assertEqual(dest.read_bytes(), SHIM.read_bytes())
            self.assertIn("PATH", proc.stdout)
            self.assertFalse((home / ".zshrc").exists())
            self.assertFalse((home / ".bashrc").exists())

    def test_installs_muse_byte_for_byte_and_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            proc = self.run_installer(home, "--codex-shim")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            dest = home / ".local/share/agent-dash/bin/muse"
            self.assertTrue(dest.is_file())
            self.assertTrue(os.access(dest, os.X_OK))
            self.assertEqual(dest.read_bytes(), (ROOT / "shims" / "muse").read_bytes())

    def test_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            first = self.run_installer(home, "--codex-shim")
            self.assertEqual(first.returncode, 0, first.stderr)
            dest = home / ".local/share/agent-dash/bin/cg"
            before = dest.read_bytes()
            second = self.run_installer(home, "--codex-shim")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("already installed", second.stdout)
            self.assertEqual(dest.read_bytes(), before)

    def test_identical_non_executable_copy_is_made_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(self.run_installer(home, "--codex-shim").returncode, 0)
            dest = home / ".local/share/agent-dash/bin/cg"
            dest.chmod(0o644)
            proc = self.run_installer(home, "--codex-shim")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(os.access(dest, os.X_OK))

    def test_uninstall_removes_both_shims(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(self.run_installer(home, "--codex-shim").returncode, 0)
            bindir = home / ".local/share/agent-dash/bin"
            self.assertTrue((bindir / "cg").is_file())
            self.assertTrue((bindir / "muse").is_file())
            proc = self.run_installer(home, "--uninstall")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((bindir / "cg").exists())
            self.assertFalse((bindir / "muse").exists())


if __name__ == "__main__":
    unittest.main()
