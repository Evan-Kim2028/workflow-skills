#!/usr/bin/env python3
"""Drive workstream lanes with registry workers. No Claude wrapper.

usage: drive_lanes.py <lanes.json> [--only TASK] [--start build|rev|fix] [--findings FILE]

The JSON file is the same object workflows/ws-wave.js takes. Prompt sentences are
copied from that script. tests/test_drive_lanes.py checks they still match.

Each lane writes <runDir>/drive-<task>.json. The summary on stdout matches the
ws-wave result shape. Exit 0 when nothing needs the lead, 3 when something does.
This process does not push, open a pull request, merge, or touch production.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_worker  # noqa: E402

LAUNCH_MAX_WAIT = 15
POLL_SECONDS = 5
REPORT_LIMIT = 262144
FINDINGS_LIMIT = 4000
SUMMARY_LIMIT = 1200
PROMPT_FINDINGS_LIMIT = 20000
DEFAULT_TRAILER = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
VERDICT_TOKENS = (
    "APPROVE_WITH_NITS",
    "APPROVE_WITH_CHANGES",
    "REQUEST_CHANGES",
    "APPROVE",
    "PARTIAL",
    "BLOCKED",
    "PASS",
)
BUILD_OK = ("PASS", "PARTIAL")

CODE_SEARCH = "Code search: CodeGraph first (query the main checkout "
DIFF_BASE = "The diff base is "
NO_PROD = "Do not push, do not open a PR, do not merge, do not touch production."
BUILD_REPORT = (
    "Report: first line PASS, PARTIAL or BLOCKED; then files changed, "
    "the test command with pass/fail counts, and what was not done."
)
REVIEW_OPEN = "READ-ONLY adversarial review, round "
VERDICT_LINE = (
    'Output exactly: first line "VERDICT: APPROVE" or "VERDICT: APPROVE_WITH_NITS" '
    'or "VERDICT: REQUEST_CHANGES"; then numbered findings "severity - file:line - problem - fix". '
    "Only REQUEST_CHANGES for real defects, not style."
)
MUST_CHECK = (
    "You MUST check and report each of these, each with evidence (command plus output, or file:line):"
)
CHECK_FAILED = (
    "A check without evidence counts as failed. If any check fails or lacks evidence, "
    "the verdict is REQUEST_CHANGES."
)
FIX_OPEN = "Address every finding below that is a real defect; say which you reject and why."
FIX_PUSH = "Do not push or open a PR."
FIX_REPORT = (
    "Report: first line PASS, PARTIAL or BLOCKED; then what changed per finding, and the test result."
)
TESTS_TOUCHED = "the explicit test files for the code you touched"
TESTS_DIFF = "the test files the diff touches"

KEY_SENTENCES = (
    CODE_SEARCH,
    DIFF_BASE,
    NO_PROD,
    BUILD_REPORT,
    REVIEW_OPEN,
    VERDICT_LINE,
    MUST_CHECK,
    CHECK_FAILED,
    FIX_OPEN,
    FIX_PUSH,
    FIX_REPORT,
    TESTS_TOUCHED,
    TESTS_DIFF,
)


def build_task(lane, ws, repo, diff_base, trailer):
    tests = lane.get("tests") or TESTS_TOUCHED
    return "\n\n".join(
        [
            (
                f"Workstream {ws}, task {lane['task']} ({lane['slug']}). "
                f"Write scope: only the worktree {lane['worktree']} on branch {lane['branch']}. "
                f"{CODE_SEARCH}{repo}, read files in the worktree)."
            ),
            f"{DIFF_BASE}{diff_base}.",
            str(lane.get("brief") or ""),
            (
                f"Tests: run exactly {tests} and nothing wider. Commit on {lane['branch']} "
                f'with a clear message ending in the line "{trailer}". {NO_PROD}'
            ),
            BUILD_REPORT,
        ]
    )


def review_task(lane, round_n, ws, diff_base):
    tests = lane.get("tests") or TESTS_DIFF
    lines = [
        (
            f"{REVIEW_OPEN}{round_n}, of branch {lane['branch']} in worktree {lane['worktree']} "
            f"against {diff_base}, for workstream {ws} task {lane['task']} ({lane['slug']}). "
            "Do not edit, commit or push."
        ),
        f"The task was:\n{lane.get('brief') or ''}",
        (
            "Check: correctness against the task; nothing outside its scope; repo rules in AGENTS.md "
            "(no inline # comments in Python, one writer per table, no new Iceberg gold writer); "
            f"tests are meaningful and pass. Re-run: {tests}."
        ),
        VERDICT_LINE,
    ]
    focus = focus_of(lane)
    if focus:
        numbered = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(focus))
        lines.append(f"{MUST_CHECK}\n{numbered}\n{CHECK_FAILED}")
    return "\n\n".join(lines)


def fix_task(lane, round_n, ws, findings, trailer):
    tests = lane.get("tests") or TESTS_TOUCHED
    return "\n\n".join(
        [
            (
                f"Fix round {round_n} for workstream {ws} task {lane['task']} ({lane['slug']}) "
                f"on branch {lane['branch']} in worktree {lane['worktree']}. {FIX_OPEN}"
            ),
            findings,
            (
                f'Tests: {tests}. Commit on the same branch (message ending with "{trailer}"). {FIX_PUSH}'
            ),
            FIX_REPORT,
        ]
    )


def focus_of(lane):
    raw = lane.get("reviewFocus") if isinstance(lane, dict) else None
    items = raw if isinstance(raw, list) else ([] if raw is None or raw == "" else [raw])
    return [str(item).strip() for item in items if str(item).strip()]


def deps_of(lane):
    raw = lane.get("deps") if isinstance(lane, dict) else None
    if raw is None or raw == "":
        return []
    items = raw if isinstance(raw, list) else [raw]
    return [str(item) for item in items if str(item)]


def base_dep_id(lane):
    raw = str(lane.get("base") or "") if isinstance(lane, dict) else ""
    if not raw.startswith("dep:"):
        return ""
    return raw[4:]


def dep_ids(lane):
    ids = deps_of(lane)
    base_id = base_dep_id(lane)
    if base_id and base_id not in ids:
        ids.append(base_id)
    return ids


def diff_base(lane, by_task):
    dep = base_dep_id(lane)
    if not dep:
        raw = lane.get("base")
        if raw is None or raw == "":
            return "origin/main"
        return str(raw)
    other = by_task.get(dep) or {}
    branch = other.get("branch")
    return str(branch) if branch else "origin/main"


def label_base(ws, lane):
    return f"{str(ws).lower()}-{lane['task']}-{lane['slug']}"


def validate(spec):
    if not isinstance(spec, dict):
        raise ValueError("drive_lanes needs a JSON object")
    missing = [
        key
        for key in ("ws", "repo", "runDir", "template", "runner", "dash", "lanes")
        if not spec.get(key)
    ]
    if missing or not isinstance(spec.get("lanes"), list) or not spec["lanes"]:
        raise ValueError("drive_lanes needs {ws, repo, runDir, template, runner, dash, lanes[]}")
    template = Path(str(spec["template"]))
    if not template.is_file():
        raise ValueError(f"drive_lanes template not found: {template}")
    lanes = spec["lanes"]
    by_task = {}
    for lane in lanes:
        if not isinstance(lane, dict) or not lane.get("task"):
            raise ValueError("drive_lanes lane is missing task")
        task = str(lane["task"])
        if task in by_task:
            raise ValueError(f"drive_lanes duplicate task {task}")
        if not lane.get("slug") or not lane.get("branch") or not lane.get("worktree"):
            raise ValueError(f"drive_lanes lane {task} needs slug, branch, and worktree")
        if lane.get("oneWay") and not focus_of(lane):
            raise ValueError(f"drive_lanes lane {task} is oneWay and needs reviewFocus")
        by_task[task] = lane
    for lane in lanes:
        for dep in dep_ids(lane):
            if dep not in by_task:
                raise ValueError(f"drive_lanes lane {lane['task']} depends on unknown task {dep}")
        base_id = base_dep_id(lane)
        if base_id and base_id in by_task and not by_task[base_id].get("branch"):
            raise ValueError(f"drive_lanes lane {lane['task']} base dep:{base_id} has no branch")
    seen = {}

    def visit(task, stack):
        state = seen.get(task, 0)
        if state == 1:
            raise ValueError(f"drive_lanes dependency cycle: {' -> '.join(stack + [task])}")
        if state == 2:
            return
        seen[task] = 1
        for dep in dep_ids(by_task[task]):
            visit(dep, stack + [task])
        seen[task] = 2

    for lane in lanes:
        visit(str(lane["task"]), [])
    return by_task


def max_fix_rounds(spec):
    raw = spec.get("maxFixRounds")
    if raw is None or raw == "":
        return 2
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 2
    return value if value >= 0 else 0


def trailer_of(spec):
    text = spec.get("trailer")
    return str(text) if text else DEFAULT_TRAILER


def build_tier(lane, spec):
    return str(lane.get("tier") or spec.get("defaultTier") or "default")


def review_tier(lane, spec):
    if lane.get("reviewTier"):
        return str(lane["reviewTier"])
    if spec.get("reviewTier"):
        return str(spec["reviewTier"])
    return build_tier(lane, spec)


def fix_tier(lane, spec):
    if lane.get("fixTier"):
        return str(lane["fixTier"])
    if spec.get("fixTier"):
        return str(spec["fixTier"])
    return build_tier(lane, spec)


def model_of(lane, kind):
    if kind == "rev":
        return str(lane.get("reviewModel") or "")
    if kind == "fix":
        return str(lane.get("fixModel") or "")
    return str(lane.get("model") or "")


def tier_of(lane, spec, kind):
    if kind == "rev":
        return review_tier(lane, spec)
    if kind == "fix":
        return fix_tier(lane, spec)
    return build_tier(lane, spec)


def wants_review(lane):
    if lane.get("oneWay"):
        return True
    return lane.get("review") is not False


def first_verdict(text):
    for line in text.splitlines():
        stripped = line.strip().strip("*").replace("VERDICT:", "").strip().strip("*").strip()
        if not stripped:
            continue
        upper = stripped.upper()
        for token in VERDICT_TOKENS:
            if upper.startswith(token):
                return token
        return "none"
    return "none"


def read_report(path):
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    if len(data) > REPORT_LIMIT:
        data = data[:REPORT_LIMIT]
    return data.decode("utf-8", "replace")


def parse_status(stdout):
    found = None
    for line in str(stdout or "").splitlines():
        text = line.strip()
        if not text.startswith("STATUS:"):
            continue
        parts = text.split()
        state = parts[1] if len(parts) > 1 else "failed"
        fields = {"state": state, "out": "", "err": "", "tier": "", "model": "", "reason": ""}
        for part in parts[2:]:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key in fields:
                fields[key] = value
        found = fields
    if found is None:
        return {"state": "failed", "out": "", "err": "", "tier": "", "model": "", "reason": "no-status"}
    return found


def read_pid(path):
    try:
        return int(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def label_taken(run_dir, label):
    root = Path(run_dir)
    return (root / f"{label}.tier").is_file() or (root / f"out-{label}.md").is_file()


def next_free(run_dir, base, kind):
    n = 1
    while label_taken(run_dir, f"{base}-{kind}{n}"):
        n += 1
        if n > 1000:
            raise RuntimeError(f"no free {kind} label for {base}")
    return n


def worker_alive(pid_path, pattern, hooks):
    pid = read_pid(pid_path)
    if pid:
        return bool(hooks["pid_alive"](pid))
    if pattern:
        return int(hooks["pgrep"](pattern) or 0) > 0
    return False


def wait_worker(pid_path, pattern, hooks):
    while worker_alive(pid_path, pattern, hooks):
        hooks["sleep"](POLL_SECONDS)


class TierGate:
    def __init__(self, registry, pgrep):
        self.registry = registry if isinstance(registry, dict) else {}
        self.pgrep = pgrep
        self.inflight = {}
        self.cv = threading.Condition()

    def name_for(self, requested):
        if requested in ("", "auto"):
            return None
        workers = self.registry.get("workers") or {}
        name = self.registry.get("default") if requested == "default" else requested
        if not isinstance(name, str) or not isinstance(workers.get(name), dict):
            return None
        return name

    def pattern(self, name, model):
        worker = (self.registry.get("workers") or {}).get(name) or {}
        use = model or str(worker.get("model") or "")
        return run_worker.cap_pattern(worker, use)

    def acquire(self, name, model):
        if not name:
            return
        worker = (self.registry.get("workers") or {}).get(name) or {}
        try:
            cap = int(worker.get("cap"))
        except (TypeError, ValueError):
            cap = 1
        if cap < 1:
            cap = 1
        pattern = self.pattern(name, model)
        with self.cv:
            while True:
                live = int(self.pgrep(pattern) or 0) if pattern else 0
                if self.inflight.get(name, 0) < cap and live < cap:
                    self.inflight[name] = self.inflight.get(name, 0) + 1
                    return
                self.cv.wait(timeout=POLL_SECONDS)

    def release(self, name):
        if not name:
            return
        with self.cv:
            current = self.inflight.get(name, 0)
            self.inflight[name] = current - 1 if current > 0 else 0
            self.cv.notify_all()


def prompt_file_text(label, template, task):
    body = str(template or "").strip("\n")
    task_text = str(task or "").strip("\n")
    return f"[label: {label}]\n\n{body}\n\n{task_text}\n"


def git_result(hooks, argv):
    if any(part in ("push", "merge", "rebase", "commit") for part in argv):
        raise RuntimeError("drive_lanes does not push, merge, rebase, or commit")
    result = hooks["git"](argv)
    if isinstance(result, tuple):
        code = int(result[0])
        text = "" if len(result) < 2 or result[1] is None else str(result[1])
        return code, text
    return int(result), ""


def git_call(hooks, argv):
    code, _text = git_result(hooks, argv)
    return code


def commit_log(worktree, ref, hooks):
    code, text = git_result(
        hooks,
        ["git", "-C", str(worktree), "log", "--oneline", f"{ref}..HEAD"],
    )
    if code != 0:
        return ""
    return text.strip()


def ensure_worktree(spec, lane, ref, hooks):
    worktree = str(lane["worktree"])
    if os.path.isdir(worktree):
        return True
    repo = str(spec["repo"])
    branch = str(lane["branch"])
    if ref == "origin/main":
        if git_call(hooks, ["git", "-C", repo, "fetch", "-q", "origin", "main"]) != 0:
            return False
    rc = git_call(hooks, ["git", "-C", repo, "worktree", "add", "-b", branch, worktree, ref])
    if rc != 0:
        rc = git_call(hooks, ["git", "-C", repo, "worktree", "add", worktree, branch])
    return rc == 0


def mark(spec, lane, status, hooks):
    argv = ["python3", str(spec["dash"]), "ws", "task", str(spec["ws"]), str(lane["task"]), status]
    try:
        hooks["dash"](argv)
    except OSError as exc:
        print(f"{lane['task']}: status {status} failed: {exc}", file=sys.stderr)


def step_row(status, verdict, report, tier, model, findings, commits=""):
    published = "none" if verdict in ("", "none", "empty") else verdict
    return {
        "status": status,
        "verdict": published,
        "summary": report[:SUMMARY_LIMIT],
        "findings": findings[:FINDINGS_LIMIT],
        "commits": commits,
        "tier": tier,
        "model": model,
    }


def launch(spec, lane, kind, round_n, task, gate, hooks, by_task):
    requested = tier_of(lane, spec, kind)
    model = model_of(lane, kind)
    base = label_base(spec["ws"], lane)
    label = base if kind == "build" else f"{base}-{kind}{round_n}"
    run_dir = Path(str(spec["runDir"]))
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = run_dir / f"{label}.md"
    template = spec.get("_template") or ""
    prompt_path.write_text(prompt_file_text(label, template, task), encoding="utf-8")
    gate_name = gate.name_for(requested)
    gate.acquire(gate_name, model)
    try:
        argv = [
            str(spec["runner"]),
            requested,
            label,
            str(lane["worktree"]),
            str(prompt_path),
            str(run_dir),
            str(LAUNCH_MAX_WAIT),
            model,
        ]
        pid_path = run_dir / f"{label}.pid"
        pattern = gate.pattern(gate_name, model) if gate_name else ""
        parsed = {"state": "failed", "out": "", "err": "", "tier": "", "model": "", "reason": "no-status"}
        while True:
            _code, stdout, _err = hooks["runner"](argv)
            parsed = parse_status(stdout)
            if parsed["state"] != "running":
                break
            wait_worker(pid_path, pattern, hooks)
        out_path = parsed["out"] or str(run_dir / f"out-{label}.md")
        report = read_report(out_path)
        if not report.strip():
            status, verdict = "failed", "empty"
        elif parsed["state"] == "failed":
            status, verdict = "failed", first_verdict(report)
        else:
            status, verdict = "done", first_verdict(report)
        findings = report[:PROMPT_FINDINGS_LIMIT] if kind == "rev" else ""
        commits = commit_log(lane["worktree"], diff_base(lane, by_task), hooks)
        row = step_row(status, verdict, report, parsed["tier"], parsed["model"], findings, commits)
        row["_report"] = report
        row["_label"] = label
        return row
    finally:
        gate.release(gate_name)


def blank_lane(spec, lane):
    return {
        "task": lane.get("task", ""),
        "label": label_base(spec["ws"], lane) if lane.get("slug") else "",
        "tier": "",
        "model": "",
        "branch": lane.get("branch", ""),
        "worktree": lane.get("worktree", ""),
        "build": None,
        "rounds": [],
        "runs": [],
        "final": "unreviewed",
    }


def run_record(role, row, round_n=None):
    item = {"role": role, "tier": row.get("tier") or "", "model": row.get("model") or ""}
    if round_n is not None:
        item["round"] = round_n
    return item


def public_row(row):
    if not row:
        return None
    return {key: row[key] for key in ("status", "verdict", "summary", "findings", "commits", "tier", "model")}


def bad_step(row, ok_tokens):
    if row.get("status") != "done":
        return True
    return row.get("verdict") not in ok_tokens


def finish_lane(spec, lane, start, findings, gate, hooks, by_task):
    out = blank_lane(spec, lane)
    ref = diff_base(lane, by_task)
    if start == "build":
        mark(spec, lane, "wip", hooks)
    if not ensure_worktree(spec, lane, ref, hooks):
        out["final"] = "build-failed"
        out["error"] = "worktree"
        return out
    ws = spec["ws"]
    trailer = trailer_of(spec)
    base = out["label"]
    run_dir = Path(str(spec["runDir"]))
    if start == "build":
        built = launch(
            spec, lane, "build", 0, build_task(lane, ws, spec["repo"], ref, trailer), gate, hooks, by_task
        )
        out["build"] = public_row(built)
        out["tier"] = built.get("tier") or ""
        out["model"] = built.get("model") or ""
        out["runs"].append(run_record("build", built))
        if bad_step(built, BUILD_OK) or not str(built.get("commits") or "").strip():
            out["final"] = "build-failed"
            return out
        if not wants_review(lane):
            out["final"] = "built"
            return out
    elif start == "fix":
        n = next_free(run_dir, base, "fix")
        fixed = launch(spec, lane, "fix", n, fix_task(lane, n, ws, findings, trailer), gate, hooks, by_task)
        out["rounds"].append({"round": n, "fix": public_row(fixed)})
        out["runs"].append(run_record("fix", fixed, n))
        if bad_step(fixed, BUILD_OK):
            out["final"] = "fix-failed"
            return out
    limit = max_fix_rounds(spec)
    attempt = 0
    while True:
        attempt += 1
        n = next_free(run_dir, base, "rev")
        reviewed = launch(
            spec, lane, "rev", n, review_task(lane, n, ws, ref), gate, hooks, by_task
        )
        round_row = {"round": n, "review": public_row(reviewed)}
        out["rounds"].append(round_row)
        out["runs"].append(run_record("review", reviewed, n))
        verdict = reviewed.get("verdict") or "none"
        if reviewed.get("status") == "done" and verdict.startswith("APPROVE"):
            mark(spec, lane, "pr", hooks)
            out["final"] = verdict
            return out
        if reviewed.get("status") != "done" or verdict in ("none", "BLOCKED"):
            out["final"] = "review-failed"
            return out
        if verdict != "REQUEST_CHANGES":
            out["final"] = "review-failed"
            return out
        if attempt > limit:
            out["final"] = "needs-lead"
            return out
        fn = next_free(run_dir, base, "fix")
        report = reviewed.get("_report") or reviewed.get("findings") or ""
        fixed = launch(
            spec, lane, "fix", fn, fix_task(lane, fn, ws, report[:PROMPT_FINDINGS_LIMIT], trailer), gate, hooks, by_task
        )
        round_row["fix"] = public_row(fixed)
        out["runs"].append(run_record("fix", fixed, fn))
        if bad_step(fixed, BUILD_OK):
            out["final"] = "fix-failed"
            return out


def succeeded(row):
    final = str((row or {}).get("final") or "")
    return final == "built" or final.startswith("APPROVE")


def skipped_result(spec, lane, dependency):
    out = blank_lane(spec, lane)
    out["final"] = "skipped-dep"
    out["dependency"] = dependency
    return out


def error_result(spec, lane, exc):
    out = blank_lane(spec, lane)
    out["final"] = "build-failed"
    out["error"] = str(exc)
    return out


def write_lane_file(spec, row):
    run_dir = Path(str(spec["runDir"]))
    run_dir.mkdir(parents=True, exist_ok=True)
    task = str(row.get("task") or "lane")
    safe = task.replace("/", "_")
    path = run_dir / f"drive-{safe}.json"
    path.write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
    return path


def summarize(ws, rows):
    approved = []
    needs = []
    built = []
    skipped = []
    for row in rows:
        final = str(row.get("final") or "")
        if final.startswith("APPROVE"):
            approved.append(
                {"task": row["task"], "branch": row.get("branch", ""), "worktree": row.get("worktree", "")}
            )
        elif final == "built":
            built.append({"task": row["task"], "branch": row.get("branch", "")})
        elif final == "skipped-dep":
            skipped.append(
                {"task": row["task"], "dependency": row.get("dependency", ""), "branch": row.get("branch", "")}
            )
        else:
            last = row["rounds"][-1] if row.get("rounds") else row.get("build")
            needs.append(
                {"task": row["task"], "final": final, "branch": row.get("branch", ""), "last": last}
            )
    return {
        "ws": ws,
        "approved": approved,
        "needsLead": needs,
        "built": built,
        "skipped": skipped,
        "lanes": rows,
    }


def load_registry(hooks):
    provided = hooks.get("registry")
    if isinstance(provided, dict):
        return provided
    try:
        return run_worker.load_registry()
    except (OSError, ValueError, json.JSONDecodeError):
        return {"workers": {}}


def default_runner(argv):
    proc = subprocess.run(list(argv), check=False, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def default_git(argv):
    proc = subprocess.run(list(argv), check=False, capture_output=True, text=True)
    return proc.returncode, proc.stdout


def default_dash(argv):
    proc = subprocess.run(list(argv), check=False, capture_output=True, text=True)
    return proc.returncode


def default_hooks():
    return {
        "runner": default_runner,
        "git": default_git,
        "dash": default_dash,
        "sleep": time.sleep,
        "pid_alive": run_worker.pid_alive,
        "pgrep": run_worker.pgrep_count,
        "registry": None,
    }


def drive(spec, only="", start="build", findings="", hooks=None):
    if not isinstance(spec, dict):
        raise ValueError("drive_lanes needs a JSON object")
    spec = dict(spec)
    by_task = validate(spec)
    spec["_template"] = Path(str(spec["template"])).read_text(encoding="utf-8")
    lanes = list(spec["lanes"])
    if only:
        lanes = [lane for lane in lanes if str(lane.get("task")) == only]
        if not lanes:
            raise ValueError(f"drive_lanes has no task {only}")
    hooks = dict(default_hooks() if hooks is None else hooks)
    for key, value in default_hooks().items():
        hooks.setdefault(key, value)
    Path(str(spec["runDir"])).mkdir(parents=True, exist_ok=True)
    gate = TierGate(load_registry(hooks), hooks["pgrep"])
    wait_deps = not only
    results = {}
    lock = threading.Lock()
    events = {str(lane["task"]): threading.Event() for lane in lanes}

    def run_thread(lane):
        task = str(lane["task"])
        try:
            row = run_one(lane)
        except Exception as exc:
            row = error_result(spec, lane, exc)
            print(f"{task}: {exc}", file=sys.stderr)
        with lock:
            results[task] = row
        try:
            write_lane_file(spec, row)
        finally:
            print(f"{task}: {row.get('final')}", file=sys.stderr)
            events[task].set()

    def run_one(lane):
        if wait_deps:
            for dep in dep_ids(lane):
                events[dep].wait()
                with lock:
                    dep_row = results[dep]
                if not succeeded(dep_row):
                    print(f"{lane['task']}: skipped-dep {dep_row.get('task') or dep}", file=sys.stderr)
                    return skipped_result(spec, lane, dep_row.get("task") or dep)
        return finish_lane(spec, lane, start, findings, gate, hooks, by_task)

    threads = [
        threading.Thread(target=run_thread, args=(lane,), name=f"lane-{lane['task']}") for lane in lanes
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ordered = [results[str(lane["task"])] for lane in lanes]
    summary = summarize(spec["ws"], ordered)
    code = 3 if summary["needsLead"] else 0
    return summary, code


def main(argv=None, hooks=None) -> int:
    parser = argparse.ArgumentParser(description="Drive workstream lanes with registry workers.")
    parser.add_argument("lanes", help="JSON file of ws-wave args")
    parser.add_argument("--only", default="", help="drive this task id only")
    parser.add_argument("--start", default="build", choices=("build", "rev", "fix"))
    parser.add_argument("--findings", default="", help="findings file for --start fix")
    args = parser.parse_args(argv)
    if args.start != "build" and not args.only:
        print("drive_lanes: --start needs --only", file=sys.stderr)
        return 2
    findings = ""
    if args.findings and args.findings != "-":
        path = Path(args.findings)
        if not path.is_file():
            print(f"drive_lanes: findings file not found: {path}", file=sys.stderr)
            return 2
        findings = path.read_text(encoding="utf-8")
    elif args.start == "fix":
        print("drive_lanes: --start fix needs --findings", file=sys.stderr)
        return 2
    try:
        spec = json.loads(Path(args.lanes).read_text(encoding="utf-8"))
        summary, code = drive(spec, only=args.only, start=args.start, findings=findings, hooks=hooks)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"drive_lanes: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
