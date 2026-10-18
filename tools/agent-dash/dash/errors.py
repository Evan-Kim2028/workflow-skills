"""One place for every agent-facing error message and exit code.

Exit codes are stable across ``agent_dash.py`` and every ``ws`` subcommand:

- 0 ok
- 1 check failed (mismatches found, or doctor found problems)
- 2 usage error (bad flags or bad argument values)
- 3 not found (workstream, task, train member, approval, bundle)
- 4 invalid data (stored JSON fails validation)
- 5 dashboard unreachable

Every message says what was wrong, the valid choices when the set is
small, and the exact next command to run. JSON goes to stdout only;
human text goes to stderr when ``--json`` is set.
"""

from __future__ import annotations

import json
import sys

OK = 0
CHECK_FAILED = 1
USAGE = 2
NOT_FOUND = 3
INVALID_DATA = 4
UNREACHABLE = 5

CODES = {
    OK: "ok",
    CHECK_FAILED: "check_failed",
    USAGE: "usage_error",
    NOT_FOUND: "not_found",
    INVALID_DATA: "invalid_data",
    UNREACHABLE: "unreachable",
}

STATUSES = ("todo", "wip", "pr", "prog", "done", "deferred")
TRAIN_STATUSES = ("planned", "assembling", "gate", "pr", "merged", "dropped")
FINISH_STATUSES = ("done", "killed", "died_early")
VERDICTS = ("PASS", "PARTIAL", "BLOCKED", "APPROVE", "APPROVE_WITH_NITS", "APPROVE_WITH_CHANGES", "REQUEST_CHANGES")


def emit_human(text: str, json_mode: bool = False) -> None:
    """Human text: stdout normally, stderr when ``--json`` is set."""
    print(text, file=sys.stderr if json_mode else sys.stdout)


def emit_json(payload: dict) -> None:
    """Machine payload: stdout only, always."""
    print(json.dumps(payload, indent=2))


def dashboard_unreachable(url: str) -> str:
    return (
        f"dashboard not reachable at {url}. "
        "Run: python3 agent_dash.py run (or restart)"
    )


def bad_port(raw: str) -> str:
    return (
        f"bad port AGENT_DASH_PORT={raw!r}. "
        "Use an integer 1-65535, e.g. AGENT_DASH_PORT=8815. "
        "Unset it to use the default 8765."
    )


def action_takes_no_args(cmd: str, arg: str) -> str:
    return (
        f"{cmd} takes no arguments (got {arg!r}). "
        f"Run: python3 agent_dash.py {cmd}"
    )


def build_mismatch(port: int, theirs: str, mine: str) -> str:
    return (
        f"run refused: 127.0.0.1:{port} already serves build {theirs}, "
        f"this checkout is build {mine}. "
        "Run: python3 agent_dash.py restart (to replace it with this build), "
        "or: python3 agent_dash.py status (to inspect the running build). "
        "Run with --force-replace to restart it directly: "
        "python3 agent_dash.py run --force-replace"
    )


def foreign_state(action: str, pid: int, port: int, target_hash: str | None, current_hash: str) -> str:
    target = target_hash or "unknown"
    return (
        f"{action} refused: dashboard pid {pid} on 127.0.0.1:{port} "
        f"belongs to another state dir (state {target}, not {current_hash}). "
        "A trial that sets AGENT_DASH_STATE_DIR can never signal the live "
        "dashboard by accident. "
        f"Run with --yes-live to signal it anyway: python3 agent_dash.py {action} --yes-live"
    )


def live_needs_flag(action: str) -> str:
    return (
        f"{action} refused: this would signal the live dashboard from a "
        "non-interactive session without AGENT_DASH_STATE_DIR set. "
        "Never run stop or restart in tests without AGENT_DASH_STATE_DIR and AGENT_DASH_PORT. "
        f"Run with --yes-live to signal it anyway: python3 agent_dash.py {action} --yes-live"
    )


def bundle_not_found(ws: str) -> str:
    return (
        f"bundle not found: {ws}. "
        "Run: python3 agent_dash.py ws list"
    )


def task_not_found(ws: str, task_id: str, known: int) -> str:
    return (
        f"task {task_id} not found in {ws} ({known} tasks). "
        f"Run: python3 agent_dash.py ws plan {ws}"
    )


def unknown_status(status: str) -> str:
    return (
        f"unknown status {status!r}. "
        f"Use one of: {', '.join(STATUSES)}. "
        "Run: python3 agent_dash.py ws validate <ID> to check the plan first"
    )


def unknown_train_status(status: str) -> str:
    return (
        f"unknown train status {status!r}. "
        f"Use one of: {', '.join(TRAIN_STATUSES)}"
    )


def unknown_finish_status(status: str) -> str:
    return (
        f"unknown finish status {status!r}. "
        f"Use one of: {', '.join(FINISH_STATUSES)}"
    )


def unknown_verdict(verdict: str) -> str:
    return (
        f"unknown verdict {verdict!r}. "
        f"Use one of: {', '.join(VERDICTS)}. "
        "Run: python3 agent_dash.py report finish --label <label> --verdict PASS"
    )


def plan_unreadable(ws: str) -> str:
    return (
        f"plan.json for {ws} is unreadable (missing, too large, or bad JSON). "
        f"Run: python3 agent_dash.py ws validate {ws}"
    )


def approvals_unreadable(ws: str) -> str:
    return (
        f"approvals.json for {ws} is unreadable (missing or bad JSON). "
        f"Run: python3 agent_dash.py ws validate {ws}"
    )


def api_error(error: str, code: str, hint: str) -> dict:
    """JSON body every API error carries: what, machine code, next step."""
    return {"error": error, "code": code, "hint": hint}
