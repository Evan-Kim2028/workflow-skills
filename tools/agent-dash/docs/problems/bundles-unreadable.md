# bundles-unreadable: bundle list failed

The workstream bundle directory cannot be listed or read.

## Why it matters

Without bundles there are no workstream tabs, plans, or hygiene checks.

## How to fix

```bash
python3 agent_dash.py ws list
```

Check `AGENT_DASH_WORKSTREAMS` points at the bundle root and the
directory is readable.

## How to verify

`ws list` names the bundles.
