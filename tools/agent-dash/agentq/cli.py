"""agentq command line.

Commands: submit, wait, escalate, ls, result, cancel, daemon, status, slots and
backlog (add, ls, rm, pause, resume). Every
command prints its output and ends with one ``ANSWER:`` line. Exit codes:
0 ok, 1 negative fact (unknown job, nothing to escalate, a failed job), 3
timeout or the store unreachable.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from agentq import backlog as backlog_lib
from agentq import fill as fill_lib
from agentq import filler as filler_lib
from agentq import broker as broker_lib
from agentq import db
from agentq import slots as slots_lib

HARNESS_VARS = (
    ("CLAUDE_CODE_SESSION_ID", "claude"),
    ("CODEX_SESSION_ID", "codex"),
    ("MUSE_SESSION_ID", "muse"),
)
HARNESS_BINARIES = ("claude", "codex", "muse", "pi")


def _ps_parent_and_args(pid: int):
    try:
        proc = subprocess.run(
            ["ps", "-o", "ppid=,command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    fields = proc.stdout.strip().split(None, 1)
    if proc.returncode != 0 or not fields or not fields[0].isdigit():
        return None
    return int(fields[0]), (fields[1] if len(fields) > 1 else "")


def harness_ancestor(start_pid: int, max_depth: int = 12):
    """Walk parent pids to the first harness process: ``(name, pid)`` or None."""
    pid = start_pid
    for _ in range(max_depth):
        if pid <= 1:
            return None
        found = _ps_parent_and_args(pid)
        if found is None:
            return None
        parent, args = found
        words = args.split()
        for word in words[:2]:
            name = os.path.basename(word)
            if name in HARNESS_BINARIES:
                return name, pid
        pid = parent
    return None


def identity(env, cwd: str, ancestor=harness_ancestor) -> tuple[str, str]:
    """``(session, harness)`` for the calling process.

    Order: the harness session variables, ``AGENTQ_SESSION``, the harness
    ancestor process (``<name>:<pid>:<cwd>``), then the shell parent.
    """
    for var, harness in HARNESS_VARS:
        value = (env.get(var) or "").strip()
        if value:
            return value, harness
    explicit = (env.get("AGENTQ_SESSION") or "").strip()
    if explicit:
        return explicit, (env.get("AGENTQ_HARNESS") or "explicit").strip()
    found = ancestor(os.getppid())
    if found is not None:
        name, pid = found
        return f"{name}:{pid}:{cwd}", name
    return f"shell:{os.getppid()}:{cwd}", "shell"


class Context:
    """Per-invocation state: home, output stream, clock and the caller's identity."""

    def __init__(self, env, out, now, ancestor):
        self.env = env
        self.out = out
        self.now = now
        self.home = db.home_dir(env)
        self._ancestor = ancestor
        self._identity = None

    @property
    def session(self) -> str:
        return self._session_and_harness()[0]

    @property
    def harness(self) -> str:
        return self._session_and_harness()[1]

    def _session_and_harness(self) -> tuple[str, str]:
        if self._identity is None:
            self._identity = identity(self.env, os.getcwd(), self._ancestor)
        return self._identity

    def print(self, text: str = "") -> None:
        print(text, file=self.out)


def answer(ctx: Context, text: str, code: int) -> int:
    ctx.print(f"ANSWER: {text}")
    return code


def cmd_submit(args, ctx: Context) -> int:
    prompt = Path(args.prompt_file).expanduser()
    if not prompt.is_file():
        return answer(ctx, f"no prompt file at {prompt}", 1)
    lane = "urgent" if args.urgent else "backlog" if args.backlog else "normal"
    job_id = db.new_job_id()
    label = args.label or broker_lib.label_for(args.kind, f"agentq-{job_id[:8]}")
    if not label:
        return answer(ctx, "label make refused; pass --label", 1)
    tier = args.tier or db.default_tier(args.kind)
    workspace = os.path.abspath(os.path.expanduser(args.workspace or os.getcwd()))
    conn = db.connect(ctx.home)
    dest = db.job_dir(ctx.home, job_id) / "prompt.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(prompt, dest)
    job = {
        "id": job_id, "lane": lane, "kind": args.kind, "res": args.res, "tier": tier,
        "label": label, "workspace": workspace, "session": ctx.session,
        "harness": ctx.harness, "prompt_path": str(dest),
        "max_runtime_s": args.max_runtime, "in_place": int(args.in_place),
    }
    with db.tx(conn):
        db.insert_job(conn, job, ctx.now())
    ctx.print(job_id)
    return answer(ctx, f"queued {job_id} ({tier}, {lane} lane, label {label})", 0)


