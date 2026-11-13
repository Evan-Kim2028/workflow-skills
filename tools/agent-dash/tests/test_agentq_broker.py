"""agentq broker and CLI: queue, lanes, slots, reconcile, cancel, concurrency.

Offline. AGENTQ_HOME is a temp folder in every test. A fake launcher stands in for
run_worker.py: it writes a pi-shaped report and starts a real short-lived process
(``sleep``) so pid and start-time checks run for real. The proxy is always stubbed.
"""

import concurrent.futures
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
from agentq import headroom as H  # noqa: E402


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


class FakeGauges:
    """Laptop headroom with fixed readings; counts reads so tests can see caching."""

    def __init__(self, free=90.0, cpu=0.2, swap=0.1, load=0.2):
        self.free = free
        self.cpu = cpu
        self.swap = swap
        self.load = load
        self.reads = 0

    def read(self):
        self.reads += 1
        return H.Headroom(self.free, self.cpu, self.swap, self.load)


def proxy_doc(total=0, output=0, uncached=0, saturated=False):
    """A proxy status document whose budgets hold ``n`` normal-lane requests each."""
    def lanes(n):
        return {"urgent": 0, "normal": n, "backlog": 0}

    return {
        "ok": True,
        "saturated": saturated,
        "budgets": {
            "total": {"queued": lanes(total)},
            "output": {"queued": lanes(output)},
            "uncached": {"queued": lanes(uncached)},
        },
    }


def family_proxy_doc(held=None, queued=None):
    """A proxy status with per-family fields. ``held`` and ``queued`` map (budget, family) to a count:
    held is what the budget is blocking, queued is everything waiting on it."""
    held = held or {}
    queued = queued or {}
    families = ("morph-dsv41flash", "morph-kimik31", "other")
    budgets = {}
    for name in ("total", "output", "uncached", "uncached_ds"):
        blocked = {family: held.get((name, family), 0) for family in families}
        waiting = {family: queued.get((name, family), blocked[family]) for family in families}
        budgets[name] = {
            "queued": {"urgent": 0, "normal": sum(waiting.values()), "backlog": 0},
            "queued_by_family": waiting,
            "blocked_by_family": blocked,
        }
    return {"ok": True, "saturated": False, "budgets": budgets}


class InlinePool:
    """Runs each launch on the calling thread, so a tick's launches have landed when it returns."""

    def submit(self, fn, *args):
        future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:
            future.set_exception(exc)
        return future


def make_broker(home, launcher=None, clock=None, proxy=None, gauges=None, launch_pool=None):
    return B.Broker(home, launcher=launcher or FakeLauncher(), now=clock or time.time,
                    proxy=proxy or (lambda: None), gauges=gauges or FakeGauges(),
                    launch_pool=launch_pool or InlinePool())


def write_config(home, slots=None, tiers=None):
    import json

    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    data = {"pacing": {"enabled": False}}
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

    def test_busy_proxy_stops_new_launches_but_not_running_jobs(self):
        first = submit(self.env, self.prompt, label="first")
        status = {"doc": proxy_doc(total=0)}
        launcher = self.fake(live_s=30)
        broker = make_broker(self.home, launcher, proxy=lambda: status["doc"])
        self.assertEqual(broker.tick(), [first])
        status["doc"] = proxy_doc(total=9)
        second = submit(self.env, self.prompt, label="second")
        self.assertEqual(broker.tick(), [])
        self.assertEqual(row(self.home, first)["state"], "running")
        self.assertEqual(row(self.home, second)["state"], "queued")
        status["doc"] = proxy_doc(total=0)
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

        make_broker(self.home, racing_launcher).tick()
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
                raise subprocess.TimeoutExpired(cmd, B.LAUNCH_TIMEOUT_S)
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

    def test_10_proxy_queue_holds_only_backlog_jobs_when_not_empty(self):
        backlog = submit(self.env, self.prompt, "--backlog", label="b")
        normal = submit(self.env, self.prompt, label="n")
        urgent = submit(self.env, self.prompt, "--urgent", label="u")
        broker = make_broker(self.home, self.fake(live_s=30), proxy=lambda: proxy_doc(total=1))
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


