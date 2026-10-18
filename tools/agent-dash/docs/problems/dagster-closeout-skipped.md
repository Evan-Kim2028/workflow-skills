# dagster-closeout-skipped: finished runs may still show running

One or more Dagster polls saw a full active page (200 runs) and
skipped the close-out pass, counted on `/api/health` as
`dagster.skipped_closeouts`. Stored `running`/`queued` rows absent
from the active set were not fetched by id that poll.

## Why it matters

Finished runs can linger as running until a later poll with a
non-full page closes them out. Counts that read the running set stay
high in the meantime.

## How to fix

```bash
python3 agent_dash.py status
```

No action is usually needed: the next poll with room on the active
page closes the backlog out. If the count keeps climbing, the active
set is genuinely over the page limit and old runs need attention on
the Dagster side.

## How to verify

`/api/health` shows `dagster.skipped_closeouts` stable (not growing)
and the running set drains on later polls.
