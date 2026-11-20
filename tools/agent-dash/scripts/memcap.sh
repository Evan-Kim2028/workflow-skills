#!/usr/bin/env bash
# Run a command under a resident-memory cap on macOS (no systemd-run there).
# Usage: memcap.sh <max_mb> <command...>   Kills the whole process group past the cap; exit 137.
# The child leads its own process group, created without bash job control: `set -m` needs
# setpgid, which some agentq pi workers refuse ("child setpgid ... Operation not permitted"),
# and a refused group leaves the poll loop summing the caller's group, so the cap kill takes
# out the caller's pipeline and memcap exits 143 with no output. When the group cannot be
# created, the poll loop sums RSS over the child's descendant tree and kills that tree instead.
# MEMCAP_NO_SETPGRP=1 forces that fallback (tests use it).
# MEMCAP_POLL_S sets the poll interval (default 2 s; 5 s once the job has run 60 s under half the cap).
set -u
if [ $# -lt 2 ]; then
  echo "usage: memcap.sh <max_mb> <command...>" >&2
  exit 2
fi
max_mb=$1; shift
rules="${AGENT_DASH_MEMCAP_RULES:-$HOME/.config/agent-dash/memcap-rules.sh}"
if [ -r "$rules" ]; then . "$rules"; fi
cap_kb=$((max_mb * 1024))
base_s=2
calm_s=5
if [ -n "${MEMCAP_POLL_S:-}" ]; then base_s=$MEMCAP_POLL_S; calm_s=$MEMCAP_POLL_S; fi

# Catch TERM/INT from the start, so a signal that arrives while the child is starting is still
# forwarded to it instead of leaving it behind in its own process group.
sig=""
trap 'sig=TERM' TERM
trap 'sig=INT' INT

# memcap's own polling must not go through a PATH shim: agentq's `sleep` shim refuses a run once
# its budget is spent, which would turn the poll loop into a fork storm.
nap() { if [ -x /bin/sleep ]; then /bin/sleep "$1"; else sleep "$1"; fi; }

# Start the child as its own process-group leader without bash job control. perl ships with
# macOS, python3 is the fallback. The wrapper execs `bash -c '"$@"'` in place, so the pid stays
# the group leader and shell builtins (`echo`, `printf`) still work. A refused setpgrp leaves
# the child in this shell's group, and the poll loop uses the tree instead.
start_child() {
  if [ -z "${MEMCAP_NO_SETPGRP:-}" ]; then
    if command -v perl >/dev/null 2>&1; then
      perl -e 'setpgrp(0, 0); exec { $ARGV[0] } @ARGV or exit 127' -- \
        "${BASH:-/bin/bash}" -c '"$@"' -- "$@" <&0 &
      return
    fi
    if command -v python3 >/dev/null 2>&1; then
      python3 -c 'import os, sys
try:
    os.setpgid(0, 0)
except OSError:
    pass
os.execvp(sys.argv[1], sys.argv[1:])' "${BASH:-/bin/bash}" -c '"$@"' -- "$@" <&0 &
      return
    fi
  fi
  "$@" <&0 &
}
start_child "$@"
pid=$!

# The child's own process group, once it has one. Empty means "use the descendant tree".
pgid=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  _g=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
  if [ -n "$_g" ] && [ "$_g" = "$pid" ]; then pgid=$_g; break; fi
  if ! kill -0 "$pid" 2>/dev/null; then break; fi
  nap 0.05
done

# Summed RSS (kB) of process group $1. BSD `ps -g` selects a process group; Linux `ps -g`
# selects a session, so Linux sums the pgid column of one snapshot instead.
if [ "$(uname -s)" = Darwin ]; then
  group_rss() { ps -o rss= -g "$1" 2>/dev/null | awk '{ s += $1 } END { print s + 0 }'; }
else
  group_rss() { ps -A -o pgid=,rss= 2>/dev/null | awk -v g="$1" '$1 == g { s += $2 } END { print s + 0 }'; }
fi

# The pids of $1 and its descendants, from one ps snapshot.
tree_pids() {
  ps -A -o pid=,ppid= 2>/dev/null | awk -v root="$1" '
    { par[$1] = $2; pids[++n] = $1 }
    END {
      for (i = 1; i <= n; i++) {
        x = pids[i]
        for (d = 0; d < 64 && x != 0 && x != 1; d++) {
          if (x == root) { print pids[i]; break }
          if (!(x in par)) break
          x = par[x]
        }
      }
    }'
}

# The summed RSS (kB) of $1 and its descendants.
tree_rss() {
  ps -A -o pid=,ppid=,rss= 2>/dev/null | awk -v root="$1" '
    { par[$1] = $2; rss[$1] = $3; pids[++n] = $1 }
    END {
      total = 0
      for (i = 1; i <= n; i++) {
        x = pids[i]
        for (d = 0; d < 64 && x != 0 && x != 1; d++) {
          if (x == root) { total += rss[pids[i]]; break }
          if (!(x in par)) break
          x = par[x]
        }
      }
      print total + 0
    }'
}

# TERM/KILL the child's tree: descendants first, then the child, so nothing is orphaned out of
# reach. Pids seen on an earlier pass are kept, so a child that outlives its parent is still hit.
tree_seen=""
kill_tree() {
  _sig=$1
  tree_seen="$tree_seen $(tree_pids "$pid")"
  for _p in $tree_seen; do
    [ "$_p" = "$pid" ] && continue
    kill -"$_sig" "$_p" 2>/dev/null
  done
  kill -"$_sig" "$pid" 2>/dev/null
}

# TERM/INT reach the child's group (or tree) even though it is not the terminal's foreground group.
forward() {
  if [ -n "$pgid" ]; then kill -"$1" -"$pgid" 2>/dev/null; else kill_tree "$1"; fi
  kill -"$1" "$pid" 2>/dev/null
}

# Forward the signal we caught, give the child a moment, then KILL what is left.
on_signal() {
  forward "$sig"
  _grace=0
  while kill -0 "$pid" 2>/dev/null && [ "$_grace" -lt 20 ]; do nap 0.1; _grace=$((_grace + 1)); done
  if kill -0 "$pid" 2>/dev/null; then forward KILL; fi
  wait "$pid" 2>/dev/null
  if [ "$sig" = INT ]; then exit 130; fi
  exit 143
}

calm_from=$SECONDS
while kill -0 "$pid" 2>/dev/null; do
  if [ -n "$sig" ]; then on_signal; fi
  if [ -n "$pgid" ]; then
    rss_kb=$(group_rss "$pgid")
  else
    rss_kb=$(tree_rss "$pid")
  fi
  if (( rss_kb > cap_kb )); then
    if [ -n "$pgid" ]; then
      echo "memcap: $((rss_kb / 1024)) MB > ${max_mb} MB cap, killing process group $pgid" >&2
      kill -TERM -"$pgid" 2>/dev/null; nap 2; kill -KILL -"$pgid" 2>/dev/null
    else
      echo "memcap: $((rss_kb / 1024)) MB > ${max_mb} MB cap, killing process tree $pid" >&2
      kill_tree TERM; nap 2; kill_tree KILL
    fi
    exit 137
  fi
  if (( rss_kb * 2 >= cap_kb )); then calm_from=$SECONDS; fi
  interval=$base_s
  if (( SECONDS - calm_from >= 60 )); then interval=$calm_s; fi
  nap "$interval"
done
wait "$pid"
status=$?
if [ -n "$sig" ]; then on_signal; fi
exit "$status"
