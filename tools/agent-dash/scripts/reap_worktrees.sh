#!/bin/bash
# reap_worktrees.sh <ws-slug> [--dry-run] [repo ...]
#   Remove worktrees whose branch is fully merged into origin/main.
#   <ws-slug> is the workstream marker in the worktree path, e.g. "ws11".
# Safe by construction: skips any worktree with uncommitted changes or unmerged commits,
# and never touches a branch that is not an ancestor of origin/main.
set -o pipefail
WS="${1:-}"; shift || true
if [ -z "$WS" ]; then echo "usage: reap_worktrees.sh <ws-slug> [--dry-run] [repo ...]" >&2; exit 2; fi
DRY=0
repos=()
for a in "$@"; do
  if [ "$a" = "--dry-run" ]; then DRY=1; else repos+=("$a"); fi
done
if [ "${#repos[@]}" -eq 0 ]; then
  echo "usage: reap_worktrees.sh <ws-slug> [--dry-run] <repo> [repo ...]" >&2
  echo "  name at least one repo directory under \$AGENT_DASH_PROJECTS_DIR" >&2
  exit 2
fi
# Parent directory holding the repo checkouts and their sibling worktrees.
# Override with AGENT_DASH_PROJECTS_DIR; defaults to the parent of this repo.
PROJ="${AGENT_DASH_PROJECTS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
reaped=0; kept=0
for repo in "${repos[@]}"; do
  R="$PROJ/$repo"
  git -C "$R" fetch origin main -q 2>/dev/null
  MAIN=$(git -C "$R" rev-parse origin/main)
  git -C "$R" worktree list --porcelain | awk '/^worktree /{print $2}' | while read -r wt; do
    [ "$wt" = "$R" ] && continue
    case "$wt" in *wt-"$WS"-*|*wt-train-"$WS"*) ;; *) continue ;; esac
    br=$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null)
    dirty=$(git -C "$wt" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    if [ "$dirty" != "0" ]; then echo "KEEP  $wt  (dirty: $dirty files)"; continue; fi
    if ! git -C "$wt" merge-base --is-ancestor HEAD "$MAIN" 2>/dev/null; then
      ahead=$(git -C "$wt" rev-list --count "$MAIN"..HEAD 2>/dev/null)
      echo "KEEP  $wt  ($br, $ahead commits not in main)"; continue
    fi
    if [ "$DRY" = "1" ]; then echo "REAP  $wt  ($br merged)"; continue; fi
    git -C "$R" worktree remove --force "$wt" 2>/dev/null \
      && { git -C "$R" branch -D "$br" >/dev/null 2>&1; echo "REAPED $wt ($br)"; } \
      || echo "FAIL  $wt"
  done
done
echo "--- remaining $WS worktrees: $(ls -d $PROJ/*wt-"$WS"-* $PROJ/*wt-train-"$WS"* 2>/dev/null | wc -l | tr -d ' ') ---"
