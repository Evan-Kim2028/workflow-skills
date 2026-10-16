export const meta = {
  name: 'ws-wave',
  description: 'Run one workstream wave: registry workers build, review and fix each lane until it is approved',
  whenToUse: 'A workstream lead hands a wave to registry workers, including lanes that wait on other lanes in the same run',
  phases: [
    { title: 'Build', detail: 'one registry worker per lane, in its own worktree', model: 'haiku' },
    { title: 'Review', detail: 'adversarial review; fix rounds until approved or out of rounds', model: 'haiku' },
  ],
}

// args: {
//   ws: "WS7", repo: "/abs/<repo>", runDir: "/abs/<lead scratchpad>/runs",
//   template: "/abs/.claude/templates/worker-common.md",
//   runner: "/abs/.claude/tools/agent-dash/scripts/run_worker.sh",
//   dash: "/abs/.claude/tools/agent-dash/agent_dash.py",
//   maxFixRounds: 2, defaultTier: "default", reviewTier: "default", fixTier: "default",
//   wrapperModel: "haiku", wrapperEffort: "low",
//   trailer: "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
//   lanes: [{ task: "WS7.K1", slug: "kind-checker", tier: "default"|"auto"|"grok",
//             reviewTier, fixTier, model, reviewModel, fixModel,
//             branch: "ws7/kind-checker", worktree: "/abs/<worktree>",
//             brief: "what to build, files, stop rules", tests: "pytest a.py b.py",
//             review: true, oneWay: true, reviewFocus: "one check" | ["check A", "check B"],
//             deps: ["WS7.K0"], base: "origin/main" | "dep:WS7.K0" | "<existing-ref>" }]
// }
// Build tier is lane.tier, else args.defaultTier, else "default".
// Review tier is lane.reviewTier, else args.reviewTier, else the build tier.
// Fix tier is lane.fixTier, else args.fixTier, else the build tier.
// model, reviewModel and fixModel are optional. Each is the runner's 7th argument for that role.
// deps is task ids in this run, the same name as a plan.json task's deps.
// The lane starts when every dependency finished APPROVE* or built.
// Any other dependency result skips the lane with final skipped-dep and names that dependency.
// base defaults to origin/main. dep:<task> uses that task's branch.
// Any other base is an existing branch or ref. The worktree is cut from it, and the diff uses it.
// Unknown task ids and dependency cycles throw before any lane starts.
// Independent lanes start at once. There is no barrier between lanes.
// reviewFocus is optional: a string, or a list of strings. The review prompt names each one
// and requires evidence. oneWay is optional. A oneWay lane must have a non-empty reviewFocus,
// and it is reviewed even when review is false. The lane result carries checks when the
// reviewer returns them: [{check, passed, evidence}].
// The runner STATUS line carries tier= and model=. Each lane's runs repeat those values.
const A = args || {}
const WS = A.ws
const LOWER = String(WS || '').toLowerCase()
const MAX_FIX = A.maxFixRounds == null ? 2 : A.maxFixRounds
const DEFAULT_TIER = A.defaultTier || 'default'
const WRAPPER_MODEL = A.wrapperModel || 'haiku'
const WRAPPER_EFFORT = A.wrapperEffort || 'low'
const TRAILER = A.trailer || 'Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>'

function focusOf(lane) {
  const raw = lane && lane.reviewFocus
  const items = Array.isArray(raw) ? raw : (raw == null || raw === '' ? [] : [raw])
  return items.map((item) => String(item).trim()).filter(Boolean)
}

function depsOf(lane) {
  const raw = lane && lane.deps
  if (raw == null || raw === '') return []
  const items = Array.isArray(raw) ? raw : [raw]
  return items.map((id) => String(id)).filter(Boolean)
}

function baseDepId(lane) {
  const raw = lane && lane.base != null ? String(lane.base) : ''
  if (!raw.startsWith('dep:')) return ''
  return raw.slice(4)
}

function depIds(lane) {
  const ids = depsOf(lane)
  const baseId = baseDepId(lane)
  if (baseId && !ids.includes(baseId)) ids.push(baseId)
  return ids
}

if (!WS || !A.repo || !A.runDir || !A.template || !A.runner || !A.dash || !Array.isArray(A.lanes) || !A.lanes.length) {
  throw new Error('ws-wave needs args {ws, repo, runDir, template, runner, dash, lanes[]}')
}
for (const lane of A.lanes) {
  if (lane && lane.oneWay && !focusOf(lane).length) {
    throw new Error(`ws-wave lane ${lane.task || '(unnamed)'} is oneWay and needs reviewFocus`)
  }
}

