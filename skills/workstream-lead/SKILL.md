---
name: workstream-lead
description: Run a planned workstream end to end as its lead session. Read the bundle's kickoff brief, run each wave with the ws-wave workflow so cheap workers (muse, Grok, GLM) do the work, move task status so the dashboard tracks it, act only on approved production steps, coordinate with other leads, and hand off cleanly. Use when a session is told "you are the WS<n> lead", is given a START-PROMPT, or picks up a workstream mid-flight.
---

# Workstream lead

The lead plans, decides, verifies and reports. Workers do everything else. Related skills: `workstream-planner` builds the bundle; `workstream-dashboard` maintains the tool.

## Start (or resume)

1. Read `~/.local/share/agent-dash/workstreams/<ID>/KICKOFF.md`, including any addendum, then `approvals.json` and `docs/`.
2. Check reality before acting:
   - `plan.json` statuses;
   - open PRs and `git log origin/main`;
   - live workers on the dashboard (`/static/index.html?ws=<ID>`).

   A resumed workstream may already be partly done.
3. Message the other leads named in the brief for shared files and the production slot. Messages that change production load need the owner's approval, unless `approvals.json` already grants direct coordination.

## Each wave

1. Build the lane list from the execution plan. Lanes in one wave share no file or test file. Use `reviewFocus` for lanes that need explicit checks, and `oneWay: true` for irreversible or production-adjacent lanes.
2. Run the `ws-wave` workflow. Use the Workflow tool with `scriptPath: <abs>/agent-dash/workflows/ws-wave.js`; the args are in the brief. Tiers come from the worker registry: `default` (muse), a named tier, or `auto` for failover by availability.
3. Read the result:
   - `approved`: assemble trains and merge with the repo's merge tool.
   - `needsLead`: read the last review and decide. Fix, re-scope, or defer with `ws task <ID> <task> deferred --note ...`.
4. Move status with `python3 ~/.claude/tools/agent-dash/agent_dash.py ws task <ID> <task> <todo|pr|prog|done|deferred>`, as soon as each fact changes. The dashboard and the Plan page read it live.
5. Start the next wave as soon as its start condition holds. Don't wait on lower wave numbers.

## Production steps

- Act only on items marked `approved` in `approvals.json`, and follow their conditions. Run one production step at a time across all workstreams that share the slot.
- For a new production step, record it as pending: `ws approve <ID> <id> pending --step ... --reversible ... --conditions ...`. Tell the owner it appears under "Needs you", and wait.
- Waits (deploys, soaks, shadow windows) go to a background watcher or a cheap worker that returns one verdict. Never sleep in the lead thread.

## Labels, logging, tokens

- Run labels are `<id lowercase>-<task-id>-<slug>`, plus `-rev<n>` and `-fix<n>` for reviews and fixes. The dashboard pins each run onto its task chip and counts attempts.
- Launch workers only through the tool's launchers (`scripts/run_worker.sh`, or `ws-wave`), which log billed tokens and session manifests. Never launch Grok with `--output-format text`.

## Hand-off

At about 250K context, or at a milestone, write `state.md`: done, in flight (labels and PIDs), next wave, blockers and messages owed. Then end. A fresh lead resumes from the brief plus `state.md`.

## Rules

- **Name things, not numbers:** in anything written for the owner, put a short phrase first; the number can follow in parentheses.
- **Use the repo's local scoped tests and merge tool.** Never wait on remote CI to prove a change.
- **The owner's instructions in the session win** over the brief when they differ.
