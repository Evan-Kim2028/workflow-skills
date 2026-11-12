"""agentq broker and CLI: queue, lanes, slots, reconcile, cancel, concurrency.

Offline. AGENTQ_HOME is a temp folder in every test. A fake launcher stands in for
run_worker.py: it writes a pi-shaped report and starts a real short-lived process
(``sleep``) so pid and start-time checks run for real. The proxy is always stubbed.
"""

import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentq import broker as B  # noqa: E402
from agentq import cli, db  # noqa: E402


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeLauncher:
    """Writes ``pi/out-<label>.md`` and starts ``sleep <live_s>`` as the worker."""

    def __init__(self, live_s=0.0, report=True):
        self.live_s = live_s
        self.report = report
        self.calls = []
        self.procs = []

    def __call__(self, job, run_dir, env):
        self.calls.append({"id": job["id"], "attempt": job["attempt"], "env": dict(env)})
        pi = Path(run_dir) / "pi"
        pi.mkdir(parents=True, exist_ok=True)
        if self.report:
            (pi / f"out-{job['label']}.md").write_text("PASS\nall green\n", encoding="utf-8")
        proc = subprocess.Popen(["sleep", str(self.live_s)], start_new_session=True)
        if self.live_s == 0:
            proc.wait()
        self.procs.append(proc)
        (Path(run_dir) / f"{job['label']}.pid").write_text(str(proc.pid), encoding="utf-8")
        return proc.pid

    def close(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def dead_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def env_for(home, session="sess-a"):
    return {"AGENTQ_HOME": str(home), "AGENTQ_SESSION": session, "PATH": os.environ["PATH"]}


def write_prompt(tmp, name="prompt.md"):
    path = Path(tmp) / name
    path.write_text("Do the thing.\n", encoding="utf-8")
    return path


def submit(env, prompt, *args, now=None, label=None):
    out = io.StringIO()
    argv = ["submit", str(prompt), "--label", label or f"t-{time.monotonic_ns()}", *args]
    code = cli.main(argv, env=env, out=out, now=now or time.time)
    if code != 0:
        raise AssertionError(out.getvalue())
    return out.getvalue().splitlines()[0]


def run_cli(env, argv, now=None):
    out = io.StringIO()
    code = cli.main(argv, env=env, out=out, now=now or time.time)
    return code, out.getvalue()


def make_broker(home, launcher=None, clock=None, proxy=None):
    return B.Broker(home, launcher=launcher or FakeLauncher(), now=clock or time.time,
                    proxy=proxy or (lambda: None))


def write_config(home, slots=None, tiers=None):
    import json

    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    data = {}
    if slots is not None:
        data["slots"] = slots
    if tiers is not None:
        data["tiers"] = tiers
    (home / "broker.json").write_text(json.dumps(data), encoding="utf-8")


def set_running(home, job_id, *, pid=None, run_dir=None, attempt=0, pi_session=None):
    conn = db.connect(home)
    info = B.process_info(pid) if pid else None
    with db.tx(conn):
        db.update_job(
            conn, job_id, state="running", pid=pid,
            pgid=B.process_group(pid) if pid else None,
            pid_start=info[0] if info else None,
            run_dir=str(run_dir) if run_dir else None, attempt=attempt,
            pi_session=pi_session, started_at=0.0,
        )


def row(home, job_id):
    return db.get_job(db.connect(home), job_id)


class ProcessCleanup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.env = env_for(self.home)
        self.prompt = write_prompt(self.tmp)
        self.launchers = []

    def tearDown(self):
        for launcher in self.launchers:
            launcher.close()
        self._tmp.cleanup()

    def fake(self, **kw):
        launcher = FakeLauncher(**kw)
        self.launchers.append(launcher)
        return launcher


class SubmitWaitTests(ProcessCleanup):
    def test_round_trip_submit_wait_result(self):
        job_id = submit(self.env, self.prompt, label="t-round")
        broker = make_broker(self.home, self.fake(live_s=0.2))
        stop = threading.Event()

        def ticker():
            while not stop.is_set():
                broker.tick()
                time.sleep(0.05)

        thread = threading.Thread(target=ticker, daemon=True)
        thread.start()
        try:
            code, out = run_cli(self.env, ["wait", job_id, "--timeout", "15"])
        finally:
            stop.set()
            thread.join(5)
        self.assertEqual(code, 0, out)
        self.assertIn(f"{job_id} PASS", out)
        code, out = run_cli(self.env, ["result", job_id, "--print"])
        self.assertEqual(code, 0)
        self.assertIn("all green", out)
        code, out = run_cli(self.env, ["ls", "--json"])
        self.assertIn(job_id, out)
        self.assertEqual(row(self.home, job_id)["state"], "done")

    def test_defaults_and_review_routing(self):
        read_id = submit(self.env, self.prompt)
        review_id = submit(self.env, self.prompt, "--kind", "review")
        read_row = row(self.home, read_id)
        review_row = row(self.home, review_id)
        self.assertEqual(read_row["tier"], "pi-deepseek-flash")
        self.assertEqual(read_row["lane"], "normal")
        self.assertEqual(read_row["res"], "light")
        self.assertEqual(read_row["session"], "sess-a")
        self.assertEqual(review_row["tier"], "pi-kimi")
        self.assertTrue(Path(read_row["prompt_path"]).read_text(encoding="utf-8").startswith("Do the thing."))

    def test_missing_label_is_built_by_agent_dash(self):
        out = io.StringIO()
        code = cli.main(["submit", str(self.prompt)], env=self.env, out=out)
        self.assertEqual(code, 0, out.getvalue())
        job_id = out.getvalue().splitlines()[0]
        self.assertTrue(row(self.home, job_id)["label"].startswith("hub-exec-agentq-"))

    def test_wait_times_out_with_exit_3(self):
        job_id = submit(self.env, self.prompt, label="t-timeout")
        code, out = run_cli(self.env, ["wait", job_id, "--timeout", "1"])
        self.assertEqual(code, 3)
        self.assertIn("timed out", out)

    def test_wait_unknown_job_is_negative(self):
        code, out = run_cli(self.env, ["wait", "nosuchjob", "--timeout", "1"])
        self.assertEqual(code, 1)
        self.assertIn("unknown job", out)

    def test_store_is_wal_with_schema(self):
        conn = db.connect(self.home)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)

    def test_concurrent_submits_from_several_processes_lose_nothing(self):
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]); from agentq import cli; import io\n"
            "tag = sys.argv[2]; prompt = sys.argv[3]; home = sys.argv[4]\n"
            "env = {'AGENTQ_HOME': home, 'AGENTQ_SESSION': tag, 'PATH': '/usr/bin:/bin'}\n"
            "for j in range(10):\n"
            "    out = io.StringIO()\n"
            "    code = cli.main(['submit', prompt, '--label', f'{tag}-{j}'], env=env, out=out)\n"
            "    assert code == 0, out.getvalue()\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(ROOT), f"w{i}", str(self.prompt), str(self.home)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(4)
        ]
        for proc in procs:
            _, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)
        jobs = db.select_jobs(db.connect(self.home))
        self.assertEqual(len(jobs), 40)
        self.assertEqual(len({job["id"] for job in jobs}), 40)


