#!/usr/bin/env python3
"""agent-dash CLI: run, stop, status, backfill, reprice, reconcile, ws."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from dash.bundles import cmd_ws
from dash.locate import expand
from dash.pricing import reprice as reprice_usage
from dash.server import DashServer
from dash.sources import Registry
from dash.sources.process import reconcile_running
from dash.store import DEFAULT_LONG_RUNNING_S, Store

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".local" / "share" / "agent-dash"
PIDFILE = STATE_DIR / "agent-dash.pid"
LOGFILE = STATE_DIR / "agent-dash.log"
DBFILE = STATE_DIR / "dash.sqlite"
HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}/"

USAGE = """agent-dash: local read-only execution dashboard

  python3 agent_dash.py run
  python3 agent_dash.py stop
  python3 agent_dash.py status
  python3 agent_dash.py backfill --session <id>
  python3 agent_dash.py reprice
  python3 agent_dash.py reconcile
  python3 agent_dash.py ws list
  python3 agent_dash.py ws new <ID> "<title>"
  python3 agent_dash.py ws validate <ID>
  python3 agent_dash.py ws render <ID>
  python3 agent_dash.py ws task <ID> <task-id> <status> [--moved-to WSn] [--note "..."]
"""


def default_cfg() -> dict:
    home = str(Path.home())
    per_file = 4 * 1024 * 1024
    per_tick = 8 * 1024 * 1024
    return {
        "host": HOST,
        "port": PORT,
        "read_budget_per_file": per_file,
        "read_budget_per_tick": per_tick,
        "read_budget": {"per_file": per_file, "per_tick": per_tick},
        "long_running_s": dict(DEFAULT_LONG_RUNNING_S),
        "roots": {
            "claude_projects": os.path.join(home, ".claude", "projects"),
            "claude_sessions": os.path.join(home, ".claude", "sessions"),
            "muse_sessions": os.path.join(home, ".local", "share", "muse", "sessions"),
            "grok_sessions": os.path.join(home, ".grok", "sessions"),
            "grok_log": os.path.join(home, ".grok", "logs", "unified.jsonl"),
            "cursor_home": os.path.join(home, ".cursor"),
            "scratchpads": [],
        },
        "hub_dir": "",
        "issues_dir": "",
        "workstreams_tsv": "",
        "state_dir": str(STATE_DIR),
        "backfill": False,
        "backfill_session": None,
    }


def load_cfg(path: Path | None = None) -> dict:
    cfg = default_cfg()
    chosen = path
    if chosen is None and os.environ.get("AGENT_DASH_CONFIG"):
        chosen = Path(expand(os.environ["AGENT_DASH_CONFIG"]))
    if chosen is not None:
        _merge_file(cfg, Path(chosen))
    else:
        _merge_file(cfg, ROOT / "config.example.json")
        _merge_file(cfg, ROOT / "config.local.json")
    _apply_env(cfg)
    _expand_cfg(cfg)
    cfg.pop("caps", None)
    cfg["host"] = HOST
    cfg["port"] = PORT
    budget = cfg.get("read_budget") if isinstance(cfg.get("read_budget"), dict) else {}
    cfg["read_budget"] = {
        "per_file": int(budget.get("per_file", cfg.get("read_budget_per_file") or 4 * 1024 * 1024)),
        "per_tick": int(budget.get("per_tick", cfg.get("read_budget_per_tick") or 8 * 1024 * 1024)),
    }
    cfg["read_budget_per_file"] = cfg["read_budget"]["per_file"]
    cfg["read_budget_per_tick"] = cfg["read_budget"]["per_tick"]
    return cfg


def bind_state(cfg: dict | None = None) -> dict:
    global STATE_DIR, PIDFILE, LOGFILE, DBFILE
    cfg = load_cfg() if cfg is None else cfg
    state = Path(expand(str(cfg.get("state_dir") or STATE_DIR)))
    STATE_DIR = state
    PIDFILE = STATE_DIR / "agent-dash.pid"
    LOGFILE = STATE_DIR / "agent-dash.log"
    DBFILE = STATE_DIR / "dash.sqlite"
    return cfg


def _merge_file(cfg: dict, file: Path) -> None:
    if not file.is_file():
        return
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(data, dict):
        _deep_merge(cfg, data)


def _deep_merge(base: dict, over: dict) -> None:
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _apply_env(cfg: dict) -> None:
    roots = cfg.setdefault("roots", {})
    if not isinstance(roots, dict):
        roots = {}
        cfg["roots"] = roots
    named = {
        "AGENT_DASH_CLAUDE_PROJECTS": "claude_projects",
        "AGENT_DASH_CLAUDE_SESSIONS": "claude_sessions",
        "AGENT_DASH_MUSE_SESSIONS": "muse_sessions",
        "AGENT_DASH_GROK_SESSIONS": "grok_sessions",
        "AGENT_DASH_GROK_LOG": "grok_log",
        "AGENT_DASH_CURSOR_HOME": "cursor_home",
    }
    for env, key in named.items():
        if os.environ.get(env):
            roots[key] = os.environ[env]
    globs = os.environ.get("AGENT_DASH_SCRATCH_GLOBS")
    if globs:
        roots["scratchpads"] = [part for part in globs.split(os.pathsep) if part]
    for env, key in (
        ("AGENT_DASH_HUB_DIR", "hub_dir"),
        ("AGENT_DASH_ISSUES_DIR", "issues_dir"),
        ("AGENT_DASH_WORKSTREAMS_TSV", "workstreams_tsv"),
        ("AGENT_DASH_STATE_DIR", "state_dir"),
    ):
        if os.environ.get(env):
            cfg[key] = os.environ[env]


def _expand_cfg(cfg: dict) -> None:
    roots = cfg.get("roots")
    if isinstance(roots, dict):
        for key, value in list(roots.items()):
            roots[key] = _expand_value(value)
    for key in ("hub_dir", "issues_dir", "workstreams_tsv", "state_dir"):
        if isinstance(cfg.get(key), str):
            cfg[key] = expand(cfg[key])


def _expand_value(value):
    if isinstance(value, str):
        return expand(value)
    if isinstance(value, list):
        return [_expand_value(item) for item in value]
    return value


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0
    cmd = args[0] if args else "run"
    if cmd == "run":
        return cmd_run()
    if cmd == "stop":
        return cmd_stop()
    if cmd == "status":
        return cmd_status()
    if cmd == "ws":
        return cmd_ws(args[1:])
    if cmd == "backfill":
        session = _session_arg(args[1:])
        if not session:
            print("backfill requires --session <id>", file=sys.stderr)
            return 2
        return cmd_backfill(session)
    if cmd == "reprice":
        return cmd_reprice()
    if cmd == "reconcile":
        return cmd_reconcile()
    print(USAGE, end="", file=sys.stderr)
    return 2


def cmd_run() -> int:
    bind_state()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    pid = live_pid()
    if pid:
        _print_running(pid)
        return 0
    try:
        os.nice(10)
    except OSError:
        pass
    child = os.fork()
    if child > 0:
        return _wait_for_child()
    os.setsid()
    try:
        _redirect_stdio()
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        _serve()
    except Exception:
        logging.exception("agent-dash exited")
        os._exit(1)
    os._exit(0)


def cmd_stop() -> int:
    bind_state()
    pid = read_pid()
    if not pid or not pid_exists(pid) or not is_dash(pid):
        PIDFILE.unlink(missing_ok=True)
        print("stopped")
        return 1
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not pid_exists(pid):
            PIDFILE.unlink(missing_ok=True)
            print("stopped")
            return 0
        time.sleep(0.1)
    print("stop signal sent; process still running")
    return 1


def cmd_status() -> int:
    bind_state()
    pid = live_pid()
    if not pid:
        print("stopped")
        return 1
    _print_running(pid)
    return 0


def cmd_backfill(session_id: str) -> int:
    try:
        os.nice(10)
    except OSError:
        pass
    cfg = bind_state()
    store = Store(DBFILE)
    try:
        passes = run_backfill(store, Registry(), cfg, session_id)
    finally:
        store.close()
    print(f"backfill session {session_id} passes {passes}")
    return 0


def cmd_reprice() -> int:
    cfg = bind_state()
    store = Store(DBFILE)
    try:
        summary = reprice_usage(store, _claude_projects(cfg))
    finally:
        store.close()
    print(json.dumps(summary, indent=2))
    return 0


def _claude_projects(cfg: dict) -> str:
    roots = cfg.get("roots") if isinstance(cfg.get("roots"), dict) else {}
    for key in ("claude_projects", "projects"):
        if roots.get(key):
            return expand(str(roots[key]))
        if cfg.get(key):
            return expand(str(cfg[key]))
    return str(Path.home() / ".claude" / "projects")


def cmd_reconcile() -> int:
    """Mark grok, muse, and glm rows whose process is gone.

    ``AGENT_DASH_DB`` selects the database. The default is the configured state dir.
    """
    bind_state()
    db = os.environ.get("AGENT_DASH_DB")
    path = Path(expand(db)) if db else DBFILE
    if not path.is_file():
        print(f"no database {path}", file=sys.stderr)
        return 2
    store = Store(path)
    try:
        ended = reconcile_running(store, time.time())
    finally:
        store.close()
    print(f"reconcile ended {ended}")
    return 0


def run_backfill(store, registry, cfg: dict, session_id: str, max_passes: int = 80) -> int:
    cfg = dict(cfg)
    cfg["backfill"] = True
    cfg["backfill_session"] = session_id
    idle = 0
    passes = 0
    for _ in range(max_passes):
        before = store.data_seq
        registry.tick(store, cfg, time.time(), force=True)
        passes += 1
        if store.data_seq == before:
            idle += 1
            if idle >= 2:
                break
        else:
            idle = 0
    return passes


def live_pid() -> int | None:
    pid = read_pid()
    if pid and pid_exists(pid) and is_dash(pid):
        return pid
    return None


def read_pid() -> int | None:
    try:
        return int(PIDFILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def is_dash(pid: int) -> bool:
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = proc.stdout or ""
    return "agent_dash.py" in text


def _serve() -> None:
    cfg = load_cfg()
    store = Store(DBFILE)
    registry = Registry()
    server = DashServer(store, registry, HOST, PORT, cfg=cfg)
    tmp = PIDFILE.with_suffix(".tmp")
    tmp.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    os.replace(tmp, PIDFILE)
    stop = threading.Event()

    def on_stop(signum, frame):
        del signum, frame
        stop.set()
        server.stop.set()

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)

    def loop():
        while not stop.wait(0.25):
            try:
                registry.tick(store, cfg, time.time(), stop=stop)
            except Exception:
                logging.exception("collector tick failed")
        server.stop.set()
        server.shutdown()

    thread = threading.Thread(target=loop, name="collector", daemon=True)
    thread.start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stop.set()
        server.stop.set()
        server.server_close()
        store.close()
        PIDFILE.unlink(missing_ok=True)


def _wait_for_child() -> int:
    deadline = time.time() + 3
    while time.time() < deadline:
        pid = live_pid()
        if pid:
            _print_running(pid)
            return 0
        time.sleep(0.05)
    print("failed to start", file=sys.stderr)
    return 1


def _redirect_stdio() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = os.open(LOGFILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.dup2(log, 1)
    os.dup2(log, 2)
    os.close(devnull)
    os.close(log)


def _print_running(pid: int) -> None:
    print(f"running\npid {pid}\nurl {URL}")


def _session_arg(args: list[str]) -> str | None:
    if "--session" not in args:
        return None
    index = args.index("--session")
    if index + 1 >= len(args):
        return None
    return args[index + 1]


if __name__ == "__main__":
    sys.exit(main())
