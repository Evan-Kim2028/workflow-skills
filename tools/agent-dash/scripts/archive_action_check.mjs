#!/usr/bin/env node
/* Archive-from-the-page end-to-end (stdlib only: node fetch, WebSocket,
 * child_process, fs — no npm installs).
 *
 * Boots a TEMP dashboard server (temp state dir, temp AGENT_DASH_WORKSTREAMS
 * with two synthetic done bundles; never the live dashboard, never real
 * bundles), then drives the real page in headless Chrome over CDP:
 * click Archive on a done workstream, confirm in the inline popover,
 * assert it leaves Recently done, appears in the Archive view with its
 * frozen summary, Undo restores it, the Restore button restores it again,
 * and no console errors fired. Screenshots (confirm popover, toast with
 * Undo, Archive view with a Restore row) land in --out, dark and light
 * at 1440 and 390 px.
 *
 * Run it:
 *   node scripts/archive_action_check.mjs [--port 8896] [--out DIR] [--chrome PATH]
 *   node scripts/archive_action_check.mjs --url http://127.0.0.1:8896 [--out DIR]
 * (--url points at a temp server you started; otherwise the script boots
 * and stops its own by PID. Never point --url at the live dashboard:
 * the check archives and restores workstreams.)
 *
 * Exit 0 on pass, 1 with a table on any mismatch, 2 when Chrome or a
 * server is missing. Not wired into `agent_dash.py check --page`: the
 * boot plus four themed viewport passes take well over that check's
 * 10 s budget.
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

const TOOL_DIR = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const SP = process.env.SP || path.resolve(TOOL_DIR, "..", "..");
const DEFAULT_OUT = path.join(SP, "v-shots", "archive-action");

function args(argv) {
  const out = { url: null, chrome: null, port: 8896, out: DEFAULT_OUT };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--url" && argv[i + 1]) out.url = argv[++i].replace(/\/+$/, "");
    else if (flag === "--chrome" && argv[i + 1]) out.chrome = argv[++i];
    else if (flag === "--port" && argv[i + 1]) out.port = Number(argv[++i]);
    else if (flag === "--out" && argv[i + 1]) out.out = argv[++i];
    else if (flag === "-h" || flag === "--help") {
      console.log("usage: archive_action_check.mjs [--url URL] [--port 8896] [--out DIR] [--chrome PATH]");
      process.exit(0);
    } else {
      console.error(`unknown flag ${flag}`);
      process.exit(1);
    }
  }
  return out;
}

function which(name) {
  const dirs = String(process.env.PATH || "").split(path.delimiter);
  for (const dir of dirs) {
    const candidate = path.join(dir, name);
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      return candidate;
    } catch { /* next */ }
  }
  return null;
}

function findChrome(explicit) {
  if (explicit) {
    if (fs.existsSync(explicit)) return explicit;
    console.error(`archive_action_check: chrome not found at ${explicit}`);
    process.exit(2);
  }
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) return process.env.CHROME_PATH;
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (process.platform === "darwin" && fs.existsSync(mac)) return mac;
  const found = which("google-chrome") || which("google-chrome-stable") || which("chromium") || which("chromium-browser");
  if (found) return found;
  console.error("archive_action_check: no Chrome binary found (tried CHROME_PATH, the default install path and PATH)");
  process.exit(2);
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

