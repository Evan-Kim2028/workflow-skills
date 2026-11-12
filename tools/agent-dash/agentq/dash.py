"""Live agentq dashboard on 127.0.0.1: one HTML page and a JSON summary.

The page polls ``/api/summary``. The summary reads the proxy's live status
and ``agentq.db`` (read-only) and the samples this module records once a
minute into its own ``dash.db``. ``agentq.db`` is never written here.
Stdlib only.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DEFAULT_PORT = 18791
DEFAULT_HOME = "~/.local/state/agentq"
PROXY_STATUS_URL = "http://127.0.0.1:18790/agentq/status"
DASH_DB_NAME = "dash.db"
RANGE_HOURS = (1, 6, 24)
DEFAULT_HOURS = 6
RECENT_JOBS = 25
LIMIT_WINDOW_MINUTES = 5
RATE_WINDOW_MINUTES = 60
SAMPLE_INTERVAL_S = 60.0
SAMPLE_KEEP_S = 7 * 24 * 3600
LIVE_TIMEOUT_S = 2.0
DB_TIMEOUT_S = 2.0
PS_TIMEOUT_S = 10.0
ERROR_CHARS = 200
LIMITS = {"total": 10_000_000, "output": 70_000, "uncached": 24_290}
LIMIT_KEYS = {"total": "total", "output": "ds_output", "uncached": "kimi_uncached"}
DEFAULT_CAPS = {"deepseek": 64, "kimi": 2}
TIER_CAP_KEYS = {"pi-deepseek-flash": "deepseek", "pi-kimi": "kimi"}
LIMIT_LABELS = {"total": "Total pool", "output": "DeepSeek output", "uncached": "Kimi uncached"}
ACTIVE_WINDOW_S = 120
PERF_WINDOW_MINUTES = 15
RUN_LOOKBACK_S = 7 * 24 * 3600
RUN_ROWS = 50
TOP_CONSUMERS = 10
VERDICTS = ("PASS", "PARTIAL", "FAIL")
QUEUE_JOB_ENV = re.compile(r"AGENTQ_JOB=[0-9a-f]{12}\b")
SAMPLES_DDL = """
CREATE TABLE IF NOT EXISTS samples (
    ts REAL PRIMARY KEY,
    ds_workers INT,
    kimi_workers INT,
    queue_workers INT,
    direct_workers INT,
    queued_jobs INT
)
"""
MINUTES_SQL = """
SELECT minute,
       SUM(COALESCE(input, 0) + COALESCE(cache_read, 0) + COALESCE(output, 0)) AS total,
       SUM(CASE WHEN model LIKE 'morph-dsv%' THEN COALESCE(output, 0) ELSE 0 END) AS ds_output,
       SUM(CASE WHEN model LIKE 'morph-kimi%' THEN COALESCE(input, 0) ELSE 0 END) AS kimi_uncached,
       SUM(COALESCE(ok, 0)) AS ok,
       SUM(COALESCE(r429, 0)) AS r429,
       SUM(COALESCE(req, 0)) AS req,
       SUM(COALESCE(input, 0)) AS input,
       SUM(COALESCE(cache_read, 0)) AS cache_read,
       SUM(COALESCE(output, 0)) AS output,
       SUM(COALESCE(wait_p50_ms, 0) * COALESCE(req, 0)) AS wait_p50_weighted,
       MAX(wait_p95_ms) AS wait_p95_ms
FROM proxy_minutes
WHERE minute >= ? AND minute < ?
GROUP BY minute
"""
JOBS_COUNT_SQL = "SELECT state, tier, COUNT(*) AS n FROM jobs GROUP BY state, tier"
JOBS_RECENT_SQL = """
SELECT id, state, lane, tier, kind, verdict, submitted_at, started_at, finished_at
FROM jobs ORDER BY submitted_at DESC LIMIT ?
"""
SAMPLES_SQL = """
SELECT ts, ds_workers, kimi_workers, queue_workers, direct_workers, queued_jobs
FROM samples WHERE ts >= ? ORDER BY ts
"""
SAMPLE_INSERT_SQL = "INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?, ?, ?)"
RUNS_SQL = """
SELECT p.job AS job,
       MIN(p.first_ts) AS first_ts,
       MAX(p.last_ts) AS last_ts,
       COALESCE(SUM(p.req), 0) AS req,
       COALESCE(SUM(p.ok), 0) AS ok,
       COALESCE(SUM(p.r429), 0) AS r429,
       COALESCE(SUM(p.input), 0) AS input,
       COALESCE(SUM(p.cache_read), 0) AS cache_read,
       COALESCE(SUM(p.output), 0) AS output,
       COALESCE(SUM(p.wait_ms_total), 0) AS wait_ms_total,
       COALESCE(SUM(p.upstream_ms_total), 0) AS upstream_ms_total,
       GROUP_CONCAT(DISTINCT p.model) AS models,
       j.label AS label,
       j.tier AS tier,
       j.state AS state,
       j.verdict AS verdict,
       j.started_at AS started_at,
       j.finished_at AS finished_at
