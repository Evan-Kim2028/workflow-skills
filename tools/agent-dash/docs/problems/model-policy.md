# model-policy: worker breaks the model policy

A live worker's argv model flags violate the configured policy: `muse`
workers require `--reasoning-effort xhigh` (or the configured level);
`cursor-agent` workers running a `grok-4.7` model must use the bracketed
500k form, never a bare `grok-4.7-high` or a `-fast` id. Only the label is
reported, never the rest of argv.

## Why it matters

Wrong flags burn money or silently downgrade reasoning. Policy keeps
every lane on the model the lead budgeted for.

## How to fix

Fix the registry entry, then relaunch the worker:

```bash
python3 agent_dash.py check
```

Edit `workers.local.json`: give the muse worker `--reasoning-effort
xhigh` in `extra_args`, or set the cursor-agent model to
`grok-4.7[context=500k,reasoning_effort=xhigh,fast=false]`.

## How to verify

Relaunch one lane; `doctor` shows no policy problem for its label.
