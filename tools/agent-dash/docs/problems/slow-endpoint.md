# slow-endpoint: an endpoint p95 is over its budget

An endpoint's p95 latency over the last 10 minutes is over its
`warm_ms` budget in `perf_budgets.json` (`perf.endpoints.<path>.p95`
on `/api/health`). Warn on breach; critical past double the budget.

## Why it matters

The pages poll these endpoints every few seconds: a slow endpoint
slows every open page and stacks single-flight waiters behind the one
computation already running.

## How to fix

```bash
python3 agent_dash.py status
```

The problem detail names the endpoint, the budget, the measured p95,
and the profile hint. Fix the code (the usual cause is a cold fold on
the request path — see `tests/test_no_cold_fold_on_request_path.py`),
or raise the budget in `perf_budgets.json` with a written reason.

## How to verify

`/api/health` shows the endpoint p95 back under its budget.
