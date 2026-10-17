# python-too-old: Python older than the runner needs

The running Python is older than `scripts/run_worker.py` requires (3.9+).

## Why it matters

Worker launches from this interpreter may fail on syntax the runner
needs.

## How to fix

```bash
python3 --version
python3 agent_dash.py status
```

Re-run the dashboard with a newer `python3`.

## How to verify

`doctor` reports the interpreter as fine.