class LaneAndSlotTests(ProcessCleanup):
    def test_urgent_then_aged_normal_then_fifo_then_backlog(self):
        write_config(self.home, slots={"light": 1, "tests": 0, "ssh": 0})
        a = submit(self.env, self.prompt, "--label", "a", now=Clock(0.0))
        b = submit(self.env, self.prompt, "--backlog", "--label", "b", now=Clock(1.0))
        d = submit(self.env, self.prompt, "--urgent", "--label", "d", now=Clock(999.0))
        c = submit(self.env, self.prompt, "--label", "c", now=Clock(1000.0))
        launcher = self.fake(live_s=0)
        broker = make_broker(self.home, launcher, clock=Clock(1000.0))
        for _ in range(4):
            broker.tick()
        self.assertEqual([call["id"] for call in launcher.calls], [a, d, c, b])

    def test_urgent_holds_at_most_half_the_slots_while_others_wait(self):
        write_config(self.home, slots={"light": 4, "tests": 0, "ssh": 0})
        urgent = [submit(self.env, self.prompt, "--urgent", label=f"u{i}") for i in range(4)]
        normal = submit(self.env, self.prompt, label="n")
        launcher = self.fake(live_s=30)
        launched = make_broker(self.home, launcher).tick()
        self.assertEqual(launched, urgent[:2] + [normal])
        self.assertEqual(row(self.home, urgent[2])["state"], "queued")

    def test_resource_and_tier_slot_limits(self):
        write_config(self.home, slots={"light": 3, "tests": 1, "ssh": 0}, tiers={"pi-kimi": 1})
        r1 = submit(self.env, self.prompt, "--kind", "review", label="r1")
        r2 = submit(self.env, self.prompt, "--kind", "review", label="r2")
        r3 = submit(self.env, self.prompt, "--kind", "review", label="r3")
        t1 = submit(self.env, self.prompt, "--res", "tests", label="t1")
        s1 = submit(self.env, self.prompt, "--res", "ssh", label="s1")
        d1 = submit(self.env, self.prompt, label="d1")
        launched = make_broker(self.home, self.fake(live_s=30)).tick()
        self.assertEqual(launched, [r1, t1, d1])
        for waiting in (r2, r3, s1):
            self.assertEqual(row(self.home, waiting)["state"], "queued")

    def test_one_writer_per_workspace(self):
        workspace = str(self.tmp / "repo")
        Path(workspace).mkdir()
        w1 = submit(self.env, self.prompt, "--kind", "write", "--workspace", workspace, label="w1")
        w2 = submit(self.env, self.prompt, "--kind", "write", "--workspace", workspace, label="w2")
        r1 = submit(self.env, self.prompt, "--workspace", workspace, label="r1")
        launched = make_broker(self.home, self.fake(live_s=30)).tick()
        self.assertEqual(launched, [w1, r1])
        self.assertEqual(row(self.home, w2)["state"], "queued")

    def test_saturated_proxy_stops_new_launches_but_not_running_jobs(self):
        first = submit(self.env, self.prompt, label="first")
        status = {"saturated": False}
        launcher = self.fake(live_s=30)
        broker = make_broker(self.home, launcher, proxy=lambda: dict(status))
        self.assertEqual(broker.tick(), [first])
        status["saturated"] = True
        second = submit(self.env, self.prompt, label="second")
        self.assertEqual(broker.tick(), [])
        self.assertEqual(row(self.home, first)["state"], "running")
        self.assertEqual(row(self.home, second)["state"], "queued")
        status["saturated"] = False
        self.assertEqual(broker.tick(), [second])

    def test_proxy_down_falls_back_to_static_slots(self):
        job_id = submit(self.env, self.prompt, label="static")
        self.assertEqual(make_broker(self.home, self.fake(live_s=30), proxy=lambda: None).tick(), [job_id])


