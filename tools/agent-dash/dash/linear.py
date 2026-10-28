"""Linear link model: stable ids, status mapping, rendered issues and the outbox.

Pure local logic. Nothing here touches the network or writes plan.json,
approvals.json or history.jsonl: ``dash/linear_sync.py`` and
``dash/linear_client.py`` do the I/O and call these helpers.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from datetime import date
from pathlib import Path

from dash import dataroot
from dash.bundles import _atomic_write, _file_stamp, _read_json, bundle_dir
from dash.orchestrate import _groups_of, critical_path, plan_tasks, ready_queue

ENV_KEY = "LINEAR_API_KEY"
LINK_FILE = "linear.json"
OUTBOX_FILE = "linear-outbox.jsonl"
SYNC_LOCK = ".linear.lock"
OUTBOX_LOCK = ".linear-outbox.lock"
AWAITING_PROD = "Awaiting prod"
APPROVAL_LABEL = "approval"
COMMENT_LIMIT = 2000

STATE_FOR_STATUS: dict[str, str] = {
    "todo": "Todo",
    "wip": "In Progress",
    "pr": "In Review",
    "prog": AWAITING_PROD,
    "done": "Done",
    "deferred": "Canceled",
}

APPROVAL_STATE: dict[str, str] = {
    "pending": "Todo",
    "approved": "Done",
    "denied": "Canceled",
}

_STATUS_BY_NAME: dict[str, str] = {name: status for status, name in STATE_FOR_STATUS.items()}
_STATUS_BY_NAME["Backlog"] = "todo"
_STATUS_BY_NAME["Duplicate"] = "deferred"

_STATUS_BY_TYPE: dict[str, str] = {
    "triage": "todo",
    "backlog": "todo",
    "unstarted": "todo",
    "started": "wip",
    "completed": "done",
    "canceled": "deferred",
    "duplicate": "deferred",
}

_ACCEPTANCE_LABELS = (
    ("metric", "Metric"),
    ("probe", "Probe"),
    ("threshold", "Threshold"),
    ("tolerance", "Tolerance"),
)

_RENDER_FOOTER = (
    "_Spec lives in the agent-dash plan; edits here are overwritten on the next sync. "
    "Status syncs both ways._"
)

_KEY_SHAPE = re.compile(r"[\x21-\x7e]+")



def api_key() -> str | None:
    """The Linear API key from ``LINEAR_API_KEY``, or None when unset, blank or malformed.

    A key is one run of printable ASCII without spaces once the ends are
    stripped. Anything else (an embedded newline or space) is rejected, so a
    bad key can never reach an http.client error message.
    """
    raw = os.environ.get(ENV_KEY, "").strip()
    if not raw or _KEY_SHAPE.fullmatch(raw) is None:
        return None
    return raw


def stable_id(*parts: str) -> str:
    """Deterministic UUID-v4-formatted id: sha256 of 'agent-dash:' + ':'.join(parts), first 16 bytes, version 4 and RFC 4122 variant bits set."""
    digest = hashlib.sha256(("agent-dash:" + ":".join(parts)).encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def status_for_state(name: str | None, state_type: str | None) -> str:
    """The agent-dash status for a Linear state, by name first, then by state type.

    An unknown name and an unknown type map to ``todo``.
    """
    if name in _STATUS_BY_NAME:
        return _STATUS_BY_NAME[name]
    return _STATUS_BY_TYPE.get((state_type or "").lower(), "todo")


def approval_for_state(state_type: str | None) -> str:
    """The approval status for a Linear state type: completed is approved, canceled or duplicate is denied, else pending."""
    kind = (state_type or "").lower()
    if kind == "completed":
        return "approved"
    if kind in ("canceled", "duplicate"):
        return "denied"
    return "pending"


def _present(value) -> bool:
    return value not in (None, "", [], {})


def _strip_ticks(text: str) -> str:
    return text.strip().strip("`").strip()


def render_issue(ws: str, task: dict, col: str | None, train: str | None) -> dict:
    """{'title': task title, 'description': markdown}. Description: first line 'Task `<task id>` in workstream <ws>' plus section, wave, size, train when present; then Acceptance (metric, probe, threshold, tolerance), Files (bullets), Test command, Rollback, Deploy tier, Approval, Depends on (task ids with dep_notes); last line '_Spec lives in the agent-dash plan; edits here are overwritten on the next sync. Status syncs both ways._'. Deterministic output for equal input."""
    tid = str(task.get("id") or "")
    title = str(task.get("title") or tid)
    blocks: list[str] = []

    meta = [
        f"- {label}: {value}"
        for label, value in (
            ("Section", col),
            ("Wave", task.get("wave")),
            ("Size", task.get("size")),
            ("Train", train),
        )
        if _present(value)
    ]
    header = f"Task `{tid}` in workstream {ws}"
    if meta:
        header += "\n\n" + "\n".join(meta)
    blocks.append(header)

    acceptance = task.get("acceptance")
    if isinstance(acceptance, dict):
        rows = [f"- {label}: {acceptance[key]}" for key, label in _ACCEPTANCE_LABELS if _present(acceptance.get(key))]
        if rows:
            blocks.append("## Acceptance\n\n" + "\n".join(rows))

    files = task.get("files")
    if isinstance(files, list) and files:
        blocks.append("## Files\n\n" + "\n".join(f"- `{path}`" for path in files))

    if _present(task.get("test_cmd")):
        raw = str(task["test_cmd"]).strip()
        bare = _strip_ticks(raw)
        blocks.append("## Test command\n\n" + (raw if "`" in bare else "`" + bare + "`"))

    for key, label in (("rollback", "Rollback"), ("deploy_tier", "Deploy tier"), ("approval", "Approval")):
        if _present(task.get(key)):
            blocks.append(f"## {label}\n\n{task[key]}")

    deps = task.get("deps")
    if isinstance(deps, list) and deps:
        notes = task.get("dep_notes") if isinstance(task.get("dep_notes"), dict) else {}
        rows = []
        for dep in deps:
            note = notes.get(dep)
            rows.append(f"- `{dep}`: {note}" if _present(note) else f"- `{dep}`")
        blocks.append("## Depends on\n\n" + "\n".join(rows))

    blocks.append(_RENDER_FOOTER)
    return {"title": title, "description": "\n\n".join(blocks) + "\n"}


def spec_hash(rendered: list[dict]) -> str:
    """sha256 of the canonical JSON of the rendered issues; equal content gives an equal hash."""
    text = json.dumps(rendered, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def link_path(ws: str, root: Path | None = None) -> Path | None:
    """``<bundle>/linear.json``, or None when the bundle directory does not exist."""
    path = bundle_dir(ws, root)
    if path is None:
        return None
    return path / LINK_FILE


def _outbox_path(ws: str, root: Path | None) -> Path | None:
    path = bundle_dir(ws, root)
    if path is None:
        return None
    return path / OUTBOX_FILE


def read_link(ws: str, root: Path | None = None) -> dict | None:
    """The parsed ``linear.json`` for a bundle, or None when unlinked or unreadable."""
    path = link_path(ws, root)
    if path is None or not path.is_file():
        return None
    data = _read_json(path)
    return data if isinstance(data, dict) else None


def _write_unique(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a uniquely named temp file and os.replace.

    Two concurrent writers each get their own temp file, so neither can
    replace the other's half-written file. The temp file is removed on error.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".linear-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_link(ws: str, data: dict, root: Path | None = None) -> None:
    """Write ``linear.json`` through a unique temp file and os.replace. Raises ValueError for an unknown bundle."""
    path = link_path(ws, root)
    if path is None:
        raise ValueError(f"unknown workstream bundle: {ws}")
    _write_unique(path, json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def is_linked(ws: str, root: Path | None = None) -> bool:
    """True when the bundle has a ``linear.json`` file."""
    path = link_path(ws, root)
    return path is not None and path.is_file()


def workspace_path() -> Path:
    """``<data_root>/linear.json``: the workspace-level ids."""
    return dataroot.data_root() / LINK_FILE


def read_workspace() -> dict | None:
    """The parsed workspace ``linear.json``, or None when absent or unreadable."""
    path = workspace_path()
    if not path.is_file():
        return None
    data = _read_json(path)
    return data if isinstance(data, dict) else None


def write_workspace(data: dict) -> None:
    """Write the workspace ``linear.json`` through a unique temp file and os.replace."""
    _write_unique(workspace_path(), json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


@contextlib.contextmanager
def _outbox_lock(bundle: Path):
    """Short exclusive lock around one outbox append, read or rewrite.

    Separate from the sync cycle lock, so a local write never waits on a
    running sync: it only serializes the append against a drain's rewrite.
    Lock order is the bundle ``.lock`` first, then this one, never the reverse.
    """
    with open(bundle / OUTBOX_LOCK, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _parse_rows(path: Path) -> list[dict]:
    """Valid outbox rows in file order. A row without an ``id`` (written before ids existed) gets one derived from its line, so it drains like any other."""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(row, dict)
            and isinstance(row.get("ts"), (int, float))
            and row.get("kind") in ("task", "approval", "comment")
            and isinstance(row.get("key"), str)
            and (row.get("kind") != "comment" or isinstance(row.get("body"), str))
        ):
            row.setdefault("id", hashlib.sha256(line.encode("utf-8")).hexdigest()[:32])
            rows.append(row)
    return rows


def _append_row(ws: str, root: Path | None, fields: dict, now: float | None) -> bool:
    """Append one outbox row built from ``fields``; False when the bundle is not linked.

    The row gets a unique ``id`` and its ``ts`` is stamped inside the outbox
    lock, so rows are ordered by when they were appended. One O_APPEND line,
    fsynced. Raises OSError on a failed append.
    """
    bundle = bundle_dir(ws, root)
    if bundle is None or not is_linked(ws, root):
        return False
    with _outbox_lock(bundle):
        if not is_linked(ws, root):
            return False
        row = {"id": uuid.uuid4().hex, "ts": time.time() if now is None else now, **fields}
        with open(bundle / OUTBOX_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return True


def enqueue(ws: str, kind: str, key: str, root: Path | None = None, now: float | None = None) -> None:
    """Record one local write for the next sync. No-op when the bundle is not linked.

    Callers hold the bundle ``.lock`` across the plan write and this call, so
    a pull that holds the same lock sees either both or neither. A failed
    append warns and returns, so the local write itself is never blocked.
    """
    try:
        _append_row(ws, root, {"kind": kind, "key": key}, now)
    except OSError as exc:
        print(f"linear outbox not recorded for {ws} {key}: {exc}", flush=True)


def enqueue_comment(ws: str, task_id: str, body: str, root: Path | None = None, now: float | None = None) -> None:
    """Queue one agent comment on a task's Linear issue for the next sync.

    ``body`` is trimmed. Raises ValueError when the text is empty or longer
    than ``COMMENT_LIMIT`` characters, when the bundle is not linked, or when
    the append fails, so the CLI can refuse with a message. Comments do not
    change status and are never deduplicated by key.
    """
    text = body.strip()
    if not text:
        raise ValueError("comment text is empty")
    if len(text) > COMMENT_LIMIT:
        raise ValueError(f"comment is {len(text)} characters; the limit is {COMMENT_LIMIT}")
    if not is_linked(ws, root):
        raise ValueError(f"workstream {ws} is not linked to Linear")
    try:
        queued = _append_row(ws, root, {"kind": "comment", "key": task_id, "body": text}, now)
    except OSError as exc:
        raise ValueError(f"linear outbox not recorded for {ws} {task_id}: {exc}") from exc
    if not queued:
        raise ValueError(f"workstream {ws} is not linked to Linear")


def has_pending(ws: str, kind: str, key: str, root: Path | None = None) -> bool:
    """True when the outbox holds a row for this kind and key, read under the outbox lock."""
    bundle = bundle_dir(ws, root)
    if bundle is None:
        return False
    with _outbox_lock(bundle):
        return any(row["kind"] == kind and row["key"] == key for row in _parse_rows(bundle / OUTBOX_FILE))


def read_outbox(ws: str, root: Path | None = None) -> list[dict]:
    """The pending outbox rows, oldest first. Malformed lines are skipped."""
    path = _outbox_path(ws, root)
    if path is None:
        return []
    return _parse_rows(path)


def drain_outbox(ws: str, ids: set[str], root: Path | None = None) -> None:
    """Remove exactly the outbox rows whose ``id`` is in ``ids``, then rewrite the file (temp + replace).

    Rows not in ``ids`` stay, including any appended after the caller read
    the outbox. An unlinked bundle with no outbox file is a no-op.
    """
    bundle = bundle_dir(ws, root)
    if bundle is None:
        return
    path = bundle / OUTBOX_FILE
    with _outbox_lock(bundle):
        keep = [row for row in _parse_rows(path) if row["id"] not in ids]
        if not path.exists() and not keep:
            return
        text = "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in keep)
        _atomic_write(path, text)


def sync_state(ws: str, root: Path | None = None) -> dict | None:
    """None when unlinked, else {'url', 'synced_at', 'last_error', 'outbox': int, 'project': name}."""
    link = read_link(ws, root)
    if link is None:
        return None
    project = link.get("project") if isinstance(link.get("project"), dict) else {}
    return {
        "url": project.get("url"),
        "synced_at": link.get("synced_at"),
        "last_error": link.get("last_error"),
        "outbox": len(read_outbox(ws, root)),
        "project": project.get("name"),
    }


def _https_url(value) -> str | None:
    """The value when it is a string starting with ``https://``, else None. The page renders these as hrefs."""
    return value if isinstance(value, str) and value.startswith("https://") else None