FROM proxy_jobs AS p
LEFT JOIN jobs AS j ON j.id = p.job
WHERE p.last_ts >= ?
GROUP BY p.job
ORDER BY MAX(p.last_ts) DESC
"""
VERDICTS_TODAY_SQL = """
SELECT verdict, COUNT(*) AS n FROM jobs
WHERE finished_at >= ? AND verdict IS NOT NULL
GROUP BY verdict
"""
PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agentq dash</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7;
  --surface: #fcfcfb;
  --ink: #0b0b0b;
  --ink-2: #52514e;
  --ink-3: #898781;
  --grid: #e1e0d9;
  --axis: #c3c2b7;
  --ring: rgba(11, 11, 11, 0.10);
  --s1: #2a78d6;
  --s2: #eb6834;
  --s3: #1baf7a;
  --crit: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d;
    --surface: #1a1a19;
    --ink: #f0efec;
    --ink-2: #c3c2b7;
    --ink-3: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --ring: rgba(255, 255, 255, 0.10);
    --s1: #3987e5;
    --s2: #d95926;
    --s3: #199e70;
    --crit: #d03b3b;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d;
  --surface: #1a1a19;
  --ink: #f0efec;
  --ink-2: #c3c2b7;
  --ink-3: #898781;
  --grid: #2c2c2a;
  --axis: #383835;
  --ring: rgba(255, 255, 255, 0.10);
  --s1: #3987e5;
  --s2: #d95926;
  --s3: #199e70;
  --crit: #d03b3b;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--page);
  color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 1120px; margin: 0 auto; padding: 20px 20px 40px; }
header { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 20px; justify-content: space-between; margin-bottom: 16px; }
h1 { font-size: 18px; font-weight: 600; margin: 0; }
h2 { font-size: 14px; font-weight: 600; margin: 0 0 2px; }
h3.group { font-size: 12px; font-weight: 600; color: var(--ink-2); text-transform: uppercase; letter-spacing: 0.04em; margin: 18px 0 8px; }
.sub { color: var(--ink-2); font-size: 12px; }
.range { display: inline-flex; border: 1px solid var(--ring); border-radius: 6px; overflow: hidden; }
.range button {
  background: var(--surface); color: var(--ink-2); border: 0; padding: 6px 14px; font: inherit; cursor: pointer;
}
.range button + button { border-left: 1px solid var(--ring); }
.range button[aria-pressed="true"] { color: var(--ink); font-weight: 600; background: var(--page); }
.status { font-size: 12px; color: var(--ink-2); }
.status .stale { color: var(--crit); font-weight: 600; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 8px; }
.tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 8px; padding: 12px 14px; }
.tile.headline { border-color: var(--s1); }
.tile .label { color: var(--ink-2); font-size: 12px; }
.tile .value { font-size: 26px; font-weight: 600; margin-top: 2px; font-variant-numeric: tabular-nums; }
.tile .value.sm { font-size: 20px; }
.tile .note { color: var(--ink-3); font-size: 12px; margin-top: 2px; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 8px; padding: 14px 16px; margin-bottom: 14px; position: relative; }
.card-head { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px 16px; align-items: baseline; margin-bottom: 6px; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 12px; color: var(--ink-2); }
.legend .key { display: inline-flex; align-items: center; gap: 6px; }
.legend .swatch { width: 14px; height: 0; border-top: 2px solid; display: inline-block; }
.legend .block { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
.chart { position: relative; width: 100%; min-height: 240px; }
.chart svg { display: block; width: 100%; overflow: visible; }
.chart text { fill: var(--ink-2); font-size: 11px; font-variant-numeric: tabular-nums; }
.chart text.tick { fill: var(--ink-3); }
.chart text.direct { font-size: 12px; }
.empty { color: var(--ink-3); padding: 60px 0; text-align: center; }
.tip {
  position: absolute; pointer-events: none; background: var(--surface); border: 1px solid var(--ring);
  border-radius: 6px; padding: 6px 8px; font-size: 12px; color: var(--ink-2); display: none; white-space: nowrap;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08); z-index: 2;
}
.tip b { color: var(--ink); font-weight: 600; }
.tip .row { display: flex; align-items: center; gap: 6px; }
.tip .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }
.grid2 .card { margin-bottom: 0; }
table { width: 100%; border-collapse: collapse; font-size: 12px; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th { color: var(--ink-2); font-weight: 600; }
th.sortable { cursor: pointer; user-select: none; }
th.sortable:hover { color: var(--ink); }
th.sorted::after { content: attr(data-dir); margin-left: 4px; color: var(--ink-3); }
td { color: var(--ink); }
td.num, th.num { text-align: right; }
td.wrap { white-space: normal; max-width: 260px; overflow-wrap: anywhere; }
.table-wrap { overflow-x: auto; }
.note-line { color: var(--ink-3); font-size: 12px; margin-top: 4px; }
</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>agentq</h1>
      <div class="status" id="status">Loading</div>
    </div>
    <div class="range" role="group" aria-label="Time range">
      <button type="button" data-hours="1" aria-pressed="false">1h</button>
      <button type="button" data-hours="6" aria-pressed="true">6h</button>
      <button type="button" data-hours="24" aria-pressed="false">24h</button>
    </div>
  </header>

  <h3 class="group">Capacity</h3>
  <section class="tiles" aria-label="Capacity">
    <div class="tile headline"><div class="label">Capacity used, 5 min</div><div class="value" id="k-used">—</div><div class="note" id="k-binding">—</div></div>
    <div class="tile"><div class="label">Total pool, last minute</div><div class="value sm" id="lim-total-now">—</div><div class="note" id="lim-total-note">—</div></div>
    <div class="tile"><div class="label">DeepSeek output, last minute</div><div class="value sm" id="lim-output-now">—</div><div class="note" id="lim-output-note">—</div></div>
    <div class="tile"><div class="label">Kimi uncached, last minute</div><div class="value sm" id="lim-uncached-now">—</div><div class="note" id="lim-uncached-note">—</div></div>
    <div class="tile"><div class="label">Waiting at the proxy now</div><div class="value" id="k-waiting">—</div><div class="note" id="k-waiting-note">—</div></div>
    <div class="tile"><div class="label">Upstream 429s, last 60 min</div><div class="value" id="k-429">—</div><div class="note">absorbed by retries</div></div>
  </section>

  <h3 class="group">Runs</h3>
  <section class="tiles" aria-label="Runs">
    <div class="tile"><div class="label">DeepSeek runs active now</div><div class="value" id="r-ds">—</div><div class="note" id="r-ds-note">—</div></div>
    <div class="tile"><div class="label">Kimi runs active now</div><div class="value" id="r-kimi">—</div><div class="note" id="r-kimi-note">—</div></div>
    <div class="tile"><div class="label">Runs seen today</div><div class="value" id="r-seen">—</div><div class="note">distinct jobs, local day</div></div>
    <div class="tile"><div class="label">Finished today, PASS / PARTIAL / FAIL</div><div class="value sm" id="r-finished">—</div><div class="note">queue jobs with a verdict</div></div>
    <div class="tile"><div class="label">Tokens today</div><div class="value" id="r-tokens">—</div><div class="note">all runs, local day</div></div>
    <div class="tile"><div class="label">Median tokens per run</div><div class="value" id="r-median-tok">—</div><div class="note">runs seen today</div></div>
    <div class="tile"><div class="label">Median requests per run</div><div class="value" id="r-median-req">—</div><div class="note">runs seen today</div></div>
  </section>

  <h3 class="group">Performance</h3>
  <section class="tiles" aria-label="Performance">
    <div class="tile"><div class="label">Proxy wait, p50 / p95</div><div class="value sm" id="p-wait">—</div><div class="note">last 15 min; p95 is the worst minute</div></div>
    <div class="tile"><div class="label">Upstream time per request</div><div class="value sm" id="p-upstream">—</div><div class="note">median, runs active in 15 min</div></div>
    <div class="tile"><div class="label">429 rate</div><div class="value sm" id="p-429rate">—</div><div class="note">429s over requests, last 60 min</div></div>
    <div class="tile"><div class="label">Requests per minute</div><div class="value sm" id="p-rpm">—</div><div class="note">average, last 5 min</div></div>
  </section>

  <section class="card" aria-labelledby="h-billed">
    <div class="card-head">
      <h2 id="h-billed">Billed use, % of each Morph limit, per minute</h2>
      <div class="legend" id="lg-billed"></div>
    </div>
    <div class="chart" id="c-billed"></div>
  </section>

  <section class="card" aria-labelledby="h-workers">
    <div class="card-head">
      <h2 id="h-workers">Active runs over time, with caps</h2>
      <div class="legend" id="lg-workers"></div>
    </div>
    <div class="chart" id="c-workers"></div>
  </section>

  <section class="card" aria-labelledby="h-tokens">
    <div class="card-head">
      <h2 id="h-tokens">Tokens per minute, millions</h2>
      <div class="legend" id="lg-tokens"></div>
    </div>
    <div class="chart" id="c-tokens"></div>
  </section>

  <section class="grid2">
    <div class="card" aria-labelledby="h-429">
      <div class="card-head">
        <h2 id="h-429">Upstream 429s per minute</h2>
        <div class="legend"><span class="key"><span class="swatch" style="border-color: var(--crit)"></span>429s</span></div>
      </div>
      <div class="chart" id="c-429"></div>
    </div>
    <div class="card" aria-labelledby="h-wait">
      <div class="card-head">
        <h2 id="h-wait">Wait p95 per minute</h2>
        <div class="legend"><span class="key"><span class="swatch" style="border-color: var(--s1)"></span>p95 wait, seconds</span></div>
      </div>
      <div class="chart" id="c-wait"></div>
    </div>
  </section>

  <section class="card" aria-labelledby="h-runs">
    <div class="card-head">
      <h2 id="h-runs">Runs</h2>
      <div class="sub" id="runs-count">Top 50 by last activity; click a header to sort</div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr id="runs-head"></tr></thead>
        <tbody id="runs-body"></tbody>
      </table>
    </div>
    <div class="note-line">Cache-read % is cache reads over all input tokens. Avg wait is per request; avg upstream is per successful request. Queue runs show their jobs-table state.</div>
  </section>

  <section class="grid2">
    <div class="card" aria-labelledby="h-top">
      <div class="card-head">
        <h2 id="h-top">Top consumers today</h2>
        <div class="sub" id="top-total"></div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Run</th><th class="num">Tokens</th><th class="num">Share</th></tr></thead>
          <tbody id="top-body"></tbody>
        </table>
      </div>
    </div>
    <div class="card" aria-labelledby="h-jobs">
      <div class="card-head">
        <h2 id="h-jobs">Recent jobs</h2>
        <div class="sub" id="jobs-counts"></div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Id</th><th>State</th><th>Lane</th><th>Tier</th><th>Kind</th><th>Verdict</th><th>Submitted</th><th>Started</th><th>Finished</th></tr></thead>
          <tbody id="jobs-body"></tbody>
        </table>
      </div>
    </div>
  </section>

  <section class="card" aria-labelledby="h-table">
    <div class="card-head">
      <h2 id="h-table">Minute data (table view)</h2>
      <div class="sub">Last 60 complete minutes in the range</div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Minute (UTC)</th><th class="num">Total pool %</th><th class="num">DeepSeek output %</th><th class="num">Kimi uncached %</th><th class="num">Tokens (M)</th><th class="num">OK</th><th class="num">429s</th><th class="num">Wait p95 s</th></tr></thead>
        <tbody id="minute-body"></tbody>
      </table>
    </div>
    <div class="note-line">Billed % is the share of each Morph limit for the minute. Minutes with no proxy row count as zero.</div>
  </section>
</main>

<script>
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var POLL_MS = 30000;
  var state = { hours: 6, data: null, timer: null, inFlight: false, lastGood: null, stale: null, sort: null };
  var COLORS = { s1: "var(--s1)", s2: "var(--s2)", s3: "var(--s3)" };
  var charts = [];

  function $(id) { return document.getElementById(id); }
  function pad(n) { return n < 10 ? "0" + n : String(n); }
  function clock(ms) {
    var d = new Date(ms);
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  function hm(sec) {
    var d = new Date(sec * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function fullTime(sec) {
    if (sec === null || sec === undefined) return "—";
    var d = new Date(sec * 1000);
    return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + hm(sec);
  }
  function pct(frac, digits) {
    if (frac === null || frac === undefined) return "—";
    return (frac * 100).toFixed(digits === undefined ? 1 : digits) + "%";
  }
  function count(n) {
    return n === null || n === undefined ? "—" : Number(n).toLocaleString("en-US");
  }
  function num1(n) {
    if (n === null || n === undefined) return "—";
    return Number(n).toLocaleString("en-US", { maximumFractionDigits: 1 });
  }
  function tokens(n) {
    if (n === null || n === undefined) return "—";
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 1 : 2) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(0) + "k";
    return String(Math.round(n));
  }
  function duration(sec) {
    if (sec === null || sec === undefined) return "—";
    if (sec < 90) return Math.round(sec) + " s";
    if (sec < 5400) return Math.round(sec / 60) + " min";
    return (sec / 3600).toFixed(1) + " h";
  }
  function seconds(ms) {
    return ms === null || ms === undefined ? "—" : (ms / 1000).toFixed(2) + " s";
  }
  function svgEl(tag, attrs, parent) {
    var node = document.createElementNS(NS, tag);
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if ((k === "fill" || k === "stroke") && String(v).indexOf("var(") === 0) node.style.setProperty(k, v);
      else node.setAttribute(k, v);
    });
    if (parent) parent.appendChild(node);
    return node;
  }
  function niceTicks(maxValue, steps) {
    var top = maxValue > 0 ? maxValue : 1;
    var raw = top / steps;
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var unit = [1, 2, 2.5, 5, 10].map(function (m) { return m * mag; }).find(function (u) { return u >= raw; });
    var max = unit * steps;
    var ticks = [];
    for (var i = 0; i <= steps; i++) ticks.push(+(unit * i).toFixed(6));
    return { min: 0, max: max, ticks: ticks };
  }
  function legendFor(el, series) {
    el.innerHTML = "";
    series.forEach(function (s) {
      var key = document.createElement("span");
      key.className = "key";
      var sw = document.createElement("span");
      sw.className = "swatch";
      sw.style.borderColor = s.color;
      key.appendChild(sw);
      key.appendChild(document.createTextNode(s.name));
      el.appendChild(key);
    });
  }

  function drawChart(host, cfg) {
    host.innerHTML = "";
    var points = cfg.points || [];
    if (!points.length) {
      var empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No data in this range";
      host.appendChild(empty);
      return;
    }
    var W = Math.max(300, host.clientWidth || 600);
    var H = cfg.height || 240;
    var m = { l: 52, r: 120, t: 12, b: 26 };
    var iw = W - m.l - m.r;
    var ih = H - m.t - m.b;
    var x0 = cfg.x0, x1 = cfg.x1;
    function X(t) { return m.l + ((t - x0) / Math.max(1, x1 - x0)) * iw; }
    function Y(v) { return m.t + ih - ((v - cfg.y.min) / (cfg.y.max - cfg.y.min || 1)) * ih; }
    var svg = svgEl("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": cfg.title }, host);

    cfg.y.ticks.forEach(function (v) {
      var isBase = v === cfg.y.min;
      svgEl("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: isBase ? "var(--axis)" : "var(--grid)", "stroke-width": 1 }, svg);
      var label = svgEl("text", { x: m.l - 8, y: Y(v) + 4, "text-anchor": "end", class: "tick" }, svg);
      label.textContent = cfg.y.fmt(v);
    });
    for (var i = 0; i <= 4; i++) {
      var t = x0 + ((x1 - x0) * i) / 4;
      var anchor = i === 0 ? "start" : i === 4 ? "end" : "middle";
      var xl = svgEl("text", { x: X(t), y: H - 6, "text-anchor": anchor, class: "tick" }, svg);
      xl.textContent = hm(t);
    }

    (cfg.refs || []).forEach(function (r) {
      svgEl("line", { x1: m.l, x2: W - m.r, y1: Y(r.v), y2: Y(r.v), stroke: "var(--ink-3)", "stroke-width": 1, "stroke-dasharray": "5 4" }, svg);
      var rl = svgEl("text", { x: W - m.r + 6, y: Y(r.v) + 4, class: "direct", style: "fill: var(--ink-3)" }, svg);
      rl.textContent = r.label;
    });

    if (cfg.kind === "bar") {
      var band = iw / Math.max(1, points.length);
      var bw = Math.min(24, Math.max(2, band * 0.6));
      points.forEach(function (p) {
        var v = p[cfg.series[0].key];
        if (!v) return;
        var cx = X(p.t);
        var top = Y(v), base = Y(cfg.y.min);
        var r = Math.min(4, bw / 2, base - top);
        var left = cx - bw / 2, right = cx + bw / 2;
        var d = "M" + left + "," + base + " L" + left + "," + (top + r) +
          " Q" + left + "," + top + " " + (left + r) + "," + top +
          " L" + (right - r) + "," + top + " Q" + right + "," + top + " " + right + "," + (top + r) +
          " L" + right + "," + base + " Z";
        svgEl("path", { d: d, fill: cfg.series[0].color }, svg);
      });
    } else if (cfg.kind === "stack") {
      var below = points.map(function () { return 0; });
      cfg.series.forEach(function (s) {
        var above = points.map(function (p, idx) { return below[idx] + (p[s.key] || 0); });
        var d = "";
        points.forEach(function (p, idx) {
          d += (idx ? " L" : "M") + X(p.t).toFixed(1) + "," + Y(above[idx]).toFixed(1);
        });
        for (var j = points.length - 1; j >= 0; j--) {
          d += " L" + X(points[j].t).toFixed(1) + "," + Y(below[j]).toFixed(1);
        }
        svgEl("path", { d: d + " Z", fill: s.color, "fill-opacity": 0.5, stroke: s.color, "stroke-width": 1 }, svg);
        below = above;
      });
    } else {
      cfg.series.forEach(function (s) {
        var d = "";
        var lastPoint = null;
        points.forEach(function (p) {
          var v = p[s.key];
          if (v === null || v === undefined) { lastPoint = null; return; }
          d += (lastPoint ? " L" : " M") + X(p.t).toFixed(1) + "," + Y(v).toFixed(1);
          lastPoint = p;
        });
        if (d) svgEl("path", { d: d.trim(), fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
      });
      var ends = [];
      cfg.series.forEach(function (s) {
        for (var j = points.length - 1; j >= 0; j--) {
          var v = points[j][s.key];
          if (v !== null && v !== undefined) {
            ends.push({ s: s, y: Y(v), x: X(points[j].t), v: v });
            return;
          }
        }
      });
      ends.sort(function (a, b) { return a.y - b.y; });
      var lastY = -Infinity;
      ends.forEach(function (e) {
        var ly = Math.max(e.y + 4, lastY + 15);
        lastY = ly;
        svgEl("circle", { cx: e.x, cy: e.y, r: 4, fill: e.s.color, stroke: "var(--surface)", "stroke-width": 2 }, svg);
        var lab = svgEl("text", { x: e.x + 9, y: ly, class: "direct", style: "fill: var(--ink-2)" }, svg);
        lab.textContent = e.s.name + " " + cfg.y.fmt(e.v, true);
      });
    }

    var cross = svgEl("line", { x1: 0, x2: 0, y1: m.t, y2: m.t + ih, stroke: "var(--ink-3)", "stroke-width": 1, visibility: "hidden" }, svg);
    var hit = svgEl("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent" }, svg);
    var tip = document.createElement("div");
    tip.className = "tip";
    host.appendChild(tip);

    function nearest(sec) {
      var best = null, bestD = Infinity;
      points.forEach(function (p) {
        var dist = Math.abs(p.t - sec);
        if (dist < bestD) { bestD = dist; best = p; }
      });
      return best;
    }
    hit.addEventListener("mousemove", function (ev) {
      var box = svg.getBoundingClientRect();
      var px = ev.clientX - box.left;
      var sec = x0 + ((px - m.l) / Math.max(1, iw)) * (x1 - x0);
      var p = nearest(sec);
      if (!p) return;
      cross.setAttribute("x1", X(p.t)); cross.setAttribute("x2", X(p.t));
      cross.setAttribute("visibility", "visible");
      tip.innerHTML = "";
      var head = document.createElement("div");
      head.innerHTML = "<b>" + hm(p.t) + "</b>";
      tip.appendChild(head);
      cfg.series.forEach(function (s) {
        var row = document.createElement("div");
        row.className = "row";
        var dot = document.createElement("span");
        dot.className = "dot";
        dot.style.background = s.color;
        row.appendChild(dot);
        var txt = document.createElement("span");
        txt.textContent = s.name + ": " + (p[s.key] === null || p[s.key] === undefined ? "—" : cfg.y.fmt(p[s.key], true));
        row.appendChild(txt);
        tip.appendChild(row);
      });
      tip.style.display = "block";
      var tipX = X(p.t) + 12;
      if (tipX + 170 > W) tipX = X(p.t) - 180;
      tip.style.left = Math.max(0, tipX) + "px";
      tip.style.top = "8px";
    });
    hit.addEventListener("mouseleave", function () {
      cross.setAttribute("visibility", "hidden");
      tip.style.display = "none";
    });
    return svg;
  }

  function registerChart(hostId, cfg) {
    var host = $(hostId);
    var entry = { host: host, cfg: cfg };
    charts.push(entry);
    drawChart(host, cfg);
  }

  function redrawAll() {
    charts.forEach(function (c) { drawChart(c.host, c.cfg); });
  }

  function setText(id, value) { $(id).textContent = value; }

  function renderCapacity(d) {
    var cap = d.capacity || {};
    var limits = d.limits || {};
    setText("k-used", pct(cap.used_frac));
    setText("k-binding", cap.binding_label ? "binding limit: " + cap.binding_label : "no traffic in 5 min");
    [["total", "lim-total"], ["output", "lim-output"], ["uncached", "lim-uncached"]].forEach(function (row) {
      var lim = limits[row[0]] || {};
      setText(row[1] + "-now", pct(lim.last_min_frac));
      var target = lim.target_frac === null || lim.target_frac === undefined ? "target n/a" : "target " + pct(lim.target_frac, 0);
      setText(row[1] + "-note", "5 min avg " + pct(lim.avg_5m_frac) + " · " + target);
    });
    setText("k-waiting", count(cap.waiting_now));
    var lanes = cap.waiting_by_lane || {};
    setText("k-waiting-note", Object.keys(lanes).length
      ? Object.keys(lanes).map(function (k) { return k + " " + lanes[k]; }).join(" · ")
      : "proxy status unavailable");
    setText("k-429", count(d.r429_60m));
  }

  function renderRunKpis(d) {
    var rk = d.run_kpis || {};
    setText("r-ds", count(rk.active_deepseek));
    setText("r-ds-note", "of cap " + count(rk.cap_deepseek));
    setText("r-kimi", count(rk.active_kimi));
    setText("r-kimi-note", "of cap " + count(rk.cap_kimi));
    setText("r-seen", count(rk.seen_today));
    var fin = rk.finished_today || {};
    setText("r-finished", ["PASS", "PARTIAL", "FAIL"].map(function (v) { return count(fin[v] || 0); }).join(" / "));
    setText("r-tokens", tokens(rk.tokens_today));
    setText("r-median-tok", tokens(rk.median_tokens_per_run));
    setText("r-median-req", num1(rk.median_requests_per_run));
  }

  function renderPerf(d) {
    var pk = d.perf_kpis || {};
    var p50 = pk.wait_p50_ms === null || pk.wait_p50_ms === undefined ? "—" : seconds(pk.wait_p50_ms);
    setText("p-wait", p50 + " / " + seconds(pk.wait_p95_ms));
    setText("p-upstream", seconds(pk.upstream_median_ms));
    setText("p-429rate", pct(pk.r429_rate_60m, 2));
    setText("p-rpm", num1(pk.requests_per_min_5m));
  }

  function renderCharts(d) {
    var win = d.window || { start: 0, end: 0 };
    var minutes = d.minutes || [];
    var runsOverTime = (d.workers || []).map(function (w) { return { t: w.ts, ds: w.ds_workers, kimi: w.kimi_workers, queued: w.queued_jobs }; });
    var caps = d.caps || { deepseek: 64, kimi: 2 };
    var maxPct = minutes.reduce(function (a, p) {
      return Math.max(a, p.pct_total, p.pct_ds_output, p.pct_kimi_uncached);
    }, 0);
    var maxWorkers = runsOverTime.reduce(function (a, w) {
      return Math.max(a, w.ds || 0, w.kimi || 0);
    }, 0);
    var pctFmt = function (v, inline) { return inline ? (v * 100).toFixed(1) + "%" : Math.round(v * 100) + "%"; };
    charts.length = 0;
    registerChart("c-billed", {
      title: "Billed use, percent of each Morph limit per minute",
      kind: "line", x0: win.start, x1: win.end, height: 260,
      points: minutes,
      series: [
        { key: "pct_total", name: "Total pool", color: COLORS.s1 },
        { key: "pct_ds_output", name: "DeepSeek output", color: COLORS.s2 },
        { key: "pct_kimi_uncached", name: "Kimi uncached", color: COLORS.s3 }
      ],
      y: niceTicks(Math.max(1.5, maxPct), 3),
      refs: [{ v: 1, label: "100% of limit" }],
      fmt: pctFmt
    });
    registerChart("c-workers", {
      title: "Active runs over time",
      kind: "line", x0: win.start, x1: win.end, height: 260,
      points: runsOverTime,
      series: [
        { key: "ds", name: "DeepSeek runs", color: COLORS.s1 },
        { key: "kimi", name: "Kimi runs", color: COLORS.s2 },
        { key: "queued", name: "Queued jobs", color: COLORS.s3 }
      ],
      y: niceTicks(Math.max(caps.deepseek, maxWorkers), 4),
      refs: [{ v: caps.deepseek, label: "DeepSeek cap " + caps.deepseek }, { v: caps.kimi, label: "Kimi cap " + caps.kimi }],
      fmt: function (v) { return Math.round(v).toLocaleString("en-US"); }
    });
    var mix = (d.token_mix || []).map(function (p) {
      return { t: p.t, cache: p.cache_read / 1e6, uncached: p.uncached / 1e6, output: p.output / 1e6 };
    });
    var maxMix = mix.reduce(function (a, p) { return Math.max(a, p.cache + p.uncached + p.output); }, 0);
    var millions = function (v) { return (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + "M"; };
    registerChart("c-tokens", {
      title: "Tokens per minute, millions",
      kind: "stack", x0: win.start, x1: win.end, height: 260,
      points: mix,
      series: [
        { key: "cache", name: "Cache read", color: COLORS.s3 },
        { key: "uncached", name: "Uncached input", color: COLORS.s1 },
        { key: "output", name: "Output", color: COLORS.s2 }
      ],
      y: niceTicks(Math.max(maxMix, 0.1), 4),
      fmt: millions
    });
    legendFor($("lg-billed"), [
      { name: "Total pool", color: COLORS.s1 },
      { name: "DeepSeek output", color: COLORS.s2 },
      { name: "Kimi uncached", color: COLORS.s3 }
    ]);
    legendFor($("lg-workers"), [
      { name: "DeepSeek runs", color: COLORS.s1 },
      { name: "Kimi runs", color: COLORS.s2 },
      { name: "Queued jobs", color: COLORS.s3 }
    ]);
    legendFor($("lg-tokens"), [
      { name: "Cache read", color: COLORS.s3 },
      { name: "Uncached input", color: COLORS.s1 },
      { name: "Output", color: COLORS.s2 }
    ]);
    var maxR = minutes.reduce(function (a, p) { return Math.max(a, p.r429); }, 0);
    registerChart("c-429", {
      title: "Upstream 429s per minute",
      kind: "bar", x0: win.start, x1: win.end, height: 200,
      points: minutes,
      series: [{ key: "r429", name: "429s", color: "var(--crit)" }],
      y: niceTicks(Math.max(maxR, 1), 3),
      fmt: function (v) { return Math.round(v).toLocaleString("en-US"); }
    });
    var maxW = minutes.reduce(function (a, p) { return p.wait_p95_ms === null ? a : Math.max(a, p.wait_p95_ms / 1000); }, 0);
    var waitPoints = minutes.map(function (p) {
      return { t: p.t, wait: p.wait_p95_ms === null || p.wait_p95_ms === undefined ? null : p.wait_p95_ms / 1000 };
    });
    registerChart("c-wait", {
      title: "Wait p95 seconds per minute",
      kind: "line", x0: win.start, x1: win.end, height: 200,
      points: waitPoints,
      series: [{ key: "wait", name: "p95 wait", color: COLORS.s1 }],
      y: niceTicks(Math.max(maxW, 1), 3),
      fmt: function (v, inline) { return v.toFixed(inline ? 1 : 0) + " s"; }
    });
  }

  var RUN_COLUMNS = [
    { key: "run", label: "Run", text: true, value: function (r) { return r.run; }, cell: function (r) { return r.run; }, wrap: true },
    { key: "tier", label: "Tier / model", text: true, value: function (r) { return (r.tier || "") + " " + r.models; }, cell: function (r) { return (r.tier ? r.tier + " · " : "") + r.models; } },
    { key: "status", label: "Status", text: true, value: function (r) { return r.status; }, cell: function (r) { return r.status; } },
    { key: "started", label: "Started", value: function (r) { return r.started; }, cell: function (r) { return fullTime(r.started); } },
    { key: "duration", label: "Duration", num: true, value: function (r) { return r.duration_s; }, cell: function (r) { return duration(r.duration_s); } },
    { key: "requests", label: "Requests", num: true, value: function (r) { return r.requests; }, cell: function (r) { return count(r.requests); } },
    { key: "total", label: "Total tokens", num: true, value: function (r) { return r.total_tokens; }, cell: function (r) { return tokens(r.total_tokens); } },
    { key: "output", label: "Output tokens", num: true, value: function (r) { return r.output; }, cell: function (r) { return tokens(r.output); } },
    { key: "cache", label: "Cache-read %", num: true, value: function (r) { return r.cache_read_pct; }, cell: function (r) { return pct(r.cache_read_pct, 0); } },
    { key: "r429", label: "429s", num: true, value: function (r) { return r.r429; }, cell: function (r) { return count(r.r429); } },
    { key: "wait", label: "Avg wait s", num: true, value: function (r) { return r.avg_wait_s; }, cell: function (r) { return r.avg_wait_s === null ? "—" : r.avg_wait_s.toFixed(2); } },
    { key: "upstream", label: "Avg upstream s", num: true, value: function (r) { return r.avg_upstream_s; }, cell: function (r) { return r.avg_upstream_s === null ? "—" : r.avg_upstream_s.toFixed(1); } },
    { key: "verdict", label: "Verdict", text: true, value: function (r) { return r.verdict; }, cell: function (r) { return r.verdict || "—"; } }
  ];

  function sortedRuns(runs) {
    var list = runs.slice();
    if (!state.sort) return list;
    var col = RUN_COLUMNS.find(function (c) { return c.key === state.sort.key; });
    if (!col) return list;
    var dir = state.sort.dir;
    list.sort(function (a, b) {
      var va = col.value(a), vb = col.value(b);
      var na = va === null || va === undefined, nb = vb === null || vb === undefined;
      if (na || nb) return na === nb ? 0 : na ? 1 : -1;
      if (typeof va === "string" || typeof vb === "string") return String(va).localeCompare(String(vb)) * dir;
      return (va - vb) * dir;
    });
    return list;
  }

  function renderRunsTable(d) {
    var head = $("runs-head");
    head.innerHTML = "";
    RUN_COLUMNS.forEach(function (col) {
      var th = document.createElement("th");
      th.className = "sortable" + (col.num ? " num" : "");
      th.setAttribute("data-key", col.key);
      th.textContent = col.label;
      if (state.sort && state.sort.key === col.key) {
        th.className += " sorted";
        th.setAttribute("data-dir", state.sort.dir > 0 ? "▲" : "▼");
      }
      th.setAttribute("aria-sort", state.sort && state.sort.key === col.key ? (state.sort.dir > 0 ? "ascending" : "descending") : "none");
      th.addEventListener("click", function () {
        if (state.sort && state.sort.key === col.key) state.sort = { key: col.key, dir: -state.sort.dir };
        else state.sort = { key: col.key, dir: col.text ? 1 : -1 };
        renderRunsTable(state.data || d);
      });
      head.appendChild(th);
    });
    var body = $("runs-body");
    body.innerHTML = "";
    var runs = d.runs || [];
    if (!runs.length) {
      var none = document.createElement("tr");
      var cell = document.createElement("td");
      cell.colSpan = RUN_COLUMNS.length;
      cell.textContent = d.errors && d.errors.runs ? "Runs unavailable: " + d.errors.runs : "No runs in the last 7 days";
      none.appendChild(cell);
      body.appendChild(none);
    }
    sortedRuns(runs).forEach(function (r) {
      var tr = document.createElement("tr");
      RUN_COLUMNS.forEach(function (col) {
        var td = document.createElement("td");
        if (col.num) td.className = "num";
        if (col.wrap) td.className += " wrap";
        td.textContent = col.cell(r);
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    $("runs-count").textContent = "Top " + runs.length + " by last activity; click a header to sort";
  }

  function renderTop(d) {
    var body = $("top-body");
    body.innerHTML = "";
    var top = d.top_consumers || [];
    if (!top.length) {
      var none = document.createElement("tr");
      var cell = document.createElement("td");
      cell.colSpan = 3;
      cell.textContent = "No runs today";
      none.appendChild(cell);
      body.appendChild(none);
    }
    top.forEach(function (r) {
      var tr = document.createElement("tr");
      [r.run, tokens(r.total_tokens), pct(r.share, 1)].forEach(function (v, i) {
        var td = document.createElement("td");
        if (i > 0) td.className = "num";
        td.textContent = String(v);
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    var today = (d.run_kpis || {}).tokens_today;
    $("top-total").textContent = today === undefined ? "" : "Today: " + tokens(today) + " tokens";
  }

  function renderJobs(d) {
    var body = $("jobs-body");
    body.innerHTML = "";
    var jobs = d.jobs || {};
    var recent = jobs.recent || [];
    if (!recent.length) {
      var none = document.createElement("tr");
      var cell = document.createElement("td");
      cell.colSpan = 9;
      cell.textContent = jobs.recent ? "No jobs yet" : "Jobs unavailable";
      none.appendChild(cell);
      body.appendChild(none);
    }
    recent.forEach(function (j) {
      var tr = document.createElement("tr");
      [j.id, j.state, j.lane, j.tier, j.kind, j.verdict || "—",
        fullTime(j.submitted_at), fullTime(j.started_at), fullTime(j.finished_at)].forEach(function (v) {
        var td = document.createElement("td");
        td.textContent = v === null || v === undefined ? "—" : String(v);
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    var byState = jobs.by_state || {};
    var parts = Object.keys(byState).sort().map(function (k) { return k + " " + byState[k]; });
    $("jobs-counts").textContent = parts.length ? "All jobs: " + parts.join(" · ") : "";
  }

  function renderMinuteTable(d) {
    var body = $("minute-body");
    body.innerHTML = "";
    var rows = (d.minutes || []).slice(-60).reverse();
    rows.forEach(function (p) {
      var tr = document.createElement("tr");
      [p.minute, pct(p.pct_total), pct(p.pct_ds_output), pct(p.pct_kimi_uncached), tokens(p.total), count(p.ok), count(p.r429),
        p.wait_p95_ms === null ? "—" : (p.wait_p95_ms / 1000).toFixed(1)].forEach(function (v, i) {
        var td = document.createElement("td");
        if (i > 0) td.className = "num";
        td.textContent = String(v);
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
  }

  function renderStatus() {
    var status = $("status");
    status.innerHTML = "";
    if (state.stale) {
      var warn = document.createElement("span");
      warn.className = "stale";
      warn.textContent = "Stale: fetch failed";
      status.appendChild(warn);
      status.appendChild(document.createTextNode(" · " + state.stale + " · "));
    }
    var text = state.lastGood
      ? "updated " + Math.max(0, Math.floor((Date.now() - state.lastGood) / 1000)) + "s ago"
      : "loading";
    var errs = state.data && state.data.errors ? Object.keys(state.data.errors) : [];
    if (errs.length) text += " · partial: " + errs.join(", ");
    status.appendChild(document.createTextNode(text));
  }

  function render(d) {
    renderCapacity(d);
    renderRunKpis(d);
    renderPerf(d);
    renderCharts(d);
    renderRunsTable(d);
    renderTop(d);
    renderJobs(d);
    renderMinuteTable(d);
    renderStatus();
  }

  function refresh() {
    if (state.inFlight) return;
    state.inFlight = true;
    fetch("/api/summary?hours=" + state.hours, { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        state.data = data;
        state.lastGood = Date.now();
        state.stale = null;
        render(data);
      })
      .catch(function (err) {
        state.stale = err && err.message ? err.message : "network error";
        renderStatus();
      })
      .then(function () { state.inFlight = false; });
  }

  function startPolling() {
    stopPolling();
    state.timer = setInterval(function () {
      if (document.visibilityState === "visible") refresh();
    }, POLL_MS);
  }
  function stopPolling() {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
  }

  document.querySelectorAll(".range button").forEach(function (btn) {
    btn.addEventListener("click", function () {
      state.hours = Number(btn.getAttribute("data-hours"));
      document.querySelectorAll(".range button").forEach(function (other) {
        other.setAttribute("aria-pressed", other === btn ? "true" : "false");
      });
      refresh();
    });
  });

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      refresh();
      startPolling();
    } else {
      stopPolling();
    }
  });

  if (window.ResizeObserver) {
    var lastWidth = 0;
    new ResizeObserver(function (entries) {
      var w = Math.round(entries[0].contentRect.width);
      if (w !== lastWidth) { lastWidth = w; redrawAll(); }
    }).observe(document.querySelector("main"));
  }

  setInterval(renderStatus, 1000);

  if (document.visibilityState === "visible") {
    refresh();
    startPolling();
  }
})();
</script>
</body>
</html>
"""


