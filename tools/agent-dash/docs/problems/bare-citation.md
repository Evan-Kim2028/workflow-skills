# bare-citation: number cited with no name

Bundle text cites a decision, task, pull request, or ADR by its number
alone, for example `#5311` or `ADR 0098` with no phrase before it. One
problem per workstream (`bare-citation:ws:<ID>`): the title names the
workstream and the count, the detail lists the worst 3, and `items`
carries up to 20 member rows (`doctor --ws <ID> --all` for all).

## Why it matters

Numbers alone are unreadable in reports and on the Plan page. Name things,
not numbers: the phrase first, the number in parentheses.

## How to fix

```bash
python3 agent_dash.py doctor --ws WS4 --all
```

Rewrite each hit as `the slow-query dashboard page (#5311)` (`ws
validate WS4` still lists every hit with its rule).

## How to verify

`ws validate WS4` reports no bare citations.
