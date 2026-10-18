#!/usr/bin/env node
/* Finished-lists pagination check: ten per page everywhere, one shared pager.
 *
 * Boots a TEMP server (port 8906, temp state dir, temp workstreams with
 * synthetic bundles -- never the owner's real bundles or :8765) holding 35
 * synthetic finished runs plus 12 done workstreams, then drives the live
 * page in headless Chrome over the DevTools protocol (stdlib only):
 * page 1 shows 10 with "Showing 1-10 of 35", Next reaches "Showing 11-20
 * of 35", the last page shows 5, the 25-per-page switch relabels and
 * persists, idle ticks keep the page with no re-render storm, a synthetic
 * arrival through the real tick path holds page 2 while shifting rows in,
 * Recently done pages 10 + 2 on the Project home, and zero console errors.
 *
 * Usage: node scripts/paginate_check.mjs [--keep]   (keeps the temp dirs)
 * Exit 0 on PASS, 1 on the first failed assertion.
 */
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.dirname(HERE);
const PORT = 8906;
const URL = `http://127.0.0.1:${PORT}`;
const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
].filter(Boolean);

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "paginate-check-"));
const wsDir = path.join(tmp, "ws");
const stateDir = path.join(tmp, "state");
const homeDir = path.join(tmp, "home");
fs.mkdirSync(wsDir, { recursive: true });
fs.mkdirSync(stateDir, { recursive: true });
fs.mkdirSync(homeDir, { recursive: true });
const keep = process.argv.includes("--keep");

const env = { ...process.env, AGENT_DASH_IGNORE_LOCAL: "1", AGENT_DASH_STATE_DIR: stateDir,
  AGENT_DASH_WORKSTREAMS: wsDir, AGENT_DASH_PORT: String(PORT), HOME: homeDir };
