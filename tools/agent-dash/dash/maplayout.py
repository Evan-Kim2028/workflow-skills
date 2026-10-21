"""Plan-progress map layout: one pure computation for the map standard.

Layer B of the map standard lives here, ported from the mockup renderer
(``mockups/render.js`` in the map-standard scratchpad): density from the
live task count, done-collapse in oversized sections, short chip labels,
focus-driven arrows above 40 task edges, solo-train untagging, slim
all-done columns past 5, and the one-line sections strip.

``layout`` never rewrites meaning: it never moves a task to another
section or a section to another column. It only collapses, shortens, caps,
and folds, so ``ws layout`` prints exactly what the page will do.
``map_warnings`` is the layer C lint (MAP101 to MAP111): warnings, never
errors, each naming the threshold, the actual number, and the fix.

Both functions are pure (stdlib only, no I/O) and deterministic: every
list follows plan order, never set iteration order.
"""

from __future__ import annotations

import re

#: Live tasks (excluding deferred) at or below this render comfortable.
DENSITY_COMFORT_MAX = 40
#: Live tasks above this render compact plus full done-collapse.
DENSITY_ULTRA_MIN = 80
#: A section with more live tasks than this collapses its done chips.
COLLAPSE_ABOVE = 12
#: Done chips a collapsed section still shows before the ``+N done`` token.
COLLAPSE_SHOWN_DONE = 4
#: Task edges at or below this render arrows-all; above renders on-focus.
ARROWS_ALL_MAX = 40
#: A task with more deps than this gets a fan-in badge instead of arrows.
FANIN_ABOVE = 6
#: Authored columns above this render all-done columns slim.
MAX_COLUMNS = 5
#: Displayed chip text is capped at this many characters.
CHIP_TEXT_CAP = 14
#: A task id suffix longer than this draws a MAP110 warning.
ID_SUFFIX_WARN = 12
#: A section with more live tasks than this draws a MAP101 warning.
GROUP_BIG_WARN = 14
#: Trains with fewer members than this render no tag (MAP107).
TRAIN_MIN_MEMBERS = 2
#: The legend names at most this many trains before ``+N more``.
LEGEND_TRAINS = 4
#: Tallest-vs-shortest column ratio above this draws a MAP104 warning
#: (only with more than this many live tasks).
BALANCE_RATIO = 3
BALANCE_MIN_LIVE = 40
#: Half or more single-task sections draws a MAP109 warning.
SINGLETON_SHARE = 0.5
#: Deferred share above this draws a MAP111 info.
DEFERRED_SHARE = 0.20
#: Columns at or above this count draw a MAP106 warning when a
#: non-first-column section has no incoming section edge.
EDGES_MIN_COLS = 3

_WS_PREFIX = re.compile(r"^WS\d+\.")
#: A title that looks like a bare id: id characters only, with a digit or
#: a separator in it. Plain single words ("Guardrails") are titles, not
#: ids, so they never match.
_IDLIKE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
_IDLIKE_HINT = re.compile(r"[0-9._-]")


def short_label(task_id: str) -> str:
    """Chip display text: the id with the ``WS<digits>.`` prefix stripped."""
    return _WS_PREFIX.sub("", str(task_id))


def capped_label(task_id: str) -> str:
    """Chip display text, capped at 14 characters with an end ellipsis."""
    text = short_label(task_id)
    if len(text) > CHIP_TEXT_CAP:
        return text[: CHIP_TEXT_CAP - 1] + "…"
    return text


def _chip_tooltip(task: dict, ws: str) -> str:
    """Full-id tooltip for a chip, mirroring the mockup renderer."""
    tid = str(task.get("id"))
    out = tid + " · " + str(task.get("title") or "") + " · " + str(task.get("status") or "todo")
    if task.get("kind") == "gate":
        out += " · gate"
    if task.get("moved_to"):
        out += " to " + str(task.get("moved_to"))
    if task.get("note"):
        out += " (" + str(task.get("note")) + ")"
    notes = task.get("dep_notes") if isinstance(task.get("dep_notes"), dict) else {}
    ups = []
    for dep in task.get("deps") or []:
        if not isinstance(dep, str):
            continue
        note = notes.get(dep) if isinstance(notes.get(dep), str) else None
        ups.append(dep + (": " + note if note else ""))
    if ups:
        out += " · waits on " + ", ".join(ups)
    linked = task.get("linear")
    if isinstance(linked, dict) and linked.get("identifier"):
        out += " · " + str(linked.get("identifier"))
    return out


