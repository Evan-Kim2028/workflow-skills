#!/usr/bin/env node
/* Sections check: Running now and Finished group workstream -> section -> label.
 *
 * Stands up a TEMP dashboard server (temp state dir, synthetic
 * workstreams, port 8910, never the live :8765 or the real data root)
 * seeded with a 4-section WS9 plan and 12 synthetic live agents spread
 * over the sections (plus 6 finished agents, one unmapped), then drives
 * the Execution view over the DevTools protocol (stdlib only: global
 * fetch and WebSocket, child_process, no npm installs) and asserts:
 *   - workstream tab collapsed: exactly 5 agent rows under section
 *     headers, header "Running now · 12", a real <button> reading
 *     "Show all 12 running agents" (aria-expanded, aria-controls);
 *   - expanding shows all 12 grouped under 4 section headers in plan
 *     order (North, East, South, West) with agent counts and section
 *     tasks-done progress, and a re-render (drawMain, the same path a
 *     stream tick takes) does not collapse it;
 *   - Group by toggle (Section | None, real buttons, aria-pressed):
 *     None renders a flat table, Section regroups;
 *   - overview groups workstream -> section -> label with collapsible
 *     workstream chevron headers;
 *   - the Finished table carries a Section column and in-page section
 *     headers, through the shared pager;
 *   - zero console errors throughout and no "phase"/"phases" text on
 *     the Execution views.
 *
 * The server child is killed by PID on the way out (never pkill).
 * Exit 0 when every assertion holds, 1 on any mismatch, 2 when Chrome
 * is missing.
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DASH_ROOT = path.dirname(HERE);
const PORT = 8910;
const URL = `http://127.0.0.1:${PORT}`;
const TIMEOUT_MS = 30000;
const ORDER = ["North", "East", "South", "West"];

function args(argv) {
  const out = { chrome: null };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--chrome" && argv[i + 1]) out.chrome = argv[++i];
    else if (flag === "-h" || flag === "--help") {
      console.log("usage: sections_check.mjs [--chrome PATH]");
      process.exit(0);
    } else {
      console.error(`unknown flag ${flag}`);
      process.exit(1);
    }
  }
  return out;
}

function which(names) {
  const dirs = String(process.env.PATH || "").split(path.delimiter);
  for (const dir of dirs) {
    for (const name of names) {
      const candidate = path.join(dir, name);
      try {
        fs.accessSync(candidate, fs.constants.X_OK);
        return candidate;
      } catch {
        /* next */
      }
    }
  }
  return null;
}

function findChrome(explicit) {
  if (explicit) {
    if (fs.existsSync(explicit)) return explicit;
    console.error(`sections_check: chrome not found at ${explicit}`);
    process.exit(2);
  }
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (process.platform === "darwin" && fs.existsSync(mac)) return mac;
  const found = which(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]);
  if (found) return found;
  console.error("sections_check: no Chrome binary found (tried CHROME_PATH, the default install path and PATH)");
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
    } catch {
      /* not up yet */
    }
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
    ws.addEventListener("open", () => {
      settled = true;
      resolve({ send, close });
    });
    ws.addEventListener("message", (event) => {
      let msg;
      try {
        msg = JSON.parse(String(event.data));
      } catch {
        return;
      }
      if (msg.id != null && pending.has(msg.id)) {
        const entry = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) entry.reject(new Error(msg.error.message || JSON.stringify(msg.error)));
        else entry.resolve(msg.result);
      } else if (msg.method && onEvent) {
        onEvent(msg.method, msg.params || {});
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
          if (pending.has(cur)) {
            pending.delete(cur);
            rej(new Error(`cdp timeout: ${method}`));
          }
        }, timeoutMs);
      });
    }
    function close() {
      try {
        ws.close();
      } catch {
        /* already closed */
      }
    }
  });
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", { expression, returnByValue: true });
  if (!result || result.error) {
    throw new Error(`cdp evaluate gave no result (page is mid-navigation): ${JSON.stringify(result)}`);
  }
  if (result.exceptionDetails) {
    throw new Error(`page expression failed: ${JSON.stringify(result.exceptionDetails).slice(0, 400)}`);
  }
  return result.result ? result.result.value : undefined;
}

