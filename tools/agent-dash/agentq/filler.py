"""Filler targets: read-only module audits that the fill controller (``agentq.fill``) submits.

A target is one module directory of a repo, listed under the ``"filler"`` key of
``$AGENTQ_HOME/backlog-seed.json``:

    "filler": {
        "report_dir": "~/src/state/reports",
        "max_runtime": "15m",
        "repos": {
            "repo-a": {"workspace": "~/src/repo-a", "roots": ["src/api", "packages"]},
            "repo-b": {"workspace": "~/src/repo-b", "modules": ["tools/agent-dash/agentq"]}
        }
    }

``roots`` expands to the module folders directly under each root; ``modules`` names them
outright (paths relative to the workspace). A repo needs one of the two.

Each audit writes ``<report_dir>/filler/<repo>/<module-slug>.md``. The cursor rotates through
targets. A target whose module has not changed since its last audit is skipped, and so is one
whose filler job is still queued or running. This module owns the ``filler_targets`` and
``filler_state`` tables; the broker writes jobs through ``db.insert_job`` as usual.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agentq import backlog as backlog_lib
from agentq import db

PROMPT = Path(__file__).resolve().parent / "backlog_prompts" / "filler.md"
CONFIG_NAME = "backlog-seed.json"
LABEL_MARKER = "-backlog-filler-"
LABEL_SLUG = "backlog-filler"
DEFAULT_MAX_RUNTIME_S = 900.0
SLUG_MAX = 60
GIT_TIMEOUT_S = 5.0
SKIP_DIRS = frozenset({"__pycache__", "node_modules", "build", "dist"})


@dataclass(frozen=True)
class Target:
    """One module to audit. ``module`` is relative to the repo's workspace."""

    repo: str
    module: str
    workspace: str
    report_dir: str

    @property
    def key(self) -> str:
        return f"{self.repo}/{self.module}"

    @property
    def report_path(self) -> str:
        return str(Path(self.report_dir) / "filler" / self.repo / f"{module_slug(self.module)}.md")


def module_slug(text: str) -> str:
    """Lowercase letters, digits and dashes, at most SLUG_MAX characters. A name with none becomes ``root``."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:SLUG_MAX].strip("-") or "root"


def label_slug(target: Target) -> str:
    """The slug passed to ``label make`` for a filler job, so it reads ``hub-exec-backlog-filler-<repo>-<module>``."""
    return f"{LABEL_SLUG}-{module_slug(target.repo)}-{module_slug(target.module)}"


def is_filler_label(label: str) -> bool:
    return LABEL_MARKER in (label or "")


def _folder(spec: dict, name: str, where: str) -> str:
    value = spec.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} needs a {name} folder")
    return str(Path(value.strip()).expanduser().resolve())


def _root_modules(workspace: str, roots: list) -> list[str]:
    modules = []
    for root in roots:
        if not isinstance(root, str) or not root.strip():
            raise ValueError(f"filler root must be a folder path: {root!r}")
        base = Path(workspace, root.strip())
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and child.name not in SKIP_DIRS:
                modules.append(f"{root.strip().rstrip('/')}/{child.name}")
    return modules


def load_targets(config: dict) -> tuple[list[Target], float]:
    """(targets sorted by key, max runtime in seconds) from the seed config's ``filler`` key.

    No ``filler`` key means no targets. Raises ValueError for a malformed section.
    """
    spec = config.get("filler") if isinstance(config, dict) else None
    if not isinstance(spec, dict):
        return [], DEFAULT_MAX_RUNTIME_S
    report_dir = _folder(spec, "report_dir", "filler")
    max_runtime = backlog_lib.parse_duration(spec["max_runtime"]) if "max_runtime" in spec else DEFAULT_MAX_RUNTIME_S
    repos = spec.get("repos")
    if not isinstance(repos, dict) or not repos:
        raise ValueError("filler.repos must name at least one repo")
    targets = []
    for repo in sorted(repos):
        folders = repos[repo]
        where = f"filler.repos.{repo}"
        if not isinstance(folders, dict):
            raise ValueError(f"{where} must be an object with workspace and roots or modules")
        workspace = _folder(folders, "workspace", where)
        if "modules" in folders:
            modules = folders["modules"]
            if not isinstance(modules, list) or not all(isinstance(m, str) and m.strip() for m in modules):
                raise ValueError(f"{where}.modules must be a list of folder paths")
        elif "roots" in folders and isinstance(folders["roots"], list):
            modules = _root_modules(workspace, folders["roots"])
        else:
            raise ValueError(f"{where} needs modules or roots")
        for module in sorted({m.strip().strip("/") for m in modules}):
            targets.append(Target(repo=repo, module=module, workspace=workspace, report_dir=report_dir))
    targets.sort(key=lambda t: t.key)
    return targets, max_runtime


def module_head(target: Target, run=subprocess.run) -> str | None:
    """The last commit that touched the module, or None when git cannot say (not a repo, untracked, error)."""
    try:
        out = run(
            ["git", "-C", target.workspace, "log", "-1", "--format=%H", "--", target.module],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    commit = (out or "").strip()
    return commit or None


def state_get(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM filler_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def state_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO filler_state (key, value) VALUES (?, ?)", (key, value))


def paused(conn) -> bool:
    """True while ``agentq backlog pause`` holds the filler."""
    return state_get(conn, "paused") == "1"


def set_paused(conn, flag: bool) -> None:
    state_set(conn, "paused", "1" if flag else "0")


def _open_job(conn, target_key: str) -> bool:
    row = conn.execute("SELECT last_job FROM filler_targets WHERE target = ?", (target_key,)).fetchone()
    if row is None or row[0] is None:
        return False
    job = db.get_job(conn, row[0])
    return job is not None and job["state"] in db.OPEN_STATES


def _audited_commit(conn, target_key: str) -> str | None:
    row = conn.execute("SELECT last_commit FROM filler_targets WHERE target = ?", (target_key,)).fetchone()
    return row[0] if row else None


def next_target(conn, targets: list[Target], head_of=module_head) -> tuple[Target, str] | None:
    """The next target after the cursor that has a known head commit, differs from its last audited commit
    and has no open job. Returns (target, head commit), or None when nothing is due."""
    if not targets:
        return None
    cursor = state_get(conn, "cursor")
    keys = [t.key for t in targets]
    start = keys.index(cursor) + 1 if cursor in keys else 0
    for target in targets[start:] + targets[:start]:
        if _open_job(conn, target.key):
            continue
        head = head_of(target)
        if head is None or _audited_commit(conn, target.key) == head:
            continue
        return target, head
    return None


def record_submission(conn, target: Target, commit: str, job_id: str, now: float) -> None:
    """Remember the commit a filler job audits and move the cursor to it. Call inside ``db.tx``."""
    conn.execute(
        """INSERT INTO filler_targets (target, last_commit, last_submitted, last_job) VALUES (?, ?, ?, ?)
           ON CONFLICT(target) DO UPDATE SET last_commit = excluded.last_commit,
             last_submitted = excluded.last_submitted, last_job = excluded.last_job""",
        (target.key, commit, now, job_id),
    )
    state_set(conn, "cursor", target.key)


def prompt_body(target: Target, commit: str, template: Path = PROMPT) -> str:
    """The audit brief for one target, with its placeholders filled in."""
    values = {
        "repo": target.repo,
        "module": target.module,
        "workspace": target.workspace,
        "report_path": target.report_path,
        "commit": commit,
    }
    text = template.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text
