"""Claude Code transcript source.

Tails main session jsonl, subagent jsonl, and workflow agent jsonl under
``~/.claude/projects``, plus the live registry in ``~/.claude/sessions``.
Journals supply workflow labels. Byte offsets and the per-file / per-tick
budgets bound each poll. The live loop opens transcripts touched in the last
36 hours; ``backfill_session`` (or ``mode=backfill`` plus ``session_id``)
opens one older session, still inside the same budgets.

Assistant lines are the only records parsed for usage. ``api-request-blob``
lines are skipped before ``json.loads``. Usage is deduped by message id,
last record wins, so a later content block replaces an earlier one when
output grows. Prompt bodies, tool inputs, and registry key files are not
stored.

``cfg`` keys (each optional):

- ``roots.claude_projects``, ``roots.claude_sessions``
- ``read_budgets.per_file`` (default 4 MiB), ``read_budgets.per_tick`` (8 MiB)
- ``live_window_s`` (36 h), ``dir_scan_s`` (15), ``running_mtime_s`` (15 min)
- ``main_running_mtime_s`` (3 min): a main session counts as writing
  only inside this window; past it a main is running only with a live
  child or an unanswered tool call (see ``decide_main_status``)
- ``backfill_session``, or ``mode`` ``backfill`` with ``session_id``

A main session that went quiet reads ``unknown`` while its transcript
is recent (under an hour) and ``done`` after: the ``unknown`` status
already means "not live, end unconfirmed" on every surface (no
running badge, never a live agent, never a stale mismatch), so no
page change is needed. Lead per-turn workstream numbers and launched
lane labels are derived here from label-shaped text only (never
stored); see :mod:`dash.lead_split`. Subagent spawn links (Agent
tool_use id to parent turn, sidecar ``toolUseId``, parent tool result
``agentId``) and the subagents' own signal votes are derived the same
way: ids and workstream numbers only, never prompt text or tool
arguments.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
from collections import namedtuple
from datetime import datetime, timezone

from dash.attribution import ws_from_bundle_path
from dash.liveness import pid_alive as _pid_alive

NAME = "claude"
INTERVAL_S = 2

FILE_BUDGET = 4 * 1024 * 1024
TICK_BUDGET = 8 * 1024 * 1024
LIVE_WINDOW_S = 36 * 3600
DIR_SCAN_S = 15
DIR_LIST_CAP = 500
SETTLED_CAP = 100000
RUNNING_MTIME_S = 15 * 60
#: A main transcript counts as still being written inside this window.
MAIN_RUNNING_MTIME_S = 3 * 60
#: Past the writing window a quiet main reads ``unknown`` (idle) while
#: its transcript is this recent, ``done`` after.
MAIN_IDLE_RECENT_S = 60 * 60
#: Tail bytes scanned for an unanswered tool call (bounded, read-only).
TAIL_SCAN_N = 8192
MAX_LABEL = 200
META_MAX = 64 * 1024
JOURNAL_MAX = 256 * 1024

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_AGENT = re.compile(r"^agent-(.+)\.jsonl$")
_BLOB_PREFIXES = (
    b'{"type":"api-request-blob"',
    b'{"type": "api-request-blob"',
)
_ASSISTANT_NEEDLES = (b'"type":"assistant"', b'"type": "assistant"')
_SOURCE = {
    "main": "claude-main",
    "subagent": "claude-subagent",
    "workflow": "claude-workflow",
}
_CLOCK_RANK = {"mtime": 0, "manifest": 1, "muse_log": 2, "transcript": 3}

Target = namedtuple("Target", "mtime kind path session_id agent_id wf_id")

_log = logging.getLogger("agent_dash.claude")

_scan_cache: dict = {}
_discard: dict = {}
_sessions: dict = {}
_runs: dict = {}
_busy: set = set()
_pids: dict = {}
_label_lock: dict = {}
_finished: set = set()
_first_ts: dict = {}
_last_ts: dict = {}
_reg_seen: dict = {}
_reg_sid: dict = {}
_emit_sig: dict = {}
_last_model: dict = {}
_cwd: dict = {}
_ws_hint: dict = {}
_saw_bof: dict = {}
_meta_cache: dict = {}
_journal_seen: dict = {}
_dir_cache: dict = {}
_settled: dict = {}
_split_cursors: dict = {}
_split_seeded: set = set()
_split_maintained: dict = {}
_split_pending: set = set()
#: Seconds between per-session slice maintenance passes.
SPLIT_MAINTAIN_S = 60.0


def reset_state() -> None:
    """Drop process-local caches. Tests use this between cases."""
    _scan_cache.clear()
    _discard.clear()
    _sessions.clear()
    _runs.clear()
    _busy.clear()
    _pids.clear()
    _label_lock.clear()
    _finished.clear()
    _first_ts.clear()
    _last_ts.clear()
    _reg_seen.clear()
    _reg_sid.clear()
    _emit_sig.clear()
    _last_model.clear()
    _cwd.clear()
    _ws_hint.clear()
    _saw_bof.clear()
    _meta_cache.clear()
    _journal_seen.clear()
    _dir_cache.clear()
    _settled.clear()
    _split_cursors.clear()
    _split_seeded.clear()
    _split_maintained.clear()
    _split_pending.clear()


def tier_of(model: str | None) -> str | None:
    if not model or not isinstance(model, str):
        return None
    text = model.lower()
    for name in ("opus", "sonnet", "haiku", "muse", "glm", "grok"):
        if name in text:
            return name
    return None


def epoch_from_iso(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def parse_assistant_records(data: bytes) -> list[dict]:
    """Parse one chunk of jsonl. Last usage for each message id wins."""
    if not data:
        return []
    parts = data.split(b"\n")
    if data.endswith(b"\n"):
        parts = parts[:-1]
    return parse_assistant_lines(parts)


def parse_assistant_lines(lines) -> list[dict]:
    found = []
    for line in lines:
        if not line or not _is_assistant_line(line):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        row = _usage_row(rec)
        if row:
            found.append(row)
    return _dedupe(found)


def transcript_status(
    *,
    caught_up: bool,
    fresh: bool,
    registry_busy: bool,
    finished: bool,
) -> str:
    if registry_busy:
        return "running"
    if not caught_up:
        return "running"
    if finished:
        return "done"
    if fresh:
        return "running"
    return "done"


def decide_main_status(
    *,
    caught_up: bool,
    mtime_age: float,
    pid_dead: bool,
    has_live_child: bool,
    pending_tool: bool,
    main_running_s: float = MAIN_RUNNING_MTIME_S,
    running_s: float = RUNNING_MTIME_S,
    idle_recent_s: float = MAIN_IDLE_RECENT_S,
) -> str:
    """A main session's status: running only on fresh evidence.

    ``running`` while still catching up on the transcript, while the
    transcript was written inside the short main window, while a child
    (a running subagent, or a running worker run parented to this
    session) is live, or while the last assistant turn left an
    unanswered tool call (a long Bash, a background task) and the
    transcript is younger than the old running bound. A just-written
    transcript wins over a dead registry pid: the registry file goes
    stale across CLI restarts while the transcript is ground truth, so
    a dead pid closes the row only once writing stopped. Otherwise the
    session is idle (``unknown``: not live, end unconfirmed) while
    recent, and ``done`` after. ``unknown`` reuses the existing status
    every surface already treats as not-running, so an idle session
    never shows the running badge and is never counted as a live agent.
    """
    if not caught_up:
        return "running"
    try:
        age = float(mtime_age)
    except (TypeError, ValueError):
        age = float("inf")
    if age <= float(main_running_s):
        return "running"
    if pid_dead:
        return "done"
    if has_live_child:
        return "running"
    if pending_tool and age <= float(running_s):
        return "running"
    if age < float(idle_recent_s):
        return "unknown"
    return "done"


def _tail_pending_tool(path: str) -> bool:
    """True when the last assistant turn left an unanswered tool call.

    Reads only the file tail (bounded): the tool_use ids of the last
    assistant message, minus any tool_result for them on later lines.
    Missing or unreadable files read as False, never an exception.
    Only ids are compared; no text is kept.
    """
    st = _stat(path)
    if st is None or st.st_size <= 0:
        return False
    n = min(st.st_size, TAIL_SCAN_N)
    try:
        with open(path, "rb") as fh:
            if st.st_size > n:
                fh.seek(st.st_size - n)
            data = fh.read(n)
    except OSError:
        return False
    lines = [line for line in data.split(b"\n") if line.strip()]
    last_uses: list | None = None
    last_index = -1
    for index, line in enumerate(lines):
        if not _is_assistant_line(line):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        last_uses = [
            block.get("id")
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "tool_use"
            and isinstance(block.get("id"), str)
        ]
        last_index = index
    if not last_uses:
        return False
    answered = set()
    for line in lines[last_index + 1 :]:
        if b"tool_result" not in line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = rec.get("message") if isinstance(rec, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            target = block.get("tool_use_id")
            if isinstance(target, str):
                answered.add(target)
            nested = block.get("content") if isinstance(block.get("content"), list) else []
            for item in nested:
                if isinstance(item, dict) and isinstance(item.get("tool_use_id"), str):
                    answered.add(item["tool_use_id"])
    return any(use not in answered for use in last_uses)


def subagent_status(final: bool, fresh: bool) -> str:
    """Running only when the last event is not a result and the transcript
    was written inside ``running_mtime_s``.

    That window is ``RUNNING_MTIME_S`` (15 minutes): the source already
    treats a transcript as still being written for that long. A result
    event ends the run even if the file was just touched. A quiet file
    is done, not left running.
    """
    if final or not fresh:
        return "done"
    return "running"


def _tail_is_result(path) -> bool:
    """True when the last transcript line is a result event.

    Only the type field on that line is inspected. The line is not stored.
    """
    st = _stat(path)
    if st is None or st.st_size <= 0:
        return False
    n = min(st.st_size, 8192)
    try:
        with open(path, "rb") as fh:
            if st.st_size > n:
                fh.seek(st.st_size - n)
            data = fh.read(n)
    except OSError:
        return False
    lines = [line for line in data.split(b"\n") if line.strip()]
    if not lines:
        return False
    head = lines[-1][:96]
    return b'"type":"result"' in head or b'"type": "result"' in head


def poll(store, cfg, now: float) -> None:
    try:
        if not isinstance(cfg, dict):
            cfg = {}
        now = float(now)
        per_file, per_tick = _budgets(cfg)
        left = _poll_registry(store, cfg, per_tick, now)
        _poll_transcripts(store, cfg, now, left, per_file)
        _refresh_inheritance(store, now)
        _drain_slices(store, cfg, now)
    except Exception as exc:
        _log.warning("claude poll: %s", type(exc).__name__)


def _drain_slices(store, cfg, now: float) -> None:
    """Refresh slice rows for sessions ingested this poll, last.

    Runs after the inherit refresh so slice maintenance reads the
    settled session attribution, not the mid-poll guess. Throttled
    per session (always in backfill). Never raises.
    """
    if not _split_pending:
        return
    pending = sorted(_split_pending)
    _split_pending.clear()
    try:
        backfill = _backfill(cfg)
    except Exception:
        backfill = None
    for session_id in pending:
        try:
            _maintain_slices(store, session_id, now, backfill)
        except Exception as exc:
            _log.warning("claude slices: %s", type(exc).__name__)


def _refresh_inheritance(store, now: float) -> None:
    """Recompute inherited/inferred workstreams; explicit ones are kept."""
    refresh = getattr(store, "refresh_workstream_inheritance", None)
    if not callable(refresh):
        return
    try:
        refresh(now)
    except Exception as exc:
        _log.warning("claude inherit refresh: %s", type(exc).__name__)


def _transcript_fresh(cfg, session_id: str, now) -> bool:
    """True when the session transcript was written inside the main window.

    Bounds the registry dead-pid close-out: a stale registry pid must
    not end a session that is still writing. One glob plus one stat;
    missing or unreadable files read as not fresh, exactly as before.
    """
    try:
        moment = float(now)
    except (TypeError, ValueError):
        return False
    if not isinstance(session_id, str) or not _UUID.match(session_id):
        return False
    try:
        window = float((cfg or {}).get("main_running_mtime_s", MAIN_RUNNING_MTIME_S))
    except (TypeError, ValueError):
        window = float(MAIN_RUNNING_MTIME_S)
    root = _root(cfg or {}, "claude_projects", "projects", _default_projects())
    try:
        hits = glob.glob(os.path.join(glob.escape(root), "*", glob.escape(session_id) + ".jsonl"))
    except (OSError, TypeError):
        return False
    for hit in hits:
        st = _stat(hit)
        if st is not None and moment - st.st_mtime <= window:
            return True
    return False


def _poll_registry(store, cfg, left, now=None) -> int:
    root = _root(cfg, "claude_sessions", "sessions", _default_sessions())
    busy: set = set()
    pids: dict = {}
    try:
        entries = list(os.scandir(root))
    except OSError:
        entries = []
    for ent in entries:
        if left <= 0:
            break
        name = ent.name
        if not name.endswith(".json"):
            continue
        stem = name[:-5]
        if not stem.isdigit():
            continue
        try:
            if not ent.is_file(follow_symlinks=False):
                continue
            st = ent.stat(follow_symlinks=False)
        except OSError:
            continue
        if st.st_size <= 0 or st.st_size > META_MAX:
            continue
        key = ent.path
        seen = _reg_seen.get(key)
        if seen == (st.st_ino, st.st_mtime_ns, st.st_size):
            continue
        if st.st_size > left:
            continue
        try:
            with open(ent.path, "rb") as fh:
                raw = fh.read(META_MAX)
        except OSError as exc:
            _log.warning("claude registry %s: %s", stem, type(exc).__name__)
            continue
        left -= len(raw)
        _reg_seen[key] = (st.st_ino, st.st_mtime_ns, st.st_size)
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        sid = rec.get("sessionId")
        if not isinstance(sid, str) or not _UUID.match(sid):
            continue
        _reg_sid[key] = sid
        status = rec.get("status")
        if not isinstance(status, str):
            status = None
        pid = rec.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            pids[sid] = pid
            pid_alive = _pid_alive(pid)
        else:
            pid = None
            pid_alive = False
        cwd = rec.get("cwd") if isinstance(rec.get("cwd"), str) else None
        if cwd and len(cwd) > 1024:
            cwd = cwd[:1024]
        label = _clean_label(rec.get("name")) if isinstance(rec.get("name"), str) else None
        started = _epoch_number(rec.get("startedAt"))
        run_id = f"claude-main:{sid}:main"
        if (
            status == "busy"
            and pid is not None
            and not pid_alive
            and not _transcript_fresh(cfg, sid, now)
        ):
            # Dead pid: the registry still says busy but the process is gone.
            # Close the row at the last evidence (transcript usage or the
            # registry file mtime) instead of leaving it running. A
            # still-writing transcript means the registry is stale (the
            # CLI rolled its process), so the transcript logic below
            # decides instead of this close-out.
            ended = _last_ts.get(run_id) or st.st_mtime
            try:
                ended = max(float(ended), float(st.st_mtime))
            except (TypeError, ValueError):
                ended = float(st.st_mtime)
            _sessions.pop(sid, None)
            _merge_session(
                store,
                {
                    "session_id": sid,
                    "tool": "claude",
                    "cwd": cwd,
                    "started_at": started,
                    "status": "done",
                    "parent_session_id": None,
                    "label": label,
                },
            )
            _emit_run(
                store,
                {
                    "run_id": run_id,
                    "source": "claude-main",
                    "session_id": sid,
                    "parent_session_id": None,
                    "status": "done",
                    "label": label,
                    "pid": None,
                    "started_at": started,
                    "ended_at": ended,
                    "clock_source": "mtime",
                },
                force_status="done",
            )
            continue
        if status == "busy":
            busy.add(sid)
        _merge_session(
            store,
            {
                "session_id": sid,
                "tool": "claude",
                "cwd": cwd,
                "started_at": started,
                "status": status,
                "parent_session_id": None,
                "label": label,
            },
        )
        if status == "busy":
            _emit_run(
                store,
                {
                    "run_id": run_id,
                    "source": "claude-main",
                    "session_id": sid,
                    "parent_session_id": None,
                    "status": "running",
                    "label": label,
                    "pid": pid if pid_alive else None,
                    "started_at": started,
                    "clock_source": "mtime",
                },
            )
    # Registry files that did not change still contribute busy/pid. Rescan
    # the cache so a quiet busy session stays busy.
    for key, sid in list(_reg_sid.items()):
        seen = _reg_seen.get(key)
        if not seen:
            continue
        try:
            st = os.stat(key, follow_symlinks=False)
        except OSError:
            _reg_sid.pop(key, None)
            _reg_seen.pop(key, None)
            continue
        if (st.st_ino, st.st_mtime_ns, st.st_size) != seen:
            continue
        cached = _sessions.get(sid) or {}
        if cached.get("status") == "busy":
            busy.add(sid)
        # pid lives on the run once emitted; keep the previous pid map
        # updated from the in-memory sessions only when this pass read it.
    _busy.clear()
    _busy.update(busy)
    _pids.update(pids)
    # Drop pids whose registry file vanished.
    live_sids = set(_reg_sid.values())
    for sid in list(_pids):
        if sid not in live_sids and sid not in pids:
            _pids.pop(sid, None)
    return left


def _poll_transcripts(store, cfg, now, left, per_file) -> None:
    root = _root(cfg, "claude_projects", "projects", _default_projects())
    window = float(cfg.get("live_window_s", LIVE_WINDOW_S))
    scan_s = float(cfg.get("dir_scan_s", DIR_SCAN_S))
    fresh_s = float(cfg.get("running_mtime_s", RUNNING_MTIME_S))
    try:
        main_running_s = float(cfg.get("main_running_mtime_s", MAIN_RUNNING_MTIME_S))
    except (TypeError, ValueError):
        main_running_s = float(MAIN_RUNNING_MTIME_S)
    backfill = _backfill(cfg)
    try:
        list_cap = int(cfg.get("dir_list_cap", DIR_LIST_CAP))
    except (TypeError, ValueError):
        list_cap = DIR_LIST_CAP
    targets = _hot_targets(root, now, window, backfill, scan_s, list_cap)
    for target in targets:
        if left <= 0 and target.kind != "journal":
            # Journals are tiny and carry labels. Still skip once the tick
            # budget is spent; the next tick reads them.
            break
        try:
            if target.kind == "journal":
                left = _ingest_journal(store, target, now, fresh_s, left)
            else:
                left = _ingest_jsonl(
                    store,
                    target,
                    now,
                    fresh_s,
                    left,
                    per_file,
                    backfill,
                    window,
                    main_running_s,
                )
        except Exception as exc:
            _log.warning(
                "claude skip %s: %s", os.path.basename(target.path), type(exc).__name__
            )


def _hot_targets(root, now, window, backfill, scan_s, list_cap=None):
    key = (str(root), backfill or "")
    hit = _scan_cache.get(key)
    if hit and now - hit[0] < scan_s:
        return hit[1]
    targets = _discover(root, now, window, backfill, list_cap=list_cap)
    targets.sort(key=lambda t: (0 if t.kind == "journal" else 1, -t.mtime))
    _scan_cache[key] = (now, targets)
    return targets


def _discover(root, now, window, backfill, list_cap=None):
    """List transcript candidates with cached directory listings.

    Unchanged directories cost one stat and no listing; new files always
    bump the parent mtime, so skipping unchanged subtrees misses nothing.
    ``list_cap`` bounds fresh listings per pass (None counts nothing);
    a deferred directory keeps its old mtime stamp and is retried next pass.
    """
    targets = []
    cap = [DIR_LIST_CAP if list_cap is None else list_cap]
    projects, _listed = _list_dir(root, None)
    for name, proj_path, _is_file, is_dir in projects:
        if not is_dir:
            continue
        children, _listed = _list_dir(proj_path, cap)
        if children is None:
            continue
        for name, ent_path, is_file, is_dir in children:
            if is_file and name.endswith(".jsonl"):
                sid = name[: -len(".jsonl")]
                if not _UUID.match(sid):
                    continue
                if backfill and sid != backfill:
                    continue
                st = _stat(ent_path)
                if st is None:
                    continue
                if backfill or st.st_mtime >= now - window:
                    targets.append(
                        Target(st.st_mtime, "main", ent_path, sid, "main", None)
                    )
            elif is_dir and _UUID.match(name):
                if backfill and name != backfill:
                    continue
                _discover_session(ent_path, name, now, window, backfill, targets, cap)
    return targets


def _list_dir(path, cap):
    from dash.sources import list_dir_cached

    entries, listed = list_dir_cached(path, _dir_cache, cap=cap)
    if entries is None:
        return None, listed
    return entries, listed


def _discover_session(session_dir, sid, now, window, backfill, targets, cap):
    sub = os.path.join(session_dir, "subagents")
    entries, _listed = _list_dir(sub, cap)
    for name, ent_path, is_file, is_dir in entries or []:
        if is_file:
            match = _AGENT.match(name)
            if not match:
                continue
            st = _stat(ent_path)
            if st is None:
                continue
            if backfill or st.st_mtime >= now - window:
                targets.append(
                    Target(st.st_mtime, "subagent", ent_path, sid, match.group(1), None)
                )
        elif is_dir and name == "workflows":
            _discover_workflows(ent_path, sid, now, window, backfill, targets, cap)


def _discover_workflows(wf_root, sid, now, window, backfill, targets, cap):
    workflows, _listed = _list_dir(wf_root, cap)
    if workflows is None:
        return
    for name, wf_path, _is_file, is_dir in workflows:
        if not is_dir or not name.startswith("wf_"):
            continue
        agents, _listed = _list_dir(wf_path, cap)
        if agents is None:
            continue
        for ent_name, ent_path, is_file, _is_dir in agents:
            if not is_file:
                continue
            st = _stat(ent_path)
            if st is None:
                continue
            if not backfill and st.st_mtime < now - window:
                continue
            if ent_name == "journal.jsonl":
                targets.append(
                    Target(st.st_mtime, "journal", ent_path, sid, None, name)
                )
                continue
            match = _AGENT.match(ent_name)
            if match:
                targets.append(
                    Target(
                        st.st_mtime,
                        "workflow",
                        ent_path,
                        sid,
                        match.group(1),
                        name,
                    )
                )


def _ingest_journal(store, target, now, fresh_s, left) -> int:
    st = _stat(target.path)
    if st is None or st.st_size <= 0:
        return left
    stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
    if _journal_seen.get(target.path) == stamp:
        return left
    n = min(st.st_size, JOURNAL_MAX, max(left, 0))
    if n <= 0:
        return left
    try:
        with open(target.path, "rb") as fh:
            raw = fh.read(n)
    except OSError as exc:
        _log.warning("claude journal %s: %s", target.wf_id, type(exc).__name__)
        return left
    left -= len(raw)
    fresh = now - st.st_mtime <= fresh_s
    for line in raw.split(b"\n"):
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        fact = _journal_fact(rec)
        if fact is None:
            continue
        agent_id, label, done = fact
        if done:
            _finished.add((target.session_id, agent_id))
        run_id = _run_id("workflow", target.session_id, agent_id, target.wf_id)
        applied = _apply_label(run_id, label, "journal")
        status = "done" if done else ("running" if fresh else "unknown")
        agent_jsonl = os.path.join(
            os.path.dirname(target.path), f"agent-{agent_id}.jsonl"
        )
        has_jsonl = os.path.isfile(agent_jsonl)
        patch = {
            "run_id": run_id,
            "source": "claude-workflow",
            "session_id": target.session_id,
            "parent_session_id": target.session_id,
            "status": status,
            "clock_source": "mtime",
        }
        if applied:
            patch["label"] = applied
        # The agent transcript owns started/ended when it exists. A journal
        # mtime must not replace that timestamp on a later tick.
        if done and not has_jsonl:
            patch["ended_at"] = st.st_mtime
        _emit_run(store, patch, force_status=status)
    if len(raw) >= st.st_size:
        _journal_seen[target.path] = stamp
    return left


def _store_has_live_child(store, session_id: str) -> bool:
    """A running child of ``session_id``, or False without the store hook."""
    checker = getattr(store, "has_live_child", None)
    if not callable(checker):
        return False
    try:
        return bool(checker(session_id))
    except Exception:
        return False


def _main_liveness(store, target, st, now, fresh, main_running_s):
    """``(main_fresh, child, pending)`` for a main transcript.

    The child (running subagent or parented worker run) and pending
    (unanswered tool call) checks run only inside the ambiguous
    window: past the short main window but inside the old running
    bound. Outside it the mtime alone decides, so quiet and stale
    files cost no extra I/O. ``None`` means "not consulted".
    """
    age = now - st.st_mtime
    main_fresh = age <= main_running_s
    if main_fresh or not fresh:
        return main_fresh, None, None
    child = _store_has_live_child(store, target.session_id)
    if child:
        return main_fresh, True, None
    return main_fresh, False, _tail_pending_tool(target.path)


def _assistant_msg_id(line: bytes):
    """The assistant message id on one line, or None (never raises)."""
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    mid = msg.get("id")
    return str(mid) if isinstance(mid, str) and mid else None


def _note_splits(store, target, lines):
    """Per-turn workstream numbers and launched lane labels for a main.

    Scans the just-read chunk in file order through the split cursor:
    every assistant line advances the cursor (usage or not, so
    text-only turns keep the topic), and lines carrying usage gain
    their assignment. Returns ``({message_id: ws}, [launched labels],
    {message_id: direct vote})``; workstream numbers only, never text.
    The direct vote is the turn's own signal pick (None when the turn
    carries no signal), which the spawn-link pass needs apart from the
    inherited cursor assignment. Non-main targets read as empty. Never
    raises: the poll stays exception-safe.
    """
    try:
        if target.kind != "main" or not lines:
            return {}, [], {}
        from dash import lead_split

        run_id = _run_id(target.kind, target.session_id, target.agent_id, target.wf_id)
        cursor = _split_cursors.get(run_id)
        if cursor is None:
            seed = None
            if run_id not in _split_seeded:
                _split_seeded.add(run_id)
                getter = getattr(store, "last_split_ws", None)
                if callable(getter):
                    try:
                        seed = getter(target.session_id)
                    except Exception:
                        seed = None
            cursor = lead_split.SplitCursor(seed)
            _split_cursors[run_id] = cursor
        splits: dict = {}
        launches: list = []
        directs: dict = {}
        for line in lines:
            order, found = lead_split.signals_in_line(line)
            for label in found:
                if label not in launches:
                    launches.append(label)
            if not _is_assistant_line(line):
                continue
            mid = _assistant_msg_id(line)
            direct = lead_split.pick_turn(order)
            assigned = cursor.observe(order)
            if mid is None:
                continue
            if direct is not None:
                directs[mid] = direct
            if assigned is not None:
                splits[mid] = assigned
        return splits, launches, directs
    except Exception as exc:
        _log.warning("claude splits: %s", type(exc).__name__)
        return {}, [], {}


def _link_lanes(store, session_id: str, launches) -> None:
    """Adopt parentless worker runs for labels this session launched."""
    if not launches:
        return
    linker = getattr(store, "link_lane_run", None)
    if not callable(linker):
        return
    for label in launches:
        try:
            linker(label, session_id)
        except Exception as exc:
            _log.warning("claude lane link: %s", type(exc).__name__)
            return


#: An ``agentId`` inside a parent tool result's metadata text. Only the
#: id is ever kept; the surrounding text is not stored.
_AGENT_ID_RE = re.compile(r"agentId:\s*([0-9A-Za-z_-]{5,80})")


def _agent_uses_in_line(line: bytes) -> list:
    """``[(tool_use_id, message_id)]`` for Agent spawns on one line.

    Only assistant lines with a real Agent tool_use block match; ids
    only, never text. Never raises.
    """
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return []
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return []
    mid = msg.get("id")
    if not isinstance(mid, str) or not mid:
        return []
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    return [
        (block.get("id"), mid)
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("name") == "Agent"
        and isinstance(block.get("id"), str)
        and block.get("id")
    ]


def _agent_links_in_line(line: bytes) -> list:
    """``[(tool_use_id, agent_id)]`` for subagent launch results.

    A parent tool result carries the spawned subagent's id as
    ``agentId: <id>`` inside its metadata text; only the two ids are
    kept. Never raises.
    """
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(rec, dict):
        return []
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if (
            not isinstance(block, dict)
            or block.get("type") != "tool_result"
            or not isinstance(block.get("tool_use_id"), str)
        ):
            continue
        inner = block.get("content")
        if isinstance(inner, str):
            texts = [inner]
        elif isinstance(inner, list):
            texts = [
                item.get("text")
                for item in inner
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
        else:
            continue
        for text in texts:
            match = _AGENT_ID_RE.search(text)
            if match:
                out.append((block["tool_use_id"], match.group(1)))
                break
    return out


def _note_spawns(store, target, lines, splits, directs) -> list:
    """Link Agent spawns to their parent turn; re-resolve linked runs.

    Main targets only. Every Agent tool_use id on the chunk gains its
    spawning turn (message id, the turn's direct signal vote, the
    cursor assignment at that turn); every tool result carrying an
    agentId links the subagent run. Either side may arrive first; the
    store joins whichever comes later, and linked runs re-resolve once
    the link completes (retroactive attribution, no double counting:
    usage rows are keyed by message id). Returns the tool use ids seen.
    Ids and workstream numbers only, never text. Never raises.
    """
    if target.kind != "main" or not lines:
        return []
    uses: dict = {}
    agents: dict = {}
    for line in lines:
        if b"tool_use" in line and b'"Agent"' in line:
            for tool_use_id, mid in _agent_uses_in_line(line):
                uses[tool_use_id] = mid
        if b"tool_result" in line and b"agentId" in line:
            for tool_use_id, agent_id in _agent_links_in_line(line):
                agents[tool_use_id] = agent_id
    if not uses and not agents:
        return []
    note_turn = getattr(store, "note_spawn_turn", None)
    if note_turn is not None:
        for tool_use_id, mid in uses.items():
            try:
                note_turn(
                    tool_use_id,
                    target.session_id,
                    mid,
                    (directs or {}).get(mid),
                    (splits or {}).get(mid),
                )
            except Exception as exc:
                _log.warning("claude spawn turn: %s", type(exc).__name__)
    note_agent = getattr(store, "note_spawn_agent", None)
    if note_agent is not None:
        for tool_use_id, agent_id in agents.items():
            try:
                note_agent(
                    _run_id("subagent", target.session_id, agent_id, None),
                    tool_use_id,
                )
            except Exception as exc:
                _log.warning("claude spawn agent: %s", type(exc).__name__)
    refresher = getattr(store, "refresh_linked_subagents", None)
    if refresher is not None:
        try:
            refresher(list(set(uses) | set(agents)))
        except Exception as exc:
            _log.warning("claude spawn refresh: %s", type(exc).__name__)
    return sorted(set(uses) | set(agents))


def _note_subagent(store, target, run_id, lines, start_offset, end_offset) -> None:
    """Record a subagent chunk's spawn link and own-signal votes.

    The sidecar's ``toolUseId`` links the run to its spawning turn;
    every assistant line's label-shaped signals vote for the run's own
    fallback (the parent-written prompt arrives as a user line and
    never votes). Both writes are incremental and idempotent, so a
    restart replays the same bytes to the same rows. Ids and workstream
    numbers only, never text. Never raises.
    """
    try:
        if target.kind != "subagent" or not lines:
            return
        from dash import lead_split

        _label, _model, tool_use_id = _read_meta(_meta_path(target.path))
        note_agent = getattr(store, "note_spawn_agent", None)
        if note_agent is not None and tool_use_id:
            try:
                note_agent(run_id, tool_use_id)
            except Exception as exc:
                _log.warning("claude subagent link: %s", type(exc).__name__)
        add_votes = getattr(store, "add_subagent_votes", None)
        if add_votes is None:
            return
        ordered: list = []
        for line in lines:
            if not _is_assistant_line(line):
                continue
            order, _launches = lead_split.signals_in_line(line)
            ordered.extend(order)
        try:
            add_votes(run_id, ordered, start_offset, end_offset)
        except Exception as exc:
            _log.warning("claude subagent votes: %s", type(exc).__name__)
    except Exception as exc:
        _log.warning("claude subagent: %s", type(exc).__name__)


def _maintain_slices(store, session_id: str, now: float, backfill) -> None:
    """Refresh one session's slice rows, throttled (always in backfill)."""
    if not isinstance(session_id, str) or not session_id:
        return
    maintainer = getattr(store, "apply_lead_slices", None)
    if not callable(maintainer):
        return
    try:
        now = float(now)
    except (TypeError, ValueError):
        return
    if not backfill:
        last = _split_maintained.get(session_id)
        if last is not None and now - last < SPLIT_MAINTAIN_S:
            return
    _split_maintained[session_id] = now
    try:
        maintainer(session_id)
    except Exception as exc:
        _log.warning("claude slices: %s", type(exc).__name__)