def _display_labels(task_ids: list[str]) -> tuple[dict[str, str], list[dict]]:
    """Display label per task id, with collisions disambiguated.

    The first task to claim a label keeps the short form; later tasks
    sharing it fall back to the full id (capped), then to a numbered
    suffix. Returns the label map plus the collision records.
    """
    labels: dict[str, str] = {}
    claimed: dict[str, str] = {}
    collisions: dict[str, list[str]] = {}
    for tid in task_ids:
        want = capped_label(tid)
        if want not in claimed:
            claimed[want] = tid
            labels[tid] = want
            continue
        collisions.setdefault(want, [claimed[want]])
        if tid not in collisions[want]:
            collisions[want].append(tid)
    for want, tids in collisions.items():
        for pos, tid in enumerate(tids):
            if pos == 0:
                continue
            full = str(tid)
            if len(full) > CHIP_TEXT_CAP:
                full = full[: CHIP_TEXT_CAP - 1] + "…"
            candidate = full
            suffix = 2
            while candidate in claimed and claimed[candidate] != tid:
                candidate = full + f"·{suffix}"
                suffix += 1
            claimed[candidate] = tid
            labels[tid] = candidate
    records = [{"label": want, "ids": tids} for want, tids in sorted(collisions.items())]
    return labels, records


def _as_groups(plan_view: dict) -> list[dict]:
    groups = plan_view.get("groups") if isinstance(plan_view, dict) else None
    return [group for group in groups or [] if isinstance(group, dict)]


def _group_tasks(group: dict) -> list[dict]:
    tasks = group.get("tasks") if isinstance(group, dict) else None
    return [task for task in tasks or [] if isinstance(task, dict)]


def _is_live(task: dict) -> bool:
    return str(task.get("status") or "todo") != "deferred"


