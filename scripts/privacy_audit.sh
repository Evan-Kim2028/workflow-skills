#!/bin/sh
# Fail when tracked files contain personal paths, private project names, or secrets. Run before every push.
cd "$(git rev-parse --show-toplevel)" || exit 2
PAT='/Users/[A-Za-z]|/private/tmp/claude|claude-501|ekcopersonal|evandekim|traded|silphco|lake-of-rage|lor-main|pokemon|TU-[0-9]|sk-ant-|ghp_|github_pat_|AKIA[0-9A-Z]{12}|BEGIN [A-Z ]*PRIVATE KEY'
hits=$(git grep -n -I -E "$PAT" -- . ':!scripts/privacy_audit.sh' ':!*.local.json' 2>/dev/null)
if [ -n "$hits" ]; then
  echo "privacy audit FAILED:" >&2
  echo "$hits" | head -40 >&2
  exit 1
fi
echo "privacy audit ok"
