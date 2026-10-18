#!/usr/bin/env node
/* Activity-over-time stack check: one card, one x axis, one time filter.
 *
 * Boots a TEMP dashboard server (temp state dir, synthetic workstreams,
 * port 8911, never the live :8765 or the real data root) seeded with
 * synthetic runs spread over 24 h plus hourly usage buckets, then drives
 * the Execution view over the DevTools protocol (stdlib only: global
 * fetch and WebSocket, child_process, no npm installs) and asserts:
 *   - one #exec-activity card (data-nav "Activity over time") sits where
 *     the tokens/gantt/conc sections were, directly before finished;
 *   - the three strips share the same x domain before and after a brush;
 *   - dragging the from-handle by 2 h shrinks all three strips together,
 *     the URL carries ?from=&to= and sessionStorage persists the range;
 *   - the Last hour preset follows now with a live chip;
 *   - keyboard ArrowLeft moves the focused handle by 5 minutes;
 *   - Reset restores the full window;
 *   - a tick (refreshSnapshot + drawMain, the stream-tick path) does not
 *     reset an absolute brush;
 *   - the workstream tab renders the same stack on its own scope;
 *   - zero console errors throughout.
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
const DASH_ROOT = path.dirname(HERE);
const PORT = 8911;
const URL = `http://127.0.0.1:${PORT}`;
const TIMEOUT_MS = 30000;

function args(argv) {
  const out = { chrome: null };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--chrome" && argv[i + 1]) out.chrome = argv[++i];
    else if (flag === "-h" || flag === "--help") {
      console.log("usage: chart_stack_check.mjs [--chrome PATH]");
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
    console.error(`chart_stack_check: chrome not found at ${explicit}`);
    process.exit(2);
  }
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (process.platform === "darwin" && fs.existsSync(mac)) return mac;
  const found = which(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]);
  if (found) return found;
  console.error("chart_stack_check: no Chrome binary found (tried CHROME_PATH, the default install path and PATH)");
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

/* 120 runs spread over 24 h across four model families (a few still
   running, one verdict mix per family) plus one usage event per run so
   the tokens strip and the brush sparkline have shape. Run ids double
   as the usage run ids so workstream-scoped buckets resolve. */
const SEED_PY = `import sys, time
root, state = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
from pathlib import Path
from dash.store import Store
store = Store(Path(state) / "dash.sqlite")
now = time.time()
specs = [
    ("claude-subagent", "sonnet", "claude-sonnet-4-5", "claude"),
    ("grok", "grok", "grok-4", "cursor"),
    ("muse", "muse", "muse-1", "muse"),
    ("glm", "glm", "glm-4", "glm"),
]
for i in range(120):
    src, tier, model, tool = specs[i % 4]
    slot = (i // 4) % 30
    start = now - 24 * 3600 + 1800 + slot * 2700 + (i % 5) * 37
    dur = 900 + (i % 9) * 120
    status, end, verdict = "done", start + dur, ["SUCCESS", "PARTIAL", "BLOCKED", None][i % 4]
    if i % 25 == 7:
        status, end, verdict = "running", None, None
        start = now - 3600 - (i % 5) * 300
    if i % 25 == 11:
        start, end = now - 20 * 3600, now - 2 * 3600
    store.upsert_run({
        "run_id": "stack-run-%d" % i,
        "source": src,
        "tier": tier,
        "model": model,
        "label": "ws1-stack-%02d" % i,
        "status": status,
        "started_at": start,
        "ended_at": end,
        "verdict": verdict,
        "elapsed_s": max(0, (end or now) - start),
        "out_bytes": 100 + i,
        "out_growing": False,
        "workstream": "WS1" if i % 3 else None,
        "tokens": {"input": 100 + i, "output": 50, "cache_read": 10, "cache_write": 0},
    })
    store.add_usage({
        "ts": (start + 60) if end is None else min(start + 60, end - 1),
        "session_id": "cs-sess-%d" % i,
        "run_id": "stack-run-%d" % i,
        "model": model,
        "tool": tool,
        "message_id": "csm%d" % i,
        "input": 2000 + i * 13,
        "output": 500 + i * 7,
        "cache_read": 1000,
        "cache_write": 0,
    })
store.close()
print("seeded 120 runs + usage")
`;

const DOMAINS = `JSON.stringify(Array.prototype.map.call(
  document.querySelectorAll('#exec-activity svg[data-x-from]'),
  function (s) { return [s.getAttribute("data-strip"), s.getAttribute("data-fam") || "",
    s.getAttribute("data-x-from"), s.getAttribute("data-x-to")]; }))`;
