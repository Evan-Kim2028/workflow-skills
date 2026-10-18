# dep-jump: a task started before its dependency finished

A plan task has a first-active moment (run start or status change) at or
before a dependency's done moment, or its dependency is still
unfinished. Jumps from the last `doctor.recent_hours` (default 24 h)
are `warn`; older ones are `info`.

## Why it matters

Starting past an unfinished dependency was a repeated source of
rework: the lane builds on an interface that moves, then rebuilds when
it lands.

## How to fix

Finish or unblock the dependency first, or move the jumping task back
to `todo` until its deps are done. The same section of `ws next` names
every jump:

```bash
python3 agent_dash.py ws next WS4
python3 agent_dash.py ws task WS4 WS4.2 todo
```

## How to verify

`doctor --ws WS4` no longer lists the jump, and `ws next WS4` shows no
dep-jump section.
