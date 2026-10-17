# dashboard-unhealthy: dashboard reports ok=false

The dashboard answers but reports unhealthy for no single named reason.

## Why it matters

Something the specific checks did not isolate is wrong. Treat it as a
pointer, not a diagnosis.

## How to fix

```bash
python3 agent_dash.py status
python3 agent_dash.py check
```

Work any named problem first; this one clears with it.

## How to verify

`/api/health` returns `ok: true`.
