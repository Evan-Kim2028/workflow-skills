"""agentq broker: the daemon that reconciles running jobs and launches queued ones.

Each tick (``TICK_S`` seconds) first reconciles every running job against its
process and its run folder, then launches queued jobs that fit the slot, tier
and workspace limits. The proxy status is read once per tick: a ``saturated``
proxy stops new launches for that tick, and running jobs keep going.

Workers start through ``scripts/run_worker.py`` with max-wait ``0``, which
returns at once. The worker runs in its own session, so a broker restart does
not stop it, and the broker finds it again by pid and process start time.

A pi run writes its report, QUOTA marker, pi session file and err log under
``<run_dir>/pi/`` (``launch_pi.py`` gets that outdir from ``run_worker.py``).
The run folder itself keeps only the launcher's one summary line in
``out-<label>.md``, which is not a report.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from agentq import db

TICK_S = 2.0
MAX_ATTEMPTS = 3
RATE_LIMIT_BACKOFF_S = 60.0
PROXY_STATUS_URL = "http://127.0.0.1:8790/agentq/status"
RUN_WORKER = Path(__file__).resolve().parents[1] / "scripts" / "run_worker.py"
DEFAULT_SLOTS = {"light": 40, "tests": 6, "ssh": 8}
DEFAULT_TIERS = {"pi-kimi": 2, "pi-deepseek-flash": 48}
VERDICT_WORDS = ("BLOCKED", "PARTIAL", "PASS")
LAUNCH_LINE_PREFIX = "launched "


class LaunchError(RuntimeError):
    """run_worker refused or failed to start a worker."""


class DaemonRunning(RuntimeError):
    """Another broker already holds the daemon lock."""


def load_config(home) -> dict:
    """Slot caps by resource and per-tier caps from ``broker.json``, defaults filled in."""
    try:
        data = json.loads((Path(home) / "broker.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "slots": _caps(DEFAULT_SLOTS, data.get("slots")),
        "tiers": _caps(DEFAULT_TIERS, data.get("tiers")),
    }


def _caps(defaults: dict, override) -> dict:
    caps = dict(defaults)
    if isinstance(override, dict):
        for name, value in override.items():
            try:
                caps[str(name)] = int(value)
            except (TypeError, ValueError):
                continue
    return caps


def queue_rank(job: dict, now: float) -> int:
    """0 for urgent and for a normal job queued past its aging mark, 1 normal, 2 backlog."""
    if job["lane"] == "urgent":
        return 0
    if job["lane"] == "normal":
        return 0 if now >= job["priority_age_at"] else 1
    return 2


def order_queue(jobs: list[dict], now: float) -> list[dict]:
    """Launch order: rank first, then queue position (FIFO inside a rank)."""
    return sorted(jobs, key=lambda job: (queue_rank(job, now), job["queue_seq"]))


def process_info(pid) -> tuple[str, str] | None:
    """``(start time, ps state)`` of a pid, or None when no such process exists."""
    try:
        proc = subprocess.run(
            ["ps", "-o", "lstart=,stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    fields = proc.stdout.split()
    if proc.returncode != 0 or len(fields) < 2:
        return None
    return " ".join(fields[:-1]), fields[-1]


def process_alive(pid, start) -> bool:
    """True when ``pid`` is running, not a zombie, and started at ``start`` (pid reuse guard)."""
    info = process_info(pid)
    return info is not None and info[0] == start and not info[1].startswith("Z")


def process_group(pid):
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def kill_group(pgid) -> bool:
    """SIGTERM a process group. Never signals the broker's own group."""
    if not pgid or pgid == os.getpgrp():
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return False
    return True


