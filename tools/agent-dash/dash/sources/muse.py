"""Muse session source.

Tails ``~/.local/share/muse/sessions/YYYY/MM/DD/<uuid>/session.jsonl`` and
the ``subagent/`` children of those logs. Token rows come from
``model_completed`` usage records: input, output, cache write, and reasoning
are taken as stored, and cache read is ``max(cached_tokens, cache_read_tokens)``.

Run ids are ``muse:<session_uuid>:<label>``. The label comes from
``muse-sessions.tsv`` (``label``, session uuid, started epoch), including a
file nested one level under the scratchpad such as ``runs/muse-sessions.tsv``.
The run's ``started_at`` is that epoch. When no manifest row exists, it is
the timestamp of the first event in ``session.jsonl``. The label slot is
then the session uuid, and ``session_id`` on the run and on each usage row
is that same uuid. A later row for the same session may fill a blank label.
It does not replace a known start with an empty one.

``cfg["roots"]["muse_sessions"]`` overrides the session root. Scratchpads come
from ``roots.scratchpads`` (one path, a list, or a glob). The default is no
scratch root. Budgets come from
``read_budgets`` or ``read_budget`` (``per_file``, ``per_tick``), default 4 MB
and 8 MB. Each tick reads manifest-known logs and logs modified within
``recent_s`` (default 15 minutes) before rotating through the rest.
``fresh_hours`` (default 36, ``0`` skips mtime discovery) and
``dir_scan_s`` (default 15) bound directory scans. ``backfill_session`` limits
manifest discovery to that Claude session and skips the fresh scan.
A zero-byte read does not create a run. When a labeled run exists for a
session, usage stored on the unlabeled ``muse:<sid>:<sid>`` row moves onto
the labeled run and that row is deleted.
"""

import glob
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from dash.sources.process import apply_liveness, live_agent_processes

log = logging.getLogger("agent_dash.muse")

NAME = "muse"
INTERVAL_S = 2

DEFAULT_SESSIONS = os.path.expanduser("~/.local/share/muse/sessions")
PER_FILE_DEFAULT = 4 * 1024 * 1024
PER_TICK_DEFAULT = 8 * 1024 * 1024
RECENT_S = 15 * 60

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
    processes = _agent_processes()

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

    for row in manifests.values():
        sid = row.get("session_id")
        path = _locate(sessions, sid)
        if not path and isinstance(sid, str):
            path = _locate(sessions, sid.lower())
        _upsert_run(store, sid, manifests, None, None, False, None, processes, path)
    _adopt_live_labels(store, manifests, processes)

    files = _candidate_files(store, sessions, manifests, now, fresh_hours, scan_s)
    recent_s = _recent_s(cfg)
    if files and budget > 0:
        hot, cold = _split_priority(files, manifests, now, recent_s)
        budget = _drain_files(
            store, hot, "muse:rotate-hot", budget, per_file, now, processes, manifests
        )
        if budget > 0:
            _drain_files(
                store, cold, "muse:rotate", budget, per_file, now, processes, manifests
            )
    _repair(store)


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

    for row in manifests.values():
        sid = row.get("session_id")
        path = _locate(root, sid)
        if not path and isinstance(sid, str):
            path = _locate(root, sid.lower())
        add(path, sid, None)
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


def _recent_s(cfg):
    try:
        return float(cfg.get("recent_s", RECENT_S))
    except (TypeError, ValueError):
        return RECENT_S


def _split_priority(files, manifests, now, recent_s):
    """Manifest-known and recently modified logs, then everything else."""
    known = set()
    for key in manifests:
        if isinstance(key, str):
            known.add(key.lower())
    hot = []
    cold = []
    for path, sid, muse_parent in files:
        sid_l = sid.lower() if isinstance(sid, str) else ""
        parent_l = muse_parent.lower() if isinstance(muse_parent, str) else ""
        recent = False
        if recent_s > 0:
            try:
                recent = now - os.path.getmtime(path) <= recent_s
            except OSError:
                recent = False
        if sid_l in known or parent_l in known or recent:
            hot.append((path, sid, muse_parent))
        else:
            cold.append((path, sid, muse_parent))
    return hot, cold


