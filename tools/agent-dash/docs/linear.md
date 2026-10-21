# Linear as a task backend

A workstream can keep its tasks in Linear. Linear then shows the same tasks, status, and approvals as the dashboard, and the phone and the cloud agents see them too. The plan in the bundle stays the source of truth for what each task is. Status and approvals sync both ways.

Local mode is the default. A workstream with no `linear.json` in its bundle behaves as it always has and makes no network call.

## Local or Linear

| Situation | Use |
|---|---|
| One person, one laptop, and the dashboard runs all day | Local. |
| Several people, or a phone needs the status | Linear. |
| Cloud agents or remote sessions need to see tasks | Linear. |
| Status should move when a pull request opens or merges | Linear. |
| You want no outbound network call at all | Local. |

## What changes and what does not

Agents and skills keep using the same commands: `ws task`, `ws train`, and `ws approve`. Nothing changes for them. In a linked workstream, each of those writes also queues a change for Linear. The next sync sends it.

Status commands are unchanged when a workstream is linked. `ws task` still writes the bundle first. Linear receives the change within a minute.

## Setup

1. Create a personal API key. In Linear, open Settings, Security & access, Personal API keys, and create one.
2. Export the key in the shell that runs the dashboard and the CLI:

   ```bash
   export LINEAR_API_KEY="<your key>"
   ```

3. Start the dashboard from that shell, or restart it so it reads the key:

   ```bash
   python3 ~/.claude/tools/agent-dash/agent_dash.py restart
   ```

4. Link the workstream to a Linear team. The team key is the prefix of its issue ids, such as `ENG`:

   ```bash
   python3 ~/.claude/tools/agent-dash/agent_dash.py ws linear link WS4 --team ENG
   ```

`link` refuses to run when `LINEAR_API_KEY` is unset. The error names the variable.

`link` creates these objects in Linear:

- An initiative, named from the project name in `project.json`, or from the bundle title when there is no project file. One initiative covers the whole project.
- A project for the workstream, with the workstream title as its name.
- One milestone per DAG column in `plan.json`.
- One issue per task, with the rendered spec as its description.
- A `blocks` relation for each task dependency.
- An "Awaiting prod" state on the team. It has the started type and the color `#a855f7`.
- An `approval` label on the team.
- One issue per pending approval in `approvals.json`.

`link` prints the project URL. It also writes `linear.json` to the bundle, and it writes the workspace ids to `linear.json` in the data root.

## Mapping

| agent-dash | Linear |
|---|---|
| Project (`project.json`) | Initiative |
| Workstream bundle | Project |
| DAG column | Project milestone |
| Task | Issue |
| Task dependency | `blocks` relation |
| Task spec: acceptance, files, test command, rollback, wave, size, train, dependency notes | Issue description, rendered from the plan |
| Pending approval | Issue with the `approval` label |
| Train | Not mirrored as a label. The train name appears in the description. |

## Status

| agent-dash status | Linear state | Linear type |
|---|---|---|
| `todo` | Todo | unstarted |
| `wip` | In Progress | started |
| `pr` | In Review | started |
| `prog` | Awaiting prod | started |
| `done` | Done | completed |
| `deferred` | Canceled | canceled |

Approvals use the same issue states:

| Approval | Linear state |
|---|---|
| `pending` | Todo |
| `approved` | Done |
| `denied` | Canceled |

When Linear holds a state name that the table does not list, the poller maps it by state type. For example, Backlog reads as `todo` and Duplicate reads as `deferred`.

## Daily flow

- **Status changes from either side.** Run `ws task` in the terminal, or move the issue in Linear. Both reach the other side.
- **The poller syncs every minute.** Each cycle sends queued local changes first, then pulls Linear changes. The poller runs inside the dashboard.
- **Conflicts.** A local change that has not reached Linear yet wins; it is sent on the next sync. Otherwise the most recent change in Linear wins.
- **Edit the spec in the plan.** Do not edit an issue description in Linear. Each push rewrites the description from `plan.json`, so a Linear edit to the description is lost. Status edits in Linear are kept.
- **Put the Linear links in the pull request.** Run `ws trains <WS>`. For each train, a linked workstream prints a `Fixes ENG-…` string that names its member issues. Paste it into the train PR body. This needs Linear's GitHub integration connected to the repository. By default Linear moves linked issues to In Progress when the PR opens and to Done when it merges; set the team's pull request automation to move them to In Review on open so they match the dashboard's `pr` status.
- **Reopen from Linear.** Moving an issue back from Done or Canceled to an open state reopens the task locally. The next sync applies it.
- **Deleted issues.** If an issue is deleted in Linear, the next push for its task recreates it under the same id. The task is not detached.
- **Approve from Linear.** Move the approval issue to Done to approve it, or to Canceled to deny it. The next sync writes the answer to `approvals.json`. Anyone with access to the Linear team can approve or deny by moving the issue, not only the owner. Keep approval issues in a team limited to people who may approve. A decision made in Linear is recorded with `decided_by` set to `owner`, the same as a decision made in the terminal.
- **Approve from the terminal.** `ws approve` still works. It queues the change for Linear.

## When the dashboard is not running

The poller runs only inside the dashboard. When the dashboard is stopped, run one sync by hand:

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws linear sync WS4
```

The sync pushes the spec when it changed, sends queued status changes, and pulls Linear changes. It does not start a poll loop.

To check the link:

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws linear status WS4
python3 ~/.claude/tools/agent-dash/agent_dash.py ws linear status WS4 --json
```

`status` shows the project, the time of the last successful sync, the last error, and the number of queued changes.

## Switching back to local

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws linear unlink WS4
```

`unlink` removes `linear.json` and the outbox from the bundle. The workstream returns to local mode. Linear keeps the project, the issues, and their current state. Those issues stop updating, and nothing else changes.

To link the workstream again later, run `ws linear link` again. It reuses the same Linear project and issues, because their ids are fixed by the workstream and task ids.

## What leaves the machine

Only plan content leaves the machine:

- task and milestone titles, and the rendered task specs;
- task status and approval text;
- the workstream and project names.

Nothing else is sent. Run labels, token counts, costs, prompts, and transcripts stay local. Paths appear only when the plan lists them as task files. The API key lives only in the environment. It is never written to a file, a log, or an error message.

The Linear poller is the dashboard's one background network call. It runs only for linked workstreams, and it is off under `AGENT_DASH_READONLY=1`. The `ws linear` commands are explicit CLI actions and still run under that switch.

## Troubleshooting

The dashboard doctor reports three Linear problems. It names each one in the CLI and the API output. The page does not show them.

| Code | Meaning | Page |
|---|---|---|
| `linear-key-missing` | A workstream is linked, and `LINEAR_API_KEY` is not set in the dashboard's environment. | [problems/linear-key-missing.md](problems/linear-key-missing.md) |
| `linear-sync-failing` | The last sync failed, and the last good sync is more than 10 minutes old. | [problems/linear-sync-failing.md](problems/linear-sync-failing.md) |
| `linear-outbox-backlog` | Queued changes are more than 10 minutes old, or there are more than 50 of them. | [problems/linear-outbox-backlog.md](problems/linear-outbox-backlog.md) |

Run `python3 ~/.claude/tools/agent-dash/agent_dash.py doctor` to see them.
