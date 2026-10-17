# over-cap: worker tier above its cap

More live workers match a tier's pgrep pattern than its registry `cap`
allows.

## Why it matters

Caps protectitzung the laptop and quota. Over cap, launches that should
queue instead pile on, and quota burn spikes.

## How to fix

Wait for lanes to finish, or raise the cap deliberately:

```bash
python3 agent_dash.py check
```

Edit `workers.local.json` only when the machine can take it.

## How to verify

Live counts drop back under the cap on the next doctor run.
