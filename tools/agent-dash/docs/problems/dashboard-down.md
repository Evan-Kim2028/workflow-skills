# dashboard-down: dashboard not reachable

Nothing answers at the dashboard URL.

## Why it matters

Every agent command that reads the dashboard (`state`, `check`,
`doctor` against the server, worker preflights) fails without it.

## How to fix

```bash
python3 agent_dash.py run
python3 agent_dash.py status
```

## How to verify

`curl -s http://127.0.0.1:8765/api/health` answers `ok`.
