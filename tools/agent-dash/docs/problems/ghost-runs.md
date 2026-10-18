# ghost-runs: recent runs mapping to no plan task

Runs from the last `doctor.recent_hours` (default 24 h) that map to no
plan task, in a workstream where at least `doctor.ghost_share`
(default 20%) of recent runs are unmapped. The detail names the labels.

## Why it matters

Unmapped runs are invisible progress: the tokens burn, but the progress
map shows nothing for them, and replay cannot place them.

## How to fix

Label every run with its task id (`ws4-WS4.1-slug`), or cover labels
that cannot hold the id with exact or `/regex/` entries in
`static/taskmap.local.json`, then reload. The problem detail lists the
labels to cover.

## How to verify

`doctor --ws WS4` no longer lists the workstream, and no rows sit above
the progress map as not mapped.
