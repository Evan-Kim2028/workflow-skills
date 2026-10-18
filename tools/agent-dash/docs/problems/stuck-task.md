# stuck-task: task stuck in progress with no live agent

A plan task has status `wip` or `pr`, but no live agent maps to it and no
run has touched it for more than `doctor.stuck_hours` (default 6 h).
One problem per workstream (`stuck-task:ws:<ID>`): the title names the
workstream and the count, the detail lists the worst 3, and `items`
carries up to 20 member rows (`doctor --ws <ID> --all` for all).

## Why it matters

The progress map says work is happening, but nothing is. Waves stall
behind it and the lead's picture of the workstream is wrong.

## How to fix

Find the lane and either re-run it or move the task to its true status:

```bash
python3 agent_dash.py doctor --ws WS4 --all
python3 agent_dash.py ws task WS4 WS4.1 wip
```

## How to verify

`doctor --ws WS4` no longer lists the task.
