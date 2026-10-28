"""Workstream dispatcher: keeps one workstream's execution slots full from its ready queue.

One long-lived process per workstream (``dispatch run <WS>``, started detached by
``dispatch start``) computes ``orchestrate.ready_queue`` every tick, launches each
ready task that has a ``prompts/<task-id>.md`` through ``scripts/run_worker.py``
with a zero max-wait (launch and return), moves the task to ``wip`` through
``bundles.set_task``, and records each outcome itself. The lead waits on one
event stream (``dispatch wait <WS>``) and reads reports, decides and merges.

Bundle files, all in the workstream bundle directory:

- ``prompts/<task-id>.md``: worker prompt, optional ``---`` front matter with
  ``tier``, ``workspace``, ``kind`` (write|read) and ``max_wait`` (seconds).
- ``dispatch.json``: config the lead owns (slots, tier, workspace, paused, merge_wave).
- ``dispatch-state.json``: dispatcher state, rewritten atomically each tick.
- ``inbox.jsonl``: append-only events, one JSON object per line with ``seq``.
- ``inbox.cursor``: the last seq that ``dispatch wait`` printed.
- ``dispatch.pid``, ``dispatch.lock``, ``dispatch.log``, ``runs/<label>/``: process
  and run bookkeeping. ``reports/decisions.tsv`` gets one row per retry.

Every collaborator (launcher, liveness, dashboard runs, status writer, plan
loader, clock) is injected, so tests never start a worker.
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
from datetime import datetime, timezone
from pathlib import Path

from dash import labels as label_lib
from dash import orchestrate
from dash.liveness import pid_alive as system_alive
from dash.liveness import read_pid_file as _read_pid_file

TOOL = Path(__file__).resolve().parents[1]
RUN_WORKER = TOOL / "scripts" / "run_worker.py"
DEFAULTS = {
    "slots": 12,
    "tier": "auto",
    "workspace": None,
    "paused": False,
    "merge_wave": {"min_ready": 3, "max_age_min": 60},
}
DEFAULT_MAX_WAIT = 3600
POLL_S = 15
NUDGE_S = 1
KINDS = ("write", "read")
VERDICTS = ("PASS", "PARTIAL", "BLOCKED")
_STATUS_RE = re.compile(r"^STATUS: (\S+)")
_TIER_RE = re.compile(r"\btier=(\S+)")
_REASON_RE = re.compile(r"\breason=(\S+)")
_VERDICT_RE = re.compile(r"^[^A-Za-z]*(PASS|PARTIAL|BLOCKED)\b")


def read_pid_file(path) -> int | None:
    """Pid recorded in a run's ``<label>.pid`` file, or None."""
    return _read_pid_file(str(path))


def parse_status(line: str) -> dict:
    """The fields of one ``run_worker.py`` STATUS line."""
    text = (line or "").strip()
    state = _STATUS_RE.match(text)
    tier = _TIER_RE.search(text)
    reason = _REASON_RE.search(text)
    out = re.search(r"out=(.*?) err=", text)
    return {
        "state": state.group(1) if state else "failed",
        "tier": tier.group(1) if tier else "",
        "reason": reason.group(1) if reason else "no-status",
        "out": out.group(1) if out else "",
    }


