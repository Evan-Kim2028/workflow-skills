"""Live-lane grouping: one lane per distinct run, not per process.

A worker run spawns several processes sharing one label, prompt file,
or session id. Counting processes overcounts lanes. This module groups
the dashboard live-worker listing into distinct lanes so the CLI count
agrees with the dashboard Running now table.

The input row shape is the dashboard listing from
``dash.sources.process.live_worker_processes``: ``pid``, ``comm``,
``started_at``, ``label``, ``prompt_label``, ``session_id``, plus an
optional ``model``. No argv is read here. The grouping key prefers a
muse session uuid, then a bracket label, then a prompt-file label.
Rows with none of those are skipped because no run can be named.
Tiers cover muse, cursor-agent as grok, the grok CLI as glm or
deepseek, Claude processes as claude or an opus sonnet haiku tier from
the model, and anything else by model family with generic fallback.
"""

from __future__ import annotations

import math
import re
import time

_UUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
_UUID_RE = re.compile(r"^" + _UUID + r"$")


def _canon_comm(comm) -> str:
    """Map versioned muse comms onto one bucket."""
    if isinstance(comm, str) and (comm == "muse-bin" or comm.startswith("muse-bin-")):
        return "muse-bin"
    if isinstance(comm, str) and (comm == "muse" or comm.startswith("muse-")):
        return "muse"
    return comm if isinstance(comm, str) else ""


def _is_uuid(value) -> bool:
    """True when value is a session uuid string."""
    return isinstance(value, str) and _UUID_RE.match(value) is not None


def _model_tier(model) -> str | None:
    """Tier hint from a model id, or None when unknown."""
    if not isinstance(model, str) or not model:
        return None
    text = model.lower()
    for name in ("opus", "sonnet", "haiku"):
        if name in text:
            return name
    for name in ("muse", "glm", "grok", "deepseek"):
        if name in text:
            return name
    return None


def tier_for_process(row: dict) -> str:
    """Tier for one live-worker row.

    Muse comms read muse, cursor-agent reads grok, the grok CLI reads
    glm or deepseek from the model, Claude comms read the model tier or
    claude, and every other comm falls back to the model family with a
    generic default. The dashboard counts the same families on its
    tiles, so this mapping keeps the CLI lane tiers aligned with it.
    """
    row = row if isinstance(row, dict) else {}
    comm = _canon_comm(row.get("comm"))
    model = row.get("model")
    if comm in ("muse", "muse-bin"):
        return "muse"
    if comm == "cursor-agent":
        return "grok"
    if comm == "grok":
        hint = _model_tier(model)
        if hint == "deepseek":
            return "deepseek"
        return "glm"
    if comm == "claude":
        hint = _model_tier(model)
        if hint in ("opus", "sonnet", "haiku"):
            return hint
        return "claude"
    hint = _model_tier(model)
    if hint in ("muse", "glm", "grok", "opus", "sonnet", "haiku", "deepseek"):
        return hint
    return "generic"


def lane_key(row: dict) -> str | None:
    """Grouping key for one live-worker row, or None when unnameable.

    A muse session uuid names one exact run across relaunches sharing
    a prompt file. Otherwise the bracket label names the run, falling
    back to the prompt-file label for muse rows that carry only that.
    Rows with none of those carry no run identity and are skipped.
    """
    row = row if isinstance(row, dict) else {}
    session = row.get("session_id")
    if _is_uuid(session):
        return "session:" + str(session).lower()
    label = row.get("label")
    if isinstance(label, str) and label:
        return "label:" + label
    prompt = row.get("prompt_label")
    if isinstance(prompt, str) and prompt:
        return "label:" + prompt
    return None


def _row_started(row: dict) -> float | None:
    """Start epoch for sorting, or None when missing."""
    try:
        value = float(row.get("started_at"))
    except (TypeError, ValueError):
        return None
    return value


def _pick_tier(rows: list) -> str:
    """Lane tier, preferring a specific worker family over generic."""
    for row in rows:
        tier = tier_for_process(row)
        if tier != "generic":
            return tier
    return "generic"


