"""Linear sync for one workstream bundle: link, push, flush the outbox, pull status back.

Orchestration only. ``dash/linear.py`` holds the pure model (stable ids,
status mapping, rendered issues, the link and outbox files) and
``dash/linear_client.py`` the network client. Local writes stay in
``dash/bundles.py``; a pull applies with ``origin='linear'`` so it never
re-enqueues the change (no ping-pong).
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import time
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path

from dash import bundles, errors, lifecycle, linear, linear_auth, project
from dash.linear_client import Client, LinearError

WORKFLOW_STATES = (
    ("Todo", "unstarted", "#bec2c8"),
    ("In Progress", "started", "#f2c94c"),
    ("In Review", "started", "#5e6ad2"),
    (linear.AWAITING_PROD, "started", "#a855f7"),
    ("Done", "completed", "#4cb782"),
    ("Canceled", "canceled", "#95a2b3"),
)

ERROR_CLIP = 200
META_BUDGET_S = 15.0
"""Seconds one sync cycle may spend on metadata calls before it yields; the rest resumes next cycle."""


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def linked_bundles(root: Path | None = None) -> list[str]:
    """Workstream ids whose bundle carries a ``linear.json``."""
    return [row["id"] for row in bundles.list_bundles(root) if linear.is_linked(row["id"], root)]


@contextlib.contextmanager
def _sync_lock(path: Path):
    """Exclusive flock on ``.linear.lock`` for one whole sync or link cycle."""
    with open(path / linear.SYNC_LOCK, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _scrub(text: object) -> str:
    """Flatten, redact the API key and clip a message for storage or print."""
    flat = " ".join(str(text).split())
    key = linear.api_key()
    if key:
        flat = flat.replace(key, "[redacted]")
    return flat[:ERROR_CLIP]


def _error_text(exc: Exception) -> str:
    if isinstance(exc, LinearError):
        return _scrub(f"{exc.code}: {exc.message}")
    return _scrub(f"{type(exc).__name__}: {exc}")


NOT_SIGNED_IN = "Not signed in to Linear. Run: python3 agent_dash.py ws linear login (or set LINEAR_API_KEY for headless use)."


def _make_client():
    """The client for the current credential: the MCP transport for a sign-in, the GraphQL client for an API key."""
    cred = linear_auth.credential()
    if cred is None:
        raise LinearError("AUTH", NOT_SIGNED_IN)
    if cred[0] == "mcp":
        try:
            from dash.linear_mcp import McpClient
        except ImportError:
            raise LinearError("AUTH", "the MCP transport is not available; set LINEAR_API_KEY for headless use") from None
        return McpClient(cred[1])
    return Client(cred[1])


def _plan(ws: str, root: Path | None) -> dict | None:
    path = bundles.bundle_dir(ws, root)
    if path is None:
        return None
    data = bundles._read_json(path / "plan.json")
    return data if isinstance(data, dict) else None


def _bundle_title(ws: str, root: Path | None) -> str:
    path = bundles.bundle_dir(ws, root)
    meta = bundles._read_json(path / "bundle.json") if path is not None else None
    title = meta.get("title") if isinstance(meta, dict) else None
    return title.strip() if isinstance(title, str) and title.strip() else ws


def _plan_has_dag(plan: dict) -> bool:
    """True when the plan carries a dag dict with a non-empty groups list."""
    dag = plan.get("dag")
    return isinstance(dag, dict) and isinstance(dag.get("groups"), list) and bool(dag["groups"])


def _dag_entries(plan: dict) -> list[tuple[dict, dict]]:
    """(group, task) pairs for every dag task with a string id, in plan order."""
    dag = plan.get("dag")
    if not isinstance(dag, dict):
        return []
    pairs = []
    for group in dag.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for task in group.get("tasks") or []:
            if isinstance(task, dict) and isinstance(task.get("id"), str):
                pairs.append((group, task))
    return pairs


def _cols(plan: dict) -> list[str]:
    dag = plan.get("dag")
    cols = dag.get("cols") if isinstance(dag, dict) else None
    return [col if isinstance(col, str) else "" for col in cols] if isinstance(cols, list) else []


def _col_name(cols: list[str], group: dict) -> str | None:
    index = group.get("col")
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(cols) and cols[index]:
        return cols[index]
    return None


def _train_titles(plan: dict) -> dict[str, str]:
    """Task id to the title of the train that lists it as a member."""
    titles: dict[str, str] = {}
    for train in plan.get("trains") or []:
        if not isinstance(train, dict) or not isinstance(train.get("title"), str):
            continue
        for member in bundles._str_list(train.get("members")):
            titles.setdefault(member, train["title"])
    return titles


def _task_status(task: dict) -> str:
    status = task.get("status")
    return status if status in bundles.STATUSES else "todo"


def _cancel_note(task: dict) -> str:
    parts = ["Deferred in agent-dash."]
    if task.get("moved_to"):
        parts.append(f"Moved to {task['moved_to']}.")
    if task.get("note"):
        parts.append(f"Note: {task['note']}")
    return " ".join(parts)


def _detach(record: dict, kind: str, key: str) -> None:
    """Forget a task or approval whose Linear issue was deleted in Linear.

    The key is listed under ``record["detached"]`` so no later push recreates it.
    """
    token = f"{kind}:{key}"
    detached = record.setdefault("detached", [])
    if token not in detached:
        detached.append(token)
    bucket = record.get("tasks" if kind == "task" else "approvals") or {}
    bucket.pop(key, None)


def _relation_map(record: dict) -> dict[str, str | None]:
    """The record's blocks relations as {"<blocker>><blocked>": relation id or None}.

    The old list form has no relation ids; it migrates to null ids on first read.
    """
    raw = record.get("relations")
    if isinstance(raw, dict):
        relations = {str(edge): value if isinstance(value, str) else None for edge, value in raw.items()}
    else:
        relations = {edge: None for edge in bundles._str_list(raw)}
    record["relations"] = relations
    return relations


def _resolve_state(known: dict[str, str], state: object) -> dict:
    """``{'name', 'type'}`` for a pulled Linear state.

    A team rename keeps the state id, so the id maps back to the workflow name
    the status table knows. An id outside the workspace map keeps the name Linear sent.
    """
    if not isinstance(state, dict):
        return {}
    sid = state.get("id")
    name = known[sid] if isinstance(sid, str) and sid in known else state.get("name")
    return {"name": name, "type": state.get("type")}


def _recorded_id(entry: object, fallback: str) -> str:
    """The Linear id recorded in linear.json for an entry (a dict with id, or a bare id), else the stable id."""
    if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
        return entry["id"]
    if isinstance(entry, str) and entry:
        return entry
    return fallback


def _push(client: Client, ws: str, root: Path | None, record: dict, ws_data: dict, plan: dict, now: float, force: bool,
          out_of_time: Callable[[], bool] | None = None) -> int:
    """The spec push, then the metadata pass. Returns the spec push's upsert count."""
    count = _push_spec(client, ws, root, record, ws_data, plan, now, force)
    _push_meta(client, ws, root, record, plan, force, out_of_time)
    return count


