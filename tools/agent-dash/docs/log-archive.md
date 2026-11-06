# Agent log archive

Finished agent event streams are large mostly because of images: the same
base64 PNG is repeated in `tool_execution_end`, `message_start`, `message_end`,
`turn_end` and `agent_end`. This archive stops new logs from growing that way,
moves finished logs into a stripped zstd copy, and makes their rows searchable in
a loopback VictoriaLogs store. The dashboard itself is unchanged. The contract
exception is in `CONTRACT.md` ("Log archive exception").

## Pieces

| Piece | Where | What |
|---|---|---|
| Base64 filter | `dash/logfilter.py` | Replaces runs of 2000+ base64 characters with `<img sha256=<16 hex> bytes=<n>>`. No quotes, so JSON stays valid. Line count and all other bytes are kept. Streaming: a run of 2000+ is hashed as it arrives, never held, so a 30 MB line costs about 64 KB of memory. Flushes per chunk. |
| Launchers | `scripts/launch_pi.py`, `scripts/launch_codex.py`, `scripts/launch_cmd.py` | Child stdout goes through `start_filtered`/`run_filtered`. If the filter raises, the pump falls back to raw passthrough for the rest of the stream, the child keeps running, and its exit code is returned. Session-id capture, verdict and quota checks read the stripped stream, and they still match: base64 never carries those strings. |
| Archive | `dash/logarchive.py`, `agent_dash.py logs archive` | Finds finished streams (idle 3 h, no open handle, no live launcher), measures them in one streaming pass, writes `log-archive/<mirror path>.zst`, ingests rows, verifies, reclaims. Manifest and lock make reruns idempotent. |
| Store client | `dash/vlogs.py` | Loopback only. Redacts secret shapes before ingest. Rows over 1.8 MiB are truncated and flagged (the zstd copy keeps them whole). |
| Service | `scripts/vlogs_install.sh` | VictoriaLogs v1.53.0, checksum-verified, launchd agent `com.agent-dash.vlogs`, 127.0.0.1:19428, flags as specified. `--uninstall`, `--purge`. |
| Option test | `scripts/log_keep_eval.py` | Evidence for what stays at the original path (below and "Per-kind decision"). |

## The archive pass, per file

1. **measure** (one streaming pass): filter, count lines and assistant tokens on the raw and stripped bytes, write zstd -9 to a temp file, keep the stripped sha256.
2. **verify the copy**: `zstd -d` of the copy must hash to the stripped sha256; raw and stripped line counts and token sums must be equal; no unparsed message lines.
3. **ingest**: rows are read back from the zstd copy (never the raw file), redacted, truncated if over 1.8 MiB, posted with `Content-Type: application/stream+json`. Before posting, the store is asked how many rows the path already has: 0 means ingest; the expected count means done (skip the POST); anything else is an error that names the path and stops that file.
4. **verify the store**: row count equals the line count; stored token sums (`unpack_json` over `_msg`) equal the sums over the rows sent.
5. **reclaim** (`--apply` only): re-check size, mtime and open handles, then apply `--keep`.

Per-file failures are recorded in the manifest (`status: error`) and do not stop the run. Exit codes: 0 ok, 1 failures or a dry run that planned nothing on real input, 2 bad options, 3 another run holds the lock, 5 store unreachable.

Fields written to the store per row: `_time`, `_msg`, `ws` (`<hub>/<first segment>`), `label`, `type`, `kind` (`stream`, `out`, `session`, `ndjson`), `path` (mirror path). Streams are keyed by `ws,label`.

## Measured on the real hub roots (dry run, read only)

Run 2026-11-06, `python3 agent_dash.py logs archive` (defaults: both hub roots, 3 h idle), under `memcap.sh 4096`, about 5 minutes.

| | Value |
|---|---|
| Files discovered | 1,128 (live: the count grows while agents run) |
| Candidates (idle, no open handle, no live launcher) | 1,099 |
| Skipped | 29 not idle |
| Ignored (not a stream) | 2 (`runs.ndjson`, `silver_pop_report.ndjson`) |
| Raw | 13.43 GB |
| Stripped | 3.13 GB |
| zstd -9 | 0.19 GB |
| Reclaimable with `--keep remove` for every tool (the first run; per-tool defaults are below) | 13.43 GB |

