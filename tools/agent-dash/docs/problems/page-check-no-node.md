# page-check-no-node: Node missing for check --page

`node` is not on PATH, so `check --page` always skips.

## Why it matters

Only the headless page comparison is lost; the API checks still run. It
never fails the doctor.

## How to fix

```bash
python3 agent_dash.py check --page
```

Install Node 18+, then re-run the command.

## How to verify

`check --page` drives Chrome instead of skipping.