def _drain_files(store, files, key, budget, per_file, now, processes, manifests):
    if not files or budget <= 0:
        return budget
    n = len(files)
    rec = _offset(store, key)
    idx = int(rec[1]) % n if rec else 0
    scanned = 0
    while scanned < n and budget > 0:
        path, sid, muse_parent = files[idx]
        budget = _tail_file(
            store, path, sid, manifests, muse_parent, budget, per_file, now, processes
        )
        idx = (idx + 1) % n
        scanned += 1
    try:
        store.set_offset(key, 0, idx, n, now)
    except Exception:
        _fail("rotate")
    return budget


def _repair(store):
    for name, where in (("repair_muse_orphans", "orphan"), ("repair_muse_clocks", "clock")):
        fn = getattr(store, name, None)
        if not callable(fn):
            continue
        try:
            fn()
        except Exception:
            _fail(where)


def _tail_file(store, path, sid, manifests, muse_parent, budget, per_file, now, processes):
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
    if not models and started_at_zero and raw:
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
            processes,
            path,
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


def _upsert_run(store, sid, manifests, muse_parent, model, have_log_start, log_ts, processes=None, log_path=None):
    row = _manifest_row(manifests, sid)
    ident = row["session_id"] if row and row.get("session_id") else sid
    label = _label(ident, row)
    parent = muse_parent or (row or {}).get("parent_session_id")
    run_id = "muse:%s:%s" % (ident, label)
    run = {
        "run_id": run_id,
        "source": "muse",
        "tier": "muse",
        "session_id": ident,
    }
    if row and row.get("label"):
        run["label"] = row["label"]
    if model:
        run["model"] = model
    if parent:
        run["parent_session_id"] = parent
    start, clock = _start_clock(store, run_id, row, have_log_start, log_ts, log_path)
    if start is not None:
        run["started_at"] = start
        run["clock_source"] = clock
    apply_liveness(store, run, "muse", processes)
    try:
        store.upsert_run(run)
    except Exception:
        _fail("run")
        return
    if run.get("label") and run["label"] != ident:
        _retire_shadow(store, ident, run["label"])
    session = {"session_id": ident, "tool": "muse"}
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


def _adopt_live_labels(store, manifests, processes):
    """Label a live muse process from its prompt file when the tsv has no row yet.

    The workstream comes from that label. The unlabeled session row is retired
    onto ``muse:<session>:<label>`` the same way a manifest row would.
    """
    if not processes:
        return
    from dash.sources.process import SOURCE_COMMS

    for proc in processes:
        if proc.get("comm") not in SOURCE_COMMS["muse"]:
            continue
        label = proc.get("prompt_label")
        sid = proc.get("session_id")
        if not isinstance(label, str) or not isinstance(sid, str):
            continue
        if _manifest_row(manifests, sid):
            continue
        _keep_latest(manifests, {"label": label, "session_id": sid, "started_at": None})
        _upsert_run(store, sid, manifests, None, None, False, None, processes)


def _agent_processes():
    """One process listing per poll. None means the listing failed."""
    try:
        return live_agent_processes()
    except Exception:
        return None


def _retire_shadow(store, sid, label):
    """Close the unlabeled ``muse:<sid>:<sid>`` row once this session has a label.

    ``parent_session_id`` is the labeled run, so the shadow is not a second
    agent and tokens stored on it roll up to that run. The session uuid is
    matched in either case: muse logs and ``--session-id`` do not agree on case.
    """
    if not label or not isinstance(sid, str) or label == sid:
        return
    labeled = "muse:%s:%s" % (sid, label)
    forms = []
    for form in (sid, sid.lower()):
        shadow = "muse:%s:%s" % (form, form)
        if shadow != labeled and shadow not in forms:
            forms.append(shadow)
    getter = getattr(store, "get_run", None)
    if not callable(getter):
        return
    for shadow in forms:
        try:
            existing = getter(shadow)
        except Exception:
            return
        if not existing:
            continue
        ended = existing.get("ended_at")
        if ended is None:
            ended = existing.get("started_at")
        try:
            store.upsert_run(
                {
                    "run_id": shadow,
                    "source": "muse",
                    "tier": "muse",
                    "session_id": existing.get("session_id") or sid,
                    "status": "unknown",
                    "ended_at": ended,
                    "parent_session_id": labeled,
                    "pid": None,
                }
            )
        except Exception:
            _fail("shadow")