def _lifecycle_state(ws: str, root: Path | None) -> str:
    """The bundle's lifecycle state from bundle.json, defaulting like the dashboard does."""
    path = bundles.bundle_dir(ws, root)
    meta = bundles._read_json(path / "bundle.json") if path is not None else None
    state = meta.get("state") if isinstance(meta, dict) else None
    return state if state in lifecycle.STATES else lifecycle.DEFAULT_STATE


PROJECT_FIELDS = ("summary", "description", "lead", "labels", "priority", "start_date", "target_date")


def _project_meta(tracker: dict, state: str) -> dict:
    """The project payload for ``update_project_meta``: the tracker's project fields plus the status."""
    meta = {key: tracker[key] for key in PROJECT_FIELDS if key in tracker}
    status = linear.project_status_for_state(state)
    if status is not None:
        meta["status"] = status
    return meta


def _meta_parts(client: Client, ws: str, root: Path | None, record: dict, plan: dict, state: str,
                priorities: dict, issue_labels: list) -> Iterator[tuple]:
    """The metadata parts of one pass as ``(key, digest, call, kind, ident, ref_id)``, in a fixed order.

    One part per project (fields and status), per milestone, per open task
    (derived priority and the tracker's issue labels). A part's digest covers exactly the
    inputs its call sends, so an unchanged part is never resent. ``kind`` and
    ``ident`` name the local task or approval a NOT_FOUND detaches, and
    ``ref_id`` is the Linear issue the existence check asks about.
    """
    tracker = linear.tracker_of(plan)
    project_id = record["project"]["id"]
    fields = {key: tracker[key] for key in PROJECT_FIELDS if key in tracker}
    yield ("project", linear.part_digest({"fields": fields, "state": state}),
           partial(client.update_project_meta, project_id, _project_meta(tracker, state)), None, None, None)
    milestones = record.get("milestones") or {}
    for col, entry in (tracker.get("milestones") or {}).items():
        if col in milestones:
            description, target = entry.get("description"), entry.get("target_date")
            yield (f"milestone:{col}", linear.part_digest({"description": description, "target_date": target}),
                   partial(client.update_milestone_meta, milestones[col], project_id, description, target),
                   None, None, None)
    detached = set(record.get("detached") or [])
    tasks = record.get("tasks") or {}
    for _group, task in _dag_entries(plan):
        ref = tasks.get(task["id"])
        if ref is None or f"task:{task['id']}" in detached or _task_status(task) in linear.CLOSED_TASK_STATUSES:
            continue
        if task["id"] in priorities:
            priority = priorities[task["id"]]
            yield (f"task:{task['id']}", linear.part_digest({"labels": issue_labels, "priority": priority}),
                   partial(client.set_issue_meta, ref["id"], issue_labels, priority),
                   "task", task["id"], ref["id"])


