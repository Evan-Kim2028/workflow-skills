#!/usr/bin/env node
/* Where-it-goes check: the stacked bar card below the tiles.
 *
 * Stands up a TEMP dashboard server (temp state dir, synthetic
 * workstreams, port 8910, never the live :8765 or the real data root)
 * seeded with usage buckets spread over the last 24 h (a dominant WS20,
 * mid WS21/WS22, two sub-2% workstreams that must fold into Other, and a
 * sub-2% unassigned share that must stay its own segment), then drives
 * the Execution overview over the DevTools protocol (stdlib only: global
 * fetch and WebSocket, child_process, no npm installs) and asserts:
 *   - three toggle pills (Tokens by workstream | Cost by workstream |
 *     Tokens by model) with aria-pressed, switching modes;
 *   - switching rebuilds only this section (the tiles node is identical
 *     before/after, the wherego node is replaced);
 *   - legend shares sum to 100 ± 1, with an Other and an unassigned entry;
 *   - legend focus dims the other segments (toggle, aria-pressed);
 *   - clicking a workstream segment opens that workstream;
 *   - zero console errors throughout and no horizontal overflow at 1400
 *     and 390 px (legend wraps, chart shrinks).
 * Also saves one screenshot per toggle (--shot-dir, default /tmp).
 * --shots-only <url> skips the temp server and assertions and only
 * captures the three screenshots (for real-data trial runs).
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

function args(argv) {
  const out = { chrome: null, shotsOnly: null, shotDir: "/tmp", width: 0, theme: null };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--chrome" && argv[i + 1]) out.chrome = argv[++i];
    else if (flag === "--shots-only" && argv[i + 1]) out.shotsOnly = argv[++i];
    else if (flag === "--shot-dir" && argv[i + 1]) out.shotDir = argv[++i];
    else if (flag === "--width" && argv[i + 1]) out.width = parseInt(argv[++i], 10) || 0;
    else if (flag === "--theme" && argv[i + 1]) out.theme = argv[++i];
    else if (flag === "-h" || flag === "--help") {
      console.log("usage: wherego_check.mjs [--chrome PATH] [--shots-only URL] [--shot-dir DIR] [--width N] [--theme light|dark]");
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
      } catch { /* next */ }
    }
  }
  return null;
}

function findChrome(explicit) {
  if (explicit) {
    if (fs.existsSync(explicit)) return explicit;
    console.error(`wherego_check: chrome not found at ${explicit}`);
    process.exit(2);
  }
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) return process.env.CHROME_PATH;
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (process.platform === "darwin" && fs.existsSync(mac)) return mac;
  const found = which(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]);
  if (found) return found;
  console.error("wherego_check: no Chrome binary found (tried CHROME_PATH, the default install path and PATH)");
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
    let id = 0, settled = false;
    ws.addEventListener("open", () => { settled = true; resolve({ send, close }); });
    ws.addEventListener("message", (event) => {
      let msg;
      try { msg = JSON.parse(String(event.data)); } catch { return; }
      if (msg.id != null && pending.has(msg.id)) {
        const entry = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) entry.reject(new Error(msg.error.message || JSON.stringify(msg.error)));
        else entry.resolve(msg.result);
      } else if (msg.method && onEvent) onEvent(msg.method, msg.params || {});
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

async function waitForLoad(cdp) {
  const deadline = Date.now() + TIMEOUT_MS;
  for (;;) {
    let state = null;
    try { state = await evaluate(cdp, "document.readyState"); } catch { state = null; }
    if (state === "complete" || state === "interactive") return;
    if (Date.now() > deadline) throw new Error("page never finished loading");
    await new Promise((r) => setTimeout(r, 300));
  }
}

async function waitFor(cdp, expression, what) {
  const deadline = Date.now() + TIMEOUT_MS;
  for (;;) {
    let value = null;
    try { value = await evaluate(cdp, expression); } catch { value = null; }
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`never settled: ${what}`);
    await new Promise((r) => setTimeout(r, 400));
  }
}

