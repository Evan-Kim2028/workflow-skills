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
#   STALLED       no file in the worktree changed for STALL_MIN minutes and
#                 no report; the process is left running for the lead.
#   OVERTIME      wall clock passed MAX_H hours; left running for the lead.
#
# Env: PATTERN (default muse-bin.*scratchpad[/]muse/<label>\.md; use
# "[g]rok -p .label: <label>" for grok build), GRACE_S=600, STALL_MIN=45,
# MAX_H=6, POLL_S=30.
set -u
label=${1:?label}; wt=${2:?worktree}; report=${3:-$wt/reports/$label.md}
pattern=${PATTERN:-"muse-bin.*scratchpad[/]muse/${label}\\.md"}
grace=${GRACE_S:-600}; stall=${STALL_MIN:-45}; max_s=$(( ${MAX_H:-6} * 3600 )); poll=${POLL_S:-30}
start=$(date +%s)

recent() { # any non-.git file in the worktree changed in the last $1 seconds
  local mins=$(( ($1 + 59) / 60 ))
  [ -n "$(find "$wt" -type f -not -path '*/.git/*' -mmin "-$mins" -print -quit 2>/dev/null)" ]
}

while pgrep -f "$pattern" >/dev/null; do
  now=$(date +%s)
  if [ -s "$report" ] && [ $(( now - $(stat -f %m "$report") )) -ge "$grace" ] && ! recent "$grace"; then
    for p in $(pgrep -f "$pattern"); do kill "$p"; done
    sleep 5
    for p in $(pgrep -f "$pattern"); do kill -9 "$p"; done
    echo "$label DONE-HUNG: report written, idle ${grace}s, process killed $(date +%T)"; exit 0
  fi
  if [ ! -s "$report" ] && [ $(( now - start )) -ge $(( stall * 60 )) ] && ! recent $(( stall * 60 )); then
    echo "$label STALLED: no worktree change in ${stall} min, no report, still running $(date +%T)"; exit 0
  fi
  if [ $(( now - start )) -ge "$max_s" ]; then
    echo "$label OVERTIME: running past ${MAX_H:-6} h $(date +%T)"; exit 0
  fi
  sleep "$poll"
done
echo "$label FINISHED $(date +%T)"
