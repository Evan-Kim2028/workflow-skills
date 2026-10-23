#!/usr/bin/env python3
"""Run one Command Code headless worker on the free models, rotating on throttle.

Usage: launch_cmd.py <label> <workspace> <prompt_file> <outdir>

Picks the free model with the fewest live runs that is not cooling down, runs
`cmd -p` with NDJSON output, and on a throttle/quota/empty failure puts that
model on a cooldown and retries on the next one. The prompt gets a
`[label: <label>]` first line when it lacks one. Outdir is any directory the
dashboard scans for `cmd-sessions.tsv` (a scratchpad `cmd/` folder works).
Writes:
  <outdir>/out-<label>.md          final text of the successful attempt
  <outdir>/stream-<label>-<n>.ndjson  raw event stream per attempt
  <outdir>/cmd-sessions.tsv        label, model, session_id, start, end, in, out, status, tier
  <outdir>/cooldown.json           model -> epoch until which it is skipped
  <outdir>/running/<label>         "<pid>\\t<model>" while an attempt is live
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time

FREE_MODELS = [
    "reflection/beam:free",
    "poolside/laguna-s-2.1-free",
    "stealth/glyph-cluster:free",
]
COOLDOWN_S = 15 * 60
#: Registry tier recorded in the manifest's last cell (source stays
#: ``commandcode``; the tier names the lane the launch used).
TIER = "commandcode"
MAX_ATTEMPTS = 8
THROTTLE = re.compile(r"429|rate.?limit|too many requests|quota|throttl|capacity|overloaded|insufficient|credits|503|today's limit|no longer available", re.I)


def locked(path, fn):
    with open(path + ".lock", "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        return fn()


def pick(outdir, label):
    cd_path = os.path.join(outdir, "cooldown.json")

    def _pick():
        try:
            cd = json.load(open(cd_path))
        except Exception:
            cd = {}
        now = time.time()
        live = {m: 0 for m in FREE_MODELS}
        rdir = os.path.join(outdir, "running")
        os.makedirs(rdir, exist_ok=True)
        for name in os.listdir(rdir):
            try:
                pid, model = open(os.path.join(rdir, name)).read().split("\t", 1)
                os.kill(int(pid), 0)
                live[model.strip()] = live.get(model.strip(), 0) + 1
            except Exception:
                os.unlink(os.path.join(rdir, name))
        ready = [m for m in FREE_MODELS if cd.get(m, 0) <= now]
        if not ready:
            return None, min(cd.values()) - now
        model = min(ready, key=lambda m: (live[m], FREE_MODELS.index(m)))
        open(os.path.join(rdir, label), "w").write(f"{os.getpid()}\t{model}")
        return model, 0

    return locked(cd_path, _pick)


RESETS = re.compile(r"Resets in (?:(\d+)h)?\s*(?:(\d+)m)?", re.I)


def cooldown_for(text):
    m = RESETS.search(text or "")
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + 60
    return COOLDOWN_S


def cool(outdir, model, seconds=COOLDOWN_S):
    cd_path = os.path.join(outdir, "cooldown.json")

    def _cool():
        try:
            cd = json.load(open(cd_path))
        except Exception:
            cd = {}
        cd[model] = time.time() + seconds
        json.dump(cd, open(cd_path, "w"))

    locked(cd_path, _cool)


def record(outdir, row):
    path = os.path.join(outdir, "cmd-sessions.tsv")

    def _rec():
        new = not os.path.exists(path)
        with open(path, "a") as f:
            if new:
                f.write(
                    "label\tmodel\tsession_id\tstarted_epoch\tended_epoch\tinput_tokens"
                    "\toutput_tokens\tstatus\ttier\n"
                )
            f.write("\t".join(str(x) for x in row) + "\n")

    locked(path, _rec)


def main():
    if len(sys.argv) != 5:
        print("usage: launch_cmd.py <label> <workspace> <prompt_file> <outdir>", file=sys.stderr)
        return 2
    label, workspace, prompt_file, outdir = sys.argv[1:5]
    prompt = open(prompt_file).read()
    if not prompt.startswith(f"[label: {label}]"):
        prompt = f"[label: {label}]\n" + prompt
    for attempt in range(1, MAX_ATTEMPTS + 1):
        model, wait = pick(outdir, label)
        if model is None:
            time.sleep(min(max(wait, 30), 600))
            continue
        stream = os.path.join(outdir, f"stream-{label}-{attempt}.ndjson")
        start = int(time.time())
        with open(stream, "w") as so, open(os.path.join(outdir, f"err-{label}.log"), "a") as se:
            se.write(f"--- attempt {attempt} model {model} at {start}\n")
            se.flush()
            rc = subprocess.run(
                ["cmd", "-p", prompt, "-m", model, "--output-format", "json", "--max-turns", "120",
                 "--yolo", "-t", "--skip-onboarding", "--no-auto-update"],
                cwd=workspace, stdout=so, stderr=se, stdin=subprocess.DEVNULL,
            ).returncode
        try:
            os.unlink(os.path.join(outdir, "running", label))
        except FileNotFoundError:
            pass
        result, tail = None, ""
        for line in open(stream, errors="replace"):
            tail = (tail + line)[-4000:]
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") == "result":
                result = d
        err_tail = open(os.path.join(outdir, f"err-{label}.log"), errors="replace").read()[-2000:]
        usage = (result or {}).get("usage") or {}
        text = (result or {}).get("finalText") or ""
        ok = result and result.get("subtype") == "success" and len(text.strip()) > 40
        status = "ok" if ok else ("throttled" if THROTTLE.search(tail + err_tail) else f"failed rc={rc}")
        record(outdir, [label, model, (result or {}).get("sessionId", "-"), start, int(time.time()),
                        usage.get("inputTokens", 0), usage.get("outputTokens", 0), status,
                        TIER])
        if ok:
            with open(os.path.join(outdir, f"out-{label}.md"), "w") as f:
                f.write(f"<!-- model: {model} attempt {attempt} -->\n{text}\n")
            return 0
        cool(outdir, model, cooldown_for(err_tail[-600:]))
    with open(os.path.join(outdir, f"out-{label}.md"), "w") as f:
        f.write(f"BLOCKED: every free model failed or throttled after {MAX_ATTEMPTS} attempts; see err-{label}.log\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
