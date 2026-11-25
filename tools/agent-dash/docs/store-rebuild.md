# Store rebuild and cutover

The VictoriaLogs store is a **rebuildable index**, not a system of record.
The original bytes live in two archives:

- the native session archive, one lossless `tar.zst` per unit under
  `~/.local/share/agent-sessions/archive` ([session archive](session-archive.md));
- the log archive, one stripped `zst` per file under
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
- Re-ingests **every archived revision** of every session unit (a unit that
  grew after it was packed is archived again with a higher `rev`, and the
  store keeps both, because `rev` is part of its dedupe key), every
  log-archive `.zst`.
- Reads the whole manifest, including the records `logs sessions manifest
  compact` rolled into `manifest-segments/*.jsonl.zst`. Compaction moves
  records, it does not drop them, so the rebuild still sees units whose
  tail record has aged out; reading only `manifest.jsonl` would silently
  skip them.
- Decompresses a member whose file is packed (`.jsonl.zst`, `.log.gz`): the
  tar holds the file's bytes losslessly, so reading the frame as text would
  write a couple of garbage rows instead of the real ones.
- Manifest records with `status: error`, or whose tar is gone, cannot be
  rebuilt. The plan prints how many revisions and recorded rows that is.
- Inserts with `--parallel N` streams (default 4), one client per thread.
- Checkpoints per unit in `DIR.rebuild/checkpoint.jsonl`, so a re-run
  resumes. `--no-resume` refuses a non-empty data dir instead.
- A unit is checkpointed only once the store accepted its rows, so a run
  that **stopped cleanly** resumes exactly. A **killed** run leaves the
  units it had in flight half written, and re-ingesting them adds
  duplicates (the store cannot drop one unit's rows), so a resume from a
  killed run prints a warning and that store is not exact: for an exact
  store, re-run with a fresh `--data-dir` and `--no-resume`.
- Runs under `scripts/memcap.sh 4096` and `nice 10` unless `--no-memcap`.
- Verifies per `ws`: row counts and the assistant token sums
  (`message.usage.input/output/cacheRead`) must match, the same fields
  `logarchive` checks. It prints the table and a cutover plan.

### What "match" means, and the live-only allowance

The archive is a snapshot, so the live store can hold rows the archive does
not: a session ingested live but not yet archived (still active, or younger
than the hourly pass). A rebuild cannot reproduce those rows, and failing on
them would make every rebuild fail forever.

The verification therefore compares per `path` (the unit id), not just per
`ws`:

- a path the **archive has** and the rebuilt store lacks is `MISSING`, and
  the rebuild exits 1;
- a path only the **live store** has is `live-only`: printed with its row
  count, subtracted from the expected total, and not a failure. Its `ws`
  shows `n/a` in the token column, because those rows carry usage the
  archive does not hold;
- a path the archive has and the live store never ingested is `new-only`:
  it adds rows rather than missing them;
- a path the archive has, the live store never had, and the rebuilt store
  lacks is `archive-only`: the store rejected its rows (a future timestamp
  from a test fixture, or a reader that produced none), so neither store
  can hold it. The archive still has the bytes, so it is reported, not a
  failure.

The archive's path list is written to `DIR.rebuild/meta.json` as
`expected_paths` at the end of a rebuild; the cutover extends it with the
delta units it re-ingests and the paths it copies from the live store.

### Copying the rows the archive cannot reproduce

A live-only path is a session ingested live but not yet archived. The
cutover copies it from the live store into the rebuilt one before the swap,
so the swap does not lose it. The same step copies any archived path the
rebuilt store lacks (nothing to read, so it is reported and skipped).

```sh
# standalone: copy into an existing rebuilt store, then verify per path
python3 agent_dash.py logs rebuild-store --data-dir "$AGENT_SCRATCH/vlogs-rebuild" \
    --port 19429 --copy-from-live --apply
```

Dry run unless `--apply`. It reads the live store (read-only), queries one
`path:in(...)` batch at a time, re-inserts each row with the same `_time`,
`_msg`, `ws`, `label` and fields, applies the same base64 strip and the
same `unit`/`session` dedupe the rebuild uses, flushes once per batch, and
verifies per path that the rebuilt count equals the live count. A row the
rebuilt store already holds is skipped, so a second pass adds only the
delta. The copied paths are added to `expected_paths` in `meta.json`.

The cutover runs the copy twice: once after pausing the archive job (the
live-only set is then complete), and again after the delta re-ingest (rows
that arrived while it ran). It refuses to swap if a path is still short.
- Never touches the live store or the plist. The live store is read only,
  for the verification queries.

`--limit N` rebuilds only the first N jobs (a smoke test). `--keep-running`
leaves the scratch store up after the verification.

## Cutover

```sh
python3 agent_dash.py logs cutover-store --from "$AGENT_SCRATCH/vlogs-rebuild" --apply
```

Dry run unless `--apply`. In order:

1. Pauses the hourly archive job `com.agent-dash.session-archive`, so
   nothing is archived mid-swap. The live store stays up for the copy.
2. Copies every live-only path and every archived path the rebuilt store
   lacks from the live store (see above), and verifies per path.
3. Re-ingests every unit archived **after the rebuild's snapshot**
   (`DIR.rebuild/meta.json`), so rows written while the rebuild ran are not
   lost.
4. Copies again for the delta: rows that arrived while step 3 ran. The copy
   skips rows the rebuilt store already holds, so it adds only the new ones.
   It refuses to swap if a path is still short.
5. Stops `com.agent-dash.vlogs`, renames the live data dir to
   `<dir>.old-<date>`, and moves the rebuilt dir into place (a rename, so
   both must be on one filesystem).
6. Starts the service, waits for `/health`, and re-runs the verification
   against the live port.
7. On failure, stops the service, moves the rebuilt dir back, restores the
   old dir, and starts the service again.
8. Resumes the archive job.

The old dir is kept. Remove it when satisfied:

```sh
rm -rf ~/.local/share/agent-sessions/vlogs.old-<date>
```

## Retention

The store runs with `-retentionPeriod=90d` (a 3-month search index over the lossless archives, owner decision 2026-11-21; `VLOGS_RETENTION` overrides it) and **no** disk-usage cap. The
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
