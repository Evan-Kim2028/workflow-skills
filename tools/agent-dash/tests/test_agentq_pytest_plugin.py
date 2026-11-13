"""agentq pytest plugin: the run takes the laptop pytest slot, skips when it should, never fails a run.

Offline. Every test uses a temp AGENTQ_HOME. The live-interpreter tests run
``sys.executable -m pytest`` (an absolute-path interpreter, as a pi worker runs
a venv) on a trivial test file in a temp folder, so no project suite runs. The
xdist cap tests run real ``-n`` runs on a temp module and count the distinct
``PYTEST_XDIST_WORKER`` values the tests write.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SHIMS = ROOT / "agentq" / "shims"
PLUGIN_DIR = ROOT / "agentq" / "pyplugin"
sys.path.insert(0, str(ROOT))

from agentq import slots  # noqa: E402

PLUGIN_PATH = PLUGIN_DIR / "agentq_pytest_slot.py"
LIMITS = slots.Limits(
    ssh=8, pytest=16, adaptive=False, floor=4, min_free_pct=35.0, max_load_per_cpu=1.0, gauge_ttl_s=15.0
)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = load_module("agentq_pytest_slot_under_test", PLUGIN_PATH)
launch = load_module("launch_pi_under_test", ROOT / "scripts" / "launch_pi.py")


def fake_config(args=("-q", "tests/x.py"), worker=False):
    config = SimpleNamespace(invocation_params=SimpleNamespace(args=list(args)))
    if worker:
        config.workerinput = {"workerid": "gw0"}
    return config


class PluginBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.home / "broker.json").write_text(json.dumps({"adaptive": {"enabled": False}}), encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {"AGENTQ_HOME": str(self.home), "AGENTQ_RUN": "ws9-exec-plug"})
        self.env.start()
        for key in ("AGENTQ_PYTEST_SLOT_HELD", "PYTEST_XDIST_WORKER", "AGENTQ_JOB", "AGENTQ_SLOTS_PY"):
            os.environ.pop(key, None)
        plugin._holder = None
        plugin._set_held_flag = False

    def tearDown(self):
        plugin.pytest_unconfigure(fake_config())
        self.env.stop()
        self._tmp.cleanup()

    def pytest_dir(self):
        return self.home / "slots" / "pytest"


class PluginSlotTests(PluginBase):
    def test_configure_takes_a_slot_labelled_with_the_run_and_unconfigure_releases_it(self):
        plugin.pytest_configure(fake_config(("-q", "tests/x.py")))
        held = slots.holders(self.home, "pytest", 16)
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["label"], "ws9-exec-plug")
        self.assertEqual(held[0]["command"], "pytest -q tests/x.py")
        self.assertEqual(os.environ.get("AGENTQ_PYTEST_SLOT_HELD"), "1")
        plugin.pytest_unconfigure(fake_config())
        self.assertEqual(slots.holders(self.home, "pytest", 16), [])
        self.assertNotIn("AGENTQ_PYTEST_SLOT_HELD", os.environ)

    def test_label_falls_back_to_agentq_job_then_dash(self):
        os.environ.pop("AGENTQ_RUN")
        os.environ["AGENTQ_JOB"] = "job-42"
        plugin.pytest_configure(fake_config())
        self.assertEqual(slots.holders(self.home, "pytest", 16)[0]["label"], "job-42")
        plugin.pytest_unconfigure(fake_config())
        os.environ.pop("AGENTQ_JOB")
        plugin.pytest_configure(fake_config())
        self.assertEqual(slots.holders(self.home, "pytest", 16)[0]["label"], "-")

    def test_xdist_worker_takes_no_slot(self):
        plugin.pytest_configure(fake_config(worker=True))
        self.assertFalse(self.pytest_dir().exists())
        self.assertNotIn("AGENTQ_PYTEST_SLOT_HELD", os.environ)

    def test_xdist_worker_environment_takes_no_slot(self):
        os.environ["PYTEST_XDIST_WORKER"] = "gw1"
        plugin.pytest_configure(fake_config())
        self.assertFalse(self.pytest_dir().exists())

    def test_held_run_skips_the_slot(self):
        os.environ["AGENTQ_PYTEST_SLOT_HELD"] = "1"
        plugin.pytest_configure(fake_config())
        self.assertFalse(self.pytest_dir().exists())
        plugin.pytest_unconfigure(fake_config())
        self.assertEqual(os.environ.get("AGENTQ_PYTEST_SLOT_HELD"), "1")

    def test_missing_slots_module_logs_one_line_and_runs_without_a_slot(self):
        os.environ["AGENTQ_SLOTS_PY"] = str(self.tmp / "missing" / "slots.py")
        err = io.StringIO()
        with redirect_stderr(err):
            plugin.pytest_configure(fake_config())
            plugin.pytest_unconfigure(fake_config())
        self.assertIn("agentq pytest slot: no slot taken", err.getvalue())
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertNotIn("AGENTQ_PYTEST_SLOT_HELD", os.environ)

    def test_unwritable_slot_dir_never_fails_the_run(self):
        blocked = self.tmp / "blocked-home"
        blocked.write_text("a file, not a folder", encoding="utf-8")
        os.environ["AGENTQ_HOME"] = str(blocked)
        err = io.StringIO()
        with redirect_stderr(err):
            plugin.pytest_configure(fake_config())
            plugin.pytest_unconfigure(fake_config())
        self.assertIn("agentq pytest slot", err.getvalue())
        self.assertNotIn("AGENTQ_PYTEST_SLOT_HELD", os.environ)

    def test_unreadable_slots_file_never_fails_the_run(self):
        broken = self.tmp / "broken-slots.py"
        broken.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        os.environ["AGENTQ_SLOTS_PY"] = str(broken)
        err = io.StringIO()
        with redirect_stderr(err):
            plugin.pytest_configure(fake_config())
        self.assertIn("boom", err.getvalue())

    def test_agentq_package_root_is_not_left_on_sys_path(self):
        without_root = [entry for entry in sys.path if entry != str(ROOT)]
        with mock.patch.object(sys, "path", list(without_root)):
            plugin.pytest_configure(fake_config())
            plugin.pytest_unconfigure(fake_config())
            self.assertEqual(sys.path, without_root)
        self.assertEqual(slots.holders(self.home, "pytest", 16), [])


class XdistCapUnitTests(unittest.TestCase):
    """The cap value and the clamp, without a subprocess."""

    def test_cap_defaults_to_two_and_ignores_blank_or_invalid_values(self):
        self.assertEqual(plugin._xdist_cap({}), 2)
        self.assertEqual(plugin._xdist_cap({"LOR_CI_XDIST": "  "}), 2)
        self.assertEqual(plugin._xdist_cap({"LOR_CI_XDIST": "nope"}), 2)
        self.assertEqual(plugin._xdist_cap({"LOR_CI_XDIST": "0"}), 2)
        self.assertEqual(plugin._xdist_cap({"LOR_CI_XDIST": "3"}), 3)

    def test_clamp_lowers_numprocesses_and_tx(self):
        config = SimpleNamespace(option=SimpleNamespace(numprocesses=8, tx=["popen"] * 8))
        self.assertEqual(plugin._clamp_xdist(config, {}), (8, 2))
        self.assertEqual(config.option.numprocesses, 2)
        self.assertEqual(config.option.tx, ["popen", "popen"])

    def test_clamp_leaves_zero_one_and_no_n_alone(self):
        for num, tx in ((0, []), (1, ["popen"]), (None, None)):
            config = SimpleNamespace(option=SimpleNamespace(numprocesses=num, tx=tx))
            self.assertIsNone(plugin._clamp_xdist(config, {}))
            self.assertEqual(config.option.numprocesses, num)
            self.assertEqual(config.option.tx, tx)

    def test_clamp_skips_a_config_without_xdist_options(self):
        self.assertIsNone(plugin._clamp_xdist(SimpleNamespace(option=SimpleNamespace()), {}))
        self.assertIsNone(plugin._clamp_xdist(SimpleNamespace(), {}))


def probe_script():
    """A temp test that records which pytest slots are busy, as the holder's JSON records."""
    return (
        "import fcntl, json, os\n"
        "from pathlib import Path\n"
        "def test_slot_held():\n"
        "    home = Path(os.environ['AGENTQ_HOME'])\n"
        "    busy = []\n"
        "    for path in sorted((home / 'slots' / 'pytest').glob('slot-*')):\n"
        "        fd = os.open(path, os.O_RDWR)\n"
        "        try:\n"
        "            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)\n"
        "            fcntl.flock(fd, fcntl.LOCK_UN)\n"
        "        except BlockingIOError:\n"
        "            busy.append(json.loads(path.read_text()))\n"
        "        finally:\n"
        "            os.close(fd)\n"
        "    Path(os.environ['PROBE_OUT']).write_text(json.dumps(busy))\n"
    )


