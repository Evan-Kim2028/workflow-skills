# agent-dash design spec (keep stable)

The dashboard is read by a visual thinker. Every view answers its question with a chart or diagram first; text supports it. Change this file before changing the look, and keep every rule below unless this file changes.

## Layout

- One shell, `static/index.html`, served at `/` and at `/plan`. Vanilla JS, inline SVG. No framework, no CDN, no build step. Mermaid is the vendored file under `static/vendor/`.
- The header and the workstream tabs render once. Plan | Execution, and tab changes, swap the main view in place. The URL is `?view=plan|exec&ws=<ID>`. Back and forward restore that view. `/plan?ws=<ID>` and `/static/index.html?ws=<ID>` still open the right view. The selected workstream stays when the view changes.
- Header: title, live dot, Plan | Execution toggle, Project | Current | Planned | Archive switches, and the one Theme toggle at the far top right (`margin-left:auto`). Every windowed number uses one fixed rolling 24-hour window; there is no range control.
- Pinned chrome: while scrolled only one slim row stays pinned (project name, workstream tab strip; `--pinned-h`, 48 px desktop and 44 px phone). The live pill, both view switches, and the Theme toggle scroll away above it, so nothing resizes on scroll; section anchors carry a matching scroll margin.
- Project name is never truncated: `h1#title` keeps its natural width (`flex:none`, no `max-width`, no ellipsis) and the tab strip flexes and scrolls internally to give way. On phones it may wrap to two lines at a smaller step (13 px) inside the 44 px row, clipped without ellipsis.
- Theme decision (owner feedback): exactly one Theme control, a real `<button id="theme">` with `aria-label` and native keyboard support, sitting in the scrolling top row at the far top right — deliberately NOT in the pinned row, so it is never mistaken for part of the workstream tab strip. Trade-off accepted: it scrolls away with the top row instead of staying reachable while scrolled; keeping the pinned row to name plus tabs also keeps the rebase small for the workstream-switcher rebuild in the same topbar markup.
- No Sections button: the button, its styles, its click wiring, and its `[` shortcut hint are gone. The section dock stays as a desktop hover rail (the always-visible floating dock expands on CSS hover; Escape closes a `?nav=open` latch). The button was the rail's only UI entry on phones, so phones now have none: the phone shut-rail hiding stays live (verified: no rail in the 390 shots) and the phone `.is-open` overlay serves `?nav=open` only — no other dead phone paths remain.
- Tabs: `Overall` on Execution and `Overview` on Plan, then one tab per workstream that has a plan in the plans map or a workstream bundle, in WS number order. The right end of the bar holds an All | Active | Completed filter with counts (Overall always shows; the selected tab stays visible but muted when the filter would hide it), and workstream tabs drag left or right to reorder with a Reset order control; filter and order are remembered per viewer. Overall stays pinned first. Completed means total > 0 with done === total, the same rule the Where we are panel uses, read from the server progress object and marked with a ✓ plus words, never color alone.
- Colors, type, and shared controls (card, tab, pill, chip, legend, tooltip, section heading, details) come from `static/tokens.css`. Plan fragments keep their class names. `--accent`, `--fg2`, `--line`, `--ok`, `--warn`, `--crit`, `--surface`, and `--surface-2` are aliases of the reference palette.
- Execution, top to bottom (Overall): a "Where we are" panel (each workstream's status, % done from its plan, live agents, Plan / Live / Copy start prompt), then or a compact approved/pending line), stat tiles (one row: agents live now, Agents finished (24 h), Tokens (24 h), Est. cost (24 h)), running-now table (full width), workstream cards (plus an Unassigned card when runs have no workstream), tokens per hour beside live agents over time, run timeline, Finished (24 h).
- Workstream tab: the same views filtered to that workstream, plus the plan progress map directly under the tiles.
- Plan view: no approvals section. Approvals stay a file the lead reads (`approvals.json`, `ws approvals`); the owner wants nothing on the page that "needs" them.
- Run details open in the right-hand drawer; Escape closes it.
- Section dock (left, floating, macOS-dock style): compact dots that magnify and expand to names on hover, pinned (names always), or hidden (slim edge handle); the mode button cycles them and the choice is remembered. At most five groups.

## Color (reference palette, validated; do not invent hues)

- Categorical slots in fixed order: `--s1` blue, `--s2` orange, `--s3` aqua, `--s4` yellow, `--s5` magenta, `--s6` green, `--s7` violet, `--s8` red. Light and dark values are defined once in `:root` and the two dark scopes.
- Entity to color is fixed and never depends on rank: opus `--s1`, haiku `--s2`, sonnet `--s3`, grok `--s4`, muse `--s5`, glm `--s6`, other `--other`. Workstreams take slots in first-seen order from `WSCOL`.
- Status colors are reserved for state and never used as a series: good `--good`, warning `--warning`, serious `--serious`, critical `--critical`. A status color always ships with a word or icon.
- Text names things, not numbers: a decision, task, PR, issue or ADR is cited by a brief phrase, with its number optional in parentheses after it. Chips show a task id only beside its title or in its tooltip.
- Text uses ink tokens (`--ink`, `--ink-2`, `--muted`), never a series color.

## Charts

- Every chart has a visible x axis and y axis: an axis line, ticks with labels, and a short title or unit ("tokens", "agents", "time"). A categorical axis labels each category (lane or row names are its tick labels). Build axes only with the shared `axes()` helper in `static/format.js`. Both views call that helper. `tests/test_page_rules.py` fails a chart without it.
- Numbers are compact everywhere: counts and tokens through `fmtN` (Intl compact: 950, 1.2K, 3.4M, 1.5B), money through `fmtUsd` ($0.42, $12, $1.2K), durations through `fmtDur` (45s, 3m 10s, 2h 5m). No raw `toFixed` or `toLocaleString` on numbers outside those helpers.
- One y-axis per chart. Different scales get separate charts or small multiples (live agents over time is one strip per tool). There are no concurrency caps or meters: the owner removed them as unclear. Counts are always scoped to the current view.
- Bars and areas: 2 px surface gap between stacked segments, rounded ends, recessive grid and axes.
- Every chart with two or more series has a legend; every mark has a hover tooltip; bars and timeline rows open details on click.
- Timeline bars with file-time clocks (`clock_source = mtime`) are hatched, never solid.
- Plan progress map: groups in columns, arrows mean "waits on": group arrows between tiles, task arrows between chips (solid when the prerequisite is done, dashed while it waits, with the edge condition on hover). Same component on every workstream tab. Task states, each with its own color AND symbol (never color alone), legend on the map:

  | State | Color | Symbol |
  |---|---|---|
  | agent working now | `--s1` blue, solid fill, pulse, tool badge | ● |
  | waiting to merge (open PR) | `--warning` amber | ⧗ |
  | merged, waiting on a prod step | `--s5` magenta | ⇡ |
  | done | `--good` green | ✓ |
  | finished in the last 15 min | green outline | (outline) |
  | not started | dashed gray | none |
  | deferred or moved to another workstream (`status: deferred`, optional `moved_to`, `note`) | dotted gray, faded | ↷ |
  | gate: an outside signal or condition (`kind: gate`), met when done | dashed border, check when met, hollow diamond while unmet | ✓ / ◇ |

  Deferred tasks stay on the map where they were planned but leave every count and progress bar.

  Blue is reserved for "working now"; do not reuse it for any stored state. Violet is not used here (too close to blue side by side). Validated: CVD ΔE 14.7 light, 9.9 dark; amber is the reserved warning status color, relieved by the symbol and the task id.

## Behavior

- Read-only on every source. Never shows prompt bodies, argv, secrets or env values. Labels only.
- Archive is the one write action, and it asks first: the Archive button opens an inline confirm popover (never `window.confirm`) naming the workstream — "Archive <name>? It moves to the Archive view; you can restore it." — with Archive and Cancel, Escape cancels, focus moves in and back. Success toasts "Archived <name>" with an Undo button kept 10 s; Restore is confirm-free with a "Restored <name>" toast. Failures toast the hint, never silent. A small "Copy command" link stays beside each real button.
- Refresh: SSE `tick` every 2 s triggers a snapshot refetch; everything else refetches every 30 s.
- Production jobs (source `dagster`) are never in agent lists; they are a count: the live tile shows `N prod jobs` / `N prod jobs ran`.
- `?fixtures` loads `../fixtures/*.json` for offline design work.
- No health or warning cards on the page; doctor is CLI and API only. Read the problem list with `agent_dash.py doctor` or `GET /api/doctor`.

## Accessibility and phone

- Every interactive element is a real button or link with an accessible name. Clickable cards use role button with tabindex 0 and Enter/Space handling; no clickable div without role and tabindex (`tests/test_page_rules.py` pins this).
- Keyboard focus is visible and tab order follows the page. The project switcher and copy-command buttons work with Enter and Space.
- Charts carry a text takeaway: an aria-label on the svg plus a sentence naming the main takeaway; tooltips open on focus as well as hover.
- State is never color alone: every status ships a glyph plus a word (✓ Done, ⧗ waits, ⇡ on prod, ◇ gate unmet).
- Muted body text meets 4.5:1 on both themes (a computed-contrast test over `tokens.css` pins this). `prefers-reduced-motion` disables pulses and blinking.
- At 390 px no view scrolls the page sideways in either theme: the slim pinned row keeps one line while the tab strip scrolls internally and keeps the selected tab in view, tables scroll inside their own container.