The brief's raw (13.43 GB), stripped (3.13 GB) and zstd (0.18 GB, here 0.19 GB) figures reproduce. The VictoriaLogs disk figure (0.25 GB) was not measured in this pass.

### Token regression reference

A pi run writes two files with the same assistant messages: the stream (`message_end`) and pi's own `sessions/*.jsonl` (`message`). Summing both double counts. Reconciled by kind on the dry run:

| kind | messages | input | output | cacheRead |
|---|---:|---:|---:|---:|
| stream | 26,474 | 462,007,585 | 15,315,238 | 1,907,280,308 |
| out (claude `out.jsonl`) | 362 | 678 | 393,109 | 40,348,847 |
| **stream + out** | **26,836** | **462,008,263** | **15,708,347** | **1,947,629,155** |
| session (duplicates stream) | 26,474 | 462,007,585 | 15,315,238 | 1,907,280,308 |

The reference (26,836 / 462,008,263 / 15,708,347 / 1,947,629,155) matches the
stream plus out sum exactly. Raw and stripped totals were equal for every file.
Session files are archived and searchable but are not summed into usage.

## Real-store verification

24-file corpus (real logs, copied to a temp hub root, old mtimes), applied against
the real VictoriaLogs v1.53.0 on 127.0.0.1 with `--keep remove`:

- 24 archived, 24 reclaimed, 0 errors. 809 MB raw, 22 MB stripped, 2.8 MB zstd.
- Independent check (`recompute from the originals, zstd round trip, store count and sums`): 24 of 24 pass. Tokens: 1,333 messages, input 2,881,425, output 1,103,364, cacheRead 123,684,662, identical in raw, stripped, zstd and the store.
- Real pi run (`launch_pi.py`, deepseek lane, one-word prompt, temp state): status `done`, verdict `PASS`, session id captured, usage 7,318 input / 3 output recorded. The filtered stream was 16 lines, all valid JSON. The archive pass archived its stream and session files with matching token sums.

## What stays at the original path: option test

Question: after a log is archived, what, if anything, stays at its path, and does
any reader change? Test: `scripts/log_keep_eval.py`.

Method. Real pi run folders are copied (read only) into a temp tree: 40 folders
plus the 791 MB base64-heavy `ws91-exec-03` stream, 310 files, 1.98 GB. The real pi
and generic pollers run on a fresh store with the folders registered. The
generic ledger is narrowed to the fixture's labels and session ids (the full ledger
re-upserts 2,000 runs every poll; the narrowing keeps the pi usage path). Each
option is applied to every stream and session file with the archive's own
`measure` and `reclaim`. Then:

- **incremental**: the same store is polled to convergence before and after the change. Nothing may be re-counted.
- **cold**: a fresh store, in a separate process, polled to convergence over the changed files. Must match the baseline.

Result file: `docs/log-archive-keep-eval.json` (the 791 MB run).

Compared: every run row (`run_id`, source, status, verdict, tier, model, session,
label) and every usage row (tokens by run and message id). Wall-clock fields are
excluded because `ended_at` for a running run follows the clock.

| option | raw reclaimed | scratch bytes left | runs / usage rows | incremental equal | cold equal | tokens equal |
|---|---:|---:|---:|:-:|:-:|:-:|
| control (none) | 0 | 1,980,644,961 | 115 / 3,393 | yes | yes | yes |
| **remove** | 1,978,424,042 | 2,220,919 | 115 / 3,393 | yes | yes | yes |
| stub (pointer line) | 1,978,327,552 | 2,317,409 | 115 / 3,393 | yes | yes | yes |
| stripped rewrite | 918,619,228 | 1,062,025,733 | 115 / 3,393 | yes | yes | yes |

"Scratch bytes left" is what remains under the scratch tree (the stub and remove
rows keep a few KB of directories and stubs; the difference from 1,978 MB is the
reclaim). Each row is one option applied to the same fixture.