def parse_prompt(text: str) -> tuple[dict, str]:
    """``(front matter, body)`` for a prompt file; no front matter gives ``{}``."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta: dict = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            meta[key.strip()] = value.strip().strip("\"'")
    return meta, text[end + len("\n---\n"):]


def verdict_of(out_path) -> str:
    """PASS, PARTIAL or BLOCKED from the first non-empty report line, else NONE."""
    try:
        text = Path(out_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "NONE"
    for line in text.splitlines():
        if line.strip():
            found = _VERDICT_RE.match(line.strip())
            return found.group(1) if found else "NONE"
    return "NONE"


def task_slug(ws: str, task_id: str) -> str:
    """Task id without its workstream prefix, for the label slug."""
    stripped = re.sub(rf"^{re.escape(ws)}[.\-_]?", "", task_id, flags=re.IGNORECASE)
    return stripped or task_id


def build_label(ws: str, task_id: str, launches: int) -> str:
    """``<ws>-exec-<task-slug>``; the Nth launch of a task adds ``-rN``."""
    return label_lib.make(ws.lower(), "exec", task_slug(ws, task_id), round=launches or None)


def worker_argv(label, tier, workspace, prompt, run_dir, max_wait=0, exclude=()) -> list[str]:
    """argv for one ``run_worker.py`` call. Max-wait 0 means launch-and-return."""
    argv = [sys.executable, str(RUN_WORKER), str(tier), str(label), str(workspace), str(prompt), str(run_dir), str(max_wait)]
    argv.extend(f"--exclude={name}" for name in exclude)
    return argv


def default_launcher(argv) -> str:
    """Run one ``run_worker.py`` call and return its STATUS line."""
    proc = subprocess.run(list(argv), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
    lines = [line for line in proc.stdout.splitlines() if line.startswith("STATUS:")]
    return lines[-1] if lines else f"STATUS: failed reason=no-status exit={proc.returncode}"


def default_runs():
    """Dashboard run rows for the workstream, or None when the dashboard is unreachable."""
    from dash.bundles import _dash_url

    return orchestrate.dashboard_runs(_dash_url(), None)


def default_load_body(ws: str):
    from dash import bundles

    def load():
        _source, body = bundles.resolve_plan(ws)
        if not isinstance(body, dict):
            raise RuntimeError(f"no plan for {ws}")
        return body

    return load


def default_set_status(ws: str):
    from dash import bundles

    def setter(task_id: str, status: str) -> None:
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            code = bundles.set_task(ws, task_id, status, None, None)
        if code != 0:
            raise RuntimeError(f"set {task_id} {status} failed (code {code})")

    return setter


def _now_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_config(bundle: Path) -> dict:
    """``dispatch.json`` merged over the defaults. Raises ValueError on a bad file."""
    path = bundle / "dispatch.json"
    cfg = json.loads(json.dumps(DEFAULTS))
    if not path.is_file():
        return cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"dispatch.json unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("dispatch.json must be a JSON object")
    cfg.update({key: value for key, value in raw.items() if key in DEFAULTS})
    merge = dict(DEFAULTS["merge_wave"])
    merge.update(raw.get("merge_wave") or {})
    cfg["merge_wave"] = merge
    try:
        cfg["slots"] = int(cfg["slots"])
        cfg["merge_wave"] = {"min_ready": int(merge["min_ready"]), "max_age_min": float(merge["max_age_min"])}
    except (TypeError, ValueError) as exc:
        raise ValueError(f"dispatch.json has a non-numeric slots or merge_wave: {exc}") from exc
    return cfg


def update_config(bundle: Path, **changes) -> dict:
    """Merge ``changes`` into ``dispatch.json`` and return the file as written."""
    path = bundle / "dispatch.json"
    raw = read_json(path, {})
    if not isinstance(raw, dict):
        raw = {}
    raw.update({key: value for key, value in changes.items() if value is not None})
    write_json_atomic(path, raw)
    return raw


def last_seq(inbox: Path) -> int:
    seq = 0
    try:
        with inbox.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    seq = max(seq, int(json.loads(line)["seq"]))
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        return 0
    return seq


def read_events(bundle: Path, after: int) -> list[dict]:
    """Every event with ``seq`` greater than ``after``, in order."""
    events = []
    try:
        with (bundle / "inbox.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and int(event.get("seq", 0)) > after:
                    events.append(event)
    except OSError:
        return []
    return events


def cursor_of(bundle: Path) -> int:
    try:
        return int((bundle / "inbox.cursor").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def format_event(event: dict) -> str:
    skip = {"seq", "ts", "type", "task", "label", "ws"}
    extra = " ".join(f"{key}={json.dumps(value) if isinstance(value, (list, dict)) else value}" for key, value in event.items() if key not in skip and value not in (None, ""))
    parts = [f"#{event.get('seq')}", str(event.get("type")), str(event.get("task") or "-"), str(event.get("label") or "-")]
    return " ".join(parts + ([extra] if extra else []))


def wait_for_events(bundle: Path, after=None, timeout=None, clock=time.time, sleep=time.sleep) -> tuple[int, list[dict]]:
    """Block until events past the cursor (or ``after``) exist, then advance the cursor.

    Returns ``(0, events)`` when events arrived, ``(3, [])`` on timeout.
    """
    start = cursor_of(bundle) if after is None else int(after)
    deadline = None if timeout is None else clock() + float(timeout)
    while True:
        events = read_events(bundle, start)
        if events:
            (bundle / "inbox.cursor").write_text(str(max(int(e["seq"]) for e in events)) + "\n", encoding="utf-8")
            return 0, events
        if deadline is not None and clock() >= deadline:
            return 3, []
        sleep(NUDGE_S)


class Lock:
    """One dispatcher per workstream: an exclusive flock held for the process life."""

    def __init__(self, bundle: Path):
        self.path = bundle / "dispatch.lock"
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


class Dispatcher:
    """One workstream's dispatch loop state and one tick of its logic."""

    def __init__(self, ws, bundle, *, launcher=None, alive=None, runs=None, set_status=None, load_body=None, now=None):
        self.ws = ws
        self.bundle = Path(bundle)
        self.launcher = launcher or default_launcher
        self.alive = alive or system_alive
        self.runs = runs or default_runs
        self.set_status = set_status or default_set_status(ws)
        self.load_body = load_body or default_load_body(ws)
        self.now = now or time.time
        self.dry = False
        self.dry_events: list[dict] = []
        self.stopping = False
        self.state = {
            "ws": ws,
            "pid": None,
            "started": None,
            "heartbeat": None,
            "tasks": {},
            "needs_prompt": [],
            "errors": {},
            "pr_since": {},
            "merge_key": None,
            "idle_key": None,
        }
        self.state.update(read_json(self.state_path, {}))
        self.seq = last_seq(self.inbox_path)

    @property
    def state_path(self) -> Path:
        return self.bundle / "dispatch-state.json"

    @property
    def inbox_path(self) -> Path:
        return self.bundle / "inbox.jsonl"

    def prompt_path(self, task_id: str) -> Path:
        return self.bundle / "prompts" / f"{task_id}.md"

    def persist(self) -> None:
        if self.dry:
            return
        self.state["heartbeat"] = self.now()
        write_json_atomic(self.state_path, self.state)

    def emit(self, event_type: str, task=None, label=None, **fields) -> dict:
        self.seq += 1
        event = {"seq": self.seq, "ts": _now_iso(self.now()), "type": event_type, "ws": self.ws, "task": task, "label": label}
        event.update(fields)
        if self.dry:
            self.dry_events.append(event)
            return event
        self.bundle.mkdir(parents=True, exist_ok=True)
        with self.inbox_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
        return event

    def _error(self, task, phase: str, message: str) -> None:
        key = f"{task}|{phase}|{message}"
        if key in self.state["errors"]:
            return
        self.state["errors"][key] = self.now()
        self.emit("error", task=task, phase=phase, reason=message)

    def _clear_errors(self, task) -> None:
        prefix = f"{task}|"
        for key in [key for key in self.state["errors"] if key.startswith(prefix)]:
            del self.state["errors"][key]

    def _plan_status(self, body, task_id: str):
        for group in orchestrate._groups_of(body):
            for task in (group.get("tasks") or []) if isinstance(group, dict) else []:
                if isinstance(task, dict) and task.get("id") == task_id:
                    return task.get("status")
        return None

    def _move(self, task_id: str, status: str) -> None:
        """Set a task's status only while the plan still shows ``wip`` (or ``todo`` for launch)."""
        if self.dry:
            return
        expect = "todo" if status == "wip" else "wip"
        if self._plan_status(self.load_body(), task_id) != expect:
            return
        self.set_status(task_id, status)

    def tracked_running(self) -> dict:
        return {tid: t for tid, t in self.state["tasks"].items() if t.get("status") == "running"}

    def tick(self, dry_run: bool = False) -> dict:
        """One loop iteration. ``dry_run`` decides and reports, with no side effects."""
        self.dry = dry_run
        self.dry_events = []
        now = self.now()
        try:
            cfg = load_config(self.bundle)
            config_error = None
        except ValueError as exc:
            cfg = json.loads(json.dumps(DEFAULTS))
            cfg["paused"] = True
            config_error = str(exc)
        if config_error:
            self._error(None, "config", config_error)
        if not dry_run:
            self._observe(now, cfg)
        body = self.load_body()
        by_id, _deps, order = orchestrate.plan_tasks(body)
        runs = self.runs()
        running = self.tracked_running()
        live_map = orchestrate.live_by_task(runs or [], self.ws, order)
        live_map.update({tid: t["label"] for tid, t in running.items()})
        dash_live = {
            row.get("label")
            for row in (runs or [])
            if isinstance(row, dict)
            and row.get("workstream") == self.ws
            and row.get("status") in ("running", "queued")
            and row.get("label")
        }
        live_labels = {t["label"] for t in running.values()} | dash_live
        capacity = cfg["slots"] - len(live_labels)
        queue = orchestrate.ready_queue(body, live_map)
        launchable = [row for row in queue if row.get("kind") != "gate" and not row.get("live_run")]
        launched = 0
        paused = bool(cfg["paused"]) or config_error is not None
        for row in launchable:
            if capacity <= 0 or paused:
                break
            tid = row["id"]
            if tid in running:
                continue
            if not self.prompt_path(tid).is_file():
                if tid not in self.state["needs_prompt"]:
                    self.state["needs_prompt"].append(tid)
                    self.emit("needs-prompt", task=tid, title=row.get("title"))
                continue
            if tid in self.state["needs_prompt"]:
                self.state["needs_prompt"].remove(tid)
            try:
                outcome = self.launch(tid, cfg, retry_from=None)
            except Exception as exc:
                self._error(tid, "launch", str(exc))
                continue
            if outcome == "none-available":
                break
            if outcome == "ok":
                launched += 1
                capacity -= 1
        self._merge_wave(by_id, cfg, now)
        if not launchable and not live_labels and not launched:
            todo = sorted(tid for tid, task in by_id.items() if task.get("status") == "todo")
            if todo:
                key = ",".join(todo)
                if key != self.state["idle_key"]:
                    self.state["idle_key"] = key
                    self.emit("idle", todo=todo)
            else:
                self.state["idle_key"] = None
        elif launched or live_labels or launchable:
            self.state["idle_key"] = None
        self.persist()
        return {"launched": launched, "capacity": capacity, "paused": paused, "events": list(self.dry_events)}

    def _merge_wave(self, by_id, cfg, now) -> None:
        pr_ids = sorted(tid for tid, task in by_id.items() if task.get("status") == "pr")
        since = self.state["pr_since"]
        for tid in pr_ids:
            since.setdefault(tid, now)
        for tid in [tid for tid in since if tid not in pr_ids]:
            del since[tid]
        if not pr_ids:
            self.state["merge_key"] = None
            return
        merge = cfg["merge_wave"]
        oldest_min = (now - min(since[tid] for tid in pr_ids)) / 60.0
        count_hit = len(pr_ids) >= merge["min_ready"]
        age_hit = oldest_min >= merge["max_age_min"]
        if not (count_hit or age_hit):
            return
        key = ",".join(pr_ids)
        if key != self.state["merge_key"]:
            self.state["merge_key"] = key
            self.emit("merge-wave", tasks=pr_ids, reason="count" if count_hit else "age", oldest_min=round(oldest_min, 1))

    def launch(self, task_id: str, cfg: dict, retry_from=None) -> str:
        """Launch one task's worker. Returns ``ok``, ``none-available`` or ``failed``."""
        meta, body = parse_prompt(self.prompt_path(task_id).read_text(encoding="utf-8"))
        kind = meta.get("kind") or "write"
        if kind not in KINDS:
            raise ValueError(f"prompt kind {kind!r}: use write or read")
        workspace = meta.get("workspace") or cfg.get("workspace")
        if not workspace:
            raise ValueError("no workspace: set workspace in dispatch.json or the prompt front matter")
        tier = meta.get("tier") or cfg["tier"]
        try:
            max_wait = int(meta.get("max_wait") or DEFAULT_MAX_WAIT)
        except ValueError as exc:
            raise ValueError(f"prompt max_wait {meta.get('max_wait')!r} is not a number") from exc
        prev = self.state["tasks"].get(task_id) or {}
        launches = int(prev.get("launches") or 0)
        attempt = int(prev.get("attempt") or 0) + 1 if retry_from else 1
        label = build_label(self.ws, task_id, launches)
        verdict = label_lib.check(label, ws=self.ws)
        if not verdict["ok"]:
            raise ValueError(f"bad label {label}: {verdict['fix']}")
        exclude = [retry_from] if retry_from else []
        if retry_from:
            tier = "auto"
        if self.dry:
            self.emit("would-launch", task=task_id, label=label, tier=tier, kind=kind, workspace=str(workspace))
            return "ok"
        run_dir = self.bundle / "runs" / label
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt_copy = run_dir / "prompt.md"
        prompt_copy.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
        status = parse_status(self.launcher(worker_argv(label, tier, workspace, prompt_copy, run_dir, 0, exclude)))
        if read_pid_file(run_dir / f"{label}.pid") is None:
            if status["reason"] == "none-available":
                return "none-available"
            raise RuntimeError(f"launch of {label} did not start: {status['reason']}")
        self.state["tasks"][task_id] = {
            "label": label,
            "tier": status["tier"],
            "requested": tier,
            "attempt": attempt,
            "launches": launches + 1,
            "kind": kind,
            "workspace": str(workspace),
            "prompt": str(prompt_copy),
            "run_dir": str(run_dir),
            "out": status["out"],
            "status": "running",
            "started": self.now(),
            "max_wait": max_wait,
            "overdue": False,
        }
        self._clear_errors(task_id)
        self.persist()
        try:
            if self._plan_status(self.load_body(), task_id) == "todo":
                self.set_status(task_id, "wip")
        except Exception as exc:
            self._error(task_id, "status", f"could not mark wip: {exc}")
        self.emit("launched", task=task_id, label=label, tier=status["tier"], attempt=attempt, kind=kind)
        if retry_from:
            self.emit("retried", task=task_id, label=label, from_tier=retry_from, to_tier=status["tier"], reason="no-verdict")
            self._decision_row(task_id, label, retry_from, status["tier"])
        return "ok"

    def _decision_row(self, task_id: str, label: str, from_tier: str, to_tier: str) -> None:
        path = self.bundle / "reports" / "decisions.tsv"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            path.write_text("ts\tdecision\twhy\tevidence\n", encoding="utf-8")
        row = [
            _now_iso(self.now()),
            f"retry {task_id} as {label} on auto, excluding {from_tier} (now {to_tier})",
            "first attempt ended with no PASS/PARTIAL/BLOCKED verdict",
            str(self.bundle / "runs" / label),
        ]
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\t".join(cell.replace("\t", " ").replace("\n", " ") for cell in row) + "\n")

    def _observe(self, now, cfg) -> None:
        for task_id, task in list(self.tracked_running().items()):
            try:
                self._observe_one(task_id, task, now)
            except Exception as exc:
                self._error(task_id, "observe", str(exc))
        self.persist()

    def _observe_one(self, task_id: str, task: dict, now: float) -> None:
        run_dir = Path(task["run_dir"])
        pid = read_pid_file(run_dir / f"{task['label']}.pid")
        if pid is not None and self.alive(pid):
            if not task.get("overdue") and now - task["started"] > task["max_wait"]:
                task["overdue"] = True
                self.emit("needs-lead", task=task_id, label=task["label"], reason="overdue", max_wait=task["max_wait"])
            return
        argv = worker_argv(task["label"], task["requested"], task["workspace"], task["prompt"], run_dir, 0)
        status = parse_status(self.launcher(argv))
        if status["state"] == "running":
            task["tier"] = status["tier"] or task["tier"]
            return
        out = status["out"] or task["out"]
        verdict = verdict_of(out) if status["state"] == "done" else "NONE"
        tier = status["tier"] or task["tier"]
        self.emit(
            "finished",
            task=task_id,
            label=task["label"],
            verdict=verdict,
            out=out,
            tier=tier,
            duration_s=round(now - task["started"]),
        )
        task["status"] = "finished"
        if verdict == "PASS":
            target = "pr" if task["kind"] == "write" else "done"
            self._move(task_id, target)
            task["status"] = target
            return
        if verdict in VERDICTS:
            self._move_needs_lead(task_id, task, verdict)
            return
        if task["attempt"] < 2:
            self._retry(task_id, task, tier)
            return
        self._move_needs_lead(task_id, task, "no-verdict-twice")

    def _move_needs_lead(self, task_id: str, task: dict, reason: str) -> None:
        task["status"] = "needs-lead"
        self.emit("needs-lead", task=task_id, label=task["label"], reason=reason)

    def _retry(self, task_id: str, task: dict, failed_tier: str) -> None:
        cfg = load_config(self.bundle)
        try:
            outcome = self.launch(task_id, cfg, retry_from=failed_tier)
        except Exception as exc:
            self._error(task_id, "retry", str(exc))
            outcome = "failed"
        if outcome != "ok":
            self._move_needs_lead(task_id, task, "retry-launch-failed")

    def status_summary(self) -> dict:
        tasks = self.state.get("tasks") or {}
        running = {tid: t for tid, t in tasks.items() if t.get("status") == "running"}
        return {
            "ws": self.ws,
            "pid": self.state.get("pid"),
            "live": bool(self.state.get("pid")) and self.alive(self.state["pid"]),
            "heartbeat": self.state.get("heartbeat"),
            "heartbeat_age_s": None if not self.state.get("heartbeat") else round(self.now() - self.state["heartbeat"]),
            "slots_used": len(running),
            "tasks": {tid: {k: t.get(k) for k in ("label", "tier", "attempt", "status", "run_dir", "out")} for tid, t in tasks.items()},
            "last_seq": last_seq(self.inbox_path),
            "paused": self._paused(),
        }

    def _paused(self) -> bool:
        try:
            return bool(load_config(self.bundle)["paused"])
        except ValueError:
            return True

    def loop(self, lock: Lock, poll_s: float = POLL_S) -> None:
        """Run ticks until stopped. A worker finishing wakes the loop at once."""
        self.state["pid"] = os.getpid()
        self.state["started"] = self.now()
        self.persist()

        def handle(signum, frame):
            self.stopping = True

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)
        last_tick = 0.0
        while not self.stopping:
            try:
                if self.now() - last_tick >= poll_s or self._any_finished():
                    self.tick()
                    last_tick = self.now()
            except Exception as exc:
                self._error(None, "tick", str(exc))
            time.sleep(NUDGE_S)
        self.state["pid"] = None
        self.emit("stopped")
        self.persist()
        lock.release()

    def _any_finished(self) -> bool:
        for task in self.tracked_running().values():
            pid = read_pid_file(Path(task["run_dir"]) / f"{task['label']}.pid")
            if pid is None or not self.alive(pid):
                return True
        return False