def page_link(ws: str, root: Path | None = None) -> dict | None:
    """None when unlinked, else {'url', 'synced_at'}: the only Linear data the page payload carries at plan level.

    ``url`` is None unless it is an https URL, so a stored ``javascript:`` or ``http:`` value never reaches an href.
    """
    link = read_link(ws, root)
    if link is None:
        return None
    project = link.get("project") if isinstance(link.get("project"), dict) else {}
    return {"url": _https_url(project.get("url")), "synced_at": link.get("synced_at")}


def task_links(ws: str, root: Path | None = None) -> dict[str, dict]:
    """{task_id: {'identifier', 'url'}}; empty when unlinked.

    ``url`` is None unless it is an https URL; the identifier is kept either way.
    """
    link = read_link(ws, root)
    tasks = link.get("tasks") if link is not None else None
    if not isinstance(tasks, dict):
        return {}
    return {
        tid: {"identifier": entry.get("identifier"), "url": _https_url(entry.get("url"))}
        for tid, entry in tasks.items()
        if isinstance(entry, dict)
    }


def stamp(ws: str, root: Path | None = None) -> tuple:
    """(mtime_ns, size) of the bundle's linear.json, (0, 0) when absent; folded into plan caches."""
    path = link_path(ws, root)
    if path is None:
        return (0, 0)
    return _file_stamp(path) or (0, 0)


