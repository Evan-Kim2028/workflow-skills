# idle-ready: ready tasks with nobody to run them

A workstream has ready tasks (`ready_queue` non-empty, gates excluded)
while fewer than 2 live agents map to it, and the plan has been
unchanged for more than `doctor.idle_ready_minutes` (default 30 min).

## Why it matters

Idle ready tasks were the largest measured cost: ready work sits while
lanes go quiet, and the clock runs with nothing in flight. Two agents is
the minimum that keeps one lane moving while the next starts.

## How to fix

List the startable lanes ranked by what they unblock, then launch the
top ones up to the worker caps in one wave:

```bash
python3 agent_dash.py ws next WS4 --limit 3
```

## How to verify

`doctor --ws WS4` no longer lists the workstream, or shows 2 or more
live agents mapped to its tasks.
