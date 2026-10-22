#!/usr/bin/env python3
"""Muse plugin entry for the agent-dash label guard.

Execs the real hook (``scripts/label_guard_hook.py``) with stdin, stdout,
stderr and the exit code preserved, so the validator stays live in the
checkout: only this two-step entry is frozen at install time. Resolves the
hook via ``LABEL_GUARD_HOOK``, then the standard
``~/.claude/tools/agent-dash`` symlink layout, then relative to this file
(a linked install inside the checkout). Fails open when no hook is found.
"""

import os
import sys


def candidates():
    env = os.environ.get("LABEL_GUARD_HOOK")
    if env:
        yield env
    yield os.path.expanduser("~/.claude/tools/agent-dash/scripts/label_guard_hook.py")
    here = os.path.dirname(os.path.abspath(__file__))
    yield os.path.normpath(
        os.path.join(here, "..", "..", "..", "scripts", "label_guard_hook.py")
    )


for path in candidates():
    if path and os.path.isfile(path):
        os.execv(sys.executable, [sys.executable, path])

sys.exit(0)
