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

``run_worker.py`` writes its manifest row in the run folder while
``launch_pi.py`` writes the stream, out and err files in ``<run_dir>/pi/``,
so a row's stream is looked up beside the manifest and then in ``pi/``.
The stream's own ``session`` event names the pi session; usage rows use
that id (the launcher's ``generic.jsonl`` lines use it too, so the two
readers upsert the same row instead of counting it twice).

The stream's session id is also the run's id, even when the run-folder
manifest carries a pre-generated one: ``run_worker.py`` writes a session
id before pi starts, pi picks its own, and the launcher records that in
``<run_dir>/pi/pi-sessions.tsv``. Keying the run by the stream id makes
the run-folder row and the launcher's ``pi/`` row one run, and matches the
``generic.jsonl`` usage rows. A pre-generated row already in the store is
folded onto the stream run on the next poll (see :func:`_reassign_twin`).

Liveness is the launcher: a live ``python3 .../scripts/launch_pi.py``
with the run's label (see ``dash.sources.process.live_agent_processes``),
recorded as a process sample for the run. The pi CLI rewrites its
process title, so the worker itself carries no label and is not used.
The ``running/<label>`` pid file backs the launcher up when the process
listing is unavailable.

Verdict comes from the first line of ``out-<label>.md``. A run whose err
or out log shows quota/credit exhaustion finishes ``died_early`` with
verdict ``BLOCKED``; ``QUOTA-<label>`` beside the manifest names the model to
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
from dash.models import is_deepseek_alias, is_kimi_alias
from dash.sources import grok_cli as _grok
from dash.sources import manifests as _manifests
from dash.sources import process as _process
from dash.sources import realpath_memo
from dash.sources import set_discovery_ttl, discovery_memo

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
_session_cache: dict = {}
_quota_cache: dict = {}
last_error = None


#: Directory-walk reuse window (seconds), the shared ``dir_scan_s`` knob.
DIR_SCAN_S = 15

def poll(store, cfg, now):
    global last_error
    last_error = None
    set_discovery_ttl((cfg or {}).get("dir_scan_s", DIR_SCAN_S))
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
    hints = _process_hints(processes)
    seen = _seen(store)
    per_file, budget = _grok._budgets(cfg)
    pending = {"usage": [], "commits": [], "folds": []}
    try:
        for row, stream_sid, folds in _collapse(rows):
            budget = _upsert_run(
                store,
                row,
                processes,
                seen,
                budget,
                per_file,
                now,
                stream_sid=stream_sid,
                folds=folds,
                hints=hints,
                pending=pending,
            )
    finally:
        if _flush_usage(store, pending["usage"]):
            _commit_cursors(store, pending["commits"])
            _apply_folds(store, pending["folds"])


_UNSET = object()


def _collapse(rows):
    """One row per run: the launcher's ``pi/`` row beats the run-folder twin.

    ``run_worker.py`` writes a manifest row in the run folder and
    ``launch_pi.py`` writes one in ``<run_dir>/pi/``. Both resolve to the
    same stream session id, so they are one run, but their pads differ and
    the emitted ``out_path``/``started_at``/``tier`` did not: the two rows
    overwrote one run row twice per poll, so the emit cache never matched
    and every poll rewrote ~2,250 rows. One row per run is emitted here and
    its twin's fields fill any gap; a dropped twin's pre-generated session
    id is still folded onto the stream run.
    """
    order = []
    groups: dict = {}
    for row in rows.values():
        stream_sid = _stream_session(row["pad"], row["label"])
        sid = stream_sid or row.get("sid") or row["label"]
        key = "pi:%s:%s" % (sid, row["label"])
        item = {"row": row, "stream_sid": stream_sid, "sid": sid}
        if key in groups:
            groups[key].append(item)
        else:
            groups[key] = [item]
            order.append(key)
    out = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            item = group[0]
            out.append((item["row"], item["stream_sid"], ()))
            continue
        chosen = max(group, key=_row_rank)
        others = [item for item in group if item is not chosen]
        folds = tuple(
            str(item["row"]["sid"])
            for item in others
            if item["row"].get("sid") and item["row"]["sid"] != item["sid"]
        )
        out.append(
            (
                _merge_twin(chosen["row"], [item["row"] for item in others]),
                chosen["stream_sid"],
                folds,
            )
        )
    return out


