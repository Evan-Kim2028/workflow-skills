# agentq proxy: Morph rate governor

`agentq/proxy.py` is a local HTTP proxy on `127.0.0.1:8790` between pi and
`api.morphllm.com`. It admits each request only when every Morph budget the
request touches has room under its learned target. Requests that do not fit
wait in lane queues (urgent, normal, backlog). Upstream 429s are retried inside
the proxy and never reach the client.

## Budgets

| Budget | Scope | Default limit | Metric |
|---|---|---|---|
| `total` | all models | 10,000,000 / min | prompt_tokens (includes cached tokens) + completion_tokens |
| `output` | models starting `morph-dsv41flash` | 70,000 / min | completion tokens |
| `uncached` | model `morph-kimik31` | 24,290 / min | prompt tokens minus cached prompt tokens |

Windows are rolling 60 s. A request's cost is estimated before it is sent:
prompt tokens are body characters / 4, output is the mean of the model's recent
completions capped at `max_tokens`, and the Kimi uncached estimate counts only
the text beyond the longest prefix the same job sent last time. Real usage from
the response replaces the estimate.

## Admission and lanes

- A budget with room is `used + estimate <= limit * target`. A budget with no
  charges in the window admits one request even when that request is larger
  than the cap.
- Lanes come from `jobs.lane` in `$AGENTQ_HOME/agentq.db` for the
  `X-Agentq-Job` header. The lookup is read-only and cached for 2 s. A missing
  database, table or row means `normal`.
- Urgent may hold at most 50 % of a budget while normal or backlog work waits.
  Backlog runs only when no urgent or normal work is queued. A normal ticket
  queued longer than `aging_s` (15 min) moves to urgent.

## Learning the edge

- Target starts at 0.95. A 429 multiplies the named budget's target by 0.97,
  floor 0.6. A body that states a limit (`Limit: N output tokens/minute.`) also
  sets that budget's limit to N. `service_overloaded` cuts `total`. A 429 with
  no recognisable limit cuts every budget the request touched.
- Each full minute with no 429 and blocked demand raises the target by 0.005,
  up to 1.0.
- `$AGENTQ_HOME/proxy_state.json` holds the learned limits and targets. It is
  written atomically and read at start.

## Retries

A 429 is retried after `max(time the budget needs to free, 1 s) + 0-2 s` of
jitter, at the head of its lane. Other upstream errors pass through unchanged.

Give-up is one bound, `give_up_s` (default 240 s), counted from the first enqueue
and covering queue wait plus 429 retries. pi gives up on a request near 300 s and
retries it, so a longer hold would send a duplicate. When the bound passes, the
client gets `429` with the body `agentq: held 240 s, budget full`, and pi's own
backoff takes over. The ticket is dropped and never sent.

If the client disconnects while its request is queued or waiting to retry, the
proxy sees EOF on the client socket, drops the ticket and logs `client_gone`.
A request that is already streaming upstream is not cancelled.

## Status and minute table

- `GET http://127.0.0.1:8790/agentq/status` returns JSON: per budget `limit`,
  `target_frac`, `used_60s` and `queued` per lane, plus `saturated` (a request
  waited more than 2 s in the last minute or is waiting now).
- Each model and minute has one `proxy_minutes` row (created if missing, WAL,
  busy timeout 5000 ms), flushed every 5 s.

## Configuration

`$AGENTQ_HOME/proxy.json` overrides the defaults above. It may set `port`,
`upstream`, `window_s`, `start_target`, `target_floor`, `target_cut`,
`target_step_up`, `default_output_tokens`, `give_up_s`, `retry_floor_s`,
`retry_jitter_s`, `aging_s` and `budgets`. Environment variables:

- `AGENTQ_HOME` (default `~/.local/state/agentq`)
- `AGENTQ_PROXY_PORT` (default 8790)
- `AGENTQ_UPSTREAM` (default `https://api.morphllm.com/v1`)
- `AGENTQ_UPSTREAM_KEY` replaces the Keychain lookup. Use it for tests only.

The key is read once at start from
`security find-generic-password -a "$USER" -s morph-api-key -w`. The proxy strips
the client's `Authorization` header and never logs, prints or writes the key.
Log lines carry only event names, job ids, models and counts.

## Running

```bash
cd tools/agent-dash
python3 -u agentq/proxy.py --home ~/.local/state/agentq
python3 -m pytest tests/test_agentq_proxy.py -q
```

Upstream connections: each upstream request opens a new TLS connection with
`Connection: close`. Connection reuse is not implemented. At about 110 requests
per minute the handshake cost is acceptable.

The proxy uses only the standard library (asyncio streams and ssl), so the
launchd agent runs the system `python3` with no virtualenv. `httpx` is not used.
The template `agentq/launchd/com.agentq.proxy.plist` has `__PYTHON__` and
`__HOME__` placeholders. Install it by substituting them and copying it to
`~/Library/LaunchAgents/`. Nothing is installed by this change.

## Requests

The proxy sets `stream_options.include_usage` on streaming requests when the
client has not set it, so the final SSE chunk carries usage. Responses stream
through chunk by chunk and are re-chunked to the client.
