#!/usr/bin/env node
/* Model display-name check: every model id renders short, never stretches.
 *
 * Boots a TEMP server (port 8908, temp state dir, one synthetic WS19 with
 * runs carrying every live model id — never the owner's real bundles or
 * :8765), then drives the live Execution view in headless Chrome over the
 * DevTools protocol (stdlib only) and asserts, at 1400 and 390 wide:
 *   - every expected display name (DeepSeek V4.1 Flash, Claude Haiku 5.5,
 *     Beam 1.0, Laguna S 2.1, ...) appears on the page;
 *   - no .tier cell is wider than its column (the name truncates with an
 *     ellipsis; the raw id stays in the title tooltip) and no table
 *     scrolls sideways because of a model cell;
 *   - the page itself never scrolls sideways at either width;
 *   - at 390 px the harness word hides (.mhar-n display:none) while the
 *     model name survives; at 1400 px the harness word shows;
 *   - every Activity tokens legend entry matches a table cell string
 *     (same seriesName output, e.g. DeepSeek V4.1 Flash · Command Code);
 *   - zero console errors and zero page exceptions throughout.
 *
 * The server child is killed by PID on the way out (never pkill).
 * Usage: node scripts/model_names_check.mjs [--chrome PATH] [--keep]
 * Exit 0 on PASS, 1 on the first failed assertion, 2 when Chrome is missing.
 */
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.dirname(HERE);
const PORT = 8908;
const URL = `http://127.0.0.1:${PORT}`;
const TIMEOUT_MS = 30000;

const EXPECTED = [
  "Claude Haiku 5.5", "Claude Opus 5.5", "Claude Fable 5.1",
  "muse Spark 1.3", "Beam 1.0", "GPT-6 Luna",
  "DeepSeek V4.1 Flash", "DeepSeek V4 Pro",
  "MiniMax M3.1 Flash", "Ornith 1.5", "Laguna S 2.1", "Glyph Cluster",
  "Grok 4.7", "Composer 2.5", "GLM 5.3 Flash",
];

function args(argv) {
  const out = { chrome: null, keep: false };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--chrome" && argv[i + 1]) out.chrome = argv[++i];
    else if (flag === "--keep") out.keep = true;
    else if (flag === "-h" || flag === "--help") {
      console.log("usage: model_names_check.mjs [--chrome PATH] [--keep]");
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
    console.error(`model_names_check: chrome not found at ${explicit}`);
    process.exit(2);
  }
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (process.platform === "darwin" && fs.existsSync(mac)) return mac;
  const found = which(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]);
  if (found) return found;
  console.error("model_names_check: no Chrome binary found (tried CHROME_PATH, the default install path and PATH)");
  process.exit(2);
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "model-names-check-"));
const wsDir = path.join(tmp, "ws");
const stateDir = path.join(tmp, "state");
const homeDir = path.join(tmp, "home");
const emptyDir = path.join(tmp, "empty");
for (const d of [wsDir, stateDir, homeDir, emptyDir]) fs.mkdirSync(d, { recursive: true });

const env = { ...process.env, AGENT_DASH_IGNORE_LOCAL: "1", AGENT_DASH_STATE_DIR: stateDir,
  AGENT_DASH_WORKSTREAMS: wsDir, AGENT_DASH_DATA_ROOT: path.join(tmp, "root"),
  AGENT_DASH_PORT: String(PORT), HOME: homeDir,
  AGENT_DASH_CLAUDE_PROJECTS: emptyDir, AGENT_DASH_CLAUDE_SESSIONS: emptyDir,
  AGENT_DASH_CURSOR_HOME: emptyDir, AGENT_DASH_GROK_LOG: emptyDir,
  AGENT_DASH_GROK_SESSIONS: emptyDir, AGENT_DASH_MUSE_SESSIONS: emptyDir,
  AGENT_DASH_ISSUES_DIR: emptyDir, AGENT_DASH_PROJECTS: emptyDir };
let failures = 0;
function check(name, cond, detail = "") {
  console.log(`${cond ? "ok" : "FAIL"}  ${name}${detail ? " -- " + detail : ""}`);
  if (!cond) failures++;
}

/* One run per live model id, plus finished twins of four of them so both
   tables render the normalised names. Usage events ride along so the
   Activity tokens-per-hour legend lists the same series the tables show.
   The server runs with an empty source registry (the exec_layout_check
   pattern): no collector ticks, so no reconcile pass reaps the synthetic
   rows and no live laptop process leaks in as a real run. */
