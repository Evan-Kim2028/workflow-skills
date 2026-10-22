"""Lead-session workstream split: per-turn signals, assignment, thresholds.

A lead (``claude-main``) session that works on several workstreams is
split across them per assistant turn, so each workstream's exec view
shows the lead with its own share and every token is still counted
exactly once. Only TARGETING fields are ever read, never TEXT bodies:
each assistant record contributes its ``cwd`` plus the path operands
of its tool uses (file paths, prompt-file paths and label operands of
worker launches, ``cd``/``git``/``gh``/``ssh``/``pytest`` targeting
operands, bundle paths); assistant text blocks, ``Agent``/``Task``
prompts and descriptions, ``Write``/``Edit`` content bodies, heredoc
bodies and quoted multi-line literals, ``echo``/``printf`` payloads,
search patterns, ``label make``/``label check`` examples, URLs, and
tool results never vote (see :func:`targeting_strings`). The regexes
below match run/lane labels (``ws<N>-...``, ``hub-...``), plan task
ids (``WS<N>.<id>``), worktree paths (``lor-wt-ws<N>-...``), branch
names (``ws<N>/...``), and the bundle directory
(``workstreams/WS<N>/``). Callers keep only the derived workstream
number per turn (``usage_events.ws_split``), never the matched text.

Assignment per turn:

- one clear signal wins that turn outright;
- several signals go to the dominant one (the most occurrences in the
  turn; ties break by first appearance). An even token split would need
  fractional tokens and could not conserve to the token, so the whole
  turn — the atomic usage row — goes to one workstream;
- a turn with no signal inherits the previous turn's workstream for at
  most ``INHERIT_RUN_MAX`` consecutive turns, else it stays with the
  session's primary attribution. Twenty turns covers one discussion
  stretch between tool batches (an orchestrator narrates, then acts);
  past that the topic has moved on and unattributed text belongs to the
  session's primary workstream, not to whatever was last signalled.

Slices are created only when a second workstream is material: at least
``SLICE_MIN_SHARE`` (5%) of the session's billed tokens or at least
``SLICE_MIN_TOKENS`` (100k) billed tokens. A session whose turns signal
a single workstream keeps its single run row: no extra rows.

Subagents (``parse_subagent_run`` / ``pick_counts``) reuse the same
signals for one whole run, never per turn: a subagent takes its
spawning turn's workstream, else the majority of its own signals, else
the cursor value at the spawn, else nothing. See
``dash/store/split.py`` for the durable link and vote tables.
"""

from __future__ import annotations

import json
import re
from collections import Counter

#: Signal rules version. Folded into the stored per-session marker
#: (``meta`` key ``lead_signal_rules:<session-id>`` in
#: :meth:`dash.store.split.SplitMixin.ensure_split_rules`): a mismatch
#: on the next ingest triggers a clean rebuild of that session's
#: slices and votes from the same bytes (offsets reset, tokens
#: conserved). Bump when the TARGETING/TEXT split below changes.
SIGNAL_RULES_VERSION = 2

#: Consecutive no-signal turns that inherit the previous workstream.
INHERIT_RUN_MAX = 20

#: A second workstream earns a slice at 5% of session billed tokens ...
SLICE_MIN_SHARE = 0.05
#: ... or 100k billed tokens, whichever comes first.
SLICE_MIN_TOKENS = 100_000

#: Billed token kinds: the same set the store totals.
BILLED_KINDS = ("input", "output", "cache_read", "cache_write")

#: Run/lane label naming a workstream, e.g. ``ws11-exec-docs``. The
#: ``wt-`` guard keeps a worktree path (``lor-wt-ws11-x``, which votes
#: through ``WORKTREE_RE``) from voting twice through its fragment.
LANE_RE = re.compile(r"(?<!wt-)\bws(\d+)-[A-Za-z]")
#: Cross-cutting lane label: factual for lane linkage, never a
#: workstream vote (a hub lane belongs to no single workstream).
HUB_RE = re.compile(r"\bhub-[A-Za-z]")
#: Plan task id, e.g. ``WS11.3``.
TASK_RE = re.compile(r"\bWS(\d+)\.[\w-]+")
#: Worktree directory, e.g. ``lor-wt-ws11-evid-docs``.
WORKTREE_RE = re.compile(r"lor-wt-ws(\d+)", re.IGNORECASE)
#: Branch name, e.g. ``ws11/notes`` (``wt-w2`` must not match).
BRANCH_RE = re.compile(r"\bws(\d+)/[\w.\-]+")
#: Bundle directory written under, e.g. ``workstreams/WS11/plan.json``.
BUNDLE_RE = re.compile(r"workstreams/WS(\d+)(?:/|[\"'\s]|$)")
#: A launched worker lane label: ``<scope>-<mark>-<slug>`` per
#: ``dash/labels.py`` (``ws11-exec-docs``, ``hub-exec-x``).
LAUNCH_RE = re.compile(
    r"\b((?:ws\d+|hub)-(?:plan|exec|review|coord)-[a-z0-9][a-z0-9-]*)"
)

