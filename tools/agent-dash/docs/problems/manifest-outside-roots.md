# manifest-outside-roots: worker manifest outside the scanned roots

A live worker manifest (`muse-sessions.tsv` and family) sits outside the
scanned scratch roots and is not covered by the run-dir registry.

## Why it matters

The collector may never read the run: the worker stays invisible until
someone registers its folder.

## How to fix

Register the folder or cover it in config:

```bash
python3 agent_dash.py report start --label <label> --run-dir <dir>
```

`doctor --fix --yes` registers it for you.

## How to verify

`doctor` no longer lists the manifest path.