def _push_meta(client: Client, ws: str, root: Path | None, record: dict, plan: dict, force: bool,
               out_of_time: Callable[[], bool] | None = None) -> bool:
    """Push project, milestone, issue and link metadata in resumable parts. Returns True once the pass is complete.

    Each part's digest is recorded in ``meta_digests`` (linear.json) as its call
    succeeds, and a part whose digest is unchanged is not sent. So a status
    change resends only the issues whose priority moved and never rewrites the
    project fields. A pass cut short by ``out_of_time``, or failed by a call,
    resumes at the first unsent part next cycle. A NOT_FOUND on a task or
    approval issue is confirmed with ``issue_exists`` and detaches it, as the
    flush does. Links are created one call each; ``meta_links`` records each
    url as its create succeeds.
    """
    tracker = linear.tracker_of(plan)
    state = _lifecycle_state(ws, root)
    priorities = linear.derived_priorities(plan)
    issue_labels = list(tracker.get("issue_labels") or [])
    digests = record.get("meta_digests")
    if not isinstance(digests, dict):
        digests = {}
    record["meta_digests"] = digests
    keys: set[str] = set()
    for key, digest, call, kind, ident, ref_id in _meta_parts(client, ws, root, record, plan, state, priorities, issue_labels):
        keys.add(key)
        if not force and digests.get(key) == digest:
            continue
        if out_of_time is not None and out_of_time():
            return False
        try:
            call()
        except LinearError as exc:
            if kind is None or exc.code != "NOT_FOUND" or client.issue_exists(ref_id):
                raise
            _detach(record, kind, ident)
        digests[key] = digest
    project_id = record["project"]["id"]
    sent = set(record.get("meta_links") or [])
    for link in tracker.get("links") or []:
        if link["url"] in sent:
            continue
        if out_of_time is not None and out_of_time():
            return False
        client.create_link(project_id, link["url"], link["title"])
        sent.add(link["url"])
        record["meta_links"] = sorted(sent)
    record["meta_digests"] = {key: value for key, value in digests.items() if key in keys}
    return True