def _pick_field(rows: list, name: str):
    """First non-empty field value in start order, or None."""
    for row in rows:
        value = row.get(name)
        if isinstance(value, str) and value:
            return value
        if name == "model" and value is not None:
            return value
    return None


def _row_label(row: dict):
    """Label identity for attaching, preferring bracket over prompt."""
    label = row.get("label")
    if isinstance(label, str) and label:
        return label
    prompt = row.get("prompt_label")
    if isinstance(prompt, str) and prompt:
        return prompt
    return None


def group_live_processes(rows, now: float | None = None) -> list:
    """Group live-worker rows into distinct lanes.

    Rows are the dashboard ``live_worker_processes`` listing. Children
    sharing a run label, prompt-file label, or session id collapse into
    one lane. A launcher row without a session joins the live session
    group carrying its label, so a wrapper plus its worker still reads
    as one lane while two live sessions on one label stay two lanes.
    The representative pid is the earliest start, ties by smallest pid,
    so elapsed time measures the run age. Lanes sort by start time,
    oldest first. Unnameable rows are dropped.
    """
    moment = float(now) if isinstance(now, (int, float)) else time.time()
    groups: dict = {}
    order: list = []
    pending: list = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = lane_key(row)
        if key is None:
            continue
        if key.startswith("session:"):
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(row)
        else:
            pending.append(row)
    for row in pending:
        label = _row_label(row if isinstance(row, dict) else {})
        target = None
        if label is not None:
            candidates = []
            for key in order:
                if not key.startswith("session:"):
                    continue
                for member in groups[key]:
                    if _row_label(member) == label:
                        candidates.append(key)
                        break
            if candidates:
                starts = {}
                for key in candidates:
                    best = None
                    for member in groups[key]:
                        started = _row_started(member)
                        if started is None:
                            continue
                        if best is None or started < best:
                            best = started
                    starts[key] = best if best is not None else float("inf")
                target = sorted(candidates, key=lambda key: starts[key])[-1]
        if target is not None:
            groups[target].append(row)
            continue
        key = lane_key(row)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    lanes = []
    for key in order:
        members = sorted(
            groups[key],
            key=lambda item: (
                _row_started(item) if _row_started(item) is not None else float("inf"),
                item.get("pid") if isinstance(item.get("pid"), int) else 10**18,
            ),
        )
        first = members[0]
        started = _row_started(first)
        elapsed = max(0.0, moment - started) if started is not None else None
        pids = sorted({item.get("pid") for item in members if isinstance(item.get("pid"), int)})
        comms = sorted({str(item.get("comm")) for item in members if item.get("comm")})
        label = _pick_field(members, "label") or _pick_field(members, "prompt_label")
        lanes.append(
            {
                "key": key,
                "label": label,
                "tier": _pick_tier(members),
                "pid": first.get("pid"),
                "started_at": started,
                "elapsed_s": elapsed,
                "session_id": _pick_field(members, "session_id"),
                "prompt_label": _pick_field(members, "prompt_label"),
                "model": _pick_field(members, "model"),
                "processes": len(members),
                "pids": pids,
                "comms": comms,
            }
        )
    lanes.sort(
        key=lambda lane: (
            lane.get("started_at") if lane.get("started_at") is not None else float("inf"),
            str(lane.get("label") or ""),
        )
    )
    return lanes


def format_elapsed(seconds) -> str:
    """Compact duration, string-for-string the dashboard fmtDur helper.

    Under a minute rounds to whole seconds; under 48 hours reads as minutes
    and hours; then days with hours (to 14 days), whole days (to 365 days),
    and years with days. Non-finite or unparsable input is "-".
    """
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(value):
        return "-"
    value = max(0.0, value)
    if value < 60:
        return "%ds" % math.floor(value + 0.5)
    total = int(value)
    if total < 3600:
        return "%dm %ds" % (total // 60, total % 60)
    if total < 172800:
        return "%dh %dm" % (total // 3600, (total % 3600) // 60)
    days = total // 86400
    hours = (total % 86400) // 3600
    if days < 14:
        return "%dd %dh" % (days, hours)
    if days < 365:
        return "%dd" % days
    years, rest = divmod(days, 365)
    return "%dy %dd" % (years, rest) if rest else "%dy" % years
