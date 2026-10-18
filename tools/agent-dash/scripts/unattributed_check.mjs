#!/usr/bin/env node
/* Unattributed-model toggle check: runs without a recorded model hide by default.
 *
 * Stands up a TEMP dashboard server (temp state dir, synthetic runs with
 * honest unknown models, port 8907, never the live :8765 or the real data
 * root), then drives the Execution overview over the DevTools protocol
 * (stdlib only: global fetch and WebSocket, child_process, no npm installs)
 * and asserts:
 *   - by default no "model not recorded" entry is listed (tile captions,
 *     token legend, live-agents captions), while a muted one-line note
 *     names the hidden runs and tokens beside a real Show button;
 *   - the hidden share still sums into the stacked total (one muted
 *     Unattributed legend entry) and every headline total matches the API;
 *   - clicking Show lists the honest entries with a dashed outline, keeps
 *     every total identical, and the choice persists across a reload;
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
const PORT = 8907;
const URL = `http://127.0.0.1:${PORT}`;
const TIMEOUT_MS = 30000;

function args(argv) {
  const out = { chrome: null };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--chrome" && argv[i + 1]) out.chrome = argv[++i];
    else if (flag === "-h" || flag === "--help") {
      console.log("usage: unattributed_check.mjs [--chrome PATH]");
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
    console.error(`unattributed_check: chrome not found at ${explicit}`);
    process.exit(2);
  }
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (process.platform === "darwin" && fs.existsSync(mac)) return mac;
  const found = which(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]);
  if (found) return found;
  console.error("unattributed_check: no Chrome binary found (tried CHROME_PATH, the default install path and PATH)");
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
SPARK = "muse-spark-1.3-contributor"
seed = Store(Path(state) / "dash.sqlite")
def add_run(rid, **kw):
    row = {"run_id": rid, "source": "muse", "tier": "muse", "label": rid.split(":")[-1],
           "status": "done", "started_at": now - 300, "ended_at": now - 100}
    row.update(kw)
    seed.upsert_run(row)
def add_use(rid, model, i, o, mid):
    seed.add_usage({"ts": now - 200, "session_id": "s-" + rid, "run_id": rid,
                    "model": model, "tool": "muse", "message_id": mid,
                    "input": i, "output": o, "cache_read": 0,
                    "cache_write": 0, "reasoning": 0})
for k in ("a", "b", "c"):
    add_run("unatt:live:known-" + k, status="running", started_at=now - 60,
            ended_at=None, model=SPARK)
    add_use("unatt:live:known-" + k, SPARK, 100, 50, "uk-live-" + k)
for k in ("a", "b"):
    add_run("unatt:live:honest-" + k, status="running", started_at=now - 60,
            ended_at=None, verdict="PASS")
    add_use("unatt:live:honest-" + k, None, 70, 0, "uh-live-" + k)
for k in ("a", "b"):
    add_run("unatt:done:known-" + k, model=SPARK, verdict="PASS")
    add_use("unatt:done:known-" + k, SPARK, 40, 10, "uk-done-" + k)
for k in ("a", "b"):
    add_run("unatt:done:honest-" + k, verdict="PASS")
    add_use("unatt:done:honest-" + k, None, 7, 0, "uh-done-" + k)
add_run("unatt:done:marker")
seed.close()
store = Store(Path(state) / "dash.sqlite")
srv = DashServer(store, Registry([]), "127.0.0.1", port, stream_interval=2, cfg=default_cfg())
srv.serve_forever(poll_interval=0.05)
`;

function billed(t) {
  return (t.input || 0) + (t.output || 0) + (t.cache_read || 0) + (t.cache_write || 0);
}

async function apiSnapshot() {
  const snap = await (await fetch(`${URL}/api/snapshot?ws=all&usage_group=model`)).json();
  const tokens = billed(snap.totals.tokens);
  let buckets = 0;
  let hiddenBuckets = 0;
  for (const b of snap.usage || []) {
    const v = billed(b);
    buckets += v;
    if (String(b.key || "").endsWith("-unknown")) hiddenBuckets += v;
  }
  return { snap, tokens, buckets, hiddenBuckets };
}

const READ = `(function () {
  function texts(sel) {
    return Array.prototype.map.call(document.querySelectorAll(sel), function (e) { return e.textContent; }).join(" | ");
  }
  var tile = document.querySelector('#exec-tiles [data-tile="tokens"]');
  var cost = document.querySelector('#exec-tiles [data-tile="cost"]');
  var ltotal = document.querySelector("#exec-tokens .ltotal");
  var unatt = document.querySelector('#exec-tokens [data-key="unattributed"]');
  return {
    listed: texts("#exec-tiles .xline") + " || " + texts("#exec-tokens .tok-legend") + " || " + texts(".conc-models") + " || " + texts(".tl-model-label"),
    bodyUnknown: (document.body.textContent || "").indexOf("model not recorded") >= 0,
    tileTokens: tile && tile.getAttribute("data-tokens"),
    tileCost: cost && cost.getAttribute("data-cost-usd"),
    legendTotal: ltotal && ltotal.getAttribute("data-legend-total"),
    unattV: unatt && unatt.getAttribute("data-v"),
    notes: Array.prototype.map.call(document.querySelectorAll(".unatt-note"), function (e) { return e.textContent; }),
    noteData: Array.prototype.map.call(document.querySelectorAll(".unatt-note"), function (e) { return e.getAttribute("data-unatt-runs") + "/" + e.getAttribute("data-unatt-tokens"); }),
    btns: Array.prototype.map.call(document.querySelectorAll(".unatt-btn"), function (e) { return e.textContent + "||" + e.getAttribute("aria-pressed"); }),
    dashed: document.querySelectorAll('#exec-tokens rect[stroke-dasharray]').length
  };
})()`;

function fail(message) {
  throw new Error(message);
}

function eq(actual, expected, what) {
  if (actual !== expected) fail(`${what}: want ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

function ok(cond, what) {
  if (!cond) fail(`assertion failed: ${what}`);
}

async function main() {
  const opts = args(process.argv.slice(2));
  const chrome = findChrome(opts.chrome);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "dash-unattributed-"));
  const stateDir = path.join(tmp, "state");
  const wsDir = path.join(tmp, "workstreams");
  fs.mkdirSync(stateDir, { recursive: true });
  fs.mkdirSync(wsDir, { recursive: true });
  const serverPy = path.join(tmp, "unattributed_server.py");
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
      const t = setTimeout(() => reject(new Error("temp server never came up (is :8907 already taken?)")), TIMEOUT_MS);
      server.once("exit", (code) => {
        clearTimeout(t);
        reject(new Error(`temp server exited early with code ${code}; see ${serverLog}`));
      });
      (async () => {
        const snap = await waitForJson(`${URL}/api/snapshot?ws=all`, TIMEOUT_MS);
        if (!Array.isArray(snap.running) || snap.running.length !== 5) {
          throw new Error(`seeded 5 live agents but the API shows ${(snap.running || []).length}`);
        }
        const u = snap.counts && snap.counts.unattributed;
        if (!u || u.runs !== 4 || u.tokens !== 154) {
          throw new Error(`seeded 4 unattributed runs / 154 tokens but the API shows ${JSON.stringify(u)}`);
        }
        clearTimeout(t);
        resolve();
      })().catch((e) => {
        clearTimeout(t);
        reject(e);
      });
    });

    const dbgPort = await freePort();
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dash-unattributed-prof-"));
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
        errors.push(`console.error: ${(params.args || []).map((a) => a.value || a.description || "").join(" ").slice(0, 300)}`);
      } else if (method === "Runtime.exceptionThrown") {
        const d = params.exceptionDetails || {};
        errors.push(`exception: ${(d.text || "")} ${(d.exception && d.exception.description) || ""}`.slice(0, 300));
      }
    });
    await cdp.send("Runtime.enable");
    await cdp.send("Page.enable");
    await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=all` });
    await waitFor(cdp, `!!document.querySelector("#exec-tiles .unatt-btn")`, "tiles control");
    await waitFor(cdp, `!!document.querySelector("#exec-activity .unatt-btn")`, "activity control");
    await waitFor(cdp, `!!document.querySelector("#exec-tokens .ltotal")`, "token legend");

    /* Default: hidden, with the note, totals matching the API. */
    const before = await apiSnapshot();
    const dom = await evaluate(cdp, READ);
    const after = await apiSnapshot();
    ok(dom.listed.indexOf("model not recorded") < 0, `unknown model listed by default: ${dom.listed.slice(0, 300)}`);
    /* Run-table model cells keep the per-run record (like the drawer);
       only the model listings above must hide. */
    eq(dom.notes.length, 2, "muted notes (tiles + activity)");
    for (const n of dom.notes) {
      ok(/^Hidden: 4 runs without a recorded model, 154 tokens$/.test(n), `note text: ${n}`);
    }
    eq(dom.noteData.join(","), "4/154,4/154", "note data attributes");
    eq(dom.btns.length, 2, "two toggle buttons");
    for (const b of dom.btns) eq(b, "Show unattributed||false", "default-off button");
    eq(dom.unattV, "154", "folded Unattributed legend entry");
    for (const api of [before, after]) {
      if (String(dom.tileTokens) === String(api.tokens) && String(dom.legendTotal) === String(api.buckets)) {
        eq(String(dom.tileCost), String(api.snap.totals.cost_usd), "cost tile vs API");
        break;
      }
    }
    if (String(dom.tileTokens) !== String(before.tokens) && String(dom.tileTokens) !== String(after.tokens)) {
      fail(`tile tokens ${dom.tileTokens} match neither API read (${before.tokens}, ${after.tokens})`);
    }
    if (String(dom.legendTotal) !== String(before.buckets) && String(dom.legendTotal) !== String(after.buckets)) {
      fail(`legend Total ${dom.legendTotal} matches neither API read (${before.buckets}, ${after.buckets})`);
    }
    eq(before.hiddenBuckets, 154, "API hidden bucket sum");

    /* Toggle on: honest entries appear with a dashed outline, totals identical. */
    await evaluate(cdp, `document.querySelector(".tiles-unatt .unatt-btn").click()`);
    await waitFor(cdp, `!!document.querySelector('.tiles-unatt .unatt-btn[aria-pressed="true"]')`, "toggle on");
    const shown = await evaluate(cdp, READ);
    ok(shown.listed.indexOf("model not recorded") >= 0, `unknown model not listed after Show: ${shown.listed.slice(0, 300)}`);
    eq(shown.btns.length, 2, "two toggle buttons after Show");
    for (const b of shown.btns) eq(b, "Hide unattributed||true", "toggled-on button");
    ok(!shown.notes.length, `note lingers after Show: ${JSON.stringify(shown.notes)}`);
    eq(shown.unattV, null, "folded entry lingers after Show");
    ok(shown.dashed > 0, "no dashed outline on shown unknown segments");
    eq(shown.tileTokens, dom.tileTokens, "tile total moved under the toggle");
    eq(shown.legendTotal, dom.legendTotal, "legend Total moved under the toggle");

    /* Reload in the same tab: the choice persists for the session. */
    await cdp.send("Page.reload");
    await waitFor(cdp, `!!document.querySelector('.tiles-unatt .unatt-btn[aria-pressed="true"]')`, "toggle persists");
    const kept = await evaluate(cdp, READ);
    ok(kept.listed.indexOf("model not recorded") >= 0, "unknown model listing lost across reload");

    /* Toggle off again: the note returns. */
    await evaluate(cdp, `document.querySelector(".tiles-unatt .unatt-btn").click()`);
    await waitFor(cdp, `!!document.querySelector('.tiles-unatt .unatt-btn[aria-pressed="false"]')`, "toggle off");
    const hidden = await evaluate(cdp, READ);
    ok(hidden.listed.indexOf("model not recorded") < 0, "unknown model listing lingers after Hide");
    eq(hidden.notes.length, 2, "notes missing after Hide");

    eq(errors.length, 0, `console errors: ${JSON.stringify(errors.slice(0, 5))}`);
    console.log("unattributed proof ok: default hides with note, totals unchanged, toggle shows, persists, no console errors");
    finish(0);
  } catch (e) {
    console.error(`unattributed_check: ${e.message}`);
    try {
      console.error(`server log tail:\n${fs.readFileSync(serverLog, "utf8").split("\n").slice(-15).join("\n")}`);
    } catch {
      /* no log */
    }
    finish(1);
  }
}

main();
