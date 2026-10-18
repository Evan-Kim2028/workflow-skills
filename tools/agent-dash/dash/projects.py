"""Multi-project registry and launcher: one dashboard instance per project.

Each project is one row in ``projects.json`` (default
``~/.config/agent-dash/projects.json``, overridden by
``AGENT_DASH_PROJECTS`` — never inside any data root)::

    {"projects": [{"id": "demo", "name": "Demo project",
                   "data_root": "/home/ada/dash-data/demo",
                   "port": 8765, "added_at": 1761000000}]}

One instance serves one project: its own data root (project.json,
workstreams/, sqlite state), its own port, its own name. This module
never opens another project's data root or state DB: liveness comes
only from the registry row plus a 1 s ``GET /api/health`` probe on
127.0.0.1:port, matched by ``state_hash`` (a different project
answering on that port reads as "port taken by another", never as
running). ``GET /api/projects`` is built the same way and cached 10 s.

``project launch <id>`` starts the instance exactly as
``agent_dash.py run`` does, with ``AGENT_DASH_DATA_ROOT``,
``AGENT_DASH_STATE_DIR`` (both the registry data root),
``AGENT_DASH_PORT``, and ``AGENT_DASH_PROJECT_ID`` set for that
project. ``project stop <id>`` runs the guarded stop with the same
env so the foreign-state guard passes only for that project's own
dashboard. ``project remove`` edits the registry only, never data.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from dash import errors

PROJECTS_ENV = "AGENT_DASH_PROJECTS"
PROJECT_ID_ENV = "AGENT_DASH_PROJECT_ID"
DATA_ROOT_ENV = "AGENT_DASH_DATA_ROOT"
STATE_ENV = "AGENT_DASH_STATE_DIR"
PORT_ENV = "AGENT_DASH_PORT"
URL_ENV = "AGENT_DASH_URL"

COMMANDS = ("add", "list", "launch", "stop", "open", "remove")

DEFAULT_START_PORT = 8765
PROBE_TIMEOUT_S = 1.0
CACHE_TTL_S = 10.0

_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

PROJECTS_USAGE = """agent-dash project registry: one dashboard instance per project

  python3 agent_dash.py project add <id> --data-root DIR [--name NAME] [--port N] [--init] [--json]
  python3 agent_dash.py project list [--json]
  python3 agent_dash.py project launch <id> [--json]
  python3 agent_dash.py project stop <id> [--json]
  python3 agent_dash.py project open <id> [--json]
  python3 agent_dash.py project remove <id> [--json]

add registers a project (default port is the lowest free port from
8765 up not used by another registered project; --init also creates
project.json in a missing data root). list shows the registry with a
health probe per row. launch starts that project's instance in the
background and prints its URL. stop stops only that project's own
dashboard. open opens its URL on macOS. remove unregisters only
(never touches data). Agents working on a project pass --project <id>
(or set the env) for state, check, doctor, ws ..., status.
Exit codes: 0 ok, 1 check failed, 2 usage error, 3 not found,
4 invalid data, 5 dashboard unreachable.
"""


def registry_path() -> Path:
    """The registry file: ``AGENT_DASH_PROJECTS`` wins, else the default."""
    override = os.environ.get(PROJECTS_ENV)
    if override and override.strip():
        return Path(override.strip()).expanduser()
    return Path.home() / ".config" / "agent-dash" / "projects.json"


def own_project_id() -> str | None:
    """This instance's project id when launched via ``project launch``."""
    raw = os.environ.get(PROJECT_ID_ENV)
    if raw is not None and raw.strip():
        return raw.strip()
    return None