async function shot(cdp, file) {
  const res = await cdp.send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(file, Buffer.from(res.data, "base64"));
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
wall = int(now // 3600) * 3600
seed = Store(Path(state) / "dash.sqlite")
seed.upsert_run({
    "run_id": "wherego:seed:1", "source": "muse", "tier": "muse",
    "model": "muse-spark-1.3", "label": "seed-1", "status": "running",
    "started_at": now - 300, "elapsed_s": 300, "out_bytes": 120,
    "out_growing": False,
    "tokens": {"input": 100, "output": 50, "cache_read": 0, "cache_write": 0},
})
# Workstream shares of 100k tokens: WS20 60k, WS21 25k, WS22 9k,
# unassigned 1.5k (kept despite <2%), WS23 1.2k + WS24 0.8k (folded).
plan = [("WS20", 60000), ("WS21", 25000), ("WS22", 9000), ("", 1500), ("WS23", 1200), ("WS24", 800)]
models = [("muse-spark-1.3", "muse", 10000), ("opus", "claude", 30000),
          ("sonnet", "claude", 15000), ("haiku", "claude", 4000), ("grok-4.7", "cursor", 1000)]
# Note: the muse-spark row shares its (hour, model, workstream, tool) key
# with the WS20 plan row above, so INSERT OR REPLACE lands 10k for WS20
# there; WS20 still totals 60k with the other four model rows.
conn = seed._conn
for h in range(6):
    t = wall - h * 3600
    frac = [0.30, 0.22, 0.18, 0.12, 0.10, 0.08][h]
    for ws, tot in plan:
        v = int(tot * frac)
        conn.execute(
            "INSERT OR REPLACE INTO usage_buckets(hour_ts, model, workstream, tool, input_tokens, output_tokens, cache_read, cache_write, reasoning, missing, cost_usd)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (t, "muse-spark-1.3", ws, "muse", v // 2, v // 4, v // 4, 0, 0, 0, round(v * 1.2e-6, 6)),
        )
    for model, tool, tot in models:
        v = int(tot * frac)
        conn.execute(
            "INSERT OR REPLACE INTO usage_buckets(hour_ts, model, workstream, tool, input_tokens, output_tokens, cache_read, cache_write, reasoning, missing, cost_usd)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (t, model, "WS20", tool, v // 2, v // 4, v // 4, 0, 0, 0, round(v * 1.2e-6, 6)),
        )
conn.commit()
seed.close()
store = Store(Path(state) / "dash.sqlite")
srv = DashServer(store, Registry([]), "127.0.0.1", port, stream_interval=2, cfg=default_cfg())
srv.serve_forever(poll_interval=0.05)
`;

const checks = [];
function check(name, fn) { checks.push([name, fn]); }
function fail(message) { throw new Error(message); }
function eq(actual, expected, what) {
  if (actual !== expected) fail(`${what}: want ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

const pillsExpr = `Array.prototype.map.call(document.querySelectorAll('#exec-wherego .seg-ctl button'), function(b){ return {text: b.textContent, pressed: b.getAttribute('aria-pressed'), tag: b.tagName}; })`;
const legendExpr = `Array.prototype.map.call(document.querySelectorAll('#exec-wherego .wg-legend button.lentry'), function(b){ return {key: b.getAttribute('data-key'), share: parseFloat(b.getAttribute('data-share')), pressed: b.getAttribute('aria-pressed'), tag: b.tagName}; })`;
const orderExpr = `Array.prototype.map.call(document.querySelectorAll('#main [data-sec]'), function(s){ return s.getAttribute('data-sec'); })`;

async function scrollToCard(cdp) {
  /* Instant jump, not smooth: the shell sets scroll-behavior:smooth, so
     scrollIntoView animates for seconds on a tall overview. */
  for (let i = 0; i < 4; i++) {
    await evaluate(cdp, `(function(){ var n = document.querySelector('#x-wherego'); if (!n) return; window.scrollTo({top: n.getBoundingClientRect().top + window.scrollY - 60, behavior: 'auto'}); })()`);
    await new Promise((r) => setTimeout(r, 500));
    const top = await evaluate(cdp, `(function(){ var n = document.querySelector('#x-wherego'); return n ? n.getBoundingClientRect().top : -9999; })()`);
    if (top >= 0 && top < 300) return;
  }
  fail("the wherego card never settled into the viewport");
}

async function captureShots(cdp, dir, tag) {
  for (const [mode, file] of [["ws-tokens", "tokens"], ["ws-cost", "cost"], ["model", "models"]]) {
    const idx = mode === "ws-tokens" ? 0 : mode === "ws-cost" ? 1 : 2;
    await evaluate(cdp, `(function(){ var bs = document.querySelectorAll('#exec-wherego .seg-ctl button'); if (bs[${idx}]) bs[${idx}].click(); })()`);
    await waitFor(cdp, `document.querySelectorAll('#exec-wherego .wg-legend button.lentry').length > 0`, `${mode} legend after pill click`);
    await scrollToCard(cdp);
    await shot(cdp, path.join(dir, `${tag}-${file}.png`));
  }
  console.log(`ok: screenshots saved to ${dir} (${tag}-{tokens,cost,models}.png)`);
}

async function runShotsOnly(chrome, url, dir, opts) {
  opts = opts || {};
  let cdp = null, child = null;
  const errors = [];
  const finish = (code) => {
    try { if (cdp) cdp.close(); } catch { /* ignore */ }
    try { if (child) child.kill(); } catch { /* ignore */ }
    process.exit(code);
  };
  process.on("SIGINT", () => finish(1));
  try {
    const dbgPort = await freePort();
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dash-wherego-prof-"));
    child = spawn(chrome,
      ["--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-extensions",
        `--user-data-dir=${profile}`, `--remote-debugging-port=${dbgPort}`, "about:blank"],
      { stdio: ["ignore", "ignore", "pipe"] });
    await waitForJson(`http://127.0.0.1:${dbgPort}/json/version`, TIMEOUT_MS);
    const created = await (await fetch(`http://127.0.0.1:${dbgPort}/json/new?about:blank`, { method: "PUT" })).json();
    cdp = await connectDebugger(created.webSocketDebuggerUrl, (method, params) => {
      if (method === "Runtime.consoleAPICalled" && params.type === "error") errors.push(`console.error: ${(params.args || []).map((a) => a.description || a.value).join(" ").slice(0, 200)}`);
      else if (method === "Runtime.exceptionThrown") errors.push(`page exception: ${JSON.stringify(params.exceptionDetails || {}).slice(0, 200)}`);
    });
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Page.navigate", { url: `${url}/?view=exec&ws=all` });
    await waitForLoad(cdp);
    await waitFor(cdp, `document.querySelector('#x-wherego') && document.querySelectorAll('#exec-wherego .wg-legend button.lentry').length > 0`, "wherego legend on trial server");
    if (opts.width) {
      await cdp.send("Emulation.setDeviceMetricsOverride", { width: opts.width, height: 900, deviceScaleFactor: 1, mobile: false });
      await new Promise((r) => setTimeout(r, 2000));
      await waitFor(cdp, `window.innerWidth === ${opts.width}`, "narrow viewport took effect");
      await waitFor(cdp, `document.querySelectorAll('#exec-wherego .wg-legend button.lentry').length > 0`, "wherego legend after viewport switch");
    }
    if (opts.theme) {
      await evaluate(cdp, `(function(){ document.documentElement.setAttribute('data-theme', '${opts.theme}'); try { localStorage.setItem('agent-dash-theme', '${opts.theme}'); } catch (e) {} if (window.drawMain) window.drawMain(); })()`);
      await waitFor(cdp, `document.querySelectorAll('#exec-wherego .wg-legend button.lentry').length > 0`, "wherego legend after theme switch");
      await new Promise((r) => setTimeout(r, 800));
    }
    fs.mkdirSync(dir, { recursive: true });
    const tag = "wherego-after" + (opts.width ? `-${opts.width}` : "") + (opts.theme ? `-${opts.theme}` : "");
    await captureShots(cdp, dir, tag);
    if (errors.length) for (const e of errors) console.log(`FAIL: ${e}`);
    else console.log("ok: zero console errors");
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* ignore */ }
    finish(errors.length ? 1 : 0);
  } catch (error) {
    console.error(`wherego shots failed: ${(error && error.message) || error}`);
    finish(1);
  }
}

async function main() {
  const opts = args(process.argv.slice(2));
  const chrome = findChrome(opts.chrome);
  if (opts.shotsOnly) { await runShotsOnly(chrome, opts.shotsOnly.replace(/\/$/, ""), opts.shotDir, opts); return; }

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "dash-wherego-"));
  const stateDir = path.join(tmp, "state");
  const wsDir = path.join(tmp, "workstreams");
  fs.mkdirSync(stateDir, { recursive: true });
  fs.mkdirSync(wsDir, { recursive: true });
  const serverPy = path.join(tmp, "wherego_server.py");
  fs.writeFileSync(serverPy, SERVER_PY);
  const serverLog = path.join(tmp, "server.log");
  const logFd = fs.openSync(serverLog, "w");
  const server = spawn("python3", [serverPy, DASH_ROOT, stateDir, String(PORT)], {
    cwd: DASH_ROOT,
    stdio: ["ignore", logFd, logFd],
    env: { ...process.env, AGENT_DASH_STATE_DIR: stateDir, AGENT_DASH_WORKSTREAMS: wsDir, AGENT_DASH_IGNORE_LOCAL: "1" },
  });
  let cdp = null, child = null;
  const errors = [];
  const finish = (code) => {
    try { if (cdp) cdp.close(); } catch { /* ignore */ }
    try { if (child) child.kill(); } catch { /* ignore */ }
    try { server.kill(); } catch { /* ignore */ }
    try { fs.closeSync(logFd); } catch { /* ignore */ }
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
        const u = await waitForJson(`${URL}/api/usage?group=workstream&bucket=hour`, TIMEOUT_MS);
        if (!Array.isArray(u.buckets) || !u.buckets.length) throw new Error("seeded usage buckets never arrived");
        clearTimeout(t);
        resolve();
      })().catch((e) => { clearTimeout(t); reject(e); });
    });

    const dbgPort = await freePort();
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dash-wherego-prof-"));
    child = spawn(chrome,
      ["--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-extensions",
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

    check("three pills, tokens by workstream first", async () => {
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
      await waitForLoad(cdp);
      const pills = await waitFor(cdp, `(${pillsExpr}).length === 3 && (${pillsExpr})`, "three toggle pills");
      eq(JSON.stringify(pills.map((p) => p.text)), JSON.stringify(["Tokens by workstream", "Cost by workstream", "Tokens by model"]), "pill labels");
      if (pills.some((p) => p.tag !== "BUTTON")) fail("pills are not real <button>s");
      eq(JSON.stringify(pills.map((p) => p.pressed)), JSON.stringify(["true", "false", "false"]), "default pressed pill");
    });

    check("legend shares sum to 100 ± 1, Other folds, unassigned kept", async () => {
      const legend = await waitFor(cdp, `(${legendExpr}).length >= 4 && (${legendExpr})`, "folded legend");
      const keys = legend.map((l) => l.key);
      if (!keys.includes("Other")) fail(`no folded Other segment (keys: ${keys.join(",")})`);
      if (!keys.includes("unassigned")) fail(`unassigned was folded away (keys: ${keys.join(",")})`);
      if (keys.includes("WS23") || keys.includes("WS24")) fail("sub-2% workstreams leak past the fold");
      const sum = legend.reduce((a, l) => a + l.share, 0);
      if (Math.abs(sum - 100) > 1) fail(`legend shares sum to ${sum}, want 100 ± 1`);
      if (legend.some((l) => l.tag !== "BUTTON")) fail("legend items are not real <button>s");
      const order = await evaluate(cdp, orderExpr);
      const i = order.indexOf("wherego");
      eq(JSON.stringify(order.slice(i - 1, i + 1)), JSON.stringify(["pulse", "wherego"]), `card sits right below pulse, where the tiles used to be (order: ${order.join(",")})`);
    });

    check("cost pill rebuilds only this section", async () => {
      await evaluate(cdp, "window.__xTiles = document.querySelector('#x-tiles')");
      await evaluate(cdp, "window.__xWherego = document.querySelector('#x-wherego')");
      await evaluate(cdp, `(function(){ var bs = document.querySelectorAll('#exec-wherego .seg-ctl button'); bs[1].click(); })()`);
      await waitFor(cdp, `(${pillsExpr})[1].pressed === "true" && (${legendExpr}).length > 0 && document.querySelector('#x-wherego') !== window.__xWherego`, "cost mode legend");
      const legend = await evaluate(cdp, legendExpr);
      const sum = legend.reduce((a, l) => a + l.share, 0);
      if (Math.abs(sum - 100) > 1) fail(`cost legend shares sum to ${sum}`);
      const total = await evaluate(cdp, `document.querySelector('#exec-wherego .ltotal').textContent`);
      if (total.indexOf("$") < 0) fail(`cost legend total never reads dollars: ${JSON.stringify(total)}`);
      eq(await evaluate(cdp, "window.__xTiles === document.querySelector('#x-tiles')"), true, "the tiles node is identical after the toggle");
    });

    check("model pill rebuilds only this section", async () => {
      await evaluate(cdp, "window.__xWherego2 = document.querySelector('#x-wherego')");
      await evaluate(cdp, `(function(){ var bs = document.querySelectorAll('#exec-wherego .seg-ctl button'); bs[2].click(); })()`);
      await waitFor(cdp, `(${pillsExpr})[2].pressed === "true" && document.querySelector('#x-wherego') !== window.__xWherego2`, "model mode rebuild");
      const legend = await waitFor(cdp, `(${legendExpr}).length >= 3 && (${legendExpr})`, "model legend");
      const sum = legend.reduce((a, l) => a + l.share, 0);
      if (Math.abs(sum - 100) > 1) fail(`model legend shares sum to ${sum}`);
      eq(await evaluate(cdp, "window.__xTiles === document.querySelector('#x-tiles')"), true, "the tiles node is identical after the model toggle");
    });

    check("legend focus dims the rest, segment click opens the workstream", async () => {
      await evaluate(cdp, `(function(){ var bs = document.querySelectorAll('#exec-wherego .seg-ctl button'); bs[0].click(); })()`);
      await waitFor(cdp, `(${pillsExpr})[0].pressed === "true"`, "back on tokens by workstream");
      await evaluate(cdp, `(function(){ var b = document.querySelector('#exec-wherego .wg-legend button.lentry[data-key="WS20"]'); if (b) b.click(); })()`);
      await new Promise((r) => setTimeout(r, 500));
      const pressed = await evaluate(cdp, `document.querySelector('#exec-wherego .wg-legend button.lentry[data-key="WS20"]').getAttribute('aria-pressed')`);
      eq(pressed, "true", "legend focus toggles aria-pressed");
      const dimmed = await evaluate(cdp, `document.querySelectorAll('#exec-wherego rect.wg-seg[data-dim="1"]').length`);
      if (!dimmed) fail("focusing a legend item dims no segment");
      await evaluate(cdp, `(function(){ var b = document.querySelector('#exec-wherego .wg-legend button.lentry[data-key="WS20"]'); if (b) b.click(); })()`);
      await new Promise((r) => setTimeout(r, 500));
      await evaluate(cdp, `(function(){ var r = document.querySelector('#exec-wherego rect.wg-seg[data-key="WS20"]'); if (r) r.dispatchEvent(new MouseEvent('click', {bubbles: true})); })()`);
      await waitFor(cdp, `location.search.indexOf('ws=WS20') >= 0`, "segment click opens WS20");
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
      await waitForLoad(cdp);
      await waitFor(cdp, `document.querySelector('#x-wherego') && document.querySelectorAll('#exec-wherego .wg-legend button.lentry').length > 0`, "back on overview");
    });

    check("no horizontal overflow at 1400 px", async () => {
      const over = await evaluate(cdp, "document.documentElement.scrollWidth - window.innerWidth");
      if (over > 1) fail(`page scrolls sideways by ${over}px at 1400`);
    });

    check("no horizontal overflow at 390 px", async () => {
      /* deviceScaleFactor 1, no mobile flag: the one override shape this
         headless Chrome honors (mobile:true is ignored here). The manage
         table already overflows the page this narrow on main, so the card
         asserts its own bounds: section, chart and legend all fit. */
      await cdp.send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
      await new Promise((r) => setTimeout(r, 2000));
      const inner = await evaluate(cdp, "window.innerWidth");
      eq(inner, 390, "narrow viewport took effect");
      const wg = await evaluate(cdp, `(function(){ var n = document.querySelector('#x-wherego'); if (!n) return null; var r = n.getBoundingClientRect(); return {right: r.right, w: r.width}; })()`);
      if (!wg) fail("the wherego section is gone at 390 px");
      if (wg.right > inner + 1) fail(`the wherego section overflows at 390 px (right ${Math.round(wg.right)})`);
      const svgR = await evaluate(cdp, `(function(){ var s = document.querySelector('#exec-wherego svg'); if (!s) return null; return s.getBoundingClientRect().right; })()`);
      if (svgR == null) fail("the wherego chart is gone at 390 px");
      if (svgR > inner + 1) fail(`the wherego chart overflows at 390 px (right ${Math.round(svgR)})`);
      const lgR = await evaluate(cdp, `(function(){ var l = document.querySelector('#exec-wherego .wg-legend'); if (!l) return null; return l.getBoundingClientRect().right; })()`);
      if (lgR > inner + 1) fail(`the wherego legend overflows at 390 px (right ${Math.round(lgR)})`);
      await cdp.send("Emulation.clearDeviceMetricsOverride");
      await new Promise((r) => setTimeout(r, 800));
    });

    let failed = 0;
    for (const [name, fn] of checks) {
      try { await fn(); console.log(`ok: ${name}`); }
      catch (error) { failed++; console.log(`FAIL: ${name}: ${(error && error.message) || error}`); }
    }
    await captureShots(cdp, opts.shotDir, "wherego-after");
    if (errors.length) {
      failed += errors.length;
      for (const e of errors) console.log(`FAIL: ${e}`);
    } else {
      console.log("ok: zero console errors");
    }
    if (failed) {
      console.log(`debug leftovers: tmp ${tmp} server log ${serverLog}`);
    } else {
      try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* ignore */ }
      try { fs.rmSync(tmp, { recursive: true, force: true }); } catch { /* ignore */ }
    }
    finish(failed ? 1 : 0);
  } catch (error) {
    console.error(`wherego_check failed: ${(error && error.message) || error}`);
    finish(1);
  }
}

main();
