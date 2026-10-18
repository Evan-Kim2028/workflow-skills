"""Project decisions and risks from the local hub dir, read-only.

The owner's hub keeps ``decisions.tsv`` (and optionally ``risks.tsv`` or
``risks.json``). The dashboard never mirrors, syncs, or writes them:
every read parses the hub files live (cached by file mtime), exposes
short fields only, and never opens a hub file for writing.

Column mapping is header-driven and tolerant: the real hub header is
``date id decision why status`` (tab-separated), but any of the aliases
below work, unknown columns are kept as short extras, and a missing
file means an empty list. Two cells never reach any payload:

- a cell longer than ``MAX_CELL`` chars (long free text stays in the hub);
- a cell that looks like a local file path (``reports/x.md``). URLs
  (``scheme://...``) and prose with spaces are kept.

Computed risks need no file at all: stale plan gates, pending approvals
past the doctor ``approval_days`` window, critical/warn doctor groups on
active workstreams, and planned workstreams blocked by an unfinished
dependency. Every served risk row carries
``{id, title, severity, source, ws, since}`` (file rows use source
``file`` and a null ``ws``/``since`` when the hub names none); every
computed title names the workstream before any task or gate id.
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from pathlib import Path

MAX_CELL = 400
MAX_FILE_BYTES = 1024 * 1024

# Statuses that close a decision. Anything else (including a missing
# status) reads as still open.
CLOSED_STATUSES = frozenset(
    {"accepted", "rejected", "superseded", "closed", "done", "dropped", "withdrawn"}
)

# Header aliases, lowercased. The first group wins on collisions.
_ID_KEYS = ("id", "decision_id", "risk_id", "key")
_TITLE_KEYS = ("title", "decision", "text", "name", "summary", "risk")
_STATUS_KEYS = ("status", "state")
_OWNER_KEYS = ("owner", "decided_by", "lead", "driver")
_DATE_KEYS = ("date", "decided", "decided_at", "created", "since")
_WS_KEYS = ("workstream", "ws", "workstreams")
_AREA_KEYS = ("area", "component", "scope")
_LINK_KEYS = ("link", "url", "ref", "adr")
_SEVERITY_KEYS = ("severity", "level")

_KNOWN = (
    ("id", _ID_KEYS),
    ("title", _TITLE_KEYS),
    ("status", _STATUS_KEYS),
    ("owner", _OWNER_KEYS),
    ("date", _DATE_KEYS),
    ("workstream", _WS_KEYS),
    ("area", _AREA_KEYS),
    ("link", _LINK_KEYS),
    ("severity", _SEVERITY_KEYS),
)

_PATH_EXTS = (".md", ".json", ".jsonl", ".tsv", ".csv", ".html", ".txt",
              ".py", ".js", ".yaml", ".yml", ".pdf")

_lock = threading.Lock()
# (resolved path, mtime_ns, size) -> parsed rows. Readers get the cached
# list itself: treat it as read-only.
_CACHE: dict = {}


def resolve_hub_dir(cfg) -> Path | None:
    """The hub dir from config, resolved like ``dash.sources.hub`` does.

    Reads ``cfg["hub_dir"]`` (the ``AGENT_DASH_HUB_DIR`` value after env
    merge). Empty, missing, or off-disk means None: no hub, empty lists.
    """
    raw = (cfg or {}).get("hub_dir")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    path = Path(os.path.expanduser(os.path.expandvars(text)))
    if not path.is_dir():
        return None
    return path


def load_decisions(hub_dir) -> list:
    """Parse ``<hub_dir>/decisions.tsv``. Missing file means []."""
    if hub_dir is None:
        return []
    return _load_cached(Path(hub_dir) / "decisions.tsv", _parse_tsv_rows)


def load_file_risks(hub_dir) -> list:
    """Parse ``<hub_dir>/risks.tsv`` plus ``<hub_dir>/risks.json``.

    Either file may be absent. TSV rows come first, then JSON rows.
    """
    if hub_dir is None:
        return []
    base = Path(hub_dir)
    rows = list(_load_cached(base / "risks.tsv", _parse_tsv_rows))
    rows.extend(_load_cached(base / "risks.json", _parse_json_rows))
    return [_normalize_risk(row) for row in rows]


def decisions_payload(hub_dir, now: float | None = None) -> dict:
    """``GET /api/decisions`` body: rows newest first, counts by status."""
    del now
    rows = sorted(
        load_decisions(hub_dir),
        key=lambda row: (row.get("date") or "", row.get("id") or ""),
        reverse=True,
    )
    counts: dict = {}
    for row in rows:
        status = row.get("status") or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return {
        "decisions": rows,
        "counts": counts,
        "source": {"present": hub_dir is not None and _has_decisions(hub_dir)},
    }


def decisions_summary(hub_dir) -> dict:
    """``decisions`` block for ``/api/state``: total plus still-open."""
    rows = load_decisions(hub_dir)
    total = len(rows)
    closed = sum(
        1 for row in rows if str(row.get("status") or "").strip().lower() in CLOSED_STATUSES
    )
    return {"total": total, "open": total - closed}


def risks_summary(store, cfg, now: float | None = None) -> dict:
    """``risks`` block for ``/api/state``: total plus counts by severity."""
    by: dict = {}
    total = 0
    for risk in risks_payload(store, cfg, now).get("risks") or []:
        severity = risk.get("severity") or "unknown"
        by[severity] = by.get(severity, 0) + 1
        total += 1
    return {"total": total, "by_severity": by}


def risks_payload(store, cfg, now: float | None = None) -> dict:
    """``GET /api/risks`` body: computed risks first, then hub file rows."""
    moment = float(now if now is not None else time.time())
    hub_dir = resolve_hub_dir(cfg)
    titles = _bundle_titles()
    computed = _computed_risks(store, cfg, moment, titles)
    try:
        filed = load_file_risks(hub_dir)
    except Exception:
        filed = []
    return {"risks": computed + filed}


def _has_decisions(hub_dir) -> bool:
    try:
        return (Path(hub_dir) / "decisions.tsv").is_file()
    except OSError:
        return False


def _load_cached(path: Path, parse):
    """Parse ``path`` with ``parse(text)``, cached by (mtime, size)."""
    try:
        stat = path.stat()
    except OSError:
        return []
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _lock:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit
    try:
        if stat.st_size > MAX_FILE_BYTES:
            return []
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return []
    try:
        rows = parse(text)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    with _lock:
        _CACHE[key] = rows
    return rows


def _parse_tsv_rows(text: str) -> list:
    """Header-driven TSV rows. Short cells only; paths never."""
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    header = None
    rows = []
    for raw in reader:
        if header is None:
            header = [(cell or "").strip().lower() for cell in raw]
            continue
        if not any((cell or "").strip() for cell in raw):
            continue
        rows.append(_map_row(header, list(raw)))
    return rows


def _map_row(header: list, cells: list) -> dict:
    """One TSV row: known aliases to fixed keys, the rest as extras."""
    by_key: dict = {}
    extras: dict = {}
    for index, name in enumerate(header):
        value = cells[index] if index < len(cells) else ""
        cleaned = _clean(value)
        if cleaned is None:
            continue
        slot = _slot(name)
        if slot is None:
            key = _extra_key(name)
            if key is not None and key not in by_key and key not in extras:
                extras[key] = cleaned
        elif slot not in by_key:
            by_key[slot] = cleaned
    row = {slot: by_key.get(slot) for slot, _keys in _KNOWN if slot in by_key}
    row.update(extras)
    return row


def _slot(name: str) -> str | None:
    for slot, keys in _KNOWN:
        if name in keys:
            return slot
    return None


def _extra_key(name: str) -> str | None:
    key = "".join(ch if ch.isalnum() else "_" for ch in name).strip("_")
    if not key or len(key) > 64:
        return None
    if _slot(key) is not None:
        return None
    return key


def _clean(value) -> str | None:
    """A cell value, or None when it must never reach a payload."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > MAX_CELL:
        return None
    if _looks_like_path(text):
        return None
    return text