def _row_rank(item):
    """Sort key for one run's twin rows, matching the order they used to be emitted in.

    The walk yields a run folder before its ``pi/`` subfolder and the
    manifest rows before the ``running/<label>`` echo, and the last row
    written won. So a live row outranks a manifest row, and between two
    manifest rows the one that already names the stream's session wins,
    since its pad holds the out and stream files.
    """
    row = item["row"]
    return (
        1 if row.get("live") else 0,
        1 if row.get("sid") and row["sid"] == item["stream_sid"] else 0,
        1 if row.get("started") is not None else 0,
    )


def _merge_twin(row, others):
    """``row`` plus every field a twin row carries and it lacks.

    ``tier`` is deliberately not merged: the run-folder row records the
    registry tier and the ``pi/`` row falls back to the model family, so
    merging it would move ~160 stored rows onto a different tier bucket.
    That correction is a separate, visible change.
    """
    merged = dict(row)
    for other in others:
        for key in ("started", "model"):
            if merged.get(key) is None and other.get(key) is not None:
                merged[key] = other[key]
        if other.get("live") is True:
            merged["live"] = True
            merged["live_pid"] = other.get("live_pid") or merged.get("live_pid")
    return merged


def _outdirs(root):
    """Directory walk, reused for this poll thread's ``dir_scan_s`` window. See :func:`_outdirs_walk`."""
    from dash.sources import discovery_memo

    return list(discovery_memo((__name__, '_outdirs', root), lambda: _outdirs_walk(root)))


def _outdirs_walk(root):
    """pi outdirs under one scratch root: the root, or ``<pad>/pi/``.

    A pi outdir holds ``pi-sessions.tsv`` or a ``running/`` folder. The
    running folder exists from the first launch, before any TSV row lands.
    The root itself is accepted (a registered flat run folder) and every
    ``pi/`` child of a scratchpad / one level under it. A bare ``running/``
    folder is not enough: another harness's outdir (Command Code keeps its
    own ``running/``) would read as a second ``pi:`` run for the same
    label, so a manifest-less outdir must also carry pi evidence (a
    ``prompt-`` file, a ``QUOTA-<label>`` marker, a ``sessions/`` dir, or a
    pi-shaped ``stream-<label>.ndjson``, never the ``-<n>`` Command Code
    shape).
    """
    found = []
    seen = set()

    def take(path):
        if not path:
            return
        real = realpath_memo(path)
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


_dir_cache: dict = {}


