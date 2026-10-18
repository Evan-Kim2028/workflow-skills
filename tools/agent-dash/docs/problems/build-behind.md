# build-behind: the running dashboard is older than origin/main

The running build is a strict ancestor of `origin/main`: newer commits
landed upstream that this dashboard does not run. Advisory only: it
never fails the doctor.

## Why it matters

The dashboard keeps working, but it misses the fixes and features the
newer commits carry. Worth knowing, not worth paging.

## How to fix

Fetch first when the latest upstream commit is wanted, then ship a
clean release:

```bash
git fetch origin
python3 agent_dash.py deploy --ref origin/main
```

## How to verify

`python3 agent_dash.py status` prints the `origin/main` build id, and
the doctor no longer lists `build-behind`.
