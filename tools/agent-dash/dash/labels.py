"""Canonical run labels: build them, check them.

A run label is ``<scope>-<mark>-<slug>[-rN]``: all lowercase, dashes
only. ``scope`` is ``ws<N>`` (a workstream) or ``hub`` (work not owned
by a workstream: dashboard tooling, cross-workstream ops). ``mark`` is
one of ``plan``, ``exec``, ``review``, ``coord``. Two older shapes are
still valid and strong: a task-lane label ``ws<N>-<plan task id>-...``
that :mod:`dash.taskmatch` maps to a task in that workstream's plan
(classified execution), and the structural review affixes from
``catalog/phases.json`` (id ``review-affix``). Fuzzy keyword matches
(``audit``, ``design``, ...) are never evidence for a new label.

:func:`make` builds a canonical label. :func:`check` returns a
structured verdict: ok with the phase and the strong rule that fixed
it (``convention`` | ``task`` | ``review-affix``), or a refusal with a
one-line fix naming the exact corrected label to use. A conventional
mark that disagrees with a plan-task match in the same label (for
example ``ws11-plan-pgload-hook`` where ``pgload`` is a task of that
workstream) is a warning, not a refusal: the mark wins, like the
classifier. ``hub`` scope has no plan ids, so only the mark form is
valid there.

Stdlib only at import time: the Claude Code label-guard hook imports
this module on every Bash call and must stay fast. Plan reads (a
bundle's ``plan.json`` for task ids, the resolved task map) and
``dash`` imports happen lazily inside functions, best effort.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

#: The four explicit marks, and the phase each one claims.
MARKS = ("plan", "exec", "review", "coord")

MARK_PHASE = {
    "plan": "planning",
    "exec": "execution",
    "review": "review",
    "coord": "coordination",
}

#: Scope: a workstream (``ws<N>``) or cross-workstream work (``hub``).
_SCOPE_RE = re.compile(r"^(ws[1-9][0-9]*|hub)$")

#: A trailing round suffix: ``-r1``, ``-r2``, ...
_ROUND_RE = re.compile(r"^(?P<body>.+)-r(?P<n>[1-9][0-9]*)$")

#: Strong evidence only. Mirrors the ``review-affix`` rule in
#: ``catalog/phases.json`` (a test pins them equal): an ``r-`` lead, a
#: ``-rev``/``-fix``/``-check`` tail, or a ``-rev-`` infix. Checked
#: before task mapping so a review pass over a task counts as review.
REVIEW_PATTERNS = (
    r"^r-",
    r"^rev-",
    r"^fix\b",
    r"-rev\d*$",
    r"-fix\d*$",
    r"-check\d*$",
    r"-rev-",
)

#: Review tails a family prefix must not swallow: a future
#: ``ws9-merge-5-rev1`` stays review even under a local family rule.
_REVIEW_TAIL_RE = re.compile(r"-(?:rev|fix|check)(?:\d+$|-|$)")

#: Opt-out for replaying old runs only. Callers (``report start``,
#: ``run_worker.py``, ``launch_grok.sh``, the label-guard hook)
#: downgrade a refusal to a stderr warning when this is ``1``.
LEGACY_ENV = "AGENT_DASH_ALLOW_LEGACY_LABELS"

_MAX_LABEL = 200


def legacy_allowed() -> bool:
    """True when the legacy-label escape hatch is set (replays only)."""
    return os.environ.get(LEGACY_ENV) == "1"


def normalize_slug(text: str) -> str:
    """Lowercase dash form: runs of other chars become one ``-``."""
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


def make(scope: str, mark: str, slug: str, round: int | str | None = None) -> str:
    """Build a canonical label. Raises :exc:`ValueError` when invalid."""
    scope = str(scope or "").lower()
    if not _SCOPE_RE.match(scope):
        raise ValueError(f"bad scope {scope!r}: use ws<N> or hub")
    mark = str(mark or "").lower()
    if mark not in MARK_PHASE:
        raise ValueError(f"bad mark {mark!r}: use one of {', '.join(MARKS)}")
    clean = normalize_slug(slug)
    if not clean:
        raise ValueError("bad slug: nothing left after normalising")
    label = f"{scope}-{mark}-{clean}"
    if round is not None:
        try:
            number = int(str(round).lstrip("rR"))
        except (TypeError, ValueError):
            raise ValueError(f"bad round {round!r}: use an integer")
        if number < 1:
            raise ValueError(f"bad round {round!r}: use an integer >= 1")
        label += f"-r{number}"
    if len(label) > _MAX_LABEL:
        raise ValueError(f"label too long ({len(label)} > {_MAX_LABEL})")
    return label


def _split_round(label: str) -> tuple[str, int | None]:
    match = _ROUND_RE.match(label)
    if not match:
        return label, None
    return match.group("body"), int(match.group("n"))


def parse(label: str) -> dict | None:
    """``{scope, mark, slug, round}`` for a canonical label, else None."""
    text = str(label or "")
    if not text or len(text) > _MAX_LABEL:
        return None
    body, number = _split_round(text)
    head, dash, rest = body.partition("-")
    if not dash or not rest:
        return None
    if not _SCOPE_RE.match(head):
        return None
    mark, dash2, slug = rest.partition("-")
    if not dash2 or not slug or mark not in MARK_PHASE:
        return None
    if text != text.lower() or re.search(r"[^a-z0-9-]", text):
        return None
    return {"scope": head, "mark": mark, "slug": slug, "round": number}


def _plan_ids_for_ws(ws: str) -> list | None:
    """Plan task ids for a workstream, or None when unreadable.

    Plain file read, no ``dash`` imports: ``<workstreams>/<WS>/plan.json``.
    """
    ident = str(ws or "").upper()
    if not re.fullmatch(r"(WS[1-9][0-9]*|HUB)", ident):
        return None
    if ident == "HUB":
        return []
    roots = []
    env = os.environ.get("AGENT_DASH_WORKSTREAMS")
    if env:
        # An explicit workstreams dir is authoritative: no fallback to
        # the live data root, so trials and tests stay hermetic.
        roots.append(Path(env).expanduser())
    else:
        roots.append(Path.home() / ".local" / "share" / "agent-dash" / "workstreams")
    for root in roots:
        plan = root / ident / "plan.json"
        try:
            data = json.loads(plan.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        try:
            from dash.taskmatch import plan_task_ids

            groups = data.get("dag", {}).get("groups")
            return [str(item) for item in plan_task_ids(groups)]
        except Exception:
            ids: list[str] = []
            groups = data.get("dag", {}).get("groups") if isinstance(data, dict) else None
            for group in groups or []:
                tasks = group.get("tasks") if isinstance(group, dict) else None
                for task in tasks or []:
                    if isinstance(task, dict) and task.get("id") not in (None, ""):
                        ids.append(str(task["id"]))
            return ids
    return None


def _task_hit(
    label: str, ws: str | None, plan_ids: list | tuple | None, taskmap: dict | None
) -> str | None:
    """Plan task id named by the label, or None. Never raises.

    The classifier's own rule (:func:`dash.phases.resolve_task`):
    taskmap keys, built-ins, plan-id substrings, dash/dot-insensitive.
    """
    ids = [str(item) for item in (plan_ids or []) if str(item)]
    if not ids or not ws:
        return None
    try:
        from dash.phases import resolve_task
    except Exception:
        return None
    try:
        return resolve_task(label, ws, taskmap, ids)
    except Exception:
        return None


def _load_taskmap() -> dict | None:
    """Resolved label-to-task map, or None when unreadable. Never raises."""
    try:
        from dash.taskmatch import load_taskmap

        table = load_taskmap()
    except Exception:
        return None
    return table if isinstance(table, dict) else None


def _task_slug_hit(slug_tokens: list[str], plan_ids: list | tuple | None) -> str | None:
    """Plan task id whose slug sits in the label tokens, or None.

    Conflict detection only, never an ok: ``ws11-plan-pgload-hook``
    names task ``WS11.pgload`` even though the classifier's substring
    rule does not fire through the ``plan`` mark. The slug is the task
    id minus its ``WS<N>.`` head, split on dashes, dots, and
    underscores; it must appear as one contiguous token run.
    """
    ids = [str(item) for item in (plan_ids or []) if str(item)]
    if not ids or not slug_tokens:
        return None
    best = None
    for ident in ids:
        head = re.sub(r"^ws[1-9][0-9]*[._-]?", "", ident.lower())
        parts = [piece for piece in re.split(r"[-_.]+", head) if piece]
        if not parts:
            continue
        width = len(parts)
        hit = any(
            slug_tokens[start : start + width] == parts
            for start in range(len(slug_tokens) - width + 1)
        )
        if hit and (best is None or len(ident) > len(best)):
            best = ident
    return best


def _review_hit(label: str) -> bool:
    for pattern in REVIEW_PATTERNS:
        try:
            if re.search(pattern, label, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _suggest_fix(scope: str | None, rest: str, number: int | None) -> str:
    """Exact corrected label: insert the ``plan`` mark, keep the rest.

    ``plan`` is the safe default; the message names the other marks so
    the launcher picks the right one.
    """
    tail = f"-r{number}" if number else ""
    rest = normalize_slug(rest)
    if scope and rest:
        return (
            f"use {scope}-plan-{rest}{tail} "
            f"(swap plan for exec, review, or coord when that fits better)"
        )
    if scope:
        return (
            f"use {scope}-plan-<slug>{tail}: "
            f"build it with: python3 agent_dash.py label make "
            f"--scope {scope} --mark plan --slug <slug>"
        )
    return (
        "build one with: python3 agent_dash.py label make "
        "--scope <wsN|hub> --mark <plan|exec|review|coord> --slug <slug>"
    )


def check(
    label: str | None,
    ws: str | None = None,
    plan_ids: list | tuple | None = None,
    taskmap: dict | None = None,
) -> dict:
    """Strict verdict for one label.

    Returns ``{"ok", "label", "phase", "rule", "warning", "fix",
    "scope", "mark", "task"}``. ``ok`` carries the phase and the
    strong rule that fixed it (``convention`` | ``task`` |
    ``review-affix``); refusal carries a one-line ``fix`` naming the
    exact corrected label. A conventional mark that disagrees with a
    plan-task match is ``ok`` with a ``warning``. When ``plan_ids`` is
    None the workstream's ``plan.json`` is read best effort; ``hub``
    scope has no plan ids.
    """
    text = str(label or "")
    verdict: dict = {
        "ok": False,
        "label": text,
        "phase": None,
        "rule": None,
        "warning": None,
        "fix": None,
        "scope": None,
        "mark": None,
        "task": None,
    }

    def refuse(reason: str, fix: str) -> dict:
        verdict["fix"] = f"{reason} {fix}"
        return verdict

    if not text:
        return refuse(
            "empty label.",
            "build one with: python3 agent_dash.py label make "
            "--scope <wsN|hub> --mark <plan|exec|review|coord> --slug <slug>",
        )
    if len(text) > _MAX_LABEL or "/" in text or "\\" in text or text.startswith(".") or "\n" in text:
        return refuse(
            f"bad label {text!r}: no /, \\, leading dot, or newline; max {_MAX_LABEL} chars.",
            _suggest_fix(None, "", None),
        )
    lowered = text.lower()
    body_l, number_l = _split_round(lowered)
    head_l, dash_l, rest_l = body_l.partition("-")
    scope: str | None = head_l if dash_l and rest_l and _SCOPE_RE.match(head_l) else None
    if scope is None:
        _body, number = _split_round(text)
        return refuse(
            f"bad scope in {text!r}: start with ws<N> or hub.",
            _suggest_fix(None, "", number),
        )
    verdict["scope"] = scope
    _body, number = _split_round(text)
    _head, _dash, rest = _body.partition("-")
    if ws is not None and scope.upper() != str(ws).upper():
        return refuse(
            f"scope {scope} does not match workstream {ws}.",
            _suggest_fix(str(ws).lower(), rest_l, number_l),
        )

    tokens = rest_l.split("-")
    mark: str | None = tokens[0] if tokens[0] in MARK_PHASE else None
    slug = "-".join(tokens[1:]) if mark else rest_l
    tail = f"-r{number_l}" if number_l else ""
    ws_id = scope.upper()
    clean = text == lowered and re.search(r"[^a-z0-9-]", text) is None

    if plan_ids is None and scope != "hub":
        try:
            plan_ids = _plan_ids_for_ws(ws_id)
        except Exception:
            plan_ids = None
    if taskmap is None and scope != "hub":
        taskmap = _load_taskmap()
    # A verified task lane keeps its documented spelling (uppercase task
    # ids like ``ws4-WS4.1-slug``): the plan match is the evidence.
    task = _task_hit(text, ws_id if scope != "hub" else None, plan_ids, taskmap)
    if task is not None:
        if mark:
            if not clean:
                fixed = f"{scope}-{mark}-{normalize_slug(slug)}{tail}"
                return refuse(
                    f"bad label {text!r}: lowercase dashes only.",
                    f"use {fixed}",
                )
            verdict.update(
                ok=True, phase=MARK_PHASE[mark], rule="convention", mark=mark
            )
            if task in (plan_ids or []) and MARK_PHASE[mark] != "execution":
                verdict["warning"] = (
                    f"warning: mark says {MARK_PHASE[mark]} but the label names "
                    f"plan task {task} (execution); the mark wins"
                )
            return verdict
        verdict.update(ok=True, phase="execution", rule="task")
        verdict["task"] = task
        return verdict

    slug_hit = (
        _task_slug_hit(slug.split("-"), plan_ids)
        if scope != "hub" and mark and slug
        else None
    )
    verdict["task"] = slug_hit

    if mark:
        if not slug:
            return refuse(
                f"bad label {text!r}: nothing after the mark.",
                f"use {scope}-{mark}-<slug>{tail}: "
                f"build it with: python3 agent_dash.py label make "
                f"--scope {scope} --mark {mark} --slug <slug>",
            )
        if not clean:
            fixed = f"{scope}-{mark}-{normalize_slug(slug)}{tail}"
            return refuse(
                f"bad label {text!r}: lowercase dashes only.",
                f"use {fixed}",
            )
        verdict.update(
            ok=True, phase=MARK_PHASE[mark], rule="convention", mark=mark
        )
        if (
            slug_hit is not None
            and slug_hit in (plan_ids or [])
            and MARK_PHASE[mark] != "execution"
        ):
            verdict["warning"] = (
                f"warning: mark says {MARK_PHASE[mark]} but the label names "
                f"plan task {slug_hit} (execution); the mark wins"
            )
        return verdict

    if scope == "hub":
        return refuse(
            f"bad label {text!r}: hub scope needs a mark.",
            f"use hub-plan-{rest_l}{tail} "
            f"(swap plan for exec, review, or coord when that fits better)",
        )

    if _review_hit(text):
        if not clean:
            return refuse(
                f"bad label {text!r}: lowercase dashes only.",
                f"use {normalize_slug(lowered)}",
            )
        verdict.update(ok=True, phase="review", rule="review-affix")
        return verdict

    if not clean:
        norm, norm_n = _split_round(normalize_slug(lowered))
        head2, dash2, rest2 = norm.partition("-")
        if _SCOPE_RE.match(head2) and rest2:
            first = rest2.split("-")[0]
            if first in MARK_PHASE:
                return refuse(
                    f"bad label {text!r}: lowercase dashes only.",
                    f"use {norm}-<slug>{f'-r{norm_n}' if norm_n else ''}: "
                    f"build it with: python3 agent_dash.py label make "
                    f"--scope {head2} --mark {first} --slug <slug>",
                )
            return refuse(
                f"bad label {text!r}: lowercase dashes only.",
                _suggest_fix(head2, rest2, norm_n),
            )
        return refuse(
            f"bad label {text!r}: lowercase dashes only.",
            _suggest_fix(None, "", None),
        )

    return refuse(
        f"bad label {text!r}: no plan, exec, review, or coord mark, "
        f"no review affix, and no plan task match.",
        _suggest_fix(scope, rest_l, number_l),
    )
