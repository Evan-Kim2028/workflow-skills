#!/usr/bin/env python3
"""Old-vs-new equivalence for ``/api/ws/progress`` on a database copy.

Runs ``wsprogress`` output (every workstream, then the ``ws=all`` project total)
under two code roots against the same SQLite copy and the same fixed ``now``,
and reports whether the JSON is identical. Read-only on the database file's
content, but it opens it with ``Store``, so pass a COPY, never the live file.

  python3 scripts/wsprogress_equiv.py --db /tmp/copy/dash.sqlite \
      --old-root /tmp/base/tools/agent-dash [--new-root <this checkout>] [--now 1793990000]

Exit 0 when identical, 1 when any key differs. Timings are printed for the
``ws=all`` pass so the speed claim can be checked on the same copy.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]

CHILD = r'''
import json, os, sys, time
root, db, now, out = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
sys.path.insert(0, root)
os.environ["AGENT_DASH_STATE_DIR"] = os.path.dirname(db)
os.environ["AGENT_DASH_READONLY"] = "1"
from pathlib import Path
from dash.store import Store
from dash import wsprogress
from dash.replay import ReplayNotFound
store = Store(Path(db))
res, timings = {}, {}
for ws in wsprogress.project_ids():
    wsprogress._CACHE.clear()
    try:
        res[ws] = wsprogress.build_wsprogress(store, ws, now)
    except ReplayNotFound:
        res[ws] = "NOT_FOUND"
wsprogress._CACHE.clear()
t = time.perf_counter()
try:
    res["__all__"] = wsprogress.build_project_progress(store, now)
except ReplayNotFound:
    res["__all__"] = "NOT_FOUND"
timings["ws_all_ms"] = round((time.perf_counter() - t) * 1000)
with open(out, "w") as fh:
    json.dump({"res": res, "timings": timings}, fh, sort_keys=True, default=str)
store.close()
'''


def run(root: str, db: str, now: float, out: str) -> dict:
    subprocess.run([sys.executable, "-c", CHILD, root, db, str(now), out], check=True)
    with open(out) as fh:
        return json.load(fh)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="a COPY of dash.sqlite")
    ap.add_argument("--old-root", required=True)
    ap.add_argument("--new-root", default=str(HERE))
    ap.add_argument("--now", type=float, default=time.time())
    args = ap.parse_args()
    db = str(Path(args.db).resolve())
    with tempfile.TemporaryDirectory() as tmp:
        old = run(args.old_root, db, args.now, os.path.join(tmp, "old.json"))
        new = run(args.new_root, db, args.now, os.path.join(tmp, "new.json"))
    keys = sorted(set(old["res"]) | set(new["res"]))
    diff = [k for k in keys if old["res"].get(k) != new["res"].get(k)]
    print(f"keys compared: {len(keys)}; differing: {len(diff)} {diff[:5]}")
    print(f"ws=all ms: old {old['timings']['ws_all_ms']}, new {new['timings']['ws_all_ms']}")
    return 1 if diff else 0


if __name__ == "__main__":
    raise SystemExit(main())
