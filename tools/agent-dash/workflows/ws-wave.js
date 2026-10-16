export const meta = {
  name: 'ws-wave',
  description: 'Run one workstream wave: each lane is built, reviewed and fixed by muse (default) or Grok workers until approved, with Haiku wrappers only',
  whenToUse: 'A workstream lead runs one wave of independent lanes (no shared files) and wants Grok/muse to do the work, not Claude subagents',
  phases: [
    { title: 'Build', detail: 'one Grok or muse worker per lane, in its own worktree', model: 'haiku' },
    { title: 'Review', detail: 'adversarial Grok review; fix rounds until approved or out of rounds', model: 'haiku' },
  ],
}

// args: {
//   ws: "WS7", repo: "/abs/lake-of-rage", runDir: "/abs/<lead scratchpad>/runs",
//   template: "/abs/.claude/templates/worker-common.md",
//   runner: "/abs/.claude/tools/agent-dash/scripts/run_worker.sh",
//   dash: "/abs/.claude/tools/agent-dash/agent_dash.py",
//   maxFixRounds: 2, defaultTier: "muse", reviewTier: "muse",   (both default to muse)
//   lanes: [{ task: "WS7.K1", slug: "kind-checker", tier: "grok"|"muse", branch: "ws7/kind-checker",
//             worktree: "/abs/lor-wt-ws7-kind", brief: "what to build, files, stop rules", tests: "pytest a.py b.py",
//             review: true }]
// }
const A = args || {}
const WS = A.ws
const LOWER = String(WS || '').toLowerCase()
const MAX_FIX = A.maxFixRounds == null ? 2 : A.maxFixRounds
const DEFAULT_TIER = A.defaultTier || 'muse'
const REVIEW_TIER = A.reviewTier || DEFAULT_TIER
if (!WS || !A.repo || !A.runDir || !A.template || !A.runner || !A.dash || !Array.isArray(A.lanes) || !A.lanes.length) {
  throw new Error('ws-wave needs args {ws, repo, runDir, template, runner, dash, lanes[]}')
}

const RUN = {
  type: 'object',
  properties: {
    status: { type: 'string', enum: ['done', 'failed'] },
    verdict: { type: 'string', description: 'first VERDICT/PASS/PARTIAL/BLOCKED/APPROVE... token from the report, or "none"' },
    summary: { type: 'string', description: 'at most 1200 characters: what the worker did or found, in plain words' },
    findings: { type: 'string', description: 'for reviews: the numbered findings verbatim (at most 4000 characters); otherwise ""' },
    commits: { type: 'string', description: 'output of git log --oneline origin/main..HEAD in the worktree, or ""' },
  },
  required: ['status', 'verdict', 'summary', 'findings', 'commits'],
}

function wrapperPrompt(o) {
  return [
    `You are a thin wrapper. Do exactly these steps with the Bash tool and nothing else. Do not edit code yourself.`,
    o.setup ? `0. Setup: ${o.setup}` : ``,
    `1. Write the prompt file ${o.promptFile} with this exact content: the first line "[label: ${o.label}]", a blank line, then the full text of ${A.template} (cat it in), a blank line, then the TASK block below verbatim. Use a heredoc with a quoted delimiter so nothing expands.`,
    `2. Run: bash ${A.runner} ${o.tier} ${o.label} ${o.workspace} ${o.promptFile} ${A.runDir} 540   (Bash timeout 600000). If it prints "STATUS: running", run the exact same command again. Repeat until it prints STATUS: done or STATUS: failed. Never kill the worker.`,
    `3. Read the report file named in out=... and the last 40 lines of the err= file if the status is failed.`,
    `4. Run: git -C ${o.workspace} log --oneline origin/main..HEAD (empty is fine).`,
    o.after ? `5. ${o.after}` : ``,
    `Return the structured result. verdict is the report's first verdict word (PASS, PARTIAL, BLOCKED, APPROVE, APPROVE_WITH_NITS, APPROVE_WITH_CHANGES, REQUEST_CHANGES) or "none".`,
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
    lane.brief,
    `Tests: run exactly ${lane.tests || 'the explicit test files for the code you touched'} and nothing wider. Commit on ${lane.branch} with a clear message ending in the line "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>". Do not push, do not open a PR, do not merge, do not touch production.`,
    `Report: first line PASS, PARTIAL or BLOCKED; then files changed, the test command with pass/fail counts, and what was not done.`,
  ].join('\n\n')
}

