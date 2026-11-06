#!/bin/sh
# gate.sh - fast scoped local gate for agent-dash.
#
# Maps changed files to test files via scripts/gate_map.json, runs each
# selected test as `python3 -m unittest discover -s tests -p "<file>.py"`
# (never a bare suite), node --check on changed JS plus the extracted
# page script, and the repo privacy audit. Prints a one-line verdict per
# file and a final PASS/FAIL.
#
# Usage: sh scripts/gate.sh [base] [--list] [--force]
#   base defaults to origin/main. --list prints the selection without
#   running anything. Run from anywhere; the script cds to tools/agent-dash.
#
# Wide selection: when the selection has more than 3 suites or more than
# 2000 test functions (GATE_MAX_SUITES / GATE_MAX_TESTS override), the gate
# prints the changed files that widened it, most test functions first, and
# stops before running anything (exit 3). Run those files' tests directly,
# or pass --force to run the whole selection anyway.
#
# A changed tests/test_<x>.py selects test_<x> itself; a nested
# tests/<dir>/test_<x>.py selects <dir>/test_<x>, which runs as the
# tests.<dir>.test_<x> module with ADASH_PERF=1 (perf-gated suites skip
# without it). Files matching no table entry select the default tests
# with a warning. test_privacy_pin and test_page_rules always run.

set -u

TOOLS_DIR=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
MAP="$TOOLS_DIR/scripts/gate_map.json"
AUDIT="$TOOLS_DIR/../../scripts/privacy_audit.sh"
BASE="origin/main"
LIST_ONLY=0
FORCE=0

for arg in "$@"; do
    case "$arg" in
        --list) LIST_ONLY=1 ;;
        --force) FORCE=1 ;;
        -h|--help)
            echo "usage: sh scripts/gate.sh [base] [--list] [--force]"
            exit 0
            ;;
        --*) echo "gate: unknown flag '$arg'" >&2; exit 2 ;;
        *) BASE="$arg" ;;
    esac
done

cd "$TOOLS_DIR" || exit 2

# Diff against the merge-base, not the base tip: a base that moved on must not
# put its own commits into this branch's selection.
MERGE_BASE=$(git merge-base "$BASE" HEAD 2>/dev/null) || MERGE_BASE="$BASE"
if ! COMMITTED=$(git diff --name-only "$MERGE_BASE" -- 2>/dev/null); then
    echo "gate: unknown base ref '$BASE'" >&2
    exit 2
fi
# Untracked *.bak* files are local backups, never a change to test.
UNTRACKED=$(git ls-files --others --exclude-standard 2>/dev/null | awk '!/\.bak/')
CHANGED=$(printf '%s\n%s\n' "$COMMITTED" "$UNTRACKED" | sed '/^[[:space:]]*$/d' | sort -u)

WARN_FILE=$(mktemp) || exit 2
TEST_FILE=$(mktemp) || exit 2
trap 'rm -f "$WARN_FILE" "$TEST_FILE"' EXIT INT TERM

printf '%s\n' "$CHANGED" | python3 -c '
import json, os, re, sys

def glob_match(pattern, path):
    """`*` and `?` stay inside one path segment; `**` crosses directories.

    fnmatch let `*` match `/`, so `dash/store*.py` selected store files and
    `static/*` selected every static subdirectory by accident.
    """
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        c = pattern[i]
        out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
        i += 1
    return re.match("".join(out) + "$", path) is not None

tools = os.getcwd()
with open("scripts/gate_map.json", encoding="utf-8") as fh:
    table = json.load(fh)
patterns = table.get("map", {})
always = table.get("always", [])
default = table.get("default", ["test_page_rules", "test_server"])

selected = set(always)
warned = []
per_file = []
for path in (line.strip() for line in sys.stdin):
    if not path:
        continue
    prefix = "tools/agent-dash/"
    rel = path[len(prefix):] if path.startswith(prefix) else path
    hits = set()
    for pattern, tests in patterns.items():
        if glob_match(pattern, rel):
            hits.update(tests)
    base = os.path.basename(rel)
    if rel.startswith("tests/") and base.startswith("test_") and base.endswith(".py"):
        name = rel[len("tests/"):-len(".py")]
        if os.path.isfile(os.path.join(tools, rel)):
            hits.add(name)
    if not hits:
        hits.update(default)
        warned.append(rel)
    selected.update(hits)
    per_file.append((rel, sorted(hits - set(always))))

def count_tests(name):
    try:
        with open(os.path.join(tools, "tests", name + ".py"), encoding="utf-8", errors="replace") as fh:
            return len(re.findall(r"^\s*def test", fh.read(), re.M))
    except OSError:
        return 0

max_suites = int(os.environ.get("GATE_MAX_SUITES", "3"))
max_tests = int(os.environ.get("GATE_MAX_TESTS", "2000"))
counts = {name: count_tests(name) for name in selected}
n_tests = sum(counts.values())
wide = len(selected) > max_suites or n_tests > max_tests
for name in sorted(selected):
    print("TEST " + name)
print("TOTAL %d %d" % (len(selected), n_tests))
print("WIDE %d" % (1 if wide else 0))
for rel in warned:
    print("WARN unmapped: " + rel, file=sys.stderr)
if wide:
    print("gate: WIDE selection: %d suites, %d test functions (limits %d suites, %d test functions)" % (len(selected), n_tests, max_suites, max_tests), file=sys.stderr)
    print("gate: changed files that widened it, most test functions first:", file=sys.stderr)
    ranked = sorted(
        ((sum(counts.get(n, 0) for n in names), rel, names) for rel, names in per_file if names),
        reverse=True,
    )
    for total, rel, names in ranked[:8]:
        print("gate:   %s -> %d suites, %d test functions (%s)" % (rel, len(names), total, ", ".join(names)), file=sys.stderr)
    if ranked:
        hints = ["python3 -m unittest tests.%s" % n.replace("/", ".") if "/" in n else "python3 -m unittest discover -s tests -p %s.py" % n for n in ranked[0][2]]
        print("gate: run the tests of the top-ranked file directly: " + " && ".join(hints), file=sys.stderr)
' >"$TEST_FILE" 2>"$WARN_FILE"

TESTS=$(grep '^TEST ' "$TEST_FILE" | cut -d' ' -f2 | sort -u)
cat "$WARN_FILE" >&2

if [ "$LIST_ONLY" -eq 1 ]; then
    for t in $TESTS; do
        echo "$t.py"
    done
    exit 0
fi

if [ "$(awk '$1=="WIDE"{print $2}' "$TEST_FILE")" = "1" ]; then
    if [ "$FORCE" -eq 0 ]; then
        echo "GATE STOPPED: wide selection, nothing run. Run the tests of the top-ranked file above, or pass --force."
        exit 3
    fi
    echo "gate: wide selection, running all of it (--force)"
fi

FAIL=0
for t in $TESTS; do
    case "$t" in
        */*)
            MOD="tests.$(printf '%s' "$t" | tr '/' '.')"
            if [ ! -f "tests/$t.py" ]; then
                echo "SKIP $t.py (no such test file; map entry stale)"
                continue
            fi
            OUT=$(mktemp) || exit 2
            if ADASH_PERF=1 python3 -m unittest "$MOD" >"$OUT" 2>&1; then
                echo "PASS $t.py"
            else
                echo "FAIL $t.py"
                tail -20 "$OUT"
                FAIL=1
            fi
            rm -f "$OUT"
            continue
            ;;
    esac
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
