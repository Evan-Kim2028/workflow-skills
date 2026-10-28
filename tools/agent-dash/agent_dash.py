#!/usr/bin/env python3
"""agent-dash CLI: run, stop, status, backfill, reprice, reconcile, ws."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from dash import dataroot
from dash import errors
from dash import deploy as deploy_lib
from dash.buildinfo import build_info
from dash.bundles import cmd_ws
from dash.locate import expand, ignore_local
from dash.pricing import reprice as reprice_usage
from dash.project import cmd_project
from dash.server import DashServer, state_dir_hash
from dash.sources import Registry
from dash.sources.generic import FILE_NAME as GENERIC_FILE
from dash.sources.generic import default_run_id as generic_run_id
from dash.sources.generic import tier_of as generic_tier_of
from dash.sources.process import reconcile_running
from dash.store import Store
from dash.worktrees import cmd_worktrees

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".local" / "share" / "agent-dash"
PIDFILE = STATE_DIR / "agent-dash.pid"
LOGFILE = STATE_DIR / "agent-dash.log"
DBFILE = STATE_DIR / "dash.sqlite"
HOST = "127.0.0.1"
PORT = 8765
RECONCILE_INTERVAL_S = 60
PRUNE_INTERVAL_S = 60
CHILD_START_S = 20.0
BACKUP_KEEP = 3
URL = f"http://{HOST}:{PORT}/"


def _port() -> int:
    """The dashboard port: ``AGENT_DASH_PORT`` wins for trials, else ``PORT``.

    The live address stays 127.0.0.1:8765; the env override exists so a
    release can be health-checked on another port without touching it.
    Tests assign ``agent_dash.PORT`` directly, which this honors too.
    """
    raw = os.environ.get("AGENT_DASH_PORT")
    if raw:
        try:
            value = int(raw)
        except ValueError:
            pass
        else:
            if 1 <= value <= 65535:
                return value
    return PORT


def _base_url() -> str:
    """The dashboard base URL from the current host and port."""
    return f"http://{HOST}:{_port()}/"

USAGE = """agent-dash: local read-only execution dashboard

  python3 agent_dash.py run [--force-replace] [--yes-live]
  python3 agent_dash.py stop [--yes-live]
  python3 agent_dash.py restart [--yes-live]
  python3 agent_dash.py deploy [--ref origin/main] [--worktree DIR] [--yes-live]
  python3 agent_dash.py rollback
  python3 agent_dash.py status [--json] [--repair]
  python3 agent_dash.py backfill --session <id>
  python3 agent_dash.py reprice [--dry-run]
  python3 agent_dash.py reconcile
  python3 agent_dash.py db stats|rollup [--dry-run] [--days N] [--batch N]
  python3 agent_dash.py runs prune-markers [--dry-run] [--yes]
  python3 agent_dash.py check [--url URL] [--hours N] [--json]
  python3 agent_dash.py check --page [--url URL] [--json]
  python3 agent_dash.py state [--url URL] [--json]
  python3 agent_dash.py status [--json]
  python3 agent_dash.py doctor [--url URL] [--json] [--ws ID] [--severity S] [--all]
  python3 agent_dash.py doctor --fix [--yes]
  python3 agent_dash.py doctor report [--since 24h] [--md]
  python3 agent_dash.py report start --label <label> [--model M] [--session S] [--run-id R] [--tier T]
  python3 agent_dash.py report finish --label <label> --verdict PASS [--session S] [--run-id R] [--status done]
  python3 agent_dash.py report usage --label <label> --input N --output N [--model M] [--session S] [--run-id R]
  python3 agent_dash.py label make --scope ws11 --mark plan --slug scout-10-partition-cold [--round 1]
  python3 agent_dash.py label check <label> [--ws WS11]
  python3 agent_dash.py ws list [--state S[,S...]|all] [--json]
  python3 agent_dash.py ws new <ID> "<title>" [--state planned|active]
  python3 agent_dash.py ws state <ID> <state> [--force] [--note "..."]
  python3 agent_dash.py ws archive <ID> [--force] [--note "..."]
  python3 agent_dash.py ws restore <ID>
  python3 agent_dash.py ws validate <ID> [--json]
  python3 agent_dash.py ws render <ID>
  python3 agent_dash.py ws plan <ID> [--json]
  python3 agent_dash.py ws layout <ID> [--json]
  python3 agent_dash.py ws task <ID> <task-id> <status> [--moved-to WSn] [--note "..."] [--force] [--no-gc]
  python3 agent_dash.py ws gc <ID> [--dry-run] [--json]
  python3 agent_dash.py ws train <ID> <train-id> [--title T] [--branch B] [--status S] [--add id,id] [--remove id,id] [--pr N] [--shared f1,f2] [--gate "..."] [--note "..."]
  python3 agent_dash.py dispatch start|stop|pause|resume|status|wait|once <ID> [...]   keep a workstream's slots full (docs/dispatcher.md)
  python3 agent_dash.py ws trains <ID> [--json]
  python3 agent_dash.py project init <name> [--data-root DIR]
  python3 agent_dash.py project show [--json]
  python3 agent_dash.py project set <field> <value>
  python3 agent_dash.py project add <id> --data-root DIR [--name NAME] [--port N] [--init] [--json]
  python3 agent_dash.py project list [--json]
  python3 agent_dash.py project launch <id> [--json]
  python3 agent_dash.py project stop <id> [--json]
  python3 agent_dash.py project open <id> [--json]
  python3 agent_dash.py project remove <id> [--json]
  python3 agent_dash.py lead-profile <session-id|--ws ID> [--json]
  python3 agent_dash.py worktrees reap --repo <path> [--apply] [--skip <path-or-branch>] [--min-age <dur>] [--json]
  python3 agent_dash.py lanes live [--json]

Agents working on a project pass --project <id> (or set the env) for
state, check, doctor, ws ..., status: it sets the data root, state
dir, and URL/port from the registry for that call.

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
        "data_root": str(STATE_DIR),
        "state_dir": str(STATE_DIR),
        "poll_budget_s": 25.0,
        "backfill": False,
        "backfill_session": None,
        "rollup_after_days": 14,
        "doctor": {
            "interval_s": 300,
            "stuck_hours": 6,
            "approval_days": 3,
            "train_hours": 24,
            "model_policy": {
                "muse_reasoning_effort": "xhigh",
                "grok_require_bracketed_500k": True,
            },
        },
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
            # A release checkout is clean, so its own config.local.json
            # never holds the user's settings: the state dir copy wins,
            # letting the dashboard run from a release unchanged.
            _merge_file(cfg, _state_config_path(cfg))
    _apply_env(cfg)
    _expand_cfg(cfg)
    # The state dir defaults to the data root: pointing AGENT_DASH_DATA_ROOT
    # (or config data_root) at a fresh tree moves the sqlite state with it,
    # while an explicit state dir (env or config) keeps winning.
    if not os.environ.get("AGENT_DASH_STATE_DIR"):
        pristine = str(Path.home() / ".local" / "share" / "agent-dash")
        if cfg.get("state_dir") == pristine and cfg.get("data_root") not in (None, "", pristine):
            cfg["state_dir"] = cfg["data_root"]
    cfg.pop("caps", None)
    cfg["host"] = HOST
    cfg["port"] = _port()
    budget = cfg.get("read_budget") if isinstance(cfg.get("read_budget"), dict) else {}
    cfg["read_budget"] = {
        "per_file": int(budget.get("per_file", cfg.get("read_budget_per_file") or 4 * 1024 * 1024)),
        "per_tick": int(budget.get("per_tick", cfg.get("read_budget_per_tick") or 8 * 1024 * 1024)),
    }
    cfg["read_budget_per_file"] = cfg["read_budget"]["per_file"]
    cfg["read_budget_per_tick"] = cfg["read_budget"]["per_tick"]
    return cfg


def _state_config_path(cfg: dict) -> Path:
    """``<state-dir>/config.local.json``: the release-proof config file.

    The state dir comes from the env override first (trials), then the
    merged config, then the data root. Missing files simply do not merge.
    """
    return dataroot.state_dir(cfg) / "config.local.json"