def _hash_path(path) -> str:
    """Short opaque id for a directory: sha256 of the resolved path.

    Same construction as the server's ``state_hash`` (which hashes the
    state dir): ``project launch`` sets the state dir to the data root,
    so a probe's ``state_hash`` equals this for the project's own
    dashboard and differs for any other project on that port.
    """
    import hashlib

    try:
        text = os.path.realpath(str(path or ""))
    except (OSError, ValueError):
        text = str(path or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def expected_state_hash(entry: dict) -> str:
    """The ``state_hash`` a healthy instance of this entry must serve."""
    return _hash_path(entry.get("data_root"))


def _blank_registry() -> dict:
    return {"projects": []}


def load_registry() -> tuple[dict, Path]:
    """``(registry, path)``. A missing file reads as an empty registry."""
    path = registry_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return _blank_registry(), path
    try:
        data = json.loads(text)
    except ValueError:
        return {"projects": [], "invalid": True}, path
    if not isinstance(data, dict) or not isinstance(data.get("projects", []), list):
        return {"projects": [], "invalid": True}, path
    rows = [row for row in data["projects"] if isinstance(row, dict)]
    return {"projects": rows}, path


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_registry(registry: dict) -> Path:
    """Persist the registry atomically. Returns the path written."""
    path = registry_path()
    _atomic_write(path, json.dumps({"projects": registry.get("projects", [])}, indent=2) + "\n")
    return path


def find_entry(registry: dict, pid: str) -> dict | None:
    """The registry row for ``pid``, or None."""
    for row in registry.get("projects", []):
        if isinstance(row, dict) and row.get("id") == pid:
            return row
    return None


def used_ports(registry: dict) -> set[int]:
    """Ports held by registered projects."""
    ports: set[int] = set()
    for row in registry.get("projects", []):
        if not isinstance(row, dict):
            continue
        try:
            port = int(row.get("port"))
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            ports.add(port)
    return ports


def default_port(registry: dict) -> int:
    """The lowest free port from 8765 up not used by another project."""
    taken = used_ports(registry)
    port = DEFAULT_START_PORT
    while port in taken:
        port += 1
    return port


def probe_health(port: int, timeout_s: float = PROBE_TIMEOUT_S) -> dict | None:
    """GET /api/health on 127.0.0.1:port, or None when it does not answer.

    The only read this module ever does of another project: one HTTP
    probe, never a data-root or state-DB open. Never raises.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/api/health", timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def entry_health(entry: dict, probe: dict | None = None) -> dict:
    """Liveness for one registry row: running only on a matching hash.

    ``running`` is True only when the port answers ``/api/health`` with
    this entry's expected ``state_hash``. A different project answering
    on that port reads ``running: False`` with ``port_taken: True``.
    """
    if probe is None:
        try:
            probe = probe_health(int(entry.get("port")))
        except (TypeError, ValueError):
            probe = None
    expected = expected_state_hash(entry)
    if not isinstance(probe, dict):
        return {"running": False, "port_taken": False, "build_id": None,
                "state_hash": None, "project_id": None}
    state_hash = probe.get("state_hash")
    match = isinstance(state_hash, str) and state_hash == expected
    build = probe.get("build_id")
    return {
        "running": bool(match),
        "port_taken": not match,
        "build_id": str(build) if isinstance(build, str) and build else None,
        "state_hash": state_hash if isinstance(state_hash, str) else None,
        "project_id": probe.get("project_id") if isinstance(probe.get("project_id"), str) else None,
    }


def _own_port(cfg: dict | None) -> int:
    if isinstance(cfg, dict):
        try:
            port = int(cfg.get("port"))
            if 1 <= port <= 65535:
                return port
        except (TypeError, ValueError):
            pass
    raw = os.environ.get(PORT_ENV, "")
    if raw.strip():
        try:
            port = int(raw.strip())
            if 1 <= port <= 65535:
                return port
        except ValueError:
            pass
    return DEFAULT_START_PORT


def _own_name(cfg: dict | None) -> str | None:
    """This instance's project name from its own data root only."""
    try:
        from dash import project as project_mod

        payload = project_mod.build_payload(None, cfg)
        name = payload.get("name")
        return name if isinstance(name, str) and name.strip() else None
    except Exception:
        return None


_CACHE_AT = 0.0
_CACHE_KEY: tuple | None = None
_CACHE_VAL: dict | None = None


def clear_cache() -> None:
    """Drop the cached /api/projects payload (tests only)."""
    global _CACHE_AT, _CACHE_KEY, _CACHE_VAL
    _CACHE_AT = 0.0
    _CACHE_KEY = None
    _CACHE_VAL = None


def build_projects_payload(cfg: dict | None = None, now: float | None = None) -> dict:
    """The ``GET /api/projects`` body: registry rows plus 1 s probes.

    Each entry carries ``id``, ``name``, ``port``, ``running``, and
    ``current`` (true for this instance) — never a filesystem path.
    Probes are cached 10 s. With no registry file (or an empty or
    unreadable one) the body holds just this instance. Never reads any
    other project's data root: names come from the registry, liveness
    from ``/api/health`` over HTTP.
    """
    moment = float(now if now is not None else time.time())
    registry, path = load_registry()
    rows = [row for row in registry.get("projects", []) if isinstance(row, dict)]
    me = own_project_id()
    mine = _own_port(cfg)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    key = (str(path), me, mine, mtime, len(rows))
    global _CACHE_AT, _CACHE_KEY, _CACHE_VAL
    if _CACHE_VAL is not None and _CACHE_KEY == key and moment - _CACHE_AT < CACHE_TTL_S:
        return json.loads(json.dumps(_CACHE_VAL))
    if not rows:
        payload = {
            "projects": [
                {"id": me, "name": _own_name(cfg), "port": mine,
                 "running": True, "current": True}
            ],
            "current": me,
        }
    else:
        entries = []
        for row in rows:
            try:
                port = int(row.get("port"))
            except (TypeError, ValueError):
                continue
            health = entry_health(row)
            if me is not None:
                current = row.get("id") == me
            else:
                current = port == mine
            # The instance serving this payload is running by
            # construction: a slow self-probe must never render this
            # row (or the header switcher) as stopped.
            running = health["running"] or bool(current)
            entries.append({
                "id": row.get("id"),
                "name": row.get("name") if isinstance(row.get("name"), str) else None,
                "port": port,
                "running": running,
                "current": bool(current),
            })
        payload = {"projects": entries, "current": me}
    _CACHE_AT = moment
    _CACHE_KEY = key
    _CACHE_VAL = json.loads(json.dumps(payload))
    return payload


def project_env(entry: dict) -> dict:
    """Env that points every dashboard path at this project's instance."""
    root = str(entry.get("data_root"))
    return {
        DATA_ROOT_ENV: root,
        STATE_ENV: root,
        PORT_ENV: str(int(entry.get("port"))),
        PROJECT_ID_ENV: str(entry.get("id")),
    }


def project_url(entry: dict) -> str:
    """``http://127.0.0.1:<port>/`` for a registry row."""
    return f"http://127.0.0.1:{int(entry.get('port'))}/"


def _agent_dash() -> Path:
    return Path(__file__).resolve().parents[1] / "agent_dash.py"


def _run_agent_dash(args: list[str], env: dict, timeout_s: float = 30.0):
    """Run one ``agent_dash.py`` child with extra env. Returns CompletedProcess."""
    merged = dict(os.environ)
    merged.update(env)
    return subprocess.run(
        [sys.executable, str(_agent_dash()), *args],
        capture_output=True, text=True, timeout=timeout_s, env=merged,
    )


# Starter seams: reassigned by tests so launch/stop/open never spawn
# real servers or browsers. Each takes the registry entry (starter and
# stopper) or the URL (opener) and returns an exit code.
def _default_starter(entry: dict) -> int:
    try:
        proc = _run_agent_dash(["run"], project_env(entry))
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return 0 if proc.returncode == 0 else 1


def _default_stopper(entry: dict) -> tuple[int, str]:
    try:
        proc = _run_agent_dash(["stop"], project_env(entry))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"stop failed for {entry.get('id')}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _default_opener(url: str) -> int:
    try:
        proc = subprocess.run(["open", url], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return 0 if proc.returncode == 0 else 1


STARTER = _default_starter
STOPPER = _default_stopper
OPENER = _default_opener


def _wait_healthy(entry: dict, timeout_s: float = 20.0) -> dict | None:
    """Poll until this entry's port serves its own state hash, else None."""
    expected = expected_state_hash(entry)
    try:
        port = int(entry.get("port"))
    except (TypeError, ValueError):
        return None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        probe = probe_health(port, timeout_s=PROBE_TIMEOUT_S)
        if isinstance(probe, dict) and probe.get("state_hash") == expected:
            return probe
        time.sleep(0.25)
    return None


def _invalid_registry() -> int:
    registry, path = load_registry()
    if isinstance(registry, dict) and registry.get("invalid"):
        print(
            f"projects registry at {path} is not valid JSON "
            '({"projects": [...]}). Fix the file, then run: '
            "python3 agent_dash.py project list",
            file=sys.stderr,
        )
        return errors.INVALID_DATA
    return errors.OK


def cmd_add(argv: list[str]) -> int:
    """Register a project: ``add <id> --data-root DIR [--name N] [--port P] [--init] [--json]`."""
    pid: str | None = None
    data_root: str | None = None
    name: str | None = None
    port_raw: str | None = None
    init = False
    json_mode = False
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token == "--data-root" and rest:
            data_root = rest.pop(0)
        elif token == "--name" and rest:
            name = rest.pop(0)
        elif token == "--port" and rest:
            port_raw = rest.pop(0)
        elif token == "--init":
            init = True
        elif token == "--json":
            json_mode = True
        elif token.startswith("-") or pid is not None:
            print(PROJECTS_USAGE, end="", file=sys.stderr)
            return errors.USAGE
        else:
            pid = token
    if pid is None or not _ID.match(pid):
        print(
            f"project add needs an id like 'demo' (letters, digits, _ . -, got {pid!r}). "
            "Run: python3 agent_dash.py project add demo --data-root DIR",
            file=sys.stderr,
        )
        return errors.USAGE
    if not data_root or not data_root.strip():
        print(
            "project add needs --data-root DIR. "
            "Run: python3 agent_dash.py project add demo --data-root DIR",
            file=sys.stderr,
        )
        return errors.USAGE
    bad = _invalid_registry()
    if bad:
        return bad
    registry, _path = load_registry()
    if find_entry(registry, pid) is not None:
        print(
            f"project id {pid!r} is already registered. "
            "Run: python3 agent_dash.py project list",
            file=sys.stderr,
        )
        return errors.USAGE
    from dash.locate import expand

    root = Path(expand(data_root.strip()))
    if port_raw is not None:
        try:
            port = int(port_raw)
        except ValueError:
            port = -1
        if not 1 <= port <= 65535:
            print(
                f"bad port {port_raw!r}. Use an integer 1-65535. "
                "Run: python3 agent_dash.py project add demo --data-root DIR --port 8765",
                file=sys.stderr,
            )
            return errors.USAGE
    else:
        port = default_port(registry)
    for row in registry.get("projects", []):
        try:
            taken = int(row.get("port"))
        except (TypeError, ValueError):
            continue
        if taken == port:
            print(
                f"port {port} is already registered by project {row.get('id')!r}. "
                "Run: python3 agent_dash.py project list",
                file=sys.stderr,
            )
            return errors.USAGE
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / "workstreams").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"project add failed for {root}: {exc}. Run: python3 agent_dash.py project list", file=sys.stderr)
        return errors.INVALID_DATA
    if init and not (root / "project.json").is_file():
        from dash import project as project_mod

        code = project_mod.cmd_init((name or pid).strip(), root)
        if code != errors.OK:
            return code
    entry = {
        "id": pid,
        "name": (name.strip() if isinstance(name, str) and name.strip() else pid),
        "data_root": str(root),
        "port": port,
        "added_at": int(time.time()),
    }
    registry.setdefault("projects", []).append(entry)
    save_registry(registry)
    clear_cache()
    if json_mode:
        errors.emit_json({"id": pid, "name": entry["name"],
                          "data_root": Path(entry["data_root"]).name, "port": port})
    else:
        print(f"added project {pid} ({entry['name']}) on port {port} at {root}")
        print("Run: python3 agent_dash.py project launch " + pid)
    return errors.OK


