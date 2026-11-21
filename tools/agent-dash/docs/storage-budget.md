# Storage budget

`agent_dash.py storage budget` measures the whole agent-log footprint once a
day, appends one line to a history file, and projects the days left on the
data volume. It is the answer to "how long until this laptop is full", which
nothing measured before: the store, the archives and the job folders each
grew on their own and the disk filled up without a warning.

Owner rule: logs are historical data kept with **maximum retention**. This
command measures and reports; it never deletes, prunes or re-encodes
anything, and it never sets a VictoriaLogs option that drops old partitions.

## What it measures

| Component | Where | Notes |
|---|---|---|
| `vlogs_compressed` | `vl_compressed_data_size_bytes` on `:19428/metrics` | The store's own counter, summed over storage types. |
| `vlogs_uncompressed` | `vl_uncompressed_data_size_bytes` | The same rows before compression; reported, never added to the footprint. |
| `session_archive` | `<dest>/archive/**/*.tar.zst` | Lossless native session archive. |
| `session_manifest` | `<dest>/state/manifest.jsonl` + `manifest-segments/` | The append-only manifest and its rolled-off segments. |
| `log_archive` | `<state>/log-archive/` | Stripped zstd copies of agentq job streams. |
| `agentq_jobs` | `~/.local/state/agentq/jobs` | Finished and running job folders. |
| `scratch` | `$AGENT_DASH_SCRATCH_ROOT` (default `~/.cache/agent-dash/scratch`) | Worker scratch. |
| `session_originals` | the tool roots in `sessionarchive.TOOLS` | Session files not reclaimed yet. |

The **footprint** is the sum of every component except
`vlogs_uncompressed` (the same rows as `vlogs_compressed`), so nothing is
counted twice. Symlinks are counted as their own size and never followed, so
a browsing symlink into the archive cannot double count it.

The disk figures come from `shutil.disk_usage` on the store's data path
(`vl_free_disk_space_bytes` names it), falling back to the state dir.

## History and growth

Each run appends one JSON line to `<state>/storage-budget.jsonl`
(`AGENT_DASH_STORAGE_BUDGET` overrides the path). The file is the only
state: the CLI, the dashboard card and the doctor check all read its last
line, so a measurement is taken once and read everywhere.

The growth rate is `(newest - oldest) / span_days` over the samples inside
the last 7 days. A component with less than 6 hours of history, or with no
earlier sample, reports `-` (unknown, not zero). A same-hour pair would
divide by almost nothing and report a wild number, so it is refused.

## Thresholds

| State | When |
|---|---|
| `warn` | disk 70% used, or under 365 days to 80% |
| `alert` | disk 78% used, or under 180 days to 80% |

`alert` exits 1, so the hourly launchd job's failure is visible. The
dashboard card and the doctor check read the same verdict.

## Where it runs

`agent_dash.py logs scheduled --apply` (the hourly
`com.agent-dash.session-archive` job) calls it after the two archive passes.
It skips when the last sample is under 20 hours old, so the hourly tick
records once a day. The same tick runs `janitor agentq` (see below).

## Dashboard

The **Storage budget** card sits next to the log search card. It reads
`GET /api/storage/budget`, which returns the last sample, the rates, the
projection and the thresholds. The endpoint never measures, never walks a
tree and never calls the store, so the card can poll it. With no sample yet
the card shows the command that takes one.

The doctor check `storage` reports the same numbers and raises a `warn` or
`critical` problem at the thresholds, so the alert is visible in the
dashboard's problem list and banner, not only in a log line.

## Operate

```sh
# take a sample now and print the table
python3 agent_dash.py storage budget

# machine JSON (the same payload the card reads)
python3 agent_dash.py storage budget --json

# measure without recording (a look, not a data point)
python3 agent_dash.py storage budget --no-record

# store counters only, no tree walk
python3 agent_dash.py storage budget --no-walk
```

## agentq backup cruft

`~/.local/state/agentq` accumulates `broker.json.bak-*`,
`agentq.db.bak-*`, `ws-live-edits-*.patch` and `*.plist.bak-*` files that
nothing pruned. `agent_dash.py janitor agentq` zstd-compresses a backup
older than `--older-than-days` (default 7) in place to `<name>.zst` and
removes the plain file only after the compressed copy decompresses to the
identical sha256. It never deletes history: the compressed copy stays.

It skips a file that is a symlink, not a regular file, already compressed,
has a `.zst` beside it, is newer than the cutoff, or is held open by a
process (one `lsof -Fn` snapshot; a failing lsof keeps everything). Dry run
unless `--apply`; a dry run compresses to a temp file to measure the real
saving, then deletes it. The hourly archive tick runs it with `--apply`; it
is a no-op when nothing is old enough, so the tick costs one directory
listing.

```sh
python3 agent_dash.py janitor agentq                 # dry run
python3 agent_dash.py janitor agentq --apply         # compress in place
python3 agent_dash.py janitor agentq --json          # machine output
```
