#!/usr/bin/env node
/* Browser regression check for token and cost coherence.
 *
 * Drives the live dashboard in headless Chrome over the DevTools protocol
 * (stdlib only: global fetch and WebSocket, child_process, no npm installs).
 * For the overview and every workstream tab at the one fixed 24 h window it
 * first asserts the page's internal invariant strictly from one DOM read
 * (tile, legend Total and chart sum agree, and the cost tile is present),
 * since that is what the owner sees. It then compares the DOM to the API
 * with a consistent read: /api/snapshot (with usage_group, so the response
 * also carries the chart buckets) is fetched right before and right after
 * the DOM read, and a DOM value between the two (or equal to either) is
 * live growth, not a mismatch. Each tab is retried up to 3 times.
 *
 * Before the tab loop it asserts the page settles: a 3 s sample of the
 * render counter (window.__adRenders, one per drawMain) must look
 * quiescent, so a perpetual re-render fails fast instead of wedging the
 * tab reads.
 *
 * After the tab loop it asserts the pinned-chrome budget: scrolled
 * 1200 px, only one slim row may stay pinned (48 px at 1440x900,
 * 44 px at 390x844 with no sideways scrolling).
 *
 * Exit 0 when everything agrees, 1 with a table on any mismatch, 2 when
 * Chrome is missing (the agent_dash.py wrapper reports a skip then).
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

const WINDOW_HOURS = 24;
const SETTLE_POLL_MS = 700;
const COMBO_TIMEOUT_MS = 30000;
const MAX_ATTEMPTS = 3;
const RETRY_MS = 1500;

function args(argv) {
  const out = { url: "http://127.0.0.1:8765", chrome: null, ws: null, perf: false };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === "--url" && argv[i + 1]) out.url = argv[++i].replace(/\/+$/, "");
    else if (flag === "--chrome" && argv[i + 1]) out.chrome = argv[++i];
    else if (flag === "--ws" && argv[i + 1]) out.ws = argv[++i];
    else if (flag === "--perf") out.perf = true;
    else if (flag === "-h" || flag === "--help") {
      console.log("usage: page_check.mjs [--url URL] [--chrome PATH] [--ws WS] [--perf]");
      console.log("  --perf runs only the snappiness gates: first section under 1.5 s");
      console.log("  and idle re-renders under 3 per 30 s (run against an idle server;");
      console.log("  a live server with active writers legitimately re-renders more).");
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
    console.error(`page_check: chrome not found at ${explicit}`);
    process.exit(2);
  }
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (process.platform === "darwin" && fs.existsSync(mac)) return mac;
  const found = which(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]);
  if (found) return found;
  console.error("page_check: no Chrome binary found (tried CHROME_PATH, the default install path and PATH)");
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

/* Wait until the tile shows numbers newer than the stale read, then
 * settle. Falls back to a plain settled read when nothing moves (an idle
 * server has nothing fresher to show). */
async function readFresh(cdp, expr, ws, staleTile) {
  const deadline = Date.now() + 8000;
  for (;;) {
    let cur = null;
    try {
      cur = await evaluate(cdp, expr);
    } catch {
      cur = null;
    }
    if (cur && cur.tile && Number(cur.tile.tokens) !== staleTile) break;
    if (Date.now() > deadline) break;
    await new Promise((r) => setTimeout(r, 500));
  }
  return await readSettled(cdp, expr, ws);
}

/* Wait until two consecutive DOM reads agree: the page has applied its
 * latest fetch. One evaluate call is atomic, so the returned object is a
 * single render (tile, legend, chart and cost from the same moment). */
async function readSettled(cdp, expr, ws) {
  const deadline = Date.now() + COMBO_TIMEOUT_MS;
  let stable = 0;
  let prev = "";
  for (;;) {
    let cur = null;
    try {
      cur = await evaluate(cdp, expr);
    } catch {
      cur = null;
    }
    if (cur && cur.settled) {
      const fingerprint = JSON.stringify([cur.settled, cur.tile, cur.legend, cur.cost]);
      if (fingerprint === prev) {
        stable++;
        if (stable >= 1) return cur;
      } else {
        stable = 0;
        prev = fingerprint;
      }
    }
    if (Date.now() > deadline) {
      throw new Error(`page never settled for ${ws} ${WINDOW_HOURS}h`);
    }
    await new Promise((r) => setTimeout(r, SETTLE_POLL_MS));
  }
}

