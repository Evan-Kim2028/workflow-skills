"""Tests for scripts/gate.sh selection logic.

Builds temp git repos holding a copy of gate.sh and gate_map.json, then
runs `sh scripts/gate.sh <base> --list`. --list prints the selection
without running anything, so these tests never execute unittest suites.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATE_SRC = HERE.parent / "scripts" / "gate.sh"
MAP_SRC = HERE.parent / "scripts" / "gate_map.json"


class GateSelectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.tools = self.repo / "tools" / "agent-dash"
        (self.tools / "scripts").mkdir(parents=True)
        (self.tools / "tests").mkdir(parents=True)
        shutil.copy(GATE_SRC, self.tools / "scripts" / "gate.sh")
        shutil.copy(MAP_SRC, self.tools / "scripts" / "gate_map.json")
        self._git("init")
        self._git("config", "user.email", "gate-test@example.com")
        self._git("config", "user.name", "gate test")

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args):
        proc = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def _write(self, rel, text="placeholder\n"):
        path = self.tools / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _commit_all(self, message="checkpoint"):
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)

    def _gate(self, *args):
        proc = subprocess.run(
            ["sh", "scripts/gate.sh", *args],
            cwd=self.tools,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return (
            proc.returncode,
            proc.stdout.split(),
            proc.stderr,
        )

    def test_store_change_selects_store_family(self):
        self._write("dash/store.py")
        self._write("tests/test_store.py")
        self._commit_all()
        self._write("dash/store.py", "placeholder\n# touched\n")
        code, selected, _err = self._gate("HEAD", "--list")
        self.assertEqual(code, 0)
        for name in (
            "test_store.py",
            "test_server.py",
            "test_state.py",
            "test_doctor.py",
            "test_orchestrate.py",
            "test_poll_budgets.py",
            "test_privacy_pin.py",
            "test_page_rules.py",
        ):
            self.assertIn(name, selected)

    def test_source_change_selects_source_test(self):
        self._commit_all("empty base")
        self._write("dash/sources/cursor.py")
        code, selected, _err = self._gate("HEAD", "--list")
        self.assertEqual(code, 0)
        self.assertIn("test_cursor.py", selected)
        self.assertIn("test_poll_budgets.py", selected)
        self.assertIn("test_privacy_pin.py", selected)
        self.assertNotIn("test_store.py", selected)

    def test_unmapped_warns_and_runs_defaults(self):
        self._commit_all("empty base")
        self._write("notes.txt")
        code, selected, err = self._gate("HEAD", "--list")
        self.assertEqual(code, 0)
        self.assertIn("WARN unmapped: notes.txt", err)
        self.assertIn("test_page_rules.py", selected)
        self.assertIn("test_server.py", selected)

    def test_changed_test_selects_itself(self):
        self._write("tests/test_cursor.py")
        self._commit_all()
        self._write("tests/test_cursor.py", "placeholder\n# touched\n")
        code, selected, _err = self._gate("HEAD", "--list")
        self.assertEqual(code, 0)
        self.assertIn("test_cursor.py", selected)

    def test_committed_diff_against_base(self):
        self._write("dash/doctor.py")
        self._commit_all("v1")
        self._write("dash/doctor.py", "placeholder\n# v2\n")
        self._commit_all("v2")
        code, selected, _err = self._gate("HEAD~1", "--list")
        self.assertEqual(code, 0)
        for name in ("test_doctor.py", "test_state.py", "test_errors.py"):
            self.assertIn(name, selected)

    def test_list_runs_nothing(self):
        # A broken test file must not explode --list: selection only.
        self._commit_all("empty base")
        self._write("tests/test_zzz_boom.py", "this is not python !!!\n")
        code, selected, err = self._gate("HEAD", "--list")
        self.assertEqual(code, 0)
        self.assertIn("test_zzz_boom.py", selected)
        self.assertNotIn("Traceback", err)

    def test_bad_base_exits_two(self):
        self._commit_all("empty base")
        code, _selected, err = self._gate("no-such-ref", "--list")
        self.assertEqual(code, 2)
        self.assertIn("unknown base ref", err)


class InstallHooksTest(unittest.TestCase):
    INSTALLER = HERE.parent / "scripts" / "install_hooks.sh"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init"], cwd=self.repo, capture_output=True, timeout=30)

    def tearDown(self):
        self.tmp.cleanup()

    def _install(self):
        return subprocess.run(
            ["sh", str(self.INSTALLER)],
            cwd=self.repo,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_install_is_idempotent(self):
        for _ in range(2):
            proc = self._install()
            self.assertEqual(proc.returncode, 0, proc.stderr)
        hook = self.repo / ".git" / "hooks" / "pre-push"
        self.assertTrue(os.access(hook, os.X_OK))
        text = hook.read_text(encoding="utf-8")
        self.assertIn("agent-dash-gate", text)
        self.assertIn("tools/agent-dash/scripts/gate.sh", text)

    def test_refuses_foreign_hook(self):
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
        proc = self._install()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refusing to overwrite", proc.stderr)
        self.assertIn("foreign", hook.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
