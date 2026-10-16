"""Process samples by exact comm. Argv is not stored.

Liveness for grok, muse, and glm reads pid, start time, and the label
token only. A run is running when that pid is still that tool's process,
or when a live process of that tool carries ``[label: <label>]``.
"""

from __future__ import annotations

import re
import subprocess
import time

NAME = "process"
INTERVAL_S = 5
COMMS = ("cursor-agent", "claude", "grok", "muse", "muse-bin")
_COMM = set(COMMS)
# Tool source -> OS comm. ``grok`` the binary is the GLM CLI; cursor-agent is Grok.
SOURCE_COMMS = {
    "grok": frozenset({"cursor-agent"}),
    "muse": frozenset({"muse", "muse-bin"}),
    "glm": frozenset({"grok"}),
}
_LABEL = re.compile(r"\[label:\s*([^\]]+?)\]")
_LINE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s*$")
_LABEL_OK = re.compile(r"^[\w.+-]{1,80}$")


def poll(store, cfg, now: float) -> None:
    del cfg
    table = _ps()
    matched = [row for row in table if row["comm"] in _COMM]
    matched_pids = {row["pid"] for row in matched}
    parents = [row for row in matched if row["ppid"] not in matched_pids]
    kids, rss = _children(table)
    for parent in parents:
        label = label_from_pid(parent["pid"])
        store.add_process_sample(
            {
                "ts": now,
                "pid": parent["pid"],
                "comm": parent["comm"],
                "rss_kb": _tree_rss(parent["pid"], kids, rss),
                "elapsed_s": parent["elapsed_s"],
                "label": label,
                "run_id": store.match_run(parent["pid"], label),
            }
        )


def live_agent_processes(now: float | None = None) -> list[dict]:
    """Live grok, muse, and glm processes.

    Each row is ``pid``, ``comm``, ``started_at``, and ``label``. The
    command line is read only to extract the label token and is not returned.
    """
    now = time.time() if now is None else float(now)
    labels = _labels_by_pid()
    rows = []
    wanted = set().union(*SOURCE_COMMS.values())
    for row in _ps():
        if row["comm"] not in wanted:
            continue
        rows.append(
            {
                "pid": row["pid"],
                "comm": row["comm"],
                "started_at": now - float(row["elapsed_s"]),
                "label": labels.get(row["pid"]),
            }
        )
    return rows


def match_agent(source: str, pid, label, processes: list[dict]):
    """The live process for this run, by pid or by ``[label: ...]``."""
    comms = SOURCE_COMMS.get(source)
    if not comms:
        return None
    number = _pid(pid)
    if number is not None:
        for row in processes:
            if row["pid"] == number and row["comm"] in comms:
                return row
    text = label if isinstance(label, str) and label else None
    if text:
        for row in processes:
            if row["comm"] in comms and row.get("label") == text:
                return row
    return None


def apply_liveness(store, run: dict, source: str, processes) -> None:
    """Set ``running`` only while the process is alive.

    When the process is gone and the stored row was ``running``, mark it
    ``done`` (a labeled or verdicted run) or ``unknown``, with ``ended_at``
    at the last activity. A failed process listing (``processes is None``)
    leaves the row alone. Other terminal statuses are not rewritten.
    """
    if processes is None or not run.get("run_id"):
        return
    existing = _existing_run(store, run["run_id"])
    pid = run.get("pid")
    if pid is None and existing:
        pid = existing.get("pid")
    label = run.get("label") or (existing or {}).get("label")
    proc = match_agent(source, pid, label, processes)
    if proc:
        run["status"] = "running"
        run["pid"] = proc["pid"]
        run["ended_at"] = None
        return
    if not existing or existing.get("status") != "running":
        return
    run["status"] = "done" if label or existing.get("verdict") else "unknown"
    run["ended_at"] = _last_activity(store, run["run_id"], existing.get("started_at") or run.get("started_at"))
    run["pid"] = None