def minute_stamp(ts: float) -> str:
    """The proxy's minute key for an epoch time: UTC, ``YYYY-MM-DDTHH:MM``."""
    return time.strftime("%Y-%m-%dT%H:%M", time.gmtime(ts))


def minute_epoch(stamp: str) -> int:
    """The epoch second at the start of a proxy minute key."""
    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M"))


def fraction(value: float, limit: int) -> float:
    """A value as a fraction of a limit, rounded for the JSON."""
    return round(value / limit, 4)


def tier_of(line: str) -> str | None:
    """The cap key ('deepseek' or 'kimi') a launch_pi.py command line runs under, if any."""
    if "pi-kimi" in line or "morph-kimi" in line:
        return "kimi"
    if "pi-deepseek-flash" in line or "morph-dsv41flash" in line:
        return "deepseek"
    return None


def parse_workers(ps_text: str) -> dict[str, int]:
    """Count live pi workers in ``ps -E -axo pid=,command=`` output.

    A queue worker carries ``AGENTQ_JOB=<12 hex>`` in its environment; every
    other launch_pi.py process is direct.
    """
    counts = {"deepseek": 0, "kimi": 0, "queue": 0, "total": 0}
    for line in ps_text.splitlines():
        if "launch_pi.py" not in line:
            continue
        counts["total"] += 1
        tier = tier_of(line)
        if tier:
            counts[tier] += 1
        if QUEUE_JOB_ENV.search(line):
            counts["queue"] += 1
    return {
        "ds_workers": counts["deepseek"],
        "kimi_workers": counts["kimi"],
        "queue_workers": counts["queue"],
        "direct_workers": counts["total"] - counts["queue"],
    }


