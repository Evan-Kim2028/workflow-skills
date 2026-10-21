# unlabeled-runs: guarded runs with weak labels

Runs in the live store whose phase is `unclassified` or whose label
fails the strict check (`python3 agent_dash.py label check <label>`).
One problem per workstream (`unlabeled-runs:ws:<ID>`, plus
`unlabeled-runs:global` for runs with no workstream): the title names
the scope, the run count, and the billed tokens; the detail lists the
worst 3 run families with exact corrected labels (`doctor --ws <ID>
--all` is the same 3 — families share one fix each, so 3 covers the
work).

## Which runs the check governs

Only runs that a guarded launch path creates. The check counts a run
when all of these hold:

- Its source is `muse`, `glm`, or the `grok` family (grok CLI and
  cursor-agent manifests; `grok_cli` and `cursor` are accepted too).
  These are the manifest-labelled lanes: their launchers build labels
  with `label make` and the refusing entry points stop weak ones.
- Its run id has no `@` and its `slice_of` is empty. Dashboard-generated
  lead slices (`ws11-lead-<hex>`, run ids ending `@WS11`) are not
  launched by a guarded path.
- Its label is not a project-directory session label (a bare name with
  a two-hex suffix such as `my-project-0e` or `documents-a4`, with no
  `ws<N>` or `hub` scope).

Everything else is out of scope and never produces this problem:
interactive Claude Code main sessions (`claude-main`), Claude subagents
(`claude-subagent`), workflow transcripts (`claude-workflow`), Codex
(`codex`, `codex-exec`), `shell`, `dagster`, `process`, `generic`, and
`hub`. These sessions have no launch path that can take a label: a
person types the session, and no `label make` call precedes it. Their
labels come from the project directory, so a guard would flag every
live session permanently and make the check useless.

## Why it matters

A run without a strong label lands in the grey Unclassified bucket:
the tokens burn, but the planning-versus-execution split says nothing
about them. Since the guard landed, weak labels are structurally
impossible for new runs (entry points refuse them); anything this
check flags is legacy history to map, or a run that slipped past the
guard and needs its launcher fixed.

## Severity

`warn` for legacy runs (started before the guard landed on
2026-10-19): map the family and move on. `critical` for runs started
after: a refusing entry point should have stopped them, so the
launcher path is broken — fix the launcher, not just the label.

## How to fix

New runs: build every label with `label make` (never hand-write one);
the refusing entry points (`report start`, `run_worker.py`,
`launch_grok.sh`, the Claude Code label-guard hook) print the exact
corrected label when they stop one.

Legacy families: map each family with an anchored entry in
`phases.local.json` beside `project.json` (owner override, entries
run first) — one keyword rule per family, anchored on the family
name so task lanes never match:

```json
{
  "rules": [
    {"id": "local-ws11-scout", "type": "keyword", "phase": "planning",
     "comment": "WS11 scouting audits: research-only lanes.",
     "patterns": ["^ws11-scout-"]}
  ]
}
```

Replaying old runs (and only then): `AGENT_DASH_ALLOW_LEGACY_LABELS=1`
downgrades the refusal to a warning at every entry point.

## How to verify

`doctor --ws <ID>` no longer lists the workstream, and the Project
home planning-versus-execution card shows no grey Unclassified slice
for it.
