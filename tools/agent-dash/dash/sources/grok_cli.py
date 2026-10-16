"""GLM source for sessions launched through the grok CLI.

Manifest rows (``glm-sessions.tsv``: label, uuid, started epoch, model) become
runs ``glm:<uuid>:<label>``. Token rows prefer ``usage.json`` under
``~/.grok/sessions/<encoded-cwd>/<uuid>/``. ``inputTokens`` already includes
``cachedReadTokens``; both are stored raw. Per-turn objects are the additive
ledger (the session object is used only when ``turns`` is empty).

When ``usage.json`` is absent, the source tails
``~/.grok/logs/unified.jsonl`` for ``shell.turn.inference_done`` and the
matching ``sid``. ``prompt_tokens`` already includes ``cached_prompt_tokens``.
Once ``usage.json`` exists for a session that already contributed log rows,
those log rows are rewritten to zero so the turn totals are the only ledger.

``cfg["roots"]`` keys: ``grok_sessions``, ``grok_log`` (or ``unified_log``),
``scratchpads`` (one path, a list, or a glob; default none). Budgets come from ``read_budgets`` or
``read_budget`` (``per_file``, ``per_tick``), default 4 MB and 8 MB.
``backfill_session`` limits manifest discovery to that Claude session.

``usage.json`` is rewritten in place. The offset stored for
``grok_cli:usage:<path>`` is ``st_mtime_ns``, not a byte cursor. Other
synthetic offset keys (``grok_cli:seen:*``, ``grok_cli:auth:*``,
``grok_cli:sup:*``) are bookkeeping, not files.
"""

import json
import logging
import os
import zlib
from datetime import datetime

from dash.sources.process import apply_liveness, live_agent_processes

log = logging.getLogger("agent_dash.grok_cli")

NAME = "grok_cli"
INTERVAL_S = 2

DEFAULT_SESSIONS = os.path.expanduser("~/.grok/sessions")
DEFAULT_LOG = os.path.expanduser("~/.grok/logs/unified.jsonl")
PER_FILE_DEFAULT = 4 * 1024 * 1024
PER_TICK_DEFAULT = 8 * 1024 * 1024

TOOL = "glm"
last_error = None


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
    sessions = _root(cfg, "grok_sessions", "glm_sessions", default=DEFAULT_SESSIONS)
    log_path = _root(cfg, "grok_log", "unified_log", "unified", default=DEFAULT_LOG)
    only = _only_session(cfg)
    processes = _agent_processes()

    manifests = {}
    for scratch in _scratch_roots(cfg):
        for pad, parent in _manifest_dirs(scratch, "glm-sessions.tsv", only):
            path = os.path.join(pad, "glm-sessions.tsv")
            text, budget = _read_small(path, budget, per_file)
            if text is None:
                continue
            for row in _parse_manifest(text):
                row["parent_session_id"] = parent
                _keep_latest(manifests, row)

    for row in manifests.values():
        _upsert_run(store, row, None, None, processes)

    index = _session_index(sessions)
    authoritative = set()
    for sid, row in manifests.items():
        sdir = index.get(sid)
        if not sdir:
            continue
        usage_path = os.path.join(sdir, "usage.json")
        if not os.path.isfile(usage_path):
            continue
        ok, budget = _apply_usage(store, usage_path, row, budget, per_file, now, processes)
        if ok:
            authoritative.add(sid)

    pending = [
        sid
        for sid in authoritative
        if _marked(store, "grok_cli:seen:" + sid) and not _marked(store, "grok_cli:auth:" + sid)
    ]
    if pending and os.path.isfile(log_path) and budget > 0:
        budget = _supersede(store, log_path, pending, manifests, budget, per_file, now)

    if os.path.isfile(log_path) and budget > 0 and manifests:
        _tail_log(store, log_path, manifests, authoritative, budget, per_file, now, processes)


