# planned-ready: planned work clear to start

A workstream whose state is `planned` has every `depends_on`
workstream `done` or `archived` (or names none) and no stale gate in
its plan. This is informational — the "can we start it" checklist is
fully ticked.

## Why it matters

Ready plans stall quietly on the shelf while lanes sit empty. One line
in the doctor output is the nudge to activate.

## How to fix

Activate the workstream so it gains a tab and joins the Current view:

```bash
python3 agent_dash.py ws state WS4 active
```

## How to verify

`ws list --state active` names the workstream, and `doctor --ws WS4`
no longer lists it.