def _ensure_split_rules(store, target) -> None:
    """Rebuild one session's slices/votes when the rules changed.

    Compares the stored per-session signal-rules marker and, on a
    version mismatch, resets that session's split state (offsets,
    per-turn workstreams, slice rows, subagent votes) so this ingest
    replays the same bytes under the current rules. Drops the
    in-memory split cursor so the replay reseeds from the rebuilt
    rows. Non-main/subagent targets read as a no-op. Never raises.
    """
    try:
        if target.kind not in ("main", "subagent"):
            return
        checker = getattr(store, "ensure_split_rules", None)
        if not callable(checker):
            return
        if checker(target.session_id):
            run_id = _run_id("main", target.session_id, "main", None)
            _split_cursors.pop(run_id, None)
            _split_seeded.discard(run_id)
    except Exception as exc:
        _log.warning("claude split rules: %s", type(exc).__name__)


def _ingest_jsonl(store, target, now, fresh_s, left, per_file, backfill, window, main_running_s=None) -> int:
    _ensure_split_rules(store, target)
    st = _stat(target.path)
    if st is None:
        _settled.pop(target.path, None)
        return left
    if not backfill and st.st_mtime < now - window:
        return left
    prev = _offset_of(store, target.path)
    offset = 0
    if prev and prev[0] == st.st_ino and st.st_size >= prev[1]:
        offset = prev[1]
    elif prev and (prev[0] != st.st_ino or st.st_size < prev[1]):
        offset = 0
        _discard.pop(target.path, None)
    fresh = now - st.st_mtime <= fresh_s
    if main_running_s is None:
        main_running_s = MAIN_RUNNING_MTIME_S
    liveness = None
    if target.kind == "main":
        liveness = _main_liveness(store, target, st, now, fresh, main_running_s)
    run_id = _run_id(target.kind, target.session_id, target.agent_id, target.wf_id)
    saw_start = offset == 0 and not _discard.get(target.path)
    if saw_start:
        _saw_bof[run_id] = True

    if target.path in _discard:
        left = _discard_chunk(store, target.path, st, offset, left, per_file)
        off = _offset_of(store, target.path)
        caught = (
            target.path not in _discard
            and off is not None
            and off[1] >= st.st_size
        )
        _emit_transcript_run(store, target, st, fresh, caught_up=caught, liveness=liveness, main_running_s=main_running_s, now=now)
        return left

    if offset >= st.st_size and target.path not in _discard:
        key = _settled_key(target, st, fresh, liveness)
        if _settled.get(target.path) == key:
            return left
        _emit_transcript_run(store, target, st, fresh, caught_up=True, liveness=liveness, main_running_s=main_running_s, now=now)
        _remember_settled(target.path, key)
        return left
    if left <= 0 or per_file <= 0:
        _emit_transcript_run(store, target, st, fresh, caught_up=False, liveness=liveness, main_running_s=main_running_s, now=now)
        return left

    requested = min(per_file, left, st.st_size - offset)
    if requested <= 0:
        return left
    try:
        with open(target.path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(requested)
    except OSError as exc:
        _log.warning("claude read %s: %s", os.path.basename(target.path), type(exc).__name__)
        return left
    left -= len(data)
    at_eof = offset + len(data) >= st.st_size
    stale = not fresh
    lines, consumed = _consume(data, at_eof=at_eof, stale=stale)
    if consumed == 0:
        if at_eof:
            _emit_transcript_run(store, target, st, fresh, caught_up=False, liveness=liveness, main_running_s=main_running_s, now=now)
            return left
        if requested >= per_file:
            _discard[target.path] = True
            if _is_assistant_line(data):
                _log.warning("claude oversized line %s", os.path.basename(target.path))
            _set_offset(store, target.path, st, offset + len(data))
        # A short read is the tick budget ending mid-line. Leave the offset
        # so the next tick can take a full per-file window.
        _emit_transcript_run(store, target, st, fresh, caught_up=False, liveness=liveness, main_running_s=main_running_s, now=now)
        return left

    rows = parse_assistant_lines(lines)
    _note_times(run_id, rows)
    _note_bundle_hint(run_id, lines)
    splits, launches, directs = _note_splits(store, target, lines)
    new_offset = offset + consumed
    if target.kind == "subagent":
        _note_subagent(store, target, run_id, lines, offset, new_offset)
    _emit_usage(store, target, rows, now, splits)
    _link_lanes(store, target.session_id, launches)
    if target.kind == "main":
        _note_spawns(store, target, lines, splits, directs)
    caught_up = new_offset >= st.st_size
    _set_offset(store, target.path, st, new_offset)
    _emit_transcript_run(store, target, st, fresh, caught_up=caught_up, liveness=liveness, main_running_s=main_running_s, now=now)
    if target.kind == "main" and caught_up:
        _split_pending.add(target.session_id)
    return left


def _settled_key(target, st, fresh, liveness=None):
    """Fingerprint covering every input of the caught-up emit.

    A file with the same key would emit the identical status, label, and
    timestamps, so a repeat poll can skip it without opening it. ``fresh``
    flips once as the file ages out of the running window; the registry
    busy bit and journal ``done`` flags can flip without a file write, so
    they are part of the key too. The main-kind pid check reads the
    registry pid map: a dead pid closes the row even when the transcript
    is untouched. ``liveness`` carries the main-kind
    ``(main_fresh, child, pending)`` triple, so a child finishing or a
    tool call answering without a file write still re-emits.
    """
    if target.kind == "main":
        busy = target.session_id in _busy
        pid = _pids.get(target.session_id)
        pid_alive = None if pid is None else bool(_pid_alive(pid))
        extra = (busy, pid_alive, liveness)
    else:
        extra = (target.session_id, target.agent_id) in _finished
    return (st.st_ino, st.st_size, st.st_mtime_ns, bool(fresh), extra)


def _remember_settled(path, key) -> None:
    """Record a caught-up file, bounding the cache.

    Eviction only costs the evicted files one re-emit on a later tick;
    entries for deleted files are dropped when the ingest sees them gone.
    """
    _settled[path] = key
    if len(_settled) > SETTLED_CAP:
        drop = len(_settled) - SETTLED_CAP // 2
        for old in list(_settled)[:drop]:
            del _settled[old]


def _discard_chunk(store, path, st, offset, left, per_file) -> int:
    if offset >= st.st_size:
        _discard.pop(path, None)
        return left
    if left <= 0:
        return left
    requested = min(per_file, left, st.st_size - offset)
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(requested)
    except OSError:
        return left
    left -= len(data)
    nl = data.find(b"\n")
    if nl < 0:
        _set_offset(store, path, st, offset + len(data))
        if offset + len(data) >= st.st_size:
            _discard.pop(path, None)
        return left
    _discard.pop(path, None)
    _set_offset(store, path, st, offset + nl + 1)
    return left


def _emit_usage(store, target, rows, now, splits=None) -> None:
    if not rows:
        return
    run_id = _run_id(target.kind, target.session_id, target.agent_id, target.wf_id)
    for row in rows:
        ts = row.get("ts")
        if ts is None:
            ts = now
        ws = (splits or {}).get(row["message_id"]) if isinstance(splits, dict) else None
        try:
            store.add_usage(
                {
                    "ts": ts,
                    "session_id": target.session_id,
                    "run_id": run_id,
                    "ws_split": ws,
                    "model": row.get("model") or "",
                    "tool": "claude",
                    "message_id": row["message_id"],
                    "input": row["input"],
                    "output": row["output"],
                    "cache_read": row["cache_read"],
                    "cache_write": row["cache_write"],
                    "cache_write_5m": row.get("cache_write_5m"),
                    "cache_write_1h": row.get("cache_write_1h"),
                    "reasoning": row["reasoning"],
                }
            )
        except Exception as exc:
            _log.warning("claude usage: %s", type(exc).__name__)
            return


def _emit_transcript_run(store, target, st, fresh, caught_up, liveness=None, main_running_s=None, now=None) -> None:
    run_id = _run_id(target.kind, target.session_id, target.agent_id, target.wf_id)
    meta_label = None
    meta_model = None
    if target.kind in ("subagent", "workflow"):
        meta_label, meta_model, _meta_tool_use = _read_meta(_meta_path(target.path))
    applied = _apply_label(run_id, meta_label, "meta")
    finished = (target.session_id, target.agent_id) in _finished
    if target.kind == "subagent":
        status = subagent_status(_tail_is_result(target.path), fresh)
    elif target.kind == "main":
        pid = _pids.get(target.session_id)
        pid_dead = pid is not None and not _pid_alive(pid)
        try:
            moment = float(now)
        except (TypeError, ValueError):
            moment = float(st.st_mtime)
        window = main_running_s or MAIN_RUNNING_MTIME_S
        if liveness is None:
            # No precomputed triple (direct callers, tests): the mtime
            # window alone decides, with no child or pending evidence.
            liveness = (moment - st.st_mtime <= window, None, None)
        _main_fresh, child, pending = liveness
        status = decide_main_status(
            caught_up=caught_up,
            mtime_age=moment - st.st_mtime,
            pid_dead=pid_dead,
            has_live_child=bool(child),
            pending_tool=bool(pending),
            main_running_s=window,
        )
    else:
        status = transcript_status(
            caught_up=caught_up,
            fresh=fresh,
            registry_busy=False,
            finished=finished,
        )
        if target.kind == "workflow" and finished:
            status = "done"
        elif target.kind == "workflow" and status == "running" and not fresh:
            status = "done"
    model = None
    last_rows_model = _last_model.get(run_id)
    if last_rows_model:
        model = last_rows_model
    elif isinstance(meta_model, str) and meta_model:
        model = meta_model
    cwd = _cwd.get(run_id)
    if target.kind == "main":
        existing = _sessions.get(target.session_id, {}).get("status")
        if existing in ("busy", "shell", "idle"):
            sess_status = existing
        elif status == "running":
            sess_status = "running"
        elif status == "unknown":
            # Idle but attached: the registry vocabulary already has
            # "idle" for this state, and the run row reads "unknown".
            sess_status = "idle"
        else:
            sess_status = "done"
        _merge_session(
            store,
            {
                "session_id": target.session_id,
                "tool": "claude",
                "cwd": cwd,
                "started_at": _first_ts.get(run_id),
                "status": sess_status,
                "parent_session_id": None,
                "ws_hint": _ws_hint.get(run_id),
            },
        )
    pid = _pids.get(target.session_id) if target.kind == "main" else None
    if pid is not None and not _pid_alive(pid):
        pid = None
    patch = {
        "run_id": run_id,
        "source": _SOURCE[target.kind],
        "tier": tier_of(model),
        "model": model,
        "session_id": target.session_id,
        "parent_session_id": None if target.kind == "main" else target.session_id,
        "status": status,
        "started_at": _first_ts.get(run_id),
        "clock_source": "transcript" if run_id in _first_ts or run_id in _last_ts else "mtime",
        "pid": pid,
    }
    if applied:
        patch["label"] = applied
    if status == "done":
        patch["ended_at"] = _last_ts.get(run_id, st.st_mtime)
        _emit_run(store, patch, force_status=status)
    else:
        # A reopened row stops carrying its close-out verdict: the
        # verdict names how the run ended, and this one did not.
        _emit_run(store, patch, force_status=status, clear=("ended_at", "verdict"))


def _note_bundle_hint(run_id, lines) -> None:
    """Remember a workstream bundle dir this run wrote to.

    Read-only scan of the already-read chunk: Write/Edit tool-use blocks
    whose file path lands under a workstream bundle directory. Only the
    bundle id is kept, never the path or the prompt. The newest evidence
    in the chunk wins.
    """
    found = None
    for line in lines:
        if b"tool_use" not in line:
            continue
        if b'"Write"' not in line and b'"Edit"' not in line:
            continue
        hint = _bundle_hint_from_line(line)
        if hint:
            found = hint
    if found:
        _ws_hint[run_id] = found


def _bundle_hint_from_line(line: bytes) -> str | None:
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") not in ("Write", "Edit"):
            continue
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            continue
        for key in ("file_path", "path"):
            value = tool_input.get(key)
            if not isinstance(value, str) or not value:
                continue
            hint = ws_from_bundle_path(value)
            if hint:
                return hint
    return None


def _note_times(run_id, rows) -> None:
    for row in rows:
        ts = row.get("ts")
        if ts is not None:
            if _saw_bof.get(run_id) or run_id in _first_ts:
                prev = _first_ts.get(run_id)
                _first_ts[run_id] = ts if prev is None else min(prev, ts)
            prev_last = _last_ts.get(run_id)
            _last_ts[run_id] = ts if prev_last is None else max(prev_last, ts)
        if row.get("model"):
            _last_model[run_id] = row["model"]
        if row.get("cwd"):
            _cwd[run_id] = row["cwd"]


def _read_meta(path):
    st = _stat(path)
    if st is None or st.st_size <= 0 or st.st_size > META_MAX:
        return None, None, None
    cached = _meta_cache.get(path)
    stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        with open(path, "rb") as fh:
            raw = fh.read(META_MAX)
    except OSError:
        return None, None, None
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError:
        return None, None, None
    if not isinstance(rec, dict):
        return None, None, None
    desc = rec.get("description")
    model = rec.get("model")
    tool_use = rec.get("toolUseId")
    found = (
        _clean_label(desc) if isinstance(desc, str) else None,
        model if isinstance(model, str) and model else None,
        tool_use if isinstance(tool_use, str) and tool_use and len(tool_use) <= 128 else None,
    )
    _meta_cache[path] = (stamp, found)
    return found


def _meta_path(jsonl_path: str) -> str:
    if jsonl_path.endswith(".jsonl"):
        return jsonl_path[: -len(".jsonl")] + ".meta.json"
    return jsonl_path + ".meta.json"


def _emit_run(store, patch, *, force_status=None, clear=()) -> None:
    run_id = patch.get("run_id")
    if not run_id:
        return
    _seed_run(store, run_id)
    status = force_status or patch.get("status")
    sig = (
        status,
        patch.get("label"),
        patch.get("model"),
        patch.get("started_at"),
        patch.get("ended_at") if "ended_at" in patch else None,
        patch.get("pid"),
        tuple(clear),
    )
    if _emit_sig.get(run_id) == sig:
        return
    _emit_sig[run_id] = sig
    _merge_run(store, patch, clear=clear)


def _seed_run(store, run_id) -> None:
    if run_id in _runs:
        return
    getter = getattr(store, "get_run", None)
    if not callable(getter):
        return
    try:
        row = getter(run_id)
    except Exception:
        return
    if isinstance(row, dict) and row.get("run_id") == run_id:
        _runs[run_id] = dict(row)
        if row.get("started_at") is not None:
            _first_ts.setdefault(run_id, row["started_at"])


def _merge_session(store, patch) -> None:
    sid = patch.get("session_id")
    if not sid:
        return
    merged = dict(_sessions.get(sid, {}))
    for key, value in patch.items():
        if value is None:
            continue
        if key == "started_at" and merged.get("started_at") is not None:
            merged[key] = min(float(merged[key]), float(value))
            continue
        if key == "status" and merged.get("status") in ("busy", "shell", "idle"):
            # Registry status is the live flag. A transcript poll should not
            # replace busy/shell/idle with running/done.
            if value not in ("busy", "shell", "idle"):
                continue
        merged[key] = value
    merged.setdefault("tool", "claude")
    merged["session_id"] = sid
    _sessions[sid] = merged
    try:
        store.upsert_session(dict(merged))
    except Exception as exc:
        _log.warning("claude session: %s", type(exc).__name__)


def _merge_run(store, patch, clear=()) -> None:
    rid = patch["run_id"]
    merged = dict(_runs.get(rid, {}))
    for key, value in patch.items():
        if key in ("session_id", "parent_session_id"):
            merged[key] = value
            continue
        if value is None:
            continue
        if key == "started_at" and merged.get("started_at") is not None:
            merged[key] = min(float(merged[key]), float(value))
            continue
        if key == "ended_at" and merged.get("ended_at") is not None:
            merged[key] = max(float(merged[key]), float(value))
            continue
        if key == "clock_source" and merged.get("clock_source"):
            if _CLOCK_RANK.get(value, 0) < _CLOCK_RANK.get(merged["clock_source"], 0):
                continue
        merged[key] = value
    for key in clear:
        merged[key] = None
    merged["run_id"] = rid
    _runs[rid] = merged
    try:
        store.upsert_run(dict(merged))
    except Exception as exc:
        _log.warning("claude run: %s", type(exc).__name__)


def _journal_fact(rec):
    if not isinstance(rec, dict):
        return None
    kind = rec.get("type")
    if kind not in ("started", "result"):
        return None
    agent = rec.get("agentId")
    if not isinstance(agent, str) or not agent or "/" in agent or "\\" in agent:
        return None
    label = None
    if kind == "started" and isinstance(rec.get("label"), str):
        label = _clean_label(rec.get("label"))
    return agent, label, kind == "result"


def _apply_label(run_id, label, kind) -> str | None:
    if not label:
        return None
    lock = _label_lock.get(run_id)
    if kind == "journal" and lock == "meta":
        return None
    if kind == "meta":
        _label_lock[run_id] = "meta"
    elif lock is None:
        _label_lock[run_id] = "journal"
    return label


def _run_id(kind, session_id, agent_id, wf_id) -> str:
    if kind == "main":
        return f"claude-main:{session_id}:main"
    if kind == "workflow":
        tail = f"{wf_id}/{agent_id}" if wf_id else str(agent_id)
        return f"claude-workflow:{session_id}:{tail}"
    return f"claude-subagent:{session_id}:{agent_id}"


def _usage_row(rec) -> dict | None:
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict) or not usage:
        return None
    message_id = msg.get("id")
    if not message_id:
        return None
    details = usage.get("output_tokens_details")
    thinking = details.get("thinking_tokens") if isinstance(details, dict) else None
    cache_creation = usage.get("cache_creation")
    write_5m = write_1h = None
    if isinstance(cache_creation, dict):
        if "ephemeral_5m_input_tokens" in cache_creation:
            write_5m = _as_int(cache_creation.get("ephemeral_5m_input_tokens"))
        if "ephemeral_1h_input_tokens" in cache_creation:
            write_1h = _as_int(cache_creation.get("ephemeral_1h_input_tokens"))
    model = msg.get("model") or rec.get("requestedModel") or ""
    if not isinstance(model, str):
        model = ""
    cwd = rec.get("cwd") if isinstance(rec.get("cwd"), str) else None
    if cwd and len(cwd) > 1024:
        cwd = cwd[:1024]
    return {
        "message_id": str(message_id),
        "model": model,
        "input": _as_int(usage.get("input_tokens")),
        "output": _as_int(usage.get("output_tokens")),
        "cache_read": _as_int(usage.get("cache_read_input_tokens")),
        "cache_write": _as_int(usage.get("cache_creation_input_tokens")),
        "cache_write_5m": write_5m,
        "cache_write_1h": write_1h,
        "reasoning": _as_int(thinking),
        "ts": epoch_from_iso(rec.get("timestamp")),
        "cwd": cwd,
    }


