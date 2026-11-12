# Native agent-session archive

`agent_dash.py logs sessions` keeps the native session history of every
coding agent on the laptop in two places: a **lossless** `tar.zst` copy
under a destination folder, and searchable transcript rows in the loopback
VictoriaLogs store. After a longer idle window it deletes the original once
the archive has been re-verified, and any unit can be restored
byte-for-byte.

This is the companion to [log archive](log-archive.md). That one handles
agent-dash's own run streams and strips base64 images; this one handles the
agents' private session stores and strips nothing. The two share the
manifest/lock conventions, the `lsof` liveness probe and the zstd helpers
from `dash/logarchive.py`, but not the base64 filter.

## Pieces

| Piece | Where | What |
|---|---|---|
| Archive pass | `dash/sessionarchive.py` | Unit discovery, `tar.zst` packing, ingestion, reclaim, restore, census. |
| CLI | `agent_dash.py logs sessions` | `--apply`, `--no-reclaim`, `--scheduled`, `--tool`, `--limit`, `--max-minutes`, `--url`, `--dest`; plus `restore` and `status`. |
| Store client | `dash/vlogs.py` | `make_row(..., extra=...)` adds the native fields; redaction and truncation apply as before. |
| Store service | `scripts/vlogs_install.sh` | `VLOGS_DATA` and `VLOGS_MAX_DISK` feed `-storageDataPath` and `-retention.maxDiskSpaceUsageBytes`. |
| Schedule | `scripts/session_archive_install.sh` | launchd agent `com.agent-dash.session-archive`, hourly, `ProcessType=Background`, `Nice=10`, under `taskpolicy -b` and `memcap.sh 4096`. |

## Config

`log_archive.sessions` in the agent-dash local config (`<state>/config.local.json`,
git-ignored), the same file `logarchive` reads `log_archive.roots` from:

```json
{"log_archive": {"sessions": {
  "dest": "~/Documents/data/agent-sessions",
  "min_idle_hours": 6,
  "reclaim_days": 14,
  "reclaim_days_by_tool": {},
  "max_minutes": 90,
  "tools": null
}}}
```

All keys are optional; the values above are the defaults. `tools: null`
means every registered tool; a list narrows it (`["pi", "claude"]`).
`SESSION_ARCHIVE_DEST` overrides `dest`, and `--dest` overrides both (used
by tests and by a read-only census that must not create the real folder).

## Layout

```
<dest>/                          mode 0700, holds .metadata_never_index
  archive/<tool>/<unit>.tar.zst
  state/manifest.jsonl           append-only; last record per unit wins
  state/archive.lock
  state/last_success.json
```

The unit path is the unit's path relative to the tool root, so a Claude
Code session archives to
`archive/claude/<proj>/<uuid>.tar.zst` and a Codex rollout to
`archive/codex/sessions/2026/11/03/rollout-....jsonl.tar.zst`. The tar
holds members relative to the unit's **parent** dir, so extract restores the
same names.

`.metadata_never_index` stops Spotlight from indexing the archive.

## Units per tool

| tool | root | unit | rows from |
|---|---|---|---|
| claude | `~/.claude/projects` | `<proj>/<uuid>.jsonl` plus sibling dir `<proj>/<uuid>/` (`subagents/`, `tool-results/`) | the `.jsonl` and `<uuid>/subagents/*.jsonl` |
| codex | `~/.codex` | each `sessions/**/rollout-*.jsonl`, `sessions/**/rollout-*.jsonl.zst`, `archived_sessions/*.jsonl.zst` | that file (`.zst` decompressed while reading) |
| grok | `~/.grok/sessions` | each dir `<enc-cwd>/<id>/` | `*.jsonl` lines; `*.log` lines as text rows `type=log` |
| cursor | `~/.cursor` | each dir `chats/<ws>/<id>/` and each dir `projects/<proj>/` | chats: `store.db` via `sqlite3` read-only after copying db+wal+shm to a temp dir; projects: `*.jsonl` and `*.log` lines |
| commandcode | `~/.commandcode/projects` | files grouped by session stem `<proj>/<uuid>.*` | `*.jsonl` |
| pi | `~/.pi/agent/sessions` | each `<proj>/<file>.jsonl` | that file |
| muse | `~/.local/share/muse/sessions` | each dir `YYYY/MM/DD/<id>/` | `session.jsonl` only |

A root that does not exist is reported `absent` and skipped.

### Never touched

- muse `.msp-view-*`, `session-index.db`, `local-tracing`
- `~/.grok/worktrees`, `~/.grok/sessions/session_search.sqlite`
- `~/.codex/*.sqlite`
- `~/.claude/projects/*/<uuid>/memory/`
- any file in a tool root that is not part of a unit above
- symlinks (never followed, never packed)

### Live append-only histories (ingest only)

These are ingested but never packed or reclaimed:
`~/.claude/history.jsonl`, `~/.codex/history.jsonl`,
`~/.commandcode/history.jsonl`, `~/.grok/sessions/*/prompt_history.jsonl`.
Each keeps a stored byte offset and the sha256 of its first `min(size,
4096)` bytes; a shrink or a prefix-hash change restarts from offset 0 with
`rev+1`.

## Row shape

Each row is built with `vlogs.make_row(..., extra=...)`, so redaction and
the 1.8 MiB text cap apply. Per row:

