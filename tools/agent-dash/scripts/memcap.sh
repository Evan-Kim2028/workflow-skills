#!/usr/bin/env bash
# Run a command under a resident-memory cap on macOS (no systemd-run there).
# Usage: memcap.sh <max_mb> <command...>   Kills the whole process group past the cap; exit 137.
# MEMCAP_POLL_S sets the poll interval (default 2 s; 5 s once the job has run 60 s under half the cap).
set -u
max_mb=$1; shift
rules="${AGENT_DASH_MEMCAP_RULES:-$HOME/.config/agent-dash/memcap-rules.sh}"
if [ -r "$rules" ]; then . "$rules"; fi
set -m
"$@" &
pid=$!
pgid=$(ps -o pgid= -p "$pid" | tr -d ' ')
cap_kb=$((max_mb * 1024))
base_s=2
calm_s=5
if [ -n "${MEMCAP_POLL_S:-}" ]; then base_s=$MEMCAP_POLL_S; calm_s=$MEMCAP_POLL_S; fi
calm_from=$SECONDS
while kill -0 "$pid" 2>/dev/null; do
  rss_kb=0
  while read -r kb; do
    if [[ $kb =~ ^[0-9]+$ ]]; then rss_kb=$((rss_kb + kb)); fi
  done <<< "$(ps -o rss= -g "$pgid" 2>/dev/null)"
  if (( rss_kb > cap_kb )); then
    echo "memcap: $((rss_kb / 1024)) MB > ${max_mb} MB cap, killing process group $pgid" >&2
    kill -TERM -"$pgid" 2>/dev/null; sleep 2; kill -KILL -"$pgid" 2>/dev/null
    exit 137
  fi
  if (( rss_kb * 2 >= cap_kb )); then calm_from=$SECONDS; fi
  interval=$base_s
  if (( SECONDS - calm_from >= 60 )); then interval=$calm_s; fi
  sleep "$interval"
done
wait "$pid"
