# unseen-worker: live worker the dashboard cannot see

A live worker process carries a label but has no running worker row, so no
tile, chip, or count includes it.

## Why it matters

The dashboard is blind to the lane: live-agent counts, workstream rows,
and reconcile all miss it. Numbers on the page are wrong.

## How to fix

```bash
python3 agent_dash.py check
```

The usual cause is a run folder the collectors do not scan: register it
(`report --run-dir <dir>`) or add it to `roots.scratchpads`. A worker
younger than two poll intervals is still ramping up and is not flagged.

## How to verify

The label appears in Running now and `check` exits 0.