#: One document-order pass over every vote kind: the alternative that
#: matches at each position wins, so occurrence order is causal order
#: (ties break by first appearance). Groups line up 1:1 with the
#: single-kind patterns above.
_VOTE_RE = re.compile(
    r"(?<!wt-)\bws(\d+)-[A-Za-z]"
    r"|\bWS(\d+)\.[\w-]+"
    r"|(?i:lor-wt-ws(\d+))"
    r"|\bws(\d+)/[\w.\-]+"
    r"|workstreams/WS(\d+)(?:/|[\"'\s]|$)"
)

#: Workstream number carried in the store (``usage_events.ws_split``).
_WS_VALUE = re.compile(r"^WS\d+$")

#: Run-id prefix for Claude Code Agent-tool subagents
#: (``claude-subagent:<parent session>:<agent id>``). Workflow agents
#: (``claude-workflow:...``) keep the journal-label path and are out of
#: scope for spawn-turn inheritance.
SUBAGENT_PREFIX = "claude-subagent:"


def norm_ws(number) -> str | None:
    """``'11'`` -> ``'WS11'``; anything else -> None (fail-closed)."""
    try:
        value = int(str(number).strip())
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return f"WS{value}"


def clean_ws(value) -> str | None:
    """A stored workstream number, or None (fail-closed for persistence)."""
    if isinstance(value, str) and _WS_VALUE.match(value.strip()):
        return value.strip()
    return None


def signals_in_text(text: str) -> tuple[list[str], list[str]]:
    """``(order, launches)`` for one TARGETING string.

    ``order`` is the occurrence-ordered workstream vote per signal
    across every kind (lane label, task id, worktree path, branch,
    bundle dir); ``hub-...`` labels cast no vote. ``launches`` is the
    occurrence-ordered list of launched lane labels (both ``ws``- and
    ``hub``-scoped): factual launch evidence for lane linkage.
    Pure: no I/O, and the caller keeps only these, never the text.

    The input must already be TARGETING text (see
    :func:`targeting_strings`): discussion prose, prompts, content
    bodies, and stripped Bash noise never reach this matcher.
    """
    if not isinstance(text, str) or not text:
        return [], []
    order: list[str] = []
    for match in _VOTE_RE.finditer(text):
        number = next(
            (group for group in match.groups() if group is not None), None
        )
        ws = norm_ws(number)
        if ws:
            order.append(ws)
    launches: list[str] = []
    for match in LAUNCH_RE.finditer(text):
        label = match.group(1)
        if label not in launches:
            launches.append(label)
    return order, launches


#: Tool-use input fields that name files being read or written: the
#: path operand of a targeting action. ``pattern``/``glob`` count
#: here (a glob selects files); the same key on a search tool is a
#: search pattern and never counts (see ``_SEARCH_TOOLS``).
_PATH_FIELDS = frozenset(
    {
        "file_path",
        "path",
        "notebook_path",
        "pattern",
        "glob",
        "file",
        "filename",
        "dir",
        "directory",
        "workdir",
        "cwd",
    }
)

#: Tools whose ``query``/``pattern``/``prompt`` inputs are search or
#: prose text, never targeting evidence. Their path operands (files
#: being searched) still count; only the pattern keys are TEXT.
_SEARCH_TOOLS = frozenset(
    {
        "Grep",
        "ToolSearch",
        "WebSearch",
        "WebFetch",
        "AskUserQuestion",
        "SendMessage",
        "PushNotification",
        "Skill",
        "TodoWrite",
        "codegraph",
        "mcp__codegraph__codegraph_explore",
    }
)

#: Path-only keys that still count on a search tool (files searched).
_SEARCH_PATH_FIELDS = frozenset(
    {"file_path", "path", "dir", "directory", "file", "filename"}
)

