# reconcile-unreadable: reconcile endpoint failed

`GET /api/reconcile` did not answer.

## Why it matters

Without reconcile the doctor cannot cross-check the numbers; the run is
partial.

## How to fix

```bash
python3 agent_dash.py status
python3 agent_dash.py check
```

Recover the dashboard first, then re-run `doctor`.

## How to verify

`check` prints its table again.
