# invalid-bundle: bundle fails validation

`ws validate` reports issues for the bundle: bad statuses, unknown dep
ids, malformed trains or approvals, or unreadable files.

## Why it matters

The dashboard renders plans from these files. An invalid bundle means the
map, progress, and replay for that workstream cannot be trusted.

## How to fix

```bash
python3 agent_dash.py ws validate WS4
```

Fix each issue it names (unknown task ids, bad statuses, train member
overflow), then re-run the command.

## How to verify

`ws validate WS4` prints no issues.
