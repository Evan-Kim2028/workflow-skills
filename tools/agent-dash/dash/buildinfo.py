"""Build identity for the safe deploy flow.

``build_id`` is the git short sha of the tool directory plus a dirty
flag (``"abc1234"`` or ``"abc1234-dirty"``), or ``"unknown"`` when the
tool is not a git checkout. ``build_info`` pairs it with the process
start time. Both are read-only git; the server captures them once at
start and serves them on ``/api/health`` and ``/api/state`` so ``run``
can refuse a mismatched port and ``deploy`` can health-gate a release.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]

DIRTY_SUFFIX = "-dirty"
UNKNOWN = "unknown"


def _git(root: Path, *args: str) -> tuple[int, str]:
    """Run a read-only git command. Returns ``(returncode, stdout)``."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, proc.stdout or ""


def build_id(root: Path | str | None = None) -> str:
    """Short sha plus dirty flag, or ``"unknown"`` outside git."""
    root = Path(root) if root is not None else TOOL_ROOT
    code, out = _git(root, "rev-parse", "--short", "HEAD")
    sha = out.strip()
    if code != 0 or not sha:
        return UNKNOWN
    code, out = _git(root, "status", "--porcelain")
    if code != 0:
        return UNKNOWN
    dirty = any(line.strip() for line in out.splitlines())
    return sha + DIRTY_SUFFIX if dirty else sha


def build_info(root: Path | str | None = None, now: float | None = None) -> dict:
    """``{"build_id", "started_at"}`` for health payloads and status."""
    return {
        "build_id": build_id(root),
        "started_at": float(now if now is not None else time.time()),
    }