def run_ps() -> str:
    """The process table with environments. Raises when ps fails, so no sample is recorded."""
    result = subprocess.run(
        ["ps", "-E", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        timeout=PS_TIMEOUT_S,
        check=True,
    )
    return result.stdout


def fetch_live_status(url: str = PROXY_STATUS_URL, timeout: float = LIVE_TIMEOUT_S) -> dict | None:
    """The proxy's status JSON, or raises when the proxy does not answer."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data if isinstance(data, dict) else None


def readonly_connection(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite file read-only. Raises FileNotFoundError when it is missing."""
    if not path.exists():
        raise FileNotFoundError(f"{path.name} not found")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=DB_TIMEOUT_S)
    connection.row_factory = sqlite3.Row
    return connection


def read_minute_rows(agentq_db: Path, since: int, until: int) -> dict[int, dict]:
    """Summed proxy_minutes rows keyed by the epoch second of each complete minute in [since, until)."""
    connection = readonly_connection(agentq_db)
    try:
        rows = connection.execute(MINUTES_SQL, (minute_stamp(since), minute_stamp(until))).fetchall()
    finally:
        connection.close()
    return {minute_epoch(row["minute"]): dict(row) for row in rows}


def minute_point(t: int, row: dict | None) -> dict:
    """One chart point: raw usage for a minute plus its fraction of each Morph limit."""
    row = row or {}
    total = int(row.get("total") or 0)
    ds_output = int(row.get("ds_output") or 0)
    kimi_uncached = int(row.get("kimi_uncached") or 0)
    return {
        "t": t,
        "minute": minute_stamp(t),
        "total": total,
        "ds_output": ds_output,
        "kimi_uncached": kimi_uncached,
        "input": int(row.get("input") or 0),
        "cache_read": int(row.get("cache_read") or 0),
        "output": int(row.get("output") or 0),
        "ok": int(row.get("ok") or 0),
        "r429": int(row.get("r429") or 0),
        "req": int(row.get("req") or 0),
        "wait_p50_weighted": int(row.get("wait_p50_weighted") or 0),
        "wait_p95_ms": row.get("wait_p95_ms"),
        "pct_total": fraction(total, LIMITS["total"]),
        "pct_ds_output": fraction(ds_output, LIMITS["output"]),
        "pct_kimi_uncached": fraction(kimi_uncached, LIMITS["uncached"]),
    }