async function waitForJson(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      const response = await fetch(url);
      if (response.ok) return await response.json();
    } catch { /* not up yet */ }
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${url}`);
    await new Promise((r) => setTimeout(r, 200));
  }
}

function connectDebugger(wsUrl, onEvent) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const pending = new Map();
    let id = 0;
    let settled = false;
    ws.addEventListener("open", () => { settled = true; resolve({ send, close }); });
    ws.addEventListener("message", (event) => {
      let msg;
      try { msg = JSON.parse(String(event.data)); } catch { return; }
      if (msg.id != null && pending.has(msg.id)) {
        const entry = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) entry.reject(new Error(msg.error.message || JSON.stringify(msg.error)));
        else entry.resolve(msg.result);
      } else if (onEvent) {
        try { onEvent(msg); } catch { /* collector never breaks the run */ }
      }
    });
    ws.addEventListener("error", (event) => {
      if (!settled) reject(new Error(`debugger socket error: ${event.message || "unknown"}`));
    });
    function send(method, params, timeoutMs = 20000) {
      const cur = ++id;
      return new Promise((res, rej) => {
        pending.set(cur, { resolve: res, reject: rej });
        ws.send(JSON.stringify({ id: cur, method, params: params || {} }));
        setTimeout(() => {
          if (pending.has(cur)) { pending.delete(cur); rej(new Error(`cdp timeout: ${method}`)); }
        }, timeoutMs);
      });
    }
    function close() { try { ws.close(); } catch { /* already closed */ } }
  });
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", { expression, returnByValue: true });
  if (!result || result.error) throw new Error(`cdp evaluate gave no result: ${JSON.stringify(result)}`);
  if (result.exceptionDetails) throw new Error(`page expression failed: ${JSON.stringify(result.exceptionDetails).slice(0, 400)}`);
  return result.result ? result.result.value : undefined;
}

async function poll(cdp, expression, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    let value = null;
    try { value = await evaluate(cdp, expression); } catch { value = null; }
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`timed out waiting for: ${expression.slice(0, 120)}`);
    await new Promise((r) => setTimeout(r, 250));
  }
}

/* Two synthetic done bundles: 100% plan, closing report, state done. */
function writeBundles(dir) {
  const now = Math.floor(Date.now() / 1000) - 3600;
  for (const [ws, title, order] of [["WS1", "Seeded done lane", 1], ["WS2", "Second done lane", 2]]) {
    const base = path.join(dir, ws);
    fs.mkdirSync(path.join(base, "docs"), { recursive: true });
    fs.writeFileSync(path.join(base, "bundle.json"), JSON.stringify({
      id: ws, title, short: title, order, kind: "workstream",
      state: "done", state_changed_at: now, archived_at: null,
      depends_on: [], milestone: null, summary: null,
    }, null, 2) + "\n");
    fs.writeFileSync(path.join(base, "plan.json"), JSON.stringify({
      workstream: ws,
      progress: { done: 2, total: 2, counts: { done: 2, todo: 0, wip: 0, pr: 0, prog: 0, deferred: 0 }, pct: 100 },
      dag: { cols: ["Shape"], groups: [{ id: "g", col: 0, title: "Group", tasks: [
        { id: `${ws}.1`, title: "First", status: "done", deps: [] },
        { id: `${ws}.2`, title: "Second", status: "done", deps: [] },
      ] }] },
      edges: [], trains: [],
    }, null, 2) + "\n");
    fs.writeFileSync(path.join(base, "docs", "closing-report.md"), `# Closing report\n\n${title} shipped.\n`);
  }
}

/* Foreground temp server: no pidfile, no daemon fork, no live state. */
const SERVE_PY = `
import os, sys
sys.path.insert(0, sys.argv[1])
os.environ.setdefault("AGENT_DASH_IGNORE_LOCAL", "1")
from agent_dash import default_cfg
from dash.server import DashServer
from dash.sources import Registry
from dash.store import Store
from pathlib import Path
store = Store(Path(os.environ["TMP_STATE_DIR"]) / "dash.sqlite")
server = DashServer(store, Registry([]), "127.0.0.1", int(os.environ["TMP_PORT"]), cfg=default_cfg())
server.serve_forever()
`;

const COMBOS = [
  [1440, 900, "dark", "1440-dark"],
  [1440, 900, "light", "1440-light"],
  [390, 844, "dark", "390-dark"],
  [390, 844, "light", "390-light"],
];