function readDomExpression(ws) {
  const hours = WINDOW_HOURS;
  return `(function(){
    var s = window.__adSettled || "";
    var parts = String(s).split("|");
    if (parts[0] !== ${JSON.stringify(ws)} || parts[1] !== ${JSON.stringify(String(WINDOW_HOURS))}) return null;
    /* The pulse KPI strip owns the overview numbers (tiles stay on
       workstream tabs only), so the overview tab reads its KPI cells. */
    var onOverview = ${JSON.stringify(ws)} === "all";
    var tile = onOverview
      ? document.querySelector('#x-pulse [data-kpi="tokens"]')
      : document.querySelector('#exec-tiles [data-tile="tokens"]');
    if (!tile) return null;
    if (!onOverview) {
      if (tile.getAttribute("data-ws") !== ${JSON.stringify(ws)}) return {bleed: tile.getAttribute("data-ws")};
      if (tile.getAttribute("data-hours") !== ${JSON.stringify(String(WINDOW_HOURS))}) return null;
    }
    var cost = onOverview
      ? document.querySelector('#x-pulse [data-kpi="cost"]')
      : document.querySelector('#exec-tiles [data-tile="cost"]');
    var lg = document.querySelector('#exec-activity .ltotal');
    var emptyChart = !lg && !!document.querySelector('#exec-activity .empty');
    if (!lg && !emptyChart) return null;
    var xl = document.querySelector('#exec-activity .xline');
    var cards = [];
    if (${JSON.stringify(ws)} === "all") {
      cards = Array.prototype.map.call(document.querySelectorAll('[data-wsc]'), function (c) {
        return {id: c.getAttribute("data-wsc"), tokens: c.getAttribute("data-tokens"), cost: c.getAttribute("data-cost-usd"), hours: c.getAttribute("data-hours")};
      });
    }
    return {settled: s,
      tile: {tokens: tile.getAttribute("data-tokens"), allTime: tile.getAttribute("data-tokens-all-time"), text: tile.textContent},
      cost: cost ? {usd: cost.getAttribute("data-cost-usd"), allTime: cost.getAttribute("data-cost-all-time")} : null,
      legend: lg ? {total: lg.getAttribute("data-legend-total"), chartSum: lg.getAttribute("data-chart-sum")} : {total: "0", chartSum: "0", empty: true},
      xline: xl ? xl.textContent : null,
      cards};
  })()`;
}

function billed(tokens) {
  return (tokens.input || 0) + (tokens.output || 0) + (tokens.cache_read || 0) + (tokens.cache_write || 0);
}

function bucketSum(buckets) {
  return (buckets || []).reduce(
    (acc, b) => acc + (b.input || 0) + (b.output || 0) + (b.cache_read || 0) + (b.cache_write || 0),
    0,
  );
}

function inBracket(value, a, b) {
  return value >= Math.min(a, b) && value <= Math.max(a, b);
}

function inCostBracket(value, a, b) {
  return value >= Math.min(a, b) - 1e-6 && value <= Math.max(a, b) + 1e-6;
}

/* The page's internal invariant, from a single DOM read: the tile, the
 * legend Total and the chart sum are one render, so they must agree
 * exactly; the cost tile must be present. Returns a detail string, or null. */
function internalDetail(dom) {
  const tileTok = Number(dom.tile.tokens);
  const legendTot = Number(dom.legend.total);
  const chartSum = Number(dom.legend.chartSum);
  if (!Number.isInteger(tileTok) || !Number.isInteger(legendTot) || !Number.isInteger(chartSum)) {
    return `one render has non-integer numbers: tile ${dom.tile.tokens} legend ${dom.legend.total} chart ${dom.legend.chartSum}`;
  }
  if (tileTok !== legendTot || tileTok !== chartSum) {
    return `one render disagrees with itself: tile ${tileTok} legend ${legendTot} chart ${chartSum}`;
  }
  if (dom.xline) {
    return `stale cross line while tile and chart agree at ${tileTok}`;
  }
  if (!dom.cost || !Number.isFinite(Number(dom.cost.usd))) {
    return `cost tile missing in the same render (tile ${tileTok})`;
  }
  return null;
}