def minute_series(raw: dict[int, dict], since: int, until: int) -> list[dict]:
    """Complete minutes from the first recorded one to the last complete one.

    The proxy writes a row only for minutes with traffic, so a minute with no
    row is a minute with zero billed use. The first recorded minute bounds the
    series so the window before the proxy started stays empty.
    """
    if not raw:
        return []
    first = max(min(raw), since)
    return [minute_point(t, raw.get(t)) for t in range(first, until, 60)]


def average_fraction(raw: dict[int, dict], until: int, key: str, limit: int) -> float:
    """Mean billed fraction of a limit over the last LIMIT_WINDOW_MINUTES complete minutes."""
    stamps = range(until - LIMIT_WINDOW_MINUTES * 60, until, 60)
    billed = sum(int((raw.get(t) or {}).get(key) or 0) for t in stamps)
    return fraction(billed / LIMIT_WINDOW_MINUTES, limit)


def recent_429s(raw: dict[int, dict], until: int) -> int:
    """Upstream 429 responses over the last RATE_WINDOW_MINUTES complete minutes."""
    stamps = range(until - RATE_WINDOW_MINUTES * 60, until, 60)
    return sum(int((raw.get(t) or {}).get("r429") or 0) for t in stamps)


def live_targets(live: dict | None) -> dict[str, float | None]:
    """The proxy's current target fraction per budget name, None when the proxy gave none."""
    budgets = (live or {}).get("budgets")
    targets: dict[str, float | None] = {}
    for name in LIMIT_KEYS:
        budget = budgets.get(name) if isinstance(budgets, dict) else None
        value = budget.get("target_frac") if isinstance(budget, dict) else None
        targets[name] = float(value) if isinstance(value, (int, float)) else None
    return targets