const SERVER_PY = `import os, sys, time
root, state, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
sys.path.insert(0, root)
os.environ.setdefault("AGENT_DASH_IGNORE_LOCAL", "1")
from pathlib import Path
from dash.store import Store
from dash.server import DashServer
from dash.sources import Registry
from agent_dash import default_cfg
live = [
    ("claude-haiku-5-5", "claude", "haiku"),
    ("claude-opus-5-5", "claude", "opus"),
    ("claude-fable-5-1", "claude", "fable"),
    ("muse-spark-1.3-contributor", "muse", "muse"),
    ("reflection/Beam-1.0-501B-A23B", "pi", "beam"),
    ("reflection/beam:free", "commandcode", "commandcode"),
    ("gpt-6-luna", "codex", "codex"),
    ("deepseek/deepseek-v4.1-flash", "commandcode", "commandcode"),
    ("deepseek/deepseek-v4-pro", "commandcode", "commandcode"),
    ("minimax/minimax-m3.1-flash:free", "commandcode", "commandcode"),
    ("ornith/ornith-1.5-122b:free", "commandcode", "commandcode"),
    ("poolside/laguna-s-2.1-free", "commandcode", "commandcode"),
    ("stealth/glyph-cluster:free", "commandcode", "commandcode"),
    ("grok-4.7[context=500k,reasoning_effort=high,fast=false]", "grok", "grok"),
    ("composer-2.5", "cursor", "cursor"),
    ("glm-5.3-flash", "glm", "glm-flash"),
]
now = time.time()
seed = Store(Path(state) / "dash.sqlite")
n = 0
for model, source, tier in live:
    n += 1
    seed.upsert_run({
        "run_id": "mn-run-%02d" % n, "source": source, "tier": tier,
        "model": model, "label": "ws19-mn-%02d" % n,
        "status": "running", "started_at": now - n * 90, "elapsed_s": n * 90,
        "out_bytes": 4096 + n, "out_growing": False,
        "tokens": {"input": 80000 + n * 1000, "output": 2000, "cache_read": 500, "cache_write": 100},
    })
    seed.add_usage({
        "tool": source, "session_id": "mn-sess-%02d" % n, "message_id": "mn-msg",
        "model": model, "ts": now - n * 90 + 30, "run_id": "mn-run-%02d" % n,
        "input": 80000 + n * 1000, "output": 2000, "cache_read": 500, "cache_write": 100,
    })
for i, (model, source, tier) in enumerate([live[7], live[0], live[3], live[4]]):
    seed.upsert_run({
        "run_id": "mn-done-%d" % i, "source": source, "tier": tier,
        "model": model, "label": "ws19-mn-done-%d" % i,
        "status": "done", "verdict": "PASS",
        "started_at": now - 9000 - i * 300, "ended_at": now - 3600 - i * 300,
        "elapsed_s": 5400, "out_bytes": 2048, "out_growing": False,
        "tokens": {"input": 40000, "output": 1000, "cache_read": 200, "cache_write": 50},
    })
    seed.add_usage({
        "tool": source, "session_id": "mn-dsess-%d" % i, "message_id": "mn-dmsg",
        "model": model, "ts": now - 3600 - i * 300 + 60, "run_id": "mn-done-%d" % i,
        "input": 40000, "output": 1000, "cache_read": 200, "cache_write": 50,
    })
seed.close()
store = Store(Path(state) / "dash.sqlite")
srv = DashServer(store, Registry([]), "127.0.0.1", port, stream_interval=2, cfg=default_cfg())
srv.serve_forever(poll_interval=0.05)
`;

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once("error", reject);
    s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}

async function waitForJson(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try { const r = await fetch(url); if (r.ok) return await r.json(); } catch {}
    if (Date.now() > deadline) throw new Error("timeout " + url);
    await new Promise((r) => setTimeout(r, 300));
  }
}

function connectDebugger(wsUrl, onEvent) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const pending = new Map(); let id = 0; let settled = false;
    ws.addEventListener("open", () => { settled = true; resolve({ send, close }); });
    ws.addEventListener("message", (ev) => {
      let msg; try { msg = JSON.parse(String(ev.data)); } catch { return; }
      if (msg.id != null && pending.has(msg.id)) {
        const e = pending.get(msg.id); pending.delete(msg.id);
        if (msg.error) e.reject(new Error(msg.error.message || JSON.stringify(msg.error)));
        else e.resolve(msg.result);
      } else if (msg.method && onEvent) {
        onEvent(msg.method, msg.params || {});
      }
    });
    ws.addEventListener("error", () => { if (!settled) reject(new Error("socket error")); });
    function send(method, params, timeoutMs = 30000) {
      const cur = ++id;
      return new Promise((res, rej) => {
        pending.set(cur, { resolve: res, reject: rej });
        ws.send(JSON.stringify({ id: cur, method, params: params || {} }));
        setTimeout(() => { if (pending.has(cur)) { pending.delete(cur); rej(new Error("cdp timeout " + method)); } }, timeoutMs);
      });
    }
    function close() { try { ws.close(); } catch {} }
  });
}

