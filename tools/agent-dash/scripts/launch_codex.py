#!/usr/bin/env python3
"""Run one Codex (``codex exec``) headless worker.

Usage: launch_codex.py <label> <workspace> <prompt_file> <outdir> [--model M] [--reasoning-effort E] [--tier T]
       launch_codex.py --doctor [--check]

Runs one labelled ``codex exec --ephemeral --json`` worker on the given
model (default ``gpt-6-luna``) with the given reasoning effort (default
``xhigh``) and reports it to the dashboard. ``--ephemeral`` keeps the
thread out of ``~/.codex/sessions`` so the rollout collector never sees
it: the only dashboard rows for this run are the ones written here, and
tokens are counted exactly once. The label is validated with the label
library and refused with the exact fix on a bad one, and the prompt gets
a ``[label: <label>]`` first line when it lacks one (the prompt is copied
to ``<outdir>/prompt-<label>.md`` with the prefix, so the original is
never modified). ``stdin`` carries the prompt: it never appears in argv.
``run_worker.py`` passes the registry tier name with ``--tier``. The one
summary line goes to stderr, never stdout, so a parent launcher
redirecting stdout into the report file cannot clobber it.

Writes, in ``outdir``:

  <outdir>/codex-sessions.tsv      label, session_id, started epoch, model, tier
  <outdir>/stream-<label>.jsonl    raw ``--json`` event stream
  <outdir>/out-<label>.md          final assistant text (``-o``)
  <outdir>/err-<label>.log         stderr
  <outdir>/running/<label>         "<pid>\\t<model>" while the run is live
  <outdir>/QUOTA-<label>           only on quota/rate-limit exhaustion
  <state-dir>/generic.jsonl        one start, one usage per assistant
                                   turn, one finish line (run id
                                   ``codex:<session>:<label>``)

Rate-limit/quota exhaustion finishes ``died_early`` with verdict
``BLOCKED`` and writes ``QUOTA-<label>`` naming the model and label, so
``run_worker.py`` can cool the tier down and fall over to the next one.
``outdir`` is registered in ``run-dirs.txt`` like ``run_worker.py`` does.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import labels as label_lib  # noqa: E402
from dash.sources.manifests import parent_session_from_pad  # noqa: E402

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_EFFORT = "xhigh"

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: Quota/rate-limit markers in stderr or stream text.
_QUOTA_WORDS = ("rate limit", "ratelimit", "quota", "429", "usage limit",
                "insufficient", "credit", "payment required", "overloaded")


def tier_of(model: str | None) -> str:
    """Live tile bucket for a Codex model: always ``codex``."""
    return "codex"


def validate_label(label: str) -> str | None:
    """Return a one-line fix when ``label`` is weak, else None."""
    try:
        verdict = label_lib.check(label)
    except Exception:
        return None
    if verdict.get("ok"):
        return None
    return verdict.get("fix") or "use a ws<N>-<plan|exec|review|coord>-<slug> label"


def ensure_label_prefix(prompt: str, label: str) -> str:
    """Prepend ``[label: <label>]`` when the prompt lacks it."""
    if prompt.startswith(f"[label: {label}]"):
        return prompt
    return f"[label: {label}]\n{prompt}"


def quota_hit(text: str) -> bool:
    """True when ``text`` names rate-limit/quota exhaustion."""
    low = (text or "").lower()
    return any(word in low for word in _QUOTA_WORDS)


def verdict_of(text: str) -> str:
    """Worker verdict from the first line of the final text."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    first = lines[0].upper() if lines else ""
    for word in ("BLOCKED", "PARTIAL", "PASS"):
        if word in first:
            return word
    return "PARTIAL"


