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

from dash import errors
from dash.bundles import cmd_ws
from dash.locate import expand, ignore_local
from dash.pricing import reprice as reprice_usage
from dash.server import DashServer
from dash.sources import Registry
from dash.sources.generic import FILE_NAME as GENERIC_FILE
from dash.sources.generic import default_run_id as generic_run_id
from dash.sources.generic import tier_of as generic_tier_of
from dash.sources.process import reconcile_running
from dash.store import Store

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".local" / "share" / "agent-dash"
PIDFILE = STATE_DIR / "agent-dash.pid"
LOGFILE = STATE_DIR / "agent-dash.log"
DBFILE = STATE_DIR / "dash.sqlite"
HOST = "127.0.0.1"
PORT = 8765
RECONCILE_INTERVAL_S = 60
PRUNE_INTERVAL_S = 60
BACKUP_KEEP = 3
URL = f"http://{HOST}:{PORT}/"

USAGE = """agent-dash: local read-only execution dashboard

  python3 agent_dash.py run
  python3 agent_dash.py stop
  python3 agent_dash.py restart
  python3 agent_dash.py status
  python3 agent_dash.py backfill --session <id>
  python3 agent_dash.py reprice
  python3 agent_dash.py reconcile
  python3 agent_dash.py check [--url URL] [--hours N] [--json]
  python3 agent_dash.py check --page [--url URL] [--json]
  python3 agent_dash.py state [--url URL] [--json]
  python3 agent_dash.py status [--json]
  python3 agent_dash.py doctor [--url URL] [--json]
  python3 agent_dash.py report start --label <label> [--model M] [--session S] [--run-id R] [--tier T]
  python3 agent_dash.py report finish --label <label> --verdict PASS [--session S] [--run-id R] [--status done]
  python3 agent_dash.py report usage --label <label> --input N --output N [--model M] [--session S] [--run-id R]
  python3 agent_dash.py ws list
  python3 agent_dash.py ws new <ID> "<title>"
  python3 agent_dash.py ws validate <ID> [--json]
  python3 agent_dash.py ws render <ID>
  python3 agent_dash.py ws plan <ID> [--json]
  python3 agent_dash.py ws task <ID> <task-id> <status> [--moved-to WSn] [--note "..."] [--force]
  python3 agent_dash.py ws train <ID> <train-id> [--title T] [--branch B] [--status S] [--add id,id] [--remove id,id] [--pr N] [--shared f1,f2] [--gate "..."] [--note "..."]
  python3 agent_dash.py ws trains <ID> [--json]

Exit codes: 0 ok, 1 check failed (mismatches found, or doctor found
problems), 2 usage error, 3 not found, 4 invalid data, 5 dashboard
unreachable. `--json` prints machine JSON to stdout; human text goes
to stderr when `--json` is set.
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
        "poll_budget_s": 25.0,
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
        if not ignore_local():
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
    if cmd == "restart":
        return cmd_restart()
    if cmd == "status":
        return cmd_status(args[1:])
    if cmd == "state":
        return cmd_state(args[1:])
    if cmd == "doctor":
        return cmd_doctor(args[1:])
    if cmd == "ws":
        return cmd_ws(args[1:])
    if cmd == "backfill":
        session = _session_arg(args[1:])
        if not session:
            print("backfill requires --session <id>. Run: python3 agent_dash.py backfill --session <id>", file=sys.stderr)
            return 2
        return cmd_backfill(session)
    if cmd == "reprice":
        return cmd_reprice()
    if cmd == "reconcile":
        return cmd_reconcile()
    if cmd == "check":
        return cmd_check(args[1:])
    if cmd == "report":
        return cmd_report(args[1:])
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
    """Stop the dashboard and return 0 only once it is really down.

    SIGTERM first, then up to 10 s of polling for the exit. A process
    that is still alive after that gets SIGKILL, but only when it is still
    the pidfile pid and still the dashboard (a reused pid is never
    signaled). The pidfile is removed and 0 is returned only once the
    port is free, so an immediate ``run`` can bind.
    """
    bind_state()
    pid = read_pid()
    if not pid or _gone(pid) or not is_dash(pid):
        PIDFILE.unlink(missing_ok=True)
        print("stopped")
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + 10
    while time.time() < deadline:
        if _gone(pid):
            break
        time.sleep(0.2)
    if not _gone(pid) and is_dash(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        deadline = time.time() + 2
        while time.time() < deadline:
            if _gone(pid):
                break
            time.sleep(0.1)
    if not _gone(pid) and is_dash(pid):
        print("stop signal sent; process still running")
        return 1
    PIDFILE.unlink(missing_ok=True)
    if not _port_free():
        print("stopped (port still bound)")
        return 1
    print("stopped")
    return 0


def _gone(pid: int) -> bool:
    """True when the pid has exited, reaping our own child first.

    An exited child that has not been waited on still answers ``kill``
    (a zombie); ``waitpid`` with ``WNOHANG`` reaps it so the stop poll
    sees the exit. Non-children fall through to the ``kill`` probe.
    """
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return True
    except (ChildProcessError, InterruptedError, OSError):
        pass
    return not pid_exists(pid)


def _port_free() -> bool:
    """True when nothing listens on the dashboard address anymore."""
    import socket

    sock = socket.socket()
    sock.settimeout(1)
    try:
        sock.connect((HOST, PORT))
    except OSError:
        return True
    finally:
        sock.close()
    return False


def cmd_restart() -> int:
    """Stop, then run again, and wait for /api/health to answer.

    The restarted server backgrounds itself and writes the pidfile like
    ``run`` does. Returns 0 only once health answers ``ok``.
    """
    cmd_stop()
    if live_pid():
        print("restart failed: old server still running", file=sys.stderr)
        return 1
    if cmd_run() != 0:
        return 1
    deadline = time.time() + 20
    while time.time() < deadline:
        if _health_ok():
            print("restarted")
            return 0
        time.sleep(0.25)
    print("restarted but health check failed", file=sys.stderr)
    return 1


def _health_ok() -> bool:
    """True when GET /api/health answers ``{"ok": true}``."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://{HOST}:{PORT}/api/health", timeout=5
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return bool(payload.get("ok"))


