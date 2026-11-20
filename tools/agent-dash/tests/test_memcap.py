import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memcap.sh"
ALLOC_200MB = (
    "import time; b = bytearray(200 * 1024 * 1024)\n"
    "for i in range(0, len(b), 4096): b[i] = 1\n"
    "time.sleep(30)\n"
)


def memcap_env(poll_s="0.2", extra_env=None):
    env = dict(os.environ, MEMCAP_POLL_S=poll_s, AGENT_DASH_MEMCAP_RULES="/nonexistent/memcap-rules.sh")
    env.pop("MEMCAP_NO_SETPGRP", None)
    if extra_env:
        env.update(extra_env)
    return env


def run_memcap(max_mb, command, poll_s="0.2", timeout=30, extra_env=None, new_session=False, stdin=None):
    started = time.monotonic()
    proc = subprocess.run(
        ["bash", str(SCRIPT), str(max_mb), *command],
        capture_output=True,
        text=True,
        env=memcap_env(poll_s, extra_env),
        timeout=timeout,
        start_new_session=new_session,
        input=stdin,
    )
    return proc, time.monotonic() - started


def alive(pid, wait_s=3.0):
    """True while pid is a live (non-zombie) process; waits up to wait_s for it to go."""
    deadline = time.monotonic() + wait_s
    while True:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        stat = out.stdout.strip()
        if not stat or stat.startswith("Z"):
            return False
        if time.monotonic() >= deadline:
            return True
        time.sleep(0.1)


class MemcapTest(unittest.TestCase):
    def test_kills_group_past_cap(self):
        proc, elapsed = run_memcap(50, [sys.executable, "-c", ALLOC_200MB])
        self.assertEqual(proc.returncode, 137, proc.stderr)
        self.assertLess(elapsed, 10)
        self.assertIn("killing process group", proc.stderr)

    def test_grandchild_counts_toward_cap(self):
        child = f"python3 -c '{ALLOC_200MB}'; sleep 30"
        proc, elapsed = run_memcap(50, ["sh", "-c", child])
        self.assertEqual(proc.returncode, 137, proc.stderr)
        self.assertLess(elapsed, 10)

    def test_child_under_cap_returns_its_status(self):
        proc, _ = run_memcap(50, ["sh", "-c", "echo hi; exit 3"])
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("hi", proc.stdout)

    def test_poll_interval_env_is_honoured(self):
        proc, elapsed = run_memcap(50, ["sh", "-c", "exit 0"], poll_s="0.2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 1)

    def test_shell_builtin_still_runs(self):
        """The group leader execs `bash -c '"$@"'`, so builtins keep working."""
        proc, _ = run_memcap(64, ["echo", "ran"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ran", proc.stdout)

    def test_stdin_reaches_the_child(self):
        proc, _ = run_memcap(64, ["sh", "-c", "read x; echo got=$x"], stdin="hello\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("got=hello", proc.stdout)

    def test_usage_without_a_command(self):
        proc = subprocess.run(["bash", str(SCRIPT), "64"], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("usage:", proc.stderr)

    def test_cap_kill_leaves_no_stray_processes(self):
        for extra in ({}, {"MEMCAP_NO_SETPGRP": "1"}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp:
                pidfile = Path(tmp) / "child.pid"
                child = f"echo $$ > {pidfile}; exec python3 -c '{ALLOC_200MB}'"
                proc, _ = run_memcap(50, ["sh", "-c", child], extra_env=extra)
                self.assertEqual(proc.returncode, 137, proc.stderr)
                pid = int(pidfile.read_text(encoding="utf-8").strip())
                self.assertFalse(alive(pid), f"child {pid} survived the cap kill with {extra}")

    def test_term_is_forwarded_to_the_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "child.pid"
            proc = subprocess.Popen(
                ["bash", str(SCRIPT), "512", "sh", "-c", f"echo $$ > {pidfile}; sleep 30"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=memcap_env(),
            )
            try:
                deadline = time.monotonic() + 10
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(pidfile.exists(), "child never started")
                child = int(pidfile.read_text(encoding="utf-8").strip())
                time.sleep(0.3)  # let memcap reach its poll loop
                proc.terminate()
                _, err = proc.communicate(timeout=20)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
            self.assertEqual(proc.returncode, 143, err)
            self.assertFalse(alive(child), f"child {child} survived the forwarded TERM")


class MemcapWithoutJobControlTest(unittest.TestCase):
    """The agentq pi worker condition: a session leader with no controlling terminal.

    There `set -m` cannot create the child's process group ("child setpgid ... Operation not
    permitted"), and the old script summed the caller's group, so the cap kill took out the
    caller's pipeline and memcap exited 143 with no output.
    """

    def test_cap_kill_under_a_new_session(self):
        proc, _ = run_memcap(50, ["python3", "-c", ALLOC_200MB], new_session=True, stdin="")
        self.assertEqual(proc.returncode, 137, proc.stderr)
        self.assertIn("killing process group", proc.stderr)

    def test_child_status_under_a_new_session(self):
        proc, _ = run_memcap(64, ["sh", "-c", "echo hi; exit 3"], new_session=True, stdin="")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("hi", proc.stdout)

    def test_poll_loop_does_not_use_path_sleep(self):
        """agentq's `sleep` shim can refuse a run; the poll loop must not fork-storm on it."""
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "sleep"
            fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            fake.chmod(0o755)
            proc, elapsed = run_memcap(
                50, ["python3", "-c", ALLOC_200MB], extra_env={"PATH": f"{tmp}:{os.environ['PATH']}"}
            )
            self.assertEqual(proc.returncode, 137, proc.stderr)
            self.assertLess(elapsed, 10)


class MemcapTreeFallbackTest(unittest.TestCase):
    """MEMCAP_NO_SETPGRP=1 is the refused-group path: RSS and kills follow the descendant tree."""

    def test_tree_fallback_kills_grandchildren(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "gc.pid"
            child = f"python3 -c '{ALLOC_200MB}' & echo $! > {pidfile}; wait"
            proc, elapsed = run_memcap(50, ["sh", "-c", child], extra_env={"MEMCAP_NO_SETPGRP": "1"})
            self.assertEqual(proc.returncode, 137, proc.stderr)
            self.assertLess(elapsed, 10)
            self.assertIn("killing process tree", proc.stderr)
            gc = int(pidfile.read_text(encoding="utf-8").strip())
            self.assertFalse(alive(gc), f"grandchild {gc} survived the cap kill")

    def test_tree_fallback_returns_child_status(self):
        proc, _ = run_memcap(64, ["sh", "-c", "echo hi; exit 3"], extra_env={"MEMCAP_NO_SETPGRP": "1"})
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("hi", proc.stdout)

    def test_tree_fallback_ignores_the_callers_group(self):
        """A big sibling in the caller's group must not count toward the cap."""
        script = (
            f"{sys.executable} -c '{ALLOC_200MB}' & hog=$!\n"
            f"bash {SCRIPT} 64 sh -c 'sleep 1; exit 0'\n"
            "rc=$?\n"
            "kill $hog 2>/dev/null\n"
            "exit $rc\n"
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=memcap_env(extra_env={"MEMCAP_NO_SETPGRP": "1"}),
            start_new_session=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
