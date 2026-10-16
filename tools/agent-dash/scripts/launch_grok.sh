#!/bin/bash
# Launch a headless cursor-agent (Grok) run and record its billed token usage for agent-dash.
# usage: launch_grok.sh <label> <workspace> <prompt-file> [model]
# stdout is the run's final assistant text (redirect it to the report file); one usage line
# per run is appended to $AGENT_DASH_CURSOR_USAGE (default ~/.local/share/agent-dash/cursor-usage.jsonl).
# The prompt file should start with "[label: <label>]" so watchers and the dashboard can find the run.
set -o pipefail
L="$1"; WS="$2"; PF="$3"
MODEL="${4:-grok-4.7[context=500k,reasoning_effort=xhigh,fast=false]}"
[ -n "$L" ] && [ -d "$WS" ] && [ -f "$PF" ] || { echo "usage: launch_grok.sh <label> <workspace> <prompt-file> [model]" >&2; exit 2; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
prompt=$(cat "$PF")
cursor-agent -p --model "$MODEL" --force --trust --workspace "$WS" --output-format stream-json "$prompt" \
  | /usr/bin/python3 "$HERE/grok_stream_filter.py" --label "$L" --cwd "$WS" --model "$MODEL"
