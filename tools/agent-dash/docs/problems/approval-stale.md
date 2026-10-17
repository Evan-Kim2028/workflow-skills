# approval-stale: pending approval past its window

A `pending` item in `approvals.json` is older than `doctor.approval_days`
(default 3 days). Age is read from the approvals file, which the lead
rewrites on every approval step.

## Why it matters

Production steps wait on an owner who may have missed the ask. Stale
pendings rot into shadow decisions.

## How to fix

Ask the owner, then record the answer:

```bash
python3 agent_dash.py ws approvals WS4
python3 agent_dash.py ws approve WS4 notes-cutover approved --note "owner said go in the session"
```

## How to verify

`ws approvals WS4` shows no pending item older than the window.
