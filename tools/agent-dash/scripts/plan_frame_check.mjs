#!/usr/bin/env node
/* Plan frame check: before/after band, objectives, path, map chips.
 *
 * Boots a TEMP server (free port, temp state dir, temp workstreams holding
 * a copy of the fixture WS1 bundle with its frame plus a frameless WS9 --
 * never the owner's real bundles or :8765), then drives the live page in
 * headless Chrome over the DevTools protocol (stdlib only) and asserts:
 *   - the plan page shows the Before/After band with both sentences, the
 *     focus line, and In/Out of scope chips;
 *   - two objective rows whose section chips link to the Execution view;
 *   - an alternatives table with exactly one chosen row;
 *   - the decisions disclosure renders collapsed;
 *   - the frameless bundle renders the no-frame hint and no band;
 *   - the Execution map tiles show objective chips and the legend gains an
 *     Objectives row with per-objective progress;
 *   - zero console errors throughout.
 * Optionally captures dark/light screenshots of both views (--shots DIR).
 *
 * The server child is killed by PID on the way out (never pkill).
 * Exit 0 when every assertion holds, 1 on any mismatch, 2 when Chrome
 * is missing.
 */
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.dirname(HERE);
const URL = (port) => `http://127.0.0.1:${port}`;
const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
].filter(Boolean);

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "plan-frame-check-"));
const wsDir = path.join(tmp, "ws");
const stateDir = path.join(tmp, "state");
const homeDir = path.join(tmp, "home");
fs.mkdirSync(wsDir, { recursive: true });
fs.mkdirSync(stateDir, { recursive: true });
fs.mkdirSync(homeDir, { recursive: true });
let shotsDir = null;
for (let i = 0; i < process.argv.length; i++) {
  if (process.argv[i] === "--shots" && process.argv[i + 1]) shotsDir = process.argv[i + 1];
  if (process.argv[i] === "-h" || process.argv[i] === "--help") {
    console.log("usage: plan_frame_check.mjs [--shots DIR]");
    process.exit(0);
  }
}
const env = { ...process.env, AGENT_DASH_IGNORE_LOCAL: "1", AGENT_DASH_STATE_DIR: stateDir,
  AGENT_DASH_WORKSTREAMS: wsDir, HOME: homeDir };
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
  fs.cpSync(path.join(ROOT, "fixtures", "bundles", "WS1"), path.join(wsDir, "WS1"), { recursive: true });
  cli("ws", "new", "WS9", "No frame yet");
  cli("ws", "render", "WS1");
  /* WS9 stays a genuine old bundle: drop the new frame skeleton so the
     no-frame fallback (today's page plus the hint line) is what renders. */
  {
    const planPath = path.join(wsDir, "WS9", "plan.json");
    const plan = JSON.parse(fs.readFileSync(planPath, "utf8"));
    delete plan.frame;
    fs.writeFileSync(planPath, JSON.stringify(plan, null, 2) + "\n");
    cli("ws", "render", "WS9");
  }

  const PORT = await freePort();
  const base = URL(PORT);
  const server = spawn("python3", ["agent_dash.py", "run"], {
    cwd: ROOT, env: { ...env, AGENT_DASH_PORT: String(PORT) }, stdio: "ignore",
  });
  const pidFile = path.join(stateDir, "agent-dash.pid");
  let pid = null;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  for (let i = 0; i < 100; i++) {
    await sleep(300);
    try {
      const h = await (await fetch(`${base}/api/health`)).json();
      if (h.ok !== undefined) break;
    } catch {}
  }
  try { pid = Number(fs.readFileSync(pidFile, "utf8").trim().split(/\s+/)[0]); } catch {}
  check("temp server answers", pid !== null, `pid ${pid}`);

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
    console.log(code === 0 && failures === 0 ? "plan_frame_check: PASS" : `plan_frame_check: FAIL (${failures})`);
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
  async function shot(name, url) {
    if (!shotsDir) return;
    await send("Page.navigate", { url });
    await waitFor(`document.querySelector('#plan-body, #exec-progress')`, 20000);
    await sleep(2500);
    const { data } = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
    fs.mkdirSync(shotsDir, { recursive: true });
    fs.writeFileSync(path.join(shotsDir, name), Buffer.from(data, "base64"));
    console.log(`shot  ${name}`);
  }

  await send("Page.enable");
  await send("Runtime.enable");
  const planState = `(${[
    "function(){",
    "var b=document.querySelector('#plan-body');",
    "if(!b||!b.textContent.trim())return null;",
    "var band=b.querySelector('[data-frame-band]');",
    "var cards=Array.prototype.slice.call(b.querySelectorAll('.obj-card'));",
    "var rows=b.querySelectorAll('table.alt-table tbody tr');",
    "var chosen=b.querySelectorAll('table.alt-table tr.chosen');",
    "var det=Array.prototype.filter.call(b.querySelectorAll('details.more'),function(d){return /Decisions/.test(d.textContent.slice(0,80));})[0];",
    "return {",
    "band:band?band.textContent:'',",
    "inChips:b.querySelectorAll('.scope-chip.in').length,",
    "outChips:b.querySelectorAll('.scope-chip.out').length,",
    "cards:cards.map(function(c){return c.querySelectorAll('a.obj-chip').length;}),",
    "links:Array.prototype.map.call(b.querySelectorAll('a.obj-chip'),function(a){return a.getAttribute('href');}),",
    "rows:rows.length,chosen:chosen.length,",
    "decisions:det?{open:det.open,text:det.textContent.slice(0,60)}:null,",
    "hint:!!b.querySelector('.frame-hint')};",
    "}",
  ].join("")})()`;

  await send("Page.navigate", { url: `${base}/?ws=WS1&view=plan` });
  check("plan view settles with the frame band",
    await waitFor(`${planState} && ${planState}.band`, 20000));
  const st = await text(planState);
  check("band carries both sentences",
    /p95 read latency sits at 900 ms/.test(st.band || "") && /p95 latency under 200 ms/.test(st.band || ""),
    (st.band || "").slice(0, 120));
  check("scope chips render", st.inChips >= 1 && st.outChips >= 1, `in=${st.inChips} out=${st.outChips}`);
  check("two objective rows with section chips", JSON.stringify(st.cards) === "[1,1]", JSON.stringify(st.cards));
  check("objective chips link to the execution view",
    st.links.length === 2 && st.links.every((h) => /view=exec/.test(h)) &&
    st.links.some((h) => h.indexOf("sec-start") >= 0) && st.links.some((h) => h.indexOf("sec-retire") >= 0),
    JSON.stringify(st.links));
  check("alternatives table has exactly one chosen row", st.rows === 3 && st.chosen === 1, `rows=${st.rows} chosen=${st.chosen}`);
  check("decisions disclosure renders collapsed", !!st.decisions && st.decisions.open === false, JSON.stringify(st.decisions));

  await send("Page.navigate", { url: `${base}/?ws=WS9&view=plan` });
  check("frameless plan shows the hint and no band",
    await waitFor(`(${[
      "function(){var b=document.querySelector('#plan-body');",
      "return b&&b.textContent.trim()&&b.querySelector('.frame-hint')&&!b.querySelector('[data-frame-band]');}"
    ].join("")})()`, 20000));

  const mapState = `(${[
    "function(){",
    "var card=document.querySelector('#exec-progress');",
    "if(!card)return null;",
    "var tiles=Array.prototype.slice.call(card.querySelectorAll('.dnode'));",
    "var leg=card.querySelector('.map-legend');",
    "return {",
    "tiles:tiles.map(function(t){return Array.prototype.map.call(t.querySelectorAll('.obj-chip'),function(c){return c.textContent+'|'+c.getAttribute('title');});}),",
    "legend:leg?leg.textContent:'',",
    "objItems:leg?Array.prototype.map.call(leg.querySelectorAll('.mleg-group'),function(g){return g.getAttribute('aria-label')+':'+g.textContent;}).filter(function(s){return /^Objectives/.test(s);}):[]};",
    "}",
  ].join("")})()`;
  await send("Page.navigate", { url: `${base}/?ws=WS1&view=exec` });
  check("execution map settles with two tiles",
    await waitFor(`${mapState} && ${mapState}.tiles.length===2`, 20000));
  const mp = await text(mapState);
  check("tiles show objective chips",
    mp.tiles.length === 2 && mp.tiles[0].join(" ").indexOf("O1") === 0 && mp.tiles[1].join(" ").indexOf("O2") === 0,
    JSON.stringify(mp.tiles));
  check("objective chip hover names the objective title",
    /One read route/.test(mp.tiles[0].join(" ")) && /No legacy readers/.test(mp.tiles[1].join(" ")),
    JSON.stringify(mp.tiles));
  check("legend gains an Objectives row with progress",
    mp.objItems.length === 1 && /O1 2\/3/.test(mp.objItems[0]) && /O2 0\/2/.test(mp.objItems[0]),
    JSON.stringify(mp.objItems));

  check("zero console errors or page exceptions", problems.length === 0, problems.slice(0, 3).join(",") || "clean");

  if (shotsDir) {
    await shot("plan-dark.png", `${base}/?ws=WS1&view=plan&theme=dark`);
    await shot("plan-light.png", `${base}/?ws=WS1&view=plan&theme=light`);
    await shot("exec-dark.png", `${base}/?ws=WS1&view=exec&theme=dark`);
    await shot("exec-light.png", `${base}/?ws=WS1&view=exec&theme=light`);
  }

  await done(0);
}

main().catch((err) => { console.error(`plan_frame_check errored: ${err}`); process.exit(1); });