class AdaptiveAdmissionTests(ProcessCleanup):
    def broker(self, proxy=lambda: None, gauges=None, clock=None):
        return make_broker(self.home, self.fake(live_s=30), proxy=proxy, gauges=gauges, clock=clock)

    def write_broker_json(self, data):
        import json

        self.home.mkdir(parents=True, exist_ok=True)
        data.setdefault("pacing", {"enabled": False})
        (self.home / "broker.json").write_text(json.dumps(data), encoding="utf-8")

    def test_morph_gate_holds_deepseek_above_the_waiting_limit(self):
        job = submit(self.env, self.prompt, label="over")
        self.assertEqual(self.broker(proxy=lambda: proxy_doc(total=9)).tick(), [])
        self.assertEqual(row(self.home, job)["state"], "queued")
        self.assertEqual(row(self.home, job)["detail"], "hold pi-deepseek-flash: proxy total waiting 9 > 8")

    def test_morph_gate_allows_exactly_the_waiting_limit(self):
        job = submit(self.env, self.prompt, label="at-limit")
        self.assertEqual(self.broker(proxy=lambda: proxy_doc(total=8)).tick(), [job])

    def test_urgent_jobs_ignore_the_morph_gate(self):
        urgent = submit(self.env, self.prompt, "--urgent", label="urgent")
        self.assertEqual(self.broker(proxy=lambda: proxy_doc(total=50)).tick(), [urgent])

    def test_backlog_also_needs_its_budgets_queue_empty(self):
        backlog = submit(self.env, self.prompt, "--backlog", label="backlog")
        status = {"doc": proxy_doc(total=1)}
        broker = self.broker(proxy=lambda: status["doc"])
        self.assertEqual(broker.tick(), [])
        self.assertEqual(row(self.home, backlog)["detail"],
                         "hold pi-deepseek-flash: proxy total waiting 1; backlog waits for 0")
        status["doc"] = proxy_doc(total=0)
        self.assertEqual(broker.tick(), [backlog])

    def test_deepseek_ignores_the_uncached_budget_and_kimi_ignores_output(self):
        deepseek = submit(self.env, self.prompt, label="deepseek")
        kimi = submit(self.env, self.prompt, "--kind", "review", label="kimi")
        self.assertEqual(self.broker(proxy=lambda: proxy_doc(output=20)).tick(), [kimi])
        self.assertEqual(row(self.home, deepseek)["state"], "queued")

    def test_kimi_requests_held_on_kimi_limits_do_not_hold_a_deepseek_job(self):
        deepseek = submit(self.env, self.prompt, label="ds-behind-kimi")
        doc = family_proxy_doc(held={("total", "morph-kimik31"): 20, ("uncached", "morph-kimik31"): 20})
        self.assertEqual(self.broker(proxy=lambda: doc).tick(), [deepseek])

    def test_deepseek_held_on_total_past_the_limit_holds_the_job(self):
        job = submit(self.env, self.prompt, label="ds-held-total")
        doc = family_proxy_doc(held={("total", "morph-dsv41flash"): 10})
        self.assertEqual(self.broker(proxy=lambda: doc).tick(), [])
        self.assertEqual(row(self.home, job)["detail"], "hold pi-deepseek-flash: proxy total waiting 10 > 8")

    def test_deepseek_queued_but_not_held_launches_even_with_a_long_queue(self):
        job = submit(self.env, self.prompt, label="ds-queued-only")
        doc = family_proxy_doc(queued={("total", "morph-dsv41flash"): 30, ("output", "morph-dsv41flash"): 30})
        self.assertEqual(self.broker(proxy=lambda: doc).tick(), [job])

    def test_backlog_deepseek_waits_only_while_its_own_family_is_held(self):
        backlog = submit(self.env, self.prompt, "--backlog", label="ds-backlog")
        status = {"doc": family_proxy_doc(held={("output", "morph-kimik31"): 3})}
        broker = self.broker(proxy=lambda: status["doc"])
        self.assertEqual(broker.tick(), [backlog])

    def test_backlog_deepseek_waits_while_a_deepseek_request_is_held(self):
        backlog = submit(self.env, self.prompt, "--backlog", label="ds-backlog-held")
        doc = family_proxy_doc(held={("total", "morph-dsv41flash"): 1})
        self.assertEqual(self.broker(proxy=lambda: doc).tick(), [])
        self.assertEqual(row(self.home, backlog)["detail"],
                         "hold pi-deepseek-flash: proxy total waiting 1; backlog waits for 0")

    def test_kimi_job_waits_on_its_own_held_requests_and_ignores_deepseek_ones(self):
        kimi = submit(self.env, self.prompt, "--kind", "review", label="kimi-held")
        held_ds = family_proxy_doc(held={("total", "morph-dsv41flash"): 20})
        self.assertEqual(self.broker(proxy=lambda: held_ds).tick(), [kimi])
        held_kimi = family_proxy_doc(held={("uncached", "morph-kimik31"): 9})
        kimi_two = submit(self.env, self.prompt, "--kind", "review", label="kimi-held-two")
        self.assertEqual(self.broker(proxy=lambda: held_kimi).tick(), [])
        self.assertEqual(row(self.home, kimi_two)["state"], "queued")

    def test_kimi_waits_on_the_uncached_budget(self):
        kimi = submit(self.env, self.prompt, "--kind", "review", label="kimi")
        deepseek = submit(self.env, self.prompt, label="deepseek")
        self.assertEqual(self.broker(proxy=lambda: proxy_doc(uncached=20)).tick(), [deepseek])
        self.assertEqual(row(self.home, kimi)["detail"], "hold pi-kimi: proxy uncached waiting 20 > 8")

    def test_unreachable_proxy_keeps_only_the_fixed_caps(self):
        jobs = [submit(self.env, self.prompt, label=f"p{i}") for i in range(3)]
        self.assertEqual(self.broker(proxy=lambda: None).tick(), jobs)

    def test_tests_jobs_launch_to_the_floor_then_wait_for_memory(self):
        write_config(self.home, slots={"light": 40, "tests": 10, "ssh": 8})
        jobs = [submit(self.env, self.prompt, "--res", "tests", label=f"t{i}") for i in range(6)]
        gauges = FakeGauges(free=31.0, load=0.2)
        self.assertEqual(self.broker(gauges=gauges).tick(), jobs[:4])
        self.assertEqual(gauges.reads, 1)
        self.assertEqual(row(self.home, jobs[4])["detail"], "hold tests: memory free 31% < 35%")

    def test_tests_jobs_wait_on_cpu_busy_above_the_floor(self):
        write_config(self.home, slots={"light": 40, "tests": 10, "ssh": 8})
        jobs = [submit(self.env, self.prompt, "--res", "tests", label=f"t{i}") for i in range(5)]
        launched = self.broker(gauges=FakeGauges(free=90.0, cpu=0.84)).tick()
        self.assertEqual(launched, jobs[:4])
        self.assertEqual(row(self.home, jobs[4])["detail"], "hold tests: cpu 84% busy >= 80%")

    def test_tests_jobs_wait_on_swap_above_the_floor(self):
        write_config(self.home, slots={"light": 40, "tests": 10, "ssh": 8})
        jobs = [submit(self.env, self.prompt, "--res", "tests", label=f"t{i}") for i in range(5)]
        launched = self.broker(gauges=FakeGauges(free=90.0, cpu=0.3, swap=0.92)).tick()
        self.assertEqual(launched, jobs[:4])
        self.assertEqual(row(self.home, jobs[4])["detail"], "hold tests: swap 92% used >= 90%")

    def test_load_per_cpu_holds_nothing(self):
        write_config(self.home, slots={"light": 40, "tests": 10, "ssh": 8})
        jobs = [submit(self.env, self.prompt, "--res", "tests", label=f"t{i}") for i in range(5)]
        self.assertEqual(self.broker(gauges=FakeGauges(free=90.0, cpu=0.3, load=9.0)).tick(), jobs[:5])

    def test_tests_ceiling_is_the_fixed_slot_count(self):
        write_config(self.home, slots={"light": 40, "tests": 2, "ssh": 8})
        jobs = [submit(self.env, self.prompt, "--res", "tests", label=f"t{i}") for i in range(3)]
        self.assertEqual(self.broker(gauges=FakeGauges(free=90.0, load=0.2)).tick(), jobs[:2])

    def test_light_jobs_wait_on_memory_below_twenty_percent_or_cpu_at_ninety_two(self):
        light = submit(self.env, self.prompt, label="light")
        ssh = submit(self.env, self.prompt, "--res", "ssh", label="ssh")
        self.assertEqual(self.broker(gauges=FakeGauges(free=15.0, cpu=0.1)).tick(), [ssh])
        self.assertEqual(row(self.home, light)["detail"], "hold light: memory free 15% < 20%")
        self.assertEqual(self.broker(gauges=FakeGauges(free=25.0, cpu=0.95)).tick(), [])
        self.assertEqual(row(self.home, light)["detail"], "hold light: cpu 95% busy >= 92%")
        self.assertEqual(self.broker(gauges=FakeGauges(free=25.0, cpu=0.5)).tick(), [light])

    def test_unreadable_gauges_fall_back_to_the_fixed_caps(self):
        write_config(self.home, slots={"light": 40, "tests": 3, "ssh": 8})
        jobs = [submit(self.env, self.prompt, "--res", "tests", label=f"t{i}") for i in range(4)]
        self.assertEqual(self.broker(gauges=FakeGauges(free=None, cpu=None, swap=None)).tick(), jobs[:3])

    def test_disabled_adaptive_restores_fixed_caps(self):
        self.write_broker_json({"slots": {"light": 40, "tests": 2, "ssh": 8},
                                "adaptive": {"enabled": False}})
        normal = submit(self.env, self.prompt, label="normal")
        t1 = submit(self.env, self.prompt, "--res", "tests", label="t1")
        t2 = submit(self.env, self.prompt, "--res", "tests", label="t2")
        t3 = submit(self.env, self.prompt, "--res", "tests", label="t3")
        backlog = submit(self.env, self.prompt, "--backlog", label="backlog")
        broker = self.broker(proxy=lambda: proxy_doc(total=50, saturated=True),
                             gauges=FakeGauges(free=5.0, cpu=0.99))
        self.assertEqual(sorted(broker.tick()), sorted([normal, t1, t2]))
        self.assertEqual(row(self.home, t3)["detail"], "hold tests: slots full")
        self.assertEqual(row(self.home, backlog)["detail"], "hold pi-deepseek-flash: proxy saturated")

    def test_absent_adaptive_key_means_on_with_defaults(self):
        cfg = B.load_config(self.home)
        self.assertIs(cfg["adaptive"]["enabled"], True)
        self.assertEqual(cfg["adaptive"]["max_waiting_requests"], 8)
        self.assertEqual(cfg["adaptive"]["tests_floor"], 4)

    def test_adaptive_overrides_merge_over_defaults(self):
        self.write_broker_json({"adaptive": {"tests_floor": 2, "enabled": False, "bogus": 1}})
        adaptive = B.load_config(self.home)["adaptive"]
        self.assertIs(adaptive["enabled"], False)
        self.assertEqual(adaptive["tests_floor"], 2.0)
        self.assertNotIn("bogus", adaptive)
        self.assertNotIn("tests_min_free_pct", adaptive)
        self.assertNotIn("light_min_free_pct", adaptive)

    def test_laptop_section_defaults_and_overrides(self):
        laptop = B.load_config(self.home)["laptop"]
        self.assertEqual(laptop["tests_max_cpu_busy"], 0.80)
        self.assertEqual(laptop["light_max_cpu_busy"], 0.92)
        self.assertEqual(laptop["max_swap_frac"], 0.90)
        self.assertEqual(laptop["fill_max_cpu_busy"], 0.75)
        self.assertEqual(laptop["fill_max_swap_frac"], 0.85)
        self.write_broker_json({"laptop": {"tests_max_cpu_busy": 0.7, "bogus": 1, "ewma_s": "x"}})
        laptop = B.load_config(self.home)["laptop"]
        self.assertEqual(laptop["tests_max_cpu_busy"], 0.7)
        self.assertEqual(laptop["ewma_s"], 60.0)
        self.assertNotIn("bogus", laptop)

    def test_memory_is_read_at_most_once_per_ttl(self):
        clock = Clock(0.0)
        calls = []

        def memory():
            calls.append(1)
            return 42.0

        gauges = H.Gauges(settings={"memory_ttl_s": 15.0}, memory=memory, ticks=lambda: None,
                          swap=lambda: 0.0, clock=clock)
        self.assertEqual(gauges.read().free_pct, 42.0)
        clock.t = 5.0
        gauges.read()
        self.assertEqual(len(calls), 1)
        clock.t = 15.0
        gauges.read()
        self.assertEqual(len(calls), 2)

    def test_memory_free_percent_is_parsed_from_memory_pressure(self):
        from types import SimpleNamespace

        out = "The system has...\nSystem-wide memory free percentage: 31%\n"
        self.assertEqual(B.memory_free_pct(run=lambda *a, **k: SimpleNamespace(stdout=out), platform="darwin"), 31.0)

        def missing(*args, **kwargs):
            raise OSError("no memory_pressure")

        self.assertIsNone(B.memory_free_pct(run=missing, platform="darwin"))

    def test_hold_reason_is_logged_and_written_only_when_it_changes(self):
        job = submit(self.env, self.prompt, label="held")
        status = {"doc": proxy_doc(total=9)}
        broker = self.broker(proxy=lambda: status["doc"])
        broker.tick()
        broker.tick()
        log_path = self.home / "logs" / "broker.log"
        self.assertEqual(log_path.read_text().count("hold pi-deepseek-flash"), 1)
        status["doc"] = proxy_doc(total=12)
        broker.tick()
        self.assertEqual(log_path.read_text().count("hold pi-deepseek-flash"), 1)
        self.assertEqual(row(self.home, job)["detail"], "hold pi-deepseek-flash: proxy total waiting 9 > 8")
        status["doc"] = proxy_doc(total=0)
        self.assertEqual(broker.tick(), [job])
        self.assertIsNone(row(self.home, job)["detail"])

    def test_hold_logs_once_while_the_cpu_reading_moves(self):
        write_config(self.home, slots={"light": 40, "tests": 10, "ssh": 8})
        jobs = [submit(self.env, self.prompt, "--res", "tests", label=f"t{i}") for i in range(5)]
        gauges = FakeGauges(free=90.0, cpu=0.85)
        broker = self.broker(gauges=gauges, clock=Clock(1000.0))
        self.assertEqual(broker.tick(), jobs[:4])
        for cpu in (0.84, 0.83, 0.81):
            gauges.cpu = cpu
            self.assertEqual(broker.tick(), [])
        log_path = self.home / "logs" / "broker.log"
        self.assertEqual(log_path.read_text().count("hold tests"), 1)
        self.assertEqual(row(self.home, jobs[4])["state"], "queued")
        self.assertEqual(row(self.home, jobs[4])["detail"], "hold tests: cpu 85% busy >= 80%")

    def test_hold_logs_again_when_the_gate_changes_from_cpu_to_memory(self):
        write_config(self.home, slots={"light": 40, "tests": 10, "ssh": 8})
        jobs = [submit(self.env, self.prompt, "--res", "tests", label=f"t{i}") for i in range(5)]
        gauges = FakeGauges(free=90.0, cpu=0.9)
        broker = self.broker(gauges=gauges, clock=Clock(1000.0))
        broker.tick()
        gauges.free = 31.0
        gauges.cpu = 0.2
        broker.tick()
        log_path = self.home / "logs" / "broker.log"
        self.assertEqual(log_path.read_text().count("hold tests"), 2)
        self.assertEqual(row(self.home, jobs[4])["detail"], "hold tests: memory free 31% < 35%")

    def test_held_job_refreshes_its_detail_five_minutes_after_the_last_write(self):
        job = submit(self.env, self.prompt, label="held", now=Clock(1000.0))
        status = {"doc": proxy_doc(total=9)}
        clock = Clock(1000.0)
        broker = self.broker(proxy=lambda: status["doc"], clock=clock)
        broker.tick()
        status["doc"] = proxy_doc(total=12)
        clock.t = 1299.0
        broker.tick()
        self.assertEqual(row(self.home, job)["detail"], "hold pi-deepseek-flash: proxy total waiting 9 > 8")
        clock.t = 1300.0
        broker.tick()
        self.assertEqual(row(self.home, job)["detail"], "hold pi-deepseek-flash: proxy total waiting 12 > 8")
        log_path = self.home / "logs" / "broker.log"
        self.assertEqual(log_path.read_text().count("hold pi-deepseek-flash"), 2)