def cmd_list(argv: list[str]) -> int:
    """List registry projects with a health probe per row."""
    json_mode = False
    for token in argv or []:
        if token == "--json":
            json_mode = True
        else:
            print(PROJECTS_USAGE, end="", file=sys.stderr)
            return errors.USAGE
    bad = _invalid_registry()
    if bad:
        return bad
    registry, _path = load_registry()
    rows = [row for row in registry.get("projects", []) if isinstance(row, dict)]
    if json_mode:
        items = []
        for row in rows:
            try:
                port = int(row.get("port"))
            except (TypeError, ValueError):
                continue
            health = entry_health(row)
            items.append({
                "id": row.get("id"),
                "name": row.get("name"),
                "data_root": Path(str(row.get("data_root"))).name,
                "port": port,
                "running": health["running"],
                "port_taken": health["port_taken"],
                "build_id": health["build_id"],
            })
        errors.emit_json({"projects": items})
        return errors.OK
    if not rows:
        print("no projects registered. Run: python3 agent_dash.py project add demo --data-root DIR")
        return errors.OK
    width = max([len(str(row.get("id", ""))) for row in rows] + [2])
    print(f"{'id':<{width}}  {'name':<20}  {'port':>5}  status  data_root")
    for row in rows:
        try:
            port = int(row.get("port"))
        except (TypeError, ValueError):
            continue
        health = entry_health(row)
        if health["running"]:
            status = f"running (build {health['build_id'] or 'unknown'})"
        elif health["port_taken"]:
            status = "port taken by another"
        else:
            status = "stopped"
        print(f"{str(row.get('id')):<{width}}  {str(row.get('name') or '-'):<20}  {port:>5}  {status}  {row.get('data_root')}")
    return errors.OK


