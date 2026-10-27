#!/usr/bin/env node
/* Plan load-race check: the page must survive its own request burst.
 *
 * A dashboard page load fires ~45 requests (scripts, CSS, APIs) in one
 * burst while snapshot folds keep the server busy. With too small a
 * listen backlog the kernel RST the excess connections, page scripts
 * died with ERR_CONNECTION_RESET, their globals (warmTabs, ADM,
 * openPlan, FIX) stayed undefined, and the inline script threw before
 * the plan body rendered.
 *
 * Stands up a TEMP dashboard server (temp state dir, one synthetic
 * workstream with a plan, port 8909, never the live :8765 or the real
 * data root), sustains a parallel static-asset burst against it for the
 * whole run, and loads /plan?ws=WS1 twenty times fresh over the DevTools
 * protocol (stdlib only: global fetch and WebSocket, child_process).
 * Asserts zero page exceptions, a defined warmTabs, and a rendered plan
 * body on every load.
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
const PORT = 8909;
const URL = `http://127.0.0.1:${PORT}`;
const LOADS = 20;
const TIMEOUT_MS = 30000;

// Best-effort handles for the crash path below: every spawned process
// is killed by handle (never pkill) even when main throws mid-run.
const live = { chrome: null, server: null, pid: null, tmp: null, keep: false };

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

function findChrome() {
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) return process.env.CHROME_PATH;
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  const candidates = [
    mac,
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ];
  for (const candidate of candidates) {
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      return candidate;
    } catch {
      /* next */
    }
  }
  return null;
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
    ws.addEventListener("error", () => {
      if (!settled) reject(new Error("debugger socket error"));
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
  if (!result || result.exceptionDetails) {
    throw new Error(`page evaluate failed: ${JSON.stringify(result && result.exceptionDetails).slice(0, 200)}`);
  }
  return result.result ? result.result.value : undefined;
}

async function main() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "plan-load-race-"));
  const wsDir = path.join(tmp, "ws");
  const stateDir = path.join(tmp, "state");
  const homeDir = path.join(tmp, "home");
  fs.mkdirSync(wsDir, { recursive: true });
  fs.mkdirSync(stateDir, { recursive: true });
  fs.mkdirSync(homeDir, { recursive: true });
  const keep = process.argv.includes("--keep");
  const env = {
    ...process.env, AGENT_DASH_IGNORE_LOCAL: "1", AGENT_DASH_STATE_DIR: stateDir,
    AGENT_DASH_WORKSTREAMS: wsDir, AGENT_DASH_PORT: String(PORT), HOME: homeDir,
  };
  let failures = 0;
  const fail = (name, detail = "") => {
    failures++;
    console.log(`FAIL: ${name}${detail ? ` -- ${detail}` : ""}`);
  };
  const ok = (name) => console.log(`ok: ${name}`);
  const cli = (...args) => {
    const r = spawnSync("python3", ["agent_dash.py", ...args], { cwd: ROOT, env, encoding: "utf8" });
    if (r.status !== 0) {
      console.error(`cli failed: agent_dash.py ${args.join(" ")}\n${r.stdout}\n${r.stderr}`);
      process.exit(1);
    }
    return r.stdout;
  };

  live.keep = keep;
  live.tmp = tmp;
  cli("ws", "new", "WS1", "Race WS1");
  const server = spawn("python3", ["agent_dash.py", "run"], { cwd: ROOT, env, stdio: "ignore" });
  live.server = server;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let pid = null;
  var chrome = null;
  const finish = async (code) => {
    try {
      if (chrome) chrome.kill();
    } catch {
      /* ignore */
    }
    try {
      if (pid) process.kill(pid);
    } catch {
      /* ignore */
    }
    await sleep(1200);
    try {
      if (pid) process.kill(pid, 9);
    } catch {
      /* ignore */
    }
    try {
      server.kill();
    } catch {
      /* ignore */
    }
    if (!keep) fs.rmSync(tmp, { recursive: true, force: true });
    console.log(code === 0 && failures === 0 ? "plan_load_race_check: PASS" : `plan_load_race_check: FAIL (${failures})`);
    process.exit(code === 0 && failures === 0 ? 0 : 1);
  };
  process.on("SIGINT", () => finish(1));

  for (let i = 0; i < 100; i++) {
    try {
      const h = await (await fetch(`${URL}/api/health`)).json();
      if (h.ok !== undefined) break;
    } catch {
      /* not up yet */
    }
    await sleep(300);
  }
  try {
    pid = Number(fs.readFileSync(path.join(stateDir, "agent-dash.pid"), "utf8").trim().split(/\s+/)[0]);
  } catch {
    /* unreadable */
  }
  live.pid = pid;
  if (!pid) {
    console.error("plan_load_race_check: temp server never came up (is :8909 already taken?)");
    await finish(1);
    return;
  }

  const chromeBin = findChrome();
  if (!chromeBin) {
    console.error("SKIP: no Chrome");
    process.exit(2);
  }
  const dbgPort = await freePort();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "plan-load-race-prof-"));
  chrome = spawn(chromeBin,
    ["--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-extensions",
      `--user-data-dir=${profile}`, `--remote-debugging-port=${dbgPort}`, "about:blank"],
    { stdio: ["ignore", "ignore", "pipe"] });
  live.chrome = chrome;
  for (let i = 0; i < 60; i++) {
    try {
      await (await fetch(`http://127.0.0.1:${dbgPort}/json/version`)).json();
      break;
    } catch {
      await sleep(200);
    }
  }

  // One bounded burst wave per load, fired alongside the page's own
  // burst: without contention an idle temp server rarely stalls its
  // accept loop, and the check would pass even with the backlog bug.
  // With it the pre-fix server drops connections on most loads. Bounded
  // (60 requests per load) so the check itself cannot exhaust the
  // ephemeral port range the way an unbounded hot loop does.
  const burstWave = async () => {
    try {
      await Promise.all(Array.from({ length: 60 },
        () => fetch(`${URL}/static/core/api.js`).then((r) => r.arrayBuffer()).catch(() => null)));
    } catch {
      /* the burst itself never fails the check */
    }
  };

  let badLoads = 0;
  for (let n = 1; n <= LOADS; n++) {
    const created = await (await fetch(`http://127.0.0.1:${dbgPort}/json/new?about:blank`, { method: "PUT" })).json();
    const exceptions = [];
    const hardFails = [];
    const pendingIds = new Set();
    const reqUrl = new Map();
    const cdp = await connectDebugger(created.webSocketDebuggerUrl, (method, params) => {
      if (method === "Runtime.exceptionThrown") {
        exceptions.push(params.exceptionDetails || {});
      } else if (method === "Network.requestWillBeSent") {
        if (!params.requestId) return;
        reqUrl.set(params.requestId, (params.request || {}).url);
        if (!((params.request || {}).url || "").includes("/api/stream")) pendingIds.add(params.requestId);
      } else if (method === "Network.responseReceived") {
        if ((params.response || {}).url && params.response.url.includes("/api/stream")) pendingIds.delete(params.requestId);
      } else if (method === "Network.loadingFinished") {
        pendingIds.delete(params.requestId);
      } else if (method === "Network.loadingFailed") {
        if (!params.canceled) hardFails.push(`${params.errorText} ${(reqUrl.get(params.requestId) || "").slice(0, 120)}`);
        pendingIds.delete(params.requestId);
      }
    });
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Network.enable");
    await cdp.send("Network.clearBrowserCache");
    const wave = burstWave();
    await cdp.send("Page.navigate", { url: `${URL}/plan?ws=WS1` });
    await wave;
    const deadline = Date.now() + TIMEOUT_MS;
    let calmSince = 0;
    for (;;) {
      let state = null;
      try {
        state = await evaluate(cdp, "document.readyState");
      } catch {
        state = null;
      }
      if (state === "complete" && pendingIds.size === 0) {
        if (!calmSince) calmSince = Date.now();
        if (Date.now() - calmSince > 1500) break;
      } else {
        calmSince = 0;
      }
      if (Date.now() > deadline) break;
      await sleep(300);
    }
    await sleep(1000);
    let page = null;
    try {
      page = await evaluate(cdp, `({warmTabs: (typeof warmTabs),
        planKids: (document.getElementById('plan-body') || {}).childElementCount ?? -1})`);
    } catch (e) {
      page = { evalError: String((e && e.message) || e) };
    }
    const problems = [
      ...exceptions.map((e) => `exception: ${((e.exception || {}).description || e.text || "").slice(0, 200)}`),
      ...hardFails.map((f) => `netfail: ${f}`),
    ];
    if (page.warmTabs !== "function") problems.push(`warmTabs is ${page.warmTabs}`);
    if (!(page.planKids > 0)) problems.push(`plan body has ${page.planKids} children`);
    if (problems.length) {
      badLoads++;
      fail(`load ${n}/${LOADS} renders the plan`, problems.slice(0, 3).join(" | "));
    }
    cdp.close();
    await fetch(`http://127.0.0.1:${dbgPort}/json/close/${created.id}`, { method: "PUT" }).catch(() => {});
    await sleep(400);
  }
  if (badLoads === 0) ok(`all ${LOADS} fresh loads render with zero page exceptions`);
  await finish(failures ? 1 : 0);
}

main().catch(async (error) => {
  console.error(`plan_load_race_check failed: ${(error && error.message) || error}`);
  try {
    if (live.chrome) live.chrome.kill();
  } catch {
    /* ignore */
  }
  try {
    if (live.pid) process.kill(live.pid);
  } catch {
    /* ignore */
  }
  try {
    if (live.server) live.server.kill();
  } catch {
    /* ignore */
  }
  await new Promise((r) => setTimeout(r, 1200));
  try {
    if (live.pid) process.kill(live.pid, 9);
  } catch {
    /* ignore */
  }
  if (live.tmp && !live.keep) fs.rmSync(live.tmp, { recursive: true, force: true });
  process.exit(1);
});
