#!/usr/bin/env python3
"""Launch one registry worker and wait. A second call for the same label only waits.

Usage: run_worker.py <tier|default|auto> <label> <workspace> <prompt-file> <run-dir> [max-wait] [--no-preflight]

A fast doctor preflight (dashboard reachable, run dir visible, tier model
priced and policy-compliant) runs before launching; a failure prints the
problem and its fix and exits 1 without launching. --no-preflight skips it.

Every kind writes its manifest row with the tier it was launched under, so
the dashboard reads the registry tier name (``deepseek-flash``) instead of
guessing one from the model id; the row layout stays ``label, session_id,
epoch, model`` for the sources that parse it positionally, and a
``<run-dir>/<label>.meta.json`` beside it records tier, harness, model,
label, and pid for every launcher to read.

A quota/rate-limit failure (any ``quota_patterns`` match in the logs, e.g.
GLM's ``Rate limit reached for requests``) cools the tier down instead of
reporting a silent ``done``, and ``auto``/``default`` skip a cooling tier.
A ``pi`` tier whose registry entry names ``standby_env_file`` relabels the
launch as ``<name>-standby`` (its own tier and cooldown) and runs the
launcher with ``AGENT_DASH_PI_ENV_FILE`` set to that path, so the launcher
sources the standby credential instead of the primary one. The path is
only passed through; no key is ever read or printed here.

Prints one line:
STATUS: running|done|failed out=<report> err=<stderr> tier=<name> [reason=...]
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RUN_RULE = (
    "RUN RULE: this is one non-interactive turn. Keep calling tools until the task is complete, "
    "then end with the full final report as your last message (it is captured from stdout). "
    "A run that ends without the report is a failed run."
)
LABEL_OK = re.compile(r"^[\w.+-]{1,80}$")
KINDS = ("muse", "cursor", "grok-build", "pi", "codex")
READ_LIMIT = 262144
#: The dashboard can take several seconds to answer right after a deploy
#: restart (cold caches, polls holding the store lock), so the preflight
#: budget is generous; a timeout below warns instead of refusing.
PREFLIGHT_TIMEOUT_S = 10.0
STANDBY_SUFFIX = "-standby"

#: Harness that runs each launch kind. The dashboard row, the meta file, and
#: the registry all name it the way ``dash.models.harness_of`` does, so a
#: Grok Build run reads grok-cli (displayed "Grok Build") and never "other".
HARNESS_BY_KIND = {
    "muse": "muse",
    "cursor": "cursor",
    "grok-build": "grok-cli",
    "pi": "pi",
    "codex": "codex",
}


def dashboard_url() -> str:
    """Dashboard base URL for the launch preflight. Env wins."""
    return os.environ.get("AGENT_DASH_URL", "http://127.0.0.1:8765").rstrip("/") or "http://127.0.0.1:8765"


def harness_of(worker) -> str:
    """Harness name for a registry worker entry, or its kind."""
    kind = str((worker or {}).get("kind") or "")
    return HARNESS_BY_KIND.get(kind, kind or "generic")


def preflight_target(registry, requested: str, ctx):
    """Worker name to preflight, or None when the normal path reports it.

    Named tiers resolve directly; ``default`` follows the registry;
    ``auto`` takes the first eligible worker. Unknown tiers and an empty
    bench return None so the caller below emits its own reason.
    """
    workers = registry.get("workers") or {}
    if requested == "default":
        name = registry.get("default")
    elif requested == "auto":
        name = eligible(ctx, set())
    else:
        name = requested
    worker = workers.get(name) if isinstance(name, str) else None
    if not isinstance(worker, dict) or worker.get("kind") not in KINDS:
        return None
    return name


def run_preflight(registry, requested: str, ctx) -> int | None:
    """Fast local preflight before launching. None when clear, else exit 1.

    Checks the dashboard answers, the run dir will be visible to it, and
    the tier's model is priced and policy-compliant. A failure prints the
    problem and its fix and stops before anything launches. The dashboard
    leg is advisory: a slow or restarting dashboard (the endpoint can
    take seconds right after a deploy) prints a warning and the launch
    proceeds, because the dashboard collects evidence either way; every
    other problem (invisible run dir, unpriced or policy-breaking tier)
    still refuses.
    """
    from dash.doctor import preflight_for_launch

    target = preflight_target(registry, requested, ctx)
    if target is None:
        return None
    worker = (registry.get("workers") or {}).get(target) or {}
    problem = preflight_for_launch(
        launch_worker(target, worker, ctx),
        worker,
        str(ctx["run_dir"]),
        str(state_dir()),
        url=dashboard_url(),
        timeout_s=PREFLIGHT_TIMEOUT_S,
    )
    if problem is None:
        return None
    if problem.get("code") == "dashboard-down":
        print(
            f"preflight warning (dashboard-down): {problem['title']}. "
            f"{problem['detail']}. Launching anyway; the dashboard collects this run when it answers.",
            file=sys.stderr,
        )
        return None
    print(
        f"preflight ({problem['code']}): {problem['title']}. "
        f"{problem['detail']}. Fix: {problem['command']}",
        file=sys.stderr,
    )
    return 1


def run(argv, spawn=None, pgrep=None, sleep=None, now=None, alive=None, new_uuid=None) -> int:
    hooks = {
        "spawn": spawn or default_spawn,
        "pgrep": pgrep or pgrep_count,
        "sleep": sleep or time.sleep,
        "now": now or time.time,
        "alive": alive or pid_alive,
        "uuid": new_uuid or (lambda: str(uuid.uuid4())),
    }
    argv = list(argv)
    no_preflight = "--no-preflight" in argv
    argv = [token for token in argv if token != "--no-preflight"]
    if len(argv) < 5:
        print(
            "usage: run_worker.py <tier|default|auto> <label> <workspace> <prompt-file> <run-dir> [max-wait] [--no-preflight]",
            file=sys.stderr,
        )
        return 2
    requested, label, workspace, prompt_file, run_dir = argv[:5]
    try:
        max_wait = 540 if len(argv) < 6 or argv[5] == "" else int(argv[5])
    except ValueError:
        print("usage: run_worker.py ... [max-wait-seconds]", file=sys.stderr)
        return 2
    if not LABEL_OK.fullmatch(label):
        emit("failed", "", "", requested, "bad-label")
        return 2
    # A weak label launches nothing: refuse with the exact corrected
    # label before any spawn, manifest row, or registration. Replays of
    # old runs set AGENT_DASH_ALLOW_LEGACY_LABELS=1 for a warning instead.
    from dash import labels as label_lib

    verdict = label_lib.check(label)
    if not verdict["ok"]:
        if label_lib.legacy_allowed():
            print(f"label warning (legacy replay): {verdict['fix']}", file=sys.stderr)
        else:
            print(f"label refused: {verdict['fix']}", file=sys.stderr)
            emit("failed", "", "", requested, "bad-label")
            return 2
    elif verdict.get("warning"):
        print(verdict["warning"], file=sys.stderr)
    run_path = Path(run_dir)
    out = run_path / f"out-{label}.md"
    err = run_path / f"err-{label}.log"
    if not os.path.isdir(workspace) or not os.path.isfile(prompt_file):
        emit("failed", out, err, requested, "missing-workspace-or-prompt")
        return 2
    try:
        registry = load_registry()
    except (OSError, ValueError, json.JSONDecodeError):
        emit("failed", out, err, requested, "bad-registry")
        return 2
    run_path.mkdir(parents=True, exist_ok=True)
    _register_run_dir(run_path)
    tier_path = run_path / f"{label}.tier"
    pid_path = run_path / f"{label}.pid"
    ctx = {
        "registry": registry,
        "requested": requested,
        "label": label,
        "workspace": workspace,
        "prompt": prompt_file,
        "run_dir": run_path,
        "out": out,
        "err": err,
        "tier_path": tier_path,
        "pid_path": pid_path,
        "max_wait": max_wait,
        "hooks": hooks,
        "cooldowns": read_cooldowns(cooldown_path()),
        "minutes": _minutes(registry),
    }
    if tier_path.is_file():
        stored = tier_path.read_text(encoding="utf-8").strip() or requested
        return observe(ctx, stored, set())
    if not no_preflight:
        blocked = run_preflight(registry, requested, ctx)
        if blocked is not None:
            return blocked
    name, reason = pick_initial(ctx)
    if not name:
        code = 0 if reason == "none-available" else 2
        emit("failed", out, err, requested, reason)
        return code
    return launch(ctx, name, set())


def pick_initial(ctx):
    requested = ctx["requested"]
    registry = ctx["registry"]
    if requested == "auto":
        name = eligible(ctx, set())
        return (name, None) if name else (None, "none-available")
    workers = registry.get("workers") or {}
    name = registry.get("default") if requested == "default" else requested
    worker = workers.get(name) if isinstance(name, str) else None
    if not isinstance(worker, dict) or worker.get("kind") not in KINDS:
        return None, "unknown-tier"
    if not worker.get("enabled", True):
        return None, "disabled"
    return name, None


def worker_at(name):
    """Registry name behind a launch name: a standby name reads its base tier.

    ``<tier>-standby`` names the same registry entry as ``<tier>``; only a
    ``pi`` entry that declares ``standby_env_file`` is ever launched this
    way, so the standby name always reads the same model, flags, and cap.
    """
    text = str(name or "")
    if text.endswith(STANDBY_SUFFIX) and len(text) > len(STANDBY_SUFFIX):
        return text[: -len(STANDBY_SUFFIX)]
    return text


def worker_for(ctx, name):
    """Registry entry for a launch name, standby names included."""
    return (ctx["registry"].get("workers") or {}).get(worker_at(name)) or {}


def launch(ctx, name, tried) -> int:
    worker = worker_for(ctx, name)
    tier = launch_worker(name, worker, ctx)
    session_id = "-" if worker.get("kind") == "cursor" else ctx["hooks"]["uuid"]()
    ctx["tier_path"].write_text(tier + "\n", encoding="utf-8")
    append_manifest(
        worker,
        ctx["run_dir"],
        ctx["label"],
        session_id,
        ctx["hooks"]["now"](),
        worker.get("model") or "",
        tier=tier,
        harness=harness_of(worker),
    )
    cmd = command(
        worker, ctx["label"], ctx["workspace"], ctx["prompt"], session_id, ctx["run_dir"], tier
    )
    try:
        pid = ctx["hooks"]["spawn"](cmd, ctx["out"], ctx["err"], env=launch_env(worker))
    except OSError:
        ctx["tier_path"].unlink(missing_ok=True)
        emit("failed", ctx["out"], ctx["err"], tier, "spawn")
        return 2
    ctx["pid_path"].write_text(str(int(pid)) + "\n", encoding="utf-8")
    write_meta(ctx, tier, worker, pid)
    return observe(ctx, tier, tried)


def launch_worker(name, worker, ctx=None) -> str:
    """Tier a launch records: the registry name, or its standby relabel.

    A ``pi`` tier with ``standby_env_file`` launches on the standby
    credential and records itself as ``<tier>-standby``, so a primary
    rate-limit cooldown never blocks the standby run (and vice versa).
    Every other tier records its registry name unchanged, which is what
    the dashboard shows for a Grok Build run.
    """
    if standby_file(worker) and str((worker or {}).get("kind") or "") == "pi":
        return name + STANDBY_SUFFIX
    return name


def standby_file(worker):
    """The registry's ``standby_env_file`` path, or None. Never read, only passed."""
    value = (worker or {}).get("standby_env_file")
    if not isinstance(value, str) or not value.strip():
        return None
    return os.path.expanduser(value.strip())


