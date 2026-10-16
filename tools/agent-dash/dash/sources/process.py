"""Process samples by exact comm. Argv is not stored.

Liveness for grok, muse, and glm reads pid, start time, and the label
token only. A run is running when that pid is still that tool's process,
or when a live process of that tool carries ``[label: <label>]``. A muse
run is also live when a live muse process has ``--prompt-file`` ending in
``/<label>.md``, or ``--session-id`` equal to the run's session uuid.
The command line is read only to extract those tokens; argv is not stored.
Versioned ``muse-bin-<ver>`` comms count as ``muse-bin``.
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
_UUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
_SESSION = re.compile(r"--session-id(?:=|\s+)(" + _UUID + r")")
_PROMPT = re.compile(r"--prompt-file(?:=|\s+)(\S+)")
_UUID_OK = re.compile(r"^" + _UUID + r"$")


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

    Each row is ``pid``, ``comm``, ``started_at``, ``label``, and (for
    muse processes) ``prompt_label`` and ``session_id``. The command line
    is read only to extract the bracket label, the ``--prompt-file`` label,
    and ``--session-id``. The path and the rest of argv are not returned.
    """
    now = time.time() if now is None else float(now)
    tokens = _tokens_by_pid()
    rows = []
    wanted = set().union(*SOURCE_COMMS.values())
    muse_comms = SOURCE_COMMS["muse"]
    for row in _ps():
        if row["comm"] not in wanted:
            continue
        token = tokens.get(row["pid"], {})
        muse = row["comm"] in muse_comms
        prompt_label = token.get("prompt_label") if muse else None
        label = token.get("label")
        if muse and not label:
            label = prompt_label
        rows.append(
            {
                "pid": row["pid"],
                "comm": row["comm"],
                "started_at": now - float(row["elapsed_s"]),
                "label": label,
                "prompt_label": prompt_label,
                "session_id": token.get("session_id") if muse else None,
            }
        )
    return rows


def match_agent(source: str, pid, label, processes: list[dict], session_id=None):
    """The live process for this run.

    Match by pid, by ``[label: ...]``, by a muse ``--prompt-file`` whose
    path ends in ``/<label>.md``, or by a muse ``--session-id`` uuid.
    """
    comms = SOURCE_COMMS.get(source)
    if not comms:
        return None
    number = _pid(pid)
    if number is not None:
        for row in processes:
            if row["pid"] == number and _canon_comm(row.get("comm")) in comms:
                return row
    text = label if isinstance(label, str) and label else None
    if text:
        for row in processes:
            if _canon_comm(row.get("comm")) not in comms:
                continue
            if row.get("label") == text:
                return row
            if source == "muse" and row.get("prompt_label") == text:
                return row
    sid = session_id if isinstance(session_id, str) and _UUID_OK.match(session_id) else None
    if sid and source == "muse":
        want = sid.lower()
        for row in processes:
            got = row.get("session_id")
            if (
                _canon_comm(row.get("comm")) in comms
                and isinstance(got, str)
                and got.lower() == want
            ):
                return row
    return None


def apply_liveness(store, run: dict, source: str, processes) -> None:
    """Set ``running`` while the process is alive.

    When the process is gone and the stored row was ``running``, mark it
    ``done`` (a labeled or verdicted run) or ``unknown``, with ``ended_at``
    at the last activity. An open ``unknown`` row is revived when a process
    matches, and closed (``done``, with ``ended_at``) when it does not.
    A failed process listing (``processes is None``) leaves the row alone.
    Other terminal statuses are not rewritten. An ``unknown`` row that
    already has ``ended_at`` is left alone.
    """
    if processes is None or not run.get("run_id"):
        return
    existing = _existing_run(store, run["run_id"])
    pid = run.get("pid")
    if pid is None and existing:
        pid = existing.get("pid")
    label = run.get("label") or (existing or {}).get("label")
    proc = match_agent(source, pid, label, processes, _run_session(run, existing))
    if proc:
        run["status"] = "running"
        run["pid"] = proc["pid"]
        run["ended_at"] = None
        return
    if not existing:
        return
    status = existing.get("status")
    if status == "unknown":
        if existing.get("ended_at") is not None:
            return
        run["status"] = "done"
        run["ended_at"] = _last_activity(
            store, run["run_id"], existing.get("started_at") or run.get("started_at")
        ) or time.time()
        run["pid"] = None
        return
    if status != "running":
        return
    run["status"] = "done" if label or existing.get("verdict") else "unknown"
    run["ended_at"] = _last_activity(store, run["run_id"], existing.get("started_at") or run.get("started_at"))
    run["pid"] = None


