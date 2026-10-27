#!/usr/bin/env node
/* Pulse check: the overview pulse card leads, and momentum sorts stuck first.
 *
 * Stands up a TEMP dashboard server (temp state dir, temp workstreams,
 * port 8906, never the live :8765 or the real data root) seeded with
 * three workstreams — WS1 live but gone quiet (stuck), WS2 live and
 * fresh (moving), WS3 quiet with old tokens (idle) — then drives the
 * Execution view over the DevTools protocol (stdlib only: global
 * fetch and WebSocket, child_process, no npm installs) and asserts:
 *   - the pulse section is the first [data-sec] on ws=all, with five
 *     KPI cells and three momentum rows ordered stuck, moving, idle;
 *   - no pulse section renders on a workstream tab;
 *   - zero console errors throughout, and no horizontal page overflow
 *     at 390 px in either theme.
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
const PORT = 8906;
const URL = `http://127.0.0.1:${PORT}`;
const TIMEOUT_MS = 30000;

function args(argv) {
  const out = { chrome: null };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--chrome" && argv[i + 1]) out.chrome = argv[++i];
    else if (flag === "-h" || flag === "--help") {
      console.log("usage: pulse_check.mjs [--chrome PATH]");
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
    console.error(`pulse_check: chrome not found at ${explicit}`);
    process.exit(2);
  }
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (process.platform === "darwin" && fs.existsSync(mac)) return mac;
  const found = which(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]);
  if (found) return found;
  console.error("pulse_check: no Chrome binary found (tried CHROME_PATH, the default install path and PATH)");
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

/* Three workstreams: WS1 live but quiet in the last 2 h (stuck),
   WS2 live and fresh (moving), WS3 planned with old tokens only
   (idle). Usage cells are hour-aligned so the pulse folder lands
   them in the right buckets. */
