"""Merged pull requests for the filler's PR reviews, read through the ``gh`` CLI.

One bounded ``gh pr list`` call per repo and refresh. ``merged_prs`` raises ``PRSourceError`` on any failure, so the
caller can skip the repo for this round and try again later.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

GH_TIMEOUT_S = 30.0
PR_LIMIT = 50
MERGED_AT = "%Y-%m-%dT%H:%M:%SZ"


class PRSourceError(RuntimeError):
    """``gh`` failed, timed out or printed something unreadable."""


def since_date(now: float, window_h: float) -> str:
    """The first UTC date the window can start on (``merged:>=`` takes whole days)."""
    start = datetime.fromtimestamp(now - window_h * 3600, tz=timezone.utc)
    return start.date().isoformat()


def merged_epoch(row: dict) -> float:
    """The merge time of one PR row as epoch seconds (0 when unreadable)."""
    try:
        return datetime.strptime(row["mergedAt"], MERGED_AT).replace(tzinfo=timezone.utc).timestamp()
    except (KeyError, TypeError, ValueError):
        return 0.0


def merged_prs(slug: str, since: str, run=subprocess.run, limit: int = PR_LIMIT) -> list[dict]:
    """Merged PRs into main since ``since`` (YYYY-MM-DD), newest first: number, title, url, mergedAt, merge_commit."""
    cmd = [
        "gh", "pr", "list", "--repo", slug, "--state", "merged", "--base", "main",
        "--search", f"merged:>={since}", "--limit", str(limit),
        "--json", "number,title,url,mergedAt,mergeCommit",
    ]
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=GH_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PRSourceError(f"gh did not run: {exc}") from exc
    if proc.returncode != 0:
        raise PRSourceError((proc.stderr or "gh failed").strip()[-200:])
    try:
        rows = json.loads(proc.stdout or "[]")
    except ValueError as exc:
        raise PRSourceError(f"gh printed no JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise PRSourceError("gh printed a non-list")
    out = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("number"), int):
            continue
        commit = row.get("mergeCommit")
        out.append({
            "number": row["number"], "title": str(row.get("title") or ""), "url": str(row.get("url") or ""),
            "mergedAt": str(row.get("mergedAt") or ""),
            "merge_commit": commit.get("oid", "") if isinstance(commit, dict) else "",
        })
    out.sort(key=merged_epoch, reverse=True)
    return out
