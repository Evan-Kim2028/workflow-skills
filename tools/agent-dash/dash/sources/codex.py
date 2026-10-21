"""Codex CLI source: one run per Codex thread from rollout transcripts.

Rollouts live under ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread>.jsonl``.
The file name carries the *thread* id (``payload.id``); the parent session id
is ``payload.session_id``. A top-level TUI thread has ``id == session_id``;
each spawned subagent gets its own rollout file whose ``session_meta``
``thread_source`` is ``subagent`` and whose ``source.subagent.thread_spawn``
names ``parent_thread_id`` (mirrored in ``state_5.sqlite``'s
``thread_spawn_edges``).

Token ledger (verified against real rollouts, see ``tests/test_codex_source.py``):
every model call appends a ``token_usage_record`` whose ``usage`` is the
per-call delta and whose ``thread_token_usage`` is the thread cumulative, plus
an ``event_msg`` of ``payload.type == "token_count"`` echoing the same call as
``info.last_token_usage`` against ``info.total_token_usage``. The additive
ledger is the ``usage`` deltas keyed by ``response_id``: over the nine real
threads on disk ``sum(usage.total_tokens)`` equals the final
``thread_token_usage.total_tokens`` exactly, with no duplicate ``response_id``.
``total_tokens`` equals ``input_tokens`` plus ``output_tokens`` on every
non-compaction row; ``cached_input_tokens`` is a subset of ``input_tokens``
and ``reasoning_output_tokens`` a subset of ``output_tokens``, so the stored
``input`` is the uncached miss (``input - cached``) and ``reasoning`` rides
along unbilled like the other CLI sources. ``token_count`` rows are never
emitted (they echo the same calls); the two compaction-adjacent rows whose
``last_token_usage`` is all zeros with a nonzero ``total_tokens`` are summary
metadata, not charges, and contribute nothing.
``turn_context`` maps each ``turn_id`` to its model, so a session that switches
models mid-flight attributes each call to the model in force.

Labels and privacy: only metadata is ever read (ids, timestamps, model names,
cwd, counts, originator, branch). ``session_meta.base_instructions``,
response bodies, task messages, and compaction history are never touched: the
parser reads each line's ``type`` first and, for whitelisted types, only the
whitelisted scalar fields. A Codex thread carries no user-given label (the
sqlite ``title``/``name`` demonstrably mirror prompt text), so every run is
labelled ``codex-<first 8 of thread id>`` and attributed from cwd/branch and
parentage by the store's usual machinery.

Run id is ``codex:<thread>:<label>``. Source is ``codex`` for a top-level
``codex-tui`` thread (the lead surface, matching the ``lead`` phase rule) and
``codex-exec`` for exec-originated and subagent threads, which classify
through their parent. Tier is ``codex`` either way. Status is ``running``
while the rollout grew within ``LIVE_WINDOW_S``; a quiet file is ``done``
when its latest turn completed (``task_complete``) else ``unknown``.

``cfg["roots"]["codex_sessions"]`` overrides the rollout root. Budgets come
from ``read_budgets`` or ``read_budget`` (``per_file``, ``per_tick``),
default 4 MB and 8 MB. ``backfill_session`` limits discovery to files whose
name contains that string. Files are tailed by byte offset: only new bytes
are parsed each poll (a partial trailing line is held back until it
completes), unchanged files cost one stat, and usage rows upsert on
``(tool, session_id, message_id)`` so re-reads never double count. The
turn-to-model map is rebuilt from ``turn_context`` lines as they stream by;
a usage row whose turn is not mapped yet waits in a small pending set and is
emitted once its turn arrives (or after one streaming whole-file turn scan
per process when the context sits in already-read bytes, e.g. after a
restart).
"""

import json
import logging
import os
import re
import sqlite3
from datetime import datetime

log = logging.getLogger("agent_dash.codex")

NAME = "codex"
INTERVAL_S = 2

DEFAULT_ROOT = os.path.expanduser("~/.codex/sessions")
DEFAULT_DB = os.path.expanduser("~/.codex/state_5.sqlite")
PER_FILE_DEFAULT = 4 * 1024 * 1024
PER_TICK_DEFAULT = 8 * 1024 * 1024
LIVE_WINDOW_S = 120.0
TAIL_SCAN_BYTES = 64 * 1024

