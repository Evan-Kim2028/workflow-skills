#!/bin/sh
# vlogs_install.sh - local VictoriaLogs for archived agent logs (macOS).
#
# Downloads the pinned VictoriaLogs release, verifies it against the
# release checksum file, installs it under ~/.local/share/agent-dash/vlogs/
# and writes a launchd agent with KeepAlive. The store listens on
# 127.0.0.1:19428 only. Never public.
#
#   sh scripts/vlogs_install.sh             install or update (idempotent)
#   sh scripts/vlogs_install.sh --uninstall stop and remove the service and binary
#   sh scripts/vlogs_install.sh --uninstall --purge   also delete the stored data
#
# Test overrides (never needed in normal use):
#   VLOGS_HOME       install dir            (default ~/.local/share/agent-dash/vlogs)
#   VLOGS_DATA       store data dir         (default $VLOGS_HOME/data)
#   VLOGS_MAX_DISK   retention disk cap     (default 20GiB)
#   VLOGS_PLIST_DIR  launch agents dir      (default ~/Library/LaunchAgents)
#   VLOGS_NO_LOAD=1  write the plist but do not bootstrap it with launchctl

set -eu

VERSION="v1.53.0"
PORT="19428"
LABEL="com.agent-dash.vlogs"
BASE_URL="https://github.com/VictoriaMetrics/VictoriaLogs/releases/download/${VERSION}"

HOME_DIR="${VLOGS_HOME:-$HOME/.local/share/agent-dash/vlogs}"
PLIST_DIR="${VLOGS_PLIST_DIR:-$HOME/Library/LaunchAgents}"
PLIST="$PLIST_DIR/$LABEL.plist"
BIN="$HOME_DIR/bin/victoria-logs-prod"
DATA="${VLOGS_DATA:-$HOME_DIR/data}"
MAX_DISK="${VLOGS_MAX_DISK:-20GiB}"
LOG="$HOME_DIR/server.log"
VERSION_FILE="$HOME_DIR/VERSION"

uninstall=0
purge=0
for arg in "$@"; do
    case "$arg" in
        --uninstall) uninstall=1 ;;
        --purge) purge=1 ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        *) echo "vlogs_install: unknown argument '$arg'" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }

unload() {
    if [ "${VLOGS_NO_LOAD:-0}" = "1" ]; then return 0; fi
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
}

if [ "$uninstall" = "1" ]; then
    unload
    rm -f "$PLIST"
    rm -f "$BIN" "$VERSION_FILE"
    if [ "$purge" = "1" ]; then
        rm -rf "$DATA"
        say "removed service, binary and data under $HOME_DIR"
    else
        say "removed service and binary; data kept under $DATA (use --purge to delete)"
    fi
    exit 0
fi

if [ "$(uname -s)" != "Darwin" ]; then
    echo "vlogs_install: this installer writes a launchd agent and runs on macOS only" >&2
    exit 2
fi

case "$(uname -m)" in
    arm64) ARCH="arm64" ;;
    x86_64) ARCH="amd64" ;;
    *) echo "vlogs_install: unsupported machine $(uname -m)" >&2; exit 2 ;;
esac

ASSET="victoria-logs-darwin-${ARCH}-${VERSION}.tar.gz"
SUMS="victoria-logs-darwin-${ARCH}-${VERSION}_checksums.txt"

mkdir -p "$HOME_DIR/bin" "$DATA" "$PLIST_DIR"

if [ -x "$BIN" ] && [ -f "$VERSION_FILE" ] && [ "$(cat "$VERSION_FILE")" = "$VERSION" ]; then
    say "binary $VERSION already installed"
else
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/vlogs-install.XXXXXX")
    trap 'rm -rf "$tmp"' EXIT
    say "downloading $ASSET"
    curl -fsSL --max-time 300 -o "$tmp/$ASSET" "$BASE_URL/$ASSET"
    curl -fsSL --max-time 60 -o "$tmp/$SUMS" "$BASE_URL/$SUMS"
    want=$(awk -v f="$ASSET" '$2 == f || $2 == "*" f { print $1; exit }' "$tmp/$SUMS")
    if [ -z "$want" ]; then
        echo "vlogs_install: $ASSET is not in $SUMS" >&2
        exit 1
    fi
    got=$(shasum -a 256 "$tmp/$ASSET" | awk '{print $1}')
    if [ "$got" != "$want" ]; then
        echo "vlogs_install: checksum mismatch for $ASSET (want $want, got $got)" >&2
        exit 1
    fi
    say "checksum ok ($got)"
    tar -xzf "$tmp/$ASSET" -C "$tmp"
    found=$(find "$tmp" -type f -name 'victoria-logs-prod' | head -1)
    if [ -z "$found" ]; then
        echo "vlogs_install: victoria-logs-prod not in the archive" >&2
        exit 1
    fi
    install -m 0755 "$found" "$BIN"
    printf '%s\n' "$VERSION" > "$VERSION_FILE"
    say "installed $BIN"
fi

cat > "$PLIST.tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$BIN</string>
        <string>-httpListenAddr=127.0.0.1:$PORT</string>
        <string>-storageDataPath=$DATA</string>
        <string>-retentionPeriod=100y</string>
        <string>-retention.maxDiskSpaceUsageBytes=$MAX_DISK</string>
        <string>-memory.allowedBytes=1GiB</string>
        <string>-insert.maxLineSizeBytes=8MiB</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
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
    if [ "${VLOGS_NO_LOAD:-0}" != "1" ]; then
        unload
        launchctl bootstrap "gui/$(id -u)" "$PLIST"
    fi
fi

if [ "${VLOGS_NO_LOAD:-0}" = "1" ]; then
    say "VLOGS_NO_LOAD=1: not started"
    exit 0
fi

i=0
until curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 30 ]; then
        echo "vlogs_install: store did not answer on 127.0.0.1:$PORT; see $LOG" >&2
        exit 1
    fi
    sleep 1
done
say "VictoriaLogs $VERSION healthy on 127.0.0.1:$PORT"
