"""Filler work supply: read-only audits and merged-PR reviews that the fill controller (``agentq.fill``) submits.

The ``"filler"`` key of ``$AGENTQ_HOME/backlog-seed.json`` names the repos and the work:

    "filler": {
        "report_dir": "~/src/state/reports",
        "max_runtime": "15m",
        "max_target_lines": 4000,
        "min_target_lines": 300,
        "kinds": ["bugs", "tests", "docs", "risk"],
        "pr_window_h": 48,
        "repos": {
            "repo-a": {"workspace": "~/src/repo-a", "roots": ["src/api", "packages"], "github": "owner/repo-a"},
            "repo-b": {"workspace": "~/src/repo-b", "modules": ["tools/agent-dash/agentq"]}
        }
    }

``roots`` expands to the module folders directly under each root; ``modules`` names them outright (paths
relative to the workspace). A module whose code (.py, .sql, .sh) exceeds ``max_target_lines`` is split into its
child packages; a package that still exceeds it into its files of at least ``min_target_lines``. Files directly in
an oversized module count as well. ``github`` names the repo for merged-PR reviews; without it the repo has none.

Audit kinds: ``bugs`` (correctness defects, dead code), ``tests`` (risky paths without tests), ``docs`` (text that
no longer matches the code), ``risk`` (security issues, expensive patterns). A target and kind pair is due when it
has never run or the target changed since that kind last ran. Reports go to
``<report_dir>/filler/<repo>/<kind>/<slug>.md``. A merged PR with no review yet gets one ``pr`` job, whose report
goes to ``<report_dir>/filler/<repo>/pr/<number>.md``.

Every finding is a bullet ``- **high** `path:line` — title``; ``append_index`` collects the top ones into
``<report_dir>/filler/INDEX.md``, newest first, deduplicated by location and title.

The module owns the ``filler_audits``, ``filler_prs``, ``filler_targets`` and ``filler_state`` tables. The broker
writes jobs through ``db.insert_job`` as usual.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agentq import backlog as backlog_lib
from agentq import db
from agentq import pr_source

PROMPT_DIR = Path(__file__).resolve().parent / "backlog_prompts"
PROMPT = PROMPT_DIR / "filler.md"
PROMPT_FILES = {
    "bugs": PROMPT_DIR / "filler.md",
    "tests": PROMPT_DIR / "filler-tests.md",
    "docs": PROMPT_DIR / "filler-docs.md",
    "risk": PROMPT_DIR / "filler-risk.md",
    "pr": PROMPT_DIR / "filler-pr.md",
}
DEFAULT_KINDS = ("bugs", "tests", "docs", "risk")
AUDIT_KINDS = frozenset(DEFAULT_KINDS)
CONFIG_NAME = "backlog-seed.json"
LABEL_MARKER = "-backlog-filler-"
LABEL_SLUG = "backlog-filler"
DEFAULT_MAX_RUNTIME_S = 900.0
DEFAULT_MAX_TARGET_LINES = 4000
DEFAULT_MIN_TARGET_LINES = 300
DEFAULT_PR_WINDOW_H = 48.0
PR_REFRESH_S = 1800.0
CODE_SUFFIXES = (".py", ".sql", ".sh")
SLUG_BASE_MAX = 48
SLUG_HASH_LEN = 8
GIT_TIMEOUT_S = 5.0
SKIP_DIRS = frozenset({"__pycache__", "node_modules", "build", "dist"})
GENERATED_DIRS = frozenset({
    "target", "dbt_packages", "vendor", "venv", "site-packages", "htmlcov", "coverage", "out",
})
SCAN_MAX_FILES = 20_000
SCAN_MAX_BYTES = 200 * 1024 * 1024
SCAN_MAX_SECONDS = 5.0
INDEX_MAX_ENTRIES = 2000
INDEX_LOCK = "INDEX.lock"
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
FINDING_LINE = re.compile(r"^- \*\*(high|medium|low)\*\* `([^`]+)` [—–-] (.+?)\s*$")
INDEX_HEADER = (
    "# Filler findings\n\n"
    "Newest first. Each bullet is one finding: severity, file:line, title. An entry is skipped when its "
    "file:line and title already appear here.\n\n"
)


@dataclass(frozen=True)
class Target:
    """One module or file to audit. ``module`` is relative to the repo's workspace."""

    repo: str
    module: str
    workspace: str
    report_dir: str

    @property
    def key(self) -> str:
        return f"{self.repo}/{self.module}"

    def report_path(self, kind: str = "bugs") -> str:
        return str(Path(self.report_dir) / "filler" / self.repo / kind / f"{module_slug(self.module)}.md")


