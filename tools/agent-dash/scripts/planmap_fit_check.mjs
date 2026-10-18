#!/usr/bin/env node
/* Plan map fit check: the "<WS> plan progress" card fits the page width.
 *
 * Stands up a TEMP dashboard server (temp state dir, one synthetic WS9
 * bundle, port 8912, never the live :8765 or the real data root) seeded
 * with a wide plan (8 sections, 64 chips with long names, section and
 * task dependencies, mixed statuses, a few live agents plus unmapped
 * ones), then drives the Execution view over the DevTools protocol
 * (stdlib only: global fetch and WebSocket, child_process, no npm
 * installs) and asserts at 1440/1024/768/390:
 *   - documentElement.scrollWidth <= innerWidth (no sideways page scroll);
 *   - the #exec-progress card's right edge stays inside the viewport;
 *   - the dag either fits its well or scrolls inside it (overflow-x
 *     auto/scroll), never pushing the page;
 *   - the legend row and the strips stay inside the viewport;
 *   - after a 1440 -> 800 -> 1440 resize sequence the card width follows
 *     within 2 px and the tile/chip render counts settle (no growth,
 *     no ResizeObserver-style feedback loop);
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
const PORT = 8912;
const URL = `http://127.0.0.1:${PORT}`;
const TIMEOUT_MS = 30000;
const WIDTHS = [1440, 1024, 768, 390];

function args(argv) {
  const out = { chrome: null };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--chrome" && argv[i + 1]) out.chrome = argv[++i];
    else if (flag === "-h" || flag === "--help") {
      console.log("usage: planmap_fit_check.mjs [--chrome PATH]");
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
    console.error(`planmap_fit_check: chrome not found at ${explicit}`);
    process.exit(2);
  }
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (process.platform === "darwin" && fs.existsSync(mac)) return mac;
  const found = which(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]);
  if (found) return found;
  console.error("planmap_fit_check: no Chrome binary found (tried CHROME_PATH, the default install path and PATH)");
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
live_tasks = ["S1-rollout-lane-depth-audit-1", "S2-quota-lane-depth-audit-2", "S4-ledger-lane-depth-audit-4"]
for i, task in enumerate(live_tasks):
    seed.upsert_run({
        "run_id": "fit-check:live-%d" % i,
        "source": "muse",
        "tier": "muse",
        "model": "muse-1",
        "label": "ws9-%s-run%d" % (task.lower(), i),
        "status": "running",
        "started_at": now - 300 - i * 60,
        "elapsed_s": 300 + i * 60,
        "out_bytes": 100 + i,
        "out_growing": False,
        "tokens": {"input": 100 + i, "output": 50, "cache_read": 0, "cache_write": 0},
    })
for i in range(2):
    seed.upsert_run({
        "run_id": "fit-check:lead-%d" % i,
        "source": "muse",
        "tier": "muse",
        "model": "muse-1",
        "label": "ws9-lead-session-no-task-%d" % i,
        "status": "running",
        "started_at": now - 120,
        "elapsed_s": 120,
        "out_bytes": 10,
        "out_growing": False,
        "tokens": {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0},
    })
seed.close()
store = Store(Path(state) / "dash.sqlite")
srv = DashServer(store, Registry([]), "127.0.0.1", port, stream_interval=2, cfg=default_cfg())
srv.serve_forever(poll_interval=0.05)
`;

/* Eight sections over five columns, 64 chips with long names, chained
 * section edges and in-section task chains, mixed statuses. Long section
 * titles stress the tile-waits microcopy; long task titles stress tiles. */
const SECTIONS = ["North", "East", "South", "West", "Ridge", "Harbor", "Meadow", "Summit"];
const STATUSES = ["done", "done", "wip", "pr", "prog", "todo", "todo", "deferred"];

