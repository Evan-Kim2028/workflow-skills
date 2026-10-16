#!/bin/bash
# Start a Grok or muse worker once, then wait for it up to a time limit. Safe to call again: a second call only waits.
# usage: run_worker.sh <grok|muse> <label> <workspace> <prompt-file> <run-dir> [max-wait-seconds, default 540]
# Prints one line: "STATUS: running|done|failed out=<report-file> err=<stderr-file>".
# Grok runs go through launch_grok.sh (billed tokens are logged for agent-dash). muse runs use the contributor model at xhigh.
# Runs are logged to <run-dir>/cursor-sessions.tsv or <run-dir>/muse-sessions.tsv so agent-dash attributes them by label.
set -u
TIER="$1"; L="$2"; WS="$3"; PF="$4"; RD="$5"; MAXW="${6:-540}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$RD"
OUT="$RD/out-$L.md"; ERR="$RD/err-$L.log"; PIDF="$RD/$L.pid"
alive() { [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; }
if ! alive && [ ! -s "$OUT" ]; then
  [ -d "$WS" ] && [ -f "$PF" ] || { echo "STATUS: failed out=$OUT err=$ERR reason=missing-workspace-or-prompt"; exit 2; }
  case "$TIER" in
    grok)
      nohup "$HERE/launch_grok.sh" "$L" "$WS" "$PF" < /dev/null > "$OUT" 2> "$ERR" &
      echo $! > "$PIDF"
      printf "%s\t-\t%s\n" "$L" "$(date +%s)" >> "$RD/cursor-sessions.tsv" ;;
    muse)
      SID=$(uuidgen | tr 'A-Z' 'a-z')
      nohup muse exec --trust-workspace --model muse-spark-1.3-contributor --reasoning-effort xhigh \
        --workspace "$WS" --prompt-file "$PF" --disable-reminders --user-input-auto-resolve \
        --session-id "$SID" < /dev/null > "$OUT" 2> "$ERR" &
      echo $! > "$PIDF"
      printf "%s\t%s\t%s\n" "$L" "$SID" "$(date +%s)" >> "$RD/muse-sessions.tsv" ;;
    *) echo "STATUS: failed out=$OUT err=$ERR reason=unknown-tier"; exit 2 ;;
  esac
  sleep 5
fi
waited=0
while alive && [ "$waited" -lt "$MAXW" ]; do sleep 10; waited=$((waited + 10)); done
if alive; then echo "STATUS: running out=$OUT err=$ERR"; exit 0; fi
if [ -s "$OUT" ]; then echo "STATUS: done out=$OUT err=$ERR"; else echo "STATUS: failed out=$OUT err=$ERR reason=empty-report"; fi