@dataclass(frozen=True)
class PrRepo:
    repo: str
    slug: str
    workspace: str


@dataclass(frozen=True)
class Settings:
    """The filler's work supply, read from the seed config."""

    targets: list
    max_runtime: float
    kinds: tuple
    report_dir: str
    max_target_lines: int
    min_target_lines: int
    pr_repos: tuple = field(default_factory=tuple)
    pr_window_h: float = DEFAULT_PR_WINDOW_H


@dataclass(frozen=True)
class Work:
    """One unit of filler work: an audit of a target and kind, or the review of one merged PR."""

    kind: str
    repo: str
    workspace: str
    module: str
    commit: str
    report_path: str
    slug: str
    key: str
    pr_number: int | None = None
    pr_title: str = ""
    pr_url: str = ""
    pr_slug: str = ""


def module_slug(text: str) -> str:
    """A readable slug plus an 8-hex hash of the full path, so two paths that share a prefix never share a slug.

    The readable part is lowercase letters, digits and dashes, at most SLUG_BASE_MAX characters. The hash is the
    first eight hex digits of sha1 over the full text.
    """
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:SLUG_BASE_MAX].strip("-") or "root"
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:SLUG_HASH_LEN]
    return f"{base}-{digest}"


def label_slug(target: Target, kind: str = "bugs") -> str:
    """The slug passed to ``label make`` for an audit, so it reads ``hub-exec-backlog-filler-<kind>-<repo>-<module>``."""
    return f"{LABEL_SLUG}-{kind}-{module_slug(target.repo)}-{module_slug(target.module)}"


def is_filler_label(label: str) -> bool:
    return LABEL_MARKER in (label or "")


def _folder(spec: dict, name: str, where: str) -> str:
    value = spec.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} needs a {name} folder")
    return str(Path(value.strip()).expanduser().resolve())


def _positive_int(spec: dict, name: str, default: int) -> int:
    value = spec.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"filler.{name} must be a positive whole number")
    return value


def _is_visible_dir(path: Path) -> bool:
    return path.is_dir() and not path.name.startswith(".") and path.name not in SKIP_DIRS


def _root_modules(workspace: str, roots: list) -> list[str]:
    modules = []
    for root in roots:
        if not isinstance(root, str) or not root.strip():
            raise ValueError(f"filler root must be a folder path: {root!r}")
        base = Path(workspace, root.strip())
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if _is_visible_dir(child):
                modules.append(f"{root.strip().rstrip('/')}/{child.name}")
    return modules


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class ScanOverrun(Exception):
    """A module scan went over its file, byte or time budget. The module is then kept whole."""


class ScanBudget:
    """Limits one module scan: files counted, bytes read, and seconds spent. Charge each file before reading it."""

    def __init__(self, max_files: int = SCAN_MAX_FILES, max_bytes: int = SCAN_MAX_BYTES,
                 max_seconds: float = SCAN_MAX_SECONDS, clock=time.monotonic):
        self.max_files, self.max_bytes, self.max_seconds, self.clock = max_files, max_bytes, max_seconds, clock
        self.files = 0
        self.bytes = 0
        self.started = clock()

    def charge(self, size: int) -> None:
        self.files += 1
        self.bytes += size
        if self.files > self.max_files or self.bytes > self.max_bytes:
            raise ScanOverrun(f"over budget: {self.files} files, {self.bytes} bytes")
        if self.clock() - self.started > self.max_seconds:
            raise ScanOverrun(f"over {self.max_seconds:g} s")


def _git_ignored(workspace: Path, rel_paths: list[str], run=subprocess.run) -> set[str]:
    """The paths git ignores, from one ``git check-ignore --stdin`` call. Empty when the folder is not a repo or git fails."""
    if not rel_paths:
        return set()
    try:
        out = run(["git", "-C", str(workspace), "check-ignore", "--stdin"], input="\n".join(rel_paths) + "\n",
                  capture_output=True, text=True, timeout=GIT_TIMEOUT_S * 2, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.strip() for line in (out or "").splitlines() if line.strip()}


