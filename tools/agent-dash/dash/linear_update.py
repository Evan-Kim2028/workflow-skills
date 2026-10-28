"""Plain-language standup update for one linked workstream, posted to its Linear project.

``build_update`` renders the markdown body and health from the bundle files
(plan, history, link). ``post`` sends it through the linked client
and records ``last_update_at`` in ``linear.json`` on success. ``due`` is the
daily rule the poller checks after a successful sync; ``send_daily`` is the
poller's entry point and reports failures as text instead of raising.

Every post runs under the bundle's sync lock. The daily path first lists today's
project updates: one from today whose first line is this body's first line is
taken as already posted, so an ambiguous failure never posts the day twice. A
manual post always posts.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from dash import bundles, errors, linear, orchestrate
from dash.linear_client import created_since

DEFAULT_HEALTH = "onTrack"
HEALTH_BY_KEY = {"ontrack": "onTrack", "atrisk": "atRisk", "offtrack": "offTrack"}
CHANGED_LIMIT = 10
IN_PROGRESS_LIMIT = 10
NEXT_LIMIT = 5
LOOKBACK_S = 24 * 3600
RETRY_S = 3600
HISTORY_FILE = "history.jsonl"
MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]()@])")


def _health(value: object) -> str | None:
    """The Linear health enum for a plan value or CLI flag (on-track, onTrack, ...), else None."""
    if not isinstance(value, str):
        return None
    return HEALTH_BY_KEY.get(re.sub(r"[-_\s]", "", value).lower())


def _finite(value: object) -> float | None:
    """The value as a finite float; None for a bool, a non-number, NaN, infinity or an int too large for a float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _stamp_of(value: object) -> float | None:
    """A stored epoch timestamp as a finite float, or None when absent or not a finite number."""
    return _finite(value)


def _reject_constant(name: str) -> object:
    """json.loads hook: NaN, Infinity and -Infinity are not stamps, so the line is malformed."""
    raise ValueError(f"{name} is not a finite number")


def _md(value: object) -> str:
    """A value safe inside the markdown body: one line, no mentions, no leading block or list marker."""
    text = MARKDOWN_SPECIAL.sub(r"\\\1", " ".join(str(value).split()))
    return "\\" + text if text[:1] in ("#", "-", ">") else text


def _read_plan(ws: str, root: Path | None) -> dict | None:
    path = bundles.bundle_dir(ws, root)
    if path is None:
        return None
    data = bundles._read_json(path / "plan.json")
    return data if isinstance(data, dict) else None


def _bundle_path(ws: str, root: Path | None) -> Path:
    """The bundle directory; raises ValueError when the bundle is missing."""
    path = bundles.bundle_dir(ws, root)
    if path is None:
        raise ValueError(f"plan.json for {ws} is unreadable or the bundle is missing")
    return path


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
    name = _md(title.strip() if isinstance(title, str) and title.strip() else task_id)
    entry = links.get(task_id, {})
    identifier = entry.get("identifier")
    url = entry.get("url")
    if identifier and url:
        return f"{name} ([{_md(identifier)}]({url}))"
    if identifier:
        return f"{name} ({_md(identifier)})"
    return name


def _more(lines: list[str], shown: int, total: int) -> list[str]:
    if total > shown:
        lines.append(f"- and {total - shown} more")
    return lines


def _history_rows(ws: str, root: Path | None) -> list[dict]:
    """Every history row with a finite ``ts``. A malformed or non-finite line is skipped, never an error."""
    path = bundles.bundle_dir(ws, root)
    if path is None or not (path / HISTORY_FILE).is_file():
        return []
    try:
        text = (path / HISTORY_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line, parse_constant=_reject_constant)
        except (ValueError, TypeError, RecursionError):
            continue
        stamp = _stamp_of(row.get("ts")) if isinstance(row, dict) else None
        if stamp is None:
            continue
        row["ts"] = stamp
        rows.append(row)
    return rows


def _changed(rows: list[dict], since: float, titles: dict, links: dict) -> list[str]:
    recent = sorted((row for row in rows if float(row["ts"]) > since), key=lambda row: float(row["ts"]), reverse=True)
    lines = []
    for row in recent[:CHANGED_LIMIT]:
        task_id = str(row.get("task", ""))
        source = " (in Linear)" if row.get("by") == "linear" else ""
        before = _md(row.get("from") or "new")
        after = _md(row.get("to"))
        name = "Workstream" if row.get("kind") == "state" or not task_id else _label(task_id, titles.get(task_id), links)
        lines.append(f"- {name}: {before} -> {after}{source}")
    if not lines:
        lines.append("- Nothing changed.")
    return _more(lines, min(len(recent), CHANGED_LIMIT), len(recent))


def _section(title: str, lines: list[str]) -> str:
    return "\n".join([f"**{title}**", *lines])


