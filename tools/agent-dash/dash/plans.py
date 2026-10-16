"""Pass through a workstream DAG and its narrative phases."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "plans.json"
MAX_BYTES = 2_000_000


class PlanNotFound(Exception):
    def __init__(self, ws: str):
        self.ws = ws
        super().__init__(ws)


class PlanError(Exception):
    pass


def configured(path: Path | None = None) -> set[str]:
    return set(mapping(path))


def mapping(path: Path | None = None) -> dict:
    return _mapping(path)


def load(ws: str, path: Path | None = None) -> dict:
    mapping = _mapping(path)
    target = mapping.get(ws)
    if not target:
        raise PlanNotFound(ws)
    file = Path(target)
    if not file.is_file():
        raise PlanNotFound(ws)
    try:
        size = file.stat().st_size
    except OSError as exc:
        raise PlanError("plan file unreadable") from exc
    if size > MAX_BYTES:
        raise PlanError("plan file too large")
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError("plan file unreadable") from exc
    if not isinstance(data, dict):
        raise PlanError("plan file unreadable")
    dag = data.get("dag") if isinstance(data.get("dag"), dict) else {}
    narrative = data.get("narrative") if isinstance(data.get("narrative"), dict) else {}
    return {
        "ws": ws,
        "cols": _strings(dag.get("cols")),
        "groups": _groups(dag.get("groups")),
        "edges": _edges(dag.get("edges")),
        "phases": _phases(narrative.get("phases")),
        "trains": _trains(data.get("trains")),
    }


def _mapping(path: Path | None) -> dict:
    if path is None:
        from dash.locate import layered_path

        found = layered_path("plans", "AGENT_DASH_PLANS")
        path = found if found is not None else DEFAULT_PATH
    else:
        path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    base = path.parent
    out = {}
    for key, value in data.items():
        if isinstance(value, str):
            out[str(key)] = _resolve(value, base)
    return out


def _resolve(value: str, base: Path) -> str:
    from dash.locate import expand

    target = Path(expand(value))
    if not target.is_absolute():
        target = (base / target).resolve()
    return str(target)


def _strings(value) -> list:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _groups(value) -> list:
    if not isinstance(value, list):
        return []
    groups = []
    for item in value:
        if not isinstance(item, dict):
            continue
        tasks = []
        for task in item.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            tasks.append(
                {"id": task.get("id"), "title": task.get("title"), "status": task.get("status")}
            )
        groups.append(
            {
                "id": item.get("id"),
                "col": item.get("col"),
                "title": item.get("title"),
                "tasks": tasks,
            }
        )
    return groups


def _edges(value) -> list:
    if not isinstance(value, list):
        return []
    edges = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            edges.append([item[0], item[1]])
    return edges


def _phases(value) -> list:
    if not isinstance(value, list):
        return []
    phases = []
    for item in value:
        if not isinstance(item, dict):
            continue
        phases.append({"name": item.get("name"), "goal": item.get("goal"), "gate": item.get("gate")})
    return phases


def _trains(value) -> list:
    """Optional top-level merge trains. Unknown keys are dropped, like other fields."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        pr = item.get("pr")
        out.append(
            {
                "id": item.get("id") if isinstance(item.get("id"), str) else None,
                "title": item.get("title") if isinstance(item.get("title"), str) else None,
                "branch": item.get("branch") if isinstance(item.get("branch"), str) else None,
                "members": [entry for entry in item.get("members") or [] if isinstance(entry, str)]
                if isinstance(item.get("members"), list)
                else [],
                "status": item.get("status") if isinstance(item.get("status"), str) else None,
                "pr": pr if isinstance(pr, int) and not isinstance(pr, bool) else None,
                "shared_files": [entry for entry in item.get("shared_files") or [] if isinstance(entry, str)]
                if isinstance(item.get("shared_files"), list)
                else [],
                "gate": item.get("gate") if isinstance(item.get("gate"), str) else None,
                "note": item.get("note") if isinstance(item.get("note"), str) else None,
            }
        )
    return out