def cmd_status(argv: list[str] | None = None) -> int:
    args = list(argv or [])
    json_mode = "--json" in args
    rest = [item for item in args if item != "--json"]
    if rest:
        print(USAGE, end="", file=sys.stderr)
        return 2
    bind_state()
    pid = live_pid()
    if not pid:
        if json_mode:
            errors.emit_json({"running": False, "hint": "Run: python3 agent_dash.py run"})
        else:
            print("stopped. Run: python3 agent_dash.py run")
        return 1
    if json_mode:
        errors.emit_json({"running": True, "pid": pid, "url": URL})
    else:
        _print_running(pid)
    return 0


def _fetch(url: str, path: str, timeout: float = 15):
    """GET ``url + path`` as JSON. Returns ``(payload, None)`` or ``(None, error)``."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")), None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, str(exc)


def _unreachable(url: str, json_mode: bool) -> int:
    message = errors.dashboard_unreachable(url)
    if json_mode:
        errors.emit_json({"error": message, "code": "unreachable", "hint": "Run: python3 agent_dash.py run (or restart)"})
    else:
        print(message, file=sys.stderr)
    return errors.UNREACHABLE


def cmd_state(argv: list[str]) -> int:
    """One call that tells an agent everything: GET /api/state, human or JSON."""
    url = URL
    json_mode = False
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if flag == "--url" and rest:
            url = rest.pop(0).rstrip("/")
        elif flag == "--json":
            json_mode = True
        else:
            print(USAGE, end="", file=sys.stderr)
            return 2
    payload, _error = _fetch(url, "/api/state")
    if payload is None:
        return _unreachable(url, json_mode)
    if json_mode:
        errors.emit_json(payload)
        return 0
    _print_state(payload)
    return 0


def _print_state(payload: dict) -> None:
    version = payload.get("version") or {}
    print(f"state {version.get('tool')} schema {version.get('schema')} (24 h window ending {int(payload.get('now', 0))})")
    health = payload.get("health") or {}
    sources = health.get("sources") or []
    bad = [item.get("name") for item in sources if item.get("last_error")]
    if bad:
        print(f"health: UNHEALTHY ({', '.join(str(name) for name in bad)})")
    else:
        print(f"health: ok ({len(sources)} sources)")
    running = payload.get("running") or {}
    by_source = running.get("by_source") or {}
    breakdown = ", ".join(f"{name}: {count}" for name, count in sorted(by_source.items())) or "-"
    print(f"running: {running.get('count', 0)} ({breakdown})")
    totals = payload.get("totals") or {}
    tokens = totals.get("tokens") or {}
    print(f"tokens 24 h: {sum(int(tokens.get(key) or 0) for key in ('input', 'output', 'cache_read', 'cache_write'))} cost_usd {float(totals.get('cost_usd') or 0.0):.2f}")
    print("workstreams:")
    for row in payload.get("workstreams") or []:
        progress = row.get("progress") or {}
        prog = f" {progress.get('pct', 0)}% ({progress.get('done', 0)}/{progress.get('total', 0)})" if progress else " no plan"
        print(
            f"  {row.get('id')} {row.get('title')} live={row.get('live_agents')} "
            f"tokens={row.get('tokens')} cost={float(row.get('cost_usd') or 0.0):.2f}{prog}"
        )
    unassigned = payload.get("unassigned") or {}
    if unassigned.get("tokens") or unassigned.get("live_agents"):
        print(f"unassigned: live={unassigned.get('live_agents')} tokens={unassigned.get('tokens')}")
    reconcile = payload.get("reconcile") or {}
    mismatches = reconcile.get("mismatches") or []
    if mismatches:
        print(f"reconcile: {len(mismatches)} MISMATCHES (run: python3 agent_dash.py check)")
    else:
        print("reconcile: ok")
    problems = payload.get("problems") or []
    if problems:
        print(f"problems ({len(problems)}):")
        for item in problems:
            print(f"  [{item.get('code')}] {item.get('message')}")
            print(f"    run: {item.get('command')}")


def cmd_doctor(argv: list[str]) -> int:
    """Diagnose setup problems and print the fix. Read-only. Exit 0 clean, 1 problems."""
    from dash.doctor import format_human, run_doctor

    url = URL
    json_mode = False
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if flag == "--url" and rest:
            url = rest.pop(0).rstrip("/")
        elif flag == "--json":
            json_mode = True
        else:
            print(USAGE, end="", file=sys.stderr)
            return 2
    cfg = bind_state()
    result = run_doctor(cfg, url)
    if json_mode:
        errors.emit_json(result)
    else:
        print(format_human(result))
    return 0 if result.get("ok") else 1


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


def cmd_check(argv: list[str]) -> int:
    """Ask a running dashboard for GET /api/reconcile, print the table, exit 1 on any mismatch."""
    url = URL
    hours = 24.0
    page = False
    json_mode = False
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if flag == "--url" and rest:
            url = rest.pop(0).rstrip("/")
        elif flag == "--hours" and rest:
            raw = rest.pop(0)
            try:
                hours = float(raw)
            except ValueError:
                print(f"check needs --hours N with N > 0 (got {raw!r}). Run: python3 agent_dash.py check --hours 24", file=sys.stderr)
                return 2
            if hours <= 0:
                print(f"check needs --hours N with N > 0 (got {raw!r}). Run: python3 agent_dash.py check --hours 24", file=sys.stderr)
                return 2
        elif flag == "--page":
            page = True
        elif flag == "--json":
            json_mode = True
        else:
            print(USAGE, end="", file=sys.stderr)
            return 2
    if page:
        return cmd_page_check(url, json_mode)
    payload, _error = _fetch(url, f"/api/reconcile?hours={hours:g}")
    if payload is None:
        return _unreachable(url, json_mode)
    if json_mode:
        errors.emit_json(payload)
        return 1 if payload.get("mismatches") else 0
    rows = payload.get("rows", [])
    unassigned = payload.get("unassigned") or {}
    everything = list(rows) + ([unassigned] if unassigned else [])
    width = max([len(str(row.get("ws", ""))) for row in everything] + [10])
    head = f"{'workstream':<{width}} {'live':>6} {'fin':>6} {'tokens':>14} {'cost_usd':>12} {'all_time':>14}"
    print(f"reconcile {hours:g}h window (since {int(payload.get('since', 0))} until {int(payload.get('until', 0))})")
    print(head)
    for row in everything:
        mark = "*" if row.get("ws") == "unassigned" else " "
        print(
            f"{mark}{str(row.get('ws', '')):<{width}} "
            f"{int(row.get('live_agents', 0)):>6} "
            f"{int(row.get('finished_in_range', 0)):>6} "
            f"{int(row.get('tokens', 0)):>14} "
            f"{float(row.get('cost_usd', 0.0)):>12.2f} "
            f"{int(row.get('tokens_all_time', 0)):>14}"
        )
    everything_total = sum(int(row.get("tokens", 0)) for row in everything)
    print(f"rows sum tokens {everything_total}")
    all_time = payload.get("all_time") or {}
    if all_time:
        print(
            f"all-time tokens {int(all_time.get('tokens', 0))} "
            f"cost_usd {float(all_time.get('cost_usd', 0.0)):.2f}"
        )
    mismatches = payload.get("mismatches", [])
    if mismatches:
        print("MISMATCHES:")
        for item in mismatches:
            print(f"  - {item}")
        return 1
    print(f"ok: {len(rows)} workstreams reconcile, no mismatches")
    return 0


def cmd_page_check(url: str, json_mode: bool = False) -> int:
    """Drive the live page in Chrome over the DevTools protocol and compare
    every rendered tile and chart legend against the API. Skips cleanly
    when Chrome or Node is missing."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        message = "page check skipped: node is not on PATH. Install Node 18+, then run: python3 agent_dash.py check --page"
        errors.emit_human(message, json_mode)
        if json_mode:
            errors.emit_json({"page_check": {"ok": True, "skipped": "node is not on PATH"}})
        return 0
    script = ROOT / "scripts" / "page_check.mjs"
    if not script.is_file():
        message = f"page check skipped: {script} is missing. Restore scripts/page_check.mjs from version control."
        if json_mode:
            errors.emit_human(message, True)
            errors.emit_json({"page_check": {"ok": False, "error": message}})
        else:
            print(message, file=sys.stderr)
        return 2
    try:
        proc = subprocess.run(
            [node, str(script), "--url", url],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        message = f"page check failed: {exc}"
        if json_mode:
            errors.emit_human(message, True)
            errors.emit_json({"page_check": {"ok": False, "error": message}})
        else:
            print(message, file=sys.stderr)
        return 2
    if proc.stdout:
        errors.emit_human(proc.stdout.rstrip("\n"), json_mode)
    if proc.returncode == 2:
        if proc.stderr:
            errors.emit_human(proc.stderr.rstrip("\n"), json_mode)
        errors.emit_human("page check skipped: Chrome is not available. Set CHROME_PATH, then run: python3 agent_dash.py check --page", json_mode)
        if json_mode:
            errors.emit_json({"page_check": {"ok": True, "skipped": "Chrome is not available"}})
        return 0
    if proc.stderr:
        errors.emit_human(proc.stderr.rstrip("\n"), json_mode)
    if json_mode:
        errors.emit_json({"page_check": {"ok": proc.returncode == 0}})
    return 0 if proc.returncode == 0 else 1


def cmd_report(argv: list[str]) -> int:
    """Append one generic.jsonl line so any harness can report a run.

    start appends a running run line, finish a terminal run line with a
    verdict, usage a token line. The collector picks the file up on its
    next poll. Never stores prompts or secrets: the schema has no field
    for them.
    """
    if not argv or argv[0] not in ("start", "finish", "usage"):
        print("usage: python3 agent_dash.py report start|finish|usage --label <label> ... Run: python3 agent_dash.py report start --label ws4-WS4.1-slug --model <model> --session <id>", file=sys.stderr)
        return 2
    action = argv[0]
    flags = _report_flags(argv[1:])
    label = flags.get("label")
    if not label or len(label) > 200 or "/" in label or "\\" in label or label.startswith("."):
        print("report requires --label <label>, e.g. --label ws4-WS4.1-slug (no /, \\, or leading dot; max 200 chars)", file=sys.stderr)
        return 2
    session = flags.get("session")
    run_id = generic_run_id(label, session, flags.get("run-id"))
    model = flags.get("model")
    tier = flags.get("tier")
    if tier is None and model is not None:
        tier = generic_tier_of(model)
    now = time.time()
    if action == "start":
        line = {
            "type": "run",
            "label": label,
            "run_id": run_id,
            "model": model,
            "tier": tier,
            "session_id": session,
            "status": "running",
            "started_at": now,
        }
    elif action == "finish":
        verdict = flags.get("verdict")
        if not verdict:
            print(f"report finish requires --verdict <VERDICT>. Use one of: {', '.join(errors.VERDICTS)}", file=sys.stderr)
            return 2
        status = flags.get("status") or "done"
        if status not in ("done", "killed", "died_early"):
            print(errors.unknown_finish_status(status), file=sys.stderr)
            return 2
        line = {
            "type": "run",
            "label": label,
            "run_id": run_id,
            "model": model,
            "tier": tier,
            "session_id": session,
            "status": status,
            "verdict": verdict,
            "ended_at": now,
        }
    else:
        try:
            tokens_in = int(flags.get("input", ""))
            tokens_out = int(flags.get("output", ""))
        except ValueError:
            print("report usage requires --input N --output N with N >= 0, e.g. --input 100 --output 50", file=sys.stderr)
            return 2
        if tokens_in < 0 or tokens_out < 0:
            print("report usage requires --input N --output N with N >= 0, e.g. --input 100 --output 50", file=sys.stderr)
            return 2
        line = {
            "type": "usage",
            "session_id": session or run_id,
            "run_id": run_id,
            "model": model,
            "tool": flags.get("tool") or "generic",
            "message_id": flags.get("message-id") or ("usage-%d" % int(now * 1000)),
            "ts": now,
            "input": tokens_in,
            "output": tokens_out,
            "cache_read": _report_int(flags.get("cache-read"), 0),
            "cache_write": _report_int(flags.get("cache-write"), 0),
            "reasoning": _report_int(flags.get("reasoning"), 0),
        }
    cfg = bind_state()
    state = Path(expand(str(cfg.get("state_dir") or STATE_DIR)))
    state.mkdir(parents=True, exist_ok=True)
    if flags.get("run-dir"):
        _register_report_dir(state, flags["run-dir"])
    dest = state / GENERIC_FILE
    try:
        with dest.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, separators=(",", ":"), ensure_ascii=True) + "\n")
    except OSError as exc:
        print(f"report failed: {exc}", file=sys.stderr)
        return 1
    print(f"reported {action} {run_id}")
    return 0


