#!/usr/bin/env python3
"""Replay finished read and review jobs through two launcher code dirs, through the live proxy.

Usage: stage_ab.py --control-code DIR --treatment-code DIR [--jobs N] [--job-ids id,...]
                   [--concurrency 2] [--timeout-min 25] [--out DIR] [--dry-run]

Each code dir is an agent-dash root that holds ``scripts/launch_pi.py``: the control dir is the
live checkout and the treatment dir carries the launcher change under test. For every chosen job
and arm the harness makes a throwaway detached worktree of the job's repo at ``origin/main``, runs
that arm's launcher on the job's prompt in it, and reads the token totals the live proxy recorded
under the run's job key. Staging runs use their own AGENTQ_HOME, so the live broker's queue, slots
and guard logs are not touched; the traffic still goes through the live proxy, so the shared Morph
rate limits stay governed.

Writes, under ``--out`` (default ``~/.cache/agentq-staging/<run-id>/report``):

  results.tsv          one row per job and arm
  report.md            per-job comparison, totals and verdict agreement
  runs/<job8>-<arm>/   the launcher's run folder for that job and arm

Job keys in the proxy are ``stage-control-<job8>`` and ``stage-treatment-<job8>``. Token totals are
summed over the models a run used; ``total`` is input + cache_read + output, and ``avg ctx`` is
(input + cache_read) / requests.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

AGENTQ_DB = Path.home() / ".local" / "state" / "agentq" / "agentq.db"
STAGING_ROOT = Path.home() / ".cache" / "agentq-staging"
LABEL_CHECKER = Path.home() / ".claude" / "tools" / "agent-dash" / "agent_dash.py"
FORBIDDEN_TERMS = (
    "ssh ",
    "psql",
    "lake-vps",
    "lor-main",
    "git push",
    "gh pr",
    "lor_merge",
    "merge",
    "deploy",
    "systemctl",
    "--apply",
    "migrate",
)
JOB_KINDS = ("read", "review")
BACKLOG_LANE = "backlog"
ARMS = ("control", "treatment")
ARM_LETTER = {"control": "c", "treatment": "t"}
VERDICT_WORDS = ("BLOCKED", "PARTIAL", "PASS")
PROXY_SETTLE_S = 90
PROXY_POLL_S = 5
KILL_GRACE_S = 15
TIER_MODELS = {"pi-deepseek-flash": "morph/morph-dsv41flash-1017", "pi-kimi": "morph/morph-kimik31"}
DROPPED_ENV = (
    "AGENTQ_RUN",
    "AGENTQ_RESUME_SESSION",
    "AGENTQ_THINKING",
    "AGENTQ_TOOL_OUTPUT_CAP",
    "AGENTQ_JOB",
)


def job8(job_id):
    """The first eight characters of a queue job id, used in labels, folders and proxy keys."""
    return job_id[:8]


def proxy_key(arm, job_id):
    """The AGENTQ_JOB value (and proxy job key) of one arm's run of one job."""
    return f"stage-{arm}-{job8(job_id)}"


def build_label(arm, job_id):
    """The run label for one arm of one job: ``hub-review-stage-<job8>-<c|t>``."""
    return f"hub-review-stage-{job8(job_id)}-{ARM_LETTER[arm]}"


def tier_model(tier):
    """The model a pi tier runs, as the live broker passes it to launch_pi.py (workers.local.json and broker.TIER_FAMILY).

    None for a tier with no mapping, so the screen refuses it instead of falling back to the launcher default.
    """
    return TIER_MODELS.get(tier or "")


def forbidden_term(prompt_text):
    """The first forbidden term the prompt contains (case-insensitive), or None."""
    lowered = prompt_text.lower()
    for term in FORBIDDEN_TERMS:
        if term in lowered:
            return term
    return None


