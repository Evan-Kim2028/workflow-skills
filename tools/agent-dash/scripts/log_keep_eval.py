#!/usr/bin/env python3
"""Option test: what stays at a finished log's original path.

Copies real pi run folders (read only) into a temp tree, runs the real
pi and generic pollers over them, then applies one keep option to every
stream and session file (with the same ``logarchive.measure`` and
``logarchive.reclaim`` the archive uses) and polls again:

* ``control``  no change (baseline; the second poll must add nothing)
* ``remove``   the raw file is deleted
* ``stub``     the raw file is replaced by a one-line archive pointer
* ``stripped`` the raw file is replaced by its base64-stripped content

For each option the same store is polled before and after the change
(incremental: nothing may be re-counted), and a fresh store is polled
in a separate process over the changed files (cold: must match the
baseline). Compared: run rows (status, verdict, tier, model, session,
label, times) and every usage event (tokens by run and message id).

Usage:
  python3 scripts/log_keep_eval.py --work /tmp/keep-eval [--limit 60] [--big]
  python3 scripts/log_keep_eval.py --work ... --option remove   (one option)

Reads ~/Documents/projects/*-hub-state only; writes only under --work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
def _hubs() -> list[Path]:
    """The archive's default roots (``log_archive.roots`` in the local config)."""
    from dash import logarchive as la

    roots = la.default_roots()
    if not roots:
        raise SystemExit("set log_archive.roots in <state>/config.local.json (the eval picks run folders from those hubs)")
    return [Path(r) for r in roots]
OPTIONS = ["control", "remove", "stub", "stripped"]
BIG_STREAM = "catalog-confidence/dispatch-bundles/WS91/runs/ws91-exec-03/pi"


#: Outdir marker per kind: the manifest (or, for out.jsonl, the stream itself).
KIND_MARK = {"pi": "pi-sessions.tsv", "cmd": "cmd-sessions.tsv", "codex": "codex-sessions.tsv", "out": "out.jsonl"}


def pick_outdirs(limit: int, include_big: bool, kinds=("pi",), max_mb: float = 100) -> list[Path]:
    """Real outdirs per kind (see KIND_MARK), small ones only, up to ``limit`` per kind."""
    picked: list[Path] = []
    for kind in kinds:
        mark = KIND_MARK[kind]
        found: list[Path] = []
        for hub in _hubs():
            for dirpath, _dirnames, filenames in os.walk(hub):
                if "/.git" not in dirpath and mark in filenames:
                    found.append(Path(dirpath))
        small = []
        for d in sorted(found):
            size = sum(p.stat().st_size for p in d.iterdir() if p.is_file())
            if size < max_mb * 2**20:
                small.append(d)
        picked.extend(d for d in small[:limit] if d not in picked)
    if include_big and "pi" in kinds:
        big = Path.home() / "Documents/projects/lor-hub-state" / BIG_STREAM
        if big.is_dir() and big not in picked:
            picked.append(big)
    return picked


def build(fixture: Path, limit: int, include_big: bool, kinds=("pi",), max_mb: float = 100) -> dict:
    if fixture.exists():
        shutil.rmtree(fixture)
    (fixture / "scratch").mkdir(parents=True)
    (fixture / "state").mkdir()
    outdirs = pick_outdirs(limit, include_big, kinds, max_mb)
    mapping = []
    for src in outdirs:
        rel = str(src).replace(str(Path.home()) + "/", "").replace("/", "__")
        dst = fixture / "scratch" / rel
        shutil.copytree(src, dst, symlinks=True)
        mapping.append({"src": str(src), "dst": str(dst)})
    _copy_generic(fixture / "state" / "generic.jsonl", mapping)
    meta = {"outdirs": mapping, "built": time.time()}
    _prepare(fixture)
    (fixture / "fixture.json").write_text(json.dumps(meta, indent=1))
    return meta


def _copy_generic(dst: Path, mapping: list) -> None:
    """The real ``generic.jsonl`` narrowed to the fixture's runs.

    The whole ledger is ~2000 runs; each poll re-upserts all of them, so a
    full copy makes every round slow. Only lines whose label belongs to a
    fixture run folder are kept, so the same pollers read the same record
    shapes (pi usage and run rows come through this file).
    """
    src = Path.home() / ".local/share/agent-dash/generic.jsonl"
    if not src.exists():
        return
    labels, sids = set(), set()
    for item in mapping:
        for name in KIND_MARK.values():
            if name == "out.jsonl":
                continue
            for tsv in Path(item["src"]).glob(name):
                lines = tsv.read_text(encoding="utf-8", errors="replace").splitlines()
                if not lines:
                    continue
                head = lines[0].split("\t")
                li = head.index("label") if "label" in head else 0
                si = head.index("session_id") if "session_id" in head else 1
                for line in lines[1:]:
                    cells = line.split("\t")
                    if len(cells) > li and cells[li]:
                        labels.add(cells[li])
                    if len(cells) > si and cells[si]:
                        sids.add(cells[si])
    kept = 0
    with open(src, "r", encoding="utf-8", errors="replace") as fin, open(dst, "w", encoding="utf-8") as fout:
        for line in fin:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and (rec.get("label") in labels or rec.get("session_id") in sids):
                fout.write(line if line.endswith("\n") else line + "\n")
                kept += 1
    (dst.parent / "generic-kept.txt").write_text(str(kept))


