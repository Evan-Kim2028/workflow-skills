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
- `run --scheduled` is the `launchd` entry point. When not due it prints
  `not due` and exits 0 without sweeping or writing a log. When due it behaves
  as `run --apply`.
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
| `.../last-run.json` | written atomically after an applied run |
| `.../recovery.jsonl` | one line per removed worktree |
| `~/Library/Logs/laptop-janitor/` | run logs (`LAPTOP_JANITOR_LOGS`) |
| `~/.config/laptop-janitor/config.json` | optional config (`LAPTOP_JANITOR_CONFIG`) |

A second run while the lock is held prints `already running` and exits 0.

`last-run.json` is written with a temp file plus `os.replace` only after an
applied run has given every sweeper its turn. A run that is killed, loses
power, or aborts on an interrupt writes nothing, so the next scheduled tick or
the next login sees it as due and runs again. Every action is idempotent, so a
repeat after a partial run is safe.

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
  "worktree_min_idle_days": 3,
  "scratch_min_age_days": 3,
  "tmp_protect_globs": [],
  "postgres_extra_roots": [],
  "repos": [],
  "repo_roots": ["~/Documents/projects"],
  "worktree_protect_globs": ["~/.local/share/agent-dash/releases/*"],
  "worktree_manager_cmd": "workspace-manager",
  "agent_dash_cmd": "~/.claude/tools/agent-dash/agent_dash.py"
}
```

Each sweeper runs inside its own `try/except` with a wall-clock budget
(`sweeper_budget_seconds`, checked between actions). One sweeper failing or
running out of time never stops the others.

## Shared guard: the process snapshot

Once per run the janitor takes `ps -axo pid=,ppid=,etime=,args=` and every
process's working directory (`lsof -a -d cwd -Fpn`, 60 s timeout). A path is
**in use** when any live process other than the janitor itself has its working
directory inside the path, or names the path (as given or as its real path) in
its arguments. If either command fails or times out, every path counts as in
use for that run and the reason is logged.

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
`stats collector`, `startup`, `walsummarizer`, `io worker`. An unknown title
counts as a client.

The stop is `pg_ctl -D <dir> stop -m fast -w -t 30` when `pg_ctl` is on the
PATH, otherwise `SIGINT` to the postmaster followed by up to 30 s of waiting.
It never sends `SIGKILL`. A server that does not stop is an error line and the
run continues.

## Sweeper: tmp

Removes old top-level entries from the user temp dir and `/private/tmp`.

The age of an entry is the newest modification time among the entry and a
bounded walk inside it (depth 3, at most 2000 entries, stopping at the first
thing newer than the cutoff). An entry is removed when that age is at least
`tmp_min_age_days`.

`<temp>/claude-<uid>` is a container: each `<project>/<session>` folder two
levels down is treated as its own entry with the same rule, and neither the
container nor the project folders are ever removed.

An entry is kept, with a reason, when it:

- is not owned by the current user (`other-owner`);
- is a socket or FIFO (`socket-or-fifo`);
- matches a protected glob (`protected-name`). Defaults: `com.*`, `.*`,
  `TemporaryItems`, `tmux-*`, `ssh-*`, `*.sock`, `launchd-*`, plus
  `tmp_protect_globs`;
- is, or has at its top level, a git repository or worktree (`git-repo`);
- contains a `postmaster.pid` (searched to depth 3) whose PID is alive
  (`live-postgres`);
- is in use per the process snapshot (`in-use`);
- has something newer than the cutoff (`too-new`).

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
when the janitor itself is applying. The tool is a black box: its result is
logged. If the command is missing or its output is not the expected JSON, that
is logged and part A is skipped.

### Part B: squash-merged worktrees

The reviewed tool only recognises branches that are ancestors of the main
branch, so squash-merged worktrees pile up. Part B handles those.

Repos come from `repos` (main checkout paths). When that is empty they are
discovered one or two levels under each `repo_roots` entry, taking every folder
that contains a `.git` **directory**.

For each repo the janitor runs `git fetch origin --quiet` (60 s, no terminal
prompt; a failure is logged and the sweep continues with the refs it has), then
`git worktree list --porcelain`.

A worktree is removed only when **all** of these hold:

1. it is not the main checkout, not locked, not detached, and its branch is not
   `main` or `master`;
2. its path matches no `worktree_protect_globs` (default
   `~/.local/share/agent-dash/releases/*`);
3. `git status --porcelain --ignored` lists nothing except ignored entries
   (`!!`) whose last path component is one of `.venv`, `node_modules`,
   `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.next`; any
   modified, staged, untracked or other ignored entry keeps it;
4. its git admin dir holds no `MERGE_HEAD`, `REBASE_HEAD`, `CHERRY_PICK_HEAD`,
   `REVERT_HEAD`, `BISECT_LOG`, `rebase-merge` or `rebase-apply`;
5. it is idle: the newest of the HEAD commit time and the modification times
   of `HEAD`, `index` and `logs/HEAD` in its admin dir is at least
   `worktree_min_idle_days` old. The timestamps are read before running
   `git status`, because status refreshes the index and would otherwise make
   every worktree look active forever;
6. it is not in use per the process snapshot;
7. its branch tip is merged, by any one of: the tip is an ancestor of
   `origin/<default branch>`; `gh` is on the PATH and
   `gh pr list --state merged --head <branch> --json headRefOid --limit 20`
   (30 s) returns a pull request whose `headRefOid` equals the tip; or the
   squash test passes (`git commit-tree <tip>^{tree} -p <merge-base>` then
   `git cherry origin/<default> <temp>` prints a line starting with `-`).
   A branch name alone is never proof.

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