TOOL = "codex"
TIER = "codex"
LEAD_SOURCE = "codex"
WORKER_SOURCE = "codex-exec"

_TID_RE = re.compile(
    r"([0-9A-Za-z]{8}-[0-9A-Za-z]{4}-[0-9A-Za-z]{4}-[0-9A-Za-z]{4}-[0-9A-Za-z]{12})\.jsonl$"
)

_META = {}
_TURNS = {}
_LATEST = {}
_TASKS = {}
_STARTED = {}
_LATEST_TS = {}
_PENDING = {}
_EMITTED = {}
_SCANNED = set()
_FILE_TID = {}
_SQLITE = {}

last_error = None


def reset_state() -> None:
    """Drop process-local caches. Tests use this between cases."""
    _META.clear()
    _TURNS.clear()
    _LATEST.clear()
    _TASKS.clear()
    _STARTED.clear()
    _LATEST_TS.clear()
    _PENDING.clear()
    _EMITTED.clear()
    _SCANNED.clear()
    _FILE_TID.clear()
    _SQLITE.clear()


def poll(store, cfg, now):
    global last_error
    last_error = None
    try:
        _poll(store, cfg or {}, float(now))
    except Exception:
        last_error = "poll"
        _fail("poll")


def _poll(store, cfg, now):
    per_file, budget = _budgets(cfg)
    root = _root(cfg, "codex_sessions", "codex", default=DEFAULT_ROOT)
    only = _only_session(cfg)
    if budget <= 0:
        return
    for path in _rollouts(root, only):
        if budget <= 0:
            break
        try:
            budget = _ingest(store, cfg, path, budget, per_file, now)
        except Exception:
            _fail("ingest")


def _rollouts(root, only):
    """Rollout files oldest-first so parents upsert before their subagents."""
    if not root or not os.path.isdir(root):
        return
    found = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            if not name.startswith("rollout-") or not name.endswith(".jsonl"):
                continue
            if only is not None and only not in name:
                continue
            found.append(os.path.join(dirpath, name))
    found.sort()
    return found


def _ingest(store, cfg, path, budget, per_file, now):
    try:
        st = os.stat(path)
    except OSError:
        return budget
    key = "codex:file:" + os.path.abspath(path)
    rec = _offset(store, key)
    if rec and rec[0] == st.st_ino and rec[1] == st.st_size:
        _refresh_run(store, cfg, path, st, now)
        return budget
    try:
        raw, budget, _started = _read_chunk(store, path, key, budget, per_file)
    except OSError:
        _fail("read")
        return budget
    except Exception:
        _fail("chunk")
        return budget
    if raw:
        _apply_chunk(store, cfg, path, raw, now)
    _refresh_run(store, cfg, path, st, now)
    return budget


def _refresh_run(store, cfg, path, st, now):
    """Upsert the run row when its signature changed (stat-only for quiet files)."""
    tid = _FILE_TID.get(os.path.abspath(path)) or _tid_from_name(path)
    if tid is None:
        return
    if tid not in _META:
        _ensure_meta(path, tid)
    live = (now - st.st_mtime) < LIVE_WINDOW_S
    if live:
        status, ended_at = "running", None
    elif _turn_done(cfg, path, tid):
        status, ended_at = "done", _LATEST_TS.get(tid)
    else:
        status, ended_at = "unknown", None
    meta = _META.get(tid, {})
    parent = meta.get("parent_thread_id")
    if parent is None:
        parent = _sqlite_parent(cfg, tid, meta.get("session_id"))
        if parent is not None:
            meta = dict(meta, parent_thread_id=parent)
            _META[tid] = meta
    if tid in _META:
        source = (
            LEAD_SOURCE
            if (parent is None and meta.get("originator") == "codex-tui")
            else WORKER_SOURCE
        )
    elif parent is not None:
        source = WORKER_SOURCE
    else:
        source = None
    model = _LATEST.get(tid)
    label = "codex-" + tid[:8]
    sig = (status, ended_at, model, source, parent)
    if _EMITTED.get(tid) == sig:
        return
    _EMITTED[tid] = sig
    row = {
        "run_id": "codex:%s:%s" % (tid, label),
        "tier": TIER,
        "session_id": tid,
        "label": label,
        "status": status,
        "started_at": _STARTED.get(tid),
        "ended_at": ended_at,
    }
    if source is not None:
        row["source"] = source
    if model is not None:
        row["model"] = model
    if parent is not None:
        row["parent_session_id"] = parent
    try:
        store.upsert_run(row)
    except Exception:
        _fail("run")
        return
    session = {"session_id": tid, "tool": TOOL, "label": label, "status": status}
    if meta.get("cwd") is not None:
        session["cwd"] = meta["cwd"]
    if row.get("started_at") is not None:
        session["started_at"] = row["started_at"]
    if parent is not None:
        session["parent_session_id"] = parent
    try:
        store.upsert_session(session)
    except Exception:
        _fail("session")


