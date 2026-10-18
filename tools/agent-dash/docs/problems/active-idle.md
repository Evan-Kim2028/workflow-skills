# active-idle: active work with no motion

A workstream whose state is `active` still has open tasks but shows no
runs and no `history.jsonl` moves for more than
`doctor.active_idle_days` (default 3 days). Last activity is the newest
of its run stamps and history moves, falling back to the plan file
mtime. `planned` and `paused` workstreams never fire this; neither does
an active workstream with nothing left open.

## Why it matters

Open work with no motion is either stuck without anyone noticing or
finished in practice but never moved. Both rot: the plan view claims
progress is coming while no lane is alive.

## How to fix

Either start work on the next startable lane, or pause the workstream
with a note saying why it waits:

```bash
python3 agent_dash.py ws next WS4
python3 agent_dash.py ws state WS4 paused --note "waits on the freeze"
```

## How to verify

`doctor --ws WS4` no longer lists the workstream: it has a live lane
again, its plan moved, or it is paused.
