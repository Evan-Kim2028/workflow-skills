"""agentq broker: the daemon that reconciles running jobs and launches queued ones.

Each tick (``TICK_S`` seconds) first reconciles every running job against its
process and its run folder, then launches queued jobs. The fixed slot and tier
caps in ``broker.json`` are ceilings. Above them, admission follows real
headroom (the "adaptive" section of ``broker.json``): a job that calls Morph
waits only while the proxy queue for its budgets is long, and a ``tests`` or
``light`` job waits only while the laptop is short of memory or CPU. The proxy
status and the laptop gauges are read at most once per tick. A held job records
why it waits in ``jobs.detail``. Each tick ends by writing ``broker.heartbeat``
so direct launches can tell the broker is alive.

Workers start through ``scripts/run_worker.py`` with max-wait ``0``, which
returns at once. The worker runs in its own session, so a broker restart does
not stop it, and the broker finds it again by pid and identity (start time, or
the label or run folder in its command line).

A pi run writes its report, QUOTA marker, pi session file and err log under
``<run_dir>/pi/`` (``launch_pi.py`` gets that outdir from ``run_worker.py``).
The run folder itself keeps only the launcher's one summary line in
``out-<label>.md``, which is not a report.

Every state change is a conditional update (``db.transition``) from the state
the decision was made in. A cancel or a finish that lands first wins, and the
broker's late write matches no row.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import NamedTuple

from agentq import db

TICK_S = 2.0
MAX_ATTEMPTS = 3
MAX_RATE_ATTEMPTS = 20
RATE_BACKOFF_S = 60.0
CAPACITY_RETRY_S = 20.0
HOLD_REFRESH_S = 300.0
STALL_S = 45 * 60
PROBE_RETRIES = 3
PROXY_STATUS_URL = "http://127.0.0.1:18790/agentq/status"
RUN_WORKER = Path(__file__).resolve().parents[1] / "scripts" / "run_worker.py"
DEFAULT_SLOTS = {"light": 40, "tests": 6, "ssh": 8}
DEFAULT_TIERS = {"pi-kimi": 2, "pi-deepseek-flash": 48}
DEFAULT_ADAPTIVE = {
    "enabled": True,
    "max_waiting_requests": 8,
    "tests_min_free_pct": 35,
    "tests_max_load_per_cpu": 1.0,
    "tests_floor": 4,
    "light_min_free_pct": 20,
    "gauge_ttl_s": 15.0,
}
TIER_BUDGETS = {"pi-deepseek-flash": ("total", "output"), "pi-kimi": ("total", "uncached")}
LAPTOP_GATED = ("tests", "light")
MEMORY_FREE_LINE = re.compile(r"System-wide memory free percentage:\s*(\d+(?:\.\d+)?)%")
VERDICT_LINE = re.compile(r"^\W*(?:verdict\W*)?(PASS|PARTIAL|BLOCKED)\b", re.IGNORECASE)
RATE_LIMIT_BODY = re.compile(r"\b429\b\s*[:\-]?\s*\{|Rate limit exceeded", re.IGNORECASE)
LAUNCH_LINE_PREFIX = "launched "
OPEN = db.OPEN_STATES
ALL_STATES = db.OPEN_STATES + db.TERMINAL_STATES


class LaunchError(RuntimeError):
    """run_worker refused or failed to start a worker."""


class CapacityRefused(LaunchError):
    """run_worker found a pool or tier cap full. Not a failure: the job waits."""


class DaemonRunning(RuntimeError):
    """Another broker already holds the daemon lock."""


class Probe(NamedTuple):
    """What ``ps`` says about a pid: alive, zombie, gone, or unknown (ps failed)."""

    state: str
    start: str | None
    args: str


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
        "adaptive": _adaptive(data.get("adaptive")),
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


def _adaptive(override) -> dict:
    """Adaptive admission settings. Absent means on with defaults; ``enabled: false`` means fixed caps."""
    settings = dict(DEFAULT_ADAPTIVE)
    if isinstance(override, dict):
        for name, value in override.items():
            if name not in DEFAULT_ADAPTIVE:
                continue
            if name == "enabled":
                settings[name] = bool(value)
                continue
            try:
                settings[name] = float(value)
            except (TypeError, ValueError):
                continue
    return settings


def queued_total(budget: dict) -> int:
    """Requests waiting on one proxy budget across all three lanes."""
    queued = budget.get("queued")
    if not isinstance(queued, dict):
        return 0
    return sum(int(count) for count in queued.values() if isinstance(count, int))


def plain_hold(text: str) -> tuple[str, str]:
    """A hold reason with no live number: its key is its text."""
    return text, text


def morph_hold(tier: str, lane: str, status: dict | None, max_waiting: float) -> tuple[str, str] | None:
    """Why a job for ``tier`` must wait on the Morph proxy, or None when it may launch.

    Returns (key, text). The key names the budget and threshold but not the live queue depth,
    so a job held by a changing queue keeps one key. Urgent jobs never wait here. A normal job
    waits while a budget its model uses has more than ``max_waiting`` requests queued. A backlog
    job also waits for every such budget to queue nothing. With no status (proxy down) there is
    no Morph gate.
    """
    if status is None or lane == "urgent":
        return None
    budgets = status.get("budgets") if isinstance(status.get("budgets"), dict) else {}
    for name in TIER_BUDGETS.get(tier, ()):
        budget = budgets.get(name)
        if not isinstance(budget, dict):
            continue
        waiting = queued_total(budget)
        if waiting > max_waiting:
            return f"proxy {name} waiting > {max_waiting:g}", f"proxy {name} waiting {waiting} > {max_waiting:g}"
        if lane == "backlog" and waiting > 0:
            return f"proxy {name} backlog waits for 0", f"proxy {name} waiting {waiting}; backlog waits for 0"
    return None


def laptop_hold(res: str, gauges: tuple, running_tests: int, adaptive: dict) -> tuple[str, str] | None:
    """Why a ``tests`` or ``light`` job must wait for laptop headroom, or None.

    Returns (key, text). ``gauges`` is (memory free percent, load per CPU), either may be None
    when unreadable. An unreadable gauge means no extra gate: the fixed slot caps apply, as with
    the proxy. ``tests`` jobs below ``tests_floor`` always launch. ``light`` waits only on memory.
    """
    free, load = gauges
    if res == "tests":
        if running_tests < adaptive["tests_floor"] or free is None or load is None:
            return None
        min_free = adaptive["tests_min_free_pct"]
        if free < min_free:
            return f"memory free < {min_free:.0f}%", f"memory free {free:.0f}% < {min_free:.0f}%"
        max_load = adaptive["tests_max_load_per_cpu"]
        if load >= max_load:
            return f"load per cpu >= {max_load:g}", f"load {load:.2f} per cpu >= {max_load:g}"
        return None
    if res == "light" and free is not None and free < adaptive["light_min_free_pct"]:
        floor = adaptive["light_min_free_pct"]
        return f"memory free < {floor:.0f}%", f"memory free {free:.0f}% < {floor:.0f}%"
    return None


def memory_free_pct(run=subprocess.run) -> float | None:
    """Free system memory percent from macOS ``memory_pressure``, or None when unavailable."""
    try:
        out = run(["memory_pressure"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = MEMORY_FREE_LINE.search(out or "")
    return float(match.group(1)) if match else None


class Gauges:
    """Laptop headroom readings. Memory is a subprocess, so it is cached for ``gauge_ttl_s``."""

    def __init__(self, *, memory=memory_free_pct, loadavg=os.getloadavg, cpus=os.cpu_count,
                 clock=time.time):
        self.memory = memory
        self.loadavg = loadavg
        self.cpus = cpus
        self.clock = clock
        self._free: float | None = None
        self._free_at: float | None = None

    def read(self, ttl_s: float) -> tuple[float | None, float | None]:
        """(memory free percent, 1-minute load per CPU); each is None when unreadable."""
        now = self.clock()
        if self._free_at is None or now - self._free_at >= ttl_s:
            self._free = self.memory()
            self._free_at = now
        try:
            load = self.loadavg()[0] / (self.cpus() or 1)
        except (OSError, IndexError):
            load = None
        return self._free, load


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


def probe(pid) -> Probe:
    """Read a pid with ``ps``. A failed or timed-out ``ps`` is ``unknown``, never gone."""
    try:
        proc = subprocess.run(
            ["ps", "-o", "lstart=,stat=,command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return Probe("unknown", None, "")
    text = proc.stdout.strip()
    if not text:
        return Probe("gone" if proc.returncode == 1 else "unknown", None, "")
    parts = text.split(None, 6)
    if proc.returncode != 0 or len(parts) < 6:
        return Probe("unknown", None, "")
    start, stat = " ".join(parts[:5]), parts[5]
    args = parts[6] if len(parts) > 6 else ""
    return Probe("zombie" if stat.startswith("Z") else "alive", start, args)


def names_job(args: str, job: dict) -> bool:
    """True when a command line names the job: its label or its run folder."""
    if job.get("label") and job["label"] in args:
        return True
    return bool(job.get("run_dir")) and job["run_dir"] in args


def job_process_state(pid, job: dict) -> str:
    """``alive``, ``dead`` or ``unknown`` for the worker recorded on a job.

    A recorded start time must match exactly (pid reuse guard). Without one, the
    command line must name the job.
    """
    found = probe(pid)
    if found.state == "unknown":
        return "unknown"
    if found.state in ("gone", "zombie"):
        return "dead"
    if job.get("pid_start"):
        return "alive" if found.start == job["pid_start"] else "dead"
    return "alive" if names_job(found.args, job) else "dead"


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
    """Verdict from the last line that opens with PASS, PARTIAL or BLOCKED, else ``none``.

    A line may open with ``verdict`` (any case) before the word, and trailing text
    is ignored: ``PASS (no BLOCKED items)`` reads PASS.
    """
    verdict = "none"
    for line in text.splitlines():
        match = VERDICT_LINE.match(line.strip())
        if match:
            verdict = match.group(1).upper()
    return verdict


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


def _quota_label(path) -> str | None:
    for line in _read(path).splitlines():
        key, _, value = line.partition("\t")
        if key.strip() == "label":
            return value.strip()
    return None


def quota_marked(run_dir, label: str) -> bool:
    """A quota marker that names this job's label: ``QUOTA-<label>``, or a bare
    ``QUOTA`` whose ``label`` line equals the label exactly."""
    for base in (_pi_dir(run_dir), Path(run_dir)):
        if (base / f"QUOTA-{label}").exists():
            return True
        bare = base / "QUOTA"
        if bare.exists() and _quota_label(bare) == label:
            return True
    return False


def rate_limited(run_dir, label: str) -> bool:
    """A structured 429 in this job's err log: ``429 {json}`` or ``Rate limit exceeded``."""
    for base in (_pi_dir(run_dir), Path(run_dir)):
        if RATE_LIMIT_BODY.search(_tail(base / f"err-{label}.log")):
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


