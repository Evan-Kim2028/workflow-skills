"""Process samples by exact comm. Argv is not stored.

Liveness for grok, muse, and glm reads pid, start time, and the label
token only. A run is running when that pid is still that tool's process,
or when a live process of that tool carries ``[label: <label>]``. A muse
run is also live when a live muse process has ``--prompt-file`` ending in
``/<label>.md``, or ``--session-id`` equal to the run's session uuid.
A pi, Command Code, or Codex run is also live when its launcher is: a
``python3`` process whose argv runs ``scripts/launch_pi.py`` (source
``pi``), ``scripts/launch_cmd.py`` (source ``commandcode``), or
``scripts/launch_codex.py`` (source ``codex``) with the
run's label as the first positional argument. The pi CLI rewrites its
process title, so the worker itself carries no label and no cli path;
the launcher pid is the same pid the ``running/<label>`` file and the
run row already record.
The command line is read only to extract those tokens; argv is not stored.
The one exception is the worker's ``--model`` flag value, which is recorded
as the sample and live-row ``model`` so a source can fill a run whose model
is otherwise unknown. Nothing else from argv is kept.
Versioned ``muse-bin-<ver>`` comms count as ``muse-bin``.
"""

from __future__ import annotations

import re
import subprocess
import time

from dash.liveness import (
    PROCESS_GONE_GRACE_S,
    dead_run_patch,
    proc_started_ok,
    row_shows_quota,
)

NAME = "process"
INTERVAL_S = 5
COMMS = ("cursor-agent", "claude", "grok", "muse", "muse-bin", "node")
_COMM = set(COMMS)
# Tool source -> OS comm. ``grok`` the binary is the GLM CLI; cursor-agent is
# Grok. pi ships as a node script (``node .../pi-coding-agent/dist/cli.js``):
# a node row only counts as a pi worker when its argv carries the pi cli path
# and a worker label (see ``pi_worker_label_from_args``), so unrelated node
# processes never match. A ``python3`` row only counts when its argv runs
# one of the launchers (see ``launcher_source_from_args``): the pi CLI
# rewrites its process title, so the launcher is the only labelled pi
# process, and Command Code rewrites its title too, so its launcher is
# the only Command Code process at all.
SOURCE_COMMS = {
    "grok": frozenset({"cursor-agent"}),
    "muse": frozenset({"muse", "muse-bin"}),
    "glm": frozenset({"grok"}),
    "pi": frozenset({"node", "pi", "python3"}),
    "commandcode": frozenset({"python3"}),
    "codex": frozenset({"python3"}),
}
_LABEL = re.compile(r"\[label:\s*([^\]]+?)\]")
_RUN_WORKER = re.compile(r"(?:^|\s)\S*run_worker\.py(\s.*)?$")
_LINE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s*$")
_LABEL_OK = re.compile(r"^[\w.+-]{1,80}$")
_UUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
_SESSION = re.compile(r"--session-id(?:=|\s+)(" + _UUID + r")")
_PROMPT = re.compile(r"--prompt-file(?:=|\s+)(\S+)")
_MODEL = re.compile(r"--model(?:=|\s+)(\S+)")
_PI_CLI = re.compile(r"pi-coding-agent")
_LAUNCH_PI = re.compile(r"(?:^|\s)\S*launch_pi\.py(?:\s|$)")
_LAUNCH_CMD = re.compile(r"(?:^|\s)\S*launch_cmd\.py(?:\s|$)")
_LAUNCH_CODEX = re.compile(r"(?:^|\s)\S*launch_codex\.py(?:\s|$)")
_CODEX_EFFORT = re.compile(r"-c\s+model_reasoning_effort=(\S+)")
_PI_PROMPT = re.compile(r"@?([^\s'\"]*/([^\s'\"/]+?)\.md)")
_MODEL_OK = re.compile(r"^[^\s'\";|&<>]{1,200}$")
_REASONING_EFFORT = re.compile(r"--reasoning-effort(?:=|\s+)(\S+)")
_EFFORT_OK = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_UUID_OK = re.compile(r"^" + _UUID + r"$")


