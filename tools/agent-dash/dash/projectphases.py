"""Planning versus execution per workstream.

``GET /api/project/phases?scope=24h|7d|all`` answers what the "Planning
versus execution" card paints: per workstream (and an all-scope total)
the billed tokens, cost, agents, runs and hours split by kind of work,
plus a cumulative-by-day cost and token series per kind of work for
each workstream.

Attribution is per usage event: each event joins its run, the run is
classified by :mod:`dash.phases` (explicit ``ws<N>-plan``/``-exec``/
``-review``/``-coord`` marks first, then review affixes, plan task
matches, planning/review/doing/coordination words, unmapped lead
sessions, and the pre-task planning window), and child-chat events fold
onto their parent run exactly like the snapshot does. Cost uses the
same catalog pricing with the same unpriced rule (priced events sum,
unpriced tokens are counted but costed nowhere), so the all-scope
total equals the sum of the kinds of work to the cent, and
per-workstream kind-of-work sums equal the bucket totals for the same
window.

``unclassified`` is its own grey bucket, never merged. The payload
carries the rules hash (caches key on it: editing the rules file
invalidates), the unclassified token share, and a short caption naming
how the split was decided.

Workstream ids on the project's ``ignore_workstreams`` list fold into
the single ``"Other"`` bucket, like the burn-up. Only names and
numbers cross this boundary: ids, titles, token and money sums, counts,
dates, the caption, and the rules hash — never a prompt, a report
body, or a path. Results are cached by scope, store sequence, rules
hash, ignore list, plan stamps, and taskmap stamp.
"""

from __future__ import annotations

import time as _time

from dash.store.util import (
    BILLED_FIELDS,
    TOKEN_FIELDS,
    UNASSIGNED,
    _is_counted_agent,
    _money,
)

DAY_S = 86400

#: Scope name to window seconds; None is lifetime.
SCOPES: dict[str, int | None] = {"24h": 86400, "7d": 7 * 86400, "all": None}

#: How the split was decided, in words (rule names, not numbers).
CAPTION = (
    "Split by kind of work: planning, execution, review, coordination. "
    "Explicit plan, exec, review and coord marks first, then review "
    "affixes, plan task matches, planning, review, doing and "
    "coordination words, unmapped lead sessions and early exploration. "
    "The rest reads Unclassified."
)

_CACHE: dict = {}


