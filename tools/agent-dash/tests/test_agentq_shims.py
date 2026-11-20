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
import signal
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
from agentq import headroom as H  # noqa: E402
from agentq.shims import dispatch  # noqa: E402

GAUGE_SETTINGS = {"memory_ttl_s": 15.0, "sample_s": 10.0, "tau_s": 60.0}
LIMITS = slots.Limits(
    ssh=8, pytest=16, adaptive=True, floor=4, min_free_pct=35.0, max_cpu_busy=0.80, max_swap_frac=0.90,
    gauge_settings=GAUGE_SETTINGS,
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
    """Injected headroom readings; counts reads."""

    def __init__(self, free, cpu, swap=0.1):
        self.free = free
        self.cpu = cpu
        self.swap = swap
        self.reads = 0

    def read(self):
        self.reads += 1
        return H.Headroom(self.free, self.cpu, self.swap, None)


def dead_pid():
    """A pid that is certainly not running: a reaped child's."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def run_bounded(argv, env, timeout=10, cwd=None):
    """Run argv in its own process group and kill the whole group on timeout.

    A shim scenario that recurses must never outlive the test: the group kill
    takes every child with it, so a fork bomb cannot fill the process table.
    """
    proc = subprocess.Popen(argv, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = proc.communicate()
        raise AssertionError(f"shim run did not finish in {timeout}s: {err[-2000:]}")
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def shim_processes(marker):
    """Pids whose command line names ``marker`` (a temp shim folder)."""
    proc = subprocess.run(["ps", "-eo", "pid=,command="], capture_output=True, text=True, timeout=30)
    found = []
    for line in proc.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and marker in command:
            found.append(int(pid))
    return found


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

    def second_shims(self, name="shims2"):
        """A second copy of the shims, the way a worker put one on PATH on 2026-11-20."""
        folder = self.bin / name
        folder.mkdir()
        for entry in SHIMS.iterdir():
            if entry.is_file():
                shutil.copy(entry, folder / entry.name)
        return folder

    def shim_env(self, extra=None):
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(SHIMS), str(self.bin), "/usr/bin", "/bin"])
        env["AGENTQ_HOME"] = str(self.home)
        env["AGENTQ_REFUSED_SLEEP_PAUSE_S"] = "0"
        env["AGENTQ_SHIM_PYTHON"] = sys.executable
        env["AGENTQ_MEMCAP"] = str(self.memcap_script())
        # A pytest run that is itself under the shims (or the plugin) exports these; the child must
        # start clean, or the shim skips the gate and the end-to-end tests see no slot.
        for key in ("AGENTQ_SLOT_HELD", "AGENTQ_PYTEST_SLOT_HELD", "AGENTQ_MEMCAPPED", "AGENTQ_RUN",
                    "AGENTQ_JOB", "AGENTQ_SHIM_DEPTH"):
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
    def admit(self, free, cpu, swap=0.1, limits=LIMITS):
        gauges = FakeGauges(free, cpu, swap)
        return slots.pytest_admission(limits, gauges), gauges

    def test_below_the_floor_admits_without_reading_gauges(self):
        admit, gauges = self.admit(1, 0.99, 0.99)
        self.assertTrue(admit(3))
        self.assertEqual(gauges.reads, 0)

    def test_at_the_floor_needs_memory_cpu_and_swap(self):
        self.assertTrue(self.admit(50.0, 0.5)[0](4))
        self.assertFalse(self.admit(20.0, 0.5)[0](4))
        self.assertFalse(self.admit(50.0, 0.85)[0](4))
        self.assertFalse(self.admit(50.0, 0.3, 0.95)[0](4))
        self.assertFalse(self.admit(34.9, 0.1)[0](4))
        self.assertTrue(self.admit(35.0, 0.79)[0](4))

    def test_load_per_cpu_is_not_a_gate(self):
        admit, _ = self.admit(60.0, 0.3)
        self.assertTrue(admit(6))

    def test_ceiling_refuses_even_with_headroom(self):
        admit, _ = self.admit(90.0, 0.1)
        self.assertFalse(admit(16))

    def test_unreadable_gauges_add_no_gate(self):
        self.assertTrue(self.admit(None, 0.2, None)[0](6))
        self.assertTrue(self.admit(60.0, None, None)[0](6))

    def test_adaptive_off_keeps_only_the_ceiling(self):
        limits = slots.Limits(ssh=8, pytest=16, adaptive=False, floor=4, min_free_pct=35.0,
                              max_cpu_busy=0.80, max_swap_frac=0.90, gauge_settings=GAUGE_SETTINGS)
        admit, gauges = self.admit(1.0, 0.99, 0.99, limits)
        self.assertTrue(admit(10))
        self.assertFalse(admit(16))
        self.assertEqual(gauges.reads, 0)

    def test_gauges_read_memory_once_per_ttl(self):
        clock = FakeClock()
        calls = []

        def memory():
            calls.append(clock.now)
            return 42.0

        gauges = slots.Gauges(self.home, settings=GAUGE_SETTINGS, memory=memory, ticks=lambda: None,
                              swap=lambda: 0.1, clock=clock)
        self.assertEqual(gauges.read().free_pct, 42.0)
        self.assertEqual(gauges.read().free_pct, 42.0)
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
        self.assertEqual(limits.max_cpu_busy, 0.80)
        self.assertEqual(limits.max_swap_frac, 0.90)


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
            self.assertEqual(slots.DEFAULT_GATED_HOSTS, ())
            self.assertEqual(slots.gated_hosts(home), ())
            self.assertFalse(dispatch.ssh_gated(["prod-box"], slots.gated_hosts(home)))
        finally:
            shutil.rmtree(home)

    def test_configured_host_list_replaces_the_default(self):
        home = Path(tempfile.mkdtemp())
        try:
            (home / "broker.json").write_text(json.dumps({"shims": {"ssh_gated_hosts": ["box-*"]}}),
                                              encoding="utf-8")
            self.assertEqual(slots.gated_hosts(home), ("box-*",))
            self.assertTrue(dispatch.ssh_gated(["box-7"], slots.gated_hosts(home)))
            self.assertFalse(dispatch.ssh_gated(["prod-box"], slots.gated_hosts(home)))
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

    def test_resolution_skips_a_second_shim_folder(self):
        real = self.fake_binary("sleep", "exit 0\n")
        second = self.second_shims()
        env = {"PATH": os.pathsep.join([str(SHIMS), str(second), str(self.bin)])}
        self.assertEqual(dispatch.real_binary("sleep", env), str(real))

    def test_missing_real_binary_names_the_shim_folders(self):
        second = self.second_shims()
        env = {"PATH": os.pathsep.join([str(SHIMS), str(second)])}
        with self.assertRaises(dispatch.ShimError) as caught:
            dispatch.real_binary("sleep", env)
        self.assertIn("no real `sleep`", str(caught.exception))
        self.assertIn(str(second), str(caught.exception))

    def test_missing_real_binary_fails_with_a_message(self):
        env = {"PATH": str(SHIMS)}
        with self.assertRaises(dispatch.ShimError) as caught:
            dispatch.real_binary("ssh", env)
        self.assertIn("no real `ssh`", str(caught.exception))

    def test_missing_real_binary_exits_127_from_the_shell_shim(self):
        result = subprocess.run([str(SHIMS / "ssh"), "-V"], env={"PATH": str(SHIMS),
                                                                 "AGENTQ_SHIM_PYTHON": sys.executable,
                                                                 "AGENTQ_HOME": str(self.home)},
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
        result = subprocess.run([str(SHIMS / "pytest"), "-q", "tests/x.py"],
                                env=self.shim_env({"AGENTQ_MEMCAPPED": "1"}),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("busy 1", result.stdout)
        self.assertEqual(slots.holders(self.home, "pytest", 16), [])

    def test_nested_pytest_inside_a_held_pytest_does_not_wait_for_a_slot(self):
        self.fake_binary("pytest", "echo nested-ok\n")
        result = subprocess.run([str(SHIMS / "pytest"), "tests/x.py"], env=self.shim_env({"AGENTQ_SLOT_HELD": "pytest",
                                                                             "AGENTQ_MEMCAPPED": "1"}),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertIn("nested-ok", result.stdout)
        self.assertFalse((self.home / "slots" / "pytest").exists())

    def test_gated_ssh_holds_an_ssh_slot_and_returns_the_exit_code(self):
        env = self.shim_env()
        slots.write_ssh_hosts(self.home)
        record = self.bin / "ssh-argv"
        self.fake_binary("ssh", f'printf "%s\\n" "$@" > "{record}"\nexit 5\n')
        result = subprocess.run([str(SHIMS / "ssh"), "-o", "BatchMode=yes", "prod-box", "uptime"],
                                env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 5)
        self.assertEqual(record.read_text().split(), ["-o", "BatchMode=yes", "prod-box", "uptime"])
        self.assertTrue((self.home / "slots" / "ssh").is_dir())
        self.assertEqual(slots.holders(self.home, "ssh", 8), [])
        self.assertEqual((self.home / "slots" / "ssh_hosts").read_text(), "prod-box\n")
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
        self.assertIn("pytest: 1 of 16 held, 0 waiting", text)
        self.assertIn("ssh: 0 of 8 held, 0 waiting", text)
        self.assertIn("ws2-exec-z", "\n".join(text))
        self.assertTrue(text[-1].startswith("ANSWER: "))
        self.assertIn("pytest 1/16", text[-1])


class WaiterTests(Base):
    """A run waiting for a slot registers a waiter file, and every exit path removes it."""

    def hold(self, limit=1):
        return slots.acquire("pytest", limit=limit, home=self.home, max_wait_s=0)

    def waiter_file(self):
        return slots.waiters_dir(self.home, "pytest") / str(os.getpid())

    def wait_for(self, predicate, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def test_a_waiting_run_registers_a_waiter_file_and_drops_it_on_acquire(self):
        holder = self.hold()
        got = []
        thread = threading.Thread(target=lambda: got.append(
            slots.acquire("pytest", limit=1, home=self.home, label="waiter", max_wait_s=10)))
        thread.start()
        try:
            self.assertTrue(self.wait_for(self.waiter_file().exists), "no waiter file while waiting")
            self.assertEqual(slots.live_state(self.home, "pytest", 1).waiting, 1)
            holder.release()
            thread.join(10)
        finally:
            holder.release()
        self.assertFalse(thread.is_alive())
        self.assertTrue(got and got[0].slotted)
        self.assertFalse(self.waiter_file().exists())
        got[0].release()

    def test_a_waiter_file_is_dropped_on_timeout(self):
        holder = self.hold()
        clock = FakeClock()
        result = slots.acquire("pytest", limit=1, home=self.home, label="slow", max_wait_s=1.0,
                               clock=clock, sleep=clock.sleep, jitter=lambda: 0.5,
                               warn=lambda message: None)
        holder.release()
        self.assertFalse(result.slotted)
        self.assertFalse(self.waiter_file().exists())
        self.assertEqual([row["outcome"] for row in slots.read_events(self.home) if row["label"] == "slow"],
                         ["timeout"])

    def test_a_dead_waiters_file_is_reaped_and_recorded(self):
        directory = slots.waiters_dir(self.home, "pytest")
        directory.mkdir(parents=True)
        dead = dead_pid()
        (directory / str(dead)).write_text(json.dumps({"pid": dead, "label": "gone", "since": 1000.0}),
                                           encoding="utf-8")
        self.assertEqual(slots.reap_waiters(self.home, "pytest", clock=lambda: 1010.0), 0)
        self.assertFalse((directory / str(dead)).exists())
        rows = slots.read_events(self.home)
        self.assertEqual([(row["outcome"], row["label"], row["wait_ms"]) for row in rows],
                         [("reaped", "gone", 10000)])

    def test_a_live_waiters_file_is_left_alone(self):
        directory = slots.waiters_dir(self.home, "pytest")
        directory.mkdir(parents=True)
        (directory / str(os.getpid())).write_text(json.dumps({"pid": os.getpid(), "label": "me", "since": 1.0}),
                                                  encoding="utf-8")
        self.assertEqual(slots.reap_waiters(self.home, "pytest"), 1)
        self.assertTrue((directory / str(os.getpid())).exists())
        self.assertEqual(slots.read_events(self.home), [])

    def test_live_state_counts_holders_and_waiters(self):
        holder = self.hold(limit=2)
        directory = slots.waiters_dir(self.home, "pytest")
        directory.mkdir(parents=True)
        (directory / str(os.getpid())).write_text(json.dumps({"pid": os.getpid(), "label": "w", "since": 1.0}),
                                                  encoding="utf-8")
        state = slots.live_state(self.home, "pytest", 2)
        holder.release()
        self.assertEqual((state.kind, state.limit, state.held, state.waiting, state.busy),
                         ("pytest", 2, 1, 1, 2))

    def test_live_state_of_an_unused_home_is_empty(self):
        state = slots.live_state(self.home, "pytest", 16)
        self.assertEqual((state.held, state.waiting, state.busy), (0, 0, 0))


class SlotHistoryTests(Base):
    """Every acquire, release, timeout and reap appends one row; ``slots --history`` aggregates them."""

    def event(self, ts, **kw):
        row = {"kind": "pytest", "label": "ws1", "pid": 1, "wait_ms": 0, "hold_ms": None,
               "outcome": "acquired", "argv": "tests/test_a.py"}
        row.update(kw)
        slots.record_event(self.home, clock=lambda: ts, **row)

    def test_acquire_and_release_write_one_row_each(self):
        holder = slots.acquire("pytest", limit=2, home=self.home, label="ws1", max_wait_s=0,
                               command="pytest -q -k slow tests/test_a.py tests/test_b.py --maxfail=1")
        holder.release()
        rows = slots.read_events(self.home)
        self.assertEqual([row["outcome"] for row in rows], ["acquired", "released"])
        self.assertEqual(rows[0]["label"], "ws1")
        self.assertEqual(rows[0]["argv"], "tests/test_a.py tests/test_b.py")
        self.assertIsNone(rows[0]["hold_ms"])
        self.assertIsInstance(rows[1]["hold_ms"], int)
        self.assertEqual(rows[1]["argv"], rows[0]["argv"])
        self.assertEqual(rows[1]["pid"], os.getpid())

    def test_an_ssh_slot_records_its_kind(self):
        holder = slots.acquire("ssh", limit=2, home=self.home, label="ws1", max_wait_s=0,
                               command="ssh prod-box uptime")
        holder.release()
        self.assertEqual([row["kind"] for row in slots.read_events(self.home)], ["ssh", "ssh"])

    def test_test_paths_keeps_only_test_files(self):
        self.assertEqual(slots.test_paths("pytest -q -k slow tests/test_a.py tests/test_b.py::test_x --maxfail=1"),
                         "tests/test_a.py tests/test_b.py::test_x")
        self.assertEqual(slots.test_paths("pytest -q"), "")
        self.assertEqual(slots.test_paths("pytest " + " ".join(f"tests/test_{i}.py" for i in range(6))),
                         "tests/test_0.py tests/test_1.py tests/test_2.py")

    def test_history_aggregates_per_label(self):
        self.event(1000.0, label="a", wait_ms=0)
        self.event(1001.0, label="a", outcome="released", hold_ms=60000)
        self.event(1002.0, label="a", wait_ms=2000)
        self.event(1003.0, label="a", outcome="released", hold_ms=120000)
        self.event(1004.0, label="b", wait_ms=500)
        self.event(1005.0, label="b", outcome="released", hold_ms=30000)
        report = slots.history(self.home, since_s=100.0, now=1100.0)
        by_label = {entry["label"]: entry for entry in report["labels"]}
        self.assertEqual(by_label["a"]["runs"], 2)
        self.assertEqual(by_label["a"]["hold_ms"], 180000)
        self.assertEqual(by_label["a"]["wait_ms"], 2000)
        self.assertEqual(by_label["a"]["wait_p50_ms"], 0)
        self.assertEqual(by_label["a"]["wait_p95_ms"], 2000)
        self.assertEqual(by_label["b"]["runs"], 1)
        self.assertEqual(report["totals"]["runs"], 3)
        self.assertEqual(report["totals"]["hold_ms"], 210000)
        self.assertEqual(report["since"], 1000.0)

    def test_history_counts_an_identical_rerun_with_no_release_in_between(self):
        self.event(1000.0, argv="tests/test_a.py")
        self.event(1001.0, argv="tests/test_a.py")
        self.event(1002.0, outcome="released", hold_ms=1000, argv="tests/test_a.py")
        self.event(1003.0, argv="tests/test_a.py")
        self.event(1004.0, argv="tests/test_b.py")
        report = slots.history(self.home, since_s=100.0, now=1100.0)
        self.assertEqual(report["labels"][0]["reruns"], 1)

    def test_history_skips_rows_outside_the_window(self):
        self.event(100.0, label="old")
        self.event(1000.0, label="new")
        report = slots.history(self.home, since_s=100.0, now=1100.0)
        self.assertEqual([entry["label"] for entry in report["labels"]], ["new"])

    def test_history_of_an_empty_home_is_empty(self):
        report = slots.history(self.home, since_s=100.0, now=1100.0)
        self.assertEqual((report["labels"], report["totals"]["runs"]), ([], 0))

    def test_the_events_file_is_rotated_when_it_grows_past_the_cap(self):
        path = slots.events_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * (slots.EVENTS_MAX_BYTES + 1), encoding="utf-8")
        self.event(1000.0)
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_slots_history_command_prints_totals_and_json(self):
        now = time.time()
        self.event(now - 60, label="ws1", wait_ms=4000)
        self.event(now - 30, label="ws1", outcome="released", hold_ms=60000)
        out = io.StringIO()
        code = cli.main(["slots", "--history", "--since", "6h"],
                        env={"AGENTQ_HOME": str(self.home)}, out=out)
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("ws1", text)
        self.assertIn("1 slot run(s) over 6h", text)
        self.assertTrue(text.splitlines()[-1].startswith("ANSWER: "))
        out = io.StringIO()
        code = cli.main(["slots", "--history", "--json"], env={"AGENTQ_HOME": str(self.home)}, out=out)
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue().rsplit("ANSWER:", 1)[0])
        self.assertEqual(payload["totals"]["runs"], 1)
        self.assertEqual(payload["labels"][0]["label"], "ws1")

    def test_slots_history_rejects_a_bad_since(self):
        out = io.StringIO()
        code = cli.main(["slots", "--history", "--since", "soon"],
                        env={"AGENTQ_HOME": str(self.home)}, out=out)
        self.assertEqual(code, 1)
        self.assertIn("not a duration", out.getvalue())


class StatusHintTests(Base):
    """``agentq status`` prints one slot line: utilization, median wait, and a cap hint."""

    def event(self, ts, **kw):
        row = {"kind": "pytest", "label": "a", "pid": 1, "wait_ms": 0, "hold_ms": None,
               "outcome": "acquired", "argv": "tests/test_a.py"}
        row.update(kw)
        slots.record_event(self.home, clock=lambda: ts, **row)

    def test_no_events_says_no_waits(self):
        self.assertIn("no waits recorded", slots.status_hint(self.home, now=1000.0))

    def test_low_waits_keep_the_cap(self):
        self.event(900.0, wait_ms=1000)
        text = slots.status_hint(self.home, now=1000.0, gauges=FakeGauges(free=90.0, cpu=0.2))
        self.assertIn("keep slots.tests at 16", text)

    def test_high_waits_with_headroom_recommend_a_higher_cap(self):
        self.event(900.0, wait_ms=120000)
        text = slots.status_hint(self.home, now=1000.0, gauges=FakeGauges(free=90.0, cpu=0.2))
        self.assertIn("raise slots.tests to 20", text)

    def test_high_waits_without_headroom_keep_the_cap(self):
        self.event(900.0, wait_ms=120000)
        text = slots.status_hint(self.home, now=1000.0, gauges=FakeGauges(free=10.0, cpu=0.2))
        self.assertIn("keep slots.tests at 16 (memory free 10% < 35%)", text)

    def test_the_recommendation_never_passes_the_tests_ceiling(self):
        (self.home / "broker.json").write_text(json.dumps({"slots": {"light": 17}}), encoding="utf-8")
        self.event(900.0, wait_ms=120000)
        text = slots.status_hint(self.home, now=1000.0, gauges=FakeGauges(free=90.0, cpu=0.2))
        self.assertIn("raise slots.tests to 17", text)

    def test_utilization_is_held_time_over_capacity(self):
        self.event(900.0, outcome="released", hold_ms=28800000)
        text = slots.status_hint(self.home, now=1000.0)
        self.assertIn("pytest 50% utilized over 1.0h (cap 16)", text)

    def test_status_prints_the_slot_line(self):
        self.event(time.time() - 60, wait_ms=1000)
        out = io.StringIO()
        cli.main(["status"], env={"AGENTQ_HOME": str(self.home)}, out=out)
        self.assertIn("slots: pytest", out.getvalue())


class ShellFastPathTests(Base):
    """The sh fast path: only pytest argv reaches dispatch.py, and ssh reaches it only for listed hosts."""

    def copied_shims(self):
        folder = self.bin / "shims-copy"
        folder.mkdir()
        for name in ("python", "python3", "uv", "ssh", "pytest", "sleep", "shim-lib.sh"):
            shutil.copy(SHIMS / name, folder / name)
        marker = self.home / "dispatch-ran"
        (folder / "dispatch.py").write_text(
            "import sys\nopen(%r, 'a').write(repr(sys.argv[1:]) + chr(10))\n" % str(marker), encoding="utf-8")
        return folder, marker

    def run_shim(self, folder, name, args, extra=None):
        env = self.shim_env(extra)
        env["AGENTQ_REFUSED_SLEEP_PAUSE_S"] = "0"
        env["PATH"] = os.pathsep.join([str(folder), str(self.bin), "/usr/bin", "/bin"])
        return subprocess.run([str(folder / name), *args], env=env, capture_output=True, text=True, timeout=60)

    def test_non_pytest_python_never_runs_dispatch(self):
        folder, marker = self.copied_shims()
        record = self.bin / "argv"
        self.fake_binary("python3", f'printf "%s\\n" "$@" > "{record}"\nexit 4\n')
        result = self.run_shim(folder, "python3", ["-c", "pass"])
        self.assertEqual(result.returncode, 4)
        self.assertEqual(record.read_text().split(), ["-c", "pass"])
        self.assertFalse(marker.exists())

    def test_pytest_words_anywhere_in_argv_reach_dispatch(self):
        folder, marker = self.copied_shims()
        for name, args in (("python3", ["-m", "pytest", "x.py"]), ("python", ["-m", "pytest"]),
                           ("uv", ["run", "pytest", "-q"]), ("pytest", ["-q"])):
            with self.subTest(name=name, args=args):
                marker.unlink(missing_ok=True)
                self.run_shim(folder, name, args)
                self.assertTrue(marker.exists())
        marker.unlink(missing_ok=True)
        self.run_shim(folder, "uv", ["sync"])
        self.assertFalse(marker.exists())

    def test_guard_words_reach_dispatch_and_other_argv_does_not(self):
        folder, marker = self.copied_shims()
        for name, args in (("uv", ["run", "pyright"]), ("uv", ["run", "scripts/lor-ci-local.sh"]),
                           ("python3", ["scripts/validate.sh"]), ("uv", ["run", "pre-commit", "run", "-a"]),
                           ("uv", ["run", "pyright", "a.py"]), ("python3", ["scripts/select_pytests.py", "--all"]),
                           ("sleep", ["600"]), ("sleep", ["2m"])):
            with self.subTest(name=name, args=args):
                marker.unlink(missing_ok=True)
                self.run_shim(folder, name, args)
                self.assertTrue(marker.exists())
        for name, args in (("uv", ["run", "ruff", "check"]), ("python3", ["scripts/tool.py", "validate"]),
                           ("sleep", ["0"]), ("sleep", ["0", "0"])):
            with self.subTest(name=name, args=args):
                marker.unlink(missing_ok=True)
                self.run_shim(folder, name, args)
                self.assertFalse(marker.exists())

    def test_shell_shim_refuses_to_recurse_into_itself(self):
        folder, _ = self.copied_shims()
        os.symlink(folder / "python3", self.bin / "python3")
        result = subprocess.run([str(folder / "python3"), "-c", "pass"],
                                env=self.shim_env({"PATH": os.pathsep.join([str(folder), str(self.bin)])}),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 127)
        self.assertIn("refusing to recurse", result.stderr)

    def test_shell_shim_without_a_real_binary_fails_cleanly(self):
        folder, _ = self.copied_shims()
        result = subprocess.run([str(folder / "python3"), "-c", "pass"],
                                env={"PATH": str(folder), "AGENTQ_HOME": str(self.home)},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 127)
        self.assertIn("no real `python3`", result.stderr)

    def test_ssh_fast_path_sends_only_listed_hosts_to_dispatch(self):
        folder, marker = self.copied_shims()
        hosts = self.home / "slots" / "ssh_hosts"
        hosts.parent.mkdir(parents=True, exist_ok=True)
        hosts.write_text("prod-box\nbox-*\n", encoding="utf-8")
        record = self.bin / "ssh-argv"
        self.fake_binary("ssh", f'printf "%s\\n" "$@" > "{record}"\n')
        for args in (["github.com"], ["-T", "git@github.com", "git-upload-pack"]):
            with self.subTest(args=args):
                marker.unlink(missing_ok=True)
                self.assertEqual(self.run_shim(folder, "ssh", args).returncode, 0)
                self.assertFalse(marker.exists())
                self.assertEqual(record.read_text().split(), args)
        marker.unlink(missing_ok=True)
        self.run_shim(folder, "ssh", ["-p", "22", "evan@prod-box", "uptime"])
        self.assertTrue(marker.exists())
        marker.unlink(missing_ok=True)
        self.run_shim(folder, "ssh", ["Box-7"])
        self.assertTrue(marker.exists())

    def test_empty_ssh_hosts_file_execs_real_ssh_for_everything(self):
        folder, marker = self.copied_shims()
        hosts = self.home / "slots" / "ssh_hosts"
        hosts.parent.mkdir(parents=True, exist_ok=True)
        hosts.write_text("", encoding="utf-8")
        self.fake_binary("ssh", "exit 0\n")
        self.assertEqual(self.run_shim(folder, "ssh", ["prod-box"]).returncode, 0)
        self.assertFalse(marker.exists())


class ShimRecursionTests(Base):
    """A second copy of the shims on PATH must never become the "real" binary.

    On 2026-11-20 a worker put a second shim folder on a PATH that already had
    the installed shims, and an exhausted sleep budget made each shim run the
    other as its "real" sleep: 5795 processes in under two minutes.
    """

    def test_shell_shim_skips_a_second_shim_folder(self):
        second = self.second_shims()
        record = self.bin / "slept"
        self.fake_binary("sleep", f'printf "%s\\n" "$@" > "{record}"\n')
        env = self.shim_env({"PATH": os.pathsep.join([str(SHIMS), str(second), str(self.bin),
                                                       "/usr/bin", "/bin"])})
        result = run_bounded([str(SHIMS / "sleep"), "1"], env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record.read_text().split(), ["1"])

    def test_exhausted_budget_with_two_shim_folders_leaves_no_child_chain(self):
        second = self.second_shims()
        (self.home / "guard").mkdir(parents=True, exist_ok=True)
        (self.home / "guard" / "sleep-bomb").write_text("1800\n", encoding="utf-8")
        env = self.shim_env({
            "PATH": os.pathsep.join([str(SHIMS), str(second), str(self.bin), "/usr/bin", "/bin"]),
            "AGENTQ_JOB": "bomb",
            "AGENTQ_REFUSED_SLEEP_PAUSE_S": "0",
        })
        result = run_bounded([str(SHIMS / "sleep"), "2"], env, timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("sleep budget used", result.stderr)
        self.assertEqual(shim_processes(str(second)), [])
        self.assertEqual(shim_processes(str(SHIMS)), [])

    def test_depth_past_two_is_refused(self):
        env = self.shim_env({"AGENTQ_SHIM_DEPTH": "2"})
        result = run_bounded([str(SHIMS / "sleep"), "0"], env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("shim depth 3", result.stderr)

    def test_depth_one_still_runs(self):
        record = self.bin / "slept"
        self.fake_binary("sleep", f'printf "%s\\n" "$@" > "{record}"\n')
        env = self.shim_env({"AGENTQ_SHIM_DEPTH": "1"})
        result = run_bounded([str(SHIMS / "sleep"), "0"], env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record.read_text().split(), ["0"])


class SshHostsFileTests(Base):
    def test_file_lists_configured_patterns_lowercased(self):
        (self.home / "broker.json").write_text(json.dumps({"shims": {"ssh_gated_hosts": ["Prod-Box", "box-*"]}}),
                                               encoding="utf-8")
        path = slots.write_ssh_hosts(self.home)
        self.assertEqual(path.read_text(), "prod-box\nbox-*\n")

    def test_file_is_empty_without_configuration(self):
        self.assertEqual(slots.write_ssh_hosts(self.home).read_text(), "")


class GuardDecisionTests(unittest.TestCase):
    """The guard's refusal decisions for each argv shape, with no process started."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "tests"
        self.dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def assertRefused(self, name, args, fragment):
        found = dispatch.refusal(name, list(args), {})
        self.assertIsNotNone(found, f"{name} {args} should be refused")
        self.assertIn(fragment, found[0])
        self.assertTrue(found[1].startswith("BLOCKED: agentq guard: "), found[1])

    def assertAllowed(self, name, args):
        self.assertIsNone(dispatch.refusal(name, list(args), {}), f"{name} {args} should pass")

    def test_pytest_without_a_test_target_is_refused(self):
        self.assertRefused("pytest", [], "no test target")
        self.assertRefused("pytest", ["-q"], "no test target")
        self.assertRefused("pytest", ["-k", "test_a"], "no test target")
        self.assertRefused("python3", ["-m", "pytest"], "no test target")
        self.assertRefused("uv", ["run", "pytest", "-q"], "no test target")
        self.assertRefused("uv", ["run", "python", "-m", "pytest"], "no test target")

    def test_directory_targets_are_refused_even_with_a_filter(self):
        self.assertRefused("pytest", ["-k", "test_a", str(self.dir)], "is a directory")
        self.assertRefused("pytest", ["tests/"], "is a directory")
        self.assertRefused("python3", ["-m", "pytest", "-m", "unit", str(self.dir)], "is a directory")

    def test_more_than_25_test_files_are_refused(self):
        self.assertRefused("pytest", [f"t{i}.py" for i in range(26)], "names 26 test files")
        self.assertAllowed("pytest", [f"t{i}.py" for i in range(25)])

    def test_explicit_test_targets_pass(self):
        self.assertAllowed("pytest", ["tests/test_a.py"])
        self.assertAllowed("pytest", ["tests/test_a.py::test_x"])
        self.assertAllowed("pytest", ["-q", "a.py", "b.py"])
        self.assertAllowed("pytest", ["-k", "tests", "a.py"])
        self.assertAllowed("python3", ["-m", "pytest", "tests/test_a.py::TestA::test_b"])
        self.assertAllowed("uv", ["run", "pytest", "tests/test_a.py"])
        self.assertAllowed("uv", ["run", "--no-sync", "python", "-m", "pytest", "tests/test_a.py"])

    def test_full_gates_are_refused_through_uv_and_python(self):
        self.assertRefused("uv", ["run", "scripts/lor-ci-local.sh"], "full gate lor-ci-local.sh")
        self.assertRefused("uv", ["run", "scripts/ci-local.sh"], "full gate ci-local.sh")
        self.assertRefused("python3", ["scripts/validate.sh"], "full gate validate.sh")
        self.assertRefused("uv", ["run", "--no-sync", "scripts/validate.sh"], "full gate validate.sh")
        self.assertRefused("uv", ["run", "pre-commit", "run", "--all-files"], "whole-repo")
        self.assertRefused("uv", ["run", "pre-commit", "run", "-a"], "whole-repo")
        self.assertRefused("python3", ["scripts/select_pytests.py", "--all"], "select_pytests.py --all")

    def test_pyright_without_file_arguments_is_refused(self):
        self.assertRefused("uv", ["run", "pyright"], "whole repo")
        self.assertRefused("uv", ["run", "pyright", "--outputjson"], "whole repo")
        self.assertRefused("uv", ["run", "pyright", "-p", "pyproject.toml"], "whole repo")

    def test_targeted_pyright_pre_commit_and_selection_pass(self):
        self.assertAllowed("uv", ["run", "pyright", "a.py"])
        self.assertAllowed("uv", ["run", "pyright", "--outputjson", "a.py"])
        self.assertAllowed("uv", ["run", "pyright", "-p", "pyproject.toml", "a.py"])
        self.assertAllowed("uv", ["run", "pre-commit", "run"])
        self.assertAllowed("python3", ["scripts/select_pytests.py", "tests/x.py"])

    def test_non_python_shims_never_match_gate_words(self):
        self.assertAllowed("ssh", ["prod-box", "pyright"])

    def test_sleep_up_to_120_seconds_passes(self):
        self.assertAllowed("sleep", ["5"])
        self.assertAllowed("sleep", ["120"])
        self.assertAllowed("sleep", ["2m"])
        self.assertAllowed("sleep", ["--help"])

    def test_sleep_over_120_seconds_is_refused_with_the_babysitting_message(self):
        found = dispatch.refusal("sleep", ["121"], {})
        self.assertEqual(found[0], "sleep 121 s refused")
        self.assertEqual(
            found[1],
            "BLOCKED: agentq guard: sleep 121 s refused; workers never babysit. Launch long work detached, "
            "write a status file, and return its PID and paths in your report.",
        )
        self.assertIn("sleep 180 s refused", dispatch.refusal("sleep", ["3m"], {})[0])

    def test_refusal_message_ends_with_the_scoped_command(self):
        found = dispatch.refusal("pytest", [], {})
        self.assertTrue(found[1].endswith(
            "Run only the test files for the code you changed: pytest <file.py> [...]"), found[1])

    def test_agentq_guard_zero_disables_every_refusal(self):
        env = {"AGENTQ_GUARD": "0"}
        self.assertIsNone(dispatch.refusal("pytest", [], env))
        self.assertIsNone(dispatch.refusal("uv", ["run", "pyright"], env))
        self.assertIsNone(dispatch.refusal("sleep", ["300"], env))
        self.assertIsNone(dispatch.refusal("sleep", ["1"], dict(env, AGENTQ_JOB="j", AGENTQ_SLEEP_BUDGET_S="0")))

    def test_sleep_budget_counts_fractional_sleeps_rounded_up(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"AGENTQ_HOME": home, "AGENTQ_JOB": "j2", "AGENTQ_SLEEP_BUDGET_S": "2"}
            self.assertIsNone(dispatch.refusal("sleep", ["0.5"], env))
            dispatch.record_sleep(home, env, ["0.5"])
            self.assertEqual((Path(home) / "guard" / "sleep-j2").read_text().strip(), "1")
            self.assertIsNone(dispatch.refusal("sleep", ["1"], env))
            dispatch.record_sleep(home, env, ["1"])
            self.assertEqual(dispatch.refusal("sleep", ["0.1"], env)[0], "sleep budget used (3 s of 2 s)")

    def test_sleep_budget_needs_a_job_label(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"AGENTQ_HOME": home, "AGENTQ_SLEEP_BUDGET_S": "0"}
            self.assertIsNone(dispatch.refusal("sleep", ["1"], env))
            dispatch.record_sleep(home, env, ["1"])
            self.assertFalse((Path(home) / "guard").exists())


class GuardShimTests(Base):
    """End to end through the real shims: a refusal exits 2, logs one line, and never runs the command."""

    def setUp(self):
        super().setUp()
        self.work = Path(self._tmp.name) / "work"
        (self.work / "tests").mkdir(parents=True)
        self.log = self.home / dispatch.GUARD_LOG
        self.user_home = Path(self._tmp.name) / "userhome"
        (self.user_home / "Documents" / "worktrees" / "repo-a" / "src").mkdir(parents=True)
        (self.user_home / "Documents" / "projects").mkdir(parents=True)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / "transform" / "scripts").mkdir(parents=True)
        for name in ("pytest", "python3", "uv", "sleep", "grep", "rg", "find"):
            self.fake_binary(name, f'echo "$@" >> "{self.bin / (name + ".ran")}"\n')

    def run_shim(self, name, args, extra=None, cwd=None):
        return subprocess.run([str(SHIMS / name), *args], cwd=cwd or self.work, env=self.shim_env(extra),
                              capture_output=True, text=True, timeout=60)

    def repo_real(self):
        return os.path.realpath(self.repo)

    def ran(self, name):
        return (self.bin / (name + ".ran")).exists()

    def log_lines(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def test_bare_pytest_is_refused_and_logged(self):
        result = self.run_shim("pytest", ["-q"], {"AGENTQ_JOB": "ws9-exec-x"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("BLOCKED: agentq guard: pytest has no test target", result.stderr)
        self.assertFalse(self.ran("pytest"))
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        epoch, job, reason, command = lines[0].split("\t")
        self.assertTrue(epoch.isdigit())
        self.assertEqual(job, "ws9-exec-x")
        self.assertIn("no test target", reason)
        self.assertEqual(command, "pytest -q")

    def test_python_directory_target_is_refused(self):
        result = self.run_shim("python3", ["-m", "pytest", "tests"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("is a directory", result.stderr)
        self.assertFalse(self.ran("python3"))

    def test_uv_full_gate_is_refused(self):
        result = self.run_shim("uv", ["run", "scripts/lor-ci-local.sh"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("BLOCKED: agentq guard: full gate lor-ci-local.sh", result.stderr)
        self.assertFalse(self.ran("uv"))

    def test_uv_pyright_without_files_is_refused(self):
        result = self.run_shim("uv", ["run", "pyright"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("pyright with no file arguments", result.stderr)

    def test_long_sleep_is_refused_and_logged_with_its_label(self):
        result = self.run_shim("sleep", ["300"], {"AGENTQ_JOB": "ws9-exec-x"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("BLOCKED: agentq guard: sleep 300 s refused; workers never babysit.", result.stderr)
        self.assertFalse(self.ran("sleep"))
        self.assertEqual(self.log_lines()[0].split("\t")[1], "ws9-exec-x")

    def test_short_sleep_execs_the_real_sleep(self):
        result = self.run_shim("sleep", ["5"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.bin / "sleep.ran").read_text().split(), ["5"])
        self.assertEqual(self.log_lines(), [])

    def test_targeted_runs_pass_through_to_the_real_command(self):
        self.assertEqual(self.run_shim("python3", ["-m", "pytest", "tests/test_a.py::test_x"]).returncode, 0)
        self.assertEqual(self.run_shim("uv", ["run", "pyright", "a.py"]).returncode, 0)
        self.assertTrue(self.ran("python3") and self.ran("uv"))
        self.assertEqual(self.log_lines(), [])

    def test_agentq_guard_zero_runs_the_refused_command(self):
        result = self.run_shim("pytest", [], {"AGENTQ_GUARD": "0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.ran("pytest"))
        self.assertEqual(self.log_lines(), [])

    def test_a_failed_log_write_never_fails_the_refusal(self):
        self.log.mkdir()
        result = self.run_shim("sleep", ["300"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("BLOCKED:", result.stderr)

    def test_logged_command_is_truncated_to_300_characters(self):
        self.run_shim("pytest", ["-k", "x" * 400])
        command = self.log_lines()[0].split("\t")[3]
        self.assertEqual(len(command), 300)
        self.assertTrue(command.startswith("pytest -k xxx"))

    def test_each_refusal_appends_one_line(self):
        self.run_shim("sleep", ["300"])
        self.run_shim("pytest", [])
        self.assertEqual(len(self.log_lines()), 2)

    def test_recursive_grep_forms_over_a_repo_root_are_refused_and_logged(self):
        forms = (["-r", "x", "."], ["-rn", "x", "."], ["-rln", "x"], ["-Rni", "x", "."],
                 ["--recursive", "x"], ["--dereference-recursive", "x", "."],
                 ["-d", "recurse", "x", "."], ["--directories=recurse", "x"], ["-drecurse", "x"])
        for args in forms:
            with self.subTest(args=args):
                before = len(self.log_lines())
                result = self.run_shim("grep", args, {"AGENTQ_JOB": "ws9-exec-x"}, cwd=self.repo)
                self.assertEqual(result.returncode, 2)
                self.assertIn(f"BLOCKED: agentq guard: recursive grep over {self.repo_real()} refused; use rg "
                              "(skips .gitignore'd dirs like .venv and data) or git grep, or grep a specific "
                              "subfolder", result.stderr)
                self.assertEqual(len(self.log_lines()), before + 1)
                self.assertEqual(self.log_lines()[-1].split("\t")[1], "ws9-exec-x")
        self.assertFalse(self.ran("grep"))
        self.assertEqual(self.log_lines()[-1].split("\t")[2], f"recursive grep over {self.repo_real()} refused")

    def test_recursive_grep_over_the_repo_root_path_is_refused(self):
        result = self.run_shim("grep", ["-rn", "x", str(self.repo)])
        self.assertEqual(result.returncode, 2)
        self.assertIn("recursive grep over ", result.stderr)

    def test_recursive_grep_over_home_is_refused(self):
        result = self.run_shim("grep", ["-r", "x"], {"HOME": str(self.user_home)}, cwd=self.user_home)
        self.assertEqual(result.returncode, 2)
        self.assertIn("recursive grep over ", result.stderr)
        self.assertFalse(self.ran("grep"))

    def test_recursive_grep_on_a_subfolder_or_file_of_the_repo_passes(self):
        scripts = self.repo / "transform" / "scripts"
        (scripts / "run.sh").write_text("--full-refresh\n", encoding="utf-8")
        self.assertEqual(self.run_shim("grep", ["-rn", "--include=*.sh", "--", "--full-refresh", str(scripts)],
                                       cwd=self.repo).returncode, 0)
        self.assertEqual(self.run_shim("grep", ["-rl", ".", "transform/scripts"], cwd=self.repo).returncode, 0)
        self.assertEqual(self.run_shim("grep", ["-r", "x", str(scripts / "run.sh")], cwd=self.repo).returncode, 0)
        self.assertEqual(len((self.bin / "grep.ran").read_text().splitlines()), 3)
        self.assertEqual(self.log_lines(), [])

    def test_plain_grep_and_option_values_that_look_like_r_pass(self):
        for args in (["-n", "x", "file.txt"], ["-e", "-r", "file.txt"], ["--", "-r", "file.txt"], ["-i", "--", "-R"]):
            with self.subTest(args=args):
                self.assertEqual(self.run_shim("grep", args).returncode, 0)
        self.assertEqual(len((self.bin / "grep.ran").read_text().splitlines()), 4)
        self.assertEqual(self.log_lines(), [])

    def test_rg_and_find_over_broad_roots_are_refused(self):
        home = self.user_home
        env = {"HOME": str(home)}
        cases = (
            ("rg", ["x", str(home)]),
            ("rg", ["x", str(home / "Documents" / "worktrees")]),
            ("rg", ["x", str(home / "Documents" / "worktrees" / "repo-a")]),
            ("rg", ["x", "/"]),
            ("find", [str(home), "-name", "x"]),
            ("find", ["-H", str(home / "Documents" / "projects"), "-name", "x"]),
            ("find", ["/", "-name", "x"]),
        )
        for name, args in cases:
            with self.subTest(name=name, args=args):
                result = self.run_shim(name, args, env)
                self.assertEqual(result.returncode, 2)
                self.assertIn(f"BLOCKED: agentq guard: {name} over ", result.stderr)
                self.assertIn("refused; search inside your own worktree or a specific folder", result.stderr)
                self.assertFalse(self.ran(name))
        self.assertEqual(len(self.log_lines()), len(cases))

    def test_rg_without_a_path_refuses_when_run_from_the_home_folder(self):
        result = self.run_shim("rg", ["x"], {"HOME": str(self.user_home)}, cwd=self.user_home)
        self.assertEqual(result.returncode, 2)
        self.assertIn("BLOCKED: agentq guard: rg over ", result.stderr)
        self.assertFalse(self.ran("rg"))

    def test_rg_and_find_in_a_subfolder_pass(self):
        src = self.user_home / "Documents" / "worktrees" / "repo-a" / "src"
        env = {"HOME": str(self.user_home)}
        self.assertEqual(self.run_shim("rg", ["needle", str(src)], env).returncode, 0)
        self.assertEqual(self.run_shim("find", [str(src), "-name", "x"], env).returncode, 0)
        self.assertEqual(self.run_shim("rg", ["needle"], env, cwd=src).returncode, 0)
        self.assertEqual(self.run_shim("rg", ["needle"], env).returncode, 0)
        self.assertEqual(len((self.bin / "rg.ran").read_text().splitlines()), 3)
        self.assertEqual(self.log_lines(), [])

    def test_agentq_guard_zero_runs_broad_searches_and_recursive_grep(self):
        env = {"AGENTQ_GUARD": "0", "HOME": str(self.user_home)}
        for name, args, cwd in (("rg", ["x", "/"], None), ("find", ["/", "-name", "x"], None),
                                ("grep", ["-r", "x", "."], self.repo)):
            with self.subTest(name=name):
                result = self.run_shim(name, args, env, cwd=cwd)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(self.ran(name))
        self.assertEqual(self.log_lines(), [])

    def test_sleep_budget_refuses_once_the_job_total_would_pass_it(self):
        extra = {"AGENTQ_JOB": "ws9-exec-x", "AGENTQ_SLEEP_BUDGET_S": "2"}
        self.assertEqual(self.run_shim("sleep", ["1"], extra).returncode, 0)
        self.assertEqual(self.run_shim("sleep", ["1"], extra).returncode, 0)
        result = self.run_shim("sleep", ["1"], extra)
        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "BLOCKED: agentq guard: sleep budget used (3 s of 2 s); workers never babysit. Launch long work "
            "detached, write a status file, and report its PID and paths.", result.stderr)
        self.assertEqual((self.bin / "sleep.ran").read_text().splitlines(), ["1", "1"])
        self.assertEqual((self.home / "guard" / "sleep-ws9-exec-x").read_text().strip(), "2")
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].split("\t")[1], "ws9-exec-x")

    def test_fractional_sleep_goes_through_the_same_budget(self):
        extra = {"AGENTQ_JOB": "ws9-exec-x", "AGENTQ_SLEEP_BUDGET_S": "0"}
        result = self.run_shim("sleep", ["0.5"], extra)
        self.assertEqual(result.returncode, 2)
        self.assertIn("sleep budget used (1 s of 0 s)", result.stderr)
        self.assertFalse(self.ran("sleep"))

    def test_sleep_budget_does_nothing_without_a_job(self):
        for _ in range(3):
            self.assertEqual(self.run_shim("sleep", ["1"], {"AGENTQ_SLEEP_BUDGET_S": "2"}).returncode, 0)
        self.assertEqual(len((self.bin / "sleep.ran").read_text().splitlines()), 3)
        self.assertFalse((self.home / "guard").exists())


if __name__ == "__main__":
    unittest.main()


class KillShimTests(unittest.TestCase):
    """pkill and killall are refused for workers and logged."""

    def run_kill_shim(self, name, args, env_extra=None):
        home = Path(tempfile.mkdtemp())
        env = dict(os.environ, AGENTQ_HOME=str(home))
        env.update(env_extra or {})
        shim = Path(__file__).resolve().parents[1] / "agentq" / "shims" / name
        result = subprocess.run(["sh", str(shim), *args], env=env, capture_output=True, text=True, timeout=30)
        return result, home / "guard_blocks.tsv"

    def test_pkill_and_killall_are_refused_and_logged(self):
        for name in ("pkill", "killall"):
            with self.subTest(name=name):
                result, log = self.run_kill_shim(name, ["-f", "x", "-P", "1"])
                self.assertEqual(result.returncode, 2)
                self.assertIn("BLOCKED: agentq guard", result.stderr)
                self.assertIn(f"{name} refused", log.read_text())


class RefusedSleepPaceTests(unittest.TestCase):
    """A refused sleep still waits a little, so polling loops cannot spin on the shim."""

    def test_refused_sleep_pauses_up_to_the_cap(self):
        start = time.monotonic()
        dispatch.pause_refused_sleep(["5"], {"AGENTQ_REFUSED_SLEEP_PAUSE_S": "0.3"})
        self.assertGreaterEqual(time.monotonic() - start, 0.25)

    def test_zero_cap_does_not_pause(self):
        start = time.monotonic()
        dispatch.pause_refused_sleep(["5"], {"AGENTQ_REFUSED_SLEEP_PAUSE_S": "0"})
        self.assertLess(time.monotonic() - start, 0.2)

    def test_budget_default_is_thirty_minutes(self):
        self.assertEqual(dispatch.SLEEP_BUDGET_DEFAULT_S, 1800)