def _turn_done(cfg, path, tid):
    """True when the thread's latest turn completed (memory, else tail scan)."""
    if tid in _TASKS:
        return _TASKS[tid] == "task_complete"
    kind, latest = _tail_tasks(path)
    if kind is not None:
        _TASKS[tid] = kind
    if latest is not None and (
        tid not in _LATEST_TS
        or latest > (_LATEST_TS[tid] if isinstance(_LATEST_TS[tid], (int, float)) else -1.0)
    ):
        _LATEST_TS[tid] = latest
    return kind == "task_complete"


def _tail_tasks(path):
    """Latest task kind and newest task ts from the file tail (bounded)."""
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    if st.st_size <= 0:
        return None, None
    start = max(0, st.st_size - TAIL_SCAN_BYTES)
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            blob = fh.read(st.st_size - start)
    except OSError:
        return None, None
    kind, latest = None, None
    for raw in blob.split(b"\n"):
        raw = raw.strip()
        if not raw or b"task_" not in raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "event_msg":
            continue
        task = _task(obj.get("payload"), _epoch_any(obj.get("timestamp")))
        if task is None:
            continue
        ts = task["ts"] if isinstance(task.get("ts"), (int, float)) else -1.0
        if latest is None or ts >= latest:
            latest = ts
            kind = task["kind"]
    return kind, latest


def _apply_chunk(store, cfg, path, raw, now):
    abspath = os.path.abspath(path)
    hint = _FILE_TID.get(abspath) or _tid_from_name(path)
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        ts = _epoch_any(obj.get("timestamp"))
        if kind == "session_meta":
            meta = _meta(obj.get("payload"))
            if meta is None:
                continue
            tid = meta["thread_id"]
            _FILE_TID[abspath] = tid
            if tid not in _META:
                _META[tid] = meta
                _STARTED[tid] = ts
            elif meta.get("parent_thread_id") and not _META[tid].get("parent_thread_id"):
                _META[tid] = dict(_META[tid], parent_thread_id=meta["parent_thread_id"])
            if meta.get("cwd") and not _META[tid].get("cwd"):
                _META[tid] = dict(_META[tid], cwd=meta["cwd"])
            if ts is not None and (
                tid not in _LATEST_TS or ts > (_LATEST_TS[tid] or -1.0)
            ):
                _LATEST_TS[tid] = ts
            hint = tid
        elif kind == "turn_context":
            turn = _turn(obj.get("payload"), ts)
            if turn is None:
                continue
            tid = hint
            if tid is None:
                continue
            _TURNS.setdefault(tid, {})[turn["turn_id"]] = turn
            if turn.get("model"):
                _LATEST[tid] = turn["model"]
            if ts is not None and (
                tid not in _LATEST_TS or ts > (_LATEST_TS[tid] or -1.0)
            ):
                _LATEST_TS[tid] = ts
            _flush_pending(store, tid, turn["turn_id"], now)
        elif kind == "token_usage_record":
            call = _call(obj.get("payload"), ts)
            if call is None:
                continue
            tid = call["thread_id"]
            _FILE_TID.setdefault(abspath, tid)
            _emit_call(store, cfg, path, call, now)
            if ts is not None and (
                tid not in _LATEST_TS or ts > (_LATEST_TS[tid] or -1.0)
            ):
                _LATEST_TS[tid] = ts
        elif kind == "event_msg":
            task = _task(obj.get("payload"), ts)
            if task is None:
                continue
            tid = hint
            if tid is None:
                continue
            _TASKS[tid] = task["kind"]
            if ts is not None and (
                tid not in _LATEST_TS or ts > (_LATEST_TS[tid] or -1.0)
            ):
                _LATEST_TS[tid] = ts


