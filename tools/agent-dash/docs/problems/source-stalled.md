# source-stalled: collector source stopped polling

A source has no successful poll for more than three of its intervals
(`lag_s` keeps growing on `/api/health`). A source with a poll still
running reads `slow`, not stalled: restart only a source that stays
`stalled` with no poll in flight.

## Why it matters

The dashboard is blind on that source: its runs freeze on the page while
the agents keep going.

## How to fix

```bash
python3 agent_dash.py status
python3 agent_dash.py restart
```

`status` shows the failing source and its last error; `restart` recovers
a wedged collector. `doctor --fix --yes` restarts for you.

## How to verify

`/api/health` shows a fresh `last_poll` and `ok: true`.