/* Same-response server invariant: the usage buckets riding along in a
 * snapshot sum to that snapshot's totals. No time skew is involved, so
 * this is strict. Returns a detail string, or null. */
function snapUsageDetail(snap, tag) {
  if (!Array.isArray(snap.usage)) return null;
  const sum = bucketSum(snap.usage);
  const tot = billed(snap.totals.tokens);
  if (sum !== tot) return `snapshot ${tag} usage buckets sum to ${sum} but totals say ${tot}`;
  return null;
}

/* DOM against the bracketing snapshots: a value between the two (or equal
 * to either) is live growth. Returns a detail string, or null. */
function bracketDetail(dom, snapBefore, snapAfter) {
  const tileTok = Number(dom.tile.tokens);
  const beforeTok = billed(snapBefore.totals.tokens);
  const afterTok = billed(snapAfter.totals.tokens);
  if (!inBracket(tileTok, beforeTok, afterTok)) {
    return `tile ${tileTok} outside snapshot bracket [${beforeTok}, ${afterTok}]`;
  }
  const domCost = Number(dom.cost.usd);
  const beforeCost = snapBefore.totals.cost_usd || 0;
  const afterCost = snapAfter.totals.cost_usd || 0;
  if (!inCostBracket(domCost, beforeCost, afterCost)) {
    return `cost tile ${dom.cost.usd} outside snapshot bracket [${beforeCost}, ${afterCost}]`;
  }
  const domAll = Number(dom.tile.allTime);
  const beforeAll = billed(snapBefore.totals_all_time.tokens);
  const afterAll = billed(snapAfter.totals_all_time.tokens);
  if (!inBracket(domAll, beforeAll, afterAll)) {
    return `tile all-time ${domAll} outside snapshot bracket [${beforeAll}, ${afterAll}]`;
  }
  return null;
}

async function apiJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`GET ${url} -> ${response.status}`);
  return response.json();
}

