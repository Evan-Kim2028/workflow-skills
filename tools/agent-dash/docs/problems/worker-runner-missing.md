# worker-runner-missing: worker runner script missing

`scripts/run_worker.py` is not where the tool expects it.

## Why it matters

No lane can launch without the runner.

## How to fix

Restore `tools/agent-dash/scripts/run_worker.py` from version control.

## How to verify

`doctor` reports the runner present.
