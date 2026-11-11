# laptop janitor

`scheduled cleanup of agent leftovers on macOS`

Agents leave four kinds of junk that nothing else removes: throwaway Postgres
servers, temp folders, finished git worktrees and worker scratch folders. The
janitor removes only what is provably unused. It is safe while dozens of agents
are working: every guard that cannot be evaluated keeps the thing. Deleting
something in use is the failure that matters; leaving something behind is fine.

- Script: `tools/agent-dash/scripts/laptop_janitor.py`, one file, standard
  library only, `/usr/bin/python3` (3.9).
- Tests: `tools/agent-dash/tests/test_laptop_janitor.py`.
- Runs from `launchd` every day at 04:40, but sweeps only when the last applied
  run is at least three days old.

## Command line

```
laptop_janitor.py run [--apply] [--scheduled] [--only a,b] [--skip a,b] [--json]
laptop_janitor.py status [--json]
laptop_janitor.py install [--apply]
laptop_janitor.py uninstall [--apply]
laptop_janitor.py print-plist
```

- `run` is a dry run unless `--apply` is given. It prints every planned action
  (sweeper, target, size when cheap, reason) and the kept count per reason, and
  changes nothing except writing its own log.
- `run --scheduled` is the `launchd` entry point. It runs exactly the
  sweepers that are due (see below) and prints `JANITOR: not due` and exits 0
  only when none is. When it runs it behaves as `run --apply`, except that
  stdout carries only the per-sweeper kept line, error lines and the summary:
  per-item removal lines go to the run's `.jsonl` log only, because `launchd`
  appends stdout to `launchd.log` without limit. A sweeper whose `ended_at` is
  in the future (clock set back, bad file) or is not a finite number counts as
  due.
- Sweepers run in this order: `postgres`, `tmp`, `worktrees`, `scratch`.
  `--only` and `--skip` take comma lists of those names.
