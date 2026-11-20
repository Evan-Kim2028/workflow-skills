"""agentq pytest result cache: a hit skips the run, a miss runs it, and nothing ever fails a run.

Offline. Every test uses a temp ``AGENTQ_HOME`` and a temp git repo, so no project
suite runs. The end-to-end tests run ``sys.executable -m pytest`` on a trivial test
file in that temp repo, the way a pi worker runs a venv interpreter by absolute path.
"""

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "agentq" / "pyplugin"
sys.path.insert(0, str(ROOT))

from agentq import slots  # noqa: E402


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = load_module("agentq_pytest_cache_under_test", PLUGIN_DIR / "agentq_pytest_cache.py")
dispatch = load_module("agentq_shim_dispatch_under_test", ROOT / "agentq" / "shims" / "dispatch.py")

TEST_FILE = "from helpers import value\n\n\ndef test_a():\n    assert value() == 1\n"
FILES = {
    "tests/test_a.py": TEST_FILE,
    "tests/helpers.py": "def value():\n    return 1\n",
    "tests/conftest.py": "",
    "uv.lock": "version = 1\n",
    "pyproject.toml": "[tool.pytest.ini_options]\n",
}


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)


def make_repo(root, files=None):
    """A temp git repo with ``files`` committed, so the tree sha and the config listing work."""
    root = Path(root)
    root.mkdir(parents=True)
    for rel, text in (FILES if files is None else files).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "init")
    return root


def fake_config(args, cwd):
    return SimpleNamespace(invocation_params=SimpleNamespace(args=list(args), dir=str(cwd)))


class CacheBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = mock.patch.dict(os.environ, {"AGENTQ_HOME": str(self.home),
                                                "AGENTQ_RUN": "ws9-exec-cache"})
        self.env.start()
        for key in ("AGENTQ_JOB", "LOR_PYTEST_CACHE", "AGENTQ_GUARD", "PYTHONPATH", "PYTEST_ADDOPTS",
                    "PGHOST", "LOR_DBT_MANIFEST"):
            os.environ.pop(key, None)
        self.repo = make_repo(self.tmp / "repo")

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def key(self, args=("-q", "tests/test_a.py"), cwd=None):
        return plugin._key_material(fake_config(args, cwd or self.repo), os.environ)

    def entries(self):
        base = self.home / "pytest-cache" / "entries"
        return sorted(base.rglob("*.json")) if base.exists() else []