def _apply_usage(store, path, row, budget, per_file, now, processes):
    """Return ``(authoritative, budget)``. Unchanged files are not re-emitted."""
    try:
        st = os.stat(path)
    except OSError:
        return False, budget
    key = "grok_cli:usage:" + path
    rec = _offset(store, key)
    mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
    if rec and rec[0] == st.st_ino and rec[1] == mtime_ns:
        return True, budget
    if st.st_size > per_file or st.st_size > budget:
        return False, budget
    try:
        with open(path, "rb") as fh:
            blob = fh.read(st.st_size)
    except OSError:
        _fail("usage.json")
        return False, budget
    budget -= len(blob)
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return False, budget
    if not isinstance(data, dict):
        return False, budget
    if "session" not in data and "turns" not in data:
        return False, budget
    model = row.get("model") or None
    for ev in _usage_events(data):
        if ev["model"]:
            model = ev["model"]
        _add_usage(
            store,
            {
                "ts": ev["ts"] if ev["ts"] is not None else now,
                "session_id": row["session_id"],
                "run_id": _run_id(row),
                "model": ev["model"] or row.get("model") or None,
                "tool": TOOL,
                "message_id": ev["message_id"],
                "input": ev["input"],
                "output": ev["output"],
                "cache_read": ev["cache_read"],
                "cache_write": ev["cache_write"],
                "reasoning": ev["reasoning"],
            },
        )
    _upsert_run(store, row, model, None, processes)
    try:
        store.set_offset(key, st.st_ino, mtime_ns, st.st_size, st.st_mtime)
    except Exception:
        _fail("usage-stamp")
    return True, budget


def _usage_events(data):
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    fallback = session.get("primaryModelId") if isinstance(session.get("primaryModelId"), str) else None
    updated = _epoch_any(data.get("updatedAt"))
    turns = data.get("turns") if isinstance(data.get("turns"), list) else []
    chosen = []
    if turns:
        for i, turn in enumerate(turns):
            if not isinstance(turn, dict):
                continue
            number = turn.get("turnNumber")
            mid = "turn:%s" % (number if number is not None else i)
            chosen.append((mid, turn, _epoch_any(turn.get("endedAt")) or updated))
    elif session:
        chosen.append(("session", session, updated))
    events = []
    for mid, turn, ts in chosen:
        model = turn.get("primaryModelId") if isinstance(turn.get("primaryModelId"), str) else fallback
        events.append(
            {
                "message_id": mid,
                "ts": ts,
                "model": model,
                "input": _num(turn.get("inputTokens")),
                "output": _num(turn.get("outputTokens")),
                "cache_read": _num(turn.get("cachedReadTokens")),
                "cache_write": _num(turn.get("cacheCreationTokens")),
                "reasoning": _num(turn.get("reasoningTokens")),
            }
        )
    return events


def _supersede(store, log_path, pending, manifests, budget, per_file, now):
    fp = zlib.adler32(",".join(sorted(pending)).encode("utf-8")) & 0xFFFFFFFF
    key = "grok_cli:sup:%s:%s" % (fp, log_path)
    try:
        raw, budget, _started = _read_chunk(
            store, log_path, key, budget, per_file, finish_partial=True
        )
    except Exception:
        _fail("supersede")
        return budget
    for ev in _iter_inference(raw):
        if ev["session_id"] not in pending:
            continue
        row = manifests.get(ev["session_id"])
        if row is None:
            continue
        _add_usage(
            store,
            {
                "ts": ev["ts"] if ev["ts"] is not None else now,
                "session_id": ev["session_id"],
                "run_id": _run_id(row),
                "model": row.get("model") or None,
                "tool": TOOL,
                "message_id": ev["message_id"],
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 0,
            },
        )
    try:
        st = os.stat(log_path)
        rec = _offset(store, key)
        if rec and rec[0] == st.st_ino and rec[1] >= st.st_size:
            for sid in pending:
                store.set_offset("grok_cli:auth:" + sid, 1, 1, 1, now)
    except Exception:
        _fail("supersede-stamp")
    return budget


