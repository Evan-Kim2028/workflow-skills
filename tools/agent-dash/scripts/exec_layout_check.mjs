#!/usr/bin/env node
/* Execution-layout check: run timeline placement + Running-now disclosure.
 *
 * Stands up a TEMP dashboard server (temp state dir, synthetic
 * workstreams, port 8905, never the live :8765 or the real data root)
 * seeded with 12 synthetic live agents, then drives the Execution view
 * over the DevTools protocol (stdlib only: global fetch and WebSocket,
 * child_process, no npm installs) and asserts:
 *   - the Activity over time section (activity) directly follows the plan
 *     card in DOM order, then finished (the stack replaced the separate
 *     Tokens per hour, Run timeline and Live agents sections);
 *   - collapsed Running now shows exactly 5 agent rows grouped under
 *     section headers with a real <button> reading "Show all 12 running
 *     agents" (aria-expanded, aria-controls);
 *   - clicking expands to all 12 grouped in place, keeps drawer clicks
 *     working, and a re-render (drawMain, the same path a stream tick
 *     takes) does not collapse it;
 *   - per-scope state (all vs WS1) and no button at 5 or fewer (WS2);
 *   - zero console errors throughout.
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
const PORT = 8905;
const URL = `http://127.0.0.1:${PORT}`;
const TIMEOUT_MS = 30000;

function args(argv) {
  const out = { chrome: null };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--chrome" && argv[i + 1]) out.chrome = argv[++i];
    else if (flag === "-h" || flag === "--help") {
      console.log("usage: exec_layout_check.mjs [--chrome PATH]");
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
    console.error(`exec_layout_check: chrome not found at ${explicit}`);
    process.exit(2);
  }
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (process.platform === "darwin" && fs.existsSync(mac)) return mac;
  const found = which(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]);
  if (found) return found;
  console.error("exec_layout_check: no Chrome binary found (tried CHROME_PATH, the default install path and PATH)");
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
for ws, count, prefix in (("WS1", 8, "ws1-layout"), ("WS2", 4, "ws2-layout")):
    for k in range(1, count + 1):
        n += 1
        age = (13 - n) * 45
        seed.upsert_run({
            "run_id": "exec-check:%s:%d" % (prefix, k),
            "source": "muse",
            "tier": "muse",
            "model": "muse-1",
            "label": "%s-%02d" % (prefix, k),
            "status": "running",
            "started_at": now - age,
            "elapsed_s": age,
            "out_bytes": 100 + n,
            "out_growing": False,
            "tokens": {"input": 100 + n, "output": 50, "cache_read": 0, "cache_write": 0},
        })
seed.close()
store = Store(Path(state) / "dash.sqlite")
srv = DashServer(store, Registry([]), "127.0.0.1", port, stream_interval=2, cfg=default_cfg())
srv.serve_forever(poll_interval=0.05)
`;

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

async function main() {
  const opts = args(process.argv.slice(2));
  const chrome = findChrome(opts.chrome);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "dash-exec-layout-"));
  const stateDir = path.join(tmp, "state");
  const wsDir = path.join(tmp, "workstreams");
  fs.mkdirSync(stateDir, { recursive: true });
  fs.mkdirSync(wsDir, { recursive: true });
  const serverPy = path.join(tmp, "layout_server.py");
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
      const t = setTimeout(() => reject(new Error("temp server never came up (is :8905 already taken?)")), TIMEOUT_MS);
      server.once("exit", (code) => {
        clearTimeout(t);
        reject(new Error(`temp server exited early with code ${code}; see ${serverLog}`));
      });
      (async () => {
        const snap = await waitForJson(`${URL}/api/snapshot?ws=all`, TIMEOUT_MS);
        if (!Array.isArray(snap.running) || snap.running.length !== 12) {
          throw new Error(`seeded 12 live agents but the API shows ${(snap.running || []).length}`);
        }
        clearTimeout(t);
        resolve();
      })().catch((e) => {
        clearTimeout(t);
        reject(e);
      });
    });
    const apiLabels = (await (await fetch(`${URL}/api/snapshot?ws=all`)).json()).running.map((r) => r.label);

    const dbgPort = await freePort();
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dash-exec-layout-prof-"));
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

    const runningState = `(function(){
      var card = document.querySelector('#exec-running');
      if (!card) return null;
      var h2 = card.querySelector('h2');
      var tbodies = Array.prototype.slice.call(card.querySelectorAll('tbody'));
      var btn = card.querySelector('button.runmore');
      var rows = [];
      Array.prototype.forEach.call(card.querySelectorAll('tbody tr.clk'), function(tr){
        var lbl = tr.querySelector('.lbl');
        rows.push({label: lbl ? lbl.textContent : '', visible: !!(tr.offsetParent)});
      });
      return {h2: h2 ? h2.textContent : '', nav: card.getAttribute('data-nav'),
        tbodies: tbodies.length, extraGone: !document.getElementById('exec-running-extra'),
        secHeads: card.querySelectorAll('tr.sec-h').length,
        btn: btn ? {text: btn.textContent, expanded: btn.getAttribute('aria-expanded'),
          controls: btn.getAttribute('aria-controls'), tag: btn.tagName,
          lastChild: btn.parentElement.lastChild === btn} : null,
        rows};
    })()`;
    const sectionOrder = `Array.prototype.map.call(
      document.querySelectorAll('#main [data-sec]'), function(s){ return s.getAttribute('data-sec'); })`;

    check("overview order: pulse leads, running, activity, manage, program, cards, finished, usage last", async () => {
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
      await waitForLoad(cdp);
      await waitFor(cdp, `(${runningState}) && ${runningState}.h2.indexOf('· 12') >= 0`, "running card with 12 agents");
      const order = await evaluate(cdp, sectionOrder);
      eq(JSON.stringify(order), JSON.stringify(["pulse", "running", "activity", "manage", "program", "cards", "finished", "phases-all"]),
        `overview order (full: ${order.join(",")})`);
      const nav = await evaluate(cdp, `document.querySelector('#exec-activity') ? document.querySelector('#exec-activity').getAttribute('data-nav') : null`);
      eq(nav, "Activity over time", "stack data-nav label");
    });

    check("overview collapsed: 5 rows, show-all button", async () => {
      const st = await evaluate(cdp, `(${runningState})`);
      eq(st.h2, "Running now · 12", "header keeps the full count");
      eq(st.nav, "Running now", "data-nav label");
      eq(st.tbodies, 1, "one tbody; the toggle rebuilds it in place");
      eq(st.extraGone, true, "the hidden-tbody flip is gone");
      if (!st.secHeads) fail("no section headers while collapsed");
      eq(st.rows.filter((r) => r.visible).length, 5, "collapsed shows exactly 5 rows");
      eq(JSON.stringify(st.rows.slice(0, 5).map((r) => r.label)), JSON.stringify(apiLabels.slice(0, 5)),
        "top 5 are the most recently active first");
      if (!st.btn || st.btn.tag !== "BUTTON") fail("no real <button> disclosure below the table");
      if (!st.btn.text.startsWith("Show all 12 running")) fail(`button reads ${JSON.stringify(st.btn.text)}`);
      if (st.btn.text.indexOf("agents") < 0) fail(`button never says agents: ${JSON.stringify(st.btn.text)}`);
      eq(st.btn.expanded, "false", "aria-expanded");
      eq(st.btn.controls, "exec-running-rows", "aria-controls");
      if (!st.btn.lastChild) fail("the disclosure button is not below the table");
    });

    check("overview expand: 12 rows, drawer works, re-render holds", async () => {
      await evaluate(cdp, `document.querySelector('#exec-running button.runmore').click()`);
      await new Promise((r) => setTimeout(r, 400));
      let st = await evaluate(cdp, `(${runningState})`);
      eq(st.rows.filter((r) => r.visible).length, 12, "expanded shows all 12");
      eq(st.btn.text, "Show top 5 agents", "button reads Show top 5");
      eq(st.btn.expanded, "true", "aria-expanded after expand");
      if (!st.secHeads) fail("no section headers while expanded");
      await evaluate(cdp, `window.__xRunning = document.querySelector('#x-running')`);
      await evaluate(cdp, `window.drawMain()`);
      await new Promise((r) => setTimeout(r, 400));
      st = await evaluate(cdp, `(${runningState})`);
      eq(st.rows.filter((r) => r.visible).length, 12, "a re-render (tick path) does not collapse the list");
      eq(st.btn.expanded, "true", "aria-expanded survives the re-render");
      eq(await evaluate(cdp, "window.__xRunning === document.querySelector('#x-running')"), true,
        "an unchanged re-render keeps the section node (no reflow storm)");
      const sixth = await evaluate(cdp, `(function(){
        var tr = document.querySelector('#exec-running tr.clk');
        if (!tr) return null;
        tr.click();
        return document.getElementById('drawer').className;
      })()`);
      if (!sixth || sixth.indexOf("open") < 0) fail("an expanded row does not open the drawer");
      await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape" });
      await new Promise((r) => setTimeout(r, 300));
      await evaluate(cdp, `document.querySelector('#exec-running button.runmore').click()`);
      await new Promise((r) => setTimeout(r, 300));
      st = await evaluate(cdp, `(${runningState})`);
      eq(st.rows.filter((r) => r.visible).length, 5, "toggling back collapses to the top 5");
    });

    check("workstream scope: independent state, no button at 4", async () => {
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=WS1` });
      await waitForLoad(cdp);
      let st = await waitFor(cdp, `(${runningState}) && ${runningState}.h2.indexOf('· 8') >= 0 && (${runningState})`, "WS1 card with 8 agents");
      eq(st.rows.filter((r) => r.visible).length, 5, "WS1 collapsed shows 5 of 8");
      eq(st.btn.expanded, "false", "WS1 starts collapsed (per-scope state)");
      if (!st.btn.text.startsWith("Show all 8 running")) fail(`WS1 button reads ${JSON.stringify(st.btn.text)}`);
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=WS2` });
      await waitForLoad(cdp);
      st = await waitFor(cdp, `(${runningState}) && ${runningState}.h2.indexOf('· 4') >= 0 && (${runningState})`, "WS2 card with 4 agents");
      eq(st.rows.filter((r) => r.visible).length, 4, "WS2 shows all 4");
      if (st.btn) fail("a disclosure button renders with 5 or fewer agents");
      if (st.tbodies !== 1) fail("extra tbody renders with 5 or fewer agents");
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
    console.error(`exec_layout_check failed: ${(error && error.message) || error}`);
    finish(1);
  }
}

main();
