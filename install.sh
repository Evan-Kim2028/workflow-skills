#!/bin/sh
# Link this repo's skills, tools, worker template and worker scripts into ~/.claude.
#
#   sh install.sh             link everything (idempotent)
#   sh install.sh --dry-run   print what would change; touch nothing
#
# Global instructions name ~/.claude/templates/worker-common.md and
# ~/.claude/scripts/<name>, so those paths are kept as symlinks into this repo.
# A real file or directory already at a link path is moved (never deleted or
# overwritten) to ~/.local/share/agent-dash/backups/<run-epoch>/<path under ~/.claude>.
# Hooks are not installed here; see docs/install.md.
set -eu

DRY=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    -h|--help)
      sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
done

ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
CLAUDE="${HOME}/.claude"
BACKUP_ROOT="${HOME}/.local/share/agent-dash/backups"
STAMP=$(date +%s)
AGENT_DASH="$ROOT/tools/agent-dash"

say() {
  if [ "$DRY" = 1 ]; then echo "dry-run: $*"; else echo "$*"; fi
}

# back_up <path>: move a real file or directory out of the way, keeping its layout.
back_up() {
  rel=${1#"$CLAUDE"/}
  bak="$BACKUP_ROOT/$STAMP/$rel"
  if [ "$DRY" = 1 ]; then
    echo "dry-run: would move $1 -> $bak"
    return 0
  fi
  mkdir -p "$(dirname "$bak")"
  mv "$1" "$bak"
  echo "backed up $1 -> $bak"
}

# link_one <source> <dest>
link_one() {
  src=$1
  dest=$2
  if [ -L "$dest" ]; then
    if [ "$(readlink "$dest")" = "$src" ]; then
      echo "ok  $dest -> $src"
      return 0
    fi
    say "replace symlink $dest (was $(readlink "$dest"))"
    [ "$DRY" = 1 ] || rm "$dest"
  elif [ -e "$dest" ]; then
    back_up "$dest"
  fi
  if [ "$DRY" = 1 ]; then
    echo "dry-run: would link $dest -> $src"
    return 0
  fi
  mkdir -p "$(dirname "$dest")"
  ln -s "$src" "$dest"
  echo "linked $dest -> $src"
}

for dir in "$ROOT"/skills/*/; do
  [ -f "${dir}SKILL.md" ] || continue
  name=$(basename "$dir")
  link_one "$ROOT/skills/$name" "$CLAUDE/skills/$name"
done
for dir in "$ROOT"/tools/*/; do
  [ -d "$dir" ] || continue
  name=$(basename "$dir")
  link_one "$ROOT/tools/$name" "$CLAUDE/tools/$name"
done
link_one "$AGENT_DASH/templates/worker-common.md" "$CLAUDE/templates/worker-common.md"
for name in worker-watch.sh muse-usage.py hub-usage.sh memcap.sh; do
  link_one "$AGENT_DASH/scripts/$name" "$CLAUDE/scripts/$name"
done