def _looks_like_path(value: str) -> bool:
    """Local file paths, never URLs and never prose.

    Prose almost always holds a space, so any value with whitespace is
    kept. URLs carry ``://`` and are kept too (a ``link`` column holds
    them). What drops is spaceless ``dir/file`` or ``~/...`` values and
    bare ``*.md``-style filenames.
    """
    if "://" in value:
        return False
    if any(ch.isspace() for ch in value):
        return False
    lowered = value.lower()
    return (
        "/" in value
        or "\\" in value
        or value.startswith("~")
        or lowered.endswith(_PATH_EXTS)
    )


def _parse_json_rows(text: str) -> list:
    """A JSON list of objects (or ``{"risks": [...]}``), same row rule."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = data.get("risks")
    if not isinstance(data, list):
        return []
    rows = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        row: dict = {}
        extras: dict = {}
        for raw_key, value in entry.items():
            name = str(raw_key or "").strip().lower()
            if not name:
                continue
            cleaned = _clean(value)
            if cleaned is None:
                continue
            slot = _slot(name)
            if slot is None:
                key = _extra_key(name)
                if key is not None and key not in row and key not in extras:
                    extras[key] = cleaned
            elif slot not in row:
                row[slot] = cleaned
        row.update(extras)
        if row:
            rows.append(row)
    return rows


def _normalize_risk(row: dict) -> dict:
    """One hub file risk: fixed keys first, extras after, severity sane.

    Hub date columns land on ``date`` through the shared aliases; risks
    carry it as ``since`` (epoch when it parses, raw short text else).
    File rows gain the computed shape (``source`` ``file``, ``ws`` from
    the workstream column, ``since`` null when unknown) so one strip
    renderer serves both.
    """
    out = dict(row)
    severity = str(row.get("severity") or "info").strip().lower()
    out["severity"] = severity if severity in ("critical", "warn", "info") else "info"
    raw_since = row.get("since", row.get("date"))
    epoch = _parse_epoch(raw_since)
    if epoch is not None:
        out["since"] = epoch
        out.pop("date", None)
    elif "date" in out and "since" not in out:
        out["since"] = out.pop("date")
    else:
        out.setdefault("since", None)
    out.setdefault("source", "file")
    out["ws"] = row.get("workstream")
    return out


def _parse_epoch(value) -> float | None:
    """Epoch seconds from a number or an ISO date/datetime string."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        stamp = text.replace("Z", "+00:00")
        if len(stamp) == 10:
            stamp += "T00:00:00+00:00"
        from datetime import datetime

        moment = datetime.fromisoformat(stamp)
        return moment.timestamp()
    except ValueError:
        return None