def poll(store, cfg, now: float) -> None:
    del cfg
    table = _ps()
    tokens = _tokens_by_pid()
    matched = [
        row
        for row in table
        if row["comm"] in _COMM
        or (
            _canon_comm(row["comm"]) == "python3"
            and tokens.get(row["pid"], {}).get("launcher") in ("pi", "commandcode", "codex")
        )
    ]
    matched_pids = {row["pid"] for row in matched}
    parents = [row for row in matched if row["ppid"] not in matched_pids]
    kids, rss = _children(table)
    for parent in parents:
        label = label_from_pid(parent["pid"]) or tokens.get(parent["pid"], {}).get("label")
        store.add_process_sample(
            {
                "ts": now,
                "pid": parent["pid"],
                "comm": parent["comm"],
                "rss_kb": _tree_rss(parent["pid"], kids, rss),
                "elapsed_s": parent["elapsed_s"],
                "label": label,
                "run_id": store.match_run(parent["pid"], label),
                "model": tokens.get(parent["pid"], {}).get("model"),
            }
        )


def live_agent_processes(now: float | None = None) -> list[dict]:
    """Live grok, muse, glm, pi, Command Code, and Codex processes.

    Each row is ``pid``, ``comm``, ``started_at``, ``label``, ``model``,
    ``launcher`` (``pi``/``commandcode`` for a launcher row, else None),
    and (for muse processes) ``prompt_label`` and ``session_id``. The
    command line is read only to extract the bracket label, the
    ``--prompt-file`` label, ``--session-id``, the pi prompt-path label,
    the launcher label, and the ``--model`` flag value. The path and the
    rest of argv are not returned. A ``node`` row is emitted only when
    its argv names the pi cli and a worker label: plain node processes
    never appear. A ``python3`` row is emitted only when its argv runs
    ``scripts/launch_pi.py``, ``scripts/launch_cmd.py``, or
    ``scripts/launch_codex.py`` with a worker
    label: plain python3 processes never appear.
    """
    now = time.time() if now is None else float(now)
    tokens = _tokens_by_pid()
    rows = []
    wanted = set().union(*SOURCE_COMMS.values())
    muse_comms = SOURCE_COMMS["muse"]
    pi_comms = SOURCE_COMMS["pi"]
    for row in _ps():
        if row["comm"] not in wanted:
            continue
        token = tokens.get(row["pid"], {})
        launcher = token.get("launcher")
        if row["comm"] == "python3":
            if launcher not in ("pi", "commandcode", "codex"):
                # A ``python3`` row that is not a labelled launcher is not ours.
                continue
        else:
            launcher = None
            pi = row["comm"] in pi_comms and token.get("pi")
            if row["comm"] in pi_comms and not pi:
                # A ``node`` row that is not a labelled pi worker is not ours.
                continue
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
                "model": token.get("model"),
                "reasoning_effort": token.get("reasoning_effort"),
                "launcher": launcher,
                "prompt_label": prompt_label,
                "session_id": token.get("session_id") if muse else None,
            }
        )
    return rows