def _module_code_files(workspace: Path, module: str, budget: ScanBudget, run=subprocess.run) -> list[tuple[str, int]]:
    """(workspace-relative path, line count) for every code file under ``module``.

    Only regular files are read. FIFOs, devices and sockets are skipped, and so are symlinks whose resolved path
    leaves the workspace. Generated and vendored folders are pruned, and git-ignored files are dropped with one
    ``git check-ignore`` call. Each file is charged to ``budget`` before it is read, so a large or slow tree raises
    ScanOverrun instead of blocking.
    """
    root = Path(workspace, module)
    if root.is_symlink() and not _is_within(root.resolve(), workspace.resolve()):
        return []
    rel_files: list[str] = []
    if root.is_file() or root.is_symlink():
        candidates = [root] if root.suffix in CODE_SUFFIXES else []
    else:
        candidates = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames
                if not d.startswith(".") and d not in SKIP_DIRS and d not in GENERATED_DIRS
                and not os.path.islink(os.path.join(dirpath, d))
            )
            for name in sorted(filenames):
                if name.endswith(CODE_SUFFIXES):
                    candidates.append(Path(dirpath, name))
    for path in candidates:
        if path.is_symlink() and not _is_within(path.resolve(), workspace.resolve()):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        rel_files.append(str(path.relative_to(workspace)))
    ignored = _git_ignored(workspace, rel_files, run=run)
    out = []
    for rel in rel_files:
        if rel in ignored:
            continue
        path = Path(workspace, rel)
        budget.charge(os.stat(path).st_size)
        out.append((rel, _count_lines(path)))
    return out


def _count_lines(path: Path) -> int:
    """Lines in a regular file. Opened without blocking, and skipped unless it is still a regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return 0
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return 0
        count, pending = 0, b""
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            count += chunk.count(b"\n")
            pending = chunk[-1:]
        return count + (1 if pending and pending != b"\n" else 0)
    finally:
        os.close(fd)


def split_module(workspace: str, module: str, max_lines: int, min_lines: int,
                 budget: ScanBudget | None = None) -> list[str]:
    """The audit targets for one module, relative to the workspace.

    A module at or under ``max_lines`` of code is one target. A larger one is split: each child package under
    the limit is a target, and each larger child contributes its files of at least ``min_lines``. Files directly
    in the module do the same. A module with no code is kept whole, so a docs-only folder still gets its docs
    audit. A scan over budget keeps the module whole.
    """
    ws = Path(workspace)
    root = Path(ws, module)
    if root.is_symlink() and not _is_within(root.resolve(), ws.resolve()):
        return []
    if not root.exists():
        return []
    try:
        files = _module_code_files(ws, module, budget or ScanBudget())
    except ScanOverrun:
        return [module]
    if not files:
        return [module]
    total = sum(lines for _, lines in files)
    if total <= max_lines or total == 0:
        return [module]
    groups: dict[str, list[tuple[str, int]]] = {}
    for rel, lines in files:
        parts = Path(rel).relative_to(module).parts if rel != module else ()
        key = parts[0] if len(parts) > 1 else ""
        groups.setdefault(key, []).append((rel, lines))
    pieces: set[str] = set()
    for key, members in groups.items():
        if key == "":
            pieces.update(rel for rel, lines in members if lines >= min_lines)
            continue
        size = sum(lines for _, lines in members)
        if size == 0:
            continue
        if size <= max_lines:
            pieces.add(f"{module}/{key}")
        else:
            pieces.update(rel for rel, lines in members if lines >= min_lines)
    return sorted(pieces)


def _kinds(spec: dict) -> tuple:
    kinds = spec.get("kinds", list(DEFAULT_KINDS))
    if not isinstance(kinds, list) or not kinds or not all(isinstance(k, str) for k in kinds):
        raise ValueError("filler.kinds must be a non-empty list of audit kinds")
    unknown = [k for k in kinds if k not in AUDIT_KINDS]
    if unknown:
        raise ValueError(f"filler.kinds has unknown kinds: {', '.join(unknown)}")
    return tuple(dict.fromkeys(kinds))


def load_settings(config: dict) -> Settings:
    """The filler's settings and targets from the seed config. No ``filler`` key means no work.

    Raises ValueError for a malformed section.
    """
    spec = config.get("filler") if isinstance(config, dict) else None
    if not isinstance(spec, dict):
        return Settings([], DEFAULT_MAX_RUNTIME_S, DEFAULT_KINDS, "", DEFAULT_MAX_TARGET_LINES,
                        DEFAULT_MIN_TARGET_LINES)
    report_dir = _folder(spec, "report_dir", "filler")
    max_runtime = backlog_lib.parse_duration(spec["max_runtime"]) if "max_runtime" in spec else DEFAULT_MAX_RUNTIME_S
    max_lines = _positive_int(spec, "max_target_lines", DEFAULT_MAX_TARGET_LINES)
    min_lines = _positive_int(spec, "min_target_lines", DEFAULT_MIN_TARGET_LINES)
    window = spec.get("pr_window_h", DEFAULT_PR_WINDOW_H)
    if not isinstance(window, (int, float)) or isinstance(window, bool) or window <= 0:
        raise ValueError("filler.pr_window_h must be a positive number of hours")
    repos = spec.get("repos")
    if not isinstance(repos, dict) or not repos:
        raise ValueError("filler.repos must name at least one repo")
    targets, pr_repos = [], []
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
        pieces = set()
        for module in {m.strip().strip("/") for m in modules}:
            pieces.update(split_module(workspace, module, max_lines, min_lines))
        for piece in sorted(pieces):
            targets.append(Target(repo=repo, module=piece, workspace=workspace, report_dir=report_dir))
        slug = folders.get("github")
        if isinstance(slug, str) and slug.strip():
            pr_repos.append(PrRepo(repo=repo, slug=slug.strip(), workspace=workspace))
    targets.sort(key=lambda t: t.key)
    return Settings(
        targets=targets, max_runtime=max_runtime, kinds=_kinds(spec), report_dir=report_dir,
        max_target_lines=max_lines, min_target_lines=min_lines, pr_repos=tuple(pr_repos),
        pr_window_h=float(window),
    )


def load_targets(config: dict) -> tuple[list[Target], float]:
    """(targets, max runtime in seconds) from the seed config. Kept for callers that need only the targets."""
    settings = load_settings(config)
    return settings.targets, settings.max_runtime


def module_head(target: Target, run=subprocess.run) -> str | None:
    """The last commit that touched the target's module or file, or None when git cannot say."""
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


