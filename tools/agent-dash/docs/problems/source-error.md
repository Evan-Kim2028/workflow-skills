# source-error: collector source poll failing

A source's last poll raised an error, recorded on `/api/health`.

## Why it matters

The dashboard is blind on that source until the poll succeeds again.

## How to fix

```bash
python3 agent_dash.py status
```

Read the `last_error`, fix the underlying cause (permissions, a moved
directory, a dead socket), then wait one poll interval.

## How to verify

The source shows `last_error: null` and a fresh `last_poll`.
