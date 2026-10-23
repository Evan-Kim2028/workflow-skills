"""pi (``@mariozechner/pi-coding-agent`` CLI) source.

Labelled headless runs come from ``pi-sessions.tsv`` written by
``scripts/launch_pi.py`` in its outdir (``<pad>/pi/``). Columns: label,
session_id, started epoch, optional model. ``running/<label>``
(``<launcher pid>\\t<model>``) exists while a launch is live; the raw
NDJSON event stream is ``stream-<label>.ndjson`` and the final assistant
text lands in ``out-<label>.md``.

A run is keyed ``pi:<session>:<label>``. Tokens come from the stream's
assistant ``message_end`` events (``usage.input`` is the cache miss,
``cacheRead`` the hit; ``output`` the completion). ``message_id`` is
``<session>-<index>`` where ``index`` counts assistant messages in the
stream. The stream is byte-cursored per file so a tick reads only bytes
appended since the last one.

Liveness is the launcher: a live ``python3 .../scripts/launch_pi.py``
with the run's label (see ``dash.sources.process.live_agent_processes``),
recorded as a process sample for the run. The pi CLI rewrites its
process title, so the worker itself carries no label and is not used.
The ``running/<label>`` pid file backs the launcher up when the process
listing is unavailable.

Verdict comes from the first line of ``out-<label>.md``. A run whose err
or out log shows quota/credit exhaustion finishes ``died_early`` with
verdict ``BLOCKED``; ``QUOTA`` beside the manifest names the model to
swap. An empty out file with a real error and no live process reads
``died_early``.

``cfg["roots"]["pi_scratch"]``/``pi_sessions`` is unused; scratchpad
roots and registered run dirs come from the same keys every manifest
source reads. Reads reuse ``grok_cli``'s byte reader.

No prompt bodies, argv, or secrets are read: only labels, ids, models,
counts, and file mtimes.
"""

from __future__ import annotations

import json
import logging
import os
import re

from dash.liveness import file_shows_quota
from dash.sources import grok_cli as _grok
from dash.sources import manifests as _manifests
from dash.sources import process as _process

log = logging.getLogger("agent_dash.pi")

NAME = "pi"
INTERVAL_S = 2

TOOL = "pi"
MANIFEST = "pi-sessions.tsv"
RUNNING = "running"
TSV_CAP = 1024 * 1024
META_CAP = 64 * 1024
VERDICT_BYTES = 4096
DIRECTORY = "pi"

_tsv_cache: dict = {}
_meta_cache: dict = {}
_verdict_cache: dict = {}
_last_write: dict = {}
last_error = None


def poll(store, cfg, now):
    global last_error
    last_error = None
    try:
        _poll(store, cfg or {}, float(now))
    except Exception:
        _fail("poll")


def _poll(store, cfg, now):
    rows: dict = {}
    for scratch in _scratch_roots(cfg):
        for pad in _outdirs(scratch):
            for row in _read_rows(os.path.join(pad, MANIFEST)):
                rows[_row_key(row)] = _tsv_row(row, pad)
            for live in _live_rows(pad):
                rows[_row_key(live)] = live
    processes = _agent_processes()
    seen = _seen(store)
    for row in rows.values():
        _upsert_run(store, row, processes, seen)


def _outdirs(root):
    """pi outdirs under one scratch root: the root, or ``<pad>/pi/``.

    A pi outdir holds ``pi-sessions.tsv`` or a ``running/`` folder. The
    running folder exists from the first launch, before any TSV row lands.
    The root itself is accepted (a registered flat run folder) and every
    ``pi/`` child of a scratchpad / one level under it. A bare ``running/``
    folder is not enough: another harness's outdir (Command Code keeps its
    own ``running/``) would read as a second ``pi:`` run for the same
    label, so a manifest-less outdir must also carry pi evidence (a
    ``prompt-`` file, the ``QUOTA`` marker, a ``sessions/`` dir, or a
    pi-shaped ``stream-<label>.ndjson``, never the ``-<n>`` Command Code
    shape).
    """
    found = []
    seen = set()

    def take(path):
        if not path:
            return
        real = os.path.realpath(path)
        if real in seen:
            return
        if os.path.isfile(os.path.join(path, MANIFEST)) or (
            os.path.isdir(os.path.join(path, RUNNING)) and _looks_like_pi_dir(path)
        ):
            seen.add(real)
            found.append(path)

    if not root or not os.path.isdir(root):
        return found
    take(root)
    take(os.path.join(root, DIRECTORY))
    for enc in _subdirs(root):
        take(enc)
        take(os.path.join(enc, DIRECTORY))
        for sess in _subdirs(enc):
            pad = os.path.join(sess, "scratchpad")
            take(pad)
            take(os.path.join(pad, DIRECTORY))
            for child in _subdirs(pad):
                take(child)
                take(os.path.join(child, DIRECTORY))
    return found


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
    try:
        return store.__dict__.setdefault("_pi_seen", {})
    except AttributeError:
        return _last_write