def layout(plan_view: dict, *, viewport_cols: int = 5, density: str | None = None) -> dict:
    """Compute the map layout for a plan view (flat ``cols/groups/edges`` shape).

    ``viewport_cols`` caps how many full columns fit before trailing ones
    fold into a ``+N later sections`` rail. ``density`` overrides the
    task-count rule (``comfortable`` or ``compact``).
    """
    if density is not None and density not in ("comfortable", "compact"):
        raise ValueError(f"unknown density {density!r}: use comfortable or compact")
    capacity = max(1, int(viewport_cols or 5))
    ws = plan_view.get("ws") if isinstance(plan_view, dict) else None
    cols = [item for item in (plan_view.get("cols") or []) if isinstance(item, str)] if isinstance(plan_view, dict) else []
    raw_edges = plan_view.get("edges") if isinstance(plan_view, dict) else None
    edges = [list(item)[:2] for item in raw_edges or [] if isinstance(item, (list, tuple)) and len(item) >= 2]
    trains = [item for item in (plan_view.get("trains") or []) if isinstance(item, dict)] if isinstance(plan_view, dict) else []
    phases = [item for item in (plan_view.get("phases") or []) if isinstance(item, dict)] if isinstance(plan_view, dict) else []
    groups = _as_groups(plan_view if isinstance(plan_view, dict) else {})

    live_tasks = sum(1 for group in groups for task in _group_tasks(group) if _is_live(task))
    deferred_tasks = sum(1 for group in groups for task in _group_tasks(group) if not _is_live(task))
    task_edges = sum(len([dep for dep in (task.get("deps") or []) if isinstance(dep, str)]) for group in groups for task in _group_tasks(group))
    ultra = live_tasks > DENSITY_ULTRA_MIN
    mode = density if density is not None else ("comfortable" if live_tasks <= DENSITY_COMFORT_MAX else "compact")

    task_ids = [str(task.get("id")) for group in groups for task in _group_tasks(group) if task.get("id") not in (None, "")]
    labels, collisions = _display_labels(task_ids)
    shortened = sum(1 for tid in task_ids if labels.get(tid, tid) != tid)

    train_of: dict[str, str] = {}
    tagged: list = []
    untagged: list = []
    for train in trains:
        members = [item for item in train.get("members") or [] if isinstance(item, str)] if isinstance(train.get("members"), list) else []
        tid = train.get("id")
        if len(members) < TRAIN_MIN_MEMBERS:
            untagged.append(tid)
            continue
        tagged.append(tid)
        for member in members:
            train_of[member] = tid if isinstance(tid, str) else str(tid)
    solo = len(untagged)
    caption = None
    if solo:
        caption = f"{solo} single-task train{'s' if solo != 1 else ''} untagged (MAP107)"

    slim_cols = len(cols) > MAX_COLUMNS

    def group_live_total(group: dict) -> int:
        return sum(1 for task in _group_tasks(group) if _is_live(task))

    def group_is_all_done(group: dict) -> bool:
        tasks = _group_tasks(group)
        live = [task for task in tasks if _is_live(task)]
        return bool(tasks) and all(str(task.get("status") or "") == "done" for task in live)

    slim_group_ids: set[str] = set()
    if slim_cols:
        for group in groups:
            if group_is_all_done(group) and isinstance(group.get("id"), str):
                slim_group_ids.add(str(group.get("id")))

    def has_unmet_gate(col_index: int) -> bool:
        for group in groups:
            if group.get("col") != col_index:
                continue
            for task in _group_tasks(group):
                if task.get("kind") == "gate" and str(task.get("status") or "") != "done":
                    return True
        return False

    full_order = [index for index in range(len(cols)) if not any(
        isinstance(group.get("id"), str) and str(group.get("id")) in slim_group_ids and group.get("col") == index
        for group in groups
    )]
    keep = set(full_order[:capacity])
    folded: list = []
    for index in full_order[capacity:]:
        if has_unmet_gate(index):
            keep.add(index)
            continue
        folded.append(index)
    folded_rail = None
    if folded:
        folded_rail = {
            "columns": [{"index": index, "title": cols[index], "tasks": 0} for index in folded],
            "label": f"+{len(folded)} later sections",
        }

    column_groups: dict[int, list[dict]] = {index: [] for index in range(len(cols))}
    for group in groups:
        col = group.get("col")
        if isinstance(col, bool) or not isinstance(col, int) or col not in column_groups:
            continue
        column_groups[col].append(group)

    columns = []
    for index, title in enumerate(cols):
        members = column_groups[index]
        total = sum(len(_group_tasks(group)) for group in members)
        live = sum(group_live_total(group) for group in members)
        if index in folded:
            state = "folded"
            visible = 0
        elif members and all(
            isinstance(group.get("id"), str) and str(group.get("id")) in slim_group_ids for group in members
        ):
            state = "slim"
            visible = 0
        else:
            state = "full"
            visible = -1
        columns.append(
            {
                "index": index,
                "title": title,
                "state": state,
                "groups": [group.get("id") for group in members],
                "tasks": total,
                "live_tasks": live,
                "visible_chips": visible,
            }
        )
        if folded_rail is not None:
            for entry in folded_rail["columns"]:
                if entry["index"] == index:
                    entry["tasks"] = total

    group_rows = []
    for group in groups:
        gid = group.get("id")
        col = group.get("col") if isinstance(group.get("col"), int) and not isinstance(group.get("col"), bool) else None
        col_title = cols[col] if isinstance(col, int) and 0 <= col < len(cols) else None
        tasks = _group_tasks(group)
        live_total = sum(1 for task in tasks if _is_live(task))
        deferred = [task for task in tasks if not _is_live(task)]
        if isinstance(gid, str) and gid in slim_group_ids:
            done = sum(1 for task in tasks if str(task.get("status") or "") == "done")
            group_rows.append(
                {
                    "id": gid,
                    "title": group.get("title"),
                    "col": col,
                    "column": col_title,
                    "state": "slim",
                    "live_tasks": live_total,
                    "deferred_tasks": len(deferred),
                    "chips": [],
                    "shown_done": 0,
                    "hidden_done": 0,
                    "hidden_done_ids": [],
                    "more_token": None,
                    "deferred_token": f"↷{len(deferred)} deferred" if deferred else None,
                    "slim_summary": {"done": done, "total": live_total},
                }
            )
            continue
        live_chips: list = []
        gates: list = []
        opened: list = []
        done: list = []
        for task in tasks:
            if not _is_live(task):
                continue
            if task.get("kind") == "gate":
                gates.append(task)
            elif str(task.get("status") or "todo") == "done":
                done.append(task)
            else:
                opened.append(task)
        collapse = ultra or live_total > COLLAPSE_ABOVE
        shown_done = done if not collapse else done[:COLLAPSE_SHOWN_DONE]
        hidden = done[COLLAPSE_SHOWN_DONE:] if collapse and len(done) > COLLAPSE_SHOWN_DONE else []

        def chip(task: dict) -> dict:
            tid = str(task.get("id"))
            deps = [dep for dep in (task.get("deps") or []) if isinstance(dep, str)]
            return {
                "id": tid,
                "label": labels.get(tid, capped_label(tid)),
                "tooltip": _chip_tooltip(task, str(ws) if ws else ""),
                "status": task.get("status"),
                "kind": task.get("kind") if isinstance(task.get("kind"), str) else "task",
                "train": train_of.get(tid),
                "fan_in": len(deps) if len(deps) > FANIN_ABOVE else 0,
            }

        chips = [chip(task) for task in gates] + [chip(task) for task in opened] + [chip(task) for task in shown_done]
        group_rows.append(
            {
                "id": gid,
                "title": group.get("title"),
                "col": col,
                "column": col_title,
                "state": "collapsed-done" if collapse else "open",
                "live_tasks": live_total,
                "deferred_tasks": len(deferred),
                "chips": chips,
                "shown_done": len(shown_done),
                "hidden_done": len(hidden),
                "hidden_done_ids": [str(task.get("id")) for task in hidden],
                "more_token": f"+{len(hidden)} done" if hidden else None,
                "deferred_token": f"↷{len(deferred)} deferred" if deferred else None,
            }
        )

    for column in columns:
        if column["visible_chips"] == -1:
            column["visible_chips"] = sum(len(row["chips"]) for row in group_rows if row.get("col") == column["index"])

    arrow_mode = "all" if task_edges <= ARROWS_ALL_MAX else "on-focus"
    fan_in = []
    for group in groups:
        for task in _group_tasks(group):
            deps = [dep for dep in (task.get("deps") or []) if isinstance(dep, str)]
            if len(deps) > FANIN_ABOVE and task.get("id") not in (None, ""):
                fan_in.append({"id": str(task.get("id")), "deps": len(deps)})
    fan_in.sort(key=lambda row: (-row["deps"], row["id"]))
    arrows = {
        "mode": arrow_mode,
        "task_edges": task_edges,
        "group_edges": len(edges),
        "reason": f"{task_edges} task deps: arrows {'all' if arrow_mode == 'all' else 'on focus'}",
        "fan_in": fan_in,
    }

    legend = [tid for tid in tagged if isinstance(tid, str)][:LEGEND_TRAINS]
    if len(legend) < LEGEND_TRAINS:
        legend += [tid for tid in tagged if not isinstance(tid, str)][: LEGEND_TRAINS - len(legend)]
    more_trains = max(0, len(tagged) - LEGEND_TRAINS)

    phases_line = "  →  ".join(f"{pos} {item.get('name') or item.get('title') or ''}".rstrip() for pos, item in enumerate(phases, start=1))

    loads = [column["tasks"] for column in columns]
    balance = {"tallest": None, "shortest": None, "ratio": None}
    if loads:
        tall = max(range(len(loads)), key=lambda index: loads[index])
        short = min(range(len(loads)), key=lambda index: loads[index])
        ratio = None
        if loads[short] > 0:
            ratio = loads[tall] / loads[short]
        balance = {
            "tallest": {"column": tall, "title": cols[tall], "tasks": loads[tall]},
            "shortest": {"column": short, "title": cols[short], "tasks": loads[short]},
            "ratio": ratio,
        }

    return {
        "ws": ws,
        "density": mode,
        "ultra": ultra,
        "live_tasks": live_tasks,
        "deferred_tasks": deferred_tasks,
        "total_tasks": live_tasks + deferred_tasks,
        "task_edges": task_edges,
        "group_edges": len(edges),
        "columns": columns,
        "folded_rail": folded_rail,
        "groups": group_rows,
        "labels": {"shortened": shortened, "collisions": collisions},
        "arrows": arrows,
        "trains": {
            "tagged": tagged,
            "untagged": untagged,
            "caption": caption,
            "legend": legend,
            "more_trains": more_trains,
        },
        "phases_line": phases_line,
        "balance": balance,
    }


