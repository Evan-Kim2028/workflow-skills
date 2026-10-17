# config-unparseable: config file does not parse

A config file (`config.local.json`, the `AGENT_DASH_CONFIG` override) is
not valid JSON or not an object.

## Why it matters

The dashboard silently keeps the last good config, so the running setup
may not be the setup on disk.

## How to fix

Parse it, fix the syntax, and restart:

```bash
python3 -c "import json; json.load(open('config.local.json'))"
python3 agent_dash.py restart
```

## How to verify

`doctor` reports config files parse.