async function ev(cdp, expr) {
  const r = await cdp.send("Runtime.evaluate", { expression: expr, returnByValue: true });
  if (r.exceptionDetails) throw new Error("eval failed: " + JSON.stringify(r.exceptionDetails).slice(0, 200));
  return r.result ? r.result.value : undefined;
}

async function waitFor(cdp, expr, timeoutMs = 25000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const v = await ev(cdp, expr).catch(() => null);
    if (v) return v;
    if (Date.now() > deadline) return null;
    await new Promise((r) => setTimeout(r, 500));
  }
}

async function settle(cdp) {
  const deadline = Date.now() + 30000;
  for (;;) {
    const s = await ev(cdp, "window.__adSettled || ''").catch(() => null);
    if (s && String(s).length > 0) break;
    if (Date.now() > deadline) break;
    await new Promise((r) => setTimeout(r, 700));
  }
  await new Promise((r) => setTimeout(r, 1200));
}

/* Tier cells, legend strings and page overflow in one DOM pass. */
const MEASURE = `(() => {
  const tiers = Array.from(document.querySelectorAll(".tier")).map((t) => {
    const name = t.querySelector(".mmod-n");
    const har = t.querySelector(".mhar-n");
    const ncs = name ? getComputedStyle(name) : null;
    return {
      over: Math.round(t.scrollWidth - t.clientWidth),
      nameOver: name ? Math.round(name.scrollWidth - name.clientWidth) : 0,
      ellipsis: ncs ? ncs.textOverflow : "",
      title: t.title || "",
      text: (t.textContent || "").replace(/\\s+/g, " ").trim(),
      harVisible: har ? getComputedStyle(har).display !== "none" : null,
      hasFam: !!t.querySelector(".mfam-n"),
    };
  });
  /* Only the Tokens per hour strip: the progress strip shares the
     .tok-legend class with its own Done/Merged entries. */
  const legends = Array.from(document.querySelectorAll("#exec-tokens .tok-legend .lname"))
    .map((e) => (e.textContent || "").trim()).filter(Boolean);
  return { tiers, legends,
    bodyText: document.body.textContent || "",
    sideways: document.documentElement.scrollWidth - document.documentElement.clientWidth };
})()`;