def _open_ids(conn, session: str) -> list[str]:
    jobs = db.select_jobs(conn, "session = ? AND state IN ('queued', 'running')", (session,))
    return [job["id"] for job in jobs]


def cmd_wait(args, ctx: Context) -> int:
    conn = db.connect(ctx.home)
    ids = _open_ids(conn, ctx.session) if args.mine else list(args.ids)
    if not ids:
        return answer(ctx, "no open jobs to wait for", 1)
    deadline = ctx.now() + args.timeout
    while True:
        jobs = [db.get_job(conn, job_id) for job_id in ids]
        unknown = [job_id for job_id, job in zip(ids, jobs) if job is None]
        if unknown:
            return answer(ctx, f"unknown job(s): {' '.join(unknown)}", 1)
        if all(job["state"] in db.TERMINAL_STATES for job in jobs):
            break
        remaining = deadline - ctx.now()
        if remaining <= 0:
            open_ids = [job["id"] for job in jobs if job["state"] not in db.TERMINAL_STATES]
            hint = "" if broker_lib.daemon_pid(ctx.home) else "; the broker daemon is not running"
            return answer(ctx, f"timed out after {args.timeout}s, still open: {' '.join(open_ids)}{hint}", 3)
        time.sleep(min(1.0, remaining))
    for job in jobs:
        ctx.print(f"{job['id']} {job['verdict'] or 'none'} {job['report_path'] or '-'}")
    failed = [job["id"] for job in jobs if job["state"] != "done"]
    if failed:
        return answer(ctx, f"{len(failed)} of {len(jobs)} not done: {' '.join(failed)}", 1)
    return answer(ctx, f"{len(jobs)} job(s) done", 0)


def cmd_escalate(args, ctx: Context) -> int:
    conn = db.connect(ctx.home)
    if args.id:
        job = db.get_job(conn, args.id)
        if job is None:
            return answer(ctx, f"unknown job {args.id}", 1)
        candidates = [job]
    else:
        candidates = db.select_jobs(conn, "session = ? AND state IN ('queued', 'running')", (ctx.session,))
    moved = [job for job in candidates if job["state"] in db.OPEN_STATES and job["lane"] != "urgent"]
    for job in moved:
        detail = json.dumps({"session": ctx.session, "harness": ctx.harness, "from": job["lane"]})
        if not db.transition(conn, job["id"], db.OPEN_STATES, ctx.now(), "escalate", detail, lane="urgent"):
            continue
        ctx.print(f"{job['id']} {job['lane']} -> urgent ({job['state']})")
    if not moved:
        return answer(ctx, "nothing to escalate: no open job below urgent for this target", 1)
    return answer(ctx, f"escalated {len(moved)} job(s) to urgent", 0)


def cmd_ls(args, ctx: Context) -> int:
    conn = db.connect(ctx.home)
    if args.mine:
        jobs = db.select_jobs(conn, "session = ?", (ctx.session,))
    else:
        jobs = db.select_jobs(conn)
    if args.json:
        ctx.print(json.dumps(jobs, indent=2))
    else:
        for job in jobs:
            worktree = f" {broker_lib.worktree_note(job)}" if job["orig_workspace"] else ""
            ctx.print(f"{job['id']} {job['state']:<9} {job['lane']:<7} {job['tier']:<20} {job['label']} {job['verdict'] or '-'}{worktree}")
    return answer(ctx, f"{len(jobs)} job(s)", 0)


