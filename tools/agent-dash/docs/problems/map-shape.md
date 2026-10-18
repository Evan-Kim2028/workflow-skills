# map-shape: plan-shape warnings on the progress map (MAP101–MAP111)

A workstream plan renders on the progress map, but its shape makes the
map hard to read: a 47-task group is a chip wall, 7 columns need
horizontal scroll, 101 task deps draw unreadable arrow spaghetti. The
MAP101–MAP111 lint names these shapes at planning time so the planner
fixes the plan instead of the renderer hiding it.

## Why it matters

The renderer only collapses, shortens, caps, and folds; it never moves
a task to another group or a group to another column. A plan that keeps
its warnings renders truthfully but stays heavy: tall tiles, folded
rails, and on-focus arrows. The warnings are the cheaper fix.

## How to fix

Run the layout and work the list from the top:

```bash
python3 agent_dash.py ws layout WS4
python3 agent_dash.py ws validate WS4
```

`ws layout` prints the computed density, columns, group states, label
shortening, and arrows policy, then the MAP warnings with fix hints.
`ws validate` prints the same warnings after any schema errors without
failing the exit code. Aim for zero MAP warnings before kickoff. The
authoring checklist lives in `SKILL-PLAN.md` ("Map checklist"): 4 to 5
stage columns, 3 to 12 tasks per group, plain-words titles, short ids,
waits-on edges between stages, 2 to 8 members per train.

## How to verify

`ws validate WS4` prints no `warning MAP` lines, and `ws layout WS4
--json` carries an empty `map_warnings` list.