| field | value |
|---|---|
| `_time` | the line's own time field, else the file mtime |
| `_msg` | the original line, lossless (text; for cursor, the decoded JSON) |
| `ws` | `native/<tool>` |
| `label` | the session id |
| `kind` | `native` |
| `type` | the line's type (`role`/`type`/`method`/`record_type`); `raw` if unparseable |
| `tool`, `session`, `project`, `model`, `subtype` | extracted per tool; empty fields omitted |
| `unit` | `<tool>/<unit path>` (relative to the tool root) |
| `member` | the file inside the unit |
| `rev` | starts `"0"`, bumped when a member is rewritten |

Streams are keyed by `ws,label`, so each session is its own stream.

## The pass, per unit (`--apply`)

1. **eligible** when the newest member mtime is at least `min_idle_hours`
   old and no member has an open handle (`logarchive.open_paths`). A unit
   never becomes eligible while open.
2. **skip** when the manifest already has a `reclaimed` record whose
   fingerprint (sorted `(rel, size, mtime_ns)` per member) matches, or an
   `archived` record with a matching fingerprint and reclaim is not due.
3. **pack**: stream a PAX tar of the members into `zstd -12 --long=27 -T2`,
   hashing each member while reading (bounded RSS, 500 MB members
   included).
4. **verify**: stream the copy back through `tarfile` and compare every
   member's sha256 and size; then atomic rename to the final path.
5. **ingest**: query the stored row count for the unit. If it equals the
   manifest's count, send only the lines after the recorded line count when
   a member's old bytes are a prefix of the new file (append growth). If the
   old bytes are not a prefix (rewrite), bump `rev` and resend every member.
   Then `flush()` and verify the stored count for `(unit, rev)`. Any
   mismatch is `status: error` naming the unit; the pass continues.
6. **record** `archived` with each member's `rel`, `size`, `mtime_ns`,
   `sha256`, `lines`, `rev`, plus the archive path and byte counts.
7. **reclaim** (unless `--no-reclaim`), only when: the unit is idle at
   least `reclaim_days` (per-tool override applies), status is `archived`,
   the fingerprint is unchanged, no member is open, every path resolves
   inside the tool root, and re-stat + re-hash matches the manifest. It
   deletes exactly the unit's members and any directory that becomes empty.
   Any doubt records why and keeps the files.

Exit codes: `0` ok, `1` failures or an empty dry run on real input, `2` bad
options, `3` another run holds the lock, `5` store unreachable (only for
`--apply`; the dry run works without the store).

## Modes

`logs sessions` (default) is a **read-only census**: per tool it prints
units, raw bytes, eligible to archive, already archived, eligible to
reclaim (and bytes), absent roots, and the out-of-scope sizes (muse views,
grok worktrees). It measures a projected archive size by compressing the
first 3 eligible units per tool into a temp dir, then deletes it. A dry run
that plans zero actions on real, non-archived input exits 1.

`--scheduled` exits `0` immediately with a reason when any of these hold:
`last_success.json` is under 20 h old, `memory_pressure` reports under 30 %
free, or `pmset -g batt` says `Battery Power`. `last_success.json` is
written only when a run finished with no error and did not stop on the time
budget (a budget stop leaves it unset so the next hourly tick continues the
backlog).

## LogsQL examples

```logsql
# every row of one session
label:="<session-id>"

# pi tool errors in the last day
ws:="native/pi" type:="error" _time:1d

# rows per tool and type over a week
_time:7d | stats by (tool, type) count() rows

# one unit's rows
unit:="claude/-Users-me-projects/1234-uuid" | stats count() n

# model usage by tool
model:* | stats by (tool, model) count() rows

# raw lines that did not parse
type:="raw" | stats by (tool) count() rows
```

## Restore

```sh
# by session id, unit path, or archive path
python3 ~/.claude/tools/agent-dash/agent_dash.py logs sessions restore <session-id>
python3 ~/.claude/tools/agent-dash/agent_dash.py logs sessions restore \
  pi/--Users-me-proj--/2026-11-03T18-25-46-002Z_01a1a2a8.jsonl
python3 ~/.claude/tools/agent-dash/agent_dash.py logs sessions restore \
  ~/Documents/data/agent-sessions/archive/pi/....tar.zst --to /tmp/restore
```

It extracts to the original parent dir (or `--to DIR`) and refuses, exit 2,
if any target path already exists. The record becomes `restored`; the next
pass sees a matching fingerprint and does nothing.

Read an archive by hand:

```sh
zstd -dc <dest>/archive/<tool>/<unit>.tar.zst | tar -tvf -
```

## Operate

```sh
# install / update the hourly agent (idempotent)
sh tools/agent-dash/scripts/session_archive_install.sh

# census only (reads, writes nothing)
python3 ~/.claude/tools/agent-dash/agent_dash.py logs sessions

# one tool, 20 units, a 10-minute budget
python3 ~/.claude/tools/agent-dash/agent_dash.py logs sessions --apply \
  --tool pi --limit 20 --max-minutes 10

# archive without deleting originals
python3 ~/.claude/tools/agent-dash/agent_dash.py logs sessions --apply --no-reclaim

# status and last errors
python3 ~/.claude/tools/agent-dash/agent_dash.py logs sessions status

# uninstall the schedule (keeps archives and state)
sh tools/agent-dash/scripts/session_archive_install.sh --uninstall
```

## Out of scope

- No network: the store is loopback only; the archive folder is local.
- No encryption, no compression of the store's row text beyond redaction
  and truncation.
- Not touched by this job: muse `.msp-view-*`/`session-index.db`/
  `local-tracing`, grok `worktrees` and `session_search.sqlite`, codex
  `*.sqlite`, Claude Code `memory/` dirs, symlinks, and any file in a tool
  root that is not part of a unit.
- No dashboard page. The rows are searchable with LogsQL; the archive is a
  directory tree.