class RecoveryTests(ProcessCleanup):
    def test_adopts_live_pid_without_relaunch(self):
        job_id = submit(self.env, self.prompt, label="adopt")
        live = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(lambda: (live.kill(), live.wait()))
        set_running(self.home, job_id, pid=live.pid, run_dir=self.tmp / "run")
        launcher = self.fake()
        make_broker(self.home, launcher).tick()
        self.assertEqual(launcher.calls, [])
        self.assertEqual(row(self.home, job_id)["state"], "running")

    def test_finishes_dead_pid_with_report(self):
        job_id = submit(self.env, self.prompt, label="finish")
        run_dir = self.tmp / "run"
        (run_dir / "pi").mkdir(parents=True)
        (run_dir / "pi" / "out-finish.md").write_text("BLOCKED\nno access\n", encoding="utf-8")
        set_running(self.home, job_id, pid=dead_pid(), run_dir=run_dir)
        make_broker(self.home, self.fake()).tick()
        job = row(self.home, job_id)
        self.assertEqual((job["state"], job["verdict"]), ("done", "BLOCKED"))
        self.assertEqual(Path(job["report_path"]).read_text(encoding="utf-8"), "BLOCKED\nno access\n")

    def test_dead_without_report_requeues_at_front_and_resumes(self):
        other = submit(self.env, self.prompt, label="other")
        job_id = submit(self.env, self.prompt, label="requeue")
        run_dir = self.tmp / "run"
        (run_dir / "pi").mkdir(parents=True)
        (run_dir / "pi" / "pi-sessions.tsv").write_text(
            "label\tsession_id\tstarted_epoch\tmodel\ttier\nrequeue\tsid-123\t1\tm\tpi-kimi\n",
            encoding="utf-8",
        )
        set_running(self.home, job_id, pid=dead_pid(), run_dir=run_dir)
        launcher = self.fake()
        write_config(self.home, slots={"light": 1, "tests": 0, "ssh": 0})
        make_broker(self.home, launcher).tick()
        self.assertEqual(launcher.calls[0]["id"], job_id)
        self.assertEqual(launcher.calls[0]["attempt"], 1)
        self.assertEqual(launcher.calls[0]["env"]["AGENTQ_RESUME_SESSION"], "sid-123")
        self.assertEqual(row(self.home, other)["state"], "queued")
        self.assertEqual(row(self.home, job_id)["pi_session"], "sid-123")

    def test_third_death_without_report_fails(self):
        job_id = submit(self.env, self.prompt, label="doomed")
        set_running(self.home, job_id, pid=dead_pid(), run_dir=self.tmp / "run", attempt=2)
        make_broker(self.home, self.fake(report=False)).tick()
        job = row(self.home, job_id)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["attempt"], 3)

    def test_quota_requeues_without_counting_an_attempt(self):
        job_id = submit(self.env, self.prompt, label="quota")
        run_dir = self.tmp / "run"
        (run_dir / "pi").mkdir(parents=True)
        (run_dir / "pi" / "QUOTA").write_text("model\tm\nlabel\tquota\n", encoding="utf-8")
        set_running(self.home, job_id, pid=dead_pid(), run_dir=run_dir, attempt=1)
        clock = Clock(500.0)
        broker = make_broker(self.home, self.fake(), clock=clock)
        self.assertEqual(broker.tick(), [])
        job = row(self.home, job_id)
        self.assertEqual((job["state"], job["attempt"]), ("queued", 1))
        self.assertGreater(job["not_before"], clock.t)
        clock.t += B.RATE_LIMIT_BACKOFF_S + 1
        self.assertEqual(broker.tick(), [job_id])

    def test_rate_limit_in_err_log_requeues_without_attempt(self):
        job_id = submit(self.env, self.prompt, label="ratelimit")
        run_dir = self.tmp / "run"
        (run_dir / "pi").mkdir(parents=True)
        (run_dir / "pi" / "err-ratelimit.log").write_text("HTTP 429 rate_limit_error\n", encoding="utf-8")
        set_running(self.home, job_id, pid=dead_pid(), run_dir=run_dir)
        make_broker(self.home, self.fake(report=False), clock=Clock(0.0)).tick()
        job = row(self.home, job_id)
        self.assertEqual((job["state"], job["attempt"]), ("queued", 0))

    def test_restart_after_crash_before_pid_write_recovers_pid_file(self):
        job_id = submit(self.env, self.prompt, label="pidfile")
        run_dir = self.tmp / "run"
        run_dir.mkdir()
        (run_dir / "pidfile.pid").write_text(str(dead_pid()), encoding="utf-8")
        set_running(self.home, job_id, run_dir=run_dir)
        (run_dir / "pi").mkdir()
        (run_dir / "pi" / "out-pidfile.md").write_text("PARTIAL\nhalf\n", encoding="utf-8")
        make_broker(self.home, self.fake()).tick()
        self.assertEqual(row(self.home, job_id)["verdict"], "PARTIAL")