def build_update(ws: str, root: Path | None = None, now: float | None = None) -> dict:
    """The update for one workstream: {"body": markdown, "health": Linear enum}.

    Raises ValueError when the bundle does not exist. Health is the plan's
    ``tracker.update.health`` when valid, else onTrack. Plan text is escaped
    for markdown on the way in.
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
    parts = [
        _md(_purpose(ws, plan, root)),
        f"**Progress:** {done} of {total} tasks done ({percent}%). In progress: {len(wip)}. "
        f"In review: {review}. Waiting on a production step: {prod}.",
        _section("Changed since the last update", _changed(_history_rows(ws, root), since, titles, links)),
        _section("In progress now", in_progress),
        _section("Next up", nxt),
    ]
    plan_health = _health(_tracker_update(plan).get("health"))
    return {"body": "\n\n".join(parts) + "\n", "health": plan_health or DEFAULT_HEALTH}


def _owed(link: dict, now: float) -> bool:
    """True when no update went out on this local day and the last attempt is at least RETRY_S old."""
    last = _stamp_of(link.get("last_update_at"))
    if last is not None and datetime.fromtimestamp(last).date() == datetime.fromtimestamp(now).date():
        return False
    attempt = _stamp_of(link.get("update_attempt_at"))
    return attempt is None or now - attempt >= RETRY_S


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
    if datetime.fromtimestamp(now).hour < hour:
        return False
    return _owed(link, now)


def _write_fields(ws: str, root: Path | None, fields: dict) -> None:
    """Merge fields into the link. The caller holds the bundle's sync lock."""
    record = linear.read_link(ws, root)
    if record is None:
        return
    record.update(fields)
    linear.write_link(ws, record, root)


def _note(ws: str, root: Path | None, fields: dict) -> None:
    """Best-effort write of error text: a failed write must not hide the error it reports."""
    try:
        _write_fields(ws, root, fields)
    except Exception:
        pass


def _day_start(now: float) -> datetime:
    """Local midnight of the day ``now`` falls on, as an aware UTC datetime."""
    local = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(UTC)


def _posted_today(client, project_id: str, body: str, now: float) -> str | None:
    """The id of today's project update whose first line equals this body's first line, or None."""
    lead = body.split("\n", 1)[0].strip()
    since = _day_start(now).isoformat()
    for row in client.recent_updates(project_id, since):
        text = row.get("body")
        if not isinstance(text, str) or text.strip().split("\n", 1)[0].strip() != lead:
            continue
        if created_since(row, since):
            return str(row.get("id") or "")
    return None


def _send(ws: str, root: Path | None, client, body: str, health: str, stamp: float, dedupe: bool) -> tuple[str | None, str | None]:
    """Post the body and return (update id or None, error text or None).

    ``dedupe`` is the daily path: it lists today's updates first and takes a
    matching one as posted. A manual post never lists and always posts. The
    attempt is stamped before the list and the post, so a failed write or a
    lost response never leads to a second daily post a minute later; the
    next daily try after RETRY_S lists first. The caller holds the sync lock.
    """
    from dash import linear_sync

    link = linear.read_link(ws, root)
    project = link.get("project") if link is not None and isinstance(link.get("project"), dict) else {}
    project_id = project.get("id")
    if not isinstance(project_id, str) or not project_id:
        message = f"not linked: run python3 agent_dash.py ws linear link {ws} --team KEY"
        _note(ws, root, {"update_attempt_at": stamp, "last_error": message})
        return None, message
    try:
        _write_fields(ws, root, {"update_attempt_at": stamp})
        active = client if client is not None else linear_sync._make_client()
        existing = _posted_today(active, project_id, body, stamp) if dedupe else None
        if existing is not None:
            update_id = existing
        else:
            update_id = active.post_update(project_id, body, health) or ""
        _write_fields(ws, root, {"last_update_at": stamp})
    except Exception as exc:
        message = linear_sync._error_text(exc)
        _note(ws, root, {"last_error": message})
        return None, message
    return update_id, None


def send_daily(ws: str, now: float, root: Path | None = None, client=None) -> str | None:
    """Post the plan's update for the poller. Returns the error text or None; never raises.

    Under the sync lock the daily rule is re-checked, so a manual post or a
    second poller cannot post the same day.
    """
    try:
        from dash.linear_sync import _sync_lock

        path = _bundle_path(ws, root)
        with _sync_lock(path):
            link = linear.read_link(ws, root)
            if link is not None and not _owed(link, now):
                return None
            update = build_update(ws, root=root, now=now)
            _id, error = _send(ws, root, client, update["body"], update["health"], now, dedupe=True)
            return error
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def post(ws: str, health: str | None = None, dry_run: bool = False, root: Path | None = None, client=None, now: float | None = None) -> int:
    """Post the update for one workstream and return the CLI exit code.

    ``health`` (on-track, at-risk, off-track or the Linear enum) overrides the
    plan's value. A dry run prints the body and posts nothing. A manual post
    always posts, even when today already has an update; only the daily path
    de-duplicates.
    """
    from dash.linear_sync import _sync_lock

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
    with _sync_lock(_bundle_path(ws, root)):
        _id, error = _send(ws, root, client, update["body"], chosen or update["health"], stamp, dedupe=False)
    if error is not None:
        print(f"update not posted for {ws}: {error}", flush=True)
        return errors.UNREACHABLE
    print(f"posted Linear project update for {ws}", flush=True)
    return errors.OK
