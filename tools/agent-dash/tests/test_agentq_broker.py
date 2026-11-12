"""agentq broker and CLI: queue, lanes, slots, reconcile, cancel, concurrency.

Offline. AGENTQ_HOME is a temp folder in every test. A fake launcher stands in for
run_worker.py: it writes a pi-shaped report and starts a real short-lived process
(``sleep``) so pid and start-time checks run for real. The proxy is always stubbed.
"""

import io
import os
import plistlib
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentq import broker as B  # noqa: E402
from agentq import cli, db  # noqa: E402


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def write_session(run_dir, sid):
    """A session file named the way pi names them: timestamp, underscore, uuid."""
    sessions = Path(run_dir) / "pi" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"2026-11-12T14-38-41-598Z_{sid}.jsonl"
    path.write_text('{"type":"session"}\n', encoding="utf-8")
    return path


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


def set_running(home, job_id, *, pid=None, run_dir=None, attempt=0, pi_session=None, rate_attempts=0):
    conn = db.connect(home)
    found = B.probe(pid) if pid else None
    db.transition(
        conn, job_id, B.ALL_STATES, time.time(), None, "",
        state="running", pid=pid,
        pgid=B.process_group(pid) if pid else None,
        pid_start=found.start if found else None,
        run_dir=str(run_dir) if run_dir else None, attempt=attempt,
        pi_session=pi_session, started_at=time.time(), rate_attempts=rate_attempts,
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
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], db.BUSY_TIMEOUT_MS)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)

    def test_concurrent_submits_from_several_processes_lose_nothing(self):
        """8 submitting processes x 25 submits, while a broker ticks in a thread."""
        processes, per_process = 8, 25
        script = (
            "import sys, io; sys.path.insert(0, sys.argv[1]); from agentq import cli\n"
            "tag, prompt, home = sys.argv[2], sys.argv[3], sys.argv[4]\n"
            "env = {'AGENTQ_HOME': home, 'AGENTQ_SESSION': tag, 'PATH': '/usr/bin:/bin'}\n"
            f"for j in range({per_process}):\n"
            "    out = io.StringIO()\n"
            "    code = cli.main(['submit', prompt, '--label', f'{tag}-{j}'], env=env, out=out)\n"
            "    if code != 0:\n"
            "        sys.exit(f'exit {code}: ' + out.getvalue())\n"
        )
        broker = make_broker(self.home, self.fake(live_s=0))
        stop = threading.Event()
        tick_errors = []

        def ticker():
            while not stop.is_set():
                try:
                    broker.tick()
                except Exception as exc:  # recorded and asserted empty below
                    tick_errors.append(repr(exc))
                time.sleep(0.01)

        thread = threading.Thread(target=ticker, daemon=True)
        thread.start()
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(ROOT), f"w{i}", str(self.prompt), str(self.home)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )  # fmt: skip
            for i in range(processes)
        ]
        try:
            results = [proc.communicate(timeout=240) for proc in procs]
        finally:
            stop.set()
            thread.join(30)
        for proc, (_, err) in zip(procs, results):
            self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(tick_errors, [])
        conn = db.connect(self.home)
        jobs = db.select_jobs(conn)
        self.assertEqual(len(jobs), processes * per_process)
        self.assertEqual(len({job["id"] for job in jobs}), processes * per_process)
        submits = conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'submit'").fetchone()[0]
        self.assertEqual(submits, processes * per_process)


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
        second = submit(self.env, self.prompt, "--backlog", label="second")
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
        sid = "b7f1c2d4-1111-4222-8333-444455556666"
        session_file = write_session(run_dir, sid)
        (run_dir / "pi" / "pi-sessions.tsv").write_text(
            f"label\tsession_id\tstarted_epoch\tmodel\ttier\nrequeue\t{sid}\t1\tm\tpi-kimi\n",
            encoding="utf-8",
        )
        set_running(self.home, job_id, pid=dead_pid(), run_dir=run_dir)
        launcher = self.fake()
        write_config(self.home, slots={"light": 1, "tests": 0, "ssh": 0})
        make_broker(self.home, launcher).tick()
        self.assertEqual(launcher.calls[0]["id"], job_id)
        self.assertEqual(launcher.calls[0]["attempt"], 1)
        self.assertEqual(launcher.calls[0]["env"]["AGENTQ_RESUME_SESSION"], str(session_file))
        self.assertNotIn("AGENTQ_RESUME_DIR", launcher.calls[0]["env"])
        self.assertEqual(row(self.home, other)["state"], "queued")
        self.assertEqual(row(self.home, job_id)["pi_session"], sid)

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
        clock.t += B.RATE_BACKOFF_S + 1
        self.assertEqual(broker.tick(), [job_id])

    def test_rate_limit_in_err_log_requeues_without_attempt(self):
        job_id = submit(self.env, self.prompt, label="ratelimit")
        run_dir = self.tmp / "run"
        (run_dir / "pi").mkdir(parents=True)
        (run_dir / "pi" / "err-ratelimit.log").write_text('429: {"error": {"type": "rate_limit_error"}}\n', encoding="utf-8")
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
        self.assertEqual(group.wait(timeout=10), -signal.SIGTERM)

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
        db.transition(db.connect(self.home), done, B.ALL_STATES, 0.0, None, "", state="done")
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


