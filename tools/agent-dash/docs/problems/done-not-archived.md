# done-not-archived: done work waiting for its closing report

A workstream whose state is `done` — or whose state is `active` with a
plan at 100% of non-deferred tasks — has been done for more than
`doctor.archive_nag_days` (default 7 days) without being archived.
Archiving is manual: a human or the lead runs `ws archive` after the
closing report. The doctor nags; it never archives.

Done-since is read from `state_changed_at` first, else the last
`history.jsonl` move, else the plan file mtime. When none of those
exists the check stays silent rather than guess.

## Why it matters

Done work that never archives keeps occupying the daily views and the
tab bar, and its summary card is never frozen. The closing report is
the claim "this is finished and summarised" — without it the next
reader cannot tell what shipped and what was deferred where.

## How to fix

Write the closing report in the bundle, then archive:

```bash
python3 agent_dash.py ws archive WS4
```

`ws archive` refuses when the closing report is missing and names the
exact filename it wants.

## How to verify

`doctor --ws WS4` no longer lists the workstream, and
`ws list --state archived` names it.