def proxy_status(url: str = PROXY_STATUS_URL, timeout: float = 0.5) -> dict | None:
    """The proxy's status document, or None when the proxy is down or unreadable."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def verdict_of(text: str) -> str:
    """PASS, PARTIAL or BLOCKED from the first non-empty line, else ``none``."""
    lines = [line.strip().upper() for line in text.splitlines() if line.strip()]
    first = lines[0] if lines else ""
    for word in VERDICT_WORDS:
        if word in first:
            return word
    return "none"


def _read(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tail(path, limit: int = 65536) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - limit))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _pi_dir(run_dir) -> Path:
    return Path(run_dir) / "pi"


def report_text(run_dir, label: str) -> str | None:
    """The worker's final text: ``pi/out-<label>.md``, else a non-launcher ``out-<label>.md``."""
    for path in (_pi_dir(run_dir) / f"out-{label}.md", Path(run_dir) / f"out-{label}.md"):
        text = _read(path)
        if text.strip() and not text.startswith(LAUNCH_LINE_PREFIX):
            return text
    return None


def quota_marked(run_dir, label: str) -> bool:
    """A launcher QUOTA marker (``QUOTA`` or ``QUOTA-<label>``) exists for this run."""
    for base in (_pi_dir(run_dir), Path(run_dir)):
        if (base / "QUOTA").exists() or (base / f"QUOTA-{label}").exists():
            return True
    return False


def rate_limited(run_dir, label: str) -> bool:
    """The err log names a 429 or a rate limit."""
    for base in (_pi_dir(run_dir), Path(run_dir)):
        tail = _tail(base / f"err-{label}.log").lower()
        if "429" in tail or "rate limit" in tail:
            return True
    return False


def pi_session_of(run_dir, label: str) -> str | None:
    """Session id of the last pi row for ``label`` in ``pi/pi-sessions.tsv``, or None."""
    session = None
    for line in _read(_pi_dir(run_dir) / "pi-sessions.tsv").splitlines()[1:]:
        cells = line.split("\t")
        if len(cells) >= 2 and cells[0] == label and cells[1]:
            session = cells[1]
    return session


