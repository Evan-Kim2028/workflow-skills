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
| `log_archive` | `<state>/log-archive/` | Stripped zstd copies of job streams. |
| `scratch` | `$AGENT_DASH_SCRATCH_ROOT` (default `~/.cache/agent-dash/scratch`) | Worker scratch. |
| `worktrees` | `~/Documents/worktrees/<repo>/<task>` plus a main checkout's `.worktrees/<task>` | Git worktrees, with the count and the oldest idle hours (newest of the worktree's git index/HEAD mtime). |
| `session_originals` | the tool roots in `sessionarchive.TOOLS` | Session files not reclaimed yet. |

The **footprint** is the sum of every component except
`vlogs_uncompressed` (the same rows as `vlogs_compressed`), so nothing is
counted twice. Symlinks are counted as their own size and never followed, so
a browsing symlink into the archive cannot double count it. A directory on
another device is skipped, so a mount inside a tree is never walked.

## On disk vs apparent

Every tree is measured twice:

* **apparent** is the sum of `st_size`, what `du -sh` prints.
* **on disk** counts each distinct physical extent once. Each regular file of
  64 KiB or more is probed with `fcntl(F_LOG2PHYS_EXT)` (65), which returns
the device offset of its first extent; two files with the same offset are the
same bytes on disk. The extent is sized by `st_blocks * 512`. Files under
64 KiB are counted at their apparent size and are not probed, because an
open+fcntl per file costs more than the bytes it could save.

APFS clones share extents, so the apparent figure overstates the disk by an
order of magnitude: measured 2026-11-21, 53 worker venvs were 42.5 GiB
apparent and 0.9 GiB on disk (44 copies of `bin/ruff` sat at 2 physical
locations), and the worktree files outside the venvs were 25.7 GiB apparent
and 0.5 GiB on disk. The on-disk figure is the one the projection uses,
because it is the one that fills the volume. A non-APFS volume, a permission
error or an unknown `struct log2phys` layout falls back to the apparent size
for that file.

A full measurement on this laptop (2026-11-21, read-only, `--no-record`):

| component | on disk | apparent | files |
|---|---|---|---|
| git worktrees | 60.2 GiB | 111.3 GiB | 3,655,354 |
| session archive | 11.1 GiB | 11.1 GiB | 11,386 |
| session originals | 10.3 GiB | 10.3 GiB | 71,887 |
| worker scratch | 4.7 GiB | 11.8 GiB | 243,199 |
| log archive | 438 MiB | 434 MiB | 3,848 |
| **footprint** | **99.1 GiB** | **157.1 GiB** | |

The worktree tree is 3.7 million files, so the walk is `lstat`-bound, not
CPU-bound: four threads measured 3.2x the one-thread rate (25.2 s to 7.9 s
for 297k files) and eight were no faster, so the disk is the limit past four.
The worktree walk alone took 249 s cold and the whole measurement 172 s with
the tree in the page cache, at load average 29 on a shared laptop.

The disk figures come from `shutil.disk_usage` on the store's data path
(`vl_free_disk_space_bytes` names it), falling back to the state dir.

## History and growth

Each run appends one JSON line to `<state>/storage-budget.jsonl`
(`AGENT_DASH_STORAGE_BUDGET` overrides the path). The file is the only
state: the CLI, the dashboard card and the doctor check all read its last
line, so a measurement is taken once and read everywhere.

The growth rate is `(newest - oldest) / span_days` over the samples inside
the last 7 days, measured in **on-disk** bytes: a clone that appears twice
does not grow the volume, so it must not shorten the projection. A component
with less than 6 hours of history, or with no earlier sample, reports `-`
(unknown, not zero). A same-hour pair would divide by almost nothing and
report a wild number, so it is refused. A sample written before the on-disk
figure existed reads its apparent bytes, so an old history file still works.

## Thresholds

| State | When |
|---|---|
| `warn` | disk 70% used, or under 365 days to 80% |
| `alert` | disk 78% used, or under 180 days to 80% |

`alert` exits 1, so the hourly launchd job's failure is visible. The
dashboard card and the doctor check read the same verdict.

## Where it runs

`agent_dash.py logs scheduled --apply` (the hourly
`com.agent-dash.session-archive` job) calls it after the session archive pass.
It skips when the last sample is under 20 hours old, so the hourly tick
records once a day.

## Dashboard

The **Storage budget** card sits next to the log search card. It reads
`GET /api/storage/budget`, which returns the last sample, the rates, the
projection and the thresholds. The table shows both columns, **on disk** and
**apparent**, and the 7-day rate is the on-disk one. The endpoint never
measures, never walks a tree and never calls the store, so the card can poll
it. With no sample yet the card shows the command that takes one.

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
