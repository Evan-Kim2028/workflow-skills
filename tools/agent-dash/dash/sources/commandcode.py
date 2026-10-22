"""Command Code (``cmd``) source.

Labelled headless runs come from ``cmd-sessions.tsv`` written by
``scripts/launch_cmd.py`` in its outdir (``<pad>/cmd/``). Columns: label,
model, session_id (``-`` when no result arrived), started epoch, ended
epoch, input tokens, output tokens, status (``ok``, ``throttled`` or
``failed rc=N``). Each row is one attempt, so a throttled model shows as
its own ``died_early`` run with verdict ``QUOTA``. A run is keyed
``commandcode:<session>:<label>``; an attempt with no session id keys on
``attempt-<label>-<started>`` and reads its tokens from the TSV totals.

A launch still in progress has no TSV row yet. Its state comes from
``<outdir>/running/<label>`` (``<launcher pid>\\t<model>``, present while
the launch is live) and its session id from the newest
``stream-<label>-<n>.ndjson`` ``run_start`` event. Liveness is the pid
only: Command Code rewrites its process title, so argv and comm do not
identify it.

Tokens come from the session file ``~/.commandcode/projects/<encoded
cwd>/<session>.jsonl``. Assistant lines carry ``usage`` (inputTokens,
outputTokens, cacheReadTokens, cacheWriteTokens) and ``model``.
``inputTokens`` is the whole prompt and already contains
``cacheReadTokens``, so the stored ``input`` is the uncached remainder,
the same convention as the Grok CLI source. A labelled attempt whose
session file is missing falls back to the TSV totals under
``tsv:<session>``; that row is zeroed once the session file has usage.

Every other session file under the projects root (interactive or
unlabelled headless) is a run ``commandcode:<session>:<session>`` with
status ``done``. Command Code keeps no pid for those, so they never show
as running.

Reads reuse the Grok CLI reader: each session file keeps a byte cursor
under ``commandcode:jsonl:<path>``, so a tick reads only bytes appended
since the last one. TSV parses and verdict reads are cached on file
signature.

``cfg["roots"]["commandcode_projects"]`` overrides the projects root
(default ``~/.commandcode/projects``). Scratchpad roots and budgets come
from the same keys ``grok_cli`` reads.
"""

import hashlib
import json
import logging
import os
import re

from dash.sources import grok_cli as _grok
from dash.sources import manifests as _manifests

log = logging.getLogger("agent_dash.commandcode")

NAME = "commandcode"
INTERVAL_S = 2

TOOL = "commandcode"
MANIFEST = "cmd-sessions.tsv"
RUNNING = "running"
DEFAULT_PROJECTS = os.path.expanduser("~/.commandcode/projects")
TSV_CAP = 1024 * 1024
VERDICT_BYTES = 4096
KEY_PREFIX = "commandcode:jsonl:"

_STREAM = re.compile(r"^stream-(.+)-(\d+)\.ndjson$")

_tsv_cache: dict = {}
_verdict_cache: dict = {}
_last_write: dict = {}
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
    per_file, budget = _grok._budgets(cfg)
    projects = _grok._root(cfg, "commandcode_projects", "cmd_projects", default=DEFAULT_PROJECTS)
    only = _grok._only_session(cfg)

    attempts: dict = {}
    for scratch in _grok._scratch_roots(cfg):
        for pad in _outdirs(scratch, only):
            for row in _read_rows(os.path.join(pad, MANIFEST)):
                attempts[_attempt_sid(row)] = _tsv_attempt(row, pad)
            for live in _live_attempts(pad):
                _merge_live(attempts, live, pad)
    _mark_latest(attempts)

    seen = _seen(store)
    for sid, row in attempts.items():
        _upsert_attempt(store, sid, row, seen)

    index = _session_index(projects)
    for sid, path in index.items():
        budget = _consume(store, path, attempts.get(sid), budget, per_file, now, seen)
        if budget <= 0:
            break
    for sid, row in attempts.items():
        if row["tsv"] and sid not in index:
            _add_tsv_fallback(store, sid, row, now, seen)


