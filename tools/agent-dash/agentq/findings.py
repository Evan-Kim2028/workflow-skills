"""A reader for filler findings: the newest high-severity ones, and whether the line they cite is still there.

Filler audits write one report per target and kind to ``<report_dir>/filler/<repo>/<kind>/<slug>.md`` and collect
their top findings in ``<report_dir>/filler/INDEX.md``, newest first, deduplicated by ``file:line`` and title.
Nothing read them: 0 of 700 commits in a week referenced a finding, and the dashboard showed only a "Filler runs"
counter. This module turns the index into a short list for the dashboard card and ``agentq filler findings``, and
checks each cited line against ``origin/main``:

* ``same`` — the line is byte-identical at ``origin/main``, so the finding is still open;
* ``changed`` — the line differs, so the finding is probably fixed (an upper bound: unrelated commits move lines);
* ``gone`` — the file or the line no longer exists, so the finding is unresolved;
* ``unknown`` — the report names no commit, or git could not read the repo.

The line check is cached per commit. A commit's file content never changes, so a cached answer is never stale;
``origin/main`` is resolved to a commit first, so its answers are cached the same way. The cache lives in
``$AGENTQ_HOME/filler-findings-cache.json`` and is bounded.

The index is the source, not the report tree: it is already newest-first and deduplicated, and reading it is a
single small file. A report is opened only to learn the commit it audited (and the module, for a finding that
cites a bare file name), and that read is cached by path, mtime and size.
"""

from __future__ import annotations

import contextlib
import json
import os
import posixpath
import re
import subprocess
import time
from pathlib import Path

from agentq import filler as filler_lib

CONFIG_NAME = "backlog-seed.json"
INDEX_NAME = "INDEX.md"
CACHE_NAME = "filler-findings-cache.json"
CACHE_VERSION = 1
CACHE_MAX_ENTRIES = 5000
#: How long a resolved ``origin/main`` is reused. A stale sha can only mislabel a finding for this long, and the
#: alternative is a ``git rev-parse`` (about 70 ms) per repo on every call.
MAIN_SHA_TTL_S = 300.0
GIT_TIMEOUT_S = 5.0
DEFAULT_LIMIT = 15
SEVERITIES = ("high", "medium", "low")
BLOCK_HEAD = re.compile(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}Z) — (.+?)\s*$")
COMMIT_LINE = re.compile(r"^- commit: ([0-9a-f]{7,40})\b", re.MULTILINE | re.IGNORECASE)
#: A PR review names the merge commit it read instead of a ``- commit:`` line.
MERGE_COMMIT = re.compile(r"merge commit:?\s*`?([0-9a-f]{7,40})\b", re.IGNORECASE)
MODULE_LINE = re.compile(r"^- module: `?([^`\n]+?)`?(?: \([^)]*\))?\s*$", re.MULTILINE | re.IGNORECASE)
CODE_SUFFIXES = (".py", ".sql", ".sh", ".md", ".ts", ".js", ".mjs", ".toml", ".yaml", ".yml")
STATUS_ORDER = {"same": 0, "changed": 1, "gone": 2, "unknown": 3}


