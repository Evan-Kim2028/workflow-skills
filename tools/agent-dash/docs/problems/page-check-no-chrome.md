# page-check-no-chrome: Chrome missing for check --page

No Chrome binary was found, so `check --page` always skips.

## Why it matters

Same as missing Node: the page comparison skips, the API checks stand.

## How to fix

Set `CHROME_PATH` to a Chrome binary, then run:

```bash
python3 agent_dash.py check --page
```

## How to verify

`check --page` drives Chrome instead of skipping.
