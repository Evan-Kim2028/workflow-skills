"""Run-folder registration for manifest-reading sources.

A harness (``scripts/run_worker.py``, ``agent_dash.py report --run-dir``)
writes its manifests wherever the lead puts the run folder. That folder
registers itself here so the dashboard finds it without any
``roots.scratchpads`` edit:

``<state-dir>/run-dirs.txt`` — one absolute path per line.

Sources scan the registered folders in addition to the configured
scratch roots (see ``scan_roots``). Entries that no longer exist are
skipped; entries untouched for ``PRUNE_AFTER_S`` (14 days) are dropped
from the file. The file never holds prompts, argv, or secrets: paths
only.
"""

from __future__ import annotations

import os
from pathlib import Path

FILE_NAME = "run-dirs.txt"
MAX_ENTRIES = 256
PRUNE_AFTER_S = 14 * 86400


def state_dir_of(cfg: dict | None = None) -> Path:
    """State dir for run registration: config, env, then the default."""
    if isinstance(cfg, dict):
        raw = cfg.get("state_dir")
        if isinstance(raw, str) and raw.strip():
            return Path(_expand(raw))
    env = os.environ.get("AGENT_DASH_STATE_DIR")
    if env and env.strip():
        return Path(_expand(env.strip()))
    return Path.home() / ".local" / "share" / "agent-dash"


def register_run_dir(state_dir, run_dir) -> bool:
    """Append ``run_dir`` to ``run-dirs.txt`` when missing. Atomic, deduped.

    Returns True when the directory is now registered. Never raises:
    registration must not break a launch.
    """
    try:
        path = Path(run_dir)
        if not path.is_absolute():
            path = Path(os.path.abspath(str(run_dir)))
        state = Path(state_dir)
        state.mkdir(parents=True, exist_ok=True)
        target = state / FILE_NAME
        lines = _read_lines(target)
        text = str(path)
        if text in lines:
            return True
        lines.append(text)
        lines = _prune_missing(lines)
        if len(lines) > MAX_ENTRIES:
            lines = lines[-MAX_ENTRIES:]
        _write_lines(target, lines)
        return True
    except (OSError, ValueError):
        return False


def registered_run_dirs(state_dir=None, cfg=None, now=None) -> list:
    """Existing registered run folders, oldest registration first.

    Reads ``cfg["state_dir"]`` when ``state_dir`` is None. Folders that no
    longer exist are skipped; folders untouched for ``PRUNE_AFTER_S`` are
    dropped from the file (the rewrite is atomic). Never raises.
    """
    try:
        if state_dir is None:
            state = state_dir_of(cfg)
        else:
            raw = str(state_dir)
            state = Path(_expand(raw)) if raw.strip() else state_dir_of(cfg)
    except (OSError, ValueError):
        return []
    try:
        stamp = float(now) if now is not None else _now()
        lines = _read_lines(state / FILE_NAME)
        if not lines:
            return []
        keep = []
        changed = False
        for text in lines:
            if not os.path.isdir(text):
                changed = True
                continue
            if stamp - _touched(text) > PRUNE_AFTER_S:
                changed = True
                continue
            keep.append(text)
        if changed:
            try:
                _write_lines(state / FILE_NAME, keep[-MAX_ENTRIES:])
            except (OSError, ValueError):
                pass
        return keep
    except (OSError, ValueError):
        return []


def scan_roots(cfg) -> list:
    """Configured scratch roots plus registered run folders, deduped.

    Sources that read manifest files iterate this instead of only the
    configured ``roots.scratchpads``, so a run folder shows up wherever
    the lead put it. Order is stable: configured roots first.
    """
    from dash.locate import scratch_roots

    try:
        roots = list(scratch_roots(cfg) or [])
    except (OSError, ValueError):
        roots = []
    try:
        extra = registered_run_dirs(cfg=cfg)
    except (OSError, ValueError):
        extra = []
    seen = set()
    for root in roots:
        try:
            seen.add(os.path.realpath(root))
        except OSError:
            seen.add(root)
    out = list(roots)
    for path in extra:
        try:
            real = os.path.realpath(path)
        except OSError:
            real = path
        if real in seen:
            continue
        seen.add(real)
        out.append(path)
    return out