class ReviewFixTests(ProcessCleanup):
    def fake_run_worker(self, stdout):
        """Patch run_worker.py calls to return a canned STATUS line; ps and others run for real."""
        real_run = subprocess.run

        def fake(cmd, **kw):
            if len(cmd) > 1 and str(cmd[1]).endswith("run_worker.py"):
                return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
            return real_run(cmd, **kw)

        return mock.patch.object(B.subprocess, "run", fake)

    def test_01_capacity_refusal_waits_without_attempt_and_logs_one_event(self):
        job_id = submit(self.env, self.prompt, label="cap")
        clock = Clock(1000.0)
        broker = make_broker(self.home, B.run_worker_launcher, clock=clock)
        line = "STATUS: failed out=x err=y tier=pi-deepseek-flash reason=none-available\n"
        with self.fake_run_worker(line):
            for _ in range(4):
                broker.tick()
                clock.t += 25
        job = row(self.home, job_id)
        self.assertEqual((job["state"], job["attempt"], job["capacity_refusals"]), ("queued", 0, 4))
        self.assertGreater(job["not_before"], 1000.0)
        kinds = [e["kind"] for e in db.connect(self.home).execute(
            "SELECT kind FROM events WHERE job_id = ?", (job_id,)).fetchall()]  # fmt: skip
        self.assertEqual(kinds.count("capacity-wait"), 1)
        self.assertNotIn("failed", kinds)

    def test_02_resume_passes_the_session_file_path(self):
        job_id = submit(self.env, self.prompt, label="resume")
        run_dir = self.tmp / "run-old"
        sid = "b7f1c2d4-1111-4222-8333-444455556666"
        session_file = write_session(run_dir, sid)
        (run_dir / "pi" / "pi-sessions.tsv").write_text(
            f"label\tsession_id\tstarted_epoch\tmodel\ttier\nresume\t{sid}\t1\tm\tpi-kimi\n", encoding="utf-8")
        set_running(self.home, job_id, pid=dead_pid(), run_dir=run_dir)
        launcher = self.fake()
        make_broker(self.home, launcher).tick()
        env = launcher.calls[0]["env"]
        self.assertEqual(env["AGENTQ_RESUME_SESSION"], str(session_file))
        self.assertTrue(env["AGENTQ_RESUME_SESSION"].endswith(f"_{sid}.jsonl"))
        self.assertNotIn("AGENTQ_RESUME_DIR", env)

    def test_03_cancel_during_launch_stops_the_new_worker(self):
        job_id = submit(self.env, self.prompt, label="race")
        spawned = []

        def racing_launcher(job, run_dir, env):
            cli.main(["cancel", job["id"]], env=self.env, out=io.StringIO())
            proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
            spawned.append(proc)
            Path(run_dir).mkdir(parents=True, exist_ok=True)
            (Path(run_dir) / f"{job['label']}.pid").write_text(str(proc.pid))
            return proc.pid

        self.assertEqual(make_broker(self.home, racing_launcher).tick(), [])
        self.assertEqual(row(self.home, job_id)["state"], "cancelled")
        self.assertEqual(spawned[0].wait(timeout=10), -signal.SIGTERM)

    def test_04_terminal_state_is_never_overwritten(self):
        job_id = submit(self.env, self.prompt, label="term")
        conn = db.connect(self.home)
        db.transition(conn, job_id, db.OPEN_STATES, 1.0, None, "", state="done", verdict="PASS")
        code, out = run_cli(self.env, ["cancel", job_id])
        self.assertEqual(code, 1)
        self.assertIn("is already done", out)
        code, _ = run_cli(self.env, ["escalate", job_id])
        self.assertEqual(code, 1)
        self.assertFalse(db.transition(conn, job_id, ("running",), 2.0, "finish", "x", state="failed"))
        self.assertEqual(row(self.home, job_id)["state"], "done")

    def test_05_unknown_probe_skips_the_job_and_never_requeues(self):
        job_id = submit(self.env, self.prompt, label="unknown")
        live = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(lambda: (live.kill(), live.wait()))
        set_running(self.home, job_id, pid=live.pid, run_dir=self.tmp / "run")
        real_run = subprocess.run

        def ps_times_out(cmd, **kw):
            if cmd and cmd[0] == "ps":
                raise subprocess.TimeoutExpired(cmd, 5)
            return real_run(cmd, **kw)

        launcher = self.fake()
        with mock.patch.object(B.subprocess, "run", ps_times_out):
            make_broker(self.home, launcher).tick()
        job = row(self.home, job_id)
        self.assertEqual((job["state"], job["attempt"], job["pid"]), ("running", 0, live.pid))
        self.assertEqual(launcher.calls, [])

    def test_05b_launcher_timeout_adopts_the_pid_file(self):
        run_dir = self.tmp / "timeout-run"
        run_dir.mkdir()
        live = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(lambda: (live.kill(), live.wait()))
        (run_dir / "tmo.pid").write_text(str(live.pid))
        job = {"tier": "pi-kimi", "label": "tmo", "workspace": str(self.tmp),
               "prompt_path": str(self.prompt)}  # fmt: skip
        real_run = subprocess.run

        def run_worker_hangs(cmd, **kw):
            if len(cmd) > 1 and str(cmd[1]).endswith("run_worker.py"):
                raise subprocess.TimeoutExpired(cmd, 300)
            return real_run(cmd, **kw)

        with mock.patch.object(B.subprocess, "run", run_worker_hangs):
            self.assertEqual(B.run_worker_launcher(job, run_dir, dict(os.environ)), live.pid)
            (run_dir / "tmo.pid").unlink()
            with self.assertRaises(B.LaunchError):
                B.run_worker_launcher(job, run_dir, dict(os.environ))

    def test_06_pid_file_adoption_requires_identity(self):
        run_dir = self.tmp / "run"
        run_dir.mkdir()
        other = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(lambda: (other.kill(), other.wait()))
        (run_dir / "ident-job.pid").write_text(str(other.pid))
        job_id = submit(self.env, self.prompt, label="ident-job")
        set_running(self.home, job_id, run_dir=run_dir)
        launcher = self.fake(report=False)
        make_broker(self.home, launcher).tick()
        job = row(self.home, job_id)
        self.assertNotEqual(job["pid"], other.pid)
        self.assertEqual(job["attempt"], 1)
        self.assertEqual(len(launcher.calls), 1)

    def test_06b_pid_file_with_a_named_process_is_adopted(self):
        run_dir = self.tmp / "run"
        run_dir.mkdir()
        named = subprocess.Popen(["sh", "-c", "sleep 30; true", "named-job"], start_new_session=True)
        self.addCleanup(lambda: (named.kill(), named.wait()))
        (run_dir / "named-job.pid").write_text(str(named.pid))
        job_id = submit(self.env, self.prompt, label="named-job")
        set_running(self.home, job_id, run_dir=run_dir)
        launcher = self.fake()
        make_broker(self.home, launcher).tick()
        job = row(self.home, job_id)
        self.assertEqual((job["state"], job["pid"]), ("running", named.pid))
        self.assertEqual(launcher.calls, [])

    def test_07_quota_marker_matches_only_this_label(self):
        run = self.tmp / "run"
        (run / "pi").mkdir(parents=True)
        (run / "pi" / "QUOTA-other").write_text("")
        (run / "pi" / "QUOTA").write_text("model\tm\nlabel\tother-job\n")
        self.assertFalse(B.quota_marked(run, "mine"))
        (run / "pi" / "QUOTA").write_text("model\tm\nlabel\tmine-two\n")
        self.assertFalse(B.quota_marked(run, "mine"))
        (run / "pi" / "QUOTA").write_text("model\tm\nlabel\tmine\n")
        self.assertTrue(B.quota_marked(run, "mine"))
        (run / "pi" / "QUOTA").unlink()
        (run / "pi" / "QUOTA-mine").write_text("")
        self.assertTrue(B.quota_marked(run, "mine"))

    def test_08_429_matches_only_structured_pi_error_text(self):
        run = self.tmp / "run"
        (run / "pi").mkdir(parents=True)
        err = run / "pi" / "err-l.log"
        cases = {
            "HTTP 429 rate_limit_error\n": False,
            "request 4290 accepted\n": False,
            '429: {"error": {"type": "rate_limit_error"}}\n': True,
            "Rate limit exceeded for account\n": True,
        }
        for text, expected in cases.items():
            err.write_text(text, encoding="utf-8")
            self.assertEqual(B.rate_limited(run, "l"), expected, text)

    def test_09_rate_requeues_are_capped_at_twenty(self):
        job_id = submit(self.env, self.prompt, label="capped")
        run_dir = self.tmp / "run"
        (run_dir / "pi").mkdir(parents=True)
        (run_dir / "pi" / "QUOTA-capped").write_text("")
        set_running(self.home, job_id, pid=dead_pid(), run_dir=run_dir, rate_attempts=20)
        make_broker(self.home, self.fake()).tick()
        job = row(self.home, job_id)
        self.assertEqual(job["state"], "failed")
        self.assertIn("rate-limited", job["detail"])

    def test_09b_rate_requeue_increments_the_separate_counter(self):
        job_id = submit(self.env, self.prompt, label="counted")
        run_dir = self.tmp / "run"
        (run_dir / "pi").mkdir(parents=True)
        (run_dir / "pi" / "QUOTA-counted").write_text("")
        set_running(self.home, job_id, pid=dead_pid(), run_dir=run_dir, attempt=1)
        make_broker(self.home, self.fake(), clock=Clock(0.0)).tick()
        job = row(self.home, job_id)
        self.assertEqual((job["state"], job["rate_attempts"], job["attempt"]), ("queued", 1, 1))

    def test_10_saturated_proxy_holds_only_backlog_jobs(self):
        backlog = submit(self.env, self.prompt, "--backlog", label="b")
        normal = submit(self.env, self.prompt, label="n")
        urgent = submit(self.env, self.prompt, "--urgent", label="u")
        broker = make_broker(self.home, self.fake(live_s=30), proxy=lambda: {"saturated": True})
        self.assertEqual(sorted(broker.tick()), sorted([normal, urgent]))
        self.assertEqual(row(self.home, normal)["state"], "running")
        self.assertEqual(row(self.home, urgent)["state"], "running")
        self.assertEqual(row(self.home, backlog)["state"], "queued")

    def test_tick_writes_the_broker_heartbeat_atomically(self):
        before = time.time()
        make_broker(self.home, self.fake()).tick()
        beat = float((self.home / "broker.heartbeat").read_text(encoding="utf-8"))
        self.assertGreaterEqual(beat, before - 1)
        self.assertLessEqual(beat, time.time() + 1)
        self.assertFalse((self.home / "broker.heartbeat.tmp").exists())

    def test_11_max_runtime_kills_the_worker_and_fails_the_job(self):
        job_id = submit(self.env, self.prompt, "--max-runtime", "5", label="slow")
        launcher = self.fake(live_s=30, report=False)
        clock = Clock(1000.0)
        broker = make_broker(self.home, launcher, clock=clock)
        broker.tick()
        clock.t = 1010.0
        broker.tick()
        job = row(self.home, job_id)
        self.assertEqual(job["state"], "failed")
        self.assertIn("max runtime 5s exceeded", job["detail"])
        self.assertEqual(launcher.procs[0].wait(timeout=10), -signal.SIGTERM)

    def test_12_stalled_worker_is_requeued_once_then_failed(self):
        job_id = submit(self.env, self.prompt, label="stall")
        launcher = self.fake(live_s=30, report=False)
        real = time.time()
        clock = Clock(real)
        broker = make_broker(self.home, launcher, clock=clock)
        broker.tick()
        clock.t = real + B.STALL_S + 60
        broker.tick()
        job = row(self.home, job_id)
        self.assertEqual((job["state"], job["watchdog_trips"], job["attempt"]), ("running", 1, 1))
        self.assertEqual(launcher.procs[0].wait(timeout=10), -signal.SIGTERM)
        clock.t = real + 2 * (B.STALL_S + 60)
        broker.tick()
        job = row(self.home, job_id)
        self.assertEqual(job["state"], "failed")
        self.assertIn("stalled", job["detail"])

    def test_13_verdict_is_the_last_verdict_line(self):
        cases = {
            "PASS (no BLOCKED items)": "PASS",
            "Summary\nPARTIAL\n\nverdict: BLOCKED": "BLOCKED",
            "verdict - pass\nall fine": "PASS",
            "no verdict here PASS": "none",
            "BLOCKED by upstream\n\nPASS": "PASS",
        }
        for text, expected in cases.items():
            self.assertEqual(B.verdict_of(text), expected, text)

    def test_14_second_daemon_exits_zero_and_logs(self):
        handle = B.acquire_lock(self.home)
        self.addCleanup(handle.close)
        out = io.StringIO()
        code = cli.main(["daemon"], env=self.env, out=out)
        self.assertEqual(code, 0)
        self.assertIn("ANSWER:", out.getvalue())
        self.assertIn("second daemon exits", (self.home / "logs" / "broker.log").read_text(encoding="utf-8"))

    def test_15_plist_throttles_restarts_to_30_seconds(self):
        data = plistlib.loads((ROOT / "agentq" / "launchd" / "com.agentq.broker.plist").read_bytes())
        self.assertEqual(data["ThrottleInterval"], 30)

    def test_16_submit_takes_max_runtime(self):
        job_id = submit(self.env, self.prompt, "--max-runtime", "120", label="mr")
        self.assertEqual(row(self.home, job_id)["max_runtime_s"], 120.0)


if __name__ == "__main__":
    unittest.main()
