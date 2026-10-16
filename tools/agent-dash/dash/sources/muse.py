"""Muse session source.

Tails ``~/.local/share/muse/sessions/YYYY/MM/DD/<uuid>/session.jsonl`` and
the ``subagent/`` children of those logs. Token rows come from
``model_completed`` usage records: input, output, cache write, and reasoning
are taken as stored, and cache read is ``max(cached_tokens, cache_read_tokens)``.

Run ids are ``muse:<session_uuid>:<label>``. The label comes from
``muse-sessions.tsv`` (``label``, session uuid, started epoch). When no
manifest row exists, the label slot is the session uuid and ``session_id``
on the run and on each usage row is that same uuid.

``cfg["roots"]["muse_sessions"]`` overrides the session root. Scratchpads come
from ``roots.scratchpads`` (one path, a list, or a glob). The default is no
scratch root. Budgets come from
``read_budgets`` or ``read_budget`` (``per_file``, ``per_tick``), default 4 MB
and 8 MB. ``fresh_hours`` (default 36, ``0`` skips mtime discovery) and
``dir_scan_s`` (default 15) bound directory scans. ``backfill_session`` limits
manifest discovery to that Claude session and skips the fresh scan.
"""

import glob
import json
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger("agent_dash.muse")

NAME = "muse"
INTERVAL_S = 2

DEFAULT_SESSIONS = os.path.expanduser("~/.local/share/muse/sessions")
PER_FILE_DEFAULT = 4 * 1024 * 1024
PER_TICK_DEFAULT = 8 * 1024 * 1024

_FRESH = {}
_LOCATE = {}
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
    sessions = _root(cfg, "muse_sessions", "muse", default=DEFAULT_SESSIONS)
    fresh_hours = float(cfg.get("fresh_hours", 36))
    scan_s = float(cfg.get("dir_scan_s", 15))
    only = _only_session(cfg)
    if only:
        fresh_hours = 0

    manifests = {}
    for scratch in _scratch_roots(cfg):
        for pad, parent in _manifest_dirs(scratch, "muse-sessions.tsv", only):
            path = os.path.join(pad, "muse-sessions.tsv")
            text, budget = _read_small(path, budget, per_file)
            if text is None:
                continue
            for row in _parse_manifest(text):
                row["parent_session_id"] = parent
                _keep_latest(manifests, row)

    for sid in manifests:
        _upsert_run(store, sid, manifests, None, None, False, None)

    files = _candidate_files(store, sessions, manifests, now, fresh_hours, scan_s)
    if not files or budget <= 0:
        return

    rec = _offset(store, "muse:rotate")
    idx = int(rec[1]) if rec else 0
    n = len(files)
    idx %= n
    scanned = 0
    while scanned < n and budget > 0:
        path, sid, muse_parent = files[idx]
        budget = _tail_file(
            store, path, sid, manifests, muse_parent, budget, per_file, now
        )
        idx = (idx + 1) % n
        scanned += 1
    try:
        store.set_offset("muse:rotate", 0, idx, n, now)
    except Exception:
        _fail("rotate")


def _candidate_files(store, root, manifests, now, fresh_hours, scan_s):
    """Manifest logs first, then recently touched logs, then one subagent level."""
    ordered = []
    seen = set()

    def add(path, sid, muse_parent):
        if not path:
            return
        path = os.path.abspath(path)
        if path in seen or not os.path.isfile(path):
            return
        seen.add(path)
        ordered.append((path, sid, muse_parent))

    for sid in manifests:
        add(_locate(root, sid), sid, None)
    for sid, path in _fresh(store, root, now, fresh_hours, scan_s):
        add(path, sid, None)

    expanded = []
    for path, sid, muse_parent in ordered:
        expanded.append((path, sid, muse_parent))
        for cpath, cid in _children(os.path.dirname(path), root):
            cpath = os.path.abspath(cpath)
            if cpath in seen or not os.path.isfile(cpath):
                continue
            seen.add(cpath)
            expanded.append((cpath, cid, sid))
    return expanded