def _push_spec(client: Client, ws: str, root: Path | None, record: dict, ws_data: dict, plan: dict, now: float, force: bool) -> int:
    """Upsert every task, then milestones and blocks relations. Approvals are not mirrored to Linear.

    Detached keys are skipped. Tasks that left the plan are canceled once with a
    note and dropped from the record. Relations whose edge left the plan are
    deleted. Skipped when the rendered spec hash matches the one recorded at the
    last push. Returns the number of issues upserted.
    """
    project_id = record["project"]["id"]
    team_id = ws_data["team"]["id"]
    states = ws_data["states"]
    cols = _cols(plan)
    trains = _train_titles(plan)
    detached = set(record.get("detached") or [])

    entries: list[tuple] = []
    for group, task in _dag_entries(plan):
        if f"task:{task['id']}" in detached:
            continue
        col = _col_name(cols, group)
        rendered = linear.render_issue(ws, task, col, trains.get(task["id"]))
        entries.append(("task", task["id"], rendered, col, task))

    digest = linear.spec_hash([entry[2] for entry in entries])
    if not force and digest == record.get("spec_hash"):
        return 0

    milestones = record.setdefault("milestones", {})
    used = {entry[3] for entry in entries if entry[0] == "task" and entry[3]}
    for index, col in enumerate(cols):
        if col and col in used:
            milestones[col] = client.ensure_milestone(
                _recorded_id(milestones.get(col), linear.stable_id("milestone", ws, col)),
                project_id,
                col,
                float(index),
            )

    tasks = record.setdefault("tasks", {})
    live = {task["id"] for _group, task in _dag_entries(plan)}
    prune = _plan_has_dag(plan) and bool(live)
    for key in list(tasks):
        if not prune:
            break
        if key in live:
            continue
        try:
            client.set_issue_state(tasks[key]["id"], states[linear.STATE_FOR_STATUS["deferred"]])
            client.comment(tasks[key]["id"], "Removed from the plan.")
        except LinearError as exc:
            if exc.code != "NOT_FOUND":
                raise
        del tasks[key]
    count = 0
    for kind, key, rendered, col, obj in entries:
        if kind == "task":
            task_status = _task_status(obj)
            ref = client.upsert_issue(
                _recorded_id(tasks.get(key), linear.stable_id("task", ws, key)),
                team_id,
                project_id,
                milestones.get(col) if col else None,
                rendered["title"],
                rendered["description"],
                state_id=states[linear.STATE_FOR_STATUS[task_status]],
            )
            prior = tasks.get(key) or {}
            entry = {"id": ref["id"], "identifier": ref["identifier"], "url": ref["url"]}
            entry["canceled_noted"] = bool(prior.get("canceled_noted"))
            entry["synced_status"] = task_status
            tasks[key] = entry
            if task_status == "deferred" and not entry["canceled_noted"]:
                client.comment(ref["id"], _cancel_note(obj))
                entry["canceled_noted"] = True
        count += 1

    relations = _relation_map(record)
    wanted: dict[str, tuple[str, str]] = {}
    for _group, task in _dag_entries(plan):
        blocked = task["id"]
        if blocked not in tasks:
            continue
        for dep in bundles._str_list(task.get("deps")):
            if dep in tasks:
                wanted[f"{dep}>{blocked}"] = (dep, blocked)
    for edge in list(relations):
        if not prune:
            break
        if edge in wanted:
            continue
        relation_id = relations.pop(edge)
        if relation_id is None:
            dep, blocked = edge.split(">", 1)
            relation_id = client.find_relation(
                _recorded_id(tasks.get(dep), linear.stable_id("task", ws, dep)),
                _recorded_id(tasks.get(blocked), linear.stable_id("task", ws, blocked)),
            )
        if relation_id is not None:
            client.delete_relation(relation_id)
    for edge, (dep, blocked) in wanted.items():
        if edge not in relations:
            relation_id = client.ensure_blocks(tasks[dep]["id"], tasks[blocked]["id"])
            if relation_id is None:
                relation_id = client.find_relation(tasks[dep]["id"], tasks[blocked]["id"])
            relations[edge] = relation_id

    record["spec_hash"] = digest
    record["pushed_at"] = now
    return count


def _synced_mark(ref: dict, local: str) -> bool:
    """True when the entry's ``synced_status`` differs from ``local``. A missing mark is set to ``local`` and is not drift."""
    synced = ref.get("synced_status")
    if not isinstance(synced, str):
        ref["synced_status"] = local
        return False
    return synced != local


def _drift_keys(record: dict, plan: dict) -> set:
    """Mapped, non-detached task keys whose local status differs from ``synced_status``.

    Records written before ``synced_status`` existed get it initialized from the
    local status here, so they never send a backlog on their first cycle.
    """
    detached = set(record.get("detached") or [])
    index = {task["id"]: task for _group, task in _dag_entries(plan)}
    drift: set = set()
    for key, ref in (record.get("tasks") or {}).items():
        task = index.get(key)
        if task is None or not isinstance(ref, dict) or f"task:{key}" in detached:
            continue
        if _synced_mark(ref, _task_status(task)):
            drift.add(("task", key))
    return drift


def _drop_comment(record: dict) -> None:
    """Count one agent comment that could not be posted (no Linear issue, or its task left the plan)."""
    record["dropped_comments"] = int(record.get("dropped_comments") or 0) + 1