def read_jobs(agentq_db: Path) -> dict:
    """Job counts by state and by tier, plus the most recent jobs. Read-only."""
    connection = readonly_connection(agentq_db)
    try:
        counts = connection.execute(JOBS_COUNT_SQL).fetchall()
        recent = connection.execute(JOBS_RECENT_SQL, (RECENT_JOBS,)).fetchall()
    finally:
        connection.close()
    by_state: dict[str, int] = {}
    by_tier: dict[str, dict[str, int]] = {}
    for row in counts:
        by_state[row["state"]] = by_state.get(row["state"], 0) + row["n"]
        by_tier.setdefault(row["tier"], {})[row["state"]] = row["n"]
    return {"by_state": by_state, "by_tier": by_tier, "recent": [dict(row) for row in recent]}


def read_queued(agentq_db: Path) -> int:
    """How many jobs are queued right now."""
    connection = readonly_connection(agentq_db)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM jobs WHERE state = 'queued'").fetchone()[0])
    finally:
        connection.close()


def read_caps(home: Path) -> dict[str, int]:
    """Worker caps from broker.json's tier limits, defaulting to the DeepSeek 64 and Kimi 2 pools."""
    caps = dict(DEFAULT_CAPS)
    try:
        tiers = json.loads((home / "broker.json").read_text(encoding="utf-8")).get("tiers") or {}
    except (OSError, ValueError, AttributeError):
        return caps
    for tier, key in TIER_CAP_KEYS.items():
        value = tiers.get(tier) if isinstance(tiers, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            caps[key] = value
    return caps


def read_samples(dash_db: Path, since: float) -> list[dict]:
    """Worker samples recorded at or after ``since``. Empty before the first sample exists."""
    if not dash_db.exists():
        return []
    connection = readonly_connection(dash_db)
    try:
        return [dict(row) for row in connection.execute(SAMPLES_SQL, (since,)).fetchall()]
    finally:
        connection.close()


def last_minute_fraction(raw: dict[int, dict], until: int, key: str, limit: int) -> float:
    """Billed fraction of a limit in the last complete minute."""
    return fraction(int((raw.get(until - 60) or {}).get(key) or 0), limit)


def window_sum(raw: dict[int, dict], until: int, minutes: int, key: str) -> int:
    """Sum of one minute-row field over the last ``minutes`` complete minutes."""
    return sum(int((raw.get(t) or {}).get(key) or 0) for t in range(until - minutes * 60, until, 60))


def day_start(now: float) -> float:
    """Epoch second of local midnight on the day ``now`` falls in."""
    local = time.localtime(now)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))