class LiveInterpreterTests(unittest.TestCase):
    """``sys.executable -m pytest`` with launch_pi's env holds a pytest slot during the run."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.work = self.tmp / "work"
        self.work.mkdir()
        (self.work / "test_probe.py").write_text(probe_script(), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def pi_env(self, extra=None):
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", str(self.tmp))}
        env = launch.shim_environ(base, shims_dir=SHIMS, python=sys.executable)
        env.update({
            "AGENTQ_HOME": str(self.home),
            "AGENTQ_RUN": "ws9-exec-live",
            "PROBE_OUT": str(self.tmp / "probe.json"),
        })
        (self.home / "broker.json").write_text(json.dumps({"adaptive": {"enabled": False}}), encoding="utf-8")
        env.update(extra or {})
        return env

    def run_pytest(self, env, interpreter=None):
        command = [interpreter or sys.executable, "-m", "pytest", "-q", "test_probe.py", "-p", "no:cacheprovider"]
        return subprocess.run(command, cwd=str(self.work), env=env, capture_output=True, text=True, timeout=120)

    def test_absolute_path_interpreter_holds_a_pytest_slot_during_the_run(self):
        result = self.run_pytest(self.pi_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        busy = json.loads((self.tmp / "probe.json").read_text())
        self.assertEqual(len(busy), 1, busy)
        self.assertEqual(busy[0]["label"], "ws9-exec-live")
        self.assertTrue(busy[0]["command"].startswith("pytest"))
        self.assertEqual(slots.holders(self.home, "pytest", 16), [])


def worker_script(count):
    """A temp test module whose tests append their ``PYTEST_XDIST_WORKER`` to ``WORKERS_OUT``."""
    body = (
        "import os\n"
        "from pathlib import Path\n"
        "def _record():\n"
        "    with open(os.environ['WORKERS_OUT'], 'a', encoding='utf-8') as fh:\n"
        "        fh.write(os.environ.get('PYTEST_XDIST_WORKER', 'main') + '\\n')\n"
    )
    for index in range(count):
        body += f"def test_{index}():\n    _record()\n"
    return body


class XdistCapTests(unittest.TestCase):
    """Real xdist runs: the plugin caps the workers xdist actually creates, not just ``-n auto``.

    Each run is ``sys.executable -m pytest`` on a temp module of 8 trivial tests that append
    their ``PYTEST_XDIST_WORKER`` to a file, so the distinct values are the workers created.
    ``AGENTQ_PYTEST_SLOT_HELD=1`` keeps the slot out of these runs; the cap is a separate hook.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.home / "broker.json").write_text(json.dumps({"adaptive": {"enabled": False}}), encoding="utf-8")
        self.work = self.tmp / "work"
        self.work.mkdir()
        (self.work / "test_workers.py").write_text(worker_script(8), encoding="utf-8")
        self.out = self.tmp / "workers.txt"

    def tearDown(self):
        self._tmp.cleanup()

    def env(self, extra=None, plugin=True):
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", str(self.tmp))}
        env = launch.shim_environ(base, shims_dir=SHIMS, python=sys.executable) if plugin else dict(base)
        env.update({
            "AGENTQ_HOME": str(self.home),
            "AGENTQ_RUN": "ws9-exec-xdist",
            "AGENTQ_PYTEST_SLOT_HELD": "1",
            "WORKERS_OUT": str(self.out),
        })
        for key in ("PYTEST_XDIST_AUTO_NUM_WORKERS", "LOR_CI_XDIST"):
            env.pop(key, None)
        env.update(extra or {})
        return env

    def run_pytest(self, args, env):
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args]
        return subprocess.run(command, cwd=str(self.work), env=env, capture_output=True, text=True, timeout=180)

    def workers(self):
        if not self.out.exists():
            return []
        return sorted({line for line in self.out.read_text(encoding="utf-8").split() if line})

    def assert_capped(self, result, cap):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        workers = self.workers()
        self.assertTrue(workers, result.stdout + result.stderr)
        self.assertLessEqual(len(workers), cap, workers)
        return workers

    def test_explicit_n_eight_is_capped_at_two(self):
        result = self.run_pytest(["-n", "8"], self.env())
        self.assert_capped(result, 2)
        self.assertIn("xdist workers lowered from 8 to 2 (LOR_CI_XDIST)", result.stderr)
        self.assertEqual(result.stderr.count("xdist workers lowered"), 1)

    def test_n_auto_with_a_large_environment_value_is_capped(self):
        result = self.run_pytest(["-n", "auto"], self.env({"PYTEST_XDIST_AUTO_NUM_WORKERS": "8"}))
        self.assert_capped(result, 2)
        self.assertIn("xdist workers lowered from 8 to 2 (LOR_CI_XDIST)", result.stderr)

    def test_n_logical_is_capped(self):
        result = self.run_pytest(["-n", "logical"], self.env())
        self.assert_capped(result, 2)

    def test_cap_is_configurable_through_the_launcher_variable(self):
        result = self.run_pytest(["-n", "8"], self.env({"LOR_CI_XDIST": "3"}))
        self.assert_capped(result, 3)
        self.assertIn("xdist workers lowered from 8 to 3 (LOR_CI_XDIST)", result.stderr)

    def test_n_one_and_no_n_are_left_alone(self):
        one = self.run_pytest(["-n", "1"], self.env())
        self.assertEqual(one.returncode, 0, one.stdout + one.stderr)
        self.assertEqual(self.workers(), ["gw0"])
        self.assertNotIn("xdist workers lowered", one.stderr)
        self.out.unlink()
        plain = self.run_pytest([], self.env())
        self.assertEqual(plain.returncode, 0, plain.stdout + plain.stderr)
        self.assertEqual(self.workers(), ["main"])
        self.assertNotIn("xdist workers lowered", plain.stderr)

    def test_without_the_plugin_the_same_run_creates_more_than_two_workers(self):
        """Control: the worker count above is real, not an artefact of the temp module."""
        result = self.run_pytest(["-n", "4"], self.env(plugin=False))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.workers()), 4, self.workers())