def bind_state(cfg: dict | None = None) -> dict:
    global STATE_DIR, PIDFILE, LOGFILE, DBFILE
    cfg = load_cfg() if cfg is None else cfg
    state = dataroot.state_dir(cfg)
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
        ("AGENT_DASH_DATA_ROOT", "data_root"),
        ("AGENT_DASH_STATE_DIR", "state_dir"),
    ):
        if os.environ.get(env):
            cfg[key] = os.environ[env]


def _expand_cfg(cfg: dict) -> None:
    roots = cfg.get("roots")
    if isinstance(roots, dict):
        for key, value in list(roots.items()):
            roots[key] = _expand_value(value)
    for key in ("hub_dir", "issues_dir", "workstreams_tsv", "data_root", "state_dir"):
        if isinstance(cfg.get(key), str):
            cfg[key] = expand(cfg[key])


def _expand_value(value):
    if isinstance(value, str):
        return expand(value)
    if isinstance(value, list):
        return [_expand_value(item) for item in value]
    return value


def _parse_port(raw: str) -> int:
    """AGENT_DASH_PORT value as a port number. Raises ValueError when bad."""
    port = int((raw or "").strip())
    if 1 <= port <= 65535:
        return port
    raise ValueError(raw)


def _no_extra_args(cmd: str, args: list[str], allowed: tuple = ()) -> int | None:
    """None when the action runs bare (or with only allowed flags); 0 after --help; 2 after extras."""
    if len(args) <= 1:
        return None
    if len(args) == 2 and args[1] in ("-h", "--help"):
        print(USAGE, end="")
        return 0
    if allowed and all(flag in allowed for flag in args[1:]):
        return None
    print(errors.action_takes_no_args(cmd, args[1]), file=sys.stderr)
    return errors.USAGE


def _extract_project_flag(args: list[str]) -> tuple[list[str], str | None, int | None]:
    """Split a global ``--project <id>`` out of argv.

    Returns ``(cleaned, project_id-or-None, error-code-or-None)``. The
    flag may sit before the command or among its flags; both read the
    same way so agents can append it to any call.
    """
    cleaned: list[str] = []
    pid: str | None = None
    index = 0
    while index < len(args):
        if args[index] == "--project":
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                print(
                    "agent-dash needs --project <id>. Run: python3 agent_dash.py project list",
                    file=sys.stderr,
                )
                return args, None, 2
            if pid is not None:
                print(USAGE, end="", file=sys.stderr)
                return args, None, 2
            pid = args[index + 1]
            index += 2
        else:
            cleaned.append(args[index])
            index += 1
    return cleaned, pid, None


def main(argv: list[str] | None = None) -> int:
    global PORT, URL
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0
    args, proj, proj_err = _extract_project_flag(args)
    if proj_err is not None:
        return proj_err
    # The multi-project flag also reaches the descriptor subcommands
    # (show, set, ignore, init), which read one project's project.json;
    # the registry subcommands (add, list, launch, stop, open, remove)
    # keep their own scope and never take it.
    _descriptor = len(args) > 1 and args[0] == "project" and args[1] in ("show", "set", "ignore", "init")
    if proj is not None and (not args or args[0] != "project" or _descriptor):
        from dash.projects import apply_project_env

        _entry, _error = apply_project_env(proj)
        if _entry is None:
            print(_error, file=sys.stderr)
            return 3
    raw_port = os.environ.get("AGENT_DASH_PORT", "")
    if raw_port.strip():
        try:
            PORT = _parse_port(raw_port)
        except ValueError:
            print(errors.bad_port(raw_port.strip()), file=sys.stderr)
            return errors.USAGE
        URL = f"http://{HOST}:{PORT}/"
        # The ws live-data fetches read AGENT_DASH_URL, not PORT: follow
        # the port unless the caller already points elsewhere.
        os.environ.setdefault("AGENT_DASH_URL", URL)
    cmd = args[0] if args else "run"
    if cmd == "run":
        allowed = ("--force-replace", "--yes-live")
    elif cmd in ("stop", "restart"):
        allowed = ("--yes-live",)
    else:
        allowed = None
    if allowed is not None:
        guard = _no_extra_args(cmd, args, allowed)
        if guard is not None:
            return guard
    if cmd == "run":
        return cmd_run(args[1:])
    if cmd == "serve":
        return cmd_serve(args[1:])
    if cmd == "stop":
        return cmd_stop(args[1:])
    if cmd == "restart":
        return cmd_restart(args[1:])
    if cmd == "deploy":
        return cmd_deploy(args[1:])
    if cmd == "rollback":
        return cmd_rollback(args[1:])
    if cmd == "status":
        return cmd_status(args[1:])
    if cmd == "state":
        return cmd_state(args[1:])
    if cmd == "doctor":
        return cmd_doctor(args[1:])
    if cmd == "ws":
        return cmd_ws(args[1:])
    if cmd == "dispatch":
        from dash.dispatch import cmd_dispatch

        return cmd_dispatch(args[1:])
    if cmd == "project":
        return cmd_project(args[1:])
    if cmd == "backfill":
        session = _session_arg(args[1:])
        if not session:
            print("backfill requires --session <id>. Run: python3 agent_dash.py backfill --session <id>", file=sys.stderr)
            return 2
        return cmd_backfill(session)
    if cmd == "reprice":
        return cmd_reprice(args[1:])
    if cmd == "reconcile":
        return cmd_reconcile()
    if cmd == "db":
        return cmd_db(args[1:])
    if cmd == "runs":
        return cmd_runs(args[1:])
    if cmd == "check":
        return cmd_check(args[1:])
    if cmd == "report":
        return cmd_report(args[1:])
    if cmd == "label":
        return cmd_label(args[1:])
    if cmd == "lead-profile":
        return cmd_lead_profile(args[1:])
    if cmd == "worktrees":
        return cmd_worktrees(args[1:])
    if cmd == "lanes":
        return cmd_lanes(args[1:])
    print(USAGE, end="", file=sys.stderr)
    return 2


def cmd_run(argv: list[str] | None = None) -> int:
    """Start the dashboard unless the port already serves another build.

    A port answering ``/api/health`` with a different ``build_id`` than
    this checkout refuses to start (pass ``--force-replace`` to restart
    it instead). ``--force-replace`` restarts even when our own pidfile
    server is up. The same build answering without a pidfile counts as
    already running. ``--yes-live`` is accepted and forwarded to the
    restart, for the state-dir guard.
    """
    force = False
    yes_live = False
    for flag in argv or []:
        if flag == "--force-replace":
            force = True
        elif flag == "--yes-live":
            yes_live = True
        else:
            print(USAGE, end="", file=sys.stderr)
            return 2
    bind_state()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    pid = live_pid()
    if pid and not force:
        _print_running(pid)
        return 0
    remote = _probe_health()
    if remote is not None:
        mine = build_info()["build_id"]
        theirs = str(remote.get("build_id") or "unknown")
        if theirs != mine:
            if force:
                if yes_live:
                    return cmd_restart(["--yes-live"])
                return cmd_restart()
            print(errors.build_mismatch(_port(), theirs, mine), file=sys.stderr)
            return 1
        _print_running_unknown(remote)
        return 0
    try:
        os.nice(10)
    except OSError:
        pass
    child = _spawn_child()
    return _wait_for_child(child.pid)


