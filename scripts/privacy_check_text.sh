#!/bin/sh
# Scan text that is not a tracked file (a PR title or body, a commit message,
# a draft) with the patterns in scripts/privacy_audit.sh. The audit reads
# tracked files only, so run this on anything you paste into GitHub.
#
# Usage: sh scripts/privacy_check_text.sh <file>...
# Exit 0 when clean, 1 on a hit (lines printed), 2 on a usage or read error.
set -u

here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
PAT=$(sed -n "s/^PAT='\(.*\)'\$/\1/p" "$here/privacy_audit.sh")
if [ -z "$PAT" ]; then
  echo "privacy_check_text: could not read PAT from privacy_audit.sh" >&2
  exit 2
fi
if [ $# -lt 1 ]; then
  echo "usage: sh scripts/privacy_check_text.sh <file>..." >&2
  exit 2
fi

status=0
for f in "$@"; do
  if [ ! -r "$f" ]; then
    echo "privacy_check_text: cannot read $f" >&2
    exit 2
  fi
  hits=$(grep -n -E "$PAT" "$f")
  if [ -n "$hits" ]; then
    echo "privacy check FAILED: $f" >&2
    echo "$hits" | head -40 >&2
    status=1
  fi
done
if [ "$status" -eq 0 ]; then
  echo "privacy check ok"
fi
exit "$status"