async function main() {
  const opts = args(process.argv.slice(2));
  const chrome = findChrome(opts.chrome);
  const wsNew = spawnSync("python3", ["agent_dash.py", "ws", "new", "WS19", "Model display-name fixtures"],
    { cwd: ROOT, env, encoding: "utf8" });
  if (wsNew.status !== 0) {
    console.error("ws new failed\n" + wsNew.stdout + "\n" + wsNew.stderr);
    process.exit(1);
  }
  const serverPy = path.join(tmp, "model_server.py");
  fs.writeFileSync(serverPy, SERVER_PY);
  const serverLog = path.join(tmp, "server.log");
  const logFd = fs.openSync(serverLog, "w");
  const server = spawn("python3", [serverPy, ROOT, stateDir, String(PORT)], {
    cwd: ROOT,
    stdio: ["ignore", logFd, logFd],
    env: {
      ...process.env,
      AGENT_DASH_STATE_DIR: stateDir,
      AGENT_DASH_WORKSTREAMS: wsDir,
      AGENT_DASH_IGNORE_LOCAL: "1",
    },
  });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const serverLogTail = () => {
    try { return fs.readFileSync(serverLog, "utf8").split("\n").slice(-5).join("\n"); } catch { return ""; }
  };
  try {
    await new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error("temp server never came up (is :8908 already taken?)")), TIMEOUT_MS);
      server.once("exit", (code) => {
        clearTimeout(t);
        reject(new Error(`temp server exited early with code ${code}; ${serverLog}: ${serverLogTail()}`));
      });
      (async () => {
        for (;;) {
          try {
            const s = await (await fetch(`${URL}/api/snapshot?ws=all`)).json();
            if ((s.running || []).length === 16 && (s.recent || []).length === 4) break;
          } catch {}
          await sleep(300);
        }
        clearTimeout(t);
        resolve();
      })().catch((e) => { clearTimeout(t); reject(e); });
    });
    check("temp server answers on 8908 with 16 live + 4 finished agents", true);

    const dbgPort = await freePort();
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), "model-names-chrome-"));
    const ch = spawn(chrome, ["--headless", "--no-sandbox", "--disable-gpu", "--no-first-run",
      "--disable-extensions", `--user-data-dir=${profile}`, `--remote-debugging-port=${dbgPort}`,
      "--window-size=1400,1000", "about:blank"], { stdio: "ignore" });
    const finishChrome = () => { try { ch.kill(); } catch {} try { fs.rmSync(profile, { recursive: true, force: true }); } catch {} };
    const errors = [];
    try {
      await waitForJson(`http://127.0.0.1:${dbgPort}/json/version`, 20000);
      const created = await (await fetch(`http://127.0.0.1:${dbgPort}/json/new?about:blank`, { method: "PUT" })).json();
      const cdp = await connectDebugger(created.webSocketDebuggerUrl, (method, params) => {
        if (method === "Runtime.consoleAPICalled" && params.type === "error") {
          errors.push(`console.error: ${(params.args || []).map((a) => a.description || a.value).join(" ").slice(0, 200)}`);
        } else if (method === "Runtime.exceptionThrown") {
          errors.push(`page exception: ${JSON.stringify(params.exceptionDetails || {}).slice(0, 200)}`);
        }
      });
      await cdp.send("Page.enable");
      await cdp.send("Runtime.enable");
      for (const width of [1400, 390]) {
        const desktop = width === 1400;
        await cdp.send("Emulation.setDeviceMetricsOverride",
          desktop ? { width, height: 1000, deviceScaleFactor: 1, mobile: false }
                  : { width, height: 844, deviceScaleFactor: 1, mobile: true });
        for (const route of ["exec/all", "exec/WS19"]) {
          const [view, ws] = route.split("/");
          await cdp.send("Page.navigate", { url: `${URL}/?view=${view}&ws=${ws}&theme=dark` });
          await settle(cdp);
          await waitFor(cdp, "document.querySelector('#exec-running table')", 20000);
          /* The collapsed top-5 may hide seeded rows: expand Running now. */
          await ev(cdp, `(() => { const b = document.querySelector("#exec-running .runmore"); if (b && b.getAttribute("aria-expanded") === "false") b.click(); return true; })()`);
          await waitFor(cdp, `document.body.textContent.indexOf("Laguna S 2.1") >= 0`, 15000);
          const m = await ev(cdp, MEASURE);
          const tag = `@${width} ${route}`;
          console.log(`-- ${tag}: ${m.tiers.length} model cells, ${m.legends.length} legend entries, sideways ${m.sideways}px`);
          check(`${tag} renders model cells`, m.tiers.length > 0);
          for (const name of EXPECTED) {
            check(`${tag} shows "${name}"`, m.bodyText.indexOf(name) >= 0);
          }
          check(`${tag} drops the separate family word`, m.tiers.every((t) => !t.hasFam));
          const wide = m.tiers.find((t) => t.over > 2);
          check(`${tag} no .tier cell wider than its column`, !wide,
            wide ? `"${wide.text.slice(0, 40)}" over by ${wide.over}px` : "");
          check(`${tag} long names truncate with an ellipsis`,
            m.tiers.every((t) => t.nameOver <= 2 || t.ellipsis === "ellipsis"));
          const noTip = m.tiers.find((t) => !t.title);
          check(`${tag} raw ids stay in cell tooltips`, !noTip,
            noTip ? `"${noTip.text.slice(0, 40)}" has no title` : "");
          if (desktop) {
            check(`${tag} harness word shows on desktop`,
              m.tiers.some((t) => t.harVisible && t.text.indexOf("·") >= 0));
          } else {
            check(`${tag} harness word hides on phones`,
              m.tiers.length > 0 && m.tiers.every((t) => !t.harVisible));
          }
          /* Series legends use the same seriesName output as the tables:
             most match a cell exactly; a key without the recorded version
             (Haiku vs Claude Haiku 5.5) matches on the name part plus the
             harness word instead. */
          check(`${tag} activity lists model series`, m.legends.length > 0, `${m.legends.length} entries`);
          const cells = m.tiers.map((t) => t.text);
          const cellSet = new Set(cells);
          const stray = m.legends.filter((l) => {
            if (l === "Unattributed" || cellSet.has(l)) return false;
            const cut = l.indexOf(" · ");
            if (cut < 0) return true;
            const name = l.slice(0, cut), har = l.slice(cut);
            return !cells.some((t) => t.indexOf(name) >= 0 && t.slice(-har.length) === har);
          });
          check(`${tag} legend strings match table strings`, stray.length === 0, stray.slice(0, 3).join(" | "));
          check(`${tag} page never scrolls sideways`, m.sideways <= 1, `overflow ${m.sideways}px`);
        }
      }
      check("zero console errors and page exceptions", errors.length === 0, errors.slice(0, 3).join(" | ") || "clean");
      cdp.close();
    } finally {
      finishChrome();
    }
  } finally {
    try { server.kill(); } catch {}
    try { fs.closeSync(logFd); } catch {}
    await sleep(1000);
    if (!opts.keep) fs.rmSync(tmp, { recursive: true, force: true });
  }
  console.log(failures === 0 ? "model_names_check: PASS" : `model_names_check: FAIL (${failures})`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => { console.error("model_names_check errored:", err); process.exit(1); });
