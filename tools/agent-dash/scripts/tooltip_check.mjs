#!/usr/bin/env node
/* Hover-text compactness check for the agent dashboard.
 *
 * Serves a TEMP copy of live-shaped data on 127.0.0.1:8918 (copies only,
 * never the live files), then drives headless Chrome over the DevTools
 * protocol (stdlib only: global fetch and WebSocket, child_process, fs)
 * across the overall Execution view, the WS9 tab, Project home and the
 * Current table. Every title attribute, aria-label and dispatched hover
 * tooltip must be compact: no 5+ digit run and no comma-separated
 * thousands. Exact figures stay in data-* attributes and the details
 * drawer, which this check never reads.
 *
 * Temp server layout (all under one mkdtemp dir, removed afterwards):
 *   - ~/.local/share/agent-dash/dash.sqlite* copied to AGENT_DASH_STATE_DIR
 *   - the live bundles copied to AGENT_DASH_WORKSTREAMS
 *   - a fresh AGENT_DASH_DATA_ROOT, empty source roots, AGENT_DASH_PORT=8918
 * The server is stopped by the PID from its own temp pidfile (never
 * pkill, never agent_dash.py stop, never the live :8765 server).
 *
 * Exit 0 when every hover string is compact, 1 with a table of offenders,
 * 2 when Chrome is missing.
 *
 * usage: tooltip_check.mjs [--repo DIR] [--url URL] [--no-serve] [--chrome PATH]
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

const PORT = 8918;
const LIVE_STATE = path.join(os.homedir(), ".local", "share", "agent-dash");
const VIEWS = [
  ["exec-overall", "/"],
  ["ws9", "/?ws=WS9"],
  ["project", "/?view=project"],
  ["current", "/?view=current"],
];
const HOVER_SELECTORS = [
  ".tile", ".lentry", ".ltotal", ".chip", ".colbar", ".pbig-txt",
  ".prog-bar", ".bar", ".wsc-plan", ".ph-seg", ".mg-phase-seg",
  ".tl-bar", ".tl-dense-hit", ".tl-lane-hit", ".dep-node", ".dep-edge",
  ".conc-strip", ".ph-name", ".ws-row", ".pg-num", "[data-t]",
  "circle", ".more-btn", ".slim-tile", ".fanin", ".att",
];
const RAW_NUMBER = /(\d{5,}|\d,\d{3})/;

function args(argv) {
  const out = { repo: null, url: null, serve: true, chrome: null };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--repo" && argv[i + 1]) out.repo = argv[++i];
    else if (flag === "--url" && argv[i + 1]) out.url = argv[++i].replace(/\/+$/, "");
    else if (flag === "--chrome" && argv[i + 1]) out.chrome = argv[++i];
    else if (flag === "--no-serve") out.serve = false;
    else if (flag === "-h" || flag === "--help") {
      console.log("usage: tooltip_check.mjs [--repo DIR] [--url URL] [--no-serve] [--chrome PATH]");
      process.exit(0);
    } else {
      console.error(`unknown flag ${flag}`);
      process.exit(1);
    }
  }
  if (!out.repo) out.repo = path.resolve(path.dirname(process.argv[1]), "..");
  if (!out.url) out.url = `http://127.0.0.1:${PORT}`;
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
    console.error(`tooltip_check: chrome not found at ${explicit}`);
    process.exit(2);
  }
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) return process.env.CHROME_PATH;
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (process.platform === "darwin" && fs.existsSync(mac)) return mac;
  const found = which(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]);
  if (found) return found;
  console.error("tooltip_check: no Chrome binary found (tried CHROME_PATH, the default install path and PATH)");
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

async function waitForPortClosed(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const closed = await new Promise((resolve) => {
      const sock = net.connect(port, "127.0.0.1");
      sock.once("connect", () => { sock.destroy(); resolve(false); });
      sock.once("error", () => resolve(true));
    });
    if (closed) return;
    if (Date.now() > deadline) throw new Error(`port ${port} never closed`);
    await new Promise((r) => setTimeout(r, 200));
  }
}

function connectDebugger(wsUrl) {
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

async function evaluate(cdp, expression, arg) {
  const params = arg === undefined
    ? { expression, returnByValue: true }
    : { expression: `(${expression})(${JSON.stringify(arg)})`, returnByValue: true };
  const result = await cdp.send("Runtime.evaluate", params);
  if (!result || result.error) throw new Error(`cdp evaluate gave no result: ${JSON.stringify(result)}`);
  if (result.exceptionDetails) throw new Error(`page expression failed: ${JSON.stringify(result.exceptionDetails).slice(0, 400)}`);
  return result.result ? result.result.value : undefined;
}

async function waitForSettled(cdp, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  let prev = null;
  for (;;) {
    const n = await evaluate(cdp, "window.__adRenders || 0");
    if (prev !== null && n === prev) return;
    prev = n;
    if (Date.now() > deadline) throw new Error("page never settled (render counter keeps climbing)");
    await new Promise((r) => setTimeout(r, 800));
  }
}

/* Every hover-visible string on the current view: title attributes,
 * aria-labels, SVG <title> text, and the shared #tip tooltip after
 * dispatching mouseenter/mousemove over each hoverable mark. */