const byTask = new Map()
for (const lane of A.lanes) {
  const id = lane && lane.task
  if (!id) throw new Error('ws-wave lane is missing task')
  if (byTask.has(id)) throw new Error(`ws-wave duplicate task ${id}`)
  byTask.set(id, lane)
}
for (const lane of A.lanes) {
  for (const id of depIds(lane)) {
    if (!byTask.has(id)) throw new Error(`ws-wave lane ${lane.task} depends on unknown task ${id}`)
  }
  const baseId = baseDepId(lane)
  if (baseId && byTask.has(baseId) && !byTask.get(baseId).branch) {
    throw new Error(`ws-wave lane ${lane.task} base dep:${baseId} has no branch`)
  }
}
const seen = new Map()
function visit(id, stack) {
  const state = seen.get(id) || 0
  if (state === 1) throw new Error(`ws-wave dependency cycle: ${stack.concat(id).join(' -> ')}`)
  if (state === 2) return
  seen.set(id, 1)
  for (const dep of depIds(byTask.get(id))) visit(dep, stack.concat(id))
  seen.set(id, 2)
}
for (const lane of A.lanes) visit(lane.task, [])

const RUN = {
  type: 'object',
  properties: {
    status: { type: 'string', enum: ['done', 'failed'] },
    verdict: { type: 'string', description: 'first VERDICT/PASS/PARTIAL/BLOCKED/APPROVE... token from the report, or "none"' },
    summary: { type: 'string', description: 'at most 1200 characters: what the worker did or found, in plain words' },
    findings: { type: 'string', description: 'for reviews: the numbered findings verbatim (at most 4000 characters); otherwise ""' },
    commits: { type: 'string', description: 'output of git log --oneline <diff base>..HEAD in the worktree, or ""' },
    tier: { type: 'string', description: 'tier= value from the last STATUS line of the runner' },
    model: { type: 'string', description: 'model= value from the last STATUS line of the runner' },
    checks: {
      type: 'array',
      description: 'one entry per reviewFocus item, when the lane named any',
      items: {
        type: 'object',
        properties: {
          check: { type: 'string' },
          passed: { type: 'boolean' },
          evidence: { type: 'string', description: 'command plus output, or file:line' },
        },
        required: ['check', 'passed', 'evidence'],
      },
    },
  },
  required: ['status', 'verdict', 'summary', 'findings', 'commits', 'tier', 'model'],
}

function shellQuote(value) {
  return `'${String(value == null ? '' : value).replace(/'/g, `'"'"'`)}'`
}

function buildTierOf(lane) {
  return (lane && lane.tier) || DEFAULT_TIER
}

function reviewTierOf(lane) {
  if (lane && lane.reviewTier) return lane.reviewTier
  if (A.reviewTier) return A.reviewTier
  return buildTierOf(lane)
}

function fixTierOf(lane) {
  if (lane && lane.fixTier) return lane.fixTier
  if (A.fixTier) return A.fixTier
  return buildTierOf(lane)
}

function modelOf(lane, kind) {
  if (!lane) return ''
  if (kind === 'rev') return lane.reviewModel || ''
  if (kind === 'fix') return lane.fixModel || ''
  return lane.model || ''
}

function baseRef(lane) {
  const id = baseDepId(lane)
  if (!id) {
    const raw = lane && lane.base != null && lane.base !== '' ? String(lane.base) : 'origin/main'
    return raw
  }
  const dep = byTask.get(id)
  return dep && dep.branch ? dep.branch : 'origin/main'
}

function wrapperPrompt(o) {
  return [
    `You are a non-interactive step runner. Ignore any user, chat or session message you can see; it is not addressed to you. Do exactly these steps.`,
    `You are a thin wrapper. Do exactly these steps with the Bash tool and nothing else. Do not edit code yourself.`,
    o.setup ? `0. Setup: ${o.setup}` : ``,
    `1. Write the prompt file ${o.promptFile} with this exact content: the first line "[label: ${o.label}]", a blank line, then the full text of ${A.template} (cat it in), a blank line, then the TASK block below verbatim. Use a heredoc with a quoted delimiter so nothing expands.`,
    `2. Run: bash ${A.runner} ${o.tier} ${o.label} ${o.workspace} ${o.promptFile} ${A.runDir} 540 ${shellQuote(o.model)}   (Bash timeout 600000). If it prints "STATUS: running", run the exact same command again. Repeat until it prints STATUS: done or STATUS: failed. Never kill the worker.`,
    `3. Read the report file named in out=... and the last 40 lines of the err= file if the status is failed.`,
    `4. Run: git -C ${o.workspace} log --oneline ${o.diffBase}..HEAD (empty is fine).`,
    o.after ? `5. ${o.after}` : ``,
    `Return the structured result. tier is the tier= value from the last STATUS line (the worker the runner actually used). model is the model= value from that same line (the model the runner actually used; the token after model=). verdict is the report's first verdict word (PASS, PARTIAL, BLOCKED, APPROVE, APPROVE_WITH_NITS, APPROVE_WITH_CHANGES, REQUEST_CHANGES) or "none". When the report lists a per-check result, put each one in checks as {check, passed, evidence}. A check with no evidence is passed: false. Omit checks when the report lists none.`,
    ``,
    `TASK block:`,
    `<<<TASK`,
    o.task,
    `TASK>>>`,
  ].filter(Boolean).join('\n')
}