def _warn(code: str, title: str, detail: str, fix: str) -> dict:
    return {"code": code, "severity": "info", "title": title, "detail": detail, "fix": fix}


def map_warnings(plan_view: dict) -> list[dict]:
    """Layer C lint (MAP101 to MAP111) for a plan view.

    Warnings, never errors: a strange plan still renders. Every entry
    carries the machine ``code``, ``severity`` info (the level the doctor
    reports them at), a human ``title`` naming the section before
    any id, the ``detail`` with actual-vs-threshold numbers, and the
    one-line ``fix``. Deterministic: plan order throughout.
    """
    found: list[dict] = []
    groups = _as_groups(plan_view if isinstance(plan_view, dict) else {})
    cols = [item for item in (plan_view.get("cols") or []) if isinstance(item, str)] if isinstance(plan_view, dict) else []
    raw_edges = plan_view.get("edges") if isinstance(plan_view, dict) else None
    edges = [item for item in raw_edges or [] if isinstance(item, (list, tuple)) and len(item) >= 2]
    trains = [item for item in (plan_view.get("trains") or []) if isinstance(item, dict)] if isinstance(plan_view, dict) else []

    live_total = 0
    deferred_total = 0
    task_edges = 0
    for group in groups:
        for task in _group_tasks(group):
            if _is_live(task):
                live_total += 1
            else:
                deferred_total += 1
            task_edges += sum(1 for dep in (task.get("deps") or []) if isinstance(dep, str))

    for group in groups:
        gid = group.get("id")
        title = group.get("title") if isinstance(group.get("title"), str) else ""
        live = sum(1 for task in _group_tasks(group) if _is_live(task))
        if live > GROUP_BIG_WARN:
            found.append(
                _warn(
                    "MAP101",
                    f'"{title or gid}" section holds {live} tasks ({gid})',
                    f"{live} live tasks in one section; sections above {COLLAPSE_ABOVE} collapse done chips, sections above {GROUP_BIG_WARN} should split",
                    f'Split "{title or gid}" ({gid}, {live} tasks) into two sections',
                )
            )

    if len(cols) > MAX_COLUMNS:
        found.append(
            _warn(
                "MAP102",
                f"Plan has {len(cols)} columns",
                f"{len(cols)} authored columns; at most {MAX_COLUMNS} render full, the rest slim or fold into a rail",
                f"Lay out in at most {MAX_COLUMNS} columns; columns {MAX_COLUMNS + 1}+ fold into a rail",
            )
        )

    col_live = {index: 0 for index in range(len(cols))}
    col_total = {index: 0 for index in range(len(cols))}
    col_has_group = {index: False for index in range(len(cols))}
    for group in groups:
        col = group.get("col")
        if isinstance(col, bool) or not isinstance(col, int) or col not in col_live:
            continue
        col_has_group[col] = True
        col_live[col] += sum(1 for task in _group_tasks(group) if _is_live(task))
        col_total[col] += len(_group_tasks(group))
    for index, title in enumerate(cols):
        if col_live[index] == 0 and col_has_group[index]:
            found.append(
                _warn(
                    "MAP103",
                    f'"{title}" column has no live tasks',
                    f"column {index} ({title}) holds sections but no live tasks",
                    f'Drop "{title}" or move a section into it',
                )
            )

    if live_total > BALANCE_MIN_LIVE and col_total:
        tall = max(col_total, key=lambda index: col_total[index])
        short = min(col_total, key=lambda index: col_total[index])
        over = col_total[short] == 0 and col_total[tall] > 0 or (
            col_total[short] > 0 and col_total[tall] / col_total[short] > BALANCE_RATIO
        )
        if over:
            found.append(
                _warn(
                    "MAP104",
                    f'"{cols[tall]}" column outweighs "{cols[short]}" ({col_total[tall]} to {col_total[short]} tasks)',
                    f"tallest column holds {col_total[tall]} tasks, shortest {col_total[short]} (bar: {BALANCE_RATIO}x with more than {BALANCE_MIN_LIVE} live tasks)",
                    "Move sections across columns; the renderer compacts but never moves sections",
                )
            )

    for group in groups:
        gid = group.get("id")
        title = group.get("title")
        if not isinstance(title, str) or not title:
            continue
        if title == gid or (_IDLIKE.match(title) and _IDLIKE_HINT.search(title)):
            found.append(
                _warn(
                    "MAP105",
                    f'"{title}" section title is a bare id ({gid})',
                    f"section {gid} titles itself with an id-shaped string, not plain words",
                    f"Retitle {gid} in plain words",
                )
            )

    if len(cols) >= EDGES_MIN_COLS and groups:
        first = min(
            (group.get("col") for group in groups if isinstance(group.get("col"), int) and not isinstance(group.get("col"), bool)),
            default=0,
        )
        incoming: dict = {}
        for edge in edges:
            incoming[edge[1]] = incoming.get(edge[1], 0) + 1
        uncovered = sorted(
            str(group.get("id"))
            for group in groups
            if isinstance(group.get("id"), str) and group.get("col") != first and not incoming.get(group.get("id"))
        )
        if uncovered:
            found.append(
                _warn(
                    "MAP106",
                    f"{len(uncovered)} sections have no waits-on edge",
                    f"{len(edges)} section edges across {len(cols)} columns leave {', '.join(uncovered)} with no upstream section",
                    "Add section waits-on edges so columns mean something",
                )
            )

    for train in trains:
        members = train.get("members") if isinstance(train.get("members"), list) else []
        members = [item for item in members if isinstance(item, str)]
        if len(members) < TRAIN_MIN_MEMBERS:
            tid = train.get("id")
            found.append(
                _warn(
                    "MAP107",
                    f"Train {tid} rides solo ({len(members)} member{'s' if len(members) != 1 else ''})",
                    f"train {tid} has {len(members)} members; solo trains render no tag",
                    f"Drop train {tid}; keep a task note instead",
                )
            )

    if task_edges > ARROWS_ALL_MAX:
        found.append(
            _warn(
                "MAP108",
                f"Plan has {task_edges} task deps",
                f"{task_edges} task deps (bar: {ARROWS_ALL_MAX}); the map defaults to on-focus arrows",
                "Map defaults to on-focus arrows; add dep_notes to cross-section edges",
            )
        )

    if len(groups) >= 2:
        singletons = sum(1 for group in groups if len(_group_tasks(group)) == 1)
        if singletons / len(groups) >= SINGLETON_SHARE:
            found.append(
                _warn(
                    "MAP109",
                    f"{singletons} of {len(groups)} sections hold a single task",
                    f"{singletons} single-task sections out of {len(groups)} (bar: half or more)",
                    "Merge 1-task sections into their column's section",
                )
            )

    for group in groups:
        for task in _group_tasks(group):
            tid = task.get("id")
            if not isinstance(tid, str) or not tid:
                continue
            suffix = _WS_PREFIX.sub("", tid)
            if len(suffix) > ID_SUFFIX_WARN:
                found.append(
                    _warn(
                        "MAP110",
                        f'Task id "{tid}" is {len(suffix)} chars',
                        f'id suffix "{suffix}" is {len(suffix)} chars (bar: {ID_SUFFIX_WARN}); chips cap at {CHIP_TEXT_CAP}',
                        f"Shorten {tid}; display caps at {CHIP_TEXT_CAP} chars",
                    )
                )

    total = live_total + deferred_total
    if total and deferred_total / total > DEFERRED_SHARE:
        found.append(
            _warn(
                "MAP111",
                f"{deferred_total} of {total} tasks are deferred",
                f"{deferred_total} deferred of {total} tasks ({100 * deferred_total / total:.0f}%; bar: {100 * DEFERRED_SHARE:.0f}%); they auto-collapse to one chip",
                "Deferred auto-collapse to one chip; no action needed",
            )
        )

    return found