def _open(conn, job_id: str | None) -> bool:
    if not job_id:
        return False
    job = db.get_job(conn, job_id)
    return job is not None and job["state"] in db.OPEN_STATES


def _audit_row(conn, target_key: str, kind: str) -> dict | None:
    row = conn.execute(
        "SELECT last_commit, last_submitted, last_job FROM filler_audits WHERE target = ? AND kind = ?",
        (target_key, kind),
    ).fetchone()
    return dict(row) if row else None


def due_kinds(conn, target: Target, kinds, head: str) -> list[str]:
    """The kinds due for one target at ``head``: never run, or the target changed since that kind last ran. Kinds
    that ran longest ago come first (never-run kinds in configured order), and a kind with an open job is left out."""
    due = []
    for position, kind in enumerate(kinds):
        row = _audit_row(conn, target.key, kind)
        if row is None:
            due.append((-1.0, position, kind))
            continue
        if _open(conn, row["last_job"]) or row["last_commit"] == head:
            continue
        due.append((row["last_submitted"] or 0.0, position, kind))
    return [kind for _, _, kind in sorted(due)]


def _pr_reviewed(conn, repo: str, number: int) -> bool:
    return conn.execute("SELECT 1 FROM filler_prs WHERE repo = ? AND number = ?", (repo, number)).fetchone() is not None


def pr_work(candidate: dict) -> Work:
    """The review job for one merged PR candidate (see ``Broker._pr_candidates``)."""
    number = int(candidate["number"])
    repo = candidate["repo"]
    return Work(
        kind="pr", repo=repo, workspace=candidate["workspace"], module="", commit=candidate.get("merge_commit", ""),
        report_path=str(Path(candidate["report_dir"]) / "filler" / repo / "pr" / f"{number}.md"),
        slug=f"pr-{module_slug(repo)}-{number}", key=f"pr:{repo}#{number}", pr_number=number,
        pr_title=candidate.get("title", ""), pr_url=candidate.get("url", ""), pr_slug=candidate.get("slug", ""),
    )


