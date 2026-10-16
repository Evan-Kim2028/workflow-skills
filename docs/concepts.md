# Concepts

How the pieces fit together. Procedures live in the [quickstart](quickstart.md). File names and variables live in [configuration](configuration.md).

## Workstreams

A workstream is one stream of work with a stable id, such as WS1 or WS4. The Execution view has an Overall tab and one tab per workstream that has a plan file or a bundle whose `kind` is `workstream`. A program bundle, `kind` set to `program`, shows on the Plan view and does not add an execution tab. The shipped sample uses OVERVIEW for that role.

Tabs from the plans map and tabs from bundles are the same kind of tab. The plans map is how the shipped sample plans appear before you create a bundle. A bundle does not need a plans-map entry.

The header stays put. Plan and Execution swap the main view and keep the same workstream. The URL is `?view=plan&ws=WS4` or `?view=exec&ws=WS4`. http://127.0.0.1:8765/plan?ws=WS4 opens the plan view in that same shell.

## Bundles

A bundle is a directory of the words for one workstream. The default root is `~/.local/share/agent-dash/workstreams/<ID>/`. Override it with `AGENT_DASH_WORKSTREAMS`. The tool directory stays free of that text, which is why bundles are not in this repository.

`ws new` writes four files:

| File | Role |
|---|---|
| `bundle.json` | Id, title, short label, order, and `kind`. `kind` is `workstream` or `program`. |
| `plan.json` | Decisions, motivation, the task graph, and the narrative. The page reads this file live. |
| `layout.json` | Which drawings from `visuals.html` go in Why, End state, Plan, Agent-friendly, and Reference. |
| `visuals.html` | Drawings, as sections the renderer can find by their `h2` title. |

`ws render` writes `page.html` from those files. Render again when the prose or the drawings change. A task status edit does not need a render.

`ws approve` creates or updates `approvals.json`. The file is optional. `ws validate` checks it when it is present.

The planner skill also asks for an execution plan, `KICKOFF.md`, and `START-PROMPT.md` inside the bundle. The `ws` commands do not create those. They are the lead's brief, written by the person or session doing the planning.

`plan.json` is a decision record and a task graph. `dag.cols` is the column titles, left to right. Each group has an id, a column index, a title, and tasks. Each task has an id, a title, a status, and `deps`: the ids of the tasks it waits on, in any group. A task may also name its `wave`. An edge is a pair of group ids. In the shipped sample plans the pair points from the earlier group to the later one, and the later group waits on the earlier one. The map draws that line. Task `deps` are finer: inside a tile, chips run left to right in the order they can start, and hovering a chip draws lines to the tasks it waits on (amber) and the tasks it unblocks (blue), across tiles. Click a chip to pin it. Plan at task level: every re-plan updates `deps`, and `ws validate` fails on a dep that names an unknown task or the task itself.

## Task states and the progress map

A task status is one of five words.

| Status | Meaning on the map |
|---|---|
| `todo` | Not started. |
| `pr` | Waiting to merge. An open pull request. |
| `prog` | Merged, waiting on a production step. |
| `done` | Finished. |
| `deferred` | Left the active count. Optional `moved_to` is a workstream id such as WS6. Optional `note` says why. |

Deferred tasks stay on the map and drop out of the done-of-total count. `ws task` is the writer. The write is a temp file plus replace. `--moved-to` and `--note` are accepted only when the status is `deferred`.

The map is one board of columns. Each group is a card of chips. A chip with a running agent uses a solid pulse and a badge with the tool color and the count. A chip whose run finished in the last 15 minutes gets a green outline, unless an agent is still running on it. A `↻` count appears when more than one run in the current range mapped to that task. The edge of that count turns amber when two or more of those runs were sent back. Sending back means a verdict of `REQUEST_CHANGES`, `APPROVE_WITH_CHANGES`, `PARTIAL`, or `BLOCKED`.

Runs that are running on this workstream and match no task are listed above the map. For WS4 that list is the signal that the task map does not cover the label. WS1, WS2, and WS7 also have matchers built into the page. Every other workstream depends on `taskmap.local.json`.

Hover a chip for the task id, title, and status. The id alone is not the label. The title is.

## Run labels and attribution

A wave label looks like `ws4-WS4.1-notes-index`. The workstream id is lowercased, then the task id, then a slug. Reviews append `-rev1`, `-rev2`, and so on. Fixes append `-fix1`, `-fix2`, and so on. The first line of a worker prompt is `[label: that-label]`. The launchers and the process sampler use that token. The prompt body is not stored.

Attribution picks the workstream in three steps. An exact label wins. Then the regex list. Then, if the label matched nothing, the session table. A child run with no label match uses its parent's session mapping and is marked `session-default`. Confidence on the run is `label`, `regex`, or `session-default`.