#: Tools whose whole input is prose or a spawned prompt: nothing in it
#: ever votes (no path operands exist).
_PROMPT_TOOLS = frozenset({"Agent", "Task"})

#: A heredoc redirect: ``<<EOF``, ``<<'EOF'``, ``<<-EOF``. The body
#: lines through the terminator are TEXT and never vote.
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?([A-Za-z0-9_]+)['\"]?")
#: A quoted literal spanning lines: TEXT even outside a heredoc.
_QUOTED_MULTILINE_RE = re.compile(
    r"'[^']*\n[^']*'|\"[^\"]*\n[^\"]*\"", re.DOTALL
)
#: A URL: the query string is the observed polluter, and no URL is a
#: file operand, so the whole token is dropped.
_URL_RE = re.compile(r"https?://\S+")
#: Shell separators for segmenting a stripped Bash command.
_SEG_SPLIT_RE = re.compile(r"&&|\|\||[;\n|&]")
#: ``label make`` / ``label check`` example commands: sample labels,
#: never work evidence.
_LABEL_EXAMPLE_RE = re.compile(r"\blabel\s+(make|check)\b")
#: ``python -c <code>``: the code operand is TEXT.
_PYTHON_C_RE = re.compile(
    r"(^|\s)python[\w.]*\s+-c\s+(\"[^\"]*\"|'[^']*'|\S+)"
)
#: ``pytest -k <pattern>``: the pattern is search text, not a path.
_PYTEST_K_RE = re.compile(r"(^|\s)-k\s+(\"[^\"]*\"|'[^']*'|\S+)")
#: Targeting verbs: only segments matching one of these (or naming a
#: bundle path) count. Anything else is discussion-shaped text that
#: happens to sit in a command string, and does not vote.
_CD_RE = re.compile(r"(^|[|&;\s])(cd|pushd)\s")
_GIT_TARGET_RE = re.compile(r"\bgit\b.*(?:\s-C\b|worktree|checkout|switch)")
_LAUNCH_BIN_RE = re.compile(
    r"\b(muse|cursor-agent|launch_grok|run_worker)\b"
)
_AGENT_DASH_TARGET_RE = re.compile(
    r"\bagent_dash\b.*\b(report|ws)\b"
)
_GH_PR_RE = re.compile(r"\bgh\b.*\bpr\b")
_SSH_RE = re.compile(r"(^|[|&;\s])ssh\b")
_PYTEST_RE = re.compile(r"(^|[|&;\s])pytest\b")
_SEARCH_SEG_RE = re.compile(
    r"(^|[|&;\s])(grep|rg|ag|ack)\b|\bgit\s+grep\b"
)
_ECHO_SEG_RE = re.compile(r"^\s*(echo|printf)\b")


def _strip_heredocs(command: str) -> str:
    """``command`` minus heredoc bodies (marker lines kept)."""
    lines = command.split("\n")
    kept: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in lines:
        if pending:
            name, allow_tab = pending[0]
            probe = line.lstrip("\t") if allow_tab else line
            if probe.strip() == name:
                pending.pop(0)
            continue
        for match in _HEREDOC_RE.finditer(line):
            pending.append((match.group(1), "<<-" in line))
        kept.append(line)
    return "\n".join(kept)


def _targeting_bash(command: str) -> str:
    """The TARGETING segments of a Bash command, joined by newlines.

    Heredoc bodies and quoted multi-line literals are stripped, URLs
    dropped, ``python -c`` code and ``pytest -k`` patterns removed,
    then the command is segmented: ``echo``/``printf`` payloads,
    search (``grep``/``rg``/``ag``/``ack``) segments, and ``label
    make``/``label check`` examples are dropped outright. A surviving
    segment counts only when it is an operand of a targeting action
    (``cd``, ``git -C``/``worktree``/``checkout``/``switch``,
    worker-launcher and ``agent_dash report``/``ws`` invocations,
    ``gh pr``, ``ssh``, ``pytest``) or names a bundle path. When
    unsure, the segment does not count.
    """
    if not isinstance(command, str) or not command:
        return ""
    text = _strip_heredocs(command)
    text = _QUOTED_MULTILINE_RE.sub("", text)
    text = _URL_RE.sub("", text)
    kept: list[str] = []
    for segment in _SEG_SPLIT_RE.split(text):
        if not segment.strip():
            continue
        if _LABEL_EXAMPLE_RE.search(segment):
            continue
        if _ECHO_SEG_RE.match(segment):
            continue
        if _SEARCH_SEG_RE.search(segment):
            continue
        segment = _PYTHON_C_RE.sub(r"\1", segment)
        segment = _PYTEST_K_RE.sub(r"\1", segment)
        if (
            _CD_RE.search(segment)
            or _GIT_TARGET_RE.search(segment)
            or _LAUNCH_BIN_RE.search(segment)
            or _AGENT_DASH_TARGET_RE.search(segment)
            or _GH_PR_RE.search(segment)
            or _SSH_RE.search(segment)
            or _PYTEST_RE.search(segment)
            or BUNDLE_RE.search(segment)
        ):
            kept.append(segment)
    return "\n".join(kept)


def _targeting_from_tool(name, tool_input: dict) -> list[str]:
    """TARGETING strings from one tool_use block, in field order."""
    out: list[str] = []
    if not isinstance(tool_input, dict):
        return out
    if name in _PROMPT_TOOLS:
        # Agent/Task: ``prompt`` and ``description`` are TEXT bodies;
        # nothing else in the input targets work.
        return out
    if name == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str) and command:
            stripped = _targeting_bash(command)
            if stripped:
                out.append(stripped)
        for key in ("cwd", "workdir", "dir"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                out.append(_URL_RE.sub("", value))
        return out
    allowed = (
        _SEARCH_PATH_FIELDS if name in _SEARCH_TOOLS else _PATH_FIELDS
    )
    for key, value in tool_input.items():
        if key not in allowed:
            continue
        if isinstance(value, str) and value:
            out.append(_URL_RE.sub("", value))
        elif isinstance(value, list) and name not in _SEARCH_TOOLS:
            for item in value:
                if isinstance(item, str) and item:
                    out.append(_URL_RE.sub("", item))
    return out


def targeting_strings(rec: dict) -> list[str]:
    """TARGETING strings from one assistant record, in document order.

    The record ``cwd`` plus every tool-use path operand (see
    :func:`_targeting_from_tool`). Assistant text blocks, ``Agent``
    prompts, content bodies, search patterns, and descriptions never
    appear here. Pure: ids and paths in, strings out; the caller
    keeps only derived workstream numbers, never these strings.
    """
    out: list[str] = []
    if not isinstance(rec, dict):
        return out
    cwd = rec.get("cwd")
    if isinstance(cwd, str) and cwd:
        out.append(_URL_RE.sub("", cwd))
    message = rec.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            # Text blocks are discussion (TEXT); tool results live on
            # user lines, which never reach this function.
            continue
        out.extend(_targeting_from_tool(block.get("name"), block.get("input")))
    return out


def signals_in_turn(rec: dict) -> tuple[list[str], list[str]]:
    """``(order, launches)`` for one assistant record's TARGETING fields.

    Occurrence order is preserved across the record's TARGETING
    strings (``cwd`` first, then tool uses in content order), so ties
    still break by first appearance. Pure: no I/O.
    """
    order: list[str] = []
    launches: list[str] = []
    for text in targeting_strings(rec):
        sub_order, sub_launches = signals_in_text(text)
        order.extend(sub_order)
        for label in sub_launches:
            if label not in launches:
                launches.append(label)
    return order, launches


def signals_in_line(line: bytes) -> tuple[list[str], list[str]]:
    """``signals_in_turn`` over one raw transcript line (bytes).

    Only assistant records vote: user lines (tool results), registry
    blobs, and undecodable lines read as no signal. Tool-result
    bodies never count (the structured ``agentId:`` spawn linkage is
    extracted separately, ids only). Never raises.
    """
    if not line:
        return [], []
    try:
        rec = json.loads(line)
    except (ValueError, TypeError):
        return [], []
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return [], []
    try:
        return signals_in_turn(rec)
    except Exception:
        return [], []


def pick_turn(order: list[str]) -> str | None:
    """The workstream one turn's occurrence-ordered signals vote for.

    Majority of occurrences wins; ties break by first appearance
    (document order is causal order). Empty input votes for nothing.
    """
    if not order:
        return None
    counts = Counter(order)
    best = max(counts.values())
    for ws in order:
        if counts[ws] == best:
            return ws
    return None


class SplitCursor:
    """Per-session assignment cursor over the ordered turn stream.

    ``observe`` takes one turn's occurrence-ordered workstream signals
    and returns the assigned workstream, or None when the turn stays
    with the session's primary attribution. Deterministic: the same
    signal sequence always yields the same assignments, so a restart
    that replays the same bytes reproduces the same slices.
    """

    def __init__(self, ws: str | None = None, run: int = 0):
        self.ws = clean_ws(ws)
        self.run = max(0, int(run or 0))

    def observe(self, order: list[str]) -> str | None:
        hit = pick_turn(order or [])
        if hit is not None:
            self.ws = hit
            self.run = 0
            return hit
        if self.ws is not None and self.run < INHERIT_RUN_MAX:
            self.run += 1
            return self.ws
        return None


def parse_subagent_run(run_id) -> tuple[str, str] | None:
    """``(parent_session_id, agent_id)`` for a subagent run, else None.

    Only ``claude-subagent:<sid>:<agent>`` run ids qualify (fail-closed:
    anything else, including workflow runs, returns None). Ids only,
    never text.
    """
    if not isinstance(run_id, str):
        return None
    parts = run_id.split(":")
    if len(parts) != 3 or parts[0] != SUBAGENT_PREFIX.rstrip(":"):
        return None
    parent, agent = parts[1].strip(), parts[2].strip()
    if not parent or not agent:
        return None
    return parent, agent


def pick_counts(counts: dict[str, tuple[int, int]]) -> str | None:
    """The majority workstream from ``{ws: (occurrences, first_seen)}``.

    Most occurrences win; ties break by earliest first appearance (the
    same rule as :func:`pick_turn`, but over persisted counters so a
    restart that replays the same bytes reproduces the same pick).
    Empty or all-zero input votes for nothing. Fail-closed on shape.
    """
    best: str | None = None
    best_n = 0
    best_first: int | None = None
    try:
        items = list((counts or {}).items())
    except AttributeError:
        return None
    for ws, value in items:
        if clean_ws(ws) is None:
            continue
        try:
            number, first = value
            number, first = int(number), int(first)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        if (
            best is None
            or number > best_n
            or (number == best_n and best_first is not None and first < best_first)
        ):
            best, best_n, best_first = ws, number, first
    return best


def slice_run_id(session_id: str, ws: str) -> str:
    """``claude-main:<sid>:main@WS11``: stable, unique per session slice."""
    return f"claude-main:{session_id}:main@{ws}"


def slice_label(ws: str, session_id: str) -> str:
    """Label for a slice run: explicitly regex-attributed, labels only.

    ``ws11-lead-<sid8>`` matches the ``^ws(\\d+)`` attribution rule, so
    the slice keeps its workstream with confidence ``regex`` through
    every inherit/refresh pass, and the ``lead`` phase rule (source
    ``claude-main``, no parent) classifies it as planning like the
    primary. The suffix is a fragment of the already-stored session
    id, never transcript text.
    """
    number = re.fullmatch(r"WS(\d+)", ws or "")
    stem = str(session_id or "")[:8]
    if not number or not stem:
        return ""
    return f"ws{number.group(1)}-lead-{stem}"


def material_slices(
    per_ws_billed: dict[str, int],
    primary_ws: str | None,
) -> tuple[str, list[str]]:
    """``(primary, [slice workstreams])`` from billed tokens per ws.

    The primary is the session's attribution, or — when the session
    has none — the top workstream by billed tokens (an unattributed
    lead that worked one workstream keeps one row). A non-primary
    workstream earns a slice at ``SLICE_MIN_SHARE`` of the session
    total or ``SLICE_MIN_TOKENS`` billed tokens. Single-workstream
    sessions yield no slices: no extra rows. Pure and monotonic.
    """
    totals = {
        ws: int(tokens or 0)
        for ws, tokens in (per_ws_billed or {}).items()
        if clean_ws(ws) and int(tokens or 0) > 0
    }
    if not totals:
        return primary_ws, []
    primary = clean_ws(primary_ws)
    if primary is None:
        primary = max(sorted(totals), key=lambda ws: totals[ws])
    if len(totals) <= 1:
        return primary, []
    whole = sum(totals.values())
    out = []
    for ws in sorted(totals):
        if ws == primary:
            continue
        share = totals[ws] / whole if whole else 0.0
        if totals[ws] >= SLICE_MIN_TOKENS or share >= SLICE_MIN_SHARE:
            out.append(ws)
    return primary, out