def _spawn_child() -> subprocess.Popen:
    """Start the dashboard server as ``agent_dash.py serve`` in its own session.

    A fresh interpreter, not ``os.fork()``: this process has run the
    health probes, whose proxy lookup starts CoreFoundation threads on
    macOS. A child forked from it can die with SIGSEGV before exec, or
    abort on its first CoreFoundation call. stdout and stderr go to the log.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, AGENT_DASH_PORT=str(_port()))
    with open(LOGFILE, "ab") as log:
        return subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "serve"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )


def cmd_serve(argv: list[str] | None = None) -> int:
    """Run the dashboard server in this process; the child ``cmd_run`` spawns.

    Logs a start line, then either a normal return or the exception (any
    ``BaseException``, ``SystemExit`` included), so a failed start always
    leaves a trace in agent-dash.log. Returns the child's exit status.
    """
    if argv:
        print(USAGE, end="", file=sys.stderr)
        return 2
    bind_state()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    status = 1
    try:
        logging.info("agent-dash child %d starting _serve", os.getpid())
        _serve()
        logging.info("agent-dash child %d _serve returned", os.getpid())
        status = 0
    except BaseException:
        logging.exception("agent-dash exited")
    return status


def cmd_stop(argv: list[str] | None = None) -> int:
    """Stop the dashboard and return 0 only once it is really down.

    SIGTERM first, then up to 10 s of polling for the exit. A process
    that is still alive after that gets SIGKILL, but only when it is still
    the pidfile pid and still the dashboard (a reused pid is never
    signaled). The pidfile is removed (only when it still names the
    stopped pid) and 0 is returned only once the port is free, so an
    immediate ``run`` can bind.

    The dashboard is found even without a pidfile: when the configured
    port answers ``/api/health`` and the listener is the dashboard, that
    pid is stopped. A target from another state dir is refused unless
    ``--yes-live`` is passed; with the default state dir, a
    non-interactive session must also pass ``--yes-live``.
    """
    for flag in argv or []:
        if flag != "--yes-live":
            print(USAGE, end="", file=sys.stderr)
            return 2
    yes_live = "--yes-live" in (argv or [])
    bind_state()
    current_hash = state_dir_hash(STATE_DIR)
    pid, listed = _claimed_pid()
    via_pidfile = pid is not None
    health = None
    if pid is None:
        pid, health = _dashboard_on_port()
    if pid is None:
        if listed is not None:
            _release_stale_pidfile(listed)
        print("stopped")
        return 1
    if health is None:
        health = _probe_health()
    health = health if isinstance(health, dict) else {}
    target_hash = current_hash if via_pidfile else health.get("state_hash")
    build = str(health.get("build_id") or "unknown")
    refused = _refuse_to_signal("stop", pid, target_hash, current_hash, yes_live)
    if refused is not None:
        print(refused, file=sys.stderr)
        return 1
    print(f"stopping dashboard pid {pid} on port {_port()} (state {target_hash or 'unknown'}, build {build})")
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
    _release_pidfile(pid)
    if not _port_free():
        print("stopped (port still bound)")
        return 1
    print("stopped")
    return 0


def _refuse_to_signal(action: str, pid: int, target_hash: str | None, current_hash: str, yes_live: bool) -> str | None:
    """None when signaling may proceed, else the refusal message.

    A target from another state dir needs ``--yes-live``. With the
    default state dir (no ``AGENT_DASH_STATE_DIR``), a non-interactive
    session also needs ``--yes-live``: a trial that sets the env can
    never signal the live dashboard by accident, and neither can a
    stray non-interactive call that forgot the env.
    """
    if target_hash != current_hash and not yes_live:
        return errors.foreign_state(action, pid, _port(), target_hash, current_hash)
    if (
        target_hash == current_hash
        and _default_state_dir()
        and not _stdin_is_tty()
        and not yes_live
    ):
        return errors.live_needs_flag(action)
    return None


def _default_state_dir() -> bool:
    """True when the state dir is the live default (no env override)."""
    return os.environ.get("AGENT_DASH_STATE_DIR") is None


def _stdin_is_tty() -> bool:
    """True for an interactive session; False (safe) when unreadable."""
    try:
        return sys.stdin.isatty()
    except (OSError, ValueError):
        return False


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
    """True when nothing holds the dashboard address anymore.

    A refused connection means free. A timeout means something holds
    the bind without accepting (a full backlog): the next ``run``
    could not bind it either, so that counts as still bound.
    """
    import errno
    import socket

    sock = socket.socket()
    sock.settimeout(1)
    try:
        sock.connect((HOST, _port()))
    except OSError as exc:
        if isinstance(exc, socket.timeout) or getattr(exc, "errno", None) == errno.ETIMEDOUT:
            return False
        return True
    finally:
        sock.close()
    return False


def _probe_health(timeout_s: float = 2.0) -> dict | None:
    """GET /api/health as a dict, or None when the port does not answer.

    A bound port serving non-JSON (not the dashboard) also reads as
    None, leaving the bind to fail loudly in the child.
    """
    payload, _error = _fetch(_base_url(), "/api/health", timeout=timeout_s)
    return payload if isinstance(payload, dict) else None


def cmd_deploy(argv: list[str]) -> int:
    """Deploy a clean release of ``--ref`` and switch the dashboard to it.

    Records the previous release first, gates the new one, then
    restarts with a health gate; any failure restarts the previous
    release and exits 1 with the reason. ``--yes-live`` is forwarded
    to the release restarts for the state-dir guard.
    """
    ref = deploy_lib.DEFAULT_REF
    worktree = None
    yes_live = False
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if flag == "--ref" and rest:
            ref = rest.pop(0)
        elif flag == "--worktree" and rest:
            worktree = rest.pop(0)
        elif flag == "--yes-live":
            yes_live = True
        else:
            print(USAGE, end="", file=sys.stderr)
            return 2
    bind_state()
    ok, message = deploy_lib.deploy(
        STATE_DIR,
        deploy_lib.default_repo(ROOT),
        ref=ref,
        worktree=worktree,
        url=_base_url(),
        yes_live=yes_live,
    )
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def cmd_rollback(argv: list[str]) -> int:
    """Switch the dashboard back to the previous release manually."""
    if list(argv or []):
        print(USAGE, end="", file=sys.stderr)
        return 2
    bind_state()
    ok, message = deploy_lib.rollback(STATE_DIR, url=_base_url())
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def preflight_db_open() -> tuple[bool, str]:
    """Open a copy of the live database with this checkout's Store.

    restart/deploy preflight: copies the live database to a temp file
    with the sqlite backup API (read-only on the source, safe against
    concurrent writers), opens it with the current Store — running
    every migration — then closes and deletes the copy. Returns
    ``(True, message)`` or ``(False, reason)``; the caller must not
    stop the live server on ``False``. A missing database is not a
    failure (fresh state dir).
    """
    db = DBFILE
    if not db.is_file():
        return True, "preflight: no database yet; nothing to migrate"
    import sqlite3
    import tempfile

    from dash.store import Store

    tmp: Path | None = None
    try:
        fd, tmppath = tempfile.mkstemp(prefix="adash-preflight-", suffix=".sqlite")
        os.close(fd)
        tmp = Path(tmppath)
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            dst = sqlite3.connect(str(tmp), timeout=10)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        store = Store(tmp)
        try:
            store._conn.execute("SELECT COUNT(*) FROM runs").fetchone()
        finally:
            store.close()
    except Exception as exc:
        return (
            False,
            f"preflight: this build cannot open the live database copy "
            f"({type(exc).__name__}: {exc})",
        )
    finally:
        if tmp is not None:
            for cand in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
                try:
                    if cand.exists():
                        cand.unlink()
                except OSError:
                    pass
    return True, "preflight: live database copy opens with this build"


def cmd_restart(argv: list[str] | None = None) -> int:
    """Stop, then run again, and wait for /api/health to answer.

    The restarted server backgrounds itself and writes the pidfile like
    ``run`` does. Returns 0 only once health answers ``ok``. The old
    server is found by pidfile or, when that is missing or stale, by
    the answering port; a foreign state dir is refused like ``stop``.
    Before signalling anything, the new code opens a copy of the live
    database: a build that cannot migrate it refuses to stop the live
    server. ``deploy`` funnels through here, so it is covered too.
    """
    for flag in argv or []:
        if flag != "--yes-live":
            print(USAGE, end="", file=sys.stderr)
            return 2
    yes_live = "--yes-live" in (argv or [])
    bind_state()
    ok, message = preflight_db_open()
    if not ok:
        print(message, file=sys.stderr)
        print("restart refused: live server left running", file=sys.stderr)
        return 1
    if yes_live:
        cmd_stop(["--yes-live"])
    else:
        cmd_stop()
    if live_pid() is not None or _dashboard_on_port()[0] is not None:
        print("restart failed: old server still running", file=sys.stderr)
        return 1
    if cmd_run() != 0:
        return 1
    deadline = time.time() + 20
    while time.time() < deadline:
        if _health_answers():
            if _health_ok():
                print("restarted")
            else:
                print("restarted (health reports problems; run: python3 agent_dash.py doctor)")
            return 0
        time.sleep(0.25)
    print("restarted but health check failed", file=sys.stderr)
    return 1


def _health_ok() -> bool:
    """True when GET /api/health answers ``{"ok": true}``."""
    payload = _health_payload()
    return bool(payload.get("ok")) if payload is not None else False


def _health_answers() -> bool:
    """True when GET /api/health answers with a payload, whatever it says."""
    return _health_payload() is not None


def _health_payload():
    """The /api/health payload, or None when the dashboard does not answer."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            _base_url() + "api/health", timeout=5
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and "ok" in payload else None


