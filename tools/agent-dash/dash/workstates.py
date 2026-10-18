"""Workstream lifecycle states for the API layer.

Reads the additive ``bundle.json`` lifecycle fields (``state``,
``state_changed_at``, ``archived_at``, ``depends_on``, ``milestone``)
written by the lifecycle worker, plus the frozen ``summary.json`` cards.
Every field is optional: a bundle without them reads as ``active``.

The store never reads bundle files; the server and ``/api/state``
builder pass this module's maps in. Privacy: only bundle metadata and
summary stats cross this boundary — never a closing-report body. Report
references are basenames only, never absolute paths.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

#: Lifecycle states, in first-to-last order. ``planned`` has no tab yet,
#: ``paused`` keeps its tab muted, ``dropped`` leaves the views like
#: ``archived`` but reads as cancelled, not celebrated.
STATES = ("planned", "active", "paused", "done", "archived", "dropped")

#: States hidden from the default scopes (tabs, ``/api/workstreams``).
HIDDEN_STATES = ("archived", "dropped")

#: States the default ``/api/workstreams`` scope keeps.
DEFAULT_STATES = ("planned", "active", "paused", "done")

#: Keys a closing-report reference may hide under inside ``summary.json``.
_REPORT_KEYS = ("report", "closing_report", "report_path", "closing-report")

#: Closing-report filename ``ws archive`` requires at ``docs/`` in a bundle.
_CLOSING_REPORT_NAME = "closing-report.md"


def workstreams_root() -> Path:
    """Bundle directory, honoring the same override as the bundle reader."""
    from dash.locate import expand

    raw = os.environ.get("AGENT_DASH_WORKSTREAMS")
    if raw:
        return Path(expand(raw))
    return Path.home() / ".local" / "share" / "agent-dash" / "workstreams"


def normalize_state(value) -> str:
    """Bundle state with the contract default: absent or unknown reads ``active``."""
    return value if isinstance(value, str) and value in STATES else "active"


def bundle_meta(ws: str, root: Path | None = None) -> dict:
    """Lifecycle metadata for one bundle id, with defaults for missing fields."""
    meta = {"state": "active", "state_changed_at": None, "archived_at": None,
            "depends_on": [], "milestone": None}
    base = Path(root) if root is not None else workstreams_root()
    try:
        raw = (base / ws / "bundle.json").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return meta
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return meta
    if not isinstance(data, dict):
        return meta
    meta["state"] = normalize_state(data.get("state"))
    changed = data.get("state_changed_at")
    meta["state_changed_at"] = changed if isinstance(changed, (int, float)) and not isinstance(changed, bool) else None
    archived = data.get("archived_at")
    meta["archived_at"] = archived if isinstance(archived, (int, float)) and not isinstance(archived, bool) else None
    depends = data.get("depends_on")
    meta["depends_on"] = [item for item in depends if isinstance(item, str)] if isinstance(depends, list) else []
    milestone = data.get("milestone")
    meta["milestone"] = milestone if isinstance(milestone, str) else None
    return meta


def all_bundle_meta(root: Path | None = None) -> dict:
    """Lifecycle metadata for every bundle directory, keyed by id."""
    base = Path(root) if root is not None else workstreams_root()
    out = {}
    try:
        children = sorted(path for path in base.iterdir() if path.is_dir() and not path.name.startswith("."))
    except OSError:
        return out
    for child in children:
        if not (child / "bundle.json").is_file():
            continue
        out[child.name] = bundle_meta(child.name, base)
    return out


def hidden_ids(meta: dict | None = None, root: Path | None = None) -> set:
    """Ids in a default-hidden state (``archived`` or ``dropped``).

    Takes a metadata map when the caller already read one, else reads it.
    """
    table = meta if meta is not None else all_bundle_meta(root)
    return {ws for ws, item in table.items() if item.get("state") in HIDDEN_STATES}


def parse_state_param(value: str | None):
    """``?state=`` filter for ``/api/workstreams``.

    Returns None for the default scope (drop ``archived``/``dropped``),
    else the allowed state set (``all`` means every state). Raises
    ``ValueError("bad state")`` on an unknown name.
    """
    if value is None or value == "":
        return None
    if value == "all":
        return set(STATES)
    wanted = set(part.strip() for part in value.split(",") if part.strip())
    unknown = wanted - set(STATES)
    if not wanted or unknown:
        raise ValueError("bad state")
    return wanted


def state_of(ws: str, meta: dict | None) -> str:
    """One row's state from a metadata map, defaulting to ``active``."""
    if meta is not None:
        item = meta.get(ws)
        if isinstance(item, dict):
            return normalize_state(item.get("state"))
    return "active"


def filter_rows(rows: list, allowed: set | None, meta: dict | None = None) -> list:
    """Scope filter for workstream rows: default drops hidden states.

    ``allowed`` is None (default scope) or the ``parse_state_param`` set.
    The ``unassigned`` row is not a workstream and always stays.
    """
    keep = set(DEFAULT_STATES) if allowed is None else set(allowed)
    out = []
    for row in rows:
        ws = row.get("ws")
        if ws == "unassigned" or ws is None:
            out.append(row)
            continue
        if state_of(ws, meta) in keep:
            out.append(row)
    return out