def cmd_launch(argv: list[str]) -> int:
    """Start one project's instance in the background and print its URL."""
    json_mode = False
    pid: str | None = None
    for token in argv or []:
        if token == "--json":
            json_mode = True
        elif token.startswith("-") or pid is not None:
            print(PROJECTS_USAGE, end="", file=sys.stderr)
            return errors.USAGE
        else:
            pid = token
    if pid is None:
        print("project launch needs an id. Run: python3 agent_dash.py project list", file=sys.stderr)
        return errors.USAGE
    bad = _invalid_registry()
    if bad:
        return bad
    registry, _path = load_registry()
    entry = find_entry(registry, pid)
    if entry is None:
        print(f"project not found: {pid}. Run: python3 agent_dash.py project list", file=sys.stderr)
        return errors.NOT_FOUND
    try:
        port = int(entry.get("port"))
    except (TypeError, ValueError):
        print(f"project {pid!r} has a bad port. Run: python3 agent_dash.py project list", file=sys.stderr)
        return errors.INVALID_DATA
    probe = probe_health(port, timeout_s=PROBE_TIMEOUT_S)
    if isinstance(probe, dict) and probe.get("state_hash") != expected_state_hash(entry):
        print(
            f"launch refused: 127.0.0.1:{port} already serves another project "
            f"(state {probe.get('state_hash') or 'unknown'}, not {expected_state_hash(entry)}). "
            "Run: python3 agent_dash.py project list",
            file=sys.stderr,
        )
        return errors.CHECK_FAILED
    if STARTER(entry) != 0:
        print(f"launch failed for {pid}: the starter exited nonzero. Run: python3 agent_dash.py project list", file=sys.stderr)
        return errors.CHECK_FAILED
    healthy = _wait_healthy(entry)
    if healthy is None:
        print(
            f"launch failed for {pid}: no healthy dashboard on 127.0.0.1:{port} "
            f"(state {expected_state_hash(entry)}). Run: python3 agent_dash.py project list",
            file=sys.stderr,
        )
        return errors.CHECK_FAILED
    url = project_url(entry)
    if json_mode:
        errors.emit_json({"id": pid, "url": url, "port": port, "running": True})
    else:
        print(f"running\nurl {url}")
    return errors.OK