The shipped map sends a label that starts with `ws` and digits to `WS` plus those digits, so `ws4-WS4.1-notes-index` is WS4 before you edit anything. Exact keys and session ids are for labels that do not follow that shape.

The task map is a second step. It runs only after the workstream is known. A key is an exact label, or a regex wrapped in slashes, such as `"/^ws4-ws4\\.1/"`. The page compiles that pattern with case insensitive matching. The value is the task id in `plan.json`, which must match exactly, including the `WS4.1` form.

`run_worker.sh` appends a TSV row: label, session id, started epoch, and model when the worker's columns include it. muse rows go to `muse-sessions.tsv`. Grok and Composer rows go to `cursor-sessions.tsv` with a dash in the session column, because the chat id shows up later from the transcript. GLM rows go to `glm-sessions.tsv` with the model in the fourth column.

Collectors find those TSV files under a scratch root: either the file sitting directly on the root, or `<scratch-root>/<encoded-cwd>/<session-uuid>/scratchpad/<file>`. The shipped scratch glob is `$HOME/agent-scratch/*`. A run directory at `$HOME/agent-scratch/notes` matches that glob. `$HOME/agent-scratch/notes/runs` does not, because the glob is one directory deep. Put the TSV on a scratch root, or add the deeper path to `roots.scratchpads`.

## Approvals

An approval is a production step the owner has to answer before the lead acts. The lead adds it as `pending`. The owner sets `approved` or `denied`. The lead reads `ws approvals` before the step.

Each item has an id, a step sentence, a status, a reversible flag, and conditions. `reversible no` is one-way. The dashboard does not show approvals; the lead reads them with `ws approvals <ID>` before any production step.

`oneWay` on a lane requires a non-empty `reviewFocus`. The workflow still reviews that lane when `review` is false. The workflow never takes the production step. The lead does, and only from an `approved` item.

## The worker registry

Workers come from a registry: `tools/agent-dash/workers.example.json`, replaced on one machine by a gitignored `workers.local.json` (or `AGENT_DASH_WORKERS`). Each worker has a kind (`muse`, `cursor`, or `grok-build`), a model, a concurrency cap, and quota patterns. Shipped tiers are `muse`, `grok`, `composer` (`composer-2.5`), `glm`, and `glm-flash`. A lane asks for `default` (the registry default), a worker name, or `auto`. `auto` takes the first worker in `auto_order` that is under its cap and not cooling down. When a run fails on a quota error, that worker cools down and an `auto` or `default` lane relaunches on the next one. A cursor cap counts `cursor-agent` processes for that model only.

`ws-wave` reads tier and model per role. Build tier is `lane.tier`, else `defaultTier`, else `default`. Review tier is `lane.reviewTier`, else `args.reviewTier`, else the build tier. Fix tier follows the same pattern with `fixTier`. `model`, `reviewModel`, and `fixModel` are optional overrides passed to `run_worker.sh`.

- `muse` runs `muse exec` with model `muse-spark-1.3-contributor` and reasoning effort `xhigh`. It records a session id in `muse-sessions.tsv`.
- `grok` and `composer` run `launch_grok.sh`, which runs `cursor-agent` and appends one billed usage line. Grok's model string is `grok-4.7[context=500k,reasoning_effort=xhigh,fast=false]`. Composer's model is `composer-2.5`.
- `glm` and `glm-flash` run the Grok Build CLI (`grok -p`) with the registry model.

## A wave

One wave is a set of lanes that do not share a file or a test file. Independent lanes start at once. A lane with `deps` waits until each named task in this run has finished `APPROVE*` or `built`. Any other dependency result returns `skipped-dep` and names the dependency. `base` is `origin/main`, `dep:<task>`, or any other existing branch or ref. `dep:<task>` cuts the worktree from that task's branch. Any other ref cuts the worktree from that ref. Unknown ids and cycles throw before any lane starts.

For each lane that starts, the sequence is:

1. Mark the task `wip`, then create the git worktree if it is missing, on the lane branch, from `origin/main`, from the dependency branch when `base` is `dep:<task>`, or from the other ref named in `base`.
2. Build. The wrapper writes the prompt, runs `run_worker.sh`, and waits. A build that is blocked, dies, or has no commits stops the lane as `build-failed`.
3. Review, unless the lane sets `"review": false` and is not `oneWay`. A passing review, verdict starting with `APPROVE`, moves the task to `pr` and stops the loop. The lane is `approved`.
4. Fix, then review again. `maxFixRounds` defaults to 2. When the last extra review still asks for changes, the lane is `needsLead`.

