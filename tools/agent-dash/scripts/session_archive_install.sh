#!/bin/sh
# session_archive_install.sh - hourly native agent-session archive (macOS).
#
# Writes a launchd agent that runs `agent_dash.py logs sessions --apply
# --scheduled` once an hour. The job packs finished native session history
# (Claude Code, Codex, Grok build, Cursor, Command Code, pi, muse) into
# lossless tar.zst copies, ingests the transcript lines into the loopback
# VictoriaLogs store, and reclaims originals after a longer idle window.
# See docs/session-archive.md.
#
#   sh scripts/session_archive_install.sh             install or update (idempotent)
#   sh scripts/session_archive_install.sh --uninstall stop and remove the agent
#
# Why the code and the dest must stay out of ~/Documents: a process started
# by launchd gets no TCC ("privacy") grant. Reading anything under
# ~/Documents, ~/Desktop, ~/Downloads or ~/Library/Mobile Documents answers
# EPERM instead of prompting the user, so a job pointed there hangs on its
# first read. The old agent ran ~/.claude/tools/agent-dash/agent_dash.py and
# ~/.claude/scripts/memcap.sh, both symlinks into a checkout under
# ~/Documents, with its dest under ~/Documents/data: every tick got EPERM and
# the job never finished. The job therefore runs the deployed release
# (~/.local/share/agent-dash/releases/CURRENT) and writes to
# ~/.local/share/agent-sessions, and this installer refuses (exit 2) any code
# dir, memcap.sh or dest whose realpath is inside a TCC-protected folder.
#
# Test overrides (never needed in normal use):
#   SESSION_ARCHIVE_PLIST_DIR  launch agents dir (default ~/Library/LaunchAgents)
#   SESSION_ARCHIVE_LOG_DIR    log dir           (default ~/Library/Logs/agent-session-archive)
#   SESSION_ARCHIVE_CODE_DIR   code dir          (default ~/.local/share/agent-dash/releases/CURRENT/tools/agent-dash)
#   SESSION_ARCHIVE_DEST       archive dest      (default: config, else ~/.local/share/agent-sessions)
#   SESSION_ARCHIVE_CONFIG     config file       (default <state>/config.local.json)
#   SESSION_ARCHIVE_NO_LOAD=1  write the plist but do not bootstrap it with launchctl

set -eu

LABEL="com.agent-dash.session-archive"
PLIST_DIR="${SESSION_ARCHIVE_PLIST_DIR:-$HOME/Library/LaunchAgents}"
LOG_DIR="${SESSION_ARCHIVE_LOG_DIR:-$HOME/Library/Logs/agent-session-archive}"
PLIST="$PLIST_DIR/$LABEL.plist"
LOG="$LOG_DIR/launchd.log"
STATE_DIR="${AGENT_DASH_STATE_DIR:-$HOME/.local/share/agent-dash}"
CONFIG="${SESSION_ARCHIVE_CONFIG:-$STATE_DIR/config.local.json}"
CODE_DIR="${SESSION_ARCHIVE_CODE_DIR:-$HOME/.local/share/agent-dash/releases/CURRENT/tools/agent-dash}"
MEMCAP="$CODE_DIR/scripts/memcap.sh"
AGENT_DASH="$CODE_DIR/agent_dash.py"
PATH_VALUE="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

uninstall=0
for arg in "$@"; do
    case "$arg" in
        --uninstall) uninstall=1 ;;
        -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
        *) echo "session_archive_install: unknown argument '$arg'" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }

unload() {
    if [ "${SESSION_ARCHIVE_NO_LOAD:-0}" = "1" ]; then return 0; fi
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
}

if [ "$uninstall" = "1" ]; then
    unload
    rm -f "$PLIST"
    say "removed $LABEL; archives and state are kept"
    exit 0
fi

if [ "$(uname -s)" != "Darwin" ]; then
    echo "session_archive_install: this installer writes a launchd agent and runs on macOS only" >&2
    exit 2
fi

# --- paths: expand, then resolve every symlink before the privacy check ------

expand_tilde() {
    case "$1" in
        "~") printf '%s\n' "$HOME" ;;
        "~/"*) printf '%s\n' "$HOME/${1#\~/}" ;;
        *) printf '%s\n' "$1" ;;
    esac
}

