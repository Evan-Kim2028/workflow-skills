# done-before-deps: task done before its dependencies

A task is `done` while one of its `deps` is not `done`.

## Why it matters

Progress and replay read completion order from the graph. A done task on
unfinished ground makes the map lie about what shipped.

## How to fix

Either finish the dependency first, or move the task back until it holds:

```bash
python3 agent_dash.py ws plan WS4
python3 agent_dash.py ws task WS4 WS4.3 wip
```

## How to verify

`ws validate WS4` passes and `doctor --ws WS4` is clean.