def cmd_result(args, ctx: Context) -> int:
    job = db.get_job(db.connect(ctx.home), args.id)
    if job is None:
        return answer(ctx, f"unknown job {args.id}", 1)
    if job["state"] not in db.TERMINAL_STATES:
        return answer(ctx, f"{args.id} is {job['state']}; no result yet", 1)
    ctx.print(f"{job['id']} {job['verdict'] or 'none'} {job['report_path'] or '-'}")
    if job["orig_workspace"]:
        ctx.print(f"{broker_lib.worktree_note(job)} (from {job['orig_workspace']})")
    if args.print and job["report_path"]:
        ctx.print(Path(job["report_path"]).read_text(encoding="utf-8"))
    ok = job["state"] == "done"
    return answer(ctx, f"{args.id} {job['state']}, verdict {job['verdict'] or 'none'}", 0 if ok else 1)


def cmd_cancel(args, ctx: Context) -> int:
    conn = db.connect(ctx.home)
    job = db.get_job(conn, args.id)
    if job is None:
        return answer(ctx, f"unknown job {args.id}", 1)
    if job["state"] in db.TERMINAL_STATES:
        return answer(ctx, f"{args.id} is already {job['state']}", 1)
    action = broker_lib.cancel(conn, job, ctx.now())
    if action is None:
        now_state = db.get_job(conn, args.id)["state"]
        return answer(ctx, f"{args.id} is already {now_state}; not changed", 1)
    return answer(ctx, f"cancelled {args.id}: {action}", 0)


def cmd_daemon(args, ctx: Context) -> int:
    try:
        broker_lib.Broker(ctx.home).run_forever()
    except broker_lib.DaemonRunning as exc:
        broker_lib.log_line(ctx.home, f"second daemon exits: {exc}")
        return answer(ctx, f"{exc}; this instance exits", 0)
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_status(args, ctx: Context) -> int:
    conn = db.connect(ctx.home)
    pid = broker_lib.daemon_pid(ctx.home)
    queued = conn.execute("SELECT lane, COUNT(*) FROM jobs WHERE state = 'queued' GROUP BY lane").fetchall()
    running = conn.execute(
        "SELECT tier, res, COUNT(*) FROM jobs WHERE state = 'running' GROUP BY tier, res"
    ).fetchall()
    depth = {lane: count for lane, count in queued}
    for lane in db.LANES:
        ctx.print(f"queued {lane}: {depth.get(lane, 0)}")
    for tier, res, count in running:
        ctx.print(f"running {tier} {res}: {count}")
    templates = backlog_lib.select_all(conn)
    enabled = sum(1 for template in templates if template["enabled"])
    ctx.print(f"backlog templates: {enabled} enabled of {len(templates)}; "
              f"template jobs open: {backlog_lib.open_backlog_jobs(conn)}")
    fill_on = broker_lib.load_config(ctx.home)["fill"]["enabled"]
    filler_state = "paused" if filler_lib.paused(conn) else ("on" if fill_on else "off")
    filler_running = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE state = 'running' AND label LIKE ?", (fill_lib.FILLER_LABEL_LIKE,)
    ).fetchone()[0]
    ctx.print(f"filler: {filler_state}; filler jobs running: {filler_running}")
    proxy = broker_lib.proxy_status()
    ctx.print(f"proxy: {'reachable' if proxy is not None else 'unreachable (static slots)'}")
    daemon = "not running" if pid is None else f"running (pid {pid or '?'})"
    total_running = sum(count for _, _, count in running)
    summary = f"daemon {daemon}; queued {sum(depth.values())}; running {total_running}"
    return answer(ctx, summary, 0 if pid is not None else 1)


def cmd_slots(args, ctx: Context) -> int:
    limits = slots_lib.load_limits(ctx.home)
    parts = []
    for kind in slots_lib.KINDS:
        limit = slots_lib.kind_limit(limits, kind)
        held = slots_lib.holders(ctx.home, kind, limit, clock=ctx.now)
        ctx.print(f"{kind}: {len(held)} of {limit} held")
        for item in held:
            ctx.print(f"  slot {item['slot']}  pid {item['pid']}  held {item['held_s']}s  "
                      f"label {item['label'] or '-'}  {item['command']}")
        parts.append(f"{kind} {len(held)}/{limit}")
    return answer(ctx, "slots held: " + "; ".join(parts), 0)


def cmd_inflight_report(args, ctx: Context) -> int:
    from agentq import inflight as inflight_lib

    text, summary = inflight_lib.report(db.db_path(ctx.home), args.hours, now=ctx.now())
    ctx.print(text)
    if not summary:
        return answer(ctx, "no DeepSeek minutes recorded in the window", 1)
    arms = ", ".join(sorted(summary))
    return answer(ctx, f"per-arm DeepSeek report over the last {args.hours:g} h (arms: {arms}); read-only", 0)


