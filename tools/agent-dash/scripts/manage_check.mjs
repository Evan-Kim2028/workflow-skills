#!/usr/bin/env node
/* End-to-end management check: state, bulk, undo, and the calm bar.
 *
 * Boots a TEMP server (port 8898, temp state dir, temp workstreams with
 * synthetic bundles -- never the owner's real bundles or :8765) holding
 * 14 synthetic workstreams in mixed states, then drives the live page in
 * headless Chrome over the DevTools protocol (stdlib only):
 * open the switcher with "/", filter, pick, pin one; Archive done in
 * bulk and assert Current loses them while Archive gains them; Undo and
 * assert they return; pause then resume one; assert zero console errors
 * and a pinned bar at most 48 px on desktop and 44 px on phones, with 14
 * and with 40 workstreams.
 *
 * Usage: node scripts/manage_check.mjs [--keep]   (keeps the temp dirs)
 * Exit 0 on PASS, 1 on the first failed assertion.
 */
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.dirname(HERE);
const PORT = 8898;
const URL = `http://127.0.0.1:${PORT}`;
const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
].filter(Boolean);

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "manage-check-"));
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
function seedOne(id, title, state, done) {
  cli("ws", "new", id, title, ...(state !== "active" ? ["--state", state] : []));
  const planPath = path.join(wsDir, id, "plan.json");
  const plan = JSON.parse(fs.readFileSync(planPath, "utf8"));
  if (done) {
    for (const g of plan.dag.groups) for (const t of g.tasks) t.status = "done";
    fs.writeFileSync(planPath, JSON.stringify(plan));
    fs.mkdirSync(path.join(wsDir, id, "docs"), { recursive: true });
    fs.writeFileSync(path.join(wsDir, id, "docs", "closing-report.md"), "# Closing report\n\nShipped.\n");
    cli("ws", "state", id, "done");
  }
}
async function apiJson(p, opts = {}) {
  const r = await fetch(URL + p, opts);
  const j = await r.json();
  return { status: r.status, body: j };
}
async function main() {
  // 14 synthetic workstreams in mixed states.
  const actives = ["WS1", "WS2", "WS3", "WS4", "WS5"];
  const paused = ["WS6", "WS7"];
  const planned = ["WS8", "WS9", "WS10"];
  const dones = ["WS11", "WS12", "WS13"];
  for (const id of actives) seedOne(id, `Lane ${id}`, "active", false);
  for (const id of paused) { seedOne(id, `Lane ${id}`, "active", false); cli("ws", "state", id, "paused"); }
  for (const id of planned) seedOne(id, `Lane ${id}`, "planned", false);
  for (const id of dones) seedOne(id, `Lane ${id}`, "active", true);
  seedOne("WS14", "Lane WS14", "active", true);
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
  check("temp server answers on 8898", pid !== null, `pid ${pid}`);

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
  const dbgPort = 9455;
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
  const key = (type, k) => send("Input.dispatchKeyEvent", type === "press" ? { type: "keyDown", key: k, windowsVirtualKeyCode: k === "/" ? 191 : k === "Enter" ? 13 : 0 } : { type: k });
  const text = async (expr) => (await ev(expr)).result.value;
  async function waitFor(expr, timeoutMs = 15000) {
    const end = Date.now() + timeoutMs;
    for (;;) {
      if (await text(expr)) return true;
      if (Date.now() > end) return false;
      await sleep(400);
    }
  }

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
  check("current table renders 13 open rows", await waitFor(
    `document.querySelectorAll('[data-mg-rows] tr').length >= 13`), "bulk of the 14 minus archived");

  // Bar height on desktop with 14 workstreams.
  const barH = await text(`Math.round(document.querySelector('.topbar').getBoundingClientRect().height)`);
  check("pinned bar is at most 48px on desktop (14 workstreams)", barH <= 48, `got ${barH}px`);

  // Switcher: "/" opens, filter narrows, arrows+Enter picks.
  await key("press", "/");
  check("slash opens the switcher", await waitFor(`!!document.getElementById('ws-switcher-pop')`));
  await ev(`(function(){var i=document.querySelector('#ws-switcher-pop input');i.value='ws1';i.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  await sleep(600);
  const visible = await text(`Array.prototype.filter.call(document.querySelectorAll('#ws-switcher-pop .ws-row'),function(b){return b.offsetParent!==null;}).length`);
  const allMatch = await text(`Array.prototype.every.call(document.querySelectorAll('#ws-switcher-pop .ws-row'),function(b){return b.offsetParent===null||/ws1/i.test(b.getAttribute('data-ws')+' '+b.textContent);})`);
  check("switcher filter narrows by id", visible >= 1 && visible < 13 && allMatch, `${visible} visible`);
  const firstWs = await text(`document.querySelector('#ws-switcher-pop .ws-row').getAttribute('data-ws')`);
  await key("press", "Enter");
  check("enter picks the hot row", await waitFor(`location.href.indexOf('ws=${firstWs}')>=0`, 8000), `picked ${firstWs}`);
  await send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
  await waitFor(`document.querySelectorAll('[data-mg-rows] tr').length >= 13`);

  // Pin one workstream from the switcher.
  await key("press", "/");
  await waitFor(`!!document.getElementById('ws-switcher-pop')`);
  await ev(`(function(){var rows=document.querySelectorAll('#ws-switcher-pop .ws-row');for(var i=0;i<rows.length;i++){if(rows[i].getAttribute('data-ws')==='WS2'){rows[i].querySelector('.ws-pin').click();break;}}})()`);
  await sleep(600);
  const pins = await text(`(function(){try{return localStorage.getItem('agent-dash-pins');}catch(e){return 'ERR';}})()`);
  check("pinning stores one viewer-local pin", !!pins && pins.indexOf("WS2") >= 0, pins);
  await key("press", "Escape");
  const pinChip = await text(`(document.querySelector('#ws-pins .pin-chip .pin-id')||{}).textContent||''`);
  check("pinned chip shows in the bar", pinChip === "WS2", pinChip || "none");

  // Bulk Archive done, with one confirm listing them.
  const doneBefore = (await apiJson("/api/workstreams")).body.workstreams.filter((r) => r.state === "done").map((r) => r.ws).sort();
  check("three done lanes await archive", JSON.stringify(doneBefore) === JSON.stringify(dones), doneBefore.join(","));
  await ev(`document.querySelector('[data-mg-archdone]').click()`);
  check("bulk confirm lists every done lane", await waitFor(
    `!!document.querySelector('.arch-confirm') && ${JSON.stringify(doneBefore)}.every(function(id){return document.querySelector('.arch-confirm').textContent.indexOf(id)>=0;})`));
  await ev(`document.querySelector('.arch-confirm .arch-btn').click()`);
  check("toast offers undo after bulk archive", await waitFor(`!!document.querySelector('#toast.show .toast-undo')`));
  const currentAfter = (await apiJson("/api/workstreams")).body.workstreams.map((r) => r.ws);
  const archivedAfter = (await apiJson("/api/workstreams?state=archived")).body.workstreams.map((r) => r.ws);
  check("current loses the archived lanes", doneBefore.every((x) => !currentAfter.includes(x)), `${currentAfter.length} rows left`);
  check("archive gains them", doneBefore.every((x) => archivedAfter.includes(x)), archivedAfter.join(","));

  // Undo restores all that changed.
  await ev(`document.querySelector('#toast .toast-undo').click()`);
  await waitFor(`document.querySelectorAll('[data-mg-rows] tr').length >= 13`, 20000);
  const currentBack = (await apiJson("/api/workstreams")).body.workstreams.map((r) => r.ws);
  check("undo restores every lane to current", doneBefore.every((x) => currentBack.includes(x)));

  // Pause then resume one workstream from its row.
  await ev(`(function(){var tr=document.querySelector('[data-mg-rows] tr[data-ws="WS1"]');var btns=tr.querySelectorAll('button');for(var i=0;i<btns.length;i++){if(btns[i].textContent==='Pause'){btns[i].click();break;}}})()`);
  check("pausing flips the state pill", await waitFor(
    `document.querySelector('[data-mg-rows] tr[data-ws="WS1"] .prog-pill').textContent.indexOf('Paused')>=0`, 20000));
  await ev(`(function(){var tr=document.querySelector('[data-mg-rows] tr[data-ws="WS1"]');var btns=tr.querySelectorAll('button');for(var i=0;i<btns.length;i++){if(btns[i].textContent==='Resume'){btns[i].click();break;}}})()`);
  check("resuming flips it back", await waitFor(
    `document.querySelector('[data-mg-rows] tr[data-ws="WS1"] .prog-pill').textContent.indexOf('Active')>=0`, 20000));

  // Phone: bar height and no sideways page scroll.
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
  await waitFor(`document.querySelectorAll('[data-mg-rows] tr').length >= 13`);
  const phoneH = await text(`Math.round(document.querySelector('.topbar').getBoundingClientRect().height)`);
  const sideways = await text(`document.documentElement.scrollWidth - document.documentElement.clientWidth`);
  check("pinned bar is at most 44px on phones (14 workstreams)", phoneH <= 44, `got ${phoneH}px`);
  check("no sideways page scroll on phones", sideways <= 1, `overflow ${sideways}px`);

  // Scale to 40 workstreams: the bar must stay calm on both widths.
  for (let i = 15; i <= 40; i++) {
    const st = i % 3 === 0 ? "paused" : i % 3 === 1 ? "planned" : "active";
    seedOne(`WS${i}`, `Lane WS${i}`, st === "planned" ? "planned" : "active", false);
    if (st === "paused") cli("ws", "state", `WS${i}`, "paused");
  }
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
  await waitFor(`document.querySelectorAll('[data-mg-rows] tr').length >= 39`, 30000);
  const bar40 = await text(`Math.round(document.querySelector('.topbar').getBoundingClientRect().height)`);
  check("pinned bar is at most 48px on desktop (40 workstreams)", bar40 <= 48, `got ${bar40}px`);
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
  await waitFor(`document.querySelectorAll('[data-mg-rows] tr').length >= 39`, 30000);
  const phone40 = await text(`Math.round(document.querySelector('.topbar').getBoundingClientRect().height)`);
  check("pinned bar is at most 44px on phones (40 workstreams)", phone40 <= 44, `got ${phone40}px`);

  check("zero console errors or page exceptions", problems.length === 0, problems.slice(0, 3).join(",") || "clean");

  ch.kill();
  try { if (pid) process.kill(pid); } catch {}
  await sleep(1500);
  try { if (pid) process.kill(pid, 9); } catch {}
  if (!keep) fs.rmSync(tmp, { recursive: true, force: true });
  console.log(failures === 0 ? "manage_check: PASS" : `manage_check: FAIL (${failures})`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => { console.error("manage_check errored:", err); process.exit(1); });
