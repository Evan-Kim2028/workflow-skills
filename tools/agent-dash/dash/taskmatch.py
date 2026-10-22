"""One run-to-task rule shared by the page and the server.

The mapping used to live only in JS (the progress map in
``static/index.html`` and the plan view in ``static/plan.js``, which had
already drifted apart on the WS2/WS7 built-ins). This module is the
canonical rule: the server uses it for ``GET /api/replay`` and
``tests/test_page_rules.py`` pins both JS copies to it on sample labels.

Order per (label, workstream):
1. exact label key in the resolved task map,
2. ``/regex/`` keys in the map, case-insensitive, JS ``RegExp`` semantics,
3. WS1/WS2/WS7 built-ins (the page's matchers for those ids),
4. plan task-id substring with word boundaries (longest wins).

Only labels are read, never prompts or argv.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

_WS1_A = re.compile(r"(?:^|[^a-z0-9])ws1[-._]?(\d+[a-z]?)", re.IGNORECASE)
_WS1_B = re.compile(r"WS1\.(\d+[a-z]?)")
_WS2_A = re.compile(r"WS2\.([0-9]+[a-z0-9]*|[A-Z][0-9]*)", re.IGNORECASE)
_WS2_B = re.compile(r"ws2[-._]([0-9]+[a-z0-9]*)", re.IGNORECASE)
_WS7_GATE = re.compile(r"\b([PS]0-\d+)\b", re.IGNORECASE)
_WS7_A = re.compile(r"WS7\.([0-9a-z]+)", re.IGNORECASE)
_WS7_B = re.compile(r"ws7[-._]([0-9a-z]+)", re.IGNORECASE)
_LEAD_LETTER = re.compile(r"^[a-z]", re.IGNORECASE)
_BOUND = re.compile(r"[a-z0-9]")

_FAMILIES = ("opus", "sonnet", "haiku", "muse", "glm", "grok")

#: Compiled ``/regex/`` rule bodies keyed by pattern text. A taskmap can hold
#: well over Python's 512-entry ``re`` cache, so compiling per call thrashes
#: it; this keeps one compiled object per distinct pattern (``None`` for a
#: pattern that does not compile). Bounded: an edited taskmap adds patterns.
_RULE_RX: dict = {}
_RULE_RX_LOCK = threading.Lock()
_RULE_RX_LIMIT = 4096

#: Plan-id index per distinct ``plan_ids`` tuple: ``(text, lowered)`` pairs in
#: plan order with empty ids dropped, so the substring scan lowercases once.
_PLAN_INDEX: dict = {}
_PLAN_INDEX_LIMIT = 256


def load_taskmap(root: Path | None = None) -> dict:
    """The resolved run-label to task-id map (local override wins)."""
    from dash.locate import layered_path

    base = root if root is not None else Path(__file__).resolve().parents[1] / "static"
    found = layered_path("taskmap", "AGENT_DASH_TASKMAP", base)
    if found is None or not found.is_file():
        return {}
    try:
        data = __import__("json").loads(found.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def plan_task_ids(groups) -> list[str]:
    """Every task id in a plan body, as strings."""
    out = []
    for group in groups or []:
        tasks = group.get("tasks") if isinstance(group, dict) else None
        for task in tasks or []:
            if isinstance(task, dict) and task.get("id") not in (None, ""):
                out.append(str(task["id"]))
    return out


def task_of(label: str | None, ws: str | None, taskmap: dict | None = None, plan_ids: list[str] | tuple = ()) -> str | None:
    """Map a run label to a plan task id. Mirrors the page's ``taskOf``.

    The taskmap, built-in and plan-substring rules run first. A hit that
    names a task in the plan is kept. Otherwise the canonical-label rule
    (:func:`_canonical_label_task`) gets a chance, and the first hit
    stands when it finds nothing.
    """
    hit = _base_task(label, ws, taskmap, plan_ids)
    if hit is not None and (not plan_ids or hit in plan_ids):
        return hit
    return _canonical_label_task(label, ws, plan_ids) or hit


_CANONICAL_LABEL = re.compile(r"^ws(\d+)-(?:plan|exec|review|coord)-(.+?)(?:-r\d+)?$", re.IGNORECASE)
_LABEL_MARK = re.compile(r"^ws\d+-(plan|exec|review|coord)-", re.IGNORECASE)


def label_mark(label: str | None) -> str | None:
    """The mark (``plan``, ``exec``, ``review`` or ``coord``) of a canonical label.

    None for any label without a canonical ``ws<N>-<mark>-`` prefix, so
    unlabelled and hand-named runs carry no mark.
    """
    match = _LABEL_MARK.match(label or "")
    return match.group(1).lower() if match else None


def _canonical_label_task(label: str | None, ws: str | None, plan_ids: list[str] | tuple = ()) -> str | None:
    """The task a canonical ``ws<N>-<mark>-<slug>[-rK]`` label names.

    The slug maps to ``WS<N>.<slug>`` when that task is in the plan, else
    to the longest dash-bounded prefix of the slug that is. The label's
    workstream must be ``ws`` itself, so nothing maps across workstreams.
    """
    match = _CANONICAL_LABEL.match(label or "")
    if not match or not plan_ids:
        return None
    number, slug = match.group(1), match.group(2)
    if str(ws or "").upper() != f"WS{number}":
        return None
    prefix = f"WS{number}."
    by_lower = {}
    for ident in plan_ids:
        text = str(ident)
        if text.lower().startswith(prefix.lower()):
            by_lower.setdefault(text.lower(), text)
    parts = slug.split("-")
    for cut in range(len(parts), 0, -1):
        found = by_lower.get((prefix + "-".join(parts[:cut])).lower())
        if found is not None:
            return found
    return None


def _base_task(label: str | None, ws: str | None, taskmap: dict | None = None, plan_ids: list[str] | tuple = ()) -> str | None:
    """The taskmap, built-in and plan-substring rules, before the canonical-label rule."""
    lab = label or ""
    table = (taskmap or {}).get(ws) if isinstance(taskmap, dict) else None
    table = table if isinstance(table, dict) else {}
    if lab and lab in table:
        return table[lab]
    for key, value in table.items():
        if not isinstance(key, str) or len(key) < 3 or not key.startswith("/"):
            continue
        rule = _rule_regex(key[1:-1])
        if rule is not None and rule.search(lab):
            return value
    if ws == "WS1":
        match = _WS1_A.search(lab) or _WS1_B.search(lab)
        if match:
            return match.group(1).lower()
    if ws == "WS2":
        match = _WS2_A.search(lab) or _WS2_B.search(lab)
        if match:
            piece = match.group(1)
            return "WS2." + (piece.upper() if _LEAD_LETTER.match(piece) else piece)
    if ws == "WS7":
        match = _WS7_GATE.search(lab)
        if match:
            return match.group(1).upper()
        match = _WS7_A.search(lab) or _WS7_B.search(lab)
        if match:
            return "WS7." + match.group(1).upper()
    if plan_ids:
        lower = lab.lower()
        best = None
        for text, low in _plan_index(plan_ids):
            start = lower.find(low)
            if start < 0:
                continue
            before = lower[start - 1] if start > 0 else ""
            after = lower[start + len(text)] if start + len(text) < len(lower) else ""
            if (before == "" or not _BOUND.match(before)) and (after == "" or not _BOUND.match(after)):
                if best is None or len(text) > len(best):
                    best = text
        if best is not None:
            return best
    return None


def _rule_regex(pattern: str):
    """The compiled ``/regex/`` body, compiled once per pattern text.

    ``None`` when the pattern does not compile (the rule is skipped, as
    the ``re.error`` handling always did). Case-insensitive, JS-like.
    """
    with _RULE_RX_LOCK:
        if pattern in _RULE_RX:
            return _RULE_RX[pattern]
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error:
        compiled = None
    with _RULE_RX_LOCK:
        if len(_RULE_RX) >= _RULE_RX_LIMIT:
            _RULE_RX.clear()
        _RULE_RX[pattern] = compiled
    return compiled


def _plan_index(plan_ids) -> tuple:
    """``(text, lowered)`` for each non-empty plan id, cached per id tuple."""
    try:
        key = tuple(plan_ids)
        hit = _PLAN_INDEX.get(key)
    except TypeError:
        key, hit = None, None
    if hit is not None:
        return hit
    texts = (str(ident) for ident in plan_ids)
    index = tuple((text, text.lower()) for text in texts if text)
    if key is not None:
        with _RULE_RX_LOCK:
            if len(_PLAN_INDEX) >= _PLAN_INDEX_LIMIT:
                _PLAN_INDEX.clear()
            _PLAN_INDEX[key] = index
    return index


def task_of_verified(label: str | None, ws: str | None, taskmap: dict | None = None, plan_ids: list[str] | tuple = ()) -> str | None:
    """``task_of`` with the phantom live-mapping fix.

    A mapped task id that is not in the workstream's plan (for example
    the WS7 built-in mapping ``ws7-ops-gold-rebuild`` to ``WS7.OPS``,
    which names no task on the map) drops to unmapped instead of
    claiming a chip that does not exist. With no plan ids to check
    against, verification is impossible and the raw mapping stands.
    """
    tid = task_of(label, ws, taskmap, plan_ids)
    if tid is None or not plan_ids:
        return tid
    return tid if tid in set(plan_ids) else None


def run_family(run: dict) -> str:
    """Model family for replay pulses: tier when known, else the source tool."""
    tier = str(run.get("tier") or "").lower()
    if tier in _FAMILIES:
        return tier
    source = str(run.get("source") or "")
    if source in ("muse", "grok", "glm"):
        return source
    if source.startswith("claude"):
        return "other"
    return "other"
