"""GLM source for sessions launched through the grok CLI.

Manifest rows (``glm-sessions.tsv`` and ``grok-build-sessions.tsv``: label,
uuid, started epoch, model, then tier and harness) become
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

The dashboard tier is the registry tier name the launcher recorded, never a
guess from the model id: ``run_worker.py`` writes it as the fifth manifest
cell and in ``<label>.meta.json``, so a DeepSeek Flash row reads tier
``deepseek-flash`` and a GLM row ``glm`` or ``glm-flash``. Rows older than
that column fall back to a model-to-tier map. The harness is always ``grok``
(the Grok Build CLI), and the model is stored verbatim, so every model shows
up under its own name instead of folding into ``glm``/``other``.

DeepSeek V4.1 Flash (model id ``deepseek-flash``, run through the same CLI)
shares the ``usage.json`` shape: ``inputTokens`` includes
``cachedReadTokens`` (``totalTokens`` equals ``inputTokens`` plus
``outputTokens``), so the reader stores the uncached miss in ``input`` and
the hit in ``cache_read`` exactly like GLM. Note the CLI's ``-p
--output-format json`` final object uses the other convention: its
``input_tokens`` is already the miss and its ``total_tokens`` equals miss
plus ``cache_read_input_tokens`` plus ``output_tokens``. ``reasoningTokens``
rides along separately; the catalog bills it at no separate rate because the
``chat_completions`` output count already includes reasoning.
"""

import json
import logging
import os
import zlib
from datetime import datetime

from dash.liveness import file_shows_quota, read_pid_file
from dash.sources.process import apply_liveness, live_agent_processes

log = logging.getLogger("agent_dash.grok_cli")

NAME = "grok_cli"
INTERVAL_S = 2

DEFAULT_SESSIONS = os.path.expanduser("~/.grok/sessions")
DEFAULT_LOG = os.path.expanduser("~/.grok/logs/unified.jsonl")
PER_FILE_DEFAULT = 4 * 1024 * 1024
PER_TICK_DEFAULT = 8 * 1024 * 1024

TOOL = "glm"
#: Manifest names this source reads. Registry tiers launched through
#: ``run_worker.py`` under kind ``grok-build`` record their rows in
#: ``grok-build-sessions.tsv`` (the worker entry's own ``manifest`` name)
#: instead of ``glm-sessions.tsv``; both live in the pad or in a lead
#: child folder such as ``grokb/``, and both become ``glm`` runs.
MANIFESTS = ("glm-sessions.tsv", "grok-build-sessions.tsv")
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
    pads = {}
    for scratch in _scratch_roots(cfg):
        for filename in MANIFESTS:
            for pad, parent in _manifest_dirs(scratch, filename, only):
                path = os.path.join(pad, filename)
                text, budget = _read_small(path, budget, per_file)
                if text is None:
                    continue
                for row in _parse_manifest(text):
                    row["parent_session_id"] = parent
                    row["pad"] = pad
                    if _keep_latest(manifests, row):
                        pads[row["session_id"]] = pad

    latest = _latest_per_label(manifests)
    for row in manifests.values():
        _upsert_run(store, row, None, None, processes, pads.get(row["session_id"]), row["session_id"] in latest, now)

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

    _repair(store, now)


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
                "input": _uncached(_num(turn.get("inputTokens")), _num(turn.get("cachedReadTokens"))),
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
        "input": _uncached(_num(ctx.get("prompt_tokens")), _num(ctx.get("cached_prompt_tokens"))),
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


def _upsert_run(store, row, model, pid, processes=None, pad=None, is_latest=True, now=None):
    sid = row["session_id"]
    chosen = model or row.get("model") or None
    run = {
        "run_id": _run_id(row),
        "source": "glm",
        "tier": _tier_of(row, chosen),
        "harness": "grok",
        "session_id": sid,
    }
    if row.get("label"):
        run["label"] = row["label"]
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
    if is_latest and pad and row.get("label") and run.get("pid") is None:
        _adopt_pid_file(run, pad, row["label"])
    apply_liveness(store, run, "glm", processes)
    if is_latest and pad and row.get("label"):
        _apply_quota_closeout(run, pad, row["label"], now)
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


def _adopt_pid_file(run, pad, label) -> None:
    """Adopt the run dir's ``<label>.pid`` onto ``run`` (in place).

    Runs before ``apply_liveness`` so the authoritative pid joins the
    pid-leg match and the dead-pid close: a dead pid closes through the
    fast dead-pid path (with its grace and sample checks) instead of the
    6 h idle one. Missing files change nothing; failures never raise.
    """
    try:
        pid_paths, _err_paths, _out_paths = _run_log_files(pad, label)
    except Exception:
        return
    try:
        for path in pid_paths:
            candidate = read_pid_file(path)
            if candidate is None:
                continue
            run["pid"] = candidate
            return
    except Exception:
        return


