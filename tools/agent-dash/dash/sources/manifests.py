"""Scratchpad manifest source.

Scans ``<scratch-root>/<encoded-cwd>/<session>/scratchpad``. A root that is
already the encoded-cwd directory counts too: a uuid child with ``scratchpad/``
is a session pad. Manifest rows become runs. Stdout, stderr, and report files are
opened only by constructed name (``out-<label>.md``, ``err-<label>.log``,
``report-<label>.md``) inside ``muse/``, ``glm/``, ``grok/``, and ``cursor/``.
``exec/`` has no manifest; each ``out-`` / ``err-`` / ``report-`` file there
becomes a ``shell`` run. Prompt files and anything nested under worktrees are
not opened.

``cursor-sessions.tsv`` is source ``grok`` (cursor-agent). Its run id third
component is ``cursor/<label>`` so it does not collide with ``grok-sessions.tsv``.

Status uses file evidence. ``store.live_labels()`` or ``store.process_samples()``
supplies the live-process bit when core has one. Without that, an empty out
file and a non-SIGTERM error stay ``unknown`` so process.py can decide.
``out`` growth is ``size >`` the offset stored on the previous poll. The
offset slot for these paths is the last observed size, not a read cursor;
verdict text is always read from the start of the file.

``cfg`` keys match ``claude.py``: ``roots.scratchpads`` / ``scratch_root`` (default none),
``read_budgets``, ``live_window_s``, ``dir_scan_s``, ``backfill_session``,
and ``process_ttl_s`` (default 30).
"""

from __future__ import annotations

import logging
import os
import re
import stat

NAME = "manifests"
INTERVAL_S = 2

FILE_BUDGET = 4 * 1024 * 1024
TICK_BUDGET = 8 * 1024 * 1024
LIVE_WINDOW_S = 36 * 3600
DIR_SCAN_S = 15
SCAN_CAP = 64 * 1024
ERR_TAIL = 8 * 1024
TSV_CAP = 1024 * 1024
PROCESS_TTL_S = 30

# filename, source, directory, default tier
_MANIFESTS = (
    ("muse-sessions.tsv", "muse", "muse", "muse"),
    ("glm-sessions.tsv", "glm", "glm", "glm"),
    ("grok-sessions.tsv", "grok", "grok", "grok"),
    ("cursor-sessions.tsv", "grok", "cursor", "grok"),
)
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_VERDICT_RE = re.compile(r"^(PASS|PARTIAL|BLOCKED|VERDICT: (\w+))")
_OUT_RE = re.compile(r"^out-(.+)\.md$")
_ERR_RE = re.compile(r"^err-(.+)\.log$")
_REPORT_RE = re.compile(r"^report-(.+)\.md$")
_CLOCK_RANK = {"mtime": 0, "manifest": 1, "muse_log": 2, "transcript": 3}

_log = logging.getLogger("agent_dash.manifests")

_scan_cache: dict = {}
_tsv_cache: dict = {}
_text_cache: dict = {}
_runs: dict = {}
_emit_sig: dict = {}


def reset_state() -> None:
    _scan_cache.clear()
    _tsv_cache.clear()
    _text_cache.clear()
    _runs.clear()
    _emit_sig.clear()


def tier_of(model: str | None) -> str | None:
    if not model or not isinstance(model, str):
        return None
    text = model.lower()
    for name in ("opus", "sonnet", "haiku", "muse", "glm", "grok"):
        if name in text:
            return name
    return None


def parse_manifest_text(text: str) -> list[dict]:
    """Parse a sessions TSV. A header row (non-numeric epoch) is skipped."""
    rows = []
    if not text:
        return rows
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        label = _safe_label(parts[0])
        if not label:
            continue
        try:
            started = float(parts[2].strip())
        except ValueError:
            continue
        sid = parts[1].strip()
        session_uuid = None if sid in {"", "-"} else sid
        model = parts[3].strip() if len(parts) > 3 and parts[3].strip() else None
        rows.append(
            {
                "label": label,
                "session_uuid": session_uuid,
                "started_at": started,
                "model": model,
            }
        )
    return rows


def verdict_of(text: str) -> str | None:
    """First line matching the contract verdict pattern."""
    if not text:
        return None
    for line in text.splitlines():
        found = _verdict_line(line)
        if found:
            return found
    return None