def live_worker_processes(now: float | None = None) -> list[dict]:
    """Live worker agents, however they were launched.

    Starts from ``live_agent_processes`` (grok, muse, glm by comm and
    ``[label:]``/prompt-file/session tokens), then adds any other live
    process whose command line carries a worker label: a ``run_worker.py``
    launch (labeled by its argv label), or any ``[label: ...]`` token in
    argv (a synthetic prober, a wrapper script, a bare launcher). Each
    row is ``pid``, ``comm``, ``started_at``, and ``label``; the rest of
    argv is discarded and never returned. A failed listing keeps whatever
    the agent listing found. Sources keep using ``live_agent_processes``
    for liveness; this wider listing is for the ``unseen worker``
    reconcile check only.
    """
    import time as _time

    now = _time.time() if now is None else float(now)
    try:
        rows = live_agent_processes(now)
    except Exception:
        rows = []
    seen = {row.get("pid") for row in rows}
    try:
        table = {row["pid"]: row for row in _ps()}
    except Exception:
        return rows
    try:
        tokens = _tokens_by_pid()
    except Exception:
        tokens = {}
    try:
        extra = _run_worker_labels()
    except Exception:
        extra = {}
    for pid, token in tokens.items():
        if pid in seen:
            continue
        label = token.get("label") or token.get("prompt_label")
        if not label:
            continue
        info = table.get(pid)
        if info is None:
            continue
        seen.add(pid)
        rows.append(
            {
                "pid": pid,
                "comm": info["comm"],
                "started_at": now - float(info["elapsed_s"]),
                "label": label,
                "prompt_label": None,
                "session_id": None,
            }
        )
    for pid, label in extra.items():
        if pid in seen:
            continue
        info = table.get(pid)
        if info is None:
            continue
        seen.add(pid)
        rows.append(
            {
                "pid": pid,
                "comm": info["comm"],
                "started_at": now - float(info["elapsed_s"]),
                "label": label,
                "prompt_label": None,
                "session_id": None,
            }
        )
    return rows


def run_worker_label_from_args(text: str) -> str | None:
    """Label from a ``run_worker.py <tier> <label> ...`` command line.

    Returns only the label. The workspace, prompt file, run folder, and
    the rest of argv are discarded.
    """
    match = _RUN_WORKER.search(text or "")
    if not match:
        return None
    rest = (match.group(1) or "").split()
    if len(rest) < 2:
        return None
    label = rest[1].strip("'\"")
    if not _LABEL_OK.fullmatch(label):
        return None
    return label


def _comm_ok(source: str, proc: dict) -> bool:
    """Whether a live row's comm can vouch for a run of ``source``.

    A ``python3`` row is a launcher, never a bare interpreter: it only
    vouches when its argv ran that source's launcher (see
    ``launcher_source_from_args``), so a pi launcher never keeps a
    Command Code run alive and vice versa.
    """
    comm = _canon_comm(proc.get("comm"))
    comms = SOURCE_COMMS.get(source)
    if not comms or comm not in comms:
        return False
    if comm == "python3":
        return proc.get("launcher") == source
    return True


def match_agent(source: str, pid, label, processes: list[dict], session_id=None, started_at=None):
    """The live process for this run.

    Match by pid first, then by a muse ``--session-id`` uuid, then by
    ``[label: ...]`` or by a muse ``--prompt-file`` whose path ends in
    ``/<label>.md``. A launcher row (``python3``) also matches by its
    pid — the launcher pid is the pid the run row records — or by its
    label, but only for the source whose launcher it runs. The label leg
    is weak: callers give a newer row with the same label the process
    (see ``_vetoed``). Every leg is guarded by ``started_at`` when known:
    a process that started well after the run did is a reused pid (or a
    relaunched same-label twin), not the run's agent, so it never vouches
    the old row alive.
    """
    if not SOURCE_COMMS.get(source):
        return None
    number = _pid(pid)
    if number is not None:
        for row in processes:
            if row["pid"] == number and _comm_ok(source, row):
                return _checked(row, started_at)
    sid = session_id if isinstance(session_id, str) and _UUID_OK.match(session_id) else None
    if sid and source == "muse":
        want = sid.lower()
        for row in processes:
            got = row.get("session_id")
            if (
                _comm_ok(source, row)
                and isinstance(got, str)
                and got.lower() == want
            ):
                checked = _checked(row, started_at)
                if checked is not None:
                    return checked
    text = label if isinstance(label, str) and label else None
    if text:
        for row in processes:
            if not _comm_ok(source, row):
                continue
            if row.get("label") == text:
                checked = _checked(row, started_at)
                if checked is not None:
                    return checked
            elif source == "muse" and row.get("prompt_label") == text:
                checked = _checked(row, started_at)
                if checked is not None:
                    return checked
    return None


