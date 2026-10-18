#!/usr/bin/env node
/* Page settle check: the dashboard must stop re-rendering on its own.
///
/// Drives the dashboard in headless Chrome over the DevTools protocol
/// (stdlib only: global fetch and WebSocket, child_process, no npm
/// installs). For the exec overview (?view=exec&ws=<ws>) and the project
/// home (?view=project) it waits for the first render, lets the initial
/// burst (tab list, snapshot, one plan-sum load) through, then samples
/// the render counter (window.__adRenders, incremented once per drawMain)
/// every 250 ms for 3 s: a tight loop raises it in nearly every poll,
/// while a healthy live page only re-renders on server ticks (about one
/// render per poll cycle, seconds apart). It then requires the idle
/// render count over the following 10 s to stay within a small bound.
///
/// A tiny or brand-new dashboard (a few bundles, no plans yet) used to
/// re-render forever: drawMain re-armed loadPlanSums on every render while
/// planSum stayed empty, and each empty load deleted the program section
/// and drew again. That wedge shows up here as a render count that never
/// holds still.
///
/// Exit 0 when every view settles, 1 with a table on any failure, 2 when
/// Chrome is missing (the agent_dash.py wrapper reports a skip then).
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

const COMBO_TIMEOUT_MS = 30000;
const GRACE_MS = 2000;
const STABLE_MS = 3000;
const IDLE_MS = 10000;
const IDLE_MAX_RENDERS = 4;
const POLL_MS = 250;

function args(argv) {
  const out = { url: "http://127.0.0.1:8765", chrome: null, ws: "all", project: true };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--url" && argv[i + 1]) out.url = argv[++i].replace(/\/+$/, "");
    else if (flag === "--chrome" && argv[i + 1]) out.chrome = argv[++i];
    else if (flag === "--ws" && argv[i + 1]) out.ws = argv[++i];
    else if (flag === "--no-project") out.project = false;
    else if (flag === "-h" || flag === "--help") {
      console.log("usage: page_settle_check.mjs [--url URL] [--chrome PATH] [--ws WS] [--no-project]");
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
  const suffixes = process.platform === "win32" ? [".exe", ".cmd", ""] : [""];
  for (const dir of dirs) {
    for (const name of names) {
      for (const suffix of suffixes) {
        const candidate = path.join(dir, name + suffix);
        try {
          fs.accessSync(candidate, fs.constants.X_OK);
          return candidate;
        } catch {
          /* next */
        }
      }
    }
  }
  return null;
}

function findChrome(explicit) {
  if (explicit) {
    if (fs.existsSync(explicit)) return explicit;
    console.error(`page_settle_check: chrome not found at ${explicit}`);
    process.exit(2);
  }
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (process.platform === "darwin" && fs.existsSync(mac)) return mac;
  const found = which(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]);
  if (found) return found;
  console.error("page_settle_check: no Chrome binary found (tried CHROME_PATH, the default install path and PATH)");
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