DISPATCH_USAGE = """usage: python3 agent_dash.py dispatch <command> <WS> [options]

  start <WS> [--slots N] [--tier T] [--workspace P]   start the dispatcher detached (idempotent)
  stop <WS>                                          SIGTERM the dispatcher (its workers keep running)
  pause <WS> | resume <WS>                           hold or release launches (dispatch.json paused)
  status <WS> [--json]                               liveness, heartbeat, slots, per-task runs
  wait <WS> [--after SEQ] [--timeout S]              print events past the cursor; exit 3 on timeout
  once <WS> [--dry-run]                              one tick; --dry-run only reports what it would do
  run <WS>                                           the loop in the foreground (start runs this)
"""


def live_pid(bundle: Path):
    """The dispatcher pid from ``dispatch.pid`` when that process is alive, else None."""
    pid = read_pid_file(bundle / "dispatch.pid")
    return pid if pid and system_alive(pid) else None


def _value(flags: list, name: str):
    if name not in flags:
        return None
    pos = flags.index(name)
    if pos + 1 >= len(flags):
        raise ValueError(f"{name} needs a value")
    value = flags[pos + 1]
    del flags[pos:pos + 2]
    return value


def cmd_dispatch(argv: list[str]) -> int:
    """``dispatch`` subcommands. Returns the process exit code."""
    from dash import bundles, errors

    if not argv or argv[0] in ("-h", "--help"):
        print(DISPATCH_USAGE, end="")
        return 0 if argv else 2
    cmd, rest = argv[0], list(argv[1:])
    json_mode = "--json" in rest
    rest = [item for item in rest if item != "--json"]
    dry = "--dry-run" in rest
    rest = [item for item in rest if item != "--dry-run"]
    if not rest or rest[0].startswith("-"):
        print(DISPATCH_USAGE, end="")
        return 2
    ws, flags = rest[0], rest[1:]
    bundle = bundles.bundle_dir(ws)
    if bundle is None:
        print(errors.bundle_not_found(ws))
        return errors.NOT_FOUND
    try:
        slots = _value(flags, "--slots")
        tier = _value(flags, "--tier")
        workspace = _value(flags, "--workspace")
        after = _value(flags, "--after")
        timeout = _value(flags, "--timeout")
    except ValueError as exc:
        print(f"{exc}. Run: python3 agent_dash.py dispatch --help")
        return 2
    if flags:
        print(DISPATCH_USAGE, end="")
        return 2
    if cmd == "start":
        return _start(ws, bundle, slots, tier, workspace)
    if cmd == "stop":
        return _stop(ws, bundle)
    if cmd in ("pause", "resume"):
        update_config(bundle, paused=(cmd == "pause"))
        print(f"{ws} dispatch {cmd}d")
        return 0
    if cmd == "status":
        summary = Dispatcher(ws, bundle).status_summary()
        if json_mode:
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            print(f"{ws} dispatcher: {'live pid ' + str(summary['pid']) if summary['live'] else 'not running'}"
                  f"{' (paused)' if summary['paused'] else ''}")
            age = "-" if summary["heartbeat_age_s"] is None else f"{summary['heartbeat_age_s']}s"
            print(f"  heartbeat age {age}  slots used {summary['slots_used']}  last seq {summary['last_seq']}")
            for tid, task in sorted(summary["tasks"].items()):
                print(f"  {tid} [{task.get('status')}] {task.get('label')} tier={task.get('tier')} attempt={task.get('attempt')}")
        return 0
    if cmd == "wait":
        try:
            code, events = wait_for_events(bundle, after=None if after is None else int(after), timeout=None if timeout is None else float(timeout))
        except ValueError:
            print("--after takes an integer seq and --timeout takes seconds", file=sys.stderr)
            return 2
        for event in events:
            print(format_event(event))
        if code == 3:
            print(f"no events for {ws} within {timeout}s", file=sys.stderr)
        return code
    if cmd == "once":
        dispatcher = Dispatcher(ws, bundle)
        before = dispatcher.seq
        result = dispatcher.tick(dry_run=dry)
        if dry:
            for event in result["events"]:
                print(format_event(event))
            print(f"dry-run: {result['launched']} would launch, capacity {result['capacity']}, paused={result['paused']}")
        else:
            for event in read_events(bundle, before):
                print(format_event(event))
            print(f"tick: launched {result['launched']}, capacity {result['capacity']}, paused={result['paused']}")
        return 0
    if cmd == "run":
        return _run(ws, bundle)
    print(DISPATCH_USAGE, end="")
    return 2


