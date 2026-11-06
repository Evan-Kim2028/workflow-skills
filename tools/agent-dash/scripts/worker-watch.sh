#!/usr/bin/env bash
# Watch one worker run and return exactly one verdict line, so a lead is
# woken when the run ends, finishes but hangs, stalls, or runs too long.
#
#   worker-watch.sh <label> <worktree> [report]   (run_in_background: true)
#
# Verdicts (last line of output):
#   FINISHED      the worker process exited.
#   DONE-HUNG     the report exists, nothing changed for GRACE_S, but the
#                 process is still alive: it is killed by PID (2026-10-28: a
#                 muse run finished at 17:13 and sat hung until 20:57).
#   STALLED       no report, and for STALL_MIN minutes none of these moved:
#                 a file in the worktree, the CPU time of any process in the
#                 run's tree (the run and its descendants), or the STREAM
#                 file's size. The process is left running for the lead.
#                 A long evaluation in a child process is activity, not a stall.
#   OVERTIME      wall clock passed MAX_H hours; left running for the lead.
#
# Env: PATTERN (default muse-bin.*scratchpad[/]muse/<label>\.md; use
# "[g]rok -p .label: <label>" for grok build), GRACE_S=600, STALL_MIN=45,
# MAX_H=6, POLL_S=30. STALL_S overrides STALL_MIN in seconds (tests).
# STREAM is an optional file the run appends to; its growth counts as activity.
set -u
label=${1:?label}; wt=${2:?worktree}; report=${3:-$wt/reports/$label.md}
pattern=${PATTERN:-"muse-bin.*scratchpad[/]muse/${label}\\.md"}
grace=${GRACE_S:-600}; stall=${STALL_MIN:-45}; max_s=$(( ${MAX_H:-6} * 3600 )); poll=${POLL_S:-30}
stall_s=${STALL_S:-$(( stall * 60 ))}
stream=${STREAM:-}
start=$(date +%s)
last_act=$start
tmp=$(mktemp -d) || exit 1
trap 'rm -rf "$tmp"' EXIT
cpu_prev="$tmp/cpu.prev"; cpu_cur="$tmp/cpu.cur"
: > "$cpu_prev"

recent() { # any non-.git file in the worktree changed in the last $1 seconds
  local mins=$(( ($1 + 59) / 60 ))
  [ -n "$(find "$wt" -type f -not -path '*/.git/*' -mmin "-$mins" -print -quit 2>/dev/null)" ]
}

family() { # the pids in $1 (space-separated) plus every descendant, one per line
  ps -A -o pid= -o ppid= | awk -v roots="$1" '
    BEGIN { n = split(roots, r, " "); for (i = 1; i <= n; i++) want[r[i]] = 1 }
    { pid[NR] = $1; ppid[NR] = $2 }
    END {
      changed = 1
      while (changed) {
        changed = 0
        for (i = 1; i <= NR; i++)
          if (!(pid[i] in want) && (ppid[i] in want)) { want[pid[i]] = 1; changed = 1 }
      }
      for (p in want) print p
    }'
}

cpu_grew() { # 0 when any process in the run's tree used more CPU time since the last poll
  local fam list grew
  fam=$(family "$1")
  if [ -z "$fam" ]; then : > "$cpu_prev"; return 1; fi
  list=$(printf '%s\n' "$fam" | paste -sd, -)
  # ps time is [[dd-]hh:]mm:ss[.cc]; convert to seconds.
  ps -o pid= -o time= -p "$list" 2>/dev/null | awk '{
      n = split($2, d, "-"); t = d[n]; days = (n == 2) ? d[1] : 0
      m = split(t, f, ":"); s = 0; for (i = 1; i <= m; i++) s = s * 60 + f[i]
      print $1, days * 86400 + s }' > "$cpu_cur"
  grew=$(awk 'NR == FNR { prev[$1] = $2; next } ($2 > prev[$1] + 0) { g = 1 } END { print g + 0 }' "$cpu_prev" "$cpu_cur")
  mv "$cpu_cur" "$cpu_prev"
  [ "$grew" = 1 ]
}

size_of() { # byte size of $1, or 0 when unset or missing
  if [ -n "$1" ] && [ -f "$1" ]; then wc -c < "$1" | tr -d ' '; else echo 0; fi
}
last_size=$(size_of "$stream")

stream_grew() { # 0 when the STREAM file is larger than at the last poll
  local now_size
  now_size=$(size_of "$stream")
  if [ "$now_size" -gt "$last_size" ]; then last_size=$now_size; return 0; fi
  return 1
}

while pgrep -f "$pattern" >/dev/null; do
  now=$(date +%s)
  roots=$(pgrep -f "$pattern" | tr '\n' ' ')
  active=0
  if cpu_grew "$roots"; then active=1; fi
  if stream_grew; then active=1; fi
  if [ "$active" = 1 ]; then last_act=$now; fi
  if [ -s "$report" ] && [ $(( now - $(stat -f %m "$report") )) -ge "$grace" ] && ! recent "$grace"; then
    for p in $(pgrep -f "$pattern"); do kill "$p"; done
    sleep 5
    for p in $(pgrep -f "$pattern"); do kill -9 "$p"; done
    echo "$label DONE-HUNG: report written, idle ${grace}s, process killed $(date +%T)"; exit 0
  fi
  if [ ! -s "$report" ] && [ $(( now - last_act )) -ge "$stall_s" ] && ! recent "$stall_s"; then
    echo "$label STALLED: no worktree change, descendant CPU or stream growth for ${stall_s}s, no report, still running $(date +%T)"; exit 0
  fi
  if [ $(( now - start )) -ge "$max_s" ]; then
    echo "$label OVERTIME: running past ${MAX_H:-6} h $(date +%T)"; exit 0
  fi
  sleep "$poll"
done
echo "$label FINISHED $(date +%T)"
