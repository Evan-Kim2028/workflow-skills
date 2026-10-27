#!/bin/sh
# install_hooks.sh - opt-in hooks for agent-dash.
#
# Nothing installs these automatically.
#
#   sh tools/agent-dash/scripts/install_hooks.sh
#     once per checkout that should gate pushes (pre-push scoped gate).
#
#   sh tools/agent-dash/scripts/install_hooks.sh --claude-label-guard
#     once per machine: installs the Claude Code PreToolUse hook that
#     refuses worker launches with weak labels (scripts/label_guard_hook.py,
#     configured in ~/.claude/settings.json). Idempotent: re-running keeps
#     one entry and never touches other settings.
#
#   sh tools/agent-dash/scripts/install_hooks.sh --grok-label-guard
#     once per machine: installs the same guard as a native Grok Build
#     PreToolUse hook (~/.grok/hooks/label-guard.json, matcher Bash).
#     Grok's payload uses toolName/toolInput keys, which the guard reads
#     alongside the Claude Code shape. Idempotent; backs up the file it
#     replaces. Takes effect in new Grok sessions (hooks load at startup).
#
#   sh tools/agent-dash/scripts/install_hooks.sh --muse-label-guard
#     once per machine: installs the same guard as a Muse user-scope
#     plugin (PreToolUse hook from plugins/label-guard/; its entry
#     execs the live checkout hook). Takes effect in new muse sessions;
#     a hook approval step may be required (the installer says so).
#
#   sh tools/agent-dash/scripts/install_hooks.sh --claude-lead-guard
#     once per machine: installs the Claude Code lead-guard hooks that
#     keep a workstream lead wakeable and refilling
#     (scripts/lead_guard_hook.py, configured in ~/.claude/settings.json
#     as a PreToolUse hook with matcher AskUserQuestion and a Stop hook
#     with no matcher, each "timeout": 10). Idempotent: re-running keeps
#     one entry per hook and never touches other settings.
#
#   sh tools/agent-dash/scripts/install_hooks.sh --codex-shim
#     once per machine: copies the `muse` label-guard shim to
#     ~/.local/share/agent-dash/bin/muse and prints the PATH line that
#     puts it first. Codex documents no user-configurable pre-exec hook
#     surface in its installed docs/--help, so the shim (which refuses
#     `muse exec --prompt-file <bad-label>.md` before execing the real
#     binary) is the structural guard there. Raw TSV appends from Codex
#     stay unguarded: the residual gap. This flag edits no shell rc and
#     no Codex config; enabling the PATH line is the owner's step.
#
#   sh tools/agent-dash/scripts/install_hooks.sh --uninstall
#     removes the grok hook file, the muse plugin, the shim copy, and
#     the lead-guard entries from ~/.claude/settings.json (other settings
#     entries stay). Config backups (*.bak-*) are left in place.
#
# The pre-push install refuses to overwrite a foreign hook (one without
# the marker below); remove or merge that hook by hand first.

set -u

resolve_guard() {
    HERE="$(cd "$(dirname "$0")" && pwd)"
    if [ -f "$HOME/.claude/tools/agent-dash/scripts/label_guard_hook.py" ]; then
        printf '%s' "$HOME/.claude/tools/agent-dash/scripts/label_guard_hook.py"
    else
        printf '%s' "$HERE/label_guard_hook.py"
    fi
}

if [ "${1:-}" = "--claude-label-guard" ]; then
    HERE="$(cd "$(dirname "$0")" && pwd)"
    if [ -f "$HOME/.claude/tools/agent-dash/scripts/label_guard_hook.py" ]; then
        GUARD="$HOME/.claude/tools/agent-dash/scripts/label_guard_hook.py"
    else
        GUARD="$HERE/label_guard_hook.py"
    fi
    [ -f "$GUARD" ] || { echo "install_hooks: no label-guard hook at $GUARD" >&2; exit 2; }
    /usr/bin/python3 - "$GUARD" "$HOME/.claude/settings.json" <<'EOF'
import json, sys, time, os
guard, settings_path = sys.argv[1], sys.argv[2]
entry = {"matcher": "Bash",
         "hooks": [{"type": "command", "command": "python3 %s" % guard}]}
