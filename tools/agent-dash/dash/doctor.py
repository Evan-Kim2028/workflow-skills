"""`agent_dash.py doctor`: diagnose setup problems and print the fix.

Read-only: doctor never changes anything. It only reads the dashboard
HTTP API, the state directory, config files, scratch roots, bundles,
and the local toolchain. Exit 0 when clean, 1 when problems found.
Warnings (a dirty checkout) are listed but do not fail.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]

KNOWN_TOP_KEYS = {
    "host",
    "port",
    "read_budget_per_file",
    "read_budget_per_tick",
    "read_budget",
    "roots",
    "hub_dir",
    "issues_dir",
    "workstreams_tsv",
    "state_dir",
    "backfill",
    "backfill_session",
    "dagster",
    "caps",
}
KNOWN_ROOT_KEYS = {
    "claude_projects",
    "claude_sessions",
    "muse_sessions",
    "grok_sessions",
    "grok_log",
    "cursor_home",
    "scratchpads",
    "projects",
    "scratch",
    "scratchpad_root",
    "scratch_root",
    "claude_tmp",
}
MANIFEST_NAMES = (
    "muse-sessions.tsv",
    "glm-sessions.tsv",
    "grok-sessions.tsv",
    "cursor-sessions.tsv",
)
RUN_DIRS_NAMES = ("run-dirs.txt", "run-dirs.json", "run_dirs.json")
SOURCE_STALE_S = 900.0
MIN_PYTHON = (3, 9)


def run_doctor(cfg: dict, url: str, now: float | None = None) -> dict:
    """Run every check. Returns ``{"ok", "problems", "checks"}``."""
    import time as _time

    now = float(now if now is not None else _time.time())
    state_dir = Path(str(cfg.get("state_dir") or "")) if cfg.get("state_dir") else None
    checks: list[dict] = []
    problems: list[dict] = []

    def problem(code: str, message: str, command: str, warning: bool = False):
        problems.append({"code": code, "message": message, "command": command, **({"warning": True} if warning else {})})

    def check(name: str, ok: bool, detail: str):
        checks.append({"name": name, "ok": ok, "detail": detail})
        return ok

    health = _get(url, "/api/health", timeout=10)
    if health is None:
        check("dashboard", False, f"not reachable at {url}")
        problem("dashboard-down", f"dashboard not reachable at {url}", "python3 agent_dash.py run")
    else:
        sources = health.get("sources") or []
        bad = [s.get("name") for s in sources if s.get("last_error")]
        if health.get("ok") and not bad:
            check("dashboard", True, f"up at {url}, {len(sources)} sources healthy")
        else:
            unpriced = [
                str(item.get("model"))
                for item in (health.get("unpriced_models") or [])
                if isinstance(item, dict) and item.get("model")
            ]
            stalled = [s.get("name") for s in sources if s.get("stalled")]
            if bad:
                cause = f"failing sources: {', '.join(bad)}"
            elif stalled:
                cause = f"stalled sources: {', '.join(stalled)}"
            elif unpriced:
                cause = f"unpriced models: {', '.join(unpriced)}"
            else:
                cause = "ok=false"
            check("dashboard", False, f"unhealthy: {cause}")
            problem(
                "dashboard-unhealthy",
                f"dashboard at {url} reports {cause}",
                "python3 agent_dash.py status",
            )
        stale = [
            s.get("name")
            for s in sources
            if not s.get("last_error")
            and (s.get("last_poll") is None or (s.get("lag_s") or 0) > SOURCE_STALE_S)
        ]
        if not stale:
            check("sources", True, f"{len(sources)} sources polled recently, no errors")
        else:
            check("sources", False, f"stale: {', '.join(stale)}")
            for name in stale:
                problem(
                    "source-stale",
                    f"source {name!r} has no recent poll",
                    "python3 agent_dash.py status",
                )
        for source in sources:
            if source.get("last_error"):
                problem(
                    "source-error",
                    f"source {source.get('name')!r} last poll failed: {source.get('last_error')}",
                    "python3 agent_dash.py status",
                )

    _check_health_roots(health, checks, problem)
    _check_scratch(cfg, checks, problem)
    _check_manifests(cfg, state_dir, checks, problem)
    _check_pricing(cfg, checks, problem)
    _check_reconcile(url, health is not None, checks, problem)
    _check_page(checks, problem)
    _check_config(cfg, checks, problem)
    _check_bundles(checks, problem)
    _check_checkout(checks, problem)
    _check_python(checks, problem)

    hard = [item for item in problems if not item.get("warning")]
    return {"ok": not hard, "problems": problems, "checks": checks}


def format_human(result: dict) -> str:
    lines = []
    for item in result.get("checks") or []:
        mark = "ok" if item.get("ok") else "FAIL"
        lines.append(f"[{mark}] {item.get('name')}: {item.get('detail')}")
    problems = result.get("problems") or []
    if problems:
        lines.append("")
        for item in problems:
            tag = "warning" if item.get("warning") else "problem"
            lines.append(f"{tag} ({item.get('code')}): {item.get('message')}")
            lines.append(f"  fix: {item.get('command')}")
    else:
        lines.append("doctor: clean, no problems found")
    return "\n".join(lines)


def _get(url: str, path: str, timeout: float = 10):
    try:
        with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _expand(pattern: str) -> list[str]:
    from dash.locate import expand

    text = expand(str(pattern)).strip()
    if not text:
        return []
    if any(ch in text for ch in "*?["):
        return sorted(item for item in glob.glob(text) if os.path.isdir(item))
    return [text] if os.path.isdir(text) else []


def _scratch_patterns(cfg: dict) -> list[str]:
    roots = cfg.get("roots") if isinstance(cfg.get("roots"), dict) else {}
    raw = roots.get("scratchpads")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def _check_health_roots(health, checks: list, problem) -> None:
    """Surface the dashboard's own run-dir visibility report.

    ``/api/health`` carries ``scratch_roots`` (configured), ``run_dirs``
    (registered), and ``warnings`` (a registered folder outside the
    configured roots holding a live worker manifest). Each warning is a
    problem with the same fix as the local manifest-coverage check.
    """
    if health is None:
        checks.append({"name": "run-dirs", "ok": True, "detail": "skipped, dashboard is down"})
        return
    roots = health.get("scratch_roots")
    run_dirs = health.get("run_dirs")
    warnings = health.get("warnings") or []
    if roots is None and run_dirs is None and not warnings:
        checks.append({"name": "run-dirs", "ok": True, "detail": "dashboard reports no run-dir data"})
        return
    detail = f"{len(run_dirs or [])} registered run dirs, {len(roots or [])} scratch roots"
    if not warnings:
        checks.append({"name": "run-dirs", "ok": True, "detail": detail})
        return
    checks.append({"name": "run-dirs", "ok": False, "detail": f"{detail}; {len(warnings)} outside roots"})
    for warning in warnings:
        problem(
            "run-dir-outside-roots",
            str(warning),
            "add the path to roots.scratchpads in config, then restart the dashboard",
        )


def _check_scratch(cfg: dict, checks: list, problem) -> None:
    from dash.locate import expand

    patterns = _scratch_patterns(cfg)
    if not patterns:
        checks.append({"name": "scratch-roots", "ok": True, "detail": "no scratchpads configured"})
        return
    missing = []
    unreadable = []
    found = 0
    for pattern in patterns:
        text = expand(str(pattern)).strip()
        matches = _expand(pattern)
        if not matches:
            missing.append(text or pattern)
            continue
        for match in matches:
            found += 1
            if not (os.access(match, os.R_OK) and os.access(match, os.X_OK)):
                unreadable.append(match)
    if not missing and not unreadable:
        checks.append({"name": "scratch-roots", "ok": True, "detail": f"{found} roots readable"})
        return
    detail = "; ".join(
        ([f"missing: {', '.join(missing)}"] if missing else [])
        + ([f"unreadable: {', '.join(unreadable)}"] if unreadable else [])
    )
    checks.append({"name": "scratch-roots", "ok": False, "detail": detail})
    for path in missing:
        problem(
            "scratch-root-missing",
            f"scratch root {path!r} matches nothing",
            "create the directory, or fix roots.scratchpads in config, then restart the dashboard",
        )
    for path in unreadable:
        problem(
            "scratch-root-unreadable",
            f"scratch root {path!r} is not readable",
            f"fix permissions on {path}, then restart the dashboard",
        )


def _home_scratch() -> str:
    return os.path.join(os.path.expanduser("~"), "agent-scratch")


def _scanned_dirs(cfg: dict) -> set[str]:
    """Run dirs the collector reads: the shipped scratch glob plus configured roots."""
    scanned: set[str] = set()
    for pattern in _scratch_patterns(cfg):
        scanned.update(_expand(pattern))
    home_scratch = _home_scratch()
    if os.path.isdir(home_scratch):
        scanned.update(
            item for item in glob.glob(os.path.join(home_scratch, "*")) if os.path.isdir(item)
        )
    return scanned


def _registry_dirs(state_dir: Path | None) -> list[str]:
    """Extra known run dirs from the run-dirs registry file in the state dir, if any.

    ``run-dirs.txt`` holds one absolute path per line (what
    ``run_worker.py`` and ``report --run-dir`` write); the ``.json``
    names keep working for older checkouts.
    """
    if state_dir is None:
        return []
    found = []
    for name in RUN_DIRS_NAMES:
        file = state_dir / name
        if not file.is_file():
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except OSError:
            continue
        if file.suffix == ".txt":
            found.extend(line.strip() for line in text.splitlines() if line.strip())
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, list):
            found.extend(str(item) for item in data if isinstance(item, str) and item)
    return found


def _manifest_dirs(places: list[str]) -> list[str]:
    """Dirs holding a live (non-empty) worker manifest, checked here and one level down."""
    found = []
    seen = set()

    def consider(path: str) -> None:
        if path in seen:
            return
        seen.add(path)
        try:
            names = os.listdir(path)
        except OSError:
            return
        live = [name for name in names if name in MANIFEST_NAMES and _nonempty(os.path.join(path, name))]
        if live:
            found.append(f"{path} ({', '.join(sorted(live))})")

    for place in places:
        if not os.path.isdir(place):
            continue
        consider(place)
        try:
            children = os.listdir(place)
        except OSError:
            continue
        for child in sorted(children):
            full = os.path.join(place, child)
            if os.path.isdir(full):
                consider(full)
    return sorted(found)


def _covered(path: str, scanned: set[str]) -> bool:
    base = path.split(" (")[0]
    return base in scanned or os.path.dirname(base) in scanned


def _check_manifests(cfg: dict, state_dir: Path | None, checks: list, problem) -> None:
    scanned = _scanned_dirs(cfg)
    home_scratch = _home_scratch()
    places = [home_scratch] + sorted(scanned) + _registry_dirs(state_dir)
    outside = [item for item in _manifest_dirs(places) if not _covered(item, scanned)]
    if not outside:
        checks.append({"name": "manifest-coverage", "ok": True, "detail": f"{len(scanned)} run dirs scanned"})
        return
    checks.append({"name": "manifest-coverage", "ok": False, "detail": f"manifests outside scanned roots: {'; '.join(outside)}"})
    for item in outside:
        problem(
            "manifest-outside-roots",
            f"live worker manifest at {item} is outside the scanned roots",
            "add the path to roots.scratchpads in config, then restart the dashboard",
        )


def _nonempty(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _db_path(cfg: dict) -> Path | None:
    from dash.locate import expand

    override = os.environ.get("AGENT_DASH_DB")
    if override:
        return Path(expand(override))
    state = cfg.get("state_dir")
    if not state:
        return None
    return Path(expand(str(state))) / "dash.sqlite"


def _recent_models(db: Path) -> list[dict]:
    """Models with usage rows, read-only (URI mode=ro, never creates files)."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT model AS model, COUNT(*) AS rows FROM usage_events GROUP BY model").fetchall()
        return [{"model": row["model"], "rows": row["rows"]} for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _check_pricing(cfg: dict, checks: list, problem) -> None:
    from dash.pricing import catalog as load_catalog

    try:
        rows = {row["model"]: row for row in load_catalog()}
    except Exception as exc:
        checks.append({"name": "pricing", "ok": False, "detail": f"catalog unreadable: {exc}"})
        problem(
            "pricing-unreadable",
            f"catalog/prices.json is unreadable: {exc}",
            "restore tools/agent-dash/catalog/prices.json, then run: python3 agent_dash.py reprice",
        )
        return
    db = _db_path(cfg)
    models: list[dict] = []
    if db is not None and db.is_file():
        models = _recent_models(db)
    missing = [
        item["model"]
        for item in models
        if item.get("model")
        and (item["model"] not in rows or rows[item["model"]].get("input") is None or rows[item["model"]].get("output") is None)
    ]
    if not missing:
        checks.append({"name": "pricing", "ok": True, "detail": f"{len(rows)} price rows, {len(models)} models on record priced"})
        return
    checks.append({"name": "pricing", "ok": False, "detail": f"unpriced models: {', '.join(missing)}"})
    for model in missing:
        problem(
            "unpriced-model",
            f"model {model!r} has usage rows with no price (cost counts unpriced runs, never $0)",
            "add a row to tools/agent-dash/catalog/prices.json, then run: python3 agent_dash.py reprice",
        )


def _check_reconcile(url: str, up: bool, checks: list, problem) -> None:
    if not up:
        checks.append({"name": "reconcile", "ok": True, "detail": "skipped, dashboard is down"})
        return
    payload = _get(url, "/api/reconcile?hours=24", timeout=15)
    if payload is None:
        checks.append({"name": "reconcile", "ok": False, "detail": "reconcile endpoint unreadable"})
        problem("reconcile-unreadable", "GET /api/reconcile failed", "python3 agent_dash.py status")
        return
    mismatches = payload.get("mismatches") or []
    if not mismatches:
        rows = payload.get("rows") or []
        checks.append({"name": "reconcile", "ok": True, "detail": f"{len(rows)} workstreams reconcile, no mismatches"})
        return
    checks.append({"name": "reconcile", "ok": False, "detail": f"{len(mismatches)} mismatches"})
    for item in mismatches:
        problem("reconcile-mismatch", str(item), "python3 agent_dash.py check")


def _chrome_path() -> str | None:
    explicit = os.environ.get("CHROME_PATH")
    if explicit and os.path.exists(explicit):
        return explicit
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if sys.platform == "darwin" and os.path.exists(mac):
        return mac
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _check_page(checks: list, problem) -> None:
    node = shutil.which("node")
    chrome = _chrome_path()
    if node and chrome:
        checks.append({"name": "page-check", "ok": True, "detail": f"node {node}, chrome {chrome}"})
        return
    missing = [name for name, found in (("node", node), ("chrome", chrome)) if not found]
    checks.append({"name": "page-check", "ok": False, "detail": f"missing: {', '.join(missing)}"})
    if not node:
        problem(
            "page-check-no-node",
            "node is not on PATH, so check --page always skips",
            "install Node 18+, then run: python3 agent_dash.py check --page",
        )
    if not chrome:
        problem(
            "page-check-no-chrome",
            "no Chrome binary found (CHROME_PATH, the default install path, or PATH)",
            "set CHROME_PATH to a Chrome binary, then run: python3 agent_dash.py check --page",
        )


def _config_files() -> list[Path]:
    files = [TOOL_ROOT / "config.example.json", TOOL_ROOT / "config.local.json"]
    override = os.environ.get("AGENT_DASH_CONFIG")
    if override:
        from dash.locate import expand

        files.append(Path(expand(override)))
    return files


def _check_config(cfg: dict, checks: list, problem) -> None:
    del cfg
    bad = []
    unknown = []
    for file in _config_files():
        if file.name != "config.example.json" and not file.is_file():
            continue
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            bad.append(f"{file}: {exc}")
            continue
        if not isinstance(data, dict):
            bad.append(f"{file}: top level must be an object")
            continue
        for key in data:
            if key not in KNOWN_TOP_KEYS:
                unknown.append(f"{file.name}: unknown key {key!r}")
        roots = data.get("roots")
        if isinstance(roots, dict):
            for key in roots:
                if key not in KNOWN_ROOT_KEYS:
                    unknown.append(f"{file.name}: unknown roots key {key!r}")
    if bad:
        checks.append({"name": "config", "ok": False, "detail": "; ".join(bad)})
        for item in bad:
            problem("config-unparseable", item, "fix the JSON, then restart the dashboard")
    for item in unknown:
        problem("config-unknown-key", item, "remove the key, then restart the dashboard")
    if unknown and not bad:
        checks.append({"name": "config", "ok": False, "detail": "; ".join(unknown)})
    if not bad and not unknown:
        checks.append({"name": "config", "ok": True, "detail": "config files parse, no unknown keys"})


def _check_bundles(checks: list, problem) -> None:
    from dash.bundles import list_bundles, validate_bundle

    try:
        bundles = list_bundles()
    except Exception as exc:
        checks.append({"name": "bundles", "ok": False, "detail": f"bundle list failed: {exc}"})
        problem("bundles-unreadable", f"bundle list failed: {exc}", "python3 agent_dash.py ws list")
        return
    failing = {}
    for item in bundles:
        try:
            issues = validate_bundle(item["id"])
        except Exception:
            continue
        if issues:
            failing[item["id"]] = issues
    if not failing:
        checks.append({"name": "bundles", "ok": True, "detail": f"{len(bundles)} bundles validate"})
        return
    checks.append({"name": "bundles", "ok": False, "detail": f"failing: {', '.join(sorted(failing))}"})
    for ws in sorted(failing):
        issues = failing[ws]
        first = issues[0]
        extra = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
        problem(
            "invalid-bundle",
            f"bundle {ws} fails validation: {first}{extra}",
            f"python3 agent_dash.py ws validate {ws}",
        )


def _check_checkout(checks: list, problem) -> None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(TOOL_ROOT), "status", "--short"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        checks.append({"name": "checkout", "ok": True, "detail": "not a git checkout, skipping"})
        return
    if proc.returncode != 0:
        checks.append({"name": "checkout", "ok": True, "detail": "not a git checkout, skipping"})
        return
    dirty = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    if not dirty:
        checks.append({"name": "checkout", "ok": True, "detail": "clean"})
        return
    checks.append({"name": "checkout", "ok": True, "detail": f"{len(dirty)} dirty files (warning only)"})
    problem(
        "checkout-dirty",
        f"tool checkout has {len(dirty)} uncommitted changes",
        "review with: git status --short",
        warning=True,
    )


def _check_python(checks: list, problem) -> None:
    current = sys.version_info[:3]
    runner = TOOL_ROOT / "scripts" / "run_worker.py"
    if tuple(current) >= MIN_PYTHON and runner.is_file():
        checks.append(
            {
                "name": "python",
                "ok": True,
                "detail": f"python {'.'.join(str(n) for n in current)} runs scripts/run_worker.py",
            }
        )
        return
    if not runner.is_file():
        checks.append({"name": "python", "ok": False, "detail": "scripts/run_worker.py is missing"})
        problem(
            "worker-runner-missing",
            "scripts/run_worker.py is missing",
            "restore tools/agent-dash/scripts/run_worker.py from version control",
        )
        return
    checks.append(
        {"name": "python", "ok": False, "detail": f"python {'.'.join(str(n) for n in current)} is older than {'.'.join(str(n) for n in MIN_PYTHON)}"}
    )
    problem(
        "python-too-old",
        f"python {'.'.join(str(n) for n in current)} is older than scripts/run_worker.py needs",
        "run the dashboard with a newer python3, then run: python3 agent_dash.py status",
    )
