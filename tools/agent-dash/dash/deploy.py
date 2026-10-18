"""Clean-release deploy with a health gate and rollback.

The live dashboard can run from a release instead of a dirty working
checkout. A release is a clean detached worktree of one commit under
``<state-dir>/releases/<sha>``; ``releases/CURRENT`` and
``releases/PREVIOUS`` are symlinks naming the running release and the
one before it. ``deploy`` records the previous release, gates the new
one (``scripts/gate.sh`` when present, else the page-rules and server
test files), switches ``CURRENT``, and restarts from the new release
with a health gate (``/api/health`` must report the release build,
then ``check`` must pass). Any failure restarts the previous release
and reports the reason. ``rollback`` performs the same switch back on
demand.

Read-only on sources: the source repo is only read (``rev-parse``,
``status``, ``worktree add`` registers the new worktree handle in its
``.git``) and no working checkout outside the releases directory is
ever written. Fetching is deliberately left out: deploy uses the ref
as it currently resolves, so run ``git fetch`` first when the latest
upstream commit is wanted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_REF = "origin/main"
CURRENT_NAME = "CURRENT"
PREVIOUS_NAME = "PREVIOUS"
GATE_TESTS = ("test_page_rules.py", "test_server.py")
GATE_TIMEOUT_S = 900.0
RESTART_TIMEOUT_S = 150.0
CHECK_TIMEOUT_S = 300.0
HEALTH_POLL_S = 0.5


class DeployError(Exception):
    """A deploy or rollback step that cannot proceed. The message names the fix."""


def releases_dir(state_dir: Path | str) -> Path:
    """``<state-dir>/releases`` (created on demand by ``ensure_release``)."""
    return Path(state_dir) / "releases"


def _read_link(link: Path) -> Path | None:
    """The symlink target when it exists on disk, else None."""
    try:
        if not link.is_symlink():
            return None
        target = Path(os.readlink(link))
    except OSError:
        return None
    if not target.is_absolute():
        target = link.parent / target
    if not target.exists():
        return None
    return target


def _set_link(link: Path, target: Path) -> None:
    """Point ``link`` at ``target`` atomically (temp symlink + replace)."""
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.parent / (link.name + f".tmp-{os.getpid()}")
    try:
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        tmp.symlink_to(target)
        os.replace(tmp, link)
    finally:
        try:
            if tmp.is_symlink():
                tmp.unlink()
        except OSError:
            pass


def current_release(state_dir: Path | str) -> Path | None:
    """The release ``CURRENT`` names, or None when unset or removed."""
    return _read_link(releases_dir(state_dir) / CURRENT_NAME)


def previous_release(state_dir: Path | str) -> Path | None:
    """The release ``PREVIOUS`` names, or None when unset or removed."""
    return _read_link(releases_dir(state_dir) / PREVIOUS_NAME)


def set_current(state_dir: Path | str, release: Path | str) -> None:
    """Record ``release`` as the running release."""
    _set_link(releases_dir(state_dir) / CURRENT_NAME, Path(release))


def set_previous(state_dir: Path | str, release: Path | str) -> None:
    """Record ``release`` as the release before the running one."""
    _set_link(releases_dir(state_dir) / PREVIOUS_NAME, Path(release))


def default_repo(tool_root: Path | str | None = None) -> str:
    """The git toplevel containing the tool, or the tool dir outside git."""
    root = str(tool_root if tool_root is not None else TOOL_ROOT)
    try:
        proc = subprocess.run(
            ["git", "-C", root, "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return root
    top = (proc.stdout or "").strip()
    return top if proc.returncode == 0 and top else root


def resolve_sha(repo: Path | str, ref: str) -> str:
    """The full commit sha ``ref`` resolves to in ``repo`` (read-only)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeployError(f"cannot read {repo}: {type(exc).__name__}") from exc
    sha = (proc.stdout or "").strip()
    if proc.returncode != 0 or len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise DeployError(
            f"cannot resolve ref {ref!r} in {repo}. "
            f"Run: git -C {repo} fetch, or pass --ref <sha|branch>"
        )
    return sha