def _outdirs(root, only=None):
    """Command Code outdirs under one scratch root.

    An outdir holds ``cmd-sessions.tsv`` or a ``running/`` folder. The
    running folder exists from the first launch, before any attempt has
    finished, so a launch shows before its TSV row lands. Checked: the root
    itself, then ``<enc>/<session>/scratchpad`` and each folder under it.
    The root may be an encoded-cwd folder (``<tmpdir>/claude-<uid>/*``)
    or the folder above one; both are walked. A bare ``running/`` folder
    is not enough: the pi launcher keeps its own ``running/`` folder, so
    a manifest-less outdir must also carry Command Code evidence (the
    ``cooldown.json`` marker or a ``stream-<label>-<n>.ndjson`` attempt
    stream, never the pi ``stream-<label>.ndjson`` shape).
    """
    found = []
    seen = set()

    def take(path):
        real = os.path.realpath(path)
        if real in seen:
            return
        if os.path.isfile(os.path.join(path, MANIFEST)) or (
            os.path.isdir(os.path.join(path, RUNNING)) and _looks_like_cmd_dir(path)
        ):
            seen.add(real)
            found.append(path)

    if not root or not os.path.isdir(root):
        return found
    take(root)
    for enc in [root] + _subdirs(root):
        for sess in _subdirs(enc):
            if only is not None and os.path.basename(sess) != only:
                continue
            pad = os.path.join(sess, "scratchpad")
            take(pad)
            for child in _subdirs(pad):
                take(child)
    return found


def _looks_like_cmd_dir(path):
    """True when a manifest-less folder carries Command Code-only evidence.

    ``out-``/``err-`` files and ``running/`` are shared with the pi
    launcher, so they do not count: only the ``cooldown.json`` marker or a
    ``stream-<label>-<n>.ndjson`` attempt stream (never the pi
    ``stream-<label>.ndjson`` shape).
    """
    try:
        names = os.listdir(path)
    except OSError:
        return False
    if "cooldown.json" in names:
        return True
    return any(_STREAM.match(name) for name in names)


def _subdirs(path):
    try:
        entries = os.scandir(path)
    except OSError:
        return []
    out = []
    with entries:
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    out.append(entry.path)
            except OSError:
                continue
    return out


def _seen(store):
    """Per-store memo of what was last written, so a fresh store is always filled."""
    try:
        return store.__dict__.setdefault("_commandcode_seen", {})
    except AttributeError:
        return _last_write


def parse_rows(text):
    """Attempt rows from ``cmd-sessions.tsv``. The header and bad rows are skipped."""
    rows = []
    for raw in (text or "").splitlines():
        parts = raw.strip("\r\n").split("\t")
        if len(parts) < 8:
            continue
        label = parts[0].strip()
        started = _float(parts[3])
        if not label or started is None:
            continue
        sid = parts[2].strip()
        rows.append(
            {
                "label": label,
                "model": parts[1].strip() or None,
                "sid": None if sid in ("", "-") else sid,
                "started": started,
                "ended": _float(parts[4]),
                "input": _int(parts[5]),
                "output": _int(parts[6]),
                "status": parts[7].strip(),
            }
        )
    return rows


def _read_rows(path):
    try:
        st = os.stat(path)
    except OSError:
        return []
    sig = (st.st_size, st.st_mtime_ns)
    hit = _tsv_cache.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    if st.st_size > TSV_CAP:
        return []
    try:
        with open(path, "rb") as fh:
            text = fh.read(st.st_size).decode("utf-8", "replace")
    except OSError:
        _fail("manifest")
        return []
    rows = parse_rows(text)
    _tsv_cache[path] = (sig, rows)
    return rows


def _attempt_sid(row):
    if row.get("sid"):
        return row["sid"]
    return "attempt-%s-%d" % (row["label"], int(row["started"] or 0))


def _tsv_attempt(row, pad):
    return {
        "label": row["label"],
        "model": row["model"],
        "started": row["started"],
        "ended": row["ended"],
        "input": row["input"],
        "output": row["output"],
        "status_raw": row["status"],
        "tsv": True,
        "live": False,
        "live_pid": None,
        "pad": pad,
        "latest": False,
    }


def _live_attempts(pad):
    """Live launches in one outdir: ``running/<label>`` plus the stream's session id."""
    rdir = os.path.join(pad, RUNNING)
    try:
        names = os.listdir(rdir)
    except OSError:
        return []
    out = []
    for label in names:
        path = os.path.join(rdir, label)
        pid, model = _read_running(path)
        if pid is None:
            continue
        sid = _stream_session(pad, label)
        if not sid:
            continue
        try:
            started = os.stat(path).st_mtime
        except OSError:
            started = None
        out.append(
            {
                "label": label,
                "pid": pid,
                "model": model,
                "sid": sid,
                "started": started,
                "alive": _alive(pid),
            }
        )
    return out


