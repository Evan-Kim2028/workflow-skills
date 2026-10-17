# reconcile-mismatch: dashboard numbers disagree

Two views of the same numbers differ: workstream rows do not sum to the
overview, a tab disagrees with its row, or a stale running row lingers.

## Why it matters

One of the tiles is wrong. Agents that plan from the page plan from a
lie.

## How to fix

```bash
python3 agent_dash.py check
```

Work the mismatch list top down: close stale rows with `report finish`,
register invisible run folders, price unknown models.

## How to verify

`check` exits 0 with no MISMATCHES.