async function main() {
  const opts = args(process.argv.slice(2));
  const chrome = findChrome(opts.chrome);
  const port = await freePort();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "dash-page-check-"));
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
  const rows = [];
  let failures = 0;
  let growthReads = 0;
  let preAtomic = false;
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

    // No tab strip anymore: discover workstreams from the page state the
    // switcher itself reads (Overall first, then plan rows in tab states).
    const tabsExpression = `["all"].concat((((window.state && window.state.wss) || []).filter(function (w) { var st = (window.ADT && window.ADT.wsStateOf(w)) || w.state || "active"; return w.plan && (st === "active" || st === "paused" || st === "done"); })).map(function (w) { return w.ws; }))`;
    await cdp.send("Page.navigate", { url: `${opts.url}/?view=exec&ws=all` });
    await waitForLoad(cdp);
    {
      // Settle gate: sample the render count (window.__adRenders, one per
      // drawMain) until a 3 s window looks quiescent. A perpetual
      // re-render (a tiny bundle set with no plans kept re-arming the plan
      // load) raises the count in nearly every poll; a healthy live page
      // only re-renders on server ticks, seconds apart. Unknown on older
      // page builds, so a missing counter passes.
      const deadline = Date.now() + COMBO_TIMEOUT_MS;
      for (;;) {
        const samples = [];
        for (let i = 0; i < 12; i++) {
          await new Promise((r) => setTimeout(r, 250));
          try {
            samples.push(await evaluate(cdp, "window.__adRenders || 0"));
          } catch {
            samples.push(samples.length ? samples[samples.length - 1] : 0);
          }
        }
        let hot = 0;
        for (let i = 1; i < samples.length; i++) if (samples[i] > samples[i - 1]) hot++;
        const delta = samples[samples.length - 1] - samples[0];
        if (hot < 4 && delta <= 4) break;
        if (Date.now() > deadline) {
          throw new Error(
            `page never settled: the render count kept climbing (${samples[0]} -> ${samples[samples.length - 1]} with ${hot} rising polls per 3 s window; suspect a re-render loop)`,
          );
        }
      }
    }
    if (opts.perf) {
      // Snappiness gates on a fresh navigation: first section fast, then
      // a quiet idle. Skips the coherence loops; run against an idle
      // server (a live one with active writers re-renders for real).
      await cdp.send("Page.navigate", { url: `${opts.url}/?view=exec&ws=all` });
      const t0 = Date.now();
      let firstMs = null;
      for (;;) {
        let has = false;
        try {
          has = await evaluate(cdp, "!!document.querySelector('[data-sec]')");
        } catch {
          has = false;
        }
        if (has) {
          firstMs = Date.now() - t0;
          break;
        }
        if (Date.now() - t0 > 60000) break;
        await new Promise((r) => setTimeout(r, 100));
      }
      console.log(`perf: first section drawn in ${firstMs} ms (budget 1500 ms)`);
      if (firstMs == null || firstMs > 1500) {
        throw new Error(`perf: first draw ${firstMs} ms exceeds the 1500 ms budget`);
      }
      const rendersBefore = await evaluate(cdp, "window.__adRenders || 0");
      await new Promise((r) => setTimeout(r, 30000));
      const rendersAfter = await evaluate(cdp, "window.__adRenders || 0");
      const idleRenders = rendersAfter - rendersBefore;
      console.log(`perf: ${idleRenders} idle re-render(s) per 30 s (budget under 3)`);
      if (idleRenders >= 3) {
        throw new Error(`perf: ${idleRenders} idle re-renders per 30 s, want under 3`);
      }
      console.log("perf: ok");
      finish(0);
      return;
    }
    let tabs = [];
    {
      // Wait for the tab set to settle, not just appear: the first render
      // has only the overview tab while the workstream list still loads.
      const deadline = Date.now() + COMBO_TIMEOUT_MS;
      let prev = "";
      for (;;) {
        let cur = [];
        try {
          cur = ((await evaluate(cdp, tabsExpression)) || []).filter(Boolean);
        } catch {
          cur = [];
        }
        const fp = JSON.stringify(cur);
        if (cur.length && fp === prev) {
          tabs = cur;
          break;
        }
        prev = fp;
        if (Date.now() > deadline) {
          throw new Error("tabs never settled with data-ws (is the page reachable from Chrome, and is the server running the token-coherence page?)");
        }
        await new Promise((r) => setTimeout(r, 500));
      }
    }
    if (opts.ws) tabs = tabs.filter((t) => t === opts.ws);
    if (!tabs.length) throw new Error("no tabs matched");
    const hours = WINDOW_HOURS;

    for (const ws of tabs) {
      await cdp.send("Page.navigate", { url: `${opts.url}/?view=exec&ws=${encodeURIComponent(ws)}` });
      await waitForLoad(cdp);
      {
        const row = { ws, hours, tile: null, snap: null, legend: null, chart: null, cost: null, allTime: null, live: "-", status: "OK", detail: "" };
        rows.push(row);
        const fail = (detail) => {
          row.status = "MISMATCH";
          row.detail = detail;
          failures++;
        };
        try {
          const expr = readDomExpression(ws);
          const seen = await readSettled(cdp, expr, ws);
          if (seen.bleed !== undefined && seen.tile === undefined) {
            fail(`cross-tab bleed: asked ${ws}, tile shows ${seen.bleed}`);
            continue;
          }
          const snapUrl = (sinceValue) =>
            `${opts.url}/api/snapshot?ws=${encodeURIComponent(ws === "all" ? "all" : ws)}&since=${sinceValue}&usage_group=model`;
          // Consistent read: bracket the DOM read with two snapshots. A DOM
          // value between the two (or equal to either) is live growth.
          // A retry re-triggers the page fetch first, so a DOM left behind
          // by growth is re-read fresh instead of failing the same way.
          let attemptDetail = "";
          let passed = null;
          let staleTile = null;
          for (let attempt = 1; attempt <= MAX_ATTEMPTS && !passed; attempt++) {
            if (attempt > 1) {
              await new Promise((r) => setTimeout(r, RETRY_MS));
              await readFresh(cdp, expr, ws, staleTile);
            }
            const snapBefore = await apiJson(snapUrl(Math.floor(Date.now() / 1000 - hours * 3600)));
            const dom = await evaluate(cdp, expr);
            const snapAfter = await apiJson(snapUrl(Math.floor(Date.now() / 1000 - hours * 3600)));
            if (!dom || !dom.tile) {
              attemptDetail = `dom read failed on attempt ${attempt}`;
              continue;
            }
            staleTile = Number(dom.tile.tokens);
            if (!Array.isArray(snapBefore.usage) || !Array.isArray(snapAfter.usage)) preAtomic = true;
            attemptDetail =
              internalDetail(dom) ||
              snapUsageDetail(snapBefore, "before") ||
              snapUsageDetail(snapAfter, "after") ||
              bracketDetail(dom, snapBefore, snapAfter) ||
              "";
            if (!attemptDetail) passed = { dom, snapBefore, snapAfter };
          }
          if (!passed) {
            fail(`after ${MAX_ATTEMPTS} tries: ${attemptDetail}`);
            continue;
          }
          const { dom, snapBefore, snapAfter } = passed;
          const tileTok = Number(dom.tile.tokens);
          row.tile = tileTok;
          row.snap = billed(snapAfter.totals.tokens);
          row.legend = Number(dom.legend.total);
          row.chart = Number(dom.legend.chartSum);
          row.cost = Number(dom.cost.usd);
          row.allTime = Number(dom.tile.allTime);
          if (
            billed(snapBefore.totals.tokens) !== billed(snapAfter.totals.tokens) ||
            (snapBefore.totals.cost_usd || 0) !== (snapAfter.totals.cost_usd || 0) ||
            billed(snapBefore.totals_all_time.tokens) !== billed(snapAfter.totals_all_time.tokens)
          ) {
            row.live = "live growth";
            growthReads++;
          }
          if (tileTok === 0) {
            // Zero with context: "0 in 24 h" (both tiles and the pulse
            // strip say it); the last-run/all-time tertiary line is the
            // tiles' own contract on workstream tabs.
            const text = (dom.tile.text || "").replace(/\s+/g, " ");
            const allTimeTok = billed(snapAfter.totals_all_time.tokens);
            if (!text.includes(`0 in ${hours} h`)) {
              fail(`zero tile does not say "0 in ${hours} h": ${text.slice(0, 120)}`);
              continue;
            }
            if (ws !== "all") {
              if (allTimeTok > 0 && !/last run .* all time/.test(text)) {
                fail(`zero tile names no last run or all-time total: ${text.slice(0, 160)}`);
                continue;
              }
              if (allTimeTok === 0 && snapAfter.last_activity !== null && snapAfter.last_activity !== undefined) {
                fail(`tile shows no all-time tokens but the scope has activity`);
                continue;
              }
            }
          }
          if (ws === "all" && dom.cards && dom.cards.length) {
            const byBefore = {};
            for (const r of snapBefore.by_workstream) byBefore[r.ws] = r;
            const byAfter = {};
            for (const r of snapAfter.by_workstream) byAfter[r.ws] = r;
            for (const card of dom.cards) {
              if (card.hours !== String(hours)) {
                fail(`overview card ${card.id} shows ${card.hours}h, asked ${hours}h`);
                break;
              }
              const key = card.id === "Unassigned" ? "unassigned" : card.id;
              const before = byBefore[key];
              const after = byAfter[key];
              if (!before && !after) continue;
              const tokLo = before ? before.tokens : after.tokens;
              const tokHi = after ? after.tokens : before.tokens;
              if (!inBracket(Number(card.tokens), tokLo, tokHi)) {
                fail(`overview card ${card.id} ${card.tokens} outside tab-row bracket [${tokLo}, ${tokHi}]`);
                break;
              }
              const costLo = before ? before.cost_usd : after.cost_usd;
              const costHi = after ? after.cost_usd : before.cost_usd;
              if (!inCostBracket(Number(card.cost), costLo, costHi)) {
                fail(`overview card ${card.id} cost ${card.cost} outside tab-row bracket [${costLo}, ${costHi}]`);
                break;
              }
            }
            if (row.status !== "OK") continue;
          }
        } catch (error) {
          row.status = "ERROR";
          row.detail = String((error && error.message) || error);
          failures++;
        }
      }
    }
    if (!rows.length) throw new Error("no tab combinations ran");
    // Pinned-chrome gate: while scrolled only one slim row may stay
    // pinned (48 px desktop, 44 px phone), and the phone viewport must
    // not scroll sideways. Same block-height definition everywhere:
    // the lowest bottom among wide sticky/fixed elements at the top.
    const pinnedExpr = `(function(){
      var bottoms = [];
      var els = document.querySelectorAll("body *");
      for (var i = 0; i < els.length; i++) {
        var e = els[i];
        var cs = getComputedStyle(e);
        if (cs.position !== "sticky" && cs.position !== "fixed") continue;
        if (cs.display === "none") continue;
        var r = e.getBoundingClientRect();
        if (r.width < 300 || r.left > 100) continue;
        if (r.top <= 1 && r.bottom > 0) bottoms.push(r.bottom);
      }
      return {pinned: bottoms.length ? Math.max.apply(null, bottoms) : 0,
        overflowX: document.documentElement.scrollWidth > window.innerWidth + 1};
    })()`;
    const headerPaths = ["/?view=exec&ws=all", "/?view=project"];
    for (const size of [{ w: 1440, h: 900, mobile: false, limit: 48 }, { w: 390, h: 844, mobile: true, limit: 44 }]) {
      await cdp.send("Emulation.setDeviceMetricsOverride", {
        width: size.w, height: size.h, deviceScaleFactor: 1, mobile: size.mobile,
      });
      for (const headerPath of headerPaths) {
        await cdp.send("Page.navigate", { url: `${opts.url}${headerPath}` });
        await waitForLoad(cdp);
        await new Promise((r) => setTimeout(r, 1200));
        await evaluate(cdp, "window.scrollTo(0, 1200)");
        await new Promise((r) => setTimeout(r, 600));
        const m = await evaluate(cdp, pinnedExpr);
        const tag = `pinned ${size.w}x${size.h}${size.mobile ? " phone" : ""} ${headerPath}`;
        const overLimit = size.limit === 48 ? m.pinned > 48 : m.pinned > 44;
        if (overLimit) {
          console.log(`${tag}: pinned chrome ${Math.round(m.pinned)}px over the <= ${size.limit}px budget`);
          failures++;
        } else {
          console.log(`${tag}: pinned chrome ${Math.round(m.pinned)}px within <= ${size.limit}px`);
        }
        if (size.mobile && m.overflowX) {
          console.log(`${tag}: page scrolls sideways at 390px`);
          failures++;
        }
      }
    }
    await cdp.send("Emulation.clearDeviceMetricsOverride");
  } catch (error) {
    console.error(`page_check failed: ${(error && error.message) || error}`);
    finish(1);
    return;
  }

  const width = Math.max(10, ...rows.map((r) => String(r.ws).length));
  console.log(`page check ${opts.url} (${rows.length} tab combinations, 24 h window)`);
  console.log(
    `${"workstream".padEnd(width)} ${"window".padStart(6)} ${"tile".padStart(12)} ${"snap".padStart(12)} ${"legend".padStart(12)} ${"chart".padStart(12)} ${"cost".padStart(10)} ${"live".padStart(11)} ${"status".padStart(8)}`,
  );
  for (const row of rows) {
    console.log(
      `${String(row.ws).padEnd(width)} ${(`${row.hours}h`).padStart(5)} ${String(row.tile ?? "-").padStart(12)} ${String(row.snap ?? "-").padStart(12)} ${String(row.legend ?? "-").padStart(12)} ${String(row.chart ?? "-").padStart(12)} ${String(row.cost ?? "-").padStart(10)} ${String(row.live ?? "-").padStart(11)} ${row.status.padStart(8)}`,
    );
    if (row.detail) console.log(`  - ${row.detail}`);
  }
  if (preAtomic) {
    console.log(
      "note: the server never sent snap.usage (pre-atomic build); internal mismatches above come from the old page code it still serves, not from this harness",
    );
  }
  if (failures) {
    console.log(`${failures} mismatch(es)`);
    finish(1);
    return;
  }
  console.log(
    `ok: every rendered tile and legend matches the API${growthReads ? ` (${growthReads} read(s) during live growth)` : ""}`,
  );
  finish(0);
}

main();