def cmd_status(argv: list[str] | None = None) -> int:
    """Report whether the dashboard runs, finding it without a pidfile.

    When the pidfile is missing or stale but the configured port answers
    ``/api/health`` and the listener is the dashboard, status reports
    ``running (pidfile missing, pid N)``; ``status --repair`` rewrites
    the pidfile for that pid, and only in that case.
    """
    args = list(argv or [])
    json_mode = "--json" in args
    repair = "--repair" in args
    rest = [item for item in args if item not in ("--json", "--repair")]
    if rest:
        print(USAGE, end="", file=sys.stderr)
        return 2
    bind_state()
    pid = live_pid()
    if pid is None:
        found, health = _dashboard_on_port()
        if found is not None:
            return _status_pidfile_less(found, health, json_mode, repair)
    if not pid:
        local = build_info()
        if json_mode:
            errors.emit_json(
                {
                    "running": False,
                    "hint": "Run: python3 agent_dash.py run",
                    "build_id": local["build_id"],
                    "started_at": local["started_at"],
                }
            )
        else:
            print(f"stopped (local build {local['build_id']}). Run: python3 agent_dash.py run")
        return 1
    health = _probe_health()
    if isinstance(health, dict) and health.get("build_id"):
        info = {"build_id": health.get("build_id"), "started_at": health.get("started_at")}
    else:
        local = build_info()
        info = {"build_id": local["build_id"], "started_at": local["started_at"]}
    if json_mode:
        errors.emit_json({"running": True, "pid": pid, "url": _base_url(), **info})
    else:
        _print_running(pid, info)
    return 0


def _status_pidfile_less(pid: int, health: dict, json_mode: bool, repair: bool) -> int:
    """Report a dashboard the pidfile does not name; repair rewrites it."""
    info = {
        "build_id": health.get("build_id") or "unknown",
        "started_at": health.get("started_at"),
    }
    repaired = False
    if repair:
        _write_pidfile(pid)
        repaired = read_pid() == pid
    if json_mode:
        errors.emit_json(
            {
                "running": True,
                "pid": pid,
                "pidfile_missing": True,
                "repaired": repaired,
                "url": _base_url(),
                **info,
            }
        )
    else:
        print(f"running (pidfile missing, pid {pid})")
        print(f"url {_base_url()}")
        print(f"build {info['build_id']}")
        if repair:
            if repaired:
                print(f"pidfile rewritten for pid {pid}")
            else:
                print(f"pidfile repair failed for pid {pid}", file=sys.stderr)
        else:
            print(f"Run: python3 agent_dash.py status --repair (to record pid {pid} in the pidfile)")
    if repair and not repaired:
        return 1
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
    url = _base_url()
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
    """Diagnose setup problems and print the fix. Exit 0 clean, 1 problems.

    Reads ``GET /api/doctor`` when the server is up and falls back to a
    local run (persisted like a server run). ``--ws`` keeps one
    workstream's problems plus global ones; ``--severity`` is the minimum
    level shown. ``--fix`` lists the safe automatic fixes without applying
    them; ``--fix --yes`` applies them. ``doctor report`` digests history.
    """
    from dash.doctor import (
        apply_safe_fixes,
        collect_problems,
        format_doctor_human,
        format_report,
        parse_since,
        safe_fixes,
    )

    url = _base_url()
    json_mode = False
    ws = None
    severity = None
    show_all = False
    fix = False
    yes = False
    rest = list(argv)
    if rest[:1] == ["report"]:
        return cmd_doctor_report(rest[1:])
    while rest:
        flag = rest.pop(0)
        if flag == "--url" and rest:
            url = rest.pop(0).rstrip("/")
        elif flag == "--json":
            json_mode = True
        elif flag == "--ws" and rest:
            ws = rest.pop(0)
        elif flag == "--severity" and rest:
            severity = rest.pop(0)
        elif flag == "--all":
            show_all = True
        elif flag == "--fix":
            fix = True
        elif flag == "--yes":
            yes = True
        else:
            print(USAGE, end="", file=sys.stderr)
            return 2
    if severity is not None and severity not in ("critical", "warn", "info"):
        print(
            f"doctor needs --severity critical|warn|info (got {severity!r}). "
            "Run: python3 agent_dash.py doctor --severity warn",
            file=sys.stderr,
        )
        return 2
    if yes and not fix:
        print("doctor --yes needs --fix. Run: python3 agent_dash.py doctor --fix --yes", file=sys.stderr)
        return 2
    cfg = bind_state()
    payload, store = _doctor_payload(cfg, url, ws, severity, show_all)
    try:
        if fix:
            return cmd_doctor_fix(store, cfg, payload, yes, json_mode)
        if json_mode:
            errors.emit_json(_doctor_cli_json(payload))
        else:
            print(format_doctor_human(payload, show_all))
        return _doctor_exit(payload)
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass


def _doctor_payload(cfg: dict, url: str, ws, severity, include_all: bool = False):
    """``(payload, store-or-None)`` from the server, else a local run.

    The local fallback collects and snapshots with the background
    scheduler's settings, so history stays continuous while the server
    is down. The caller closes the returned store.
    """
    from dash.doctor import collect_problems, doctor_cfg

    params = ""
    if ws not in (None, "all"):
        params += f"&ws={ws}" if params else f"?ws={ws}"
    if severity is not None:
        params += f"&severity={severity}" if params else f"?severity={severity}"
    if include_all:
        params += "&all=1" if params else "?all=1"
    payload, _error = _fetch(url, f"/api/doctor{params}")
    if payload is not None:
        return payload, None
    from dash.sources.process import live_worker_processes

    try:
        processes = live_worker_processes() or []
    except Exception:
        processes = []
    store = Store(DBFILE)
    now = time.time()
    result = collect_problems(store, cfg, now, url=url, health=None, processes=processes)
    payload = store.doctor_snapshot(result["problems"], now, doctor_cfg(cfg)["interval_s"])
    if ws not in (None, "all") or severity is not None or include_all:
        payload = store.doctor_latest(now, ws, severity, include_all)
    return payload, store


def _doctor_cli_json(payload: dict) -> dict:
    """Machine payload: the API body plus the exit verdict over shown problems."""
    shown = [item for item in payload.get("problems") or [] if item.get("severity") in ("critical", "warn")]
    return {**payload, "ok": not shown}


def _doctor_exit(payload: dict) -> int:
    """Exit 1 when a shown problem is critical or a warning, else 0."""
    for item in payload.get("problems") or []:
        if item.get("severity") in ("critical", "warn"):
            return 1
    return 0


def cmd_doctor_fix(store, cfg: dict, payload: dict, yes: bool, json_mode: bool) -> int:
    """List (``--fix``) or apply (``--fix --yes``) the safe automatic fixes."""
    from dash.doctor import _fix_command, apply_safe_fixes, safe_fixes

    problems = payload.get("problems") or []
    fixes = safe_fixes(problems)
    if not yes:
        lines = [f"doctor --fix dry run: {len(fixes)} safe fix(es), nothing applied"]
        for item in fixes:
            lines.append(f"  would apply [{item.get('code')}] {_fix_command(item)}")
        lines.append("  would also: prune dead offset rows; backfill run models")
        lines.append("re-run with --fix --yes to apply")
        text = "\n".join(lines)
        if json_mode:
            errors.emit_human(text, True)
            errors.emit_json({"dry_run": True, "fixes": [_fix_command(item) for item in fixes]})
        else:
            print(text)
        return 0
    if store is None:
        store = Store(DBFILE)
        close = True
    else:
        close = False
    try:
        messages = apply_safe_fixes(store, cfg, problems)
    finally:
        if close:
            try:
                store.close()
            except Exception:
                pass
    text = "\n".join(["doctor --fix applied:"] + [f"  {line}" for line in messages])
    if json_mode:
        errors.emit_human(text, True)
        errors.emit_json({"applied": messages})
    else:
        print(text)
    return 0


