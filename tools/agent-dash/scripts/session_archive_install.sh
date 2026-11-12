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
# Test overrides (never needed in normal use):
#   SESSION_ARCHIVE_PLIST_DIR  launch agents dir (default ~/Library/LaunchAgents)
#   SESSION_ARCHIVE_LOG_DIR    log dir           (default ~/Library/Logs/agent-session-archive)
#   SESSION_ARCHIVE_NO_LOAD=1  write the plist but do not bootstrap it with launchctl

set -eu

LABEL="com.agent-dash.session-archive"
PLIST_DIR="${SESSION_ARCHIVE_PLIST_DIR:-$HOME/Library/LaunchAgents}"
LOG_DIR="${SESSION_ARCHIVE_LOG_DIR:-$HOME/Library/Logs/agent-session-archive}"
PLIST="$PLIST_DIR/$LABEL.plist"
LOG="$LOG_DIR/launchd.log"
MEMCAP="$HOME/.claude/scripts/memcap.sh"
AGENT_DASH="$HOME/.claude/tools/agent-dash/agent_dash.py"
PATH_VALUE="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

uninstall=0
for arg in "$@"; do
    case "$arg" in
        --uninstall) uninstall=1 ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) echo "session_archive_install: unknown argument '$arg'" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }

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
