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
returns at once. The broker runs those launches on a small pool (at most
``MAX_CONCURRENT_LAUNCHES``), so one slow start never holds up a tick, a
heartbeat or another job's launch; the pool thread only starts the worker,
and every database write happens on the broker thread.

The worker runs in its own session, so a broker restart does not stop it, and
the broker finds it again by pid and identity (start time, or the label or run
folder in its command line).

A pi run writes its report, QUOTA marker, pi session file and err log under
``<run_dir>/pi/`` (``launch_pi.py`` gets that outdir from ``run_worker.py``).
The run folder itself keeps only the launcher's one summary line in
``out-<label>.md``, which is not a report.

Every state change is a conditional update (``db.transition``) from the state
the decision was made in. A cancel or a finish that lands first wins, and the
broker's late write matches no row.

A write job whose workspace is the main checkout of a git repo (with an
``origin`` remote) gets its own worktree at launch, unless it was submitted
with ``--in-place`` or ``broker.json`` turns ``auto_worktree`` off. The worktree
is ``<root>/<repo>/agentq-<job id>`` on a new branch ``agentq/<job id>`` from
``origin/main``. The job's ``workspace`` moves to it and ``orig_workspace``
keeps the checkout, so the one-writer rule applies per worktree and such jobs
run in parallel. A failed creation retries after 60 s and fails the job on the
third try; it never falls back to writing in place.
"""

from __future__ import annotations

import concurrent.futures
import fcntl
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import NamedTuple

from agentq import backlog as backlog_lib
from agentq import db
from agentq import fill as fill_lib
from agentq import filler as filler_lib
from agentq import headroom as headroom_lib

TICK_S = 2.0
BACKLOG_RETRY_S = 300.0
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
    "tests_floor": 4,
    "gauge_ttl_s": 15.0,
}
DEFAULT_LAPTOP = {
    "tests_max_cpu_busy": 0.80,
    "tests_min_free_pct": 35.0,
    "light_max_cpu_busy": 0.92,
    "light_min_free_pct": 20.0,
    "max_swap_frac": 0.90,
    "fill_max_cpu_busy": 0.75,
    "fill_min_free_pct": 20.0,
    "fill_max_swap_frac": 0.85,
    "sample_s": 10.0,
    "ewma_s": 60.0,
}
TIER_BUDGETS = {"pi-deepseek-flash": ("total", "output", "uncached_ds"), "pi-kimi": ("total", "uncached")}
TIER_FAMILY = {"pi-deepseek-flash": "morph-dsv41flash", "pi-kimi": "morph-kimik31"}
DEFAULT_AUTO_WORKTREE = {"enabled": True, "root": "~/Documents/worktrees"}
AUTO_WORKTREE_RETRY_S = 60.0
AUTO_WORKTREE_MAX_TRIES = 3
GIT_TIMEOUT_S = 60.0
LAUNCH_TIMEOUT_S = 120.0
MAX_CONCURRENT_LAUNCHES = 8
LAPTOP_GATED = ("tests", "light")
memory_free_pct = headroom_lib.memory_free_pct
VERDICT_LINE = re.compile(r"^\W*(?:verdict\W*)?(PASS|PARTIAL|BLOCKED)\b", re.IGNORECASE)
RATE_LIMIT_BODY = re.compile(r"\b429\b\s*[:\-]?\s*\{|Rate limit exceeded", re.IGNORECASE)
LAUNCH_LINE_PREFIX = "launched "
AGENT_DASH_LABEL = Path(__file__).resolve().parents[1] / "agent_dash.py"
CONTROLLER_STATE_KEY = "fill_controller"
MEASURE_REFRESH_S = 600.0
PROCESS_REFRESH_S = 30.0
OPEN = db.OPEN_STATES
ALL_STATES = db.OPEN_STATES + db.TERMINAL_STATES


class LaunchError(RuntimeError):
    """run_worker refused or failed to start a worker."""


class CapacityRefused(LaunchError):
    """run_worker found a pool or tier cap full. Not a failure: the job waits."""


class DaemonRunning(RuntimeError):
    """Another broker already holds the daemon lock."""


class WorktreeError(RuntimeError):
    """git could not inspect a workspace or create a job's worktree. The job waits and retries."""


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
        "laptop": _laptop(data.get("laptop")),
        "auto_worktree": _auto_worktree(data.get("auto_worktree")),
        "fill": fill_lib.section(data.get("fill"), fill_lib.DEFAULT_FILL),
        "pacing": fill_lib.section(data.get("pacing"), fill_lib.DEFAULT_PACING),
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