def _manifest_row(manifests, sid):
    if not isinstance(sid, str):
        return None
    row = manifests.get(sid)
    if row:
        return row
    return manifests.get(sid.lower())


def _run_id(sid, manifests):
    row = _manifest_row(manifests, sid)
    ident = row["session_id"] if row and row.get("session_id") else sid
    return "muse:%s:%s" % (ident, _label(ident, row))


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
    """Keep one manifest row per session.

    A later row may replace the label. It must not replace a known start
    epoch with an empty one. When both epochs are set, the later one wins.
    """
    key = row["session_id"].lower() if isinstance(row.get("session_id"), str) else row.get("session_id")
    prev = manifests.get(key)
    if prev is None:
        manifests[key] = dict(row)
        return
    new_ts = row.get("started_at")
    old_ts = prev.get("started_at")
    if new_ts is None and old_ts is not None:
        merged = dict(prev)
        _fill_blank(merged, row, ("label", "parent_session_id"))
        manifests[key] = merged
        return
    if old_ts is None and new_ts is not None:
        merged = dict(row)
        _fill_blank(merged, prev, ("label", "parent_session_id"))
        manifests[key] = merged
        return
    if (new_ts or -1) >= (old_ts or -1):
        merged = dict(row)
        if merged.get("started_at") is None and old_ts is not None:
            merged["started_at"] = old_ts
        _fill_blank(merged, prev, ("label", "parent_session_id"))
        manifests[key] = merged
        return
    manifests[key] = prev


def _fill_blank(dest, src, keys):
    for key in keys:
        if not dest.get(key) and src.get(key):
            dest[key] = src[key]


def _start_clock(store, run_id, row, have_log_start, log_ts, log_path):
    """Manifest epoch, else the first session.jsonl event, else the chunk."""
    if row and row.get("started_at") is not None:
        return row["started_at"], "manifest"
    if _stored_start(store, run_id) is not None:
        return None, None
    if log_path:
        first = _file_first_ts(log_path)
        if first is not None:
            return first, "muse_log"
    if have_log_start and log_ts is not None:
        return log_ts, "muse_log"
    return None, None


def _stored_start(store, run_id):
    getter = getattr(store, "get_run", None)
    if not callable(getter):
        return None
    try:
        prev = getter(run_id)
    except Exception:
        return None
    if not prev:
        return None
    return prev.get("started_at")


def _file_first_ts(path, limit=262144):
    """Timestamp of the first JSON event in a session log, or None.

    Reads at most ``limit`` bytes. Does not move the tail offset.
    """
    try:
        fh = open(path, "rb")
    except OSError:
        return None
    used = 0
    try:
        while used < limit:
            line = fh.readline(limit - used)
            if not line:
                return None
            used += len(line)
            if b"recorded_at" not in line and b'"timestamp"' not in line:
                continue
            try:
                obj = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            ts = _epoch_num(obj.get("recorded_at"))
            if ts is None:
                ts = _epoch_num(obj.get("timestamp"))
            if ts is not None:
                return ts
    finally:
        fh.close()
    return None


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
    a scratchpad (those trees hold worktrees and database files). A root
    that is itself an ``<encoded-cwd>`` dir (e.g. from a glob-expanded
    scratchpads root) is also accepted: a uuid child holding
    ``scratchpad/<filename>`` is a session pad directly.
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

    def emit_session(sess_dir, sess_name):
        if not _is_uuid(sess_name) or (only is not None and sess_name != only):
            return
        pad_dir = os.path.join(sess_dir, "scratchpad")
        tsv = os.path.join(pad_dir, filename)
        if os.path.isfile(tsv):
            item = emit(os.path.dirname(tsv))
            if item:
                yield item
        try:
            children = os.scandir(pad_dir)
        except OSError:
            return
        with children:
            for child in children:
                try:
                    if not child.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                nested = os.path.join(child.path, filename)
                if os.path.isfile(nested):
                    item = emit(child.path)
                    if item:
                        yield (item[0], sess_name)

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