def _send_status(client: Client, record: dict, kind: str, key: str, tasks: dict,
                 index: dict, states: dict) -> int:
    """Send one task's current status. Returns 1 when sent, 0 when skipped (no issue or no plan item).

    Legacy approval outbox rows drain as skips: existing approval issues are left alone.
    """
    if kind == "task":
        ref = tasks.get(key)
        task = index.get(key)
        if ref is None or task is None:
            return 0
        status = _task_status(task)
        try:
            client.set_issue_state(ref["id"], states[linear.STATE_FOR_STATUS[status]])
            if status == "deferred" and not ref.get("canceled_noted"):
                client.comment(ref["id"], _cancel_note(task))
                ref["canceled_noted"] = True
        except LinearError as exc:
            if exc.code != "NOT_FOUND" or client.issue_exists(ref["id"]):
                raise
            _detach(record, kind, key)
    else:
        return 0
    ref["synced_status"] = status
    return 1


def _flush(client: Client, ws: str, root: Path | None, record: dict, ws_data: dict) -> int:
    """Send the current local status of every outbox key and every drift key, then comments, then drain.

    Outbox rows are read first and the plan and approvals after them: a row is
    written only after its plan write, so every row read is visible in the later
    reads and the value sent is never older than the row. The newest row per key
    wins. Drift keys (local status differs from ``synced_status``) are sent the
    same way and need no row. Keys with no mapped issue are skipped. A key whose
    send fails keeps its rows and does not stop the other keys or the comments; the
    first failure is raised after the comments are posted and drained.

    Comment rows (``kind == "comment"``) are not part of the newest-per-key rule:
    each is posted in ts order and drained as soon as it is posted, or dropped.
    A comment is dropped, and counted in ``dropped_comments``, when its task has no
    Linear issue, left the plan, or was deleted in Linear.
    """
    rows = linear.read_outbox(ws, root)
    plan = _plan(ws, root)
    if plan is None:
        raise RuntimeError(f"plan.json for {ws} is unreadable")
    status_rows = [row for row in rows if row["kind"] != "comment"]
    comment_rows = sorted((row for row in rows if row["kind"] == "comment"), key=lambda row: row["ts"])
    newest: dict = {}
    for row in status_rows:
        newest[(row["kind"], row["key"])] = row
    order = list(dict.fromkeys(list(newest) + sorted(_drift_keys(record, plan))))
    if not order and not comment_rows:
        return 0
    tasks = record.get("tasks") or {}
    index = {task["id"]: task for _group, task in _dag_entries(plan)}
    states = ws_data["states"]
    sent = 0
    failures: list[LinearError] = []
    done_keys: set = set()
    for kind, key in order:
        try:
            sent += _send_status(client, record, kind, key, tasks, index, states)
        except LinearError as exc:
            failures.append(exc)
            continue
        done_keys.add((kind, key))
    done_ids = {row["id"] for row in status_rows if (row["kind"], row["key"]) in done_keys}
    try:
        for row in comment_rows:
            ref = tasks.get(row["key"])
            if ref is None or row["key"] not in index:
                _drop_comment(record)
                done_ids.add(row["id"])
                continue
            try:
                client.comment(ref["id"], row["body"])
            except LinearError as exc:
                if exc.code != "NOT_FOUND" or client.issue_exists(ref["id"]):
                    failures.append(exc)
                    continue
                _detach(record, "task", row["key"])
                _drop_comment(record)
                done_ids.add(row["id"])
                continue
            sent += 1
            done_ids.add(row["id"])
    finally:
        if done_ids:
            linear.drain_outbox(ws, done_ids, root)
    if failures:
        raise failures[0]
    return sent


def _apply_task(ws: str, root: Path | None, key: str, state: dict) -> int:
    plan = _plan(ws, root) or {}
    task = {task_obj["id"]: task_obj for _group, task_obj in _dag_entries(plan)}.get(key)
    if task is None:
        return 0
    target = linear.status_for_state(state.get("name"), state.get("type"))
    if target == _task_status(task):
        return 0
    note = "canceled in Linear" if target == "deferred" else None
    with contextlib.redirect_stdout(io.StringIO()):
        rc = bundles.set_task(ws, key, target, moved_to=None, note=note, root=root, force=True, origin="linear")
    if rc == errors.SKIPPED_PENDING:
        return 0
    if rc != 0:
        raise RuntimeError(f"local apply failed for task {key} (exit {rc})")
    return 1