def _read_pid(path) -> int | None:
    try:
        return int(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def run_worker_launcher(job: dict, run_dir, env: dict) -> int:
    """Start one worker through ``run_worker.py`` (max-wait 0) and return its pid."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(RUN_WORKER), job["tier"], job["label"],
        job["workspace"], job["prompt_path"], str(run_dir), "0",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, env=env,
            stdin=subprocess.DEVNULL, start_new_session=True, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LaunchError(str(exc)) from exc
    status = next((ln for ln in proc.stdout.splitlines() if ln.startswith("STATUS:")), "")
    if proc.returncode != 0 or status.startswith("STATUS: failed"):
        raise LaunchError(status or proc.stderr.strip()[-200:] or f"rc {proc.returncode}")
    pid = _read_pid(run_dir / f"{job['label']}.pid")
    if pid is None:
        raise LaunchError("run_worker wrote no pid file")
    return pid


def log_line(home, message: str) -> None:
    """Append one timestamped line to ``logs/broker.log``. Best effort."""
    try:
        path = Path(home) / "logs" / "broker.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + message + "\n")
    except OSError:
        pass


def acquire_lock(home) -> object:
    """Take the single-daemon lock on ``broker.pid``; keep the returned handle alive."""
    Path(home).mkdir(parents=True, exist_ok=True)
    handle = open(Path(home) / "broker.pid", "a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise DaemonRunning(f"broker already running (pid {daemon_pid(home) or '?'})") from None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def daemon_pid(home) -> int | None:
    """Pid of the live broker, 0 when it is locked but the pid is unreadable, None when down."""
    try:
        handle = open(Path(home) / "broker.pid", "r", encoding="utf-8")
    except OSError:
        return None
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            text = handle.read().strip()
            return int(text) if text.isdigit() else 0
        fcntl.flock(handle, fcntl.LOCK_UN)
    return None


def cancel(conn, job: dict, now: float) -> str:
    """Drop a queued job, or SIGTERM a running job's group after checking its identity.

    Returns a one-line description of what happened.
    """
    if job["state"] == "queued":
        action = "dropped from the queue"
    elif job["pgid"] and job["pid"] and process_alive(job["pid"], job["pid_start"]):
        action = "stopped" if kill_group(job["pgid"]) else "signal failed"
    else:
        action = "process already gone"
    with db.tx(conn):
        db.update_job(conn, job["id"], state="cancelled", finished_at=now)
        db.add_event(conn, job["id"], "cancel", action, now)
    return action


class Broker:
    """One broker over one agentq home. ``launcher``, ``now`` and ``proxy`` are injectable."""

    def __init__(self, home, *, launcher=None, now=time.time, proxy=None):
        self.home = Path(home)
        self.launcher = launcher or run_worker_launcher
        self.now = now
        self.proxy = proxy or proxy_status
        self._saturated_last = False

    def log(self, message: str) -> None:
        log_line(self.home, message)

    def tick(self) -> list[str]:
        """One pass: reconcile, then launch. Returns the ids launched this tick."""
        conn = db.connect(self.home)
        try:
            now = self.now()
            self.reconcile(conn, now)
            if self._is_saturated():
                return []
            return self.launch_ready(conn, now)
        finally:
            conn.close()

    def run_forever(self, tick_s: float = TICK_S) -> None:
        handle = acquire_lock(self.home)
        self.log(f"broker start pid {os.getpid()}")
        try:
            while True:
                try:
                    self.tick()
                except Exception as exc:  # the loop must survive a bad tick
                    self.log(f"tick failed: {exc!r}")
                time.sleep(tick_s)
        finally:
            handle.close()

    def reconcile(self, conn, now: float) -> None:
        """Adopt, finish or requeue every running job. Runs first each tick and on start."""
        for job in db.select_jobs(conn, "state = 'running'"):
            self._reconcile_one(conn, self._adopt_pid_file(conn, job, now), now)

    def launch_ready(self, conn, now: float) -> list[str]:
        cfg = load_config(self.home)
        running = db.select_jobs(conn, "state = 'running'")
        ready = [
            job for job in db.select_jobs(conn, "state = 'queued'") if job["not_before"] <= now
        ]
        slots_free = dict(cfg["slots"])
        tier_used: dict[str, int] = {}
        for job in running:
            slots_free[job["res"]] = slots_free.get(job["res"], 0) - 1
            tier_used[job["tier"]] = tier_used.get(job["tier"], 0) + 1
        busy_workspaces = {job["workspace"] for job in running if job["kind"] == "write"}
        urgent_running = sum(1 for job in running if job["lane"] == "urgent")
        urgent_cap = max(1, sum(cfg["slots"].values()) // 2)
        others_waiting = any(job["lane"] != "urgent" for job in ready)
        launched: list[str] = []
        for job in order_queue(ready, now):
            if job["lane"] == "urgent" and others_waiting and urgent_running >= urgent_cap:
                continue
            if job["kind"] == "write" and job["workspace"] in busy_workspaces:
                continue
            if slots_free.get(job["res"], 0) <= 0:
                continue
            tier_cap = cfg["tiers"].get(job["tier"])
            if tier_cap is not None and tier_used.get(job["tier"], 0) >= tier_cap:
                continue
            slots_free[job["res"]] -= 1
            tier_used[job["tier"]] = tier_used.get(job["tier"], 0) + 1
            if job["kind"] == "write":
                busy_workspaces.add(job["workspace"])
            if job["lane"] == "urgent":
                urgent_running += 1
            self._launch(conn, job, now)
            launched.append(job["id"])
        return launched

    def _is_saturated(self) -> bool:
        status = self.proxy()
        saturated = bool(status and status.get("saturated"))
        if saturated != self._saturated_last:
            self.log(f"proxy saturated={saturated}")
        self._saturated_last = saturated
        return saturated

    def _transition(self, conn, job_id: str, now: float, kind: str, detail: str, **fields) -> None:
        with db.tx(conn):
            db.update_job(conn, job_id, **fields)
            db.add_event(conn, job_id, kind, detail, now)
        self.log(f"{job_id} {kind} {detail}")

    def _adopt_pid_file(self, conn, job: dict, now: float) -> dict:
        """A crash between launch and pid write leaves the pid only in the run folder."""
        if job["pid"] is not None or not job["run_dir"]:
            return job
        pid = _read_pid(Path(job["run_dir"]) / f"{job['label']}.pid")
        if pid is None:
            return job
        info = process_info(pid)
        self._transition(
            conn, job["id"], now, "pid-recovered", f"pid {pid}",
            pid=pid, pgid=process_group(pid), pid_start=info[0] if info else None,
        )
        return db.get_job(conn, job["id"])

    def _reconcile_one(self, conn, job: dict, now: float) -> None:
        if job["pid"] is not None and process_alive(job["pid"], job["pid_start"]):
            return
        run_dir, label = job["run_dir"], job["label"]
        if run_dir and quota_marked(run_dir, label):
            return self._requeue(conn, job, now, "quota or credit limit", attempt=job["attempt"], backoff=RATE_LIMIT_BACKOFF_S)
        text = report_text(run_dir, label) if run_dir else None
        if text is not None:
            return self._finish(conn, job, text, now)
        if run_dir and rate_limited(run_dir, label):
            return self._requeue(conn, job, now, "rate limit", attempt=job["attempt"], backoff=RATE_LIMIT_BACKOFF_S)
        self._death(conn, job, now, "died without a report")

    def _finish(self, conn, job: dict, text: str, now: float) -> None:
        dest = db.job_dir(self.home, job["id"]) / "report.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        verdict = verdict_of(text)
        self._transition(
            conn, job["id"], now, "finish", f"verdict {verdict}",
            state="done", verdict=verdict, report_path=str(dest), finished_at=now,
            pi_session=pi_session_of(job["run_dir"], job["label"]) or job["pi_session"],
        )

    def _death(self, conn, job: dict, now: float, reason: str) -> None:
        attempt = job["attempt"] + 1
        if attempt >= MAX_ATTEMPTS:
            self._transition(
                conn, job["id"], now, "failed", f"{reason}; {attempt} attempts",
                state="failed", attempt=attempt, finished_at=now,
            )
            return
        self._requeue(conn, job, now, reason, attempt=attempt)

    def _requeue(self, conn, job: dict, now: float, reason: str, *, attempt: int, backoff: float = 0.0) -> None:
        session_id = pi_session_of(job["run_dir"], job["label"]) if job["run_dir"] else None
        self._transition(
            conn, job["id"], now, "requeue", f"{reason}; attempt {attempt}",
            state="queued", queue_seq=db.front_queue_seq(conn, job["lane"]),
            attempt=attempt, pid=None, pgid=None, pid_start=None,
            pi_session=session_id or job["pi_session"], not_before=now + backoff,
        )

    def _launch(self, conn, job: dict, now: float) -> None:
        previous = job["run_dir"]
        run_dir = db.job_dir(self.home, job["id"]) / "runs" / uuid.uuid4().hex[:8]
        self._transition(
            conn, job["id"], now, "launch", f"tier {job['tier']} attempt {job['attempt'] + 1}",
            state="running", run_dir=str(run_dir), pid=None, pgid=None, pid_start=None, started_at=now,
        )
        job = db.get_job(conn, job["id"])
        env = dict(os.environ, AGENTQ_JOB=job["id"], AGENTQ_HOME=str(self.home))
        env.pop("AGENTQ_RESUME_SESSION", None)
        env.pop("AGENTQ_RESUME_DIR", None)
        if job["pi_session"] and previous:
            env["AGENTQ_RESUME_SESSION"] = job["pi_session"]
            env["AGENTQ_RESUME_DIR"] = str(_pi_dir(previous) / "sessions")
        try:
            pid = self.launcher(job, run_dir, env)
        except LaunchError as exc:
            self._death(conn, job, now, f"launch failed: {exc}")
            return
        info = process_info(pid)
        self._transition(
            conn, job["id"], now, "started", f"pid {pid}",
            pid=pid, pgid=process_group(pid), pid_start=info[0] if info else None,
        )