def is_git_checkout(path):
    """True when ``path`` is a directory inside a git work tree that has an ``origin/main`` ref."""
    if not path or not os.path.isdir(path):
        return False
    inside = subprocess.run(
        ["git", "-C", path, "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return False
    base = subprocess.run(
        ["git", "-C", path, "rev-parse", "--verify", "-q", "origin/main"], capture_output=True, text=True
    )
    return base.returncode == 0


def screen(row, is_checkout):
    """None when a job row can be replayed, else the reason it cannot."""
    if tier_model(row["tier"]) is None:
        return f"tier {row['tier']!r} has no model mapping"
    prompt = row["prompt_path"] or ""
    if not os.path.isfile(prompt):
        return "prompt file missing"
    if not is_checkout(row["workspace"] or ""):
        return "workspace is not a git checkout with origin/main"
    try:
        text = Path(prompt).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "prompt file unreadable"
    term = forbidden_term(text)
    if term is not None:
        return f"prompt mentions {term.strip()!r}"
    return None


def job_rows(conn):
    """Finished read and review jobs, most recent first."""
    marks = ",".join("?" for _ in JOB_KINDS)
    query = (
        "SELECT id, lane, kind, label, tier, workspace, prompt_path, submitted_at FROM jobs "
        f"WHERE state = 'done' AND kind IN ({marks}) ORDER BY submitted_at DESC, id"
    )
    return conn.execute(query, JOB_KINDS)


def choose_by_id(conn, job_ids, is_checkout):
    """The named jobs, in the order given. Refuses an unknown id or one the screen rejects."""
    conn.row_factory = sqlite3.Row
    chosen = []
    for job_id in job_ids:
        row = conn.execute(
            "SELECT id, lane, kind, label, tier, workspace, prompt_path, submitted_at FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no job {job_id} in agentq.db")
        reason = screen(row, is_checkout)
        if reason is not None:
            raise ValueError(f"job {job_id} refused: {reason}")
        chosen.append(_choice(row, "named with --job-ids"))
    return chosen


def _choice(row, reason):
    return {
        "id": row["id"],
        "lane": row["lane"],
        "kind": row["kind"],
        "label": row["label"],
        "tier": row["tier"],
        "model": tier_model(row["tier"]),
        "workspace": row["workspace"],
        "prompt_path": row["prompt_path"],
        "reason": reason,
    }


def choose_jobs(conn, count, is_checkout=is_git_checkout):
    """The most recent ``count`` replayable jobs, half from the backlog (filler audit) lane when available.

    Returns ``(choices, rejections)``: the chosen jobs with the reason each was picked, and a Counter of
    why recent jobs were skipped. Scans newest first and stops once both lanes have enough candidates.
    """
    conn.row_factory = sqlite3.Row
    backlog, other, rejections = [], [], Counter()
    for rank, row in enumerate(job_rows(conn), start=1):
        reason = screen(row, is_checkout)
        if reason is not None:
            rejections[reason] += 1
            continue
        pool = backlog if row["lane"] == BACKLOG_LANE else other
        if len(pool) < count:
            pool.append((rank, row))
        if len(backlog) >= count and len(other) >= count:
            break
    want_backlog = count // 2
    picked = [(rank, row, "filler audit (backlog lane), recent valid") for rank, row in backlog[:want_backlog]]
    other_take = other[: count - len(picked)]
    picked += [(rank, row, f"lane {row['lane']}, recent valid") for rank, row in other_take]
    spare = backlog[want_backlog : want_backlog + count - len(picked)]
    picked += [(rank, row, "backlog lane, other lanes exhausted") for rank, row in spare]
    picked.sort(key=lambda item: item[0])
    return [_choice(row, f"{why} #{rank}") for rank, row, why in picked], rejections


def build_command(code_dir, label, workspace, prompt_file, outdir, model, tier):
    """The launcher command line for one arm: the arm's own launch_pi.py with the job's tier model and tier.

    The same ``--model`` and ``--tier`` pair run_worker.py passes, so the run is not left to the launcher default.
    """
    launcher = Path(code_dir) / "scripts" / "launch_pi.py"
    return [sys.executable, str(launcher), label, str(workspace), str(prompt_file), str(outdir),
            "--model", model, "--tier", tier]


def parse_env_pairs(pairs):
    """``KEY=VALUE`` strings from --control-env / --treatment-env as a dict; a malformed pair raises ValueError."""
    out = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"expected KEY=VALUE, got {pair!r}")
        out[key] = value
    return out


def build_env(base, home, key, scratch, dash_state):
    """The child environment: a separate queue home and dash state, the arm's proxy key, and a scratch folder.

    Variables that would make the run a live broker launch, or change the launcher under test, are dropped.
    """
    env = {k: v for k, v in base.items() if k not in DROPPED_ENV}
    env["AGENTQ_HOME"] = str(home)
    env["AGENTQ_JOB"] = key
    env["AGENT_SCRATCH"] = str(scratch)
    env["AGENT_DASH_STATE_DIR"] = str(dash_state)
    return env


def worktree_command(workspace, path):
    """Create a detached worktree of the job's repo at origin/main."""
    return ["git", "-C", str(workspace), "worktree", "add", "--detach", str(path), "origin/main"]


def label_check(label):
    """(ok, text) from the repo's label checker; skipped with ok=True when the checker is not installed."""
    if not LABEL_CHECKER.is_file():
        return True, "label checker not installed; skipped"
    done = subprocess.run(
        [sys.executable, str(LABEL_CHECKER), "label", "check", "--label", label],
        capture_output=True,
        text=True,
    )
    return done.returncode == 0, (done.stdout + done.stderr).strip()


def read_verdict(outdir, label):
    """The verdict word on the first line of the run's final text, the same rule the launcher uses."""
    path = Path(outdir) / f"out-{label}.md"
    if not path.is_file():
        return "NONE"
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    first = lines[0].upper() if lines else ""
    for word in VERDICT_WORDS:
        if word in first:
            return word
    return "PARTIAL" if lines else "NONE"


def usage_of(rows):
    """Token totals for one proxy key from its per-model rows (each row is a mapping with the proxy columns)."""
    totals = {"requests": 0, "input": 0, "cache_read": 0, "output": 0}
    for row in rows:
        totals["requests"] += row["req"] or 0
        totals["input"] += row["input"] or 0
        totals["cache_read"] += row["cache_read"] or 0
        totals["output"] += row["output"] or 0
    totals["total"] = totals["input"] + totals["cache_read"] + totals["output"]
    requests = totals["requests"]
    totals["avg_ctx"] = round((totals["input"] + totals["cache_read"]) / requests, 1) if requests else None
    return totals


def proxy_rows(db_path, key):
    """The proxy_jobs rows for one key, read-only. Empty when the store or the key is absent."""
    if not Path(db_path).is_file():
        return []
    uri = f"file:{Path(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT model, req, input, cache_read, output FROM proxy_jobs WHERE job = ?", (key,)
        ).fetchall()
    finally:
        conn.close()


