"""Cleanup after a successful ``ws archive <ID>``.

Two steps, both after the archive has landed:

1. ``agent_dash.py logs archive --ws <ID> --apply`` when that command exists
   (the ``dash.logarchive`` module is present). Skipped with one line otherwise.
2. Removal of the workstream's scratch folders (``dash/scratchgc.py``).
   Refused, with one line naming the blockers, while the workstream has live
   runs or when the live-process check cannot run. ``--keep-scratch`` skips
   this step.

A failure in either step is reported and never fails the archive: the
caller ignores the return value except for the printed lines.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

LOGS_TIMEOUT_S = 900
TOOL_ROOT = Path(__file__).resolve().parents[1]


def logs_archive_available() -> bool:
    """True when the ``logs archive`` service module is installed."""
    try:
        return importlib.util.find_spec("dash.logarchive") is not None
    except (ImportError, ValueError):
        return False


def _run_logs_archive(ws: str, runner=None) -> str:
    cmd = [sys.executable, str(TOOL_ROOT / "agent_dash.py"), "logs", "archive", "--ws", ws, "--apply"]
    try:
        if runner is not None:
            code = runner(cmd)
        else:
            code = subprocess.run(cmd, capture_output=True, text=True, timeout=LOGS_TIMEOUT_S).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        return f"logs archive did not run ({exc.__class__.__name__}); archive kept"
    if code != 0:
        return f"logs archive exited {code}; archive kept"
    return "logs archive applied"


def cleanup_after_archive(
    ws: str,
    *,
    keep_scratch: bool = False,
    root=None,
    now: float | None = None,
    logs_runner=None,
    logs_available=None,
    out=print,
) -> dict:
    """Run the post-archive cleanup. Never raises; returns what it did."""
    result = {"logs": None, "scratch": None, "removed": [], "freed": 0}
    try:
        available = logs_archive_available() if logs_available is None else bool(logs_available)
        if available:
            result["logs"] = _run_logs_archive(ws, logs_runner)
        else:
            result["logs"] = "skipped: logs archive is not available in this build"
        out(f"{ws}: {result['logs']}")
    except Exception as exc:  # cleanup failure never fails the archive
        out(f"{ws}: logs archive step failed ({exc.__class__.__name__}); archive kept")

    if keep_scratch:
        result["scratch"] = "kept: --keep-scratch"
        out(f"{ws}: scratch kept (--keep-scratch)")
        return result
    try:
        from dash import scratchgc

        verifiable, blockers = scratchgc.workstream_live(ws, root, now)
        if not verifiable:
            result["scratch"] = "refused: live-process check did not run"
            out(f"{ws}: scratch cleanup refused: the live-run check did not run. Archive kept.")
            return result
        if blockers:
            names = ", ".join(blockers[:5]) + (" ..." if len(blockers) > 5 else "")
            result["scratch"] = f"refused: live runs ({names})"
            out(f"{ws}: scratch cleanup refused: live runs present ({names}). Archive kept; re-run cleanup later with: python3 agent_dash.py scratch gc --apply")
            return result
        done = scratchgc.remove_workstream(ws, root, now)
        result["removed"] = done["removed"]
        result["freed"] = done["freed"]
        result["scratch"] = f"removed {len(done['removed'])} folder(s)"
        line = f"{ws}: scratch reclaimed {scratchgc.human(done['freed'])} ({len(done['removed'])} folder(s))"
        if done["kept"]:
            line += f"; kept {len(done['kept'])}: " + "; ".join(done["kept"][:3])
        out(line)
    except Exception as exc:
        result["scratch"] = f"failed: {exc.__class__.__name__}"
        out(f"{ws}: scratch cleanup failed ({exc.__class__.__name__}); archive kept")
    return result
