#!/usr/bin/env python3
"""Endpoint and CPU probe for the agent-dash server (stdlib only, read-only GETs).

Usage:
  python3 scripts/perf_probe.py --base http://127.0.0.1:8897 [--n 20] [--cpu-window 30]
         [--pid 18059] [--state-dir /tmp/x] [--json out.json]

Per endpoint: first (cold) hit, then ``--n`` warm hits, reporting median and
p95 in ms plus body bytes. ``--cpu-window`` runs a page-like load for that
many seconds (snapshot every 2 s plus the workstream and runs reads, as one
open tab would) and reports server CPU as a percent of one core. CPU comes
from the server's own ``/api/health`` ``cpu.cpu_seconds`` counter (sub-second
resolution), with ``ps`` lifetime %CPU as a cross-check when ``--pid`` is set.
With ``--state-dir`` the probe also reports dash.sqlite and WAL sizes.
Never writes: GET only. Stop nothing.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request

ENDPOINTS = [
    "/api/health",
    "/api/state",
    "/api/snapshot",
    "/api/workstreams",
    "/api/workstreams?state=all",
    "/api/project",
    "/api/project/phases",
    "/api/project/feed",
    "/api/project/burnup",
    "/api/runs",
    "/api/usage",
    "/api/concurrency",
    "/api/sessions",
    "/api/doctor",
    "/api/risks",
    "/api/decisions",
    "/api/approvals?ws=WS1",
    "/api/ws/progress?ws=all",
    "/api/project/graph",
    "/api/reconcile",
    "/api/archive",
]

# Page-like load for the CPU window: one tab polling snapshot every 2 s plus
# the workstream and runs reads on each tick, as the exec view does.
PAGE_TICK = ["/api/snapshot", "/api/workstreams", "/api/runs"]


def get(base: str, path: str, gz: bool = False, timeout: float = 120.0):
    req = urllib.request.Request(base + path)
    if gz:
        req.add_header("Accept-Encoding", "gzip")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        enc = resp.headers.get("Content-Encoding", "")
    dt = (time.perf_counter() - t0) * 1000.0
    if enc == "gzip":
        body = gzip.decompress(body)
    return dt, body


def pctl(values, q):
    values = sorted(values)
    if not values:
        return None
    idx = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
    return values[idx]


def cpu_seconds(base: str):
    try:
        _, body = get(base, "/api/health", timeout=30)
        return json.loads(body).get("perf", {}).get("cpu", {}).get("cpu_seconds")
    except Exception:
        return None


def ps_cpu(pid: int):
    try:
        out = subprocess.run(["ps", "-o", "%cpu=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
        return float(out.stdout.strip() or "nan")
    except Exception:
        return None


def sqlite_sizes(state_dir: str):
    sizes = {}
    for name in ("dash.sqlite", "dash.sqlite-wal"):
        path = os.path.join(state_dir, name)
        sizes[name] = os.path.getsize(path) if os.path.exists(path) else None
    return sizes


def probe_endpoints(base: str, n: int, endpoints):
    rows = []
    for path in endpoints:
        try:
            cold_ms, body = get(base, path)
        except Exception as exc:  # report, keep going
            rows.append({"path": path, "error": repr(exc)})
            continue
        warm = []
        for _ in range(n):
            try:
                ms, body = get(base, path)
                warm.append(ms)
            except Exception as exc:
                rows.append({"path": path, "error": repr(exc)})
                break
        _, gz_body = get(base, path, gz=True)
        rows.append({
            "path": path,
            "cold_ms": round(cold_ms, 1),
            "med_ms": round(statistics.median(warm), 1) if warm else None,
            "p95_ms": round(pctl(warm, 0.95), 1) if warm else None,
            "bytes": len(body),
            "gzip_bytes": len(gz_body) if gz_body else None,
        })
    return rows


def cpu_window(base: str, seconds: float, pid):
    """Page-like load for ``seconds``; returns server CPU percent of one core."""
    c0 = cpu_seconds(base)
    w0 = time.time()
    ps0 = ps_cpu(pid) if pid else None
    lat = {p: [] for p in PAGE_TICK}
    while time.time() - w0 < seconds:
        tick = time.time()
        for p in PAGE_TICK:
            try:
                ms, _ = get(base, p)
                lat[p].append(ms)
            except Exception:
                pass
        time.sleep(max(0.0, 2.0 - (time.time() - tick)))
    wall = time.time() - w0
    c1 = cpu_seconds(base)
    out = {
        "window_s": round(wall, 1),
        "cpu_pct_of_core": round(100.0 * (c1 - c0) / wall, 1) if None not in (c0, c1) else None,
        "ps_lifetime_pct": ps_cpu(pid) if pid else None,
        "ps_lifetime_pct_at_start": ps0,
        "tick_p95_ms": {p: round(pctl(v, 0.95), 1) for p, v in lat.items() if v},
    }
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--n", type=int, default=20, help="warm hits per endpoint")
    ap.add_argument("--cpu-window", type=float, default=0.0, help="seconds of page-like load for CPU")
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--endpoints", nargs="*", default=None)
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)

    report = {"base": args.base, "t": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if args.state_dir:
        report["sqlite_bytes_before"] = sqlite_sizes(args.state_dir)
    report["endpoints"] = probe_endpoints(args.base, args.n, args.endpoints or ENDPOINTS)
    if args.cpu_window > 0:
        report["cpu"] = cpu_window(args.base, args.cpu_window, args.pid)
    if args.state_dir:
        report["sqlite_bytes"] = sqlite_sizes(args.state_dir)

    print(f"{'endpoint':30} {'cold ms':>9} {'med ms':>9} {'p95 ms':>9} {'bytes':>9} {'gzip':>8}")
    for r in report["endpoints"]:
        if "error" in r:
            print(f"{r['path']:30} ERROR {r['error']}")
            continue
        cells = [r[k] if r[k] is not None else '-' for k in ('cold_ms', 'med_ms', 'p95_ms', 'bytes', 'gzip_bytes')]
        print(f"{r['path']:30} {cells[0]:>9} {cells[1]:>9} {cells[2]:>9} {cells[3]:>9} {cells[4]:>8}")
    if "cpu" in report:
        print("cpu:", json.dumps(report["cpu"]))
    if "sqlite_bytes" in report:
        print("sqlite:", json.dumps(report["sqlite_bytes"]))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