def _tail_file(store, path, sid, manifests, muse_parent, budget, per_file, now):
    try:
        raw, budget, started_at_zero = _read_chunk(store, path, path, budget, per_file)
    except Exception:
        _fail("read")
        return budget
    first_ts = None
    models = {}
    if raw:
        for line in raw.split(b"\n"):
            if not line.strip():
                continue
            try:
                ev = _parse_line(line, sid)
            except Exception:
                _fail("parse")
                continue
            if ev is None:
                continue
            if first_ts is None and ev["ts"] is not None:
                first_ts = ev["ts"]
            esid = ev["session_id"]
            if ev["model"]:
                models[esid] = ev["model"]
            elif esid not in models:
                models[esid] = None
            try:
                store.add_usage(
                    {
                        "ts": ev["ts"] if ev["ts"] is not None else now,
                        "session_id": esid,
                        "run_id": _run_id(esid, manifests),
                        "model": ev["model"],
                        "tool": "muse",
                        "message_id": ev["message_id"],
                        "input": ev["input"],
                        "output": ev["output"],
                        "cache_read": ev["cache_read"],
                        "cache_write": ev["cache_write"],
                        "reasoning": ev["reasoning"],
                    }
                )
            except Exception:
                _fail("usage")
    if not models and started_at_zero:
        models[sid] = None
    for esid, model in models.items():
        _upsert_run(
            store,
            esid,
            manifests,
            muse_parent,
            model,
            started_at_zero and first_ts is not None,
            first_ts,
        )
    return budget