def _emit_call(store, cfg, path, call, now):
    tid = call["thread_id"]
    model = _turn_model(path, tid, call.get("turn_id"))
    usage = call["usage"]
    row = {
        "ts": call["ts"] if call["ts"] is not None else now,
        "session_id": tid,
        "run_id": "codex:%s:codex-%s" % (tid, tid[:8]),
        "model": model,
        "tool": TOOL,
        "message_id": call["response_id"],
        "input": max(0, usage["input_tokens"] - usage["cached_input_tokens"]),
        "output": usage["output_tokens"],
        "cache_read": usage["cached_input_tokens"],
        "cache_write": usage["cache_write_input_tokens"],
        "reasoning": usage["reasoning_output_tokens"],
    }
    if model is None:
        # Track only calls whose turn may still arrive: a turn already seen
        # (with no model) never resolves, so it is not worth remembering.
        # The row is already stored above with a null model; the flush only
        # upgrades it in place on the same (tool, session, message) key.
        if call.get("turn_id") is not None and call["turn_id"] not in _TURNS.get(tid, {}):
            if len(_PENDING) < 50000:
                _PENDING[(tid, call["response_id"])] = (call.get("turn_id"), dict(row))
    else:
        _PENDING.pop((tid, call["response_id"]), None)
        _LATEST[tid] = model
    try:
        store.add_usage(row)
    except Exception:
        _fail("usage")


def _turn_model(path, tid, turn_id):
    if turn_id is not None:
        hit = _TURNS.get(tid, {}).get(turn_id)
        if hit is not None and hit.get("model"):
            return hit["model"]
    if tid not in _SCANNED:
        _SCANNED.add(tid)
        _scan_turns(path, tid)
        if turn_id is not None:
            hit = _TURNS.get(tid, {}).get(turn_id)
            if hit is not None and hit.get("model"):
                return hit["model"]
    return _LATEST.get(tid)


def _scan_turns(path, tid):
    """One streaming pass for ``turn_context`` lines (prefiltered, constant memory).

    Only lines mentioning ``turn_context`` are parsed, and only their turn
    id and model are kept. Runs at most once per process per thread; the
    common case (contexts streaming by in order) never needs it.
    """
    try:
        st = os.stat(path)
    except OSError:
        return
    if st.st_size <= 0:
        return
    try:
        fh = open(path, "rb")
    except OSError:
        return
    with fh:
        carry = b""
        while True:
            blob = fh.read(1 << 20)
            if not blob:
                break
            blob = carry + blob
            parts = blob.split(b"\n")
            carry = parts.pop()
            for raw in parts:
                if b"turn_context" not in raw:
                    continue
                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, UnicodeError):
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "turn_context":
                    continue
                turn = _turn(obj.get("payload"), None)
                if turn is None:
                    continue
                _TURNS.setdefault(tid, {})[turn["turn_id"]] = turn
                if turn.get("model"):
                    _LATEST[tid] = turn["model"]


def _flush_pending(store, tid, turn_id, now):
    for key in [key for key in _PENDING if key[0] == tid and _PENDING[key][0] == turn_id]:
        _turn_id, row = _PENDING.pop(key)
        model = _TURNS.get(tid, {}).get(turn_id, {}).get("model")
        if model is None:
            _PENDING[key] = (_turn_id, row)
            continue
        row = dict(row, model=model, ts=row.get("ts") if row.get("ts") is not None else now)
        _LATEST[tid] = model
        try:
            store.add_usage(row)
        except Exception:
            _fail("usage")