def _register_report_dir(state: Path, run_dir: str) -> None:
    """Register a harness run folder from ``report --run-dir``. Never raises."""
    try:
        from dash.rundirs import register_run_dir

        register_run_dir(state, run_dir)
    except Exception:
        pass


def _report_flags(argv: list[str]) -> dict:
    flags: dict[str, str] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("--") and index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            flags[token[2:]] = argv[index + 1]
            index += 2
        else:
            index += 1
    return flags


def _report_int(value, default: int) -> int:
    try:
        number = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


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


def reconcile_if_due(store, now: float, last: float | None, interval: float = RECONCILE_INTERVAL_S) -> float:
    """Call ``reconcile_running`` when ``interval`` seconds have passed.

    ``last is None`` runs it immediately, so a restart closes rows that were
    left unknown. Returns the timestamp of the last reconcile.
    """
    if last is not None and now - last < interval:
        return last
    reconcile_running(store, now)
    return now


def rotate_backups(state_dir, keep: int = BACKUP_KEEP, dest=None) -> list:
    """Move ``*.bak-*`` files beyond the newest ``keep`` out of the state dir.

    ``dest`` defaults to a ``<state-dir>-retired`` sibling. Returns the
    moved paths as strings. Never raises: failures are logged and skipped.
    """
    log = logging.getLogger("agent_dash.prune")
    try:
        root = Path(state_dir)
        cands = [path for path in root.glob("*.bak-*") if path.is_file()]
    except OSError as exc:
        log.warning("backup rotation: %s", type(exc).__name__)
        return []
    if len(cands) <= keep:
        return []
    try:
        order = sorted(cands, key=lambda path: path.stat().st_mtime)
    except OSError as exc:
        log.warning("backup rotation: %s", type(exc).__name__)
        return []
    if dest is None:
        dest = root.parent / (root.name + "-retired")
    try:
        Path(dest).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("backup rotation: %s", type(exc).__name__)
        return []
    moved = []
    for path in order[: len(order) - keep]:
        try:
            target = Path(dest) / path.name
            os.replace(path, target)
        except OSError as exc:
            log.warning("backup rotation %s: %s", path.name, type(exc).__name__)
            continue
        moved.append(str(target))
    if moved:
        log.info("backup rotation moved %d file(s) to %s", len(moved), dest)
    return moved


def prune_if_due(store, state_dir, now: float, last: float | None, interval: float = PRUNE_INTERVAL_S) -> float:
    """Prune one batch of dead offset rows and rotate backups when due.

    ``last is None`` runs immediately so a restart starts draining on the
    first tick. Returns the timestamp of the last prune.
    """
    if last is not None and now - last < interval:
        return last
    log = logging.getLogger("agent_dash.prune")
    try:
        deleted = store.prune_offsets()
    except Exception:
        log.exception("offset prune failed")
    else:
        if deleted:
            log.info("offset prune deleted %d row(s)", deleted)
    rotate_backups(state_dir)
    return now


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
        last_reconcile = None
        last_prune = None
        while not stop.wait(0.25):
            now = time.time()
            try:
                registry.tick(store, cfg, now, stop=stop)
                last_reconcile = reconcile_if_due(store, now, last_reconcile)
                last_prune = prune_if_due(store, STATE_DIR, now, last_prune)
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