# realpath(1) exists on recent macOS, but python3 is required anyway
# (agent_dash.py needs it), so use it and keep a pwd -P fallback.
resolve_path() {
    if [ -x /usr/bin/python3 ]; then
        /usr/bin/python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
    else
        (cd -P "$(dirname "$1")" 2>/dev/null && printf '%s/%s\n' "$(pwd -P)" "$(basename "$1")") \
            || printf '%s\n' "$1"
    fi
}

# The dest the archive pass itself will use: SESSION_ARCHIVE_DEST, else the
# config's log_archive.sessions.dest, else the module default.
configured_dest() {
    [ -f "$CONFIG" ] || return 0
    [ -x /usr/bin/python3 ] || return 0
    /usr/bin/python3 - "$CONFIG" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        section = (json.load(fh).get("log_archive") or {}).get("sessions") or {}
    dest = section.get("dest") if isinstance(section, dict) else None
except (OSError, ValueError, AttributeError):
    dest = None
print(dest if isinstance(dest, str) else "")
PY
}

DEST="${SESSION_ARCHIVE_DEST:-$(configured_dest)}"
DEST="$(expand_tilde "${DEST:-$HOME/.local/share/agent-sessions}")"

TCC_HOME="$(resolve_path "$HOME")"

refuse_tcc() {  # refuse_tcc <what> <path>
    real="$(resolve_path "$2")"
    for prefix in "$TCC_HOME/Documents" "$TCC_HOME/Desktop" "$TCC_HOME/Downloads" \
                  "$TCC_HOME/Library/Mobile Documents"; do
        case "$real" in
            "$prefix"|"$prefix"/*)
                warn "session_archive_install: refusing to install: $1 '$2' resolves to '$real',"
                warn "which is inside the macOS privacy (TCC) folder '$prefix'."
                warn "A launchd job cannot read that folder (EPERM), so the hourly run would"
                warn "fail or hang. Move the code (SESSION_ARCHIVE_CODE_DIR, normally"
                warn "~/.local/share/agent-dash/releases/CURRENT) and the archive dest"
                warn "(log_archive.sessions.dest, normally ~/.local/share/agent-sessions) out of"
                warn "~/Documents, ~/Desktop, ~/Downloads and iCloud Drive, then rerun."
                exit 2
                ;;
        esac
    done
}

refuse_tcc "code dir" "$CODE_DIR"
refuse_tcc "memcap.sh" "$MEMCAP"
refuse_tcc "archive dest" "$DEST"

if [ ! -f "$AGENT_DASH" ]; then
    warn "session_archive_install: warning: $AGENT_DASH does not exist yet."
    warn "The hourly job will fail until a release is deployed there (agent_dash.py deploy)."
fi

mkdir -p "$PLIST_DIR" "$LOG_DIR"

cat > "$PLIST.tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/sbin/taskpolicy</string>
        <string>-b</string>
        <string>$MEMCAP</string>
        <string>4096</string>
        <string>/usr/bin/python3</string>
        <string>$AGENT_DASH</string>
        <string>logs</string>
        <string>sessions</string>
        <string>--apply</string>
        <string>--scheduled</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>ProcessType</key>
    <string>Background</string>
    <key>LowPriorityIO</key>
    <true/>
    <key>Nice</key>
    <integer>10</integer>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$PATH_VALUE</string>
    </dict>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
EOF
if [ -f "$PLIST" ] && cmp -s "$PLIST" "$PLIST.tmp"; then
    rm -f "$PLIST.tmp"
    say "launch agent unchanged"
else
    mv "$PLIST.tmp" "$PLIST"
    say "wrote $PLIST"
    if [ "${SESSION_ARCHIVE_NO_LOAD:-0}" != "1" ]; then
        unload
        launchctl bootstrap "gui/$(id -u)" "$PLIST"
    fi
fi

if [ "${SESSION_ARCHIVE_NO_LOAD:-0}" = "1" ]; then
    say "SESSION_ARCHIVE_NO_LOAD=1: not started"
    exit 0
fi
say "installed $LABEL (hourly, logs at $LOG)"
