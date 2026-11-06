"""worker-watch.sh stall signal, with fake runs only. No real agents.

A run is a python or sh process whose command line carries a unique marker
(PATTERN). Busy means a process in the run's tree burns CPU; idle means
nothing in the tree moves. The watcher uses STALL_S=3 and POLL_S=1 so the
tests finish in seconds.
"""
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
WATCH = HERE.parent / "scripts" / "worker-watch.sh"
LABEL = "ww-check"  # must not contain the marker, or pgrep matches the watcher


def kill_tree(pid):
    """Kill pid and its children by PID (never pkill -f)."""
    kids = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
    for kid in kids:
        try:
            os.kill(int(kid), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class WorkerWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name) / "wt"
        self.wt.mkdir()
        self.marker = "wwtest" + uuid.uuid4().hex
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            kill_tree(proc.pid)
            proc.wait()
        self.tmp.cleanup()

    def start_run(self, script):
        proc = subprocess.Popen(["sh", "-c", script, self.marker], stdout=subprocess.DEVNULL)
        self.procs.append(proc)
        return proc

    def watch(self, extra_env=None, timeout=60):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PATTERN": self.marker,
            "STALL_S": "3",
            "POLL_S": "1",
            "GRACE_S": "600",
            "MAX_H": "1",
        }
        env.update(extra_env or {})
        proc = subprocess.Popen(
            ["sh", str(WATCH), LABEL, str(self.wt)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = proc.communicate(timeout=timeout)
        return out.strip().splitlines(), err

    def test_idle_run_is_reported_stalled(self):
        # Nothing in the tree moves: the watcher must still say STALLED.
        self.start_run_py("import time; time.sleep(120)")
        lines, err = self.watch()
        self.assertEqual(len(lines), 1, (lines, err))
        self.assertIn("STALLED", lines[-1])

    def test_busy_child_process_keeps_the_watch_going(self):
        # The run is a shell whose child burns CPU for longer than STALL_S.
        # The watcher must keep watching, not report STALLED.
        script = "python3 -c 'while True: pass' & wait"
        proc = self.start_run(script)
        time.sleep(0.5)

        def finish_later():
            time.sleep(7)  # several STALL_S windows of CPU burn
            kill_tree(proc.pid)

        killer = threading.Thread(target=finish_later)
        killer.start()
        lines, err = self.watch(timeout=60)
        killer.join()
        self.assertNotIn("STALLED", "\n".join(lines), err)
        self.assertTrue(lines and "FINISHED" in lines[-1], (lines, err))

    def test_growing_stream_file_keeps_the_watch_going(self):
        # Idle process, but the stream file grows for a while, then stops.
        self.start_run_py("import time; time.sleep(120)")
        stream = Path(self.tmp.name) / "stream.log"
        stream.write_text("", encoding="utf-8")
        stop = threading.Event()

        def append():
            n = 0
            while not stop.is_set() and n < 12:
                with stream.open("a", encoding="utf-8") as fh:
                    fh.write("narration line\n")
                n += 1
                time.sleep(0.5)
            stop.set()

        writer = threading.Thread(target=append)
        writer.start()
        lines, err = self.watch(extra_env={"STREAM": str(stream)}, timeout=60)
        writer.join()
        self.assertEqual(len(lines), 1, (lines, err))
        self.assertIn("STALLED", lines[-1])

    def start_run_py(self, code):
        proc = subprocess.Popen(
            ["python3", "-c", code, self.marker], stdout=subprocess.DEVNULL
        )
        self.procs.append(proc)
        return proc


if __name__ == "__main__":
    unittest.main()