def reconcile_running(store, now: float, processes=None) -> int:
    """End grok, muse, and glm rows still marked running after the process is gone.

    An unlabeled row that shares a session with a labeled run is linked to
    that run (``parent_session_id`` = the labeled run id) so it is not a
    second agent. Returns how many rows were ended.
    """
    if processes is None:
        processes = live_agent_processes(now)
    ended = 0
    for row in store.running_tool_runs():
        source = row.get("source")
        if source not in SOURCE_COMMS:
            continue
        proc = match_agent(source, row.get("pid"), row.get("label"), processes)
        if proc:
            if row.get("pid") != proc["pid"]:
                store.upsert_run(
                    {
                        "run_id": row["run_id"],
                        "pid": proc["pid"],
                        "status": "running",
                        "ended_at": None,
                    }
                )
            continue
        patch = {
            "run_id": row["run_id"],
            "status": "done" if row.get("label") or row.get("verdict") else "unknown",
            "ended_at": _last_activity(store, row["run_id"], row.get("started_at")) or float(now),
            "pid": None,
        }
        parent = _shadow_parent(store, row)
        if parent:
            patch["parent_session_id"] = parent
        store.upsert_run(patch)
        ended += 1
    return ended


def _labels_by_pid() -> dict:
    """Pid -> label token. The rest of the command line is discarded."""
    try:
        proc = subprocess.run(
            ["ps", "-axww", "-o", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    found = {}
    for line in proc.stdout.splitlines():
        label = label_from_args(line)
        if not label:
            continue
        pid = _pid(line.strip().split(None, 1)[0] if line.strip() else None)
        if pid is not None:
            found[pid] = label
    return found


def _pid(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _existing_run(store, run_id):
    getter = getattr(store, "get_run", None)
    if not callable(getter):
        return None
    try:
        return getter(run_id)
    except Exception:
        return None


def _last_activity(store, run_id, started_at):
    latest = getattr(store, "last_activity", None)
    if callable(latest):
        try:
            found = latest(run_id, started_at)
        except Exception:
            found = None
        if found is not None:
            return found
    return started_at


def _shadow_parent(store, row):
    if row.get("source") != "grok" or row.get("label") or not row.get("session_id"):
        return None
    finder = getattr(store, "session_runs", None)
    if not callable(finder):
        return None
    try:
        siblings = finder(row["session_id"])
    except Exception:
        return None
    labeled = [
        item
        for item in siblings
        if item.get("source") == "grok" and item.get("label") and item.get("run_id") != row["run_id"]
    ]
    if not labeled:
        return None
    labeled.sort(key=lambda item: item.get("started_at") or 0, reverse=True)
    return labeled[0]["run_id"]


def label_from_args(text: str) -> str | None:
    match = _LABEL.search(text or "")
    if not match:
        return None
    label = match.group(1).strip()
    if not _LABEL_OK.fullmatch(label):
        return None
    return label


def label_from_pid(pid: int) -> str | None:
    try:
        proc = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "args="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    label = label_from_args(proc.stdout)
    return label


def parse_etime(text: str) -> float:
    days = 0
    body = text.strip()
    if "-" in body:
        day, body = body.split("-", 1)
        days = int(day)
    parts = [int(part) for part in body.split(":")]
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    else:
        return 0.0
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def parse_ps(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        comm = match.group(6)
        if "/" in comm:
            comm = comm.rsplit("/", 1)[-1]
        try:
            elapsed = parse_etime(match.group(5))
        except ValueError:
            continue
        rows.append(
            {
                "pid": int(match.group(1)),
                "ppid": int(match.group(2)),
                "pgid": int(match.group(3)),
                "rss_kb": int(match.group(4)),
                "elapsed_s": elapsed,
                "comm": comm,
            }
        )
    return rows


def _ps() -> list[dict]:
    proc = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,rss=,etime=,comm="],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        raise RuntimeError("ps failed")
    return parse_ps(proc.stdout)


def _children(table: list[dict]) -> tuple[dict, dict]:
    kids: dict = {}
    rss = {}
    for row in table:
        rss[row["pid"]] = row["rss_kb"]
        kids.setdefault(row["ppid"], []).append(row["pid"])
    return kids, rss


def _tree_rss(root: int, kids: dict, rss: dict) -> int:
    total = rss.get(root, 0)
    seen = {root}
    stack = list(kids.get(root, ()))
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += rss.get(pid, 0)
        stack.extend(kids.get(pid, ()))
    return total