let failures = 0;
function check(name, cond, detail = "") {
  console.log(`${cond ? "ok" : "FAIL"}  ${name}${detail ? " -- " + detail : ""}`);
  if (!cond) failures++;
}
function cli(...args) {
  const r = spawnSync("python3", ["agent_dash.py", ...args], { cwd: ROOT, env, encoding: "utf8" });
  if (r.status !== 0) { console.error(`cli failed: agent_dash.py ${args.join(" ")}\n${r.stdout}\n${r.stderr}`); process.exit(1); }
  return r.stdout;
}
function seedDone(id, title) {
  cli("ws", "new", id, title);
  const planPath = path.join(wsDir, id, "plan.json");
  const plan = JSON.parse(fs.readFileSync(planPath, "utf8"));
  for (const g of plan.dag.groups) for (const t of g.tasks) t.status = "done";
  fs.writeFileSync(planPath, JSON.stringify(plan));
  fs.mkdirSync(path.join(wsDir, id, "docs"), { recursive: true });
  fs.writeFileSync(path.join(wsDir, id, "docs", "closing-report.md"), "# Closing report\n\nShipped.\n");
  cli("ws", "state", id, "done");
}
function seedRuns(count) {
  const seedPath = path.join(tmp, "seed_runs.py");
  fs.writeFileSync(seedPath, `import sys, time
sys.path.insert(0, ${JSON.stringify(ROOT)})
from pathlib import Path
from dash.store import Store
store = Store(Path(${JSON.stringify(stateDir)}) / "dash.sqlite")
now = time.time()
for i in range(${count}):
    store.upsert_run({
        "run_id": f"muse:pg:ws21-fin-{i:02d}",
        "source": "muse",
        "tier": "muse",
        "model": "claude-sonnet-4-5",
        "label": f"ws21-pg-fin-{i:02d}",
        "status": "done",
        "started_at": now - 3600 - i * 60,
        "ended_at": now - 60 * (i + 1),
    })
store.close()
print("seeded ${count} finished runs")
`);
  const r = spawnSync("python3", [seedPath], { cwd: ROOT, env, encoding: "utf8" });
  if (r.status !== 0) { console.error(`seed failed\n${r.stdout}\n${r.stderr}`); process.exit(1); }
}
async function main() {
  // 12 done workstreams for Recently done, 35 finished runs for Finished.
  for (let i = 21; i <= 32; i++) seedDone(`WS${i}`, `Lane WS${i}`);
  seedRuns(35);

  const server = spawn("python3", ["agent_dash.py", "run"], { cwd: ROOT, env, stdio: "ignore" });
  const pidFile = path.join(stateDir, "agent-dash.pid");
  let pid = null;
  for (let i = 0; i < 100; i++) {
    await new Promise((r) => setTimeout(r, 300));
    try {
      const h = await (await fetch(`${URL}/api/health`)).json();
      if (h.ok !== undefined) break;
    } catch {}
  }
  try { pid = Number(fs.readFileSync(pidFile, "utf8").trim().split(/\s+/)[0]); } catch {}
  check("temp server answers on 8906", pid !== null, `pid ${pid}`);

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let chromeBin = process.env.CHROME_PATH || null;
  if (!chromeBin) {
    for (const c of CHROME_CANDIDATES) {
      if (!c) continue;
      let ok = false;
      try { fs.accessSync(c, fs.constants.X_OK); ok = true; } catch {}
      if (!ok) { try { ok = fs.existsSync(c); } catch {} }
      if (ok) { chromeBin = c; break; }
    }
  }
  if (!chromeBin) {
    console.error("SKIP: no Chrome");
    try { if (pid) process.kill(pid); } catch {}
    process.exit(2);
  }
  const dbgPort = 9456;
  const prof = path.join(tmp, "prof");
  const ch = spawn(chromeBin, ["--headless=new", `--remote-debugging-port=${dbgPort}`, `--user-data-dir=${prof}`, "--window-size=1440,1000", "about:blank"], { stdio: "ignore" });
  let tgt = null;
  for (let i = 0; i < 60; i++) {
    try {
      const ts = await (await fetch(`http://127.0.0.1:${dbgPort}/json`)).json();
      tgt = ts.find((t) => t.type === "page");
      if (tgt) break;
    } catch {}
    await sleep(200);
  }
  check("chrome target opens", !!tgt);
  const ws = new WebSocket(tgt.webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));
  let id = 0;
  const pend = {};
  const problems = [];
  ws.onmessage = (e) => {
    const m = JSON.parse(typeof e.data === "string" ? e.data : e.data.toString());
    if (m.method === "Runtime.consoleAPICalled" && m.params && m.params.type === "error") problems.push("console.error");
    if (m.method === "Runtime.exceptionThrown") problems.push("exception");
    if (m.id && pend[m.id]) pend[m.id](m.result);
  };
  const send = (method, params = {}) => new Promise((r) => { pend[++id] = r; ws.send(JSON.stringify({ id, method, params })); });
  const ev = (expression, awaitPromise = false) => send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise });
  const text = async (expr) => (await ev(expr)).result.value;
  async function waitFor(expr, timeoutMs = 20000) {
    const end = Date.now() + timeoutMs;
    for (;;) {
      if (await text(expr)) return true;
      if (Date.now() > end) return false;
      await sleep(400);
    }
  }
  const finRows = `document.querySelectorAll('#exec-finished tbody tr').length`;
  const finLabel = `(document.querySelector('#exec-finished .pg-label')||{}).textContent||''`;

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
  check("page 1 shows 10 of 35", await waitFor(`${finRows}===10 && ${finLabel}==='Showing 1-10 of 35'`), await text(finLabel));
  check("Prev disabled on page 1", await text(`document.querySelector('[data-pg-footer="finished"] [data-pg="prev"]').disabled`) === true);

  await ev(`document.querySelector('[data-pg-footer="finished"] [data-pg="next"]').click()`);
  check("Next shows 11-20 of 35", await waitFor(`${finLabel}==='Showing 11-20 of 35' && ${finRows}===10`), await text(finLabel));

  await ev(`document.querySelector('[data-pg-footer="finished"] [data-pg="page-4"]').click()`);
  check("last page shows 5", await waitFor(`${finLabel}==='Showing 31-35 of 35' && ${finRows}===5`), await text(finLabel));
  check("Next disabled on last page", await text(`document.querySelector('[data-pg-footer="finished"] [data-pg="next"]').disabled`) === true);

  await ev(`(function(){var s=document.querySelector('[data-pg-footer="finished"] select');s.value='25';s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  check("size 25 clamps page 4 to 26-35", await waitFor(`${finLabel}==='Showing 26-35 of 35' && ${finRows}===10`), await text(finLabel));
  await ev(`document.querySelector('[data-pg-footer="finished"] [data-pg="page-1"]').click()`);
  check("size 25 page 1 shows 25", await waitFor(`${finLabel}==='Showing 1-25 of 35' && ${finRows}===25`), await text(finLabel));
  check("size persists per list", await text(`(function(){try{return sessionStorage.getItem('agent-dash-pagesize:finished');}catch(e){return 'ERR';}})()`) === "25");
  await send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
  check("size 25 survives reload", await waitFor(`${finRows}===25`), await text(finLabel));
  await ev(`(function(){var s=document.querySelector('[data-pg-footer="finished"] select');s.value='10';s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  check("size back to 10", await waitFor(`${finRows}===10 && ${finLabel}==='Showing 1-10 of 35'`), await text(finLabel));
  await ev(`document.querySelector('[data-pg-footer="finished"] [data-pg="next"]').click()`);
  check("back on page 2", await waitFor(`${finLabel}==='Showing 11-20 of 35'`), await text(finLabel));

  // Idle ticks: several 2 s stream ticks must keep the page with no storm.
  const renders0 = await text(`window.__adRenders||0`);
  const page2first = await text(`(function(){var r=document.querySelector('#exec-finished tbody tr .lbl');return r?r.textContent:'';})()`);
  await sleep(7000);
  const renders1 = await text(`window.__adRenders||0`);
  check("idle ticks keep page 2", await text(finLabel) === "Showing 11-20 of 35", await text(finLabel));
  check("idle ticks keep the rows", await text(`(function(){var r=document.querySelector('#exec-finished tbody tr .lbl');return r?r.textContent:'';})()`) === page2first);
  check("no re-render storm on idle ticks", renders1 - renders0 <= 1, `${renders0} -> ${renders1}`);

  // A real arrival through the report CLI and the collector poll holds
  // page 2: new rows shift into page 1 while the current page keeps its
  // span. The generic source polls every 2 s, so the 36th run lands on
  // the next ticks. A tick fetch started before the arrival can still
  // repaint 35 once (its snapshot predates the write); the next tick
  // heals it, so the assertion needs 36 stable across two reads plus the
  // one-row shift by run label (token cells fill in async and would flake
  // a full-text compare). Reporting twice is idempotent (one run id).
  const labExpr = `(function(){var r=document.querySelectorAll('#exec-finished tbody tr');return Array.prototype.map.call(r,function(x){var l=x.querySelector('.lbl');return l?l.textContent:'';});})()`;
  const preLabels = await text(labExpr);
  let held = false, shifted = false;
  for (let cyc = 0; cyc < 3 && !shifted; cyc++) {
    cli("report", "start", "--label", "ws21-pg-fin-extra", "--model", "claude-sonnet-4-5", "--session", "sess-pg-extra");
    cli("report", "finish", "--label", "ws21-pg-fin-extra", "--model", "claude-sonnet-4-5", "--verdict", "PASS", "--session", "sess-pg-extra");
    if (!(await waitFor(`${finLabel}==='Showing 11-20 of 36'`, 60000))) break;
    await sleep(1200);
    held = (await text(finLabel)) === "Showing 11-20 of 36";
    if (!held) continue;
    const labels = await text(labExpr);
    shifted = labels.length === 10 && labels[1] === preLabels[0];
  }
  check("arrival holds page 2 at 36", held, await text(finLabel));
  check("arrival shifts rows in", shifted, `page-2 second row is "${(await text(labExpr))[1] || "none"}", want "${preLabels[0]}"`);
  await ev(`document.querySelector('[data-pg-footer="finished"] [data-pg="page-1"]').click()`);
  check("new arrival leads page 1", await waitFor(`(function(){var r=document.querySelector('#exec-finished tbody tr');return !!r&&r.textContent.indexOf('pg-fin-extra')>=0;})()`, 15000));

  // Project home: Recently done pages 10 + 2 through the same footer.
  await send("Page.navigate", { url: `${URL}/?view=project` });
  const doneRows = `document.querySelectorAll('.pj-done .prog-row.s-done').length`;
  const doneLabel = `(document.querySelector('.pj-done .pg-label')||{}).textContent||''`;
  check("recently done shows 10 of 12", await waitFor(`${doneRows}===10 && ${doneLabel}==='Showing 1-10 of 12'`), await text(doneLabel));
  await ev(`document.querySelector('[data-pg-footer="recently-done"] [data-pg="next"]').click()`);
  check("recently done last shows 2", await waitFor(`${doneLabel}==='Showing 11-12 of 12' && ${doneRows}===2`), await text(doneLabel));

  check("zero console errors or page exceptions", problems.length === 0, problems.slice(0, 3).join(",") || "clean");

  ch.kill();
  try { if (pid) process.kill(pid); } catch {}
  await sleep(1500);
  try { if (pid) process.kill(pid, 9); } catch {}
  if (!keep) fs.rmSync(tmp, { recursive: true, force: true });
  else console.log(`kept temp dirs: ${tmp}`);
  console.log(failures === 0 ? "paginate_check: PASS" : `paginate_check: FAIL (${failures})`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => { console.error("paginate_check errored:", err); process.exit(1); });
