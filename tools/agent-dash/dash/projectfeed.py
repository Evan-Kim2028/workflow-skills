"""What-changed feed: newest-first project events from recorded history.

Sources, all local and read-only:

- each bundle's ``history.jsonl``: task moves (``{ts, task, from, to}``)
  and workstream state moves (``{kind: state, ts, ws, from, to}``),
- each bundle's ``approvals.json``: decided items (``approved``/``denied``)
  carry the record's ``decided_at`` date; undated records are omitted,
- finished agent runs from the store, summarized per workstream per hour.

``build_feed`` is pure (plain data in, events out); ``collect_feed`` does
the filesystem and store reads. The endpoint serves
``{"feed": [...], "total": n, "limit": n, "earliest_ts": t|null}`` where
each event is ``{"ts", "day", "ws", "name", "kind", "text", "target"}``:
``day`` is the UTC ``YYYY-MM-DD`` the client groups on, ``ws``/``name``
name the workstream (name before id in ``text``), and ``target`` is the
workstream id to open on click (null when a line spans workstreams).

Never invents timestamps: history lines without a numeric ``ts`` are
omitted, and ``earliest_ts`` (the oldest consumed stamp, or null) lets
the page say ``no history before <date>`` instead of filling the gap.
No doctor or health content is ever emitted here.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
RUN_QUERY_LIMIT = 5000

#: Task moves that read as forward progress on the feed.
FORWARD_TO = ("done", "pr", "prog")

#: Finished run statuses that count as "agents finished".
FINISHED_STATUS = ("done", "killed", "died_early")

#: Sources that are never agents (interactive leads, launch rows, jobs).
NON_AGENT_SOURCES = frozenset({"", "claude-main", "codex", "shell", "dagster"})

VERDICT_WORDS = {
    "PASS": "pass",
    "PARTIAL": "partial",
    "BLOCKED": "blocked",
    "APPROVE": "approved",
    "APPROVE_WITH_NITS": "approved with nits",
    "APPROVE_WITH_CHANGES": "approved with changes",
    "REQUEST_CHANGES": "changes requested",
}


def parse_ts(value) -> float | None:
    """A numeric epoch stamp, or None when the line carries no time."""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts != ts or ts in (float("inf"), float("-inf")):
        return None
    return ts


def day_key(ts: float) -> str:
    """UTC ``YYYY-MM-DD`` for one stamp (the client groups on this)."""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")


def parse_iso(value) -> float | None:
    """An ISO-8601 UTC stamp to epoch seconds, or None when absent."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    try:
        return moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


#: Tool lane for a pi/generic run, by model family. Mirrors the store's
#: mapping: cursor-family models count as ``grok`` (the Cursor tool lane).
_FAMILY_TOOL = {
    "beam": "beam",
    "deepseek": "deepseek",
    "glm": "glm",
    "claude": "claude",
    "codex": "codex",
    "cursor": "grok",
    "muse": "muse",
}


def _model_family(run: dict) -> str:
    from dash.models import model_key

    hint = " ".join(str(run.get(name) or "") for name in ("tier", "source"))
    return model_key(run.get("model"), hint)[0]


def _tool_key(run: dict) -> str | None:
    """Mirror of the store's tool mapping: which agent family, if any."""
    source = run.get("source") or ""
    if source == "dagster":
        return None
    tier = (run.get("tier") or "").lower()
    if source == "grok" or tier == "grok":
        return "grok"
    if source == "muse" or tier == "muse":
        return "muse"
    if source == "glm" or tier == "glm":
        fam = _model_family(run)
        if fam == "deepseek":
            return "deepseek"
        if fam == "beam":
            return "beam"
        if fam == "other":
            return "other"
        return "glm"
    if source in ("codex", "codex-exec") or tier == "codex":
        return "codex"
    if source == "commandcode" or tier == "commandcode":
        return "beam" if _model_family(run) == "beam" else "commandcode"
    if source.startswith("claude") or tier in ("opus", "sonnet", "haiku"):
        return "claude"
    if source in ("pi", "generic"):
        return _FAMILY_TOOL.get(_model_family(run), "other")
    if _model_family(run) == "beam":
        return "beam"
    return None


def is_agent_run(run: dict) -> bool:
    """Finished-list agent rule: a tool-family run, never a lead or job."""
    if not isinstance(run, dict):
        return False
    if (run.get("source") or "") in NON_AGENT_SOURCES:
        return False
    return _tool_key(run) is not None


def _names(bundles: list[dict]) -> dict:
    names = {}
    for bundle in bundles or []:
        if not isinstance(bundle, dict):
            continue
        ws = bundle.get("id")
        if not isinstance(ws, str) or not ws:
            continue
        title = bundle.get("title") or bundle.get("short") or ws
        names[ws] = title if isinstance(title, str) and title else ws
    return names


