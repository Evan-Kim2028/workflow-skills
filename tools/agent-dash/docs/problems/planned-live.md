# planned-live: planned work with runs behind it

A workstream whose state is still `planned` already has runs recorded
against it: the first agent started, but no one ran
`ws state <ID> active` yet. The dashboard never writes bundles, so the
state stays `planned` until a human moves it. The Project home shows
the workstream under Current with a "first agent started" hint until
then.

## Why it matters

Work that already started keeps sitting on the Planned shelf and never
gains a tab. The next reader cannot tell whether the plan is waiting
or already running.

## How to fix

Activate the workstream at kickoff:

```bash
python3 agent_dash.py ws state WS4 active
```

## How to verify

`ws list --state active` names the workstream, and `doctor --ws WS4`
no longer lists it.