def _utc_day(ts: float) -> int:
    """UTC-midnight epoch for an epoch stamp (calendar day, UTC)."""
    return int(ts // DAY_S) * DAY_S


def _ws_sort_key(ws: str):
    import re

    match = re.search(r"(\d+)", ws or "")
    return (int(match.group(1)) if match else 10**9, str(ws))


def _ignored_now(cfg=None) -> list:
    try:
        from dash.project import read_ignore_list

        return read_ignore_list(cfg)
    except Exception:
        return []


def _names() -> dict:
    """Workstream titles for the rows, or {} when unreadable."""
    try:
        from dash.bundles import list_bundles

        names = {
            str(item.get("id")): str(item.get("title") or "")
            for item in (list_bundles() or [])
            if isinstance(item, dict) and item.get("id")
        }
    except Exception:
        names = {}
    # The snapshot spells the unattributed row "Unassigned"; match it.
    names.setdefault(UNASSIGNED, "Unassigned")
    return names


def _plans_stamp():
    try:
        from dash.bundles import plans_stamp

        return plans_stamp()
    except Exception:
        return None


def _taskmap_stamp():
    try:
        from dash.locate import layered_path
        from dash.taskmatch import load_taskmap  # noqa: F401 (warms nothing; path only)

        found = layered_path("taskmap", "AGENT_DASH_TASKMAP")
        if found is not None and found.is_file():
            stat = found.stat()
            return (stat.st_mtime_ns, stat.st_size)
    except Exception:
        pass
    return None


def _cache_key(store, scope: str, rules_hash: str, ignored) -> tuple:
    seq = getattr(store, "data_seq", None)
    if seq is None:
        seq = getattr(store, "_data_seq", 0)
    try:
        from dash.project import ignore_cache_key

        ignore_key = ignore_cache_key(ignored)
    except Exception:
        ignore_key = ()
    return (scope, seq, rules_hash, ignore_key, _plans_stamp(), _taskmap_stamp())


def _billed(event: dict) -> int:
    return sum(int(event.get(part) or 0) for part in ("input", "output", "cache_read", "cache_write"))


def _overlap_hours(run: dict, since: float | None, until: float, now: float) -> float:
    """Run hours inside ``[since, until]`` (open runs end now)."""
    try:
        start = float(run.get("started_at")) if run.get("started_at") is not None else None
    except (TypeError, ValueError):
        start = None
    if start is None:
        return 0.0
    if run.get("status") == "running" or run.get("ended_at") is None:
        end = now
    else:
        try:
            end = float(run.get("ended_at"))
        except (TypeError, ValueError):
            return 0.0
    low = max(start, since) if since is not None else start
    high = min(end, until)
    return max(0.0, high - low) / 3600.0


def _zero_tokens() -> dict:
    return {key: 0 for key in TOKEN_FIELDS}


def build_phases(store, scope: str = "24h", now: float | None = None, cfg=None, root=None) -> dict:
    """Phases payload for ``GET /api/project/phases``.

    Pure read path: one store pass for runs plus windowed usage
    events, then classification and folding in Python. Cached by
    scope, store sequence, rules hash, ignore list, plan stamps and
    taskmap stamp.
    """
    from dash import phases as phases_mod
    from dash.store.runs import _parent_map
    from dash.store.usage import _fold_events

    if scope not in SCOPES:
        raise ValueError("bad scope")
    moment = float(now if now is not None else _time.time())
    window = SCOPES[scope]
    since = moment - window if window is not None else None

    rules, digest, version = phases_mod.load_rules(local=phases_mod.local_path(cfg, root))
    ignored = _ignored_now(cfg)
    key = _cache_key(store, scope, digest, ignored)
    hit = _CACHE.get("key")
    if hit is not None and hit == key and "payload" in _CACHE:
        return _CACHE["payload"]

    try:
        from dash.project import OTHER_WS, is_ignored
    except Exception:
        OTHER_WS, is_ignored = "Other", None

    runs, events = store.phase_events(since, moment)
    by_id = {run["run_id"]: run for run in runs if run.get("run_id")}
    parents = _parent_map([dict(run) for run in runs])

    try:
        from dash.taskmatch import load_taskmap

        taskmap = load_taskmap()
    except Exception:
        taskmap = {}
    ws_ids = sorted({str(run.get("workstream") or UNASSIGNED) for run in runs})
    plan_tasks = phases_mod.plan_tasks_for(ws_ids)
    phase_of = phases_mod.classify_runs(runs, taskmap=taskmap, plan_tasks=plan_tasks, rules=rules)

    def owner_ws(run_id: str) -> str:
        """Parent run's workstream for folded child chats, else the run's own."""
        target = parents.get(run_id, run_id)
        run = by_id.get(target, by_id.get(run_id))
        ws = (run or {}).get("workstream") or UNASSIGNED
        ws = str(ws)
        if ignored and is_ignored is not None and is_ignored(ws, ignored):
            return OTHER_WS
        return ws

    def owner_phase(run_id: str) -> str:
        target = parents.get(run_id, run_id)
        phase, _rule = phase_of.get(target, phase_of.get(run_id, ("unclassified", "none")))
        return phase if phase in phases_mod.PHASES else "unclassified"

    from dash.pricing import catalog as _catalog

    table = _catalog()
    memo: dict = {}
    # Fold per (owner run, UTC day) with the same pricer and memo the
    # snapshot fold uses, so phase sums match the bucket totals.
    groups: dict = {}
    for event in events:
        if not isinstance(event, dict) or not event.get("run_id"):
            continue
        ts = event.get("ts")
        try:
            day = _utc_day(float(ts)) if ts is not None else None
        except (TypeError, ValueError):
            continue
        if day is None:
            continue
        key2 = (parents.get(event["run_id"], event["run_id"]), day)
        groups.setdefault(key2, []).append(event)

    per_ws: dict = {}
    day_cost: dict = {}
    day_tokens: dict = {}
    run_seen: dict = {}

    def cell(ws: str, phase: str) -> dict:
        bucket = per_ws.setdefault(
            ws,
            {
                phase: {
                    "tokens": _zero_tokens(),
                    "cost_usd": 0.0,
                    "unpriced_tokens": 0,
                    "runs": set(),
                    "agents": set(),
                    "hours": 0.0,
                }
                for phase in phases_mod.PHASES
            },
        )
        return bucket[phase]

    for (owner_run, day), items in groups.items():
        folded, cost, _unpriced = _fold_events(items, _table=table, _memo=memo)
        ws = owner_ws(owner_run)
        phase = owner_phase(owner_run)
        entry = cell(ws, phase)
        for token_key in TOKEN_FIELDS:
            entry["tokens"][token_key] += int(folded.get(token_key) or 0)
        billed = sum(int(folded.get(part) or 0) for part in BILLED_FIELDS)
        if cost is None:
            entry["unpriced_tokens"] += billed
        else:
            entry["cost_usd"] += cost
        day_cost[(ws, phase, day)] = day_cost.get((ws, phase, day), 0.0) + (cost or 0.0)
        day_tokens[(ws, phase, day)] = day_tokens.get((ws, phase, day), 0) + billed
        if owner_run not in run_seen:
            run_seen[owner_run] = (ws, phase)

    # Runs, agents and hours ride on distinct runs with spend in the
    # window (agents use the same counted-agent rule as the snapshot).
    for owner_run, (ws, phase) in run_seen.items():
        run = by_id.get(owner_run)
        if run is None:
            continue
        entry = cell(ws, phase)
        entry["runs"].add(owner_run)
        if _is_counted_agent(run):
            entry["agents"].add(owner_run)
        entry["hours"] += _overlap_hours(run, since, moment, moment)

    titles = _names()
    order = sorted(set(per_ws) | {ws for ws, _phase in run_seen.values()}, key=_ws_sort_key)
    if OTHER_WS in order:
        order = [ws for ws in order if ws != OTHER_WS] + [OTHER_WS]

    first_day = min((day for (_ws, _phase, day) in day_cost), default=None)
    today = _utc_day(moment)
    start_day = _utc_day(since) if since is not None else (first_day if first_day is not None else today)

    workstreams = []
    for ws in order:
        phases_out = {}
        for phase in phases_mod.PHASES:
            entry = per_ws.get(ws, {}).get(phase)
            if entry is None:
                entry = {"tokens": _zero_tokens(), "cost_usd": 0.0, "unpriced_tokens": 0,
                         "runs": set(), "agents": set(), "hours": 0.0}
            phases_out[phase] = {
                "tokens": dict(entry["tokens"]),
                "billed_tokens": sum(entry["tokens"][part] for part in BILLED_FIELDS),
                "cost_usd": _money(entry["cost_usd"]),
                "unpriced_tokens": entry["unpriced_tokens"],
                "agents": len(entry["agents"]),
                "runs": len(entry["runs"]),
                "hours": round(entry["hours"], 3),
            }
        total_tokens = _zero_tokens()
        total_cost = 0.0
        for phase in phases_mod.PHASES:
            for token_key in TOKEN_FIELDS:
                total_tokens[token_key] += phases_out[phase]["tokens"][token_key]
            total_cost += phases_out[phase]["cost_usd"] or 0.0
        days: list[int] = []
        day = start_day
        while day <= today:
            days.append(day)
            day += DAY_S
        series = {}
        for phase in phases_mod.PHASES:
            per_day_cost = [_money(day_cost.get((ws, phase, day), 0.0)) for day in days]
            per_day_tokens = [day_tokens.get((ws, phase, day), 0) for day in days]
            cum_cost: list[float] = []
            cum_tokens: list[int] = []
            running_cost = 0.0
            running_tokens = 0
            for cost_value, token_value in zip(per_day_cost, per_day_tokens):
                running_cost = _money(running_cost + (cost_value or 0.0))
                running_tokens += token_value
                cum_cost.append(running_cost)
                cum_tokens.append(running_tokens)
            series[phase] = {
                "cost": per_day_cost,
                "tokens": per_day_tokens,
                "cumulative_cost": cum_cost,
                "cumulative_tokens": cum_tokens,
            }
        workstreams.append(
            {
                "ws": ws,
                "name": titles.get(ws, ""),
                "by_phase": phases_out,
                "total": {"tokens": total_tokens, "cost_usd": _money(total_cost)},
                "days": days,
                "series": series,
            }
        )

    grand_tokens = _zero_tokens()
    grand_cost = 0.0
    unc_billed = 0
    all_billed = 0
    for item in workstreams:
        for phase in phases_mod.PHASES:
            for token_key in TOKEN_FIELDS:
                grand_tokens[token_key] += item["by_phase"][phase]["tokens"][token_key]
            grand_cost += item["by_phase"][phase]["cost_usd"] or 0.0
        unc_billed += item["by_phase"]["unclassified"]["billed_tokens"]
        all_billed += sum(item["total"]["tokens"][part] for part in BILLED_FIELDS)
    payload = {
        "scope": scope,
        "since": since,
        "until": moment,
        "day": "UTC",
        "rules_hash": digest,
        "rules_version": version,
        "caption": CAPTION,
        "workstreams": workstreams,
        "total": {"tokens": grand_tokens, "cost_usd": _money(grand_cost)},
        "unclassified_share": round(unc_billed / all_billed, 4) if all_billed else 0.0,
    }
    _CACHE.clear()
    _CACHE["key"] = key
    _CACHE["payload"] = payload
    return payload


def cache_key(store, scope: str, cfg=None, root=None) -> tuple:
    """Current cache key without building (rules hash included)."""
    from dash import phases as phases_mod

    _rules, digest, _version = phases_mod.load_rules(local=phases_mod.local_path(cfg, root))
    return _cache_key(store, scope, digest, _ignored_now(cfg))


def clear_cache() -> None:
    """Drop the cached payload (tests only; production invalidates by key)."""
    _CACHE.clear()
