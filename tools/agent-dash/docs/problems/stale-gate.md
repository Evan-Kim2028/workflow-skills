# stale-gate: an unmet gate past its freshness bound

A gate task carries `fresh_hours`, and its `confirmed_at` is missing or
older than that many hours, while the gate itself is not `done`. Done
gates never fire: their signal already landed.

## Why it matters

A gate checked once decays silently. Dependents treat the old check as
green and build on a signal that may have flipped.

## How to fix

Re-check the outside signal, then set the gate's `confirmed_at` to the
new check time (ISO-8601 UTC) in `plan.json` — or move the gate to
`done` when the signal landed:

```bash
python3 agent_dash.py ws gates WS4
python3 agent_dash.py ws task WS4 WS4.2 done
```

## How to verify

`doctor --ws WS4` no longer lists the gate, and `ws gates WS4` shows it
fresh.