def _tid_from_name(path):
    try:
        base = os.path.basename(path)
    except (TypeError, ValueError):
        return None
    match = _TID_RE.search(base)
    return match.group(1) if match else None


def _meta(payload):
    """Thread metadata. Never reads ``base_instructions`` or any content."""
    if not isinstance(payload, dict):
        return None
    thread_id = payload.get("id")
    if not isinstance(thread_id, str) or not thread_id:
        return None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = thread_id
    cwd = payload.get("cwd")
    originator = payload.get("originator")
    thread_source = payload.get("thread_source")
    version = payload.get("cli_version")
    git = payload.get("git")
    branch = git.get("branch") if isinstance(git, dict) else None
    parent = None
    source = payload.get("source")
    if isinstance(source, dict):
        sub = source.get("subagent")
        if isinstance(sub, dict):
            spawn = sub.get("thread_spawn")
            if isinstance(spawn, dict):
                candidate = spawn.get("parent_thread_id")
                if isinstance(candidate, str) and candidate:
                    parent = candidate
    if parent is None and thread_source == "subagent" and session_id != thread_id:
        parent = session_id
    return {
        "thread_id": thread_id,
        "session_id": session_id,
        "cwd": cwd if isinstance(cwd, str) and cwd else None,
        "originator": originator if isinstance(originator, str) else None,
        "thread_source": thread_source if isinstance(thread_source, str) else None,
        "cli_version": version if isinstance(version, str) else None,
        "branch": branch if isinstance(branch, str) and branch else None,
        "parent_thread_id": parent,
    }


def _turn(payload, ts):
    if not isinstance(payload, dict):
        return None
    turn_id = payload.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        return None
    model = payload.get("model")
    cwd = payload.get("cwd")
    effort = payload.get("effort")
    return {
        "turn_id": turn_id,
        "model": model if isinstance(model, str) and model else None,
        "cwd": cwd if isinstance(cwd, str) and cwd else None,
        "effort": effort if isinstance(effort, str) else None,
        "ts": ts,
    }


def _usage_triple(value):
    if not isinstance(value, dict):
        return None
    try:
        numbers = {key: _num(value.get(key)) for key in (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        )}
    except (TypeError, ValueError):
        return None
    return numbers


def _call(payload, ts):
    if not isinstance(payload, dict):
        return None
    response_id = payload.get("response_id")
    if not isinstance(response_id, str) or not response_id:
        return None
    thread_id = payload.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        thread_id = payload.get("session_id")
    if not isinstance(thread_id, str) or not thread_id:
        return None
    usage = _usage_triple(payload.get("usage"))
    if usage is None:
        return None
    turn_id = payload.get("turn_id")
    return {
        "response_id": response_id,
        "thread_id": thread_id,
        "turn_id": turn_id if isinstance(turn_id, str) else None,
        "ts": ts,
        "usage": usage,
    }


def _task(payload, ts):
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    if kind not in ("task_started", "task_complete"):
        return None
    turn_id = payload.get("turn_id")
    root = payload.get("root_turn_id")
    return {
        "kind": kind,
        "turn_id": turn_id if isinstance(turn_id, str) else None,
        "root_turn_id": root if isinstance(root, str) else None,
        "ts": ts,
    }