def compare(tasks, usage):
    """Per-job paired rows and the per-arm totals.

    ``tasks`` are the run results (dicts with job8, lane, arm, verdict, wall_s, status, key). ``usage`` maps a
    proxy key to its usage_of() totals. Returns (jobs, totals): jobs is one dict per job with both arms.
    """
    by_job = {}
    for task in tasks:
        by_job.setdefault(task["job8"], {})[task["arm"]] = task
    jobs = []
    for short, arms in sorted(by_job.items()):
        entry = {"job8": short, "lane": next(iter(arms.values()))["lane"]}
        for arm in ARMS:
            task = arms.get(arm, {})
            entry[arm] = {**task, **usage.get(task.get("key"), usage_of([]))}
        ctl, trt = entry["control"], entry["treatment"]
        entry["delta_total"] = trt["total"] - ctl["total"]
        entry["delta_requests"] = trt["requests"] - ctl["requests"]
        entry["agree"] = ctl.get("verdict") == trt.get("verdict")
        jobs.append(entry)
    totals = {}
    for arm in ARMS:
        arm_rows = [job[arm] for job in jobs]
        agg = usage_of(
            [{"req": r["requests"], "input": r["input"], "cache_read": r["cache_read"], "output": r["output"]} for r in arm_rows]
        )
        agg["runs"] = len(arm_rows)
        agg["wall_s"] = sum(r.get("wall_s", 0) for r in arm_rows)
        agg["verdicts"] = dict(Counter(r.get("verdict", "NONE") for r in arm_rows))
        totals[arm] = agg
    totals["agree"] = sum(1 for job in jobs if job["agree"])
    totals["paired"] = len(jobs)
    return jobs, totals


def results_rows(jobs):
    """Flat rows for results.tsv: one per job and arm."""
    header = [
        "job8", "job_id", "lane", "arm", "label", "status", "rc", "timed_out", "wall_s", "verdict",
        "requests", "input", "cache_read", "output", "total", "avg_ctx", "outdir",
    ]
    rows = [header]
    for job in jobs:
        for arm in ARMS:
            r = job[arm]
            if not r.get("label"):
                continue
            rows.append([
                job["job8"], r.get("job_id", ""), job["lane"], arm, r.get("label", ""), r.get("status", ""),
                r.get("rc", ""), r.get("timed_out", ""), round(r.get("wall_s", 0), 1), r.get("verdict", ""),
                r["requests"], r["input"], r["cache_read"], r["output"], r["total"],
                "" if r["avg_ctx"] is None else r["avg_ctx"], r.get("outdir", ""),
            ])
    return rows