def _pull(client: Client, ws: str, root: Path | None, record: dict) -> tuple[int, int]:
    """Read issues updated since the cursor and apply changed statuses locally.

    Legacy approval issues are not read: approvals are a local decision log, so
    their Linear issues are left alone. A key with a local write still in the outbox, or a drift key (local status
    differs from ``synced_status``), is never applied from Linear: the local
    change is newer and reaches Linear on the next flush. Such an issue counts
    as mapped, and the cursor stops before it so the next cycle reads it again.
    An applied change, or one where Linear already matches local, sets the
    key's ``synced_status``. Returns (mapped issues read, local changes applied).
    The cursor otherwise moves to the newest updatedAt seen, mapped or not.
    """
    drift = _drift_keys(record, _plan(ws, root) or {})
    pending = {(row["kind"], row["key"]) for row in linear.read_outbox(ws, root)} | drift
    reverse: dict[str, tuple[str, str]] = {}
    for key, ref in (record.get("tasks") or {}).items():
        reverse[ref["id"]] = ("task", key)
    known = {sid: name for name, sid in ((linear.read_workspace() or {}).get("states") or {}).items() if isinstance(sid, str)}
    cursor = record.get("cursor")
    issues = client.issues_updated_since(record["project"]["id"], cursor)
    mapped = applied = 0
    held = False
    for issue in issues:
        updated = issue.get("updatedAt")
        hit = reverse.get(issue.get("id"))
        if hit is not None and hit in pending:
            mapped += 1
            held = True
            continue
        if not held and isinstance(updated, str) and (cursor is None or updated > cursor):
            cursor = updated
        if hit is None:
            continue
        mapped += 1
        state = _resolve_state(known, issue.get("state"))
        applied += _apply_task(ws, root, hit[1], state)
        record["tasks"][hit[1]]["synced_status"] = linear.status_for_state(state.get("name"), state.get("type"))
    record["cursor"] = cursor
    return mapped, applied


def _cycle(ws: str, root: Path | None, client: Client, record: dict, now: float, force_push: bool,
           out_of_time: Callable[[], bool] | None = None) -> dict:
    """One spec push, metadata pass, flush and pull. Mutates ``record``; the caller writes it.

    A metadata failure does not stop the flush or the pull: it comes back as
    ``meta_error`` (None when the pass succeeded) and the caller records it.
    """
    ws_data = linear.read_workspace()
    if ws_data is None:
        raise RuntimeError(f"workspace linear.json is missing; run: python3 agent_dash.py ws linear link {ws} --team KEY")
    plan = _plan(ws, root)
    if plan is None:
        raise RuntimeError(f"plan.json for {ws} is unreadable")
    pushed = _push_spec(client, ws, root, record, ws_data, plan, now, force_push)
    meta_error = None
    try:
        _push_meta(client, ws, root, record, plan, force_push, out_of_time)
    except Exception as exc:
        meta_error = _error_text(exc)
    flushed = _flush(client, ws, root, record, ws_data)
    pulled, applied = _pull(client, ws, root, record)
    return {"pushed": pushed, "flushed": flushed, "pulled": pulled, "applied": applied, "meta_error": meta_error}


def _out_of_time(budget_s: float | None, clock: Callable[[], float]) -> Callable[[], bool] | None:
    """A check that turns True once ``budget_s`` seconds of ``clock`` have passed; None when unbounded."""
    if budget_s is None:
        return None
    deadline = clock() + budget_s
    return lambda: clock() >= deadline