def _ensure_meta(path, tid):
    """Parse the head line for ``session_meta`` (bounded, once per file).

    A fresh process meets quiet files mid-history: their offsets are already
    consumed, so the meta line never streams by again. The head line alone
    restores thread identity without re-reading the transcript.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(262144)
    except OSError:
        return
    cut = head.find(b"\n")
    if cut < 0:
        return
    try:
        obj = json.loads(head[:cut])
    except (json.JSONDecodeError, UnicodeError):
        return
    if not isinstance(obj, dict) or obj.get("type") != "session_meta":
        return
    meta = _meta(obj.get("payload"))
    if meta is None:
        return
    found = meta["thread_id"]
    _FILE_TID[os.path.abspath(path)] = found
    if found not in _META:
        _META[found] = meta
        _STARTED[found] = _epoch_any(obj.get("timestamp"))
    if tid != found and tid not in _META:
        _META[tid] = dict(meta, thread_id=tid)


def _sqlite_parent(cfg, tid, session_id):
    """Parent thread id from ``thread_spawn_edges``, rollout fallback only.

    Opened read-only; any failure (WAL lock, missing db) silently yields
    nothing so the sqlite cross-check never breaks the rollout ledger.
    The outcome is cached per process: the edges table only grows.
    """
    if tid in _SQLITE:
        return _SQLITE[tid]
    db = _root(cfg, "codex_db", default=DEFAULT_DB)
    try:
        uri = "file:" + db + "?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
    except Exception:
        return None
    try:
        try:
            row = con.execute(
                "SELECT parent_thread_id FROM thread_spawn_edges WHERE child_thread_id=? LIMIT 1",
                (tid,),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row and isinstance(row[0], str) and row[0]:
            _SQLITE[tid] = row[0]
            return row[0]
    finally:
        try:
            con.close()
        except Exception:
            pass
    if isinstance(session_id, str) and session_id and session_id != tid:
        _SQLITE[tid] = session_id
        return session_id
    return None


def _fail(token):
    global last_error
    last_error = token


def _offset(store, key):
    try:
        rec = store.get_offset(key)
    except Exception:
        return None
    if not rec:
        return None
    try:
        rec[0], rec[1]
    except (TypeError, IndexError):
        return None
    return rec


def _read_chunk(store, path, key, budget, per_file):
    st = os.stat(path)
    rec = _offset(store, key)
    if rec and rec[0] == st.st_ino and 0 <= rec[1] <= st.st_size:
        offset = int(rec[1])
    else:
        offset = 0
    if offset >= st.st_size or budget <= 0:
        if budget > 0 and offset >= st.st_size and rec is None:
            store.set_offset(key, st.st_ino, offset, st.st_size, st.st_mtime)
        return b"", budget, offset == 0
    want = st.st_size - offset
    if want > per_file:
        want = per_file
    if want > budget:
        want = budget
    with open(path, "rb") as fh:
        fh.seek(offset)
        disk = fh.read(want)
    charge = len(disk)
    if not disk:
        return b"", budget, offset == 0
    end = offset + charge
    if disk.endswith(b"\n"):
        kept = disk
        new_off = end
    else:
        # A live file can end mid-line: hold the fragment back until the
        # writer completes it. Never store a partial line.
        cut = disk.rfind(b"\n")
        if cut < 0:
            return b"", budget - charge, offset == 0
        kept = disk[: cut + 1]
        new_off = offset + len(kept)
    store.set_offset(key, st.st_ino, new_off, st.st_size, st.st_mtime)
    return kept, budget - charge, offset == 0


def _budgets(cfg):
    raw = cfg.get("read_budgets") or cfg.get("read_budget") or cfg.get("budgets") or {}
    if not isinstance(raw, dict):
        raw = {}
    per_file = raw.get(
        "per_file",
        raw.get(
            "per_file_bytes",
            raw.get("file", cfg.get("read_budget_per_file", cfg.get("per_file", cfg.get("per_file_bytes", PER_FILE_DEFAULT)))),
        ),
    )
    per_tick = raw.get(
        "per_tick",
        raw.get(
            "per_tick_bytes",
            raw.get("tick", cfg.get("read_budget_per_tick", cfg.get("per_tick", cfg.get("per_tick_bytes", PER_TICK_DEFAULT)))),
        ),
    )
    return max(1, int(per_file)), max(0, int(per_tick))


def _only_session(cfg):
    value = cfg.get("backfill_session")
    if value:
        return str(value)
    if cfg.get("mode") == "backfill" and cfg.get("session_id"):
        return str(cfg["session_id"])
    return None


def _root(cfg, *keys, default):
    roots = cfg.get("roots") if isinstance(cfg.get("roots"), dict) else {}
    for key in keys:
        val = roots.get(key) or cfg.get(key)
        if val:
            return os.path.expanduser(str(val))
    return default


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _epoch_any(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value > 10**14:
            return float(value) / 1_000_000.0
        if value > 10**12:
            return float(value) / 1_000.0
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None