def cmd_doctor_report(argv: list[str]) -> int:
    """Digest history for hub reports: new, regressed, resolved, open by severity and workstream."""
    from dash.doctor import format_report, parse_since

    since_raw = None
    md = False
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if flag == "--since" and rest:
            since_raw = rest.pop(0)
        elif flag == "--md":
            md = True
        else:
            print(USAGE, end="", file=sys.stderr)
            return 2
    bind_state()
    now = time.time()
    try:
        since = parse_since(since_raw, now)
    except ValueError as exc:
        print(f"{exc}. Run: python3 agent_dash.py doctor report --since 24h", file=sys.stderr)
        return 2
    store = Store(DBFILE)
    try:
        report = store.doctor_report(since, now)
    finally:
        try:
            store.close()
        except Exception:
            pass
    print(format_report(report, md), end="")
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


REPRICE_USAGE = """usage: python3 agent_dash.py reprice [--dry-run]

Recompute est_cost_usd for every usage row (rereading Claude cache
splits where the 5m/1h split is empty). With --dry-run the summary is
computed but nothing is written."""


def cmd_reprice(argv: list[str] | None = None) -> int:
    args = list(argv or [])
    if any(flag in ("-h", "--help") for flag in args):
        print(REPRICE_USAGE)
        return 0
    dry_run = False
    for flag in args:
        if flag == "--dry-run":
            dry_run = True
        else:
            print(REPRICE_USAGE, file=sys.stderr)
            return 2
    cfg = bind_state()
    store = Store(DBFILE)
    try:
        summary = reprice_usage(store, _claude_projects(cfg), dry_run=dry_run)
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


def cmd_db(argv: list[str] | None = None) -> int:
    """Database maintenance: stats, usage rollup, dry runs.

    ``db stats`` prints size, row counts, rollup coverage, and the
    live/rolled time span. ``db rollup --dry-run`` reports what would
    roll without writing. ``db rollup [--days N] [--batch N]`` compacts
    usage rows older than N days (default: rollup_after_days, 14) into
    hourly bundles, batch after batch until nothing qualifies, with a
    per-batch verification that rolls back on mismatch. The live
    server is never touched; run this against the state dir directly.
    """
    rest = list(argv or [])
    if not rest or rest[0] not in ("stats", "rollup"):
        print("usage: python3 agent_dash.py db stats|rollup [--dry-run] [--days N] [--batch N]", file=sys.stderr)
        return 2
    bind_state()
    from dash.store import Store

    dry = "--dry-run" in rest
    days = 14
    batch = 200
    try:
        if "--days" in rest:
            days = float(rest[rest.index("--days") + 1])
        if "--batch" in rest:
            batch = int(rest[rest.index("--batch") + 1])
    except (ValueError, IndexError):
        print("db: --days N and --batch N take numbers", file=sys.stderr)
        return 2
    store = Store(DBFILE)
    try:
        if rest[0] == "stats":
            stats = store.db_rollup_stats()
            size = "unknown" if stats["size_mb"] is None else f"{stats['size_mb']:.0f} MB"
            print(f"database {DBFILE} ({size})")
            print(f"runs: {stats['runs']}, usage rows live: {stats['usage_event_rows']}, buckets: {stats['buckets']}")
            print(f"rolled: {stats['rolled_events']} event(s) in {stats['rollup_bundle_rows']} bundle(s)")
            print(f"live span: {stats['live_ts_min']}..{stats['live_ts_max']}")
            print(f"rolled span: {stats['rolled_hour_min']}..{stats['rolled_hour_max']}")
            return 0
        if dry:
            report = store.rollup_usage(older_than_days=days, batch_runs=batch, dry_run=True)
            print(
                f"rollup dry run: {report['candidate_rows']} row(s) across {report['candidate_runs']} run(s)"
                f" older than {days:g} days would roll (cutoff {report['cutoff']})"
            )
            return 0
        total_runs = total_rows = total_bundles = 0
        while True:
            report = store.rollup_usage(older_than_days=days, batch_runs=batch, time_budget_s=25.0)
            total_runs += report["rolled_runs"]
            total_rows += report["rolled_rows"]
            total_bundles += report["rolled_bundles"]
            if report.get("failed_runs"):
                print(f"rollup: verification kept {len(report['failed_runs'])} run(s) live", file=sys.stderr)
            if report["rolled_runs"] == 0:
                break
        print(f"rollup: compacted {total_rows} row(s) into {total_bundles} bundle(s) across {total_runs} run(s)")
        return 0
    finally:
        store.close()


def cmd_reconcile() -> int:
    """Mark grok, muse, glm, and pi rows whose process is gone.

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


def cmd_runs(argv: list[str] | None = None) -> int:
    """Run-row maintenance. ``runs prune-markers`` lists (default dry run)
    or deletes placeholder run rows: worker-source rows with no model, no
    verdict, and no usage history. ``--yes`` performs the deletion after
    copying the database to a timestamped backup; without it nothing is
    written. ``AGENT_DASH_DB`` selects the database, defaulting to the
    configured state dir.
    """
    rest = list(argv or [])
    if not rest or rest[0] != "prune-markers":
        print(USAGE, end="", file=sys.stderr)
        return 2
    rest = rest[1:]
    yes = False
    for flag in rest:
        if flag == "--yes":
            yes = True
        elif flag == "--dry-run":
            continue
        else:
            print(USAGE, end="", file=sys.stderr)
            return 2
    bind_state()
    db = os.environ.get("AGENT_DASH_DB")
    path = Path(expand(db)) if db else DBFILE
    if not path.is_file():
        print(f"no database {path}", file=sys.stderr)
        return 2
    backup = None
    if yes:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-markers-{stamp}")
        try:
            import sqlite3 as _sqlite3

            source = _sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                target = _sqlite3.connect(str(backup))
                try:
                    source.backup(target)
                finally:
                    target.close()
            finally:
                source.close()
        except Exception as exc:
            print(f"prune-markers backup failed: {exc}", file=sys.stderr)
            return 1
    store = Store(path)
    try:
        report = store.prune_marker_runs(dry_run=not yes)
    finally:
        store.close()
    markers = report.get("markers", [])
    for row in markers:
        print(
            "%s\t%s\t%s\t%s"
            % (
                row.get("run_id"),
                row.get("source"),
                row.get("label"),
                row.get("status"),
            )
        )
    if not yes:
        print(f"prune-markers dry run: {len(markers)} marker row(s) listed, 0 deleted")
        return 0
    print(
        "prune-markers backup %s: %d marker row(s) listed, %d deleted"
        % (backup, len(markers), report.get("deleted", 0))
    )
    return 0


def cmd_lead_profile(argv: list[str]) -> int:
    """A lead session's delegation profile: every worker it launched. Read-only."""
    from dash.orchestrate import lead_profile

    session_id = None
    ws = None
    json_mode = False
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if flag == "--ws" and rest:
            ws = rest.pop(0)
        elif flag == "--json":
            json_mode = True
        elif session_id is None and ws is None and not flag.startswith("-"):
            session_id = flag
        else:
            print(USAGE, end="", file=sys.stderr)
            return 2
    if (session_id is None) == (ws is None):
        print("lead-profile needs one session id or --ws <ID>. Run: python3 agent_dash.py lead-profile --ws WS4", file=sys.stderr)
        return 2
    bind_state()
    db = os.environ.get("AGENT_DASH_DB")
    path = Path(expand(db)) if db else DBFILE
    if not path.is_file():
        print(f"no database {path}", file=sys.stderr)
        return 2
    store = Store(path)
    try:
        profile = lead_profile(store, session_id=session_id, ws=ws)
    finally:
        store.close()
    if profile is None:
        message = f"unknown session {session_id}. Run: python3 agent_dash.py ws list" if session_id else f"no lead sessions for {ws}. Run: python3 agent_dash.py ws list"
        if json_mode:
            errors.emit_json({"error": message, "code": "not_found", "hint": "Run: python3 agent_dash.py ws list"})
        else:
            print(message, file=sys.stderr)
        return 3
    if json_mode:
        errors.emit_json(profile)
        return 0
    print(f"lead {profile.get('session_id')} ({profile.get('tool')}, {profile.get('label')}) ws={profile.get('ws')}")
    print(f"workers launched: {profile.get('workers_launched')} {profile.get('by_source')}")
    print(f"own tool calls: {profile.get('own_tool_calls')} context tokens: {profile.get('context_tokens')}")
    print(f"worker concurrency: mean {profile.get('mean_concurrency')} peak {profile.get('peak_concurrency')}")
    print(f"idle-ready share: {profile.get('idle_ready_share')} waves: {profile.get('wave_sizes')}")
    for tail in profile.get("tails") or []:
        print(f"  tail {tail.get('label')}: {tail.get('seconds')}s")
    flags = profile.get("flags") or []
    print(f"flags: {', '.join(flags) if flags else '-'}")
    return 0


