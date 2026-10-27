#!/usr/bin/env node
/* Planned/archive workstream switching check: the header switcher must
 * move the selection for planned (and archived) rows, not just flip the
 * view.
 *
 * Boots a TEMP server (port 8907, temp state dir, temp workstreams with
 * synthetic bundles -- never the owner's real bundles or :8765) holding
 * two active, two planned and one archived workstream, then drives the
 * live page in headless Chrome over the DevTools protocol (stdlib only):
 * from ?ws=WS1&view=planned the switcher picks WS88 (planned) and the URL
 * becomes ?ws=WS88&view=planned with the chip following; picking WS89 from
 * inside the Planned view (the old silent no-op) moves to
 * ?ws=WS89&view=planned without a reload; switching back to WS88 works the
 * same way; the Planned list "Plan WS88" button opens ?ws=WS88&view=plan;
 * the switcher picks the archived WS14 into ?ws=WS14&view=archive.
 * The planned ids sit far above any real workstream: live-agent counts come
 * from the machine process table, which the temp server cannot isolate, so
 * a real ws8 agent would otherwise give the synthetic WS8 activity.
 * Zero console errors throughout.
 *
 * Usage: node scripts/planned_switch_check.mjs [--keep]   (keeps the temp dirs)
 * Exit 0 on PASS, 1 on the first failed assertion, 2 when Chrome is missing.
 */
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.dirname(HERE);
const PORT = 8907;
const URL = `http://127.0.0.1:${PORT}`;
const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
].filter(Boolean);

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "planned-switch-check-"));
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
function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once("error", reject);
    s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}

