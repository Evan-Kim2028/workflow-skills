# long-tail: one lane still runs while its wave finished

In a dependency wave, every lane but one finished more than
`doctor.long_tail_minutes` (default 60 min) ago, and the remaining lane
is still running (status `wip` or `pr` with a live run mapped to it).
Deferred tasks are left out of the wave before comparing.

## Why it matters

One slow lane held whole waves for hours while every other lane sat
done. The wave cannot close, and dependent work cannot start.

## How to fix

Split the slow lane into smaller tasks or reassign it to a fresh
worker, then move its status to match reality:

```bash
python3 agent_dash.py ws waves WS4
python3 agent_dash.py ws task WS4 WS4.5 wip
```

## How to verify

`doctor --ws WS4` no longer lists the lane: either it finished, or the
rest of its wave is recent again.
