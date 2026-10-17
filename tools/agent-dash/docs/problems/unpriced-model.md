# unpriced-model: usage with no price

A model has usage rows in the last 24 h but no input/output rate in
`catalog/prices.json`. Cost counts unpriced runs separately and never
shows `$0` for them.

## Why it matters

The cost tile understates spend while models go unpriced. Money the page
cannot see is money nobody budgets.

## How to fix

```bash
python3 agent_dash.py reprice
```

If the model is genuinely new, add a row to
`tools/agent-dash/catalog/prices.json` first (exact model string, USD per
million tokens), then reprice. `doctor --fix --yes` runs the reprice and
the run-model backfill for you.

## How to verify

`/api/health` lists no `unpriced_models` and `doctor` is clean.