def git(cwd, *args):
    """Run git in ``cwd`` with a throwaway identity; fail the test on a non-zero exit."""
    return subprocess.run(
        ["git", "-c", "user.name=agentq-test", "-c", "user.email=agentq-test@example.invalid", *args],
        cwd=str(cwd), capture_output=True, text=True, check=True,
    ).stdout.strip()


def write_auto_config(home, **auto):
    import json

    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    (home / "broker.json").write_text(json.dumps({"auto_worktree": auto}), encoding="utf-8")


class AutoWorktreeTests(ProcessCleanup):
    """Write jobs on a main checkout get their own worktree at launch (a real temp git repo)."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "worktrees"
        self.main = self.make_repo()
        write_auto_config(self.home, root=str(self.root))

    def make_repo(self):
        origin = self.tmp / "origin.git"
        seed = self.tmp / "seed"
        main = self.tmp / "workflow-skills"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        git(self.tmp, "init", "-q", "-b", "main", str(seed))
        (seed / "README.md").write_text("seed\n", encoding="utf-8")
        git(seed, "add", "README.md")
        git(seed, "commit", "-q", "-m", "seed")
        git(seed, "remote", "add", "origin", str(origin))
        git(seed, "push", "-q", "origin", "main")
        git(self.tmp, "clone", "-q", str(origin), str(main))
        return main

    def submit_write(self, workspace=None, *extra, label=None):
        args = ["--kind", "write", *extra]
        if workspace is not None:
            args += ["--workspace", str(workspace)]
        return submit(self.env, self.prompt, *args, label=label)

    def expected_path(self, job_id):
        return self.root / "workflow-skills" / f"agentq-{job_id}"

    def test_write_on_main_checkout_gets_a_worktree_and_launches(self):
        job_id = self.submit_write(self.main, label="w-main")
        launcher = self.fake(live_s=30)
        launched = make_broker(self.home, launcher).tick()
        self.assertEqual(launched, [job_id])
        job = row(self.home, job_id)
        path = self.expected_path(job_id)
        self.assertEqual(job["workspace"], str(path))
        self.assertEqual(job["orig_workspace"], str(self.main))
        self.assertEqual(git(path, "rev-parse", "--abbrev-ref", "HEAD"), f"agentq/{job_id}")
        self.assertEqual(git(self.main, "status", "--porcelain"), "")
        self.assertIn(f"auto-worktree {job_id} {path}", (self.home / "logs" / "broker.log").read_text())
        self.assertEqual(launcher.calls[0]["id"], job_id)

    def test_two_write_jobs_on_the_main_checkout_launch_in_parallel(self):
        first = self.submit_write(self.main, label="w1")
        second = self.submit_write(self.main, label="w2")
        launched = make_broker(self.home, self.fake(live_s=30)).tick()
        self.assertEqual(launched, [first, second])
        self.assertNotEqual(row(self.home, first)["workspace"], row(self.home, second)["workspace"])

    def test_auto_worktree_is_not_blocked_by_an_in_place_write_on_the_checkout(self):
        in_place = self.submit_write(self.main, "--in-place", label="ip")
        auto = self.submit_write(self.main, label="auto")
        second_in_place = self.submit_write(self.main, "--in-place", label="ip2")
        launched = make_broker(self.home, self.fake(live_s=30)).tick()
        self.assertEqual(launched, [in_place, auto])
        self.assertEqual(row(self.home, second_in_place)["state"], "queued")
        self.assertEqual(row(self.home, second_in_place)["detail"], "hold light: workspace busy")

    def test_linked_worktree_workspace_is_untouched(self):
        linked = self.tmp / "linked"
        git(self.main, "worktree", "add", "-q", "-b", "feat-linked", str(linked), "origin/main")
        job_id = self.submit_write(linked, label="linked")
        self.assertEqual(make_broker(self.home, self.fake(live_s=30)).tick(), [job_id])
        job = row(self.home, job_id)
        self.assertEqual(job["workspace"], str(linked))
        self.assertIsNone(job["orig_workspace"])

    def test_non_git_workspace_is_untouched(self):
        folder = self.tmp / "hub-state"
        folder.mkdir()
        job_id = self.submit_write(folder, label="plain")
        self.assertEqual(make_broker(self.home, self.fake(live_s=30)).tick(), [job_id])
        job = row(self.home, job_id)
        self.assertEqual(job["workspace"], str(folder))
        self.assertIsNone(job["orig_workspace"])

    def test_in_place_flag_keeps_the_checkout(self):
        job_id = self.submit_write(self.main, "--in-place", label="ip")
        self.assertEqual(make_broker(self.home, self.fake(live_s=30)).tick(), [job_id])
        job = row(self.home, job_id)
        self.assertEqual(job["workspace"], str(self.main))
        self.assertIsNone(job["orig_workspace"])
        self.assertEqual(job["in_place"], 1)

    def test_read_jobs_on_the_checkout_are_untouched(self):
        job_id = submit(self.env, self.prompt, "--workspace", str(self.main), label="read")
        self.assertEqual(make_broker(self.home, self.fake(live_s=30)).tick(), [job_id])
        self.assertIsNone(row(self.home, job_id)["orig_workspace"])

    def test_disabled_config_keeps_the_checkout(self):
        write_auto_config(self.home, enabled=False, root=str(self.root))
        job_id = self.submit_write(self.main, label="off")
        self.assertEqual(make_broker(self.home, self.fake(live_s=30)).tick(), [job_id])
        job = row(self.home, job_id)
        self.assertEqual(job["workspace"], str(self.main))
        self.assertIsNone(job["orig_workspace"])

    def test_worktree_creation_failure_retries_then_fails_without_writing_in_place(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("a file, not a folder\n", encoding="utf-8")
        write_auto_config(self.home, root=str(blocker))
        job_id = self.submit_write(self.main, label="fails")
        launcher = self.fake(live_s=30)
        clock = Clock(1000.0)
        broker = make_broker(self.home, launcher, clock=clock)

        self.assertEqual(broker.tick(), [])
        job = row(self.home, job_id)
        self.assertEqual(job["state"], "queued")
        self.assertTrue(job["detail"].startswith("auto-worktree failed: "), job["detail"])
        self.assertEqual(job["not_before"], 1060.0)
        self.assertEqual(job["workspace"], str(self.main))

        clock.t = 1061.0
        self.assertEqual(broker.tick(), [])
        self.assertEqual(row(self.home, job_id)["state"], "queued")

        clock.t = 1122.0
        self.assertEqual(broker.tick(), [])
        job = row(self.home, job_id)
        self.assertEqual(job["state"], "failed")
        self.assertTrue(job["detail"].startswith("auto-worktree failed after 3 tries: "), job["detail"])
        self.assertEqual(launcher.calls, [])

    def test_a_retry_after_the_fault_clears_launches_in_a_worktree(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("x\n", encoding="utf-8")
        write_auto_config(self.home, root=str(blocker))
        job_id = self.submit_write(self.main, label="later")
        clock = Clock(1000.0)
        broker = make_broker(self.home, self.fake(live_s=30), clock=clock)
        self.assertEqual(broker.tick(), [])
        write_auto_config(self.home, root=str(self.root))
        clock.t = 1061.0
        self.assertEqual(broker.tick(), [job_id])
        job = row(self.home, job_id)
        self.assertEqual(job["workspace"], str(self.expected_path(job_id)))
        self.assertIsNone(job["detail"])

    def test_worker_prompt_copy_starts_with_the_worktree_line(self):
        job_id = self.submit_write(self.main, label="prompted")
        seen = {}
        worker = self.fake(live_s=30)

        def recording(job, run_dir, env):
            seen["prompt"] = Path(job["prompt_path"]).read_text(encoding="utf-8")
            seen["path"] = job["prompt_path"]
            return worker(job, run_dir, env)

        make_broker(self.home, recording).tick()
        path = self.expected_path(job_id)
        expected_line = (
            f"You are working in a fresh worktree {path} on branch agentq/{job_id}, created from "
            f"origin/main for this job. Commit your work on this branch; do not edit {self.main}."
        )
        self.assertEqual(seen["prompt"], expected_line + "\n\nDo the thing.\n")
        self.assertNotEqual(seen["path"], row(self.home, job_id)["prompt_path"])
        self.assertEqual(Path(row(self.home, job_id)["prompt_path"]).read_text(encoding="utf-8"), "Do the thing.\n")

    def test_finish_log_and_result_show_the_worktree_and_branch(self):
        job_id = self.submit_write(self.main, label="done")
        broker = make_broker(self.home, self.fake(live_s=0))
        broker.tick()
        broker.tick()
        job = row(self.home, job_id)
        self.assertEqual(job["state"], "done")
        note = f"worktree {job['workspace']} branch agentq/{job_id}"
        self.assertIn(f"{job_id} finish verdict PASS; {note}", (self.home / "logs" / "broker.log").read_text())
        code, out = run_cli(self.env, ["result", job_id])
        self.assertEqual(code, 0)
        self.assertIn(f"{note} (from {self.main})", out)
        code, out = run_cli(self.env, ["ls", "--mine"])
        self.assertIn(f"worktree {job['workspace']}", out)

    def test_submit_records_in_place_as_zero_by_default(self):
        job_id = self.submit_write(self.main, label="default")
        self.assertEqual(row(self.home, job_id)["in_place"], 0)


class SchemaUpgradeTests(ProcessCleanup):
    def test_a_version_one_store_gains_the_worktree_columns(self):
        conn = db.connect(self.home)
        conn.execute("ALTER TABLE jobs DROP COLUMN orig_workspace")
        conn.execute("ALTER TABLE jobs DROP COLUMN in_place")
        conn.execute("PRAGMA user_version = 1")
        conn.close()
        conn = db.connect(self.home)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        self.assertIn("orig_workspace", columns)
        self.assertIn("in_place", columns)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        conn.close()


class BlockingLauncher(FakeLauncher):
    """Holds every launch whose label is in ``slow`` until ``release`` is set. Real sleeping workers."""

    def __init__(self, slow, release, owner):
        super().__init__(live_s=30)
        self.slow = set(slow)
        self.release = release
        self.lock = threading.Lock()
        owner.launchers.append(self)

    def __call__(self, job, run_dir, env):
        if job["label"] in self.slow:
            self.release.wait(10)
        with self.lock:
            return super().__call__(job, run_dir, env)

    def calls_for(self, job_id):
        return [call for call in self.calls if call["id"] == job_id]


class NonBlockingLaunchTests(ProcessCleanup):
    def setUp(self):
        super().setUp()
        write_config(self.home)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=B.MAX_CONCURRENT_LAUNCHES)
        self.addCleanup(self.pool.shutdown, wait=False)
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def blocking_broker(self, slow, clock=None):
        launcher = BlockingLauncher(slow, self.release, self)
        return make_broker(self.home, launcher, clock=clock, launch_pool=self.pool), launcher

    def wait_for(self, broker, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError("condition not reached")
            broker.tick()
            time.sleep(0.02)

    def settle(self, broker, timeout=10.0):
        deadline = time.monotonic() + timeout
        while broker._inflight:
            if time.monotonic() > deadline:
                raise AssertionError("launches still in flight")
            concurrent.futures.wait(list(broker._inflight.values()), timeout=0.05)
            conn = db.connect(self.home)
            try:
                broker._collect_launches(conn, broker.now())
            finally:
                conn.close()

    def test_a_slow_launch_does_not_block_the_next_job(self):
        write_config(self.home, slots={"light": 4, "tests": 0, "ssh": 0})
        slow = submit(self.env, self.prompt, label="slow-one")
        fast = submit(self.env, self.prompt, label="fast-one")
        broker, launcher = self.blocking_broker({"slow-one"})
        self.assertEqual(broker.tick(), [slow, fast])
        self.wait_for(broker, lambda: row(self.home, fast)["pid"] is not None)
        self.assertIsNone(row(self.home, slow)["pid"])
        self.assertFalse(self.release.is_set())
        self.release.set()
        self.settle(broker)
        self.assertIsNotNone(row(self.home, slow)["pid"])
        self.assertEqual(row(self.home, slow)["state"], "running")

    def test_heartbeat_is_written_while_a_launch_is_slow(self):
        job_id = submit(self.env, self.prompt, label="hb-slow")
        broker, _ = self.blocking_broker({"hb-slow"})
        broker.tick()
        self.assertIn(job_id, broker._inflight)
        beat = float((self.home / "broker.heartbeat").read_text(encoding="utf-8"))
        self.assertLess(abs(time.time() - beat), 60)
        self.release.set()
        self.settle(broker)

    def test_an_in_flight_launch_holds_its_slot(self):
        write_config(self.home, slots={"light": 1, "tests": 0, "ssh": 0})
        first = submit(self.env, self.prompt, label="hold-a")
        second = submit(self.env, self.prompt, label="hold-b")
        broker, launcher = self.blocking_broker({"hold-a"})
        self.assertEqual(broker.tick(), [first])
        self.assertEqual(broker.tick(), [])
        self.assertEqual(row(self.home, second)["state"], "queued")
        self.assertEqual(launcher.calls_for(second), [])
        self.release.set()
        self.settle(broker)
        self.assertEqual(row(self.home, first)["state"], "running")

    def test_an_in_flight_launch_is_not_launched_twice(self):
        job_id = submit(self.env, self.prompt, label="once-slow")
        broker, launcher = self.blocking_broker({"once-slow"})
        self.assertEqual(broker.tick(), [job_id])
        for _ in range(3):
            self.assertEqual(broker.tick(), [])
        self.release.set()
        self.settle(broker)
        self.assertEqual(len(launcher.calls_for(job_id)), 1)
        self.assertEqual(row(self.home, job_id)["state"], "running")

    def test_in_flight_launches_are_capped(self):
        ids = [submit(self.env, self.prompt, label=f"cap-{i}") for i in range(B.MAX_CONCURRENT_LAUNCHES + 1)]
        broker, _ = self.blocking_broker({f"cap-{i}" for i in range(len(ids))})
        self.assertEqual(broker.tick(), ids[: B.MAX_CONCURRENT_LAUNCHES])
        self.assertEqual(row(self.home, ids[-1])["state"], "queued")
        self.release.set()
        self.settle(broker)
        self.assertEqual(broker.tick(), [ids[-1]])
        self.settle(broker)
        self.assertEqual(row(self.home, ids[-1])["state"], "running")

    def test_a_timed_out_launch_requeues_then_fails_after_the_attempt_limit(self):
        job_id = submit(self.env, self.prompt, label="timeout-job")

        def timing_out(job, run_dir, env):
            raise B.LaunchError("run_worker timed out with no pid file")

        broker = make_broker(self.home, timing_out, launch_pool=self.pool)
        for attempt in range(1, B.MAX_ATTEMPTS):
            broker.tick()
            self.settle(broker)
            job = row(self.home, job_id)
            self.assertEqual((job["state"], job["attempt"]), ("queued", attempt))
        broker.tick()
        self.settle(broker)
        job = row(self.home, job_id)
        self.assertEqual(job["state"], "failed")
        self.assertIn("timed out with no pid file", job["detail"])

    def test_launcher_runs_with_no_preflight_and_a_bounded_wait(self):
        run_dir = self.tmp / "bounded-run"
        job = {"tier": "pi-kimi", "label": "bounded", "workspace": str(self.tmp),
               "prompt_path": str(self.prompt)}  # fmt: skip
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = list(cmd)
            seen["timeout"] = kw["timeout"]
            (run_dir / "bounded.pid").write_text("4242", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="STATUS: running tier=pi-kimi\n", stderr="")

        with mock.patch.object(B.subprocess, "run", fake_run):
            self.assertEqual(B.run_worker_launcher(job, run_dir, dict(os.environ)), 4242)
        self.assertIn("--no-preflight", seen["cmd"])
        self.assertEqual(seen["timeout"], 120)
        self.assertEqual(B.LAUNCH_TIMEOUT_S, 120)


if __name__ == "__main__":
    unittest.main()
