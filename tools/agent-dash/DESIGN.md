# agent-dash design spec (keep stable)

The dashboard is read by a visual thinker. Every view answers its question with a chart or diagram first; text supports it. Change this file before changing the look, and keep every rule below unless this file changes.

## Layout

- Execution is `static/index.html`. Plan is `static/plan.html`, served at `/plan`. Vanilla JS, inline SVG. No framework, no CDN, no build step. Mermaid is the vendored file under `static/vendor/`.
- Header: title, live dot, range control (6 h, 24 h, 3 d), theme toggle. A Plans row links each workstream to its plan page.
- Tabs: `Overall`, then one tab per workstream that has a plan in the plans map or a workstream bundle, in WS number order.
- Overall, top to bottom: stat tiles (one row), running-now table beside alerts, workstream cards, tokens per hour beside live agents over time, run timeline, finished in the last hour.
- Workstream tab: the same views filtered to that workstream, plus the plan progress map directly under the tiles.
- Run details open in the right-hand drawer; Escape closes it.

## Color (reference palette, validated; do not invent hues)

- Categorical slots in fixed order: `--s1` blue, `--s2` orange, `--s3` aqua, `--s4` yellow, `--s5` magenta, `--s6` green, `--s7` violet, `--s8` red. Light and dark values are defined once in `:root` and the two dark scopes.
- Entity to color is fixed and never depends on rank: opus `--s1`, haiku `--s2`, sonnet `--s3`, grok `--s4`, muse `--s5`, glm `--s6`, other `--other`. Workstreams take slots in first-seen order from `WSCOL`.
- Status colors are reserved for state and never used as a series: good `--good`, warning `--warning`, serious `--serious`, critical `--critical`. A status color always ships with a word or icon.
- Text names things, not numbers: a decision, task, PR, issue or ADR is cited by a brief phrase, with its number optional in parentheses after it. Chips show a task id only beside its title or in its tooltip.
- Text uses ink tokens (`--ink`, `--ink-2`, `--muted`), never a series color.

## Charts

- Every chart has a visible x axis and y axis: an axis line, ticks with labels, and a short title or unit ("tokens", "agents", "time"). A categorical axis labels each category (lane or row names are its tick labels). Build axes only with the shared `axes()` helper in `static/index.html` (and its twin in `static/plan.js`); `tests/test_page_rules.py` fails a chart without it.
- Numbers are compact everywhere: counts and tokens through `fmtN` (Intl compact: 950, 1.2K, 3.4M, 1.5B), money through `fmtUsd` ($0.42, $12, $1.2K), durations through `fmtDur` (45s, 3m 10s, 2h 5m). No raw `toFixed` or `toLocaleString` on numbers outside those helpers.
- One y-axis per chart. Different scales get separate charts or small multiples (live agents over time is one strip per tool). There are no concurrency caps or meters: the owner removed them as unclear. Counts are always scoped to the current view.
- Bars and areas: 2 px surface gap between stacked segments, rounded ends, recessive grid and axes.
- Every chart with two or more series has a legend; every mark has a hover tooltip; bars and timeline rows open details on click.
- Timeline bars with file-time clocks (`clock_source = mtime`) are hatched, never solid.
- Plan progress map: groups in columns, arrows mean "waits on". Same component on every workstream tab. Task states, each with its own color AND symbol (never color alone), legend on the map:

  | State | Color | Symbol |
  |---|---|---|
  | agent working now | `--s1` blue, solid fill, pulse, tool badge | ● |
  | waiting to merge (open PR) | `--warning` amber | ⧗ |
  | merged, waiting on a prod step | `--s5` magenta | ⇡ |
  | done | `--good` green | ✓ |
  | finished in the last 15 min | green outline | (outline) |
  | not started | dashed gray | none |
  | deferred or moved to another workstream (`status: deferred`, optional `moved_to`, `note`) | dotted gray, faded | ↷ |

  Deferred tasks stay on the map where they were planned but leave every count and progress bar.

  Blue is reserved for "working now"; do not reuse it for any stored state. Violet is not used here (too close to blue side by side). Validated: CVD ΔE 14.7 light, 9.9 dark; amber is the reserved warning status color, relieved by the symbol and the task id.

## Behavior

- Read-only on every source. Never shows prompt bodies, argv, secrets or env values. Labels only.
- Refresh: SSE `tick` every 2 s triggers a snapshot refetch; everything else refetches every 30 s.
- `?fixtures` loads `../fixtures/*.json` for offline design work.
