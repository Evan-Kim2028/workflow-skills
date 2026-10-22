# linear-outbox-backlog: local changes are waiting to reach Linear

The outbox file `linear-outbox.jsonl` in a linked bundle holds more than 50 rows, or its oldest row is more than 10 minutes old. Each row is one local write that Linear has not received yet.

## Why it matters

Linear shows stale status. Anyone who reads Linear, or a pull request that moves issues through `Fixes` references, sees the old state. The rows are safe on disk, and the next successful sync sends them.

## How to fix

Check whether a sync is failing. A failing sync is the usual cause:

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws linear status WS4
```

If `last_error` is set, fix it first. See [linear-sync-failing](linear-sync-failing.md). If the dashboard is stopped, flush the outbox by hand. This command writes to Linear, so run it on purpose:

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws linear sync WS4
```

The sync sends the current local status of each queued task, so a long queue of old rows for one task sends one update.

While the read-only switch (`AGENT_DASH_READONLY=1`) is on, the poller is off by design, and `doctor` does not report this problem.

## How to verify

`ws linear status WS4` shows an outbox count of 0, and `doctor` no longer lists `linear-outbox-backlog`.
