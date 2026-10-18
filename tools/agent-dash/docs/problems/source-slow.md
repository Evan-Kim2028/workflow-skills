# source-slow: collector source poll is slow but returning

A source's last poll took longer than 15 s (`last_duration_s` on
`/api/health`) but returned. The source state is `slow`, counted in
`health.slow_sources`.

## Why it matters

The numbers are still fresh, but the margin is gone: a slower tick
away the poll overruns its budget and the source goes blind.

## How to fix

```bash
python3 agent_dash.py status
```

Read the `last_duration_s` for the slow source. A Dagster poll is one
ssh plus GraphQL call per poll; check the link and the webserver load.
A file source is usually a grown tree outrunning its scan budget.

## How to verify

`/api/health` shows the source back in state `ok` with a smaller
`last_duration_s`.
