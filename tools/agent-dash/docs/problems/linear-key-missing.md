# linear-key-missing: a linked workstream has no Linear sign-in

A bundle has `linear.json`, and the dashboard has no Linear credential: no saved OAuth sign-in, and no `LINEAR_API_KEY` in its environment.

## Why it matters

The poller cannot sync the workstream. Status changes made in the terminal queue up in the outbox, and changes made in Linear do not reach the plan. The Linear issues go stale while the local plan keeps moving.

## How to fix

Sign in through the browser. The setup is in [linear.md](../linear.md#setup):

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws linear login
```

On a headless machine, export a personal API key, then restart the dashboard from that shell:

```bash
export LINEAR_API_KEY="<your key>"
python3 ~/.claude/tools/agent-dash/agent_dash.py restart
```

To stop syncing a workstream instead, unlink it. Linear keeps its data:

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws linear unlink WS4
```

## How to verify

`doctor` no longer lists `linear-key-missing`, and `ws linear status WS4` shows a recent `synced_at`.