def launch_env(worker):
    """Extra env for the child launcher: the standby file, when declared."""
    path = standby_file(worker)
    if path is None or str((worker or {}).get("kind") or "") != "pi":
        return None
    return {"AGENT_DASH_PI_ENV_FILE": path}


def write_meta(ctx, tier, worker, pid) -> None:
    """``<label>.meta.json``: tier, harness, model, label, pid, and out/err paths.

    Written for every launch so a reader never has to guess a tier from a
    model id. Best effort: a failed write never breaks a launch.
    """
    meta = {
        "label": ctx["label"],
        "tier": tier,
        "harness": harness_of(worker),
        "model": worker.get("model") or "",
        "pid": int(pid),
        "out_path": str(ctx["out"]),
        "err_path": str(ctx["err"]),
        "started_at": int(ctx["hooks"]["now"]()),
    }
    path = Path(ctx["run_dir"]) / f"{ctx['label']}.meta.json"
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(meta, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return


def observe(ctx, tier, tried) -> int:
    name = worker_at(tier)
    worker = (ctx["registry"].get("workers") or {}).get(name) or {}
    pid = read_pid(ctx["pid_path"])
    if pid and still_running(pid, ctx["max_wait"], ctx["hooks"]):
        emit("running", ctx["out"], ctx["err"], tier)
        return 0
    result = outcome(worker, ctx["out"], ctx["err"])
    if result != "quota" and quota_file_hit(ctx):
        result = "quota"
    if result != "quota":
        if result == "done":
            emit("done", ctx["out"], ctx["err"], tier)
        else:
            emit("failed", ctx["out"], ctx["err"], tier, "empty-report")
        return 0
    note_quota(ctx, tier)
    if ctx["requested"] not in ("auto", "default"):
        emit("failed", ctx["out"], ctx["err"], tier, "quota")
        return 0
    tried.add(tier)
    nxt = eligible(ctx, tried)
    if not nxt:
        emit("failed", ctx["out"], ctx["err"], tier, "quota")
        return 0
    archive(ctx["run_dir"], ctx["label"], tier)
    return launch(ctx, nxt, tried)


def eligible(ctx, skip):
    """First ``auto_order`` tier under its cap and not cooling, or None.

    ``skip`` holds launch names already tried this call (standby names
    included). A cooling tier is passed over in favour of the next one:
    the whole point of a quota cooldown is to stop relaunching into the
    limit. A ``pi`` tier whose next launch would use its standby
    credential is judged under the standby name and the standby tier's
    cooldown, since that is the attempt that would actually run.
    """
    registry = ctx["registry"]
    now = ctx["hooks"]["now"]()
    workers = registry.get("workers") or {}
    for name in registry.get("auto_order") or []:
        worker = workers.get(name)
        if not isinstance(worker, dict) or not worker.get("enabled", True):
            continue
        if worker.get("kind") not in KINDS:
            continue
        launch_name = launch_worker(name, worker, ctx)
        if name in skip or launch_name in skip:
            continue
        if cooling(ctx["cooldowns"], launch_name, now):
            continue
        try:
            cap = int(worker.get("cap"))
        except (TypeError, ValueError):
            continue
        pattern = worker.get("pgrep") or ""
        count = ctx["hooks"]["pgrep"](pattern) if pattern else 0
        if count >= cap:
            continue
        return launch_name
    return None


def note_quota(ctx, name):
    ctx["cooldowns"][name] = ctx["hooks"]["now"]() + ctx["minutes"] * 60
    write_cooldowns(cooldown_path(), ctx["cooldowns"])


def outcome(worker, out_path, err_path):
    """``quota``, ``done``, or ``empty`` for a finished worker's logs.

    A quota/rate-limit hit always wins, even when the out file has text:
    a run that reported ``Rate limit reached for requests`` did not do the
    task, and counting it as ``done`` hides the limit.
    """
    if quota_hit(worker, out_path, err_path):
        return "quota"
    try:
        if Path(out_path).is_file() and Path(out_path).stat().st_size > 0:
            return "done"
    except OSError:
        pass
    return "empty"


def quota_file_hit(ctx) -> bool:
    """True when a launcher QUOTA marker names this label from this attempt.

    ``launch_pi.py`` writes a shared ``QUOTA`` file (``model`` and
    ``label`` lines) and ``launch_codex.py`` writes ``QUOTA-<label>``:
    both land beside the launcher's outdir, which the log-pattern scan
    cannot see when the worker's quota error never reaches the report
    tails (an empty report with a quota marker is a quota hit, not an
    empty report). Only a marker naming this label and written after
    this attempt's tier file counts, so a previous attempt's marker (or
    another label sharing the outdir) never cools this launch.
    """
    label = ctx.get("label") or ""
    try:
        tier_mtime = Path(ctx["tier_path"]).stat().st_mtime
    except OSError:
        return False
    base = Path(ctx["run_dir"])
    candidates = [
        base / "QUOTA",
        base / "pi" / "QUOTA",
        base / f"QUOTA-{label}",
        base / "pi" / f"QUOTA-{label}",
        base / "codex" / f"QUOTA-{label}",
    ]
    for path in candidates:
        try:
            if path.stat().st_mtime < tier_mtime:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            parts = raw.strip().split("\t")
            if len(parts) == 2 and parts[0].strip() == "label" and parts[1].strip() == label:
                return True
    return False


def quota_hit(worker, out_path, err_path) -> bool:
    patterns = worker.get("quota_patterns") or []
    if not patterns:
        return False
    text = read_tail(err_path) + "\n" + read_tail(out_path)
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern:
            continue
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def command(worker, label, workspace, prompt_file, session_id, run_dir=None, tier=None):
    """The exact argv for one worker launch.

    ``pi`` runs ``launch_pi.py`` with the run folder's ``pi/`` outdir, so a
    pi tier launches through the one launcher every other tier uses and
    the pi source finds its manifest, stream, and out files where it
    already looks for them. ``codex`` runs ``launch_codex.py`` (headless
    ``codex exec``) in the run folder itself, so the manifests source
    finds its ``codex-sessions.tsv`` row beside ``out-<label>.md``; the
    launcher's one summary line goes to stderr, never stdout, so the
    report file it writes is never clobbered.
    """
    kind = worker.get("kind")
    model = str(worker.get("model") or "")
    extra = [str(arg) for arg in (worker.get("extra_args") or [])]
    if kind == "muse":
        return [
            "muse",
            "exec",
            "--model",
            model,
            "--workspace",
            workspace,
            "--prompt-file",
            prompt_file,
            "--session-id",
            session_id,
            *extra,
        ]
    if kind == "cursor":
        return [str(ROOT / "scripts" / "launch_grok.sh"), label, workspace, prompt_file, model]
    if kind == "pi":
        return [
            "python3",
            str(ROOT / "scripts" / "launch_pi.py"),
            label,
            workspace,
            prompt_file,
            str(Path(run_dir or Path(prompt_file).parent) / "pi"),
            "--model",
            model,
            "--tier",
            str(tier or worker.get("tier") or ""),
            *extra,
        ]
    if kind == "codex":
        return [
            "python3",
            str(ROOT / "scripts" / "launch_codex.py"),
            label,
            workspace,
            prompt_file,
            str(run_dir or Path(prompt_file).parent),
            "--model",
            model,
            "--tier",
            str(tier or worker.get("tier") or ""),
            *extra,
        ]
    text = Path(prompt_file).read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    text = text + "\n" + RUN_RULE
    return [
        "grok",
        "-p",
        text,
        "-m",
        model,
        "-s",
        session_id,
        "--permission-mode",
        "bypassPermissions",
        "--cwd",
        workspace,
        "--output-format",
        "plain",
        *extra,
    ]


# Manifest wiring by launcher kind. Every file named here is scanned by the
# dashboard collectors; a worker entry that names no manifest/columns of
# its own still lands in its kind's TSV with its model recorded.
MANIFEST_BY_KIND = {
    "muse": ("muse-sessions.tsv", ["label", "session_id", "epoch", "model"]),
    "cursor": ("cursor-sessions.tsv", ["label", "session_id", "epoch", "model"]),
    "grok-build": ("glm-sessions.tsv", ["label", "session_id", "epoch", "model"]),
    "pi": ("pi-sessions.tsv", ["label", "session_id", "epoch", "model"]),
    "codex": ("codex-sessions.tsv", ["label", "session_id", "epoch", "model"]),
}


def append_manifest(worker, run_dir, label, session_id, epoch, model, tier="", harness=""):
    """Append one manifest row in the worker's declared column order.

    The declared columns are written first, exactly as before, and only
    then ``tier`` and ``harness`` when the caller passes them. Sources read
    the leading columns positionally, so a newer row never breaks an older
    reader, and the dashboard takes the tier from the row instead of
    guessing it from the model id.
    """
    # Defense in depth: no manifest row for a weak label. ``run()``
    # refuses first; this raises on any direct misuse.
    from dash import labels as label_lib

    verdict = label_lib.check(label)
    if not verdict["ok"] and not label_lib.legacy_allowed():
        raise ValueError(verdict["fix"])
    default_name, default_cols = MANIFEST_BY_KIND.get(
        worker.get("kind") or "", ("sessions.tsv", ["label", "session_id", "epoch"])
    )
    name = str(worker.get("manifest") or default_name)
    cols = [str(col) for col in (worker.get("columns") or default_cols)]
    values = {
        "label": label,
        "session_id": session_id,
        "session": session_id,
        "epoch": str(int(epoch)),
        "model": str(model or ""),
        "tier": str(tier or ""),
        "harness": str(harness or ""),
    }
    for extra in ("tier", "harness"):
        if values[extra] and extra not in cols:
            cols.append(extra)
    line = "\t".join(str(values.get(col, "")) for col in cols)
    path = Path(run_dir) / name
    prefix = ""
    if path.is_file() and path.stat().st_size > 0:
        with path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                prefix = "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(prefix + line + "\n")


def archive(run_dir, label, tier):
    for src_name, dst_name in (
        (f"out-{label}.md", f"out-{label}.{tier}.md"),
        (f"err-{label}.log", f"err-{label}.{tier}.log"),
    ):
        src = Path(run_dir) / src_name
        if src.exists():
            os.replace(src, Path(run_dir) / dst_name)


def still_running(pid, max_wait, hooks) -> bool:
    deadline = hooks["now"]() + max_wait
    while hooks["alive"](pid) and hooks["now"]() < deadline:
        remain = deadline - hooks["now"]()
        hooks["sleep"](10 if remain > 10 else remain)
    return bool(hooks["alive"](pid))


def emit(state, out, err, tier, reason=None):
    line = f"STATUS: {state} out={out} err={err} tier={tier}"
    if reason:
        line += f" reason={reason}"
    print(line)


def load_registry():
    from dash.locate import layered_path

    path = layered_path("workers", "AGENT_DASH_WORKERS")
    if path is None or not path.is_file():
        raise FileNotFoundError("workers registry")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("workers"), dict):
        raise ValueError("workers registry")
    return data


