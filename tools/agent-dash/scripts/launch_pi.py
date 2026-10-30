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

  <outdir>/pi-sessions.tsv          label, session_id, start epoch, model, tier
  <outdir>/stream-<label>.ndjson    raw NDJSON event stream
  <outdir>/out-<label>.md           final assistant text
  <outdir>/err-<label>.log          stderr
  <outdir>/running/<label>          "<pid>\\t<model>" while the run is live
  <outdir>/QUOTA                    only on quota/credit exhaustion
  <state-dir>/generic.jsonl         one start, one usage per assistant
                                    message, one finish line (run id
                                    ``pi:<session>:<label>``)

Quota/credit exhaustion (an ``errorMessage`` naming insufficient
credit/quota/balance, HTTP 402, or a rate-limit 429 the run never
recovered from) finishes ``died_early`` with verdict ``BLOCKED`` and
writes ``QUOTA`` naming the model and label, so the lead knows to swap
the API key. A transient 429 pi auto-retried past is not quota: the
summary line records it as ``rate_limit_retries=<n>``. ``outdir`` is
registered in ``run-dirs.txt`` like ``run_worker.py`` does.

The manifest's fifth cell is the tier: ``run_worker.py`` (``kind: pi``)
passes the registry tier name with ``--tier``, so a DeepSeek run reads
``pi-deepseek`` and a Beam run ``pi-beam`` instead of the bare harness
name. Without ``--tier`` the tier comes from the model family.

``AGENT_DASH_PI_ENV_FILE`` names an env file to source before the run:
``run_worker.py`` sets it for a tier that declares ``standby_env_file``, so
a rate-limited primary credential switches to the standby one. Only the
path travels through this script; no key is read, logged, or printed.
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

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash import labels as label_lib  # noqa: E402
from dash.sources import pi as pi_source  # noqa: E402
from dash.sources.manifests import parent_session_from_pad  # noqa: E402

DEFAULT_MODEL = "river/deepseek-ai/DeepSeek-V4.1-Flash-1017"
USAGE = "usage: launch_pi.py <label> <workspace> <prompt_file> <outdir> [--model M] [--tier T]"

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: Quota/credit exhaustion markers in an assistant ``errorMessage``.
_QUOTA_WORDS = ("insufficient", "credit", "quota", "balance", "payment required")

#: Rate-limit markers: River's transient per-account 429 lands as
#: ``errorMessage`` "429: ... rate_limit_error ... account_rate". pi
#: auto-retries these, so they are quota only when nothing recovers.
_RATE_LIMIT_MARKERS = ("429", "rate limit", "rate_limit_error", "account_rate")


def tier_of(model: str | None) -> str:
    """Tier for a pi model: deepseek for DeepSeek, glm for GLM, beam for Reflection Beam.

    ``run_worker.py`` passes the registry tier (``pi-deepseek``, ``pi-beam``)
    and that wins; this is the fallback for a direct launch.
    """
    text = (model or "").lower()
    if "glm" in text:
        return "glm"
    if "beam" in text:
        return "beam"
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


def _event_text(ev) -> str:
    """Lowercased error text of one assistant message_end summary."""
    return (str(ev.get("error") or "") + " " + str(ev.get("stop") or "")).lower()


def _credit_hit(text: str) -> bool:
    """Insufficient credit/quota/balance or HTTP 402: never transient."""
    return "402" in text or any(word in text for word in _QUOTA_WORDS)


def _rate_limit_hit(text: str) -> bool:
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def _recovered(events: list[dict], index: int) -> bool:
    """A successful assistant message_end follows ``events[index]``."""
    return any(not str(later.get("error") or "") for later in events[index + 1 :])


def quota_hit(events) -> bool:
    """Quota/credit exhaustion from the ordered assistant message_end events.

    Credit exhaustion (HTTP 402, insufficient credit/quota/balance,
    payment required) is an immediate quota hit. A rate-limit 429 counts
    only when the run never recovered: it is the last assistant
    message_end, or no successful assistant message_end follows it, which
    is the empty-final-text case. pi auto-retries a transient River 429
    and finishes normally, so a 429 with a later successful message is a
    retry (``rate_limit_retries``), not quota.
    """
    events = list(events)
    for index, ev in enumerate(events):
        text = _event_text(ev)
        if _credit_hit(text):
            return True
        if _rate_limit_hit(text) and not _recovered(events, index):
            return True
    return False


def rate_limit_retries(events) -> int:
    """Transient rate-limit 429s pi auto-retried past (recovered runs)."""
    events = list(events)
    return sum(
        1
        for index, ev in enumerate(events)
        if _rate_limit_hit(_event_text(ev)) and _recovered(events, index)
    )


def assistant_events(raw: bytes) -> list[dict]:
    """Every assistant ``message_end`` in stream order, errors included.

    Each item is ``{"error": errorMessage-or-None, "stop": stopReason}``;
    an empty ``error`` marks a successful message, the recovery signal
    that separates a transient auto-retried 429 from quota exhaustion.
    """
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


def parent_session_of(outdir) -> str | None:
    """Lead Claude session that launched this worker, or None.

    The outdir under a session scratchpad encodes it
    (``.../<session>/scratchpad/pi``); otherwise the launcher's own
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
    tier = None
    for flag in ("--model", "--tier"):
        if flag not in argv:
            continue
        at = argv.index(flag)
        if at + 1 >= len(argv):
            print(USAGE, file=sys.stderr)
            return 2
        if flag == "--model":
            model = argv[at + 1]
        else:
            tier = argv[at + 1].strip() or None
        del argv[at : at + 2]
    if len(argv) != 4:
        print(USAGE, file=sys.stderr)
        return 2
    label, workspace, prompt_file, outdir = argv
    tier = tier or tier_of(model)
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
    assistant = assistant_events(raw)
    quota = quota_hit(assistant)
    retries = rate_limit_retries(assistant)
    sid = session_id_of(raw) or str(uuid.uuid4())
    events = pi_source.parse_stream(raw, sid)
    text = pi_source.final_text(raw)
    ended = int(time.time())

    (out / f"out-{label}.md").write_text(text if text.strip() else "", encoding="utf-8")
    manifest = out / "pi-sessions.tsv"
    with open(manifest, "a", encoding="utf-8") as fh:
        if manifest.stat().st_size == 0:
            fh.write("label\tsession_id\tstarted_epoch\tmodel\ttier\n")
        fh.write(f"{label}\t{sid}\t{started}\t{model}\t{tier}\n")

    if quota:
        (out / "QUOTA").write_text(f"model\t{model}\nlabel\t{label}\n", encoding="utf-8")
        verdict, status = "BLOCKED", "died_early"
    else:
        verdict = verdict_of(text)
        status = "done" if rc == 0 and text.strip() else "died_early"

    parent = parent_session_of(out)
    lines = [
        {
            "type": "run",
            "label": label,
            "run_id": f"pi:{sid}:{label}",
            "model": model,
            "tier": tier,
            "harness": "pi",
            "session_id": sid,
            "parent_session_id": parent,
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
            "tier": tier,
            "harness": "pi",
            "session_id": sid,
            "parent_session_id": parent,
            "status": status,
            "verdict": verdict,
            "ended_at": float(ended),
        }
    )
    report_lines(state_dir(), lines)
    print(
        f"launched pi {label} model {model} status {status} verdict {verdict}"
        f" rate_limit_retries={retries}"
    )
    return 0 if status == "done" and rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
