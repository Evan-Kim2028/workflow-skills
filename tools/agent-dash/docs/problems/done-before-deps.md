# done-before-deps: task done before its dependencies

A task is `done` while one of its `deps` is not `done`. One problem per
workstream (`done-before-deps:ws:<ID>`): the title names the workstream
and the count, the detail lists the worst 3, and `items` carries up to
20 member rows (`doctor --ws <ID> --all` for all).

## Why it matters

Progress and replay read completion order from the graph. A done task on
unfinished ground makes the map lie about what shipped.

## How to fix

Either finish the dependency first, or move the task back until it holds:

```bash
python3 agent_dash.py doctor --ws WS4 --all
python3 agent_dash.py ws task WS4 WS4.3 wip
```

## How to verify

`ws validate WS4` passes and `doctor --ws WS4` is clean.