def _when(ts) -> str:
    return "never" if ts is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _next_due(conn, template: dict, now: float) -> str:
    if not template["enabled"]:
        return "paused"
    if backlog_lib.open_job(conn, template["name"]) is not None:
        return "job open"
    if backlog_lib.is_due(template, now):
        return "now (waits for idle Morph)"
    return _when(template["last_submitted"] + template["every_s"])


def cmd_backlog_add(args, ctx: Context) -> int:
    conn = db.connect(ctx.home)
    try:
        every_s = backlog_lib.parse_duration(args.every)
        max_runtime = backlog_lib.parse_duration(args.max_runtime) if args.max_runtime else None
        workspace = os.path.abspath(os.path.expanduser(args.workspace or os.getcwd()))
        with db.tx(conn):
            row = backlog_lib.add(
                conn, ctx.home, name=args.name, prompt_file=args.prompt_file, every_s=every_s,
                kind=args.kind, res=args.res, workspace=workspace, tier=args.tier,
                max_runtime_s=max_runtime, now=ctx.now(),
            )
    except ValueError as exc:
        return answer(ctx, str(exc), 1)
    every = backlog_lib.format_duration(row["every_s"])
    return answer(ctx, f"added backlog {row['name']}: every {every}, {row['kind']} {row['res']}, "
                       f"prompt copied to {row['prompt_path']}", 0)


def cmd_backlog_ls(args, ctx: Context) -> int:
    conn = db.connect(ctx.home)
    templates = backlog_lib.select_all(conn)
    now = ctx.now()
    for template in templates:
        last = backlog_lib.last_job(conn, template["name"])
        last_text = f"{last['id']} {last['state']} {last['verdict'] or '-'}" if last else "-"
        ctx.print(f"{template['name']:<24} every {backlog_lib.format_duration(template['every_s']):<4} "
                  f"{'enabled' if template['enabled'] else 'paused':<8} "
                  f"last {_when(template['last_submitted']):<17} "
                  f"next {_next_due(conn, template, now)}  job {last_text}")
    enabled = sum(1 for template in templates if template["enabled"])
    return answer(ctx, f"{len(templates)} backlog template(s), {enabled} enabled", 0)


def _set_paused(args, ctx: Context, enabled: bool) -> int:
    """Pause or resume one named template. With no name, every template and the filler at once."""
    conn = db.connect(ctx.home)
    state = "resumed" if enabled else "paused"
    if args.name is None:
        with db.tx(conn):
            for template in backlog_lib.select_all(conn):
                backlog_lib.set_enabled(conn, template["name"], enabled)
            filler_lib.set_paused(conn, not enabled)
        count = len(backlog_lib.select_all(conn))
        return answer(ctx, f"{state} all {count} backlog template(s) and the filler", 0)
    with db.tx(conn):
        found = backlog_lib.set_enabled(conn, args.name, enabled)
    if not found:
        return answer(ctx, f"no backlog template {args.name}", 1)
    return answer(ctx, f"{state} backlog {args.name}", 0)


def cmd_backlog_pause(args, ctx: Context) -> int:
    return _set_paused(args, ctx, False)


def cmd_backlog_resume(args, ctx: Context) -> int:
    return _set_paused(args, ctx, True)