`wrapperModel` defaults to Haiku and `wrapperEffort` defaults to `low`. That is the Workflow tool's model for the wrapper. The worker is whichever registry tier the lane named. Each lane's `runs` list repeats the tier and model from the runner's STATUS line. A full lane example is in `tools/agent-dash/docs/workstream-lead.md`.

The lead merges `approved` branches, reads `needsLead`, and starts the next wave when its start condition holds. The workflow cannot wait out a multi-day window or receive a message from another session. Those stay with the lead.

## Token accounting

Two different numbers show up around a Grok run, and only one of them is a bill.

Billed counts are usage events. For Grok they come from `cursor-usage.jsonl` under the state directory. `scripts/cursor_usage_hook.py` appends one JSON line per turn when Cursor runs it as a stop hook. `launch_grok.sh` appends the line itself from the stream-json result, so a wave does not depend on that hook. Headless `--print` does not run the hook, and this kit does not launch Grok that way. A chat blob that already contains input and output token fields is recorded as usage too. The stream filter treats `inputTokens` as uncached input. The CLI has already subtracted cache read and cache write. Reasoning is null in that line because the printer drops it.

A checkpoint context-window estimate is a different event, tool `grok-context`. Input is tokens used, output is the window max, and reasoning is 0. Hourly buckets and per-run totals skip `grok-context`. Those estimates do not enter the cost tile or the token chart. Leaving them in would bill a window size as if it were generated tokens.

Cache is stored beside input. The database keeps cache read in its own column.

- Claude keeps cache read and cache write separate from input. Cache write can split into a 5 minute TTL and a 1 hour TTL. `python3 agent_dash.py reprice` rereads Claude project transcripts to fill that split when the split is missing, then recomputes estimated cost.
- muse and GLM store an input count that already includes cached reads. The catalog row sets `input_includes_cache_read`. The pricer subtracts cache read from input, prices the remainder at the input rate, and prices cache read at the cache rate. If cache read is larger than input, the cost is left unset.
- Grok's billed input is already uncached, so the pricer does not subtract.

Rates live in `tools/agent-dash/catalog/prices.json` as USD per million tokens. A model with no input rate or no output rate does not become zero. The run stays unpriced, and the cost tile counts unpriced runs. A missing price shown as `$0` would hide the gap. `billing_mode` on a catalog row is `api`, `plan-included`, `subscription`, or `unknown`. The estimator still applies the numeric rates when they exist. `plan-included` does not force the estimate to zero. That mode means the rate is the price after a pool. It is a catalog note.

When a catalog row includes `long_context`, a prompt longer than its threshold uses those higher rates. The shipped Haiku row does this for a prompt of more than 100000 tokens.

`python3 agent_dash.py reprice` prints a JSON summary of the recompute. It does not change the catalog.

Every source stores **uncached** input. Claude and cursor-agent already report it that way. muse and the Grok Build CLI (GLM) count cached tokens inside their input, so agent-dash subtracts the cached part at ingest, and a one-time migration (`uncached_input_v1`) fixes older rows. Totals are input + output + cache read + cache write with no double counting, and the price catalog prices each part once.

## Live counts

The live tile counts running agents.

The tool buckets are `grok`, `muse`, `glm`, and `claude`. A running run counts once per tool and label. A child Grok chat rolls its tokens onto the parent and does not add a second agent. An unlabeled muse row for a session that already has a labeled run does the same. Counting both would show two workers for one launch. The agent count skips source `claude-main` and source `shell`.

Finished today counts runs whose status is `done`, `killed`, or `died_early` and whose end time is after local midnight. The pass, partial, and blocked line on that tile uses the snapshot's verdict tally.

The Alerts tile counts runs that died early, were killed in the last hour, or are still running past a tier limit. The limits are haiku 20 minutes, sonnet 45 minutes, muse 45 minutes, grok 45 minutes, and glm 30 minutes. Opus has no entry in that table, so a long Opus run does not raise this alert.

Collectors poll on their own intervals. The claude, muse, cursor, grok_cli, and manifests sources poll every 2 seconds. cursor is the Grok chat source. grok_cli is the GLM source. Process samples poll every 5 seconds. The optional hub poll is every 60 seconds. The page opens a stream that ticks every 2 seconds and refetches the snapshot when the sequence number changes. Runs, usage, the plan, and approvals refresh every 30 seconds.

The range control on Execution is 6 hours, 12 hours, 24 hours, or 3 days. It filters the charts and the run list. It is hidden on the Plan view.

Click a row, a timeline bar, or a live chip for the details drawer. The drawer shows the label, model, workstream, confidence, timings, token classes, and estimated cost. Escape closes it.
