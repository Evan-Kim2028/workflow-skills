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
.sub { color: var(--ink-2); font-size: 12px; }
.range { display: inline-flex; border: 1px solid var(--ring); border-radius: 6px; overflow: hidden; }
.range button {
  background: var(--surface); color: var(--ink-2); border: 0; padding: 6px 14px; font: inherit; cursor: pointer;
}
.range button + button { border-left: 1px solid var(--ring); }
.range button[aria-pressed="true"] { color: var(--ink); font-weight: 600; background: var(--page); }
.status { font-size: 12px; color: var(--ink-2); }
.status .stale { color: var(--crit); font-weight: 600; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 14px; }
.tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 8px; padding: 12px 14px; }
.tile .label { color: var(--ink-2); font-size: 12px; }
.tile .value { font-size: 26px; font-weight: 600; margin-top: 2px; }
.tile .note { color: var(--ink-3); font-size: 12px; margin-top: 2px; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 8px; padding: 14px 16px; margin-bottom: 14px; position: relative; }
.card-head { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px 16px; align-items: baseline; margin-bottom: 6px; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 12px; color: var(--ink-2); }
.legend .key { display: inline-flex; align-items: center; gap: 6px; }
.legend .swatch { width: 14px; height: 0; border-top: 2px solid; display: inline-block; }
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
td { color: var(--ink); }
td.num, th.num { text-align: right; }
.table-wrap { overflow-x: auto; }
details { margin-top: 10px; }
summary { cursor: pointer; color: var(--ink-2); font-size: 12px; }
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

  <section class="tiles" aria-label="Current state">
    <div class="tile"><div class="label">DeepSeek workers running</div><div class="value" id="t-ds">—</div><div class="note" id="n-ds">—</div></div>
    <div class="tile"><div class="label">Kimi workers running</div><div class="value" id="t-kimi">—</div><div class="note" id="n-kimi">—</div></div>
    <div class="tile"><div class="label">Jobs queued</div><div class="value" id="t-queued">—</div><div class="note" id="n-queued">—</div></div>
    <div class="tile"><div class="label">Upstream 429s, last 60 min</div><div class="value" id="t-429">—</div><div class="note" id="n-429">—</div></div>
    <div class="tile"><div class="label">Total pool, billed avg (5 min)</div><div class="value" id="t-total">—</div><div class="note" id="n-total">—</div></div>
    <div class="tile"><div class="label">DeepSeek output, billed avg (5 min)</div><div class="value" id="t-output">—</div><div class="note" id="n-output">—</div></div>
    <div class="tile"><div class="label">Kimi uncached, billed avg (5 min)</div><div class="value" id="t-uncached">—</div><div class="note" id="n-uncached">—</div></div>
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
      <h2 id="h-workers">Workers</h2>
      <div class="legend" id="lg-workers"></div>
    </div>
    <div class="chart" id="c-workers"></div>
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

  <section class="card" aria-labelledby="h-jobs">
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
  </section>

  <section class="card" aria-labelledby="h-table">
    <div class="card-head">
      <h2 id="h-table">Minute data (table view)</h2>
      <div class="sub">Last 60 complete minutes in the range</div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Minute (UTC)</th><th class="num">Total pool %</th><th class="num">DeepSeek output %</th><th class="num">Kimi uncached %</th><th class="num">OK</th><th class="num">429s</th><th class="num">Wait p95 s</th></tr></thead>
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
  var POLL_MS = 15000;
  var state = { hours: 6, data: null, timer: null, inFlight: false, lastGood: null };
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

  function renderTiles(d) {
    var workers = d.workers || [];
    var last = workers.length ? workers[workers.length - 1] : null;
    var caps = d.caps || { deepseek: 64, kimi: 2 };
    setText("t-ds", last && last.ds_workers !== null ? count(last.ds_workers) : "—");
    setText("n-ds", "of cap " + caps.deepseek + (last ? " · sampled " + clock(last.ts * 1000) : " · no samples yet"));
    setText("t-kimi", last && last.kimi_workers !== null ? count(last.kimi_workers) : "—");
    setText("n-kimi", "of cap " + caps.kimi + (last ? " · sampled " + clock(last.ts * 1000) : " · no samples yet"));
    var queued = d.jobs && d.jobs.by_state ? d.jobs.by_state.queued || 0 : null;
    setText("t-queued", count(queued));
    setText("n-queued", "jobs in state queued now");
    setText("t-429", count(d.r429_60m));
    setText("n-429", "upstream responses (complete minutes)");
    var limits = d.limits || {};
    [["total", "t-total", "n-total"], ["output", "t-output", "n-output"], ["uncached", "t-uncached", "n-uncached"]].forEach(function (row) {
      var lim = limits[row[0]];
      setText(row[1], lim ? pct(lim.avg_5m_frac) : "—");
      var target = lim && lim.target_frac !== null && lim.target_frac !== undefined
        ? "proxy target " + pct(lim.target_frac, 0)
        : "proxy target unavailable";
      setText(row[2], target);
    });
  }

  function renderCharts(d) {
    var win = d.window || { start: 0, end: 0 };
    var minutes = d.minutes || [];
    var workers = (d.workers || []).map(function (w) { return { t: w.ts, ds: w.ds_workers, kimi: w.kimi_workers, queued: w.queued_jobs }; });
    var caps = d.caps || { deepseek: 64, kimi: 2 };
    var maxPct = minutes.reduce(function (a, p) {
      return Math.max(a, p.pct_total, p.pct_ds_output, p.pct_kimi_uncached);
    }, 0);
    var maxWorkers = workers.reduce(function (a, w) {
      return Math.max(a, w.ds || 0, w.kimi || 0, w.queued || 0);
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
      title: "Workers and queued jobs",
      kind: "line", x0: win.start, x1: win.end, height: 260,
      points: workers.map(function (w) { return { t: w.t, ds: w.ds, kimi: w.kimi, queued: w.queued }; }),
      series: [
        { key: "ds", name: "DeepSeek workers", color: COLORS.s1 },
        { key: "kimi", name: "Kimi workers", color: COLORS.s2 },
        { key: "queued", name: "Queued jobs", color: COLORS.s3 }
      ],
      y: niceTicks(Math.max(caps.deepseek, maxWorkers), 4),
      refs: [{ v: caps.deepseek, label: "DeepSeek cap " + caps.deepseek }],
      fmt: function (v) { return Math.round(v).toLocaleString("en-US"); }
    });
    legendFor($("lg-billed"), [
      { name: "Total pool", color: COLORS.s1 },
      { name: "DeepSeek output", color: COLORS.s2 },
      { name: "Kimi uncached", color: COLORS.s3 }
    ]);
    legendFor($("lg-workers"), [
      { name: "DeepSeek workers", color: COLORS.s1 },
      { name: "Kimi workers", color: COLORS.s2 },
      { name: "Queued jobs", color: COLORS.s3 }
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
      [p.minute, pct(p.pct_total), pct(p.pct_ds_output), pct(p.pct_kimi_uncached), count(p.ok), count(p.r429),
        p.wait_p95_ms === null ? "—" : (p.wait_p95_ms / 1000).toFixed(1)].forEach(function (v, i) {
        var td = document.createElement("td");
        if (i > 0) td.className = "num";
        td.textContent = String(v);
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
  }

  function render(d) {
    renderTiles(d);
    renderCharts(d);
    renderJobs(d);
    renderMinuteTable(d);
    var errs = Object.keys(d.errors || {});
    var status = $("status");
    status.innerHTML = "";
    var up = document.createElement("span");
    up.textContent = "Last updated " + clock(d.generated_at * 1000) + (errs.length ? " · partial: " + errs.join(", ") : "");
    status.appendChild(up);
  }

  function setStale(message) {
    var status = $("status");
    if (!message) return;
    var last = state.lastGood ? clock(state.lastGood) : "never";
    status.innerHTML = "";
    var warn = document.createElement("span");
    warn.className = "stale";
    warn.textContent = "Stale: fetch failed";
    status.appendChild(warn);
    status.appendChild(document.createTextNode(" · last good refresh " + last + " · " + message));
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
        render(data);
      })
      .catch(function (err) {
        setStale(err && err.message ? err.message : "network error");
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
        "ok": int(row.get("ok") or 0),
        "r429": int(row.get("r429") or 0),
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
    targets = live_targets(live)
    limits = {
        name: {
            "limit": LIMITS[name],
            "avg_5m_frac": average_fraction(raw, until, LIMIT_KEYS[name], LIMITS[name]),
            "target_frac": targets[name],
        }
        for name in LIMIT_KEYS
    }
    return {
        "generated_at": now,
        "hours": hours,
        "window": {"start": since, "end": until},
        "live": live,
        "limits": limits,
        "r429_60m": recent_429s(raw, until),
        "minutes": minute_series(raw, since, until),
        "jobs": jobs,
        "workers": samples,
        "caps": read_caps(home),
        "errors": errors,
    }


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
