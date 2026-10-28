# Plan bundles

A new session adds a workstream by writing a bundle, not by editing the dashboard. The tool code stays generic. The words live under `AGENT_DASH_WORKSTREAMS`, one directory per id. Default is `~/.local/share/agent-dash/workstreams`.

## Add a workstream

```
python3 agent_dash.py ws new WS4 "Read path"
```

That writes `bundle.json`, a template `plan.json`, `layout.json`, an empty `visuals.html`, and `START-PROMPT.md`. `ws validate WS4` should already pass.

`START-PROMPT.md` is the paste-ready prompt that starts this workstream's lead in a fresh session. Edit it so it names the kickoff brief and the done criteria. The bundle API and the plan API return that text as `start_prompt`. A missing file is null.

Fill `plan.json` before you render. The shape is the decision record: `frame`, `dag`, `decisions`, `motivation`, `agent_friendly`, `narrative`. Task status is `todo`, `wip`, `pr`, `prog`, `done`, or `deferred`. `wip` is work on a branch; `pr` is approved or open. A deferred task can set `moved_to` to a workstream id and a `note`. Keep done tasks in the dag so the map shows progress. Each section (a `dag.groups` entry) may carry `objective: "O1"` as an alternative to listing the section under an objective's `sections`; either form is accepted, and when both are present they must agree.

## Plan frame

Write the frame before anything else, in this order:

1. Write `frame.before` and `frame.after` first, each one sentence with the number that matters. If you cannot name the number, the plan is not ready.
2. Write `focus` (what you optimise for and what you trade away) and the scope lists; put real owners or reasons on every out-of-scope item.
3. Name 2 to 5 objectives as before → after pairs with a measure. Then build dag sections so every section serves exactly one objective.
4. Write `path`: the choice, why, trade-offs, and at least three alternatives including doing nothing, each with a verdict and when to revisit.
5. Draw one architecture diagram (before and after, same scale, new parts accented) and one chart that proves the after claim. Put them first in `why_visual`.
6. Keep prose short: the frame carries the argument; decisions, motivation detail and wave tables are reference material.

The plan page reads, top down: the Before → After band with focus and scope chips; the architecture drawing and the supporting chart; "How the work maps to the goal" (one row per objective, with section chips that link to the Execution view); "Why this path" (choice, why, trade-offs, the alternatives table with the chosen row highlighted); then the existing page from end state onward. Decisions, motivation detail and wave/window tables sit collapsed behind disclosures. `ws validate` reports the frame lint as PLAN201–PLAN206 warnings (exit stays 0): missing sentences, long or numberless sentences, orphan sections, objectives with no section, unknown section ids, and thin or choiceless alternatives. The naming rule applies to every `frame` string.

`dag.cols` is the column titles, left to right. Each section (a `dag.groups` entry) has `id`, `col`, `title`, and `tasks`. Each task has `id`, `title`, and `status`. A task may carry `deps` (task ids it waits on) and `dep_notes` (a map from one of its dep ids to the short condition on that edge, e.g. "run only after the export"). A task that is an outside signal or condition rather than work in this workstream carries `"kind": "gate"` (default `"task"`); the map draws it as a diamond. Edges are pairs of section ids and mean "this section waits on that one". A section may carry `"phase": "planning"`; put research, review, and frozen-design groups there so they show on the map but never count toward progress, waves, critical path, the ready queue, or progress-over-time.

A task may declare `"files": ["api/x.py", "api/*.py"]`, the file footprints it touches. `ws waves` splits a dependency level so no two tasks in one wave share a file, and `ws overlap` warns while colliding tasks are still open. Keep the globs tight: a literal path beats a wildcard.

A gate may carry `"signal"` (what it waits on, e.g. "the freeze lifts"), `"confirmed_at"` (ISO-8601 UTC of the last check, e.g. `"2026-10-01T12:00:00Z"`), and `"fresh_hours"` (recheck bound, e.g. `24`). `ws gates` reports STALE when `confirmed_at` is missing or older than `fresh_hours`; treat a stale gate as unready before launching its dependents.

A deferred task may carry `"received_as"` (the task id in the receiving workstream that picked it up, e.g. `"WS6.3"`). `ws deferred` matches by `received_as` first, then by title similarity, and flags deferrals nobody picked up as orphans.

