# over-cap: worker tier above its cap

More live workers match a tier's pgrep pattern than its registry `cap`
allows. One global problem (`over-cap:global`) for all tiers over cap:
the title carries the count, the detail lists the worst 3, and `items`
carries up to 20 member rows (`doctor --all` for all).

## Why it matters

Caps protectitzung the laptop and quota. Over cap, launches that should
queue instead pile on, and quota burn spikes.

## How to fix

Wait for lanes to finish, or raise the cap deliberately:

```bash
python3 agent_dash.py doctor --all
```

Edit `workers.local.json` only when the machine can take it.

## How to verify

Live counts drop back under the cap on the next doctor run.
