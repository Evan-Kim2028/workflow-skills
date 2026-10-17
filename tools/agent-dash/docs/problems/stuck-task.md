# stuck-task: task stuck in progress with no live agent

A plan task has status `wip` or `pr`, but no live agent maps to it and no
run has touched it for more than `doctor.stuck_hours` (default 6 h).

## Why it matters

The progress map says work is happening, but nothing is. Waves stall
behind it and the lead's picture of the workstream is wrong.

## How to fix

Find the lane and either re-run it or move the task to its true status:

```bash
python3 agent_dash.py ws plan WS4
python3 agent_dash.py ws task WS4 WS4.1 wip
```

## How to verify

`doctor --ws WS4` no longer lists the task.