`narrative.thesis` is one sentence. `narrative.end_state` is what is true when the workstream is done. `narrative.phases` is `name`, `goal`, and `gate`.

Hand-made drawings go in `visuals.html` as `<section class="pl-sec lay"><h2>Exact title</h2>...</section>`. `layout.json` lists those h2 titles under `why_visual`, `end_body`, `plan_extra`, `agent_visual`, and `ref_sections`. A string that starts with `<` is inserted as HTML. A title is looked up in `visuals.html`.

```
python3 agent_dash.py ws render WS4
python3 agent_dash.py ws validate WS4
```

`ws render` rewrites `page.html`. `ws validate` checks the schema and the naming rule. Do not cite a decision, task, pull request, ADR, or issue by number alone. Write a short phrase first. The number can sit in parentheses after it. `the read-path migration decision (D-12)` is the shape. The checker flags a TU-style id, a #-prefixed issue number, or an ADR number when no phrase sits earlier on the same line. It looks after the last tag, so a class name earlier in the tag does not count as the phrase.

Open `http://127.0.0.1:8765/plan?ws=WS4`. Overview is first, then bundles in order. Plan | Execution keeps the same workstream and does not reload the page. Execution is the dashboard view in the same shell.

`fixtures/bundles/` in this repo is a tiny OVERVIEW plus one workstream, for tests. Point `AGENT_DASH_WORKSTREAMS` at that directory when you want the plan page to render with no private data.

## Worktrees and test scope in a plan

Every lane runs in its own worktree and is removed when its task finishes. Write the plan so that holds:

- One worktree per lane, created from origin/main. When a lane's task reaches `prog` or `done`, or is dropped, its worktree goes too: `ws task` already runs worktree gc on each of those moves, so removing is the default, not a chore. Leads also run `ws gc <WS>` at each milestone and before closeout; `ws gc <WS> --dry-run` lists what would go first.
- Acceptance probes name explicit test files, one lane's files per probe. A train `gate` names the tests for the files two or more members both touch, re-cut from member diffs as they land.
- A plan never contains a full-suite gate: no bare `pytest`, no `--full`, no argument-free `lor-ci-local.sh` / `ci-local.sh`, no `scripts/validate.sh`, no `pre-commit run --all-files`, no `gh workflow run` / `gh run rerun`. `ws validate` flags any of them as a PLAN207 warning (exit stays 0) naming the task or train and the fix.

## Map checklist

The plan-progress map stays readable when the plan follows these nine rules. Run `ws layout <ID>` before kickoff and aim for zero MAP warnings. `ws validate` reports the same MAP101–MAP111 codes as warnings (exit stays 0); each names its threshold, the actual number, and the fix.

1. Lay out 4 to 5 columns with one- or two-word plain names. Never more than 5 (MAP102), never an empty column (MAP103).
2. Keep 2 to 4 sections per column and 3 to 12 tasks per section. Merge 1-task sections into their column's section (MAP109); split sections above 14 tasks (MAP101).
3. Title each section with the outcome in plain words (at most 40 chars), never a bare id (MAP105).
4. Keep task id suffixes short and unique inside the workstream (at most 12 chars, MAP110). The map strips the `WS<n>.` prefix and caps display at 14 chars.
5. Give every section outside the first column a waits-on edge to a section on its left, so columns tell the left-to-right story (MAP106).
6. Mark outside signals as `kind: gate` diamonds with `signal`, `confirmed_at`, and `fresh_hours`. Keep gate ids short.
7. Keep trains at 2 to 8 members. A 1-member train renders untagged (MAP107); keep a task note instead.
8. Put one short condition (at most 80 chars) in `dep_notes` on each cross-section dep. Above 40 task edges the map defaults to on-focus arrows (MAP108).
9. Keep done tasks in the dag. Leave deferred tasks where planned with `moved_to` plus `note`; the map folds them to one chip (above 20% deferred is MAP111, info only).

## Move a task

Label the run `ws4-<task-id>-<slug>`, for example `ws4-WS4.1-read-route`. The label must hold the full task id or the progress map cannot pin it. The progress map matches that label to the task id.

When the status changes:

```
python3 agent_dash.py ws task WS4 WS4.1 pr
python3 agent_dash.py ws task WS4 WS4.1 deferred --moved-to WS6 --note "waits on the cutover gate"
```

`pr` is an open pull request. `prog` is merged and waiting on a prod step. `done` is finished. `todo` is not started. Deferred tasks stay on the map and drop out of the counts. The write is a temp file plus replace, so a crash does not leave a half-written plan.

When the workstream is linked to Linear, the same command also queues the change for Linear. See [docs/linear.md](docs/linear.md).

Every status change also appends one `{ts, task, from, to}` line to `history.jsonl` in the bundle (a single flushed append, so one line is atomic; no change writes no line). That file is the recorded history `GET /api/replay` prefers over inferred times: the replay plays real transitions once they exist instead of guessing them from run labels.

Run `ws render` again only when the prose or the drawings change. Status edits do not need a render. The page reads `plan.json` live.

## Workstream lifecycle

A bundle has a lifecycle state in `bundle.json`: `planned`, `active`, `paused`, `done`, `archived`, or `dropped` (default `active`, so every existing bundle keeps working). The state moves with the per-bundle lock, and every move appends one `{kind: "state"}` line to `history.jsonl` next to the task lines, so replay and the feed see lifecycle moves without breaking on them.

```
python3 agent_dash.py ws new WS4 "Read path" --state planned
python3 agent_dash.py ws state WS4 active
python3 agent_dash.py ws state WS4 paused
python3 agent_dash.py ws state WS4 done
python3 agent_dash.py ws archive WS4
python3 agent_dash.py ws restore WS4
python3 agent_dash.py ws list --state all
```

Legal moves: `planned -> active`; `active -> paused|done|dropped`; `paused -> active|dropped`; `done -> archived|active` (reopening to `active` needs `--force --note`); only `ws restore` leaves `archived` (back to `done`); `dropped` is terminal. An illegal move exits 2 naming the legal moves. A planned bundle has no tab yet and waits on its `depends_on` workstreams; the lead activates it at kickoff with `ws state <ID> active`.

Archiving is manual: a human or the lead runs `ws archive` after the closing report. It refuses unless the plan is 100% done of non-deferred tasks (or `--force --note "..."`) and unless the bundle holds `docs/closing-report.md` (or `--force`). It freezes `summary.json` in the bundle — started/finished dates, duration, task counts by status, trains, approvals counts, and lifetime run/token/cost totals from the dashboard API when reachable (nulls with a note when not) — then sets state `archived`. Nothing is deleted; `ws restore` reopens to `done` and keeps `summary.json` as the previously-archived record. `ws list` hides archived and dropped bundles by default; `--state` filters (`planned,active,paused,done,archived,dropped`, comma-separated, or `all`), `--json` prints the rows. The dashboard page offers the same moves as real buttons (`POST /api/workstreams/archive|restore`, the only write action) behind an inline confirm and an Undo toast, local-only with a read-only switch; when writes are off the buttons fall back to copying the CLI commands above.

`bundle.json` may also carry `depends_on` (workstream ids that must exist; never `OVERVIEW`), `milestone` (a phase name), `state_changed_at`, and `archived_at`. `ws validate` rejects a bad value with exit 4 and names the next command.

## Merge trains

Related branches ship as one train branch (`train/<area>-<slug>`) and one PR, so CI runs once per set. The plan carries a provisional layout that the lead edits as diffs land. `ws new` writes an empty `"trains": []` for it.

`plan.json` holds an optional top-level `trains` list, the only source of truth. There is no per-task train field; task-to-train is derived from this list. Tasks in no train ship alone. A missing `trains` key is valid.

```json
{"id": "T1", "title": "read-path cutover", "branch": "train/api-read-path",
 "members": ["WS4.3", "WS4.12"], "status": "planned", "pr": 5357,
 "shared_files": ["api/x.py"], "gate": "pytest api/tests/test_x.py", "note": "..."}
```

- `id` is a machine key (`^[A-Za-z0-9._-]{1,40}$`), unique across the plan. `title` is a plain phrase and leads everywhere the train is named.
- `branch` is optional until assembling and must start with `train/`.
- `members` are task ids from `dag.groups[].tasks`. A task belongs to at most one train. At most 8 members per train.
- `shared_files` are files two or more members touch; they drive the train-level integration gate. `gate` is the one-line check for them.
- `status` moves like this:

