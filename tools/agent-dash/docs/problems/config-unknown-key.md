# config-unknown-key: config file has an unknown key

A config file names a top-level or `roots` key the dashboard does not
know.

## Why it matters

Almost always a typo. The setting is ignored, so the dashboard does not
do what the author thinks it does.

## How to fix

Remove or rename the key, then restart:

```bash
python3 agent_dash.py restart
```

## How to verify

`doctor` reports no unknown keys.
