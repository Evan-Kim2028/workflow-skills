#!/usr/bin/env bash
# Run a command under a resident-memory cap on macOS (no systemd-run there).
# Usage: memcap.sh <max_mb> <command...>   Kills the whole process group past the cap; exit 137.
set -u
max_mb=$1; shift
# Machine-specific refusals live outside this repo; a rule file may exit before the run starts.
rules="${AGENT_DASH_MEMCAP_RULES:-$HOME/.config/agent-dash/memcap-rules.sh}"
if [ -r "$rules" ]; then . "$rules"; fi
set -m
"$@" &
pid=$!
pgid=$(ps -o pgid= -p "$pid" | tr -d ' ')
while kill -0 "$pid" 2>/dev/null; do
  rss_kb=$(ps -A -o pgid=,rss= 2>/dev/null | awk -v g="$pgid" '$1==g {s+=$2} END {print s+0}')
  if [ "$rss_kb" -gt $((max_mb * 1024)) ]; then
    echo "memcap: $((rss_kb / 1024)) MB > ${max_mb} MB cap, killing process group $pgid" >&2
    kill -TERM -"$pgid" 2>/dev/null; sleep 2; kill -KILL -"$pgid" 2>/dev/null
    exit 137
  fi
  sleep 1
done
wait "$pid"
