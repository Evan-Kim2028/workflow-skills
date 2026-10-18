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
- ``backfill_session``, or ``mode`` ``backfill`` with ``session_id``
"""

from __future__ import annotations

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
        left = _poll_registry(store, cfg, per_tick)
        _poll_transcripts(store, cfg, now, left, per_file)
        _refresh_inheritance(store, now)
    except Exception as exc:
        _log.warning("claude poll: %s", type(exc).__name__)


def _refresh_inheritance(store, now: float) -> None:
    """Recompute inherited/inferred workstreams; explicit ones are kept."""
    refresh = getattr(store, "refresh_workstream_inheritance", None)
    if not callable(refresh):
        return
    try:
        refresh(now)
    except Exception as exc:
        _log.warning("claude inherit refresh: %s", type(exc).__name__)


def _poll_registry(store, cfg, left) -> int:
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
        else:
            pid = None
        cwd = rec.get("cwd") if isinstance(rec.get("cwd"), str) else None
        if cwd and len(cwd) > 1024:
            cwd = cwd[:1024]
        label = _clean_label(rec.get("name")) if isinstance(rec.get("name"), str) else None
        started = _epoch_number(rec.get("startedAt"))
        run_id = f"claude-main:{sid}:main"
        if status == "busy" and pid is not None and not _pid_alive(pid):
            # Dead pid: the registry still says busy but the process is gone.
            # Close the row at the last evidence (transcript usage or the
            # registry file mtime) instead of leaving it running.
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
                    "pid": pids.get(sid),
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
                    store, target, now, fresh_s, left, per_file, backfill, window
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


def _ingest_jsonl(store, target, now, fresh_s, left, per_file, backfill, window) -> int:
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
        _emit_transcript_run(store, target, st, fresh, caught_up=caught)
        return left

    if offset >= st.st_size and target.path not in _discard:
        key = _settled_key(target, st, fresh)
        if _settled.get(target.path) == key:
            return left
        _emit_transcript_run(store, target, st, fresh, caught_up=True)
        _remember_settled(target.path, key)
        return left
    if left <= 0 or per_file <= 0:
        _emit_transcript_run(store, target, st, fresh, caught_up=False)
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
            _emit_transcript_run(store, target, st, fresh, caught_up=False)
            return left
        if requested >= per_file:
            _discard[target.path] = True
            if _is_assistant_line(data):
                _log.warning("claude oversized line %s", os.path.basename(target.path))
            _set_offset(store, target.path, st, offset + len(data))
        # A short read is the tick budget ending mid-line. Leave the offset
        # so the next tick can take a full per-file window.
        _emit_transcript_run(store, target, st, fresh, caught_up=False)
        return left

    rows = parse_assistant_lines(lines)
    _note_times(run_id, rows)
    _note_bundle_hint(run_id, lines)
    _emit_usage(store, target, rows, now)
    new_offset = offset + consumed
    caught_up = new_offset >= st.st_size
    _set_offset(store, target.path, st, new_offset)
    _emit_transcript_run(store, target, st, fresh, caught_up=caught_up)
    return left


def _settled_key(target, st, fresh):
    """Fingerprint covering every input of the caught-up emit.

    A file with the same key would emit the identical status, label, and
    timestamps, so a repeat poll can skip it without opening it. ``fresh``
    flips once as the file ages out of the running window; the registry
    busy bit and journal ``done`` flags can flip without a file write, so
    they are part of the key too. The main-kind pid check reads the
    registry pid map: a dead pid closes the row even when the transcript
    is untouched.
    """
    if target.kind == "main":
        busy = target.session_id in _busy
        pid = _pids.get(target.session_id)
        pid_alive = None if pid is None else bool(_pid_alive(pid))
        extra = (busy, pid_alive)
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


def _emit_usage(store, target, rows, now) -> None:
    if not rows:
        return
    run_id = _run_id(target.kind, target.session_id, target.agent_id, target.wf_id)
    for row in rows:
        ts = row.get("ts")
        if ts is None:
            ts = now
        try:
            store.add_usage(
                {
                    "ts": ts,
                    "session_id": target.session_id,
                    "run_id": run_id,
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


def _emit_transcript_run(store, target, st, fresh, caught_up) -> None:
    run_id = _run_id(target.kind, target.session_id, target.agent_id, target.wf_id)
    meta_label = None
    meta_model = None
    if target.kind in ("subagent", "workflow"):
        meta_label, meta_model = _read_meta(_meta_path(target.path))
    applied = _apply_label(run_id, meta_label, "meta")
    finished = (target.session_id, target.agent_id) in _finished
    if target.kind == "subagent":
        status = subagent_status(_tail_is_result(target.path), fresh)
    else:
        status = transcript_status(
            caught_up=caught_up,
            fresh=fresh,
            registry_busy=target.kind == "main" and target.session_id in _busy,
            finished=finished,
        )
        if target.kind == "main" and status == "running":
            pid = _pids.get(target.session_id)
            if pid is not None and not _pid_alive(pid):
                status = "done"
            elif not fresh:
                # Busy registry or an unread tail is not liveness on its own:
                # a stale transcript means the session is idle, not running.
                status = "done"
        elif target.kind == "workflow" and finished:
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
        _emit_run(store, patch, force_status=status, clear=("ended_at",))


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
        return None, None
    cached = _meta_cache.get(path)
    stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        with open(path, "rb") as fh:
            raw = fh.read(META_MAX)
    except OSError:
        return None, None
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(rec, dict):
        return None, None
    desc = rec.get("description")
    model = rec.get("model")
    found = (
        _clean_label(desc) if isinstance(desc, str) else None,
        model if isinstance(model, str) and model else None,
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