def cmd_lanes(argv: list[str] | None = None) -> int:
    """List distinct live lanes, one per run, not per process.

    ``lanes live`` groups the dashboard live-worker listing by run
    identity (muse session uuid, bracket label, prompt-file label) so a
    run spawning several worker processes counts once. Tiers cover
    muse, cursor-agent as grok, the grok CLI as glm, and Claude
    subagents as claude when visible. The listing reuses
    ``live_worker_processes``, the same source the dashboard uses for
    its live set, so both agree. ``--json`` emits machine JSON.
    """
    rest = list(argv or [])
    if not rest or rest[0] != "live":
        print(USAGE, end="", file=sys.stderr)
        return 2
    rest = rest[1:]
    json_mode = False
    for flag in rest:
        if flag == "--json":
            json_mode = True
        else:
            print(USAGE, end="", file=sys.stderr)
            return 2
    from dash import lanes as lanes_lib
    from dash.sources.process import live_worker_processes

    try:
        processes = live_worker_processes()
    except Exception as exc:
        message = "lanes live failed: %s" % (exc,)
        if json_mode:
            errors.emit_json({"error": message, "code": "unavailable"})
        else:
            print(message, file=sys.stderr)
        return 1
    if processes is None:
        processes = []
    lanes = lanes_lib.group_live_processes(processes)
    total = len(processes)
    if json_mode:
        errors.emit_json({"count": len(lanes), "processes": total, "lanes": lanes})
        return 0
    print("live lanes: %d (from %d processes)" % (len(lanes), total))
    if lanes:
        width_label = max([len(str(lane.get("label") or "-")) for lane in lanes] + [5])
        width_tier = max([len(str(lane.get("tier") or "-")) for lane in lanes] + [4])
        print(
            "%s %s %s %s %s %s"
            % (
                "label".ljust(width_label),
                "tier".ljust(width_tier),
                "pid".rjust(8),
                "elapsed".rjust(10),
                "prompt".ljust(12),
                "procs".rjust(5),
            )
        )
        for lane in lanes:
            label = str(lane.get("label") or "-")
            tier = str(lane.get("tier") or "-")
            pid = lane.get("pid")
            pid_text = str(pid) if isinstance(pid, int) else "-"
            elapsed = lanes_lib.format_elapsed(lane.get("elapsed_s"))
            prompt = str(lane.get("prompt_label") or "-")
            procs = int(lane.get("processes") or 0)
            print(
                "%s %s %s %s %s %s"
                % (
                    label.ljust(width_label),
                    tier.ljust(width_tier),
                    pid_text.rjust(8),
                    elapsed.rjust(10),
                    prompt.ljust(12),
                    str(procs).rjust(5),
                )
            )
    return 0


def cmd_check(argv: list[str]) -> int:
    """Ask a running dashboard for GET /api/reconcile, print the table, exit 1 on any mismatch."""
    url = _base_url()
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
    mismatches = list(payload.get("mismatches", []))
    unseen = [item for item in mismatches if str(item).startswith("unseen worker: ")]
    if unseen:
        from dash.doctor import unseen_startup_ready

        health, _health_error = _fetch(url, "/api/health")
        if not unseen_startup_ready(None, health, time.time()):
            payload["mismatches"] = [
                item for item in mismatches if not str(item).startswith("unseen worker: ")
            ]
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


def cmd_label(argv: list[str]) -> int:
    """Build or check a canonical run label (``dash/labels.py``).

    ``label make`` prints the label. ``label check`` exits 0 when the
    label is strong (printing phase and rule, plus any conflict
    warning), 2 when refused (printing the one-line fix).
    """
    from dash import labels as label_lib

    if not argv or argv[0] not in ("make", "check"):
        print(
            "usage: python3 agent_dash.py label make --scope <wsN|hub> "
            "--mark <plan|exec|review|coord> --slug <slug> [--round N] "
            "or python3 agent_dash.py label check <label> [--ws <WS>]",
            file=sys.stderr,
        )
        return 2
    if argv[0] == "make":
        flags = _report_flags(argv[1:])
        if not flags.get("scope") or not flags.get("mark") or not flags.get("slug"):
            print(
                "label make needs --scope, --mark, and --slug. Run: python3 agent_dash.py "
                "label make --scope ws11 --mark plan --slug scout-10-partition-cold --round 1",
                file=sys.stderr,
            )
            return 2
        try:
            print(
                label_lib.make(
                    flags.get("scope", ""),
                    flags.get("mark", ""),
                    flags.get("slug", ""),
                    flags.get("round"),
                )
            )
        except ValueError as exc:
            print(f"label make failed: {exc}", file=sys.stderr)
            return 2
        return 0
    rest = argv[1:]
    label = next((token for token in rest if not token.startswith("--")), None)
    if label is None:
        print(
            "label check needs a label. Run: python3 agent_dash.py label check <label> [--ws <WS>]",
            file=sys.stderr,
        )
        return 2
    flags = _report_flags(rest)
    verdict = label_lib.check(label, ws=flags.get("ws"))
    if not verdict["ok"]:
        print(f"label refused: {verdict['fix']}", file=sys.stderr)
        return 2
    print(f"ok: {label} -> {verdict['phase']} via {verdict['rule']}")
    if verdict.get("warning"):
        print(verdict["warning"])
    return 0


def cmd_report(argv: list[str]) -> int:
    """Append one generic.jsonl line so any harness can report a run.

    start appends a running run line, finish a terminal run line with a
    verdict, usage a token line. The collector picks the file up on its
    next poll. Never stores prompts or secrets: the schema has no field
    for them.
    """
    if argv and argv[0] == "--run-dir":
        return cmd_report_run_dir(_report_flags(argv))
    if not argv or argv[0] not in ("start", "finish", "usage"):
        print("usage: python3 agent_dash.py report start|finish|usage --label <label> ... or report --run-dir <dir>. Run: python3 agent_dash.py report start --label ws4-WS4.1-slug --model <model> --session <id>", file=sys.stderr)
        return 2
    action = argv[0]
    flags = _report_flags(argv[1:])
    label = flags.get("label")
    if not label or len(label) > 200 or "/" in label or "\\" in label or label.startswith("."):
        print("report requires --label <label>, e.g. --label ws4-WS4.1-slug (no /, \\, or leading dot; max 200 chars)", file=sys.stderr)
        return 2
    if action == "start":
        # A bad label never opens a run: refuse before anything launches
        # or appends, with the exact corrected label. finish/usage stay
        # open so already-running (including legacy) runs can close out.
        from dash import labels as label_lib

        verdict = label_lib.check(label)
        if not verdict["ok"]:
            if label_lib.legacy_allowed():
                print(f"label warning (legacy replay): {verdict['fix']}", file=sys.stderr)
            else:
                print(f"label refused: {verdict['fix']}", file=sys.stderr)
                return 2
        elif verdict.get("warning"):
            print(verdict["warning"], file=sys.stderr)
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
        if verdict not in errors.VERDICTS:
            print(errors.unknown_verdict(verdict), file=sys.stderr)
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