def median_or_none(values: list[float]) -> float | None:
    """The median of ``values``, or None when there are none."""
    return statistics.median(values) if values else None


def capacity_headline(limits: dict, live: dict | None) -> dict:
    """The limit with the highest 5-minute billed fraction (the binding one), and requests waiting at the proxy."""
    binding = max(LIMIT_KEYS, key=lambda name: limits[name]["avg_5m_frac"]) if limits else None
    queued = (live or {}).get("queued") if isinstance(live, dict) else None
    by_lane = None
    waiting = None
    if isinstance(queued, dict):
        by_lane = {str(lane): int(n) for lane, n in queued.items() if isinstance(n, int)}
        waiting = sum(by_lane.values())
    return {
        "used_frac": limits[binding]["avg_5m_frac"] if binding else None,
        "binding": binding,
        "binding_label": LIMIT_LABELS[binding] if binding else None,
        "waiting_now": waiting,
        "waiting_by_lane": by_lane,
    }


def read_run_rows(agentq_db: Path, since: int) -> list[dict]:
    """Per-job proxy totals joined to the queue job row, for jobs with proxy activity since ``since``. Read-only."""
    connection = readonly_connection(agentq_db)
    try:
        return [dict(row) for row in connection.execute(RUNS_SQL, (since,)).fetchall()]
    finally:
        connection.close()


def read_verdicts_today(agentq_db: Path, day: float) -> dict[str, int]:
    """Queue jobs finished since ``day`` by verdict. Read-only."""
    connection = readonly_connection(agentq_db)
    try:
        rows = connection.execute(VERDICTS_TODAY_SQL, (day,)).fetchall()
    finally:
        connection.close()
    return {row["verdict"]: row["n"] for row in rows}


def shape_run(row: dict, now: float) -> dict:
    """One run: a queue job when the jobs table has its id (status from jobs.state), else a direct launch."""
    queue = row["state"] is not None
    if queue:
        active = row["state"] == "running"
        status = row["state"]
    else:
        active = now - row["last_ts"] <= ACTIVE_WINDOW_S
        status = "active" if active else "idle"
    if queue and row["started_at"] is not None:
        started = row["started_at"]
        end = row["finished_at"] if row["finished_at"] is not None else row["last_ts"]
    else:
        started = row["first_ts"]
        end = row["last_ts"]
    requests, ok = row["req"], row["ok"]
    input_tokens, cache_read, output = row["input"], row["cache_read"], row["output"]
    fresh = input_tokens + cache_read
    return {
        "job": row["job"],
        "run": row["label"] or row["job"],
        "tier": row["tier"],
        "models": row["models"] or "",
        "queue": queue,
        "status": status,
        "active": active,
        "started": started,
        "last_ts": row["last_ts"],
        "duration_s": end - started,
        "requests": requests,
        "r429": row["r429"],
        "input": input_tokens,
        "cache_read": cache_read,
        "output": output,
        "total_tokens": fresh + output,
        "cache_read_pct": cache_read / fresh if fresh else None,
        "avg_wait_s": row["wait_ms_total"] / requests / 1000 if requests else None,
        "avg_upstream_s": row["upstream_ms_total"] / ok / 1000 if ok else None,
        "upstream_ms_per_req": row["upstream_ms_total"] / ok if ok else None,
        "verdict": row["verdict"],
    }


def run_tier(run: dict) -> str | None:
    """'deepseek' or 'kimi' for a run: its queue tier, else its models (a run on both counts as DeepSeek)."""
    if run["queue"] and run["tier"]:
        return tier_of(run["tier"])
    tiers = {tier_of(model) for model in run["models"].split(",") if model}
    if "deepseek" in tiers:
        return "deepseek"
    return "kimi" if "kimi" in tiers else None


def run_kpis(runs: list[dict], day: float, caps: dict[str, int], verdicts: dict[str, int]) -> dict:
    """Active runs against their caps, runs and tokens seen today, and today's finished queue verdicts."""
    active_tiers = [run_tier(run) for run in runs if run["active"]]
    today = [run for run in runs if run["last_ts"] >= day]
    return {
        "active_deepseek": active_tiers.count("deepseek"),
        "active_kimi": active_tiers.count("kimi"),
        "cap_deepseek": caps["deepseek"],
        "cap_kimi": caps["kimi"],
        "seen_today": len(today),
        "finished_today": {verdict: int(verdicts.get(verdict, 0)) for verdict in VERDICTS},
        "tokens_today": sum(run["total_tokens"] for run in today),
        "median_tokens_per_run": median_or_none([run["total_tokens"] for run in today]),
        "median_requests_per_run": median_or_none([run["requests"] for run in today]),
    }


