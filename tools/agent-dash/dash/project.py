"""Project descriptor: one project per dashboard instance.

``project.json`` lives in the data root (see ``dash/dataroot.py``)::

    {
      "name": "Demo project",
      "short": "demo",
      "description": "What the project is for.",
      "goals": ["One phrase per goal"],
      "started": "2026-10-15",
      "target": null,
      "phases": [{"id": "p1", "title": "First cut",
                  "summary": "What ships.",
                  "gate": ["WS1", "WS2"]}],
      "links": [],
      "owner_notes": ""
    }

``phases[].gate`` names member workstream ids (a list) or carries gate
text (a string). A phase ``state`` is computed, never stored: ``done``
when every member workstream is ``done``/``archived``, ``active`` while
any member is ``active``/``paused``, ``planned`` before that, ``dropped``
when every member is ``dropped``. Member states come from ``bundle.json``
``state`` when present, else ``active``. A missing ``project.json`` is
fine: the name reads null and the header falls back to "Agent dashboard".
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dash import dataroot, errors

PROJECT_USAGE = """agent-dash project descriptor

  python3 agent_dash.py project init <name> [--data-root DIR]
  python3 agent_dash.py project show [--json] [--data-root DIR]
  python3 agent_dash.py project set <field> <value> [--data-root DIR]
  python3 agent_dash.py project add <id> --data-root DIR [--name NAME] [--port N] [--init] [--json]
  python3 agent_dash.py project list [--json]
  python3 agent_dash.py project launch <id> [--json]
  python3 agent_dash.py project stop <id> [--json]
  python3 agent_dash.py project open <id> [--json]
  python3 agent_dash.py project remove <id> [--json]

