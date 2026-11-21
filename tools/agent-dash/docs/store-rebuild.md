# Store rebuild and cutover

The VictoriaLogs store is a **rebuildable index**, not a system of record.
The original bytes live in two archives:

- the native session archive, one lossless `tar.zst` per unit under
  `~/.local/share/agent-sessions/archive` ([session archive](session-archive.md));
- the agentq log archive, one stripped `zst` per file under
  `~/.local/share/agent-dash/log-archive` ([log archive](log-archive.md)).

`dash/storebuild.py` re-ingests both into a second VictoriaLogs instance on
a scratch port and data dir, verifies it against the live store, and then
swaps it in. Nothing here deletes history: the old data dir is renamed,
never removed, and the exact `rm -rf` is printed for the owner.

## Why the store is smaller than the archive

The store holds **stripped** rows, not the lossless bytes:

- base64 image runs are replaced with `<img sha256=… bytes=…>` before
  ingest, by the same `dash/logfilter.py` filter the log archive uses. The
  `tar.zst` keeps the original bytes, so a restore is still byte-for-byte.
- a row carries `path` (the unit id) and `label` (the session id) only. The
  duplicate `unit` and `session` extra fields are gone; every reader
  (`dash/logsearch.py`, the log search card, `logs sessions restore` and
  `status`) queries `path`/`label`.

The manifest records how much was stripped: `stripped_bytes` and
`stripped_rows` on a session unit, `stripped_bytes` on a log-archive file.

## Rebuild

```sh
python3 agent_dash.py logs rebuild-store \
    --data-dir "$AGENT_SCRATCH/vlogs-rebuild" --port 19429 --apply
```

- Starts a second VictoriaLogs with the **live plist's flags** (minus the
  listen address and data path), so the verification compares like with
  like. The binary is `~/.local/share/agent-dash/vlogs/bin/victoria-logs-prod`.
- Re-ingests every archived session unit (stripped, deduped by
  `(path, rev, member, line)`) and every log-archive `.zst`, including the
  agentq job entries.
- Inserts with `--parallel N` streams (default 4), one client per thread.
- Checkpoints per unit in `DIR.rebuild/checkpoint.jsonl`, so a re-run
  resumes. `--no-resume` refuses a non-empty data dir instead.
- Runs under `scripts/memcap.sh 4096` and `nice 10` unless `--no-memcap`.
- Verifies per `ws`: row counts must be equal and the assistant token sums
  (`message.usage.input/output/cacheRead`) must be equal, the same fields
  `logarchive` checks. It prints the table and a cutover plan.
- Never touches the live store or the plist. The live store is read only,
  for the verification queries.

`--limit N` rebuilds only the first N jobs (a smoke test). `--keep-running`
leaves the scratch store up after the verification.

## Cutover

```sh
python3 agent_dash.py logs cutover-store --from "$AGENT_SCRATCH/vlogs-rebuild" --apply
```

Dry run unless `--apply`. In order:

1. Stops `com.agent-dash.vlogs` and pauses the hourly archive job
   `com.agent-dash.session-archive`, so nothing is archived mid-swap.
2. Re-ingests every unit archived **after the rebuild's snapshot**
   (`DIR.rebuild/meta.json`), so rows written while the rebuild ran are not
   lost.
3. Renames the live data dir to `<dir>.old-<date>` and moves the rebuilt
   dir into place (a rename, so both must be on one filesystem).
4. Starts the service, waits for `/health`, and re-runs the verification
   against the live port.
5. On failure, stops the service, moves the rebuilt dir back, restores the
   old dir, and starts the service again.
6. Resumes the archive job.

The old dir is kept. Remove it when satisfied:

```sh
rm -rf ~/.local/share/agent-sessions/vlogs.old-<date>
```

## Retention

The store runs with `-retentionPeriod=100y` and **no** disk-usage cap. The
installer writes no `-retention.maxDiskSpaceUsageBytes` or
`maxDiskUsagePercent` line, so the store never evicts a partition. It runs
with `-storage.minFreeDiskSpaceBytes=20GiB` instead: below that floor it
refuses writes and logs loudly, and the owner frees space. An older plist's
cap line is dropped on the next `scripts/vlogs_install.sh` run.

## Tests

`tests/test_storebuild.py` uses a fake store and a fake client: it builds
synthetic archive units and log-archive files, runs a rebuild, checks the
verification table, and exercises the cutover swap and swap-back with a
fake `launchctl`.