def _subdirs(path):
    """Child directories of ``path``, from the (inode, mtime) listing cache."""
    from dash.sources import list_dir_cached

    entries, _listed = list_dir_cached(path, _dir_cache)
    return [full for _name, full, _is_file, is_dir in (entries or []) if is_dir]


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
    Code launcher, so they do not count: only a ``prompt-`` file, a
    ``QUOTA-<label>`` marker, the pi ``sessions/`` dir, or a pi-shaped stream
    (``stream-<label>.ndjson``, never the ``-<n>`` Command Code shape).
    """
    try:
        names = os.listdir(path)
    except OSError:
        return False
    if "QUOTA" in names or any(name.startswith("QUOTA-") for name in names):
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


def _stream_file(pad, label):
    """The run's stream: beside the manifest, else in the launcher's ``pi/``.

    ``run_worker.py`` records its manifest row in the run folder while
    ``launch_pi.py`` writes the stream in ``<run_dir>/pi/``, so a row read
    from the run folder still finds its stream one level down.
    """
    path = _stream_path(pad, label)
    if os.path.isfile(path):
        return path
    alt = os.path.join(pad, DIRECTORY, "stream-%s.ndjson" % label)
    return alt if os.path.isfile(alt) else path


def _stream_session(pad, label):
    """Session id from the stream's ``session`` event, or None.

    The head is cached on (inode, size, mtime): an unchanged stream is not
    re-opened on every poll. A growing stream keeps its head in place, so
    the cache stays valid while the tail is appended."""
    path = _stream_file(pad, label)
    try:
        st = os.stat(path)
    except OSError:
        return None
    sig = (st.st_ino, st.st_size, getattr(st, "st_mtime_ns", None))
    hit = _session_cache.get(path)
    if hit is not None and hit[0] == sig:
        return hit[1]
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
    except OSError:
        return None
    out = None
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
                out = sid
                break
    if len(_session_cache) > 20000:
        _session_cache.clear()
    _session_cache[path] = (sig, out)
    return out


def _agent_processes():
    try:
        return _process.live_agent_processes()
    except Exception:
        return None


def _model_tier(model):
    """Model family for a pi run: beam, glm, deepseek or kimi; the harness stays in ``source``."""
    text = (model or "").lower()
    if "beam" in text:
        return "beam"
    if "glm" in text:
        return "glm"
    if "deepseek" in text or is_deepseek_alias(text):
        return "deepseek"
    if is_kimi_alias(text):
        return "kimi"
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


def _upsert_run(
    store,
    row,
    processes,
    seen,
    budget=0,
    per_file=0,
    now=None,
    stream_sid=_UNSET,
    folds=(),
    hints=None,
    pending=None,
):
    label = row["label"]
    pad = row["pad"]
    if stream_sid is _UNSET:
        stream_sid = _stream_session(pad, label)
    sid = stream_sid or row.get("sid") or label
    out_path = _first_file(pad, "out-%s.md" % label)
    err_path = _first_file(pad, "err-%s.log" % label)
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
    if _needs_liveness(store, run, hints):
        _process_apply(store, run, processes)
    budget = _apply_stream(store, run, pad, label, budget, per_file, now, pending)
    _emit_run(store, run, seen)
    if stream_sid and row.get("sid") and row["sid"] != stream_sid:
        # After the emit: the stream run exists, so the fold can move the
        # pre-generated row on this poll instead of the next one.
        _queue_fold(store, pending, row["sid"], stream_sid, label)
    for old_sid in folds:
        _queue_fold(store, pending, old_sid, sid, label)
    return budget


_TERMINAL = ("done", "died_early")


def _process_hints(processes):
    """``(pids, labels)`` from one process listing, or None when unavailable."""
    if processes is None:
        return None
    pids = set()
    labels = set()
    for proc in processes:
        pid = proc.get("pid")
        if pid is not None:
            pids.add(pid)
        label = proc.get("label")
        if label:
            labels.add(label)
    return pids, labels


def _needs_liveness(store, run, hints) -> bool:
    """True when ``apply_liveness`` can still change this row.

    ``apply_liveness`` only revives an open ``unknown`` row, closes a
    ``running`` one, or makes a row ``running`` when a live process matches
    it. A settled row (a verdict, a failure, or a closed ``unknown`` row)
    is only matched when the listing names its pid or label, or the stored
    row is still open, so a recent process sample can revive it. Without a
    listing (``hints is None``) there is nothing to do.
    """
    if hints is None:
        return False
    if not _settled(run):
        return True
    pids, labels = hints
    pid = run.get("pid")
    if pid is not None and pid in pids:
        return True
    label = run.get("label")
    if label and label in labels:
        return True
    existing = _stored_run(store, run.get("run_id"))
    if existing is not None and existing.get("status") in ("running", "unknown"):
        return True
    return False


def _stored_run(store, run_id):
    getter = getattr(store, "get_run", None)
    if not callable(getter):
        return None
    try:
        return getter(run_id)
    except Exception:
        return None


def _settled(run) -> bool:
    """True when this poll's file evidence already ends the run.

    A terminal status, or an ``unknown`` row with ``ended_at``, can only
    change through liveness, which :func:`_needs_liveness` checks against
    the listing and the stored row before it runs.
    """
    status = run.get("status")
    if status in _TERMINAL:
        return True
    return status == "unknown" and run.get("ended_at") is not None


def _queue_fold(store, pending, old_sid, new_sid, label):
    """Defer a twin fold until after the poll's usage flush.

    ``reassign_run`` extends a terminal destination's ``ended_at`` to its
    latest usage timestamp, so the rows it reads must be written first.
    """
    if pending is None:
        _reassign_twin(store, old_sid, new_sid, label)
        return
    pending["folds"].append((old_sid, new_sid, label))


def _apply_folds(store, folds):
    done = set()
    for old_sid, new_sid, label in folds:
        pair = (old_sid, new_sid, label)
        if pair in done:
            continue
        done.add(pair)
        _reassign_twin(store, old_sid, new_sid, label)


def _commit_cursors(store, cursors):
    for key, inode, offset, size, mtime in cursors:
        try:
            store.set_offset(key, inode, offset, size, mtime)
        except Exception:
            _fail("stream-index")


def _restore_cursors(store, restores):
    for key, inode, offset, size, mtime in restores:
        try:
            store.set_offset(key, inode, offset, size, mtime)
        except Exception:
            _fail("stream-cursor")


def _reassign_twin(store, old_sid, new_sid, label):
    """Fold a pre-generated run-folder row onto the stream's run.

    ``run_worker.py`` records a session id before pi starts; pi picks its
    own and the stream records it. The run-folder row and the launcher's
    ``pi/`` row are one launch, so a stored row keyed by the pre-generated
    id is moved onto the stream run instead of listed as a tokenless twin.
    The pair is remembered per store so a quiet run costs no query after
    the first fold; a fold that cannot run yet (the stream run has not been
    written) is retried on the next poll.
    """
    src = "pi:%s:%s" % (old_sid, label)
    dst = "pi:%s:%s" % (new_sid, label)
    try:
        done = store.__dict__.setdefault("_pi_reassigned", set())
    except AttributeError:
        done = None
    if done is not None and (src, dst) in done:
        return
    reassign = getattr(store, "reassign_run", None)
    if not callable(reassign):
        return
    get_run = getattr(store, "get_run", None)
    try:
        if callable(get_run) and get_run(src) is None:
            if done is not None:
                done.add((src, dst))
            return
        moved = reassign(src, dst)
    except Exception:
        _fail("reassign")
        return
    if moved and done is not None:
        done.add((src, dst))


def _first_file(pad, name):
    """``pad/name``, else the launcher's ``pad/pi/name`` (the split layout)."""
    path = os.path.join(pad, name)
    if os.path.isfile(path):
        return path
    alt = os.path.join(pad, DIRECTORY, name)
    return alt if os.path.isfile(alt) else path


