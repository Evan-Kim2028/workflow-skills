# linear-sync-failing: the last Linear sync failed and no sync has succeeded for 10 minutes

A linked bundle has `last_error` set in `linear.json`, and its `synced_at` is more than 10 minutes old. The error is one of `RATELIMITED`, `AUTH`, `NOT_FOUND`, `HTTP`, `GRAPHQL`, or `NETWORK`. `ws linear status` shows which one.

## Why it matters

Local changes queue in the outbox and do not reach Linear. Linear changes do not reach the plan. A `RATELIMITED` error clears on its own. An `AUTH` error does not, and it stops every sync for the workstream until the key is fixed.

## How to fix

Read the error first:

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws linear status WS4 --json
```

Then act on the code:

- `AUTH`: the key is wrong or revoked. Create a new personal API key, export it as `LINEAR_API_KEY`, and restart the dashboard.
- `RATELIMITED`: wait. The poller backs off up to 300 seconds and retries.
- `NETWORK`, `HTTP`, `GRAPHQL`: check the connection to `api.linear.app`. Retry once the network is back.
- `NOT_FOUND`: a Linear object was deleted in Linear. Run `ws linear unlink WS4`, then `ws linear link WS4 --team ENG` to recreate it.

Run one sync to test the fix:

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws linear sync WS4
```

## How to verify

`ws linear status WS4` shows an empty `last_error` and a current `synced_at`. `doctor` no longer lists `linear-sync-failing`.
