#!/usr/bin/env python3
"""Token usage and throughput for muse exec sessions.

Usage:
  muse-usage.py SESSION_ID [SESSION_ID...]
  muse-usage.py --match SUBSTR [--match SUBSTR ...]   (find sessions whose log contains SUBSTR, e.g. a distinctive phrase from the prompt; prompt-file paths are not logged)
  muse-usage.py --manifest FILE                       (TSV: label<TAB>session_id[<TAB>anything])
  muse-usage.py --since-hours N                       (every session started in the last N hours)

Reads ~/.local/share/muse/sessions/YYYY/MM/DD/<id>/session.jsonl, sums every
`model_completed` usage record, follows the session's `subagent/` children, and
prints per-session and total input/cached/output/reasoning tokens, model steps,
model wall time, and output tokens per second of model time.
"""
import argparse, glob, json, os, sys, time

ROOT = os.path.expanduser("~/.local/share/muse/sessions")


def session_dirs():
    return sorted(glob.glob(os.path.join(ROOT, "*", "*", "*", "*")))


def find_dir(sid):
    hits = glob.glob(os.path.join(ROOT, "*", "*", "*", sid))
    return hits[0] if hits else None


def read_usage(d):
    f = os.path.join(d, "session.jsonl")
    tot = dict(input=0, cached=0, output=0, reasoning=0, steps=0, ms=0, first=None, last=None)
    if not os.path.exists(f):
        return tot
    with open(f, errors="replace") as fh:
        for line in fh:
            if '"model_completed"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            e = (o.get("payload") or {}).get("event") or {}
            if e.get("kind") != "model_completed":
                continue
            u = e.get("usage") or {}
            tot["input"] += u.get("input_tokens", 0)
            tot["cached"] += max(u.get("cached_tokens", 0), u.get("cache_read_tokens", 0))
            tot["output"] += u.get("output_tokens", 0)
            tot["reasoning"] += u.get("reasoning_tokens", 0)
            tot["steps"] += 1
            tot["ms"] += e.get("duration_ms", 0)
            ts = o.get("recorded_at")
            if ts:
                tot["first"] = ts if tot["first"] is None else min(tot["first"], ts)
                tot["last"] = ts if tot["last"] is None else max(tot["last"], ts)
    return tot


def children(d):
    sub = os.path.join(d, "subagent")
    if not os.path.isdir(sub):
        return []
    out = []
    for cid in os.listdir(sub):
        cd = os.path.join(sub, cid)
        if os.path.exists(os.path.join(cd, "session.jsonl")):
            out.append(cd)
        else:
            alt = find_dir(cid)
            if alt:
                out.append(alt)
    return out


def collect(d):
    tot = read_usage(d)
    kids = 0
    for c in children(d):
        k = read_usage(c)
        kids += 1
        for key in ("input", "cached", "output", "reasoning", "steps", "ms"):
            tot[key] += k[key]
    tot["children"] = kids
    return tot


def matches(d, subs):
    f = os.path.join(d, "session.jsonl")
    if not os.path.exists(f):
        return False
    with open(f, errors="replace") as fh:
        head = fh.read(400_000)
    return any(s in head for s in subs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--match", action="append", default=[])
    ap.add_argument("--manifest")
    ap.add_argument("--since-hours", type=float)
    a = ap.parse_args()
    rows = []
    if a.manifest:
        for line in open(a.manifest):
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[1]:
                rows.append((parts[0], parts[1]))
    for sid in a.ids:
        rows.append((sid[:8], sid))
    if a.match:
        for d in session_dirs():
            if matches(d, a.match):
                rows.append((os.path.basename(d)[:8], os.path.basename(d)))
    if a.since_hours:
        cutoff = time.time() - a.since_hours * 3600
        for d in session_dirs():
            f = os.path.join(d, "session.jsonl")
            if os.path.exists(f) and os.stat(f).st_mtime >= cutoff:
                rows.append((os.path.basename(d)[:8], os.path.basename(d)))
    seen = set()
    hdr = f"{'label':<14}{'session':<10}{'steps':>6}{'kids':>5}{'input':>14}{'cached':>14}{'output':>10}{'reason':>10}{'model_s':>9}{'out_tok/s':>10}"
    print(hdr)
    print("-" * len(hdr))
    T = dict(input=0, cached=0, output=0, reasoning=0, steps=0, ms=0, children=0)
    for label, sid in rows:
        if sid in seen:
            continue
        seen.add(sid)
        d = find_dir(sid)
        if not d:
            print(f"{label:<14}{sid[:8]:<10} (not found)")
            continue
        t = collect(d)
        for k in T:
            T[k] += t[k]
        tps = t["output"] / (t["ms"] / 1000) if t["ms"] else 0
        print(f"{label:<14}{sid[:8]:<10}{t['steps']:>6}{t['children']:>5}{t['input']:>14,}{t['cached']:>14,}{t['output']:>10,}{t['reasoning']:>10,}{t['ms']/1000:>9.0f}{tps:>10.1f}")
    tps = T["output"] / (T["ms"] / 1000) if T["ms"] else 0
    print("-" * len(hdr))
    print(f"{'TOTAL':<14}{len(seen):<10}{T['steps']:>6}{T['children']:>5}{T['input']:>14,}{T['cached']:>14,}{T['output']:>10,}{T['reasoning']:>10,}{T['ms']/1000:>9.0f}{tps:>10.1f}")


if __name__ == "__main__":
    main()