def parse_rows(text):
    """Session rows from ``pi-sessions.tsv``: label, session_id, start, model, tier.

    The optional fifth cell is the tier the launcher recorded (``pi-deepseek``,
    ``pi-beam``); older rows have none and fall back to the model family.
    """
    rows = []
    for raw in (text or "").splitlines():
        line = raw.strip("\r\n").lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        label = parts[0].strip()
        started = _float(parts[2])
        if not label or started is None:
            continue
        sid = parts[1].strip()
        model = parts[3].strip() if len(parts) > 3 and parts[3].strip() else None
        tier = parts[4].strip() if len(parts) > 4 and parts[4].strip() else None
        rows.append(
            {
                "label": label,
                "sid": None if sid in ("", "-") else sid,
                "started": started,
                "model": model,
                "tier": tier,
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


_CMD_STREAM = re.compile(r"^stream-(.+)-(\d+)\.ndjson$")


def _looks_like_pi_dir(path):
    """True when a manifest-less folder carries pi-only evidence.

    ``out-``/``err-`` files and ``running/`` are shared with the Command
    Code launcher, so they do not count: only a ``prompt-`` file, the
    ``QUOTA`` marker, the pi ``sessions/`` dir, or a pi-shaped stream
    (``stream-<label>.ndjson``, never the ``-<n>`` Command Code shape).
    """
    try:
        names = os.listdir(path)
    except OSError:
        return False
    if "QUOTA" in names:
        return True
    if "sessions" in names and os.path.isdir(os.path.join(path, "sessions")):
        return True
    for name in names:
        if name.startswith("prompt-") and name.endswith(".md"):
            return True
        if (
            name.startswith("stream-")
            and name.endswith(".ndjson")
            and not _CMD_STREAM.match(name)
        ):
            return True
    return False


def _row_key(row):
    sid = row.get("sid") or row.get("label")
    return "%s|%s" % (sid, row["label"])


def _tsv_row(row, pad):
    return {
        "label": row["label"],
        "sid": row["sid"],
        "model": row["model"],
        "started": row["started"],
        "tier": row.get("tier"),
        "meta": _read_meta(pad, row["label"]),
        "pad": pad,
        "live": False,
        "live_pid": None,
    }


def _live_rows(pad):
    """Live launches in one outdir: ``running/<label>`` plus the stream session id.

    A launch with no session id yet (the stream has not recorded its
    ``session`` event) is skipped until the next poll: emitting it now
    would key the run ``pi:<label>:<label>`` and orphan that row the
    moment the stream or the TSV row names the real session.
    """
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
        if sid is None:
            continue
        try:
            started = os.stat(path).st_mtime
        except OSError:
            started = None
        out.append(
            {
                "label": label,
                "sid": sid,
                "model": model,
                "started": started,
                "pad": pad,
                "live": _alive(pid),
                "live_pid": pid,
            }
        )
    return out


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


def _stream_path(pad, label):
    return os.path.join(pad, "stream-%s.ndjson" % label)


def _stream_session(pad, label):
    """Session id from the stream's ``session`` event, or None."""
    path = _stream_path(pad, label)
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
    except OSError:
        return None
    for line in head.split(b"\n")[:8]:
        if b'"session"' not in line:
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "session":
            sid = obj.get("id")
            if isinstance(sid, str) and sid:
                return sid
    return None


def _agent_processes():
    try:
        return _process.live_agent_processes()
    except Exception:
        return None


def _model_tier(model):
    """Model family for a pi run: beam, glm or deepseek; the harness stays in ``source``."""
    text = (model or "").lower()
    if "beam" in text:
        return "beam"
    if "glm" in text:
        return "glm"
    if "deepseek" in text:
        return "deepseek"
    return NAME


def _tier_of(row):
    """Tier for one manifest row: the recorded tier, then the meta file, then the model.

    ``run_worker.py`` records the registry tier it launched (``pi-deepseek``,
    ``pi-beam``) as a fifth cell and in ``<label>.meta.json``. Both beat the
    model-family guess, which is what keeps a pi run off the ``pi`` bucket.
    """
    for value in (row.get("tier"), (row.get("meta") or {}).get("tier")):
        text = str(value or "").strip()
        if text:
            return text
    return _model_tier(row.get("model"))


def _read_meta(pad, label):
    """``<label>.meta.json`` from ``run_worker.py``, or {}. Never raises."""
    path = os.path.join(pad, "%s.meta.json" % label)
    try:
        st = os.stat(path)
    except OSError:
        return {}
    sig = (st.st_size, st.st_mtime_ns)
    hit = _meta_cache.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read(META_CAP).decode("utf-8", "replace"))
    except (OSError, ValueError):
        return {}
    meta = data if isinstance(data, dict) else {}
    _meta_cache[path] = (sig, meta)
    return meta


def _upsert_run(store, row, processes, seen):
    label = row["label"]
    pad = row["pad"]
    sid = row.get("sid") or _stream_session(pad, label) or label
    out_path = os.path.join(pad, "out-%s.md" % label)
    err_path = os.path.join(pad, "err-%s.log" % label)
    quota = _quota(pad, label, out_path, err_path, sid)
    run = {
        "run_id": "pi:%s:%s" % (sid, label),
        "source": NAME,
        "tier": _tier_of(row),
        "session_id": sid,
        "label": label,
        "out_path": out_path,
    }
    parent = _manifests.parent_session_from_pad(pad)
    if parent:
        run["parent_session_id"] = parent
    if row.get("model"):
        run["model"] = row["model"]
    if row.get("started") is not None:
        run["started_at"] = row["started"]
    if row.get("live") and row.get("live_pid"):
        run["pid"] = row["live_pid"]
    if quota:
        run["status"] = "died_early"
        run["verdict"] = "BLOCKED"
        run["ended_at"] = _mtime(out_path) or _mtime(err_path) or row.get("started")
    else:
        verdict = _verdict(out_path)
        if verdict:
            run["verdict"] = verdict
        if row.get("live"):
            run["status"] = "running"
        elif verdict:
            run["status"] = "done"
            run["ended_at"] = _mtime(out_path) or row.get("started")
        else:
            out_bytes = _size(out_path)
            err_bytes = _size(err_path)
            if out_bytes == 0 and err_bytes > 0:
                run["status"] = "died_early"
                run["ended_at"] = _mtime(err_path) or row.get("started")
            elif out_bytes > 0:
                run["status"] = "done"
                run["ended_at"] = _mtime(out_path) or row.get("started")
            else:
                run["status"] = "unknown"
    _process_apply(store, run, processes)
    _emit_run(store, run, seen)


def _process_apply(store, run, processes):
    """Live pi process vouches the run is running; a gone one closes it."""
    try:
        _process.apply_liveness(store, run, NAME, processes)
    except Exception:
        _fail("liveness")


def _quota(pad, label, out_path, err_path, sid) -> bool:
    """True when the QUOTA marker or a quota log names this run's failure."""
    if os.path.isfile(os.path.join(pad, "QUOTA")):
        try:
            with open(os.path.join(pad, "QUOTA"), "r", encoding="utf-8") as fh:
                text = fh.read(4096)
        except OSError:
            text = ""
        if not text.strip() or label in text or sid in text:
            return True
    for path in (err_path, out_path):
        try:
            if path and file_shows_quota(path):
                return True
        except Exception:
            continue
    return False


def _verdict(out_path):
    """Verdict line of ``out-<label>.md``, cached on the file's signature."""
    try:
        st = os.stat(out_path)
    except OSError:
        return None
    sig = (st.st_size, st.st_mtime_ns)
    hit = _verdict_cache.get(out_path)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        with open(out_path, "rb") as fh:
            head = fh.read(VERDICT_BYTES).decode("utf-8", "replace")
    except OSError:
        return None
    found = _manifests.verdict_of(head)
    _verdict_cache[out_path] = (sig, found)
    return found


def parse_stream(raw, fallback_sid):
    """Assistant usage events from raw NDJSON bytes.

    Each ``message_end`` with an assistant message and a usage block is one
    event: ``input`` the cache miss, ``cacheRead`` the hit, ``cacheWrite``
    the write, ``output`` the completion. ``message_id`` is
    ``<session>-<index>`` where index counts assistant messages in order.
    """
    events = []
    index = 0
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "message_end":
            continue
        message = obj.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        sid = fallback_sid
        index += 1
        model = message.get("model") if isinstance(message.get("model"), str) else None
        events.append(
            {
                "message_id": "%s-%d" % (sid, index),
                "ts": _epoch(obj.get("timestamp") if obj.get("timestamp") is not None else message.get("timestamp")),
                "model": model,
                "input": _num(usage.get("input")),
                "output": _num(usage.get("output")),
                "cache_read": _num(usage.get("cacheRead")),
                "cache_write": _num(usage.get("cacheWrite")),
            }
        )
    return events


def final_text(raw):
    """Last assistant text block in a stream (the worker's report), or ''."""
    text = ""
    for line in (raw or b"").split(b"\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "message_end":
            continue
        message = obj.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        parts = [
            c.get("text", "")
            for c in (message.get("content") or [])
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        joined = "".join(parts)
        if joined.strip():
            text = joined
    return text


def _emit_run(store, run, seen):
    sig = repr(sorted(run.items()))
    key = run["run_id"]
    if seen.get(key) == sig:
        return
    try:
        store.upsert_run(run)
    except Exception:
        _fail("run")
        return
    session = {"session_id": run.get("session_id"), "tool": TOOL}
    if run.get("started_at") is not None:
        session["started_at"] = run["started_at"]
    if run.get("label"):
        session["label"] = run["label"]
    try:
        store.upsert_session(session)
    except Exception:
        _fail("session")
        return
    seen[key] = sig


def _scratch_roots(cfg):
    return _grok._scratch_roots(cfg)


def _size(path):
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _fail(where):
    global last_error
    last_error = where
    log.warning("%s failed", where)


def _float(text):
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _epoch(value):
    return _grok._epoch_any(value)