- `--json` prints a JSON report before the summary line.
- The last line of every run is:
  `JANITOR: mode=<dry-run|apply> removed=<n> kept=<n> errors=<n> freed_bytes=<n>`
  In a dry run `removed` counts the actions that would be taken. `freed_bytes`
  only counts sizes that are cheap to obtain (loose files, and the scratch
  tool's own report).
- Exit code 0 means the run completed and no sweeper raised; 1 otherwise.
  A per-item failure is an error line but does not by itself change the exit
  code.

## State, lock and log

| Path | Meaning |
| --- | --- |
| `~/.local/state/laptop-janitor/` | state dir (`LAPTOP_JANITOR_STATE`) |
| `.../lock` | `flock(LOCK_EX\|LOCK_NB)` for the whole run |
| `.../last-run.json` | one entry per sweeper, written atomically |
| `.../recovery.jsonl` | one line per removed worktree |
| `~/Library/Logs/laptop-janitor/` | run logs (`LAPTOP_JANITOR_LOGS`) |
| `~/.config/laptop-janitor/config.json` | optional config (`LAPTOP_JANITOR_CONFIG`) |

A second run while the lock is held prints `already running` and exits 0.

`last-run.json` holds one entry per sweeper:

```json
{"sweepers": {"tmp": {"ended_at": 1700000000, "removed": 2, "kept": 9, "errors": 0}}}
```

A sweeper's entry is written with a temp file plus `os.replace` as soon as that
sweeper finishes, in apply mode only, and only when it finished without raising
and without running out of its budget. A sweeper that raised or timed out keeps
its previous entry, so the next scheduled tick runs it again. A run that is
killed, loses power, or aborts on an interrupt writes nothing for the sweepers
it did not finish, so the next tick or the next login sees them as due. Every
action is idempotent, so a repeat after a partial run is safe.

A sweeper is due when it has no entry, its `ended_at` is missing, non-finite or
in the future, or it is older than `interval_days`. `run --scheduled` runs
exactly the due sweepers. A `last-run.json` in the old single-timestamp format
(`{"ended_at": ...}`) is read as "every sweeper last ran at that time".

The three-day cadence lives in `last-run.json` (`interval_days`, default 3),
not in `launchd`. A reboot, a sleep through the 04:40 slot or a missed day only
delays the run until the next load or the next daily tick.

Each run appends to `run-<UTC stamp>.jsonl`: one JSON object per run start, per
kept item, per action, per error, per kept group and one summary. The newest 30
files are kept.

## Config

Every key has a default in the script. The file is optional. A config that
cannot be read, is not valid JSON, contains an unknown key or has a value of
the wrong type is an error: the janitor prints it and exits 1 rather than
silently using defaults.

```json
{
  "interval_days": 3,
  "sweeper_budget_seconds": 1200,
  "pg_min_age_hours": 24,
  "tmp_min_age_days": 2,
  "session_min_age_days": 7,
  "tmp_age_scan_max_entries": 200000,
  "snapshot_max_age_seconds": 120,
  "worktree_min_idle_days": 3,
  "scratch_min_age_days": 3,
  "tmp_protect_globs": [],
  "postgres_extra_roots": [],
  "repos": [],
  "repo_roots": ["~/Documents/projects"],
  "worktree_protect_globs": ["~/.local/share/agent-dash/releases/*"],
  "worktree_ignorable_globs": [],
  "worktree_manager_cmd": "workspace-manager",
  "agent_dash_cmd": "~/.claude/tools/agent-dash/agent_dash.py"
}
```

`worktree_ignorable_globs` names extra ignored paths a machine's build or
`test` run recreates, so they do not keep a worktree. Each entry is a glob
matched with `fnmatch.fnmatchcase` against the ignored path relative to the
worktree root, for example `["tests/fixtures/generated", "*.log"]`. List only
files a build or test run recreates: they are deleted with the worktree and
are not in the archive ref.

Each sweeper runs inside its own `try/except` with a wall-clock budget
(`sweeper_budget_seconds`, checked between actions). One sweeper failing or
running out of time never stops the others.

## Shared guard: the process snapshot

Once per run, and again whenever the janitor's own snapshot is older than
`snapshot_max_age_seconds` (default 120) at the moment a guard needs it, the
janitor takes `ps -axo pid=,ppid=,etime=,args=` and every process's working
directory (`lsof -a -d cwd -Fpn`, 60 s timeout). A snapshot injected by a test
is never replaced. A failed refresh returns the failed snapshot, so every path
counts as in use.

On first use the snapshot builds two indexes: the set of every ancestor
directory of every working directory, and one joined string of every argument.
Both carry every form of the path (see below), so the common call (no ignored
PIDs beyond the janitor's own) is a set lookup plus a substring test rather
than a loop over every process. Calls that pass `ignore_pids` keep the
per-process loop.

A path is **in use** when any live process other than the janitor itself has
its working directory inside the path, or names the path in its arguments.
Every form of the path is compared: the path as given, its real path, and for
paths under `/private/tmp`, `/private/var` or `/private/etc` also the form
without `/private` (and the reverse for paths under `/tmp`, `/var`, `/etc`).
macOS processes usually name the short form (`TMPDIR` has no `/private`), so a
folder held by such a process must not look free.

If either command fails or times out, every path counts as in use for that run
and the reason is logged. The snapshot is also failed when the parsed working
directories do not include the janitor's own PID: the janitor always has a
working directory, so a scan that does not list it did not work. The `lsof`
exit code alone is not used, because `lsof` can exit nonzero while still
listing processes.

Right before each removal the janitor also re-checks that the target's resolved
path is still inside the root it was found under. A symlink entry is checked
without following it and is unlinked, never followed.

## Sweeper: postgres

Stops throwaway Postgres servers nobody is using. It does not remove the data
directory; the `tmp` and `scratch` sweepers remove that later by age.

A postmaster is a process whose executable name is `postgres` and whose
arguments contain `-D <dir>`. A relative `<dir>` is resolved against that
process's working directory from the snapshot; if it cannot be resolved the
server is kept (`unresolvable-data-dir`).

A server is eligible only when its resolved data dir is inside a disposable
root: the user temp directory, `/tmp`, `/private/tmp`, the scratch root
`~/.cache/agent-dash/scratch`, plus `postgres_extra_roots`. Anything else,
such as a Homebrew service, is kept (`outside-disposable-roots`).

It is kept when it is younger than `pg_min_age_hours` (`too-young`), when it
has a client backend (`client-backend`), or when the snapshot names its data
dir (`in-use`). A client backend is a child of the postmaster whose title
starts with `postgres:` and is not one of `checkpointer`, `background writer`,
`walwriter`, `autovacuum launcher`, `logical replication launcher`, `archiver`,
`stats collector`, `startup`, `walsummarizer`, `io worker`. A helper matches
only when the title is the helper name exactly, or the helper name followed by
a space and digits only (`io worker 3`); a prefix match would take a client
whose database user starts with a helper name (`archiver accounting ...`) for
a helper. An unknown title counts as a client.

The stop is `pg_ctl -D <dir> stop -m fast -w -t 30` when `pg_ctl` is on the
PATH, otherwise `SIGINT` to the postmaster followed by up to 30 s of waiting.
Before the signal the janitor reads that one PID's command line again
(`ps -o args= -p <pid>`) and signals only if it is still a `postgres` process
with the same `-D` value; otherwise the server is reported as already gone.
It never sends `SIGKILL`. A server that does not stop is an error line and the
run continues.

## Sweeper: tmp

Removes old top-level entries from the user temp dir and `/private/tmp`.

The age of an entry is the newest modification time among the entry and the
whole tree inside it. The walk never follows symlinks and stops early only at
the first thing newer than the cutoff or at `tmp_age_scan_max_entries`
(default 200000) entries; an entry cut off at that limit is kept with reason
`age-scan-truncated` rather than judged on a partial view. An entry is removed
when that age is at least `tmp_min_age_days`.

`<temp>/claude-<uid>` is a container: each `<project>/<session>` folder two
levels down is treated as its own entry with the same rule except the age,
which is `session_min_age_days` (default 7) instead of `tmp_min_age_days`: a
session can be alive and quiet for days without naming its folder in any
process arguments. Neither the container nor the project folders are ever
removed.

Guards run cheapest first. An entry is kept, with a reason, when it:

- is not owned by the current user (`other-owner`);
- is a socket or FIFO (`socket-or-fifo`);
- matches a protected glob (`protected-name`). Defaults: `com.*`, `.*`,
  `TemporaryItems`, `tmux-*`, `ssh-*`, `*.sock`, `launchd-*`, plus
  `tmp_protect_globs`;
- is, or has at its top level, a git repository or worktree (`git-repo`);
- is older than the cutoff only on a partial view (`age-scan-truncated`);
- has something newer than the cutoff (`too-new`);
- holds a `postmaster.pid`, found at any depth during the same walk, whose PID
  is alive (`live-postgres`), or that cannot be read as a regular file with a
  numeric first line (`unreadable-postmaster-pid`); only a numeric PID that is
  not alive counts as no server;
- is in use per the process snapshot (`in-use`);
- changed between the first `lstat` and the removal: device, inode or
  modification time differ (`changed-during-scan`);
- escapes its root on re-check (`escaped-root`);
- could not be walked (`unreadable-age`).

Folders are removed with `shutil.rmtree` and an error handler that fixes
read-only permissions once and retries; a failure is an error line.

## Sweeper: worktrees

Removes git worktrees whose work is already on the main branch. The branch
itself is never deleted.

### Part A: the reviewed tool

When `worktree_manager_cmd` (default `workspace-manager`) is on the PATH the
janitor runs `<cmd> plan`, reads the plan file named by the `plan` key of its
JSON output, and for each action whose `decision` is `candidate` runs
`<cmd> closeout --worktree <path> --expected-head <sha>`, adding `--apply` only
when the janitor itself is applying. The action keys it reads are exactly
`path` (worktree path), `HEAD` (head sha) and `decision`; a `candidate` without
both `path` and `HEAD` is an error line, not a guess. The tool is a black box:
its result is logged. If the command is missing or its output is not the
expected JSON, that is logged and part A is skipped.

### Part B: squash-merged worktrees

The reviewed tool only recognises branches that are ancestors of the main
branch, so squash-merged worktrees pile up. Part B handles those.

Repos come from `repos` (main checkout paths). When that is empty they are
discovered one or two levels under each `repo_roots` entry, taking every folder
that contains a `.git` **directory**.

For each repo the janitor runs `git fetch origin --quiet` (60 s, no terminal
prompt). A repo with no `origin` remote is skipped with one log line, not an
error. When the fetch fails, nothing is removed from that repo in this run:
every otherwise-eligible worktree is kept with reason `fetch-failed`, because
stale refs are not proof of a merge. Then it runs
`git worktree list --porcelain`.

A worktree is removed only when **all** of these hold:

1. it is not the main checkout, not locked, not detached, and its branch is not
   `main` or `master`;
2. its path matches no `worktree_protect_globs` (default
   `~/.local/share/agent-dash/releases/*`);
3. `git status --porcelain --ignored` lists nothing except ignored entries
   (`!!`) whose last path component is one of `.venv`, `node_modules`,
   `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.next`, or
   whose worktree-relative path (trailing `/` removed) matches one of
   `worktree_ignorable_globs`; any modified, staged, untracked or other
   ignored entry keeps it. Git may report an ignored directory as one entry
   (`!! some/dir/`): a glob matches it only when it matches the directory
   path itself, so a glob for files inside the directory does not cover the
   directory entry;
4. its git admin dir holds no `MERGE_HEAD`, `REBASE_HEAD`, `CHERRY_PICK_HEAD`,
   `REVERT_HEAD`, `BISECT_LOG`, `rebase-merge` or `rebase-apply`;
5. it is idle: the newest of the HEAD commit time and the modification times
   of `HEAD` and `logs/HEAD` in its admin dir is at least
   `worktree_min_idle_days` old. The index is deliberately not part of the
   idle signal: any `git status` rewrites it, and other tools run status scans
   over every worktree daily, so an index-based signal keeps everything
   `recent` forever. Uncommitted work is covered by guard 3. The timestamps
   are read before running `git status`;
6. it is not in use per the process snapshot;
7. its branch tip is merged, by any one of: the tip is an ancestor of
   `origin/<default branch>`; `gh` is on the PATH and
   `gh pr list --state merged --head <branch> --base <default branch> --json
   headRefOid --limit 20` (30 s) returns a pull request whose `headRefOid`
   equals the tip; or the squash test passes (`git commit-tree <tip>^{tree} -p
   <merge-base>` then `git cherry origin/<default> <temp>` prints a line
   starting with `-`). A branch name alone is never proof, and a pull request
   into any other base does not count.

Immediately before the archive ref is written, the janitor re-reads the
worktree's HEAD (`git -C <path> rev-parse HEAD`) and keeps it with reason
`head-moved` unless it equals the tip the merge proof used, then takes a new
process snapshot (not the cached one) and keeps it if it is now in use. The
merge proof and the `gh` call can take a long time, during which an agent can
start working there or commit.

To remove, the janitor writes `refs/workspace-archive/janitor/<UTC
stamp>/<branch>` to the tip, appends `{repo, path, branch, sha, archive_ref,
time}` to `recovery.jsonl`, then runs `git worktree remove <path>` with no
`--force`. A refusal from git is an error line and the worktree stays. Each
repo finishes with `git worktree prune` in apply mode.

### Undo a worktree removal

The branch was never deleted, so the worktree comes back with:

```
git -C <repo> worktree add <path> <branch>
```

If the branch itself is gone (someone deleted it later), the archive ref still
holds the tip:

```
git -C <repo> update-ref refs/heads/<branch> <sha>
git -C <repo> worktree add <path> <branch>
```

`recovery.jsonl` has the exact `repo`, `path`, `branch`, `sha` and
`archive_ref` for every removal.

## Sweeper: scratch

When `agent_dash_cmd` exists (default
`~/.claude/tools/agent-dash/agent_dash.py`) the janitor runs
`python3 <it> scratch gc --older-than-days <scratch_min_age_days> --json`,
adding `--apply` only when applying, and records the JSON summary in the log.
That command removes finished worker scratch folders (its own guards decide
what is finished; a folder without a launcher marker is never touched). If it
is missing or fails, that is logged and the run goes on.

## Install

```
python3 tools/agent-dash/scripts/laptop_janitor.py install          # dry run
python3 tools/agent-dash/scripts/laptop_janitor.py install --apply
```

`install --apply` copies the running script to
`~/.local/share/laptop-janitor/laptop_janitor.py`, writes
`~/Library/LaunchAgents/com.agent-dash.laptop-janitor.plist`, runs
`launchctl bootout gui/<uid>/com.agent-dash.laptop-janitor` (ignoring failure)
and `launchctl bootstrap gui/<uid> <plist>`.

The plist runs `/usr/bin/python3 <installed script> run --scheduled` with
`RunAtLoad` true and a daily `StartCalendarInterval` at 04:40, as a background
process with low-priority IO and `Nice` 10. `print-plist` prints the exact text
for review.

`uninstall --apply` boots the agent out and removes the plist and the installed
script. The state dir and logs are left alone; remove
`~/.local/state/laptop-janitor/` and `~/Library/Logs/laptop-janitor/` by hand
if you want them gone.

## Reading the log

```
tail -f ~/Library/Logs/laptop-janitor/run-*.jsonl
python3 tools/agent-dash/scripts/laptop_janitor.py status
```

Each JSON line is one of:

- `run` - mode, pid, start, log path;
- `keep` - one kept item and its reason (also aggregated per sweeper);
- `kept` - counts by reason for that sweeper;
- `action` - a removal or planned removal (sweeper, target, reason, size);
- `error` - a per-item or per-sweeper failure;
- `warn` / `skip` / `timeout` / `snapshot` - guards that could not be
  evaluated or a part that was skipped;
- `gc` - the scratch tool's JSON report;
- `summary` - the same numbers as the summary line.

A high `kept` count is normal: most temp entries belong to a live session and
most worktrees are still active. `errors` above zero means something was not
removed; the log line names it.
