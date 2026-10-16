"""Process samples by exact comm. Caps count parents. Argv is not stored."""

from __future__ import annotations

import re
import subprocess

NAME = "process"
INTERVAL_S = 5
COMMS = ("cursor-agent", "claude", "grok", "muse", "muse-bin")
_COMM = set(COMMS)
_LABEL = re.compile(r"\[label:\s*([^\]]+?)\]")
_LINE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s*$")
_LABEL_OK = re.compile(r"^[\w.+-]{1,80}$")


def poll(store, cfg, now: float) -> None:
    del cfg
    table = _ps()
    matched = [row for row in table if row["comm"] in _COMM]
    matched_pids = {row["pid"] for row in matched}
    parents = [row for row in matched if row["ppid"] not in matched_pids]
    kids, rss = _children(table)
    for parent in parents:
        label = label_from_pid(parent["pid"])
        store.add_process_sample(
            {
                "ts": now,
                "pid": parent["pid"],
                "comm": parent["comm"],
                "rss_kb": _tree_rss(parent["pid"], kids, rss),
                "elapsed_s": parent["elapsed_s"],
                "label": label,
                "run_id": store.match_run(parent["pid"], label),
            }
        )


def label_from_args(text: str) -> str | None:
    match = _LABEL.search(text or "")
    if not match:
        return None
    label = match.group(1).strip()
    if not _LABEL_OK.fullmatch(label):
        return None
    return label


def label_from_pid(pid: int) -> str | None:
    try:
        proc = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "args="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    label = label_from_args(proc.stdout)
    return label


def parse_etime(text: str) -> float:
    days = 0
    body = text.strip()
    if "-" in body:
        day, body = body.split("-", 1)
        days = int(day)
    parts = [int(part) for part in body.split(":")]
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    else:
        return 0.0
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def parse_ps(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        comm = match.group(6)
        if "/" in comm:
            comm = comm.rsplit("/", 1)[-1]
        try:
            elapsed = parse_etime(match.group(5))
        except ValueError:
            continue
        rows.append(
            {
                "pid": int(match.group(1)),
                "ppid": int(match.group(2)),
                "pgid": int(match.group(3)),
                "rss_kb": int(match.group(4)),
                "elapsed_s": elapsed,
                "comm": comm,
            }
        )
    return rows


def _ps() -> list[dict]:
    proc = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,rss=,etime=,comm="],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        raise RuntimeError("ps failed")
    return parse_ps(proc.stdout)


def _children(table: list[dict]) -> tuple[dict, dict]:
    kids: dict = {}
    rss = {}
    for row in table:
        rss[row["pid"]] = row["rss_kb"]
        kids.setdefault(row["ppid"], []).append(row["pid"])
    return kids, rss


def _tree_rss(root: int, kids: dict, rss: dict) -> int:
    total = rss.get(root, 0)
    seen = {root}
    stack = list(kids.get(root, ()))
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += rss.get(pid, 0)
        stack.extend(kids.get(pid, ()))
    return total