def _apply_stream(store, run, pad, label, budget, per_file, now, pending=None):
    """Emit usage from the run's stream, byte-cursored per file.

    The stream is the authoritative ledger: ``launch_pi.py`` writes its
    ``generic.jsonl`` lines only after the child exits, so a launcher that
    dies first leaves the run tokenless. Reading the stream here fills
    that gap and backfills finished runs from offset 0 on the first poll.
    Usage rows carry the stream's session id (the launcher's lines do too),
    so a run with both readers upserts one row instead of two.

    ``_read_chunk`` commits the byte cursor itself, so its prior position is
    restored right after the read. Both cursor advances are then deferred to
    ``pending`` and committed only after the poll's usage flush succeeds, so
    a failed flush or a crash mid-poll re-reads the same bytes and upserts
    the same message ids instead of losing the usage.
    """
    path = _stream_file(pad, label)
    if not os.path.isfile(path):
        return budget
    key = "pi:stream:" + path
    prev = _offset(store, key)
    try:
        raw, budget, started = _grok._read_chunk(store, path, key, budget, per_file)
    except Exception:
        _fail("stream")
        return budget
    if not raw:
        return budget
    previous = (key, prev[0] if prev else 0, prev[1] if prev else 0, 0, 0)
    _restore_cursors(store, [previous])
    stream_sid = _stream_session(pad, label) or run.get("session_id")
    model = run.get("model")
    try:
        st = os.stat(path)
    except OSError:
        return budget
    index_key = "pi:stream-index:" + path
    rec = _offset(store, index_key)
    # A read that starts at byte 0 (first poll, or a cursor reset) numbers
    # its messages from 1 again, so a re-read upserts the same rows instead
    # of minting new ids and double counting the run.
    start_index = int(rec[1]) if rec and rec[0] == st.st_ino and not started else 0
    events = parse_stream(raw, stream_sid, default_model=model, start_index=start_index)
    for ev in events:
        row = {
            "ts": ev["ts"] if ev["ts"] is not None else now,
            "session_id": stream_sid,
            "run_id": run["run_id"],
            "model": ev["model"] or model,
            "tool": TOOL,
            "message_id": ev["message_id"],
            "input": ev["input"],
            "output": ev["output"],
            "cache_read": ev["cache_read"],
            "cache_write": ev["cache_write"],
            "cache_write_5m": ev["cache_write_5m"],
            "cache_write_1h": ev["cache_write_1h"],
            "reasoning": 0,
        }
        if pending is None:
            _add_usage(store, row)
        else:
            pending["usage"].append(row)
    base = 0 if started else (prev[1] if prev else 0)
    commits = [(key, st.st_ino, base + len(raw), st.st_size, st.st_mtime)]
    if events:
        commits.append((index_key, st.st_ino, start_index + len(events), 0, 0))
    if pending is None:
        _commit_cursors(store, commits)
    else:
        pending["commits"].extend(commits)
    return budget


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


def _add_usage(store, row):
    try:
        store.add_usage(row)
    except Exception:
        _fail("usage")


def _flush_usage(store, rows):
    """Write a poll's usage rows in one store call.

    ``add_usage_batch`` holds the store lock and rebuilds each hourly bucket
    once for the whole batch. The per-row path rebuilds the hour after every
    row, which is quadratic in a poll's new events: ~190 rows against a
    ~5,800-event hour cost 16 s of a 18 s poll. A store without the batch
    method falls back to the per-row call. Returns True when every row was
    written; a False result keeps the poll's cursors where they were.
    """
    if not rows:
        return True
    batch = getattr(store, "add_usage_batch", None)
    if callable(batch):
        try:
            batch(rows)
            return True
        except Exception:
            _fail("usage")
    ok = True
    for row in rows:
        try:
            store.add_usage(row)
        except Exception:
            _fail("usage")
            ok = False
    return ok


