# owner-queue: items an agent cannot do

`reports/<WS>/owner-queue.md` under the hub lists the things an agent cannot
do itself: a sudo step, a missing credential, a paid upgrade. The doctor
raises one `owner-queue` info item per workstream that has lines in it. It is
info only: it never pages and never turns the doctor check red.

Production decisions are not in this queue. The lead logs each production
step with `ws approve` and decides it, so nothing waits on the owner for a
decision (the old `approval-stale` problem is gone).

## Why it matters

The queue is the one place agent-owned work reaches the owner. Keeping it to
things only the owner can do keeps the owner's time for those items.

## How to fix

Read the file, do the step if you can, and clear the line once it is done:

```bash
cat <hub>/reports/WS4/owner-queue.md
```

Remove a line when the owner has acted on it. An empty file, or one with only
headings and blank lines, raises nothing.

## How to verify

`python3 agent_dash.py doctor` lists no `owner-queue` item for the workstream.
