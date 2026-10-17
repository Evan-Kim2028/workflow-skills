# bare-citation: number cited with no name

Bundle text cites a decision, task, pull request, or ADR by its number
alone, for example `#5311` or `ADR 0098` with no phrase before it.

## Why it matters

Numbers alone are unreadable in reports and on the Plan page. Name things,
not numbers: the phrase first, the number in parentheses.

## How to fix

```bash
python3 agent_dash.py ws validate WS4
```

Rewrite each hit as `the slow-query dashboard page (#5311)`.

## How to verify

`ws validate WS4` reports no bare citations.