const SERVER_PY = `import os, sys, time
root, state, wsroot, port = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
sys.path.insert(0, root)
os.environ.setdefault("AGENT_DASH_IGNORE_LOCAL", "1")
from pathlib import Path
from dash.store import Store
from dash.server import DashServer
from dash.sources import Registry
from agent_dash import default_cfg
import json
now = time.time()
hour = int(now // 3600) * 3600
for ws, st in (("WS1", "active"), ("WS2", "active"), ("WS3", "planned")):
    d = Path(wsroot) / ws
    d.mkdir(parents=True, exist_ok=True)
    (d / "bundle.json").write_text(json.dumps({"id": ws, "title": "Pulse %s" % ws, "kind": "workstream", "state": st}))
seed = Store(Path(state) / "dash.sqlite")
seed.upsert_run({
    "run_id": "pulse-check:ws1-live",
    "source": "muse",
    "tier": "muse",
    "model": "muse-1",
    "label": "ws1-live",
    "workstream": "WS1",
    "status": "running",
    "started_at": now - 300,
    "elapsed_s": 300,
    "out_bytes": 100,
    "out_growing": False,
    "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
})
seed.upsert_run({
    "run_id": "pulse-check:ws2-live",
    "source": "muse",
    "tier": "muse",
    "model": "muse-1",
    "label": "ws2-live",
    "workstream": "WS2",
    "status": "running",
    "started_at": now - 60,
    "elapsed_s": 60,
    "out_bytes": 100,
    "out_growing": True,
    "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
})
seed.upsert_run({
    "run_id": "pulse-check:ws3-old",
    "source": "muse",
    "tier": "muse",
    "model": "muse-1",
    "label": "ws3-old",
    "workstream": "WS3",
    "status": "done",
    "started_at": now - 21 * 3600,
    "ended_at": now - 20 * 3600,
    "out_bytes": 50,
    "out_growing": False,
    "tokens": {"input": 4000, "output": 1000, "cache_read": 0, "cache_write": 0},
})
cells = []
for k in (6, 5, 4):
    cells.append((hour - k * 3600, "muse-1", "WS1", "muse", 400, 200, 0, 0, 0, 0, 0.01))
cells.append((hour - 3600, "muse-1", "WS2", "muse", 400, 200, 0, 0, 0, 0, 0.01))
cells.append((hour, "muse-1", "WS2", "muse", 200, 100, 0, 0, 0, 0, 0.01))
cells.append((hour - 20 * 3600, "muse-1", "WS3", "muse", 400, 200, 0, 0, 0, 0, 0.01))
seed._conn.executemany(
    "INSERT INTO usage_buckets(hour_ts, model, workstream, tool, input_tokens, output_tokens, cache_read, cache_write, reasoning, missing, cost_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
    cells,
)
seed._conn.commit()
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

/* Pulse section snapshot: first section id, KPI cells, momentum rows. */
const pulseState = `(function(){
  var secs = Array.prototype.map.call(document.querySelectorAll('#main [data-sec]'), function(s){ return s.getAttribute('data-sec'); });
  var card = document.querySelector('#x-pulse');
  if (!card) return {secs};
  var kpis = Array.prototype.map.call(card.querySelectorAll('[data-kpi]'), function(k){ return k.getAttribute('data-kpi'); });
  var rows = Array.prototype.map.call(card.querySelectorAll('.pulse-row[data-ws]'), function(r){
    return {ws: r.getAttribute('data-ws'), m: r.getAttribute('data-m'), tag: r.tagName,
      glyph: (r.querySelector('.pulse-glyph') || {}).textContent || null,
      label: r.getAttribute('aria-label') || ''};
  });
  var prog = card.querySelector('[data-kpi="in-progress"]');
  var liveEl = card.querySelector('[data-kpi="live"]');
  return {secs, kpis, rows,
    active: prog ? prog.getAttribute('data-active') : null,
    planned: prog ? prog.getAttribute('data-planned') : null,
    live: liveEl ? liveEl.getAttribute('data-live') : null};
})()`;

async function main() {
  const opts = args(process.argv.slice(2));
  const chrome = findChrome(opts.chrome);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "dash-pulse-"));
  const stateDir = path.join(tmp, "state");
  const wsDir = path.join(tmp, "workstreams");
  fs.mkdirSync(stateDir, { recursive: true });
  fs.mkdirSync(wsDir, { recursive: true });
  const serverPy = path.join(tmp, "pulse_server.py");
  fs.writeFileSync(serverPy, SERVER_PY);
  const serverLog = path.join(tmp, "server.log");
  const logFd = fs.openSync(serverLog, "w");
  const server = spawn("python3", [serverPy, DASH_ROOT, stateDir, wsDir, String(PORT)], {
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
      const t = setTimeout(() => reject(new Error("temp server never came up (is :8906 already taken?)")), TIMEOUT_MS);
      server.once("exit", (code) => {
        clearTimeout(t);
        reject(new Error(`temp server exited early with code ${code}; see ${serverLog}`));
      });
      (async () => {
        const snap = await waitForJson(`${URL}/api/snapshot?ws=all`, TIMEOUT_MS);
        if (!Array.isArray(snap.running) || snap.running.length !== 2) {
          throw new Error(`seeded 2 live agents but the API shows ${(snap.running || []).length}`);
        }
        const usage = await waitForJson(`${URL}/api/usage?group=workstream&bucket=hour&since=${Math.floor(Date.now() / 1000) - 24 * 3600}`, TIMEOUT_MS);
        if (!Array.isArray(usage.buckets) || !usage.buckets.length) {
          throw new Error("seeded usage cells but the API shows none");
        }
        clearTimeout(t);
        resolve();
      })().catch((e) => {
        clearTimeout(t);
        reject(e);
      });
    });

    const dbgPort = await freePort();
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dash-pulse-prof-"));
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

    check("overview: pulse leads with five KPIs and stuck-first rows", async () => {
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
      await waitForLoad(cdp);
      const st = await waitFor(cdp, `(${pulseState}) && ${pulseState}.rows && ${pulseState}.rows.length === 3 && (${pulseState})`, "pulse card with 3 momentum rows");
      eq(st.secs[0], "pulse", `pulse is the first section (full: ${st.secs.join(",")})`);
      eq(JSON.stringify(st.kpis), JSON.stringify(["in-progress", "live", "tokens", "cost", "finished"]), `five KPI cells (got ${st.kpis.join(",")})`);
      eq(JSON.stringify(st.rows.map((r) => r.m)), JSON.stringify(["stuck", "moving", "idle"]), `momentum order (got ${st.rows.map((r) => r.ws + ":" + r.m).join(",")})`);
      eq(st.rows[0].ws, "WS1", "the seeded quiet workstream sorts first");
      eq(st.active, "2", "two active workstreams in progress");
      eq(st.planned, "1", "the planned workstream with old tokens counts");
      eq(st.live, "2", "two live agents");
      if (st.rows.some((r) => r.tag !== "BUTTON")) fail("momentum rows are not real <button> elements");
      if (st.rows.some((r) => !r.glyph || !r.label)) fail("a momentum row misses its glyph or accessible name");
    });

    check("workstream tab: no pulse section", async () => {
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=WS2` });
      await waitForLoad(cdp);
      const secs = await waitFor(cdp, `Array.prototype.map.call(document.querySelectorAll('#main [data-sec]'), function(s){ return s.getAttribute('data-sec'); }).length > 0 && Array.prototype.map.call(document.querySelectorAll('#main [data-sec]'), function(s){ return s.getAttribute('data-sec'); })`, "workstream sections");
      if (secs.indexOf("pulse") >= 0) fail(`pulse renders on a workstream tab (full: ${secs.join(",")})`);
      if (secs.indexOf("tiles") < 0) fail(`tiles left the workstream tab (full: ${secs.join(",")})`);
    });

    check("no horizontal overflow at 390 px", async () => {
      await cdp.send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
      await waitForLoad(cdp);
      await waitFor(cdp, `(${pulseState}) && ${pulseState}.rows && ${pulseState}.rows.length === 3`, "pulse rows at 390 px");
      const sideways = await evaluate(cdp, `document.documentElement.scrollWidth - document.documentElement.clientWidth`);
      if (sideways > 1) fail(`page scrolls sideways at 390 px: overflow ${sideways}px`);
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
    console.error(`pulse_check failed: ${(error && error.message) || error}`);
    finish(1);
  }
}

main();
