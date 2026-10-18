# Dashboard design v2 — "bold, dark-first"

**Status: base layer landed** (v2 tokens in `static/tokens.css`, page shell
in `static/index.html`, dark by default). Cards build on top per §2–§4.
The build order is [roadmap.md](roadmap.md) Phase 3.
Anything not named here stays as `DESIGN.md` says — entity→colour
mapping, status as colour plus symbol plus word, one shared `axes()`
helper, compact formats, hover with exact values.

All numbers below are invented examples, not live data.

## 1. Theme and tokens

- Dark is the default (`<html data-theme="dark">`, no media-query guessing). Light is a toggle that sets `data-theme="light"` and remembers per viewer (`localStorage`, same as today).
- [design-v2-tokens.css](design-v2-tokens.css) is the full set: `--bg/--card/--raised/--overlay/--inset` surfaces, `--ink/--ink-2/--muted` inks, `--accent`, status colours, eight entity hues retuned for dark, `--grid/--axis/--ring`, radii (`--r-card: 16px` cards, pills `999px`), shadows, type scale (KPI value 34px/750, tabular numerals everywhere a number appears), 4pt spacing, motion durations/easing. Contrast ratios are stated per token in that file — re-verify with the same formula after any hex change. Hard rules:
  - White text on the bright dark-mode accent fails (~3:1), so **all filled accent/blue pills use near-black ink text** (`--accent-ink`, ~6.4:1). Light mode fills use a darker shade of the same blue with white text (`--accent-fill`, ~5.5:1).
  - `--muted` passes AA even at 11px in both themes, so tertiary text is never the contrast failure.

## 2. Card anatomy (Execution → Overall, top to bottom)

1. **Header.** Title, live pill (pulsing green dot + `LIVE · seq N` in mono — the seq proves freshness), Plan | Execution segmented control, theme toggle. Sticky, blurred, 1px `--grid` bottom border.
2. **Workstream tabs.** `Overall` pinned first, then one tab per workstream in WS order: grip affordance (`⋮⋮` + `cursor: grab`), name, `✓` when the plan is 100%, live-count chip when live > 0. Native HTML5 drag-to-reorder with a Reset control; order remembered per viewer. Right-pinned `All | Active | Completed` filter with the tab strip scrolling *under* it (the filter never scrolls away). A tab the filter would hide stays visible but muted when selected. Completed = `total > 0 && done === total`, `✓` plus the word Done — never colour alone.
3. **KPI tiles** (agents live · finished 24 h · tokens 24 h · cost 24 h). Each tile: 12.5px label → 34px tabular value → delta chip → 24-point sparkline (gradient area + end dot) → one muted tertiary line (all-time total, cache share, verdict split). Hover anywhere on a tile shows exact values (§4 for the math).
4. **Where we are.** Overall line first (example: `63%` hero + `19 of 30 tasks done across 4 workstreams · 3 agents live`). One row per workstream: name + status pill underneath (`✓ Done` green, `● In progress` blue fill, `○ Not started` dashed, `No plan` gray), segmented progress bar (green done + amber to-merge + magenta prod-waiting + blue working, 2px gaps, 14px tall, rounded), 22px tabular % right-aligned, one mono meta line (example: `7/12 tasks · 2 to merge · ● 1 live · 48.1M · $9.20`), quiet Plan / Live buttons. No-plan rows show `—` for %, never 0%.
5. **Running now.** Model (family dot + name, family prefix de-duplicated), label, WS, elapsed, output (`▲` when the out file grew since last poll), tokens + cached %, status (`● running` + word, pulsing dot). Click/Enter opens the run drawer; Esc closes; focus moves to Close on open and returns on close. Tooltip on row hover with exact tokens/elapsed.
6. **Activity over time** (one card, owner ask). A time filter bar on top (track over the loaded window with a tokens-per-hour sparkline, two draggable handles plus draggable middle, Last hour / 6 h / 24 h / All presets, Reset, the selected range label) zooms all four strips below to one shared time axis at once. Tokens per hour: stacked bars by family, 2px surface gaps, 5px rounded tops, axis + `tokens` unit, legend with exact totals (example: `900M total · Claude 400M · …`), peak labelled above its bar, end-of-stack values on the top-3 hours, hover splits the stack with exact per-family numbers. Run timeline: lanes by family with `N runs` labels, bars start→end (running bars reach the blue now-rule), outcome = colour + glyph + legend word (`✓` finished green, `◐` partial amber, `✕` died/blocked red with a larger mark, `●` running blue). **The timeline draws every run in the selected range, never a sample** — lane counts always match what is drawn, and the caption says so; runs outside the range are not drawn and bars crossing the edge carry a visible cap. Bars click through to the same run drawer. Progress over time (right below the run timeline): the Done plus Merged-waiting-on-prod step series as % of tasks on the shared x with its own 0-100% y axis, legend, milestone dots and takeaway aria label; a workstream tab shows its series, the overall view shows the project total; hidden with no empty frame when no plan data exists. Live agents over time: strips, one gradient area per family (peak opacity ~.45 → 0) on its own y axis (`agents`), `pk N` end annotation + labelled peak dot, hover reads any hour. The x labels draw once at the bottom of the stack; a crosshair spans all four strips at the pointer's time.
9. **Recently finished.** Label, model, verdict pill (colour + symbol + word: `✓ pass`, `◐ partial`, `✕ changes asked`, `— no verdict`), duration, tokens. Newest first.

