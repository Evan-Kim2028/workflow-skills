#!/bin/sh
# Link every skill and tool in this repo into ~/.claude. Idempotent. A real directory is moved to
# ~/.local/share/agent-dash/backups/install/<skills|tools>/, not deleted.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
CLAUDE="${HOME}/.claude"
BACKUP_ROOT="${HOME}/.local/share/agent-dash/backups/install"
mkdir -p "$CLAUDE/skills" "$CLAUDE/tools"

link_one() {
  src=$1
  dest=$2
  if [ -L "$dest" ]; then
    current=$(readlink "$dest")
    if [ "$current" = "$src" ]; then
      echo "ok  $dest -> $src"
      return
    fi
    rm "$dest"
    echo "replaced symlink $dest"
  elif [ -e "$dest" ]; then
    # Backups go outside ~/.claude so they never sit beside live skills or tools.
    bak_dir="${BACKUP_ROOT}/$(basename "$(dirname "$dest")")"
    mkdir -p "$bak_dir"
    bak="${bak_dir}/$(basename "$dest").bak-$(date +%s)"
    if [ -e "$bak" ]; then
      bak="${bak}-$$"
    fi
    mv "$dest" "$bak"
    echo "backed up $dest -> $bak"
  fi
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