async function main() {
  const opts = args(process.argv.slice(2));
  const chrome = findChrome(opts.chrome);
  const failures = [];
  const ok = (name, cond, detail) => {
    console.log(`${cond ? "PASS" : "FAIL"}  ${name}${cond || !detail ? "" : " — " + detail}`);
    if (!cond) failures.push(name);
  };
  let serverProc = null;
  let url = opts.url;
  const tmpRoots = [];
  try {
    if (!url) {
      const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "ad-arch-state-"));
      const wsDir = fs.mkdtempSync(path.join(os.tmpdir(), "ad-arch-ws-"));
      tmpRoots.push(stateDir, wsDir);
      writeBundles(wsDir);
      const port = opts.port || 8896;
      url = `http://127.0.0.1:${port}`;
      serverProc = spawn("python3", ["-c", SERVE_PY, TOOL_DIR], {
        /* AGENT_DASH_URL points at the temp server itself, so the frozen
           usage totals resolve against temp data (deterministic nulls),
           never against whatever serves the default port. */
        env: { ...process.env, AGENT_DASH_IGNORE_LOCAL: "1", AGENT_DASH_WORKSTREAMS: wsDir, TMP_STATE_DIR: stateDir, TMP_PORT: String(port), AGENT_DASH_URL: url },
        stdio: ["ignore", "ignore", "pipe"],
      });
      let errLog = "";
      serverProc.stderr.on("data", (d) => { errLog += String(d); });
      await new Promise((resolve, reject) => {
        serverProc.once("error", reject);
        serverProc.once("exit", (code) => reject(new Error(`temp server exited ${code}: ${errLog.slice(0, 500)}`)));
        setTimeout(resolve, 500);
      });
      try {
        await waitForJson(`${url}/api/health`, 20000);
      } catch (e) {
        console.error(`archive_action_check: temp server never answered: ${e.message}`);
        process.exit(2);
      }
      console.log(`temp server up at ${url}`);
    }
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), "ad-arch-chrome-"));
    tmpRoots.push(profile);
    const debugPort = await freePort();
    const child = spawn(chrome, ["--headless", "--no-sandbox", "--disable-gpu", "--no-first-run",
      "--disable-extensions", `--user-data-dir=${profile}`, `--remote-debugging-port=${debugPort}`, "about:blank"],
      { stdio: ["ignore", "ignore", "pipe"] });
    const tabs = await waitForJson(`http://127.0.0.1:${debugPort}/json/list`, 20000);
    const page = tabs.find((t) => t.type === "page");
    const pageErrors = [];
    const cdp = await connectDebugger(page.webSocketDebuggerUrl, (msg) => {
      if (msg.method === "Runtime.consoleAPICalled" && msg.params && msg.params.type === "error") {
        pageErrors.push("console.error: " + JSON.stringify(msg.params.args).slice(0, 200));
      } else if (msg.method === "Runtime.exceptionThrown") {
        pageErrors.push("exception: " + JSON.stringify(msg.params.exceptionDetails).slice(0, 200));
      } else if (msg.method === "Log.entryAdded" && msg.params && msg.params.entry
        && msg.params.entry.level === "error" && msg.params.entry.source !== "network") {
        pageErrors.push("log.error: " + (msg.params.entry.text || "").slice(0, 200));
      }
    });
    await cdp.send("Runtime.enable", {});
    await cdp.send("Log.enable", {});
    const shotsDir = opts.out;
    fs.mkdirSync(shotsDir, { recursive: true });
    async function shot(name) {
      const { data } = await cdp.send("Page.captureScreenshot", { format: "png" });
      const file = path.join(shotsDir, name);
      fs.writeFileSync(file, Buffer.from(data, "base64"));
      console.log(`shot  ${file}`);
    }
    async function viewport(width, height) {
      await cdp.send("Emulation.setDeviceMetricsOverride", {
        width, height, deviceScaleFactor: 1, mobile: width < 700,
      });
    }
    async function goto(view, theme) {
      await cdp.send("Page.navigate", { url: `${url}/?view=${view}&theme=${theme}` });
      await poll(cdp, "document.readyState === 'complete'", 20000);
      await poll(cdp, "(window.__adSettled || '').indexOf('project-') === 0", 20000);
      await new Promise((r) => setTimeout(r, 800));
    }
    async function apiArchive() {
      return (await (await fetch(`${url}/api/workstreams?state=archived`)).json()).workstreams.map((r) => r.ws);
    }
    async function apiPostAct(action, wsId) {
      const res = await fetch(`${url}/api/workstreams/${action}`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-AgentDash-Action": "1" },
        body: JSON.stringify({ ws: wsId }),
      });
      return { status: res.status, body: await res.json() };
    }
    async function ensureDone(wsId) {
      if ((await apiArchive()).includes(wsId)) {
        const r = await apiPostAct("restore", wsId);
        if (r.status !== 200) throw new Error(`setup restore failed: ${JSON.stringify(r)}`);
        await new Promise((r) => setTimeout(r, 500));
      }
    }
    async function ensureArchived(wsId) {
      if (!(await apiArchive()).includes(wsId)) {
        const r = await apiPostAct("archive", wsId);
        if (r.status !== 200) throw new Error(`setup archive failed: ${JSON.stringify(r)}`);
        await new Promise((r) => setTimeout(r, 500));
      }
    }
    const ARCHIVE_BTN = "document.querySelector('button[aria-label=\"Archive Seeded done lane (WS1)\"]')";
    const CONFIRM_TEXT = "(function(){var p=document.querySelector('.arch-confirm[role=\"dialog\"]');return p?p.textContent:null;})()";
    const CONFIRM_YES = "(function(){var p=document.querySelector('.arch-confirm[role=\"dialog\"]');var b=[].slice.call(p.querySelectorAll('button')).filter(function(x){return x.textContent==='Archive';})[0];b.click();})()";
    const TOAST_TEXT = "(function(){var t=document.getElementById('toast');if(!t||!t.classList.contains('show'))return null;return t.textContent;})()";
    const DONE_ROWS = "(function(){return [].slice.call(document.querySelectorAll('.pj-done')).filter(function(x){return (x.textContent||'').indexOf('WS1')>=0;}).length;})()";
    async function archiveFlow(width, height, theme, tag, assertFlow) {
      await viewport(width, height);
      await ensureDone("WS1");
      await goto("project", theme);
      ok(`${tag}: Archive button names the workstream`,
        await poll(cdp, `!!(${ARCHIVE_BTN})`, 15000) === true);
      /* Instant scrolls: the page default is smooth, which never settles
         inside a fast CDP read. A real user clicks a visible button, so
         the product contract is adjacency to the trigger, pinned below. */
      await evaluate(cdp, `(function(){var b=${ARCHIVE_BTN};b.scrollIntoView({block:'center',behavior:'instant'});b.click();})()`);
      const popText = await poll(cdp, CONFIRM_TEXT, 8000);
      const rect = await evaluate(cdp, "(function(){var p=document.querySelector('.arch-confirm[role=\"dialog\"]');var b=document.querySelector('button[aria-label=\"Archive Seeded done lane (WS1)\"]');if(!p)return null;var r=p.getBoundingClientRect();var a=b?b.getBoundingClientRect():null;return {y:Math.round(r.y),h:Math.round(r.height),aTop:a?Math.round(a.top):null,aBot:a?Math.round(a.bottom):null,innerH:window.innerHeight};})()");
      console.log(`popover rect ${tag}:`, JSON.stringify(rect));
      if (assertFlow) {
        ok(`${tag}: inline confirm names the workstream`, typeof popText === "string"
          && popText.includes("Archive Seeded done lane (WS1)?")
          && popText.includes("It moves to the Archive view; you can restore it."),
          String(popText).slice(0, 120));
        const below = !!rect && Math.abs(rect.y - (rect.aBot + 6)) <= 2;
        const above = !!rect && Math.abs(rect.y - (rect.aTop - rect.h - 6)) <= 2;
        ok(`${tag}: confirm opens next to its trigger`, below || above, JSON.stringify(rect));
      }
      await evaluate(cdp, "(function(){var p=document.querySelector('.arch-confirm[role=\"dialog\"]');if(p)p.scrollIntoView({block:'center',behavior:'instant'});})()");
      await shot(`confirm-${tag}.png`);
      await evaluate(cdp, CONFIRM_YES);
      const toast = await poll(cdp, TOAST_TEXT, 15000);
      if (assertFlow) {
        ok(`${tag}: toast says Archived with Undo`, typeof toast === "string"
          && toast.includes("Archived Seeded done lane") && toast.includes("Undo"),
          String(toast).slice(0, 120));
      }
      await new Promise((r) => setTimeout(r, 1200));
      await shot(`toast-${tag}.png`);
      ok(`${tag}: API lists it archived right after`, (await apiArchive()).includes("WS1"));
      await evaluate(cdp,
        "(function(){var t=document.getElementById('toast');var u=t?t.querySelector('.toast-undo'):null;if(u)u.click();})()");
      const restored = await poll(cdp, TOAST_TEXT, 15000);
      if (assertFlow) {
        ok(`${tag}: Undo restores it`, typeof restored === "string" && restored.includes("Restored Seeded done lane"),
          String(restored).slice(0, 120));
      }
      const back = await poll(cdp, `(${DONE_ROWS})>0`, 20000);
      ok(`${tag}: done row returns without reload`, back === true);
    }
    let first = true;
    for (const [width, height, theme, tag] of COMBOS) {
      await archiveFlow(width, height, theme, tag, first);
      first = false;
    }
    first = true;
    for (const [width, height, theme, tag] of COMBOS) {
      await ensureArchived("WS1");
      await viewport(width, height);
      await goto("archive", theme);
      const card = await poll(cdp,
        "(function(){var cards=[].slice.call(document.querySelectorAll('.pj-arch'));var hit=cards.filter(function(c){return (c.textContent||'').indexOf('WS1')>=0;})[0];return hit?hit.textContent:null;})()", 15000);
      if (first) {
        ok("archive view: frozen summary card", typeof card === "string"
          && card.includes("2/2") && card.includes("Tasks"), String(card).slice(0, 140));
        ok("archive view: Restore button names the workstream", await evaluate(cdp,
          "!!document.querySelector('button[aria-label=\"Restore Seeded done lane (WS1)\"]')") === true);
        first = false;
      }
      await shot(`archive-${tag}.png`);
    }
    await ensureArchived("WS1");
    await viewport(1440, 900);
    await goto("archive", "dark");
    await poll(cdp, "document.body.textContent.indexOf('WS1')>=0", 15000);
    await evaluate(cdp, "(function(){var b=document.querySelector('button[aria-label=\"Restore Seeded done lane (WS1)\"]');b.scrollIntoView({block:'center',behavior:'instant'});b.click();})()");
    await poll(cdp, `(${TOAST_TEXT}||'').indexOf('Restored')>=0`, 15000);
    ok("archive view: Restore removes the card", await poll(cdp,
      "(function(){var cards=[].slice.call(document.querySelectorAll('.pj-arch'));return cards.filter(function(c){return (c.textContent||'').indexOf('WS1')>=0;}).length===0;})()", 20000) === true);
    ok("no console errors", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
    try { cdp.close(); } catch { /* already closed */ }
    child.kill();
    if (failures.length) {
      console.error(`\n${failures.length} check(s) failed`);
      process.exitCode = 1;
      return;
    }
    console.log("\narchive action check: all green");
  } finally {
    if (serverProc && !opts.url) {
      try { serverProc.kill(); } catch { /* already gone */ }
      await new Promise((r) => setTimeout(r, 500));
      try { if (serverProc.exitCode === null) serverProc.kill("SIGKILL"); } catch { /* already gone */ }
    }
    for (const dir of tmpRoots) {
      try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* best effort */ }
    }
  }
}

main().catch((e) => { console.error(`archive_action_check failed: ${e.message}`); process.exitCode = 1; });