def _register_run_dir(run_path: Path) -> None:
    """Record the run folder so the dashboard scans it wherever it lives.

    Best effort: registration must never break a launch.
    """
    try:
        from dash.rundirs import register_run_dir

        register_run_dir(state_dir(), run_path)
    except Exception:
        pass


def state_dir() -> Path:
    from dash.locate import expand

    env = os.environ.get("AGENT_DASH_STATE_DIR")
    if env:
        return Path(expand(env))
    return Path.home() / ".local" / "share" / "agent-dash"


def cooldown_path() -> Path:
    return state_dir() / "worker-cooldowns.json"


def read_cooldowns(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    found = {}
    for key, value in data.items():
        try:
            found[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return found


def write_cooldowns(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _minutes(registry) -> float:
    try:
        return float(registry.get("cooldown_minutes", 30))
    except (TypeError, ValueError):
        return 30.0


def cooling(cooldowns, name, now) -> bool:
    until = cooldowns.get(name)
    try:
        return until is not None and now < float(until)
    except (TypeError, ValueError):
        return False


def read_pid(path: Path):
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def read_tail(path) -> str:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    if len(data) > READ_LIMIT:
        data = data[-READ_LIMIT:]
    return data.decode("utf-8", "replace")


def pid_alive(pid) -> bool:
    """True while the worker runs. One shared helper lives in dash.liveness."""
    from dash.liveness import pid_alive as _alive

    return _alive(pid)


def pgrep_count(pattern: str) -> int:
    if not pattern:
        return 0
    cmd = ["pgrep", "-f"]
    if pattern.startswith("-"):
        cmd.append("--")
    cmd.append(pattern)
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if proc.returncode not in (0, 1):
        return 0
    return len([line for line in proc.stdout.splitlines() if line.strip()])


def spawn_env(extra=None):
    """Parent env plus a launcher's extra variables (a standby env-file path)."""
    if not extra:
        return None
    env = dict(os.environ)
    env.update({str(key): str(value) for key, value in extra.items()})
    return env


def default_spawn(cmd, out_path, err_path, env=None):
    """Start one worker detached. ``env`` names extra variables, not a full env."""
    out_path = Path(out_path)
    err_path = Path(err_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as out, err_path.open("wb") as err:
        proc = subprocess.Popen(
            list(cmd),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
            env=spawn_env(env),
        )
    return proc.pid


def main(argv=None) -> int:
    return run(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