class CancelAndEscalateTests(ProcessCleanup):
    def test_cancel_running_kills_the_process_group(self):
        job_id = submit(self.env, self.prompt, label="cancel-me")
        group = subprocess.Popen(["sh", "-c", "sleep 30 & sleep 30 & wait"], start_new_session=True)
        self.addCleanup(lambda: group.poll() is None and (group.kill(), group.wait()))
        set_running(self.home, job_id, pid=group.pid, run_dir=self.tmp / "run")
        code, out = run_cli(self.env, ["cancel", job_id])
        self.assertEqual(code, 0, out)
        self.assertEqual(row(self.home, job_id)["state"], "cancelled")
        self.assertEqual(group.wait(timeout=10), -signal_term())

    def test_cancel_queued_drops_it(self):
        job_id = submit(self.env, self.prompt, label="drop")
        code, _ = run_cli(self.env, ["cancel", job_id])
        self.assertEqual(code, 0)
        self.assertEqual(row(self.home, job_id)["state"], "cancelled")
        self.assertEqual(make_broker(self.home, self.fake()).tick(), [])
        code, out = run_cli(self.env, ["cancel", job_id])
        self.assertEqual(code, 1)

    def test_escalate_without_argument_moves_only_this_sessions_open_jobs(self):
        mine = submit(self.env, self.prompt, label="mine")
        done = submit(self.env, self.prompt, label="done")
        with db.tx(db.connect(self.home)) as conn:
            db.update_job(conn, done, state="done")
        theirs = submit(env_for(self.home, "sess-b"), self.prompt, label="theirs")
        code, out = run_cli(self.env, ["escalate"])
        self.assertEqual(code, 0, out)
        self.assertEqual(row(self.home, mine)["lane"], "urgent")
        self.assertEqual(row(self.home, done)["lane"], "normal")
        self.assertEqual(row(self.home, theirs)["lane"], "normal")
        conn = db.connect(self.home)
        events = conn.execute("SELECT job_id, kind, detail FROM events WHERE kind = 'escalate'").fetchall()
        self.assertEqual([(e["job_id"], e["kind"]) for e in events], [(mine, "escalate")])
        self.assertIn("sess-a", events[0]["detail"])
        code, out = run_cli(self.env, ["escalate"])
        self.assertEqual(code, 1)

    def test_escalate_one_job_by_id(self):
        job_id = submit(self.env, self.prompt, label="one")
        code, _ = run_cli(env_for(self.home, "someone-else"), ["escalate", job_id])
        self.assertEqual(code, 0)
        self.assertEqual(row(self.home, job_id)["lane"], "urgent")


class DaemonLockTests(ProcessCleanup):
    def test_second_daemon_is_refused(self):
        handle = B.acquire_lock(self.home)
        self.addCleanup(handle.close)
        self.assertEqual(B.daemon_pid(self.home), os.getpid())
        with self.assertRaises(B.DaemonRunning):
            B.acquire_lock(self.home)
        code, out = run_cli(self.env, ["status"])
        self.assertIn("daemon running", out)


def signal_term():
    import signal

    return signal.SIGTERM


if __name__ == "__main__":
    unittest.main()