const COLLECT_EXPR = `(function (selectors) {
  const strings = [];
  document.querySelectorAll("[title]").forEach((n) => strings.push(["title", n.getAttribute("title") || ""]));
  document.querySelectorAll("[aria-label]").forEach((n) => strings.push(["aria-label", n.getAttribute("aria-label") || ""]));
  document.querySelectorAll("svg title").forEach((n) => strings.push(["svg-title", n.textContent || ""]));
  const tip = document.getElementById("tip");
  selectors.forEach((sel) => {
    document.querySelectorAll(sel).forEach((node, i) => {
      if (i >= 8) return;
      try {
        const r = node.getBoundingClientRect();
        const x = r.left + r.width / 2, y = r.top + r.height / 2;
        ["mouseenter", "mousemove"].forEach((t) => node.dispatchEvent(new MouseEvent(t, { bubbles: true, clientX: x, clientY: y })));
        if (tip && tip.style.display !== "none" && tip.textContent.trim()) strings.push(["tooltip", tip.textContent.trim()]);
        node.dispatchEvent(new MouseEvent("mouseleave", { bubbles: true }));
      } catch (e) { /* a mark without layout never tooltips */ }
    });
  });
  return strings;
})`;

async function checkView(cdp, base, viewPath) {
  await cdp.send("Page.navigate", { url: base + viewPath });
  await new Promise((r) => setTimeout(r, 1500));
  await waitForSettled(cdp);
  return await evaluate(cdp, COLLECT_EXPR, HOVER_SELECTORS);
}

async function runCheck(base, chrome) {
  const debugPort = await freePort();
  const userDir = fs.mkdtempSync(path.join(os.tmpdir(), "tip-chrome-"));
  const child = spawn(chrome, [
    "--headless=new", `--remote-debugging-port=${debugPort}`,
    "--no-first-run", "--no-default-browser-check", `--user-data-dir=${userDir}`,
    "--window-size=1440,900", "about:blank",
  ], { stdio: "ignore" });
  try {
    const deadline = Date.now() + 30000;
    let debuggerUrl = null;
    for (;;) {
      try {
        const res = await fetch(`http://127.0.0.1:${debugPort}/json/list`);
        if (res.ok) {
          const targets = await res.json();
          const page = targets.find((t) => t.type === "page" && t.webSocketDebuggerUrl) || targets.find((t) => t.webSocketDebuggerUrl);
          if (page) { debuggerUrl = page.webSocketDebuggerUrl; break; }
        }
      } catch { /* not up yet */ }
      if (Date.now() > deadline) throw new Error("chrome debugger never came up");
      await new Promise((r) => setTimeout(r, 200));
    }
    const cdp = await connectDebugger(debuggerUrl);
    const offenders = [];
    let checked = 0;
    for (const [name, viewPath] of VIEWS) {
      const strings = await checkView(cdp, base, viewPath);
      for (const [kind, text] of strings) {
        if (!text) continue;
        checked++;
        if (RAW_NUMBER.test(text)) offenders.push({ view: name, kind, text: text.slice(0, 160) });
      }
    }
    cdp.close();
    return { offenders, checked };
  } finally {
    try { child.kill("SIGTERM"); } catch { /* already gone */ }
    for (let i = 0; i < 5; i++) {
      try {
        fs.rmSync(userDir, { recursive: true, force: true });
        break;
      } catch { await new Promise((r) => setTimeout(r, 500)); }
    }
  }
}

/* A temp server over copies of live-shaped data. Copies only: the live
 * sqlite, bundles and sources are never opened for writing. */
