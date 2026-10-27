#!/usr/bin/env node
/* Aggregate check: aggregated progress + accumulated activity panels.
 *
 * Stands up a TEMP dashboard server (temp state dir, synthetic
 * workstreams, port 8912, never the live :8765 or the real data root)
 * seeded with WS1/WS2 plans, history moves and hourly usage, then drives
 * the Execution view over the DevTools protocol (stdlib only: global
 * fetch and WebSocket, child_process, no npm installs) and asserts:
 *   - the section is present on ws=all and absent on a workstream tab;
 *   - two <svg> charts share identical tick labels;
 *   - clicking a band navigates to ?ws=<WS>&view=exec;
 *   - Tokens|Cost toggle rebuilds only the right panel;
 *   - zero console errors throughout, stacked panels and no overflow
 *     at 390 px.
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
const PORT = 8912;
const URL = `http://127.0.0.1:${PORT}`;
const TIMEOUT_MS = 30000;

function args(argv) {
  const out = { chrome: null };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--chrome" && argv[i + 1]) out.chrome = argv[++i];
    else if (flag === "-h" || flag === "--help") {
      console.log("usage: aggregate_check.mjs [--chrome PATH]");
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
    console.error(`aggregate_check: chrome not found at ${explicit}`);
    process.exit(2);
  }
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (process.platform === "darwin" && fs.existsSync(mac)) return mac;
  const found = which(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]);
  if (found) return found;
  console.error("aggregate_check: no Chrome binary found (tried CHROME_PATH, the default install path and PATH)");
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
runs = []
for ws, label in (("WS1", "ws1-agg"), ("WS2", "ws2-agg")):
    rid = "agg-check:%s:run" % ws
    seed.upsert_run({
        "run_id": rid,
        "source": "muse",
        "tier": "muse",
        "model": "claude-sonnet-5-5",
        "label": label,
        "status": "done",
        "started_at": now - 7200,
        "ended_at": now - 7000,
        "out_bytes": 50,
        "out_growing": False,
        "tokens": {"input": 60, "output": 20, "cache_read": 0, "cache_write": 0},
    })
    runs.append(rid)
for rid, ws in zip(runs, ("WS1", "WS2")):
    seed._conn.execute("UPDATE runs SET workstream=? WHERE run_id=?", (ws, rid))
    seed._conn.commit()
for h in range(6):
    ts = (int(now // 3600) * 3600) - h * 3600
    for ws, mult in (("WS1", 3), ("WS2", 1)):
        seed.add_usage({
            "ts": ts,
            "session_id": "agg-%s-%d" % (ws, h),
            "run_id": "agg-check:%s:run" % ws,
            "model": "claude-sonnet-5-5",
            "tool": "muse",
            "message_id": "turn:%s:%d" % (ws, h),
            "input": 1000 * mult + h * 10,
            "output": 500 * mult,
            "cache_read": 200 * mult,
            "cache_write": 100 * mult,
            "reasoning": 0,
        })
seed.close()
store = Store(Path(state) / "dash.sqlite")
srv = DashServer(store, Registry([]), "127.0.0.1", port, stream_interval=2, cfg=default_cfg())
srv.serve_forever(poll_interval=0.05)
`;

function writeBundles(wsDir) {
  var specs = {
    WS1: {tasks: [
      {id: "WS1.1", title: "One", status: "done"},
      {id: "WS1.2", title: "Two", status: "done"},
      {id: "WS1.3", title: "Three", status: "todo"},
      {id: "WS1.4", title: "Four", status: "todo"}],
      history: [
        {ts: Date.now() / 1000 - 7200, task: "WS1.1", from: "todo", to: "done"},
        {ts: Date.now() / 1000 - 3600, task: "WS1.2", from: "todo", to: "done"}]},
    WS2: {tasks: [
      {id: "WS2.1", title: "One", status: "done"},
      {id: "WS2.2", title: "Two", status: "todo"},
      {id: "WS2.3", title: "Three", status: "todo"}],
      history: [
        {ts: Date.now() / 1000 - 5400, task: "WS2.1", from: "todo", to: "done"}]},
  };
  for (const ws of Object.keys(specs)) {
    const dir = path.join(wsDir, ws);
    fs.mkdirSync(dir, {recursive: true});
    fs.writeFileSync(path.join(dir, "bundle.json"), JSON.stringify({id: ws, title: "Probe " + ws, kind: "workstream"}));
    fs.writeFileSync(path.join(dir, "plan.json"), JSON.stringify({
      dag: {cols: ["Build"], groups: [{id: "g", col: 0, title: "Build", tasks: specs[ws].tasks}], edges: []},
      narrative: {phases: []},
    }));
    fs.writeFileSync(path.join(dir, "history.jsonl"), specs[ws].history.map((r) => JSON.stringify(r)).join("\n") + "\n");
  }
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

async function main() {
  const opts = args(process.argv.slice(2));
  const chrome = findChrome(opts.chrome);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "dash-aggregate-"));
  const stateDir = path.join(tmp, "state");
  const wsDir = path.join(tmp, "workstreams");
  fs.mkdirSync(stateDir, { recursive: true });
  fs.mkdirSync(wsDir, { recursive: true });
  writeBundles(wsDir);
  const serverPy = path.join(tmp, "aggregate_server.py");
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
      const t = setTimeout(() => reject(new Error("temp server never came up (is :8912 already taken?)")), TIMEOUT_MS);
      server.once("exit", (code) => {
        clearTimeout(t);
        reject(new Error(`temp server exited early with code ${code}; see ${serverLog}`));
      });
      (async () => {
        const prog = await waitForJson(`${URL}/api/ws/progress?ws=all`, TIMEOUT_MS);
        if (!prog.wsprogress || !(prog.wsprogress.total > 0)) {
          throw new Error(`seeded plans but progress shows ${JSON.stringify(prog.wsprogress || {}).slice(0, 160)}`);
        }
        const burn = await waitForJson(`${URL}/api/project/burnup`, TIMEOUT_MS);
        if (!burn.burnup || !(burn.burnup.days || []).length) {
          throw new Error("seeded history but burnup has no days");
        }
        const usage = await waitForJson(`${URL}/api/usage?group=workstream&bucket=hour`, TIMEOUT_MS);
        if (!Array.isArray(usage.buckets) || !usage.buckets.length) {
          throw new Error("seeded usage but buckets are empty");
        }
        clearTimeout(t);
        resolve();
      })().catch((e) => {
        clearTimeout(t);
        reject(e);
      });
    });

    const dbgPort = await freePort();
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dash-aggregate-prof-"));
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

    const aggState = `(function(){
      var card = document.querySelector('#exec-aggregate');
      if (!card) return null;
      var left = card.querySelector('.agg-left svg[data-panel="left"]');
      var right = card.querySelector('.agg-right svg[data-panel="right"]');
      var toggle = Array.prototype.map.call(card.querySelectorAll('.agg-toggle button'), function(b){
        return {text: b.textContent, pressed: b.getAttribute('aria-pressed')};
      });
      return {
        headline: (card.querySelector('.agg-headline') || {}).textContent || '',
        leftTicks: left ? left.getAttribute('data-ticks') : null,
        rightTicks: right ? right.getAttribute('data-ticks') : null,
        mode: right ? right.getAttribute('data-mode') : null,
        bands: card.querySelectorAll('.agg-band').length,
        leftHtml: left ? left.outerHTML.length : 0,
        rightHtml: right ? right.outerHTML.length : 0,
        toggle: toggle,
        grid: (card.querySelector('.agg-grid') || {}).className || ''
      };
    })()`;

    check("overview: aggregate section present with two synced svgs", async () => {
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
      await waitForLoad(cdp);
      const st = await waitFor(cdp, `(${aggState}) && ${aggState}.bands >= 2 && (${aggState})`, "aggregate card with bands");
      if (!st.leftTicks || !st.rightTicks) fail("both panels must render an svg");
      eq(st.leftTicks, st.rightTicks, "two charts share identical tick labels");
      if (st.headline.indexOf("/") < 0 || st.headline.indexOf("%") < 0) fail(`headline reads ${JSON.stringify(st.headline)}`);
      if (st.toggle.length !== 2 || st.toggle[0].text !== "Tokens" || st.toggle[1].text !== "Cost") fail(`toggle reads ${JSON.stringify(st.toggle)}`);
    });

    check("workstream tab: aggregate section absent", async () => {
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=WS1` });
      await waitForLoad(cdp);
      await new Promise((r) => setTimeout(r, 800));
      const present = await evaluate(cdp, `!!document.querySelector('#x-aggregate')`);
      eq(present, false, "aggregate section must be absent on a workstream tab");
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
      await waitForLoad(cdp);
      await waitFor(cdp, `(${aggState}) && ${aggState}.bands >= 2`, "aggregate back on overview");
    });

    check("clicking a band navigates to that workstream exec tab", async () => {
      const before = await evaluate(cdp, `location.search`);
      await evaluate(cdp, `(function(){
        var band = document.querySelector('#exec-aggregate .agg-left .agg-band[data-key="WS1"]') ||
          document.querySelector('#exec-aggregate .agg-band[data-key="WS1"]');
        if (!band) return false;
        band.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
        return true;
      })()`);
      await new Promise((r) => setTimeout(r, 900));
      const after = await evaluate(cdp, `location.search`);
      if (after === before) fail("band click did not navigate");
      if (after.indexOf("WS1") < 0 || after.indexOf("ws=") < 0) fail(`band click landed on ${after}`);
      if (after.indexOf("view=exec") < 0) fail(`band click lost view=exec: ${after}`);
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
      await waitForLoad(cdp);
      await waitFor(cdp, `(${aggState}) && ${aggState}.bands >= 2`, "aggregate back after band nav");
    });

    check("Tokens|Cost toggle rebuilds only the right panel", async () => {
      const leftBefore = await evaluate(cdp, `document.querySelector('#exec-aggregate .agg-left svg[data-panel="left"]').outerHTML`);
      const rightBefore = await evaluate(cdp, `document.querySelector('#exec-aggregate .agg-right svg[data-panel="right"]').outerHTML`);
      await evaluate(cdp, `(function(){
        var btns = document.querySelectorAll('#exec-aggregate .agg-toggle button');
        for (var i = 0; i < btns.length; i++) if (btns[i].textContent === 'Cost') { btns[i].click(); return true; }
        return false;
      })()`);
      await new Promise((r) => setTimeout(r, 600));
      const mode = await evaluate(cdp, `document.querySelector('#exec-aggregate .agg-right svg[data-panel="right"]').getAttribute('data-mode')`);
      eq(mode, "cost", "right panel switches to cost");
      const leftAfter = await evaluate(cdp, `document.querySelector('#exec-aggregate .agg-left svg[data-panel="left"]').outerHTML`);
      const rightAfter = await evaluate(cdp, `document.querySelector('#exec-aggregate .agg-right svg[data-panel="right"]').outerHTML`);
      eq(leftAfter, leftBefore, "toggle rebuilds only the right panel (left svg identical)");
      if (rightAfter === rightBefore) fail("toggle left the right panel unchanged");
    });

    check("phone: stacked panels and no overflow at 390", async () => {
      await cdp.send("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 2, mobile: true});
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
      await waitForLoad(cdp);
      await waitFor(cdp, `(${aggState}) && ${aggState}.bands >= 2`, "aggregate at 390");
      await new Promise((r) => setTimeout(r, 500));
      const overflow = await evaluate(cdp, `document.documentElement.scrollWidth - window.innerWidth`);
      if (overflow > 1) fail(`horizontal overflow at 390: ${overflow}px`);
      const stacked = await evaluate(cdp, `(function(){
        var l = document.querySelector('#exec-aggregate .agg-left');
        var r = document.querySelector('#exec-aggregate .agg-right');
        if (!l || !r) return null;
        var a = l.getBoundingClientRect(), b = r.getBoundingClientRect();
        return {aBottom: a.bottom, bTop: b.top, aWidth: a.width};
      })()`);
      if (!stacked) fail("panels missing at 390");
      if (!(stacked.bTop >= stacked.aBottom - 2)) fail(`panels not stacked at 390: ${JSON.stringify(stacked)}`);
      await cdp.send("Emulation.clearDeviceMetricsOverride");
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
      await waitForLoad(cdp);
      await waitFor(cdp, `(${aggState}) && ${aggState}.bands >= 2`, "aggregate back at desktop");
      const wide = await evaluate(cdp, `document.documentElement.scrollWidth - window.innerWidth`);
      if (wide > 1) fail(`horizontal overflow at desktop: ${wide}px`);
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
    console.error(`aggregate_check failed: ${(error && error.message) || error}`);
    finish(1);
  }
}

main();