try:
    text = open(settings_path, encoding="utf-8").read() if os.path.isfile(settings_path) else "{}"
    settings = json.loads(text)
except (OSError, ValueError) as exc:
    print("install_hooks: %s does not parse (%s); fix it by hand" % (settings_path, exc), file=sys.stderr)
    sys.exit(1)
if not isinstance(settings, dict):
    print("install_hooks: %s is not an object; fix it by hand" % settings_path, file=sys.stderr)
    sys.exit(1)
hooks = settings.get("hooks")
if not isinstance(hooks, dict):
    hooks = {}
    settings["hooks"] = hooks
pre = hooks.get("PreToolUse")
if not isinstance(pre, list):
    pre = []
    hooks["PreToolUse"] = pre
if any(guard in json.dumps(item) for item in pre):
    print("install_hooks: label-guard hook already installed in %s" % settings_path)
    sys.exit(0)
backup = "%s.bak-%d" % (settings_path, int(time.time()))
try:
    with open(backup, "w", encoding="utf-8") as fh:
        fh.write(text)
except OSError as exc:
    print("install_hooks: cannot back up %s (%s)" % (settings_path, exc), file=sys.stderr)
    sys.exit(1)
pre.append(entry)
try:
    with open(settings_path, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2, sort_keys=True)
        fh.write("\n")
except OSError as exc:
    print("install_hooks: cannot write %s (%s)" % (settings_path, exc), file=sys.stderr)
    sys.exit(1)
print("install_hooks: label-guard hook installed in %s (backup %s)" % (settings_path, backup))
EOF
    exit $?
fi

if [ "${1:-}" = "--claude-lead-guard" ]; then
    HERE="$(cd "$(dirname "$0")" && pwd)"
    if [ -f "$HOME/.claude/tools/agent-dash/scripts/lead_guard_hook.py" ]; then
        GUARD="$HOME/.claude/tools/agent-dash/scripts/lead_guard_hook.py"
    else
        GUARD="$HERE/lead_guard_hook.py"
    fi
    [ -f "$GUARD" ] || { echo "install_hooks: no lead-guard hook at $GUARD" >&2; exit 2; }
    /usr/bin/python3 - "$GUARD" "$HOME/.claude/settings.json" <<'EOF'
import json, sys, time, os
guard, settings_path = sys.argv[1], sys.argv[2]
pre_entry = {"matcher": "AskUserQuestion",
             "hooks": [{"type": "command", "command": "python3 %s" % guard, "timeout": 10}]}
stop_entry = {"hooks": [{"type": "command", "command": "python3 %s" % guard, "timeout": 10}]}
try:
    text = open(settings_path, encoding="utf-8").read() if os.path.isfile(settings_path) else "{}"
    settings = json.loads(text)
except (OSError, ValueError) as exc:
    print("install_hooks: %s does not parse (%s); fix it by hand" % (settings_path, exc), file=sys.stderr)
    sys.exit(1)
if not isinstance(settings, dict):
    print("install_hooks: %s is not an object; fix it by hand" % settings_path, file=sys.stderr)
    sys.exit(1)
hooks = settings.get("hooks")
if not isinstance(hooks, dict):
    hooks = {}
    settings["hooks"] = hooks
changed = False
pre = hooks.get("PreToolUse")
if not isinstance(pre, list):
    pre = []
    hooks["PreToolUse"] = pre
if not any(guard in json.dumps(item) and (item.get("matcher") if isinstance(item, dict) else None) == "AskUserQuestion" for item in pre if isinstance(item, dict)):
    pre.append(pre_entry)
    changed = True
stop = hooks.get("Stop")
if not isinstance(stop, list):
    stop = []
    hooks["Stop"] = stop
if not any(guard in json.dumps(item) for item in stop):
    stop.append(stop_entry)
    changed = True
if not changed:
    print("install_hooks: lead-guard hook already installed in %s" % settings_path)
    sys.exit(0)
backup = "%s.bak-%d" % (settings_path, int(time.time()))
try:
    with open(backup, "w", encoding="utf-8") as fh:
        fh.write(text)