def load_config(home) -> dict:
    """The seed config's ``filler`` section, or an empty dict when the file is missing or malformed."""
    try:
        data = json.loads((Path(home) / CONFIG_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    section = data.get("filler") if isinstance(data, dict) else None
    return section if isinstance(section, dict) else {}


def report_root(home) -> Path | None:
    """The folder holding ``filler/INDEX.md``, from ``filler.report_dir`` in the seed config."""
    value = load_config(home).get("report_dir")
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(os.path.expanduser(value.strip()))


def workspaces(home) -> dict[str, str]:
    """Repo name to its workspace folder, from ``filler.repos``."""
    repos = load_config(home).get("repos")
    found = {}
    if isinstance(repos, dict):
        for name, spec in repos.items():
            if isinstance(spec, dict) and isinstance(spec.get("workspace"), str):
                found[str(name)] = os.path.expanduser(spec["workspace"])
    return found


def parse_label(label: str, repo_shorts: dict[str, str]) -> tuple[str, str, str] | None:
    """(repo, kind, tail) from a filler job label, or None when it is not one.

    A label is ``hub-exec-backlog-filler-<kind>-<repo-short>-<readable>-<hash>`` for an audit and
    ``hub-exec-backlog-filler-pr-<repo-short>-<number>`` for a merged-PR review. ``tail`` is the part after the
    repo's short name: the readable slug and hash, or the PR number.
    """
    text = label or ""
    for prefix in ("hub-exec-", "hub-review-"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if not text.startswith(filler_lib.LABEL_SLUG + "-"):
        return None
    rest = text[len(filler_lib.LABEL_SLUG) + 1:]
    kind, _, tail = rest.partition("-")
    if kind not in ("bugs", "tests", "docs", "risk", "pr") or not tail:
        return None
    for repo in sorted(repo_shorts, key=lambda name: -len(repo_shorts[name])):
        short = repo_shorts[repo]
        if tail.startswith(short + "-"):
            return repo, kind, tail[len(short) + 1:]
    return None


def report_path(root: Path, repo: str, kind: str, tail: str, folders: dict | None = None) -> Path | None:
    """The report file a label's block came from, or None when it is not on disk.

    An audit's report is named after the module's slug, which ends in the same 8-hex digest the label ends in, so
    the digest finds the file without knowing the module. A PR review's report is named after the PR number.
    ``folders`` caches one ``os.listdir`` per repo and kind, so a whole index costs one listing per folder; a
    ``Path`` per entry would cost about 0.1 s over the 1,600 reports on disk.
    """
    folder = root / "filler" / repo / kind
    if kind == "pr":
        candidate = folder / f"{tail}.md"
        return candidate if candidate.is_file() else None
    digest = tail.rsplit("-", 1)[-1]
    if not re.fullmatch(r"[0-9a-f]{8}", digest):
        return None
    key = (repo, kind)
    if folders is not None and key not in folders:
        try:
            folders[key] = os.listdir(folder)
        except OSError:
            folders[key] = []
    if folders is None:
        try:
            names = os.listdir(folder)
        except OSError:
            return None
    else:
        names = folders[key]
    exact = f"{digest}.md"
    if exact in names:
        return folder / exact
    return next((folder / name for name in names if name.endswith(f"-{digest}.md")), None)


def read_header(path: Path) -> tuple[str | None, str | None]:
    """(commit, module) from a report's first lines. Either is None when the report does not name it."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            head = "".join(handle.readline() for _ in range(15))
    except OSError:
        return None, None
    commit = COMMIT_LINE.search(head) or MERGE_COMMIT.search(head)
    module = MODULE_LINE.search(head)
    return (commit.group(1) if commit else None, module.group(1).strip() if module else None)


def index_blocks(text: str) -> list[tuple[str, str, list[tuple[str, str, str]]]]:
    """(stamp, label, findings) for every block of an index, newest first (the file's own order)."""
    blocks: list[tuple[str, str, list[tuple[str, str, str]]]] = []
    stamp = label = None
    findings: list[tuple[str, str, str]] = []
    for line in (text or "").splitlines():
        if line.startswith("## "):
            head = BLOCK_HEAD.match(line)
            if head:
                if label is not None:
                    blocks.append((stamp, label, findings))
                stamp, label, findings = head.group(1), head.group(2), []
                continue
        if label is None or "- **" not in line:
            continue
        match = filler_lib.FINDING_LINE.match(line.strip())
        if match:
            findings.append((match.group(1), match.group(2), match.group(3)))
    if label is not None:
        blocks.append((stamp, label, findings))
    return blocks


def split_location(loc: str) -> tuple[str, int | None]:
    """(path, line) from ``path:line`` or ``path:first-last``; the line is None when there is no number."""
    path, _, number = loc.rpartition(":")
    if path:
        first = number.split("-", 1)[0]
        if first.isdigit():
            return path, int(first)
    return loc, None


def _git(workspace: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", workspace, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


class _Batch:
    """One ``git cat-file --batch`` process: many ``<sha>:<path>`` reads without a process per read.

    A cold check reads two commits of a handful of files, and a ``git show`` per read costs about 40 ms of process
    start-up on a large repo. The batch protocol is one request line and one ``<sha> <type> <size>`` header plus
    the content per read, so the whole check is two process starts (one per repo) instead of twenty.
    """

    def __init__(self, workspace: str) -> None:
        self.proc = subprocess.Popen(
            ["git", "-C", workspace, "cat-file", "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

    def get(self, spec: str) -> str | None:
        """The content of ``<sha>:<path>``, or None when the object is missing or the process died."""
        try:
            self.proc.stdin.write(spec.encode("utf-8") + b"\n")
            self.proc.stdin.flush()
            header = self.proc.stdout.readline()
            if not header:
                return None
            parts = header.split()
            if len(parts) < 3 or parts[1] == b"missing":
                return None
            data = self.proc.stdout.read(int(parts[2]))
            self.proc.stdout.read(1)
            return data.decode("utf-8", "replace")
        except (OSError, ValueError):
            return None

    def close(self) -> None:
        for stream in (self.proc.stdin, self.proc.stdout):
            with contextlib.suppress(OSError):
                stream.close()
        with contextlib.suppress(OSError):
            self.proc.wait(timeout=GIT_TIMEOUT_S)
        if self.proc.poll() is None:
            self.proc.kill()


class LineCheck:
    """Whether a finding's cited line is still identical at ``origin/main``, cached per commit.

    A commit's content never changes, so a cached line is never stale. ``origin/main`` is resolved to a commit
    first and cached the same way. The cache is a small JSON file under the agentq home; a missing or malformed
    file is an empty cache, and a failed write is ignored (the next call rebuilds it).
    """

    def __init__(self, home, *, run=_git, now=time.time) -> None:
        self.path = Path(home) / CACHE_NAME
        self.run = run
        self.now = now
        self._lines: dict[str, str | None] = {}
        self._paths: dict[str, str | None] = {}
        self._mains: dict[str, list] = {}
        self._files: dict[str, list[str] | None] = {}
        self._batches: dict[str, _Batch] = {}
        self._main: dict[str, str | None] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return
        lines = data.get("lines")
        paths = data.get("paths")
        mains = data.get("mains")
        if isinstance(lines, dict):
            self._lines = {str(k): (None if v is None else str(v)) for k, v in lines.items()}
        if isinstance(paths, dict):
            self._paths = {str(k): (None if v is None else str(v)) for k, v in paths.items()}
        if isinstance(mains, dict):
            self._mains = {str(k): v for k, v in mains.items()
                           if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)}

    def save(self) -> None:
        """Write the cache when this call added to it. A failed write is ignored."""
        if not self._dirty:
            return
        for store in (self._lines, self._paths):
            while len(store) > CACHE_MAX_ENTRIES:
                store.pop(next(iter(store)))
        body = {"version": CACHE_VERSION, "lines": self._lines, "paths": self._paths, "mains": self._mains}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}")
            tmp.write_text(json.dumps(body), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def main_sha(self, repo: str, workspace: str) -> str | None:
        """``origin/main``'s commit, reused for ``MAIN_SHA_TTL_S`` so a repeat call starts no git process."""
        if repo in self._main:
            return self._main[repo]
        key = f"{repo}|{workspace}"
        cached = self._mains.get(key)
        if cached is not None and 0 <= self.now() - float(cached[1]) < MAIN_SHA_TTL_S:
            self._main[repo] = cached[0]
            return cached[0]
        out = self.run(workspace, "rev-parse", "origin/main")
        self._main[repo] = (out or "").strip() or None
        if self._main[repo] is not None:
            self._mains[key] = [self._main[repo], self.now()]
            self._dirty = True
        return self._main[repo]

    def _read(self, workspace: str, spec: str) -> str | None:
        """One ``<sha>:<path>`` read: through the batch process, or through an injected ``run`` in tests."""
        if self.run is not _git:
            return self.run(workspace, "show", spec)
        batch = self._batches.get(workspace)
        if batch is None:
            try:
                batch = self._batches[workspace] = _Batch(workspace)
            except OSError:
                return None
        return batch.get(spec)

    def close(self) -> None:
        """Stop every batch process this check started."""
        for batch in self._batches.values():
            batch.close()
        self._batches.clear()

    def _file(self, repo: str, workspace: str, sha: str, path: str) -> list[str] | None:
        """A file's lines at one commit, read once per call. Only the cited lines are persisted, not the file."""
        key = f"{repo}|{sha}|{path}"
        if key not in self._files:
            out = self._read(workspace, f"{sha}:{path}")
            self._files[key] = out.split("\n") if out is not None else None
        return self._files[key]

    def _resolve(self, repo: str, workspace: str, sha: str, module: str | None, loc_path: str) -> str | None:
        """The repo-relative path a finding cites: the location itself, else under the report's module."""
        key = f"{repo}|{sha}|{module or ''}|{loc_path}"
        if key in self._paths:
            return self._paths[key]
        candidates = [loc_path]
        if module:
            if module.endswith(CODE_SUFFIXES):
                candidates.append(posixpath.join(posixpath.dirname(module), loc_path))
            else:
                candidates.append(posixpath.join(module, loc_path))
        for candidate in dict.fromkeys(candidates):
            if self._file(repo, workspace, sha, candidate) is not None:
                self._paths[key] = candidate
                self._dirty = True
                return candidate
        self._paths[key] = None
        self._dirty = True
        return None

    def status(self, repo: str, workspace: str, commit: str | None, module: str | None, loc: str) -> str:
        """``same``, ``changed``, ``gone`` or ``unknown`` for one finding (see the module docstring)."""
        if not commit or not workspace:
            return "unknown"
        main = self.main_sha(repo, workspace)
        if main is None:
            return "unknown"
        loc_path, number = split_location(loc)
        if number is None:
            return "unknown"
        path = self._resolve(repo, workspace, commit, module, loc_path)
        if path is None:
            return "gone"
        old = self._line(repo, workspace, commit, path, number)
        new = self._line(repo, workspace, main, path, number)
        if old is None or new is None:
            return "gone"
        return "same" if old.strip() == new.strip() else "changed"

    def _line(self, repo: str, workspace: str, sha: str, path: str, number: int) -> str | None:
        key = f"{repo}|{sha}|{path}|{number}"
        if key in self._lines:
            return self._lines[key]
        lines = self._file(repo, workspace, sha, path)
        value = None if lines is None or number < 1 or number > len(lines) else lines[number - 1]
        self._lines[key] = value
        self._dirty = True
        return value


def _matches_path(loc: str, wanted: list[str]) -> bool:
    """True when a finding's location names one of ``wanted`` (a full path, or a bare file name)."""
    path, _ = split_location(loc)
    for item in wanted:
        item = item.strip().lstrip("./")
        if not item:
            continue
        if path == item or path.endswith("/" + item) or item.endswith("/" + path):
            return True
    return False


def open_findings(home, *, repo: str | None = None, severity: str = "high", paths=None, limit: int = DEFAULT_LIMIT,
                  check: LineCheck | None = None) -> dict:
    """The newest findings of ``severity``, deduplicated by location and title, with their line status.

    Returns ``{"rows", "total", "repos", "error"}``. ``rows`` is at most ``limit`` long, newest first; ``total``
    counts every match before the limit; ``repos`` counts matches per repo. ``paths`` keeps only findings whose
    location names one of the given files. Nothing is written except the line-check cache.
    """
    home = Path(home)
    root = report_root(home)
    if root is None:
        return {"rows": [], "total": 0, "repos": {}, "error": "no filler.report_dir in backlog-seed.json"}
    try:
        text = (root / "filler" / INDEX_NAME).read_text(encoding="utf-8")
    except OSError as exc:
        return {"rows": [], "total": 0, "repos": {}, "error": f"no filler index at {root / 'filler' / INDEX_NAME}: {exc}"}
    spaces = workspaces(home)
    repo_shorts = {name: filler_lib.repo_short(name) for name in spaces}
    checker = check or LineCheck(home)
    wanted = [p for p in (paths or []) if p]
    folders: dict = {}
    seen: set[tuple[str, str]] = set()
    rows: list[dict] = []
    total = 0
    per_repo: dict[str, int] = {}
    try:
        for stamp, label, findings in index_blocks(text):
            parsed = parse_label(label, repo_shorts)
            if parsed is None:
                continue
            found_repo, kind, tail = parsed
            if repo and found_repo != repo:
                continue
            for found_severity, loc, title in findings:
                if found_severity != severity:
                    continue
                if wanted and not _matches_path(loc, wanted):
                    continue
                key = (loc, title)
                if key in seen:
                    continue
                seen.add(key)
                total += 1
                per_repo[found_repo] = per_repo.get(found_repo, 0) + 1
                if len(rows) >= limit:
                    continue
                report = report_path(root, found_repo, kind, tail, folders)
                commit = module = None
                if report is not None:
                    commit, module = read_header(report)
                rows.append({
                    "repo": found_repo,
                    "kind": kind,
                    "severity": found_severity,
                    "loc": loc,
                    "title": title,
                    "stamp": stamp,
                    "report": str(report) if report is not None else None,
                    "commit": commit,
                    "status": checker.status(found_repo, spaces.get(found_repo, ""), commit, module, loc),
                })
    finally:
        checker.save()
        checker.close()
    return {"rows": rows, "total": total, "repos": per_repo, "error": None}


def format_rows(result: dict, limit: int = DEFAULT_LIMIT) -> list[str]:
    """One short line per finding, newest first, at most ``limit`` lines. The report is ``<kind>/<file>``."""
    lines = []
    for row in result["rows"][:limit]:
        report = f"{row['kind']}/{Path(row['report']).name}" if row["report"] else "-"
        lines.append(f"{row['status']:<7} {row['repo']:<16} {row['loc']:<40} {row['title'][:60]:<60} {report}")
    return lines
