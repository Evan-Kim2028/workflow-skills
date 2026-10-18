# Plan bundles

A new session adds a workstream by writing a bundle, not by editing the dashboard. The tool code stays generic. The words live under `AGENT_DASH_WORKSTREAMS`, one directory per id. Default is `~/.local/share/agent-dash/workstreams`.

## Add a workstream

```
python3 agent_dash.py ws new WS4 "Read path"
```

That writes `bundle.json`, a template `plan.json`, `layout.json`, an empty `visuals.html`, and `START-PROMPT.md`. `ws validate WS4` should already pass.

`START-PROMPT.md` is the paste-ready prompt that starts this workstream's lead in a fresh session. Edit it so it names the kickoff brief and the done criteria. The bundle API and the plan API return that text as `start_prompt`. A missing file is null.

Fill `plan.json` before you render. The shape is the decision record: `dag`, `decisions`, `motivation`, `agent_friendly`, `narrative`. Task status is `todo`, `wip`, `pr`, `prog`, `done`, or `deferred`. `wip` is work on a branch; `pr` is approved or open. A deferred task can set `moved_to` to a workstream id and a `note`. Keep done tasks in the dag so the map shows progress.

`dag.cols` is the column titles, left to right. Each group has `id`, `col`, `title`, and `tasks`. Each task has `id`, `title`, and `status`. A task may carry `deps` (task ids it waits on) and `dep_notes` (a map from one of its dep ids to the short condition on that edge, e.g. "run only after the export"). A task that is an outside signal or condition rather than work in this workstream carries `"kind": "gate"` (default `"task"`); the map draws it as a diamond. Edges are pairs of group ids and mean "this group waits on that one".

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

## Move a task

Label the run `ws4-<task-id>-<slug>`, for example `ws4-WS4.1-read-route`. The label must hold the full task id or the progress map cannot pin it. The progress map matches that label to the task id.

When the status changes:

```
python3 agent_dash.py ws task WS4 WS4.1 pr
python3 agent_dash.py ws task WS4 WS4.1 deferred --moved-to WS6 --note "waits on the cutover gate"
```

`pr` is an open pull request. `prog` is merged and waiting on a prod step. `done` is finished. `todo` is not started. Deferred tasks stay on the map and drop out of the counts. The write is a temp file plus replace, so a crash does not leave a half-written plan.

Every status change also appends one `{ts, task, from, to}` line to `history.jsonl` in the bundle (a single flushed append, so one line is atomic; no change writes no line). That file is the recorded history `GET /api/replay` prefers over inferred times: the replay plays real transitions once they exist instead of guessing them from run labels.

Run `ws render` again only when the prose or the drawings change. Status edits do not need a render. The page reads `plan.json` live.

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

## Approvals

The lead records a pending decision before asking the owner to take it. The owner sets the status. The lead reads that status before any production step.

```
python3 agent_dash.py ws approve WS4 cutover pending --step "Ship the read path" --reversible no --conditions "The sample set loads."
python3 agent_dash.py ws approve WS4 cutover approved --note "Owner signed off."
python3 agent_dash.py ws approvals WS4
```

`pending` adds or reopens an item. `approved` and `denied` are the owner's answer. `reversible` is `yes` or `no`. `no` is one-way. The write is a temp file plus replace.

`approvals.json` is optional. `ws validate` checks it when the file is there: `ws`, `decided_at`, `decided_by`, and `items` of `id`, `step`, `status` (`pending`, `approved`, `denied`), `reversible` (true or false), and `conditions`. The naming rule applies to that file too.

The dashboard does not display approvals (owner decision). The lead reads them with `ws approvals <ID>` before any production step.