function connectDebugger(wsUrl) {
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
  const deadline = Date.now() + COMBO_TIMEOUT_MS;
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

async function renders(cdp) {
  try {
    return (await evaluate(cdp, "window.__adRenders || 0")) || 0;
  } catch {
    return null;
  }
}

async function checkView(cdp, url, label) {
  const row = { view: label, first: null, stable: null, idle: null, status: "OK", detail: "" };
  await cdp.send("Page.navigate", { url });
  await waitForLoad(cdp);
  {
    const deadline = Date.now() + COMBO_TIMEOUT_MS;
    for (;;) {
      const n = await renders(cdp);
      if (n !== null && n > 0) {
        row.first = n;
        break;
      }
      if (Date.now() > deadline) {
        row.status = "NO-RENDER";
        row.detail = "drawMain never ran (no __adRenders)";
        return row;
      }
      await new Promise((r) => setTimeout(r, POLL_MS));
    }
  }
  await new Promise((r) => setTimeout(r, GRACE_MS));
  {
    // Sampled stability: a tight re-render loop raises the count in nearly
    // every 250 ms poll, while a healthy live page only re-renders on
    // server ticks (about one render per poll cycle, seconds apart).
    const samples = [];
    for (let i = 0; i < STABLE_MS / POLL_MS; i++) {
      await new Promise((r) => setTimeout(r, POLL_MS));
      const n = await renders(cdp);
      if (n === null) {
        row.status = "ERROR";
        row.detail = "render count unreadable during the stability window";
        return row;
      }
      samples.push(n);
    }
    let hot = 0;
    for (let i = 1; i < samples.length; i++) if (samples[i] > samples[i - 1]) hot++;
    const delta = samples[samples.length - 1] - samples[0];
    row.stable = `${samples[0]} -> ${samples[samples.length - 1]} (+${delta})`;
    if (hot >= 4 || delta > 4) {
      row.status = "LOOP";
      row.detail = `render count kept climbing (${samples[0]} -> ${samples[samples.length - 1]} with ${hot} rising polls over ${STABLE_MS / 1000} s): suspect a re-render loop`;
      return row;
    }
  }
  {
    const before = await renders(cdp);
    await new Promise((r) => setTimeout(r, IDLE_MS));
    const after = await renders(cdp);
    row.idle = `${before} -> ${after}`;
    if (before === null || after === null) {
      row.status = "ERROR";
      row.detail = "render count unreadable during the idle window";
      return row;
    }
    if (after - before > IDLE_MAX_RENDERS) {
      row.status = "BUSY-IDLE";
      row.detail = `${after - before} renders over ${IDLE_MS / 1000} idle s (bound ${IDLE_MAX_RENDERS})`;
      return row;
    }
  }
  return row;
}

async function main() {
  const opts = args(process.argv.slice(2));
  const chrome = findChrome(opts.chrome);
  const port = await freePort();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dash-settle-check-"));
  const child = spawn(
    chrome,
    [
      "--headless",
      "--no-sandbox",
      "--disable-gpu",
      "--no-first-run",
      "--disable-extensions",
      `--user-data-dir=${profile}`,
      `--remote-debugging-port=${port}`,
      "about:blank",
    ],
    { stdio: ["ignore", "ignore", "pipe"] },
  );
  let cdp = null;
  const finish = (code) => {
    try {
      if (cdp) cdp.close();
    } catch {
      /* ignore */
    }
    try {
      child.kill();
    } catch {
      /* ignore */
    }
    try {
      fs.rmSync(profile, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
    process.exit(code);
  };
  process.on("SIGINT", () => finish(1));
  try {
    await waitForJson(`http://127.0.0.1:${port}/json/version`, 20000);
    const created = await (
      await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })
    ).json();
    cdp = await connectDebugger(created.webSocketDebuggerUrl);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");

    const targets = [{ url: `${opts.url}/?view=exec&ws=${encodeURIComponent(opts.ws)}`, label: `exec&ws=${opts.ws}` }];
    if (opts.project) targets.push({ url: `${opts.url}/?view=project`, label: "project" });
    const rows = [];
    let failures = 0;
    for (const target of targets) {
      try {
        rows.push(await checkView(cdp, target.url, target.label));
      } catch (error) {
        rows.push({ view: target.label, first: null, stable: null, idle: null, status: "ERROR", detail: String((error && error.message) || error) });
      }
    }
    for (const row of rows) if (row.status !== "OK") failures++;

    const width = Math.max(10, ...rows.map((r) => String(r.view).length));
    console.log(`settle check ${opts.url} (${GRACE_MS / 1000}s grace, ${STABLE_MS / 1000}s stability, ${IDLE_MS / 1000}s idle)`);
    console.log(
      `${"view".padEnd(width)} ${"first".padStart(7)} ${"stable(3s)".padStart(14)} ${"idle(10s)".padStart(14)} ${"status".padStart(9)}`,
    );
    for (const row of rows) {
      console.log(
        `${String(row.view).padEnd(width)} ${String(row.first ?? "-").padStart(7)} ${String(row.stable ?? "-").padStart(14)} ${String(row.idle ?? "-").padStart(14)} ${row.status.padStart(9)}`,
      );
      if (row.detail) console.log(`  - ${row.detail}`);
    }
    if (failures) {
      console.log(`${failures} view(s) never settled`);
      finish(1);
      return;
    }
    console.log("ok: every view settled and stayed idle");
    finish(0);
  } catch (error) {
    console.error(`page_settle_check failed: ${(error && error.message) || error}`);
    finish(1);
  }
}

main();
