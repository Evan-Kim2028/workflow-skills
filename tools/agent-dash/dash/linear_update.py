"""Plain-language standup update for one linked workstream, posted to its Linear project.

``build_update`` renders the markdown body and health from the bundle files
(plan, history, approvals, link). ``post`` sends it through the linked client
and records ``last_update_at`` in ``linear.json`` on success. ``due`` is the
daily rule the poller checks after a successful sync; ``send_daily`` is the
poller's entry point and reports failures as text instead of raising.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

from dash import bundles, errors, linear, orchestrate

DEFAULT_HEALTH = "onTrack"
HEALTH_BY_KEY = {"ontrack": "onTrack", "atrisk": "atRisk", "offtrack": "offTrack"}
CHANGED_LIMIT = 10
IN_PROGRESS_LIMIT = 10
NEXT_LIMIT = 5
LOOKBACK_S = 24 * 3600
RETRY_S = 3600
HISTORY_FILE = "history.jsonl"


def _health(value: object) -> str | None:
    """The Linear health enum for a plan value or CLI flag (on-track, onTrack, ...), else None."""
    if not isinstance(value, str):
        return None
    return HEALTH_BY_KEY.get(re.sub(r"[-_\s]", "", value).lower())


def _stamp_of(value: object) -> float | None:
    """A stored epoch timestamp as a float, or None when absent or not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _read_plan(ws: str, root: Path | None) -> dict | None:
    path = bundles.bundle_dir(ws, root)
    if path is None:
        return None
    data = bundles._read_json(path / "plan.json")
    return data if isinstance(data, dict) else None


def _tracker_update(plan: dict) -> dict:
    tracker = plan.get("tracker") if isinstance(plan.get("tracker"), dict) else {}
    update = tracker.get("update")
    return update if isinstance(update, dict) else {}


def _daily_hour(plan: dict) -> int | None:
    hour = _tracker_update(plan).get("daily_hour")
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        return None
    return hour


def _purpose(ws: str, plan: dict, root: Path | None) -> str:
    tracker = plan.get("tracker") if isinstance(plan.get("tracker"), dict) else {}
    summary = tracker.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    narrative = plan.get("narrative") if isinstance(plan.get("narrative"), dict) else {}
    thesis = narrative.get("thesis")
    if isinstance(thesis, str) and thesis.strip():
        return thesis.strip()
    path = bundles.bundle_dir(ws, root)
    meta = bundles._read_json(path / "bundle.json") if path is not None else None
    title = meta.get("title") if isinstance(meta, dict) else None
    return title.strip() if isinstance(title, str) and title.strip() else ws


def _label(task_id: str, title: object, links: dict) -> str:
    """A task title with its Linear identifier as a link when one is known."""
    name = title.strip() if isinstance(title, str) and title.strip() else task_id
    entry = links.get(task_id, {})
    identifier = entry.get("identifier")
    url = entry.get("url")
    if identifier and url:
        return f"{name} ([{identifier}]({url}))"
    if identifier:
        return f"{name} ({identifier})"
    return name


def _more(lines: list[str], shown: int, total: int) -> list[str]:
    if total > shown:
        lines.append(f"- and {total - shown} more")
    return lines


def _history_rows(ws: str, root: Path | None) -> list[dict]:
    path = bundles.bundle_dir(ws, root)
    if path is None or not (path / HISTORY_FILE).is_file():
        return []
    rows = []
    for line in (path / HISTORY_FILE).read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and _stamp_of(row.get("ts")) is not None:
            rows.append(row)
    return rows


def _changed(rows: list[dict], since: float, titles: dict, links: dict) -> list[str]:
    recent = sorted((row for row in rows if float(row["ts"]) > since), key=lambda row: float(row["ts"]), reverse=True)
    lines = []
    for row in recent[:CHANGED_LIMIT]:
        task_id = str(row.get("task", ""))
        source = " (in Linear)" if row.get("by") == "linear" else ""
        before = row.get("from") or "new"
        name = "Workstream" if row.get("kind") == "state" or not task_id else _label(task_id, titles.get(task_id), links)
        lines.append(f"- {name}: {before} -> {row.get('to')}{source}")
    if not lines:
        lines.append("- Nothing changed.")
    return _more(lines, min(len(recent), CHANGED_LIMIT), len(recent))


def _section(title: str, lines: list[str]) -> str:
    return "\n".join([f"**{title}**", *lines])