def cmd_report_run_dir(flags: dict) -> int:
    """Register a manifest folder: ``report --run-dir <dir>``. No run line."""
    run_dir = flags.get("run-dir")
    if not run_dir:
        print("report --run-dir <dir> registers a manifest folder. Run: python3 agent_dash.py report --run-dir <dir>", file=sys.stderr)
        return 2
    cfg = bind_state()
    state = Path(expand(str(cfg.get("state_dir") or STATE_DIR)))
    state.mkdir(parents=True, exist_ok=True)
    if _register_report_dir(state, run_dir):
        print(f"registered run dir {run_dir}")
        return 0
    print(f"report could not register run dir {run_dir!r}", file=sys.stderr)
    return 1


def _register_report_dir(state: Path, run_dir: str) -> bool:
    """Register a harness run folder from ``report --run-dir``. Never raises."""
    try:
        from dash.rundirs import register_run_dir

        return bool(register_run_dir(state, run_dir))
    except Exception:
        return False


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


def _label_plan_ids(ws_filter: str | None = None) -> dict:
    """Plan task ids per workstream, best effort. Never raises."""
    found: dict[str, list[str]] = {}
    try:
        root = dataroot.workstreams_dir()
    except Exception:
        return found
    try:
        if ws_filter:
            targets = [str(ws_filter).upper()]
        else:
            targets = [entry.name for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return found
    for target in targets:
        ws_id = str(target).upper()
        try:
            data = json.loads((root / target / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        try:
            from dash.taskmatch import plan_task_ids

            groups = data.get("dag", {}).get("groups") if isinstance(data, dict) else None
            found[ws_id] = [str(item) for item in plan_task_ids(groups)]
        except Exception:
            continue
    return found


def _label_taskmap() -> dict:
    """Resolved label-to-task map, or empty when unreadable. Never raises."""
    try:
        from dash.taskmatch import load_taskmap

        table = load_taskmap()
    except Exception:
        return {}
    return table if isinstance(table, dict) else {}


def _label_collision_warning(
    label: str, scope: str | None, plans: dict, taskmap: dict
) -> str | None:
    """Plan-task collision for a label the strict check accepted.

    Only the label's own workstream is consulted: a run attributes
    inside its own scope, so task ids from other workstreams are never
    a collision. Returns the warning line, or None when quiet.
    """
    text = str(label or "")
    scope_id = str(scope or "").upper()
    if scope_id == "HUB":
        return None
    if not scope_id:
        head, dash, _rest = text.partition("-")
        if dash and re.fullmatch(r"ws[1-9][0-9]*|hub", head.lower()):
            scope_id = head.upper()
    if not scope_id or scope_id == "HUB":
        return None
    ids = [str(item) for item in (plans.get(scope_id) or []) if str(item)]
    if not ids:
        return None
    lowered = text.casefold()
    for ident in ids:
        if ident.casefold() == lowered:
            return (
                f"warning: label equals plan task {ident} ({scope_id}); "
                f"the run will attribute to that task"
            )
    try:
        from dash.phases import resolve_task
    except Exception:
        return None
    try:
        hit = resolve_task(text, scope_id, taskmap, ids)
    except Exception:
        return None
    if hit is not None and str(hit) in set(ids):
        return (
            f"warning: label collides with plan task {hit} ({scope_id}); "
            f"the run will attribute to that task"
        )
    return None


def cmd_label(argv: list[str]) -> int:
    """Build or check a canonical run label (``dash/labels.py``).

    ``label make`` prints the label. ``label check`` exits 0 when the
    label is strong (printing phase and rule, plus any conflict
    warning), 2 when refused (printing the one-line fix). A label that
    names a plan task of its own workstream gains the collision
    warning even when the strict verdict stays quiet.
    """
    from dash import labels as label_lib

    if not argv or argv[0] not in ("make", "check"):
        print(
            "usage: python3 agent_dash.py label make --scope <wsN|hub> "
            "--mark <plan|exec|review|coord> --slug <slug> [--round N] "
            "or python3 agent_dash.py label check <label> [--ws <WS>]",
            file=sys.stderr,
        )
        return 2
    if argv[0] == "make":
        flags = _report_flags(argv[1:])
        if not flags.get("scope") or not flags.get("mark") or not flags.get("slug"):
            print(
                "label make needs --scope, --mark, and --slug. Run: python3 agent_dash.py "
                "label make --scope ws11 --mark plan --slug scout-10-partition-cold --round 1",
                file=sys.stderr,
            )
            return 2
        try:
            print(
                label_lib.make(
                    flags.get("scope", ""),
                    flags.get("mark", ""),
                    flags.get("slug", ""),
                    flags.get("round"),
                )
            )
        except ValueError as exc:
            print(f"label make failed: {exc}", file=sys.stderr)
            return 2
        return 0
    rest = argv[1:]
    flags = _report_flags(rest)
    label = flags.get("label")
    if label is None:
        skip_next = False
        for token in rest:
            if skip_next:
                skip_next = False
                continue
            if token.startswith("--"):
                skip_next = True
                continue
            label = token
            break
    if label is None:
        print(
            "label check needs a label. Run: python3 agent_dash.py label check <label> [--ws <WS>]",
            file=sys.stderr,
        )
        return 2
    verdict = label_lib.check(label, ws=flags.get("ws"))
    if not verdict["ok"]:
        print(f"label refused: {verdict['fix']}", file=sys.stderr)
        return 2
    print(f"ok: {label} -> {verdict['phase']} via {verdict['rule']}")
    if verdict.get("warning"):
        print(verdict["warning"])
        return 0
    scope = str(verdict.get("scope") or "").upper()
    warning = _label_collision_warning(
        label, scope or None, _label_plan_ids(scope or None), _label_taskmap()
    )
    if warning:
        print(warning)
    return 0


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
        before = (store.data_seq, store.cursor_seq)
        registry.tick(store, cfg, time.time(), force=True)
        passes += 1
        if (store.data_seq, store.cursor_seq) == before:
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


def _write_pidfile(pid: int) -> None:
    """Record ``pid`` atomically (temp file + replace), as ``_serve`` does."""
    tmp = PIDFILE.with_suffix(".tmp")
    tmp.write_text(str(pid) + "\n", encoding="utf-8")
    os.replace(tmp, PIDFILE)


def _release_pidfile(pid: int) -> None:
    """Remove the pidfile, but only when it still names ``pid``.

    A restart writes a new pidfile while the old server shuts down; an
    unconditional unlink by the exiting server would orphan the new one
    (status "stopped" forever, stop unable to find it). Never raises.
    """
    try:
        if read_pid() != pid:
            return
        PIDFILE.unlink(missing_ok=True)
    except OSError:
        pass


def _claimed_pid() -> tuple[int | None, int | None]:
    """``(live, listed)``: the pidfile pid when it is a live dashboard,
    plus the raw pidfile entry (kept so the stale path can owner-check
    the exact value it verified, not a later replacement)."""
    listed = read_pid()
    if listed is not None and pid_exists(listed) and is_dash(listed):
        return listed, listed
    return None, listed


def _release_stale_pidfile(pid: int) -> None:
    """Remove a stale pidfile entry its verifier already checked.

    The entry is removed only when the pidfile still names ``pid`` and
    ``pid`` is not a live dashboard (a racing replacement, possibly
    under a reused pid number, is always kept). Never raises.
    """
    try:
        if read_pid() != pid:
            return
        if pid_exists(pid) and is_dash(pid):
            return
        PIDFILE.unlink(missing_ok=True)
    except OSError:
        pass


def _listener_pid(port: int) -> int | None:
    """The pid listening on 127.0.0.1:``port``, via read-only ``lsof``.

    None when nothing listens, ``lsof`` is missing, or the output does
    not parse. Never signals anything.
    """
    try:
        proc = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        try:
            return int(line.strip())
        except ValueError:
            continue
    return None


def _serves_dashboard(pid: int) -> bool:
    """True when ``pid``'s command line is the dashboard server (``run`` or the spawned ``serve``)."""
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
    return "agent_dash.py" in text and re.search(r"\b(run|serve)\b", text) is not None


def _dashboard_on_port() -> tuple[int | None, dict | None]:
    """``(pid, health)`` for the dashboard answering the configured port.

    The port must answer ``/api/health`` and its listener's command
    line must be the dashboard server. ``(None, None)`` otherwise; this
    is how ``status``/``stop``/``restart`` find a dashboard whose
    pidfile is missing or stale.
    """
    health = _probe_health()
    if not isinstance(health, dict):
        return None, None
    pid = _listener_pid(_port())
    if pid is not None and pid_exists(pid) and _serves_dashboard(pid):
        return pid, health
    return None, None


def reconcile_if_due(store, now: float, last: float | None, interval: float = RECONCILE_INTERVAL_S) -> float:
    """Call ``reconcile_running`` when ``interval`` seconds have passed.

    ``last is None`` runs it immediately, so a restart closes rows that were
    left unknown. Returns the timestamp of the last reconcile.
    """
    if last is not None and now - last < interval:
        return last
    reconcile_running(store, now)
    return now


NULL_REPRICE_INTERVAL_S = 60


def reprice_if_due(store, now: float, last: float | None, interval: float = NULL_REPRICE_INTERVAL_S) -> float:
    """Reprice NULL-cost usage rows once per price-catalog change.

    Costs are written once at ingest, so rows ingested before a catalog
    edit keep NULL costs until ``reprice`` runs. When the catalog file's
    signature (mtime + size) differs from the one recorded in ``meta``,
    one bounded pass prices NULL-cost rows with the current catalog, off
    the request path on the collector tick. A pass that fills its batch
    leaves the signature unmarked so the next tick continues; a drained
    pass records it. Never raises.
    """
    if last is not None and now - last < interval:
        return last
    log = logging.getLogger("agent_dash.reprice")
    try:
        from dash.pricing import CATALOG_SIG_KEY, catalog_sig, reprice_null_costs

        sig = catalog_sig()
        if sig is None:
            return now
        with store._lock:
            store._conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            try:
                store._conn.commit()
            except Exception:
                pass
            row = store._conn.execute(
                "SELECT value FROM meta WHERE key=?", (CATALOG_SIG_KEY,)
            ).fetchone()
            if row and row["value"] == sig:
                return now
        report = reprice_null_costs(store)
        if report.get("truncated"):
            log.info(
                "catalog changed: repriced %d NULL-cost row(s), more remain",
                report.get("priced"),
            )
            return now
        with store._lock:
            store._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (CATALOG_SIG_KEY, sig),
            )
            try:
                store._conn.commit()
            except Exception:
                pass
        if report.get("read"):
            log.info(
                "catalog changed: repriced %d NULL-cost row(s)", report.get("priced")
            )
    except Exception:
        log.exception("catalog reprice pass failed")
    return now


ROLLUP_INTERVAL_S = 300


def rollup_if_due(store, cfg: dict, now: float, last: float | None, interval: float = ROLLUP_INTERVAL_S) -> float:
    """Compact old usage rows and tend the database file when due.

    Runs at low priority every few minutes: a small time-budgeted
    rollup batch (off unless ``rollup_after_days`` is positive), a
    passive WAL checkpoint so the -wal stays small, and a weekly
    ``PRAGMA optimize`` so the planner keeps its statistics. Returns
    the timestamp of the last run. Never raises.
    """
    if last is not None and now - last < interval:
        return last
    log = logging.getLogger("agent_dash.rollup")
    try:
        days = (cfg or {}).get("rollup_after_days", 14)
        report = store.rollup_usage(older_than_days=days, batch_runs=200, time_budget_s=2.0, now=now)
        if report.get("rolled_rows"):
            log.info(
                "rollup compacted %d row(s) into %d bundle(s) across %d run(s) in %.1fs",
                report["rolled_rows"], report["rolled_bundles"], report["rolled_runs"], report["elapsed_s"],
            )
        if report.get("failed_runs"):
            log.warning("rollup verification kept %d run(s) live", len(report["failed_runs"]))
    except Exception:
        log.exception("rollup batch failed")
    # The store connection is shared with the request threads: every
    # statement here runs under the facade lock, like any other store
    # access. rollup_usage locks internally per batch.
    try:
        with store._lock:
            store._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass
    try:
        with store._lock:
            meta = store._conn.execute("SELECT value FROM meta WHERE key='rollup_optimize_at'").fetchone()
            last_opt = float(meta["value"]) if meta and meta["value"] else 0.0
            if now - last_opt > 7 * 86400:
                store._conn.execute("PRAGMA optimize")
                try:
                    store._conn.execute("ANALYZE usage_events")
                except Exception:
                    pass
                store._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('rollup_optimize_at', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(now),),
                )
    except Exception:
        log.exception("optimize failed")
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
    from dash.doctor import DoctorScheduler

    cfg = load_cfg()
    store = Store(DBFILE)
    try:
        stale = store.repair_stale_tiers()
    except Exception:
        logging.getLogger("agent_dash.tiers").exception("stale tier repair failed")
    else:
        if stale:
            logging.getLogger("agent_dash.tiers").info("retiered %d run(s) from stale source tiers", stale)
    registry = Registry()
    server = DashServer(store, registry, HOST, _port(), cfg=cfg)
    server.doctor = DoctorScheduler(store, registry, cfg, build=server.build)
    server.doctor.start()
    server.warm()
    _write_pidfile(os.getpid())
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
        last_rollup = None
        last_reprice = None
        while not stop.wait(0.25):
            now = time.time()
            try:
                registry.tick(store, cfg, now, stop=stop)
                last_reconcile = reconcile_if_due(store, now, last_reconcile)
                last_prune = prune_if_due(store, STATE_DIR, now, last_prune)
                last_rollup = rollup_if_due(store, cfg, now, last_rollup)
                last_reprice = reprice_if_due(store, now, last_reprice)
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
        try:
            server.doctor.stop()
        except Exception:
            pass
        server.server_close()
        store.close()
        _release_pidfile(os.getpid())


def _wait_for_child(child: int) -> int:
    """Wait for the spawned dashboard ``child`` to record its pidfile, or to exit.

    The child writes the pidfile only after its store, sources and server
    are up, which can take longer than a few seconds on a loaded machine.
    A child that exits first has failed and is reported at once; a child
    still starting gets ``CHILD_START_S`` seconds before the start counts
    as failed. The pidfile names ``child`` itself, so no ``ps`` probe is
    needed (a slow probe would read as a failed start).
    """
    deadline = time.time() + CHILD_START_S
    while time.time() < deadline:
        if read_pid() == child and pid_exists(child):
            _print_running(child)
            return 0
        try:
            reaped, status = os.waitpid(child, os.WNOHANG)
        except ChildProcessError:
            print(f"failed to start: the dashboard process was already reaped (log: {LOGFILE})", file=sys.stderr)
            return 1
        if reaped == child:
            print(f"failed to start: the dashboard process exited ({_how_ended(status)}; log: {LOGFILE})", file=sys.stderr)
            return 1
        time.sleep(0.05)
    print(f"failed to start: no pidfile after {CHILD_START_S:g} s (log: {LOGFILE})", file=sys.stderr)
    return 1


def _how_ended(status: int) -> str:
    """Words for a ``waitpid`` status: the exit code or the signal."""
    if os.WIFSIGNALED(status):
        return f"signal {signal.Signals(os.WTERMSIG(status)).name}"
    return f"exit code {os.WEXITSTATUS(status)}"


def _print_running(pid: int, build: dict | None = None) -> None:
    lines = [f"running\npid {pid}\nurl {_base_url()}"]
    if build is not None:
        lines.append(f"build {build.get('build_id', 'unknown')}")
        lines.append(f"started_at {build.get('started_at')}")
    print("\n".join(lines))


def _print_running_unknown(health: dict) -> None:
    """The port answers our build but the pidfile is stale: still running."""
    print(f"running\nurl {_base_url()}\nbuild {health.get('build_id', 'unknown')}\nstarted_at {health.get('started_at')}")


def _session_arg(args: list[str]) -> str | None:
    if "--session" not in args:
        return None
    index = args.index("--session")
    if index + 1 >= len(args):
        return None
    return args[index + 1]


if __name__ == "__main__":
    sys.exit(main())
