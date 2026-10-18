"""Workstream lifecycle: states, archive with a frozen summary, restore, list.

A bundle's lifecycle lives in ``bundle.json``, additive with defaults so
every bundle written before this module reads as ``active``::

    {"state": "active", "state_changed_at": 0, "archived_at": None,
     "summary": None, "depends_on": ["WS1"], "milestone": "Part 2"}

States (``STATES``) and the legal moves between them (``LEGAL_MOVES``)
are the single source of truth: ``ws state`` (``set_state``), ``ws
archive`` (``archive_bundle``), ``ws restore`` (``restore_bundle``), and
``ws list`` (``list_workstreams``) all read them. Archiving is manual:
a human or the lead runs ``ws archive`` after the closing report; the
doctor nags about done-not-archived bundles, it never archives for you.

Pure helpers for other consumers (doctor, server) with no I/O:

- ``legal_moves``: the legal next states from one state.
- ``closing_report_path``: where the required closing report lives.
- ``freeze_summary``: the frozen archive card from plan/approval/usage facts.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

from dash import errors

#: Every lifecycle state a bundle.json ``state`` field may hold.
STATES = ("planned", "active", "paused", "done", "archived", "dropped")

#: Bundles without a ``state`` field read as this, so every existing
#: bundle keeps working unchanged.
DEFAULT_STATE = "active"

#: Legal next states per current state. ``archived`` is reachable from
#: ``done`` only (via ``ws archive``, or ``ws state <ID> archived``
#: which moves state without freezing a summary); only ``ws restore``
#: leaves ``archived``. Reopening ``done`` to ``active`` needs
#: ``--force --note``. ``dropped`` is terminal.
LEGAL_MOVES: dict[str, tuple[str, ...]] = {
    "planned": ("active",),
    "active": ("paused", "done", "dropped"),
    "paused": ("active", "dropped"),
    "done": ("archived", "active"),
    "archived": (),
    "dropped": (),
}

#: States ``ws list`` shows when ``--state`` is absent: everything still
#: on the daily views. Archived and dropped bundles stay referenceable
#: via ``--state archived``, ``--state dropped``, or ``--state all``.
DEFAULT_LIST_STATES = ("active", "paused", "planned", "done")

#: Closing-report filename inside a bundle directory.
CLOSING_REPORT = Path("docs") / "closing-report.md"

#: Dashboard base URL when ``AGENT_DASH_URL`` names none (mirrors
#: ``dash.bundles._dash_url`` without importing it).
_DEFAULT_URL = "http://127.0.0.1:8765"


def legal_moves(state: str) -> list[str]:
    """Legal next states from ``state`` (empty for terminal states).

    Unknown input reads as no legal moves, so callers name the valid
    states instead of crashing on a bad stored value.
    """
    return list(LEGAL_MOVES.get(state, ()))


def closing_report_path(bundle_path: Path) -> Path:
    """Path of the required closing report inside a bundle directory."""
    return Path(bundle_path) / CLOSING_REPORT


def current_state(meta: dict | None) -> str:
    """Lifecycle state of a bundle.json body, defaulting when absent.

    Unknown stored values fall back to ``DEFAULT_STATE``: old and
    hand-written bundles keep working, and ``ws validate`` (not the
    readers) is what complains about them.
    """
    if isinstance(meta, dict) and meta.get("state") in STATES:
        return meta["state"]
    return DEFAULT_STATE


def plan_complete(progress: dict | None) -> bool:
    """True when every non-deferred task is done (and at least one exists)."""
    if not isinstance(progress, dict):
        return False
    total = progress.get("total")
    done = progress.get("done")
    return (
        isinstance(total, int)
        and not isinstance(total, bool)
        and isinstance(done, int)
        and not isinstance(done, bool)
        and total > 0
        and done == total
    )


def freeze_summary(
    ws: str,
    *,
    started_at: float | None,
    finished_at: float,
    progress: dict | None,
    trains: list | None,
    approvals: dict | None,
    usage: dict | None,
    closing_report: bool,
    note: str | None = None,
) -> dict:
    """Frozen archive card for a workstream from already-read facts.

    Pure: no disk or network reads, so the doctor and the server can
    reuse it. ``progress`` is the ``/api/plan`` progress object,
    ``trains`` the plan's raw trains list, ``approvals`` the
    ``approvals_for`` record, and ``usage`` the
    ``fetch_workstream_usage`` payload (nulls with a note when the
    dashboard was unreachable). ``duration_s`` is None when no start
    moment was ever recorded.
    """
    counts = {}
    done = 0
    total = 0
    pct = 0
    if isinstance(progress, dict):
        raw_counts = progress.get("counts")
        if isinstance(raw_counts, dict):
            counts = {key: value for key, value in raw_counts.items() if isinstance(value, int) and not isinstance(value, bool)}
        done = progress.get("done") if isinstance(progress.get("done"), int) else 0
        total = progress.get("total") if isinstance(progress.get("total"), int) else 0
        pct = progress.get("pct") if isinstance(progress.get("pct"), int) else 0
    by_status: dict[str, int] = {}
    train_total = 0
    for item in trains or []:
        if not isinstance(item, dict):
            continue
        train_total += 1
        status = item.get("status")
        if isinstance(status, str) and status:
            by_status[status] = by_status.get(status, 0) + 1
    approval_counts = {"pending": 0, "approved": 0, "denied": 0, "total": 0}
    for item in (approvals.get("items") if isinstance(approvals, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        approval_counts["total"] += 1
        if status in approval_counts:
            approval_counts[status] += 1
    duration = None
    if isinstance(started_at, (int, float)) and not isinstance(started_at, bool):
        duration = finished_at - float(started_at)
    return {
        "ws": ws,
        "state": "archived",
        "frozen_at": finished_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_s": duration,
        "tasks": {"done": done, "total": total, "counts": counts, "pct": pct},
        "trains": {"total": train_total, "by_status": by_status},
        "approvals": approval_counts,
        "usage": usage,
        "closing_report": closing_report,
        "note": note,
    }


def fetch_workstream_usage(base_url: str | None, ws: str) -> dict:
    """Lifetime token/cost totals for ``ws`` from the dashboard API.

    Never raises and never fails the caller: when the dashboard is
    unreachable (or has no row for the workstream) the totals are null
    with a note naming the URL, and ``reachable`` says which happened.
    """
    base = base_url or os.environ.get("AGENT_DASH_URL") or _DEFAULT_URL
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/api/workstreams", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload.get("workstreams") if isinstance(payload, dict) else None
        row = next(
            (item for item in rows or [] if isinstance(item, dict) and item.get("ws") == ws),
            None,
        )
        if row is None:
            return {
                "tokens_all_time": None,
                "cost_usd_all_time": None,
                "reachable": True,
                "url": base,
                "note": f"dashboard at {base} has no workstreams row for {ws}; totals recorded as null",
            }
        tokens = row.get("tokens_all_time")
        cost = row.get("cost_usd_all_time")
        return {
            "tokens_all_time": tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None,
            "cost_usd_all_time": cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
            "reachable": True,
            "url": base,
            "note": None,
        }
    except Exception:
        return {
            "tokens_all_time": None,
            "cost_usd_all_time": None,
            "reachable": False,
            "url": base,
            "note": f"dashboard unreachable at {base}; totals recorded as null",
        }


def _history_bounds(path: Path) -> tuple[float | None, float | None]:
    """(first, last) numeric ``ts`` across every history.jsonl line."""
    try:
        text = (path / "history.jsonl").read_text(encoding="utf-8")
    except OSError:
        return None, None
    stamps = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        try:
            stamps.append(float(row["ts"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not stamps:
        return None, None
    return min(stamps), max(stamps)


def append_state_history(
    path: Path,
    ws: str,
    frm: str,
    to: str,
    note: str | None,
    now: float,
) -> None:
    """Append one ``{kind: state}`` line; replay skips lines with no task.

    ``dash.replay.read_history`` only keeps rows naming a ``task``, so
    lifecycle lines flow through the same file without breaking the
    timeline (pinned by ``tests/test_lifecycle.py``).
    """
    row: dict = {"kind": "state", "ts": now, "ws": ws, "from": frm, "to": to}
    if note is not None:
        row["note"] = note
    try:
        with open(path / "history.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
    except OSError as exc:
        print(f"history not recorded for {ws}: {exc}", flush=True)


def set_state(
    ws: str,
    target: str,
    *,
    force: bool = False,
    note: str | None = None,
    root: Path | None = None,
    now: float | None = None,
) -> int:
    """Move a bundle to ``target`` state under the per-bundle lock.

    Illegal moves exit 2 naming the legal moves; reopening ``done`` to
    ``active`` needs ``--force --note``. Sets ``state_changed_at`` (and
    ``archived_at`` on entering or leaving ``archived``) and appends one
    ``{kind: state}`` history line. A no-op move (already there) exits 0
    without writing.
    """
    from dash.bundles import _atomic_write, _bundle_lock, _read_json, bundle_dir

    if target not in STATES:
        print(
            f"unknown state {target!r}. Use one of: {', '.join(STATES)}. "
            f"Run: python3 agent_dash.py ws list --state all",
            flush=True,
        )
        return errors.USAGE
    path = bundle_dir(ws, root)
    if path is None:
        print(errors.bundle_not_found(ws), flush=True)
        return errors.NOT_FOUND
    moment = time.time() if now is None else now
    with _bundle_lock(path):
        meta = _read_json(path / "bundle.json")
        if not isinstance(meta, dict):
            print(f"bundle.json for {ws} is unreadable. Run: python3 agent_dash.py ws validate {ws}", flush=True)
            return errors.INVALID_DATA
        frm = current_state(meta)
        if frm == target:
            print(f"{ws} already {target}")
            return errors.OK
        if target not in legal_moves(frm):
            moves = ", ".join(legal_moves(frm)) or "none (terminal state)"
            print(
                f"illegal move {ws} {frm} -> {target}. Legal moves from {frm}: {moves}. "
                f"Run: python3 agent_dash.py ws state {ws} <one of: {moves}>",
                flush=True,
            )
            return errors.USAGE
        if frm == "done" and target == "active" and not (force and note and note.strip()):
            print(
                f"refusing to reopen {ws} done -> active without --force --note. "
                f"Run: python3 agent_dash.py ws state {ws} active --force --note \"why it reopened\"",
                flush=True,
            )
            return errors.USAGE
        meta["state"] = target
        meta["state_changed_at"] = moment
        if target == "archived":
            meta["archived_at"] = moment
        elif frm == "archived":
            meta["archived_at"] = None
        _atomic_write(path / "bundle.json", json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
        append_state_history(path, ws, frm, target, note.strip() if isinstance(note, str) and note.strip() else None, moment)
    print(f"{ws} {frm} -> {target}")
    return errors.OK


def archive_bundle(
    ws: str,
    *,
    force: bool = False,
    note: str | None = None,
    root: Path | None = None,
    now: float | None = None,
    base_url: str | None = None,
) -> int:
    """Archive a workstream: freeze ``summary.json``, move state to archived.

    Refuses (exit 2) unless the plan is 100% done of non-deferred tasks
    or ``--force --note TEXT`` is given, and unless the bundle holds
    ``docs/closing-report.md`` or ``--force`` is given. Fetches lifetime
    run/token/cost totals from the dashboard API when reachable, else
    records nulls with a note. Never deletes anything: the bundle, its
    plan, and its history stay, and ``ws restore`` reopens it.
    """
    from dash.bundles import _atomic_write, _bundle_lock, _read_json, approvals_for, bundle_dir

    path = bundle_dir(ws, root)
    if path is None:
        print(errors.bundle_not_found(ws), flush=True)
        return errors.NOT_FOUND
    moment = time.time() if now is None else now
    meta = _read_json(path / "bundle.json")
    if not isinstance(meta, dict):
        print(f"bundle.json for {ws} is unreadable. Run: python3 agent_dash.py ws validate {ws}", flush=True)
        return errors.INVALID_DATA
    frm = current_state(meta)
    if frm == "archived":
        print(f"{ws} is already archived. Run: python3 agent_dash.py ws restore {ws} to reopen it", flush=True)
        return errors.USAGE
    if frm == "dropped":
        print(f"{ws} is dropped and cannot be archived. Run: python3 agent_dash.py ws list --state all", flush=True)
        return errors.USAGE
    from dash.bundles import resolve_plan
    from dash.plans import plan_progress

    body = _read_json(path / "plan.json")
    if not isinstance(body, dict):
        try:
            _source, body = resolve_plan(ws)
        except Exception:
            body = None
    if not isinstance(body, dict):
        print(errors.plan_unreadable(ws), flush=True)
        return errors.INVALID_DATA
    progress = body.get("progress") if isinstance(body.get("progress"), dict) else None
    if progress is None:
        dag = body.get("dag") if isinstance(body.get("dag"), dict) else {}
        groups = dag.get("groups") if isinstance(dag.get("groups"), list) else []
        if not groups and isinstance(body.get("groups"), list):
            groups = body.get("groups")
        progress = plan_progress(groups)
    forced = force and note is not None and note.strip()
    if not plan_complete(progress) and not forced:
        print(
            f"refusing to archive {ws}: plan {progress.get('done')}/{progress.get('total')} done. "
            f"Finish the plan, or re-run with --force --note \"why it archived early\": "
            f"python3 agent_dash.py ws archive {ws} --force --note \"...\"",
            flush=True,
        )
        return errors.USAGE
    if force and (note is None or not note.strip()):
        print(
            f"--force needs --note TEXT. Run: python3 agent_dash.py ws archive {ws} --force --note \"...\"",
            flush=True,
        )
        return errors.USAGE
    report = closing_report_path(path)
    has_report = report.is_file()
    if not has_report and not force:
        print(
            f"refusing to archive {ws}: missing {CLOSING_REPORT} in the bundle. "
            f"Write the closing report first (what shipped, decisions, residuals, links), "
            f"or re-run with --force: python3 agent_dash.py ws archive {ws} --force --note \"...\"",
            flush=True,
        )
        return errors.USAGE
    trains = body.get("trains") if isinstance(body.get("trains"), list) else []
    approvals = approvals_for(ws, root)
    usage = fetch_workstream_usage(base_url, ws)
    started_at, _last = _history_bounds(path)
    summary = freeze_summary(
        ws,
        started_at=started_at,
        finished_at=moment,
        progress=progress,
        trains=trains,
        approvals=approvals,
        usage=usage,
        closing_report=has_report,
        note=note.strip() if isinstance(note, str) and note.strip() else None,
    )
    with _bundle_lock(path):
        meta = _read_json(path / "bundle.json")
        if not isinstance(meta, dict):
            print(f"bundle.json for {ws} is unreadable. Run: python3 agent_dash.py ws validate {ws}", flush=True)
            return errors.INVALID_DATA
        frm = current_state(meta)
        if frm == "archived":
            print(f"{ws} is already archived. Run: python3 agent_dash.py ws restore {ws} to reopen it", flush=True)
            return errors.USAGE
        meta["state"] = "archived"
        meta["state_changed_at"] = moment
        meta["archived_at"] = moment
        _atomic_write(path / "bundle.json", json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
        _atomic_write(path / "summary.json", json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
        append_state_history(path, ws, frm, "archived", summary["note"], moment)
    print(f"archived {ws} (was {frm})")
    return errors.OK


def restore_bundle(ws: str, *, root: Path | None = None, now: float | None = None) -> int:
    """Reopen an archived workstream to ``done`` (``archived`` -> ``done``).

    Only archived bundles restore (anything else exits 2). Keeps
    ``summary.json`` as the "previously archived" record, clears
    ``archived_at``, and appends one ``{kind: state}`` history line.
    """
    from dash.bundles import _atomic_write, _bundle_lock, _read_json, bundle_dir

    path = bundle_dir(ws, root)
    if path is None:
        print(errors.bundle_not_found(ws), flush=True)
        return errors.NOT_FOUND
    moment = time.time() if now is None else now
    with _bundle_lock(path):
        meta = _read_json(path / "bundle.json")
        if not isinstance(meta, dict):
            print(f"bundle.json for {ws} is unreadable. Run: python3 agent_dash.py ws validate {ws}", flush=True)
            return errors.INVALID_DATA
        frm = current_state(meta)
        if frm != "archived":
            print(
                f"only archived workstreams restore ({ws} is {frm}). "
                f"Run: python3 agent_dash.py ws list --state archived",
                flush=True,
            )
            return errors.USAGE
        meta["state"] = "done"
        meta["state_changed_at"] = moment
        meta["archived_at"] = None
        _atomic_write(path / "bundle.json", json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
        append_state_history(path, ws, "archived", "done", None, moment)
    print(f"{ws} archived -> done (summary.json kept)")
    return errors.OK


def _progress_for(ws: str, root: Path | None) -> dict | None:
    from dash.bundles import _read_json, bundle_dir, resolve_plan
    from dash.plans import plan_progress

    path = bundle_dir(ws, root)
    if path is not None:
        plan = _read_json(path / "plan.json")
        if isinstance(plan, dict):
            dag = plan.get("dag") if isinstance(plan.get("dag"), dict) else {}
            groups = dag.get("groups") if isinstance(dag.get("groups"), list) else []
            try:
                return plan_progress(groups)
            except Exception:
                pass
    try:
        _source, body = resolve_plan(ws)
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    progress = body.get("progress")
    return progress if isinstance(progress, dict) else None


def list_workstreams(
    states: list[str] | None,
    *,
    root: Path | None = None,
    json_mode: bool = False,
) -> int:
    """List bundles filtered by lifecycle state.

    ``states`` is None for the default scope (active, paused, planned,
    done). Rows carry id, title, state, progress, and state_changed_at.
    """
    from dash.bundles import list_bundles

    if states is None:
        wanted: set[str] | None = set(DEFAULT_LIST_STATES)
    elif "all" in states:
        wanted = None
    else:
        unknown = [item for item in states if item not in STATES]
        if unknown:
            print(
                f"unknown state {unknown[0]!r}. Use one of: {', '.join(STATES)},all. "
                f"Run: python3 agent_dash.py ws list --state all",
                flush=True,
            )
            return errors.USAGE
        wanted = set(states)
    rows = []
    for bundle in list_bundles(root):
        state = bundle.get("state") if bundle.get("state") in STATES else DEFAULT_STATE
        if wanted is not None and state not in wanted:
            continue
        rows.append(
            {
                "id": bundle["id"],
                "title": bundle["title"],
                "state": state,
                "progress": _progress_for(bundle["id"], root),
                "state_changed_at": bundle.get("state_changed_at"),
            }
        )
    if json_mode:
        errors.emit_json({"workstreams": rows})
        return errors.OK
    if not rows:
        print("(no workstreams)")
        return errors.OK
    print("ID\tTITLE\tSTATE\tPROGRESS\tCHANGED")
    for item in rows:
        progress = item["progress"]
        if isinstance(progress, dict):
            progress_text = f"{progress.get('done')}/{progress.get('total')} ({progress.get('pct')}%)"
        else:
            progress_text = "-"
        changed = item["state_changed_at"]
        print(f"{item['id']}\t{item['title']}\t{item['state']}\t{progress_text}\t{changed if changed is not None else '-'}")
    return errors.OK


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in str(value).split(",") if part.strip()]


def cmd_state(argv: list[str], json_mode: bool = False) -> int:
    """``ws state <ID> <state> [--force] [--note TEXT]``."""
    from dash.bundles import WS_USAGE

    if len(argv) < 2:
        print(WS_USAGE, end="", flush=True)
        return errors.USAGE
    ws, target = argv[0], argv[1]
    force = False
    note = None
    flags = argv[2:]
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag == "--force":
            force = True
            index += 1
        elif flag == "--note" and index + 1 < len(flags):
            note = flags[index + 1]
            index += 2
        else:
            print(WS_USAGE, end="", flush=True)
            return errors.USAGE
    code = set_state(ws, target, force=force, note=note)
    if json_mode and code == errors.OK:
        from dash.bundles import _read_json, bundle_dir

        path = bundle_dir(ws)
        meta = _read_json(path / "bundle.json") if path is not None else None
        errors.emit_json({"ws": ws, "state": current_state(meta), "ok": True})
    return code


def cmd_archive(argv: list[str], json_mode: bool = False) -> int:
    """``ws archive <ID> [--force] [--note TEXT]``."""
    from dash.bundles import WS_USAGE

    if len(argv) < 1:
        print(WS_USAGE, end="", flush=True)
        return errors.USAGE
    ws = argv[0]
    force = False
    note = None
    flags = argv[1:]
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag == "--force":
            force = True
            index += 1
        elif flag == "--note" and index + 1 < len(flags):
            note = flags[index + 1]
            index += 2
        else:
            print(WS_USAGE, end="", flush=True)
            return errors.USAGE
    code = archive_bundle(ws, force=force, note=note)
    if json_mode and code == errors.OK:
        errors.emit_json({"ws": ws, "state": "archived", "ok": True})
    return code


def cmd_restore(argv: list[str], json_mode: bool = False) -> int:
    """``ws restore <ID>``."""
    from dash.bundles import WS_USAGE

    if len(argv) != 1:
        print(WS_USAGE, end="", flush=True)
        return errors.USAGE
    code = restore_bundle(argv[0])
    if json_mode and code == errors.OK:
        errors.emit_json({"ws": argv[0], "state": "done", "ok": True})
    return code


def cmd_list(argv: list[str], json_mode: bool = False) -> int:
    """``ws list [--state S[,S...]|all]``."""
    from dash.bundles import WS_USAGE

    states: list[str] | None = None
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag == "--state" and index + 1 < len(argv):
            states = _split_csv(argv[index + 1])
            index += 2
        else:
            print(WS_USAGE, end="", flush=True)
            return errors.USAGE
    return list_workstreams(states, json_mode=json_mode)
