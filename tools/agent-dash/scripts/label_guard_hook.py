#!/usr/bin/env python3
"""Claude Code PreToolUse hook: refuse worker launches with weak labels.

Reads the tool-call JSON from stdin. Only Bash commands that launch a
worker are examined; everything else (including a grep or echo that
merely mentions a launcher) passes untouched:

- ``muse exec ... --prompt-file <dir>/<label>.md`` (label = basename)
- ``launch_grok.sh <label> ...``
- ``launch_cmd.py <label> <workspace> <prompt-file> <outdir>``
- ``launch_pi.py <label> <workspace> <prompt-file> <outdir>`` (pi, the
  ``@mariozechner/pi-coding-agent`` CLI)
- ``launch_codex.py <label> <workspace> <prompt-file> <outdir>`` (Codex,
  headless ``codex exec``)
- ``cmd -p "<prompt>"`` (Command Code headless): the prompt must start with
  ``[label: <label>]``; a prompt without that prefix is refused
- ``codex exec [options] "<prompt>"`` (Codex headless): the prompt must
  start with ``[label: <label>]``; a stdin (``-``), subcommand, or
  flag-only launch is not checkable and passes
- ``run_worker.sh|run_worker.py <tier> <label> ...``
- ``agent_dash.py report start --label <label>``
- appends to ``muse-sessions.tsv`` / ``cursor-sessions.tsv`` (first column)
  via ``printf '%s...' <label> ...``, ``echo [-e] '<label>...'``,
  here-strings/heredocs, and ``| tee -a`` (a ``%s``/``%b`` format string
  alone is never the label; unresolvable expansions fail open)

A label that fails :mod:`dash.labels` strict check exits 2 with the
one-line fix on stderr (Claude Code feeds that back to the model), so a
hand-launched worker cannot start unlabeled. A valid label exits 0
silently. Anything uncertain exits 0: bad JSON, an unparseable command,
an unreadable plan, an import error (fail-open, never blocks the
session). Own errors append one line to the state-dir log and exit 0.

Installs via ``sh scripts/install_hooks.sh --claude-label-guard``
into ``~/.claude/settings.json`` (PreToolUse, matcher ``Bash``), via
``--grok-label-guard`` into ``~/.grok/hooks/label-guard.json`` (same
matcher; Grok's ``toolName``/``toolInput`` keys are accepted), and via
``--muse-label-guard`` as a Muse user-scope plugin. Well under a
second: stdlib plus ``dash.labels`` (stdlib-only import) plus at most
one small ``plan.json`` read.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TSV_NAMES = ("muse-sessions.tsv", "cursor-sessions.tsv", "codex-sessions.tsv", "pi-sessions.tsv")
_WRAPPERS = {"nohup", "sudo", "env", "setsid", "nice"}
_LAUNCHERS = {"launch_grok.sh", "launch_cmd.py", "launch_pi.py", "launch_codex.py", "run_worker.sh", "run_worker.py"}
_COMMANDCODE_BINARIES = ("cmd", "commandcode")

#: Command Code headless prompt label prefix, ``[label: <label>]``.
_CMD_LABEL = re.compile(r"^\s*\[label:\s*([^\]]*?)\s*\]")
#: Shell expansion inside a Command Code prompt: the label is unknowable.
_CMD_EXPANSION_RE = re.compile(r"[$`]")
_TSV_WRITERS = ("echo", "printf", "cat", "tee")

#: A shell expansion we cannot resolve: variable, command substitution,
#: glob, or a leftover format spec. Seeing one where the label should be
#: means fail open, never refuse.
_UNCERTAIN_RE = re.compile(r"[$`(){}\[\]*?%]")

#: First printf conversion in a format string (``%%`` excluded): the
#: first data argument fills it. ``s``/``b`` carry the TSV label column;
#: anything else first (``%d``, ...) means the label is not args[0].
_FORMAT_SPEC_RE = re.compile(r"%(?!%)[-+ #0]*\d*(?:\.\d+)?[a-zA-Z]")

#: A shell redirect operator (``>>``, ``>``, ``2>``, ``<<``, ``<<<``):
#: skipped with its file target so neither is mistaken for label text.
_REDIR_RE = re.compile(r"^\d*(>>|>|<<|<<<|<)(&\d*)?$")


def _strip_redirects(argv: list[str]) -> list[str]:
    """Argv without redirect operators and their file targets."""
    out: list[str] = []
    skip = False
    for token in argv:
        if skip:
            skip = False
            continue
        if _REDIR_RE.match(token):
            if "&" not in token:
                skip = True  # a file target follows (`2>&1` has none)
            continue
        out.append(token)
    return out


def _log(text: str) -> None:
    """One best-effort line to the state-dir log. Never raises."""
    try:
        state = os.environ.get("AGENT_DASH_STATE_DIR") or str(
            Path.home() / ".local" / "share" / "agent-dash"
        )
        with open(os.path.join(state, "label-guard.log"), "a", encoding="utf-8") as fh:
            fh.write(text[:500] + "\n")
    except Exception:
        pass


def _split_segments(command: str) -> list[str]:
    """Split a shell command on operators outside quotes."""
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    depth = 0
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
            current.append(char)
        elif quote is not None:
            current.append(char)
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            current.append(char)
        elif char == "$" and index + 1 < len(command) and command[index + 1] == "(":
            depth += 1
            current.extend(("$", "("))
            index += 1
        elif char == "(" and depth:
            depth += 1
            current.append(char)
        elif char == ")" and depth:
            depth -= 1
            current.append(char)
        elif not depth and char in (";", "\n"):
            segments.append("".join(current))
            current = []
        elif not depth and char in ("&", "|"):
            # && and || are one operator; a single & or | ends here.
            while index + 1 < len(command) and command[index + 1] == char:
                current.append(char)
                index += 1
            segments.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    segments.append("".join(current))
    return segments


def _base(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def _nonflags(argv: list[str]) -> list[str]:
    return [token for token in argv if not token.startswith("-")]


def _label_from_argv(argv: list[str]) -> str | None:
    """A worker label invoked by this argv, or None when not a launch."""
    items = list(argv)
    while len(items) > 1 and _base(items[0]) in _WRAPPERS:
        items = items[1:]
    if not items:
        return None
    base = _base(items[0])
    if base == "muse":
        # Real invocation only: argv[0] is muse itself, so a grep or
        # echo that merely mentions 'muse exec' never reaches here.
        if len(items) < 2 or items[1] != "exec":
            return None
        try:
            at = items.index("--prompt-file")
        except ValueError:
            return None
        if at + 1 >= len(items):
            return None
        name = _base(items[at + 1])
        return name[:-3] if name.endswith(".md") else None
    if base in _COMMANDCODE_BINARIES:
        return _label_from_cmd(items[1:])
    if base == "codex":
        return _label_from_codex_exec(items[1:])
    if base in _LAUNCHERS:
        rest = _nonflags(items[1:])
        if base in ("launch_grok.sh", "launch_cmd.py", "launch_pi.py", "launch_codex.py"):
            return rest[0] if rest else None
        return rest[1] if len(rest) >= 2 else None
    if base in ("python", "python3") and len(items) > 1:
        inner = _base(items[1])
        if inner == "run_worker.py":
            rest = _nonflags(items[2:])
            return rest[1] if len(rest) >= 2 else None
        if inner == "launch_cmd.py":
            rest = _nonflags(items[2:])
            return rest[0] if rest else None
        if inner == "launch_pi.py":
            rest = _nonflags(items[2:])
            return rest[0] if rest else None
        if inner == "launch_codex.py":
            rest = _nonflags(items[2:])
            return rest[0] if rest else None
        if inner == "agent_dash.py":
            return _label_from_report(items[2:])
        return None
    if base == "agent_dash.py":
        return _label_from_report(items[1:])
    return None


def _label_from_cmd(argv: list[str]) -> str | None:
    """Command Code headless launch: the ``-p`` prompt's ``[label: ...]`` prefix.

    No ``-p`` means an interactive session, not a worker launch. A prompt
    that opens with a shell expansion fails open. Any other prompt with no
    prefix yields an empty label, which the strict check refuses.
    """
    if "-p" not in argv:
        return None
    at = argv.index("-p")
    if at + 1 >= len(argv):
        return None
    prompt = argv[at + 1]
    found = _CMD_LABEL.match(prompt)
    if found:
        return found.group(1)
    if _CMD_EXPANSION_RE.search(prompt):
        return None
    return ""


#: ``codex exec`` flags that take a value (skipped with it when hunting
#: the positional PROMPT). Anything else starting with ``-`` is a
#: boolean flag and skipped alone.
_CODEX_VALUE_FLAGS = frozenset({
    "-c", "--config", "-i", "--image", "-m", "--model", "--local-provider",
    "-p", "--profile", "-s", "--sandbox", "-C", "--cd", "--add-dir",
    "--thread-source", "--cyber-access-program",
    "--output-schema", "--color", "-o", "--output-last-message",
    "--enable", "--disable",
})
#: ``codex exec`` subcommands: a fresh worker launch takes a PROMPT, not one of these.
_CODEX_SUBCOMMANDS = frozenset({"resume", "fork", "review", "help"})


def _label_from_codex_exec(argv: list[str]) -> str | None:
    """Codex headless launch: the positional PROMPT's ``[label: ...]`` prefix.

    ``codex exec`` with no PROMPT (stdin ``-``, a subcommand, or flags
    only) is not a checkable launch and passes. A prompt that opens with
    a shell expansion fails open. Any other prompt with no prefix yields
    an empty label, which the strict check refuses. The blessed path is
    ``scripts/launch_codex.py`` (argv label, checked like the other
    launchers); this covers a raw ``codex exec`` the same way ``-p`` is
    covered for Command Code.
    """
    if not argv or argv[0] != "exec":
        return None
    rest = _strip_redirects(argv[1:])
    index = 0
    prompt: str | None = None
    while index < len(rest):
        token = rest[index]
        if index == 0 and token in _CODEX_SUBCOMMANDS:
            return None
        if token == "-":
            # Stdin prompt: unknowable on its own, but a later positional
            # PROMPT is still checkable, so keep scanning.
            index += 1
            continue
        if not token.startswith("-"):
            prompt = token
            break
        name = token.split("=", 1)[0]
        if "=" in token or name not in _CODEX_VALUE_FLAGS:
            index += 1
            continue
        index += 2
    if prompt is None:
        return None
    found = _CMD_LABEL.match(prompt)
    if found:
        return found.group(1)
    if _CMD_EXPANSION_RE.search(prompt):
        return None
    return ""


def _label_from_report(argv: list[str]) -> str | None:
    if "report" not in argv or "start" not in argv:
        return None
    try:
        at = argv.index("--label")
    except ValueError:
        return None
    if at + 1 >= len(argv) or argv[at + 1].startswith("-"):
        return None
    return argv[at + 1]


def _uncertain(segment: str, why: str) -> None:
    """Fail open with a breadcrumb: the label is not confidently known."""
    _log(f"uncertain tsv append ({why}), passing: {segment[:200]}")
    return None


def _settle(candidate: str | None, segment: str, why: str) -> str | None:
    """Return a concrete candidate, fail open on expansions, else None."""
    if candidate is None:
        return None
    if _UNCERTAIN_RE.search(candidate):
        return _uncertain(segment, why)
    return candidate


def _first_column(text: str) -> str | None:
    """First whitespace-separated field of appended TSV text, or None.

    ``\\t``/``\\n`` spellings read as tabs/newlines. Header rows
    (session_id/epoch markers) and overlong/pathological fields are not
    labels: None (pass silently).
    """
    full = text.replace("\\t", "\t").replace("\\n", "\n")
    if not full or "session_id" in full.lower() or "epoch" in full.lower():
        return None
    first = full.split()[0].strip("\"'") if full.split() else ""
    if not first or len(first) > 200 or "/" in first or "\\" in first or "\n" in first:
        return None
    return first or None


def _label_from_printf(argv: list[str], segment: str) -> str | None:
    """Label recorded by ``printf FORMAT ARGS...`` into a sessions TSV.

    The label is the first data argument filling the first ``%s``/``%b``
    (``printf '%s\\t%s\\t%s\\n' <label> ...``); a literal format with no
    conversions carries the row itself. Anything else (variables, a
    non-string first conversion, conversions with no data args, literal
    text before the first conversion) fails open.
    """
    rest = _strip_redirects(argv)[1:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    if rest and rest[0] == "-v":
        return None  # writes to a variable, not to the TSV
    if not rest:
        return _uncertain(segment, "printf without format")
    fmt, args = rest[0], rest[1:]
    match = _FORMAT_SPEC_RE.search(fmt)
    if match is None:
        return _settle(_first_column(fmt), segment, "printf row not concrete")
    if match.group(0)[-1] not in ("s", "b"):
        return _uncertain(segment, "printf first conversion is not %s/%b")
    if fmt[: match.start()].strip(" \t'\""):
        return _uncertain(segment, "printf literal prefix before first %s")
    if not args:
        return _uncertain(segment, "printf %s/%b without data args")
    return _settle(args[0].strip("\"'") or None, segment, "printf label not concrete")


def _label_from_echo(argv: list[str], segment: str) -> str | None:
    """Label recorded by ``echo [-e] '<label>\\t...'`` into a sessions TSV."""
    words = [token for token in _strip_redirects(argv)[1:] if not token.startswith("-")]
    if not words:
        return _uncertain(segment, "echo without text")
    return _settle(_first_column(" ".join(words)), segment, "echo label not concrete")


def _has_tsv_redirect(segment: str) -> bool:
    """True when any ``>``/``>>`` redirect in the segment targets a TSV."""
    if not any(name in segment.lower() for name in TSV_NAMES):
        return False
    for match in re.finditer(r">>?", segment):
        after = segment[match.end():].strip()
        if after.startswith("&"):
            continue  # fd duplication (`2>&1`), not a file target
        first = after.split()[0] if after.split() else ""
        if any(name in first for name in TSV_NAMES):
            return True
    return False


def _has_pipe_tee_tsv(command: str) -> bool:
    """True when the command pipes into ``tee`` writing a sessions TSV."""
    if not any(name in command.lower() for name in TSV_NAMES):
        return False
    return re.search(r"\btee\b", command) is not None


def _label_from_tsv_append(segment: str, argv: list[str]) -> str | None:
    """First-column label of a sessions-TSV append, else None.

    Only fires on a real ``>>``/``>`` redirect into a sessions TSV, and
    only when the appended text opens with one concrete label-like token
    (headers fail open; variables, substitutions and format leftovers
    fail open with a log line, never refuse).
    """
    if not _has_tsv_redirect(segment):
        return None
    if not argv:
        return None
    base = _base(argv[0])
    if base == "printf":
        return _label_from_printf(argv, segment)
    if base == "echo":
        return _label_from_echo(argv, segment)
    # `cat >> <tsv>` with no heredoc ships a file, not inline text:
    # nothing confidently checkable, so pass.
    return None


def extract_labels(command: str) -> list[str]:
    """Every worker label the command launches (usually zero or one)."""
    found: list[str] = []
    heredoc_tsv = False
    pipe_tee = _has_pipe_tee_tsv(command or "")
    for segment in _split_segments(command or ""):
        text = segment.strip().strip("&|;")
        if not text:
            heredoc_tsv = False
            continue
        try:
            argv = shlex.split(text)
        except ValueError:
            heredoc_tsv = False
            continue
        if heredoc_tsv:
            # First body line of `cat >> <tsv> <<EOF ...` (tabs spelled
            # literally, as in the echo/printf shapes).
            settled = _settle(_first_column(text), text, "heredoc label not concrete")
            if settled is not None:
                found.append(settled)
            heredoc_tsv = False
            continue
        if not argv:
            continue
        label = _label_from_argv(argv)
        if label is not None:
            found.append(label)
            continue
        if "<<<" in argv and (_has_tsv_redirect(text) or pipe_tee):
            # Here-string: `... <<< '<label>\t...' >> <tsv>`
            # (checked before `<<`, which `<<<` contains).
            at = argv.index("<<<")
            label = _settle(
                _first_column(" ".join(_strip_redirects(argv[at + 1 :]))),
                text,
                "here-string label not concrete",
            )
            if label is not None:
                found.append(label)
            continue
        if "<<" in text and any(name in text.lower() for name in TSV_NAMES):
            heredoc_tsv = True
            continue
        label = _label_from_tsv_append(text, argv)
        if label is not None:
            found.append(label)
            continue
        if pipe_tee and _base(argv[0]) in ("echo", "printf"):
            # Producer half of `echo|printf ... | tee -a <tsv>`.
            if _base(argv[0]) == "printf":
                label = _label_from_printf(argv, text)
            else:
                label = _label_from_echo(argv, text)
            if label is not None:
                found.append(label)
    return found


def judge(command: str) -> tuple[int, str]:
    """``(exit_code, stderr_text)`` for one Bash command. Never raises."""
    try:
        from dash import labels as label_lib
    except Exception as exc:  # fail open: a broken import never blocks
        _log(f"import failed: {exc}")
        return 0, ""
    try:
        labels = extract_labels(command)
    except Exception as exc:
        _log(f"extract failed: {exc}")
        return 0, ""
    for label in labels:
        try:
            verdict = label_lib.check(label)
        except Exception as exc:
            _log(f"check failed for {label!r}: {exc}")
            continue
        if verdict["ok"]:
            continue
        if label_lib.legacy_allowed():
            return 0, f"label warning (legacy replay): {verdict['fix']}\n"
        return 2, f"label refused: {verdict['fix']}\n"
    return 0, ""


#: Shell-tool names across the CLIs this guard serves. Claude Code sends
#: ``Bash``; Grok Build sends its native ``run_terminal_command`` (its
#: ``Bash`` matcher alias covers both, but the payload still carries the
#: native name); other harnesses use ``Shell``/``shell``. Anything else
#: passes untouched.
_SHELL_TOOLS = frozenset({"Bash", "run_terminal_command", "Shell", "shell"})


def _payload_command(payload: dict) -> str | None:
    """Shell command from a Claude-, Grok-, or Muse-shaped payload.

    Claude Code uses ``tool_name``/``tool_input``; Grok sends the same
    event with ``toolName``/``toolInput`` (plus ``hook_event_name``);
    Muse-style harnesses vary the key casing again. Only a dict input
    with a string ``command`` is examined; everything else fails open.
    """
    tool = payload.get("tool_name", payload.get("toolName", payload.get("tool", "")))
    if tool not in _SHELL_TOOLS:
        return None
    tool_input = (
        payload.get("tool_input")
        or payload.get("toolInput")
        or payload.get("input")
        or {}
    )
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    return command


def main() -> int:
    try:
        raw = sys.stdin.read(2 * 1024 * 1024)
    except Exception:
        return 0
    if not raw or not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except ValueError:
        return 0
    try:
        if not isinstance(payload, dict):
            return 0
        command = _payload_command(payload)
        if command is None:
            return 0
        code, err = judge(command)
        if err:
            sys.stderr.write(err)
        return code
    except Exception as exc:  # fail open on anything unexpected
        _log(f"hook failed: {exc}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
