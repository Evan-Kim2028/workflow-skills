# train-stuck: train sitting in one status

A merge train has a non-terminal status (`planned`, `assembling`, `gate`,
`pr`) and the bundle plan has not changed for more than
`doctor.train_hours` (default 24 h).

## Why it matters

Trains only pay off when they move. A stuck train holds approved branches
off main while main drifts underneath them.

## How to fix

Move the train to its true status, or dissolve it:

```bash
python3 agent_dash.py ws trains WS4
python3 agent_dash.py ws train WS4 T1 --status gate
```

## How to verify

`ws trains WS4` shows movement and `doctor --ws WS4` is clean.
