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
Right after a (re)start nothing is flagged until the dashboard process
is older than max(3x the slowest source interval, 90 s) and every
source has finished its first poll: a worker that only becomes visible
once a source's first scan lands is not a problem while the registry
still reports that source as starting. `check` shares the same rule
through the same function.

## How to verify

The label appears in Running now and `check` exits 0.