def classify_status(
    *,
    out_bytes: int,
    prev_out_bytes: int | None,
    err_has_sigterm: bool,
    err_nonempty: bool,
    label_alive: bool | None,
    err_known: bool = True,
) -> str:
    """Map file evidence and an optional process bit to a run status.

    ``label_alive`` is None when the store has no process sample to read.
    ``prev_out_bytes`` is None the first time the out file is seen.
    """
    grew = prev_out_bytes is not None and out_bytes > prev_out_bytes
    if grew or label_alive is True:
        return "running"
    if not err_known:
        return "unknown"
    if err_has_sigterm:
        return "killed"
    if out_bytes == 0 and err_nonempty and label_alive is False:
        return "died_early"
    if label_alive is None and out_bytes == 0:
        return "unknown"
    return "done"


def poll(store, cfg, now: float) -> None:
    try:
        if not isinstance(cfg, dict):
            cfg = {}
        now = float(now)
        per_file, per_tick = _budgets(cfg)
        window = float(cfg.get("live_window_s", LIVE_WINDOW_S))
        scan_s = float(cfg.get("dir_scan_s", DIR_SCAN_S))
        ttl = float(cfg.get("process_ttl_s", PROCESS_TTL_S))
        backfill = _backfill(cfg)
        left = per_tick
        live = _live_labels(store, now, ttl)
        pads = []
        for root in _scratch_roots(cfg):
            pads.extend(_scratchpads(root, now, backfill, scan_s))
        pending = []
        for parent, scratch in pads:
            for filename, source, directory, default_tier in _MANIFESTS:
                path = os.path.join(scratch, filename)
                if not _is_file(path):
                    continue
                try:
                    rows, left = _load_tsv(path, left)
                except OSError as exc:
                    _log.warning("manifests tsv %s: %s", filename, type(exc).__name__)
                    continue
                for row in rows:
                    pending.append((parent, scratch, source, directory, default_tier, row))
        pending.sort(key=lambda item: -_out_mtime(item[1], item[3], item[5]["label"]))
        for parent, scratch, source, directory, default_tier, row in pending:
            try:
                left = _emit_manifest(
                    store,
                    parent,
                    scratch,
                    source,
                    directory,
                    default_tier,
                    row,
                    now,
                    left,
                    per_file,
                    live,
                )
            except Exception as exc:
                _log.warning("manifests row: %s", type(exc).__name__)
        for parent, scratch in pads:
            try:
                left = _emit_exec(
                    store, parent, scratch, now, window, backfill, left, per_file, live
                )
            except Exception as exc:
                _log.warning("manifests exec: %s", type(exc).__name__)
    except Exception as exc:
        _log.warning("manifests poll: %s", type(exc).__name__)


def _emit_manifest(
    store, parent, scratch, source, directory, default_tier, row, now, left, per_file, live
) -> int:
    label = row["label"]
    base = os.path.join(scratch, directory)
    out_path = os.path.join(base, f"out-{label}.md")
    err_path = os.path.join(base, f"err-{label}.log")
    report_path = os.path.join(base, f"report-{label}.md")
    run_id = _run_id(source, directory, row["session_uuid"], parent, label, row["started_at"])
    model = row["model"]
    tier = tier_of(model) or default_tier
    left = _emit_files(
        store,
        run_id=run_id,
        source=source,
        tier=tier,
        model=model,
        label=label,
        session_id=row["session_uuid"],
        parent=parent,
        started_at=row["started_at"],
        clock="manifest",
        out_path=out_path,
        err_path=err_path,
        report_path=        report_path,
        left=left,
        per_file=per_file,
        live=live,
    )
    return left