def _run(ws: str, bundle: Path) -> int:
    lock = Lock(bundle)
    if not lock.acquire():
        print(f"dispatcher already running for {ws}: pid {live_pid(bundle) or 'unknown'}", file=sys.stderr)
        return 4
    (bundle / "dispatch.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        Dispatcher(ws, bundle).loop(lock)
    finally:
        if read_pid_file(bundle / "dispatch.pid") == os.getpid():
            (bundle / "dispatch.pid").unlink(missing_ok=True)
    return 0


def _start(ws: str, bundle: Path, slots, tier, workspace) -> int:
    running = live_pid(bundle)
    if running:
        print(f"{ws} dispatcher already running: pid {running}")
        return 0
    if slots is not None:
        try:
            int(slots)
        except ValueError:
            print(f"--slots takes an integer, got {slots!r}")
            return 2
    update_config(bundle, slots=None if slots is None else int(slots), tier=tier, workspace=workspace)
    try:
        load_config(bundle)
    except ValueError as exc:
        print(f"{exc}. Fix dispatch.json, then run dispatch start again")
        return 2
    bundle.mkdir(parents=True, exist_ok=True)
    with (bundle / "dispatch.log").open("ab") as log:
        proc = subprocess.Popen(
            [sys.executable, str(TOOL / "agent_dash.py"), "dispatch", "run", ws],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(TOOL),
        )
    deadline = time.time() + 5
    while time.time() < deadline and read_pid_file(bundle / "dispatch.pid") != proc.pid:
        time.sleep(0.1)
    if read_pid_file(bundle / "dispatch.pid") != proc.pid:
        print(f"{ws} dispatcher did not come up; see {bundle / 'dispatch.log'}")
        return 1
    print(f"{ws} dispatcher started: pid {proc.pid}")
    return 0


def _stop(ws: str, bundle: Path) -> int:
    pid = live_pid(bundle)
    if pid is None:
        print(f"{ws} dispatcher is not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    print(f"{ws} dispatcher pid {pid}: SIGTERM sent (launched workers keep running)")
    return 0