def audit_work(target: Target, kind: str, head: str) -> Work:
    return Work(
        kind=kind, repo=target.repo, workspace=target.workspace, module=target.module, commit=head,
        report_path=target.report_path(kind), slug=label_slug(target, kind), key=f"{target.key}#{kind}",
    )


def next_work(conn, targets: list[Target], kinds=DEFAULT_KINDS, head_of=module_head, prs=()) -> Work | None:
    """The next job to submit: the oldest unreviewed merged PR first, else the next due target and kind.

    Targets rotate from the saved cursor. A target with no known head commit is skipped. Nothing is written here.
    """
    for candidate in prs:
        if not _pr_reviewed(conn, candidate["repo"], int(candidate["number"])):
            return pr_work(candidate)
    if not targets:
        return None
    cursor = state_get(conn, "cursor")
    keys = [t.key for t in targets]
    start = keys.index(cursor) + 1 if cursor in keys else 0
    for target in targets[start:] + targets[:start]:
        head = head_of(target)
        if head is None:
            continue
        due = due_kinds(conn, target, kinds, head)
        if due:
            return audit_work(target, due[0], head)
    return None


def record_work(conn, work: Work, job_id: str, now: float, target_key: str | None = None) -> None:
    """Remember a submitted job: an audit's commit and kind (and the cursor), or a reviewed PR. Call inside ``db.tx``."""
    if work.kind == "pr":
        conn.execute(
            """INSERT INTO filler_prs (repo, number, title, reviewed_at, last_job) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(repo, number) DO UPDATE SET reviewed_at = excluded.reviewed_at,
               last_job = excluded.last_job""",
            (work.repo, work.pr_number, work.pr_title, now, job_id),
        )
        return
    conn.execute(
        """INSERT INTO filler_audits (target, kind, last_commit, last_submitted, last_job) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(target, kind) DO UPDATE SET last_commit = excluded.last_commit,
           last_submitted = excluded.last_submitted, last_job = excluded.last_job""",
        (target_key or work.key.split("#")[0], work.kind, work.commit, now, job_id),
    )
    state_set(conn, "cursor", target_key or work.key.split("#")[0])


def record_submission(conn, target: Target, commit: str, job_id: str, now: float, kind: str = "bugs") -> None:
    """Remember the commit one audit kind of a target audits, and move the cursor to it. Call inside ``db.tx``."""
    record_work(conn, audit_work(target, kind, commit), job_id, now, target_key=target.key)


def next_target(conn, targets: list[Target], head_of=module_head, kinds=("bugs",)) -> tuple[Target, str] | None:
    """Compatibility view of ``next_work`` for audits only: the next due target and its head commit."""
    work = next_work(conn, targets, kinds=kinds, head_of=head_of)
    if work is None or work.kind == "pr":
        return None
    target = next(t for t in targets if t.key == work.key.split("#")[0])
    return target, work.commit


def prompt_for(work: Work, template: Path | None = None) -> str:
    """The brief for one job, with its placeholders filled in."""
    values = {
        "repo": work.repo,
        "module": work.module,
        "workspace": work.workspace,
        "report_path": work.report_path,
        "commit": work.commit,
        "pr_number": str(work.pr_number or ""),
        "pr_title": work.pr_title,
        "pr_url": work.pr_url,
        "pr_slug": work.pr_slug,
    }
    text = (template or PROMPT_FILES[work.kind]).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def prompt_body(target: Target, commit: str, template: Path = PROMPT) -> str:
    """The bugs audit brief for one target (kept for callers that build the bugs prompt directly)."""
    return prompt_for(audit_work(target, "bugs", commit), template)


def parse_findings(text: str) -> list[tuple[str, str, str]]:
    """(severity, file:line, title) for every finding bullet in a report, in order."""
    found = []
    for line in (text or "").splitlines():
        match = FINDING_LINE.match(line.strip())
        if match:
            found.append((match.group(1), match.group(2), match.group(3)))
    return found


def top_findings(text: str, count: int = 5) -> list[tuple[str, str, str]]:
    """The ``count`` most severe findings of a report; ties keep the report's order."""
    ordered = sorted(parse_findings(text), key=lambda f: SEVERITY_ORDER[f[0]])
    return ordered[:count]