def _emit_exec(store, parent, scratch, now, window, backfill, left, per_file, live) -> int:
    exec_dir = os.path.join(scratch, "exec")
    try:
        entries = list(os.scandir(exec_dir))
    except OSError:
        return left
    grouped: dict = {}
    for ent in entries:
        try:
            if not ent.is_file(follow_symlinks=False):
                continue
            st = ent.stat(follow_symlinks=False)
        except OSError:
            continue
        kind, label = _exec_name(ent.name)
        if not kind or not label:
            continue
        bucket = grouped.setdefault(label, {})
        bucket[kind] = ent.path
        bucket.setdefault("mtimes", []).append(st.st_mtime)
    for label, bucket in grouped.items():
        if not backfill:
            newest = max(bucket.get("mtimes") or [0])
            if newest < now - window:
                continue
        mtimes = bucket.get("mtimes") or []
        started = min(mtimes) if mtimes else now
        try:
            left = _emit_files(
                store,
                run_id=f"shell:{parent}:{label}",
                source="shell",
                tier=None,
                model=None,
                label=label,
                session_id=None,
                parent=parent,
                started_at=started,
                clock="mtime",
                out_path=bucket.get("out"),
                err_path=bucket.get("err"),
                report_path=bucket.get("report"),
                left=left,
                per_file=per_file,
                live=live,
            )
        except Exception as exc:
            _log.warning("manifests exec %s: %s", label, type(exc).__name__)
    return left


def _emit_files(
    store,
    *,
    run_id,
    source,
    tier,
    model,
    label,
    session_id,
    parent,
    started_at,
    clock,
    out_path,
    err_path,
    report_path,
    left,
    per_file,
    live,
) -> int:
    out_st = _stat(out_path) if out_path else None
    err_st = _stat(err_path) if err_path else None
    report_st = _stat(report_path) if report_path else None
    out_bytes = out_st.st_size if out_st else 0
    err_bytes = err_st.st_size if err_st else 0
    prev_out = _prev_size(store, out_path, out_st) if out_st else None

    verdict = None
    out_settled = out_st is None or out_bytes == 0
    if out_st is not None and out_bytes > 0:
        verdict, out_settled, left = _cached_verdict(out_path, out_st, left, per_file)
    if verdict is None and out_settled and report_st is not None and report_st.st_size > 0:
        report_verdict, report_settled, left = _cached_verdict(
            report_path, report_st, left, per_file
        )
        if report_settled or report_verdict:
            verdict = report_verdict

    if err_st is None or err_bytes == 0:
        err_sig, err_nonempty, err_known = False, False, True
    else:
        err_sig, err_nonempty, err_known, left = _cached_err(err_path, err_st, left, per_file)

    if live is None:
        alive = None
    else:
        alive = label in live
    status = classify_status(
        out_bytes=out_bytes,
        prev_out_bytes=prev_out,
        err_has_sigterm=err_sig,
        err_nonempty=err_nonempty,
        label_alive=alive,
        err_known=err_known,
    )
    ended = None
    if status in ("done", "killed", "died_early"):
        mtimes = [st.st_mtime for st in (out_st, err_st, report_st) if st is not None]
        if mtimes:
            ended = max(mtimes)
    patch = {
        "run_id": run_id,
        "source": source,
        "tier": tier,
        "model": model,
        "label": label,
        "session_id": session_id,
        "parent_session_id": parent,
        "status": status,
        "verdict": verdict,
        "started_at": started_at,
        "clock_source": clock,
        "out_bytes": out_bytes,
        "err_bytes": err_bytes,
        "pid": None,
        "out_path": out_path if out_st is not None else None,
    }
    clear = ()
    if status in ("running", "unknown"):
        clear = ("ended_at",)
    else:
        patch["ended_at"] = ended
    _remember_size(store, out_path, out_st)
    _remember_size(store, err_path, err_st)
    _upsert(store, patch, clear=clear)
    return left


def _cached_verdict(path, st, left, per_file):
    stamp = (st.st_ino, st.st_size, st.st_mtime_ns)
    cached = _text_cache.get(("v", path))
    if cached and cached[0] == stamp:
        return cached[1], True, left
    cap = min(SCAN_CAP, per_file)
    if left <= 0:
        return None, False, left
    try:
        data, n = _read_prefix(path, st.st_size, left, cap)
    except OSError as exc:
        _log.warning("manifests read: %s", type(exc).__name__)
        return None, False, left
    left -= n
    verdict, settled = _scan_verdict(data, st.st_size, cap)
    if settled:
        _text_cache[("v", path)] = (stamp, verdict)
    return verdict, settled, left