def read_cache_splits(projects_root: str, wanted: dict) -> dict:
    """Return (session_id, message_id) -> (5m, 1h) from assistant usage only."""
    pending = {sid: set(ids) for sid, ids in wanted.items() if ids}
    if not pending or not projects_root:
        return {}
    files = _jsonl_for_sessions(projects_root, set(pending))
    found = {}
    for sid, paths in files.items():
        need = pending.get(sid)
        if not need:
            continue
        for path in paths:
            try:
                with open(path, "rb") as fh:
                    for line in fh:
                        if not _is_assistant_line(line) or b"cache_creation" not in line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        row = _usage_row(rec)
                        if row is None or row["message_id"] not in need:
                            continue
                        found[(sid, row["message_id"])] = (
                            row.get("cache_write_5m"),
                            row.get("cache_write_1h"),
                        )
            except OSError:
                continue
    return found


def _jsonl_for_sessions(root: str, session_ids: set) -> dict:
    found = {sid: [] for sid in session_ids}
    try:
        walker = os.walk(root, followlinks=False)
    except OSError:
        return found
    for dirpath, _dirnames, filenames in walker:
        parts = set(dirpath.split(os.sep))
        owners = parts.intersection(session_ids)
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            stem = name[: -len(".jsonl")]
            path = os.path.join(dirpath, name)
            targets = set(owners)
            if stem in session_ids:
                targets.add(stem)
            for sid in targets:
                found[sid].append(path)
    for sid, paths in found.items():
        found[sid] = sorted(set(paths))
    return found