<field> is one of: name, short, description, target. init creates
project.json plus the data dirs and refuses to overwrite an existing
file. show prints the descriptor with computed phase states (exit 4
when the file fails validation). Missing project.json is fine: the
name reads null and the page header falls back to "Agent dashboard".
add/list/launch/stop/open/remove manage the multi-project registry
(projects.json, default ~/.config/agent-dash/projects.json): one
dashboard instance per project, each with its own data root, state,
port, and name. Agents working on a project pass --project <id> (or
set the env) for state, check, doctor, ws ..., status.
Exit codes: 0 ok, 1 check failed, 2 usage error, 3 not found, 4 invalid data.
"""

LIFECYCLE_STATES = ("planned", "active", "paused", "done", "archived", "dropped")
SETTABLE_FIELDS = ("name", "short", "description", "target")
_WS_ID = re.compile(r"^WS\d+$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _ws_root(cfg: dict | None = None, root=None) -> Path:
    """Bundles dir for a data root: the ``AGENT_DASH_WORKSTREAMS`` override wins."""
    override = os.environ.get(dataroot.WORKSTREAMS_ENV)
    if override and override.strip():
        from dash.locate import expand

        return Path(expand(override.strip()))
    base = Path(root) if root is not None else dataroot.data_root(cfg)
    return base / "workstreams"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_project(cfg: dict | None = None, root=None):
    """``(data-or-None, path, issues)`` for the project descriptor.

    A missing file is fine: ``(None, path, [])``. An unreadable or
    misshapen file returns its validation issues; the data is returned
    as well when it parsed, else None.
    """
    path = dataroot.project_file(cfg, root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, path, []
    try:
        data = json.loads(text)
    except ValueError:
        return None, path, [
            f"project.json at {path} is not valid JSON. "
            "Fix the file, then run: python3 agent_dash.py project show"
        ]
    issues = validate_project(data, source=str(path))
    return data, path, issues


def validate_project(data, source: str = "project.json") -> list[str]:
    """Name-before-id validation issues for a parsed descriptor. Empty when clean."""
    hint = "Fix project.json, then run: python3 agent_dash.py project show"
    issues: list[str] = []
    if not isinstance(data, dict):
        return [f"project descriptor in {source} must hold an object (got {type(data).__name__}). {hint}"]
    name = data.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        issues.append(f'project "name" must be a non-empty string (got {name!r}). {hint}')
    for field in ("short", "description", "owner_notes"):
        value = data.get(field)
        if value is not None and not isinstance(value, str):
            issues.append(f'project "{field}" must be a string (got {value!r}). {hint}')
    for field in ("started", "target"):
        value = data.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            issues.append(f'project "{field}" must be a date string or null (got {value!r}). {hint}')
    goals = data.get("goals")
    if goals is not None:
        if not isinstance(goals, list) or any(not isinstance(item, str) for item in goals):
            issues.append(f'project "goals" must be a list of strings (got {goals!r}). {hint}')
    links = data.get("links")
    if links is not None:
        if not isinstance(links, list) or any(not isinstance(item, (str, dict)) for item in links):
            issues.append(f'project "links" must be a list of links (got {links!r}). {hint}')
    phases = data.get("phases")
    if phases is not None:
        if not isinstance(phases, list):
            issues.append(f'project "phases" must be a list of phases (got {phases!r}). {hint}')
        else:
            seen: set[str] = set()
            for phase in phases:
                issues.extend(_validate_phase(phase, seen, hint))
    return issues


def _validate_phase(phase, seen: set[str], hint: str) -> list[str]:
    if not isinstance(phase, dict):
        return [f"project phase must hold an object (got {phase!r}). {hint}"]
    issues: list[str] = []
    pid = phase.get("id")
    title = phase.get("title")
    label = f'phase "{title} ({pid})"' if isinstance(title, str) and title.strip() else f"phase {pid!r}"
    if not isinstance(pid, str) or not pid.strip():
        issues.append(f"project phase id must be a non-empty string (got {pid!r}). {hint}")
        return issues
    if pid in seen:
        issues.append(f"{label}: duplicate phase id. Give each phase its own id. {hint}")
    seen.add(pid)
    if not isinstance(title, str) or not title.strip():
        issues.append(f"{label}: \"title\" must be a non-empty string. {hint}")
    summary = phase.get("summary")
    if summary is not None and not isinstance(summary, str):
        issues.append(f"{label}: \"summary\" must be a string (got {summary!r}). {hint}")
    gate = phase.get("gate")
    if gate is not None and not isinstance(gate, str):
        if not isinstance(gate, list) or any(not isinstance(item, str) or not item for item in gate):
            issues.append(
                f"{label}: \"gate\" must be workstream ids or gate text "
                f"(a list of strings or a string, got {gate!r}). {hint}"
            )
    return issues


def bundle_state(ws: str, ws_root: Path | None = None) -> str:
    """Lifecycle state for one workstream: ``bundle.json`` state, else active."""
    try:
        from dash.bundles import bundle_dir

        path = bundle_dir(ws, ws_root) if ws_root is not None else bundle_dir(ws)
    except Exception:
        return "active"
    if path is None:
        return "planned"
    data = _read_json(path / "bundle.json")
    if not isinstance(data, dict):
        return "active"
    state = data.get("state")
    return state if isinstance(state, str) and state in LIFECYCLE_STATES else "active"


def phase_members(phase: dict, ws_root: Path | None = None) -> list[str]:
    """Workstream ids a phase gates on: a list gate, or a lone id string."""
    gate = phase.get("gate") if isinstance(phase, dict) else None
    if isinstance(gate, list):
        return [item for item in gate if isinstance(item, str) and item]
    if isinstance(gate, str) and gate.strip():
        text = gate.strip()
        if " " not in text and _SAFE_ID.match(text):
            if _WS_ID.match(text):
                return [text]
            try:
                from dash.bundles import bundle_dir

                if bundle_dir(text, ws_root) is not None:
                    return [text]
            except Exception:
                pass
    return []


def phase_state(members: list[str], states: dict) -> str:
    """Computed phase state over member workstream states."""
    if not members:
        return "planned"
    values = [states.get(ws, "planned") for ws in members]
    if all(value in ("done", "archived") for value in values):
        return "done"
    if all(value == "dropped" for value in values):
        return "dropped"
    if any(value in ("active", "paused") for value in values):
        return "active"
    if all(value == "planned" for value in values):
        return "planned"
    return "active"


def _zero_totals() -> dict:
    return {
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0},
        "cost_usd": 0.0,
    }


def build_payload(store=None, cfg: dict | None = None, root=None, now: float | None = None) -> dict:
    """The ``GET /api/project`` body: descriptor plus computed roll-up.

    Never raises and never exposes a filesystem path: the data root
    travels as ``data_root_hash`` only. Lifetime totals reuse the
    existing ``totals_all_time`` summary.
    """
    moment = float(now if now is not None else time.time())
    data, _path, issues = read_project(cfg, root)
    if not isinstance(data, dict):
        data = {}
    ws_root = _ws_root(cfg, root)
    try:
        from dash.bundles import list_bundles

        bundles = list_bundles(ws_root)
    except Exception:
        bundles = []
    states = {item["id"]: bundle_state(item["id"], ws_root) for item in bundles if isinstance(item, dict)}
    counts = {state: 0 for state in LIFECYCLE_STATES}
    for state in states.values():
        counts[state] = counts.get(state, 0) + 1
    phases = []
    raw_phases = data.get("phases")
    if not isinstance(raw_phases, list):
        raw_phases = []
    for phase in raw_phases:
        if not isinstance(phase, dict):
            continue
        members = phase_members(phase, ws_root)
        member_states = dict(states)
        for ws in members:
            member_states.setdefault(ws, "planned")
        phases.append(
            {
                "id": phase.get("id"),
                "title": phase.get("title"),
                "summary": phase.get("summary") if isinstance(phase.get("summary"), str) else "",
                "gate": phase.get("gate") if isinstance(phase.get("gate"), (list, str)) else [],
                "state": phase_state(members, member_states),
                "members": [{"ws": ws, "state": member_states.get(ws, "planned")} for ws in members],
            }
        )
    totals = _zero_totals()
    if store is not None:
        try:
            snapshot = store.snapshot_cached(moment, "all")
            candidate = snapshot.get("totals_all_time")
            if isinstance(candidate, dict):
                totals = candidate
        except Exception:
            totals = _zero_totals()
    try:
        from dash.actions import actions_enabled

        actions_on = bool(actions_enabled(cfg))
    except Exception:
        actions_on = False
    payload = {
        "id": (os.environ.get("AGENT_DASH_PROJECT_ID") or "").strip() or None,
        "name": data.get("name") if isinstance(data.get("name"), str) else None,
        "actions": {"archive": actions_on},
        "short": data.get("short") if isinstance(data.get("short"), str) else "",
        "description": data.get("description") if isinstance(data.get("description"), str) else "",
        "goals": list(data.get("goals")) if isinstance(data.get("goals"), list) else [],
        "started": data.get("started") if isinstance(data.get("started"), str) else None,
        "target": data.get("target") if isinstance(data.get("target"), str) else None,
        "phases": phases,
        "links": list(data.get("links")) if isinstance(data.get("links"), list) else [],
        "owner_notes": data.get("owner_notes") if isinstance(data.get("owner_notes"), str) else "",
        "data_root_hash": dataroot.data_root_hash(cfg, root),
        "totals_all_time": totals,
        "counts": counts,
        "issues": issues,
    }
    return payload


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "project"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _template(name: str) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "name": name,
        "short": _slug(name),
        "description": "",
        "goals": [],
        "started": today,
        "target": None,
        "phases": [],
        "links": [],
        "owner_notes": "",
    }


def cmd_project(argv: list[str]) -> int:
    """Project descriptor commands: init, show, set. Read-only except writes."""
    args = list(argv or [])
    if not args or args[0] in ("-h", "--help"):
        print(PROJECT_USAGE, end="")
        return 0 if args and args[0] in ("-h", "--help") else errors.USAGE
    from dash.projects import COMMANDS, cmd_projects

    if args[0] in COMMANDS:
        return cmd_projects(args)
    cmd = args[0]
    rest = args[1:]
    root = None
    json_mode = False
    positional: list[str] = []
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--data-root" and index + 1 < len(rest):
            from dash.locate import expand

            root = Path(expand(rest[index + 1]))
            index += 2
        elif token == "--json" and cmd == "show":
            json_mode = True
            index += 1
        elif token.startswith("-"):
            print(PROJECT_USAGE, end="", file=sys.stderr)
            return errors.USAGE
        else:
            positional.append(token)
            index += 1
    if cmd == "init":
        if len(positional) != 1 or not positional[0].strip():
            print(
                'project init needs one name, e.g. python3 agent_dash.py project init "Demo project"',
                file=sys.stderr,
            )
            return errors.USAGE
        return cmd_init(positional[0].strip(), root)
    if cmd == "show":
        if positional:
            print(PROJECT_USAGE, end="", file=sys.stderr)
            return errors.USAGE
        return cmd_show(root, json_mode)
    if cmd == "set":
        if len(positional) != 2:
            print(
                "project set needs a field and a value (field is name, short, description, target). "
                'Run: python3 agent_dash.py project set description "What ships."',
                file=sys.stderr,
            )
            return errors.USAGE
        return cmd_set(positional[0], positional[1], root)
    print(PROJECT_USAGE, end="", file=sys.stderr)
    return errors.USAGE


def cmd_init(name: str, root=None) -> int:
    """Create ``project.json`` plus the data dirs. Refuses to overwrite."""

    if not name.strip():
        print('project init needs a non-empty name. Run: python3 agent_dash.py project init "Demo project"', file=sys.stderr)
        return errors.USAGE
    base = Path(root) if root is not None else dataroot.data_root(None)
    dest = base / dataroot.PROJECT_FILENAME
    if dest.exists():
        print(
            f"project {name!r} already has {dest}: refusing to overwrite. "
            "Run: python3 agent_dash.py project show",
            file=sys.stderr,
        )
        return errors.USAGE
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / "workstreams").mkdir(parents=True, exist_ok=True)
        _atomic_write(dest, json.dumps(_template(name.strip()), indent=2) + "\n")
    except OSError as exc:
        print(f"project init failed for {dest}: {exc}. Run: python3 agent_dash.py project show", file=sys.stderr)
        return errors.INVALID_DATA
    print(f"created {dest}")
    print(f'Run: python3 agent_dash.py project show (header reads "{name.strip()}" on reload)')
    return errors.OK


def _open_store(state_dir: Path):
    db = state_dir / "dash.sqlite"
    if not db.is_file():
        return None
    try:
        from dash.store import Store

        return Store(db)
    except Exception:
        return None


def cmd_show(root=None, json_mode: bool = False) -> int:
    """Print the descriptor with computed phase states and lifetime totals."""

    cfg = {"data_root": str(root)} if root is not None else None
    data, path, _missing_issues = read_project(cfg, root)
    store = _open_store(Path(root) if root is not None else dataroot.state_dir(cfg))
    try:
        payload = build_payload(store, cfg, root)
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
    if data is None and not payload["issues"]:
        notice = (
            f"no project.json at {path} (the header falls back to \"Agent dashboard\"). "
            'Run: python3 agent_dash.py project init "Demo project"'
        )
        if json_mode:
            errors.emit_json(payload)
        else:
            print(notice)
        return errors.OK
    if json_mode:
        errors.emit_json(payload)
    else:
        _print_human(payload, path)
    if payload["issues"]:
        print(
            f"project show found {len(payload['issues'])} issue(s) in {path}. "
            "Fix the file, then run: python3 agent_dash.py project show",
            file=sys.stderr,
        )
        return errors.INVALID_DATA
    return errors.OK


def _print_human(payload: dict, path: Path) -> None:
    name = payload.get("name") or "(unnamed)"
    print(f"project {name} ({payload.get('short') or '-'})")
    if payload.get("description"):
        print(payload["description"])
    for goal in payload.get("goals") or []:
        print(f"  - {goal}")
    started = payload.get("started") or "-"
    target = payload.get("target") or "-"
    print(f"started {started} target {target}")
    for phase in payload.get("phases") or []:
        members = ", ".join(item["ws"] for item in phase.get("members") or []) or "-"
        print(f"  {phase.get('id')} {phase.get('title')} [{phase.get('state')}] gate {members}")
    counts = payload.get("counts") or {}
    print("workstreams: " + ", ".join(f"{state}={counts.get(state, 0)}" for state in LIFECYCLE_STATES))
    totals = payload.get("totals_all_time") or {}
    tokens = totals.get("tokens") or {}
    billed = sum(int(tokens.get(key) or 0) for key in ("input", "output", "cache_read", "cache_write"))
    print(f"lifetime: {billed} tokens cost_usd {float(totals.get('cost_usd') or 0.0):.2f}")
    print(f"source: {path}")


def cmd_set(field: str, value: str, root=None) -> int:
    """Set one descriptor field: name, short, description, or target."""

    if field not in SETTABLE_FIELDS:
        print(
            f"unknown project field {field!r}. Use one of: {', '.join(SETTABLE_FIELDS)}. "
            "Run: python3 agent_dash.py project show",
            file=sys.stderr,
        )
        return errors.USAGE
    cfg = {"data_root": str(root)} if root is not None else None
    data, path, _issues = read_project(cfg, root)
    if data is None:
        print(
            f"no project.json at {path}. "
            'Run: python3 agent_dash.py project init "Demo project"',
            file=sys.stderr,
        )
        return errors.NOT_FOUND
    if not isinstance(data, dict):
        print(
            f"project.json at {path} must hold an object. "
            "Fix the file, then run: python3 agent_dash.py project show",
            file=sys.stderr,
        )
        return errors.INVALID_DATA
    if field == "name" and not value.strip():
        print(
            'project "name" must be a non-empty string. '
            "Run: python3 agent_dash.py project show",
            file=sys.stderr,
        )
        return errors.INVALID_DATA
    data[field] = None if field == "target" and value.strip().lower() in ("", "null", "none") else value
    issues = validate_project(data, source=str(path))
    relevant = [issue for issue in issues if f'"{field}"' in issue or "must hold an object" in issue]
    if relevant:
        for issue in relevant:
            print(issue, file=sys.stderr)
        return errors.INVALID_DATA
    try:
        _atomic_write(path, json.dumps(data, indent=2) + "\n")
    except OSError as exc:
        print(f"project set failed for {path}: {exc}. Run: python3 agent_dash.py project show", file=sys.stderr)
        return errors.INVALID_DATA
    print(f"set project {field} to {data[field]!r} in {path}")
    return errors.OK