def _bundle_titles() -> dict:
    try:
        from dash.bundles import list_bundles

        return {item["id"]: item.get("title") or "" for item in list_bundles() or []}
    except Exception:
        return {}


def _display(ws: str, titles: dict) -> str:
    title = titles.get(ws) or ""
    return f"{ws} {title}".strip() if title else ws


def _computed_risks(store, cfg, moment: float, titles: dict) -> list:
    """The four file-free risk sources, grouped by workstream."""
    try:
        from dash.workstates import all_bundle_meta, state_of
    except Exception:
        return []
    try:
        meta = all_bundle_meta() or {}
    except Exception:
        meta = {}
    hidden = {"archived", "dropped"}
    live = sorted(ws for ws, item in meta.items() if state_of(ws, meta) not in hidden)
    per_ws: dict = {ws: [] for ws in live}
    for ws in live:
        per_ws[ws].extend(_gate_risks(ws, meta, moment, titles))
    for ws in live:
        per_ws[ws].extend(_approval_risks(ws, meta, moment, cfg, titles))
    doctor_groups = _doctor_groups(store, moment)
    for ws in live:
        if state_of(ws, meta) != "active":
            continue
        per_ws[ws].extend(_doctor_risks(ws, doctor_groups, titles))
    for ws in live:
        if state_of(ws, meta) != "planned":
            continue
        per_ws[ws].extend(_dependency_risks(ws, meta, titles))
    return [risk for ws in sorted(per_ws) for risk in per_ws[ws]]