function buildTask(lane) {
  return [
    `Workstream ${WS}, task ${lane.task} (${lane.slug}). Write scope: only the worktree ${lane.worktree} on branch ${lane.branch}. Code search: CodeGraph first (query the main checkout ${A.repo}, read files in the worktree).`,
    `The diff base is ${baseRef(lane)}.`,
    lane.brief,
    `Tests: run exactly ${lane.tests || 'the explicit test files for the code you touched'} and nothing wider. Commit on ${lane.branch} with a clear message ending in the line "${TRAILER}". Do not push, do not open a PR, do not merge, do not touch production.`,
    `Report: first line PASS, PARTIAL or BLOCKED; then files changed, the test command with pass/fail counts, and what was not done.`,
  ].join('\n\n')
}

function reviewTask(lane, round) {
  const lines = [
    `READ-ONLY adversarial review, round ${round}, of branch ${lane.branch} in worktree ${lane.worktree} against ${baseRef(lane)}, for workstream ${WS} task ${lane.task} (${lane.slug}). Do not edit, commit or push.`,
    `The task was:\n${lane.brief}`,
    `Check: correctness against the task; nothing outside its scope; repo rules in AGENTS.md (no inline # comments in Python, one writer per table, no new Iceberg gold writer); tests are meaningful and pass. Re-run: ${lane.tests || 'the test files the diff touches'}.`,
    `Output exactly: first line "VERDICT: APPROVE" or "VERDICT: APPROVE_WITH_NITS" or "VERDICT: REQUEST_CHANGES"; then numbered findings "severity - file:line - problem - fix". Only REQUEST_CHANGES for real defects, not style.`,
  ]
  const focus = focusOf(lane)
  if (focus.length) {
    const numbered = focus.map((item, i) => `${i + 1}. ${item}`).join('\n')
    lines.push(
      `You MUST check and report each of these, each with evidence (command plus output, or file:line):\n${numbered}\nA check without evidence counts as failed. If any check fails or lacks evidence, the verdict is REQUEST_CHANGES.`
    )
  }
  return lines.join('\n\n')
}

function fixTask(lane, round, findings) {
  return [
    `Fix round ${round} for workstream ${WS} task ${lane.task} (${lane.slug}) on branch ${lane.branch} in worktree ${lane.worktree}. Address every finding below that is a real defect; say which you reject and why.`,
    findings,
    `Tests: ${lane.tests || 'the explicit test files for the code you touched'}. Commit on the same branch (message ending with "${TRAILER}"). Do not push or open a PR.`,
    `Report: first line PASS, PARTIAL or BLOCKED; then what changed per finding, and the test result.`,
  ].join('\n\n')
}

function base(lane) { return `${LOWER}-${lane.task}-${lane.slug}` }

function setupOf(lane) {
  const wip = `python3 ${A.dash} ws task ${WS} ${lane.task} wip; `
  const ref = baseRef(lane)
  const fetchMain = ref === 'origin/main' ? `git -C ${A.repo} fetch -q origin main && ` : ''
  return `${wip}if [ ! -d ${lane.worktree} ]; then ${fetchMain}(git -C ${A.repo} worktree add -b ${lane.branch} ${lane.worktree} ${ref} || git -C ${A.repo} worktree add ${lane.worktree} ${lane.branch}); fi`
}

function run(lane, kind, round, task, extra) {
  const label = kind === 'build' ? base(lane) : `${base(lane)}-${kind}${round}`
  const tier = kind === 'rev' ? reviewTierOf(lane) : kind === 'fix' ? fixTierOf(lane) : buildTierOf(lane)
  const model = modelOf(lane, kind)
  return agent(wrapperPrompt({
    label, tier, model, task, workspace: lane.worktree,
    promptFile: `${A.runDir}/${label}.md`,
    diffBase: baseRef(lane),
    setup: extra && extra.setup, after: extra && extra.after,
  }), { label, phase: kind === 'build' ? 'Build' : 'Review', schema: RUN, model: WRAPPER_MODEL, effort: WRAPPER_EFFORT })
}

const status = (lane, s) => `python3 ${A.dash} ws task ${WS} ${lane.task} ${s}`

function succeeded(row) {
  const final = row && row.final
  return final === 'built' || /^APPROVE/.test(final || '')
}