def perf_kpis(raw: dict[int, dict], until: int, runs: list[dict], now: float) -> dict:
    """Proxy wait, upstream time, 429 rate and request rate. Wait p50 is request-weighted; p95 is the worst minute."""
    window = [raw[t] for t in range(until - PERF_WINDOW_MINUTES * 60, until, 60) if t in raw]
    req_15m = sum(row["req"] for row in window)
    p95s = [row["wait_p95_ms"] for row in window if row["wait_p95_ms"] is not None]
    upstream = [
        run["upstream_ms_per_req"]
        for run in runs
        if run["upstream_ms_per_req"] is not None and now - run["last_ts"] <= PERF_WINDOW_MINUTES * 60
    ]
    req_60m = window_sum(raw, until, RATE_WINDOW_MINUTES, "req")
    return {
        "wait_p50_ms": sum(row["wait_p50_weighted"] for row in window) / req_15m if req_15m else None,
        "wait_p95_ms": max(p95s) if p95s else None,
        "upstream_median_ms": median_or_none(upstream),
        "r429_rate_60m": window_sum(raw, until, RATE_WINDOW_MINUTES, "r429") / req_60m if req_60m else None,
        "requests_per_min_5m": window_sum(raw, until, LIMIT_WINDOW_MINUTES, "req") / LIMIT_WINDOW_MINUTES,
    }


def token_mix(minutes: list[dict]) -> list[dict]:
    """Per-minute tokens split into cache read, uncached input and output."""
    return [
        {
            "t": point["t"],
            "minute": point["minute"],
            "cache_read": point["cache_read"],
            "uncached": point["input"],
            "output": point["output"],
        }
        for point in minutes
    ]


def top_consumers(runs: list[dict], day: float, tokens_today: int) -> list[dict]:
    """The runs with the most tokens today, each with its share of today's total."""
    today = sorted((run for run in runs if run["last_ts"] >= day), key=lambda run: run["total_tokens"], reverse=True)
    return [
        {
            "run": run["run"],
            "job": run["job"],
            "total_tokens": run["total_tokens"],
            "share": run["total_tokens"] / tokens_today if tokens_today else None,
        }
        for run in today[:TOP_CONSUMERS]
    ]


def _guard(name: str, errors: dict, fn, default, *args):
    """Run one data source; on failure record its error and return the default."""
    try:
        return fn(*args)
    except Exception as exc:
        errors[name] = f"{type(exc).__name__}: {exc}"[:ERROR_CHARS]
        return default


def build_summary(home: Path, hours: int = DEFAULT_HOURS, *, now: float | None = None, fetch_live=fetch_live_status) -> dict:
    """The JSON the page renders. Each source degrades to null or empty and is named in ``errors``."""
    home = Path(home)
    now = time.time() if now is None else float(now)
    hours = hours if hours in RANGE_HOURS else DEFAULT_HOURS
    until = int(now // 60) * 60
    since = until - hours * 3600
    agentq_db = home / "agentq.db"
    errors: dict[str, str] = {}
    raw = _guard("minutes", errors, read_minute_rows, {}, agentq_db, since, until)
    live = _guard("live", errors, fetch_live, None)
    jobs = _guard("jobs", errors, read_jobs, None, agentq_db)
    samples = _guard("workers", errors, read_samples, [], home / DASH_DB_NAME, float(since))
    day = day_start(now)
    run_rows = _guard("runs", errors, read_run_rows, [], agentq_db, int(now - RUN_LOOKBACK_S))
    verdicts = _guard("verdicts", errors, read_verdicts_today, {}, agentq_db, day)
    caps = read_caps(home)
    targets = live_targets(live)
    limits = {
        name: {
            "label": LIMIT_LABELS[name],
            "limit": LIMITS[name],
            "last_min_frac": last_minute_fraction(raw, until, LIMIT_KEYS[name], LIMITS[name]),
            "avg_5m_frac": average_fraction(raw, until, LIMIT_KEYS[name], LIMITS[name]),
            "target_frac": targets[name],
        }
        for name in LIMIT_KEYS
    }
    runs = shape_runs(run_rows, now)
    minutes = minute_series(raw, since, until)
    kpis = run_kpis(runs, day, caps, verdicts)
    return {
        "generated_at": now,
        "hours": hours,
        "window": {"start": since, "end": until},
        "live": live,
        "limits": limits,
        "capacity": capacity_headline(limits, live),
        "r429_60m": recent_429s(raw, until),
        "minutes": minutes,
        "token_mix": token_mix(minutes),
        "jobs": jobs,
        "workers": samples,
        "caps": caps,
        "runs": runs[:RUN_ROWS],
        "run_kpis": kpis,
        "perf_kpis": perf_kpis(raw, until, runs, now),
        "top_consumers": top_consumers(runs, day, kpis["tokens_today"]),
        "errors": errors,
    }


def shape_runs(rows: list[dict], now: float) -> list[dict]:
    """Every run in the lookback, most recent activity first."""
    return [shape_run(row, now) for row in rows]


def write_sample(dash_db: Path, row: dict) -> None:
    """Store one worker sample and prune samples older than SAMPLE_KEEP_S. Never touches agentq.db."""
    dash_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(dash_db, timeout=DB_TIMEOUT_S)
    try:
        connection.execute(SAMPLES_DDL)
        connection.execute(
            SAMPLE_INSERT_SQL,
            (
                row["ts"],
                row["ds_workers"],
                row["kimi_workers"],
                row["queue_workers"],
                row["direct_workers"],
                row["queued_jobs"],
            ),
        )
        connection.execute("DELETE FROM samples WHERE ts < ?", (row["ts"] - SAMPLE_KEEP_S,))
        connection.commit()
    finally:
        connection.close()


def sample_once(home: Path, ps_runner=run_ps, now: float | None = None) -> dict:
    """Take one worker sample. Raises when ps fails, so the gap shows instead of a false zero."""
    home = Path(home)
    now = time.time() if now is None else now
    row = {"ts": now, **parse_workers(ps_runner()), "queued_jobs": None}
    try:
        row["queued_jobs"] = read_queued(home / "agentq.db")
    except (sqlite3.Error, FileNotFoundError):
        row["queued_jobs"] = None
    write_sample(home / DASH_DB_NAME, row)
    return row


class Sampler(threading.Thread):
    """Samples worker counts every SAMPLE_INTERVAL_S seconds on a daemon thread."""

    def __init__(self, home: Path, ps_runner=run_ps, interval: float = SAMPLE_INTERVAL_S) -> None:
        super().__init__(name="agentq-dash-sampler", daemon=True)
        self.home = Path(home)
        self.ps_runner = ps_runner
        self.interval = interval
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                sample_once(self.home, self.ps_runner)
            except Exception as exc:
                sys.stderr.write(f"agentq dash: sample skipped: {type(exc).__name__}: {exc}\n")
            self.stop_event.wait(self.interval)


class Handler(BaseHTTPRequestHandler):
    """GET / serves the page; GET /api/summary?hours=N serves the JSON summary."""

    server_version = "agentq-dash"

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE_HTML.encode("utf-8"))
        elif parts.path == "/api/summary":
            requested = parse_qs(parts.query).get("hours", [None])[0]
            hours = int(requested) if requested and requested.isdigit() else DEFAULT_HOURS
            summary = build_summary(self.server.home, hours, fetch_live=self.server.fetch_live)
            self._send(200, "application/json", json.dumps(summary).encode("utf-8"))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


def make_server(home: Path, port: int, *, fetch_live=fetch_live_status) -> ThreadingHTTPServer:
    """A server bound to 127.0.0.1. Port 0 picks a free port."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.home = Path(home)
    server.fetch_live = fetch_live
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentq-dash", description="Live agentq dashboard on 127.0.0.1.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--home", default=os.environ.get("AGENTQ_HOME", DEFAULT_HOME))
    args = parser.parse_args(argv)
    home = Path(os.path.expanduser(args.home))
    server = make_server(home, args.port)
    Sampler(home).start()
    sys.stderr.write(f"agentq dash on http://127.0.0.1:{server.server_address[1]}/ (home {home})\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