def _label(names: dict, ws: str | None) -> str:
    if isinstance(ws, str) and ws and ws in names:
        return "%s (%s)" % (names[ws], ws)
    return ws or "unassigned"


def _task_events(names: dict, histories: dict) -> tuple[list[dict], list[dict]]:
    """Individual task lines plus bulk groups ``(minute, to) -> rows``.

    Returns ``(singles, groups)``: moves to done/pr/prog collect into
    per-minute buckets for collapsing; reopenings (done -> open) stay
    individual lines.
    """
    singles: list[dict] = []
    buckets: dict = {}
    for ws, rows in (histories or {}).items():
        if not isinstance(ws, str) or not ws:
            continue
        for row in rows or []:
            if not isinstance(row, dict) or row.get("kind") == "state":
                continue
            task = row.get("task")
            to = row.get("to")
            if not isinstance(task, str) or not task or not isinstance(to, str) or not to:
                continue
            ts = parse_ts(row.get("ts"))
            if ts is None:
                continue
            frm = row.get("from")
            if to in FORWARD_TO:
                buckets.setdefault((int(ts // 60), to), []).append((ts, ws, task, frm))
            elif frm == "done" and to in ("todo", "wip", "pr", "prog"):
                singles.append(
                    {
                        "ts": ts,
                        "day": day_key(ts),
                        "ws": ws,
                        "name": names.get(ws, ws),
                        "kind": "task",
                        "text": "%s: task %s reopened (was done)" % (_label(names, ws), task),
                        "target": ws,
                    }
                )
    return singles, buckets


def _collapse_task_buckets(names: dict, buckets: dict) -> list[dict]:
    """One line per move, or one summary line for a bulk minute bucket."""
    events: list[dict] = []
    for (minute, to), rows in buckets.items():
        if len(rows) == 1:
            ts, ws, task, _frm = rows[0]
            events.append(
                {
                    "ts": ts,
                    "day": day_key(ts),
                    "ws": ws,
                    "name": names.get(ws, ws),
                    "kind": "task",
                    "text": "%s: task %s moved to %s" % (_label(names, ws), task, to),
                    "target": ws,
                }
            )
            continue
        counts: dict = {}
        for _ts, ws, _task, _frm in rows:
            counts[ws] = counts.get(ws, 0) + 1
        total = len(rows)
        latest = max(ts for ts, _ws, _task, _frm in rows)
        parts = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        detail = ", ".join("%s (%d)" % (_label(names, ws), count) for ws, count in parts)
        events.append(
            {
                "ts": latest,
                "day": day_key(latest),
                "ws": None,
                "name": "",
                "kind": "tasks",
                "text": "%d tasks moved to %s across %s" % (total, to, detail),
                "target": None,
            }
        )
    return events


def _state_events(names: dict, histories: dict) -> list[dict]:
    """Workstream state moves, with archive/restore worded on their own."""
    events: list[dict] = []
    for ws, rows in (histories or {}).items():
        if not isinstance(ws, str) or not ws:
            continue
        for row in rows or []:
            if not isinstance(row, dict) or row.get("kind") != "state":
                continue
            ts = parse_ts(row.get("ts"))
            frm = row.get("from")
            to = row.get("to")
            if ts is None or not isinstance(to, str) or not to:
                continue
            who = _label(names, ws)
            if to == "archived":
                kind, text = "archive", "%s was archived" % who
            elif frm == "archived":
                kind, text = "restore", "%s was restored to %s" % (who, to)
            elif isinstance(frm, str) and frm:
                kind, text = "state", "%s moved from %s to %s" % (who, frm, to)
            else:
                kind, text = "state", "%s is now %s" % (who, to)
            events.append(
                {
                    "ts": ts,
                    "day": day_key(ts),
                    "ws": ws,
                    "name": names.get(ws, ws),
                    "kind": kind,
                    "text": text,
                    "target": ws,
                }
            )
    return events


def _approval_events(names: dict, approvals: list[dict]) -> list[dict]:
    """Decided approval items that carry a date; undated ones are omitted."""
    events: list[dict] = []
    for record in approvals or []:
        if not isinstance(record, dict):
            continue
        ws = record.get("ws")
        if not isinstance(ws, str) or not ws:
            continue
        ts = parse_iso(record.get("decided_at"))
        if ts is None:
            continue
        items = record.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            if status not in ("approved", "denied"):
                continue
            step = item.get("step") if isinstance(item.get("step"), str) and item.get("step") else item.get("id", "item")
            events.append(
                {
                    "ts": ts,
                    "day": day_key(ts),
                    "ws": ws,
                    "name": names.get(ws, ws),
                    "kind": "approval",
                    "text": "%s: %s %s" % (_label(names, ws), step, status),
                    "target": ws,
                }
            )
    return events


def _run_events(names: dict, runs: list[dict]) -> tuple[list[dict], dict]:
    """Per-workstream-per-hour finish summaries plus first-run stamps.

    Returns ``(events, first_starts)``; runs without a workstream or
    without a recorded end never become lines.
    """
    groups: dict = {}
    first_starts: dict = {}
    for run in runs or []:
        if not isinstance(run, dict) or not is_agent_run(run):
            continue
        ws = run.get("workstream")
        if not isinstance(ws, str) or not ws:
            continue
        start = parse_ts(run.get("started_at"))
        if start is not None:
            if ws not in first_starts or start < first_starts[ws]:
                first_starts[ws] = start
        if run.get("status") not in FINISHED_STATUS:
            continue
        end = parse_ts(run.get("ended_at"))
        if end is None:
            continue
        groups.setdefault((ws, int(end // 3600)), []).append((end, run.get("verdict")))
    events: list[dict] = []
    for (ws, _hour), rows in groups.items():
        latest = max(end for end, _verdict in rows)
        words: dict = {}
        for _end, verdict in rows:
            word = VERDICT_WORDS.get(verdict)
            if word is None:
                continue
            words[word] = words.get(word, 0) + 1
        text = "%d %s finished in %s" % (
            len(rows),
            "agent" if len(rows) == 1 else "agents",
            _label(names, ws),
        )
        if words:
            text += "; " + ", ".join(
                "%d %s" % (count, word) for word, count in sorted(words.items(), key=lambda item: (-item[1], item[0]))
            )
        events.append(
            {
                "ts": latest,
                "day": day_key(latest),
                "ws": ws,
                "name": names.get(ws, ws),
                "kind": "run",
                "text": text,
                "target": ws,
            }
        )
    return events, first_starts


def _first_run_events(names: dict, first_starts: dict) -> list[dict]:
    events: list[dict] = []
    for ws, start in first_starts.items():
        events.append(
            {
                "ts": start,
                "day": day_key(start),
                "ws": ws,
                "name": names.get(ws, ws),
                "kind": "first-run",
                "text": "first agent run in %s" % _label(names, ws),
                "target": ws,
            }
        )
    return events


def build_feed(
    bundles: list[dict] | None,
    histories: dict | None,
    approvals: list[dict] | None,
    runs: list[dict] | None,
    now: float,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """Newest-first feed events from plain data. Pure: no I/O, no clock."""
    try:
        count = int(limit)
    except (TypeError, ValueError):
        count = DEFAULT_LIMIT
    count = max(1, min(count, MAX_LIMIT))
    names = _names(bundles)
    singles, buckets = _task_events(names, histories)
    events = list(singles)
    events.extend(_collapse_task_buckets(names, buckets))
    events.extend(_state_events(names, histories))
    events.extend(_approval_events(names, approvals))
    run_events, first_starts = _run_events(names, runs)
    events.extend(run_events)
    events.extend(_first_run_events(names, first_starts))
    events.sort(key=lambda event: event["ts"], reverse=True)
    earliest: float | None = None
    for event in events:
        if earliest is None or event["ts"] < earliest:
            earliest = event["ts"]
    try:
        now_f = float(now)
    except (TypeError, ValueError):
        now_f = 0.0
    return {
        "feed": events[:count],
        "total": len(events),
        "limit": count,
        "earliest_ts": earliest,
        "now": now_f,
    }


def read_history_file(bundle_dir: Path) -> list[dict]:
    """Raw history rows for one bundle dir; unreadable means no history."""
    try:
        text = (bundle_dir / "history.jsonl").read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def collect_feed(root=None, store=None, now: float = 0.0, limit: int = DEFAULT_LIMIT) -> dict:
    """Read bundles, histories, approvals, and runs, then build the feed."""
    from dash.bundles import approvals_all, bundle_dir, list_bundles, workstreams_root

    root = Path(root) if root is not None else workstreams_root()
    bundles = list_bundles(root)
    histories: dict = {}
    for bundle in bundles:
        path = bundle_dir(bundle["id"], root)
        if path is None:
            continue
        rows = read_history_file(path)
        if rows:
            histories[bundle["id"]] = rows
    try:
        approvals = approvals_all(root)
    except Exception:
        approvals = []
    runs: list = []
    if store is not None:
        try:
            runs = store.query_runs(now, None, None, None, RUN_QUERY_LIMIT) or []
        except Exception:
            runs = []
    return build_feed(bundles, histories, approvals, runs, now, limit)
