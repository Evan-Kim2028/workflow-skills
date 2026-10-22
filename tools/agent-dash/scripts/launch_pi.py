#!/usr/bin/env python3
"""Run one pi (``@mariozechner/pi-coding-agent``) headless worker.

Usage: launch_pi.py <label> <workspace> <prompt_file> <outdir> [--model M]

Runs one labelled ``pi -p --mode json`` worker on the River provider and
reports it to the dashboard. The label is validated with the label
library and refused with the exact fix on a bad one, and the prompt gets a
``[label: <label>]`` first line when it lacks one (the prompt is copied to
``<outdir>/prompt-<label>.md`` with the prefix, so the original is never
modified). ``stdin`` is closed: pi hangs otherwise.

Writes, in ``outdir``:

  <outdir>/pi-sessions.tsv          label, session_id, start epoch, model
  <outdir>/stream-<label>.ndjson    raw NDJSON event stream
  <outdir>/out-<label>.md           final assistant text
  <outdir>/err-<label>.log          stderr
  <outdir>/running/<label>          "<pid>\\t<model>" while the run is live
  <outdir>/QUOTA                    only on quota/credit exhaustion
  <state-dir>/generic.jsonl         one start, one usage per assistant
                                    message, one finish line (run id
                                    ``pi:<session>:<label>``)

Quota/credit exhaustion (an ``errorMessage`` naming insufficient
credit/quota/balance, or HTTP 402/429) finishes ``died_early`` with
verdict ``BLOCKED`` and writes ``QUOTA`` naming the model and label, so
the lead knows to swap the API key. ``outdir`` is registered in
``run-dirs.txt`` like ``run_worker.py`` does.
"""
from __future__ import annotations

import json
import os
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
from dash.sources import pi as pi_source  # noqa: E402

DEFAULT_MODEL = "river/deepseek-ai/DeepSeek-V4.1-Flash-1017"

#: Quota/credit exhaustion markers in an assistant ``errorMessage``.
_QUOTA_WORDS = ("insufficient", "credit", "quota", "balance", "payment required")


def tier_of(model: str | None) -> str:
    """Live tile bucket for a pi model: deepseek for DeepSeek, glm for GLM."""
    text = (model or "").lower()
    if "glm" in text:
        return "glm"
    return "deepseek"


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


def quota_hit(events) -> bool:
    """True when any assistant event names quota/credit exhaustion."""
    for ev in events:
        message = str(ev.get("error") or "") + " " + str(ev.get("stop") or "")
        low = message.lower()
        if any(word in low for word in _QUOTA_WORDS):
            return True
        for code in ("402", "429"):
            if code in message:
                return True
    return False