class ShimHandoffTests(unittest.TestCase):
    """The pytest shim marks its child, so the plugin inside it takes no second slot."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.home / "broker.json").write_text(json.dumps({"adaptive": {"enabled": False}}), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def shim_env(self, extra=None):
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(SHIMS), str(self.bin), "/usr/bin", "/bin"])
        env["AGENTQ_HOME"] = str(self.home)
        env["AGENTQ_SHIM_PYTHON"] = sys.executable
        env["AGENTQ_MEMCAPPED"] = "1"
        for key in ("AGENTQ_SLOT_HELD", "AGENTQ_PYTEST_SLOT_HELD", "AGENTQ_RUN", "AGENTQ_JOB"):
            env.pop(key, None)
        env.update(extra or {})
        return env

    def test_shim_child_sees_the_held_flag_and_takes_no_second_slot(self):
        seen = self.tmp / "seen"
        fake = self.bin / "pytest"
        fake.write_text(
            f'#!/bin/sh\nprintf "%s" "$AGENTQ_PYTEST_SLOT_HELD" > "{seen}"\n'
            f'exec {sys.executable} -c "import sys; sys.path.insert(0, \'{ROOT}\');'
            "from agentq import slots; import os; print(len(slots.holders(os.environ['AGENTQ_HOME'], 'pytest', 16)))\"\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        result = subprocess.run([str(SHIMS / "pytest"), "-q"], env=self.shim_env(),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(seen.read_text(), "1")
        self.assertEqual(result.stdout.strip(), "1")

    def test_plugin_held_run_makes_a_nested_shim_pytest_skip_the_slot(self):
        fake = self.bin / "pytest"
        fake.write_text("#!/bin/sh\necho nested-ok\n", encoding="utf-8")
        fake.chmod(0o755)
        result = subprocess.run([str(SHIMS / "pytest")], env=self.shim_env({"AGENTQ_PYTEST_SLOT_HELD": "1"}),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nested-ok", result.stdout)
        self.assertFalse((self.home / "slots" / "pytest").exists())


class LaunchPiPluginEnvTests(unittest.TestCase):
    def env(self, base):
        return launch.shim_environ(dict(base), shims_dir=SHIMS, python="/py", plugin_dir=PLUGIN_DIR)

    def test_plugin_dir_is_prepended_to_an_existing_pythonpath(self):
        env = self.env({"PATH": "/bin", "PYTHONPATH": "/a:/b"})
        self.assertEqual(env["PYTHONPATH"], os.pathsep.join([str(PLUGIN_DIR), "/a", "/b"]))

    def test_plugin_dir_is_the_only_pythonpath_entry_when_unset(self):
        env = self.env({"PATH": "/bin"})
        self.assertEqual(env["PYTHONPATH"], str(PLUGIN_DIR))

    def test_plugin_dir_is_not_duplicated_on_a_second_call(self):
        once = self.env({"PATH": "/bin"})
        twice = launch.shim_environ(dict(once), shims_dir=SHIMS, python="/py", plugin_dir=PLUGIN_DIR)
        self.assertEqual(twice["PYTHONPATH"].split(os.pathsep).count(str(PLUGIN_DIR)), 1)
        self.assertEqual(twice["PYTEST_ADDOPTS"].count("-p agentq_pytest_slot"), 1)

    def test_addopts_keep_the_existing_value_and_gain_the_plugin(self):
        env = self.env({"PATH": "/bin", "PYTEST_ADDOPTS": "-q --maxfail=1"})
        self.assertEqual(env["PYTEST_ADDOPTS"], "-q --maxfail=1 -p agentq_pytest_slot")

    def test_addopts_are_just_the_plugin_when_unset(self):
        env = self.env({"PATH": "/bin"})
        self.assertEqual(env["PYTEST_ADDOPTS"], "-p agentq_pytest_slot")

    def test_xdist_cap_defaults_to_two_and_keeps_an_explicit_cap(self):
        env = self.env({"PATH": "/bin"})
        self.assertEqual(env["PYTEST_XDIST_AUTO_NUM_WORKERS"], "2")
        self.assertEqual(env["LOR_CI_XDIST"], "2")
        kept = self.env({"PATH": "/bin", "PYTEST_XDIST_AUTO_NUM_WORKERS": "4", "LOR_CI_XDIST": "3"})
        self.assertEqual(kept["PYTEST_XDIST_AUTO_NUM_WORKERS"], "4")
        self.assertEqual(kept["LOR_CI_XDIST"], "3")

    def test_empty_xdist_value_counts_as_unset(self):
        env = self.env({"PATH": "/bin", "LOR_CI_XDIST": "  "})
        self.assertEqual(env["LOR_CI_XDIST"], "2")

    def test_agentq_shims_zero_skips_the_plugin_env(self):
        before = {"PATH": "/usr/bin:/bin", "AGENTQ_SHIMS": "0"}
        env = self.env(before)
        self.assertEqual(env, before)
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("PYTEST_ADDOPTS", env)

    def test_default_plugin_dir_is_the_pyplugin_folder(self):
        self.assertEqual(launch.PYTEST_PLUGIN_DIR.resolve(), PLUGIN_DIR.resolve())
        self.assertTrue((launch.PYTEST_PLUGIN_DIR / "agentq_pytest_slot.py").is_file())


if __name__ == "__main__":
    unittest.main()