def build_update(ws: str, root: Path | None = None, now: float | None = None) -> dict:
    """The update for one workstream: {"body": markdown, "health": Linear enum}.

    Raises ValueError when the bundle does not exist. Health is the plan's
    ``tracker.update.health`` when valid, else onTrack.
    """
    stamp = time.time() if now is None else now
    plan = _read_plan(ws, root)
    if plan is None:
        raise ValueError(f"plan.json for {ws} is unreadable or the bundle is missing")
    link = linear.read_link(ws, root) or {}
    links = linear.task_links(ws, root)
    by_id, _deps, order = orchestrate.plan_tasks(plan)
    titles = {tid: by_id[tid].get("title") for tid in by_id}
    tasks = [by_id[tid] for tid in order]
    counted = [task for task in tasks if task.get("status") != "deferred"]
    done = sum(1 for task in counted if task.get("status") == "done")
    total = len(counted)
    percent = round(100 * done / total) if total else 0
    wip = [task for task in counted if task.get("status") == "wip"]
    review = sum(1 for task in counted if task.get("status") == "pr")
    prod = sum(1 for task in counted if task.get("status") == "prog")
    since = _stamp_of(link.get("last_update_at"))
    since = stamp - LOOKBACK_S if since is None else since
    in_progress = [f"- {_label(task['id'], task.get('title'), links)}" for task in wip[:IN_PROGRESS_LIMIT]] or ["- None."]
    in_progress = _more(in_progress, min(len(wip), IN_PROGRESS_LIMIT), len(wip))
    nxt = [f"- {_label(row['id'], row.get('title'), links)}" for row in orchestrate.ready_queue(plan)[:NEXT_LIMIT]] or ["- Nothing is ready."]
    approvals = bundles.approvals_for(ws, root) or {}
    pending = [item for item in approvals.get("items", []) if isinstance(item, dict) and item.get("status") == "pending"]
    decisions = [f"- {item.get('step') or item.get('id')}" for item in pending] or ["- Nothing waits on a decision."]
    parts = [
        _purpose(ws, plan, root),
        f"**Progress:** {done} of {total} tasks done ({percent}%). In progress: {len(wip)}. "
        f"In review: {review}. Waiting on a production step: {prod}.",
        _section("Changed since the last update", _changed(_history_rows(ws, root), since, titles, links)),
        _section("In progress now", in_progress),
        _section("Next up", nxt),
        _section("Needs a decision", decisions),
    ]
    plan_health = _health(_tracker_update(plan).get("health"))
    return {"body": "\n\n".join(parts) + "\n", "health": plan_health or DEFAULT_HEALTH}


def due(ws: str, now: float, root: Path | None = None) -> bool:
    """True when the daily update is owed: the local hour is at or past ``tracker.update.daily_hour``,
    no update went out on this local day, and the last failed attempt is at least an hour old.

    False when ``daily_hour`` is unset or the workstream is not linked.
    """
    plan = _read_plan(ws, root)
    hour = _daily_hour(plan) if plan is not None else None
    link = linear.read_link(ws, root)
    if hour is None or link is None:
        return False
    local = datetime.fromtimestamp(now)
    if local.hour < hour:
        return False
    last = _stamp_of(link.get("last_update_at"))
    if last is not None and datetime.fromtimestamp(last).date() == local.date():
        return False
    attempt = _stamp_of(link.get("update_attempt_at"))
    return attempt is None or now - attempt >= RETRY_S


def _record(ws: str, root: Path | None, fields: dict) -> None:
    from dash.linear_sync import _sync_lock

    path = bundles.bundle_dir(ws, root)
    if path is None:
        return
    with _sync_lock(path):
        record = linear.read_link(ws, root)
        if record is None:
            return
        record.update(fields)
        linear.write_link(ws, record, root)


def _send(ws: str, root: Path | None, client, body: str, health: str, stamp: float) -> tuple[str | None, str | None]:
    """Post the body. Returns (update id or None, error text or None) and records the outcome on the link."""
    from dash import linear_sync

    link = linear.read_link(ws, root)
    project = link.get("project") if link is not None and isinstance(link.get("project"), dict) else {}
    project_id = project.get("id")
    if not isinstance(project_id, str) or not project_id:
        return None, f"not linked: run python3 agent_dash.py ws linear link {ws} --team KEY"
    try:
        active = client if client is not None else linear_sync._make_client()
        update_id = active.post_update(project_id, body, health)
    except Exception as exc:
        message = linear_sync._error_text(exc)
        _record(ws, root, {"update_attempt_at": stamp, "last_error": message})
        return None, message
    _record(ws, root, {"last_update_at": stamp, "update_attempt_at": stamp})
    return update_id or "", None


def send_daily(ws: str, now: float, root: Path | None = None, client=None) -> str | None:
    """Post the plan's update for the poller. Returns the error text or None; never raises."""
    try:
        update = build_update(ws, root=root, now=now)
        _id, error = _send(ws, root, client, update["body"], update["health"], now)
        return error
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def post(ws: str, health: str | None = None, dry_run: bool = False, root: Path | None = None, client=None, now: float | None = None) -> int:
    """Post the update for one workstream and return the CLI exit code.

    ``health`` (on-track, at-risk, off-track or the Linear enum) overrides the
    plan's value. A dry run prints the body and posts nothing.
    """
    stamp = time.time() if now is None else now
    chosen = None
    if health is not None:
        chosen = _health(health)
        if chosen is None:
            print(f"unknown health {health!r}. Use one of: on-track, at-risk, off-track. Run: python3 agent_dash.py ws linear update {ws} --health on-track", flush=True)
            return errors.USAGE
    try:
        update = build_update(ws, root=root, now=stamp)
    except ValueError as exc:
        print(str(exc), flush=True)
        return errors.NOT_FOUND
    if dry_run:
        print(update["body"], end="", flush=True)
        return errors.OK
    _id, error = _send(ws, root, client, update["body"], chosen or update["health"], stamp)
    if error is not None:
        print(f"update not posted for {ws}: {error}", flush=True)
        return errors.UNREACHABLE
    print(f"posted Linear project update for {ws}", flush=True)
    return errors.OK