def cmd_stop(argv: list[str]) -> int:
    """Stop one project's own dashboard via the guarded stop."""
    json_mode = False
    pid: str | None = None
    for token in argv or []:
        if token == "--json":
            json_mode = True
        elif token.startswith("-") or pid is not None:
            print(PROJECTS_USAGE, end="", file=sys.stderr)
            return errors.USAGE
        else:
            pid = token
    if pid is None:
        print("project stop needs an id. Run: python3 agent_dash.py project list", file=sys.stderr)
        return errors.USAGE
    bad = _invalid_registry()
    if bad:
        return bad
    registry, _path = load_registry()
    entry = find_entry(registry, pid)
    if entry is None:
        print(f"project not found: {pid}. Run: python3 agent_dash.py project list", file=sys.stderr)
        return errors.NOT_FOUND
    code, output = STOPPER(entry)
    if json_mode:
        errors.emit_human(output.strip() or ("stopped " + pid), True)
        errors.emit_json({"id": pid, "stopped": code == 0})
    elif output.strip():
        print(output.strip())
    return code


def cmd_open(argv: list[str]) -> int:
    """Open one project's URL with macOS ``open`` (read-only)."""
    json_mode = False
    pid: str | None = None
    for token in argv or []:
        if token == "--json":
            json_mode = True
        elif token.startswith("-") or pid is not None:
            print(PROJECTS_USAGE, end="", file=sys.stderr)
            return errors.USAGE
        else:
            pid = token
    if pid is None:
        print("project open needs an id. Run: python3 agent_dash.py project list", file=sys.stderr)
        return errors.USAGE
    bad = _invalid_registry()
    if bad:
        return bad
    registry, _path = load_registry()
    entry = find_entry(registry, pid)
    if entry is None:
        print(f"project not found: {pid}. Run: python3 agent_dash.py project list", file=sys.stderr)
        return errors.NOT_FOUND
    url = project_url(entry)
    if OPENER(url) != 0:
        print(f"open failed for {url}. Open it by hand: {url}", file=sys.stderr)
        return errors.CHECK_FAILED
    if json_mode:
        errors.emit_json({"id": pid, "url": url})
    else:
        print(url)
    return errors.OK


