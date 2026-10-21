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
from pathlib import Path

from dash import bundles, errors, linear, project
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


def _make_client() -> Client:
    key = linear.api_key()
    if key is None:
        raise LinearError("AUTH", f"{linear.ENV_KEY} is not set")
    return Client(key)


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


def _approval_items(ws: str, root: Path | None) -> dict[str, dict]:
    record = bundles.approvals_for(ws, root) or {}
    return {
        item["id"]: item
        for item in record.get("items") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


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


def _push(client: Client, ws: str, root: Path | None, record: dict, ws_data: dict, plan: dict, now: float, force: bool) -> int:
    """Upsert every task and pending approval, then milestones and blocks relations.

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
    for item in _approval_items(ws, root).values():
        if item.get("status") == "pending" and f"approval:{item['id']}" not in detached:
            entries.append(("approval", item["id"], linear.render_approval(ws, item), None, item))

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
    approvals = record.setdefault("approvals", {})
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
            tasks[key] = entry
            if task_status == "deferred" and not entry["canceled_noted"]:
                client.comment(ref["id"], _cancel_note(obj))
                entry["canceled_noted"] = True
        else:
            ref = client.upsert_issue(
                _recorded_id(approvals.get(key), linear.stable_id("approval", ws, key)),
                team_id,
                project_id,
                None,
                rendered["title"],
                rendered["description"],
                state_id=states[linear.APPROVAL_STATE["pending"]],
                label_ids=[ws_data["approval_label"]],
            )
            approvals[key] = {"id": ref["id"], "identifier": ref["identifier"], "url": ref["url"]}
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


def _flush(client: Client, ws: str, root: Path | None, record: dict, ws_data: dict) -> int:
    """Send the current local status of every outbox key, then drain the outbox.

    Rows are read first and the plan and approvals are read after them: a
    row is written only after its plan write, so every row read is visible
    in the later reads and the value sent is never older than the row. The
    newest row per key wins. Keys with no mapped issue are skipped. The
    rows read are drained by id, so rows appended meanwhile stay; a failure leaves it
    untouched and the next cycle resends (state sets are idempotent).
    """
    rows = linear.read_outbox(ws, root)
    if not rows:
        return 0
    newest: dict[tuple[str, str], dict] = {}
    for row in rows:
        newest[(row["kind"], row["key"])] = row
    plan = _plan(ws, root)
    if plan is None:
        raise RuntimeError(f"plan.json for {ws} is unreadable")
    tasks = record.get("tasks") or {}
    approvals = record.get("approvals") or {}
    index = {task["id"]: task for _group, task in _dag_entries(plan)}
    items = _approval_items(ws, root)
    states = ws_data["states"]
    sent = 0
    for kind, key in newest:
        if kind == "task":
            ref = tasks.get(key)
            task = index.get(key)
            if ref is None or task is None:
                continue
            task_status = _task_status(task)
            try:
                client.set_issue_state(ref["id"], states[linear.STATE_FOR_STATUS[task_status]])
                if task_status == "deferred" and not ref.get("canceled_noted"):
                    client.comment(ref["id"], _cancel_note(task))
                    ref["canceled_noted"] = True
            except LinearError as exc:
                if exc.code != "NOT_FOUND" or client.issue_exists(ref["id"]):
                    raise
                _detach(record, kind, key)
        else:
            ref = approvals.get(key)
            item = items.get(key)
            if ref is None or item is None or item.get("status") not in linear.APPROVAL_STATE:
                continue
            try:
                client.set_issue_state(ref["id"], states[linear.APPROVAL_STATE[item["status"]]])
            except LinearError as exc:
                if exc.code != "NOT_FOUND" or client.issue_exists(ref["id"]):
                    raise
                _detach(record, kind, key)
        sent += 1
    linear.drain_outbox(ws, {row["id"] for row in rows}, root)
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


def _apply_approval(ws: str, root: Path | None, key: str, state: dict) -> int:
    item = _approval_items(ws, root).get(key)
    if item is None:
        return 0
    target = linear.approval_for_state(state.get("type"))
    if target == item.get("status"):
        return 0
    with contextlib.redirect_stdout(io.StringIO()):
        rc = bundles.set_approval(
            ws,
            key,
            target,
            step=item.get("step"),
            reversible=item.get("reversible"),
            conditions=item.get("conditions"),
            note=item.get("note"),
            root=root,
            origin="linear",
        )
    if rc == errors.SKIPPED_PENDING:
        return 0
    if rc != 0:
        raise RuntimeError(f"local apply failed for approval {key} (exit {rc})")
    return 1


def _pull(client: Client, ws: str, root: Path | None, record: dict) -> tuple[int, int]:
    """Read issues updated since the cursor and apply changed statuses locally.

    A key with a local write still in the outbox is never applied from
    Linear: the local change is newer and reaches Linear on the next flush.
    Such an issue counts as mapped, and the cursor stops before it so the
    next cycle reads it again. Returns (mapped issues read, local changes applied).
    The cursor otherwise moves to the newest updatedAt seen, mapped or not.
    """
    pending = {(row["kind"], row["key"]) for row in linear.read_outbox(ws, root)}
    reverse: dict[str, tuple[str, str]] = {}
    for key, ref in (record.get("tasks") or {}).items():
        reverse[ref["id"]] = ("task", key)
    for key, ref in (record.get("approvals") or {}).items():
        reverse[ref["id"]] = ("approval", key)
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
        if hit[0] == "task":
            applied += _apply_task(ws, root, hit[1], state)
        else:
            applied += _apply_approval(ws, root, hit[1], state)
    record["cursor"] = cursor
    return mapped, applied


def _cycle(ws: str, root: Path | None, client: Client, record: dict, now: float, force_push: bool) -> dict:
    """One push, flush and pull pass. Mutates ``record``; the caller writes it."""
    ws_data = linear.read_workspace()
    if ws_data is None:
        raise RuntimeError(f"workspace linear.json is missing; run: python3 agent_dash.py ws linear link {ws} --team KEY")
    plan = _plan(ws, root)
    if plan is None:
        raise RuntimeError(f"plan.json for {ws} is unreadable")
    pushed = _push(client, ws, root, record, ws_data, plan, now, force_push)
    flushed = _flush(client, ws, root, record, ws_data)
    pulled, applied = _pull(client, ws, root, record)
    return {"pushed": pushed, "flushed": flushed, "pulled": pulled, "applied": applied}


def sync(ws: str, root: Path | None = None, client=None, now: float | None = None) -> dict:
    """One sync cycle under ``.linear.lock``. Never raises.

    Returns {'pushed', 'flushed', 'pulled', 'applied', 'error'}. A failed cycle
    records ``last_error`` and leaves ``synced_at`` alone; a completed one sets
    ``synced_at`` and clears ``last_error``.
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
                counts = _cycle(ws, root, client if client is not None else _make_client(), record, stamp, False)
            except Exception as exc:
                record["last_error"] = _error_text(exc)
                linear.write_link(ws, record, root)
                result["error"] = record["last_error"]
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

    Refuses without ``LINEAR_API_KEY``. Re-linking a linked bundle is a
    full push that updates in place, so it creates nothing twice.
    """
    if linear.api_key() is None:
        print(f"{linear.ENV_KEY} is not set. Export the Linear API key, then run: python3 agent_dash.py ws linear link {ws} --team {team_key}", flush=True)
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
                f"Workstream {ws}: {title}. Tasks and pending approvals mirror the agent-dash bundle.",
            )
            api.add_project_to_initiative(ws_data["initiative"]["id"], proj["id"])
            record["project"] = proj
            linear.write_link(ws, record, root)
            _cycle(ws, root, api, record, stamp, True)
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
    }
    if as_json:
        errors.emit_json(payload)
        return errors.OK
    print(f"{ws} project: {payload['url'] or '-'}")
    print(f"synced: {_age(time.time(), payload['synced_at'])}")
    print(f"last error: {payload['last_error'] or 'none'}")
    print(f"outbox: {payload['outbox']}")
    print(f"linked tasks: {payload['linked_tasks']}")
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