def sync(ws: str, root: Path | None = None, client=None, now: float | None = None,
         meta_budget_s: float | None = META_BUDGET_S, clock: Callable[[], float] = time.monotonic) -> dict:
    """One sync cycle under ``.linear.lock``. Never raises.

    The metadata pass stops after ``meta_budget_s`` seconds and resumes in the
    next cycle (the per-part ``meta_digests`` in the link record), so one cycle
    never runs for minutes; None means unbounded. Returns {'pushed', 'flushed', 'pulled',
    'applied', 'error'}. A failed cycle records ``last_error`` and leaves
    ``synced_at`` alone; a completed one sets ``synced_at`` and clears
    ``last_error``.
    """
    stamp = _now(now)
    result: dict = {"pushed": 0, "flushed": 0, "pulled": 0, "applied": 0, "error": None}
    try:
        path = bundles.bundle_dir(ws, root)
        if path is None:
            result["error"] = f"bundle not found: {ws}"
            return result
        with _sync_lock(path):
            record = linear.read_link(ws, root)
            if record is None:
                result["error"] = f"not linked: run python3 agent_dash.py ws linear link {ws} --team KEY"
                return result
            try:
                counts = _cycle(ws, root, client if client is not None else _make_client(), record, stamp, False,
                                _out_of_time(meta_budget_s, clock))
            except Exception as exc:
                record["last_error"] = _error_text(exc)
                linear.write_link(ws, record, root)
                result["error"] = record["last_error"]
                return result
            meta_error = counts.pop("meta_error", None)
            if meta_error:
                record["last_error"] = meta_error
                linear.write_link(ws, record, root)
                result.update(counts)
                result["error"] = meta_error
                return result
            record["synced_at"] = stamp
            record["last_error"] = None
            linear.write_link(ws, record, root)
            result.update(counts)
            return result
    except Exception as exc:
        result["error"] = _error_text(exc)
        return result


def _ensure_workspace(client: Client, team_key: str, ws: str, root: Path | None) -> dict:
    team = client.team(team_key)
    team_id = team["id"]
    states: dict[str, str] = {}
    for name, state_type, color in WORKFLOW_STATES:
        states[name] = client.ensure_state(
            team_id, linear.stable_id("state", team_key, name), name, state_type, color
        )
    label_id = client.ensure_label(team_id, linear.APPROVAL_LABEL)
    data, _path, _issues = project.read_project()
    initiative_name = data.get("name") if isinstance(data, dict) and isinstance(data.get("name"), str) else None
    initiative = client.ensure_initiative(initiative_name or _bundle_title(ws, root))
    ws_data = {
        "team": {"id": team_id, "key": team_key},
        "states": states,
        "initiative": initiative,
        "approval_label": label_id,
    }
    linear.write_workspace(ws_data)
    return ws_data


def link(ws: str, team_key: str, root: Path | None = None, client=None, now: float | None = None) -> int:
    """Link one bundle to a Linear project and run a full push. Returns the CLI exit code.

    Refuses when not signed in (no OAuth token and no ``LINEAR_API_KEY``). Re-linking a linked bundle is a
    full push that updates in place, so it creates nothing twice.
    """
    if linear_auth.credential() is None:
        print(NOT_SIGNED_IN, flush=True)
        return errors.USAGE
    path = bundles.bundle_dir(ws, root)
    if path is None:
        print(errors.bundle_not_found(ws), flush=True)
        return errors.NOT_FOUND
    existing_ws = linear.read_workspace()
    if existing_ws is not None and (existing_ws.get("team") or {}).get("key") not in (None, team_key):
        print(f"agent-dash is linked to team {existing_ws['team']['key']}, not {team_key}. Use --team {existing_ws['team']['key']}", flush=True)
        return errors.USAGE
    stamp = _now(now)
    record: dict | None = None
    try:
        with _sync_lock(path):
            existing = linear.read_link(ws, root)
            if existing is not None and existing.get("team_key") not in (None, team_key):
                print(f"{ws} is linked to team {existing['team_key']}, not {team_key}. Run: python3 agent_dash.py ws linear unlink {ws} first", flush=True)
                return errors.USAGE
            api = client if client is not None else _make_client()
            ws_data = _ensure_workspace(api, team_key, ws, root)
            record = existing or {
                "version": 1,
                "team_key": team_key,
                "project": {},
                "milestones": {},
                "tasks": {},
                "approvals": {},
                "relations": [],
                "spec_hash": None,
                "cursor": None,
                "linked_at": stamp,
                "pushed_at": 0.0,
                "synced_at": 0.0,
                "last_error": None,
            }
            title = _bundle_title(ws, root)
            proj = api.ensure_project(
                linear.stable_id("project", ws),
                title,
                ws_data["team"]["id"],
                f"Workstream {ws}: {title}. Tasks mirror the agent-dash bundle.",
            )
            api.add_project_to_initiative(ws_data["initiative"]["id"], proj["id"])
            record["project"] = proj
            linear.write_link(ws, record, root)
            meta_error = _cycle(ws, root, api, record, stamp, True)["meta_error"]
            if meta_error:
                raise RuntimeError(meta_error)
            record["synced_at"] = stamp
            record["last_error"] = None
            linear.write_link(ws, record, root)
    except Exception as exc:
        message = _error_text(exc)
        if record is not None:
            record["last_error"] = message
            linear.write_link(ws, record, root)
        print(f"linear link failed for {ws}: {message}. Run: python3 agent_dash.py ws linear sync {ws}", flush=True)
        return errors.CHECK_FAILED
    print(record["project"]["url"], flush=True)
    return 0