except OSError as exc:
    print("install_hooks: cannot back up %s (%s)" % (settings_path, exc), file=sys.stderr)
    sys.exit(1)
try:
    with open(settings_path, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2, sort_keys=True)
        fh.write("\n")
except OSError as exc:
    print("install_hooks: cannot write %s (%s)" % (settings_path, exc), file=sys.stderr)
    sys.exit(1)
print("install_hooks: lead-guard hook installed in %s (backup %s)" % (settings_path, backup))
EOF
    exit $?
fi

if [ "${1:-}" = "--grok-label-guard" ]; then
    GUARD="$(resolve_guard)"
    [ -f "$GUARD" ] || { echo "install_hooks: no label-guard hook at $GUARD" >&2; exit 2; }
    HOOKDIR="$HOME/.grok/hooks"
    FILE="$HOOKDIR/label-guard.json"
    mkdir -p "$HOOKDIR" || { echo "install_hooks: cannot create $HOOKDIR" >&2; exit 1; }
    WANT="$(printf '{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"python3 %s"}]}]}}' "$GUARD")"
    if [ -f "$FILE" ] && [ "$(cat "$FILE" 2>/dev/null)" = "$WANT" ]; then
        echo "install_hooks: grok label-guard hook already installed in $FILE"
        exit 0
    fi
    if [ -f "$FILE" ]; then
        BACKUP="$FILE.bak-$(date +%s)"
        cp "$FILE" "$BACKUP" || { echo "install_hooks: cannot back up $FILE" >&2; exit 1; }
        echo "install_hooks: backed up $FILE to $BACKUP"
    fi
    printf '%s\n' "$WANT" >"$FILE" || { echo "install_hooks: cannot write $FILE" >&2; exit 1; }
    if /usr/bin/python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$FILE"; then
        echo "install_hooks: grok label-guard hook installed in $FILE (takes effect in new Grok sessions)"
    else
        echo "install_hooks: wrote $FILE but it does not parse; fix it by hand" >&2
        exit 1
    fi
    exit 0
fi

if [ "${1:-}" = "--muse-label-guard" ]; then
    HERE="$(cd "$(dirname "$0")" && pwd)"
    BUNDLE="$HERE/../plugins/label-guard"
    [ -f "$BUNDLE/.muse-plugin/plugin.json" ] || { echo "install_hooks: no plugin bundle at $BUNDLE" >&2; exit 2; }
    GUARD="$(resolve_guard)"
    [ -f "$GUARD" ] || { echo "install_hooks: no label-guard hook at $GUARD" >&2; exit 2; }
    if ! command -v muse >/dev/null 2>&1; then
        echo "install_hooks: no 'muse' on PATH; install the bundle by hand: muse plugins install $BUNDLE --scope user" >&2
        exit 2
    fi
    VALID_OUT="$(muse plugins validate "$BUNDLE" 2>&1 | head -n 3)"
    printf '%s\n' "$VALID_OUT"
    case "$VALID_OUT" in
        valid*) ;;
        *) echo "install_hooks: plugin bundle does not validate" >&2; exit 1 ;;
    esac
    if muse plugins list --json 2>/dev/null | grep -q '"label-guard"'; then
        echo "install_hooks: muse label-guard plugin already installed (re-install with: muse plugins remove label-guard && $0 --muse-label-guard)"
        exit 0
    fi
    muse plugins install "$BUNDLE" --scope user
    code=$?
    if [ $code -ne 0 ]; then
        echo "install_hooks: muse plugins install failed (exit $code)" >&2
        exit $code
    fi
    echo "install_hooks: muse label-guard plugin installed (takes effect in new muse sessions;"
    echo "install_hooks: if 'muse plugins list' shows it needs approval, run: muse plugins approve <stable-id>)"
    exit 0
fi

