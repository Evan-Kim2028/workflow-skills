# Quickstart

You will install the kit, open a page of sample runs, create a workstream called WS4, and point one wave at it. The dashboard at the end is the live one on port 8765. The sample page on port 8766 never sees WS4.

Commands below assume the clone directory is named `workflow-skills`. Replace `https://github.com/Evan-Kim2028/workflow-skills.git` with the URL of this repository.

## 1. Clone and install

```bash
git clone https://github.com/Evan-Kim2028/workflow-skills.git workflow-skills
cd workflow-skills
./install.sh
```

`install.sh` symlinks each `skills/<name>` directory that contains `SKILL.md` into `~/.claude/skills/`, and each `tools/<name>` directory into `~/.claude/tools/`. You should see a `linked` line for `workstream-dashboard`, `workstream-planner`, `workstream-lead`, and `agent-dash`.

Run it again any time. A symlink that already points at this clone prints `ok`. A real directory at the destination is renamed to `*.bak-<epoch>` and then replaced with the symlink. Nothing is deleted.

Claude Code loads skills from `~/.claude/skills/`.

## 2. Start the dashboard

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py run
```

You should see three lines:

```text
running
pid <pid>
url http://127.0.0.1:8765/
```

The process sets a niceness of 10, forks, and the child binds to 127.0.0.1 port 8765. A second `run` prints the existing pid and leaves that process up.

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py status
```

`status` prints the same three lines while the dashboard is up. When it is down, the command prints `stopped`.

Open http://127.0.0.1:8765/ . With no sessions on the machine the tables are empty. The shipped plans still add tabs for WS1, WS2, and WS7.

```bash
curl -s http://127.0.0.1:8765/api/health
```

Look for `"ok": true`. A missing log directory is skipped. It does not by itself fail this check.

## 3. Open the sample data

The fixture preview does not use the dashboard process and does not read your sessions. From the clone:

```bash
cd tools/agent-dash
python3 -m http.server 8766 --bind 127.0.0.1
```

The server prints that it is listening on 127.0.0.1 port 8766. Open these URLs:

- http://127.0.0.1:8766/static/index.html?fixtures&theme=dark
- http://127.0.0.1:8766/static/index.html?fixtures&view=plan&ws=WS1

The first is the Overall tab in the dark theme: live agents, token and cost tiles, a run timeline. The second is the WS1 plan, including the Approvals section. The footer says `fixture data`.

Stop the preview with Control-C. Leave the dashboard on port 8765 running.

To regenerate the JSON files from the seed:

```bash
python3 fixtures/make_fixtures.py
```

Run that from `tools/agent-dash`. The script uses a fixed seed and does not read local sessions.

## 4. Plan a first workstream