def resume_file(run_dir, session_id: str | None) -> str | None:
    """The pi session file of ``session_id`` under a previous run, or None if absent."""
    if not run_dir or not session_id:
        return None
    matches = sorted((_pi_dir(run_dir) / "sessions").glob(f"*{session_id}*.jsonl"))
    return str(matches[-1]) if matches else None


def last_activity(run_dir, fallback: float) -> float:
    """Newest modification time under the run folder, never older than ``fallback``."""
    newest = fallback
    if not run_dir:
        return newest
    for root, _, files in os.walk(run_dir):
        for name in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, name)))
            except OSError:
                continue
    return newest


def _read_pid(path) -> int | None:
    try:
        return int(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def run_worker_launcher(job: dict, run_dir, env: dict) -> int:
    """Start one worker through ``run_worker.py`` (max-wait 0) and return its pid.

    A launcher that times out still counts if it left a pid file: the worker
    is adopted rather than launched again.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(RUN_WORKER), job["tier"], job["label"],
        job["workspace"], job["prompt_path"], str(run_dir), "0",
    ]  # fmt: skip
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, env=env,
            stdin=subprocess.DEVNULL, start_new_session=True, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        pid = _read_pid(run_dir / f"{job['label']}.pid")
        if pid is None:
            raise LaunchError("run_worker timed out with no pid file") from None
        return pid
    except (OSError, subprocess.SubprocessError) as exc:
        raise LaunchError(str(exc)) from exc
    status = next((ln for ln in proc.stdout.splitlines() if ln.startswith("STATUS:")), "")
    reason = status.split("reason=", 1)[1].split()[0] if "reason=" in status else ""
    if reason == "none-available" or "cap" in reason or "full" in reason:
        raise CapacityRefused(reason or "capacity")
    if proc.returncode != 0 or status.startswith("STATUS: failed"):
        raise LaunchError(status or proc.stderr.strip()[-200:] or f"rc {proc.returncode}")
    pid = _read_pid(run_dir / f"{job['label']}.pid")
    if pid is None:
        raise LaunchError("run_worker wrote no pid file")
    return pid


def write_heartbeat(home) -> None:
    """Write the current epoch to ``broker.heartbeat`` atomically (temp file, then rename)."""
    path = Path(home) / "broker.heartbeat"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f"{time.time():.3f}\n", encoding="utf-8")
    os.replace(tmp, path)


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


def cancel(conn, job: dict, now: float) -> str | None:
    """Cancel an open job. Returns what happened, or None when the job already ended.

    A running worker's group is signalled only when its identity is verified.
    A launch still in progress is stopped by the launch itself when it finishes.
    """
    if job["state"] == "queued":
        action = "dropped from the queue"
    elif not job["pid"]:
        action = "launch in progress; the new worker is stopped when it starts"
    else:
        state = job_process_state(job["pid"], job)
        if state == "alive":
            action = "stopped" if kill_group(job["pgid"]) else "signal failed"
        elif state == "unknown":
            action = "process state unknown; not signalled"
        else:
            action = "process already gone"
    moved = db.transition(conn, job["id"], OPEN, now, "cancel", action,
                          state="cancelled", finished_at=now, detail=None)  # fmt: skip
    return action if moved else None


class Broker:
    """One broker over one agentq home. ``launcher``, ``now`` and ``proxy`` are injectable."""

    def __init__(self, home, *, launcher=None, now=time.time, proxy=None, gauges=None):
        self.home = Path(home)
        self.launcher = launcher or run_worker_launcher
        self.now = now
        self.proxy = proxy or proxy_status
        self.gauges = gauges or Gauges(clock=now)
        self._saturated_last = False
        self._holds: dict[str, tuple[tuple[str, str], float]] = {}

    def log(self, message: str) -> None:
        log_line(self.home, message)

    def tick(self) -> list[str]:
        """One pass: reconcile, then launch. Returns the ids launched this tick."""
        conn = db.connect(self.home)
        try:
            now = self.now()
            self.reconcile(conn, now)
            launched = self.launch_ready(conn, now, status=self._proxy_status())
        finally:
            conn.close()
        write_heartbeat(self.home)
        return launched

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
        """Adopt, finish, requeue or watch every running job. Runs first each tick."""
        for job in db.select_jobs(conn, "state = 'running'"):
            self._reconcile_one(conn, job, now)

    def launch_ready(self, conn, now: float, status: dict | None = None) -> list[str]:
        """Launch every ready job that passes its gates. ``status`` is the proxy status, or None."""
        cfg = load_config(self.home)
        adaptive = cfg["adaptive"]
        on = adaptive["enabled"]
        running = db.select_jobs(conn, "state = 'running'")
        ready = [
            job for job in db.select_jobs(conn, "state = 'queued'") if job["not_before"] <= now
        ]
        slots_free = dict(cfg["slots"])
        tier_used: dict[str, int] = {}
        res_used: dict[str, int] = {}
        for job in running:
            slots_free[job["res"]] = slots_free.get(job["res"], 0) - 1
            tier_used[job["tier"]] = tier_used.get(job["tier"], 0) + 1
            res_used[job["res"]] = res_used.get(job["res"], 0) + 1
        busy_workspaces = {job["workspace"] for job in running if job["kind"] == "write"}
        urgent_running = sum(1 for job in running if job["lane"] == "urgent")
        urgent_cap = max(1, sum(cfg["slots"].values()) // 2)
        others_waiting = any(job["lane"] != "urgent" for job in ready)
        saturated = bool(status and status.get("saturated"))
        gauge_reading: list[tuple] = []

        def laptop() -> tuple:
            if not gauge_reading:
                gauge_reading.append(self.gauges.read(adaptive["gauge_ttl_s"]))
            return gauge_reading[0]

        holds: dict[str, tuple[str, tuple[str, str]]] = {}
        launched: list[str] = []
        for job in order_queue(ready, now):
            name, reason = job["res"], None
            if on:
                reason = morph_hold(job["tier"], job["lane"], status, adaptive["max_waiting_requests"])
                if reason:
                    name = job["tier"]
            elif saturated and job["lane"] == "backlog":
                name, reason = job["tier"], plain_hold("proxy saturated")
            if reason is None and job["lane"] == "urgent" and others_waiting and urgent_running >= urgent_cap:
                reason = plain_hold("urgent share full")
            if reason is None and job["kind"] == "write" and job["workspace"] in busy_workspaces:
                reason = plain_hold("workspace busy")
            if reason is None and slots_free.get(job["res"], 0) <= 0:
                reason = plain_hold("slots full")
            tier_cap = cfg["tiers"].get(job["tier"])
            if reason is None and tier_cap is not None and tier_used.get(job["tier"], 0) >= tier_cap:
                name, reason = job["tier"], plain_hold("tier cap full")
            if reason is None and on and job["res"] in LAPTOP_GATED:
                reason = laptop_hold(job["res"], laptop(), res_used.get("tests", 0), adaptive)
            if reason:
                holds[job["id"]] = (name, reason)
                continue
            slots_free[job["res"]] -= 1
            tier_used[job["tier"]] = tier_used.get(job["tier"], 0) + 1
            res_used[job["res"]] = res_used.get(job["res"], 0) + 1
            if job["kind"] == "write":
                busy_workspaces.add(job["workspace"])
            if job["lane"] == "urgent":
                urgent_running += 1
            if self._launch(conn, job, now):
                launched.append(job["id"])
            self._holds.pop(job["id"], None)
        self._record_holds(conn, now, ready, holds)
        return launched

    def _record_holds(self, conn, now: float, ready: list[dict], holds: dict) -> None:
        """Write each queued job's hold to ``jobs.detail`` and the log.

        A hold is identified by (gate, reason key), not by its text, because the text carries
        live numbers. A job writes and logs when its key changes, and otherwise at most once
        per ``HOLD_REFRESH_S`` so its detail shows a current number.
        """
        ready_ids = {job["id"] for job in ready}
        for job_id in [job_id for job_id in self._holds if job_id not in ready_ids]:
            del self._holds[job_id]
            db.transition(conn, job_id, ("queued",), now, None, "", detail=None)
        for job in ready:
            job_id = job["id"]
            hold = holds.get(job_id)
            if hold is None:
                if self._holds.pop(job_id, None) is not None:
                    db.transition(conn, job_id, ("queued",), now, None, "", detail=None)
                continue
            name, (key, text) = hold
            identity = (name, key)
            written = self._holds.get(job_id)
            if written is not None and written[0] == identity and now - written[1] < HOLD_REFRESH_S:
                continue
            self._holds[job_id] = (identity, now)
            message = f"hold {name}: {text}"
            db.transition(conn, job_id, ("queued",), now, None, "", detail=message)
            self.log(f"{message} (job {job_id})")

    def _proxy_status(self) -> dict | None:
        """The proxy status for this tick (None when down). Logs each change of its saturated flag."""
        status = self.proxy()
        saturated = bool(status and status.get("saturated"))
        if saturated != self._saturated_last:
            self.log(f"proxy saturated={saturated}")
        self._saturated_last = saturated
        return status

    def _move(self, conn, job_id: str, from_states, now: float, kind: str | None, note: str, **fields) -> bool:
        moved = db.transition(conn, job_id, from_states, now, kind, note, **fields)
        if moved and kind:
            self.log(f"{job_id} {kind} {note}")
        return moved

    def _reconcile_one(self, conn, job: dict, now: float) -> None:
        if job["pid"] is None:
            job = self._adopt_pid_file(conn, job, now)
            if job is None:
                return
        if job["pid"] is not None:
            state = job_process_state(job["pid"], job)
            if state == "unknown":
                return
            if state == "alive":
                return self._watch(conn, job, now)
        self._worker_ended(conn, job, now)

    def _adopt_pid_file(self, conn, job: dict, now: float) -> dict | None:
        """Recover a pid written to the run folder but never recorded (a crash mid-launch).

        Returns the job (adopted, or still without a pid), or None when the probe
        is unknown and the decision waits for the next tick.
        """
        if not job["run_dir"]:
            return job
        pid = _read_pid(Path(job["run_dir"]) / f"{job['label']}.pid")
        if pid is None:
            return job
        found = probe(pid)
        if found.state == "unknown":
            return None
        if found.state != "alive" or not names_job(found.args, job):
            return job
        self._move(
            conn, job["id"], ("running",), now, "pid-recovered", f"pid {pid}",
            pid=pid, pgid=process_group(pid), pid_start=found.start,
        )  # fmt: skip
        return db.get_job(conn, job["id"])

    def _worker_ended(self, conn, job: dict, now: float) -> None:
        run_dir, label = job["run_dir"], job["label"]
        if run_dir and quota_marked(run_dir, label):
            return self._rate_requeue(conn, job, now, "quota or credit limit")
        text = report_text(run_dir, label) if run_dir else None
        if text is not None:
            return self._finish(conn, job, text, now)
        if run_dir and rate_limited(run_dir, label):
            return self._rate_requeue(conn, job, now, "rate limit")
        self._death(conn, job, now, "died without a report")

    def _watch(self, conn, job: dict, now: float) -> None:
        """A live worker past its runtime cap or silent for the stall window is stopped."""
        if job["started_at"] is not None and now - job["started_at"] > job["max_runtime_s"]:
            return self._trip(conn, job, now, f"max runtime {int(job['max_runtime_s'])}s exceeded", requeue_once=False)
        if now - last_activity(job["run_dir"], job["started_at"] or now) > STALL_S:
            return self._trip(conn, job, now, "stalled: no change to the run folder for 45 min", requeue_once=True)

    def _trip(self, conn, job: dict, now: float, reason: str, *, requeue_once: bool) -> None:
        kill_group(job["pgid"])
        text = report_text(job["run_dir"], job["label"]) if job["run_dir"] else None
        if text is not None:
            return self._finish(conn, job, text, now, note=reason)
        if requeue_once and job["watchdog_trips"] == 0:
            return self._requeue(conn, job, now, reason, attempt=job["attempt"] + 1, watchdog_trips=1)
        self._fail(conn, job, now, reason)

    def _finish(self, conn, job: dict, text: str, now: float, note: str = "") -> None:
        dest = db.job_dir(self.home, job["id"]) / "report.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        verdict = verdict_of(text)
        self._move(
            conn, job["id"], ("running",), now, "finish", f"verdict {verdict}" + (f"; {note}" if note else ""),
            state="done", verdict=verdict, report_path=str(dest), finished_at=now,
            pi_session=pi_session_of(job["run_dir"], job["label"]) or job["pi_session"],
        )  # fmt: skip

    def _fail(self, conn, job: dict, now: float, reason: str, attempt: int | None = None) -> None:
        self._move(
            conn, job["id"], ("running",), now, "failed", reason,
            state="failed", finished_at=now, detail=reason,
            attempt=job["attempt"] if attempt is None else attempt,
        )  # fmt: skip

    def _death(self, conn, job: dict, now: float, reason: str) -> None:
        attempt = job["attempt"] + 1
        if attempt >= MAX_ATTEMPTS:
            return self._fail(conn, job, now, f"{reason}; {attempt} attempts", attempt=attempt)
        self._requeue(conn, job, now, reason, attempt=attempt)

    def _rate_requeue(self, conn, job: dict, now: float, reason: str) -> None:
        rate = job["rate_attempts"] + 1
        if rate > MAX_RATE_ATTEMPTS:
            return self._fail(conn, job, now, f"rate-limited: {rate - 1} requeues")
        self._requeue(conn, job, now, reason, rate_attempts=rate, backoff=RATE_BACKOFF_S)

    def _requeue(self, conn, job: dict, now: float, reason: str, *, attempt: int | None = None,
                 rate_attempts: int | None = None, watchdog_trips: int | None = None,
                 backoff: float = 0.0) -> None:
        session_id = pi_session_of(job["run_dir"], job["label"]) if job["run_dir"] else None
        fields = {
            "state": "queued",
            "queue_seq": db.front_queue_seq(conn, job["lane"]),
            "attempt": job["attempt"] if attempt is None else attempt,
            "rate_attempts": job["rate_attempts"] if rate_attempts is None else rate_attempts,
            "pid": None, "pgid": None, "pid_start": None,
            "pi_session": session_id or job["pi_session"],
            "not_before": now + backoff,
        }  # fmt: skip
        if watchdog_trips is not None:
            fields["watchdog_trips"] = watchdog_trips
        self._move(conn, job["id"], ("running",), now, "requeue",
                   f"{reason}; attempt {fields['attempt']}", **fields)  # fmt: skip

    def _capacity_refused(self, conn, job: dict, now: float, reason: str) -> None:
        """Keep the job queued, no attempt counted. One event per refusal streak."""
        streak = job["capacity_refusals"] + 1
        self._move(
            conn, job["id"], ("running",), now, "capacity-wait" if streak == 1 else None,
            f"run_worker reports {reason}; retry in {int(CAPACITY_RETRY_S)}s",
            state="queued", pid=None, pgid=None, pid_start=None,
            capacity_refusals=streak, not_before=now + CAPACITY_RETRY_S,
        )  # fmt: skip

    def _launch(self, conn, job: dict, now: float) -> bool:
        """Start one queued job. Returns False when it was cancelled before the launch."""
        previous = job["run_dir"]
        run_dir = db.job_dir(self.home, job["id"]) / "runs" / uuid.uuid4().hex[:8]
        if not self._move(
            conn, job["id"], ("queued",), now, "launch", f"tier {job['tier']} attempt {job['attempt'] + 1}",
            state="running", run_dir=str(run_dir), pid=None, pgid=None, pid_start=None, started_at=now,
            detail=None,
        ):  # fmt: skip
            return False
        env = dict(os.environ, AGENTQ_JOB=job["id"], AGENTQ_HOME=str(self.home))
        env.pop("AGENTQ_RESUME_SESSION", None)
        env.pop("AGENTQ_RESUME_DIR", None)
        resume = resume_file(previous, job["pi_session"])
        if resume:
            env["AGENTQ_RESUME_SESSION"] = resume
        try:
            pid = self.launcher(job, run_dir, env)
        except CapacityRefused as exc:
            self._capacity_refused(conn, job, now, str(exc))
            return False
        except LaunchError as exc:
            self._death(conn, job, now, f"launch failed: {exc}")
            return False
        found = probe(pid)
        for _ in range(PROBE_RETRIES - 1):
            if found.state != "unknown":
                break
            found = probe(pid)
        if not self._move(
            conn, job["id"], ("running",), now, "started", f"pid {pid}",
            pid=pid, pgid=process_group(pid), pid_start=found.start, capacity_refusals=0,
        ):  # fmt: skip
            kill_group(process_group(pid))
            self.log(f"{job['id']} cancelled during launch; stopped pid {pid}")
            return False
        return True