async function main() {
  cli("ws", "new", "WS1", "Lane WS1");
  cli("ws", "new", "WS2", "Lane WS2");
  cli("ws", "new", "WS88", "Lane WS88", "--state", "planned");
  cli("ws", "new", "WS89", "Lane WS89", "--state", "planned");
  cli("ws", "new", "WS14", "Lane WS14");
  {
    const planPath = path.join(wsDir, "WS14", "plan.json");
    const plan = JSON.parse(fs.readFileSync(planPath, "utf8"));
    for (const g of plan.dag.groups) for (const t of g.tasks) t.status = "done";
    fs.writeFileSync(planPath, JSON.stringify(plan));
    fs.mkdirSync(path.join(wsDir, "WS14", "docs"), { recursive: true });
    fs.writeFileSync(path.join(wsDir, "WS14", "docs", "closing-report.md"), "# Closing report\n\nShipped.\n");
    cli("ws", "state", "WS14", "done");
  }
  cli("ws", "archive", "WS14");

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
  check("temp server answers on 8907", pid !== null, `pid ${pid}`);

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
  const done = async (code) => {
    try { ch.kill(); } catch {}
    try { if (pid) process.kill(pid); } catch {}
    await sleep(1200);
    try { if (pid) process.kill(pid, 9); } catch {}
    if (!keep) fs.rmSync(tmp, { recursive: true, force: true });
    console.log(code === 0 && failures === 0 ? "planned_switch_check: PASS" : `planned_switch_check: FAIL (${failures})`);
    process.exit(code === 0 && failures === 0 ? 0 : 1);
  };
  if (!chromeBin) {
    console.error("SKIP: no Chrome");
    await done(0).catch(() => process.exit(2));
    process.exit(2);
  }
  const dbgPort = await freePort();
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
  if (!tgt) { await done(1); return; }
  const ws = new WebSocket(tgt.webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));
  let id = 0;
  const pend = {};
  const problems = [];
  ws.onmessage = (e) => {
    const m = JSON.parse(typeof e.data === "string" ? e.data : e.data.toString());
    if (m.method === "Runtime.consoleAPICalled" && m.params && m.params.type === "error")
      problems.push("console.error: " + JSON.stringify(m.params.args || []).slice(0, 200));
    if (m.method === "Runtime.exceptionThrown") problems.push("exception: " + JSON.stringify(m.params.exceptionDetails || {}).slice(0, 200));
    if (m.id && pend[m.id]) pend[m.id](m.result);
  };
  const send = (method, params = {}) => new Promise((r) => { pend[++id] = r; ws.send(JSON.stringify({ id, method, params })); });
  const ev = (expression, awaitPromise = false) => send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise });
  const text = async (expr) => (await ev(expr)).result.value;
  async function waitFor(expr, timeoutMs = 15000) {
    const end = Date.now() + timeoutMs;
    for (;;) {
      if (await text(expr)) return true;
      if (Date.now() > end) return false;
      await sleep(400);
    }
  }
  async function pickFromSwitcher(wsId) {
    await ev(`document.querySelector('#ws-switcher').click()`);
    if (!await waitFor(`!!document.getElementById('ws-switcher-pop')`, 8000)) return false;
    if (!await waitFor(`!!document.querySelector('.ws-row[data-ws="${wsId}"]')`, 8000)) return false;
    await ev(`(function(){var b=document.querySelector('.ws-row[data-ws="${wsId}"]');if(b)b.click();})()`);
    return true;
  }
  // Exact view-param match: "view=planned" contains the substring "view=plan".
  const viewIs = (v) => `new RegExp('[?&]view=${v}([&#]|$)').test(location.search)`;
  async function waitPlannedSettled(n) {
    return waitFor(`document.querySelectorAll('#planned-ready-rows tr').length === ${n}`, 20000);
  }

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.navigate", { url: `${URL}/?ws=WS1&view=planned` });
  check("planned view settles with two planned rows",
    await waitFor(`document.querySelectorAll('#planned-ready-rows tr').length === 2`), "readiness table");
  check("planned list offers both plan buttons",
    await waitFor(`Array.prototype.some.call(document.querySelectorAll('button'),function(b){return b.textContent==='Plan WS88';})`
      + ` && Array.prototype.some.call(document.querySelectorAll('button'),function(b){return b.textContent==='Plan WS89';})`));

  // Header switcher: planned row from outside the Planned-path no-op.
  await ev(`window.__mark='stay'`);
  check("switcher opens", await (async () => {
    await ev(`document.querySelector('#ws-switcher').click()`);
    return waitFor(`!!document.getElementById('ws-switcher-pop')`, 8000);
  })());
  const plannedLabel = await text(`(document.querySelector('.ws-row[data-ws="WS88"]')||{}).getAttribute ? document.querySelector('.ws-row[data-ws="WS88"]').getAttribute('aria-label') : ''`);
  check("switcher lists WS88 as planned", /Planned/.test(plannedLabel || ""), plannedLabel);
  await ev(`document.querySelector('.ws-row[data-ws="WS88"]').click()`);
  check("picking planned WS88 moves the URL to ?ws=WS88&view=planned",
    await waitFor(`location.href.indexOf('ws=WS88')>=0 && ${viewIs("planned")}`, 8000), "header pick");
  check("selection chip follows to WS88",
    await waitFor(`(document.querySelector('#selected-chip')||{}).textContent
      && document.querySelector('#selected-chip').textContent.indexOf('WS88')>=0`, 8000));
  check("planned list resettles on WS88", await waitPlannedSettled(2));
  check("no reload on the switch", await text(`window.__mark==='stay'`));

  // The reported no-op: picking another planned row while already in the
  // Planned view must move the selection without a reload.
  await ev(`window.__mark='stay'`);
  check("switcher reopens in planned view", await pickFromSwitcher("WS89"));
  check("picking planned WS89 from the planned view moves to ?ws=WS89&view=planned",
    await waitFor(`location.href.indexOf('ws=WS89')>=0 && ${viewIs("planned")}`, 8000));
  check("selection chip follows to WS89",
    await waitFor(`document.querySelector('#selected-chip').textContent.indexOf('WS89')>=0`, 8000));
  check("planned list resettles on WS89", await waitPlannedSettled(2));
  check("no reload switching planned to planned", await text(`window.__mark==='stay'`));

  // And back again.
  await ev(`window.__mark='stay'`);
  check("switching back to WS88 works", await pickFromSwitcher("WS88"));
  check("back at ?ws=WS88&view=planned",
    await waitFor(`location.href.indexOf('ws=WS88')>=0 && ${viewIs("planned")}`, 8000));
  check("planned list resettles back on WS88", await waitPlannedSettled(2));
  check("no reload switching back", await text(`window.__mark==='stay'`));

  // Planned list: its Plan button opens that workstream's plan view. The
  // list only carries the buttons once the post-pick refresh resettles,
  // so confirm one is present before clicking (a loading skeleton has none).
  check("plan button present after resettle",
    await waitFor(`Array.prototype.some.call(document.querySelectorAll('button'),function(b){return b.textContent==='Plan WS88';})`, 8000));
  await ev(`(function(){var bs=Array.prototype.slice.call(document.querySelectorAll('button'));for(var i=0;i<bs.length;i++){if(bs[i].textContent==='Plan WS88'){bs[i].click();break;}}})()`);
  check("planned list opens WS88 plan view",
    await waitFor(`location.href.indexOf('ws=WS88')>=0 && ${viewIs("plan")}`, 8000));
  check("plan view shows the WS88 bundle",
    await waitFor(`!!document.querySelector('#plan-body') && !document.querySelector('#plan-view').hidden
      && document.querySelector('#plan-body').textContent.trim().length>0`, 15000));

  // Archived rows select into the Archive view.
  await send("Page.navigate", { url: `${URL}/?ws=WS1&view=archive` });
  check("archive view settles", await waitFor(`!!document.querySelector('#archive-shelf-list')`, 15000));
  await ev(`window.__mark='stay'`);
  check("switcher picks archived WS14", await pickFromSwitcher("WS14"));
  check("archived pick moves to ?ws=WS14&view=archive",
    await waitFor(`location.href.indexOf('ws=WS14')>=0 && ${viewIs("archive")}`, 8000));
  check("no reload switching to archived", await text(`window.__mark==='stay'`));

  check("zero console errors or page exceptions", problems.length === 0, problems.slice(0, 3).join(",") || "clean");

  await done(0);
}

main().catch((err) => { console.error("planned_switch_check errored:", err); process.exit(1); });