def _age(now: float, stamp: float | None) -> str:
    if not stamp:
        return "never"
    seconds = max(0, int(now - stamp))
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def status(ws: str, root: Path | None = None, as_json: bool = False) -> int:
    """Print the Linear link state of one bundle: human lines, or one JSON object."""
    if bundles.bundle_dir(ws, root) is None:
        print(errors.bundle_not_found(ws), flush=True)
        return errors.NOT_FOUND
    record = linear.read_link(ws, root)
    if record is None:
        if as_json:
            errors.emit_json({"ws": ws, "linked": False})
        else:
            print(f"{ws} is not linked to Linear. Run: python3 agent_dash.py ws linear link {ws} --team KEY")
        return errors.OK
    state = linear.sync_state(ws, root) or {}
    payload = {
        "ws": ws,
        "linked": True,
        "url": state.get("url"),
        "project": state.get("project"),
        "synced_at": state.get("synced_at"),
        "last_error": state.get("last_error"),
        "outbox": state.get("outbox", 0),
        "linked_tasks": len(record.get("tasks") or {}),
        "dropped_comments": int(record.get("dropped_comments") or 0),
    }
    if as_json:
        errors.emit_json(payload)
        return errors.OK
    print(f"{ws} project: {payload['url'] or '-'}")
    print(f"synced: {_age(time.time(), payload['synced_at'])}")
    print(f"last error: {payload['last_error'] or 'none'}")
    print(f"outbox: {payload['outbox']}")
    print(f"linked tasks: {payload['linked_tasks']}")
    print(f"dropped comments: {payload['dropped_comments']}")
    return errors.OK


def unlink(ws: str, root: Path | None = None) -> int:
    """Remove ``linear.json`` and the outbox. Linear issues and the project stay."""
    path = bundles.bundle_dir(ws, root)
    if path is None:
        print(errors.bundle_not_found(ws), flush=True)
        return errors.NOT_FOUND
    record = linear.read_link(ws, root)
    if record is None:
        print(f"{ws} is not linked; nothing to remove")
        return errors.OK
    project_url = (record.get("project") or {}).get("url") or "-"
    with _sync_lock(path), linear._outbox_lock(path):
        (path / linear.LINK_FILE).unlink(missing_ok=True)
        (path / linear.OUTBOX_FILE).unlink(missing_ok=True)
    print(f"unlinked {ws}. Kept in Linear: project {project_url} and its issues (not deleted).")
    return errors.OK


def train_refs(ws: str, train: dict, root: Path | None = None) -> str | None:
    """'Fixes ENG-1, ENG-2' for the train's linked members, None when unlinked or none are linked."""
    record = linear.read_link(ws, root)
    if record is None:
        return None
    tasks = record.get("tasks") or {}
    refs = []
    for member in bundles._str_list(train.get("members")):
        entry = tasks.get(member)
        if isinstance(entry, dict) and entry.get("identifier"):
            refs.append(entry["identifier"])
    return f"Fixes {', '.join(refs)}" if refs else None


def run(sub: str, ws: str, team_key: str | None = None, json_mode: bool = False) -> int:
    """CLI entry for ``ws linear <sub> <ws>``. Returns the exit code."""
    if sub == "link":
        return link(ws, team_key or "")
    if bundles.bundle_dir(ws) is None:
        print(errors.bundle_not_found(ws), flush=True)
        return errors.NOT_FOUND
    if sub == "sync":
        result = sync(ws)
        if json_mode:
            errors.emit_json({"ws": ws, **result})
        elif result["error"]:
            print(f"linear sync failed for {ws}: {result['error']}. Run: python3 agent_dash.py ws linear status {ws}")
        else:
            print(f"{ws} linear sync: pushed {result['pushed']}, flushed {result['flushed']}, pulled {result['pulled']}, applied {result['applied']}")
        return errors.CHECK_FAILED if result["error"] else errors.OK
    if sub == "status":
        return status(ws, as_json=json_mode)
    return unlink(ws)
