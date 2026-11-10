"""Committed-tree evidence for a dispatched task's acceptance probe.

A worker's PASS is a claim. This module turns the claim into a receipt that
binds three things: the task contract, the probe that was run, and the git
tree the probe ran against. A receipt is valid only while all three still
match, so an amended commit or a dirty worktree invalidates it.

Nothing raw is stored. A receipt carries hashes of stdout and stderr, never
their text, and never the environment, the prompt or a credential.

A task with no ``contract_version`` is legacy: it has no acceptance probe, so
it gets a legacy receipt and is never probed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from dash import admission

RECEIPT_VERSION = 1

#: The fields that make up a task contract. Everything else (title, status,
#: owner) is presentation and must not change the hash.
CONTRACT_FIELDS = ("contract_version", "acceptance", "files", "deps", "kind")

GIT_TIMEOUT_S = 30


class EvidenceError(ValueError):
    """A contract, probe or worktree that cannot produce trustworthy evidence."""

    def __init__(self, reason: str, message: str | None = None):
        super().__init__(message or reason)
        self.reason = reason


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def contract_hash(task: dict) -> str:
    """Hash of the contract-bearing fields only, in canonical JSON."""
    body = {key: task.get(key) for key in CONTRACT_FIELDS}
    return _digest(_canonical(body))


def probe_commands(task: dict) -> tuple[list[str], str]:
    """``(probes, cwd)`` for a contract task. ``cwd`` is relative to the workspace.

    An absolute, home-relative or ``..`` cwd is refused: a probe that runs
    outside the workspace proves nothing about the committed tree.
    """
    acceptance = task.get("acceptance")
    if not isinstance(acceptance, dict) or not acceptance:
        raise EvidenceError("no-acceptance", "acceptance must be a non-empty object")
    probe = acceptance.get("probe")
    if isinstance(probe, str):
        probes = [probe]
    elif isinstance(probe, list) and probe:
        probes = list(probe)
    else:
        raise EvidenceError("no-probe", "acceptance.probe must be a non-empty string or list")
    for item in probes:
        if not isinstance(item, str) or not item.strip():
            raise EvidenceError("no-probe", "acceptance.probe entries must be non-empty strings")
    cwd = acceptance.get("cwd") or "."
    if not isinstance(cwd, str) or not cwd.strip():
        raise EvidenceError("bad-cwd", "acceptance.cwd must be a relative path string")
    if os.path.isabs(cwd) or cwd.startswith("~"):
        raise EvidenceError("bad-cwd", f"acceptance.cwd must be relative: {cwd}")
    if ".." in Path(cwd).parts:
        raise EvidenceError("bad-cwd", f"acceptance.cwd must not contain ..: {cwd}")
    return probes, cwd


def probe_hash(task: dict) -> str:
    """Hash of the probe commands and their cwd, so a changed probe invalidates."""
    probes, cwd = probe_commands(task)
    return _digest(_canonical({"probes": probes, "cwd": cwd}))


def _git(workspace, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
    )


def git_tree(workspace) -> str:
    """The HEAD tree hash of a clean git worktree.

    A dirty worktree, a directory that is not a git worktree, or a repository
    with no commit is refused, because none of them names a committed tree.
    """
    root = Path(str(workspace))
    if not root.is_dir():
        raise EvidenceError("no-workspace", f"workspace is not a directory: {workspace}")
    try:
        inside = _git(root, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceError("not-a-git-worktree", f"git unavailable: {exc}") from exc
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise EvidenceError("not-a-git-worktree", f"not a git worktree: {workspace}")
    status = _git(root, "status", "--porcelain")
    if status.returncode != 0:
        raise EvidenceError("not-a-git-worktree", f"git status failed: {status.stderr.strip()}")
    if status.stdout.strip():
        raise EvidenceError("dirty-tree", "worktree has uncommitted changes")
    head = _git(root, "rev-parse", "HEAD^{tree}")
    if head.returncode != 0 or not head.stdout.strip():
        raise EvidenceError("no-git-tree", f"no committed tree: {head.stderr.strip()}")
    return head.stdout.strip()


def _receipt(task, **fields) -> dict:
    body = {
        "version": RECEIPT_VERSION,
        "ok": False,
        "reason": "ok",
        "task_id": task.get("id") if isinstance(task, dict) else str(task),
        "contract_hash": None,
        "tree": None,
        "probe_hash": None,
        "exit_code": None,
        "checked_at": None,
        "duration_s": 0.0,
        "stdout_sha256": None,
        "stderr_sha256": None,
    }
    body.update(fields)
    return body


def verify_task(task: dict, workspace, timeout_s: float = 300, now=None) -> dict:
    """Run a contract task's probes against ``workspace`` and return a receipt.

    Each probe is split with :func:`shlex.split` and run without a shell, in
    ``workspace/acceptance.cwd``, under one bounded timeout. A timeout, a
    nonzero exit, a dirty worktree or a missing tree fails the receipt. Raw
    output is hashed, never stored.
    """
    checked_at = time.time() if now is None else float(now)
    if not isinstance(task, dict):
        return _receipt(task, reason="bad-task", checked_at=checked_at)
    if task.get("contract_version") is None:
        return _receipt(task, ok=True, reason="legacy", legacy=True, checked_at=checked_at)
    try:
        probes, cwd = probe_commands(task)
        contract = contract_hash(task)
        probe = probe_hash(task)
    except EvidenceError as exc:
        return _receipt(task, reason=exc.reason, checked_at=checked_at)
    receipt = _receipt(task, contract_hash=contract, probe_hash=probe, checked_at=checked_at)
    try:
        receipt["tree"] = git_tree(workspace)
    except EvidenceError as exc:
        receipt["reason"] = exc.reason
        return receipt
    cwd_path = os.path.join(str(workspace), cwd)
    if not os.path.isdir(cwd_path):
        receipt["reason"] = "missing-cwd"
        return receipt
    started = time.time()
    for command in probes:
        try:
            argv = shlex.split(command)
        except ValueError:
            receipt["reason"] = "bad-probe"
            receipt["duration_s"] = round(time.time() - started, 3)
            return receipt
        if not argv:
            receipt["reason"] = "bad-probe"
            receipt["duration_s"] = round(time.time() - started, 3)
            return receipt
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd_path,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            receipt["reason"] = "timeout"
            receipt["stdout_sha256"] = _digest(exc.stdout)
            receipt["stderr_sha256"] = _digest(exc.stderr)
            receipt["duration_s"] = round(time.time() - started, 3)
            return receipt
        except OSError:
            receipt["reason"] = "probe-not-runnable"
            receipt["duration_s"] = round(time.time() - started, 3)
            return receipt
        receipt["exit_code"] = proc.returncode
        receipt["stdout_sha256"] = _digest(proc.stdout)
        receipt["stderr_sha256"] = _digest(proc.stderr)
        if proc.returncode != 0:
            receipt["reason"] = "probe-failed"
            receipt["duration_s"] = round(time.time() - started, 3)
            return receipt
    receipt["ok"] = True
    receipt["duration_s"] = round(time.time() - started, 3)
    return receipt


def _task_id(task) -> str:
    return str(task.get("id") if isinstance(task, dict) else task or "task")


def receipt_path(task, state_dir=None) -> Path:
    """Where a task's receipt lives under the dashboard state dir."""
    base = Path(state_dir or admission.default_state_dir()) / "receipts"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", _task_id(task))
    if safe in ("", ".", ".."):
        safe = "task"
    return base / f"{safe}.json"


def write_receipt(receipt: dict, state_dir=None) -> Path:
    """Write a receipt atomically and return its path."""
    path = receipt_path(receipt.get("task_id"), state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".receipt-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load_receipt(task, state_dir=None) -> dict | None:
    """The stored receipt for a task, or None when there is none or it is unreadable."""
    try:
        body = json.loads(receipt_path(task, state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def receipt_valid(receipt: dict, task: dict, workspace) -> bool:
    """True only while the receipt still matches the contract, probe and tree.

    An amended commit changes the tree hash, a dirty worktree has no tree, and
    an edited contract or probe changes its hash. Any of those invalidates.
    """
    if not isinstance(receipt, dict) or not receipt.get("ok"):
        return False
    if not isinstance(task, dict):
        return False
    if task.get("contract_version") is None:
        return bool(receipt.get("legacy"))
    if receipt.get("legacy"):
        return False
    try:
        if receipt.get("contract_hash") != contract_hash(task):
            return False
        if receipt.get("probe_hash") != probe_hash(task):
            return False
    except EvidenceError:
        return False
    try:
        return receipt.get("tree") == git_tree(workspace)
    except EvidenceError:
        return False