def write_results(path, jobs):
    """results.tsv, tab-separated with a header row."""
    lines = ["\t".join(str(cell) for cell in row) for row in results_rows(jobs)]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pct(delta, base):
    return "n/a" if not base else f"{100.0 * delta / base:+.1f}%"


def render_report(run_id, meta, jobs, totals, choices, rejections):
    """report.md: the run setup, the per-arm totals, one row per job, and the verdict agreement."""
    out = [f"# Staging A/B run {run_id}", ""]
    out += [f"- control code: `{meta['control_code']}`", f"- treatment code: `{meta['treatment_code']}`"]
    out += [f"- jobs: {len(choices)}, concurrency {meta['concurrency']}, timeout {meta['timeout_min']} min per run"]
    out += ["- model per job: the job's tier model (see Selection)", ""]
    out += ["## Arm totals", "", "| arm | runs | requests | input | cache_read | output | total | avg ctx/req | wall min | verdicts |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for arm in ARMS:
        t = totals[arm]
        verdicts = ", ".join(f"{k} {v}" for k, v in sorted(t["verdicts"].items()))
        out.append(
            f"| {arm} | {t['runs']} | {t['requests']} | {t['input']} | {t['cache_read']} | {t['output']} | "
            f"{t['total']} | {t['avg_ctx'] if t['avg_ctx'] is not None else 'n/a'} | {t['wall_s'] / 60:.1f} | {verdicts} |"
        )
    tc, cc = totals["treatment"], totals["control"]
    out += ["", f"Treatment minus control: total {tc['total'] - cc['total']:+d} "
            f"({_pct(tc['total'] - cc['total'], cc['total'])}), requests {tc['requests'] - cc['requests']:+d}, "
            f"verdict agreement {totals['agree']} of {totals['paired']} paired jobs.", ""]
    out += ["## Per job", "", "| job | lane | req c/t | total c | total t | delta total | ctx/req c | ctx/req t | verdict c | verdict t | agree |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for job in jobs:
        c, t = job["control"], job["treatment"]
        out.append(
            f"| {job['job8']} | {job['lane']} | {c['requests']}/{t['requests']} | {c['total']} | {t['total']} | "
            f"{job['delta_total']:+d} ({_pct(job['delta_total'], c['total'])}) | {c['avg_ctx']} | {t['avg_ctx']} | "
            f"{c.get('verdict', 'NONE')} | {t.get('verdict', 'NONE')} | {'yes' if job['agree'] else 'no'} |"
        )
    out += ["", "## Selection", ""]
    out += [f"- {job8(c['id'])} ({c['lane']}, tier `{c['tier']}`, model `{c['model']}`): {c['reason']}" for c in choices]
    if rejections:
        out += ["", "Recent jobs skipped by the screen: " + "; ".join(f"{k} x{v}" for k, v in rejections.most_common())]
    missing = [f"{job['job8']} {arm}" for job in jobs for arm in ARMS
               if job[arm].get("label") and job[arm].get("requests") == 0]
    if missing:
        out += ["", "No proxy requests recorded for: " + ", ".join(missing) + " (check the run's err log)."]
    return "\n".join(out) + "\n"


def kill_group(pid):
    """SIGTERM the process group started with ``pid``, then SIGKILL it if it is still there after the grace period."""
    for sig, wait in ((signal.SIGTERM, KILL_GRACE_S), (signal.SIGKILL, 0)):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        if wait:
            time.sleep(wait)


def run_arm(argv, env, cwd, log_path, timeout_s):
    """Run one arm's launcher in the foreground, in its own process group, and kill the group at the timeout.

    Returns (rc, timed_out, wall_seconds). The launcher's stdout is kept in ``log_path``; stdin is closed.
    """
    started = time.time()
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            rc = proc.wait(timeout=timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            kill_group(proc.pid)
            rc = proc.wait()
            timed_out = True
    return rc, timed_out, time.time() - started


def summary_of(log_path):
    """The launcher's one summary line from its stdout, or an empty string."""
    path = Path(log_path)
    if not path.is_file():
        return ""
    for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        if line.startswith("launched pi "):
            return line
    return ""


def run_task(task, args, home, dash_state, out_dir, staging):
    """Make the worktree, run one arm on one job, and return the task's result dict."""
    short = job8(task["id"])
    arm = task["arm"]
    key = proxy_key(arm, task["id"])
    label = build_label(arm, task["id"])
    worktree = Path(staging) / f"{short}-{arm}"
    run_dir = Path(out_dir) / "runs" / f"{short}-{arm}"
    scratch = run_dir / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    result = {"job8": short, "job_id": task["id"], "lane": task["lane"], "arm": arm, "label": label, "key": key,
              "outdir": str(run_dir), "status": "", "rc": "", "timed_out": False, "wall_s": 0.0, "verdict": "NONE"}
    made = subprocess.run(worktree_command(task["workspace"], worktree), capture_output=True, text=True)
    if made.returncode != 0:
        result["status"] = "worktree failed: " + (made.stderr.strip().splitlines() or [""])[-1]
        return result
    argv = build_command(args.control_code if arm == "control" else args.treatment_code, label,
                         worktree, task["prompt_path"], run_dir, task["model"], task["tier"])
    env = build_env(os.environ, home, key, scratch, dash_state)
    env.update(parse_env_pairs(args.control_env if arm == "control" else args.treatment_env))
    rc, timed_out, wall = run_arm(argv, env, worktree, run_dir / "harness-stdout.txt", args.timeout_min * 60)
    result.update(rc=rc, timed_out=timed_out, wall_s=wall, verdict=read_verdict(run_dir, label))
    result["status"] = "timeout" if timed_out else ("done" if rc == 0 else f"exit {rc}")
    result["summary"] = summary_of(run_dir / "harness-stdout.txt")
    return result


def settle_usage(keys, db_path):
    """Usage per key, polling until every key shows requests or the settle window ends (the proxy flushes every 5 s)."""
    deadline = time.time() + PROXY_SETTLE_S
    usage = {}
    while True:
        usage = {key: usage_of(proxy_rows(db_path, key)) for key in keys}
        if all(usage[key]["requests"] for key in keys) or time.time() >= deadline:
            return usage
        time.sleep(PROXY_POLL_S)


def remove_worktrees(paths_by_workspace):
    """Remove the harness's own worktrees and prune each repo's stale entries."""
    for workspace, paths in paths_by_workspace.items():
        for path in paths:
            subprocess.run(["git", "-C", workspace, "worktree", "remove", "--force", str(path)],
                           capture_output=True, text=True)
        subprocess.run(["git", "-C", workspace, "worktree", "prune"], capture_output=True, text=True)


def parse_args(argv=None):
    """The harness command line."""
    parser = argparse.ArgumentParser(description="Replay finished jobs through two launcher code dirs.")
    parser.add_argument("--control-code", required=True, help="agent-dash root for the control arm")
    parser.add_argument("--treatment-code", required=True, help="agent-dash root for the treatment arm")
    parser.add_argument("--control-env", action="append", default=[], metavar="KEY=VALUE",
                        help="extra environment for control runs (repeatable)")
    parser.add_argument("--treatment-env", action="append", default=[], metavar="KEY=VALUE",
                        help="extra environment for treatment runs (repeatable), e.g. AGENTQ_TOOL_OUTPUT_CAP=8000")
    parser.add_argument("--jobs", type=int, default=4, help="how many jobs to replay (default 4)")
    parser.add_argument("--job-ids", default="", help="comma-separated job ids to replay instead of choosing")
    parser.add_argument("--arms", default="control,treatment", help="comma-separated arms to run (smoke runs use control)")
    parser.add_argument("--concurrency", type=int, default=2, help="runs at once (default 2)")
    parser.add_argument("--timeout-min", type=float, default=25, help="kill each run after this many minutes")
    parser.add_argument("--out", default="", help="report folder (default under ~/.cache/agentq-staging)")
    parser.add_argument("--dry-run", action="store_true", help="print the chosen jobs and commands; run nothing")
    return parser.parse_args(argv)


def selected_arms(args):
    """The arms named by --arms, in the order given; the caller has validated them."""
    return [a.strip() for a in args.arms.split(",") if a.strip()]


def print_plan(choices, staging, args, home, dash_state, out_dir):
    """Dry-run output: the chosen jobs, and for each job and arm the worktree, label check and launch command."""
    for choice in choices:
        print(f"job {choice['id']} lane={choice['lane']} kind={choice['kind']} tier={choice['tier']} "
              f"model={choice['model']} :: {choice['reason']}")
    print()
    for choice in choices:
        for arm in selected_arms(args):
            short = job8(choice["id"])
            label = build_label(arm, choice["id"])
            ok, text = label_check(label)
            run_dir = Path(out_dir) / "runs" / f"{short}-{arm}"
            code = args.control_code if arm == "control" else args.treatment_code
            model, tier = choice["model"], choice["tier"]
            env = build_env({}, home, proxy_key(arm, choice["id"]), run_dir / "scratch", dash_state)
            print(f"--- {short} {arm}: label {label} check {'ok' if ok else 'FAILED'} ({text})")
            print("  " + " ".join(worktree_command(choice["workspace"], Path(staging) / f"{short}-{arm}")))
            print("  env " + " ".join(f"{k}={env[k]}" for k in ("AGENTQ_HOME", "AGENTQ_JOB", "AGENT_SCRATCH", "AGENT_DASH_STATE_DIR")))
            print("  " + " ".join(build_command(code, label, Path(staging) / f"{short}-{arm}", choice["prompt_path"],
                                               run_dir, model, tier)))


def main(argv=None):
    """Choose jobs, run both arms of each, settle the proxy counts, write the results and the report."""
    args = parse_args(argv)
    if args.concurrency < 1 or args.jobs < 1:
        print("--jobs and --concurrency must be at least 1", file=sys.stderr)
        return 2
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    staging = STAGING_ROOT / run_id
    home = staging / "home"
    dash_state = staging / "dash-state"
    out_dir = Path(args.out).expanduser() if args.out else staging / "report"
    conn = sqlite3.connect(f"file:{AGENTQ_DB}?mode=ro", uri=True, timeout=10)
    try:
        if args.job_ids:
            choices = choose_by_id(conn, [j.strip() for j in args.job_ids.split(",") if j.strip()], is_git_checkout)
            rejections = Counter()
        else:
            choices, rejections = choose_jobs(conn, args.jobs)
    except ValueError as exc:
        print(f"stage_ab: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    if not choices:
        print("stage_ab: no replayable jobs found", file=sys.stderr)
        return 2
    if len(choices) < args.jobs and not args.job_ids:
        print(f"stage_ab: only {len(choices)} replayable jobs found, wanted {args.jobs}", file=sys.stderr)
    if args.dry_run:
        print_plan(choices, staging, args, home, dash_state, out_dir)
        return 0

    for path in (home, dash_state, out_dir / "runs"):
        path.mkdir(parents=True, exist_ok=True)
    arms = selected_arms(args)
    if not arms or any(a not in ARMS for a in arms):
        print(f"stage_ab: --arms must name {', '.join(ARMS)}", file=sys.stderr)
        return 2
    tasks = [{**choice, "arm": arm} for choice in choices for arm in arms]
    for task in tasks:
        ok, text = label_check(build_label(task["arm"], task["id"]))
        if not ok:
            print(f"stage_ab: label check failed: {text}", file=sys.stderr)
            return 2
    print(f"stage_ab run {run_id}: {len(tasks)} runs, concurrency {args.concurrency}", flush=True)
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(lambda t: run_task(t, args, home, dash_state, out_dir, staging), tasks))
    usage = settle_usage([r["key"] for r in results], AGENTQ_DB)
    jobs, totals = compare(results, usage)
    write_results(out_dir / "results.tsv", jobs)
    meta = {"control_code": args.control_code, "treatment_code": args.treatment_code,
            "concurrency": args.concurrency, "timeout_min": args.timeout_min}
    (out_dir / "report.md").write_text(render_report(run_id, meta, jobs, totals, choices, rejections), encoding="utf-8")
    (out_dir / "run.json").write_text(json.dumps({"run_id": run_id, "jobs": choices}, indent=2), encoding="utf-8")
    worktrees = {}
    for task in tasks:
        worktrees.setdefault(task["workspace"], []).append(staging / f"{job8(task['id'])}-{task['arm']}")
    remove_worktrees(worktrees)
    print(f"report: {out_dir / 'report.md'}")
    print(f"results: {out_dir / 'results.tsv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