PROJECT_STATUS_BY_STATE: dict[str, str | None] = {
    "planned": "Planned",
    "active": "In Progress",
    "paused": "Paused",
    "done": "Completed",
    "dropped": "Canceled",
    "archived": None,
}

CLOSED_TASK_STATUSES = ("done", "deferred")

PRIORITY_CRITICAL = 2
PRIORITY_READY = 3
PRIORITY_OTHER = 4

_HEALTH_VALUES = ("onTrack", "atRisk", "offTrack")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _clean_text(value, limit: int | None = None) -> str | None:
    """The stripped string, or None when it is not a non-empty string within ``limit``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or (limit is not None and len(text) > limit):
        return None
    return text


def _clean_date(value) -> str | None:
    """The value when it is an ISO calendar date (YYYY-MM-DD), else None."""
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _clean_priority(value) -> int | None:
    """The value when it is an int from 0 to 4 (Linear's scale; bool is not an int here), else None."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4:
        return None
    return value


def _clean_names(value) -> list[str]:
    """Distinct non-empty label names, in order; anything that is not a list of strings gives []."""
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        text = _clean_text(item)
        if text is not None and text not in names:
            names.append(text)
    return names


def _clean_links(value) -> list[dict]:
    """{'title', 'url'} rows with a title and an https url; other rows are dropped."""
    if not isinstance(value, list):
        return []
    links = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title"), 255)
        url = _https_url(item.get("url"))
        if title is not None and url is not None:
            links.append({"title": title, "url": url})
    return links


def _clean_milestones(value) -> dict[str, dict]:
    """{column: {'description'?, 'target_date'?}} for each column that carries a valid field."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict] = {}
    for col, entry in value.items():
        if not isinstance(col, str) or not col.strip() or not isinstance(entry, dict):
            continue
        clean: dict = {}
        description = _clean_text(entry.get("description"))
        if description is not None:
            clean["description"] = description
        target = _clean_date(entry.get("target_date"))
        if target is not None:
            clean["target_date"] = target
        if clean:
            out[col.strip()] = clean
    return out


def _clean_update(value) -> dict:
    """{'health'?, 'daily_hour'?} with a known health word and an hour from 0 to 23."""
    if not isinstance(value, dict):
        return {}
    clean: dict = {}
    if value.get("health") in _HEALTH_VALUES:
        clean["health"] = value["health"]
    hour = value.get("daily_hour")
    if not isinstance(hour, bool) and isinstance(hour, int) and 0 <= hour <= 23:
        clean["daily_hour"] = hour
    return clean


def tracker_of(plan) -> dict:
    """The validated ``tracker`` section of a plan, or {} when absent.

    Every field is type-checked; an invalid value is dropped, and keys the
    schema does not name are dropped too. Empty fields are omitted from the
    result, so callers test ``in`` rather than ``None``.
    """
    raw = plan.get("tracker") if isinstance(plan, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key, limit in (("summary", 255), ("description", None), ("lead", None)):
        text = _clean_text(raw.get(key), limit)
        if text is not None:
            out[key] = text
    for key in ("labels", "issue_labels"):
        names = _clean_names(raw.get(key))
        if names:
            out[key] = names
    priority = _clean_priority(raw.get("priority"))
    if priority is not None:
        out["priority"] = priority
    for key in ("start_date", "target_date"):
        day = _clean_date(raw.get(key))
        if day is not None:
            out[key] = day
    links = _clean_links(raw.get("links"))
    if links:
        out["links"] = links
    milestones = _clean_milestones(raw.get("milestones"))
    if milestones:
        out["milestones"] = milestones
    update = _clean_update(raw.get("update"))
    if update:
        out["update"] = update
    return out


def derived_priorities(plan) -> dict[str, int]:
    """Priority per open task id: critical path 2, ready now 3, other open 4.

    Open means any status other than done or deferred; closed tasks are absent
    from the result because the sync never touches them. Ready is the
    ``ready_queue`` rule (status todo, every dep satisfied). The critical path
    is computed over the open tasks only, so closed tasks never shape it. A
    critical-path task that is also ready takes the higher priority (2).
    """
    if not isinstance(plan, dict):
        return {}
    by_id, _deps, _order = plan_tasks(plan)
    critical = set(critical_path(_open_body(plan)))
    ready = {row["id"] for row in ready_queue(plan)}
    out: dict[str, int] = {}
    for tid, task in by_id.items():
        if task.get("status", "todo") in CLOSED_TASK_STATUSES:
            continue
        if tid in critical:
            out[tid] = PRIORITY_CRITICAL
        elif tid in ready:
            out[tid] = PRIORITY_READY
        else:
            out[tid] = PRIORITY_OTHER
    return out


def _open_body(plan: dict) -> dict:
    """A plan body holding only the open tasks (no done or deferred ones), in the same group shape."""
    groups = []
    for group in _groups_of(plan):
        tasks = group.get("tasks") if isinstance(group, dict) else None
        open_tasks = [
            task
            for task in tasks or []
            if isinstance(task, dict) and task.get("status", "todo") not in CLOSED_TASK_STATUSES
        ]
        groups.append({"tasks": open_tasks})
    return {"groups": groups}


def project_status_for_state(state: str | None) -> str | None:
    """The Linear project status name for a bundle lifecycle state, or None to leave the status alone."""
    return PROJECT_STATUS_BY_STATE.get(state or "")


def part_digest(payload) -> str:
    """sha256 of the canonical JSON of one metadata part; equal payloads give an equal digest."""
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