def archived_aggregate(rows: list, archived_ws: set) -> dict:
    """Windowed plus lifetime sums over the hidden-state rows.

    Sums the per-workstream row dicts (same ``_sum_cells`` numbers the
    snapshot carries), so the aggregate agrees with the rows exactly.
    """
    picked = [row for row in rows if row.get("ws") in archived_ws]
    return {
        "tokens": sum(row.get("tokens", 0) for row in picked),
        "cost_usd": sum(row.get("cost_usd", 0.0) for row in picked),
        "tokens_all_time": sum(row.get("tokens_all_time", 0) for row in picked),
        "cost_usd_all_time": sum(row.get("cost_usd_all_time", 0.0) for row in picked),
        "workstreams": sorted(row.get("ws") for row in picked if row.get("ws")),
    }


def check_archived_identity(rows: list, unassigned: dict, archived: dict,
                             all_tokens: int, all_cost: float,
                             all_time_tokens: int, all_time_cost: float) -> list:
    """Mismatch lines unless visible rows + archived + unassigned = all.

    ``rows`` holds every per-workstream row (all states); the visible
    part is rows minus the archived aggregate's workstreams. Totals are
    billed scalars (token ints, cost floats). Cost compares within 1e-6
    like the other money checks; tokens compare exactly.
    """
    names = set(archived.get("workstreams") or [])
    visible = [row for row in rows if row.get("ws") not in names]
    problems = []
    if sum(row.get("tokens", 0) for row in visible) + archived.get("tokens", 0) + (unassigned.get("tokens", 0) or 0) != (all_tokens or 0):
        problems.append(
            "archived tokens: visible rows plus archived plus unassigned do not equal the all-scope total"
        )
    if abs(sum(row.get("cost_usd", 0.0) for row in visible) + archived.get("cost_usd", 0.0) + (unassigned.get("cost_usd", 0.0) or 0.0) - (all_cost or 0.0)) > 1e-6:
        problems.append(
            "archived cost_usd: visible rows plus archived plus unassigned do not equal the all-scope total"
        )
    if sum(row.get("tokens_all_time", 0) for row in visible) + archived.get("tokens_all_time", 0) + (unassigned.get("tokens_all_time", 0) or 0) != (all_time_tokens or 0):
        problems.append(
            "archived tokens_all_time: visible rows plus archived plus unassigned do not equal the all-scope total"
        )
    if abs(sum(row.get("cost_usd_all_time", 0.0) for row in visible) + archived.get("cost_usd_all_time", 0.0) + (unassigned.get("cost_usd_all_time", 0.0) or 0.0) - (all_time_cost or 0.0)) > 1e-6:
        problems.append(
            "archived cost_usd_all_time: visible rows plus archived plus unassigned do not equal the all-scope total"
        )
    return problems


def _summary_card(ws: str, base: Path) -> dict | None:
    """Frozen ``summary.json`` card contents, or None when there is none."""
    try:
        data = json.loads((base / ws / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _report_basename(ws: str, base: Path, summary: dict | None) -> str | None:
    """Closing-report reference as a basename only, never a path or body.

    Resolution order: the ``closing_report_name`` frozen in
    ``summary.json`` when that file still exists in the bundle's
    ``docs/``; then ``docs/closing-report.md`` (where ``ws archive``
    requires it); then the legacy ``summary.json`` report keys; then the
    newest ``reports/*closing*.md`` file. Absolute or directory-qualified
    values reduce to their final component so local paths never leak.
    """
    docs = base / ws / "docs"
    if isinstance(summary, dict):
        frozen = _basename(summary.get("closing_report_name"))
        if frozen and (docs / frozen).is_file():
            return frozen
    if (docs / _CLOSING_REPORT_NAME).is_file():
        return _CLOSING_REPORT_NAME
    if isinstance(summary, dict):
        for key in _REPORT_KEYS:
            name = _basename(summary.get(key))
            if name:
                return name
    try:
        reports = base / ws / "reports"
        candidates = sorted(reports.glob("*closing*.md")) if reports.is_dir() else []
    except OSError:
        candidates = []
    return candidates[-1].name if candidates else None


def _basename(value) -> str | None:
    """Final path component of a non-empty string, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    name = value.strip().split("/")[-1].split("\\")[-1]
    return name if name and name not in (".", "..") else None


def archive_cards(meta: dict | None = None, root: Path | None = None) -> list:
    """Frozen summary cards for archived workstreams, newest first.

    Each card is the ``summary.json`` contents plus ``id``, ``title``,
    ``archived_at``, and the closing-report basename (``report``). Cards
    sort by ``archived_at`` descending; bundles without ``summary.json``
    still list with their metadata so nothing archived goes silent.
    """
    from dash.bundles import _read_json  # local import: bundles never imports this module

    base = Path(root) if root is not None else workstreams_root()
    table = meta if meta is not None else all_bundle_meta(base)
    cards = []
    for ws, item in table.items():
        if item.get("state") != "archived":
            continue
        summary = _summary_card(ws, base)
        bundle = _read_json(base / ws / "bundle.json")
        title = ""
        if isinstance(bundle, dict) and isinstance(bundle.get("title"), str):
            title = bundle.get("title")
        card = dict(summary) if summary is not None else {}
        card.update({
            "id": ws,
            "title": card.get("title") if isinstance(card.get("title"), str) and card.get("title") else title,
            "archived_at": item.get("archived_at"),
            "report": _report_basename(ws, base, summary),
        })
        cards.append(card)
    cards.sort(key=lambda card: (card.get("archived_at") is not None, card.get("archived_at") or 0, card.get("id") or ""), reverse=True)
    return cards
