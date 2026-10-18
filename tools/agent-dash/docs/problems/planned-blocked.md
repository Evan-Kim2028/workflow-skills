# planned-blocked: planned work that is still waiting

A workstream whose state is `planned` names a `depends_on` workstream
that is not `done` or `archived`, or carries a stale gate in its plan.
The detail names what it waits on: the unmet dependency workstreams and
the stale gate signals. This is informational — waiting is the correct
state for a blocked plan, not a failure.

## Why it matters

A planned shelf with no waits-on labels looks startable when it is not.
Naming the blocker keeps the planner from launching a lane whose
dependency has not landed.

## How to fix

Check gate freshness, and activate only when the waits clear:

```bash
python3 agent_dash.py ws gates WS4
python3 agent_dash.py ws state WS4 active
```

`ws state active` guards on `depends_on` being all done or archived
unless `--force` carries a note.

## How to verify

`doctor --ws WS4` shows `planned-ready` instead: every dependency done
or archived and no stale gate.