def _dedupe(rows: list[dict]) -> list[dict]:
    order = []
    by_id = {}
    for row in rows:
        mid = row["message_id"]
        if mid not in by_id:
            order.append(mid)
        by_id[mid] = row
    return [by_id[mid] for mid in order]


def _is_assistant_line(line: bytes) -> bool:
    if line.startswith(_BLOB_PREFIXES):
        return False
    return any(needle in line for needle in _ASSISTANT_NEEDLES)


def _consume(data: bytes, *, at_eof: bool, stale: bool):
    """Return (lines, nbytes). Only a newline-terminated prefix is consumed
    while the file is still being written. A stale EOF may include a final
    line that has no trailing newline.
    """
    if not data:
        return [], 0
    nl = data.rfind(b"\n")
    if nl == -1:
        if at_eof and stale:
            return [data], len(data)
        return [], 0
    lines = data[:nl].split(b"\n")
    consumed = nl + 1
    tail = data[nl + 1 :]
    if tail and at_eof and stale:
        lines.append(tail)
        consumed = len(data)
    return lines, consumed


def _clean_label(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) > MAX_LABEL:
        text = text[:MAX_LABEL]
    return text


def _as_int(value) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _epoch_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number > 10**14:
        return number / 1_000_000.0
    if number > 10**12:
        return number / 1000.0
    return number


