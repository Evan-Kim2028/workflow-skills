import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memcap.sh"
ALLOC_200MB = (
    "import time; b = bytearray(200 * 1024 * 1024)\n"
    "for i in range(0, len(b), 4096): b[i] = 1\n"
    "time.sleep(30)\n"
)


def run_memcap(max_mb, command, poll_s="0.2", timeout=30):
    env = dict(os.environ, MEMCAP_POLL_S=poll_s, AGENT_DASH_MEMCAP_RULES="/nonexistent/memcap-rules.sh")
    started = time.monotonic()
    proc = subprocess.run(
        ["bash", str(SCRIPT), str(max_mb), *command],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    return proc, time.monotonic() - started


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


if __name__ == "__main__":
    unittest.main()