def _checked(proc: dict, started_at) -> dict | None:
    """A matched process that cannot be the run's agent matches nothing.

    Without both clocks there is nothing to compare and the match
    stands, exactly as before.
    """
    if started_at is None:
        return proc
    try:
        proc_started = proc.get("started_at")
    except AttributeError:
        return proc
    if proc_started is None:
        return proc
    if proc_started_ok(proc_started, started_at):
        return proc
    return None


def _match_leg(source: str, run_pid, run_session, proc: dict) -> str:
    """How ``proc`` matched: ``pid``, ``session``, or ``label``.

    Only a session id names one run exactly: a relaunch reuses the prompt
    file and label under a new session id, so the label leg loses to a
    newer row with the same label (see ``_vetoed``).
    """
    try:
        if run_pid is not None and proc.get("pid") is not None and int(run_pid) == int(proc["pid"]):
            return "pid"
    except (TypeError, ValueError):
        pass
    if source == "muse":
        want = run_session if isinstance(run_session, str) and _UUID_OK.match(run_session) else None
        got = proc.get("session_id")
        if want and isinstance(got, str) and got.lower() == want.lower():
            return "session"
    return "label"


def _vetoed(store, source: str, run_id, label, started_at, run_session, proc, leg: str) -> bool:
    """Whether a newer row with the same label owns ``proc`` instead.

    A session match is exact and never vetoed. A label (or prompt-file)
    match is used only when no newer open row with that label exists. A
    pid match is vetoed only when the newer twin owns that same process:
    it matches by session id, or it already carries that pid (a restamp
    from before this rule existed must not keep the dead row alive).
    Unknown stores (no ``newer_open_label_row``) keep the old behavior.
    """
    if leg == "session" or not label:
        return False
    finder = getattr(store, "newer_open_label_row", None)
    if not callable(finder):
        return False
    try:
        twin = finder(label, source, started_at, run_id)
    except Exception:
        return False
    if not twin:
        return False
    if leg == "label":
        return True
    twin_session = twin.get("session_id")
    proc_session = proc.get("session_id")
    if (
        isinstance(twin_session, str)
        and isinstance(proc_session, str)
        and _UUID_OK.match(twin_session)
        and twin_session.lower() == proc_session.lower()
    ):
        return True
    try:
        if twin.get("pid") is not None and int(twin["pid"]) == int(proc["pid"]):
            return True
    except (TypeError, ValueError):
        pass
    return False


def process_alive(store, run_id, pid, label) -> bool:
    """True when fresh process evidence vouches for the run.

    Liveness wins over file evidence: a sample for the run id — or for
    its stored pid under the same label, which is what a live pid
    observably means (a reused pid from another worker cannot vouch) —
    within ``PROCESS_GONE_GRACE_S`` means the worker is still there even
    when no live listing row matches it right now. Bare-label samples are
    not consulted: they may belong to a newer same-label run. Unknown
    stores (no ``last_sample_ts``) keep the old behavior.
    """
    sample_fn = getattr(store, "last_sample_ts", None)
    if not callable(sample_fn):
        return False
    try:
        found = sample_fn(run_id, pid, label)
    except Exception:
        return False
    if found is None:
        return False
    try:
        return time.time() - float(found) <= PROCESS_GONE_GRACE_S
    except (TypeError, ValueError):
        return False