Totals in the 115-run baseline: input 4,786,705, output 1,160,199, cacheRead
144,612,264. Statuses: 115 done. Verdicts unchanged across options.
(Run `python3 scripts/log_keep_eval.py --work <dir> --limit 40 --big` to reproduce;
about 5 minutes.)

Finding: no poller reads a finished run's stream or out-of-band stream bytes. The
pi poller reads the TSV, `out-*.md`, `err-*.log`, the `running/` marker, and the
`sessions/` directory's existence. Usage comes from `generic.jsonl`, which the
launcher writes after the run. The commandcode poller reads only the first lines
of a live stream (`run_start`), which the filter preserves.

### Per-kind decision (`--keep` default is per tool)

The first option test used only pi and generic pollers, so it could not decide
for the other kinds. A second run (`--kinds cmd,codex,out --limit 8 --max-folder-mb 5`,
real folders, real `manifests`, `commandcode`, `pi` and `generic` pollers, roots
isolated to empty directories so live data cannot move the result) and two
targeted checks decide the rest. Full numbers: `docs/log-archive-keep-eval-kinds.json`.

| tool (stream or file) | keep | why, measured |
|---|---|---|
| `pi` (`stream-<label>.ndjson`) | **stripped** | A real pi outdir with no manifest row (`ws19/pi`, stream + `sessions/` + prompt, no `pi-sessions.tsv`) is a run only the stream identifies. On a fresh store, `remove` and `stub` drop its run row (`pi:01a185ca…:ws19-plan-slab-ledger-r2`); `stripped` keeps it. pi.poll reads the stream's `session` event on this path. |
| `cmd` (`stream-<label>-<n>.ndjson`) | **stripped** | commandcode.poll reads `run_start` from the stream for a launch with no manifest row. Crash-path check (real cmd folder, manifest removed, dead `running/` pid): `remove` and `stub` lose the `died_early` row on a cold rebuild; `stripped` is equal to the baseline. |
| `codex` (`stream-<label>.jsonl`) | **remove** | No poller reads it. The launcher reads it once, at finish, and writes `codex-sessions.tsv` and `generic.jsonl`. Cold rebuild identical under `remove` and `stub`; `codex.py` reads `~/.codex` rollouts, not this file. |
| `pi-session` (`sessions/*.jsonl`) | **remove** | Duplicates the stream's assistant messages (see the token reference). No poller reads the contents, only that `sessions/` exists, and the empty directory stays. The first option test (40 pi folders) was identical under `remove`, cold and incremental. |
| `out` (`out.jsonl`, claude runs) | **stripped** | Nothing in this repo reads it, but `catalog-confidence`'s `method_gate.py` and `part_checks.py` read tool calls from the raw path. `stripped` keeps every tool call at the path and removes only the base64. Its reclaim is the base64 only. |
| `other` (other `*.ndjson`) | **stripped** | Unknown readers. Conservative. |

Option results on the 8-per-kind fixture (23 folders, 370 files, 706 runs, 46,527 usage rows):

| option | incremental equal | cold equal | tokens equal | stream bytes left at path |
|---|:-:|:-:|:-:|---:|
| control | yes | yes | yes | 1,335,997,352 |
| remove | yes | **no** (the ws19 pi run) | yes | 0 |
| stub | yes | **no** (the ws19 pi run) | yes | 128,661 |
| stripped | yes | yes | yes | 1,321,425,017 |

The "no" rows are the reason for the per-tool table. The earlier pi-only test
(`scripts/log_keep_eval.py` without `--kinds`, 791 MB stream included) had no
folder of this shape, so it did not show it. Its result file is still
`docs/log-archive-keep-eval.json`.

**Cost of the table.** `stripped` keeps the base64-free part of pi and cmd
streams at their paths. On the real roots that is about 3.1 GB of the 13.4 GB
raw (`stripped` column of the dry run). Choosing `remove` for pi and cmd would
reclaim it, and would lose a fresh-store rebuild of no-manifest launches and the
crash-path rows. That trade is the owner's; `--keep remove` overrides it for a run.