function writeBundle(wsDir) {
  const ws9 = path.join(wsDir, "WS9");
  fs.mkdirSync(ws9, { recursive: true });
  fs.writeFileSync(path.join(ws9, "bundle.json"), JSON.stringify({ id: "WS9", title: "Fit", kind: "workstream" }));
  const groups = SECTIONS.map((sec, gi) => {
    const tasks = [];
    for (let k = 0; k < 8; k++) {
      const id = `S${gi + 1}-${["rollout", "quota", "cache", "ledger", "switch", "bridge", "tunnel", "gate"][k]}-lane-depth-audit-${k + 1}`;
      tasks.push({
        id,
        title: `${sec} ${["rollout", "quota", "cache", "ledger", "switch", "bridge", "tunnel", "gate"][k]} lane depth audit number ${k + 1} with a long descriptive tail`,
        status: STATUSES[(gi + k) % STATUSES.length],
        deps: k > 0 ? [`S${gi + 1}-${["rollout", "quota", "cache", "ledger", "switch", "bridge", "tunnel", "gate"][k - 1]}-lane-depth-audit-${k}`] : [],
      });
    }
    return { id: `g-${gi + 1}`, col: gi % 5, title: `${sec} section with a deliberately long section title for wrap stress`, tasks };
  });
  const edges = SECTIONS.slice(1).map((_, gi) => [`g-${gi}`, `g-${gi + 1}`]);
  fs.writeFileSync(path.join(ws9, "plan.json"), JSON.stringify({
    workstream: "WS9",
    dag: { cols: ["Alpha column", "Beta column", "Gamma column", "Delta column", "Epsilon column"], groups, edges },
    narrative: { phases: [] },
    trains: [{ id: "t1", title: "First merge train with a long title", branch: "train/t1", status: "assembling", members: ["S1-rollout-lane-depth-audit-1", "S2-rollout-lane-depth-audit-1"] }],
  }));
}

const checks = [];
function check(name, fn) {
  checks.push([name, fn]);
}
function fail(message) {
  throw new Error(message);
}

/* Geometry snapshot of the plan card and its map parts. */
const FIT_STATE = `(function(){
  var card = document.querySelector('#exec-progress');
  if (!card) return null;
  function rect(e) {
    if (!e) return null;
    var b = e.getBoundingClientRect();
    return { left: Math.round(b.left * 10) / 10, right: Math.round(b.right * 10) / 10,
      w: Math.round(b.width * 10) / 10, sw: e.scrollWidth, cw: e.clientWidth,
      ox: getComputedStyle(e).overflowX };
  }
  var dag = card.querySelector('.dag'), scr = card.querySelector('.dag-scroll');
  return {
    vw: window.innerWidth,
    docSW: document.documentElement.scrollWidth,
    card: rect(card),
    dag: rect(dag),
    dagScroll: rect(scr),
    legend: rect(card.querySelector('.map-legend')),
    strips: ['.working-now', '.unassigned', '.trlegend', '.alegend', '.phases-line', '.pbig'].map(function (s) {
      return { sel: s, r: rect(card.querySelector(s)) };
    }),
    dnodes: card.querySelectorAll('.dnode,.slim-tile').length,
    chips: card.querySelectorAll('.chip').length,
    fitted: scr ? scr.className : '',
    headline: (card.querySelector('.pbig-txt') || {}).textContent || ''
  };
})()`;

async function setWidth(cdp, w) {
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: w, height: 1400, deviceScaleFactor: 1, mobile: false,
  });
  await new Promise((r) => setTimeout(r, 1200));
}

function assertFit(st, where) {
  if (!st) fail(`${where}: no #exec-progress card`);
  if (!(st.docSW <= st.vw)) fail(`${where}: page scrolls sideways (scrollWidth ${st.docSW} > viewport ${st.vw})`);
  if (!(st.card.right <= st.vw + 0.5)) fail(`${where}: card right edge ${st.card.right} past viewport ${st.vw}`);
  if (!(st.card.left >= -0.5)) fail(`${where}: card left edge ${st.card.left} off viewport`);
  const scr = st.dagScroll;
  if (scr && scr.sw > scr.cw + 1 && scr.ox !== "auto" && scr.ox !== "scroll") {
    fail(`${where}: dag overflows its well (${scr.sw} > ${scr.cw}) with overflow-x ${scr.ox}: nothing scrolls it internally`);
  }
  if (st.legend && !(st.legend.right <= st.vw + 0.5)) fail(`${where}: legend row past viewport (${st.legend.right} > ${st.vw})`);
  for (const s of st.strips) {
    if (s.r && !(s.r.right <= st.vw + 0.5)) fail(`${where}: strip ${s.sel} past viewport (${s.r.right} > ${st.vw})`);
  }
  if (!/\d+ of \d+ tasks done/.test(st.headline)) fail(`${where}: headline lost its counts wording: ${JSON.stringify(st.headline.slice(0, 80))}`);
  if (/\bphases?\b/i.test(st.headline)) fail(`${where}: headline names phases: ${JSON.stringify(st.headline.slice(0, 80))}`);
}