def _apply_quota_closeout(run, pad, label, now=None) -> None:
    """Close a quota/rate-limit failure as died_early/QUOTA (in place).

    Runs after ``apply_liveness`` and wins over it: a quota hit in the
    run dir's err/out logs ends the run even while a label match still
    reads running, so quota failures never linger as running or inherit
    a shared out file's PASS. The session died without reporting a
    verdict, so it reads died (red): a close-out BLOCKED would paint a
    dead session blocked-orange. Missing files change nothing; failures
    never raise.
    """
    try:
        _pid_paths, err_paths, out_paths = _run_log_files(pad, label)
    except Exception:
        return
    quota_paths = []
    try:
        for path in list(err_paths) + list(out_paths):
            try:
                if file_shows_quota(path):
                    quota_paths.append(path)
            except Exception:
                continue
    except Exception:
        quota_paths = []
    if not quota_paths:
        return
    run["status"] = "died_early"
    run["verdict"] = "QUOTA"
    try:
        stamp = _quota_ended_at(quota_paths, now if now is not None else __import__("time").time())
    except Exception:
        stamp = None
    if stamp is not None:
        run["ended_at"] = stamp
    run["pid"] = None


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
    """Rows from ``glm-sessions.tsv`` / ``grok-build-sessions.tsv``: label, uuid, started, model, then tier.

    The fifth cell is the registry tier the launch recorded. Older rows stop
    at the model; they get their tier from the meta file or the model map.
    """
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
        tier = parts[4].strip() if len(parts) > 4 and parts[4].strip() else None
        rows.append(
            {
                "label": label,
                "session_id": sid,
                "started_at": started,
                "model": model or None,
                "tier": tier,
            }
        )
    return rows


#: Fallback tier per model id, for rows written before the tier column.
MODEL_TIERS = {
    "deepseek-flash": "deepseek-flash",
    "glm-5.3": "glm",
    "glm-5.3-flash": "glm-flash",
    "reflection-beam": "beam",
}


def _tier_of(row, model=None):
    """Registry tier for a run: recorded tier, meta file, then the model map.

    ``row["tier"]`` comes from the manifest's fifth cell and
    ``row["meta_tier"]`` from ``<label>.meta.json``; both are the registry
    name the launch used. The map is the only guess, and it never returns
    ``glm`` for a DeepSeek model.
    """
    for value in (row.get("tier"), row.get("meta_tier")):
        text = str(value or "").strip()
        if text:
            return text
    key = str(model or row.get("model") or "").strip().lower()
    if key in MODEL_TIERS:
        return MODEL_TIERS[key]
    for known, tier in MODEL_TIERS.items():
        if key.endswith("/" + known):
            return tier
    if "deepseek" in key:
        return "deepseek-flash"
    if "beam" in key:
        return "beam"
    if "glm" in key:
        return "glm-flash" if "flash" in key else "glm"
    return TOOL


def _read_meta_tier(pad, label):
    """The tier in ``<label>.meta.json`` beside the manifest, or None."""
    if not pad or not label:
        return None
    path = os.path.join(pad, "%s.meta.json" % label)
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read(64 * 1024).decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    tier = data.get("tier") if isinstance(data, dict) else None
    return str(tier).strip() or None if tier is not None else None


def _keep_latest(manifests, row):
    """Keep the newest row per session id. True when ``row`` was kept."""
    prev = manifests.get(row["session_id"])
    if prev is None or (row.get("started_at") or -1) >= (prev.get("started_at") or -1):
        manifests[row["session_id"]] = row
        return True
    return False


def _latest_per_label(manifests) -> set:
    """Session ids of the newest manifest row per label.

    Retries reuse the label under a new session id while the TSV keeps
    every attempt. Pid files and err logs are per label, so they name the
    newest attempt only: quota and pid evidence applies to it alone.
    """
    best: dict = {}
    for sid, row in (manifests or {}).items():
        label = (row.get("label") or "").strip()
        if not label:
            continue
        started = row.get("started_at")
        started = float(started) if isinstance(started, (int, float)) and not isinstance(started, bool) else -1.0
        prev = best.get(label)
        if prev is None or started >= prev[0]:
            best[label] = (started, sid)
    return {sid for _, sid in best.values()}


def _run_log_files(pad, label):
    """Candidate ``(<label>.pid``, ``err-<label>.log``, ``out-<label>.md``) paths.

    ``run_worker.py`` writes the pid file and the logs beside the manifest
    TSV: directly beside it in a flat run folder, or under the ``glm/``
    tool folder in a session pad. Every candidate is room-checked by the
    caller; missing files simply miss.
    """
    pid_paths, err_paths, out_paths = [], [], []
    if pad and isinstance(pad, str) and label:
        for directory in (pad, os.path.join(pad, "glm")):
            pid_paths.append(os.path.join(directory, "%s.pid" % label))
            err_paths.append(os.path.join(directory, "err-%s.log" % label))
            out_paths.append(os.path.join(directory, "out-%s.md" % label))
    return pid_paths, err_paths, out_paths


def _quota_ended_at(paths, now):
    """Newest mtime of the quota-hit logs, or ``now`` when unreadable."""
    latest = None
    for path in paths or ():
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if latest is None or mtime > latest:
            latest = mtime
    return latest if latest is not None else now


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
    A manifest nested one level under the scratchpad (a lead child folder
    such as ``runs/<tsv>``) is listed too, with the session as its parent.
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
    from dash.rundirs import scan_roots

    return scan_roots(cfg)


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


def _repair(store, now):
    """Fold GLM launcher rows into their session row.

    Unknown stores keep the old behavior.
    """
    repair = getattr(store, "merge_manifest_rows", None)
    if not callable(repair):
        return
    try:
        repair("glm", now=now)
    except Exception:
        _fail("glm-repair")


def _fail(where):
    global last_error
    last_error = where
    log.warning("%s failed", where)


def _uncached(total, cached):
    """The Grok Build CLI counts cached tokens inside its input; store the uncached part like every other source."""
    if total is None:
        return None
    return max(total - (cached or 0), 0)
