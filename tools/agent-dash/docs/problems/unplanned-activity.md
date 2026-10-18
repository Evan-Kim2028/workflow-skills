# unplanned-activity: runs with no plan bundle

A workstream id has runs recorded but no plan bundle on disk. One
finding per id (`unplanned-activity:ws:<ID>`): the title reads
"Active without a plan: <ID>", the detail gives the run count, the
first run time, and the exact creation command.

## Why it matters

Runs without a bundle are invisible progress: tokens burn, but no plan
page explains what the work was for, and replay cannot place the runs.

## How to fix

Create the bundle with the named command:

```bash
python3 agent_dash.py ws new WS345 "Work for WS345"
```

An id without a workstream number (a shared tool or planning lane)
reads the same way: create it with `ws new`, then, when it is truly
shared rather than one workstream, set `"kind"` to `"program"` in
`bundle.json` after creation. There is no ignore list: every active id
either gains a bundle or stays listed here.

## How to verify

`ws list --state all` names the id, and `doctor --ws <ID>` no longer
lists it. The Project home "Unplanned activity" strip clears with it.