def apply_liveness(store, run: dict, source: str, processes) -> None:
    """Set ``running`` while the process is alive.

    When the process is gone and the stored row was ``running``, mark it
    ``died_early`` (a labeled or verdicted run: the session ended without
    a verdict) or ``unknown``, with ``ended_at`` at the last activity. An
    open ``unknown`` row is revived when a process matches, and closed
    (``done``, with ``ended_at``) when it does not. A failed process
    listing (``processes is None``) leaves the row alone. A run the
    reporter already ended this poll is never downgraded. Liveness wins
    over file evidence: a row with a process sample for its id or stored
    pid within ``PROCESS_GONE_GRACE_S`` is kept ``running`` instead of
    being closed by a quiet file. Other terminal statuses are not
    rewritten. An ``unknown`` row that already has ``ended_at`` is left
    alone.
    """
    if processes is None or not run.get("run_id"):
        return
    existing = _existing_run(store, run["run_id"])
    pid = run.get("pid")
    if pid is None and existing:
        pid = existing.get("pid")
    label = run.get("label") or (existing or {}).get("label")
    session = _run_session(run, existing)
    started = run.get("started_at")
    if started is None and existing:
        started = existing.get("started_at")
    proc = match_agent(source, pid, label, processes, session, started)
    if proc is not None:
        leg = _match_leg(source, pid, session, proc)
        if _vetoed(store, source, run["run_id"], label, started, session, proc, leg):
            proc = None
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
        if process_alive(store, run["run_id"], pid, label):
            run["status"] = "running"
            run["ended_at"] = None
            return
        run["status"] = "done"
        run["ended_at"] = _last_activity(
            store, run["run_id"], existing.get("started_at") or run.get("started_at")
        ) or time.time()
        run["pid"] = None
        return
    if status != "running":
        return
    if process_alive(store, run["run_id"], pid, label):
        run["status"] = "running"
        run["ended_at"] = None
        return
    if run.get("status") in ("done", "killed", "died_early", "finished"):
        # The reporter already ended the run this poll (a finish line beat
        # the process listing): the close-out must not downgrade it.
        return
    # The process vanished without a finish line. A labeled or verdicted
    # run reads died_early, never done: it ended without a verdict, so the
    # dashboard colours it died (red), not finished (green) or blocked
    # (orange). An anonymous row with nothing to say reads unknown.
    run["status"] = "died_early" if label or existing.get("verdict") else "unknown"
    run["ended_at"] = _last_activity(store, run["run_id"], existing.get("started_at") or run.get("started_at"))
    run["pid"] = None


def reconcile_running(store, now: float, processes=None) -> int:
    """End grok, muse, glm, pi, and Command Code rows still marked running after the process is gone.

    A muse row still marked unknown is adopted when a live process matches
    its prompt-file label or session uuid, and closed (``done`` with
    ``ended_at``) when nothing matches. Liveness wins over file evidence:
    a row with a process sample for its id or stored pid within
    ``PROCESS_GONE_GRACE_S`` stays (or becomes) ``running`` instead of
    closing. An unlabeled row that shares a session with a labeled run is
    linked to that run (``parent_session_id`` = the labeled run id) so it
    is not a second agent; a live process is recorded on the labeled run.
    Orphan muse usage is moved onto the labeled run and the orphan row is
    deleted. A missing muse start is filled from the earliest usage event,
    and a finished end that sits before the latest usage event is raised to
    it. Pi, Command Code, and Codex rows match their launcher (a
    ``python3`` ``launch_pi.py``/``launch_cmd.py``/``launch_codex.py``
    process with the run's label), so a
    live launcher keeps them ``running`` here too; their own polls still
    close vanished attempts from the running file alone. Returns how many
    rows were ended because the process was gone.
    """
    if processes is None:
        processes = live_agent_processes(now)
    repair = getattr(store, "repair_muse_orphans", None)
    if callable(repair):
        repair()
    clocks = getattr(store, "repair_muse_clocks", None)
    if callable(clocks):
        clocks()
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
        session = _run_session(row, None)
        proc = match_agent(source, row.get("pid"), row.get("label"), processes, session, row.get("started_at"))
        if proc is not None:
            leg = _match_leg(source, row.get("pid"), session, proc)
            if _vetoed(
                store, source, row["run_id"], row.get("label"),
                row.get("started_at"), session, proc, leg,
            ):
                proc = None
        labeled = _shadow_parent(store, row)
        if proc and labeled:
            _link_shadow(store, row, labeled, now)
            _mark_running(store, labeled, proc["pid"])
            continue
        if proc:
            if row.get("pid") != proc["pid"] or row.get("status") != "running":
                _mark_running(store, row["run_id"], proc["pid"])
            continue
        if not labeled and process_alive(
            store, row["run_id"], row.get("pid"), row.get("label")
        ):
            if row.get("status") != "running" or row.get("ended_at") is not None:
                _mark_running(store, row["run_id"], row.get("pid"))
            continue
        if row.get("status") != "running":
            if row.get("status") == "unknown" and row.get("ended_at") is None and not labeled:
                _close_open_unknown(store, row, now)
                ended += 1
                continue
            if labeled and row.get("parent_session_id") != labeled:
                _link_shadow(store, row, labeled, now)
            continue
        # Vanished without a finish line: died_early reads died (red),
        # never done (green). Anonymous rows stay unknown. The record is
        # the reaper's (dead_run_patch), so a rate-limit log still closes
        # QUOTA even when the process listing reports the end first.
        patch = dead_run_patch(
            row,
            _last_activity(store, row["run_id"], row.get("started_at")) or float(now),
            status="died_early" if row.get("label") or row.get("verdict") else "unknown",
            quota=row_shows_quota(row),
        )
        if labeled:
            patch["parent_session_id"] = labeled
        store.upsert_run(patch)
        ended += 1
    ended += _close_stale_claude_shell(store, now)
    if callable(clocks):
        clocks()
    return ended