def _gate_risks(ws: str, meta: dict, moment: float, titles: dict) -> list:
    """One risk per stale plan gate (``ws gates`` freshness)."""
    try:
        from dash.bundles import resolve_plan
        from dash.orchestrate import stale_gates
    except Exception:
        return []
    try:
        _source, body = resolve_plan(ws)
    except Exception:
        return []
    if not isinstance(body, dict):
        return []
    try:
        gates = stale_gates(body, moment)
    except Exception:
        return []
    disp = _display(ws, titles)
    risks = []
    for gate in gates:
        if not isinstance(gate, dict) or not gate.get("stale"):
            continue
        gid = str(gate.get("id") or "gate")
        gtitle = gate.get("title")
        label = gid if not gtitle or gtitle == gid else f"{gid} ({gtitle})"
        risks.append(
            {
                "id": f"gates:{ws}:{gid}",
                "title": f"{disp}: stale gate {label}",
                "severity": "warn",
                "source": "gates",
                "ws": ws,
                "since": _parse_epoch(gate.get("confirmed_at")),
            }
        )
    return risks


def _approval_risks(ws: str, meta: dict, moment: float, cfg, titles: dict) -> list:
    """One risk per workstream whose pending approvals passed the window."""
    try:
        from dash.bundles import approvals_for, bundle_dir
    except Exception:
        return []
    try:
        record = approvals_for(ws)
    except Exception:
        return []
    if not isinstance(record, dict):
        return []
    pending = [
        entry
        for entry in (record.get("items") or [])
        if isinstance(entry, dict) and entry.get("status") == "pending"
    ]
    if not pending:
        return []
    try:
        path = bundle_dir(ws)
        mtime = (path / "approvals.json").stat().st_mtime if path is not None else None
    except OSError:
        mtime = None
    if mtime is None:
        return []
    limit_days = 3.0
    try:
        limit_days = float(((cfg or {}).get("doctor") or {}).get("approval_days", 3.0))
    except (TypeError, ValueError):
        pass
    if moment - mtime <= limit_days * 86400:
        return []
    days = (moment - mtime) / 86400
    count = len(pending)
    noun = "approval" if count == 1 else "approvals"
    return [
        {
            "id": f"approvals:{ws}",
            "title": (
                f"{_display(ws, titles)}: {count} {noun} pending "
                f"{days:.1f} days (limit {limit_days:g} days)"
            ),
            "severity": "warn",
            "source": "approvals",
            "ws": ws,
            "since": float(mtime),
        }
    ]


def _doctor_groups(store, moment: float) -> dict:
    """Latest persisted doctor problems, keyed by workstream."""
    try:
        payload = store.doctor_latest(moment)
    except Exception:
        return {}
    groups: dict = {}
    for problem in (payload or {}).get("problems") or []:
        if not isinstance(problem, dict):
            continue
        if problem.get("severity") not in ("critical", "warn"):
            continue
        ws = problem.get("ws")
        if not ws:
            continue
        groups.setdefault(ws, []).append(problem)
    return groups


def _doctor_risks(ws: str, groups: dict, titles: dict) -> list:
    """One risk per critical/warn doctor group on an active workstream."""
    disp = _display(ws, titles)
    risks = []
    for problem in groups.get(ws, []):
        severity = problem.get("severity")
        risks.append(
            {
                "id": str(problem.get("id") or f"doctor:{ws}:{problem.get('code') or 'group'}"),
                "title": f"{disp}: {problem.get('title') or problem.get('code') or 'doctor flag'}",
                "severity": "warn" if severity in ("critical", "warn") else "info",
                "source": "doctor",
                "ws": ws,
                "since": problem.get("first_seen"),
            }
        )
    return risks


def _dependency_risks(ws: str, meta: dict, titles: dict) -> list:
    """One risk per planned workstream blocked by an unfinished dependency."""
    try:
        from dash.workstates import state_of
    except Exception:
        return []
    depends = meta.get(ws, {}).get("depends_on") or []
    waiting = [
        dep for dep in depends
        if isinstance(dep, str) and state_of(dep, meta) not in ("done", "archived")
    ]
    if not waiting:
        return []
    clauses = []
    for dep in waiting:
        dep_state = state_of(dep, meta)
        clauses.append(dep if dep_state == "active" else f"{dep} ({dep_state})")
    return [
        {
            "id": f"dependency:{ws}",
            "title": f"{_display(ws, titles)}: waits on {', '.join(clauses)}",
            "severity": "info",
            "source": "dependency",
            "ws": ws,
            "since": meta.get(ws, {}).get("state_changed_at"),
        }
    ]
