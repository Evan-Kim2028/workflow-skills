# dashboard-dirty-checkout: uncommitted changes in the tool checkout

The agent-dash checkout has uncommitted changes. Advisory only: it never
fails the doctor.

## Why it matters

A dirty tool checkout means the running code may differ from what the
team reviewed. Worth knowing, not worth paging.

## How to fix

```bash
git status --short
```

Review and commit, or leave it and move on.

## How to verify

A clean `git status --short` clears it on the next run.
