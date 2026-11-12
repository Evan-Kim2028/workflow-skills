# agentq: one laptop job queue

agentq runs agent work for every harness session on this laptop (Claude Code,
codex, muse, pi) through one durable queue. A session submits a prompt file, the
broker launches a pi worker through `scripts/run_worker.py`, and the report comes
back as a file. The queue survives broker, harness and laptop restarts.

Code: `agentq/db.py` (schema, the only writer of `jobs` and `events`),
`agentq/broker.py` (the daemon loop), `agentq/cli.py` (the commands),
`scripts/agentq` (wrapper), `agentq/launchd/com.agentq.broker.plist` (template,
not installed). Tests: `tests/test_agentq_broker.py`.

## The five commands

Each command ends with one `ANSWER:` line. Exit codes: 0 ok, 1 negative fact
(unknown job, nothing to escalate, a failed job), 3 timeout or store unreachable.

```
agentq submit PROMPT_FILE [--kind read|write|review] [--urgent|--backlog]
              [--workspace PATH] [--tier T] [--label L] [--res light|tests|ssh]
agentq wait [ID ...|--mine] [--timeout S]        # default 1800 s
agentq escalate [ID|--mine]                      # no argument: this session's open jobs
agentq ls [--mine|--all] [--json]
agentq result ID [--print]
agentq cancel ID
agentq status
agentq daemon                                    # the broker loop; launchd runs this
```

- `submit` copies the prompt into `$AGENTQ_HOME/jobs/<id>/prompt.md`, records the
  submitting session and harness, and prints the job id. Defaults: kind `read`,
  lane `normal`, resource `light`, workspace the current directory. Tier: `review`
  goes to `pi-kimi`, everything else to `pi-deepseek-flash`, unless `--tier` is given.
  The label comes from `agent_dash.py label make` unless `--label` is given.
- `wait` blocks until every named job (or every open job of this session with
  `--mine`) is done, failed or cancelled. It prints `id verdict report_path` per
  job. It can be called from any process, at any time.
- `escalate` moves jobs to the `urgent` lane. A running job speeds up on its next
  request through the proxy. Each escalation is logged in `events` with the session.
- `result ID` prints the report path and verdict; `--print` also cats the report.
- `cancel` drops a queued job, or sends SIGTERM to a running job's process group
  (stored pgid, checked against the stored process start time first).

Typical use from a harness: `agentq submit prompt.md --kind review`, then
`agentq wait <id>`. For "escalate this subagent work", run `agentq escalate`.

## Lanes and ordering

- Lanes: `urgent` > `normal` > `backlog`. FIFO inside each lane.
- A normal job queued for more than 15 minutes sorts with urgent jobs.
- Urgent jobs hold at most half of the total slots while another job waits
  (never less than one slot).
- Slots: `broker.json` `slots` gives caps per resource (defaults `light` 40,
  `tests` 6, `ssh` 8). `tiers` gives caps per tier (defaults `pi-kimi` 2,
  `pi-deepseek-flash` 48). Both are read on every tick.
- One write job per workspace at a time; a second waits.
- When the proxy reports `saturated`, the broker starts nothing new for that
  tick. Running jobs continue. If the proxy is down, static slots apply.

## Session identity

The session is the first of `CLAUDE_CODE_SESSION_ID`, `CODEX_SESSION_ID`,
`MUSE_SESSION_ID`, `AGENTQ_SESSION`. Otherwise the CLI walks parent pids to the
first harness process and uses `<name>:<pid>:<cwd>`. The harness name is recorded
with each job.

## Durability

- Every job state change is one SQLite transaction (`BEGIN IMMEDIATE`, WAL,
  busy timeout 5 s). Concurrent submits from many processes do not lose jobs.
- Workers run in their own session, so they outlive the broker.
- Each tick reconciles running jobs first:
  - process alive with a matching start time: adopted, nothing changes;
  - dead with a report: finished, the report is copied to `jobs/<id>/report.md`;
  - dead with a QUOTA marker or a 429 in the err log: requeued at the front of
    its lane, no attempt counted, retried after 60 s;
  - dead without a report: `attempt` goes up by one and the job is requeued at
    the front of its lane, resuming from its pi session file when one exists.
    After 3 such deaths the job is `failed`.
- The broker writes `logs/broker.log`. A single daemon holds the flock on
  `broker.pid`; a second `agentq daemon` exits with a message.

## Files

Under `$AGENTQ_HOME` (default `~/.local/state/agentq`):

| Path | What it is |
|---|---|
| `agentq.db` | SQLite store: `jobs`, `events`. `proxy_minutes` belongs to the proxy |
| `broker.json` | optional slot and tier caps (see above) |
| `broker.pid` | the daemon's flock'd pidfile |
| `jobs/<id>/prompt.md` | the submitted prompt |
| `jobs/<id>/runs/<n>/` | the run folder of each launch (run_worker's files live here) |
| `jobs/<id>/report.md` | the finished report |
| `logs/broker.log` | broker log |

Install the LaunchAgent (not done by the repo): fill `__PYTHON__` and `__HOME__` in
`agentq/launchd/com.agentq.broker.plist`, copy it to
`~/Library/LaunchAgents/`, then `launchctl bootstrap gui/$(id -u) <plist>`.

## Known limits

- Resume: the broker passes `AGENTQ_RESUME_SESSION` and `AGENTQ_RESUME_DIR` to the
  launch. `launch_pi.py` must honour them for a resumed run to continue its
  session; until it does, a requeued run starts again from its prompt.
- Cancel signals the process group. A pi child that moved to its own session
  survives a cancel.
