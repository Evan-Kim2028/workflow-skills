# scratch-root-missing: configured scratch root matches nothing

A `roots.scratchpads` entry matches no directory.

## Why it matters

The collector scans nothing there. A typo in config silently drops every
run folder underneath it.

## How to fix

Create the directory, or fix the entry:

```bash
python3 agent_dash.py restart
```

Then re-run `doctor`.

## How to verify

`doctor` lists the root as readable.