def _process_apply(store, run, processes):
    """Live pi process vouches the run is running; a gone one closes it."""
    try:
        _process.apply_liveness(store, run, NAME, processes)
    except Exception:
        _fail("liveness")


def _names_label(text, label) -> bool:
    """True when a ``label<TAB><label>`` line of a QUOTA marker names exactly ``label``.

    Exact on the whole label, so ``x`` never matches a marker for ``x2``.
    """
    if not isinstance(label, str):
        return False
    for raw in text.splitlines():
        parts = raw.strip().split("\t")
        if len(parts) == 2 and parts[0].strip() == "label" and parts[1].strip() == label:
            return True
    return False


def _quota(pad, label, out_path, err_path, sid) -> bool:
    """True when this run's quota marker or a quota log names its failure.

    The marker is ``QUOTA-<label>``, per label, so runs sharing a ``pi/``
    outdir each keep their own. The bare ``QUOTA`` file that launchers wrote
    before that name is still read, with the same text test, so a run started
    before the change is seen. Cached on (path, size, mtime_ns) so a quiet
    run does not re-open and regex-scan its logs every 2 s across 140+ live runs.
    """
    for quota_path in (os.path.join(pad, f"QUOTA-{label}"), os.path.join(pad, "QUOTA")):
        try:
            st = os.stat(quota_path)
            sig = (st.st_size, st.st_mtime_ns)
            hit = _quota_cache.get(quota_path)
            if hit is not None and hit[0] == sig:
                cached_text = hit[1]
            else:
                try:
                    with open(quota_path, "r", encoding="utf-8") as fh:
                        cached_text = fh.read(4096)
                except OSError:
                    cached_text = ""
                _quota_cache[quota_path] = (sig, cached_text)
            if not cached_text.strip() or _names_label(cached_text, label) or (isinstance(sid, str) and sid in cached_text):
                return True
        except OSError:
            pass
    for path in (err_path, out_path):
        if not path:
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        sig = (st.st_size, st.st_mtime_ns)
        hit = _quota_cache.get(path)
        if hit is not None and hit[0] == sig:
            if hit[1]:
                return True
            continue
        try:
            hit_result = file_shows_quota(path)
        except Exception:
            hit_result = False
        _quota_cache[path] = (sig, hit_result)
        if hit_result:
            return True
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


def write_split(usage, model):
    """``(cache_write_5m, cache_write_1h)`` for one pi usage block.

    pi reports ``cacheWrite1h`` beside ``cacheWrite``. When it is present
    the 1h part is that value (clamped to the total) and the 5m part is the
    remainder, so the two always sum to ``cacheWrite``. When it is absent
    on an Anthropic model every write is 5m (``(total, 0)``): pi's own
    ``usage.cost`` matched the 5m rate on those messages. Any other model
    keeps ``(None, None)``, so its pricing reads exactly as before.
    """
    total = _num(usage.get("cacheWrite"))
    raw = usage.get("cacheWrite1h")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        one = min(max(int(raw), 0), total)
        return total - one, one
    if _is_anthropic(model):
        return total, 0
    return None, None


def _is_anthropic(model):
    return "claude" in str(model or "").lower()


def parse_stream(raw, fallback_sid, default_model=None, start_index=0):
    """Assistant usage events from raw NDJSON bytes.

    Each ``message_end`` with an assistant message and a usage block is one
    event: ``input`` the cache miss, ``cacheRead`` the hit, ``cacheWrite``
    the write, ``output`` the completion, and ``cache_write_5m`` /
    ``cache_write_1h`` the write split (see :func:`write_split`).
    ``default_model`` is the launch model, used for the Anthropic rule
    when the message names none. ``message_id`` is
    ``<session>-<index>`` where index counts assistant messages in order;
    ``start_index`` continues that count when a stream is read in chunks,
    so the ids match the launcher's full-stream read.
    """
    events = []
    index = int(start_index or 0)
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
        five, one = write_split(usage, model or default_model)
        events.append(
            {
                "message_id": "%s-%d" % (sid, index),
                "ts": _epoch(obj.get("timestamp") if obj.get("timestamp") is not None else message.get("timestamp")),
                "model": model,
                "input": _num(usage.get("input")),
                "output": _num(usage.get("output")),
                "cache_read": _num(usage.get("cacheRead")),
                "cache_write": _num(usage.get("cacheWrite")),
                "cache_write_5m": five,
                "cache_write_1h": one,
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