def _git_in(repo: Path | str, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, (proc.stdout or "").strip()


def ensure_release(
    repo: Path | str,
    ref: str,
    state_dir: Path | str,
    worktree: Path | str | None = None,
) -> tuple[Path, str, str]:
    """Create or reuse the clean detached worktree for ``ref``.

    Returns ``(release_path, full_sha, expected_build_id)``. An
    existing path is reused only when it is at exactly ``sha`` with a
    clean status; anything else (wrong commit, dirty, not a repo)
    raises instead of touching it.
    """
    sha = resolve_sha(repo, ref)
    target = Path(worktree) if worktree is not None else releases_dir(state_dir) / sha
    if target.exists() or target.is_symlink():
        code, head = _git_in(target, "rev-parse", "HEAD")
        if code != 0 or not head:
            raise DeployError(
                f"release path {target} is not a git checkout. "
                f"Move it aside and re-run: python3 agent_dash.py deploy --ref {ref}"
            )
        if head != sha:
            raise DeployError(
                f"release path {target} is at {head}, not {sha}. "
                f"Move it aside and re-run: python3 agent_dash.py deploy --ref {ref}"
            )
        code, dirty = _git_in(target, "status", "--porcelain")
        if code != 0 or any(line.strip() for line in dirty.splitlines()):
            raise DeployError(
                f"release path {target} has uncommitted changes. "
                f"Move it aside and re-run: python3 agent_dash.py deploy --ref {ref}"
            )
    else:
        releases_dir(state_dir).mkdir(parents=True, exist_ok=True)
        code, out = _git_in(repo, "worktree", "add", "--detach", str(target), sha)
        if code != 0:
            raise DeployError(f"cannot create release worktree at {target}: {out or 'git worktree add failed'}")
    code, short = _git_in(target, "rev-parse", "--short", "HEAD")
    if code != 0 or not short:
        raise DeployError(f"cannot read the release build in {target}")
    return target, sha, short


def tool_dir_of(release: Path | str) -> Path:
    """The tool directory inside a release worktree (or the release itself)."""
    release = Path(release)
    nested = release / "tools" / "agent-dash"
    if (nested / "agent_dash.py").is_file():
        return nested
    return release


def _scrubbed_env() -> dict:
    """The inherited env minus ``AGENT_DASH_PORT``.

    The fallback gate runs the repo's own test files, which choose
    their ports via the in-process seam; a trial port leaking in would
    rebind them all onto one port. Custom ``gate.sh`` keeps the full
    env (it owns its own contract).
    """
    env = dict(os.environ)
    env.pop("AGENT_DASH_PORT", None)
    return env


def _run(argv: list[str], cwd: Path, timeout_s: float, label: str, env=None) -> tuple[bool, str]:
    """Run ``argv`` in ``cwd``. Returns ``(ok, tail-of-output-on-failure)``."""
    try:
        proc = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(cwd),
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{label} failed: {type(exc).__name__}"
    if proc.returncode == 0:
        return True, ""
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-8:]
    detail = "\n".join(tail).strip() or f"exit {proc.returncode}"
    return False, f"{label} failed: {detail}"


def run_gate(release: Path | str) -> tuple[bool, str]:
    """Gate the release: ``scripts/gate.sh`` when present, else the
    page-rules and server test files. Returns ``(ok, reason)``."""
    tool = tool_dir_of(release)
    gate = tool / "scripts" / "gate.sh"
    if gate.is_file():
        ok, reason = _run(["sh", str(gate)], tool, GATE_TIMEOUT_S, str(gate))
        if not ok:
            return False, reason
        return True, f"gate passed ({gate.name})"
    for name in GATE_TESTS:
        ok, reason = _run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", name],
            tool,
            GATE_TIMEOUT_S,
            name,
            env=_scrubbed_env(),
        )
        if not ok:
            return False, reason
    return True, f"gate passed ({' + '.join(GATE_TESTS)})"


def fetch_health(url: str, timeout_s: float = 5.0) -> dict | None:
    """GET ``url/api/health`` as a dict, or None when unreachable."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def wait_healthy(url: str, expected_build: str, timeout_s: float = 25.0) -> tuple[bool, str]:
    """Wait until health answers with ``build_id == expected_build``."""
    deadline = time.time() + timeout_s
    seen = "no answer"
    while time.time() < deadline:
        health = fetch_health(url)
        if isinstance(health, dict):
            seen = str(health.get("build_id") or "no build_id")
            if seen == expected_build:
                return True, ""
        time.sleep(HEALTH_POLL_S)
    return False, f"/api/health never reported build {expected_build} (last saw {seen})"


SETTLE_TIMEOUT_S = 60.0


def wait_settled(url: str, timeout_s: float = SETTLE_TIMEOUT_S) -> tuple[bool, str]:
    """Wait until every source completed at least one poll.

    A fresh restart has empty views until the first polls land; running
    ``check`` before that reports workers the next poll would have
    shown. Non-fatal: on expiry the caller still runs ``check``, which
    reports genuinely stalled sources itself.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        health = fetch_health(url)
        if isinstance(health, dict):
            sources = health.get("sources") or []
            if all(isinstance(item, dict) and item.get("last_poll") is not None for item in sources):
                return True, ""
        time.sleep(1.0)
    return False, f"sources did not all poll within {timeout_s:.0f} s; running check anyway"


def restart_release(release_tool: Path | str, yes_live: bool = False) -> tuple[bool, str]:
    """Restart the dashboard from a release checkout. Returns ``(ok, reason)``.

    ``yes_live`` forwards ``--yes-live`` so the restart passes the
    state-dir guard when the target is the live dashboard.
    """
    cli = Path(release_tool) / "agent_dash.py"
    argv = [sys.executable, str(cli), "restart"]
    if yes_live:
        argv.append("--yes-live")
    return _run(argv, Path(release_tool), RESTART_TIMEOUT_S, "restart")


def run_check(release_tool: Path | str) -> tuple[bool, str]:
    """Run the release's own ``check``. Returns ``(ok, reason)``."""
    cli = Path(release_tool) / "agent_dash.py"
    return _run([sys.executable, str(cli), "check"], Path(release_tool), CHECK_TIMEOUT_S, "check")