def _budgets(cfg) -> tuple[int, int]:
    raw = cfg.get("read_budgets") or cfg.get("budgets") or {}
    if not isinstance(raw, dict):
        raw = {}
    per_file = raw.get("per_file", raw.get("file", cfg.get("per_file", FILE_BUDGET)))
    per_tick = raw.get("per_tick", raw.get("tick", cfg.get("per_tick", TICK_BUDGET)))
    return max(1, int(per_file)), max(0, int(per_tick))


def _backfill(cfg):
    value = cfg.get("backfill_session")
    if value:
        return str(value)
    if cfg.get("mode") == "backfill" and cfg.get("session_id"):
        return str(cfg["session_id"])
    return None


def _root(cfg, roots_key, flat_key, default):
    roots = cfg.get("roots") if isinstance(cfg.get("roots"), dict) else {}
    for key in (roots_key, flat_key):
        if roots.get(key):
            return os.path.abspath(os.path.expanduser(str(roots[key])))
        if cfg.get(key):
            return os.path.abspath(os.path.expanduser(str(cfg[key])))
    return default


def _default_projects() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def _default_sessions() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "sessions")


def _stat(path):
    try:
        return os.stat(path, follow_symlinks=False)
    except OSError:
        return None


def _offset_of(store, path):
    try:
        prev = store.get_offset(path)
    except Exception:
        return None
    if not prev:
        return None
    try:
        return int(prev[0]), int(prev[1])
    except (TypeError, ValueError, IndexError):
        return None


def _set_offset(store, path, st, offset) -> None:
    store.set_offset(path, st.st_ino, int(offset), st.st_size, st.st_mtime)