async function startTempServer(repo) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "tip-dash-"));
  const stateDir = path.join(tmp, "state");
  const dataRoot = path.join(tmp, "data");
  const wsDir = path.join(tmp, "workstreams");
  fs.mkdirSync(stateDir, { recursive: true });
  fs.mkdirSync(dataRoot, { recursive: true });
  for (const entry of fs.readdirSync(LIVE_STATE)) {
    if (entry.startsWith("dash.sqlite")) fs.copyFileSync(path.join(LIVE_STATE, entry), path.join(stateDir, entry));
  }
  fs.cpSync(path.join(LIVE_STATE, "workstreams"), wsDir, { recursive: true });
  const emptyRoots = {};
  for (const name of ["src-claude-projects", "src-claude-sessions", "src-muse-sessions", "src-grok-sessions", "src-grok-log", "src-cursor-home"]) {
    const dir = path.join(tmp, name);
    fs.mkdirSync(dir, { recursive: true });
    emptyRoots[name] = dir;
  }
  const env = {
    ...process.env,
    AGENT_DASH_PORT: String(PORT),
    AGENT_DASH_STATE_DIR: stateDir,
    AGENT_DASH_DATA_ROOT: dataRoot,
    AGENT_DASH_WORKSTREAMS: wsDir,
    AGENT_DASH_IGNORE_LOCAL: "1",
    AGENT_DASH_CLAUDE_PROJECTS: emptyRoots["src-claude-projects"],
    AGENT_DASH_CLAUDE_SESSIONS: emptyRoots["src-claude-sessions"],
    AGENT_DASH_MUSE_SESSIONS: emptyRoots["src-muse-sessions"],
    AGENT_DASH_GROK_SESSIONS: emptyRoots["src-grok-sessions"],
    AGENT_DASH_GROK_LOG: path.join(emptyRoots["src-grok-log"], "unified.jsonl"),
    AGENT_DASH_CURSOR_HOME: emptyRoots["src-cursor-home"],
  };
  delete env.AGENT_DASH_CONFIG;
  delete env.AGENT_DASH_URL;
  const child = spawn("python3", ["agent_dash.py", "run"], { cwd: repo, env, stdio: "ignore" });
  const code = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("agent_dash.py run never exited")), 60000);
    child.once("exit", (c) => { clearTimeout(timer); resolve(c); });
    child.once("error", (e) => { clearTimeout(timer); reject(e); });
  });
  if (code !== 0) {
    fs.rmSync(tmp, { recursive: true, force: true });
    throw new Error(`agent_dash.py run exited with code ${code} (port ${PORT} may serve another tree; retry when free)`);
  }
  await waitForJson(`http://127.0.0.1:${PORT}/api/health`, 30000);
  let pid = 0;
  try {
    pid = Number(fs.readFileSync(path.join(stateDir, "agent-dash.pid"), "utf8").trim().split(/\s+/)[0]);
  } catch { /* handled below */ }
  if (!pid) {
    fs.rmSync(tmp, { recursive: true, force: true });
    throw new Error(`port ${PORT} answers but left no pidfile in the temp state dir: another tree serves it; retry when free`);
  }
  return { tmp, pid };
}

async function stopTempServer(server) {
  try { process.kill(server.pid, "SIGTERM"); } catch { /* already gone */ }
  try { await waitForPortClosed(PORT, 15000); }
  catch { try { process.kill(server.pid, "SIGKILL"); } catch { /* gone */ } }
  fs.rmSync(server.tmp, { recursive: true, force: true });
}

async function main() {
  const opts = args(process.argv.slice(2));
  const chrome = findChrome(opts.chrome);
  let server = null;
  try {
    if (opts.serve) server = await startTempServer(opts.repo);
    const { offenders, checked } = await runCheck(opts.url, chrome);
    const seen = new Set();
    const rows = offenders.filter((o) => {
      const key = `${o.view}|${o.kind}|${o.text}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    console.log(`tooltip_check: ${checked} hover strings across ${VIEWS.length} views, ${rows.length} raw-number offenders`);
    for (const o of rows.slice(0, 30)) console.log(`  [${o.view} ${o.kind}] ${o.text}`);
    if (rows.length > 30) console.log(`  ... and ${rows.length - 30} more`);
    process.exitCode = rows.length ? 1 : 0;
  } finally {
    if (server) await stopTempServer(server);
  }
}

await main();