def _tail_log(store, log_path, manifests, authoritative, budget, per_file, now, processes):
    try:
        raw, budget, _started = _read_chunk(store, log_path, log_path, budget, per_file)
    except Exception:
        _fail("unified")
        return budget
    if not raw:
        return budget
    for ev in _iter_inference(raw):
        sid = ev["session_id"]
        row = manifests.get(sid)
        if row is None or sid in authoritative:
            continue
        _add_usage(
            store,
            {
                "ts": ev["ts"] if ev["ts"] is not None else now,
                "session_id": sid,
                "run_id": _run_id(row),
                "model": row.get("model") or None,
                "tool": TOOL,
                "message_id": ev["message_id"],
                "input": ev["input"],
                "output": ev["output"],
                "cache_read": ev["cache_read"],
                "cache_write": ev["cache_write"],
                "reasoning": ev["reasoning"],
            },
        )
        try:
            store.set_offset("grok_cli:seen:" + sid, 1, 1, 1, now)
        except Exception:
            _fail("seen")
        if ev["pid"]:
            _upsert_run(store, row, row.get("model") or None, ev["pid"], processes)
    return budget


def _iter_inference(raw):
    for line in raw.split(b"\n"):
        if b"shell.turn.inference_done" not in line:
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue
        ev = _inference_event(obj)
        if ev is not None:
            yield ev


def _inference_event(obj):
    if not isinstance(obj, dict) or obj.get("msg") != "shell.turn.inference_done":
        return None
    sid = obj.get("sid")
    if not isinstance(sid, str) or not sid:
        return None
    ctx = obj.get("ctx") if isinstance(obj.get("ctx"), dict) else {}
    loop = ctx.get("loop_index")
    ts_text = obj.get("ts") if isinstance(obj.get("ts"), str) else ""
    pid = obj.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        pid = None
    return {
        "session_id": sid,
        "message_id": "inference:%s:%s" % (ts_text, "" if loop is None else loop),
        "ts": _epoch_any(obj.get("ts")),
        "input": _num(ctx.get("prompt_tokens")),
        "output": _num(ctx.get("completion_tokens")),
        "cache_read": _num(ctx.get("cached_prompt_tokens")),
        "cache_write": _num(ctx.get("cache_creation_tokens")),
        "reasoning": _num(ctx.get("reasoning_tokens")),
        "pid": pid,
    }


def _add_usage(store, row):
    try:
        store.add_usage(row)
    except Exception:
        _fail("usage")


def _upsert_run(store, row, model, pid, processes=None):
    sid = row["session_id"]
    run = {
        "run_id": _run_id(row),
        "source": "glm",
        "tier": "glm",
        "session_id": sid,
    }
    if row.get("label"):
        run["label"] = row["label"]
    chosen = model or row.get("model") or None
    if chosen:
        run["model"] = chosen
    parent = row.get("parent_session_id")
    if parent:
        run["parent_session_id"] = parent
    if row.get("started_at") is not None:
        run["started_at"] = row["started_at"]
        run["clock_source"] = "manifest"
    if pid:
        run["pid"] = pid
    apply_liveness(store, run, "glm", processes)
    try:
        store.upsert_run(run)
    except Exception:
        _fail("run")
        return
    session = {"session_id": sid, "tool": TOOL}
    if run.get("started_at") is not None:
        session["started_at"] = run["started_at"]
    if parent:
        session["parent_session_id"] = parent
    if run.get("label"):
        session["label"] = run["label"]
    try:
        store.upsert_session(session)
    except Exception:
        _fail("session")


def _agent_processes():
    """One process listing per poll. None means the listing failed."""
    try:
        return live_agent_processes()
    except Exception:
        return None


def _run_id(row):
    return "glm:%s:%s" % (row["session_id"], _label(row["session_id"], row))


def _label(sid, row):
    label = (row.get("label") or "").strip()
    return label or sid


