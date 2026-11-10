"""Committed-tree evidence: contract hash, probes, git tree, receipts.

Every test uses a temp state dir and a temp git repo. Nothing live is read.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

from dash import evidence  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "T")
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "one")
    return path


def contract_task(**overrides):
    task = {
        "id": "WS9.A",
        "contract_version": 1,
        "acceptance": {
            "metric": "tests",
            "threshold": "pass",
            "tolerance": "0",
            "probe": f"{sys.executable} -c 'pass'",
        },
        "files": ["src/mod.py"],
        "deps": [],
        "kind": "task",
    }
    task.update(overrides)
    return task


class ContractHashTests(unittest.TestCase):
    def test_hash_ignores_presentation_fields_and_key_order(self):
        first = contract_task()
        second = dict(reversed(list(contract_task().items())))
        second["title"] = "a different title"
        second["status"] = "wip"
        self.assertEqual(evidence.contract_hash(first), evidence.contract_hash(second))

    def test_hash_changes_with_acceptance_files_or_deps(self):
        base = evidence.contract_hash(contract_task())
        changed_acceptance = contract_task()
        changed_acceptance["acceptance"] = dict(changed_acceptance["acceptance"], threshold="0 failures")
        self.assertNotEqual(base, evidence.contract_hash(changed_acceptance))
        self.assertNotEqual(base, evidence.contract_hash(contract_task(files=["src/other.py"])))
        self.assertNotEqual(base, evidence.contract_hash(contract_task(deps=["WS9.B"])))
        self.assertNotEqual(base, evidence.contract_hash(contract_task(kind="read")))


class ProbeTests(unittest.TestCase):
    def test_string_and_list_probes(self):
        probes, cwd = evidence.probe_commands(contract_task())
        self.assertEqual(probes, [f"{sys.executable} -c 'pass'"])
        self.assertEqual(cwd, ".")
        task = contract_task()
        task["acceptance"] = dict(task["acceptance"], probe=["one", "two"], cwd="tools/app")
        self.assertEqual(evidence.probe_commands(task), (["one", "two"], "tools/app"))

    def test_absolute_home_and_parent_cwd_are_refused(self):
        for cwd in ("/tmp", "~/x", "../outside", "a/../../b"):
            task = contract_task()
            task["acceptance"] = dict(task["acceptance"], cwd=cwd)
            with self.assertRaises(evidence.EvidenceError) as caught:
                evidence.probe_commands(task)
            self.assertEqual(caught.exception.reason, "bad-cwd", cwd)

    def test_missing_probe_is_refused(self):
        task = contract_task()
        task["acceptance"] = {"metric": "tests", "threshold": "pass", "tolerance": "0"}
        with self.assertRaises(evidence.EvidenceError) as caught:
            evidence.probe_commands(task)
        self.assertEqual(caught.exception.reason, "no-probe")


class GitTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_repo_returns_the_head_tree(self):
        repo = make_repo(self.root / "repo")
        tree = evidence.git_tree(repo)
        self.assertRegex(tree, r"^[0-9a-f]{40}$")
        self.assertEqual(tree, git(repo, "rev-parse", "HEAD^{tree}").stdout.strip())

    def test_dirty_repo_is_refused(self):
        repo = make_repo(self.root / "repo")
        (repo / "a.txt").write_text("changed\n", encoding="utf-8")
        with self.assertRaises(evidence.EvidenceError) as caught:
            evidence.git_tree(repo)
        self.assertEqual(caught.exception.reason, "dirty-tree")

    def test_untracked_file_is_dirty(self):
        repo = make_repo(self.root / "repo")
        (repo / "new.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(evidence.EvidenceError) as caught:
            evidence.git_tree(repo)
        self.assertEqual(caught.exception.reason, "dirty-tree")

    def test_non_git_directory_is_refused(self):
        plain = self.root / "plain"
        plain.mkdir()
        with self.assertRaises(evidence.EvidenceError) as caught:
            evidence.git_tree(plain)
        self.assertEqual(caught.exception.reason, "not-a-git-worktree")

    def test_repo_without_a_commit_is_refused(self):
        repo = self.root / "empty"
        repo.mkdir()
        git(repo, "init", "-q")
        with self.assertRaises(evidence.EvidenceError) as caught:
            evidence.git_tree(repo)
        self.assertEqual(caught.exception.reason, "no-git-tree")


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = make_repo(self.root / "repo")

    def tearDown(self):
        self.tmp.cleanup()

    def test_passing_probe_returns_a_receipt_with_hashes_only(self):
        receipt = evidence.verify_task(contract_task(), self.repo, now=1234.0)
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["reason"], "ok")
        self.assertEqual(receipt["task_id"], "WS9.A")
        self.assertEqual(receipt["checked_at"], 1234.0)
        self.assertEqual(receipt["exit_code"], 0)
        self.assertRegex(receipt["tree"], r"^[0-9a-f]{40}$")
        self.assertRegex(receipt["stdout_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(receipt["stderr_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("stdout", receipt)
        self.assertNotIn("stderr", receipt)
        self.assertNotIn("env", receipt)
        self.assertNotIn("prompt", receipt)

    def test_nonzero_exit_fails(self):
        task = contract_task()
        task["acceptance"] = dict(task["acceptance"], probe=f"{sys.executable} -c 'raise SystemExit(3)'")
        receipt = evidence.verify_task(task, self.repo)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["reason"], "probe-failed")
        self.assertEqual(receipt["exit_code"], 3)

    def test_timeout_fails(self):
        task = contract_task()
        task["acceptance"] = dict(task["acceptance"], probe=f"{sys.executable} -c 'import time; time.sleep(5)'")
        receipt = evidence.verify_task(task, self.repo, timeout_s=0.2)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["reason"], "timeout")

    def test_dirty_tree_fails_before_any_probe(self):
        (self.repo / "a.txt").write_text("changed\n", encoding="utf-8")
        receipt = evidence.verify_task(contract_task(), self.repo)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["reason"], "dirty-tree")
        self.assertIsNone(receipt["exit_code"])

    def test_probe_runs_in_the_declared_cwd(self):
        (self.repo / "sub").mkdir()
        task = contract_task()
        probe = f'{sys.executable} -c "import os,sys; sys.exit(0 if os.path.basename(os.getcwd()) == \'sub\' else 1)"'
        task["acceptance"] = dict(task["acceptance"], probe=probe, cwd="sub")
        self.assertTrue(evidence.verify_task(task, self.repo)["ok"])

    def test_legacy_task_is_not_probed(self):
        task = {"id": "WS9.A", "acceptance": {"probe": f"{sys.executable} -c 'raise SystemExit(9)'"}}
        receipt = evidence.verify_task(task, self.repo)
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["reason"], "legacy")
        self.assertTrue(receipt["legacy"])
        self.assertIsNone(receipt["probe_hash"])


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = make_repo(self.root / "repo")
        self.state = self.root / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_and_load_roundtrip(self):
        task = contract_task()
        receipt = evidence.verify_task(task, self.repo)
        path = evidence.write_receipt(receipt, state_dir=self.state)
        self.assertTrue(path.is_file())
        self.assertEqual(evidence.load_receipt(task, state_dir=self.state), receipt)
        self.assertIsNone(evidence.load_receipt({"id": "WS9.Z"}, state_dir=self.state))

    def test_valid_receipt_matches_contract_probe_and_tree(self):
        task = contract_task()
        receipt = evidence.verify_task(task, self.repo)
        self.assertTrue(evidence.receipt_valid(receipt, task, self.repo))

    def test_amended_tree_invalidates(self):
        task = contract_task()
        receipt = evidence.verify_task(task, self.repo)
        (self.repo / "a.txt").write_text("two\n", encoding="utf-8")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-q", "--amend", "--no-edit")
        self.assertFalse(evidence.receipt_valid(receipt, task, self.repo))

    def test_dirty_tree_invalidates(self):
        task = contract_task()
        receipt = evidence.verify_task(task, self.repo)
        (self.repo / "a.txt").write_text("changed\n", encoding="utf-8")
        self.assertFalse(evidence.receipt_valid(receipt, task, self.repo))

    def test_changed_contract_or_probe_invalidates(self):
        task = contract_task()
        receipt = evidence.verify_task(task, self.repo)
        changed = contract_task(files=["src/other.py"])
        self.assertFalse(evidence.receipt_valid(receipt, changed, self.repo))
        changed_probe = contract_task()
        changed_probe["acceptance"] = dict(changed_probe["acceptance"], probe=f"{sys.executable} -c 'pass '")
        self.assertFalse(evidence.receipt_valid(receipt, changed_probe, self.repo))

    def test_failed_receipt_is_never_valid(self):
        task = contract_task()
        task["acceptance"] = dict(task["acceptance"], probe=f"{sys.executable} -c 'raise SystemExit(1)'")
        receipt = evidence.verify_task(task, self.repo)
        self.assertFalse(evidence.receipt_valid(receipt, task, self.repo))

    def test_legacy_receipt_is_valid_only_for_a_legacy_task(self):
        legacy = {"id": "WS9.A"}
        receipt = evidence.verify_task(legacy, self.repo)
        self.assertTrue(evidence.receipt_valid(receipt, legacy, self.repo))
        self.assertFalse(evidence.receipt_valid(receipt, contract_task(), self.repo))

    def test_receipt_path_is_under_the_state_dir(self):
        path = evidence.receipt_path({"id": "WS9.A"}, state_dir=self.state)
        self.assertEqual(path, self.state / "receipts" / "WS9.A.json")
        self.assertEqual(evidence.receipt_path({"id": "../x"}, state_dir=self.state).parent, self.state / "receipts")


if __name__ == "__main__":
    unittest.main()
