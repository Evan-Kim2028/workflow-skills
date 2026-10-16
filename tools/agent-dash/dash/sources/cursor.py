"""Cursor-agent source.

Chats live in ``~/.cursor/chats/<md5(cwd)>/<uuid>/``. The run label is the
first ``[label: ...]`` in that chat's transcript under
``~/.cursor/projects/<encoded cwd>/agent-transcripts/``. A
``cursor-sessions.tsv`` row (``label``, ``-``, started epoch) supplies a
label only when the transcript has none, the start is unique within 5
seconds, and the cwd is a workspace that transcript-labeled rows from the
same manifest already use.

Run id is ``grok:<chat-uuid>:<label>``. Source and tier are ``grok``.
Status is ``running`` when ``updatedAtMs`` is less than 60 seconds old or
a live ``cursor-agent`` process carries that label. Otherwise a chat with
a timestamp is ``done``.

The local store has no billed input, output, cache, or reasoning counts.
A usage row is written only when a blob contains those JSON fields.
Checkpoint context-window estimates are not billed usage and are not stored.

A non-empty ``store.db-wal`` is copied under the agent-dash tmp dir and
opened ``mode=ro``. A database with no WAL is opened ``mode=ro&immutable=1``
so sqlite does not create a shm beside the original. Offsets on the
``store.db`` path remember ``updatedAtMs``. Each handled blob id has its
own offset key.
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

log = logging.getLogger("agent_dash.cursor")

NAME = "cursor"
INTERVAL_S = 2

DEFAULT_HOME = os.path.expanduser("~/.cursor")
DEFAULT_TMP = os.path.expanduser("~/.local/share/agent-dash/tmp")
PER_FILE_DEFAULT = 4 * 1024 * 1024
PER_TICK_DEFAULT = 8 * 1024 * 1024
RUNNING_WINDOW_S = 60.0
MANIFEST_WINDOW_S = 5.0
MANIFEST_GAP_S = 0.05
LABEL_BYTES = 2048
MIN_OPEN = 4096
TOOL = "cursor"

LABEL_RE = re.compile(r"\[label:\s*([^\]]+)\]")
MODEL_RE = re.compile(rb'"modelName"\s*:\s*"([^"\\]+)"')
HEX_ID = re.compile(r"^[0-9a-fA-F]{8,128}$")
TOKEN_RE = re.compile(
    rb'"(input_tokens|output_tokens|cache_read_tokens|cache_write_tokens|reasoning_tokens)"\s*:\s*(-?\d+)'
)
TOKEN_MAP = {
    b"input_tokens": "input",
    b"output_tokens": "output",
    b"cache_read_tokens": "cache_read",
    b"cache_write_tokens": "cache_write",
    b"reasoning_tokens": "reasoning",
}
SECRET_MARKERS = ("encrypt", "secret", "password", "token", "apikey", "api_key")

_LABEL_CACHE = {}
_TRANSCRIPTS = {}
_MODEL_COUNTS = {}
last_error = None


def poll(store, cfg, now):
    global last_error
    last_error = None
    try:
        _poll(store, cfg or {}, float(now))
    except Exception:
        last_error = "poll"
        _fail("poll")


def live_label_pids():
    """Map a ``[label: ...]`` on a live cursor-agent process to its pid.

    The command line is not stored. Only the label and pid are kept.
    """
    try:
        proc = subprocess.run(
            ["ps", "-axww", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    found = {}
    for line in proc.stdout.splitlines():
        if "cursor-agent" not in line:
            continue
        match = LABEL_RE.search(line)
        if not match:
            continue
        head = line.strip().split(None, 1)
        if not head:
            continue
        try:
            pid = int(head[0])
        except ValueError:
            continue
        label = _clean_label(match.group(1))
        if label:
            found[label] = pid
    return found


def encode_cwd(cwd):
    """Project directory name Cursor uses for a workspace path."""
    if not isinstance(cwd, str):
        return ""
    parts = []
    for part in cwd.split("/"):
        if not part:
            continue
        cleaned = part.lstrip(".-")
        if cleaned:
            parts.append(cleaned)
    return "-".join(parts)


def open_store(path, tmp_root, budget, tick_budget):
    """Return ``(connection, cleanup_dir, budget, status)``.

    ``status`` is ``ok``, ``need-budget``, ``too-big``, or ``error``.
    ``cleanup_dir`` is set when a WAL copy was made and must be removed
    after the connection is closed.
    """
    wal_size = _file_size(path + "-wal")
    if wal_size > 0:
        db_size = _file_size(path)
        need = db_size + wal_size
        if need > tick_budget:
            return None, None, budget, "too-big"
        if need > budget:
            return None, None, budget, "need-budget"
        try:
            os.makedirs(tmp_root, exist_ok=True)
            dest_dir = tempfile.mkdtemp(prefix="cursor-", dir=tmp_root)
            dest = os.path.join(dest_dir, "store.db")
            shutil.copyfile(path, dest)
            shutil.copyfile(path + "-wal", dest + "-wal")
        except OSError:
            _fail("wal-copy")
            return None, None, budget, "error"
        budget -= need
        uri = Path(dest).as_uri() + "?mode=ro"
        try:
            con = sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            shutil.rmtree(dest_dir, ignore_errors=True)
            _fail("wal-open")
            return None, None, budget, "error"
        return con, dest_dir, budget, "ok"
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        _fail("immutable-open")
        return None, None, budget, "error"
    return con, None, budget, "ok"


def _poll(store, cfg, now):
    per_file, budget = _budgets(cfg)
    tick_budget = budget
    if budget <= 0:
        return
    home = _home(cfg)
    tmp_root = _tmp_root(cfg)
    only = _only_session(cfg)
    allow = _allow_labels(cfg)

    rows = []
    for scratch in _scratch_roots(cfg):
        for pad, parent in _manifest_dirs(scratch, "cursor-sessions.tsv", only):
            path = os.path.join(pad, "cursor-sessions.tsv")
            text, budget = _read_small(path, budget, per_file)
            if text is None:
                continue
            for row in _parse_manifest(text):
                row["parent"] = parent
                row["path"] = path
                rows.append(row)
    if only:
        manifest_labels = {row["label"] for row in rows}
        allow = manifest_labels if allow is None else allow & manifest_labels

    chats, budget = _discover(home, budget, per_file)
    if not chats:
        return
    budget = _attach_labels(home, chats, budget, per_file, allow)
    _assign(chats, rows)
    _attach_pids(chats, live_label_pids())

    chosen = []
    for chat in chats:
        if allow is not None and chat.get("label") not in allow:
            continue
        if not _worth_tracking(chat, now):
            continue
        _upsert(store, chat, now, None)
        chosen.append(chat)

    pending = [chat for chat in chosen if not _db_fresh(store, chat)]
    _drain(store, pending, budget, per_file, tick_budget, now, tmp_root)


def _worth_tracking(chat, now):
    if chat.get("label"):
        return True
    if chat.get("pid"):
        return True
    updated = chat.get("updated_ms")
    if isinstance(updated, int) and now - (updated / 1000.0) < RUNNING_WINDOW_S:
        return True
    return False


def _drain(store, pending, budget, per_file, tick_budget, now, tmp_root):
    hot = []
    cold = []
    for chat in pending:
        if _is_hot(chat, now):
            hot.append(chat)
        else:
            cold.append(chat)
    hot.sort(key=lambda chat: chat["uuid"])
    cold.sort(key=lambda chat: chat["uuid"])
    budget = _drain_queue(store, hot, "cursor:rotate-hot", budget, per_file, tick_budget, now, tmp_root)
    if budget >= MIN_OPEN:
        _drain_queue(store, cold, "cursor:rotate-cold", budget, per_file, tick_budget, now, tmp_root)


def _drain_queue(store, chats, key, budget, per_file, tick_budget, now, tmp_root):
    if not chats or budget < MIN_OPEN:
        return budget
    n = len(chats)
    rec = _offset(store, key)
    start = int(rec[1]) % n if rec else 0
    for step in range(n):
        if budget < MIN_OPEN:
            _stamp(store, key, (start + step) % n, n, now)
            return budget
        chat = chats[(start + step) % n]
        status, budget, model = _ingest_db(
            store, chat, budget, per_file, tick_budget, tmp_root, now
        )
        if model:
            _upsert(store, chat, now, model)
        if status == "need-budget":
            _stamp(store, key, (start + step) % n, n, now)
            return budget
        if budget < MIN_OPEN:
            _stamp(store, key, (start + step + 1) % n, n, now)
            return budget
    _stamp(store, key, (start + n) % n, n, now)
    return budget


def _ingest_db(store, chat, budget, per_file, tick_budget, tmp_root, now):
    path = chat.get("store_path")
    if not path or not os.path.isfile(path):
        return "missing", budget, None
    try:
        st = os.stat(path)
    except OSError:
        return "error", budget, None
    if _db_fresh(store, chat):
        return "fresh", budget, None
    inode = int(st.st_ino)
    store_key = _abs(path)
    rec = _offset(store, store_key)
    if rec and rec[0] != inode:
        _MODEL_COUNTS.pop(store_key, None)

    con, cleanup, budget, status = open_store(path, tmp_root, budget, tick_budget)
    if con is None:
        return status, budget, _top_model(store_key)
    deferred = False
    file_left = per_file
    events = []
    try:
        try:
            root_id, _created = _root_id(con)
        except sqlite3.Error:
            _fail("meta")
            return "error", budget, None
        try:
            indexed, budget, file_left = _blob_index(con, budget, file_left)
        except sqlite3.Error:
            _fail("blobs")
            return "error", budget, None
        if indexed is None:
            return "need-budget", budget, _top_model(store_key)

        root_row = None
        assistants = []
        others = []
        for blob_id, length, prefix in indexed:
            blob_key = _blob_key(store_key, blob_id)
            if _offset(store, blob_key) is not None:
                continue
            if root_id and blob_id == root_id:
                root_row = (blob_id, length, prefix)
            elif _is_assistant(prefix):
                assistants.append((blob_id, length, prefix))
            else:
                others.append(blob_id)
        assistants.sort(key=lambda item: item[1])

        if root_row is not None:
            ok, budget, file_left = _take_blob(
                store, con, store_key, root_row[0], root_row[1], budget, file_left, per_file, events
            )
            if not ok:
                deferred = True
        for blob_id, length, _prefix in assistants:
            if budget < MIN_OPEN and length > budget:
                deferred = True
                continue
            ok, budget, file_left = _take_blob(
                store, con, store_key, blob_id, length, budget, file_left, per_file, events
            )
            if not ok:
                deferred = True
        for blob_id in others:
            _mark_blob(store, store_key, blob_id)
        if not deferred:
            _stamp_store(store, path, st, _cursor_of(chat, st))
            return "ok", budget, _top_model(store_key)
        return "partial", budget, _top_model(store_key)
    except sqlite3.Error:
        _fail("ingest")
        return "error", budget, _top_model(store_key)
    finally:
        _emit_events(store, chat, now, events, store_key)
        _close_store(con, cleanup)


def _take_blob(store, con, store_key, blob_id, length, budget, file_left, per_file, events):
    if length > per_file:
        sample = min(8192, budget, file_left)
        if sample <= 0:
            return False, budget, file_left
        try:
            row = con.execute(
                "SELECT substr(data, 1, ?) FROM blobs WHERE id=?",
                (sample, blob_id),
            ).fetchone()
        except sqlite3.Error:
            _fail("blob")
            return False, budget, file_left
        data = _as_bytes(row[0] if row else None)
        spent = len(data)
        if spent > budget or spent > file_left:
            return False, budget, file_left
        budget -= spent
        file_left -= spent
        _consume_blob(store_key, blob_id, data, events)
        _mark_blob(store, store_key, blob_id)
        return True, budget, file_left
    if length > budget or length > file_left:
        return False, budget, file_left
    try:
        row = con.execute("SELECT data FROM blobs WHERE id=?", (blob_id,)).fetchone()
    except sqlite3.Error:
        _fail("blob")
        return False, budget, file_left
    data = _as_bytes(row[0] if row else None)
    spent = len(data)
    if spent > budget or spent > file_left:
        return False, budget, file_left
    budget -= spent
    file_left -= spent
    _consume_blob(store_key, blob_id, data, events)
    _mark_blob(store, store_key, blob_id)
    return True, budget, file_left


def _consume_blob(store_key, blob_id, data, events):
    if not data:
        return
    if data.startswith(b'{"role":"assistant"') or data.startswith(b'{"role": "assistant"'):
        for match in MODEL_RE.finditer(data):
            try:
                name = match.group(1).decode("utf-8")
            except UnicodeDecodeError:
                continue
            if name:
                _MODEL_COUNTS.setdefault(store_key, Counter())[name] += 1
    billing = _billing(data)
    if billing is not None:
        events.append((blob_id, billing))


def _emit_events(store, chat, now, events, store_key):
    if not events:
        return
    model = _top_model(store_key)
    ts = _usage_ts(chat, now)
    run_id = _run_id(chat)
    for blob_id, billing in events:
        _emit_usage(
            store,
            {
                "ts": ts,
                "session_id": chat["uuid"],
                "run_id": run_id,
                "model": model,
                "tool": TOOL,
                "message_id": blob_id,
                "input": billing.get("input"),
                "output": billing.get("output"),
                "cache_read": billing.get("cache_read"),
                "cache_write": billing.get("cache_write"),
                "reasoning": billing.get("reasoning"),
            },
        )


def _emit_usage(store, row):
    try:
        store.add_usage(row)
    except Exception:
        _fail("usage")


def _usage_ts(chat, now):
    updated = chat.get("updated_ms")
    if isinstance(updated, int):
        return updated / 1000.0
    return now


def _billing(data):
    if not isinstance(data, (bytes, bytearray)) or b"_tokens" not in data:
        return None
    found = {}
    for match in TOKEN_RE.finditer(data):
        key = TOKEN_MAP.get(match.group(1))
        if key:
            found[key] = int(match.group(2))
    if "input" not in found or "output" not in found:
        return None
    return found


def _blob_index(con, budget, file_left):
    rows = con.execute("SELECT id, length(data), substr(data, 1, 24) FROM blobs").fetchall()
    indexed = []
    charge = 0
    for blob_id, length, prefix in rows:
        ident = _blob_ident(blob_id)
        if not ident:
            continue
        raw = _as_bytes(prefix)
        charge += len(raw)
        try:
            size = int(length or 0)
        except (TypeError, ValueError):
            size = 0
        indexed.append((ident, size, raw))
    if charge > budget or charge > file_left:
        return None, budget, file_left
    return indexed, budget - charge, file_left - charge


def _root_id(con):
    row = con.execute("SELECT value FROM meta WHERE key='0'").fetchone()
    if not row or row[0] is None:
        return None, None
    obj = _parse_meta(row[0])
    root = obj.get("latestRootBlobId")
    created = obj.get("createdAt")
    if isinstance(root, str) and HEX_ID.match(root):
        root_id = root.lower()
    else:
        root_id = None
    created_ms = int(created) if isinstance(created, (int, float)) and not isinstance(created, bool) else None
    return root_id, created_ms


def _parse_meta(raw):
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {}
    if not isinstance(raw, str):
        return {}
    text = raw.strip()
    if not text:
        return {}
    if text[0] != "{":
        try:
            text = bytes.fromhex(text).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return {}
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(obj, dict):
        return {}
    for key in list(obj):
        low = key.lower()
        if any(mark in low for mark in SECRET_MARKERS):
            obj.pop(key, None)
    return obj


def _discover(home, budget, per_file):
    root = os.path.join(home, "chats")
    found = []
    if not os.path.isdir(root):
        return found, budget
    try:
        buckets = os.scandir(root)
    except OSError:
        return found, budget
    with buckets:
        for bucket in buckets:
            try:
                if not bucket.is_dir(follow_symlinks=False):
                    continue
                chats = os.scandir(bucket.path)
            except OSError:
                continue
            with chats:
                for chat in chats:
                    try:
                        if not chat.is_dir(follow_symlinks=False) or not _is_uuid(chat.name):
                            continue
                    except OSError:
                        continue
                    meta_path = os.path.join(chat.path, "meta.json")
                    meta, budget = _read_json(meta_path, budget, per_file)
                    if not isinstance(meta, dict):
                        continue
                    cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else None
                    found.append(
                        {
                            "uuid": chat.name,
                            "directory": chat.path,
                            "store_path": os.path.join(chat.path, "store.db"),
                            "cwd": cwd,
                            "created_ms": _ms(meta.get("createdAtMs")),
                            "updated_ms": _ms(meta.get("updatedAtMs")),
                            "label": None,
                            "label_from": None,
                            "parent": None,
                            "manifest_epoch": None,
                            "transcript": None,
                            "pid": None,
                        }
                    )
    return found, budget


def _attach_labels(home, chats, budget, per_file, allow):
    have = set()
    for chat in chats:
        if allow is not None and allow <= have:
            break
        path = _transcript(home, chat.get("cwd"), chat["uuid"])
        chat["transcript"] = path
        if not path:
            continue
        label, budget = _read_label(path, budget, per_file)
        if label:
            chat["label"] = label
            chat["label_from"] = "transcript"
            have.add(label)
    return budget


def _transcript(home, cwd, uuid):
    key = (home, uuid)
    if key in _TRANSCRIPTS:
        cached = _TRANSCRIPTS[key]
        return cached or None
    path = None
    if cwd:
        direct = os.path.join(
            home, "projects", encode_cwd(cwd), "agent-transcripts", uuid, uuid + ".jsonl"
        )
        if os.path.isfile(direct):
            path = direct
    if path is None and os.path.isdir(os.path.join(home, "projects")):
        try:
            projects = os.scandir(os.path.join(home, "projects"))
        except OSError:
            projects = None
        if projects is not None:
            with projects:
                for proj in projects:
                    cand = os.path.join(proj.path, "agent-transcripts", uuid, uuid + ".jsonl")
                    if os.path.isfile(cand):
                        path = cand
                        break
    _TRANSCRIPTS[key] = path or ""
    return path


def _read_label(path, budget, per_file):
    try:
        st = os.stat(path)
    except OSError:
        return None, budget
    mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
    hit = _LABEL_CACHE.get(path)
    if hit and hit[0] == mtime_ns and hit[1] == st.st_size:
        return hit[2] or None, budget
    n = min(LABEL_BYTES, st.st_size)
    if n > per_file or n > budget:
        return None, budget
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(n)
    except OSError:
        return None, budget
    budget -= len(chunk)
    label = _first_label(chunk)
    if label or len(chunk) >= st.st_size or n >= LABEL_BYTES:
        _LABEL_CACHE[path] = (mtime_ns, st.st_size, label or "")
    return label, budget


def _first_label(chunk):
    if not chunk:
        return None
    match = LABEL_RE.search(chunk.decode("utf-8", "replace"))
    if not match:
        return None
    return _clean_label(match.group(1))


def _assign(chats, rows):
    for chat in chats:
        label = chat.get("label")
        if not label:
            continue
        best = None
        best_delta = None
        created = chat.get("created_ms")
        for row in rows:
            if row["label"] != label:
                continue
            if isinstance(created, int):
                delta = abs(created / 1000.0 - row["epoch"])
            else:
                delta = 0.0
            if best is None or delta < best_delta:
                best = row
                best_delta = delta
        if best is not None:
            chat["parent"] = best.get("parent")
            chat["manifest_epoch"] = best["epoch"]

    labeled = {chat["label"] for chat in chats if chat.get("label")}
    unused = [row for row in rows if row["label"] not in labeled]
    cwds = {}
    for chat in chats:
        parent = chat.get("parent")
        if chat.get("label_from") == "transcript" and chat.get("cwd") and parent:
            cwds.setdefault(parent, set()).add(chat["cwd"])
    unlabeled = [
        chat
        for chat in chats
        if not chat.get("label") and isinstance(chat.get("created_ms"), int)
    ]
    used = set()
    for row in unused:
        known = cwds.get(row.get("parent")) or set()
        cands = []
        for chat in unlabeled:
            if id(chat) in used:
                continue
            if known and chat.get("cwd") not in known:
                continue
            delta = abs(chat["created_ms"] / 1000.0 - row["epoch"])
            if delta <= MANIFEST_WINDOW_S:
                cands.append((delta, chat["uuid"], chat))
        cands.sort()
        if not cands:
            continue
        if len(cands) >= 2 and cands[1][0] - cands[0][0] < MANIFEST_GAP_S:
            continue
        chat = cands[0][2]
        near = 0
        primary = cands[0][0]
        for other in unused:
            other_delta = abs(chat["created_ms"] / 1000.0 - other["epoch"])
            if other_delta <= MANIFEST_WINDOW_S and abs(other_delta - primary) < MANIFEST_GAP_S:
                near += 1
        if near > 1:
            continue
        chat["label"] = row["label"]
        chat["label_from"] = "manifest"
        chat["parent"] = row.get("parent")
        chat["manifest_epoch"] = row["epoch"]
        used.add(id(chat))


def _attach_pids(chats, live):
    groups = {}
    for chat in chats:
        label = chat.get("label")
        if label:
            groups.setdefault(label, []).append(chat)
    for label, group in groups.items():
        pid = live.get(label) if isinstance(live, dict) else None
        if not pid:
            continue
        group.sort(key=lambda chat: chat.get("updated_ms") or 0, reverse=True)
        try:
            group[0]["pid"] = int(pid)
        except (TypeError, ValueError):
            continue


def _upsert(store, chat, now, model):
    run_id = _run_id(chat)
    status = _status(chat, now)
    run = {
        "run_id": run_id,
        "source": "grok",
        "tier": "grok",
        "session_id": chat["uuid"],
        "status": status,
        "pid": chat.get("pid"),
        "ended_at": _ended(status, chat.get("updated_ms")),
    }
    if chat.get("label"):
        run["label"] = chat["label"]
    if model:
        run["model"] = model
    if chat.get("parent"):
        run["parent_session_id"] = chat["parent"]
    started, clock = _started(chat)
    if started is not None:
        run["started_at"] = started
        run["clock_source"] = clock
    out_path = chat.get("transcript")
    if out_path:
        run["out_path"] = out_path
        size = _file_size(out_path)
        if size:
            run["out_bytes"] = size
    try:
        store.upsert_run(run)
    except Exception:
        _fail("run")
        return
    session = {"session_id": chat["uuid"], "tool": TOOL, "status": status}
    if chat.get("cwd"):
        session["cwd"] = chat["cwd"]
    if run.get("label"):
        session["label"] = run["label"]
    if run.get("started_at") is not None:
        session["started_at"] = run["started_at"]
    if chat.get("parent"):
        session["parent_session_id"] = chat["parent"]
    try:
        store.upsert_session(session)
    except Exception:
        _fail("session")


def _run_id(chat):
    label = chat.get("label") or chat["uuid"]
    return "grok:%s:%s" % (chat["uuid"], label)


def _status(chat, now):
    if chat.get("pid"):
        return "running"
    updated = chat.get("updated_ms")
    if not isinstance(updated, int):
        return "unknown"
    if now - (updated / 1000.0) < RUNNING_WINDOW_S:
        return "running"
    return "done"


def _ended(status, updated_ms):
    if status != "done" or not isinstance(updated_ms, int):
        return None
    return updated_ms / 1000.0


def _started(chat):
    created = chat.get("created_ms")
    if isinstance(created, int):
        return created / 1000.0, "transcript"
    epoch = chat.get("manifest_epoch")
    if isinstance(epoch, (int, float)) and not isinstance(epoch, bool):
        return float(epoch), "manifest"
    return None, None


def _is_hot(chat, now):
    if chat.get("pid"):
        return True
    updated = chat.get("updated_ms")
    return isinstance(updated, int) and now - (updated / 1000.0) < RUNNING_WINDOW_S


def _db_fresh(store, chat):
    path = chat.get("store_path")
    if not path or not os.path.isfile(path):
        return True
    try:
        st = os.stat(path)
    except OSError:
        return True
    rec = _offset(store, _abs(path))
    if not rec or rec[0] != int(st.st_ino):
        return False
    try:
        return int(rec[1]) == _cursor_of(chat, st)
    except (TypeError, ValueError):
        return False


def _cursor_of(chat, st):
    updated = chat.get("updated_ms")
    if isinstance(updated, int):
        return updated
    return int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))


def _stamp_store(store, path, st, cursor):
    try:
        store.set_offset(_abs(path), int(st.st_ino), int(cursor), int(st.st_size), float(st.st_mtime))
    except Exception:
        _fail("offset")


def _mark_blob(store, store_key, blob_id):
    try:
        store.set_offset(_blob_key(store_key, blob_id), 1, 1, 0, 0.0)
    except Exception:
        _fail("blob-offset")


def _stamp(store, key, index, size, now):
    try:
        store.set_offset(key, 0, int(index), int(size), float(now))
    except Exception:
        _fail("rotate")


def _top_model(store_key):
    counts = _MODEL_COUNTS.get(store_key)
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _close_store(con, cleanup):
    try:
        con.close()
    except sqlite3.Error:
        pass
    if cleanup:
        shutil.rmtree(cleanup, ignore_errors=True)


def _manifest_dirs(root, filename, only):
    if not root or not os.path.isdir(root):
        return
    seen = set()

    def emit(pad):
        try:
            rp = os.path.realpath(pad)
        except OSError:
            return None
        if rp in seen:
            return None
        seen.add(rp)
        parent_name = os.path.basename(os.path.dirname(rp))
        parent = parent_name if _is_uuid(parent_name) else None
        return rp, parent

    direct = os.path.join(root, filename)
    if os.path.isfile(direct):
        item = emit(root)
        if item and (only is None or item[1] == only):
            yield item
    try:
        encoded = os.scandir(root)
    except OSError:
        return
    with encoded:
        for enc in encoded:
            try:
                if not enc.is_dir(follow_symlinks=False):
                    continue
                sessions = os.scandir(enc.path)
            except OSError:
                continue
            with sessions:
                for sess in sessions:
                    try:
                        if not sess.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if not _is_uuid(sess.name) or (only is not None and sess.name != only):
                        continue
                    tsv = os.path.join(sess.path, "scratchpad", filename)
                    if os.path.isfile(tsv):
                        item = emit(os.path.dirname(tsv))
                        if item:
                            yield item


def _parse_manifest(text):
    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = raw.rstrip("\n").split("\t")
        if len(parts) < 3:
            continue
        label = _clean_label(parts[0])
        if not label:
            continue
        try:
            epoch = float(parts[2].strip())
        except ValueError:
            continue
        rows.append({"label": label, "epoch": epoch})
    return rows


def _read_small(path, budget, per_file):
    try:
        st = os.stat(path)
    except OSError:
        return None, budget
    if st.st_size > per_file or st.st_size > budget or st.st_size > 1024 * 1024:
        return None, budget
    try:
        with open(path, "rb") as fh:
            data = fh.read(st.st_size)
    except OSError:
        _fail("manifest")
        return None, budget
    return data.decode("utf-8", "replace"), budget - len(data)


def _read_json(path, budget, per_file, limit=65536):
    try:
        st = os.stat(path)
    except OSError:
        return None, budget
    if st.st_size <= 0 or st.st_size > limit or st.st_size > per_file or st.st_size > budget:
        return None, budget
    try:
        with open(path, "rb") as fh:
            raw = fh.read(st.st_size)
    except OSError:
        return None, budget
    budget -= len(raw)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None, budget
    if not isinstance(obj, dict):
        return None, budget
    return obj, budget


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
    try:
        return max(1, int(per_file)), max(0, int(per_tick))
    except (TypeError, ValueError):
        return PER_FILE_DEFAULT, PER_TICK_DEFAULT


def _home(cfg):
    roots = cfg.get("roots") if isinstance(cfg.get("roots"), dict) else {}
    for key in ("cursor_home", "cursor", "cursor_chats"):
        val = roots.get(key) or cfg.get(key)
        if val:
            return os.path.expanduser(str(val))
    return DEFAULT_HOME


def _scratch_roots(cfg):
    from dash.locate import scratch_roots

    return scratch_roots(cfg)


def _scratch_root(cfg):
    found = _scratch_roots(cfg)
    return found[0] if found else ""


def _tmp_root(cfg):
    if cfg.get("tmp_dir"):
        return os.path.expanduser(str(cfg["tmp_dir"]))
    state = cfg.get("state_dir")
    if state:
        return os.path.join(os.path.expanduser(str(state)), "tmp")
    return DEFAULT_TMP


def _only_session(cfg):
    value = cfg.get("backfill_session")
    if value:
        return str(value)
    if cfg.get("mode") == "backfill" and cfg.get("session_id"):
        return str(cfg["session_id"])
    return None


def _allow_labels(cfg):
    raw = cfg.get("labels")
    if not raw:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return None
    allow = set()
    for item in raw:
        if isinstance(item, str):
            label = _clean_label(item)
            if label:
                allow.add(label)
    return allow or None


def _clean_label(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 200 or "\n" in text or "\r" in text:
        return None
    return text


def _is_uuid(value):
    if not isinstance(value, str):
        return False
    parts = value.split("-")
    if [len(part) for part in parts] != [8, 4, 4, 4, 12]:
        return False
    hexdigits = "0123456789abcdefABCDEF"
    return all(char in hexdigits for part in parts for char in part)


def _ms(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _file_size(path):
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
    except OSError:
        return 0
    return 0


def _abs(path):
    return os.path.realpath(path)


def _blob_key(store_key, blob_id):
    return store_key + "#b#" + blob_id


def _blob_ident(value):
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str) and HEX_ID.match(value):
        return value.lower()
    return None


def _as_bytes(value):
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8", "replace")
    return b""


def _is_assistant(prefix):
    return prefix.startswith(b'{"role":"assistant"') or prefix.startswith(b'{"role": "assistant"')


def _fail(where):
    global last_error
    last_error = where
    log.warning("%s failed", where)