def _close_stale_claude_shell(store, now: float) -> int:
    """Close lead-session, shell, and generic rows the sources no longer see.

    Transcripts that aged out of the live window and deleted scratch dirs
    never get re-emitted, so a dead pid or an idle bound would linger
    forever; a generic run whose writer never appends a terminal line is
    likewise never re-polled. The store's stale check (dead pid via the
    shared helper, or no source evidence over the bound) names them; this
    closes them in the DB with ``ended_at`` at the last DB evidence.
    Dagster rows are left to the Dagster poll's by-id close-out, and
    worker-harness rows (muse, glm, grok, pi, commandcode) to their own
    polls and the reconcile pass above.
    """
    stale_fn = getattr(store, "stale_running", None)
    if not callable(stale_fn):
        return 0
    try:
        stale = stale_fn(now)
    except Exception:
        return 0
    ended = 0
    for run, _reason in stale:
        if (run.get("source") or "") not in (
            "claude-main",
            "claude-subagent",
            "claude-workflow",
            "generic",
            "shell",
        ):
            continue
        if run.get("status") != "running":
            continue
        anon = not run.get("label") and not run.get("verdict")
        # Idle closes ended without a verdict: died_early reads died
        # (red). The IDLE verdict names the reason in tooltips. Same close
        # record as the reaper and the reconcile close (dead_run_patch).
        patch = dead_run_patch(
            run,
            _last_activity(store, run["run_id"], run.get("started_at")) or float(now),
            status="unknown" if anon else "died_early",
            verdict="IDLE",
        )
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
    if isinstance(comm, str) and comm.lower() in ("python", "python3"):
        return "python3"
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