| Status | Meaning |
|---|---|
| planned | proposed at planning, members may change |
| assembling | member branches being merged into the train branch |
| gate | train-level integration checks running |
| pr | train PR open |
| merged | on main |
| dropped | train dissolved, members reassigned |

```
python3 agent_dash.py ws train WS4 T1 --title "read-path cutover" --add WS4.3,WS4.12
python3 agent_dash.py ws train WS4 T1 --branch train/api-read-path --status assembling
python3 agent_dash.py ws train WS4 T1 --status pr --pr 5357
python3 agent_dash.py ws trains WS4
```

`ws train` creates the train when it is missing (`--title` is required then). `--add` moves a task out of any other train and says so. The write is a temp file plus replace, like `ws task`. `ws trains` prints a table: train, status, branch, PR, members with each member's task status, and the shared-files count.

Re-cut the trains as member diffs land: per member `git diff --name-only origin/main...<branch>`, intersect the lists into `shared_files`, set the `gate` to the tests for those files, and move the train status with `ws train`. Set `pr` when the train PR opens and `merged` after it lands. `ws validate` checks the schema, the member cap, and the naming rule on train titles and notes.

## Linear tracker

`plan.json` may carry an optional top-level `tracker` object. It holds the non-engineer text and dates that the Linear project shows. Every field is optional. The sync pushes it only for a linked bundle (see [docs/linear.md](docs/linear.md)).

```json
"tracker": {
  "summary": "Move the notes read path to Postgres, one table at a time.",
  "description": "Markdown for non-engineers: purpose, why now, what done looks like.",
  "lead": "Alex Rivera",
  "labels": ["Infrastructure"],
  "issue_labels": ["Infrastructure"],
  "priority": 2,
  "start_date": "2026-10-21",
  "target_date": null,
  "links": [{"title": "Dashboard", "url": "http://127.0.0.1:8765/?ws=WS4&view=exec"}],
  "milestones": {"Read path": {"description": "What this section delivers and its gate.", "target_date": null}},
  "update": {"health": "onTrack", "daily_hour": 9}
}
```

- `summary` is one line, at most 255 characters. `description` is Markdown.
- `lead` is the name of a Linear user.
- `labels` are project labels, created when missing. `issue_labels` are added to every task issue and are never removed by the sync.
- `priority` uses Linear's scale: 0 none, 1 urgent, 2 high, 3 medium, 4 low. Task priorities are derived from the plan (critical path high, ready now medium, other open tasks low), so they are not stored here.
- `start_date` and `target_date` are `YYYY-MM-DD`, or `null`.
- `links` is a list of `{title, url}`. The sync appends new links and does not remove old ones.
- `milestones` is keyed by DAG column name. Each entry has a plain-language `description` and a `target_date`.
- `update.health` is the default standup health: `onTrack`, `atRisk`, or `offTrack`. `update.daily_hour` is the local hour (0 to 23) for the automatic daily standup update. See `ws linear update` in [docs/linear.md](docs/linear.md).

The project status follows `bundle.json` `state`, so do not put a status in `tracker`. Name things in the text fields by a short phrase first, as in the rest of the plan.

## Approvals

The lead records each production decision and its rollback before running the step. The lead sets the status; an owner `denied` overrides it. The lead reads that status before any production step.

```
python3 agent_dash.py ws approve WS4 cutover pending --step "Ship the read path" --reversible no --conditions "The sample set loads."
python3 agent_dash.py ws approve WS4 cutover approved --note "Lead decided: sample set checked; rollback: revert the read path."
python3 agent_dash.py ws approvals WS4
```

`pending` adds or reopens an item. `approved` is the lead's decision with its basis; `denied` is the owner's override. `reversible` is `yes` or `no`. `no` is one-way. The write is a temp file plus replace.

`approvals.json` is optional. `ws validate` checks it when the file is there: `ws`, `decided_at`, `decided_by`, and `items` of `id`, `step`, `status` (`pending`, `approved`, `denied`), `reversible` (true or false), and `conditions`. The naming rule applies to that file too.

The dashboard does not display approvals (owner decision). The lead reads them with `ws approvals <ID>` before any production step.