def _cached_err(path, st, left, per_file):
    stamp = (st.st_ino, st.st_size, st.st_mtime_ns)
    cached = _text_cache.get(("e", path))
    if cached and cached[0] == stamp:
        sig, nonempty = cached[1]
        return sig, nonempty, True, left
    if left <= 0:
        return False, False, False, left
    try:
        sig, nonempty, settled, n = _read_err(path, st.st_size, left, per_file)
    except OSError as exc:
        _log.warning("manifests err: %s", type(exc).__name__)
        return False, False, False, left
    left -= n
    if settled:
        _text_cache[("e", path)] = (stamp, (sig, nonempty))
    return sig, nonempty, settled, left


def _scan_verdict(data: bytes, size: int, cap: int):
    if not data and size > 0:
        return None, False
    saw_all = len(data) >= size
    hit_cap = len(data) >= cap and size > len(data)
    if saw_all or data.endswith(b"\n"):
        text = data.decode("utf-8", "replace")
    else:
        nl = data.rfind(b"\n")
        if nl < 0:
            if saw_all or hit_cap:
                text = data.decode("utf-8", "replace")
                return verdict_of(text), True
            return None, False
        text = data[:nl].decode("utf-8", "replace")
    found = verdict_of(text)
    if found:
        return found, True
    settled = saw_all or hit_cap or (data.endswith(b"\n") and saw_all)
    # A complete file, or a full scan window, is settled. A budget cut that
    # still saw a newline but not the rest of the file is not.
    if saw_all or hit_cap:
        settled = True
    elif data.endswith(b"\n") and len(data) < size:
        settled = False
    return found, settled


def _read_err(path, size, left, per_file):
    cap = min(SCAN_CAP, per_file, left)
    n = min(size, cap)
    with open(path, "rb") as fh:
        head = fh.read(n) if n else b""
        tail = b""
        remain = left - len(head)
        if size > len(head) and remain > 0:
            back = min(ERR_TAIL, size, remain)
            fh.seek(max(0, size - back))
            tail = fh.read(back)
    data = head + tail
    charged = len(head) + len(tail)
    sig = b"SIGTERM" in data
    nonempty = bool(data.strip())
    if len(head) >= size:
        settled = True
    elif tail and (len(head) >= cap or len(head) + len(tail) >= size):
        settled = True
    else:
        settled = False
    if sig:
        settled = True
    return sig, nonempty, settled, charged


def _read_prefix(path, size, left, cap):
    n = min(size, left, cap)
    if n <= 0:
        return b"", 0
    with open(path, "rb") as fh:
        data = fh.read(n)
    return data, len(data)


def _load_tsv(path, left):
    st = _stat(path)
    if st is None:
        return [], left
    stamp = (st.st_ino, st.st_size, st.st_mtime_ns)
    cached = _tsv_cache.get(path)
    if cached and cached[0] == stamp:
        return cached[1], left
    if left <= 0 or st.st_size <= 0:
        return [], left
    n = min(st.st_size, left, TSV_CAP)
    with open(path, "rb") as fh:
        data = fh.read(n)
    left -= len(data)
    if len(data) < st.st_size and not data.endswith(b"\n"):
        nl = data.rfind(b"\n")
        if nl < 0:
            return [], left
        data = data[: nl + 1]
        rows = parse_manifest_text(data.decode("utf-8", "replace"))
        return rows, left
    rows = parse_manifest_text(data.decode("utf-8", "replace"))
    if n >= st.st_size:
        _tsv_cache[path] = (stamp, rows)
    return rows, left


def _scratchpads(root, now, backfill, scan_s):
    key = (root, backfill or "")
    hit = _scan_cache.get(key)
    if hit and now - hit[0] < scan_s:
        return hit[1]
    found = []
    seen = set()

    def consider(path, name):
        if not _UUID.match(name):
            return
        if backfill and name != backfill:
            return
        scratch = os.path.join(path, "scratchpad")
        if not _is_dir(scratch):
            return
        try:
            real = os.path.realpath(scratch)
        except OSError:
            return
        if real in seen:
            return
        seen.add(real)
        found.append((name, scratch))

    try:
        encoded = list(os.scandir(root))
    except OSError:
        _scan_cache[key] = (now, found)
        return found
    for enc in encoded:
        try:
            if not enc.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        consider(enc.path, enc.name)
        try:
            sessions = list(os.scandir(enc.path))
        except OSError:
            continue
        for sess in sessions:
            try:
                if not sess.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            consider(sess.path, sess.name)
    _scan_cache[key] = (now, found)
    return found


