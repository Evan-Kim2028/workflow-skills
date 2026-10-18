# review-spiral: a task past the fix-round cap

An open task (`todo`, `wip`, or `pr`) has more than
`doctor.fix_rounds_cap` (default 2) distinct fix/review rounds, counted
from `-fixN` / `-revN` run-label suffixes. Finished tasks never fire:
their spiral is over.

## Why it matters

Review loops were the bulk of runs where measured. Past the second
round the lane is usually stuck on unclear acceptance, not on effort.

## How to fix

The third round goes to the lead: take the lane over, sharpen its
acceptance, or re-brief it as smaller tasks:

```bash
python3 agent_dash.py ws plan WS4
```

## How to verify

`doctor --ws WS4` no longer lists the task: it moved forward, or its
rounds were folded into a re-briefed plan.