def index_path(report_dir: str) -> Path:
    return Path(report_dir) / "filler" / "INDEX.md"


def _index_blocks(text: str) -> tuple[str, list[str]]:
    """Split the index into its header and its job blocks (each starts at a line that begins ``## ``)."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith("## ")]
    if not starts:
        return text, []
    header = "".join(lines[: starts[0]])
    bounds = starts + [len(lines)]
    return header, ["".join(lines[bounds[k]: bounds[k + 1]]) for k in range(len(starts))]


def _block_bullets(block: str) -> list[tuple[str, str, str]]:
    return parse_findings(block)


def _block_month(block: str) -> str:
    """YYYY-MM of a job block's heading (its stamp), or the current month when the heading has none."""
    match = re.match(r"## (\d{4})-(\d{2})-\d{2}", block)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return time.strftime("%Y-%m", time.gmtime())


def append_index(report_dir: str, heading: str, findings, now: float,
                 max_entries: int = INDEX_MAX_ENTRIES) -> int:
    """Add the findings not already in the index as a new block at the top. Returns how many were added.

    Safe for concurrent appenders: the index is read, changed and rewritten under an exclusive lock on
    ``filler/INDEX.lock``, and the rewrite goes to a temporary file that replaces the index in one step.
    The index keeps the newest ``max_entries`` findings. Older job blocks move to ``INDEX-<YYYY-MM>.md`` archives,
    which are appended to and never reread. Deduplication is against the retained window.
    """
    base = Path(report_dir) / "filler"
    base.mkdir(parents=True, exist_ok=True)
    with open(base / INDEX_LOCK, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _append_locked(base, heading, findings, now, max_entries)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _append_locked(base: Path, heading: str, findings, now: float, max_entries: int) -> int:
    path = base / "INDEX.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    header, blocks = _index_blocks(existing)
    seen = {(loc, title) for block in blocks for _, loc, title in _block_bullets(block)}
    fresh: list[tuple[str, str, str]] = []
    for severity, loc, title in findings:
        if (loc, title) in seen:
            continue
        seen.add((loc, title))
        fresh.append((severity, loc, title))
    if not fresh:
        return 0
    stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    new_block = f"## {stamp} — {heading}\n" + "".join(f"- **{s}** `{loc}` — {t}\n" for s, loc, t in fresh) + "\n"
    blocks.insert(0, new_block)
    counts = [len(_block_bullets(block)) for block in blocks]
    retained, total = len(blocks), sum(counts)
    while retained > 1 and total > max_entries:
        retained -= 1
        total -= counts[retained]
    moved = blocks[retained:]
    kept = blocks[:retained]
    if not header.strip():
        header = INDEX_HEADER
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(header + "".join(kept), encoding="utf-8")
    os.replace(tmp, path)
    by_month: dict[str, list[str]] = {}
    for block in moved:
        by_month.setdefault(_block_month(block), []).append(block)
    for month, group in by_month.items():
        with open(base / f"INDEX-{month}.md", "a", encoding="utf-8") as archive:
            archive.write("".join(group))
    return len(fresh)


class WorkCache:
    """The filler's settings and merged PRs, read off the caller's thread.

    ``snapshot()`` never blocks. It returns the last complete read (empty before the first) and starts a background
    refresh when that read is older than ``refresh_s`` or the seed file changed. The scan of the repos and the GitHub
    calls run in that thread, so a slow tree or a slow ``gh`` cannot hold the broker tick. ``synchronous=True`` runs
    the refresh inline (tests).
    """

    def __init__(self, home, *, now=time.time, synchronous: bool = False, refresh_s: float = PR_REFRESH_S,
                 log=lambda message: None, run=subprocess.run):
        self.path = Path(home) / CONFIG_NAME
        self.now = now
        self.synchronous = synchronous
        self.refresh_s = refresh_s
        self.log = log
        self.run = run
        self._lock = threading.Lock()
        self._settings: Settings | None = None
        self._prs: list[dict] = []
        self._built_at: float | None = None
        self._built_mtime: float | None = None
        self._thread: threading.Thread | None = None

    def _mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def snapshot(self) -> tuple[Settings, list[dict]]:
        now = self.now()
        mtime = self._mtime()
        with self._lock:
            stale = (self._built_at is None or now - self._built_at >= self.refresh_s
                     or mtime != self._built_mtime)
            busy = self._thread is not None and self._thread.is_alive()
        if stale and not busy:
            if self.synchronous:
                self._refresh(now)
            else:
                thread = threading.Thread(target=self._refresh, args=(now,), name="filler-work", daemon=True)
                with self._lock:
                    self._thread = thread
                thread.start()
        with self._lock:
            return (self._settings or empty_settings(), list(self._prs))

    def _refresh(self, now: float) -> None:
        mtime = self._mtime()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if mtime is not None else {}
            settings = load_settings(data)
        except (OSError, ValueError) as exc:
            self.log(f"filler config refused: {exc}")
            settings = empty_settings()
        prs: list[dict] = []
        window_start = now - settings.pr_window_h * 3600
        for repo in settings.pr_repos:
            try:
                rows = pr_source.merged_prs(repo.slug, pr_source.since_date(now, settings.pr_window_h), run=self.run)
            except pr_source.PRSourceError as exc:
                self.log(f"filler PR source {repo.repo}: {exc}")
                continue
            for row in rows:
                if pr_source.merged_epoch(row) >= window_start:
                    prs.append({**row, "repo": repo.repo, "slug": repo.slug, "workspace": repo.workspace,
                                "report_dir": settings.report_dir})
        prs.sort(key=pr_source.merged_epoch, reverse=True)
        with self._lock:
            self._settings, self._prs = settings, prs
            self._built_at, self._built_mtime = now, mtime


def empty_settings() -> Settings:
    return Settings([], DEFAULT_MAX_RUNTIME_S, DEFAULT_KINDS, "", DEFAULT_MAX_TARGET_LINES, DEFAULT_MIN_TARGET_LINES)


def main(argv=None, out=None, run_pr=pr_source.merged_prs, now=None, head_of=None) -> int:
    """Print the filler's due work from an agentq home: target and kind pairs and merged PRs due now.

    Read-only apart from the migration of the store. Run it on a copy of the store, not the live one.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="agentq.filler", description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--home", required=True, help="agentq home holding agentq.db and backlog-seed.json")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    home = Path(args.home).expanduser()
    try:
        seed = json.loads((home / CONFIG_NAME).read_text(encoding="utf-8"))
        settings = load_settings(seed if isinstance(seed, dict) else {})
    except (OSError, ValueError) as exc:
        print(f"ANSWER: no usable filler config at {home / CONFIG_NAME}: {exc}", file=out)
        return 1
    conn = db.connect(home)
    try:
        now = time.time() if now is None else now
        pairs = []
        head_lookup = head_of or module_head
        for target in settings.targets:
            head = head_lookup(target)
            if head is not None:
                pairs.extend((target.key, kind) for kind in due_kinds(conn, target, settings.kinds, head))
        prs_due, pr_errors = [], []
        window_start = now - settings.pr_window_h * 3600
        for repo in settings.pr_repos:
            try:
                rows = run_pr(repo.slug, pr_source.since_date(now, settings.pr_window_h))
            except pr_source.PRSourceError as exc:
                pr_errors.append(f"{repo.repo}: {exc}")
                continue
            for row in rows:
                if pr_source.merged_epoch(row) < window_start:
                    continue
                if not _pr_reviewed(conn, repo.repo, int(row["number"])):
                    prs_due.append(f"{repo.repo}#{row['number']}")
    finally:
        conn.close()
    print(f"targets: {len(settings.targets)} (split at {settings.max_target_lines} code lines, "
          f"min {settings.min_target_lines})", file=out)
    print(f"kinds: {', '.join(settings.kinds)}", file=out)
    print(f"due target+kind pairs: {len(pairs)}", file=out)
    for kind in settings.kinds:
        print(f"  {kind}: {sum(1 for _, k in pairs if k == kind)}", file=out)
    print(f"merged PRs due for review in the last {settings.pr_window_h:g} h: {len(prs_due)}", file=out)
    for line in prs_due:
        print(f"  {line}", file=out)
    for error in pr_errors:
        print(f"PR source error: {error}", file=out)
    print(f"ANSWER: {len(pairs)} target+kind pair(s) and {len(prs_due)} merged PR(s) due now", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
