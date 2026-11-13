<!-- agent-dash:worker-rules v1 -->
# Worker rules (include in every muse and Haiku worker prompt)

You are a worker, not an orchestrator. Do the task in this prompt and return.
Do not submit jobs (`agentq submit`) and do not launch any worker (`run_worker.*`,
`launch_pi.py`, `muse exec`, `launch_codex.py`). If the task needs another
worker, name it in your report; the lead submits it.

- Scope: obey the read-only or write scope stated in the prompt. Write only in
  your own worktree or the paths named. Read-only means read-only on every
  system you touch, including third-party databases and APIs.
- Tests: run only explicit test files for code you touched
  (`pytest <files>`, or the repo's scoped gate with the right base). Never a
  bare `pytest`, a whole package suite, `validate.sh`, or
  `pre-commit run --all-files`. Never trigger, wait on, or re-run GitHub
  Actions. If you use the repo's scoped gate and it selects more than 3
  suites or about 2,000 tests, stop it and run the test files for your
  changed files directly; report which file widened the selection.
- Memory: on Linux, wrap every test or data run in
  `systemd-run --user --scope -p MemoryMax=8G -p MemorySwapMax=0 <cmd>`
  and check `free -g` first. On macOS (no `systemd-run`, `free` or
  `ulimit -v`), run explicit test files directly: a missing wrapper is
  never a reason to skip tests. Data-loading runs over about 1M rows go to
  the shared data host, not the laptop. No whole-table parity scans on the laptop.
- Long jobs: never babysit. Start anything that runs longer than a few
  minutes detached (`nohup` or `systemd-run --user`), write a status file,
  and return the PID, log path and status path. Do not sleep-poll.
- Ad hoc analysis scripts (your own `python3 -`, loops over files) finish
  in under 2 minutes: prefix them with `timeout 120`. For "is X used
  anywhere" use `codegraph callers <symbol>` or one `rg -w` per name, never
  a script that regex-scans every file for every definition.
- Worktrees: create new worktrees at `~/Documents/worktrees/<repo>/<task>`
  with `workspace-manager new --repo <repo> --task <task>` (dry run; add
  `--apply` to create). Keep existing active worktrees where they are. Before
  task closeout, run `workspace-manager plan`, then
  `workspace-manager closeout --worktree <path>` as a dry run, and add
  `--apply` only when the guards pass. When your task's branch is merged,
  abandoned or handed back, stop every server or process you started, by PID,
  then remove the worktree with `git worktree remove <path>` (never with
  `--force`). Delete its local branch only when it is merged into
  `origin/main`, not checked out in any worktree, and its tip is backed up
  first (`git update-ref refs/workspace-archive/<task>/<branch> <sha>`), using
  `git branch -d`. Never force-delete unmerged work. Leave nothing behind; end
  your report with `git worktree list`. If the parent still needs the branch
  to push or merge, say so instead.
- Git: never use `git stash`. The stash list is shared by every worktree of a
  repo, so a pop can take another agent's work into yours (2026-10-22: a pop
  swapped WIP between a WS14 and a WS15 worktree). Commit WIP to your own
  branch instead, and check `git status` is clean of files you did not touch
  before you commit.
- Killing: only by PID, from a bracket-guarded pattern
  (`for p in $(pgrep -f '[m]y-pattern'); do kill $p; done`). Never
  `pkill -f`.
- Access: if a credential or permission is missing, stop within your first
  few tool calls and return BLOCKED with the exact ask. Never search
  transcripts, other repos or env files for secrets.
- Paid or rate-limited APIs: read the free usage or quota endpoint first and
  stay under the cap the prompt gives. Prefer a local dataset when one exists.
- Remote hosts: leave no scratch files behind; write scratch locally.
- Scratch: environments (venvs), local Postgres clusters, decompressed caches, downloads and
  images go in `$AGENT_SCRATCH`. The launcher creates that folder for this run and exports it
  to every worker, and it is disposable: `python3 ~/.claude/tools/agent-dash/agent_dash.py scratch gc`
  reclaims it once the run has finished. Only prompts, reports, out and err files and decisions
  stay in the run folder. Never write a venv, a database directory or a large cache into the run
  folder; the doctor warns on it and the hub grows without bound. If `AGENT_SCRATCH` is unset
  (you were not started by the launcher), use `~/.cache/agent-dash/scratch/<WS or hub>/<label>/`
  and name that path in your report.
- Public writes (GitHub issues, PR comments, ADRs): draft to a file and
  return it; the parent posts. State any compute or prod-load change a
  recommendation implies.
- Code search: CodeGraph first. Use the `codegraph` MCP tool
  (`codegraph_explore`) when your tool has it, else the `cg` CLI:
  `cg explore "<question>"`, `cg node <symbol|file>`, `cg callers <symbol>`,
  `cg impact <symbol>`; without `cg` on PATH, run
  `codegraph <subcommand> -p <main checkout>`. `cg` works from a worktree and
  reads the main checkout's index; run it before any grep over source code.
  Grep and Read are for non-code text or one detail; never run `cg init` in a worktree.
- Labels: build every run label with `python3 ~/.claude/tools/agent-dash/agent_dash.py label make ...`, never hand-write one; hooks refuse bad ones.
- Finish: after the report is written and committed, print it as your final
  message and exit at once. Do not idle or wait for input. For queued
  jobs the broker watchdog applies: a 6 h max runtime, and a stall after 45 min
  with no change to the run folder. For muse and grok runs, the parent's watcher
  ends a run still alive 10 minutes after its report.
- Report: start with a verdict line, `PASS`, `PARTIAL` (list what was not
  covered or compared), or `BLOCKED`. Cite `file:line`, list exact test
  commands with pass/fail counts, and state what was not done.
- Data access, efficient by default (owner rule, 2026-10-30):
  - Postgres: one set-based query per question, never a loop of per-batch
    or per-id queries (36 batches of 200 ids re-scanned a 5.7 GB table 36
    times). EXPLAIN before any query on a table over 1M rows and keep
    predicates sargable (filter on indexed columns, no functions on them,
    no OR across columns). Select only the columns you need. Large pulls
    run once next to the database (ssh <data-host>, psql on the local
    socket, read-only PGOPTIONS, `\copy ... to stdout | zstd`), not through
    the laptop tunnel; then work from the local file.
  - Local analysis: Polars lazy only (`pl.scan_parquet`, filter and select
    before joins so predicates and projections push down, `join_asof` and
    `over` windows instead of loops, one `.collect()` at the end; check
    `.explain()` when a step is slow). No DuckDB, no pandas `apply`, no
    Python row loops, no reading a whole file eagerly to filter it.
  - Reuse existing exports in the run folder before querying again.
  - On macOS run every local data or test job under the memory cap:
    `~/.claude/scripts/memcap.sh 4096 <cmd>`
    (a Polars step that built a day x row grid hit 17 GB on 2026-10-30).
    Never build day x row grids; one heavy local process at a time; never
    the full CI gate (any `*ci-local.sh` script), only explicit files.