async function main() {
  const opts = args(process.argv.slice(2));
  const chrome = findChrome(opts.chrome);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "dash-planmap-"));
  const stateDir = path.join(tmp, "state");
  const wsDir = path.join(tmp, "workstreams");
  fs.mkdirSync(stateDir, { recursive: true });
  fs.mkdirSync(wsDir, { recursive: true });
  writeBundle(wsDir);
  const serverPy = path.join(tmp, "planmap_server.py");
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
        const plan = await waitForJson(`${URL}/api/plan?ws=WS9`, TIMEOUT_MS);
        const groups = (plan && plan.groups) || [];
        const tasks = groups.reduce((n, g) => n + ((g.tasks) || []).length, 0);
        if (groups.length !== 8 || tasks < 60) {
          throw new Error(`seeded 8 sections / 60+ chips but the API shows ${groups.length} / ${tasks}`);
        }
        if (!plan.layout) throw new Error("the synthetic plan carries no server layout (fitted path untested)");
        clearTimeout(t);
        resolve();
      })().catch((e) => {
        clearTimeout(t);
        reject(e);
      });
    });

    const dbgPort = await freePort();
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dash-planmap-prof-"));
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

    check("wide plan fits at every width", async () => {
      await cdp.send("Page.navigate", { url: `${URL}/?view=exec&ws=WS9` });
      await waitForLoad(cdp);
      const deadline = Date.now() + TIMEOUT_MS;
      for (;;) {
        const n = await evaluate(cdp, "document.querySelectorAll('#exec-progress .dnode,.slim-tile').length");
        if (n >= 8) break;
        if (Date.now() > deadline) fail(`only ${n} tiles rendered, want 8 sections`);
        await new Promise((r) => setTimeout(r, 400));
      }
      for (const w of WIDTHS) {
        await setWidth(cdp, w);
        assertFit(await evaluate(cdp, `(${FIT_STATE})`), `width ${w}`);
      }
      await cdp.send("Emulation.clearDeviceMetricsOverride");
    });

    check("resize sequence reflows with no page scroll and settled counts", async () => {
      await setWidth(cdp, 1440);
      const before = await evaluate(cdp, `(${FIT_STATE})`);
      assertFit(before, "resize start 1440");
      await setWidth(cdp, 800);
      const mid = await evaluate(cdp, `(${FIT_STATE})`);
      assertFit(mid, "resize 800");
      await setWidth(cdp, 1440);
      const after = await evaluate(cdp, `(${FIT_STATE})`);
      assertFit(after, "resize back 1440");
      if (Math.abs(after.card.w - before.card.w) > 2) {
        fail(`card width did not follow the resize back (${before.card.w} -> ${after.card.w})`);
      }
      await new Promise((r) => setTimeout(r, 2000));
      const settled = await evaluate(cdp, `(${FIT_STATE})`);
      if (settled.dnodes !== after.dnodes || settled.chips !== after.chips) {
        fail(`render counts grew while idle (tiles ${after.dnodes} -> ${settled.dnodes}, chips ${after.chips} -> ${settled.chips})`);
      }
      await cdp.send("Emulation.clearDeviceMetricsOverride");
    });

    let failed = 0;
    for (const [name, fn] of checks) {
      try {
        await fn();
        console.log(`ok: ${name}`);
      } catch (e) {
        failed += 1;
        console.error(`FAIL: ${name}: ${e.message}`);
      }
    }
    if (errors.length) {
      failed += 1;
      console.error(`FAIL: console errors on the page:\n  ${errors.join("\n  ")}`);
    }
    finish(failed ? 1 : 0);
  } catch (e) {
    console.error(`FAIL: ${e.message}`);
    finish(1);
  }
}

main();