def _parse_manifest(text):
    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = raw.rstrip("\n").split("\t")
        if len(parts) < 2:
            continue
        label = parts[0].strip()
        sid = parts[1].strip()
        if not _is_uuid(sid):
            continue
        started = None
        if len(parts) > 2 and parts[2].strip():
            try:
                started = float(parts[2].strip())
            except ValueError:
                started = None
        model = parts[3].strip() if len(parts) > 3 else ""
        rows.append(
            {"label": label, "session_id": sid, "started_at": started, "model": model or None}
        )
    return rows


def _keep_latest(manifests, row):
    prev = manifests.get(row["session_id"])
    if prev is None or (row.get("started_at") or -1) >= (prev.get("started_at") or -1):
        manifests[row["session_id"]] = row


def _session_index(root):
    """Map uuid -> session directory. Lists encoded-cwd and uuid names only."""
    found = {}
    if not root or not os.path.isdir(root):
        return found
    try:
        encoded = os.scandir(root)
    except OSError:
        return found
    with encoded:
        for enc in encoded:
            try:
                if not enc.is_dir(follow_symlinks=False):
                    continue
                kids = os.scandir(enc.path)
            except OSError:
                continue
            with kids:
                for kid in kids:
                    try:
                        if kid.is_dir(follow_symlinks=False) and kid.name not in found:
                            found[kid.name] = kid.path
                    except OSError:
                        continue
    return found


def _marked(store, key):
    return _offset(store, key) is not None


def _manifest_dirs(root, filename, only=None):
    """Scratchpads that contain ``filename``.

    Lists ``<root>/<encoded-cwd>/<session>/`` and, when ``root`` is already
    the encoded-cwd directory, a uuid child that holds ``scratchpad/<filename>``.
    """
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

    def emit_session(sess_dir, sess_name):
        if not _is_uuid(sess_name) or (only is not None and sess_name != only):
            return
        tsv = os.path.join(sess_dir, "scratchpad", filename)
        if os.path.isfile(tsv):
            item = emit(os.path.dirname(tsv))
            if item:
                yield item

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
            except OSError:
                continue
            yield from emit_session(enc.path, enc.name)
            try:
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
                    yield from emit_session(sess.path, sess.name)


def _read_small(path, budget, per_file):
    try:
        st = os.stat(path)
    except OSError:
        return None, budget
    if st.st_size > per_file or st.st_size > budget:
        return None, budget
    try:
        with open(path, "rb") as fh:
            data = fh.read(st.st_size)
    except OSError:
        _fail("manifest")
        return None, budget
    return data.decode("utf-8", "replace"), budget - len(data)


def _read_chunk(store, path, key, budget, per_file, finish_partial=False):
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
    at_eof = end >= st.st_size
    if disk.endswith(b"\n") or (finish_partial and at_eof):
        kept = disk
        new_off = end
    else:
        cut = disk.rfind(b"\n")
        if cut < 0:
            if charge >= per_file and not at_eof:
                store.set_offset(key, st.st_ino, end, st.st_size, st.st_mtime)
                return b"", budget - charge, False
            return b"", budget - charge, offset == 0
        kept = disk[: cut + 1]
        new_off = offset + len(kept)
    store.set_offset(key, st.st_ino, new_off, st.st_size, st.st_mtime)
    return kept, budget - charge, offset == 0


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
    return max(1, int(per_file)), max(0, int(per_tick))


def _scratch_roots(cfg):
    from dash.locate import scratch_roots

    return scratch_roots(cfg)


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


def _is_uuid(value):
    if not isinstance(value, str):
        return False
    parts = value.split("-")
    if [len(p) for p in parts] != [8, 4, 4, 4, 12]:
        return False
    hexdigits = "0123456789abcdefABCDEF"
    return all(c in hexdigits for part in parts for c in part)


def _fail(where):
    global last_error
    last_error = where
    log.warning("%s failed", where)