Note on the stripped rewrite: it changes the file in place, so the manifest
records the rewritten size and mtime. The next pass skips the file (it does not
re-archive the stripped copy every three hours).

## Exclusions and refusals

Checks before any file is reclaimed, in order. Each row says whether the check
already existed or was added for this change.

| check | applies | status |
|---|---|---|
| not idle (mtime younger than `--min-idle-hours`, default 3) | every mode | existing |
| open handle (`lsof` on the file) | every mode | existing |
| live launcher: a `running/<label>` whose pid is alive, or a live `pid` file | every mode | existing (`launcher_block`) |
| `running/` marker with a dead pid (a crashed launch) | hub-wide pass only (no `--ws`) | **added**: refused. With `--ws` it is allowed. Before this change it was reclaimable (reported as stale) |
| `--exclude GLOB` (repeatable) | every mode | **added**: `fnmatch` against the path relative to the root; `*` crosses `/`. Counted as `excluded` |

The live-process refusal covers the case the brief names; the dead-pid marker
case was not covered before and is refused now in a hub-wide pass. A folder
with one stale marker blocks every stream in it until the owner passes `--ws`
for that workstream.

Exclusion used for the catalog-confidence folder (it is active, and
`method_gate.py`, `part_checks.py` and `evidence/audit/method-followup/tools/*.py`
read `out.jsonl` by path):

```sh
python3 ~/.claude/tools/agent-dash/agent_dash.py logs archive --exclude 'catalog-confidence/*'
```

Dry run on the real roots (read only, `AGENT_DASH_STATE_DIR` set to a temp dir,
run under `memcap 4096`, about 5 minutes; the run wrote nothing):

| | value |
|---|---|
| files discovered | 1,070 (live: grows while agents run) |
| excluded (`catalog-confidence/*`) | 73 files |
| skipped: running/ marker, no `--ws` | 132 files, 5.24 GB raw (80 of them, 5.08 GB, in `ja-catalog/runs` behind one dead-pid marker `hub-exec-identity-v11-shadow-gate-r2`) |
| skipped: not idle | 32 files |
| skipped: live launcher | 10 files |
| candidates | 896 files, raw 2.52 GB, stripped 2.47 GB, zstd -9 0.15 GB |
| reclaimable (per tool) | **0.42 GB** |
| by tool (keep, files, raw, reclaim) | cmd stripped 136, 1.18 GB, 0.01 GB; codex remove 186, 0.27 GB, 0.27 GB; pi stripped 287, 0.97 GB, 0.03 GB; pi-session remove 287, 0.11 GB, 0.11 GB |
| assistant tokens (raw = stripped) | messages 37,984; input 37,518,224; output 19,345,988; cacheRead 2,524,412,632 |

Attribution of the change from the earlier 13.43 GB dry run: 13.43 minus 5.24
GB refused by the marker rule minus about 5.7 GB excluded (catalog-confidence
pi streams, out.jsonl and session files) is 2.5 GB. The rest of the reclaim
under the default is small because pi and cmd streams are `stripped` (the
base64 only, see the per-kind table).

## Corrections to the brief

- `pi.py` does not tail stream bytes with a byte cursor. Its stream reads are
  head-of-file session-id reads during a live run. The byte-cursor readers in this
  repo are `grok_cli`, `commandcode` (command session files), `codex`, `muse`,
  none of which read our stream files. `file_offsets` for stream paths is not used.
- Pi's own session files (`<outdir>/sessions/*.jsonl`) are written by pi itself
  (`--session-dir`), not through our pipe. They are archived by the same pass.
- The VictoriaLogs content-type pitfall reproduces only for the urllib default. A
  POST with no Content-Type (sent as `application/x-www-form-urlencoded`) returns
  200 and stores zero rows. `application/json` and `application/stream+json` both
  store the row. The client pins `application/stream+json`.
- `POST /internal/force_flush` is a POST (GET returns 405).
- NaN: a LogsQL `sum` over an empty set returns `NaN`. `_as_int` maps it to 0.

## Caveats

- **Irreversible for images.** Raw base64 image bytes are not kept anywhere once a
  file is reclaimed. The zstd copy is stripped by design, and so is a stripped
  rewrite. Non-image content is in the zstd copy and the store. Confirm before
  `--apply`.