def _run_worker_labels() -> dict:
    """Pid -> argv label for live ``run_worker.py`` launches.

    Only the label is kept. Workspaces, prompt files, run folders, and
    the rest of argv are discarded and never returned.
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
        label = run_worker_label_from_args(line)
        if not label:
            continue
        pid = _pid(line.strip().split(None, 1)[0] if line.strip() else None)
        if pid is not None:
            found[pid] = label
    return found


def _tokens_by_pid() -> dict:
    """Pid -> label, prompt-file label, session uuid, launcher, and ``--model`` value.

    The command line is read only to extract those tokens. The prompt path
    and the rest of argv are discarded and never returned. A pi worker's
    label comes from the ``<label>.md`` prompt path it was handed; a
    launcher's label is its first positional argument, with ``launcher``
    naming the source whose script it runs.
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
        model = model_from_args(line)
        effort = reasoning_effort_from_args(line)
        pi_label = pi_worker_label_from_args(line)
        if not label and pi_label:
            # A pi launch carries the label only in its prompt path; use it
            # for the other consumers that read the single "label" slot.
            label = pi_label
        launcher = launcher_source_from_args(line)
        launcher_label = launcher_label_from_args(line) if launcher else None
        if launcher and not launcher_label:
            # A launcher-shaped argv without a valid label is not ours.
            launcher = None
        if not label and launcher_label:
            label = launcher_label
        if not label and not session_id and not prompt_label and not model and not effort and not pi_label and not launcher:
            continue
        pid = _pid(line.strip().split(None, 1)[0] if line.strip() else None)
        if pid is not None:
            found[pid] = {
                "label": label,
                "session_id": session_id,
                "prompt_label": prompt_label,
                "model": model,
                "reasoning_effort": effort,
                "pi": pi_label is not None,
                "launcher": launcher,
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


def launcher_source_from_args(text: str) -> str | None:
    """Which launcher's argv this is: ``pi``, ``commandcode``, ``codex``, or None.

    ``scripts/launch_pi.py``, ``scripts/launch_cmd.py``, and
    ``scripts/launch_codex.py`` run one labelled worker as
    ``<script> <label> <workspace> <prompt> <outdir>``. Returns only the
    source; the label and the rest of argv are read by
    ``launcher_label_from_args`` and discarded.
    """
    if not text:
        return None
    if _LAUNCH_PI.search(text):
        return "pi"
    if _LAUNCH_CMD.search(text):
        return "commandcode"
    if _LAUNCH_CODEX.search(text):
        return "codex"
    return None


def launcher_label_from_args(text: str) -> str | None:
    """Label of a live launcher launch, or None.

    The label is the first positional argument after the launcher
    script. Returns only the label; the workspace, prompt file, outdir,
    and the rest of argv are discarded.
    """
    if not text:
        return None
    match = _LAUNCH_PI.search(text) or _LAUNCH_CMD.search(text) or _LAUNCH_CODEX.search(text)
    if not match:
        return None
    rest = text[match.end() :].split()
    if not rest:
        return None
    label = rest[0].strip("'\"")
    if not _LABEL_OK.fullmatch(label):
        return None
    return label


def pi_worker_label_from_args(text: str) -> str | None:
    """Label of a live pi worker launch, or None.

    A pi process runs as ``node .../pi-coding-agent/dist/cli.js``; the
    label is carried in the prompt path (``@/dir/<label>.md``) because the
    prompt body sits in a file, not on the command line. Both the pi cli
    path and the prompt path must be present; only the label basename is
    returned, never the path. Non-pi node processes return None.
    """
    if not text or not _PI_CLI.search(text):
        return None
    for match in _PI_PROMPT.finditer(text):
        candidate = match.group(2)
        if _LABEL_OK.fullmatch(candidate):
            return candidate
    return None


def model_from_args(text: str) -> str | None:
    """The worker's ``--model <id>`` (or ``--model=<id>``) flag value.

    Returns only that one value; every other argument is discarded. A
    missing flag, an empty value, or a value with shell metacharacters or
    whitespace is None.
    """
    match = _MODEL.search(text or "")
    if not match:
        return None
    value = match.group(1).strip("'\"")
    if not _MODEL_OK.match(value):
        return None
    return value


def reasoning_effort_from_args(text: str) -> str | None:
    """The muse ``--reasoning-effort <level>`` (or ``--reasoning-effort=<level>``) token.

    Codex passes its effort as ``-c model_reasoning_effort=<level>``,
    which is accepted too. Returns only that one value; every other
    argument is discarded. This feeds the model-policy doctor check,
    which reports the worker label and never the rest of argv.
    """
    match = _REASONING_EFFORT.search(text or "")
    if match is None:
        match = _CODEX_EFFORT.search(text or "")
    if not match:
        return None
    value = match.group(1).strip("'\"")
    if not _EFFORT_OK.match(value):
        return None
    return value


def ppid_map() -> dict:
    """Pid -> parent pid for every live process. Empty when ps fails.

    The orphan-worker doctor check joins this against the labelled worker
    listing; a worker reparented to pid 1 with no live run row is reported,
    never signaled.
    """
    try:
        return {row["pid"]: row["ppid"] for row in _ps()}
    except Exception:
        return {}


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