def _laptop(override) -> dict:
    """Laptop headroom bars and sampling cadence. Only the known keys are read; bad values keep the default."""
    settings = dict(DEFAULT_LAPTOP)
    if isinstance(override, dict):
        for name, value in override.items():
            if name not in DEFAULT_LAPTOP:
                continue
            try:
                settings[name] = float(value)
            except (TypeError, ValueError):
                continue
    return settings


def _auto_worktree(override) -> dict:
    """Write-job worktree settings. ``enabled: false`` or a root folder override the defaults."""
    settings = dict(DEFAULT_AUTO_WORKTREE)
    if isinstance(override, dict):
        if "enabled" in override:
            settings["enabled"] = bool(override["enabled"])
        if isinstance(override.get("root"), str) and override["root"].strip():
            settings["root"] = override["root"].strip()
    return settings


def queued_total(budget: dict) -> int:
    """Requests waiting on one proxy budget across all three lanes."""
    queued = budget.get("queued")
    if not isinstance(queued, dict):
        return 0
    return sum(int(count) for count in queued.values() if isinstance(count, int))


def blocked_for_family(budget: dict, family: str | None) -> int:
    """Requests of one model family that a proxy budget is holding right now.

    A proxy older than the per-family fields reports no ``blocked_by_family``; its whole queue
    stands in, which is the old gate's count.
    """
    by_family = budget.get("blocked_by_family")
    if isinstance(by_family, dict) and family is not None:
        count = by_family.get(family, 0)
        return count if isinstance(count, int) else 0
    return queued_total(budget)


def plain_hold(text: str) -> tuple[str, str]:
    """A hold reason with no live number: its key is its text."""
    return text, text


def morph_hold(tier: str, lane: str, status: dict | None, max_waiting: float) -> tuple[str, str] | None:
    """Why a job for ``tier`` must wait on the Morph proxy, or None when it may launch.

    Returns (key, text). The key names the budget and threshold but not the live queue depth,
    so a job held by a changing queue keeps one key. Urgent jobs never wait here. A normal job
    waits while a budget its tier uses is holding more than ``max_waiting`` requests of the
    tier's own model family. A backlog job also waits while any such budget holds one of them.
    Requests from another family, such as Kimi's own uncached limit, never hold a DeepSeek job.
    With no status (proxy down) there is no Morph gate.
    """
    if status is None or lane == "urgent":
        return None
    budgets = status.get("budgets") if isinstance(status.get("budgets"), dict) else {}
    family = TIER_FAMILY.get(tier)
    for name in TIER_BUDGETS.get(tier, ()):
        budget = budgets.get(name)
        if not isinstance(budget, dict):
            continue
        waiting = blocked_for_family(budget, family)
        if waiting > max_waiting:
            return f"proxy {name} waiting > {max_waiting:g}", f"proxy {name} waiting {waiting} > {max_waiting:g}"
        if lane == "backlog" and waiting > 0:
            return f"proxy {name} backlog waits for 0", f"proxy {name} waiting {waiting}; backlog waits for 0"
    return None


def laptop_hold(res: str, reading: headroom_lib.Headroom, running_tests: int, laptop: dict,
                tests_floor: int) -> tuple[str, str] | None:
    """Why a ``tests`` or ``light`` job must wait for laptop headroom, or None.

    Returns (key, text). ``tests`` jobs below ``tests_floor`` always launch; from the floor up they
    wait on CPU busy, memory free or swap used. ``light`` jobs wait on the same three measures at
    their own bars, with no floor. An unreadable measure never holds, so the fixed slot caps apply
    alone when every gauge is unreadable.
    """
    if res == "tests":
        if running_tests < tests_floor:
            return None
        bars = dict(max_cpu_busy=laptop["tests_max_cpu_busy"], min_free_pct=laptop["tests_min_free_pct"],
                    max_swap_frac=laptop["max_swap_frac"])
    elif res == "light":
        bars = dict(max_cpu_busy=laptop["light_max_cpu_busy"], min_free_pct=laptop["light_min_free_pct"],
                    max_swap_frac=laptop["max_swap_frac"])
    else:
        return None
    return headroom_lib.pressure(reading, **bars)


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

    The broker has already checked tier and caps, so the launcher skips the
    dashboard preflight (``--no-preflight``). A launcher that times out still
    counts if it left a pid file: the worker is adopted rather than launched again.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(RUN_WORKER), job["tier"], job["label"],
        job["workspace"], job["prompt_path"], str(run_dir), "0", "--no-preflight",
    ]  # fmt: skip
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=LAUNCH_TIMEOUT_S, env=env,
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