def cmd_remove(argv: list[str]) -> int:
    """Unregister a project. Registry only, never touches data."""
    json_mode = False
    pid: str | None = None
    for token in argv or []:
        if token == "--json":
            json_mode = True
        elif token.startswith("-") or pid is not None:
            print(PROJECTS_USAGE, end="", file=sys.stderr)
            return errors.USAGE
        else:
            pid = token
    if pid is None:
        print("project remove needs an id. Run: python3 agent_dash.py project list", file=sys.stderr)
        return errors.USAGE
    bad = _invalid_registry()
    if bad:
        return bad
    registry, _path = load_registry()
    entry = find_entry(registry, pid)
    if entry is None:
        print(f"project not found: {pid}. Run: python3 agent_dash.py project list", file=sys.stderr)
        return errors.NOT_FOUND
    registry["projects"] = [row for row in registry.get("projects", []) if row.get("id") != pid]
    save_registry(registry)
    clear_cache()
    if json_mode:
        errors.emit_json({"id": pid, "removed": True})
    else:
        print(f"removed project {pid} (data left alone at {entry.get('data_root')})")
    return errors.OK


def cmd_projects(argv: list[str]) -> int:
    """Registry subcommands: add, list, launch, stop, open, remove."""
    args = list(argv or [])
    if not args or args[0] in ("-h", "--help"):
        print(PROJECTS_USAGE, end="")
        return errors.OK if args and args[0] in ("-h", "--help") else errors.USAGE
    cmd, rest = args[0], args[1:]
    if cmd == "add":
        return cmd_add(rest)
    if cmd == "list":
        return cmd_list(rest)
    if cmd == "launch":
        return cmd_launch(rest)
    if cmd == "stop":
        return cmd_stop(rest)
    if cmd == "open":
        return cmd_open(rest)
    if cmd == "remove":
        return cmd_remove(rest)
    print(PROJECTS_USAGE, end="", file=sys.stderr)
    return errors.USAGE


def resolve_project(pid: str) -> tuple[dict | None, str | None]:
    """``(entry, None)`` for a registered id, else ``(None, message)``."""
    bad = _invalid_registry()
    if bad:
        return None, "projects registry is not valid JSON. Run: python3 agent_dash.py project list"
    registry, _path = load_registry()
    entry = find_entry(registry, pid)
    if entry is None:
        return None, f"project not found: {pid}. Run: python3 agent_dash.py project list"
    return entry, None


def apply_project_env(pid: str) -> tuple[dict | None, str | None]:
    """Point this call at one project's data root, state dir, and port.

    Agents working on a project pass ``--project <id>`` (or set the env
    directly). Returns ``(entry, None)`` on success and applies
    ``AGENT_DASH_DATA_ROOT``/``AGENT_DASH_STATE_DIR``/``AGENT_DASH_PORT``/
    ``AGENT_DASH_URL`` for the call.
    """
    entry, error = resolve_project(pid)
    if entry is None:
        return None, error
    os.environ.update(project_env(entry))
    os.environ[URL_ENV] = project_url(entry)
    return entry, None
