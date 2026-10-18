# build-dirty: the running dashboard was built from uncommitted changes

The running build id ends in `-dirty`: the dashboard started from a
checkout with uncommitted changes. A warning, so the doctor fails until
a clean release runs.

## Why it matters

A dirty running build may differ from what the team reviewed. Deploys
install from a clean worktree so the running code always matches one
commit.

## How to fix

Ship a clean release of the ref you want:

```bash
python3 agent_dash.py deploy --ref origin/main
```

Or roll back to the previous clean release:

```bash
python3 agent_dash.py rollback
```

## How to verify

`python3 agent_dash.py status` prints a build id with no `-dirty`
suffix, and the doctor no longer lists `build-dirty`.