def label_for(kind: str, slug: str) -> str | None:
    """A hub label from ``agent_dash.py label make``, or None when it refuses.

    The mark is ``review`` for a review job and ``exec`` otherwise.
    """
    mark = "review" if kind == "review" else "exec"
    cmd = [
        sys.executable, str(AGENT_DASH_LABEL), "label", "make",
        "--scope", "hub", "--mark", mark, "--slug", slug,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = proc.stdout.strip().splitlines()
    return lines[-1] if proc.returncode == 0 and lines else None


def is_filler(job: dict) -> bool:
    """True for a filler audit job (a backlog-session job whose label carries the filler marker)."""
    return job["session"] == backlog_lib.SESSION and filler_lib.is_filler_label(job["label"])


def branch_for(job_id: str) -> str:
    """The branch a job's auto worktree is created on."""
    return f"agentq/{job_id}"


def worktree_path(root: str, repo: Path, job_id: str) -> Path:
    """``<root>/<repo name>/agentq-<job id>``, with ``~`` expanded."""
    return Path(os.path.expanduser(root)) / repo.name / f"agentq-{job_id}"


def worktree_note(job: dict) -> str:
    """``worktree <path> branch <branch>`` for a job that runs in its own worktree, else empty."""
    if not job.get("orig_workspace"):
        return ""
    return f"worktree {job['workspace']} branch {branch_for(job['id'])}"


def _short(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return (lines[-1] if lines else "no output")[:200]


def _git(cwd, *args, timeout: float = GIT_TIMEOUT_S):
    """Run ``git -C cwd args``. A missing git or a timeout raises WorktreeError."""
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True,
            timeout=timeout, check=False, stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorktreeError(f"git {args[0]} did not run: {_short(str(exc))}") from exc


def main_checkout_of(workspace) -> Path | None:
    """The workspace when it is the main working tree of a git repo with an ``origin`` remote.

    The main working tree is the one whose git common dir is its own ``.git``. A linked
    worktree points elsewhere, and a subfolder of a checkout resolves to the parent's
    ``.git``, so both return None, as does a folder that is not a repo or has no origin.
    A git failure other than "not a git repository" raises WorktreeError.
    """
    ws = Path(workspace)
    if not ws.is_dir():
        return None
    probe = _git(ws, "rev-parse", "--git-common-dir")
    if probe.returncode != 0:
        if "not a git repository" in probe.stderr.lower():
            return None
        raise WorktreeError(f"rev-parse failed: {_short(probe.stderr)}")
    common = Path(probe.stdout.strip())
    if not common.is_absolute():
        common = ws / common
    if os.path.realpath(common) != os.path.realpath(ws / ".git"):
        return None
    if _git(ws, "remote", "get-url", "origin").returncode != 0:
        return None
    return ws


def plan_worktree(job: dict, settings: dict):
    """``(repo, worktree path)`` for a queued write job that gets its own worktree, else None.

    Off when auto worktrees are disabled, when the submitter passed ``--in-place``, or when the
    job already runs in a worktree it was given (a requeued job keeps it).
    """
    if not settings["enabled"] or job["in_place"] or job["orig_workspace"]:
        return None
    repo = main_checkout_of(job["workspace"])
    if repo is None:
        return None
    return repo, worktree_path(settings["root"], repo, job["id"])


def create_worktree(repo: Path, path: Path, branch: str, *, fetch: bool) -> str:
    """Create ``path`` on a new ``branch`` from ``origin/main``. Returns a note for the log.

    The fetch runs first unless ``fetch`` is False; a failed fetch falls back to the local
    ``origin/main`` ref and says so in the note. A path or branch left by an earlier attempt
    is reused when it already holds that branch, so a retry converges.
    """
    note = ""
    if fetch:
        got = _git(repo, "fetch", "-q", "origin", "main")
        if got.returncode != 0:
            note = f"fetch origin main failed, using local origin/main: {_short(got.stderr)}"
    if _git(repo, "rev-parse", "--verify", "-q", "origin/main^{commit}").returncode != 0:
        raise WorktreeError("no origin/main ref to branch from")
    if path.exists():
        head = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
        if head.returncode == 0 and head.stdout.strip() == branch:
            return note
        raise WorktreeError(f"{path} exists and is not a worktree on {branch}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorktreeError(f"cannot create {path.parent}: {_short(str(exc))}") from exc
    if _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0:
        added = _git(repo, "worktree", "add", str(path), branch)
    else:
        added = _git(repo, "worktree", "add", "-b", branch, str(path), "origin/main")
    if added.returncode != 0:
        raise WorktreeError(f"worktree add failed: {_short(added.stderr)}")
    return note


def worktree_prompt(run_dir, job: dict) -> Path:
    """Copy the job's prompt into its run folder with the worktree instruction as the first line."""
    line = (
        f"You are working in a fresh worktree {job['workspace']} on branch {branch_for(job['id'])}, "
        f"created from origin/main for this job. Commit your work on this branch; "
        f"do not edit {job['orig_workspace']}."
    )
    dest = Path(run_dir) / "prompt-worktree.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(line + "\n\n" + _read(job["prompt_path"]), encoding="utf-8")
    return dest


class Broker:
    """One broker over one agentq home. ``launcher``, ``now`` and ``proxy`` are injectable."""

    def __init__(self, home, *, launcher=None, now=time.time, proxy=None, gauges=None, launch_pool=None,
                 labeler=None):
        self.home = Path(home)
        self.launcher = launcher or run_worker_launcher
        self.labeler = labeler or label_for
        self.now = now
        self.proxy = proxy or proxy_status
        self.gauges = gauges or headroom_lib.Gauges(home, settings=headroom_lib.settings(load_config(home)),
                                                    clock=now)
        self.launch_pool = launch_pool or concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_LAUNCHES, thread_name_prefix="agentq-launch")
        self._inflight: dict[str, concurrent.futures.Future] = {}
        self._saturated_last = False
        self._holds: dict[str, tuple[tuple[str, str], float]] = {}
        self._plans: dict[str, tuple[Path, Path] | None] = {}
        self._backlog_retry: dict[str, float] = {}
        self._controller: fill_lib.FillController | None = None
        self._saved_controller: dict | None = None
        self._fill_key: str | None = None
        self._ps: tuple[float, str | None] | None = None
        self._measured: tuple[float, int | None] | None = None
        self._filler_cache: tuple[float, tuple] | None = None
        self._filler_retry = 0.0

    def log(self, message: str) -> None:
        log_line(self.home, message)

    def tick(self) -> list[str]:
        """One pass: reconcile, launch, then submit due backlog jobs. Returns the ids launched this tick."""
        conn = db.connect(self.home)
        try:
            now = self.now()
            self._collect_launches(conn, now)
            self.reconcile(conn, now)
            status = self._proxy_status()
            launched = self.launch_ready(conn, now, status=status)
            self.submit_backlog(conn, now, status)
            self.submit_filler(conn, now, status)
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
        """Adopt, finish, requeue or watch every running job. Runs first each tick.

        A job whose launch is still in the pool is running with no pid yet; it is
        skipped here, so its worker is not mistaken for a dead one.
        """
        for job in db.select_jobs(conn, "state = 'running'"):
            if job["id"] in self._inflight:
                continue
            self._reconcile_one(conn, job, now)

    def launch_ready(self, conn, now: float, status: dict | None = None) -> list[str]:
        """Launch every ready job that passes its gates. ``status`` is the proxy status, or None."""
        cfg = load_config(self.home)
        adaptive = cfg["adaptive"]
        on = adaptive["enabled"]
        pace = self._pacing_gate(conn, now, cfg["pacing"], status)
        filler_paused = filler_lib.paused(conn)
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
        laptop_cfg = cfg["laptop"]
        gauge_reading: list[headroom_lib.Headroom] = []

        def laptop() -> headroom_lib.Headroom:
            if not gauge_reading:
                gauge_reading.append(self.gauges.read())
            return gauge_reading[0]

        worktree_settings = cfg["auto_worktree"]
        ready_ids = {job["id"] for job in ready}
        self._plans = {job_id: plan for job_id, plan in self._plans.items() if job_id in ready_ids}
        fetched: set[Path] = set()
        holds: dict[str, tuple[str, tuple[str, str]]] = {}
        launched: list[str] = []
        for job in order_queue(ready, now):
            if len(self._inflight) >= MAX_CONCURRENT_LAUNCHES:
                break
            name, reason = job["res"], None
            if on:
                reason = morph_hold(job["tier"], job["lane"], status, adaptive["max_waiting_requests"])
                if reason:
                    name = job["tier"]
            elif saturated and job["lane"] == "backlog":
                name, reason = job["tier"], plain_hold("proxy saturated")
            if reason is None and job["lane"] == "urgent" and others_waiting and urgent_running >= urgent_cap:
                reason = plain_hold("urgent share full")
            plan = None
            if reason is None and job["kind"] == "write":
                try:
                    plan = self._worktree_plan(job, worktree_settings)
                except WorktreeError as exc:
                    self._worktree_failed(conn, job, now, exc)
                    continue
            if reason is None and plan is None and job["kind"] == "write" and job["workspace"] in busy_workspaces:
                reason = plain_hold("workspace busy")
            if reason is None and slots_free.get(job["res"], 0) <= 0:
                reason = plain_hold("slots full")
            tier_cap = cfg["tiers"].get(job["tier"])
            if reason is None and tier_cap is not None and tier_used.get(job["tier"], 0) >= tier_cap:
                name, reason = job["tier"], plain_hold("tier cap full")
            if reason is None and on and job["res"] in LAPTOP_GATED:
                reason = laptop_hold(job["res"], laptop(), res_used.get("tests", 0), laptop_cfg,
                                     adaptive["tests_floor"])
            if reason is None and filler_paused and is_filler(job):
                name, reason = "filler", plain_hold("filler paused")
            paced = reason is None and pace is not None and job["tier"] == fill_lib.DS_TIER
            if paced and pace["left"] <= 0 and not (job["lane"] == "urgent" and pace["urgent_free"]):
                name, reason = "proxy", pace["hold"]
            if reason:
                holds[job["id"]] = (name, reason)
                continue
            if plan is not None:
                job = self._prepare_worktree(conn, job, plan, now, fetched)
                if job is None:
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
                if paced:
                    self._spend_pacing(pace)
            self._holds.pop(job["id"], None)
        self._collect_launches(conn, now)
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

    def submit_backlog(self, conn, now: float, status: dict | None) -> list[str]:
        """Queue one job per due backlog template whose Morph budgets are idle. Returns the new job ids.

        A template with a queued or running job waits. The Morph gate is the same one a
        backlog job meets at launch (``morph_hold``), so no request queues behind a template. With no
        proxy status there is no headroom to read, so nothing is submitted. A refused label
        is retried after ``BACKLOG_RETRY_S``.
        """
        if status is None:
            return []
        max_waiting = load_config(self.home)["adaptive"]["max_waiting_requests"]
        submitted = []
        for template in backlog_lib.select_all(conn):
            name = template["name"]
            if not backlog_lib.is_due(template, now) or self._backlog_retry.get(name, 0.0) > now:
                continue
            if backlog_lib.open_job(conn, name) is not None:
                continue
            if morph_hold(backlog_lib.tier_for(template), "backlog", status, max_waiting) is not None:
                continue
            job_id = self._backlog_job(conn, template, now)
            if job_id is not None:
                submitted.append(job_id)
        return submitted

    def _backlog_job(self, conn, template: dict, now: float) -> str | None:
        """Write one template's prompt into a new job folder and queue the job. Returns the job id, or None."""
        name = template["name"]
        retry_at = now + BACKLOG_RETRY_S
        try:
            body = Path(template["prompt_path"]).read_text(encoding="utf-8")
        except OSError as exc:
            self._backlog_retry[name] = retry_at
            self.log(f"backlog {name}: prompt unreadable ({exc}); retry in {int(BACKLOG_RETRY_S)}s")
            return None
        label = self.labeler(template["kind"], backlog_lib.label_slug(name))
        if label is None:
            self._backlog_retry[name] = retry_at
            self.log(f"backlog {name}: label make refused; retry in {int(BACKLOG_RETRY_S)}s")
            return None
        job_id = db.new_job_id()
        prompt = db.job_dir(self.home, job_id) / "prompt.md"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text(backlog_lib.preamble_for(template["kind"], body), encoding="utf-8")
        job = {
            "id": job_id, "lane": "backlog", "kind": template["kind"], "res": template["res"],
            "tier": backlog_lib.tier_for(template), "label": label, "workspace": template["workspace"],
            "session": backlog_lib.SESSION, "harness": backlog_lib.SESSION, "prompt_path": str(prompt),
        }  # fmt: skip
        if template["max_runtime_s"]:
            job["max_runtime_s"] = template["max_runtime_s"]
        with db.tx(conn):
            db.insert_job(conn, job, now)
            backlog_lib.mark_submitted(conn, name, now)
        self.log(f"backlog {name} submitted {job_id} ({label})")
        return job_id

    def _pacing_gate(self, conn, now: float, pacing: dict, status: dict | None) -> dict | None:
        """This tick's DeepSeek launch allowance, or None when pacing is off.

        The current Morph minute's uncached input comes from the live proxy status (the store when the
        proxy is down), and each DeepSeek launch whose first request is not yet recorded adds its
        first-request cost. ``left`` counts launches allowed now; ``urgent_free`` lets one urgent DeepSeek
        job launch per tick even with nothing left; ``hold`` is the (key, text) a paced job waits under.
        """
        if not pacing["enabled"]:
            return None
        current, source, pending = fill_lib.pacing_inputs(conn, now, status, pacing)
        left, cost = fill_lib.pacing_allowance(current, pending, pacing, self._measured_cost(conn, now, pacing))
        cap = pacing["uncached_frac"] * pacing["uncached_limit"]
        text = (f"proxy uncached pacing: {current:,} uncached this minute ({source}), {pending} pending "
                f"launch(es) at {cost:,} each, cap {cap:,.0f}")
        return {"left": left, "cost": cost, "urgent_free": True, "hold": ("proxy uncached pacing", text)}

    def _spend_pacing(self, pace: dict) -> None:
        """Charge one launch to the tick's allowance. A launch past an empty allowance was the urgent bypass."""
        if pace["left"] > 0:
            pace["left"] -= 1
        else:
            pace["urgent_free"] = False

    def _measured_cost(self, conn, now: float, pacing: dict) -> int | None:
        """The measured first-request cost, refreshed every MEASURE_REFRESH_S. None when off or unmeasured."""
        if not pacing["measure_first_request"]:
            return None
        if self._measured is not None and now - self._measured[0] < MEASURE_REFRESH_S:
            return self._measured[1]
        try:
            value = fill_lib.measured_first_request(conn, now, pacing)
        except sqlite3.OperationalError:
            value = None
        self._measured = (now, value)
        return value

    def _process_table(self, now: float) -> str | None:
        """The ``ps -E`` table, refreshed every PROCESS_REFRESH_S. None when ps failed on the last read."""
        if self._ps is None or now - self._ps[0] >= PROCESS_REFRESH_S:
            self._ps = (now, fill_lib.process_table())
        return self._ps[1]

    def _filler_targets(self) -> tuple[list, float]:
        """Filler targets and max runtime from backlog-seed.json, re-read when the file changes.
        A malformed section logs once and yields no targets."""
        path = self.home / filler_lib.CONFIG_NAME
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        if self._filler_cache is not None and self._filler_cache[0] == mtime:
            return self._filler_cache[1]
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if mtime is not None else {}
            targets, max_runtime = filler_lib.load_targets(data)
            result = (targets, max_runtime)
        except (OSError, ValueError) as exc:
            self.log(f"filler config refused: {exc}")
            result = ([], filler_lib.DEFAULT_MAX_RUNTIME_S)
        self._filler_cache = (mtime, result)
        return result

    def _log_fill(self, plan: fill_lib.FillPlan) -> None:
        """Log the controller's reason once per change of reason, not once per tick."""
        if plan.key != self._fill_key:
            self._fill_key = plan.key
            self.log(f"fill: {plan.reason}")

    def _load_controller_state(self, conn) -> dict | None:
        """The fill controller's saved level and step timer, or None when none was saved or it is unreadable."""
        raw = filler_lib.state_get(conn, CONTROLLER_STATE_KEY)
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def submit_filler(self, conn, now: float, status: dict | None) -> list[str]:
        """Run the fill controller for this tick and submit the filler jobs it asks for. Returns the new job ids.

        With fill disabled, nothing is read from the process table, git or the store's DeepSeek history.
        """
        cfg = load_config(self.home)
        fill_cfg, pacing = cfg["fill"], cfg["pacing"]
        if self._controller is None:
            self._controller = fill_lib.FillController.from_state(self._load_controller_state(conn))
            self._saved_controller = self._controller.state()
        enabled = fill_cfg["enabled"]
        ps_text = self._process_table(now) if enabled else None
        laptop = self.gauges.read() if enabled else None
        decision = fill_lib.decide(
            conn, now, controller=self._controller, fill=fill_cfg, pacing=pacing, status=status,
            ps_text=ps_text, laptop=laptop, laptop_cfg=cfg["laptop"],
            paused=filler_lib.paused(conn), measured=self._measured_cost(conn, now, pacing),
        )
        self._log_fill(decision.plan)
        state = self._controller.state()
        if state != self._saved_controller:
            with db.tx(conn):
                filler_lib.state_set(conn, CONTROLLER_STATE_KEY, json.dumps(state))
            self._saved_controller = state
        if decision.plan.submit <= 0 or now < self._filler_retry:
            return []
        targets, max_runtime = self._filler_targets()
        submitted = []
        for _ in range(decision.plan.submit):
            job_id = self._filler_job(conn, now, targets, max_runtime)
            if job_id is None:
                break
            submitted.append(job_id)
        return submitted

    def _filler_job(self, conn, now: float, targets: list, max_runtime: float) -> str | None:
        """Queue one audit for the next due target. Returns the job id, or None when nothing is due or the label is refused."""
        found = filler_lib.next_target(conn, targets, head_of=filler_lib.module_head)
        if found is None:
            return None
        target, commit = found
        label = self.labeler("read", filler_lib.label_slug(target))
        if label is None:
            self._filler_retry = now + BACKLOG_RETRY_S
            self.log(f"filler {target.key}: label make refused; retry in {int(BACKLOG_RETRY_S)}s")
            return None
        job_id = db.new_job_id()
        prompt = db.job_dir(self.home, job_id) / "prompt.md"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text(backlog_lib.preamble_for("read", filler_lib.prompt_body(target, commit)), encoding="utf-8")
        job = {
            "id": job_id, "lane": "backlog", "kind": "read", "res": "light",
            "tier": db.default_tier("read"), "label": label, "workspace": target.workspace,
            "session": backlog_lib.SESSION, "harness": backlog_lib.SESSION, "prompt_path": str(prompt),
            "max_runtime_s": max_runtime,
        }  # fmt: skip
        with db.tx(conn):
            db.insert_job(conn, job, now)
            filler_lib.record_submission(conn, target, commit, job_id, now)
        self.log(f"filler {target.key} submitted {job_id} ({label}) at {commit[:12]}")
        return job_id

    def _proxy_status(self) -> dict | None:
        """The proxy status for this tick (None when down). Logs each change of its saturated flag."""
        status = self.proxy()
        saturated = bool(status and status.get("saturated"))
        if saturated != self._saturated_last:
            self.log(f"proxy saturated={saturated}")
        self._saturated_last = saturated
        return status

    def _worktree_plan(self, job: dict, settings: dict):
        """The job's worktree plan, cached until it launches or leaves the queue."""
        if job["id"] not in self._plans:
            self._plans[job["id"]] = plan_worktree(job, settings)
        return self._plans[job["id"]]

    def _prepare_worktree(self, conn, job: dict, plan, now: float, fetched: set) -> dict | None:
        """Create the job's worktree and point the job at it. Returns the updated job, or None.

        None means the job stays queued: the creation failed (retry scheduled) or the job
        moved on (cancelled) before the change was recorded.
        """
        repo, path = plan
        try:
            note = create_worktree(repo, path, branch_for(job["id"]), fetch=repo not in fetched)
        except WorktreeError as exc:
            self._worktree_failed(conn, job, now, exc)
            return None
        fetched.add(repo)
        if not db.transition(conn, job["id"], ("queued",), now, "auto-worktree", str(path),
                             workspace=str(path), orig_workspace=job["workspace"], detail=None):  # fmt: skip
            return None
        self._plans.pop(job["id"], None)
        if note:
            self.log(f"{job['id']} {note}")
        self.log(f"auto-worktree {job['id']} {path}")
        return {**job, "workspace": str(path), "orig_workspace": job["workspace"]}

    def _worktree_failed(self, conn, job: dict, now: float, exc: Exception) -> None:
        """Count one failed worktree attempt. The job retries after a minute; the third failure fails it.

        The job never falls back to writing in place: that is the hazard this step removes.
        The attempt count lives in the events table, so it survives a broker restart.
        """
        self._holds.pop(job["id"], None)
        self._plans.pop(job["id"], None)
        tries = db.count_events(conn, job["id"], "auto-worktree-failed") + 1
        if tries >= AUTO_WORKTREE_MAX_TRIES:
            reason = f"auto-worktree failed after {tries} tries: {exc}"
            db.transition(conn, job["id"], ("queued",), now, "failed", reason,
                          state="failed", finished_at=now, detail=reason)  # fmt: skip
            self.log(f"{job['id']} failed: {reason}")
            return
        message = f"auto-worktree failed: {exc}"
        db.transition(conn, job["id"], ("queued",), now, "auto-worktree-failed", str(exc),
                      detail=message, not_before=now + AUTO_WORKTREE_RETRY_S)  # fmt: skip
        self.log(f"{job['id']} {message}; retry in {int(AUTO_WORKTREE_RETRY_S)}s")

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
        note = "; ".join(part for part in (note, worktree_note(job)) if part)
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

    def _collect_launches(self, conn, now: float) -> None:
        """Record the outcome of every finished launch. Runs on the broker thread only.

        A launch that was cancelled while in the pool still ends here: its worker is
        stopped, because the ``started`` write no longer matches a running job.
        """
        for job_id, future in list(self._inflight.items()):
            if not future.done():
                continue
            del self._inflight[job_id]
            job = db.get_job(conn, job_id)
            try:
                pid, found, pgid = future.result()
            except CapacityRefused as exc:
                self._capacity_refused(conn, job, now, str(exc))
                continue
            except Exception as exc:  # a LaunchError, or a launcher bug: the job's attempt logic decides
                self._death(conn, job, now, f"launch failed: {exc}")
                continue
            if not self._move(
                conn, job_id, ("running",), now, "started", f"pid {pid}",
                pid=pid, pgid=pgid, pid_start=found.start, capacity_refusals=0,
            ):  # fmt: skip
                kill_group(pgid)
                self.log(f"{job_id} cancelled during launch; stopped pid {pid}")

    def _start_worker(self, job: dict, run_dir, env: dict):
        """Pool thread: run the launcher and identify the pid it returns. No database access."""
        pid = self.launcher(job, run_dir, env)
        found = probe(pid)
        for _ in range(PROBE_RETRIES - 1):
            if found.state != "unknown":
                break
            found = probe(pid)
        return pid, found, process_group(pid)

    def _launch(self, conn, job: dict, now: float) -> bool:
        """Mark one queued job running and hand its worker start to the launch pool.

        The job stays ``running`` with no pid until ``_collect_launches`` records the
        result, which is how a cancel during the launch is recognised.
        Returns False when the job was cancelled before the launch began.
        """
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
        launch_job = job
        if job["orig_workspace"]:
            launch_job = {**job, "prompt_path": str(worktree_prompt(run_dir, job))}
        self._inflight[job["id"]] = self.launch_pool.submit(self._start_worker, launch_job, run_dir, env)
        return True
