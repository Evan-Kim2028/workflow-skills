#!/bin/sh
# gate.sh - fast scoped local gate for agent-dash.
#
# Maps changed files to test files via scripts/gate_map.json, runs each
# selected test as `python3 -m unittest discover -s tests -p "<file>.py"`
# (never a bare suite), node --check on changed JS plus the extracted
# page script, and the repo privacy audit. Prints a one-line verdict per
# file and a final PASS/FAIL.
#
# Usage: sh scripts/gate.sh [base] [--list]
#   base defaults to origin/main. --list prints the selection without
#   running anything. Run from anywhere; the script cds to tools/agent-dash.
#
# A changed tests/test_<x>.py selects test_<x> itself. Files matching no
# table entry select the default tests with a warning. test_privacy_pin
# and test_page_rules always run.

set -u

TOOLS_DIR=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
MAP="$TOOLS_DIR/scripts/gate_map.json"
AUDIT="$TOOLS_DIR/../../scripts/privacy_audit.sh"
BASE="origin/main"
LIST_ONLY=0

for arg in "$@"; do
    case "$arg" in
        --list) LIST_ONLY=1 ;;
        -h|--help)
            echo "usage: sh scripts/gate.sh [base] [--list]"
            exit 0
            ;;
        --*) echo "gate: unknown flag '$arg'" >&2; exit 2 ;;
        *) BASE="$arg" ;;
    esac
done

cd "$TOOLS_DIR" || exit 2

if ! COMMITTED=$(git diff --name-only "$BASE" -- 2>/dev/null); then
    echo "gate: unknown base ref '$BASE'" >&2
    exit 2
fi
UNTRACKED=$(git ls-files --others --exclude-standard 2>/dev/null)
CHANGED=$(printf '%s\n%s\n' "$COMMITTED" "$UNTRACKED" | sed '/^[[:space:]]*$/d' | sort -u)

WARN_FILE=$(mktemp) || exit 2
TEST_FILE=$(mktemp) || exit 2
trap 'rm -f "$WARN_FILE" "$TEST_FILE"' EXIT INT TERM

printf '%s\n' "$CHANGED" | python3 -c '
import fnmatch, json, os, sys

tools = os.getcwd()
with open("scripts/gate_map.json", encoding="utf-8") as fh:
    table = json.load(fh)
patterns = table.get("map", {})
always = table.get("always", [])
default = table.get("default", ["test_page_rules", "test_server"])

selected = set(always)
warned = []
for path in (line.strip() for line in sys.stdin):
    if not path:
        continue
    prefix = "tools/agent-dash/"
    rel = path[len(prefix):] if path.startswith(prefix) else path
    hits = set()
    for pattern, tests in patterns.items():
        if fnmatch.fnmatchcase(rel, pattern):
            hits.update(tests)
    base = os.path.basename(rel)
    if rel.startswith("tests/") and base.startswith("test_") and base.endswith(".py"):
        name = base[:-3]
        if os.path.isfile(os.path.join(tools, "tests", base)):
            hits.add(name)
    if not hits:
        hits.update(default)
        warned.append(rel)
    selected.update(hits)
for name in sorted(selected):
    print("TEST " + name)
for rel in warned:
    print("WARN unmapped: " + rel, file=sys.stderr)
' >"$TEST_FILE" 2>"$WARN_FILE"

TESTS=$(grep '^TEST ' "$TEST_FILE" | cut -d' ' -f2 | sort -u)
cat "$WARN_FILE" >&2

if [ "$LIST_ONLY" -eq 1 ]; then
    for t in $TESTS; do
        echo "$t.py"
    done
    exit 0
fi

FAIL=0
for t in $TESTS; do
    if [ ! -f "tests/$t.py" ]; then
        echo "SKIP $t.py (no such test file; map entry stale)"
        continue
    fi
    OUT=$(mktemp) || exit 2
    if python3 -m unittest discover -s tests -p "$t.py" >"$OUT" 2>&1; then
        echo "PASS $t.py"
    else
        echo "FAIL $t.py"
        tail -20 "$OUT"
        FAIL=1
    fi
    rm -f "$OUT"
done

if command -v node >/dev/null 2>&1; then
    JS_CHANGED=$(printf '%s\n' "$CHANGED" | grep '^tools/agent-dash/.*\.js$' | sed 's|^tools/agent-dash/||')
    for js in $JS_CHANGED; do
        if [ -f "$js" ]; then
            if node --check "$js" >/dev/null 2>&1; then
                echo "PASS node $js"
            else
                echo "FAIL node $js"
                FAIL=1
            fi
        fi
    done
    case " $CHANGED " in
        *"tools/agent-dash/static/index.html"*)
            PAGE_TMP="$(mktemp -d)/page.js" || exit 2
            python3 -c '
import re, sys
text = open("static/index.html", encoding="utf-8").read()
scripts = [m for m in re.findall(r"<script>(.*?)</script>", text, re.S) if m.strip()]
sys.stdout.write("\n".join(scripts))
' >"$PAGE_TMP"
            if node --check "$PAGE_TMP" >/dev/null 2>&1; then
                echo "PASS node <page script>"
            else
                echo "FAIL node <page script>"
                FAIL=1
            fi
            rm -rf "$(dirname "$PAGE_TMP")"
            ;;
    esac
else
    echo "SKIP node checks (node not found)"
fi

if [ -f "$AUDIT" ]; then
    if sh "$AUDIT" >/dev/null 2>&1; then
        echo "PASS privacy_audit"
    else
        echo "FAIL privacy_audit"
        sh "$AUDIT" 2>&1 | tail -10
        FAIL=1
    fi
else
    echo "SKIP privacy_audit (no script at ../../scripts/privacy_audit.sh)"
fi

if [ "$FAIL" -eq 0 ]; then
    echo "GATE PASS"
    exit 0
else
    echo "GATE FAIL"
    exit 1
fi