def describe_roots(cfg, now=None, processes="auto") -> dict:
    """Scratch-root visibility report for ``/api/health``.

    Returns ``scratch_roots`` (the configured, resolved list),
    ``run_dirs`` (the registered, existing folders), and ``warnings``:
    one entry per registered folder outside the configured roots that
    holds a manifest for a currently live worker. A local config that
    sets ``roots.scratchpads`` without the run folder a lead chose shows
    up here instead of silently dropping those workers.
    """
    from dash.locate import scratch_roots

    try:
        roots = list(scratch_roots(cfg) or [])
    except (OSError, ValueError):
        roots = []
    try:
        reg = registered_run_dirs(cfg=cfg, now=now)
    except (OSError, ValueError):
        reg = []
    live = _live_labels(processes)
    warnings = []
    for rundir in reg:
        if _covered(rundir, roots):
            continue
        labels = _manifest_labels(rundir)
        if not labels:
            continue
        if live is None:
            warnings.append(
                "worker manifests outside roots.scratchpads: %s (liveness unknown)" % rundir
            )
            continue
        hit = sorted(set(labels) & live)
        if not hit:
            continue
        extra = "" if len(hit) == 1 else " (+%d more)" % (len(hit) - 1)
        warnings.append(
            "live worker manifest outside roots.scratchpads: %s in %s%s"
            % (hit[0], rundir, extra)
        )
    return {"scratch_roots": roots, "run_dirs": reg, "warnings": warnings}


def _live_labels(processes) -> set | None:
    """Labels of live worker processes, or None when the listing failed."""
    if processes is None:
        return None
    if processes == "auto":
        try:
            from dash.sources.process import live_worker_processes

            rows = live_worker_processes() or []
        except Exception:
            return None
    else:
        rows = processes
    found = set()
    for row in rows or []:
        if isinstance(row, dict) and row.get("label"):
            found.add(row["label"])
    return found


def _covered(path: str, roots: list) -> bool:
    try:
        real = os.path.realpath(path)
    except OSError:
        real = path
    for root in roots:
        try:
            want = os.path.realpath(root)
        except OSError:
            want = root
        if real == want or real.startswith(want + os.sep):
            return True
    return False


def _manifest_labels(rundir: str) -> list:
    """First-column labels of manifest TSVs directly in ``rundir``.

    Bounded (1 MB, 2000 lines per file): labels only, never file bodies.
    """
    labels = []
    try:
        names = os.listdir(rundir)
    except OSError:
        return labels
    for name in sorted(names):
        if not name.endswith("-sessions.tsv"):
            continue
        path = os.path.join(rundir, name)
        try:
            with open(path, "rb") as fh:
                blob = fh.read(1024 * 1024)
        except OSError:
            continue
        for lineno, line in enumerate(blob.split(b"\n")):
            if lineno >= 2000:
                break
            try:
                text = line.decode("utf-8", "replace")
            except ValueError:
                continue
            text = text.strip().lstrip("\ufeff")
            if not text or text.startswith("#"):
                continue
            label = " ".join(text.split("\t")[0].split())
            if label and "/" not in label and "\\" not in label and not label.startswith("."):
                labels.append(label[:200])
    return labels


def _touched(path: str) -> float:
    """Latest mtime of the folder or its direct children (one scandir)."""
    try:
        latest = os.stat(path).st_mtime
    except OSError:
        return 0.0
    try:
        with os.scandir(path) as kids:
            for kid in kids:
                try:
                    stamp = kid.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
                if stamp > latest:
                    latest = stamp
    except OSError:
        pass
    return float(latest)


def _read_lines(target: Path) -> list:
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line not in lines:
            lines.append(line)
    return lines


def _prune_missing(lines: list) -> list:
    return [line for line in lines if os.path.isdir(line)]


def _write_lines(target: Path, lines: list) -> None:
    tmp = target.with_suffix(".txt.tmp")
    tmp.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    os.replace(tmp, target)


def _expand(value: str) -> str:
    text = os.path.expanduser(os.path.expandvars(value))
    if "$HOME" in text:
        text = text.replace("$HOME", str(Path.home()))
    return text


def _now() -> float:
    import time

    return time.time()
