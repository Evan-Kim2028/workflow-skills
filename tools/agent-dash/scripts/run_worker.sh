#!/bin/bash
# Exec run_worker.py so existing callers keep working.
# usage: run_worker.sh <tier|default|auto> <label> <workspace> <prompt-file> <run-dir> [max-wait]
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 "$HERE/run_worker.py" "$@"