class KeyTests(CacheBase):
    def test_a_clean_tree_keys_and_the_same_inputs_key_the_same(self):
        first, why = self.key()
        self.assertEqual(why, "")
        self.assertEqual(len(first), 64)
        self.assertEqual(self.key()[0], first)

    def test_an_edit_to_the_test_file_misses(self):
        before = self.key()[0]
        (self.repo / "tests/test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
        self.assertNotEqual(self.key()[0], before)

    def test_an_edit_to_a_transitive_import_misses(self):
        before = self.key()[0]
        (self.repo / "tests/helpers.py").write_text("def value():\n    return 2\n", encoding="utf-8")
        self.assertNotEqual(self.key()[0], before)

    def test_an_edit_to_a_conftest_misses(self):
        before = self.key()[0]
        (self.repo / "tests/conftest.py").write_text("import pytest\n", encoding="utf-8")
        self.assertNotEqual(self.key()[0], before)

    def test_an_edit_to_uv_lock_misses(self):
        before = self.key()[0]
        (self.repo / "uv.lock").write_text("version = 2\n", encoding="utf-8")
        self.assertNotEqual(self.key()[0], before)

    def test_a_new_nested_pyproject_misses(self):
        before = self.key()[0]
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg/pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        self.assertNotEqual(self.key()[0], before)

    def test_a_k_flag_misses(self):
        before = self.key()[0]
        self.assertNotEqual(self.key(("-q", "-k", "a", "tests/test_a.py"))[0], before)

    def test_a_pythonpath_change_misses(self):
        before = self.key()[0]
        os.environ["PYTHONPATH"] = "/somewhere"
        self.assertNotEqual(self.key()[0], before)

    def test_a_pg_variable_change_misses(self):
        before = self.key()[0]
        os.environ["PGHOST"] = "elsewhere"
        self.assertNotEqual(self.key()[0], before)

    def test_a_lor_variable_change_misses(self):
        before = self.key()[0]
        os.environ["LOR_DBT_MANIFEST"] = "/tmp/manifest.json"
        self.assertNotEqual(self.key()[0], before)

    def test_the_control_variable_is_not_part_of_the_key(self):
        before = self.key()[0]
        os.environ["LOR_PYTEST_CACHE"] = "1"
        self.assertEqual(self.key()[0], before)

    def test_a_different_interpreter_misses(self):
        before = self.key()[0]
        with mock.patch.object(plugin, "_interpreter", return_value="/other/python"):
            self.assertNotEqual(self.key()[0], before)

    def test_a_second_repo_at_another_path_keys_differently(self):
        other = make_repo(self.tmp / "other")
        self.assertNotEqual(self.key(cwd=other)[0], self.key()[0])

    def test_a_missing_target_is_not_cached(self):
        key, why = self.key(("-q", "tests/test_missing.py"))
        self.assertIsNone(key)
        self.assertIn("missing target", why)

    def test_a_directory_target_is_not_cached(self):
        key, why = self.key(("-q", "tests"))
        self.assertIsNone(key)
        self.assertIn("directory target", why)

    def test_no_target_is_not_cached(self):
        key, why = self.key(("-q",))
        self.assertIsNone(key)
        self.assertIn("no test target", why)

    def test_an_unresolved_repo_import_is_not_cached(self):
        (self.repo / "tests/test_a.py").write_text("import tests.missing_mod\n", encoding="utf-8")
        key, why = self.key()
        self.assertIsNone(key)
        self.assertIn("unresolved import", why)

    def test_an_external_import_is_not_unresolved(self):
        (self.repo / "tests/test_a.py").write_text("import json\n\nimport pytest\n", encoding="utf-8")
        key, why = self.key()
        self.assertEqual(why, "")
        self.assertIsNotNone(key)

    def test_a_relative_import_is_followed(self):
        (self.repo / "tests/pkg").mkdir()
        (self.repo / "tests/pkg/__init__.py").write_text("", encoding="utf-8")
        (self.repo / "tests/pkg/mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "tests/test_a.py").write_text("from .pkg.mod import VALUE\n", encoding="utf-8")
        before = self.key()[0]
        (self.repo / "tests/pkg/mod.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.assertNotEqual(self.key()[0], before)

    def test_a_dynamic_import_with_a_literal_name_is_followed(self):
        (self.repo / "tests/dyn.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "tests/test_a.py").write_text(
            "import importlib\n\nmod = importlib.import_module('tests.dyn')\n", encoding="utf-8")
        before = self.key()[0]
        (self.repo / "tests/dyn.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.assertNotEqual(self.key()[0], before)

    def test_a_src_layout_import_is_followed(self):
        (self.repo / "pkg/src/deep").mkdir(parents=True)
        (self.repo / "pkg/src/deep/__init__.py").write_text("", encoding="utf-8")
        (self.repo / "pkg/src/deep/mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "tests/test_a.py").write_text("from deep.mod import VALUE\n", encoding="utf-8")
        before = self.key()[0]
        (self.repo / "pkg/src/deep/mod.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.assertNotEqual(self.key()[0], before)

    def test_a_whole_tree_test_keys_on_the_tree(self):
        (self.repo / "tests/test_a.py").write_text(
            "import subprocess\n\n\ndef test_a():\n    subprocess.run(['git', 'ls-files'])\n",
            encoding="utf-8")
        before = self.key()[0]
        (self.repo / "docs").mkdir()
        (self.repo / "docs/note.md").write_text("x\n", encoding="utf-8")
        self.assertNotEqual(self.key()[0], before)

    def test_a_whole_tree_test_ignores_pytest_cache_and_pycache(self):
        (self.repo / "tests/test_a.py").write_text(
            "import subprocess\n\n\ndef test_a():\n    subprocess.run(['git', 'ls-files'])\n",
            encoding="utf-8")
        before = self.key()[0]
        (self.repo / ".pytest_cache").mkdir()
        (self.repo / ".pytest_cache/README.md").write_text("x\n", encoding="utf-8")
        (self.repo / "tests/__pycache__").mkdir()
        (self.repo / "tests/__pycache__/helpers.cpython-311.pyc").write_bytes(b"x")
        self.assertEqual(self.key()[0], before)

    def test_a_whole_tree_test_without_git_is_not_cached(self):
        plain = self.tmp / "plain"
        (plain / "tests").mkdir(parents=True)
        (plain / "tests/test_a.py").write_text("import subprocess\nsubprocess.run(['git', 'ls-files'])\n",
                                               encoding="utf-8")
        key, why = self.key(cwd=plain)
        self.assertIsNone(key)
        self.assertIn("no tree sha", why)


class ImportCacheTests(CacheBase):
    def counting(self):
        calls = []
        real = plugin._import_targets

        def wrapper(source):
            calls.append(1)
            return real(source)

        return calls, mock.patch.object(plugin, "_import_targets", wrapper)

    def test_the_import_list_is_cached_by_blob_and_reused(self):
        calls, patcher = self.counting()
        with patcher:
            self.key()
            first = len(calls)
            self.key()
            self.assertEqual(len(calls), first)
        self.assertGreater(first, 0)

    def test_an_edited_file_is_parsed_again(self):
        self.key()
        (self.repo / "tests/helpers.py").write_text("def value():\n    return 3\n", encoding="utf-8")
        calls, patcher = self.counting()
        with patcher:
            self.key()
        self.assertGreater(len(calls), 0)

    def test_the_import_cache_lives_under_the_agentq_home(self):
        self.key()
        self.assertTrue(list((self.home / "pytest-cache" / "imports").glob("*.json")))

    def test_the_closure_is_cached_and_a_repeat_does_not_walk(self):
        walks = []
        real = plugin._walk

        def wrapper(*args, **kwargs):
            walks.append(1)
            return real(*args, **kwargs)

        with mock.patch.object(plugin, "_walk", wrapper):
            first = self.key()[0]
            self.assertEqual(len(walks), 1)
            self.assertEqual(self.key()[0], first)
            self.assertEqual(len(walks), 1)
        self.assertTrue(list((self.home / "pytest-cache" / "closures").glob("*.json")))

    def test_a_changed_member_pulls_in_its_new_import(self):
        self.key()
        (self.repo / "tests/extra.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "tests/helpers.py").write_text(
            "import extra\n\n\ndef value():\n    return extra.VALUE\n", encoding="utf-8")
        after_import = self.key()[0]
        (self.repo / "tests/extra.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.assertNotEqual(self.key()[0], after_import)


class EventTests(CacheBase):
    def test_a_miss_and_a_hit_are_recorded_with_the_cache_field(self):
        config = fake_config(("-q", "tests/test_a.py"), self.repo)
        self.assertIsNone(plugin.pytest_cmdline_main(config))
        key = config._agentq_pytest_cache_key
        self.assertTrue(key)
        plugin._write_entry(os.environ, key, config)
        err = io.StringIO()
        with redirect_stderr(err):
            code = plugin.pytest_cmdline_main(fake_config(("-q", "tests/test_a.py"), self.repo))
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertIn("1 file(s) unchanged since a recorded pass", err.getvalue())
        self.assertIn("LOR_PYTEST_CACHE=0", err.getvalue())
        rows = slots.read_events(self.home)
        self.assertEqual([row["cache"] for row in rows], ["miss", "hit"])
        self.assertEqual([row["outcome"] for row in rows], ["cache-miss", "cache-hit"])
        self.assertEqual(rows[0]["label"], "ws9-exec-cache")
        self.assertEqual(rows[0]["argv"], "tests/test_a.py")
        self.assertEqual(rows[0]["kind"], "pytest")

    def test_a_miss_that_cannot_be_keyed_records_why(self):
        plugin.pytest_cmdline_main(fake_config(("-q", "tests/test_missing.py"), self.repo))
        rows = slots.read_events(self.home)
        self.assertEqual(rows[0]["cache"], "miss")
        self.assertIn("missing target", rows[0]["why"])

    def test_history_counts_cache_hits_and_misses(self):
        config = fake_config(("-q", "tests/test_a.py"), self.repo)
        plugin.pytest_cmdline_main(config)
        plugin._write_entry(os.environ, config._agentq_pytest_cache_key, config)
        plugin.pytest_cmdline_main(fake_config(("-q", "tests/test_a.py"), self.repo))
        report = slots.history(self.home, since_s=3600.0)
        self.assertEqual(report["totals"]["cache_hits"], 1)
        self.assertEqual(report["totals"]["cache_misses"], 1)
        self.assertEqual(report["totals"]["runs"], 0)
        table = slots.format_history(report)
        self.assertIn("hit miss", table)
        self.assertIn("ws9-exec-cache", table)


class GatingTests(CacheBase):
    def test_inactive_without_a_run_label(self):
        os.environ.pop("AGENTQ_RUN")
        self.assertFalse(plugin._active(os.environ))
        self.assertIsNone(plugin.pytest_cmdline_main(fake_config(("-q", "tests/test_a.py"), self.repo)))
        self.assertFalse((self.home / "slots").exists())

    def test_agentq_job_alone_activates_it(self):
        os.environ.pop("AGENTQ_RUN")
        os.environ["AGENTQ_JOB"] = "job-7"
        self.assertTrue(plugin._active(os.environ))

    def test_lor_pytest_cache_zero_disables_it(self):
        os.environ["LOR_PYTEST_CACHE"] = "0"
        self.assertFalse(plugin._active(os.environ))
        self.assertIsNone(plugin.pytest_cmdline_main(fake_config(("-q", "tests/test_a.py"), self.repo)))
        self.assertFalse((self.home / "slots").exists())


class RefusalTests(CacheBase):
    def test_a_bare_pytest_is_refused_with_the_shim_message(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = plugin.pytest_cmdline_main(fake_config((), self.repo))
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED: agentq guard: pytest has no test target", err.getvalue())
        self.assertIn("pytest <file.py>", err.getvalue())
        rows = (self.home / "guard_blocks.tsv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 1)
        self.assertIn("no test target", rows[0])
        self.assertIn("ws9-exec-cache", rows[0])

    def test_a_directory_target_is_refused(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = plugin.pytest_cmdline_main(fake_config(("tests",), self.repo))
        self.assertEqual(code, 2)
        self.assertIn("is a directory, not a test file", err.getvalue())

    def test_more_than_the_file_limit_is_refused(self):
        names = tuple(f"tests/test_{index}.py" for index in range(26))
        err = io.StringIO()
        with redirect_stderr(err):
            code = plugin.pytest_cmdline_main(fake_config(names, self.repo))
        self.assertEqual(code, 2)
        self.assertIn("limit 25", err.getvalue())

    def test_agentq_guard_zero_turns_the_refusal_off(self):
        os.environ["AGENTQ_GUARD"] = "0"
        self.assertIsNone(plugin.pytest_cmdline_main(fake_config((), self.repo)))
        self.assertFalse((self.home / "guard_blocks.tsv").exists())

    def test_the_refusal_rule_matches_the_shim(self):
        cases = [
            [], ["-q"], ["tests"], ["tests/"], ["tests/test_a.py"], ["tests/test_a.py::test_x"],
            ["-k", "slow", "tests/test_a.py"], ["--", "tests/test_a.py"],
            ["-p", "no:cacheprovider", "tests/test_a.py"],
            [f"tests/test_{index}.py" for index in range(26)],
        ]
        for args in cases:
            self.assertEqual(plugin._pytest_reason(args), dispatch.pytest_reason(args), args)


class RecordTests(CacheBase):
    def config(self, stats):
        config = fake_config(("-q", "tests/test_a.py"), self.repo)
        config._agentq_pytest_cache_key = "a" * 64
        config.pluginmanager = SimpleNamespace(getplugin=lambda name: SimpleNamespace(stats=stats))
        return config

    def test_a_clean_pass_records_the_entry(self):
        plugin.pytest_sessionfinish(SimpleNamespace(config=self.config({"passed": [1]})), 0)
        self.assertEqual(len(self.entries()), 1)

    def test_a_nonzero_exit_records_nothing(self):
        plugin.pytest_sessionfinish(SimpleNamespace(config=self.config({"passed": [1]})), 1)
        self.assertEqual(self.entries(), [])

    def test_a_skip_only_run_records_nothing(self):
        plugin.pytest_sessionfinish(SimpleNamespace(config=self.config({"skipped": [1]})), 0)
        self.assertEqual(self.entries(), [])

    def test_a_run_without_a_terminal_reporter_records_nothing(self):
        config = fake_config(("-q", "tests/test_a.py"), self.repo)
        config._agentq_pytest_cache_key = "a" * 64
        config.pluginmanager = SimpleNamespace(getplugin=lambda name: None)
        plugin.pytest_sessionfinish(SimpleNamespace(config=config), 0)
        self.assertEqual(self.entries(), [])

    def test_an_xdist_worker_records_nothing(self):
        config = self.config({"passed": [1]})
        config.workerinput = {"workerid": "gw0"}
        plugin.pytest_sessionfinish(SimpleNamespace(config=config), 0)
        self.assertEqual(self.entries(), [])

    def test_a_run_that_was_not_keyed_records_nothing(self):
        config = fake_config(("-q", "tests/test_a.py"), self.repo)
        config.pluginmanager = SimpleNamespace(getplugin=lambda name: SimpleNamespace(stats={"passed": [1]}))
        plugin.pytest_sessionfinish(SimpleNamespace(config=config), 0)
        self.assertEqual(self.entries(), [])

    def test_entries_older_than_the_max_age_are_pruned(self):
        path = plugin._entry_path(os.environ, "b" * 64)
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")
        old = time.time() - plugin.MAX_AGE_S - 60
        os.utime(path, (old, old))
        plugin._prune(self.home)
        self.assertFalse(path.exists())

    def test_a_fresh_entry_survives_a_prune(self):
        path = plugin._entry_path(os.environ, "c" * 64)
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")
        plugin._prune(self.home)
        self.assertTrue(path.exists())


class NeverFailsTests(CacheBase):
    def test_a_broken_key_logs_one_line_and_runs_pytest(self):
        err = io.StringIO()
        with mock.patch.object(plugin, "_key_material", side_effect=RuntimeError("boom")):
            with redirect_stderr(err):
                code = plugin.pytest_cmdline_main(fake_config(("-q", "tests/test_a.py"), self.repo))
        self.assertIsNone(code)
        self.assertIn("cache skipped (boom)", err.getvalue())
        self.assertEqual(err.getvalue().count("\n"), 1)

    def test_a_broken_record_logs_one_line(self):
        config = fake_config(("-q", "tests/test_a.py"), self.repo)
        config._agentq_pytest_cache_key = "a" * 64
        config.pluginmanager = SimpleNamespace(getplugin=lambda name: SimpleNamespace(stats={"passed": [1]}))
        err = io.StringIO()
        with mock.patch.object(plugin, "_write_entry", side_effect=OSError("nope")):
            with redirect_stderr(err):
                plugin.pytest_sessionfinish(SimpleNamespace(config=config), 0)
        self.assertIn("cache not recorded (nope)", err.getvalue())
        self.assertEqual(err.getvalue().count("\n"), 1)

    def test_an_unwritable_home_never_fails_the_run(self):
        blocked = self.tmp / "blocked-home"
        blocked.write_text("a file, not a folder", encoding="utf-8")
        os.environ["AGENTQ_HOME"] = str(blocked)
        config = fake_config(("-q", "tests/test_a.py"), self.repo)
        self.assertIsNone(plugin.pytest_cmdline_main(config))
        config.pluginmanager = SimpleNamespace(getplugin=lambda name: SimpleNamespace(stats={"passed": [1]}))
        plugin.pytest_sessionfinish(SimpleNamespace(config=config), 0)


class EndToEndTests(CacheBase):
    """Real ``sys.executable -m pytest`` runs in the temp repo, the way a worker runs a venv.

    Each subprocess costs a few seconds on a loaded laptop, so the scenarios are grouped:
    one method per story rather than one per assertion.
    """

    def run_pytest(self, *args, env=None):
        base = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.tmp),
            "AGENTQ_HOME": str(self.home),
            "AGENTQ_RUN": "ws9-exec-cache",
            "PYTHONPATH": str(PLUGIN_DIR),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        base.update(env or {})
        return subprocess.run([sys.executable, "-m", "pytest", "-p", "agentq_pytest_cache", *args],
                              cwd=str(self.repo), env=base, capture_output=True, text=True, timeout=300)

    def test_a_repeat_is_skipped_and_an_edit_runs_it_again(self):
        first = self.run_pytest("-q", "tests/test_a.py")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("1 passed", first.stdout)
        second = self.run_pytest("-q", "tests/test_a.py")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("agentq pytest cache: 1 file(s) unchanged", second.stderr)
        self.assertIn("LOR_PYTEST_CACHE=0", second.stderr)
        self.assertNotIn("passed", second.stdout)
        (self.repo / "tests/helpers.py").write_text("def value():\n    return 1\n\n\ndef other():\n    return 2\n",
                                                    encoding="utf-8")
        third = self.run_pytest("-q", "tests/test_a.py")
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertIn("1 passed", third.stdout)
        self.assertNotIn("unchanged", third.stderr)
        rows = slots.read_events(self.home)
        self.assertEqual([row["cache"] for row in rows], ["miss", "hit", "miss"])

    def test_a_failing_run_and_a_skip_only_run_write_no_entry(self):
        (self.repo / "tests/test_a.py").write_text("def test_a():\n    assert False\n", encoding="utf-8")
        first = self.run_pytest("-q", "tests/test_a.py")
        self.assertEqual(first.returncode, 1)
        self.assertEqual(self.entries(), [])
        second = self.run_pytest("-q", "tests/test_a.py")
        self.assertNotIn("unchanged", second.stderr)
        (self.repo / "tests/test_a.py").write_text(
            "import pytest\n\n\n@pytest.mark.skip\ndef test_a():\n    pass\n", encoding="utf-8")
        third = self.run_pytest("-q", "tests/test_a.py")
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(self.entries(), [])

    def test_the_switches_and_the_guard(self):
        refused = self.run_pytest()
        self.assertEqual(refused.returncode, 2)
        self.assertIn("BLOCKED: agentq guard: pytest has no test target", refused.stderr)
        off = {"LOR_PYTEST_CACHE": "0"}
        self.assertEqual(self.run_pytest("-q", "tests/test_a.py", env=off).returncode, 0)
        again = self.run_pytest("-q", "tests/test_a.py", env=off)
        self.assertIn("1 passed", again.stdout)
        self.assertNotIn("unchanged", again.stderr)
        self.assertEqual(self.entries(), [])


if __name__ == "__main__":
    unittest.main()
