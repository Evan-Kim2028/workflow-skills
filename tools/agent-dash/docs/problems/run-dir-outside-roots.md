# run-dir-outside-roots: registered run folder outside the roots

A registered run folder holds a live worker manifest but sits outside the
configured `roots.scratchpads`. It is scanned through the registry, so
nothing is lost, but the config does not cover it.

## Why it matters

The next machine, or a config edit that drops the registry, silently
loses the run. Loud now beats invisible later.

## How to fix

Add the path to `roots.scratchpads` in `config.local.json`, then restart:

```bash
python3 agent_dash.py restart
```

`doctor --fix --yes` registers the folder, or adds the missing root to
`config.local.json` with a backup, for you.

## How to verify

`/api/health` `warnings` is empty for the folder.
