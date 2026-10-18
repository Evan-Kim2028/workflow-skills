# lead-undelegated: a live lead doing worker jobs

The workstream has a live run right now, and its lead profile
(`lead-profile --ws`) flags `LOW-DELEGATION` (fewer than 2 workers
launched, or a busy lead with fewer than 3) or `CONTEXT>250k` (lead
context past 250,000 tokens).

## Why it matters

Every profiled lead session passed the context limit doing reads and
runs directly. A lead doing worker jobs stalls its lanes and burns the
context its briefs need.

## How to fix

Hand every multi-step job to a worker, and past the context limit
re-brief or start a successor instead of continuing:

```bash
python3 agent_dash.py lead-profile --ws WS4
```

## How to verify

`doctor --ws WS4` no longer lists the lead: it launched workers, or a
fresh lead took over.
