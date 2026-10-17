# pricing-unreadable: price catalog unreadable

`catalog/prices.json` cannot be read or parsed.

## Why it matters

Without the catalog every model is unpriced and cost tiles go dark.

## How to fix

```bash
python3 agent_dash.py reprice
```

Restore `tools/agent-dash/catalog/prices.json` from version control,
then reprice.

## How to verify

`doctor` counts price rows again.