- **Writers outside this worktree.** `lor-hub-state/catalog-confidence/tools/launch_b2_claude.py` (claude `out.jsonl`) and `tools/watch-pi-pid.py` write streams without the filter. Route them through `dash/logfilter.py` (`run_filtered`) in that repo. Not changed here.
- **Readers outside this worktree.** `catalog-confidence/tools/visual-audit-method/method_gate.py` and `part_checks.py` read raw `out.jsonl` by path for tool calls. After `--apply`, they should read `log-archive/<mirror>.zst` (`zstd -dc`). The stripped copy keeps every tool call, so they still work from the copy.
- **Stale running markers.** A `running/<label>` whose pid is gone is treated as a crashed launch and does not block (the plan reports it). Only a live pid blocks.
- **Session files and totals.** Do not sum `session` rows into usage (they duplicate the stream).
- **Pi `sessions/` directory** stays, empty, after reclaim. `_looks_like_pi_dir` still sees it.
- **Launcher death (fixed).** The launchers (`launch_pi.py`, `launch_cmd.py`, `launch_codex.py`) now pass `detached=True`: the child's stdout pipe goes to a filter process in its own session (`dash/logfilter.py`), not to the launcher. If the launcher is killed, the filter keeps reading until the child closes stdout, so the child finishes as it did when it wrote to a file. Measured with a killed launcher (`SIGKILL` at 1.5 s, child writes 61 lines over 6 s): the old file redirect finished (61 lines); the in-process pump died at 15 lines with `BrokenPipeError`; the detached filter finished (61 lines). What a killed launcher still loses is its own bookkeeping (the TSV row, the usage lines, the verdict), exactly as before. The reaper marks those runs dead. If the filter cannot start, the launcher uses the in-process pump (the old failure mode) and says so in its err log.
- **Uninstall while running.** `vlogs_install.sh --uninstall --purge` removes data. Under launchd it boots out the agent first. With `VLOGS_NO_LOAD=1` it cannot stop a running process. Do not purge a store that is running.

## Roots

With no `--root`, the archive scans the roots in `log_archive.roots` of the
agent-dash local config (`<state>/config.local.json`, git-ignored). The repo
names no project, and there is no fallback: with no `log_archive.roots` the
command refuses (exit 2), so `ws archive` cleanup archives nothing until the key
is set. (`hub_dir` is not used: it can name a project that is not a log hub.)
To scan both hubs add, in that file:

```json
{"log_archive": {"roots": ["~/Documents/projects/<hub-one>-hub-state", "~/Documents/projects/<hub-two>-hub-state"]}}
```

## Operate

```sh
# install (once; idempotent)
sh tools/agent-dash/scripts/vlogs_install.sh          # or ~/.claude/tools/agent-dash/scripts/vlogs_install.sh

# dry run against the real roots (reads only, writes nothing)
python3 ~/.claude/tools/agent-dash/agent_dash.py logs archive

# apply (store must be up; takes a lock; resumable)
python3 ~/.claude/tools/agent-dash/agent_dash.py logs archive --apply

# hub-wide, leaving catalog-confidence alone (read only without --apply)
python3 ~/.claude/tools/agent-dash/agent_dash.py logs archive --exclude 'catalog-confidence/*'

# one workstream (what a ws archive hook would call)
python3 ~/.claude/tools/agent-dash/agent_dash.py logs archive --ws ws25-ja-quality --apply

# read an archived log back
zstd -dc ~/.local/share/agent-dash/log-archive/<mirror path>.zst | head

# uninstall the store (keeps data), or purge it
sh tools/agent-dash/scripts/vlogs_install.sh --uninstall
sh tools/agent-dash/scripts/vlogs_install.sh --uninstall --purge
```

Manifest: `~/.local/share/agent-dash/log-archive/manifest.jsonl` (last record per
path wins; statuses `measured`, `archived`, `reclaimed`, `error`). Lock:
`log-archive/archive.lock`. A file archived and reclaimed is skipped on every
rerun.
