# dep-cycle: dependency cycle in the plan

Task `deps` form a loop: A waits on B waits on A.

## Why it matters

No wave order satisfies a cycle, so "ready when every dep is done" never
fires. The workstream looks busy and never ships.

## How to fix

```bash
python3 agent_dash.py ws validate WS4
python3 agent_dash.py ws plan WS4
```

Break the loop by removing the dep that states the weaker ordering.

## How to verify

`doctor --ws WS4` no longer lists the cycle.
