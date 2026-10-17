# orphan-worker: labelled worker still alive with no live run

A labelled worker process is still alive after 10 minutes although its
parent is pid 1 (reparented) or its run row already ended. The doctor
never signals it; this only reports.

## Why it matters

Nobody watches the run anymore: the wrapper is gone or the row closed.
Its output may never be collected and its verdict never recorded.

## How to fix

Check what it is doing, then close the row so the books balance:

```bash
python3 agent_dash.py check
python3 agent_dash.py report finish --label <label> --verdict PASS --status done
```

Use the verdict the report actually earns. Never kill the process from
the doctor; close the row and let it exit on its own.

## How to verify

The label leaves the Running now list and `doctor` resolves the problem
on its next run.