## 3. Workstream tab (Overall filter + plan map under the tiles)

Same tiles scoped to the workstream (live, finished, tokens, cost with the same sparkline/delta components), then the **plan progress map**:

- Always-visible compact legend row (colour + symbol + words; details stay in the popover as today). Columns hold plan sections, cards = sections with `N of M done`, chips = tasks: `✓` done green, `●` working-now blue solid + pulse + agent-count badge, `⧗` open-PR amber, `⇡` merged-waiting-prod magenta, `◇` gate dashed (hollow unmet, `✓` met), `↷` deferred dotted-faded, dashed gray not-started.
- **Arrows on focus:** hovering or keyboard-focusing a chip dims unrelated groups, outlines the chip, draws amber dashed arrows from its prerequisites and blue dashed arrows to its dependents, and lists `waits on: …` in the tooltip. Group-level dashed arrows between columns render always (from the plan's group edges).
- Merge-train members get a tag + a caption line naming the trains (example: `3 merge trains — showing tags for: …`).

## 4. Sparklines and deltas — how they are computed

Headline values stay the API's rolling-24 h fields (`snapshot.totals`, `counts.finished_in_range`, `counts.live_agents`) — the same numbers the page shows today. Sparklines and deltas compare the last 24 complete hours against the prior 24, from endpoints the API already has:

| Tile | Sparkline (24 pts, old → now) | Delta |
|---|---|---|
| Agents live | Hourly mean of `/api/concurrency` totals | now − value 24 h ago, as a count (example: `▲ +3 vs 24 h ago`), not a % — a % of a small integer misleads |
| Finished | `/api/runs` `ended_at` bucketed by hour, `status == "done"` | % change vs prior 24 buckets |
| Tokens | `/api/usage?group=model` hourly token sums (input+output+cache_read+cache_write+reasoning); WS tab uses `group=workstream` | % change vs prior 24 buckets |
| Cost | Same buckets' `cost_usd`; WS tab same | % change vs prior 24 buckets |

- Chip states: `▲ green / ▼ red ±N.N% vs prior 24 h`; `◆ flat` when |Δ| < 0.5%; **`▲ new past 24 h` (accent fill) when the prior window is zero** — never `+∞%`.
- Because hourly buckets are hour-aligned and headlines are rolling, sparkline sums can differ a few percent from headlines; tooltips label which is which (`This 24 h` buckets vs `Rolling (API)`).
- **Required API addition:** `GET /api/snapshot` gains **`prev_24h`: `{tokens, cost_usd, finished, live_agents}`** — the exact rolling-window values from 24 h ago, so builder deltas match headlines to the digit instead of the hour-bucket approximation, and so the live-agent delta survives retention changes. Field name: `prev_24h`.

## 5. Interaction and motion

- Hover: one shared tooltip, exact values, tabular numerals, family colour chips; bars/timeline rows click through to the run drawer.
- Drawer: slides in 200 ms `cubic-bezier(.2,.8,.2,1)`; Esc closes; focus to Close on open, restored on close.
- Live pulse (dot, working-now chips, now-rule): 1.6–2 s beat. Bars/tiles settle once on load (~450 ms ease-out).
- `prefers-reduced-motion: reduce` kills every animation/transition and smooth scroll. Focus-visible: 2px accent outline everywhere (tabs, buttons, rows, chips — chips are `tabindex="0"` for the arrow-tracing in §3).
- Refresh keeps today's contract (SSE tick → snapshot refetch; 30 s for the rest). Cards keep per-section min-heights + skeleton shimmer so live refresh never shifts layout.

## 6. Empty / loading / error — distinct from zero

| State | Look |
|---|---|
| Loading | Skeleton shimmer bars, `aria-busy="true"` on the card |
| Zero (real 0) | Full card, `0`, flat sparkline at axis, `◆ flat` chip |
| New (no prior) | `▲ new past 24 h` accent chip |
| No plan | `—` for %, `No plan` pill, no bar fill |
| Not started | `○ Not started` dashed pill, empty track |
| Error/offline | Card keeps last data, red `stale · retrying` line in the card sub, live pill loses its dot |
| Filtered out | Tabs stay visible but muted when selected under a hiding filter; other tabs simply absent |

Zero is a number and renders like one; empty and error never do.

## 7. Phone (390 px)

- 16 px gutters (`--gutter-phone`), **no horizontal scroll**: tables drop to essential columns (Output/Elapsed hide; labels ellipsis), tiles go 2-up, chart pair stacks, plan-map columns stack vertically with the same chips and tooltips.
- Tab strip scrolls under the pinned filter; sticky blurred topbar stays.
- Touch targets ≥ 36 px for tabs, buttons, chips.

## 8. What changes vs today's DESIGN.md

1. Dark-first: dark is the default theme; light is the toggle. (Tokens file replaces the old palette block.)
2. KPI tiles gain sparklines + prior-24 h delta chips + tertiary lines (§2.3, §4). New required API field `prev_24h` (§4).
3. Area charts gain gradient fills; bars are 5px-rounded with labelled peaks and top-3 values; strips are one-per-family with `pk N` annotations.
4. Cards gain depth: 16px radius, ring border + top inner highlight, hover border, card/pop/drawer shadows (token elevation).
5. Tabs gain drag affordance (grip, Reset, remembered order) and a pinned filter that never scrolls away.
6. Plan map gains an always-visible legend row and arrows-on-focus chip tracing.
7. Tables gain the run drawer focus contract and exact-value tooltips.
8. Timeline draws every run in the window, never a sample.
9. New §6 states: loading/zero/new/no-plan/error are visually distinct; `—` (not `0`) for missing values.
10. Type rule: tabular numerals for every number; 34px KPI values; contrast numbers stated per token.

Everything else in `DESIGN.md` stands unchanged.

## 9. Landed tokens

The full set lives in `static/tokens.css` (dark `:root` default, light under
`:root[data-theme="light"]`). Card CSS may use any of these names; every old
palette name still works as an alias (right column) so CSS written against
either set renders the same. Do not invent new token names — reuse these.

| Use | v2 name | Old alias (still works) |
|---|---|---|
| App page background | `--bg` | `--page` |
| Card / panel background | `--card` | `--surface` |
| Table head, wells, hover fill, skeleton base | `--raised` | `--surface-2` |
| Drawer, tooltip, popover | `--overlay` | — |
| Sunken chart plot | `--inset` | — |
| Primary / secondary / tertiary text | `--ink`, `--ink-2`, `--muted` | `--fg`, `--fg2`, `--fg3`, `--mute` |
| Chart gridlines, hairline dividers | `--grid` | `--line` |
| Axis baselines, tick marks | `--axis` | `--pass` (axis-coloured marks) |
| Card borders / hover borders | `--ring`, `--ring-strong` | — |
| Accent links, lines, ticks | `--accent` | — |
| Text on accent fills (near-black; white fails) | `--accent-ink` | — |
| Accent fill (dark = accent; light = darker shade) | `--accent-fill` | — |
| Accent tint wash / pulse halo | `--accent-soft`, `--accent-glow` | — |
| Status greens / ambers / reds | `--good`, `--warning`, `--serious`, `--critical` | `--ok`, `--warn`, `--crit` |
| Status small-text inks | `--good-ink`, `--warning-ink`, `--critical-ink` | — |
| Status tint washes | `--good-soft`, `--warning-soft`, `--critical-soft` | `--ok-soft`, `--warn-soft`, `--crit-soft` |
| Entity hues (family colour) | `--s1` … `--s8`, `--other` | `--port` (= `--s1`), `--unk` (= `--grid`) |
| Model shades | — | `--m-print`, `--m-veto`, `--m-cat`, `--m-inst`, `--m-replay`, `--m-none` |
| Radii: controls / wells / cards / pills | `--r-sm`, `--r-md`, `--r-card` (16px), `--r-pill` | — |
| Shadows | `--shadow-card`, `--shadow-pop`, `--shadow-drawer` | — |
| Type stacks | `--font`, `--mono` | `--f-display`, `--f-body`, `--f-mono` |
| Type sizes | `--fs-hero` (34px KPI), `--fs-h1`, `--fs-h2`, `--fs-body`, `--fs-sub`, `--fs-micro` | — |
| Tabular numerals for every number | `--tnum` | — |
| Spacing 4/8/12/16/24/32 | `--sp-1` … `--sp-6` | — |
| Page gutters desktop / phone | `--gutter`, `--gutter-phone` | — |
| Motion durations / easings | `--dur-instant`, `--dur-swift`, `--dur-settle`, `--ease-out`, `--ease-spring` | — |
| Chart bar gap / radius / area fill / peak dot | `--bar-gap`, `--bar-radius`, `--area-fill`, `--peak-dot` | — |
| Chart heights desktop / phone | `--chart-h`, `--chart-h-phone` | — |

Hard rules (from §1): filled accent/blue pills use `--accent-ink` text
(never white in dark mode); `--muted` passes AA even at 11px; status is
always colour plus symbol plus word.
