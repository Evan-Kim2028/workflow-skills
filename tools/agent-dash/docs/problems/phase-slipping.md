# phase-slipping: a finished phase with no follow-through

A `project.json` phase whose member workstreams are all `done` or
`archived` has no started successor phase within
`doctor.phase_slip_days` (default 7 days), or a phase passed its target
date with member workstreams still unfinished. Members are the bundles
carrying the phase name in `milestone`, or the phase's explicit
`workstreams` list when it names one. This is informational — a pause
between phases can be deliberate, but it should be a decision, not
drift.

## Why it matters

Phase boundaries are where momentum dies: the last lane merges, nobody
starts the next, and a week later the project has quietly stalled. The
target-date variant is the same failure with a date attached.

## How to fix

Review the project strip and start the next phase or re-plan the date:

```bash
python3 agent_dash.py project show
```

## How to verify

`doctor` no longer lists the phase: the next phase has a started
workstream, the target moved with a note, or every member is done.