def _parse_line(line, fallback_sid):
    if b"model_completed" not in line:
        return None
    try:
        obj = json.loads(line.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    ev = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    if ev.get("kind") != "model_completed":
        return None
    usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else None
    if usage is None:
        return None
    stream = obj.get("stream") if isinstance(obj.get("stream"), dict) else {}
    sid = stream.get("id") if isinstance(stream.get("id"), str) and stream.get("id") else fallback_sid
    mid = obj.get("id") if isinstance(obj.get("id"), str) and obj.get("id") else None
    if mid is None and obj.get("sequence") is not None:
        mid = "seq:%s" % obj.get("sequence")
    if not mid or not sid:
        return None
    cached = _num(usage.get("cached_tokens"))
    cache_read = _num(usage.get("cache_read_tokens"))
    model = ev.get("model") if isinstance(ev.get("model"), str) else None
    return {
        "session_id": sid,
        "message_id": mid,
        "ts": _epoch_num(obj.get("recorded_at")),
        "model": model,
        "input": _num(usage.get("input_tokens")),
        "output": _num(usage.get("output_tokens")),
        "cache_read": cached if cached >= cache_read else cache_read,
        "cache_write": _num(usage.get("cache_write_tokens")),
        "reasoning": _num(usage.get("reasoning_tokens")),
    }


def _upsert_run(store, sid, manifests, muse_parent, model, have_log_start, log_ts):
    row = manifests.get(sid)
    label = _label(sid, row)
    parent = muse_parent or (row or {}).get("parent_session_id")
    run = {
        "run_id": "muse:%s:%s" % (sid, label),
        "source": "muse",
        "tier": "muse",
        "session_id": sid,
    }
    if row and row.get("label"):
        run["label"] = row["label"]
    if model:
        run["model"] = model
    if parent:
        run["parent_session_id"] = parent
    if row and row.get("started_at") is not None:
        run["started_at"] = row["started_at"]
        run["clock_source"] = "manifest"
    elif have_log_start and log_ts is not None:
        run["started_at"] = log_ts
        run["clock_source"] = "muse_log"
    try:
        store.upsert_run(run)
    except Exception:
        _fail("run")
        return
    session = {"session_id": sid, "tool": "muse"}
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


def _run_id(sid, manifests):
    return "muse:%s:%s" % (sid, _label(sid, manifests.get(sid)))


def _label(sid, row):
    if row:
        label = (row.get("label") or "").strip()
        if label:
            return label
    return sid


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
        rows.append({"label": label, "session_id": sid, "started_at": started})
    return rows


def _keep_latest(manifests, row):
    prev = manifests.get(row["session_id"])
    if prev is None or (row.get("started_at") or -1) >= (prev.get("started_at") or -1):
        manifests[row["session_id"]] = row


def _fresh(store, root, now, hours, scan_s):
    if hours <= 0 or not root or not os.path.isdir(root):
        return []
    cached = _FRESH.get(root)
    due = cached is None or _scan_due(store, "muse:fresh-scan", now, scan_s)
    if not due:
        return cached or []
    cutoff = now - hours * 3600
    found = []
    for day in _recent_days(root, now):
        try:
            names = os.listdir(day)
        except OSError:
            continue
        for sid in names:
            path = os.path.join(day, sid, "session.jsonl")
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime >= cutoff:
                found.append((sid, path))
    _FRESH[root] = found
    try:
        store.set_offset("muse:fresh-scan", 0, int(now), len(found), now)
    except Exception:
        _fail("fresh-scan")
    return found


def _scan_due(store, key, now, interval):
    if interval <= 0:
        return True
    rec = _offset(store, key)
    if rec is None:
        return True
    try:
        last = float(rec[1])
    except (TypeError, ValueError):
        return True
    return now - last >= interval


def _recent_days(root, now):
    days = []
    base = datetime.fromtimestamp(now, timezone.utc)
    for back in range(3):
        dt = base - timedelta(days=back)
        days.append(os.path.join(root, "%04d" % dt.year, "%02d" % dt.month, "%02d" % dt.day))
    return days


def _locate(root, sid):
    if not root or not sid:
        return None
    key = (root, sid)
    hit = _LOCATE.get(key)
    if hit and os.path.isfile(hit):
        return hit
    pattern = os.path.join(root, "*", "*", "*", sid, "session.jsonl")
    try:
        hits = glob.glob(pattern)
    except OSError:
        return None
    if not hits:
        _LOCATE.pop(key, None)
        return None
    hits.sort(key=lambda p: os.path.getmtime(p) if os.path.isfile(p) else 0, reverse=True)
    _LOCATE[key] = hits[0]
    return hits[0]


def _children(session_dir, root):
    sub = os.path.join(session_dir, "subagent")
    if not os.path.isdir(sub):
        return
    try:
        names = os.listdir(sub)
    except OSError:
        return
    for cid in names:
        direct = os.path.join(sub, cid, "session.jsonl")
        if os.path.isfile(direct):
            yield direct, cid
            continue
        alt = _locate(root, cid)
        if alt:
            yield alt, cid


def _manifest_dirs(root, filename, only=None):
    """Scratchpads that contain ``filename``.

    Lists ``<root>/<encoded-cwd>/<session>/`` only. Never lists the inside of
    a scratchpad (those trees hold worktrees and database files).
    """
    if not root or not os.path.isdir(root):
        return
    seen = set()

    def emit(pad):
        try:
            rp = os.path.realpath(pad)
        except OSError:
            return
        if rp in seen:
            return
        seen.add(rp)
        parent_name = os.path.basename(os.path.dirname(rp))
        parent = parent_name if _is_uuid(parent_name) else None
        yield_pad = (rp, parent)
        return yield_pad

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


def _read_chunk(store, path, key, budget, per_file):
    """Return ``(bytes, budget, started_at_zero)``.

    Stops on a newline so a partial trailing line stays for the next tick.
    A line that fills ``per_file`` with no newline is skipped so the cursor
    cannot stick. The byte budget counts bytes read from disk.
    """
    st = os.stat(path)
    rec = _offset(store, key)
    if rec and rec[0] == st.st_ino and 0 <= rec[1] <= st.st_size:
        offset = int(rec[1])
    else:
        offset = 0
    if offset >= st.st_size or budget <= 0:
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
    if disk.endswith(b"\n"):
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


def _scratch_root(cfg):
    found = _scratch_roots(cfg)
    return found[0] if found else ""


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


def _epoch_num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value > 10**14:
        return float(value) / 1_000_000.0
    if value > 10**12:
        return float(value) / 1_000.0
    return float(value)


def _is_uuid(value):
    if not isinstance(value, str):
        return False
    parts = value.split("-")
    if [len(p) for p in parts] != [8, 4, 4, 4, 12]:
        return False
    hexdigits = "0123456789abcdefABCDEF"
    return all(c in hexdigits for p in parts for c in p)


def _fail(where):
    global last_error
    last_error = where
    log.warning("%s failed", where)