def cmd_backlog_rm(args, ctx: Context) -> int:
    conn = db.connect(ctx.home)
    with db.tx(conn):
        removed = backlog_lib.remove(conn, ctx.home, args.name)
    if not removed:
        return answer(ctx, f"no backlog template {args.name}", 1)
    return answer(ctx, f"removed backlog {args.name}; its past jobs stay in the history", 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentq", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("submit", help="queue a prompt file")
    p.add_argument("prompt_file")
    p.add_argument("--kind", choices=db.KINDS, default="read")
    lane = p.add_mutually_exclusive_group()
    lane.add_argument("--urgent", action="store_true")
    lane.add_argument("--backlog", action="store_true")
    p.add_argument("--workspace")
    p.add_argument("--in-place", action="store_true",
                   help="a write job runs in the workspace itself, not in its own worktree")
    p.add_argument("--tier")
    p.add_argument("--label")
    p.add_argument("--res", choices=db.RESOURCES, default="light")
    p.add_argument("--max-runtime", type=float, default=db.MAX_RUNTIME_S, metavar="SECONDS",
                   help="kill the worker after this long (default 21600)")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("wait", help="block until the named jobs are terminal")
    p.add_argument("ids", nargs="*")
    p.add_argument("--mine", action="store_true")
    p.add_argument("--timeout", type=float, default=1800)
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("escalate", help="move jobs to the urgent lane")
    p.add_argument("id", nargs="?")
    p.add_argument("--mine", action="store_true", help="same as no argument")
    p.set_defaults(func=cmd_escalate)

    p = sub.add_parser("ls", help="list jobs")
    p.add_argument("--mine", action="store_true")
    p.add_argument("--all", action="store_true", help="every session (the default)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("result", help="report path and verdict of a job")
    p.add_argument("id")
    p.add_argument("--print", action="store_true", help="also print the report")
    p.set_defaults(func=cmd_result)

    p = sub.add_parser("cancel", help="drop a queued job or stop a running one")
    p.add_argument("id")
    p.set_defaults(func=cmd_cancel)

    sub.add_parser("daemon", help="run the broker loop (launchd runs this)").set_defaults(func=cmd_daemon)
    sub.add_parser("status", help="daemon, queue depth, running slots, proxy").set_defaults(func=cmd_status)
    inflight = sub.add_parser("inflight", help="DeepSeek in-flight limiter evidence").add_subparsers(dest="inflight_cmd", required=True)
    p = inflight.add_parser("report", help="throughput and latency per limiter arm, from proxy_minutes (read-only)")
    p.add_argument("--hours", type=float, default=4.0)
    p.set_defaults(func=cmd_inflight_report)
    sub.add_parser("slots", help="ssh and pytest slots held by pi workers, with holder pid and label").set_defaults(func=cmd_slots)

    p = sub.add_parser("backlog", help="recurring low-priority jobs that fill idle Morph capacity")
    bsub = p.add_subparsers(dest="backlog_cmd", required=True)
    b = bsub.add_parser("add", help="add a template: its prompt is resubmitted every interval")
    b.add_argument("name", help="lowercase letters, digits and dashes")
    b.add_argument("prompt_file")
    b.add_argument("--every", required=True, metavar="DURATION", help="interval: 30m, 6h or 1d")
    b.add_argument("--kind", choices=db.KINDS, default="read")
    b.add_argument("--res", choices=db.RESOURCES, default="light")
    b.add_argument("--workspace", help="folder the jobs run in (default the current directory)")
    b.add_argument("--tier", help="tier for every job (default by kind)")
    b.add_argument("--max-runtime", metavar="DURATION", help="wall-clock cap per job, e.g. 2h (default six hours)")
    b.set_defaults(func=cmd_backlog_add)
    b = bsub.add_parser("ls", help="templates with interval, state, last and next submission, last job")
    b.set_defaults(func=cmd_backlog_ls)
    b = bsub.add_parser("rm", help="delete a template (its past jobs stay)")
    b.add_argument("name")
    b.set_defaults(func=cmd_backlog_rm)
    b = bsub.add_parser("pause", help="stop submitting a template, or with no name every template and the filler; "
                                      "a running job finishes")
    b.add_argument("name", nargs="?", help="one template to pause; omit to pause all templates and the filler")
    b.set_defaults(func=cmd_backlog_pause)
    b = bsub.add_parser("resume", help="submit a paused template again; with no name, resume all templates and the filler")
    b.add_argument("name", nargs="?", help="one template to resume; omit to resume all templates and the filler")
    b.set_defaults(func=cmd_backlog_resume)
    return parser


def main(argv=None, env=None, out=None, now=time.time, ancestor=harness_ancestor) -> int:
    """Run one command and return its exit code. Injectable for tests."""
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    ctx = Context(os.environ if env is None else env, out or sys.stdout, now, ancestor)
    try:
        return args.func(args, ctx)
    except sqlite3.Error as exc:
        return answer(ctx, f"store unreachable: {exc}", 3)


if __name__ == "__main__":
    sys.exit(main())
