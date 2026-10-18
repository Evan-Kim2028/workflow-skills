#!/bin/sh
# install_hooks.sh - opt-in pre-push hook for the agent-dash scoped gate.
#
# Nothing installs this automatically; run it once per checkout that
# should gate pushes:
#   sh tools/agent-dash/scripts/install_hooks.sh
#
# Idempotent: re-running refreshes our own hook. Refuses to overwrite a
# foreign hook (one without the marker below); remove or merge that hook
# by hand first.

set -u

MARKER="# agent-dash-gate (opt-in via tools/agent-dash/scripts/install_hooks.sh)"

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo "install_hooks: not inside a git checkout" >&2
    exit 2
}
HOOK="$ROOT/.git/hooks/pre-push"

if [ -f "$HOOK" ] && ! grep -q "agent-dash-gate" "$HOOK" 2>/dev/null; then
    echo "install_hooks: refusing to overwrite foreign hook at $HOOK" >&2
    echo "install_hooks: remove it or merge the gate call by hand, then re-run" >&2
    exit 1
fi

cat >"$HOOK" <<EOF
#!/bin/sh
$MARKER
# Push gate: scoped agent-dash tests for what changed. Bypass with --no-verify.
sh tools/agent-dash/scripts/gate.sh
EOF
chmod +x "$HOOK"
echo "install_hooks: pre-push hook installed at $HOOK"
