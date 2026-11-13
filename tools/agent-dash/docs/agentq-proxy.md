# agentq proxy: Morph rate governor

`agentq/proxy.py` is a local HTTP proxy on `127.0.0.1:18790` between pi and
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
| `uncached_ds` | models starting `morph-dsv41flash` | 400,000 / min | prompt tokens minus cached prompt tokens |

Each budget has a `limit_type` (`total`, `output` or `uncached`) that says which
Morph limit it tracks. Uncached is split per model because Morph states one
uncached limit per model: DeepSeek's and Kimi's limits are different numbers.

Windows are rolling 60 s by default; `window_mode` can make a budget a UTC
calendar minute instead (see "Calendar window and charge basis"). A request's cost is estimated before it is sent:
prompt tokens are body characters / 4, output is the mean of the model's recent
completions capped at `max_tokens`, and the uncached estimate (for Kimi and
DeepSeek alike) counts only the text beyond the longest prefix any recent request
shared with it (Morph's prompt cache is shared, so the match is across jobs).
Requests with no job id or the literal `none` are anonymous: a request that
extends a stored conversation replaces that entry, and any other request adds
one, so each session keeps one entry. The store holds the 256 most recent jobs,
and requests from both models share it. Real usage from the response replaces the
estimate.

## Admission and lanes

- A budget with room is `used + estimate <= limit * target`. A budget with no
  charges in the window admits one request even when that request is larger
  than the cap.
- Lanes come from `jobs.lane` in `$AGENTQ_HOME/agentq.db` for the
  `X-Agentq-Job` header. The lookup is read-only and cached for 2 s. A missing
  database, table or row means `normal`.
- Urgent may hold at most 50 % of a budget while normal or backlog work waits.
  Blocking is per budget: a ticket that does not fit closes its budget for the
  rest of the pass, and later tickets on that budget wait in order, while tickets
  on other budgets still run. Backlog runs only on budgets that no waiting urgent
  or normal ticket is blocked on. A block caused only by the urgent 50 % guard
  closes that budget for the rest of the urgent lane; normal and backlog work
  still use it. A normal ticket queued longer than `aging_s` (15 min) moves to
  urgent.

## Learning the edge

- Target starts at 0.98 (`start_target`). A 429 multiplies the named budget's
  target by 0.97 (`target_cut`), floor 0.6. A cut is paced: a budget is cut at most
  once per `fast_interval_s` (10 s), so a burst of 429s is one cut. Every 429
  still counts in the minute row and refreshes the quiet clock. A body that states
  a limit (`Limit: N output tokens/minute.`) also sets the limit to N on every
  budget that has that `limit_type` and applies to the refused model. A stated
  limit never reaches a budget for another model, so a DeepSeek body cannot
  overwrite Kimi's uncached limit or the reverse. `service_overloaded` cuts
  `total`. A 429 with no recognisable limit, or with a stated limit that no
  budget for the model tracks, cuts every budget the request touched.
- With no 429 history a saturated budget climbs the fast ramp: its target rises
  by `fast_step_up` (0.05) every `fast_interval_s` (10 s) up to `fast_until`
  (1.5), so from 0.98 it reaches 1.0 in about 10 s and keeps climbing until Morph's
  first 429. The 429 cut sets the budget's `fast_limit` to the cut target, and the
  fast ramp stops there: above it the slow rule applies, one `target_step_up`
  (0.02) per full minute with no 429 and blocked demand, up to `target_ceiling`
  (1.5). `fast_limit` clears after 30 minutes with no 429 on that budget, so the
  ramp can try the edge again. It is kept in `proxy_state.json`.
- Steady state: the proxy sits at the edge it learned. The first 429 sets
  `fast_limit` near Morph's real edge E. The target then probes up by 0.02 a
  minute and is cut by 0.97 on the next 429, so it oscillates between about
  0.97 E and E (plus at most one probe step). Between 429s it is at 1.0 of the
  learned edge; right after a cut it is at about 0.97 of it.

Targets may exceed 1.0. The window counts each in-flight request at its full
estimate from admission and again at its real usage for 60 s after completion,
so it overstates what Morph tallies. A budget at 1.0 can therefore stop
admitting work that Morph would accept. Targets above 1.0 let AIMD find the real
edge from 429 feedback instead of stopping at that projection.
- `$AGENTQ_HOME/proxy_state.json` holds the learned limits and targets. It is
  written atomically and read at start.

## Calendar window and charge basis

Evidence for the experiment. Of 626 upstream 429s whose body states
`Limit: 10000000 tokens/minute`, 1 % fell in seconds :00 to :29 of the UTC
minute, 7 % in :30 to :39, 31 % in :40 to :49 and 62 % in :50 to :59, and the
bursts stop at :00. Morph therefore appears to enforce the total limit on a fixed
UTC minute that resets at :00, not on a rolling 60 s window. Morph also seems to
count more than the billed usage: it reaches 10 M by about :45, while the billed
prompt plus completion for the whole minute is 5 to 8 M. One explanation is that
Morph charges the prompt plus the requested `max_tokens` when a request starts,
so requests that ask for a large `max_tokens` (pi sends about 74,000, which the
proxy caps at 32,768 for DeepSeek) hold more than they bill.

Two settings test this. Both default to the current behaviour.

- `window_mode`: `rolling` (default) or `calendar`. In `calendar` mode a budget's
  usage is the charges admitted in the current UTC minute, and the window resets
  at :00. A request refused by a full calendar budget waits for the next :00 plus
  up to `calendar_jitter_s` (1.5 s) of jitter, instead of the time until
  completed charges age out.
- `charge_basis`: `billed` (default) estimates at admission and replaces the
  estimate with real usage at completion. `prompt_plus_max` charges the prompt
  estimate plus the capped requested `max_tokens` at admission and keeps that
  charge after completion, which models a reservation Morph holds for the minute.
  A reservation is released only when the request is refused (429), fails or is
  abandoned, because upstream did not bill it. The output budget reserves its
  requested `max_tokens`, and each uncached budget keeps its estimate.

Set either in `$AGENTQ_HOME/proxy.json`, globally or per budget:

```json
{"window_mode": "calendar", "charge_basis": "prompt_plus_max",
 "budgets": {"total": {"window_mode": "calendar"}}}
```

Restart the proxy after an edit. Config is read at start, and an unknown mode
or basis stops the proxy at start.

Calibration runs in every mode. Each upstream 429 logs a `calib` line with the
budget its stated limit applied to, the second of the UTC minute it fell in, and the minute's
totals so far: admitted requests, admitted prompt estimate, admitted capped
`max_tokens`, and billed prompt plus completion of requests admitted in that
minute. Each UTC minute also has one `proxy_calib` row (`minute`, `req`,
`prompt_est`, `max_tokens`, `billed`, `first_429_s`, `r429`), flushed with the
minute rows. Minutes are held in memory for three minutes and seeded from the row
after a restart, so counts survive a restart. The per-family split is in the log
line only, and it restarts from zero after a restart.

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

- `GET http://127.0.0.1:18790/agentq/status` returns JSON: per budget `limit`,
  `target_frac`, `used_60s` and `queued` per lane, plus `saturated` (a request
  waited more than 2 s in the last minute or is waiting now).
- Each budget reports `window_mode` and `charge_basis`. A calendar budget also
  reports `seconds_to_reset`, the seconds until its next UTC :00.
- Each UTC minute has one `proxy_calib` row (see "Calendar window and charge basis").
- Each model and minute has one `proxy_minutes` row (created if missing, WAL,
  busy timeout 5000 ms), flushed every 5 s.
- Each (job, model) pair has one `proxy_jobs` row for per-run accounting. The job
  is the `X-Agentq-Job` header, which the broker sets to the 12-hex job id; a
  request without one counts under job `none`. Direct pi launches send their run
  label (`AGENTQ_RUN`, set by `launch_pi.py`) once the models.json header reads it.
  Columns: `first_ts` and `last_ts` (wall-clock epochs), `req` (admitted
  requests, a retry is admitted again), `ok` (200 responses), `r429` (upstream
  429s), `input` (uncached prompt tokens), `cache_read`, `output`,
  `wait_ms_total` (queue wait before each admission, summed) and
  `upstream_ms_total` (send to end of response, summed over upstream attempts).
  The counters live in memory per key and are flushed with the minute rows. Each
  flush adds its deltas with an upsert and subtracts them only after the write
  succeeds, so a failed write is retried, a restart does not replay counts, and
  several processes can share the table.

## Configuration

`$AGENTQ_HOME/proxy.json` overrides the defaults above. It may set `port`,
`upstream`, `window_s`, `start_target`, `target_floor`, `target_cut`,
`target_step_up`, `target_ceiling`, `fast_until`, `fast_step_up`, `fast_interval_s`, `default_output_tokens`, `give_up_s`, `retry_floor_s`,
`retry_jitter_s`, `aging_s`, `connect_timeout_s` (10), `upstream_idle_s` (120),
`shutdown_s` (25), `budgets`, `max_tokens_cap`, `window_mode` (`rolling`),
`charge_basis` (`billed`), `calendar_jitter_s` (1.5) and the log file settings
`log_path` (`logs/proxy.err.log`, relative to `$AGENTQ_HOME`; empty means
stderr), `log_max_bytes` (10 MB) and `log_backups` (3). Environment variables:

- `AGENTQ_HOME` (default `~/.local/state/agentq`)
- `AGENTQ_PROXY_PORT` (default 18790)
- `AGENTQ_UPSTREAM` (default `https://api.morphllm.com/v1`)
- `AGENTQ_UPSTREAM_KEY` replaces the Keychain lookup. Use it for tests only.

`max_tokens_cap` maps a model-name prefix to an integer. When a request's model
starts with a prefix (the longest match wins) and its `max_tokens` or
`max_completion_tokens` is above the cap, the proxy lowers that field to the cap
before forwarding and estimates output from the capped value. Requests at or below
the cap pass through byte for byte. The default is empty, so nothing is capped.
Example, for an experiment: `{"max_tokens_cap": {"morph-dsv41flash": 8000}}`.

The key is read once at start from
`security find-generic-password -a "$USER" -s morph-api-key -w`. The proxy strips
the client's `Authorization` header and never logs, prints or writes the key.
Log lines carry only event names, job ids, models and counts.

The log is a file the proxy opens itself (`log_path`), rotating at
`log_max_bytes` and keeping `log_backups` old files (`proxy.err.log.1` …
`.3`); restarting it does not lose the file. launchd's stderr redirect still
points at the same path, so a startup failure (a missing key, a traceback
before the governor exists) still lands there. A line launchd writes after the
proxy has rotated goes into the rotated file, which is why stderr carries
startup errors only. Readers of the recent window (the dashboard's 429 minutes
from `calib` lines) read the live file preceded by `proxy.err.log.1`, so a 429
written just before a rotation is still counted; the interactive log viewer
reads the live file only.

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
`~/Library/LaunchAgents/`, then reload the agent (see the commands in the PR). Nothing is installed by this change.

## Hang auto-heal

The broker probes `GET /agentq/status` on every tick, with a 5 s limit. If the probe
fails, or takes longer than 5 s, for `proxy_heal.fail_s` (90 s) without a break, and the
launchd job `com.agentq.proxy` reports a running pid, the broker logs
`proxy unresponsive for N s; restarting` once and runs
`launchctl kickstart -k gui/<uid>/com.agentq.proxy`. Restarts are at least
`min_interval_s` (600 s) apart, and after each restart the proxy must fail for another
`fail_s` before the next one. A job that is not loaded, or has no pid, is never touched.
Settings live in `broker.json` under `proxy_heal`: `{"enabled": true, "fail_s": 90,
"min_interval_s": 600}`. Only the daemon started by `agentq.cli daemon` runs the healer.

The launch agent restarts quickly after a crash: `ThrottleInterval` is 5 s and the job
runs at `Interactive` priority.

## Failure handling

Every path after admission settles or releases the ticket's charge: a 200 that
ends cleanly settles to real usage, and anything else releases it (a 429, an
error, a client that left, a timeout, a truncated stream).

- A client that closes while its request is queued, waiting to retry, or already
  admitted but not yet sent is dropped and its charge released. Nothing is sent.
- Upstream timeouts: connect after `connect_timeout_s`; each read after
  `upstream_idle_s` without bytes. A timeout before any response byte returns 504
  to the client. After that, the client connection is aborted.
- Upstream closes before a status line: `502` with `{"error": "bad_status_line"}`.
- A chunked or length-delimited body that ends early is a truncation. The client
  connection is aborted with no terminating chunk, so the client sees the cut.
  The proxy never reports a truncated body as complete.
- Errors are counted by kind in the `errors` object of the status endpoint:
  `timeout`, `truncated`, `bad_status_line`, `connection`.
- Backstop: an unfinished charge older than `give_up_s + upstream_idle_s` is
  expired from the budget, so a lost request cannot hold budget forever. A
  stream still legitimately running past that age stops counting. This is rare
  and the error is in the safe direction.

## Shutdown and state

- On SIGTERM or SIGINT the proxy stops accepting connections and runs one shutdown
  budget of `shutdown_s` (25 s). In-flight requests get `shutdown_s - 5` s and are then
  cancelled. Pending seed reads get what is left minus 3 s. The final writes always get
  at least 3 s: they run on their own thread with a 1 s busy timeout, so they never queue
  behind a seed read still running. launchd's `ExitTimeOut` is 40 s, which covers the
  whole budget with margin.
- The learned state is written by the maintenance loop (about every 0.2 s when
  it changed, and at least every 5 s), off the request path, not on every 429.
  Shutdown always writes it once more, after in-flight requests settle.
- Warm restarts: each budget's state records `window_used`, the tokens charged
  in the current window (completed and in-flight). If the state is less than one
  window old at startup, that usage is restored as one completed charge that
  expires when the old window would have, so a restart does not reopen an empty
  budget while Morph still counts the previous minute. Older state seeds nothing,
  and state without `window_used` loads as before.
- Minute rows: the first flush of a model's minute in a process seeds its counters
  from the stored `proxy_minutes` row, so a restart adds to the minute instead of
  replacing it.
- Prefix memory: each job (or anonymous session) keeps cumulative 8-byte digests
  of 1024-character blocks, not its prompt. The uncached estimate is accurate to one block (about 256 tokens).
- Large request bodies (over 256 KiB) are parsed in a worker thread.

## Missing key

If the Keychain has no Morph key, the proxy writes one line to stderr and exits
with status 78 (EX_CONFIG). The plist sets `ThrottleInterval` to 30 s, so a
missing key shows as a slow restart loop with one clear line in the log.

## Requests

The proxy sets `stream_options.include_usage` on streaming requests when the
client has not set it, so the final SSE chunk carries usage. Responses stream
through chunk by chunk and are re-chunked to the client.