def reconcile_running(store, now: float, processes=None) -> int:
    """End grok, muse, and glm rows still marked running after the process is gone.

    A muse row still marked unknown is adopted when a live process matches
    its prompt-file label or session uuid, and closed (``done`` with
    ``ended_at``) when nothing matches. An unlabeled row that shares a
    session with a labeled run is linked to that run (``parent_session_id``
    = the labeled run id) so it is not a second agent; a live process is
    recorded on the labeled run. Orphan muse usage is moved onto the labeled
    run and the orphan row is deleted. Returns how many rows were ended
    because the process was gone.
    """
    if processes is None:
        processes = live_agent_processes(now)
    repair = getattr(store, "repair_muse_orphans", None)
    if callable(repair):
        repair()
    ended = 0
    rows = list(store.running_tool_runs())
    extra = getattr(store, "unknown_tool_runs", None)
    if callable(extra):
        try:
            rows.extend(extra())
        except Exception:
            pass
    for row in rows:
        source = row.get("source")
        if source not in SOURCE_COMMS:
            continue
        proc = match_agent(source, row.get("pid"), row.get("label"), processes, _run_session(row, None))
        labeled = _shadow_parent(store, row)
        if proc and labeled:
            _link_shadow(store, row, labeled, now)
            _mark_running(store, labeled, proc["pid"])
            continue
        if proc:
            if row.get("pid") != proc["pid"] or row.get("status") != "running":
                _mark_running(store, row["run_id"], proc["pid"])
            continue
        if row.get("status") != "running":
            if row.get("status") == "unknown" and row.get("ended_at") is None and not labeled:
                _close_open_unknown(store, row, now)
                ended += 1
                continue
            if labeled and row.get("parent_session_id") != labeled:
                _link_shadow(store, row, labeled, now)
            continue
        patch = {
            "run_id": row["run_id"],
            "status": "done" if row.get("label") or row.get("verdict") else "unknown",
            "ended_at": _last_activity(store, row["run_id"], row.get("started_at")) or float(now),
            "pid": None,
        }
        if labeled:
            patch["parent_session_id"] = labeled
        store.upsert_run(patch)
        ended += 1
    return ended


def _close_open_unknown(store, row: dict, now: float) -> None:
    ended_at = _last_activity(store, row["run_id"], row.get("started_at"))
    if ended_at is None:
        ended_at = float(now)
    store.upsert_run(
        {
            "run_id": row["run_id"],
            "status": "done",
            "ended_at": ended_at,
            "pid": None,
        }
    )


def _mark_running(store, run_id: str, pid: int) -> None:
    store.upsert_run(
        {
            "run_id": run_id,
            "pid": pid,
            "status": "running",
            "ended_at": None,
        }
    )


def _link_shadow(store, row: dict, labeled: str, now: float) -> None:
    """Point an unlabeled row at the labeled run and take it off the live list."""
    ended = _last_activity(store, row["run_id"], row.get("started_at"))
    if ended is None:
        ended = float(now)
    store.upsert_run(
        {
            "run_id": row["run_id"],
            "status": "unknown",
            "ended_at": ended,
            "parent_session_id": labeled,
            "pid": None,
        }
    )


def _canon_comm(comm) -> str:
    """Map versioned ``muse-bin-<ver>`` names to ``muse-bin``."""
    if isinstance(comm, str) and (comm == "muse-bin" or comm.startswith("muse-bin-")):
        return "muse-bin"
    return comm


def _run_session(run: dict, existing) -> str | None:
    """Session uuid for matching: the run's, the stored row's, or the uuid
    in a ``muse:<uuid>:<label>`` run id."""
    sid = (run or {}).get("session_id") or (existing or {}).get("session_id")
    if isinstance(sid, str) and _UUID_OK.match(sid):
        return sid
    return _uuid_from_run_id((run or {}).get("run_id"))


def _uuid_from_run_id(run_id) -> str | None:
    if not isinstance(run_id, str):
        return None
    parts = run_id.split(":", 2)
    if len(parts) == 3 and parts[0] == "muse" and _UUID_OK.match(parts[1]):
        return parts[1]
    return None


def _labels_by_pid() -> dict:
    """Pid -> label token. The rest of the command line is discarded."""
    return {pid: token["label"] for pid, token in _tokens_by_pid().items() if token.get("label")}


def _tokens_by_pid() -> dict:
    """Pid -> label, prompt-file label, and session uuid.

    The command line is read only to extract those tokens. The prompt path
    and the rest of argv are discarded and never returned.
    """
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
        session_id = session_id_from_args(line)
        prompt_label = prompt_label_from_args(line)
        if not label and not session_id and not prompt_label:
            continue
        pid = _pid(line.strip().split(None, 1)[0] if line.strip() else None)
        if pid is not None:
            found[pid] = {
                "label": label,
                "session_id": session_id,
                "prompt_label": prompt_label,
            }
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
    """Labeled run id for an unlabeled grok or muse row with the same session."""
    source = row.get("source")
    if source not in ("grok", "muse") or row.get("label") or not row.get("session_id"):
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
        if item.get("source") == source and item.get("label") and item.get("run_id") != row["run_id"]
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


def session_id_from_args(text: str) -> str | None:
    """The muse ``--session-id <uuid>`` (or ``--session-id=<uuid>``) token.

    Returns only the uuid; the rest of the command line is discarded.
    """
    match = _SESSION.search(text or "")
    if not match:
        return None
    return match.group(1)


def prompt_label_from_args(text: str) -> str | None:
    """Label from ``--prompt-file`` when the path ends in ``/<label>.md``.

    Returns only the label. The path is discarded.
    """
    match = _PROMPT.search(text or "")
    if not match:
        return None
    raw = match.group(1).strip("'\"")
    slash = raw.rfind("/")
    if slash < 0 or not raw.endswith(".md"):
        return None
    label = raw[slash + 1 : -3]
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
    text = proc.stdout
    return label_from_args(text) or prompt_label_from_args(text)


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
        comm = _canon_comm(comm)
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