async function waitForLoad(cdp) {
  const deadline = Date.now() + TIMEOUT_MS;
  for (;;) {
    let state = null;
    try {
      state = await evaluate(cdp, "document.readyState");
    } catch {
      state = null;
    }
    if (state === "complete" || state === "interactive") return;
    if (Date.now() > deadline) throw new Error("page never finished loading");
    await new Promise((r) => setTimeout(r, 300));
  }
}

async function waitFor(cdp, expression, what) {
  const deadline = Date.now() + TIMEOUT_MS;
  for (;;) {
    let value = null;
    try {
      value = await evaluate(cdp, expression);
    } catch {
      value = null;
    }
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`never settled: ${what}`);
    await new Promise((r) => setTimeout(r, 400));
  }
}

const SERVER_PY = `import os, sys, time
root, state, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
sys.path.insert(0, root)
os.environ.setdefault("AGENT_DASH_IGNORE_LOCAL", "1")
from pathlib import Path
from dash.store import Store
from dash.server import DashServer
from dash.sources import Registry
from agent_dash import default_cfg
now = time.time()
seed = Store(Path(state) / "dash.sqlite")
n = 0
plan_tasks = [("North", "N1"), ("North", "N2"), ("North", "N3"), ("North", "N4"),
              ("East", "E1"), ("East", "E2"), ("East", "E3"),
              ("South", "S1"), ("South", "S2"), ("South", "S3"),
              ("West", "W1"), ("West", "W2")]
for sec, task in plan_tasks:
    n += 1
    age = (13 - n) * 45
    seed.upsert_run({
        "run_id": "sec-check:ws9-%s-%02d" % (task.lower(), n),
        "source": "muse",
        "tier": "muse",
        "model": "muse-1",
        "label": "ws9-%s-k%02d" % (task.lower(), n),
        "status": "running",
        "started_at": now - age,
        "elapsed_s": age,
        "out_bytes": 100 + n,
        "out_growing": False,
        "tokens": {"input": 100 + n, "output": 50, "cache_read": 0, "cache_write": 0},
    })
for k in range(1, 7):
    seed.upsert_run({
        "run_id": "sec-check:fin-%02d" % k,
        "source": "muse",
        "tier": "muse",
        "model": "muse-1",
        "label": ("ws9-%s-old%02d" % (["n1", "e1", "s1", "w1", "n2", "zzz"][k - 1], k)) if k < 6 else "ws9-zzz-old",
        "status": "done",
        "started_at": now - 7200 - k * 60,
        "ended_at": now - 3600 - k * 60,
        "out_bytes": 50,
        "out_growing": False,
        "tokens": {"input": 60, "output": 20, "cache_read": 0, "cache_write": 0},
    })
seed.close()
store = Store(Path(state) / "dash.sqlite")
srv = DashServer(store, Registry([]), "127.0.0.1", port, stream_interval=2, cfg=default_cfg())
srv.serve_forever(poll_interval=0.05)
`;

function writeBundle(wsDir) {
  const ws9 = path.join(wsDir, "WS9");
  fs.mkdirSync(ws9, { recursive: true });
  fs.writeFileSync(path.join(ws9, "bundle.json"), JSON.stringify({ id: "WS9", title: "Nine", kind: "workstream" }));
  const groups = [
    { id: "g-n", col: 0, title: "North", tasks: [
      { id: "N1", title: "North one", status: "wip" },
      { id: "N2", title: "North two", status: "done" },
      { id: "N3", title: "North three", status: "todo" },
      { id: "N4", title: "North four", status: "todo" }] },
    { id: "g-e", col: 1, title: "East", tasks: [
      { id: "E1", title: "East one", status: "wip" },
      { id: "E2", title: "East two", status: "todo" },
      { id: "E3", title: "East three", status: "todo" }] },
    { id: "g-s", col: 2, title: "South", tasks: [
      { id: "S1", title: "South one", status: "wip" },
      { id: "S2", title: "South two", status: "todo" },
      { id: "S3", title: "South three", status: "done" }] },
    { id: "g-w", col: 3, title: "West", tasks: [
      { id: "W1", title: "West one", status: "todo" },
      { id: "W2", title: "West two", status: "todo" }] },
  ];
  fs.writeFileSync(path.join(ws9, "plan.json"), JSON.stringify({
    dag: { cols: ["C1", "C2", "C3", "C4"], groups, edges: [] },
    narrative: { phases: [] },
  }));
}