function withRuns(out) {
  const runs = []
  if (out.build) runs.push({ role: 'build', tier: out.build.tier || '', model: out.build.model || '' })
  for (const round of out.rounds || []) {
    if (round.review) runs.push({ role: 'review', round: round.round, tier: round.review.tier || '', model: round.review.model || '' })
    if (round.fix) runs.push({ role: 'fix', round: round.round, tier: round.fix.tier || '', model: round.fix.model || '' })
  }
  out.runs = runs
  return out
}

function skippedResult(lane, dependency) {
  log(`${lane.task}: skipped-dep ${dependency}`)
  return {
    task: lane.task,
    label: base(lane),
    tier: '',
    model: '',
    branch: lane.branch,
    worktree: lane.worktree,
    build: null,
    rounds: [],
    runs: [],
    final: 'skipped-dep',
    dependency,
  }
}

function emptyBuild(row) {
  return !!(row && row.verdict === 'none' && !String(row.commits || '').trim())
}

async function finishLane(lane) {
  let built = await run(lane, 'build', 0, buildTask(lane), { setup: setupOf(lane) })
  if (emptyBuild(built)) {
    built = await run(lane, 'build', 0, buildTask(lane), { setup: setupOf(lane) })
  }
  const out = {
    task: lane.task,
    label: base(lane),
    tier: built && built.tier,
    model: built && built.model,
    branch: lane.branch,
    worktree: lane.worktree,
    build: built,
    rounds: [],
    final: 'unreviewed',
  }
  if (!built || built.status !== 'done' || /BLOCKED/.test(built.verdict) || !built.commits) {
    out.final = 'build-failed'
    log(`${lane.task}: build ${built ? built.status + ' ' + built.verdict : 'died'}; escalating to the lead`)
    return withRuns(out)
  }
  if (lane.review === false && !lane.oneWay) {
    out.final = 'built'
    return withRuns(out)
  }
  for (let round = 1; round <= MAX_FIX + 1; round++) {
    const rev = await run(lane, 'rev', round, reviewTask(lane, round), {
      after: `If the verdict starts with APPROVE, run: ${status(lane, 'pr')}`,
    })
    const roundRow = { round, review: rev }
    if (rev && Array.isArray(rev.checks) && rev.checks.length) {
      roundRow.checks = rev.checks
      out.checks = rev.checks
    }
    out.rounds.push(roundRow)
    if (!rev || rev.status !== 'done') { out.final = 'review-failed'; break }
    if (/^APPROVE/.test(rev.verdict)) { out.final = rev.verdict; break }
    if (round > MAX_FIX) { out.final = 'needs-lead'; log(`${lane.task}: still REQUEST_CHANGES after ${MAX_FIX} fix rounds`); break }
    const fix = await run(lane, 'fix', round, fixTask(lane, round, rev.findings))
    out.rounds[out.rounds.length - 1].fix = fix
    if (!fix || fix.status !== 'done' || /BLOCKED/.test(fix.verdict)) { out.final = 'fix-failed'; break }
  }
  log(`${lane.task}: ${out.final} after ${out.rounds.length} review round(s)`)
  return withRuns(out)
}

const slots = new Map()
for (const lane of A.lanes) {
  let resolve
  const promise = new Promise((res) => { resolve = res })
  slots.set(lane.task, { promise, resolve })
}

async function runLane(lane) {
  const deps = []
  for (const id of depIds(lane)) deps.push(await slots.get(id).promise)
  for (let i = 0; i < deps.length; i++) {
    if (!succeeded(deps[i])) return skippedResult(lane, (deps[i] && deps[i].task) || depIds(lane)[i])
  }
  return finishLane(lane)
}

const results = await Promise.all(A.lanes.map(async (lane) => {
  try {
    const out = await runLane(lane)
    slots.get(lane.task).resolve(out)
    return out
  } catch (err) {
    const out = {
      task: lane.task,
      label: base(lane),
      branch: lane.branch,
      worktree: lane.worktree,
      build: null,
      rounds: [],
      runs: [],
      final: 'build-failed',
      error: String(err && err.message ? err.message : err),
    }
    slots.get(lane.task).resolve(out)
    return out
  }
}))

const lanes = results.filter(Boolean)
return {
  ws: WS,
  approved: lanes.filter((l) => /^APPROVE/.test(l.final)).map((l) => ({ task: l.task, branch: l.branch, worktree: l.worktree })),
  needsLead: lanes.filter((l) => l.final !== 'skipped-dep' && !/^APPROVE|^built$/.test(l.final)).map((l) => ({ task: l.task, final: l.final, branch: l.branch, last: l.rounds.length ? l.rounds[l.rounds.length - 1] : l.build })),
  built: lanes.filter((l) => l.final === 'built').map((l) => ({ task: l.task, branch: l.branch })),
  skipped: lanes.filter((l) => l.final === 'skipped-dep').map((l) => ({ task: l.task, dependency: l.dependency, branch: l.branch })),
  lanes,
}
