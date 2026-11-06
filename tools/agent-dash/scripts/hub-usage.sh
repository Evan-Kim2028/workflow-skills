#!/usr/bin/env bash
# Hub token accounting for a project laid out as reports/<WS>/ directories.
#   reports/<WS>/muse-sessions.tsv     label<TAB>session_id<TAB>started_epoch   (muse runs)
#   reports/<WS>/claude-subagents.tsv  ts ws model label subagent_tokens tool_uses duration_ms
# Usage: hub-usage.sh <project-dir>
# Prints per-WS and TOTAL tables. Muse rows come from muse-usage.py --manifest;
# Claude subagent rows are grouped by the model column.
set -u
[ $# -eq 1 ] || { echo "usage: $(basename "$0") <project-dir>" >&2; exit 2; }
proj=$(cd "$1" 2>/dev/null && pwd) || { echo "no such dir: $1" >&2; exit 2; }
reports="$proj/reports"
[ -d "$reports" ] || { echo "no reports/ under $proj" >&2; exit 1; }
MUSE="$(CDPATH= cd -- "$(dirname "$0")" && pwd)/muse-usage.py"
tmp=$(mktemp -d) || exit 1
trap 'rm -rf "$tmp"' EXIT

# --- Muse: per-WS manifest totals (header row and blank rows dropped) ---
: > "$tmp/muse.tsv"
for f in "$reports"/*/muse-sessions.tsv; do
  [ -s "$f" ] || continue
  ws=$(basename "$(dirname "$f")")
  awk -F'\t' '$2 != "" && $2 != "session_id"' "$f" > "$tmp/manifest.tsv"
  [ -s "$tmp/manifest.tsv" ] || continue
  python3 -I "$MUSE" --manifest "$tmp/manifest.tsv" 2>/dev/null \
    | awk -v ws="$ws" '$1=="TOTAL"{gsub(/,/,""); print ws"\t"$2"\t"$3"\t"$5"\t"$6"\t"$7"\t"$9}' >> "$tmp/muse.tsv"
done

echo "## Muse sessions by workstream (manifest totals)"
printf 'WS\tsessions\tsteps\tinput\tcached\toutput\tmodel_s\n'
sort "$tmp/muse.tsv"
awk -F'\t' '{s+=$2; st+=$3; i+=$4; c+=$5; o+=$6; m+=$7}
  END{printf "TOTAL\t%d\t%d\t%d\t%d\t%d\t%d\n", s, st, i, c, o, m}' "$tmp/muse.tsv"

# --- Claude subagents: one row per (WS directory, model) ---
: > "$tmp/claude.tsv"
for f in "$reports"/*/claude-subagents.tsv; do
  [ -s "$f" ] || continue
  ws=$(basename "$(dirname "$f")")
  awk -F'\t' -v ws="$ws" '$5 ~ /^[0-9]+$/ {print ws"\t"$3"\t"$5"\t"$6"\t"$7}' "$f" >> "$tmp/claude.tsv"
done

echo; echo "## Claude subagents by workstream and model"
printf 'WS\tmodel\truns\ttokens\ttool_uses\twall_min\n'
awk -F'\t' '{k=$1"\t"$2; n[k]++; t[k]+=$3; u[k]+=$4; d[k]+=$5}
  END{for (k in n) printf "%s\t%d\t%d\t%d\t%.1f\n", k, n[k], t[k], u[k], d[k]/60000}' "$tmp/claude.tsv" | sort
echo; echo "## Claude subagents by model (all workstreams)"
printf 'model\truns\ttokens\ttool_uses\twall_min\n'
awk -F'\t' '{k=$2; n[k]++; t[k]+=$3; u[k]+=$4; d[k]+=$5; N++; T+=$3; U+=$4; D+=$5}
  END{for (k in n) printf "%s\t%d\t%d\t%d\t%.1f\n", k, n[k], t[k], u[k], d[k]/60000
       printf "TOTAL\t%d\t%d\t%d\t%.1f\n", N, T, U, D/60000}' "$tmp/claude.tsv" | sort

echo; echo "## Combined TOTAL"
printf 'muse_sessions\tmuse_output\tmuse_model_min\tclaude_runs\tclaude_tokens\tclaude_wall_min\n'
awk -F'\t' 'NR==FNR{ms+=$2; mo+=$6; mm+=$7; next} {cr++; ct+=$3; cd+=$5}
  END{printf "%d\t%d\t%.1f\t%d\t%d\t%.1f\n", ms, mo, mm/60, cr, ct, cd/60000}' "$tmp/muse.tsv" "$tmp/claude.tsv"