def _prepare(base: Path) -> None:
    """Measure every stream once: zstd copies and stripped sha256, shared by all options."""
    from dash import logarchive as la

    entries, _skipped = la.discover([str(base / "scratch")])
    zdir = base / "zst"
    zdir.mkdir(exist_ok=True)
    rows = []
    for n, e in enumerate(entries):
        zst = zdir / ("%05d.zst" % n)
        m = la.measure(e.path, str(zst))
        rows.append({"name": os.path.relpath(e.path, base / "scratch"), "sha": m.stripped_sha256, "lines": m.stripped_lines, "size": e.size})
    (base / "measured.json").write_text(json.dumps(rows))


def _env_for(state: Path) -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.environ["AGENT_DASH_STATE_DIR"] = str(state)
    os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"


def _cfg(state: Path) -> dict:
    sys.path.insert(0, str(ROOT))
    from agent_dash import default_cfg

    cfg = default_cfg()
    cfg["state_dir"] = str(state)
    cfg["data_root"] = str(state)
    cfg["roots"]["scratchpads"] = []
    # Isolate from live roots (~/.commandcode, ~/.claude, ...): an empty root
    # per key, so every poll reads only the fixture and the run is repeatable.
    empty = state / "empty-roots"
    empty.mkdir(parents=True, exist_ok=True)
    for key in list(cfg["roots"]):
        cfg["roots"][key] = str(empty / key)
    return cfg


def _register(state: Path, meta: dict, work: Path) -> None:
    from dash import rundirs

    for item in meta["outdirs"]:
        dst = work / "scratch" / Path(item["dst"]).name
        rundirs.register_run_dir(state, dst)


def _poll(state: Path) -> None:
    from dash.sources import commandcode, generic, manifests, pi
    from dash.store import Store

    cfg = _cfg(state)
    store = Store(state / "dash.sqlite")
    try:
        now = time.time()
        manifests.poll(store, cfg, now)
        commandcode.poll(store, cfg, now)
        pi.poll(store, cfg, now)
        generic.poll(store, cfg, now)
    finally:
        store.close()


def _snapshot(state: Path) -> dict:
    con = sqlite3.connect("file:%s?mode=ro" % (state / "dash.sqlite"), uri=True)
    try:
        runs = [
            list(r)
            for r in con.execute(
                "select run_id, source, status, verdict, tier, model, session_id, label "
                "from runs order by run_id"
            )
        ]
        usage = [
            list(r)
            for r in con.execute(
                "select run_id, tool, session_id, message_id, input_tokens, output_tokens, cache_read, cache_write, reasoning "
                "from usage_events order by run_id, tool, session_id, message_id"
            )
        ]
    finally:
        con.close()
    totals = {
        "input": sum(u[4] or 0 for u in usage),
        "output": sum(u[5] or 0 for u in usage),
        "cache_read": sum(u[6] or 0 for u in usage),
        "cache_write": sum(u[7] or 0 for u in usage),
        "usage_rows": len(usage),
        "runs": len(runs),
    }
    digest = hashlib.sha256(json.dumps([runs, usage], sort_keys=True).encode()).hexdigest()[:16]
    return {"runs": runs, "usage": usage, "totals": totals, "digest": digest}


def _mutate(work: Path, option: str) -> dict:
    """Apply one keep option to every stream and session file under scratch."""
    from dash import logarchive as la

    entries, _skipped = la.discover([str(work / "scratch")])
    bytes_before = sum(e.size for e in entries)
    if option == "control":
        return {"files": len(entries), "raw_bytes": bytes_before, "scratch_bytes_after": bytes_before}
    measured = json.loads((work / "measured.json").read_text())
    if len(measured) != len(entries):
        raise SystemExit("fixture changed: %d measured, %d found" % (len(measured), len(entries)))
    zdir = work / "zst"
    for n, e in enumerate(entries):
        if os.path.relpath(e.path, work / "scratch") != measured[n]["name"]:
            raise SystemExit("entry order changed at %d: %s" % (n, e.path))
        zst = zdir / ("%05d.zst" % n)
        rec = {"stripped_sha256": measured[n]["sha"], "stripped_lines": measured[n]["lines"]}
        la.reclaim(e, zst, option, rec)
    # Stream bytes still at their original paths (stubs and stripped rewrites included).
    remaining = sum(os.path.getsize(e.path) for e in entries if os.path.exists(e.path))
    return {"files": len(entries), "raw_bytes": bytes_before, "scratch_bytes_after": remaining}