`workstream-planner` is the skill that fills a bundle. The commands it starts from are below. WS4 is the example id. If `ws new` prints `bundle exists`, pick another id, such as WS5, and use that id in every command after this.

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws new WS4 "Notes index"
```

You should see `created` and a path ending in `workstreams/WS4`. The directory is under `~/.local/share/agent-dash/workstreams/`. The command writes `bundle.json`, `plan.json`, `layout.json`, and `visuals.html`. The fresh plan already validates.

Set the thesis and the first task title:

```bash
python3 - <<'PY'
import json
import os
from pathlib import Path
root = Path(os.path.expanduser("~/.local/share/agent-dash/workstreams/WS4"))
plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
plan["narrative"]["thesis"] = "A notes index is one page, generated from the notes directory."
plan["narrative"]["end_state"] = "The index lists every note and links to it."
plan["dag"]["groups"][0]["tasks"][0]["title"] = "Write the notes index"
(root / "plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
print("updated", root / "plan.json")
PY
```

The scaffolded task id is `WS4.1`. Then render and check:

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws render WS4
python3 ~/.claude/tools/agent-dash/agent_dash.py ws validate WS4
python3 ~/.claude/tools/agent-dash/agent_dash.py ws list
```

`render` prints `wrote` and a path ending in `page.html`. `validate` prints `OK WS4`. `list` includes a line `WS4`, then `workstream`, then the title `Notes index`.

Record one owner decision (the lead reads these before any production step):

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws approve WS4 notes-cutover pending --step "Publish the notes index" --reversible no --conditions "The index file is on disk."
python3 ~/.claude/tools/agent-dash/agent_dash.py ws approvals WS4
```

`approvals` prints a table. The WS4 row has status `pending` and way `one-way`.

Reload http://127.0.0.1:8765/?view=plan&ws=WS4 . The plan page reads `plan.json` on refresh. A status edit does not need `ws render`.

## 5. Map the run label onto the task

The dashboard attributes a label such as `ws4-WS4.1-notes-index` to workstream WS4 with the shipped regex. Pinning that run to task `WS4.1` needs a task map. Built-in task matchers exist only for WS1, WS2, and WS7.

`taskmap.local.json` replaces `taskmap.example.json` entirely. This file keeps the shipped examples and adds WS4. From the clone root:

```bash
cat > tools/agent-dash/static/taskmap.local.json <<'EOF'
{
  "_doc": "Run label to plan task id. Keys are exact labels or /regex/ strings.",
  "WS1": {
    "ws1-schema": "12",
    "/^ws1-migrate/": "13"
  },
  "WS2": {
    "ws2-routes": "WS2.3"
  },
  "WS7": {
    "ws7-landing": "WS7.LANDING",
    "/^ws7-notes/": "WS7.NOTES"
  },
  "WS4": {
    "/^ws4-ws4\\.1/": "WS4.1"
  }
}
EOF
```

The page loads this file on refresh. No restart. The file is gitignored.

## 6. Run a first wave

`ws-wave` is a Claude Code Workflow script, not a shell command. Each lane is built, then reviewed, then fixed until the review approves it or the fix rounds run out. Every step is a low-effort Haiku wrapper around `run_worker.sh`. The heavy work stays on muse or Grok.

`defaultTier` and `reviewTier` both default to `muse`. A lane may set `tier` to `muse` or `grok`. `run_worker.sh` rejects any other tier and prints `reason=unknown-tier`.

The lead skill names a `workers.local.json` registry, with a default tier, named tiers, and an `auto` tier that would fail over by availability. This repository does not ship that file, and no code reads one. Set the tier on the wave arguments.

Print the absolute tool paths:

```bash
realpath ~/.claude/tools/agent-dash/workflows/ws-wave.js
realpath ~/.claude/tools/agent-dash/scripts/run_worker.sh
realpath ~/.claude/tools/agent-dash/agent_dash.py
```

Create the scratch directory the collectors scan. The shipped config treats each directory directly under `$HOME/agent-scratch/` as a scratch root. `run_worker.sh` appends `muse-sessions.tsv` or `cursor-sessions.tsv` in the run directory, and the collectors read that file when it sits on a scratch root.

```bash
mkdir -p "$HOME/agent-scratch/notes"
```

If you already replaced `roots.scratchpads` in `config.local.json`, include this directory in that list and restart the dashboard. A directory created under the shipped glob is picked up on the next poll.

Write a markdown file of worker rules and pass its absolute path as `template`. This repository does not ship that file. The workflow inserts the file into each worker prompt.

Call the Workflow tool with `scriptPath` set to the absolute path of `ws-wave.js` and `args` set to:

```json
{
  "ws": "WS4",
  "repo": "/absolute/path/to/your-repo",
  "runDir": "/absolute/path/to/agent-scratch/notes",
  "template": "/absolute/path/to/worker-rules.md",
  "runner": "/absolute/path/to/run_worker.sh",
  "dash": "/absolute/path/to/agent_dash.py",
  "maxFixRounds": 2,
  "defaultTier": "muse",
  "reviewTier": "muse",
  "lanes": [
    {
      "task": "WS4.1",
      "slug": "notes-index",
      "tier": "muse",
      "branch": "ws4/notes-index",
      "worktree": "/absolute/path/to/wt-notes-index",
      "brief": "Add a notes index. Write only in this worktree. Stop when the index lists the notes.",
      "tests": "the explicit test command for the files this lane changes",
      "review": true
    }
  ]
}
```

Required fields are `ws`, `repo`, `runDir`, `template`, `runner`, `dash`, and a non-empty `lanes` array. `maxFixRounds` defaults to 2. After that many fixes, one more review can still return the lane as `needsLead`.

The build label is `ws4-WS4.1-notes-index`. Reviews use the suffix `-rev1`, `-rev2`, and so on. Fixes use `-fix1`, `-fix2`, and so on. On approval the workflow runs `ws task WS4 WS4.1 pr`. It does not push, open a pull request, merge, or take a production step.

The result object has `approved`, `needsLead`, and `built`. `built` is a lane with `"review": false` whose build finished with commits.

To run the same worker once, without the Workflow tool, write a prompt file whose first line is `[label: ws4-WS4.1-notes-index]` and then:

```bash
bash ~/.claude/tools/agent-dash/scripts/run_worker.sh muse ws4-WS4.1-notes-index /absolute/path/to/wt-notes-index /absolute/path/to/prompt.md "$HOME/agent-scratch/notes" 540
```

The arguments are tier, label, workspace, prompt file, run directory, and max wait seconds. The default wait is 540. The command prints one line starting with `STATUS: running`, `STATUS: done`, or `STATUS: failed`. A second call while the pid file is live only waits. muse must be on `PATH` for tier `muse`. Tier `grok` calls `launch_grok.sh`, which needs `cursor-agent`.

## 7. Watch it

Open the live dashboard, not the fixture server:

- http://127.0.0.1:8765/?view=exec&ws=WS4
- http://127.0.0.1:8765/?view=plan&ws=WS4

The Execution view shows the WS4 progress map. A running agent whose label matches the task map pulses on the `WS4.1` chip. A run the map cannot place is listed as `Running, not mapped to a task`. The live dot reads `live`.

Move the task when the fact changes:

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py ws task WS4 WS4.1 pr
```

The command prints `WS4 WS4.1 pr`. Status values are `todo`, `pr`, `prog`, `done`, and `deferred`. `--moved-to` and `--note` belong only on `deferred`.

```bash
python3 ~/.claude/tools/agent-dash/agent_dash.py stop
```

`stop` prints `stopped` after the pidfile process exits. It signals that pid only.

## Where to go next

The lead loop, merge trains, and hand-off are in [Running a workstream end to end](../tools/agent-dash/docs/workstream-lead.md). Labels, tokens, and the map are in [Concepts](concepts.md). Files and environment variables are in [Configuration](configuration.md).
