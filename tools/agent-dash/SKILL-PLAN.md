# Plan bundles

A new session adds a workstream by writing a bundle, not by editing the dashboard. The tool code stays generic. The words live under `AGENT_DASH_WORKSTREAMS`, one directory per id. Default is `~/.local/share/agent-dash/workstreams`.

## Add a workstream

```
python3 agent_dash.py ws new WS4 "Read path"
```

That writes `bundle.json`, a template `plan.json`, `layout.json`, an empty `visuals.html`, and `START-PROMPT.md`. `ws validate WS4` should already pass.

`START-PROMPT.md` is the paste-ready prompt that starts this workstream's lead in a fresh session. Edit it so it names the kickoff brief and the done criteria. The bundle API and the plan API return that text as `start_prompt`. A missing file is null.

Fill `plan.json` before you render. The shape is the decision record: `dag`, `decisions`, `motivation`, `agent_friendly`, `narrative`. Task status is `todo`, `pr`, `prog`, `done`, or `deferred`. A deferred task can set `moved_to` to a workstream id and a `note`. Keep done tasks in the dag so the map shows progress.

`dag.cols` is the column titles, left to right. Each group has `id`, `col`, `title`, and `tasks`. Each task has `id`, `title`, and `status`. Edges are pairs of group ids and mean "this group waits on that one".

`narrative.thesis` is one sentence. `narrative.end_state` is what is true when the workstream is done. `narrative.phases` is `name`, `goal`, and `gate`.

Hand-made drawings go in `visuals.html` as `<section class="pl-sec lay"><h2>Exact title</h2>...</section>`. `layout.json` lists those h2 titles under `why_visual`, `end_body`, `plan_extra`, `agent_visual`, and `ref_sections`. A string that starts with `<` is inserted as HTML. A title is looked up in `visuals.html`.

```
python3 agent_dash.py ws render WS4
python3 agent_dash.py ws validate WS4
```

`ws render` rewrites `page.html`. `ws validate` checks the schema and the naming rule. Do not cite a decision, task, pull request, ADR, or issue by number alone. Write a short phrase first. The number can sit in parentheses after it. `the read-path migration decision (D-12)` is the shape. The checker flags a TU-style id, an issue number of 3 to 5 digits, or an ADR number when no phrase sits earlier on the same line. It looks after the last tag, so a class name earlier in the tag does not count as the phrase.

Open `http://127.0.0.1:8765/plan?ws=WS4`. Overview is first, then bundles in order. Plan | Execution keeps the same workstream and does not reload the page. Execution is the dashboard view in the same shell.

`fixtures/bundles/` in this repo is a tiny OVERVIEW plus one workstream, for tests. Point `AGENT_DASH_WORKSTREAMS` at that directory when you want the plan page to render with no private data.

## Move a task

Label the run `ws4-<task-id>-<slug>`, for example `ws4-1-read-route`. The progress map matches that label to the task id.

When the status changes:

```
python3 agent_dash.py ws task WS4 WS4.1 pr
python3 agent_dash.py ws task WS4 WS4.1 deferred --moved-to WS6 --note "waits on the cutover gate"
```

`pr` is an open pull request. `prog` is merged and waiting on a prod step. `done` is finished. `todo` is not started. Deferred tasks stay on the map and drop out of the counts. The write is a temp file plus replace, so a crash does not leave a half-written plan.

Run `ws render` again only when the prose or the drawings change. Status edits do not need a render. The page reads `plan.json` live.

## Approvals

The lead records a pending decision before asking the owner to take it. The owner sets the status. The lead reads that status before any production step.

```
python3 agent_dash.py ws approve WS4 cutover pending --step "Ship the read path" --reversible no --conditions "The sample set loads."
python3 agent_dash.py ws approve WS4 cutover approved --note "Owner signed off."
python3 agent_dash.py ws approvals WS4
```

`pending` adds or reopens an item. `approved` and `denied` are the owner's answer. `reversible` is `yes` or `no`. `no` is one-way. The write is a temp file plus replace.

`approvals.json` is optional. `ws validate` checks it when the file is there: `ws`, `decided_at`, `decided_by`, and `items` of `id`, `step`, `status` (`pending`, `approved`, `denied`), `reversible` (true or false), and `conditions`. The naming rule applies to that file too.

The plan view lists the rows under Approvals, after the header and before Why, grouped Needs you, Approved, and Denied. Execution shows a Needs you card at the top of Overall and of each workstream tab. A pending row links to that Approvals section. When nothing is pending, the card is one line, for example `WS2: 5 approved, 0 pending`.