def _converge(state: Path, max_rounds: int = 80) -> tuple[dict, int]:
    """Poll until the snapshot stops changing. Each poll reads a bounded budget."""
    prev = None
    for rounds in range(1, max_rounds + 1):
        _poll(state)
        snap = _snapshot(state)
        if prev is not None and snap["digest"] == prev:
            return snap, rounds
        prev = snap["digest"]
    raise SystemExit("did not converge in %d polls" % max_rounds)


def child_incr(work: Path, option: str) -> dict:
    state = work / "state"
    _env_for(state)
    meta = json.loads((work / "fixture.json").read_text())
    _register(state, meta, work)
    before, rounds_before = _converge(state)
    info = _mutate(work, option)
    after, rounds_after = _converge(state)
    info.update({"rounds_before": rounds_before, "rounds_after": rounds_after})
    return {"before": before, "after": after, "mutate": info}


def child_cold(work: Path) -> dict:
    cold = work / "cold"
    if cold.exists():
        shutil.rmtree(cold)
    cold.mkdir(parents=True)
    _env_for(cold)
    meta = json.loads((work / "fixture.json").read_text())
    generic = work / "state" / "generic.jsonl"
    if generic.exists():
        shutil.copy(generic, cold / "generic.jsonl")
    _register(cold, meta, work)
    snap, rounds = _converge(cold)
    return {"cold": snap, "rounds": rounds}


def _child(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["incr", "cold"])
    parser.add_argument("--work", required=True)
    parser.add_argument("--option", default="control")
    ns = parser.parse_args(argv)
    work = Path(ns.work)
    if ns.mode == "incr":
        out = child_incr(work, ns.option)
    else:
        out = child_cold(work)
    print(json.dumps(out, sort_keys=True))
    return 0


def _run_child(args: list[str]) -> dict:
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "_child", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit("child failed: %s\n%s" % (args, proc.stderr[-2000:]))
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main(argv: list[str]) -> int:
    if argv and argv[0] == "_child":
        return _child(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work", required=True)
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--big", action="store_true", help="include the 791 MB base64-heavy pi stream")
    parser.add_argument("--kinds", default="pi", help="comma list from: pi,cmd,codex,out (default pi)")
    parser.add_argument("--max-folder-mb", type=float, default=100, help="skip run folders larger than this")
    parser.add_argument("--option", choices=OPTIONS, action="append")
    parser.add_argument("--json-out")
    ns = parser.parse_args(argv)
    work = Path(ns.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    fixture = work / "fixture"
    kinds = [k for k in ns.kinds.split(",") if k]
    bad = [k for k in kinds if k not in KIND_MARK]
    if bad:
        parser.error("unknown kind(s): %s" % ", ".join(bad))
    meta = build(fixture, ns.limit, ns.big, kinds, ns.max_folder_mb)
    print("fixture: %d run folders (%s)" % (len(meta["outdirs"]), ",".join(kinds)))
    results = {}
    for option in ns.option or OPTIONS:
        run = work / ("run-" + option)
        if run.exists():
            shutil.rmtree(run)
        shutil.copytree(fixture, run, symlinks=True)
        inc = _run_child(["incr", "--work", str(run), "--option", option])
        cold = _run_child(["cold", "--work", str(run)])
        before, after, cold_snap = inc["before"], inc["after"], cold["cold"]
        results[option] = {
            "mutate": inc["mutate"],
            "baseline": {"runs": before["totals"]["runs"], "usage_rows": before["totals"]["usage_rows"], "digest": before["digest"]},
            "incremental_equal": before["runs"] == after["runs"] and before["usage"] == after["usage"],
            "cold_equal": before["runs"] == cold_snap["runs"] and before["usage"] == cold_snap["usage"],
            "after_totals": after["totals"],
            "tokens_equal": before["totals"] == after["totals"],
            "statuses": _counts([r[2] for r in after["runs"]]),
            "verdicts": _counts([r[3] for r in after["runs"]]),
        }
    print(json.dumps(results, indent=1, sort_keys=True))
    if ns.json_out:
        Path(ns.json_out).write_text(json.dumps(results, indent=1, sort_keys=True))
    ok = all(r["incremental_equal"] and r["cold_equal"] and r["tokens_equal"] for r in results.values())
    print("VERDICT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _counts(values) -> dict:
    out: dict = {}
    for v in values:
        key = str(v)
        out[key] = out.get(key, 0) + 1
    return out


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