def parse_stream(raw: bytes) -> dict:
    """Thread id, per-turn usage rows, and final text from a ``--json`` stream.

    ``codex exec --json`` prints one object per line: ``thread.started``
    carries the thread id, each ``turn.completed`` carries that turn's
    token usage, and each ``item.completed`` with an ``agent_message``
    item carries final text. Anything unparseable is skipped.
    """
    thread_id: str | None = None
    usage: list[dict] = []
    texts: list[str] = []
    turn = 0
    for line in (raw or b"").split(b"\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "thread.started" and thread_id is None:
            tid = obj.get("thread_id")
            if isinstance(tid, str) and tid:
                thread_id = tid
        elif kind == "turn.completed" and isinstance(obj.get("usage"), dict):
            turn += 1
            usage.append({"message_id": f"turn-{turn}", **obj["usage"]})
        elif kind == "item.completed":
            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
    return {"thread_id": thread_id, "usage": usage, "text": "\n\n".join(texts).strip()}


def state_dir() -> Path:
    from dash.locate import expand

    env = os.environ.get("AGENT_DASH_STATE_DIR")
    if env:
        return Path(expand(env))
    return Path.home() / ".local" / "share" / "agent-dash"


def register_run_dir(run_dir: Path) -> None:
    """Best effort: registration must never break a launch."""
    try:
        from dash.rundirs import register_run_dir as register

        register(state_dir(), run_dir)
    except Exception:
        pass


def parent_session_of(outdir) -> str | None:
    """Lead Claude session that launched this worker, or None.

    The outdir under a session scratchpad encodes it
    (``.../<session>/scratchpad``); otherwise the launcher's own
    session env (``CLAUDE_SESSION_ID``, else ``CLAUDE_CODE_SESSION_ID``)
    names the launching session. Only a uuid-shaped value is ever
    returned, so a stray env value never misattributes the run.
    """
    try:
        parent = parent_session_from_pad(str(outdir))
    except Exception:
        parent = None
    if parent:
        return parent
    for env in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        value = (os.environ.get(env) or "").strip()
        if value and _UUID.match(value):
            return value
    return None


def report_lines(state: Path, lines) -> None:
    """Append generic.jsonl lines. Best effort; never stores prompts."""
    try:
        state.mkdir(parents=True, exist_ok=True)
        with open(state / "generic.jsonl", "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line, separators=(",", ":"), ensure_ascii=True) + "\n")
    except OSError:
        pass


def codex_binary() -> str | None:
    """The ``codex`` binary, or None when it is not on PATH."""
    found = shutil.which("codex")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/codex", "/usr/local/bin/codex"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


CODEX_SANDBOX_ENV = "AGENT_DASH_CODEX_SANDBOX"
CODEX_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")


def codex_config_path() -> Path:
    """The Codex config file: ``$CODEX_HOME/config.toml`` or ``~/.codex/config.toml``."""
    home = (os.environ.get("CODEX_HOME") or "").strip()
    base = Path(home).expanduser() if home else Path.home()
    return (base / "config.toml") if home else (base / ".codex" / "config.toml")


def codex_config_value(path, key: str) -> str | None:
    """One top-level string value from a Codex ``config.toml``, or None.

    A small ``key = "value"`` scan, not a TOML parser: only keys before
    the first ``[table]`` header count, so a profile's ``sandbox_mode``
    never leaks into the default. Enough for the launcher's flat keys (``sandbox_mode``, ``model``,
    ``model_reasoning_effort``) without a new dependency.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    text = re.split(r"^\s*\[", text, maxsplit=1, flags=re.M)[0]
    for quote in ('"', "'"):
        match = re.search(rf'^{re.escape(key)}\s*=\s*{quote}([^"\']*){quote}', text, re.M)
        if match:
            return match.group(1).strip()
    return None


def codex_sandbox_with_source() -> tuple:
    """Effective sandbox mode and where it came from.

    Order: ``AGENT_DASH_CODEX_SANDBOX`` wins; else the ``sandbox_mode``
    from the Codex config when it is a valid mode; else
    ``workspace-write``. An unknown value at any level falls through to
    the next one.
    """
    env_value = (os.environ.get(CODEX_SANDBOX_ENV) or "").strip()
    if env_value in CODEX_SANDBOXES:
        return env_value, f"env {CODEX_SANDBOX_ENV}"
    path = codex_config_path()
    config_value = codex_config_value(path, "sandbox_mode")
    if config_value in CODEX_SANDBOXES:
        return config_value, f"config {path}"
    return "workspace-write", "default"


def codex_sandbox() -> str:
    """Sandbox mode for headless runs (see ``codex_sandbox_with_source``)."""
    mode, _ = codex_sandbox_with_source()
    return mode


#: Markers that a workspace-write run was fenced in by its sandbox.
_SANDBOX_DENIED_WORDS = (
    "operation not permitted",
    "network is unreachable",
    "temporary failure",
    "could not resolve host",
    "failed to connect",
    "connection refused",
    "connection timed out",
    "ssh:",
)

SANDBOX_HINT = (
    "hint: workspace-write sandbox blocks git worktrees, commits, ssh and "
    "network; retry with AGENT_DASH_CODEX_SANDBOX=danger-full-access"
)


def sandbox_hint_needed(text: str) -> bool:
    """True when ``text`` looks like a sandbox denial under workspace-write."""
    low = (text or "").lower()
    return any(word in low for word in _SANDBOX_DENIED_WORDS)


def build_argv(binary, workspace, out_path, model, effort, sandbox=None):
    """The exact headless codex command line (prompt travels on stdin)."""
    return [
        binary,
        "exec",
        "--ephemeral",
        "--json",
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-C",
        str(workspace),
        "-s",
        sandbox or codex_sandbox(),
        "-c",
        'approval_policy="never"',
        "--skip-git-repo-check",
        "-o",
        str(out_path),
        "-",
    ]


def codex_version(binary: str) -> str:
    """One-line ``codex --version`` output, or ``unknown``."""
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    line = (proc.stdout or proc.stderr or "").strip().splitlines()
    return line[0].strip() if line and line[0].strip() else "unknown"


def doctor_lines() -> list:
    """Human-readable launcher checks. Prints only: never calls a model."""
    binary = codex_binary()
    path = codex_config_path()
    sandbox, source = codex_sandbox_with_source()
    try:
        found = path.is_file()
    except OSError:
        found = False
    lines = [
        f"codex: {binary or 'not found on PATH'}",
        f"version: {codex_version(binary) if binary else 'unknown'}",
        f"model: {DEFAULT_MODEL}",
        f"effort: {DEFAULT_EFFORT}",
        f"config: {path} ({'found' if found else 'missing'})",
        f"sandbox: {sandbox} (source: {source})",
    ]
    if sandbox == "workspace-write":
        lines.append(
            "warning: workspace-write sandbox blocks git worktrees, commits, "
            "ssh and network; set AGENT_DASH_CODEX_SANDBOX=danger-full-access "
            "for worker runs that need them."
        )
    return lines


def run_doctor() -> int:
    """Print the launcher checks and exit 0."""
    print("\n".join(doctor_lines()))
    return 0


def usage_row(entry: dict) -> dict:
    """Token counts from one ``turn.completed`` usage object.

    The Codex ledger mirrors the rollout collector: ``input_tokens``
    holds the whole prompt including the cached slice, so the stored
    ``input`` is the uncached miss and ``cache_read`` the hit.
    """
    try:
        total = int(entry.get("input_tokens") or 0)
    except (TypeError, ValueError):
        total = 0
    try:
        cached = int(entry.get("cached_input_tokens") or 0)
    except (TypeError, ValueError):
        cached = 0
    try:
        cache_write = int(entry.get("cache_write_input_tokens") or 0)
    except (TypeError, ValueError):
        cache_write = 0
    try:
        output = int(entry.get("output_tokens") or 0)
    except (TypeError, ValueError):
        output = 0
    try:
        reasoning = int(entry.get("reasoning_output_tokens") or 0)
    except (TypeError, ValueError):
        reasoning = 0
    return {
        "input": max(0, total - cached),
        "output": output,
        "cache_read": cached,
        "cache_write": cache_write,
        "reasoning": reasoning,
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(token in ("--doctor", "--check", "doctor", "check") for token in argv):
        return run_doctor()
    model = DEFAULT_MODEL
    effort = DEFAULT_EFFORT
    tier = None
    rest = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--model" and index + 1 < len(argv):
            model = argv[index + 1]
            index += 2
        elif token.startswith("--model="):
            model = token.split("=", 1)[1]
            index += 1
        elif token == "--reasoning-effort" and index + 1 < len(argv):
            effort = argv[index + 1]
            index += 2
        elif token.startswith("--reasoning-effort="):
            effort = token.split("=", 1)[1]
            index += 1
        elif token == "--tier" and index + 1 < len(argv):
            tier = argv[index + 1].strip() or None
            index += 2
        elif token.startswith("--tier="):
            tier = token.split("=", 1)[1].strip() or None
            index += 1
        else:
            rest.append(token)
            index += 1
    if len(rest) != 4:
        print(
            "usage: launch_codex.py <label> <workspace> <prompt_file> <outdir> [--model M] [--reasoning-effort E] [--tier T]",
            file=sys.stderr,
        )
        return 2
    label, workspace, prompt_file, outdir = rest
    tier = tier or tier_of(model)
    fix = validate_label(label)
    if fix is not None:
        print(f"label refused: {fix}", file=sys.stderr)
        return 2
    binary = codex_binary()
    if binary is None:
        print("codex binary not found on PATH", file=sys.stderr)
        return 2

    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "running").mkdir(parents=True, exist_ok=True)
    register_run_dir(out)

    prompt_text = Path(prompt_file).read_text(encoding="utf-8")
    local_prompt = out / f"prompt-{label}.md"
    local_prompt.write_text(ensure_label_prefix(prompt_text, label), encoding="utf-8")
    prompt_bytes = (local_prompt.read_text(encoding="utf-8") + "\n").encode("utf-8")

    final_path = out / f"out-{label}.md"
    stream = out / f"stream-{label}.jsonl"
    err = out / f"err-{label}.log"
    sandbox, sandbox_source = codex_sandbox_with_source()
    started = int(time.time())
    run_id = f"running/{label}"
    (out / run_id).write_text(f"{os.getpid()}\t{model}", encoding="utf-8")
    if final_path.exists():
        try:
            final_path.unlink()
        except OSError:
            pass
    with open(stream, "w") as so, open(err, "a") as se:
        se.write(
            f"--- codex launch label {label} model {model} "
            f"sandbox {sandbox} (source: {sandbox_source}) at {started}\n"
        )
        se.flush()
        rc = subprocess.run(
            build_argv(binary, workspace, final_path, model, effort, sandbox),
            cwd=str(Path(workspace).expanduser()),
            input=prompt_bytes,
            stdout=so,
            stderr=se,
        ).returncode
    try:
        os.unlink(out / run_id)
    except FileNotFoundError:
        pass

    raw = stream.read_bytes() if stream.exists() else b""
    err_text = err.read_text(encoding="utf-8", errors="replace")[-8192:] if err.exists() else ""
    parsed = parse_stream(raw)
    sid = parsed["thread_id"] or str(uuid.uuid4())
    text = ""
    if final_path.is_file():
        try:
            text = final_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    if not text.strip():
        text = parsed["text"]
        if text.strip():
            try:
                final_path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
            except OSError:
                pass
    ended = int(time.time())

    tsv = out / "codex-sessions.tsv"
    with open(tsv, "a", encoding="utf-8") as fh:
        if tsv.stat().st_size == 0:
            fh.write("label\tsession_id\tstarted_epoch\tmodel\ttier\n")
        fh.write(f"{label}\t{sid}\t{started}\t{model}\t{tier}\n")

    if quota_hit(err_text) or quota_hit(raw.decode("utf-8", "replace")):
        (out / f"QUOTA-{label}").write_text(f"model\t{model}\nlabel\t{label}\n", encoding="utf-8")
        verdict, status = "BLOCKED", "died_early"
    else:
        verdict = verdict_of(text)
        status = "done" if rc == 0 and text.strip() else "died_early"

    parent = parent_session_of(out)
    run_id_full = f"codex:{sid}:{label}"
    lines = [
        {
            "type": "run",
            "label": label,
            "run_id": run_id_full,
            "model": model,
            "tier": tier,
            "session_id": sid,
            "parent_session_id": parent,
            "status": "running",
            "started_at": float(started),
        }
    ]
    for entry in parsed["usage"]:
        counts = usage_row(entry)
        lines.append(
            {
                "type": "usage",
                "session_id": sid,
                "run_id": run_id_full,
                "model": model,
                "tool": "codex",
                "message_id": entry["message_id"],
                "ts": float(ended),
                "input": counts["input"],
                "output": counts["output"],
                "cache_read": counts["cache_read"],
                "cache_write": counts["cache_write"],
                "reasoning": counts["reasoning"],
            }
        )
    lines.append(
        {
            "type": "run",
            "label": label,
            "run_id": run_id_full,
            "model": model,
            "tier": tier,
            "session_id": sid,
            "parent_session_id": parent,
            "status": status,
            "verdict": verdict,
            "ended_at": float(ended),
        }
    )
    report_lines(state_dir(), lines)
    print(
        f"launched codex {label} model {model} sandbox {sandbox} "
        f"(source: {sandbox_source}) status {status} verdict {verdict}",
        file=sys.stderr,
    )
    if (
        sandbox == "workspace-write"
        and status != "done"
        and sandbox_hint_needed(err_text + "\n" + raw.decode("utf-8", "replace"))
    ):
        try:
            with open(err, "a", encoding="utf-8") as fh:
                fh.write(SANDBOX_HINT + "\n")
        except OSError:
            pass
        print(SANDBOX_HINT, file=sys.stderr)
    return 0 if status == "done" and rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