if [ "${1:-}" = "--codex-shim" ]; then
    HERE="$(cd "$(dirname "$0")" && pwd)"
    SRC="$HERE/../shims/muse"
    [ -f "$SRC" ] || { echo "install_hooks: no shim at $SRC" >&2; exit 2; }
    DESTDIR="$HOME/.local/share/agent-dash/bin"
    DEST="$DESTDIR/muse"
    mkdir -p "$DESTDIR" || { echo "install_hooks: cannot create $DESTDIR" >&2; exit 1; }
    if [ -f "$DEST" ] && cmp -s "$SRC" "$DEST"; then
        echo "install_hooks: codex shim already installed at $DEST"
    else
        cp "$SRC" "$DEST" || { echo "install_hooks: cannot write $DEST" >&2; exit 1; }
        chmod +x "$DEST"
        echo "install_hooks: muse label-guard shim installed at $DEST"
    fi
    echo "install_hooks: to enable, put it first on PATH in new shells:"
    echo "install_hooks:   export PATH=\"$DESTDIR:\$PATH\""
    echo "install_hooks: then verify with: PATH=\"$DESTDIR:\$PATH\" command -v muse"
    exit 0
fi

if [ "${1:-}" = "--uninstall" ]; then
    GFILE="$HOME/.grok/hooks/label-guard.json"
    [ -f "$GFILE" ] && rm -f "$GFILE" && echo "install_hooks: removed $GFILE"
    SFILE_SETTINGS="$HOME/.claude/settings.json"
    if [ -f "$SFILE_SETTINGS" ]; then
        /usr/bin/python3 - "$SFILE_SETTINGS" <<'EOF'
import json, sys
settings_path = sys.argv[1]
try:
    with open(settings_path, encoding="utf-8") as fh:
        settings = json.load(fh)
except (OSError, ValueError) as exc:
    print("install_hooks: %s does not parse (%s); left alone" % (settings_path, exc), file=sys.stderr)
    sys.exit(0)
hooks = settings.get("hooks")
if not isinstance(hooks, dict):
    sys.exit(0)
changed = False
for key in ("PreToolUse", "Stop"):
    entries = hooks.get(key)
    if not isinstance(entries, list):
        continue
    kept = [item for item in entries if "lead_guard_hook.py" not in json.dumps(item)]
    if len(kept) != len(entries):
        changed = True
        if kept:
            hooks[key] = kept
        else:
            del hooks[key]
if changed:
    try:
        with open(settings_path, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except OSError as exc:
        print("install_hooks: cannot write %s (%s)" % (settings_path, exc), file=sys.stderr)
        sys.exit(1)
    print("install_hooks: removed lead-guard entries from %s" % settings_path)
else:
    print("install_hooks: no lead-guard entries in %s" % settings_path)
EOF
    fi
    if command -v muse >/dev/null 2>&1 && muse plugins list --json 2>/dev/null | grep -q '"label-guard"'; then
        muse plugins remove label-guard 2>&1 | head -n 3 || echo "install_hooks: muse plugin remove failed" >&2
    fi
    SFILE="$HOME/.local/share/agent-dash/bin/muse"
    [ -f "$SFILE" ] && rm -f "$SFILE" && echo "install_hooks: removed $SFILE"
    echo "install_hooks: uninstall done (backups *.bak-* left in place)"
    exit 0
fi

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    echo "usage: sh tools/agent-dash/scripts/install_hooks.sh [--claude-label-guard|--claude-lead-guard|--grok-label-guard|--muse-label-guard|--codex-shim|--uninstall]"
    exit 0
fi

if [ -n "${1:-}" ]; then
    echo "install_hooks: unknown flag '$1' (try --help)" >&2
    exit 2
fi

MARKER="# agent-dash-gate (opt-in via tools/agent-dash/scripts/install_hooks.sh)"

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo "install_hooks: not inside a git checkout" >&2
    exit 2
}
HOOK="$ROOT/.git/hooks/pre-push"

if [ -f "$HOOK" ] && ! grep -q "agent-dash-gate" "$HOOK" 2>/dev/null; then
    echo "install_hooks: refusing to overwrite foreign hook at $HOOK" >&2
    echo "install_hooks: remove it or merge the gate call by hand, then re-run" >&2
    exit 1
fi

cat >"$HOOK" <<EOF
#!/bin/sh
$MARKER
# Push gate: scoped agent-dash tests for what changed. Bypass with --no-verify.
sh tools/agent-dash/scripts/gate.sh
EOF
chmod +x "$HOOK"
echo "install_hooks: pre-push hook installed at $HOOK"
