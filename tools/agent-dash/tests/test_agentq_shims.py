"""agentq shims: laptop-wide ssh and pytest slots, the pytest memory cap, and the pi PATH setup.

Offline. Every test uses a temp AGENTQ_HOME. Shim end-to-end runs put fake real
binaries (``ssh``, ``pytest``, ``python3``) and a fake memcap script on PATH, so
nothing reaches a real host and no real test suite runs.
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIMS = ROOT / "agentq" / "shims"
sys.path.insert(0, str(ROOT))

from agentq import cli, slots  # noqa: E402
from agentq.shims import dispatch  # noqa: E402

LIMITS = slots.Limits(
    ssh=8, pytest=16, adaptive=True, floor=4, min_free_pct=35.0, max_load_per_cpu=1.0, gauge_ttl_s=15.0
)


class FakeClock:
    """A clock that advances only when the test sleeps."""

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeGauges:
    """Injected memory and load readings; counts reads."""

    def __init__(self, free, load):
        self.free = free
        self.load = load
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.free, self.load


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.bin = Path(self._tmp.name) / "bin"
        self.bin.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def fake_binary(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def shim_env(self, extra=None):
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(SHIMS), str(self.bin), "/usr/bin", "/bin"])
        env["AGENTQ_HOME"] = str(self.home)
        env["AGENTQ_SHIM_PYTHON"] = sys.executable
        env["AGENTQ_MEMCAP"] = str(self.memcap_script())
        for key in ("AGENTQ_SLOT_HELD", "AGENTQ_MEMCAPPED", "AGENTQ_RUN", "AGENTQ_JOB"):
            env.pop(key, None)
        (self.home / "broker.json").write_text(json.dumps({"shims": {"ssh_gated_hosts": ["prod-box"]}}),
                                               encoding="utf-8")
        env.update(extra or {})
        return env

    def memcap_script(self):
        path = self.bin / "memcap.sh"
        if not path.exists():
            record = self.home / "memcap-argv"
            body = '#!/bin/sh\nprintf "%s\\n" "$@" > "' + str(record) + '"\nshift\nexec "$@"\n'
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        return path


class SemaphoreTests(Base):
    def test_admits_up_to_n_and_blocks_the_next(self):
        first = slots.acquire("ssh", limit=2, home=self.home, max_wait_s=0)
        second = slots.acquire("ssh", limit=2, home=self.home, max_wait_s=0)
        self.assertTrue(first.slotted and second.slotted)
        third = slots.acquire("ssh", limit=2, home=self.home, max_wait_s=0, warn=lambda m: None)
        self.assertFalse(third.slotted)
        first.release()
        fourth = slots.acquire("ssh", limit=2, home=self.home, max_wait_s=0)
        self.assertTrue(fourth.slotted)
        second.release()
        fourth.release()

    def test_waiter_gets_the_slot_when_a_holder_releases(self):
        held = slots.acquire("ssh", limit=1, home=self.home, max_wait_s=0)
        got = {}

        def waiter():
            got["holder"] = slots.acquire("ssh", limit=1, home=self.home, max_wait_s=10)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.4)
        self.assertTrue(thread.is_alive())
        held.release()
        thread.join(10)
        self.assertTrue(got["holder"].slotted)
        got["holder"].release()

    def test_crashed_holder_frees_its_slot(self):
        script = (
            "import os, sys; sys.path.insert(0, sys.argv[1]); from agentq import slots;"
            "slots.acquire('ssh', limit=1, home=sys.argv[2], label='crash', max_wait_s=0); os._exit(0)"
        )
        subprocess.run([sys.executable, "-c", script, str(ROOT), str(self.home)], check=True, timeout=30)
        self.assertEqual(len(slots.holders(self.home, "ssh", 1)), 0)
        holder = slots.acquire("ssh", limit=1, home=self.home, max_wait_s=0)
        self.assertTrue(holder.slotted)
        holder.release()

    def test_holder_record_names_pid_label_and_command(self):
        holder = slots.acquire("ssh", limit=3, home=self.home, label="ws9-exec-x", command="ssh prod-box uptime",
                               max_wait_s=0)
        found = slots.holders(self.home, "ssh", 3)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["pid"], os.getpid())
        self.assertEqual(found[0]["label"], "ws9-exec-x")
        self.assertEqual(found[0]["command"], "ssh prod-box uptime")
        self.assertIsInstance(found[0]["held_s"], int)
        holder.release()

    def test_max_wait_runs_unslotted_and_logs_an_overrun(self):
        held = slots.acquire("ssh", limit=1, home=self.home, max_wait_s=0)
        clock = FakeClock()
        warnings = []
        holder = slots.acquire("ssh", limit=1, home=self.home, label="ws1-exec-y", max_wait_s=60,
                               clock=clock, sleep=clock.sleep, warn=warnings.append)
        self.assertFalse(holder.slotted)
        self.assertTrue(warnings and "unslotted" in warnings[0])
        self.assertTrue((self.home / "slots" / "overruns.log").is_file())
        self.assertGreaterEqual(clock.now, 1060)
        self.assertTrue(all(0.3 <= s <= 6.0 for s in clock.sleeps))
        held.release()

    def test_backoff_starts_at_half_a_second_and_caps_at_five(self):
        held = slots.acquire("ssh", limit=1, home=self.home, max_wait_s=0)
        clock = FakeClock()
        slots.acquire("ssh", limit=1, home=self.home, max_wait_s=120, clock=clock, sleep=clock.sleep,
                      jitter=lambda: 0.5, warn=lambda m: None)
        self.assertAlmostEqual(clock.sleeps[0], 0.5)
        self.assertLessEqual(max(clock.sleeps), 5.0)
        held.release()


class PytestAdmissionTests(Base):
    def admit(self, free, load, limits=LIMITS):
        gauges = FakeGauges(free, load)
        return slots.pytest_admission(limits, gauges), gauges

    def test_below_the_floor_admits_without_reading_gauges(self):
        admit, gauges = self.admit(1, 9.0)
        self.assertTrue(admit(3))
        self.assertEqual(gauges.reads, 0)

    def test_at_the_floor_needs_memory_and_load(self):
        admit, _ = self.admit(50.0, 0.5)
        self.assertTrue(admit(4))
        self.assertFalse(self.admit(20.0, 0.5)[0](4))
        self.assertFalse(self.admit(50.0, 1.2)[0](4))
        self.assertFalse(self.admit(34.9, 0.1)[0](4))
        self.assertTrue(self.admit(35.0, 0.99)[0](4))

    def test_ceiling_refuses_even_with_headroom(self):
        admit, _ = self.admit(90.0, 0.1)
        self.assertFalse(admit(16))

    def test_unreadable_gauges_add_no_gate(self):
        self.assertTrue(self.admit(None, 0.2)[0](6))
        self.assertTrue(self.admit(60.0, None)[0](6))

    def test_adaptive_off_keeps_only_the_ceiling(self):
        limits = slots.Limits(ssh=8, pytest=16, adaptive=False, floor=4, min_free_pct=35.0,
                              max_load_per_cpu=1.0, gauge_ttl_s=15.0)
        admit, gauges = self.admit(1.0, 9.0, limits)
        self.assertTrue(admit(10))
        self.assertFalse(admit(16))
        self.assertEqual(gauges.reads, 0)

    def test_gauges_read_memory_once_per_ttl(self):
        clock = FakeClock()
        calls = []

        def memory():
            calls.append(clock.now)
            return 42.0

        gauges = slots.Gauges(self.home, ttl_s=15.0, memory=memory, loadavg=lambda: (0.8, 0.0, 0.0),
                              cpus=lambda: 4, clock=clock)
        self.assertEqual(gauges.read(), (42.0, 0.2))
        self.assertEqual(gauges.read(), (42.0, 0.2))
        self.assertEqual(len(calls), 1)
        clock.now += 16
        gauges.read()
        self.assertEqual(len(calls), 2)

    def test_load_limits_reads_broker_keys_and_defaults(self):
        self.assertEqual(slots.load_limits(self.home).pytest, 16)
        (self.home / "broker.json").write_text(json.dumps({
            "slots": {"tests": 10, "ssh": 4},
            "adaptive": {"tests_floor": 2},
        }), encoding="utf-8")
        limits = slots.load_limits(self.home)
        self.assertEqual((limits.pytest, limits.ssh, limits.floor), (10, 4, 2))
        self.assertEqual(limits.min_free_pct, 35.0)


class HostParsingTests(unittest.TestCase):
    def test_destination_host_skips_option_values(self):
        cases = {
            ("prod-box-main", "uptime"): "prod-box-main",
            ("-p", "2222", "prod-box", "ls"): "prod-box",
            ("-i", "~/.ssh/k", "-o", "StrictHostKeyChecking=no", "evan@prod-box"): "prod-box",
            ("-oBatchMode=yes", "-p22", "prod-box"): "prod-box",
            ("-J", "bastion", "-l", "evan", "prod-box-main"): "prod-box-main",
            ("-vvT", "git@github.com", "git-upload-pack"): "github.com",
            ("--", "prod-box"): "prod-box",
            ("ssh://evan@prod-box:22",): "prod-box",
            ("-V",): None,
            (): None,
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(dispatch.ssh_host(list(argv)), expected)

    def test_only_listed_hosts_are_gated(self):
        patterns = ("prod-box-main", "prod-box")
        self.assertTrue(dispatch.ssh_gated(["prod-box-main", "uptime"], patterns))
        self.assertTrue(dispatch.ssh_gated(["-p", "22", "evan@prod-box"], patterns))
        self.assertFalse(dispatch.ssh_gated(["-T", "git@github.com"], patterns))
        self.assertFalse(dispatch.ssh_gated(["-V"], patterns))

    def test_no_host_is_gated_until_the_local_config_lists_one(self):
        home = Path(tempfile.mkdtemp())
        try:
            self.assertEqual(dispatch.DEFAULT_GATED_HOSTS, ())
            self.assertEqual(dispatch.gated_hosts(home), ())
            self.assertFalse(dispatch.ssh_gated(["prod-box"], dispatch.gated_hosts(home)))
        finally:
            shutil.rmtree(home)

    def test_configured_host_list_replaces_the_default(self):
        home = Path(tempfile.mkdtemp())
        try:
            (home / "broker.json").write_text(json.dumps({"shims": {"ssh_gated_hosts": ["box-*"]}}),
                                              encoding="utf-8")
            self.assertEqual(dispatch.gated_hosts(home), ("box-*",))
            self.assertTrue(dispatch.ssh_gated(["box-7"], dispatch.gated_hosts(home)))
            self.assertFalse(dispatch.ssh_gated(["prod-box"], dispatch.gated_hosts(home)))
        finally:
            shutil.rmtree(home)

    def test_pytest_invocations_are_recognised(self):
        self.assertTrue(dispatch.is_pytest("pytest", ["-q", "x.py"]))
        self.assertTrue(dispatch.is_pytest("python3", ["-m", "pytest", "x.py"]))
        self.assertTrue(dispatch.is_pytest("python", ["-m", "pytest"]))
        self.assertTrue(dispatch.is_pytest("uv", ["run", "pytest", "x.py"]))
        self.assertTrue(dispatch.is_pytest("uv", ["run", "--no-sync", "python", "-m", "pytest"]))
        self.assertFalse(dispatch.is_pytest("python3", ["script.py", "-m", "pytest"]))
        self.assertFalse(dispatch.is_pytest("python3", ["-c", "print(1)"]))
        self.assertFalse(dispatch.is_pytest("uv", ["sync"]))
        self.assertFalse(dispatch.is_pytest("ssh", ["pytest"]))


class RealBinaryTests(Base):
    def test_resolution_skips_the_shim_folder(self):
        real = self.fake_binary("ssh", "exit 0\n")
        env = {"PATH": os.pathsep.join([str(SHIMS), str(self.bin)])}
        self.assertEqual(dispatch.real_binary("ssh", env), str(real))

    def test_missing_real_binary_fails_with_a_message(self):
        env = {"PATH": str(SHIMS)}
        with self.assertRaises(dispatch.ShimError) as caught:
            dispatch.real_binary("ssh", env)
        self.assertIn("no real `ssh`", str(caught.exception))

    def test_missing_real_binary_exits_127_from_the_shell_shim(self):
        result = subprocess.run([str(SHIMS / "ssh"), "-V"], env={"PATH": str(SHIMS),
                                                                 "AGENTQ_SHIM_PYTHON": sys.executable},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 127)
        self.assertIn("no real `ssh`", result.stderr)


class ShimEndToEndTests(Base):
    def test_python_passes_non_pytest_argv_through_untouched(self):
        record = self.bin / "argv"
        self.fake_binary("python3", f'printf "%s\\n" "$0" "$@" > "{record}"\n')
        result = subprocess.run([str(SHIMS / "python3"), "-c", "print('a b')", "--flag"],
                                env=self.shim_env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = record.read_text().splitlines()
        self.assertEqual(lines[1:], ["-c", "print('a b')", "--flag"])
        self.assertFalse((self.home / "slots").exists())

    def test_pytest_runs_under_memcap_and_returns_the_childs_exit_code(self):
        self.fake_binary("python3", 'printf "%s\\n" "$@" > "{}"\nexit 3\n'.format(self.bin / "child"))
        result = subprocess.run([str(SHIMS / "python3"), "-m", "pytest", "tests/x.py"],
                                env=self.shim_env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 3)
        wrapped = (self.home / "memcap-argv").read_text().split("\n")[:5]
        self.assertEqual(wrapped, ["8192", str(self.bin / "python3"), "-m", "pytest", "tests/x.py"])
        self.assertEqual((self.bin / "child").read_text().split(), ["-m", "pytest", "tests/x.py"])

    def test_pytest_holds_a_slot_while_the_child_runs(self):
        child = (
            f"exec {sys.executable} -c \"import sys; sys.path.insert(0, '{ROOT}');"
            "from agentq import slots; print('busy', len(slots.holders('%s', 'pytest', 16)))\""
        ) % self.home
        self.fake_binary("pytest", child + "\n")
        result = subprocess.run([str(SHIMS / "pytest"), "-q"], env=self.shim_env({"AGENTQ_MEMCAPPED": "1"}),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("busy 1", result.stdout)
        self.assertEqual(slots.holders(self.home, "pytest", 16), [])

    def test_nested_pytest_inside_a_held_pytest_does_not_wait_for_a_slot(self):
        self.fake_binary("pytest", "echo nested-ok\n")
        result = subprocess.run([str(SHIMS / "pytest")], env=self.shim_env({"AGENTQ_SLOT_HELD": "pytest",
                                                                             "AGENTQ_MEMCAPPED": "1"}),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertIn("nested-ok", result.stdout)
        self.assertFalse((self.home / "slots" / "pytest").exists())

    def test_gated_ssh_holds_an_ssh_slot_and_returns_the_exit_code(self):
        record = self.bin / "ssh-argv"
        self.fake_binary("ssh", f'printf "%s\\n" "$@" > "{record}"\nexit 5\n')
        result = subprocess.run([str(SHIMS / "ssh"), "-o", "BatchMode=yes", "prod-box", "uptime"],
                                env=self.shim_env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 5)
        self.assertEqual(record.read_text().split(), ["-o", "BatchMode=yes", "prod-box", "uptime"])
        self.assertEqual(slots.holders(self.home, "ssh", 8), [])
        self.assertTrue((self.home / "slots" / "ssh").is_dir())

    def test_ungated_ssh_is_exec_d_without_a_slot(self):
        record = self.bin / "ssh-argv"
        self.fake_binary("ssh", f'printf "%s\\n" "$@" > "{record}"\n')
        result = subprocess.run([str(SHIMS / "ssh"), "-T", "git@github.com", "git-upload-pack"],
                                env=self.shim_env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(record.read_text().split(), ["-T", "git@github.com", "git-upload-pack"])
        self.assertFalse((self.home / "slots" / "ssh").exists())


class LaunchPiShimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("launch_pi", ROOT / "scripts" / "launch_pi.py")
        cls.launch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.launch)

    def test_shims_are_prepended_to_path(self):
        env = self.launch.shim_environ({"PATH": "/usr/bin:/bin"}, shims_dir=SHIMS, python="/py")
        self.assertEqual(env["PATH"], os.pathsep.join([str(SHIMS), "/usr/bin", "/bin"]))
        self.assertEqual(env["AGENTQ_SHIM_PYTHON"], "/py")

    def test_shims_are_not_duplicated(self):
        env = self.launch.shim_environ({"PATH": os.pathsep.join([str(SHIMS), "/bin"])}, shims_dir=SHIMS)
        self.assertEqual(env["PATH"].split(os.pathsep).count(str(SHIMS)), 1)

    def test_agentq_shims_zero_leaves_path_alone(self):
        before = {"PATH": "/usr/bin:/bin", "AGENTQ_SHIMS": "0"}
        env = self.launch.shim_environ(dict(before), shims_dir=SHIMS, python="/py")
        self.assertEqual(env, before)

    def test_launch_pi_shim_dir_is_the_shims_folder(self):
        self.assertEqual(self.launch.SHIMS_DIR.resolve(), SHIMS.resolve())


class SlotsCommandTests(Base):
    def test_slots_command_lists_holders_and_ends_with_answer(self):
        holder = slots.acquire("pytest", limit=16, home=self.home, label="ws2-exec-z", command="pytest -q",
                               max_wait_s=0)
        out = io.StringIO()
        code = cli.main(["slots"], env={"AGENTQ_HOME": str(self.home)}, out=out)
        holder.release()
        text = out.getvalue().splitlines()
        self.assertEqual(code, 0)
        self.assertIn("pytest: 1 of 16 held", text)
        self.assertIn("ssh: 0 of 8 held", text)
        self.assertIn("ws2-exec-z", "\n".join(text))
        self.assertTrue(text[-1].startswith("ANSWER: "))
        self.assertIn("pytest 1/16", text[-1])


if __name__ == "__main__":
    unittest.main()