def stream_errors(raw: bytes) -> list[dict]:
    """Assistant events carrying a stopReason of error or an errorMessage."""
    out = []
    for line in (raw or b"").split(b"\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "message_end":
            continue
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        if message.get("role") != "assistant":
            continue
        if message.get("stopReason") == "error" or message.get("errorMessage"):
            out.append(
                {
                    "error": message.get("errorMessage"),
                    "stop": message.get("stopReason"),
                }
            )
    return out


def verdict_of(text: str) -> str:
    """Worker verdict from the first line of the final text."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    first = lines[0].upper() if lines else ""
    for word in ("BLOCKED", "PARTIAL", "PASS"):
        if word in first:
            return word
    return "PARTIAL"


def session_id_of(raw: bytes) -> str | None:
    """Session id from the stream's ``session`` event, or None."""
    for line in (raw or b"").split(b"\n")[:8]:
        if b'"session"' not in line:
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "session":
            sid = obj.get("id")
            if isinstance(sid, str) and sid:
                return sid
    return None


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


def report_lines(state: Path, lines) -> None:
    """Append generic.jsonl lines. Best effort; never stores prompts."""
    try:
        state.mkdir(parents=True, exist_ok=True)
        with open(state / "generic.jsonl", "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line, separators=(",", ":"), ensure_ascii=True) + "\n")
    except OSError:
        pass


def build_argv(label, workspace, prompt_file, outdir, model):
    """The exact pi command line. ``stdin`` is closed by the caller."""
    return [
        "pi",
        "-p",
        "--mode",
        "json",
        "--session-dir",
        str(Path(outdir) / "sessions"),
        "--model",
        model,
        f"@{prompt_file}",
        "Do the task in the attached prompt file. Your final message is the report.",
    ]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    model = DEFAULT_MODEL
    if "--model" in argv:
        at = argv.index("--model")
        if at + 1 >= len(argv):
            print("usage: launch_pi.py <label> <workspace> <prompt_file> <outdir> [--model M]", file=sys.stderr)
            return 2
        model = argv[at + 1]
        del argv[at : at + 2]
    if len(argv) != 4:
        print("usage: launch_pi.py <label> <workspace> <prompt_file> <outdir> [--model M]", file=sys.stderr)
        return 2
    label, workspace, prompt_file, outdir = argv
    fix = validate_label(label)
    if fix is not None:
        print(f"label refused: {fix}", file=sys.stderr)
        return 2

    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "running").mkdir(parents=True, exist_ok=True)
    register_run_dir(out)

    prompt_text = Path(prompt_file).read_text(encoding="utf-8")
    local_prompt = out / f"prompt-{label}.md"
    local_prompt.write_text(ensure_label_prefix(prompt_text, label), encoding="utf-8")

    stream = out / f"stream-{label}.ndjson"
    err = out / f"err-{label}.log"
    started = int(time.time())
    run_id = f"running/{label}"
    (out / run_id).write_text(f"{os.getpid()}\t{model}", encoding="utf-8")
    with open(stream, "w") as so, open(err, "a") as se:
        se.write(f"--- pi launch label {label} model {model} at {started}\n")
        se.flush()
        rc = subprocess.run(
            build_argv(label, workspace, local_prompt, out, model),
            cwd=str(Path(workspace).expanduser()),
            stdout=so,
            stderr=se,
            stdin=subprocess.DEVNULL,
        ).returncode
    try:
        os.unlink(out / run_id)
    except FileNotFoundError:
        pass

    raw = stream.read_bytes() if stream.exists() else b""
    errors = stream_errors(raw)
    quota = quota_hit(errors)
    sid = session_id_of(raw) or str(uuid.uuid4())
    events = pi_source.parse_stream(raw, sid)
    text = pi_source.final_text(raw)
    ended = int(time.time())

    (out / f"out-{label}.md").write_text(text if text.strip() else "", encoding="utf-8")
    with open(out / "pi-sessions.tsv", "a", encoding="utf-8") as fh:
        if (out / "pi-sessions.tsv").stat().st_size == 0:
            fh.write("label\tsession_id\tstarted_epoch\tmodel\n")
        fh.write(f"{label}\t{sid}\t{started}\t{model}\n")

    if quota:
        (out / "QUOTA").write_text(f"model\t{model}\nlabel\t{label}\n", encoding="utf-8")
        verdict, status = "BLOCKED", "died_early"
    else:
        verdict = verdict_of(text)
        status = "done" if rc == 0 and text.strip() else "died_early"

    lines = [
        {
            "type": "run",
            "label": label,
            "run_id": f"pi:{sid}:{label}",
            "model": model,
            "tier": tier_of(model),
            "session_id": sid,
            "status": "running",
            "started_at": float(started),
        }
    ]
    for ev in events:
        lines.append(
            {
                "type": "usage",
                "session_id": sid,
                "run_id": f"pi:{sid}:{label}",
                "model": model,
                "tool": "pi",
                "message_id": ev["message_id"],
                "ts": float(ended),
                "input": ev["input"],
                "output": ev["output"],
                "cache_read": ev["cache_read"],
                "cache_write": ev["cache_write"],
                "reasoning": 0,
            }
        )
    lines.append(
        {
            "type": "run",
            "label": label,
            "run_id": f"pi:{sid}:{label}",
            "model": model,
            "tier": tier_of(model),
            "session_id": sid,
            "status": status,
            "verdict": verdict,
            "ended_at": float(ended),
        }
    )
    report_lines(state_dir(), lines)
    print(f"launched pi {label} model {model} status {status} verdict {verdict}")
    return 0 if status == "done" and rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