def _release_build(release: Path) -> str | None:
    code, short = _git_in(release, "rev-parse", "--short", "HEAD")
    return short if code == 0 and short else None


def _recover(state_dir: Path | str, prev: Path | None, url: str | None, health_timeout_s: float, yes_live: bool = False) -> str:
    """Best-effort restart of the previous release after a failed deploy."""
    if prev is None:
        return "no previous release recorded; the dashboard was left as-is"
    set_current(state_dir, prev)
    ok, reason = restart_release(tool_dir_of(prev), yes_live)
    if not ok:
        return f"automatic rollback to {prev} failed to restart: {reason}"
    prev_build = _release_build(prev)
    if url is not None and prev_build is not None:
        ok, reason = wait_healthy(url, prev_build, timeout_s=health_timeout_s)
        if not ok:
            return f"rolled back to {prev} but {reason}"
    return f"restored previous release {prev}"


def deploy(
    state_dir: Path | str,
    repo: Path | str,
    ref: str = DEFAULT_REF,
    worktree: Path | str | None = None,
    url: str | None = None,
    health_timeout_s: float = 25.0,
    yes_live: bool = False,
) -> tuple[bool, str]:
    """Deploy ``ref`` as a clean release and switch the dashboard to it.

    Returns ``(ok, message)``; failures name the reason and the
    recovery. The previous release restarts automatically on failure.
    ``yes_live`` forwards ``--yes-live`` to the release restarts.
    """
    state_dir = Path(state_dir)
    try:
        release, sha, expected = ensure_release(repo, ref, state_dir, worktree)
    except DeployError as exc:
        return False, str(exc)
    prev = current_release(state_dir)
    ok, reason = run_gate(release)
    if not ok:
        recovered = _recover(state_dir, prev, url, health_timeout_s, yes_live)
        return False, f"gate failed for {sha}: {reason}; {recovered}"
    set_current(state_dir, release)
    ok, reason = restart_release(tool_dir_of(release), yes_live)
    if not ok:
        recovered = _recover(state_dir, prev, url, health_timeout_s, yes_live)
        return False, f"restart of {sha} failed: {reason}; {recovered}"
    if url is not None:
        ok, reason = wait_healthy(url, expected, timeout_s=health_timeout_s)
        if not ok:
            recovered = _recover(state_dir, prev, url, health_timeout_s, yes_live)
            return False, f"new release unhealthy: {reason}; {recovered}"
        wait_settled(url)
    ok, reason = run_check(tool_dir_of(release))
    if not ok:
        recovered = _recover(state_dir, prev, url, health_timeout_s, yes_live)
        return False, f"check failed on {sha}: {reason}; {recovered}"
    if prev is not None and prev != release:
        set_previous(state_dir, prev)
        previous = f"; previous release: {prev}"
    else:
        previous = ""
    return True, f"deployed {sha} (build {expected}) from {release}{previous}"


def rollback(
    state_dir: Path | str,
    url: str | None = None,
    health_timeout_s: float = 25.0,
    yes_live: bool = False,
) -> tuple[bool, str]:
    """Switch the dashboard back to the previous release manually.

    Returns ``(ok, message)``; failures swap the links back and name
    the reason. ``yes_live`` forwards ``--yes-live`` to the restart.
    """
    state_dir = Path(state_dir)
    cur = current_release(state_dir)
    prev = previous_release(state_dir)
    if prev is None:
        return False, (
            f"no previous release recorded in {releases_dir(state_dir)}. "
            "Run: python3 agent_dash.py deploy"
        )
    if cur is not None and prev == cur:
        return False, f"previous release is already current ({prev}); nothing to roll back"
    if cur is not None:
        set_previous(state_dir, cur)
    set_current(state_dir, prev)
    prev_build = _release_build(prev)
    ok, reason = restart_release(tool_dir_of(prev), yes_live)
    if not ok and cur is not None:
        set_current(state_dir, cur)
        set_previous(state_dir, prev)
        return False, f"rollback to {prev} failed to restart: {reason}; kept {cur}"
    if not ok:
        return False, f"rollback to {prev} failed to restart: {reason}"
    if url is not None and prev_build is not None:
        ok, reason = wait_healthy(url, prev_build, timeout_s=health_timeout_s)
        if not ok and cur is not None:
            set_current(state_dir, cur)
            set_previous(state_dir, prev)
            return False, f"rollback to {prev} unhealthy: {reason}; kept {cur}"
        if not ok:
            return False, f"rollback to {prev} unhealthy: {reason}"
        wait_settled(url)
    ok, reason = run_check(tool_dir_of(prev))
    if not ok and cur is not None:
        set_current(state_dir, cur)
        set_previous(state_dir, prev)
        return False, f"check failed on {prev}: {reason}; kept {cur}"
    if not ok:
        return False, f"check failed on {prev}: {reason}"
    return True, f"rolled back to {prev}" + (f" (was {cur})" if cur is not None else "")
