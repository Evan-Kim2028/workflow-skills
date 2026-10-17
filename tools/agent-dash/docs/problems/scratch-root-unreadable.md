# scratch-root-unreadable: scratch root not readable

A configured scratch root exists but is not readable by the dashboard
user.

## Why it matters

Same as a missing root: every run folder under it is invisible.

## How to fix

```bash
ls -ld <root>
```

Fix ownership or permissions, then `python3 agent_dash.py restart`.

## How to verify

`doctor` lists the root as readable.