def _merge_live(attempts, live, pad):
    prior = attempts.get(live["sid"])
    if prior is None:
        attempts[live["sid"]] = {
            "label": live["label"],
            "model": live["model"],
            "started": live["started"],
            "ended": None,
            "input": None,
            "output": None,
            "status_raw": None,
            "tsv": False,
            "live": live["alive"],
            "live_pid": live["pid"] if live["alive"] else None,
            "pad": pad,
            "latest": False,
        }
        return
    if live["alive"]:
        prior["live"] = True
        prior["live_pid"] = live["pid"]
        prior["model"] = prior["model"] or live["model"]


def _mark_latest(attempts):
    """The newest attempt per (outdir, label) owns the label's ``out-`` file."""
    best: dict = {}
    for sid, row in attempts.items():
        key = (row["pad"], row["label"])
        started = row["started"] if row["started"] is not None else -1.0
        if key not in best or started >= best[key][0]:
            best[key] = (started, sid)
    for sid, row in attempts.items():
        row["latest"] = best.get((row["pad"], row["label"]), (None, None))[1] == sid


def _read_running(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read(256)
    except OSError:
        return None, None
    head, _sep, model = text.partition("\t")
    try:
        pid = int(head.strip())
    except ValueError:
        return None, None
    return pid, model.strip() or None


def _stream_session(pad, label):
    """Session id from the newest attempt's ``run_start`` event, or None."""
    best = None
    try:
        names = os.listdir(pad)
    except OSError:
        return None
    for name in names:
        m = _STREAM.match(name)
        if not m or m.group(1) != label:
            continue
        n = int(m.group(2))
        if best is None or n > best[0]:
            best = (n, name)
    if best is None:
        return None
    try:
        with open(os.path.join(pad, best[1]), "rb") as fh:
            head = fh.read(8192)
    except OSError:
        return None
    for line in head.split(b"\n")[:8]:
        if b"run_start" not in line:
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        event = obj.get("event") if isinstance(obj, dict) else None
        sid = event.get("sessionId") if isinstance(event, dict) else None
        if isinstance(sid, str) and sid:
            return sid
    return None


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _upsert_attempt(store, sid, row, seen):
    label = row["label"]
    verdict = None
    ended = None
    pid = None
    if row["live"]:
        status = "running"
        pid = row["live_pid"]
    elif row["tsv"]:
        status, verdict = _tsv_status(row["status_raw"])
        ended = row["ended"]
        if status == "done" and row["latest"]:
            verdict = _verdict(row["pad"], label)
    else:
        status = "died_early"
    run = {
        "run_id": "commandcode:%s:%s" % (sid, label),
        "source": NAME,
        "tier": NAME,
        "session_id": sid,
        "label": label,
        "status": status,
        "ended_at": ended,
        "clock_source": "manifest",
    }
    if row["model"]:
        run["model"] = row["model"]
    if row["started"] is not None:
        run["started_at"] = row["started"]
    if pid:
        run["pid"] = pid
    if verdict:
        run["verdict"] = verdict
    _emit_run(store, run, sid, label, row["started"], seen)


def _tsv_status(status):
    """``(run status, verdict)`` for one TSV status cell."""
    if status == "ok":
        return "done", None
    if status == "throttled":
        return "died_early", "QUOTA"
    return "died_early", None


def _verdict(pad, label):
    """Verdict line of ``out-<label>.md``, cached on the file's signature."""
    path = os.path.join(pad, "out-%s.md" % label)
    try:
        st = os.stat(path)
    except OSError:
        return None
    sig = (st.st_size, st.st_mtime_ns)
    hit = _verdict_cache.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        with open(path, "rb") as fh:
            head = fh.read(VERDICT_BYTES).decode("utf-8", "replace")
    except OSError:
        return None
    found = _manifests.verdict_of(head)
    _verdict_cache[path] = (sig, found)
    return found


def _emit_run(store, run, sid, label, started, seen):
    sig = repr(sorted(run.items()))
    if seen.get(run["run_id"]) == sig:
        return
    try:
        store.upsert_run(run)
    except Exception:
        _fail("run")
        return
    session = {"session_id": sid, "tool": TOOL}
    if started is not None:
        session["started_at"] = started
    if label and label != sid:
        session["label"] = label
    try:
        store.upsert_session(session)
    except Exception:
        _fail("session")
        return
    seen[run["run_id"]] = sig


def _session_index(root):
    """Map session id -> jsonl path under ``<root>/<encoded-cwd>/``."""
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
                files = os.scandir(enc.path)
            except OSError:
                continue
            with files:
                for entry in files:
                    name = entry.name
                    if not name.endswith(".jsonl") or name.endswith(".checkpoints.jsonl"):
                        continue
                    try:
                        if entry.is_file(follow_symlinks=False):
                            found[name[: -len(".jsonl")]] = entry.path
                    except OSError:
                        continue
    return found


def _consume(store, path, info, budget, per_file, now, seen):
    """Read the bytes appended to one session file since the last tick."""
    sid = os.path.basename(path)[: -len(".jsonl")]
    try:
        raw, budget, _first = _grok._read_chunk(store, path, KEY_PREFIX + path, budget, per_file)
    except OSError:
        _fail("session")
        return budget
    if not raw:
        return budget
    header, events, last_ts = _parse_lines(raw)
    if info is None:
        label, tsv, hint = None, False, None
        run_id = "commandcode:%s:%s" % (sid, sid)
    else:
        label, tsv, hint = info["label"], info["tsv"], info["model"]
        run_id = "commandcode:%s:%s" % (sid, label)
    model = None
    for ev in events:
        model = ev["model"] or model
        _add(
            store,
            {
                "ts": ev["ts"] if ev["ts"] is not None else now,
                "session_id": sid,
                "run_id": run_id,
                "model": ev["model"] or hint,
                "tool": TOOL,
                "message_id": ev["message_id"],
                "input": ev["input"],
                "output": ev["output"],
                "cache_read": ev["cache_read"],
                "cache_write": ev["cache_write"],
                "reasoning": 0,
            },
        )
    if events and tsv:
        _add(
            store,
            {
                "ts": now,
                "session_id": sid,
                "run_id": run_id,
                "model": hint,
                "tool": TOOL,
                "message_id": "tsv:" + sid,
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 0,
            },
        )
    if info is None:
        started = _epoch(header.get("timestamp")) if header else None
        run = {
            "run_id": run_id,
            "source": NAME,
            "tier": NAME,
            "session_id": sid,
            "status": "done",
            "clock_source": "transcript",
        }
        if model:
            run["model"] = model
        if started is not None:
            run["started_at"] = started
        if last_ts is not None:
            run["ended_at"] = last_ts
        _emit_run(store, run, sid, None, started, seen)
    elif model and not hint:
        info["model"] = model
        _emit_run(store, {"run_id": run_id, "source": NAME, "tier": NAME, "session_id": sid, "model": model}, sid, label, None, seen)
    return budget


def _parse_lines(raw):
    header = None
    events = []
    last_ts = None
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "session":
            if header is None:
                header = obj
            continue
        ts = _epoch(obj.get("timestamp"))
        if ts is not None and (last_ts is None or ts > last_ts):
            last_ts = ts
        usage = obj.get("usage")
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        if not isinstance(usage, dict) or message.get("role") == "user":
            continue
        events.append(_usage_event(obj, usage, ts, line))
    return header, events, last_ts


def _usage_event(obj, usage, ts, line):
    total = _num(usage.get("inputTokens"))
    cached = _num(usage.get("cacheReadTokens"))
    mid = obj.get("id")
    if not isinstance(mid, str) or not mid:
        mid = "line:" + hashlib.sha1(line).hexdigest()[:16]
    model = obj.get("model") if isinstance(obj.get("model"), str) else None
    return {
        "message_id": mid,
        "ts": ts,
        "model": model,
        "input": max(total - cached, 0),
        "output": _num(usage.get("outputTokens")),
        "cache_read": cached,
        "cache_write": _num(usage.get("cacheWriteTokens")),
    }


def _add_tsv_fallback(store, sid, row, now, seen):
    marker = ("tsv", sid)
    fingerprint = (row["input"], row["output"], row["model"])
    if seen.get(marker) == fingerprint:
        return
    _add(
        store,
        {
            "ts": row["ended"] or now,
            "session_id": sid,
            "run_id": "commandcode:%s:%s" % (sid, row["label"]),
            "model": row["model"],
            "tool": TOOL,
            "message_id": "tsv:" + sid,
            "input": row["input"] or 0,
            "output": row["output"] or 0,
            "cache_read": 0,
            "cache_write": 0,
            "reasoning": 0,
        },
    )
    seen[marker] = fingerprint


def _add(store, row):
    try:
        store.add_usage(row)
    except Exception:
        _fail("usage")


def _fail(where):
    global last_error
    last_error = where
    log.warning("%s failed", where)


def _float(text):
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _int(text):
    try:
        return int(float(str(text).strip()))
    except (TypeError, ValueError):
        return 0


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _epoch(value):
    return _grok._epoch_any(value)

