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
RANGE_HOURS = (1, 6, 24, 168)
BUCKET_S = {1: 60, 6: 60, 24: 300, 168: 1800}
DEFAULT_HOURS = 6
RECENT_JOBS = 200
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
UNTAGGED_JOBS = frozenset({"", "none"})
UNTAGGED_LABEL = "untagged (pre-tagging workers)"
ACTIVE_WINDOW_S = 120
PERF_WINDOW_MINUTES = 15
RUN_LOOKBACK_S = 7 * 24 * 3600
RUN_ROWS = 200
TOP_CONSUMERS = 10
VERDICTS = ("PASS", "PARTIAL", "FAIL")
FAR_FUTURE = 4102444800.0
BUCKET_SUM_KEYS = ("total", "ds_output", "kimi_uncached", "ok", "r429", "req", "input", "cache_read", "output", "wait_p50_weighted")
PCT_KEYS = ("pct_total", "pct_ds_output", "pct_kimi_uncached")
WORKER_KEYS = ("ds_workers", "kimi_workers", "queue_workers", "direct_workers", "queued_jobs")
LOG_SOURCES = {"proxy": "proxy.err.log", "broker": "broker.log"}
LOG_PAGE = 100
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BLOCK = 64 * 1024
LOG_SECRET_MARKERS = ("authorization", "api-key", "api_key")
LOG_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\s?(.*)$")
LOG_COMPONENT = re.compile(r"^agentq[\w.-]*$")
LOG_PAIR = re.compile(r"^([A-Za-z_][\w.-]*)=(.*)$")
JOB_ID = re.compile(r"^[0-9a-f]{12}$")
CALIB_SQL = "SELECT minute, billed FROM proxy_calib WHERE minute >= ? AND minute < ?"
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
SELECT id, state, lane, tier, kind, verdict, label, submitted_at, started_at, finished_at
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
VERDICTS_SQL = """
SELECT verdict, COUNT(*) AS n FROM jobs
WHERE finished_at >= ? AND finished_at < ? AND verdict IS NOT NULL
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
main { max-width: 1280px; margin: 0 auto; padding: 20px 20px 40px; }
header { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 20px; justify-content: space-between; margin-bottom: 14px; }
h1 { font-size: 18px; font-weight: 600; margin: 0; }
h2 { font-size: 14px; font-weight: 600; margin: 0 0 2px; }
h3.group { font-size: 12px; font-weight: 600; color: var(--ink-2); text-transform: uppercase; letter-spacing: 0.04em; margin: 0 0 4px; }
.sub { color: var(--ink-2); font-size: 12px; }
.range, .seg { display: inline-flex; border: 1px solid var(--ring); border-radius: 6px; overflow: hidden; }
.range button, .seg button, .pager button {
  background: var(--surface); color: var(--ink-2); border: 0; padding: 5px 12px; font: inherit; font-size: 13px; cursor: pointer;
}
.range button + button, .seg button + button { border-left: 1px solid var(--ring); }
.range button[aria-pressed="true"], .seg button[aria-pressed="true"] { color: var(--ink); font-weight: 600; background: var(--page); }
.status { font-size: 12px; color: var(--ink-2); }
.status .stale { color: var(--crit); font-weight: 600; }

.panels { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 12px; margin-bottom: 12px; }
.panel { background: var(--surface); border: 1px solid var(--ring); border-radius: 8px; padding: 10px 12px 8px; min-width: 0; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0 14px; }
.kpi { border-top: 1px solid var(--grid); padding: 6px 0 7px; min-width: 0; }
.kpi.head { border-left: 2px solid var(--s1); padding-left: 8px; }
.kpi .label { color: var(--ink-2); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.kpi .value { font-size: 20px; font-weight: 600; line-height: 1.25; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.kpi .value.sm { font-size: 16px; }
.kpi .note { color: var(--ink-3); font-size: 11px; line-height: 1.3; }
.range-panel { margin-bottom: 14px; }
.range-panel .kpis { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }

.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 8px; padding: 14px 16px; margin-bottom: 14px; position: relative; }
.card-head { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px 16px; align-items: center; margin-bottom: 6px; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 12px; color: var(--ink-2); }
.legend .key { display: inline-flex; align-items: center; gap: 6px; }
.legend .swatch { width: 14px; height: 0; border-top: 2px solid; display: inline-block; }
.legend .block { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
.chart { position: relative; width: 100%; }
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
.tip .warn { color: var(--ink-3); white-space: normal; max-width: 260px; }
.grid3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 14px; }
.grid3 .card { margin-bottom: 0; }

.tbl-tools { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px; margin: 2px 0 8px; }
.filter input {
  font: inherit; font-size: 12px; padding: 4px 8px; width: 240px; max-width: 100%;
  border: 1px solid var(--ring); border-radius: 6px; background: var(--page); color: var(--ink);
}
.table-wrap { overflow: auto; max-height: 460px; border-top: 1px solid var(--grid); }
table { width: 100%; border-collapse: collapse; font-size: 12px; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
thead th { position: sticky; top: 0; z-index: 1; background: var(--surface); box-shadow: inset 0 -1px 0 var(--grid); }
th { color: var(--ink-2); font-weight: 600; }
th.sortable { cursor: pointer; user-select: none; }
th.sortable:hover { color: var(--ink); }
th.sorted::after { content: attr(data-dir); margin-left: 4px; color: var(--ink-3); }
td { color: var(--ink); }
td.num, th.num { text-align: right; }
td.wrap { white-space: normal; word-break: break-word; min-width: 36ch; }
td.wrap .cont { color: var(--ink-3); display: block; }
.ellipsis-inner { display: block; max-width: 28ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pager { display: flex; align-items: center; justify-content: flex-end; gap: 10px; font-size: 12px; color: var(--ink-2); margin-top: 8px; }
.pager button { border: 1px solid var(--ring); border-radius: 6px; padding: 3px 10px; font-size: 12px; }
.pager button:disabled { color: var(--ink-3); cursor: default; }
.note-line { color: var(--ink-3); font-size: 12px; margin-top: 6px; }
</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>agentq</h1>
      <div class="status" id="status" role="status" aria-live="polite">Loading</div>
    </div>
    <div class="range" role="group" aria-label="Time range">
      <button type="button" data-hours="1" aria-pressed="false">1h</button>
      <button type="button" data-hours="6" aria-pressed="true">6h</button>
      <button type="button" data-hours="24" aria-pressed="false">24h</button>
      <button type="button" data-hours="168" aria-pressed="false">7d</button>
    </div>
  </header>

  <div class="panels">
    <section class="panel" aria-labelledby="g-cap">
      <h3 class="group" id="g-cap">Capacity</h3>
      <div class="kpis">
        <div class="kpi head"><div class="label">Capacity used, 5 min</div><div class="value" id="cap-used">—</div><div class="note" id="cap-binding">—</div></div>
        <div class="kpi"><div class="label">DeepSeek output, 5 min avg</div><div class="value sm" id="cap-dso">—</div><div class="note" id="cap-dso-note">—</div></div>
        <div class="kpi"><div class="label">Total pool, last minute</div><div class="value sm" id="lim-total-now">—</div><div class="note" id="lim-total-note">—</div></div>
        <div class="kpi"><div class="label">DeepSeek output, last minute</div><div class="value sm" id="lim-output-now">—</div><div class="note" id="lim-output-note">—</div></div>
        <div class="kpi"><div class="label">Kimi uncached, last minute</div><div class="value sm" id="lim-uncached-now">—</div><div class="note" id="lim-uncached-note">—</div></div>
        <div class="kpi"><div class="label">Waiting at the proxy</div><div class="value" id="k-waiting">—</div><div class="note" id="k-waiting-note">—</div></div>
      </div>
    </section>
    <section class="panel" aria-labelledby="g-runs">
      <h3 class="group" id="g-runs">Runs</h3>
      <div class="kpis">
        <div class="kpi"><div class="label">DeepSeek runs active</div><div class="value" id="r-ds">—</div><div class="note" id="r-ds-note">—</div></div>
        <div class="kpi"><div class="label">Kimi runs active</div><div class="value" id="r-kimi">—</div><div class="note" id="r-kimi-note">—</div></div>
        <div class="kpi"><div class="label">Runs seen today</div><div class="value" id="r-seen">—</div><div class="note">distinct jobs, local day</div></div>
        <div class="kpi"><div class="label">Finished today</div><div class="value sm" id="r-finished">—</div><div class="note">PASS / PARTIAL / FAIL</div></div>
        <div class="kpi"><div class="label">Tokens today</div><div class="value" id="r-tokens">—</div><div class="note">all runs, local day</div></div>
        <div class="kpi"><div class="label">Median tokens per run</div><div class="value" id="r-median-tok">—</div><div class="note" id="r-median-req">—</div></div>
      </div>
    </section>
    <section class="panel" aria-labelledby="g-perf">
      <h3 class="group" id="g-perf">Performance</h3>
      <div class="kpis">
        <div class="kpi"><div class="label">Proxy wait, p50 / p95</div><div class="value sm" id="p-wait">—</div><div class="note">last 15 min; p95 is the worst minute</div></div>
        <div class="kpi"><div class="label">Upstream per request</div><div class="value sm" id="p-upstream">—</div><div class="note">median, runs active in 15 min</div></div>
        <div class="kpi"><div class="label">429 rate</div><div class="value sm" id="p-429rate">—</div><div class="note" id="p-429-note">—</div></div>
        <div class="kpi"><div class="label">Requests per minute</div><div class="value sm" id="p-rpm">—</div><div class="note">average, last 5 min</div></div>
      </div>
    </section>
  </div>

  <section class="panel range-panel" aria-labelledby="g-range">
    <h3 class="group" id="g-range">Selected range</h3>
    <div class="kpis">
      <div class="kpi"><div class="label">Total tokens</div><div class="value" id="rg-tokens">—</div><div class="note" id="rg-tokens-note">—</div></div>
      <div class="kpi"><div class="label">Requests</div><div class="value" id="rg-req">—</div><div class="note">proxy requests in range</div></div>
      <div class="kpi"><div class="label">429s absorbed</div><div class="value" id="rg-r429">—</div><div class="note">retried by the proxy</div></div>
      <div class="kpi"><div class="label">Runs started</div><div class="value" id="rg-started">—</div><div class="note">queue and direct</div></div>
      <div class="kpi"><div class="label">Finished, PASS / PARTIAL / FAIL</div><div class="value sm" id="rg-finished">—</div><div class="note">queue jobs with a verdict</div></div>
      <div class="kpi"><div class="label">DeepSeek output tokens</div><div class="value" id="rg-ds">—</div><div class="note">counted per finished request</div></div>
    </div>
  </section>

  <section class="card" aria-labelledby="h-billed">
    <div class="card-head">
      <h2 id="h-billed">Total pool and DeepSeek output, % of limit</h2>
      <div class="legend" id="lg-billed"></div>
    </div>
    <div class="chart" id="c-billed"></div>
    <div class="note-line">Tokens are counted in the minute each request finishes; Morph enforces only the 10M total per UTC minute. Output above 100% has never been rejected.</div>
    <div class="note-line">Total pool uses tokens admitted in each minute where the proxy recorded them, and falls back to completion minutes elsewhere.</div>
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

  <section class="grid3">
    <div class="card" aria-labelledby="h-kimi">
      <div class="card-head">
        <h2 id="h-kimi">Kimi uncached input, estimated % of 24,290/min</h2>
      </div>
      <div class="legend" style="margin-bottom: 4px"><span class="key"><span class="swatch" style="border-color: var(--s3)"></span>Kimi uncached (estimate)</span></div>
      <div class="chart" id="c-kimi"></div>
    </div>
    <div class="card" aria-labelledby="h-429">
      <div class="card-head">
        <h2 id="h-429">Upstream 429s per minute</h2>
      </div>
      <div class="legend" style="margin-bottom: 4px"><span class="key"><span class="block" style="background: var(--crit)"></span>429s</span></div>
      <div class="chart" id="c-429"></div>
    </div>
    <div class="card" aria-labelledby="h-wait">
      <div class="card-head">
        <h2 id="h-wait">Wait p95 per minute</h2>
      </div>
      <div class="legend" style="margin-bottom: 4px"><span class="key"><span class="swatch" style="border-color: var(--s1)"></span>p95 wait, seconds</span></div>
      <div class="chart" id="c-wait"></div>
    </div>
  </section>

  <section class="card" aria-labelledby="h-runs">
    <div class="card-head">
      <h2 id="h-runs">Runs</h2>
      <div class="sub" id="runs-count">Newest activity first; click a header to sort</div>
    </div>
    <div class="tbl-tools">
      <div class="filter"><input type="search" data-filter="runs" placeholder="Filter by run, tier or status" aria-label="Filter runs"></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr id="runs-head"></tr></thead>
        <tbody id="runs-body"></tbody>
      </table>
    </div>
    <div class="pager">
      <button type="button" data-pager="runs" data-step="-1">‹ Prev</button>
      <span id="runs-page">Page 1 of 1</span>
      <button type="button" data-pager="runs" data-step="1">Next ›</button>
    </div>
    <div class="note-line">Cache-read % is cache reads over all input tokens. Avg wait is per request; avg upstream is per successful request. Queue runs show their jobs-table state.</div>
  </section>

  <section class="card" aria-labelledby="h-top">
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
  </section>

  <section class="card" aria-labelledby="h-jobs">
    <div class="card-head">
      <h2 id="h-jobs">Recent jobs</h2>
      <div class="sub" id="jobs-counts"></div>
    </div>
    <div class="tbl-tools">
      <div class="filter"><input type="search" data-filter="jobs" placeholder="Filter by id, label, state or tier" aria-label="Filter jobs"></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr id="jobs-head"></tr></thead>
        <tbody id="jobs-body"></tbody>
      </table>
    </div>
    <div class="pager">
      <button type="button" data-pager="jobs" data-step="-1">‹ Prev</button>
      <span id="jobs-page">Page 1 of 1</span>
      <button type="button" data-pager="jobs" data-step="1">Next ›</button>
    </div>
  </section>

  <section class="card" aria-labelledby="h-table">
    <div class="card-head">
      <h2 id="h-table">Minute data (table view)</h2>
      <div class="sub" id="minute-sub">Newest first; one row per minute, or per bucket above 6 h</div>
    </div>
    <div class="tbl-tools">
      <div class="filter"><input type="search" data-filter="minute" placeholder="Filter by minute (HH:MM)" aria-label="Filter minute rows"></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr id="minute-head"></tr></thead>
        <tbody id="minute-body"></tbody>
      </table>
    </div>
    <div class="pager">
      <button type="button" data-pager="minute" data-step="-1">‹ Prev</button>
      <span id="minute-page">Page 1 of 1</span>
      <button type="button" data-pager="minute" data-step="1">Next ›</button>
    </div>
    <div class="note-line">Billed % is the share of each Morph limit for the minute. Minutes with no proxy row count as zero. Above 6 h each row is a bucket: counts are sums, wait p95 is the worst minute, and % is the mean over the bucket.</div>
  </section>

  <section class="card" aria-labelledby="h-log">
    <div class="card-head">
      <h2 id="h-log">Raw log</h2>
      <div class="seg" role="group" aria-label="Log source">
        <button type="button" data-src="proxy" aria-pressed="true">Proxy</button>
        <button type="button" data-src="broker" aria-pressed="false">Broker</button>
      </div>
    </div>
    <div class="tbl-tools">
      <div class="filter"><input type="search" id="log-q" placeholder="Filter lines" aria-label="Filter log lines"></div>
      <div class="sub" id="log-meta"></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Time</th><th>Event</th><th>Fields</th></tr></thead>
        <tbody id="log-body"></tbody>
      </table>
    </div>
    <div class="pager">
      <button type="button" id="log-prev">‹ Newer</button>
      <span id="log-page">Page 1 of 1</span>
      <button type="button" id="log-next">Older ›</button>
    </div>
    <div class="note-line">Newest first, 100 lines a page, read from the last 5 MB of the file. Lines that name an Authorization header or an api key are dropped.</div>
  </section>
</main>

<script>
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var POLL_MS = 30000;
  var SLOW_POLL_MS = 120000;
  var RANGE_NAME = { 1: "1h", 6: "6h", 24: "24h", 168: "7d" };
  var NAME_RANGE = { "1h": 1, "6h": 6, "24h": 24, "7d": 168 };
  var COLORS = { s1: "var(--s1)", s2: "var(--s2)", s3: "var(--s3)", ink: "var(--ink)" };
  var state = { hours: 6, data: null, seq: 0, lastFetch: 0, lastGood: null, stale: null, timer: null };
  var log = { source: "proxy", page: 1, q: "", seq: 0, data: null, qTimer: null };
  var charts = [];
  var tables = {};

  function $(id) { return document.getElementById(id); }
  function setText(id, value) { $(id).textContent = value; }
  function pad(n) { return n < 10 ? "0" + n : String(n); }
  function hm(sec) {
    var d = new Date(sec * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function dateHM(sec) {
    var d = new Date(sec * 1000);
    return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + hm(sec);
  }
  function fullTime(sec) {
    if (sec === null || sec === undefined) return "—";
    return dateHM(sec);
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
    var ticks = [];
    for (var i = 0; i <= steps; i++) ticks.push(+(unit * i).toFixed(6));
    return { min: 0, max: unit * steps, ticks: ticks };
  }
  function percentile(values, q) {
    if (!values.length) return 0;
    var sorted = values.slice().sort(function (a, b) { return a - b; });
    return sorted[Math.floor(q * (sorted.length - 1))];
  }
  function valuesOf(points, keys) {
    var out = [];
    points.forEach(function (p) {
      keys.forEach(function (k) { if (typeof p[k] === "number") out.push(p[k]); });
    });
    return out;
  }
  function clippedAxis(values, fmt) {
    var top = Math.max(1.5, 1.1 * percentile(values, 0.95));
    var ticks = niceTicks(top, 3).ticks.filter(function (v) { return v <= top; });
    return { min: 0, max: top, clip: top, ticks: ticks, fmt: fmt };
  }
  function legendFor(el, series) {
    el.innerHTML = "";
    series.forEach(function (s) {
      var key = document.createElement("span");
      key.className = "key";
      var sw = document.createElement("span");
      sw.className = s.block ? "block" : "swatch";
      if (s.block) sw.style.background = s.color;
      else sw.style.borderColor = s.color;
      key.appendChild(sw);
      key.appendChild(document.createTextNode(s.name));
      el.appendChild(key);
    });
  }
  function measure(svg, text, cls) {
    var probe = svgEl("text", { x: 0, y: 0, class: cls }, svg);
    probe.textContent = String(text);
    var width = probe.getComputedTextLength ? probe.getComputedTextLength() : 0;
    svg.removeChild(probe);
    return width || String(text).length * 6.5;
  }
  function emptyChart(host) {
    host.innerHTML = "";
    var empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No data in this range";
    host.appendChild(empty);
  }
  function xTicks(svg, cfg, iw, H, m, X, fmtX) {
    var sample = Math.max(measure(svg, fmtX(cfg.x0), "tick"), measure(svg, fmtX(cfg.x1), "tick"));
    var steps = Math.max(1, Math.min(6, Math.floor(iw / (sample * 1.5 + 8))));
    for (var i = 0; i <= steps; i++) {
      var t = cfg.x0 + ((cfg.x1 - cfg.x0) * i) / steps;
      var anchor = i === 0 ? "start" : i === steps ? "end" : "middle";
      svgEl("text", { x: X(t), y: H - 6, "text-anchor": anchor, class: "tick" }, svg).textContent = fmtX(t);
    }
  }
  function tooltipFor(host, svg, W, X, hitRect, cross, tip, onMove) {
    hitRect.addEventListener("mouseleave", function () {
      cross.setAttribute("visibility", "hidden");
      tip.style.display = "none";
    });
    hitRect.addEventListener("mousemove", function (ev) {
      var box = svg.getBoundingClientRect();
      onMove(ev.clientX - box.left);
    });
    return function place(x, top) {
      cross.setAttribute("x1", x); cross.setAttribute("x2", x);
      cross.setAttribute("visibility", "visible");
      tip.style.display = "block";
      var tipW = tip.offsetWidth || 160;
      var tipX = x + 12;
      if (tipX + tipW > W) tipX = x - tipW - 12;
      tip.style.left = Math.max(0, tipX) + "px";
      tip.style.top = (top || 8) + "px";
    };
  }
  function fillTip(tip, head, rows) {
    tip.innerHTML = "";
    var h = document.createElement("div");
    h.innerHTML = "<b></b>";
    h.firstChild.textContent = head;
    tip.appendChild(h);
    rows.forEach(function (r) {
      var row = document.createElement("div");
      row.className = "row";
      var dot = document.createElement("span");
      dot.className = "dot";
      dot.style.background = r.color;
      row.appendChild(dot);
      var txt = document.createElement("span");
      txt.textContent = r.text;
      row.appendChild(txt);
      tip.appendChild(row);
      if (r.note) {
        var note = document.createElement("div");
        note.className = "warn";
        note.textContent = r.note;
        tip.appendChild(note);
      }
    });
  }
  function endSeries(cfg, points) {
    var out = [];
    cfg.series.forEach(function (s) {
      for (var j = points.length - 1; j >= 0; j--) {
        var v = points[j][s.key];
        if (v !== null && v !== undefined) { out.push({ s: s, v: v, t: points[j].t }); return; }
      }
    });
    return out;
  }

  function drawChart(host, cfg) {
    host.innerHTML = "";
    var points = cfg.points || [];
    if (!points.length) { emptyChart(host); return; }
    var W = Math.max(260, Math.floor(host.clientWidth || 600));
    var H = cfg.height || 240;
    var svg = svgEl("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": cfg.title }, host);
    var fmtY = cfg.y.fmt || cfg.fmt || String;
    var fmtX = cfg.tickFmt || hm;
    var tickW = 0;
    cfg.y.ticks.forEach(function (v) { tickW = Math.max(tickW, measure(svg, fmtY(v), "tick")); });
    var ends = cfg.endLabels ? endSeries(cfg, points) : [];
    var endText = function (e) { return e.s.name + " " + fmtY(e.v, true); };
    var endW = 0;
    ends.forEach(function (e) { endW = Math.max(endW, measure(svg, endText(e), "direct")); });
    var m = {
      l: Math.max(44, Math.ceil(tickW) + 12),
      r: ends.length ? Math.min(Math.ceil(endW) + 24, Math.floor(W * 0.42)) : 12,
      t: 14,
      b: 24
    };
    var iw = W - m.l - m.r;
    var ih = H - m.t - m.b;
    var x0 = cfg.x0, x1 = cfg.x1;
    var plotBottom = m.t + ih;
    function X(t) { return m.l + ((t - x0) / Math.max(1, x1 - x0)) * iw; }
    function Y(v) { return m.t + ih - ((v - cfg.y.min) / (cfg.y.max - cfg.y.min || 1)) * ih; }
    function Yc(v) { return Y(cfg.y.clip !== undefined && v > cfg.y.clip ? cfg.y.clip : v); }

    cfg.y.ticks.forEach(function (v) {
      svgEl("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: v === cfg.y.min ? "var(--axis)" : "var(--grid)", "stroke-width": 1 }, svg);
      svgEl("text", { x: m.l - 8, y: Y(v) + 4, "text-anchor": "end", class: "tick" }, svg).textContent = fmtY(v);
    });
    xTicks(svg, cfg, iw, H, m, X, fmtX);

    var refs = (cfg.refs || []).slice();
    refs.forEach(function (r) {
      svgEl("line", { x1: m.l, x2: W - m.r, y1: Y(r.v), y2: Y(r.v), stroke: "var(--ink-3)", "stroke-width": 1, "stroke-dasharray": "5 4" }, svg);
    });
    var refPlaced = refs.map(function (r) { return { text: r.label, y: Y(r.v) - 5 }; })
      .sort(function (a, b) { return a.y - b.y; });
    refPlaced.forEach(function (r, i) {
      if (i && r.y - refPlaced[i - 1].y < 13) r.y = refPlaced[i - 1].y + 13;
      svgEl("text", {
        x: m.l + 6, y: r.y, class: "direct",
        style: "fill: var(--ink-3); stroke: var(--surface); stroke-width: 3px; paint-order: stroke"
      }, svg).textContent = r.text;
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
          d += (lastPoint ? " L" : " M") + X(p.t).toFixed(1) + "," + Yc(v).toFixed(1);
          lastPoint = p;
        });
        if (d) svgEl("path", { d: d.trim(), fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
        if (cfg.y.clip !== undefined) {
          points.forEach(function (p) {
            if (p[s.key] > cfg.y.clip) {
              var cx = X(p.t);
              svgEl("path", { d: "M" + (cx - 4) + "," + (m.t + 6) + " L" + cx + "," + m.t + " L" + (cx + 4) + "," + (m.t + 6) + " Z", fill: s.color }, svg);
            }
          });
        }
      });
    }

    if (ends.length) {
      var labelX = W - m.r + 10;
      var order = ends.map(function (e) { e.y = Yc(e.v); e.x = X(e.t); return e; })
        .sort(function (a, b) { return a.y - b.y; });
      var lo = m.t + 7, hi = plotBottom - 2;
      order.forEach(function (e, i) {
        e.c = i ? Math.max(e.y, order[i - 1].c + 14) : Math.max(e.y, lo);
      });
      for (var k = order.length - 1; k >= 0; k--) {
        var cap = k === order.length - 1 ? hi : order[k + 1].c - 14;
        if (order[k].c > cap) order[k].c = cap;
      }
      order.forEach(function (e) {
        if (Math.abs(e.c - e.y) > 1.5 || e.x < W - m.r - 2) {
          svgEl("line", { x1: e.x + 5, y1: e.y, x2: labelX - 5, y2: e.c, stroke: "var(--ink-3)", "stroke-width": 1 }, svg);
        }
        svgEl("circle", { cx: e.x, cy: e.y, r: 4, fill: e.s.color, stroke: "var(--surface)", "stroke-width": 2 }, svg);
        svgEl("text", { x: labelX, y: e.c + 4, class: "direct", style: "fill: var(--ink-2)" }, svg).textContent = endText(e);
      });
    }

    var cross = svgEl("line", { x1: 0, x2: 0, y1: m.t, y2: plotBottom, stroke: "var(--ink-3)", "stroke-width": 1, visibility: "hidden" }, svg);
    var hit = svgEl("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent" }, svg);
    var tip = document.createElement("div");
    tip.className = "tip";
    host.appendChild(tip);
    var place = tooltipFor(host, svg, W, X, hit, cross, tip, function (px) {
      var sec = x0 + ((px - m.l) / Math.max(1, iw)) * (x1 - x0);
      var p = nearest(points, sec);
      if (!p) return;
      cross.setAttribute("x1", X(p.t)); cross.setAttribute("x2", X(p.t));
      var span = cfg.bucket_s && cfg.bucket_s > 60 ? " – " + hm(p.t + cfg.bucket_s) : "";
      var rows = cfg.series.map(function (s) {
        var v = p[s.key];
        var text = s.name + ": " + (v === null || v === undefined ? "—" : fmtY(v, true));
        var note = s.overNote && typeof v === "number" && v > 1 ? s.overNote : null;
        return { color: s.color, text: text, note: note };
      });
      fillTip(tip, fmtX(p.t) + span, rows);
      place(X(p.t), 8);
    });
    hit.addEventListener("mouseenter", function () { cross.setAttribute("visibility", "visible"); });
    return svg;
  }

  function nearest(points, sec) {
    var best = null, bestD = Infinity;
    points.forEach(function (p) {
      var dist = Math.abs(p.t - sec);
      if (dist < bestD) { bestD = dist; best = p; }
    });
    return best;
  }

  function drawTokenCombo(host, cfg) {
    host.innerHTML = "";
    var points = cfg.points || [];
    if (!points.length) { emptyChart(host); return; }
    var W = Math.max(260, Math.floor(host.clientWidth || 600));
    var H = cfg.height || 280;
    var svg = svgEl("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": cfg.title }, host);
    var fmtM = function (v) { return (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + "M"; };
    var stackMax = points.reduce(function (a, p) { return Math.max(a, p.cache + p.uncached + p.output); }, 0);
    var cumMax = points.reduce(function (a, p) { return Math.max(a, p.cumulative); }, 0);
    var left = niceTicks(Math.max(stackMax, 0.1), 4);
    var right = niceTicks(Math.max(cumMax, 0.1), 4);
    var leftW = 0, rightW = 0;
    left.ticks.forEach(function (v) { leftW = Math.max(leftW, measure(svg, fmtM(v), "tick")); });
    right.ticks.forEach(function (v) { rightW = Math.max(rightW, measure(svg, fmtM(v), "tick")); });
    var m = { l: Math.max(44, Math.ceil(leftW) + 12), r: Math.max(44, Math.ceil(rightW) + 14), t: 26, b: 24 };
    var iw = W - m.l - m.r;
    var ih = H - m.t - m.b;
    var plotBottom = m.t + ih;
    var x0 = cfg.x0, x1 = cfg.x1;
    function X(t) { return m.l + ((t - x0) / Math.max(1, x1 - x0)) * iw; }
    function YL(v) { return plotBottom - (v / (left.max || 1)) * ih; }
    function YR(v) { return plotBottom - (v / (right.max || 1)) * ih; }

    left.ticks.forEach(function (v) {
      svgEl("line", { x1: m.l, x2: W - m.r, y1: YL(v), y2: YL(v), stroke: v === 0 ? "var(--axis)" : "var(--grid)", "stroke-width": 1 }, svg);
      svgEl("text", { x: m.l - 8, y: YL(v) + 4, "text-anchor": "end", class: "tick" }, svg).textContent = fmtM(v);
    });
    right.ticks.forEach(function (v) {
      svgEl("text", { x: W - m.r + 8, y: YR(v) + 4, "text-anchor": "start", class: "tick" }, svg).textContent = fmtM(v);
    });
    svgEl("text", { x: m.l, y: 12, class: "tick" }, svg).textContent = "tokens per " + cfg.unit + " (M)";
    svgEl("text", { x: W - m.r, y: 12, "text-anchor": "end", class: "tick" }, svg).textContent = "cumulative tokens (M)";
    xTicks(svg, cfg, iw, H, m, X, cfg.tickFmt || hm);

    var band = iw / Math.max(1, points.length);
    var bw = Math.min(24, Math.max(2, band * 0.6));
    var stacks = [
      { key: "cache", color: COLORS.s3 },
      { key: "uncached", color: COLORS.s1 },
      { key: "output", color: COLORS.s2 }
    ];
    points.forEach(function (p) {
      var base = 0;
      var top = 0;
      var topIdx = -1;
      stacks.forEach(function (s, idx) { if ((p[s.key] || 0) > 0) topIdx = idx; });
      stacks.forEach(function (s, idx) {
        var v = p[s.key] || 0;
        if (v <= 0) return;
        base = top;
        top = top + v;
        var yTop = YL(top), yBase = YL(base);
        var h = Math.max(0, yBase - yTop);
        var cx = X(p.t);
        var l = cx - bw / 2, r = cx + bw / 2;
        var rad = Math.min(4, bw / 2, h);
        var d;
        if (idx === topIdx && rad > 0) {
          d = "M" + l + "," + yBase + " L" + l + "," + (yTop + rad) + " Q" + l + "," + yTop + " " + (l + rad) + "," + yTop +
            " L" + (r - rad) + "," + yTop + " Q" + r + "," + yTop + " " + r + "," + (yTop + rad) + " L" + r + "," + yBase + " Z";
        } else {
          d = "M" + l + "," + yBase + " L" + l + "," + yTop + " L" + r + "," + yTop + " L" + r + "," + yBase + " Z";
        }
        svgEl("path", { d: d, fill: s.color, stroke: "var(--surface)", "stroke-width": 2 }, svg);
      });
    });

    var line = "";
    points.forEach(function (p, idx) {
      line += (idx ? " L" : "M") + X(p.t).toFixed(1) + "," + YR(p.cumulative).toFixed(1);
    });
    if (line) {
      svgEl("path", { d: line, fill: "none", stroke: "var(--surface)", "stroke-width": 6, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
      svgEl("path", { d: line, fill: "none", stroke: COLORS.ink, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    }
    var lastP = points[points.length - 1];
    svgEl("circle", { cx: X(lastP.t), cy: YR(lastP.cumulative), r: 4, fill: COLORS.ink, stroke: "var(--surface)", "stroke-width": 2 }, svg);

    var cross = svgEl("line", { x1: 0, x2: 0, y1: m.t, y2: plotBottom, stroke: "var(--ink-3)", "stroke-width": 1, visibility: "hidden" }, svg);
    var hit = svgEl("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent" }, svg);
    var tip = document.createElement("div");
    tip.className = "tip";
    host.appendChild(tip);
    var place = tooltipFor(host, svg, W, X, hit, cross, tip, function (px) {
      var sec = x0 + ((px - m.l) / Math.max(1, iw)) * (x1 - x0);
      var p = nearest(points, sec);
      if (!p) return;
      var span = cfg.bucket_s && cfg.bucket_s > 60 ? " – " + hm(p.t + cfg.bucket_s) : "";
      fillTip(tip, (cfg.tickFmt || hm)(p.t) + span, [
        { color: COLORS.s3, text: "Cache read: " + fmtM(p.cache) },
        { color: COLORS.s1, text: "Uncached input: " + fmtM(p.uncached) },
        { color: COLORS.s2, text: "Output: " + fmtM(p.output) },
        { color: COLORS.ink, text: "Cumulative (right axis): " + fmtM(p.cumulative) }
      ]);
      place(X(p.t), 8);
    });
    return svg;
  }

  function registerChart(hostId, cfg, draw) {
    var host = $(hostId);
    if (cfg.fmt && cfg.y && !cfg.y.fmt) cfg.y.fmt = cfg.fmt;
    var entry = { host: host, cfg: cfg, draw: draw || drawChart, w: 0 };
    charts.push(entry);
    entry.draw(host, cfg);
    entry.w = host.clientWidth;
  }

  function redrawEntry(entry) { entry.draw(entry.host, entry.cfg); }

  function unitLabel(bucket) {
    return bucket === 60 ? "minute" : bucket / 60 + " min";
  }

  function renderCapacity(d) {
    var cap = d.capacity || {};
    var limits = d.limits || {};
    setText("cap-used", pct(cap.used_frac));
    setText("cap-binding", cap.binding_label ? "binding: " + cap.binding_label : "no traffic in 5 min");
    var dso = d.ds_output_5m_per_min;
    setText("cap-dso", dso === null || dso === undefined ? "—" : tokens(dso) + "/min");
    setText("cap-dso-note", "stated limit " + tokens((limits.output || {}).limit) + "/min");
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
    setText("r-median-req", "median " + num1(rk.median_requests_per_run) + " requests");
  }

  function renderPerf(d) {
    var pk = d.perf_kpis || {};
    var p50 = pk.wait_p50_ms === null || pk.wait_p50_ms === undefined ? "—" : seconds(pk.wait_p50_ms);
    setText("p-wait", p50 + " / " + seconds(pk.wait_p95_ms));
    setText("p-upstream", seconds(pk.upstream_median_ms));
    setText("p-429rate", pct(pk.r429_rate_60m, 2));
    var r429 = d.r429_60m;
    setText("p-429-note", (r429 === undefined ? "—" : count(r429)) + " 429s in the last 60 min");
    setText("p-rpm", num1(pk.requests_per_min_5m));
  }

  function renderRange(d) {
    var rt = d.range_totals || {};
    var tok = rt.tokens || {};
    setText("g-range", "Selected range, " + (RANGE_NAME[d.hours] || ""));
    setText("rg-tokens", tokens(tok.total));
    setText("rg-tokens-note", "cache " + tokens(tok.cache_read) + " · uncached " + tokens(tok.uncached) + " · output " + tokens(tok.output));
    setText("rg-req", count(rt.requests));
    setText("rg-r429", count(rt.r429));
    setText("rg-started", count(rt.runs_started));
    var fin = rt.finished || {};
    setText("rg-finished", ["PASS", "PARTIAL", "FAIL"].map(function (v) { return count(fin[v] || 0); }).join(" / "));
    setText("rg-ds", tokens(rt.ds_output));
  }

  function renderCharts(d) {
    var win = d.window || { start: 0, end: 0 };
    var bucket = d.bucket_s || 60;
    var unit = unitLabel(bucket);
    var fmtX = d.hours >= 168 ? dateHM : hm;
    var minutes = d.minutes || [];
    var caps = d.caps || { deepseek: 64, kimi: 2 };
    var workers = (d.workers || []).map(function (w) {
      return { t: w.ts, ds: w.ds_workers, kimi: w.kimi_workers, queued: w.queued_jobs };
    });
    var maxWorkers = workers.reduce(function (a, w) {
      return Math.max(a, w.ds || 0, w.kimi || 0, w.queued || 0);
    }, 0);
    var pctFmt = function (v, inline) { return inline ? (v * 100).toFixed(1) + "%" : Math.round(v * 100) + "%"; };
    var intFmt = function (v) { return Math.round(v).toLocaleString("en-US"); };
    charts.length = 0;

    registerChart("c-billed", {
      title: "Total pool and DeepSeek output, percent of limit per interval",
      kind: "line", endLabels: true, bucket_s: bucket, tickFmt: fmtX,
      x0: win.start, x1: win.end, height: 260,
      points: minutes,
      series: [
        { key: "pct_total", name: "Total pool", color: COLORS.s1 },
        { key: "pct_ds_output", name: "DeepSeek output vs stated 70k/min (not enforced per minute)", color: COLORS.s2,
          overNote: "above stated limit, not rejected by Morph (0 output-limit 429s)" }
      ],
      y: clippedAxis(valuesOf(minutes, ["pct_total", "pct_ds_output"]), pctFmt),
      refs: [{ v: 1, label: "100% of limit" }],
      fmt: pctFmt
    });
    setText("h-billed", "Total pool and DeepSeek output, % of limit");
    registerChart("c-workers", {
      title: "Active runs over time",
      kind: "line", endLabels: true, bucket_s: bucket, tickFmt: fmtX,
      x0: win.start, x1: win.end, height: 260,
      points: workers,
      series: [
        { key: "ds", name: "DeepSeek runs", color: COLORS.s1 },
        { key: "kimi", name: "Kimi runs", color: COLORS.s2 },
        { key: "queued", name: "Queued jobs", color: COLORS.s3 }
      ],
      y: niceTicks(Math.max(caps.deepseek, maxWorkers), 4),
      refs: [{ v: caps.deepseek, label: "DeepSeek cap " + caps.deepseek }, { v: caps.kimi, label: "Kimi cap " + caps.kimi }],
      fmt: intFmt
    });
    var mix = (d.token_mix || []).map(function (p) {
      return {
        t: p.t, cache: p.cache_read / 1e6, uncached: p.uncached / 1e6, output: p.output / 1e6,
        cumulative: p.cumulative / 1e6
      };
    });
    registerChart("c-tokens", {
      title: "Tokens per interval, millions, with cumulative total",
      unit: unit, bucket_s: bucket, tickFmt: fmtX, x0: win.start, x1: win.end, height: 280,
      points: mix
    }, drawTokenCombo);
    setText("h-tokens", "Tokens per " + unit + ", millions");
    legendFor($("lg-billed"), [
      { name: "Total pool", color: COLORS.s1 },
      { name: "DeepSeek output vs stated 70k/min (not enforced per minute)", color: COLORS.s2 }
    ]);
    legendFor($("lg-workers"), [
      { name: "DeepSeek runs", color: COLORS.s1 },
      { name: "Kimi runs", color: COLORS.s2 },
      { name: "Queued jobs", color: COLORS.s3 }
    ]);
    legendFor($("lg-tokens"), [
      { name: "Cache read", color: COLORS.s3, block: true },
      { name: "Uncached input", color: COLORS.s1, block: true },
      { name: "Output", color: COLORS.s2, block: true },
      { name: "Cumulative (right axis)", color: COLORS.ink }
    ]);
    registerChart("c-kimi", {
      title: "Kimi uncached input, estimated percent of limit per minute",
      kind: "line", bucket_s: bucket, tickFmt: fmtX, x0: win.start, x1: win.end, height: 200,
      points: minutes,
      series: [{ key: "pct_kimi_uncached", name: "Kimi uncached (estimate)", color: COLORS.s3 }],
      y: clippedAxis(valuesOf(minutes, ["pct_kimi_uncached"]), pctFmt),
      refs: [{ v: 1, label: "100% of limit" }],
      fmt: pctFmt
    });
    setText("h-kimi", "Kimi uncached input, estimated % of 24,290/min");
    var maxR = minutes.reduce(function (a, p) { return Math.max(a, p.r429); }, 0);
    registerChart("c-429", {
      title: "Upstream 429s per " + unit,
      kind: "bar", bucket_s: bucket, tickFmt: fmtX, x0: win.start, x1: win.end, height: 200,
      points: minutes,
      series: [{ key: "r429", name: "429s", color: "var(--crit)" }],
      y: niceTicks(Math.max(maxR, 1), 3),
      fmt: intFmt
    });
    setText("h-429", "Upstream 429s per " + unit);
    var maxW = minutes.reduce(function (a, p) { return p.wait_p95_ms === null ? a : Math.max(a, p.wait_p95_ms / 1000); }, 0);
    var waitPoints = minutes.map(function (p) {
      return { t: p.t, wait: p.wait_p95_ms === null || p.wait_p95_ms === undefined ? null : p.wait_p95_ms / 1000 };
    });
    registerChart("c-wait", {
      title: "Wait p95 seconds per " + unit,
      kind: "line", bucket_s: bucket, tickFmt: fmtX, x0: win.start, x1: win.end, height: 200,
      points: waitPoints,
      series: [{ key: "wait", name: "p95 wait", color: COLORS.s1 }],
      y: niceTicks(Math.max(maxW, 1), 3),
      fmt: function (v, inline) { return v.toFixed(inline ? 1 : 0) + " s"; }
    });
    setText("h-wait", "Wait p95 per " + unit);
    setText("minute-sub", bucket === 60 ? "Newest first; one row per minute" : "Newest first; one row per " + unit + " bucket");
  }

  var RUN_COLUMNS = [
    { label: "Run", text: true, ellipsis: true, value: function (r) { return r.run; }, cell: function (r) { return r.run; } },
    { label: "Tier / model", text: true, value: function (r) { return (r.tier || "") + " " + r.models; }, cell: function (r) { return (r.tier ? r.tier + " · " : "") + r.models; } },
    { label: "Status", text: true, value: function (r) { return r.status; }, cell: function (r) { return r.status; } },
    { label: "Started", value: function (r) { return r.started; }, cell: function (r) { return fullTime(r.started); } },
    { label: "Duration", num: true, value: function (r) { return r.duration_s; }, cell: function (r) { return duration(r.duration_s); } },
    { label: "Requests", num: true, value: function (r) { return r.requests; }, cell: function (r) { return count(r.requests); } },
    { label: "Total tokens", num: true, value: function (r) { return r.total_tokens; }, cell: function (r) { return tokens(r.total_tokens); } },
    { label: "Output tokens", num: true, value: function (r) { return r.output; }, cell: function (r) { return tokens(r.output); } },
    { label: "Cache-read %", num: true, value: function (r) { return r.cache_read_pct; }, cell: function (r) { return pct(r.cache_read_pct, 0); } },
    { label: "429s", num: true, value: function (r) { return r.r429; }, cell: function (r) { return count(r.r429); } },
    { label: "Avg wait s", num: true, value: function (r) { return r.avg_wait_s; }, cell: function (r) { return r.avg_wait_s === null ? "—" : r.avg_wait_s.toFixed(2); } },
    { label: "Avg upstream s", num: true, value: function (r) { return r.avg_upstream_s; }, cell: function (r) { return r.avg_upstream_s === null ? "—" : r.avg_upstream_s.toFixed(1); } },
    { label: "Verdict", text: true, value: function (r) { return r.verdict; }, cell: function (r) { return r.verdict || "—"; } }
  ];
  var JOB_COLUMNS = [
    { label: "Id", text: true, value: function (r) { return r.id; }, cell: function (r) { return r.id; } },
    { label: "Label", text: true, ellipsis: true, value: function (r) { return r.label; }, cell: function (r) { return r.label || "—"; } },
    { label: "State", text: true, value: function (r) { return r.state; }, cell: function (r) { return r.state; } },
    { label: "Lane", text: true, value: function (r) { return r.lane; }, cell: function (r) { return r.lane; } },
    { label: "Tier", text: true, value: function (r) { return r.tier; }, cell: function (r) { return r.tier; } },
    { label: "Kind", text: true, value: function (r) { return r.kind; }, cell: function (r) { return r.kind; } },
    { label: "Verdict", text: true, value: function (r) { return r.verdict; }, cell: function (r) { return r.verdict || "—"; } },
    { label: "Submitted", value: function (r) { return r.submitted_at; }, cell: function (r) { return fullTime(r.submitted_at); } },
    { label: "Started", value: function (r) { return r.started_at; }, cell: function (r) { return fullTime(r.started_at); } },
    { label: "Finished", value: function (r) { return r.finished_at; }, cell: function (r) { return fullTime(r.finished_at); } }
  ];
  var MINUTE_COLUMNS = [
    { label: "Start (UTC)", text: true, value: function (r) { return r.t; }, cell: function (r) { return r.minute; } },
    { label: "Total pool %", num: true, value: function (r) { return r.pct_total; }, cell: function (r) { return pct(r.pct_total); } },
    { label: "DeepSeek output %", num: true, value: function (r) { return r.pct_ds_output; }, cell: function (r) { return pct(r.pct_ds_output); } },
    { label: "Kimi uncached %", num: true, value: function (r) { return r.pct_kimi_uncached; }, cell: function (r) { return pct(r.pct_kimi_uncached); } },
    { label: "Tokens", num: true, value: function (r) { return r.total; }, cell: function (r) { return tokens(r.total); } },
    { label: "OK", num: true, value: function (r) { return r.ok; }, cell: function (r) { return count(r.ok); } },
    { label: "429s", num: true, value: function (r) { return r.r429; }, cell: function (r) { return count(r.r429); } },
    { label: "Wait p95 s", num: true, value: function (r) { return r.wait_p95_ms; }, cell: function (r) { return r.wait_p95_ms === null ? "—" : (r.wait_p95_ms / 1000).toFixed(1); } }
  ];

  function defineTable(id, opts) {
    var t = {
      id: id, columns: opts.columns, match: opts.match, pinLast: !!opts.pinLast,
      pageSize: 25, sort: null, page: 1, q: "", rows: [], empty: "No rows"
    };
    tables[id] = t;
    var input = document.querySelector('input[data-filter="' + id + '"]');
    if (input) {
      input.addEventListener("input", function () {
        t.q = input.value.trim().toLowerCase();
        t.page = 1;
        paint(t);
      });
    }
    document.querySelectorAll('button[data-pager="' + id + '"]').forEach(function (b) {
      b.addEventListener("click", function () {
        t.page += Number(b.getAttribute("data-step"));
        paint(t);
      });
    });
    return t;
  }

  function sortRows(t, rows) {
    if (!t.sort) return rows;
    var col = t.columns[t.sort.index];
    var dir = t.sort.dir;
    return rows.slice().sort(function (a, b) {
      var va = col.value(a), vb = col.value(b);
      var na = va === null || va === undefined, nb = vb === null || vb === undefined;
      if (na || nb) return na === nb ? 0 : na ? 1 : -1;
      if (typeof va === "string" || typeof vb === "string") return String(va).localeCompare(String(vb)) * dir;
      return (va - vb) * dir;
    });
  }

  function paint(t) {
    var head = $(t.id + "-head");
    var body = $(t.id + "-body");
    head.innerHTML = "";
    t.columns.forEach(function (col, index) {
      var th = document.createElement("th");
      th.className = "sortable" + (col.num ? " num" : "");
      th.textContent = col.label;
      var active = t.sort && t.sort.index === index;
      if (active) {
        th.className += " sorted";
        th.setAttribute("data-dir", t.sort.dir > 0 ? "▲" : "▼");
      }
      th.setAttribute("aria-sort", active ? (t.sort.dir > 0 ? "ascending" : "descending") : "none");
      th.addEventListener("click", function () {
        if (t.sort && t.sort.index === index) t.sort = { index: index, dir: -t.sort.dir };
        else t.sort = { index: index, dir: col.text ? 1 : -1 };
        t.page = 1;
        paint(t);
      });
      head.appendChild(th);
    });
    var rows = t.rows;
    if (t.q) rows = rows.filter(function (r) { return t.match(r).toLowerCase().indexOf(t.q) !== -1; });
    rows = sortRows(t, rows);
    if (t.pinLast) rows = rows.filter(function (r) { return !r.untagged; }).concat(rows.filter(function (r) { return r.untagged; }));
    var pages = Math.max(1, Math.ceil(rows.length / t.pageSize));
    t.page = Math.min(Math.max(1, t.page), pages);
    var slice = rows.slice((t.page - 1) * t.pageSize, t.page * t.pageSize);
    body.innerHTML = "";
    if (!slice.length) {
      var tr = document.createElement("tr");
      var td = document.createElement("td");
      td.colSpan = t.columns.length;
      td.textContent = t.q ? "No rows match the filter" : t.empty;
      tr.appendChild(td);
      body.appendChild(tr);
    }
    slice.forEach(function (r) {
      var tr = document.createElement("tr");
      t.columns.forEach(function (col) {
        var td = document.createElement("td");
        td.className = (col.num ? "num" : "");
        if (col.ellipsis) {
          td.title = String(col.cell(r));
          var inner = document.createElement("span");
          inner.className = "ellipsis-inner";
          inner.textContent = String(col.cell(r));
          td.appendChild(inner);
        } else {
          td.textContent = String(col.cell(r));
        }
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    setText(t.id + "-page", "Page " + t.page + " of " + pages + " · " + rows.length + (rows.length === 1 ? " row" : " rows"));
    document.querySelectorAll('button[data-pager="' + t.id + '"]').forEach(function (b) {
      var step = Number(b.getAttribute("data-step"));
      b.disabled = step < 0 ? t.page <= 1 : t.page >= pages;
    });
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

  function renderRuns(d) {
    var runs = d.runs || [];
    var t = tables.runs;
    t.rows = runs;
    t.empty = d.errors && d.errors.runs ? "Runs unavailable: " + d.errors.runs : "No runs in the last 7 days";
    setText("runs-count", runs.length + " runs in the last 7 days; untagged runs listed last");
    paint(t);
  }

  function renderJobs(d) {
    var jobs = d.jobs || {};
    var t = tables.jobs;
    t.rows = jobs.recent || [];
    t.empty = jobs.recent ? "No jobs yet" : "Jobs unavailable";
    var byState = jobs.by_state || {};
    var parts = Object.keys(byState).sort().map(function (k) { return k + " " + byState[k]; });
    setText("jobs-counts", parts.length ? "All jobs: " + parts.join(" · ") : "");
    paint(t);
  }

  function renderMinuteTable(d) {
    var t = tables.minute;
    t.rows = (d.minutes || []).slice().reverse();
    t.empty = "No minutes in this range";
    paint(t);
  }

  function renderLog() {
    var body = $("log-body");
    body.innerHTML = "";
    var d = log.data || {};
    var rows = d.rows || [];
    if (!rows.length) {
      var tr = document.createElement("tr");
      var td = document.createElement("td");
      td.colSpan = 3;
      td.textContent = d.error ? d.error : (log.q ? "No lines match the filter" : "No log lines");
      tr.appendChild(td);
      body.appendChild(tr);
    }
    rows.forEach(function (r) {
      var tr = document.createElement("tr");
      var time = document.createElement("td");
      time.textContent = r.time || "—";
      tr.appendChild(time);
      var ev = document.createElement("td");
      ev.textContent = r.event || "";
      tr.appendChild(ev);
      var fields = document.createElement("td");
      fields.className = "wrap";
      var pairs = r.fields.map(function (f) { return f.k + "=" + f.v; }).join(" ");
      fields.textContent = [pairs, r.text].filter(function (x) { return x; }).join(" ");
      r.continuation.forEach(function (line) {
        var c = document.createElement("span");
        c.className = "cont";
        c.textContent = line;
        fields.appendChild(c);
      });
      tr.appendChild(fields);
      body.appendChild(tr);
    });
    var page = d.page || 1, pages = d.pages || 1;
    setText("log-page", "Page " + page + " of " + pages + " · " + (d.total || 0) + " lines");
    $("log-prev").disabled = page <= 1;
    $("log-next").disabled = page >= pages;
    var meta = d.file || "";
    if (d.truncated) meta += " · last 5 MB";
    if (d.redacted_lines) meta += " · " + d.redacted_lines + " secret lines dropped";
    setText("log-meta", meta);
  }

  function refreshLog() {
    var seq = ++log.seq;
    var url = "/api/log?source=" + encodeURIComponent(log.source) + "&page=" + log.page + "&q=" + encodeURIComponent(log.q);
    fetch(url, { cache: "no-store" })
      .then(function (res) { return res.json(); })
      .then(function (body) {
        if (seq !== log.seq) return;
        log.data = body;
        renderLog();
      })
      .catch(function () { /* keep the last page on screen */ });
  }

  function renderCapacityAndRest(d) {
    renderCapacity(d);
    renderRunKpis(d);
    renderPerf(d);
    renderRange(d);
  }

  function render(d) {
    renderCapacityAndRest(d);
    renderCharts(d);
    renderRuns(d);
    renderTop(d);
    renderJobs(d);
    renderMinuteTable(d);
    renderStatus();
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

  function refresh() {
    var seq = ++state.seq;
    var hours = state.hours;
    state.lastFetch = Date.now();
    fetch("/api/summary?hours=" + hours, { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        if (seq !== state.seq) return;
        state.data = data;
        state.lastGood = Date.now();
        state.stale = null;
        render(data);
      })
      .catch(function (err) {
        if (seq !== state.seq) return;
        state.stale = err && err.message ? err.message : "network error";
        renderStatus();
      });
    refreshLog();
  }

  function setRangeButtons() {
    document.querySelectorAll(".range button").forEach(function (btn) {
      btn.setAttribute("aria-pressed", Number(btn.getAttribute("data-hours")) === state.hours ? "true" : "false");
    });
  }

  function startPolling() {
    stopPolling();
    state.timer = setInterval(function () {
      if (document.visibilityState !== "visible") return;
      if (state.hours === 168 && Date.now() - state.lastFetch < SLOW_POLL_MS - 1000) return;
      refresh();
    }, POLL_MS);
  }
  function stopPolling() {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
  }

  document.querySelectorAll(".range button").forEach(function (btn) {
    btn.addEventListener("click", function () {
      state.hours = Number(btn.getAttribute("data-hours"));
      setRangeButtons();
      if (window.history && window.history.replaceState) {
        window.history.replaceState(null, "", "?range=" + RANGE_NAME[state.hours]);
      }
      refresh();
    });
  });

  document.querySelectorAll("button[data-src]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      log.source = btn.getAttribute("data-src");
      log.page = 1;
      document.querySelectorAll("button[data-src]").forEach(function (other) {
        other.setAttribute("aria-pressed", other === btn ? "true" : "false");
      });
      refreshLog();
    });
  });
  $("log-prev").addEventListener("click", function () { log.page = Math.max(1, log.page - 1); refreshLog(); });
  $("log-next").addEventListener("click", function () { log.page += 1; refreshLog(); });
  $("log-q").addEventListener("input", function (ev) {
    clearTimeout(log.qTimer);
    var value = ev.target.value.trim();
    log.qTimer = setTimeout(function () {
      log.q = value;
      log.page = 1;
      refreshLog();
    }, 300);
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
    var observer = new ResizeObserver(function (entries) {
      entries.forEach(function (en) {
        var w = Math.round(en.contentRect.width);
        charts.forEach(function (c) {
          if (c.host === en.target && c.w !== w) {
            c.w = w;
            redrawEntry(c);
          }
        });
      });
    });
    document.querySelectorAll(".chart").forEach(function (h) { observer.observe(h); });
  }

  defineTable("runs", { columns: RUN_COLUMNS, pinLast: true, match: function (r) { return [r.run, r.tier, r.status].join(" "); } });
  defineTable("jobs", { columns: JOB_COLUMNS, match: function (r) { return [r.id, r.label, r.state, r.lane, r.tier, r.kind, r.verdict].join(" "); } });
  defineTable("minute", { columns: MINUTE_COLUMNS, match: function (r) { return r.minute; } });

  var initial = new URLSearchParams(window.location.search).get("range");
  if (initial && NAME_RANGE[initial]) state.hours = NAME_RANGE[initial];
  setRangeButtons();
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
    """Complete minutes from the first recorded one through the last complete minute that has a row.

    The proxy writes a row only for minutes with traffic, so a minute between
    rows with no row is a minute with zero billed use. Trailing minutes with no
    row are left out: they may simply not be flushed yet, so they are not zero.
    The first recorded minute bounds the series so the window before the proxy
    started stays empty.
    """
    if not raw:
        return []
    first = max(min(raw), since)
    stamped = [t for t in range(first, until, 60) if t in raw]
    if not stamped:
        return []
    return [minute_point(t, raw.get(t)) for t in range(first, stamped[-1] + 60, 60)]


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


def last_complete_minute(raw: dict[int, dict], until: int) -> int | None:
    """The minute the "last minute" tiles read: the minute before ``until`` when it has a row, else the one before that.

    The proxy flushes a minute's row shortly after the minute ends, so the newest complete minute may not have one yet.
    """
    for stamp in (until - 60, until - 120):
        if stamp in raw:
            return stamp
    return None


def last_minute_fraction(raw: dict[int, dict], until: int, key: str, limit: int) -> float | None:
    """Billed fraction of a limit in the last complete minute that has a row; None when neither recent minute has one."""
    stamp = last_complete_minute(raw, until)
    if stamp is None:
        return None
    return fraction(int(raw[stamp].get(key) or 0), limit)


def window_sum(raw: dict[int, dict], until: int, minutes: int, key: str) -> int:
    """Sum of one minute-row field over the last ``minutes`` complete minutes."""
    return sum(int((raw.get(t) or {}).get(key) or 0) for t in range(until - minutes * 60, until, 60))


def deepseek_output_rate(raw: dict[int, dict], until: int) -> float:
    """Average DeepSeek output tokens per minute over the last LIMIT_WINDOW_MINUTES complete minutes."""
    return window_sum(raw, until, LIMIT_WINDOW_MINUTES, "ds_output") / LIMIT_WINDOW_MINUTES


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


def read_verdicts(agentq_db: Path, since: float, until: float | None = None) -> dict[str, int]:
    """Queue jobs finished in [since, until) by verdict; no upper bound when ``until`` is None. Read-only."""
    connection = readonly_connection(agentq_db)
    try:
        rows = connection.execute(VERDICTS_SQL, (since, FAR_FUTURE if until is None else until)).fetchall()
    finally:
        connection.close()
    return {row["verdict"]: row["n"] for row in rows}


def read_calib(agentq_db: Path, since: int, until: int) -> dict[int, int]:
    """Tokens admitted per UTC minute from proxy_calib, keyed by epoch second. Read-only; empty when the table is absent."""
    connection = readonly_connection(agentq_db)
    try:
        rows = connection.execute(CALIB_SQL, (minute_stamp(since), minute_stamp(until))).fetchall()
    finally:
        connection.close()
    return {minute_epoch(row["minute"]): int(row["billed"] or 0) for row in rows}


def apply_admitted_totals(points: list[dict], admitted: dict[int, int]) -> None:
    """Charge the total pool from admitted tokens (proxy_calib.billed) where a minute has one.

    Minutes without a calib row keep the completion-minute total from proxy_minutes, and each
    point says which one it used in ``total_source``.
    """
    for point in points:
        if point["t"] in admitted:
            point["total_source"] = "admitted"
            point["pct_total"] = fraction(admitted[point["t"]], LIMITS["total"])
        else:
            point["total_source"] = "completed"


def bucket_minutes(points: list[dict], size: int) -> list[dict]:
    """Minute points grouped into buckets of ``size`` seconds.

    Counts are summed, the p95 wait is the worst minute, and each % of limit is the mean over
    the bucket's minutes. Sizes of one minute return the points unchanged.
    """
    if size <= 60:
        return points
    groups: dict[int, list[dict]] = {}
    for point in points:
        groups.setdefault(point["t"] - point["t"] % size, []).append(point)
    buckets = []
    for start in sorted(groups):
        members = groups[start]
        p95s = [m["wait_p95_ms"] for m in members if m["wait_p95_ms"] is not None]
        bucket = {"t": start, "minute": minute_stamp(start), "minutes": len(members)}
        for key in BUCKET_SUM_KEYS:
            bucket[key] = sum(m[key] for m in members)
        bucket["wait_p95_ms"] = max(p95s) if p95s else None
        for key in PCT_KEYS:
            bucket[key] = round(sum(m[key] for m in members) / len(members), 4)
        buckets.append(bucket)
    return buckets


def bucket_workers(samples: list[dict], size: int) -> list[dict]:
    """Worker samples averaged per bucket of ``size`` seconds; ``None`` values are left out of the mean."""
    if size <= 60:
        return samples
    groups: dict[int, list[dict]] = {}
    for sample in samples:
        groups.setdefault(int(sample["ts"] // size) * size, []).append(sample)
    rows = []
    for start in sorted(groups):
        row: dict = {"ts": float(start)}
        for key in WORKER_KEYS:
            values = [sample[key] for sample in groups[start] if sample[key] is not None]
            row[key] = round(sum(values) / len(values), 2) if values else None
        rows.append(row)
    return rows


def range_totals(raw: dict[int, dict], since: int, until: int, runs: list[dict], verdicts: dict[str, int]) -> dict:
    """Totals over the selected range: tokens by kind, requests, 429s absorbed, runs started and queue verdicts finished."""
    rows = [raw[t] for t in range(since, until, 60) if t in raw]

    def total(key: str) -> int:
        return sum(int(row.get(key) or 0) for row in rows)

    return {
        "tokens": {
            "total": total("total"),
            "cache_read": total("cache_read"),
            "uncached": total("input"),
            "output": total("output"),
        },
        "requests": total("req"),
        "r429": total("r429"),
        "ds_output": total("ds_output"),
        "runs_started": sum(1 for run in runs if run["started"] is not None and since <= run["started"] < until),
        "finished": {verdict: int(verdicts.get(verdict, 0)) for verdict in VERDICTS},
    }


def parse_log_line(line: str) -> dict | None:
    """A timestamped log line as time, event, key=value fields and leftover text; None for a continuation line.

    A leading agentq component name and a bare 12-hex job id are read as fields or dropped, so the
    event is the first bare word after them.
    """
    match = LOG_TIME.match(line)
    if match is None:
        return None
    tokens = match.group(2).split()
    if tokens and LOG_COMPONENT.match(tokens[0]):
        tokens.pop(0)
    event: str | None = None
    fields: list[dict] = []
    rest: list[str] = []
    for token in tokens:
        pair = LOG_PAIR.match(token)
        if pair:
            fields.append({"k": pair.group(1), "v": pair.group(2)})
        elif JOB_ID.match(token) and not any(f["k"] == "job" for f in fields):
            fields.append({"k": "job", "v": token})
        elif event is None and not rest:
            event = token
        else:
            rest.append(token)
    return {"time": match.group(1), "event": event or "", "fields": fields, "text": " ".join(rest)}


def is_secret_line(line: str) -> bool:
    """True for a line that names an Authorization header or an api key; such lines are never shown."""
    lowered = line.lower()
    return any(marker in lowered for marker in LOG_SECRET_MARKERS)


def log_entries(text: str) -> tuple[list[dict], int]:
    """Logical log rows in file order. Lines without a timestamp attach to the row before them.

    A timestamped line that is secret drops its own continuation lines too. Returns the rows and
    the count of dropped lines.
    """
    entries: list[dict] = []
    dropped = 0
    skipping = False
    for line in text.splitlines():
        if not line.strip():
            continue
        parsed = parse_log_line(line)
        if parsed is not None:
            skipping = False
            if is_secret_line(line):
                dropped += 1
                skipping = True
                continue
            entries.append({**parsed, "continuation": []})
            continue
        if is_secret_line(line):
            dropped += 1
            continue
        if skipping:
            continue
        if entries:
            entries[-1]["continuation"].append(line)
        else:
            entries.append({"time": None, "event": "", "fields": [], "text": line, "continuation": []})
    return entries, dropped


def read_log_tail(path: Path, max_bytes: int = LOG_MAX_BYTES) -> tuple[bytes, bool]:
    """The last ``max_bytes`` of a file, read backwards in blocks. Returns the bytes and whether the file was cut."""
    with open(path, "rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        start = max(0, size - max_bytes)
        chunks: list[bytes] = []
        pos = size
        while pos > start:
            step = min(LOG_BLOCK, pos - start)
            pos -= step
            handle.seek(pos)
            chunks.append(handle.read(step))
    data = b"".join(reversed(chunks))
    truncated = start > 0
    if truncated:
        cut = data.find(b"\n")
        data = data[cut + 1:] if cut >= 0 else b""
    return data, truncated


def read_log(home: Path, source: str, page: int = 1, q: str = "") -> dict:
    """One page of a log, newest first. ``source`` must be a key of LOG_SOURCES; no path is ever taken from a request.

    Only ``$home/logs`` is read, and a file that resolves outside it is refused.
    """
    if source not in LOG_SOURCES:
        raise ValueError("source must be proxy or broker")
    logs_dir = (Path(home) / "logs").resolve()
    rel = f"logs/{LOG_SOURCES[source]}"
    base = {"source": source, "file": rel, "per_page": LOG_PAGE, "q": q, "truncated": False, "redacted_lines": 0}
    try:
        resolved = (logs_dir / LOG_SOURCES[source]).resolve(strict=True)
    except FileNotFoundError:
        return {**base, "page": 1, "pages": 1, "total": 0, "rows": [], "error": "log file not found"}
    if logs_dir not in resolved.parents:
        return {**base, "page": 1, "pages": 1, "total": 0, "rows": [], "error": "log path is outside the logs folder"}
    data, truncated = read_log_tail(resolved)
    entries, dropped = log_entries(data.decode("utf-8", errors="replace"))
    needle = q.strip().lower()
    if needle:
        entries = [e for e in entries if needle in row_text(e).lower()]
    entries.reverse()
    pages = max(1, -(-len(entries) // LOG_PAGE))
    page = min(max(1, page), pages)
    rows = entries[(page - 1) * LOG_PAGE: page * LOG_PAGE]
    return {
        **base,
        "q": needle,
        "page": page,
        "pages": pages,
        "total": len(entries),
        "truncated": truncated,
        "redacted_lines": dropped,
        "rows": rows,
        "error": None,
    }


def row_text(entry: dict) -> str:
    """The searchable text of one log row."""
    pairs = " ".join(f"{f['k']}={f['v']}" for f in entry["fields"])
    return " ".join([entry["time"] or "", entry["event"], pairs, entry["text"], *entry["continuation"]])


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
    untagged = row["job"] in UNTAGGED_JOBS
    return {
        "job": row["job"],
        "run": UNTAGGED_LABEL if untagged else (row["label"] or row["job"]),
        "untagged": untagged,
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
    """Tokens per interval split into cache read, uncached input and output, plus the running total from the range start."""
    running = 0
    mix = []
    for point in minutes:
        running += point["cache_read"] + point["input"] + point["output"]
        mix.append(
            {
                "t": point["t"],
                "minute": point["minute"],
                "cache_read": point["cache_read"],
                "uncached": point["input"],
                "output": point["output"],
                "cumulative": running,
            }
        )
    return mix


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
    bucket = BUCKET_S[hours]
    raw = _guard("minutes", errors, read_minute_rows, {}, agentq_db, since, until)
    admitted = _guard("admitted", errors, read_calib, {}, agentq_db, since, until)
    live = _guard("live", errors, fetch_live, None)
    jobs = _guard("jobs", errors, read_jobs, None, agentq_db)
    samples = _guard("workers", errors, read_samples, [], home / DASH_DB_NAME, float(since))
    day = day_start(now)
    run_rows = _guard("runs", errors, read_run_rows, [], agentq_db, int(now - RUN_LOOKBACK_S))
    verdicts = _guard("verdicts", errors, read_verdicts, {}, agentq_db, day)
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
    minute_points = minute_series(raw, since, until)
    apply_admitted_totals(minute_points, admitted)
    minutes = bucket_minutes(minute_points, bucket)
    range_verdicts = _guard("range_verdicts", errors, read_verdicts, {}, agentq_db, float(since), float(until))
    kpis = run_kpis(runs, day, caps, verdicts)
    return {
        "generated_at": now,
        "hours": hours,
        "window": {"start": since, "end": until},
        "live": live,
        "limits": limits,
        "capacity": capacity_headline(limits, live),
        "r429_60m": recent_429s(raw, until),
        "ds_output_5m_per_min": deepseek_output_rate(raw, until),
        "bucket_s": bucket,
        "minutes": minutes,
        "token_mix": token_mix(minutes),
        "range_totals": range_totals(raw, since, until, runs, range_verdicts),
        "jobs": jobs,
        "workers": bucket_workers(samples, bucket),
        "caps": caps,
        "runs": table_runs(runs),
        "run_kpis": kpis,
        "perf_kpis": perf_kpis(raw, until, runs, now),
        "top_consumers": top_consumers(runs, day, kpis["tokens_today"]),
        "errors": errors,
    }


def shape_runs(rows: list[dict], now: float) -> list[dict]:
    """Every run in the lookback, most recent activity first, with the untagged aggregate last."""
    return sorted((shape_run(row, now) for row in rows), key=lambda run: run["untagged"])


def table_runs(runs: list[dict]) -> list[dict]:
    """The rows the runs table shows: the newest RUN_ROWS tagged runs, then the untagged row, which is never cut."""
    tagged = [run for run in runs if not run["untagged"]]
    return tagged[:RUN_ROWS] + [run for run in runs if run["untagged"]]


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
        query = parse_qs(parts.query)
        if parts.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE_HTML.encode("utf-8"))
        elif parts.path == "/api/summary":
            requested = query.get("hours", [None])[0]
            if requested is None:
                hours = DEFAULT_HOURS
            elif requested.isdigit() and int(requested) in RANGE_HOURS:
                hours = int(requested)
            else:
                self._send_json(400, {"error": "hours must be one of 1, 6, 24, 168"})
                return
            summary = build_summary(self.server.home, hours, fetch_live=self.server.fetch_live)
            self._send_json(200, summary)
        elif parts.path == "/api/log":
            source = query.get("source", ["proxy"])[0]
            requested = query.get("page", ["1"])[0]
            if source not in LOG_SOURCES:
                self._send_json(400, {"error": "source must be proxy or broker"})
                return
            if not requested.isdigit():
                self._send_json(400, {"error": "page must be a positive integer"})
                return
            q = query.get("q", [""])[0][:200]
            self._send_json(200, read_log(self.server.home, source, int(requested), q))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _send_json(self, status: int, body: dict) -> None:
        self._send(status, "application/json", json.dumps(body).encode("utf-8"))

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
