# Install

`install.sh` links this repo into `~/.claude`. Run it after clone and after each pull:

```bash
sh install.sh             # link everything (idempotent)
sh install.sh --dry-run   # print what would change; touch nothing
```

## What it links

| Link | Points at |
|---|---|
| `~/.claude/skills/<name>` | `skills/<name>/` (each directory with a `SKILL.md`) |
| `~/.claude/tools/<name>` | `tools/<name>/` |
| `~/.claude/templates/worker-common.md` | `tools/agent-dash/templates/worker-common.md` |
| `~/.claude/scripts/worker-watch.sh`, `muse-usage.py`, `hub-usage.sh`, `memcap.sh` | `tools/agent-dash/scripts/<name>` |

Global instructions name those `~/.claude` paths, so they must keep resolving. A
link path that already holds a real file or directory is moved to
`~/.local/share/agent-dash/backups/<epoch>/`, keeping its path under `~/.claude`.
Nothing is overwritten or deleted. A symlink pointing somewhere else is replaced.

## Hooks (not linked by install.sh)

Hooks live in `~/.claude/settings.json` and are wired by
`tools/agent-dash/scripts/install_hooks.sh`, which is opt-in and idempotent.

| Hook | Install flag | Required |
|---|---|---|
| Label guard (PreToolUse, Bash) | `--claude-label-guard` | Yes: refuses worker launches with weak labels |
| Lead guard (PreToolUse AskUserQuestion, Stop) | `--claude-lead-guard` | Yes: keeps a workstream lead wakeable |
| Pre-push scoped gate | run once per checkout, no flag | Recommended |
| Grok Build label guard | `--grok-label-guard` | Only if you use Grok Build |
| Muse label guard | `--muse-label-guard` | Only if you use Muse |

```bash
sh tools/agent-dash/scripts/install_hooks.sh --claude-label-guard --claude-lead-guard
```

## Shims

`sh tools/agent-dash/scripts/install_hooks.sh --codex-shim` copies the `muse`
label-guard shim and the `cg` codegraph shim to
`~/.local/share/agent-dash/bin/` and prints the PATH line; it edits no shell
rc. `cg` is a worktree-aware wrapper for the `codegraph` CLI: read
subcommands run against the main checkout's index when the current worktree
has none, index writes (`init`, `index`, `sync`, `uninit`) are refused in a
worktree, and `cg --where` prints the index it would use.

## Other skill folders

Skills under `~/.claude/skills` that are not symlinks into this repo are not moved
by `install.sh`. Some come from third parties; see the owner's list before moving them.

## Local memcap rules

`scripts/memcap.sh` sources `~/.config/agent-dash/memcap-rules.sh` when it exists (override the path with `AGENT_DASH_MEMCAP_RULES`). The file sees the wrapped command in `$*` and may `exit` to refuse a run. Keep machine-specific paths there, not in this repo.
