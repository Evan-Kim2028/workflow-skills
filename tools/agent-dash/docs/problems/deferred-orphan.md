# deferred-orphan: a deferred task nobody picked up

A task was deferred with a `moved_to` workstream (or none), and no
receiving task matches it there — neither by the explicit `received_as`
id nor by title similarity. Only orphans older than
`doctor.recent_hours` (default 24 h) fire, so a handoff still in flight
stays quiet.

## Why it matters

Deferred tasks leave the plan counts, so an orphan is invisible work:
the sending workstream counts it gone and no receiving workstream counts
it at all.

## How to fix

Either land the handoff (create or name the receiving task) or reclaim
the work:

```bash
python3 agent_dash.py ws deferred WS4
python3 agent_dash.py ws task WS4 WS4.3 todo
```

## How to verify

`doctor --ws WS4` no longer lists the orphan, and `ws deferred WS4`
shows a receiving task for it.