const STACK_READY = `!!document.querySelector('#exec-activity svg[data-strip="tokens"]')`;
const ORDER = `Array.prototype.map.call(
  document.querySelectorAll('#main [data-sec]'), function(s){ return s.getAttribute('data-sec'); })`;

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
function closeTo(actual, expected, tol, what) {
  if (Math.abs(actual - expected) > tol) fail(`${what}: want ${expected}±${tol}, got ${actual}`);
}

async function main() {
  const opts = args(process.argv.slice(2));
  const chrome = findChrome(opts.chrome);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "dash-chart-stack-"));
  const stateDir = path.join(tmp, "state");
  const wsDir = path.join(tmp, "workstreams");
  const homeDir = path.join(tmp, "home");
  const dataDir = path.join(tmp, "data");
  fs.mkdirSync(stateDir, { recursive: true });
  fs.mkdirSync(wsDir, { recursive: true });
  fs.mkdirSync(homeDir, { recursive: true });
  fs.mkdirSync(dataDir, { recursive: true });
  /* Empty data root: the temp server must not ingest live runs from the
     real laptop data root, or foreign ticks rebuild the page mid-gesture
     and the brush assertions stop being deterministic. */
  const env = { ...process.env, AGENT_DASH_IGNORE_LOCAL: "1", AGENT_DASH_STATE_DIR: stateDir,
    AGENT_DASH_WORKSTREAMS: wsDir, AGENT_DASH_DATA_ROOT: dataDir,
    AGENT_DASH_PORT: String(PORT), HOME: homeDir };
  const cli = (...a) => {
    const r = spawnSync("python3", ["agent_dash.py", ...a], { cwd: DASH_ROOT, env, encoding: "utf8" });
    if (r.status !== 0) throw new Error(`cli agent_dash.py ${a.join(" ")} failed\n${r.stdout}\n${r.stderr}`);
    return r.stdout;
  };
  cli("ws", "new", "WS1", "Stack One");
  const seedPath = path.join(tmp, "seed_stack.py");
  fs.writeFileSync(seedPath, SEED_PY.replace("sys.argv[1], sys.argv[2]", `${JSON.stringify(DASH_ROOT)}, ${JSON.stringify(stateDir)}`));
  const seed = spawnSync("python3", [seedPath], { cwd: DASH_ROOT, env, encoding: "utf8" });
  if (seed.status !== 0) throw new Error(`seed failed\n${seed.stdout}\n${seed.stderr}`);

  const serverLog = path.join(tmp, "server.log");
  const logFd = fs.openSync(serverLog, "w");
  const server = spawn("python3", ["agent_dash.py", "run"], {
    cwd: DASH_ROOT, env, stdio: ["ignore", logFd, logFd],
  });
  let cdp = null;
  let child = null;
  const errors = [];
  const finish = (code) => {
    try { if (cdp) cdp.close(); } catch { /* ignore */ }
    try { if (child) child.kill(); } catch { /* ignore */ }
    try {
      const pidFile = path.join(stateDir, "agent-dash.pid");
      const pid = Number(fs.readFileSync(pidFile, "utf8").trim().split(/\s+/)[0]);
      if (pid) process.kill(pid);
    } catch { /* ignore */ }
    try { server.kill(); } catch { /* ignore */ }
    try { fs.closeSync(logFd); } catch { /* ignore */ }
    process.exit(code);
  };
  process.on("SIGINT", () => finish(1));
  try {
    await waitForJson(`${URL}/api/health`, TIMEOUT_MS);
    const buckets = await waitForJson(`${URL}/api/usage?group=model`, TIMEOUT_MS);
    if (!Array.isArray(buckets.buckets) || !buckets.buckets.length) throw new Error("seeded usage never landed");

    const dbgPort = await freePort();
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dash-chart-stack-prof-"));
    child = spawn(chrome,
      ["--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-extensions",
        "--window-size=1440,1000",
        `--user-data-dir=${profile}`, `--remote-debugging-port=${dbgPort}`, "about:blank"],
      { stdio: ["ignore", "ignore", "pipe"] });
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

    function domainsOf(raw) {
      return JSON.parse(raw);
    }
    function assertShared(rows, what) {
      if (rows.length < 3) fail(`${what}: only ${rows.length} strips drew (want tokens, timeline, conc)`);
      const spans = new Set(rows.map((r) => r[2] + ">" + r[3]));
      if (spans.size !== 1) fail(`${what}: strips disagree on the x domain: ${JSON.stringify(rows)}`);
      return { from: Number(rows[0][2]), to: Number(rows[0][3]) };
    }

    check("overview: one stack card before finished", async () => {
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
      await waitForLoad(cdp);
      await waitFor(cdp, STACK_READY, "activity stack with a tokens strip");
      const order = await evaluate(cdp, ORDER);
      const i = order.indexOf("activity");
      eq(JSON.stringify(order.slice(i, i + 2)), JSON.stringify(["activity", "finished"]),
        `section order around activity (full: ${order.join(",")})`);
      for (const gone of ["tokens", "gantt", "conc"]) {
        if (order.indexOf(gone) >= 0) fail(`old section ${gone} still renders`);
      }
      const nav = await evaluate(cdp, `document.querySelector('#exec-activity').getAttribute('data-nav')`);
      eq(nav, "Activity over time", "stack data-nav label");
      const rows = domainsOf(await evaluate(cdp, DOMAINS));
      const d = assertShared(rows, "default range");
      closeTo(d.to - d.from, 86400, 300, "default range spans 24 h");
    });

    async function brushIntoView() {
      /* Instant scroll (the page stylesheet smooth-scrolls, which would
         keep the brush moving under the cursor mid-gesture), then wait
         for the scroll position to actually settle before measuring. */
      await evaluate(cdp, `document.querySelector('#exec-activity .act-svg').scrollIntoView({block: "center", behavior: "instant"})`);
      const deadline = Date.now() + 10000;
      let last = -1;
      for (;;) {
        const y = await evaluate(cdp, "window.scrollY");
        if (y === last) break;
        last = y;
        if (Date.now() > deadline) break;
        await new Promise((r) => setTimeout(r, 150));
      }
      await new Promise((r) => setTimeout(r, 300));
    }

    async function dragFromHandleBy(svgDx) {
      await brushIntoView();
      const geo = await evaluate(cdp, `(function(){
        var svg = document.querySelector('#exec-activity .act-svg');
        var hit = document.querySelector('#exec-activity .act-handle-from');
        if (!svg || !hit) return null;
        var r = svg.getBoundingClientRect(), h = hit.getBoundingClientRect();
        return {left: r.left, top: r.top, width: r.width, height: r.height,
          W: Number(svg.getAttribute("width")), cx: h.left + h.width / 2, cy: h.top + h.height / 2};
      })()`);
      if (!geo) fail("brush geometry unreadable");
      const k = geo.width / geo.W;
      const x0 = geo.cx, y = geo.cy, x1 = geo.cx + svgDx * k;
      const pause = (ms) => new Promise((r) => setTimeout(r, ms));
      await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x: x0, y, button: "left", buttons: 1, clickCount: 1 });
      await pause(150);
      for (let i = 1; i <= 6; i++) {
        await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: x0 + (x1 - x0) * i / 6, y, buttons: 1 });
        await pause(120);
      }
      await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: x1, y, button: "left", buttons: 0, clickCount: 1 });
      /* The release commits through the page's own queue, which may be
         draining throttled strip repaints: wait for the shared domain
         to actually move instead of sleeping fixed. */
      const landedFrom = JSON.parse(await evaluate(cdp, DOMAINS))[0][2];
      const landedExpr = `(function(){ try {
        return JSON.parse(${JSON.stringify(DOMAINS)})[0][2] !== ${JSON.stringify(landedFrom)};
      } catch (e) { return false; } })()`;
      await waitFor(cdp, landedExpr, "brush commit landing").catch(() => null);
      await new Promise((r) => setTimeout(r, 600));
    }

    check("handle drag: 2 h brush shrinks all three strips together", async () => {
      const before = assertShared(domainsOf(await evaluate(cdp, DOMAINS)), "pre-brush");
      const geo = await evaluate(cdp, `(function(){
        var svg = document.querySelector('#exec-activity .act-svg');
        var r = svg.getBoundingClientRect();
        return {W: Number(svg.getAttribute("width")), L: 150, pw: Number(svg.getAttribute("width")) - 164};
      })()`);
      await dragFromHandleBy(geo.pw * 7200 / 86400);
      const rows = domainsOf(await evaluate(cdp, DOMAINS));
      const d = assertShared(rows, "post-brush");
      closeTo(d.from - before.from, 7200, 400, "brush moved the start ~2 h");
      closeTo(d.to - before.to, 0, 400, "brush kept the end");
      closeTo(d.to - d.from, before.to - before.from - 7200, 600, "the window shrank by the brush");
      const url = await evaluate(cdp, "location.search");
      if (url.indexOf("from=") < 0 || url.indexOf("to=") < 0) fail(`URL carries no range: ${url}`);
      const q = new URLSearchParams(url.replace(/^\?/, ""));
      closeTo(Number(q.get("from")) / 1000, d.from, 5, "URL from matches the brush");
      closeTo(Number(q.get("to")) / 1000, d.to, 5, "URL to matches the brush");
      const stored = await evaluate(cdp, `sessionStorage.getItem('agent-dash-activity:all')`);
      if (!stored) fail("brush never persisted to sessionStorage");
      const sj = JSON.parse(stored);
      closeTo(sj.from, d.from, 5, "stored from matches the brush");
      const live = await evaluate(cdp, `!!document.querySelector('#exec-activity .act-live')`);
      eq(live, false, "an absolute brush must not show the live chip");
    });

    check("keyboard: ArrowLeft moves the focused handle 5 minutes", async () => {
      await brushIntoView();
      const before = assertShared(domainsOf(await evaluate(cdp, DOMAINS)), "pre-key");
      await evaluate(cdp, `document.querySelector('#exec-activity .act-handle-from').focus()`);
      await new Promise((r) => setTimeout(r, 300));
      await cdp.send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "ArrowLeft", code: "ArrowLeft", windowsVirtualKeyCode: 37 });
      await new Promise((r) => setTimeout(r, 150));
      await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "ArrowLeft", code: "ArrowLeft", windowsVirtualKeyCode: 37 });
      await new Promise((r) => setTimeout(r, 900));
      const d = assertShared(domainsOf(await evaluate(cdp, DOMAINS)), "post-key");
      closeTo(before.from - d.from, 300, 5, "ArrowLeft moved the start back 5 minutes");
      closeTo(d.to, before.to, 5, "ArrowLeft kept the end");
    });

    check("tick: refreshSnapshot + drawMain keep an absolute brush", async () => {
      const before = assertShared(domainsOf(await evaluate(cdp, DOMAINS)), "pre-tick");
      await evaluate(cdp, "refreshSnapshot(); drawMain();");
      await new Promise((r) => setTimeout(r, 1500));
      const d = assertShared(domainsOf(await evaluate(cdp, DOMAINS)), "post-tick");
      closeTo(d.from, before.from, 5, "a tick moved the brush start");
      closeTo(d.to, before.to, 5, "a tick moved the brush end");
    });

    check("Reset restores the full window", async () => {
      await evaluate(cdp, `(function(){
        var btns = document.querySelectorAll('#exec-activity .act-controls button');
        for (var i = 0; i < btns.length; i++) if (btns[i].textContent === "Reset") btns[i].click();
      })()`);
      await new Promise((r) => setTimeout(r, 900));
      const d = assertShared(domainsOf(await evaluate(cdp, DOMAINS)), "post-reset");
      closeTo(d.to - d.from, 86400, 300, "reset did not restore ~24 h");
      const url = await evaluate(cdp, "location.search");
      if (/[?&](from|to)=/.test(url)) fail(`reset left a range in the URL: ${url}`);
    });

    check("preset: Last hour follows now with a live chip", async () => {
      await evaluate(cdp, `(function(){
        var btns = document.querySelectorAll('#exec-activity .act-controls button');
        for (var i = 0; i < btns.length; i++) if (btns[i].textContent === "Last hour") btns[i].click();
      })()`);
      await new Promise((r) => setTimeout(r, 900));
      const d = assertShared(domainsOf(await evaluate(cdp, DOMAINS)), "last-hour");
      closeTo(d.to - d.from, 3600, 300, "Last hour spans ~1 h");
      closeTo(d.to, Date.now() / 1000, 300, "Last hour ends at now");
      const live = await evaluate(cdp, `!!document.querySelector('#exec-activity .act-live')`);
      eq(live, true, "a following preset must show the live chip");
    });

    check("workstream tab renders the same stack on its own scope", async () => {
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=WS1` });
      await waitForLoad(cdp);
      await waitFor(cdp, STACK_READY, "WS1 activity stack");
      const rows = domainsOf(await evaluate(cdp, DOMAINS));
      assertShared(rows, "WS1 range");
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
    finish(failed ? 1 : 0);
  } catch (error) {
    console.error(`chart_stack_check failed: ${(error && error.message) || error}`);
    finish(1);
  }
}

main();