function reviewTask(lane, round) {
  return [
    `READ-ONLY adversarial review, round ${round}, of branch ${lane.branch} in worktree ${lane.worktree} against origin/main, for workstream ${WS} task ${lane.task} (${lane.slug}). Do not edit, commit or push.`,
    `The task was:\n${lane.brief}`,
    `Check: correctness against the task; nothing outside its scope; repo rules in AGENTS.md (no inline # comments in Python, one writer per table, no new Iceberg gold writer); tests are meaningful and pass. Re-run: ${lane.tests || 'the test files the diff touches'}.`,
    `Output exactly: first line "VERDICT: APPROVE" or "VERDICT: APPROVE_WITH_NITS" or "VERDICT: REQUEST_CHANGES"; then numbered findings "severity - file:line - problem - fix". Only REQUEST_CHANGES for real defects, not style.`,
  ].join('\n\n')
}

function fixTask(lane, round, findings) {
  return [
    `Fix round ${round} for workstream ${WS} task ${lane.task} (${lane.slug}) on branch ${lane.branch} in worktree ${lane.worktree}. Address every finding below that is a real defect; say which you reject and why.`,
    findings,
    `Tests: ${lane.tests || 'the explicit test files for the code you touched'}. Commit on the same branch (message ending with "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"). Do not push or open a PR.`,
    `Report: first line PASS, PARTIAL or BLOCKED; then what changed per finding, and the test result.`,
  ].join('\n\n')
}

function base(lane) { return `${LOWER}-${lane.task}-${lane.slug}` }

function run(lane, kind, round, task, extra) {
  const label = kind === 'build' ? base(lane) : `${base(lane)}-${kind}${round}`
  const tier = kind === 'rev' ? REVIEW_TIER : (lane.tier || DEFAULT_TIER)
  return agent(wrapperPrompt({
    label, tier, task, workspace: lane.worktree,
    promptFile: `${A.runDir}/${label}.md`,
    setup: extra && extra.setup, after: extra && extra.after,
  }), { label, phase: kind === 'build' ? 'Build' : 'Review', schema: RUN, model: 'haiku', effort: 'low' })
}

const status = (lane, s) => `python3 ${A.dash} ws task ${WS} ${lane.task} ${s}`

const results = await pipeline(
  A.lanes,
  (lane) => run(lane, 'build', 0, buildTask(lane), {
    setup: `if [ ! -d ${lane.worktree} ]; then git -C ${A.repo} fetch -q origin main && (git -C ${A.repo} worktree add -b ${lane.branch} ${lane.worktree} origin/main || git -C ${A.repo} worktree add ${lane.worktree} ${lane.branch}); fi`,
  }),
  async (built, lane) => {
    const out = { task: lane.task, label: base(lane), branch: lane.branch, worktree: lane.worktree, build: built, rounds: [], final: 'unreviewed' }
    if (!built || built.status !== 'done' || /BLOCKED/.test(built.verdict) || !built.commits) {
      out.final = 'build-failed'
      log(`${lane.task}: build ${built ? built.status + ' ' + built.verdict : 'died'}; escalating to the lead`)
      return out
    }
    if (lane.review === false) { out.final = 'built'; return out }
    for (let round = 1; round <= MAX_FIX + 1; round++) {
      const rev = await run(lane, 'rev', round, reviewTask(lane, round), {
        after: `If the verdict starts with APPROVE, run: ${status(lane, 'pr')}`,
      })
      out.rounds.push({ round, review: rev })
      if (!rev || rev.status !== 'done') { out.final = 'review-failed'; break }
      if (/^APPROVE/.test(rev.verdict)) { out.final = rev.verdict; break }
      if (round > MAX_FIX) { out.final = 'needs-lead'; log(`${lane.task}: still REQUEST_CHANGES after ${MAX_FIX} fix rounds`); break }
      const fix = await run(lane, 'fix', round, fixTask(lane, round, rev.findings))
      out.rounds[out.rounds.length - 1].fix = fix
      if (!fix || fix.status !== 'done' || /BLOCKED/.test(fix.verdict)) { out.final = 'fix-failed'; break }
    }
    log(`${lane.task}: ${out.final} after ${out.rounds.length} review round(s)`)
    return out
  },
)

const lanes = results.filter(Boolean)
return {
  ws: WS,
  approved: lanes.filter((l) => /^APPROVE/.test(l.final)).map((l) => ({ task: l.task, branch: l.branch, worktree: l.worktree })),
  needsLead: lanes.filter((l) => !/^APPROVE|^built$/.test(l.final)).map((l) => ({ task: l.task, final: l.final, branch: l.branch, last: l.rounds.length ? l.rounds[l.rounds.length - 1] : l.build })),
  built: lanes.filter((l) => l.final === 'built').map((l) => ({ task: l.task, branch: l.branch })),
  lanes,
}