def _live_labels(store, now, ttl):
    fn = getattr(store, "live_labels", None)
    if callable(fn):
        try:
            values = fn()
        except Exception as exc:
            _log.warning("manifests live_labels: %s", type(exc).__name__)
            return None
        if values is None:
            return None
        return {item for item in values if isinstance(item, str)}
    fn = getattr(store, "process_samples", None)
    if not callable(fn):
        return None
    try:
        rows = fn()
    except Exception as exc:
        _log.warning("manifests process_samples: %s", type(exc).__name__)
        return None
    if rows is None:
        return None
    labels = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = row.get("label")
        if not isinstance(label, str) or not label:
            continue
        ts = row.get("ts")
        if isinstance(ts, bool):
            continue
        if ts is None or (
            isinstance(ts, (int, float)) and now - float(ts) <= ttl
        ):
            labels.add(label)
    return labels


def _upsert(store, patch, clear=()) -> None:
    rid = patch["run_id"]
    sig = (
        patch.get("status"),
        patch.get("verdict"),
        patch.get("out_bytes"),
        patch.get("err_bytes"),
        patch.get("ended_at"),
        patch.get("model"),
        patch.get("label"),
        tuple(clear),
    )
    if _emit_sig.get(rid) == sig:
        return
    _emit_sig[rid] = sig
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
            if _CLOCK_RANK.get(str(value), 0) < _CLOCK_RANK.get(str(merged["clock_source"]), 0):
                continue
        merged[key] = value
    for key in clear:
        merged[key] = None
    merged["run_id"] = rid
    _runs[rid] = merged
    try:
        store.upsert_run(dict(merged))
    except Exception as exc:
        _log.warning("manifests run: %s", type(exc).__name__)


def _run_id(source, directory, session_uuid, parent, label, started) -> str:
    ident = session_uuid or parent
    tail = f"cursor/{label}" if directory == "cursor" else label
    if not session_uuid:
        tail = f"{tail}@{int(started)}"
    return f"{source}:{ident}:{tail}"


def _exec_name(name: str):
    for pattern, kind in ((_OUT_RE, "out"), (_ERR_RE, "err"), (_REPORT_RE, "report")):
        match = pattern.match(name)
        if match:
            return kind, _safe_label(match.group(1))
    return None, None


def _safe_label(value) -> str | None:
    if not isinstance(value, str):
        return None
    label = " ".join(value.split())
    if not label:
        return None
    if len(label) > 200:
        label = label[:200]
    if "/" in label or "\\" in label or label.startswith("."):
        return None
    return label


def _out_mtime(scratch, directory, label) -> float:
    path = os.path.join(scratch, directory, f"out-{label}.md")
    st = _stat(path)
    if st is None:
        return 0.0
    return st.st_mtime


def _prev_size(store, path, st):
    if st is None or not path:
        return None
    prev = _offset_of(store, path)
    if not prev or prev[0] != st.st_ino:
        return None
    return prev[1]


def _remember_size(store, path, st) -> None:
    if st is None or not path:
        return
    prev = _offset_of(store, path)
    if prev and prev[0] == st.st_ino and prev[1] == st.st_size:
        return
    try:
        store.set_offset(path, st.st_ino, st.st_size, st.st_size, st.st_mtime)
    except Exception as exc:
        _log.warning("manifests offset: %s", type(exc).__name__)


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


def _stat(path):
    try:
        return os.stat(path, follow_symlinks=False)
    except OSError:
        return None


def _is_file(path) -> bool:
    st = _stat(path)
    return st is not None and stat.S_ISREG(st.st_mode)


def _is_dir(path) -> bool:
    st = _stat(path)
    return st is not None and stat.S_ISDIR(st.st_mode)


def _budgets(cfg):
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


def _scratch_roots(cfg) -> list:
    from dash.locate import scratch_roots

    return scratch_roots(cfg)


def _scratch_root(cfg) -> str:
    found = _scratch_roots(cfg)
    return found[0] if found else ""


def _verdict_line(line: str):
    if line.startswith("\ufeff"):
        line = line.lstrip("\ufeff")
    match = _VERDICT_RE.match(line)
    if not match:
        return None
    return match.group(2) or match.group(1)