const checks = [];
function check(name, fn) {
  checks.push([name, fn]);
}
function fail(message) {
  throw new Error(message);
}
function eq(actual, expected, what) {
  if (actual !== expected) fail(`${what}: want ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

/* Snapshot of the Running now card: header, disclosure button, toggle
   buttons, section headers (in order), workstream headers, agent rows
   (label + section when grouped). */
const runningState = `(function(){
  var card = document.querySelector('#exec-running');
  if (!card) return null;
  var h2 = card.querySelector('h2');
  var btn = card.querySelector('button.runmore');
  var toggles = Array.prototype.map.call(card.querySelectorAll('.grp-toggle button.grp-btn'), function(b){
    return {text: b.textContent, pressed: b.getAttribute('aria-pressed'), tag: b.tagName};
  });
  var secHeads = Array.prototype.map.call(card.querySelectorAll('tr.sec-h td'), function(td){ return td.textContent; });
  var wsHeads = Array.prototype.map.call(card.querySelectorAll('tr.ws-h'), function(tr){ return tr.textContent; });
  var chevs = Array.prototype.map.call(card.querySelectorAll('tr.ws-h button.ws-chev'), function(b){
    return {expanded: b.getAttribute('aria-expanded'), tag: b.tagName};
  });
  var rows = Array.prototype.map.call(card.querySelectorAll('tr.clk'), function(tr){
    var lbl = tr.querySelector('.lbl');
    return {label: lbl ? lbl.textContent : '', visible: !!(tr.offsetParent), cells: tr.querySelectorAll('td').length};
  });
  var ths = Array.prototype.map.call(card.querySelectorAll('thead th'), function(th){ return th.textContent; });
  return {h2: h2 ? h2.textContent : '', nav: card.getAttribute('data-nav'),
    btn: btn ? {text: btn.textContent, expanded: btn.getAttribute('aria-expanded'),
      controls: btn.getAttribute('aria-controls'), tag: btn.tagName,
      lastChild: btn.parentElement.lastChild === btn} : null,
    toggles, secHeads, wsHeads, chevs, rows, ths};
})()`;

const finishedState = `(function(){
  var card = document.querySelector('#exec-finished');
  if (!card) return null;
  var ths = Array.prototype.map.call(card.querySelectorAll('thead th'), function(th){ return th.textContent; });
  var secHeads = Array.prototype.map.call(card.querySelectorAll('tr.sec-h td'), function(td){ return td.textContent; });
  var secCells = Array.prototype.map.call(card.querySelectorAll('td .sec'), function(s){ return s.textContent; });
  var toggles = Array.prototype.map.call(card.querySelectorAll('.grp-toggle button.grp-btn'), function(b){
    return {text: b.textContent, pressed: b.getAttribute('aria-pressed'), tag: b.tagName};
  });
  return {ths, secHeads, secCells, toggles,
    rows: card.querySelectorAll('tr.clk').length};
})()`;

/* No "phase"/"phases" wording anywhere in the main view. */
const phaseText = `(function(){
  var main = document.querySelector('#main');
  var text = main ? main.innerText : document.body.innerText;
  var hit = text.match(/\\bphases?\\b/i);
  return hit ? ('found: ' + hit[0] + ' in: ' + text.slice(Math.max(0, text.indexOf(hit[0]) - 60), text.indexOf(hit[0]) + 60)) : null;
})()`;

async function main() {
  const opts = args(process.argv.slice(2));
  const chrome = findChrome(opts.chrome);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "dash-sections-"));
  const stateDir = path.join(tmp, "state");
  const wsDir = path.join(tmp, "workstreams");
  fs.mkdirSync(stateDir, { recursive: true });
  fs.mkdirSync(wsDir, { recursive: true });
  writeBundle(wsDir);
  const serverPy = path.join(tmp, "sections_server.py");
  fs.writeFileSync(serverPy, SERVER_PY);
  const serverLog = path.join(tmp, "server.log");
  const logFd = fs.openSync(serverLog, "w");
  const server = spawn("python3", [serverPy, DASH_ROOT, stateDir, String(PORT)], {
    cwd: DASH_ROOT,
    stdio: ["ignore", logFd, logFd],
    env: {
      ...process.env,
      AGENT_DASH_STATE_DIR: stateDir,
      AGENT_DASH_WORKSTREAMS: wsDir,
      AGENT_DASH_IGNORE_LOCAL: "1",
    },
  });
  let cdp = null;
  let child = null;
  const errors = [];
  const finish = (code) => {
    try {
      if (cdp) cdp.close();
    } catch {
      /* ignore */
    }
    try {
      if (child) child.kill();
    } catch {
      /* ignore */
    }
    try {
      server.kill();
    } catch {
      /* ignore */
    }
    try {
      fs.closeSync(logFd);
    } catch {
      /* ignore */
    }
    process.exit(code);
  };
  process.on("SIGINT", () => finish(1));
  try {
    await new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error("temp server never came up (is :8910 already taken?)")), TIMEOUT_MS);
      server.once("exit", (code) => {
        clearTimeout(t);
        reject(new Error(`temp server exited early with code ${code}; see ${serverLog}`));
      });
      (async () => {
        const snap = await waitForJson(`${URL}/api/snapshot?ws=all`, TIMEOUT_MS);
        if (!Array.isArray(snap.running) || snap.running.length !== 12) {
          throw new Error(`seeded 12 live agents but the API shows ${(snap.running || []).length}`);
        }
        const secs = snap.running.map((r) => r.section);
        for (const want of ORDER) {
          if (!secs.includes(want)) throw new Error(`no running row carries section ${want}`);
        }
        if (snap.running.some((r) => !("section_index" in r))) {
          throw new Error("a running row misses section_index");
        }
        clearTimeout(t);
        resolve();
      })().catch((e) => {
        clearTimeout(t);
        reject(e);
      });
    });

    const dbgPort = await freePort();
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dash-sections-prof-"));
    child = spawn(
      chrome,
      ["--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-extensions",
        `--user-data-dir=${profile}`, `--remote-debugging-port=${dbgPort}`, "about:blank"],
      { stdio: ["ignore", "ignore", "pipe"] },
    );
    await waitForJson(`http://127.0.0.1:${dbgPort}/json/version`, TIMEOUT_MS);
    const created = await (await fetch(`http://127.0.0.1:${dbgPort}/json/new?about:blank`, { method: "PUT" })).json();
    cdp = await connectDebugger(created.webSocketDebuggerUrl, (method, params) => {
      if (method === "Runtime.consoleAPICalled" && params.type === "error") {
        errors.push(`console.error: ${(params.args || []).map((a) => a.description || a.value).join(" ").slice(0, 200)}`);
      } else if (method === "Runtime.exceptionThrown") {
        errors.push(`page exception: ${JSON.stringify(params.exceptionDetails || {}).slice(0, 200)}`);
      }
    });
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");

    check("tab collapsed: 5 agents under section headers, show-all button", async () => {
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=WS9` });
      await waitForLoad(cdp);
      const st = await waitFor(cdp, `(${runningState}) && ${runningState}.h2.indexOf('· 12') >= 0 && (${runningState})`, "tab card with 12 agents");
      eq(st.h2, "Running now · 12", "header keeps the full count");
      eq(st.nav, "Running now", "data-nav label");
      eq(st.rows.filter((r) => r.visible).length, 5, "collapsed shows exactly 5 agents");
      if (!st.secHeads.length) fail("no section headers while collapsed");
      for (const h of st.secHeads) {
        if (h.indexOf("agent") < 0) fail(`section header names no agent count: ${JSON.stringify(h)}`);
      }
      if (!st.btn || st.btn.tag !== "BUTTON") fail("no real <button> disclosure below the table");
      if (!st.btn.text.startsWith("Show all 12 running")) fail(`button reads ${JSON.stringify(st.btn.text)}`);
      eq(st.btn.expanded, "false", "aria-expanded");
      eq(st.btn.controls, "exec-running-rows", "aria-controls");
      if (!st.btn.lastChild) fail("the disclosure button is not below the table");
      eq(JSON.stringify(st.toggles.map((t) => t.text)), JSON.stringify(["Section", "None"]), "Group by toggle buttons");
      if (st.toggles.some((t) => t.tag !== "BUTTON")) fail("the Group by toggle is not real buttons");
      eq(st.toggles[0].pressed, "true", "Section is the default");
      eq(st.toggles[1].pressed, "false", "None starts unpressed");
      eq(JSON.stringify(st.ths), JSON.stringify(["Label", "Model", "Elapsed", "Output", "Tokens", "Status"]), "agent rows are not label-first");
      const hit = await evaluate(cdp, `(${phaseText})`);
      if (hit) fail(`tab says phase: ${hit}`);
    });

    check("tab expand: 12 agents grouped in plan order with progress, re-render holds", async () => {
      await evaluate(cdp, `document.querySelector('#exec-running button.runmore').click()`);
      await new Promise((r) => setTimeout(r, 400));
      let st = await evaluate(cdp, `(${runningState})`);
      eq(st.rows.filter((r) => r.visible).length, 12, "expanded shows all 12");
      eq(st.btn.text, "Show top 5 agents", "button reads Show top 5");
      eq(st.btn.expanded, "true", "aria-expanded after expand");
      const names = st.secHeads.map((h) => h.split(" · ")[0]);
      eq(JSON.stringify(names), JSON.stringify(ORDER), `section order follows the plan (got ${names.join(",")})`);
      for (const h of st.secHeads) {
        if (!/1 of \d+ done|2 of \d+ done|\d+ of \d+ done/.test(h)) fail(`section header misses tasks-done progress: ${JSON.stringify(h)}`);
      }
      await evaluate(cdp, `window.drawMain()`);
      await new Promise((r) => setTimeout(r, 400));
      st = await evaluate(cdp, `(${runningState})`);
      eq(st.rows.filter((r) => r.visible).length, 12, "a re-render (tick path) does not collapse the list");
      eq(st.btn.expanded, "true", "aria-expanded survives the re-render");
      const sixth = await evaluate(cdp, `(function(){
        var tr = document.querySelector('#exec-running tr.clk');
        if (!tr) return null;
        tr.click();
        return document.getElementById('drawer').className;
      })()`);
      if (!sixth || sixth.indexOf("open") < 0) fail("an agent row does not open the drawer");
      await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape" });
      await new Promise((r) => setTimeout(r, 300));
      const hit = await evaluate(cdp, `(${phaseText})`);
      if (hit) fail(`expanded tab says phase: ${hit}`);
    });

    check("tab toggle None: flat table, then back to sections", async () => {
      await evaluate(cdp, `(function(){
        var btns = document.querySelectorAll('#exec-running .grp-toggle button.grp-btn');
        for (var i = 0; i < btns.length; i++) if (btns[i].textContent === 'None') btns[i].click();
      })()`);
      await new Promise((r) => setTimeout(r, 400));
      let st = await evaluate(cdp, `(${runningState})`);
      eq(st.secHeads.length, 0, "None still renders section headers");
      eq(st.rows.filter((r) => r.visible).length, 12, "None hides agents");
      eq(st.toggles[1].pressed, "true", "None is pressed after the click");
      await evaluate(cdp, `window.drawMain()`);
      await new Promise((r) => setTimeout(r, 400));
      st = await evaluate(cdp, `(${runningState})`);
      eq(st.secHeads.length, 0, "a re-render forgets the None toggle");
      await evaluate(cdp, `(function(){
        var btns = document.querySelectorAll('#exec-running .grp-toggle button.grp-btn');
        for (var i = 0; i < btns.length; i++) if (btns[i].textContent === 'Section') btns[i].click();
      })()`);
      await new Promise((r) => setTimeout(r, 400));
      st = await evaluate(cdp, `(${runningState})`);
      if (!st.secHeads.length) fail("Section toggle does not regroup");
      await evaluate(cdp, `document.querySelector('#exec-running button.runmore').click()`);
      await new Promise((r) => setTimeout(r, 300));
    });

    check("tab finished: Section column and in-page section headers", async () => {
      const st = await waitFor(cdp, `(${finishedState}) && ${finishedState}.rows >= 6 && (${finishedState})`, "finished table with 6 agents");
      eq(JSON.stringify(st.ths), JSON.stringify(["Model", "Label", "WS", "Section", "Elapsed", "Output", "Tokens", "Status"]), "Finished columns");
      if (!st.secCells.some((c) => c === "North")) fail(`no finished cell names a section: ${JSON.stringify(st.secCells)}`);
      if (!st.secCells.some((c) => c === "Unsectioned")) fail(`no finished cell reads Unsectioned: ${JSON.stringify(st.secCells)}`);
      if (!st.secHeads.length) fail("no in-page section headers in Finished");
      eq(JSON.stringify(st.toggles.map((t) => t.text)), JSON.stringify(["Section", "None"]), "Finished Group by toggle");
    });

    check("overview: workstream -> section -> label with chevrons", async () => {
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
      await waitForLoad(cdp);
      const st = await waitFor(cdp, `(${runningState}) && ${runningState}.h2.indexOf('· 12') >= 0 && (${runningState})`, "overview card with 12 agents");
      if (!st.wsHeads.length) fail("no workstream headers on the overview");
      if (!st.wsHeads[0].match(/WS9/)) fail(`workstream header names no workstream: ${JSON.stringify(st.wsHeads[0])}`);
      if (!st.chevs.length || st.chevs.some((c) => c.tag !== "BUTTON")) fail("workstream headers are not chevron buttons");
      if (st.chevs.some((c) => c.expanded !== "true")) fail("workstream chevrons do not start expanded");
      eq(st.rows.filter((r) => r.visible).length, 5, "overview collapsed shows 5 agents");
      await evaluate(cdp, `document.querySelector('#exec-running button.runmore').click()`);
      await new Promise((r) => setTimeout(r, 400));
      let open = await evaluate(cdp, `(${runningState})`);
      eq(open.rows.filter((r) => r.visible).length, 12, "overview expanded shows 12");
      const names = open.secHeads.map((h) => h.split(" · ")[0]);
      eq(JSON.stringify(names), JSON.stringify(ORDER), `overview section order follows the plan (got ${names.join(",")})`);
      await evaluate(cdp, `document.querySelector('#exec-running tr.ws-h button.ws-chev').click()`);
      await new Promise((r) => setTimeout(r, 400));
      let shut = await evaluate(cdp, `(${runningState})`);
      eq(shut.rows.filter((r) => r.visible).length, 0, "collapsing the workstream hides its agents");
      await evaluate(cdp, `window.drawMain()`);
      await new Promise((r) => setTimeout(r, 400));
      shut = await evaluate(cdp, `(${runningState})`);
      eq(shut.rows.filter((r) => r.visible).length, 0, "a re-render reopens the chevron");
      await evaluate(cdp, `document.querySelector('#exec-running tr.ws-h button.ws-chev').click()`);
      await new Promise((r) => setTimeout(r, 400));
      const hit = await evaluate(cdp, `(${phaseText})`);
      if (hit) fail(`overview says phase: ${hit}`);
    });

    let failed = 0;
    for (const [name, fn] of checks) {
      try {
        await fn();
        console.log(`ok: ${name}`);
      } catch (error) {
        failed++;
        console.log(`FAIL: ${name}: ${(error && error.message) || error}`);
      }
    }
    if (errors.length) {
      failed += errors.length;
      for (const e of errors) console.log(`FAIL: ${e}`);
    } else {
      console.log("ok: zero console errors");
    }
    if (failed) {
      console.log(`debug leftovers: tmp ${tmp} server log ${serverLog}`);
    } else {
      try {
        fs.rmSync(profile, { recursive: true, force: true });
      } catch {
        /* ignore */
      }
      try {
        fs.rmSync(tmp, { recursive: true, force: true });
      } catch {
        /* ignore */
      }
    }
    finish(failed ? 1 : 0);
  } catch (error) {
    console.error(`sections_check failed: ${(error && error.message) || error}`);
    finish(1);
  }
}

main();
