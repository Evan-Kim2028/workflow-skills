"""Live agentq dashboard on 127.0.0.1: one HTML page and a JSON summary.

The page polls ``/api/summary``. The summary reads the proxy's live status
and ``agentq.db`` (read-only) and the samples this module records once a
minute into its own ``dash.db``. ``agentq.db`` is never written here.
Stdlib only.
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import faulthandler
import json
import math
import os
import re
import signal
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
TOKEN_BUCKET_S = {1: 300, 6: 300, 24: 300, 168: 1800}
AVERAGE_SATURATION_FRAC = 0.90
ROOM_FRAC = 0.95
POOL_NEAR_CAPACITY_FRAC = 0.85
MORPH_429_MINUTES = 2
TTFB_SLOW_FACTOR = 1.8
TTFB_SLOW_MINUTES = 3
LAPTOP_BAR_DEFAULTS = {
    "tests_max_cpu_busy": 0.80, "tests_min_free_pct": 35.0, "light_max_cpu_busy": 0.92,
    "light_min_free_pct": 20.0, "max_swap_frac": 0.90,
}
MEMORY_CACHE_S = 15.0
MEMORY_LINE = re.compile(r"System-wide memory free percentage:\s*(\d+(?:\.\d+)?)%")
BUDGET_FIELDS = {"total": "total", "output": "ds_output", "uncached": "kimi_uncached", "uncached_ds": "ds_uncached"}
DEEPSEEK_FAMILY = "morph-dsv41flash"
ROOM_BOUND_LABELS = {
    "laptop": "bounded by laptop CPU",
    "total": "bounded by Morph total",
    "uncached_ds": "bounded by Morph uncached input",
}
HEADLINE_AVERAGE_BUDGETS = ("total",)
HEADLINE_429_BUDGETS = ("total", "uncached_ds")
VERDICT_CHIPS = {
    "morph_limit": "Morph limit",
    "morph_slow": "Morph slow",
    "laptop": "Laptop",
    "near_capacity": "Near capacity",
    "proxy_pacing": "Proxy pacing",
    "filling": "Near target",
    "launching": "Launching",
    "headroom": "Headroom",
    "held_laptop": "Laptop",
    "held_ssh": "SSH slots",
    "held_proxy": "Proxy pacing",
    "held_workspace": "Workspace",
    "held_other": "Held",
}
HOLD_DETAIL = re.compile(r"^hold ([^:]+): (.*)$", re.DOTALL)
CPU_HOLD = re.compile(r"cpu (\d+)% busy >= (\d+)%")
MEMORY_HOLD = re.compile(r"memory free (\d+)% < (\d+)%")
SWAP_HOLD = re.compile(r"swap (\d+)% used >= (\d+)%")
HOLD_GROUP_ORDER = ("laptop", "ssh", "proxy", "workspace", "other")
HOLD_GROUP_KEYS = {group: f"held_{group}" for group in HOLD_GROUP_ORDER}
RATE_WINDOW_MINUTES = 60
SAMPLE_INTERVAL_S = 60.0
SAMPLE_KEEP_S = 7 * 24 * 3600
LIVE_TIMEOUT_S = 2.0
RANGE_CADENCE_S = {1: 5.0, 6: 5.0, 24: 15.0, 168: 60.0}
READER_CADENCE_S = 5.0
IDLE_AFTER_S = 120.0
WORKER_TICK_S = 1.0
REQUEST_BUDGET_S = 0.8
BACKGROUND_BUDGET_S = 30.0
WATCHDOG_FACTOR = 3.0
GONE_LOG_S = 60.0
DB_TIMEOUT_S = 2.0
PS_TIMEOUT_S = 5.0
ERROR_CHARS = 200
LIMITS = {"total": 10_000_000, "output": 70_000, "uncached": 24_290, "uncached_ds": 400_000}
LIMIT_KEYS = {"total": "total", "output": "ds_output", "uncached": "kimi_uncached", "uncached_ds": "ds_uncached"}
DEFAULT_CAPS = {"deepseek": 64, "kimi": 2}
TIER_CAP_KEYS = {"pi-deepseek-flash": "deepseek", "pi-kimi": "kimi"}
LIMIT_LABELS = {
    "total": "Total pool",
    "output": "DeepSeek output",
    "uncached": "Kimi uncached",
    "uncached_ds": "DeepSeek uncached input",
}
UNTAGGED_JOBS = frozenset({"", "none"})
UNTAGGED_LABEL = "untagged (pre-tagging workers)"
ACTIVE_WINDOW_S = 120
RUN_LOOKBACK_S = 7 * 24 * 3600
RUN_ROWS = 200
TOP_CONSUMERS = 10
VERDICTS = ("PASS", "PARTIAL", "FAIL")
FAR_FUTURE = 4102444800.0
BUCKET_SUM_KEYS = ("total", "ds_output", "kimi_uncached", "ds_uncached", "ok", "r429", "req", "input", "cache_read", "output", "wait_p50_weighted")
PCT_KEYS = ("pct_total", "pct_ds_output", "pct_kimi_uncached", "pct_ds_uncached")
WORKER_KEYS = ("ds_workers", "kimi_workers", "queue_workers", "direct_workers", "queued_jobs", "ds_queued", "kimi_queued", "queued_unsplit")
DS_TIER = "pi-deepseek-flash"
KIMI_TIER = "pi-kimi"
SAMPLE_COLUMNS = ("ts", "ds_workers", "kimi_workers", "queue_workers", "direct_workers", "queued_jobs", "ds_queued", "kimi_queued")
SPLIT_COLUMNS = ("ds_queued", "kimi_queued")
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
    queued_jobs INT,
    ds_queued INT,
    kimi_queued INT
)
"""
MINUTES_SQL = """
SELECT minute,
       SUM(COALESCE(input, 0) + COALESCE(cache_read, 0) + COALESCE(output, 0)) AS total,
       SUM(CASE WHEN model LIKE 'morph-dsv%' THEN COALESCE(input, 0) + COALESCE(cache_read, 0) + COALESCE(output, 0)
                ELSE 0 END) AS ds_total,
       SUM(CASE WHEN model LIKE 'morph-dsv%' THEN COALESCE(output, 0) ELSE 0 END) AS ds_output,
       SUM(CASE WHEN model LIKE 'morph-kimi%' THEN COALESCE(input, 0) ELSE 0 END) AS kimi_uncached,
       SUM(CASE WHEN model LIKE 'morph-dsv%' THEN COALESCE(input, 0) ELSE 0 END) AS ds_uncached,
       SUM(COALESCE(ok, 0)) AS ok,
       SUM(COALESCE(r429, 0)) AS r429,
       SUM(COALESCE(req, 0)) AS req,
       SUM(COALESCE(input, 0)) AS input,
       SUM(COALESCE(cache_read, 0)) AS cache_read,
       SUM(COALESCE(output, 0)) AS output,
       SUM(COALESCE(wait_p50_ms, 0) * COALESCE(req, 0)) AS wait_p50_weighted,
       MAX(wait_p95_ms) AS wait_p95_ms,
       {upstream_ms_sum} AS upstream_ms_sum,
       {upstream_n} AS upstream_n,
       {upstream_p50_weighted} AS upstream_p50_weighted,
       {upstream_p50_weight} AS upstream_p50_weight,
       {upstream_p95_ms} AS upstream_p95_ms,
       {ds_p50_weighted} AS ds_p50_weighted,
       {ds_p50_weight} AS ds_p50_weight,
       {ds_ttfb_p50_ms} AS ds_ttfb_p50_ms,
       {inflight_limit} AS inflight_limit
FROM proxy_minutes
WHERE minute >= ? AND minute < ?
GROUP BY minute
"""
JOBS_COUNT_SQL = "SELECT state, tier, COUNT(*) AS n FROM jobs GROUP BY state, tier"
QUEUED_DS_DETAIL_SQL = """
SELECT detail FROM jobs
WHERE state = 'queued' AND tier = 'pi-deepseek-flash' AND detail IS NOT NULL AND detail != ''
"""
RUNNING_BY_RES_SQL = "SELECT res, COUNT(*) AS n FROM jobs WHERE state = 'running' GROUP BY res"
FILLER_RUNNING_SQL = "SELECT COUNT(*) AS n FROM jobs WHERE state = 'running' AND label LIKE '%-backlog-filler-%'"
JOBS_RECENT_SQL = """
SELECT id, state, lane, tier, kind, verdict, label, submitted_at, started_at, finished_at
FROM jobs ORDER BY submitted_at DESC LIMIT ?
"""
SAMPLE_INSERT_SQL = (
    f"INSERT OR REPLACE INTO samples ({', '.join(SAMPLE_COLUMNS)}) VALUES ({', '.join('?' * len(SAMPLE_COLUMNS))})"
)
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
.status .aged { color: #c77700; font-weight: 600; }

.panels { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 12px; margin-bottom: 12px; }
.panel { background: var(--surface); border: 1px solid var(--ring); border-radius: 8px; padding: 10px 12px 8px; min-width: 0; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0 14px; }
.kpi { border-top: 1px solid var(--grid); padding: 6px 0 7px; min-width: 0; }
.kpi.head { border-left: 2px solid var(--s1); padding-left: 8px; }
.kpi .label { color: var(--ink-2); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.kpi .value { font-size: 20px; font-weight: 600; line-height: 1.25; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.kpi .value.sm { font-size: 16px; }
.kpi .note { color: var(--ink-3); font-size: 11px; line-height: 1.3; }
.verdict { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; font-size: 16px; font-weight: 600; }
.verdict .text { font-weight: 400; color: var(--ink); }
.chip { display: inline-block; border: 1px solid var(--ring); border-radius: 999px; padding: 2px 10px; font-size: 12px; font-weight: 600; color: var(--ink-2); }
.chip.crit { color: var(--crit); border-color: var(--crit); }
.chip.ok { color: var(--s3); border-color: var(--s3); }
.room { margin-top: 6px; font-size: 14px; color: var(--ink-2); }
.activity { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 12px; }
.stat { background: var(--surface); border: 1px solid var(--ring); border-radius: 8px; padding: 10px 12px; min-width: 0; }
.stat .label { color: var(--ink-2); font-size: 11px; }
.stat .value { font-size: 22px; font-weight: 600; line-height: 1.25; white-space: nowrap; }
.stat .note { color: var(--ink-3); font-size: 11px; }
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
.grid3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 14px; }
.grid3 .card { margin-bottom: 0; }
.grid3 .note-line { min-height: 35px; margin-top: 0; }
.pair { display: grid; grid-template-columns: 1fr; gap: 14px; }
.pair .card { margin-bottom: 0; }
.pair .card-head { min-height: 58px; align-items: flex-start; }
.pair .legend { min-height: 18px; }
.pair .pair-note { grid-column: 1 / -1; margin-top: 0; }
@media (min-width: 1100px) { .pair { grid-template-columns: 1fr 1fr; } }

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

  <section class="panel verdict-panel" aria-labelledby="g-verdict">
    <h3 class="group" id="g-verdict">Limited by</h3>
    <div class="verdict"><span class="chip" id="v-chip">—</span><span class="text" id="v-text">Loading</span></div>
    <div class="room" id="v-secondary"></div>
    <div class="room" id="v-room">—</div>
  </section>

  <h3 class="group" id="g-now">Now</h3>
  <div class="activity" aria-label="Now">
    <div class="stat"><div class="label">Tokens, last 5 min</div><div class="value" id="a5-tokens">—</div><div class="note" id="a5-note">—</div><div class="note" id="a5-min">—</div></div>
    <div class="stat"><div class="label">Tokens, last 15 min</div><div class="value" id="a15-tokens">—</div><div class="note" id="a15-note">—</div><div class="note" id="a15-min">—</div></div>
    <div class="stat"><div class="label">DeepSeek output, 5 min avg per min</div><div class="value" id="a-ds">—</div><div class="note">tokens per minute, plain count</div></div>
    <div class="stat"><div class="label">Waiting at the proxy</div><div class="value" id="k-waiting">—</div><div class="note" id="k-waiting-note">—</div></div>
    <div class="stat"><div class="label">DeepSeek in flight / limit</div><div class="value" id="p-inflight">—</div><div class="note">sent upstream, not yet answered; limit is the DeepSeek cap (shadow: the cap it would set)</div></div>
    <div class="stat"><div class="label">DeepSeek runs active</div><div class="value" id="r-ds">—</div><div class="note" id="r-ds-note">—</div></div>
    <div class="stat"><div class="label">Jobs queued</div><div class="value" id="r-queued">—</div><div class="note">waiting for a worker</div></div>
    <div class="stat"><div class="label">Filler runs</div><div class="value" id="r-filler">—</div><div class="note">backlog audits running now</div></div>
  </div>

  <div class="panels">
    <section class="panel" aria-labelledby="g-cap">
      <h3 class="group" id="g-cap">Capacity, <span class="rng">last 6 h</span></h3>
      <div class="kpis">
        <div class="kpi head"><div class="label">Total pool, avg <span class="rng">last 6 h</span></div><div class="value sm" id="cap-used">—</div><div class="note" id="cap-binding">—</div></div>
        <div class="kpi"><div class="label">DeepSeek uncached, avg <span class="rng">last 6 h</span></div><div class="value sm" id="cap-ds-unc">—</div><div class="note" id="cap-ds-unc-note">—</div></div>
        <div class="kpi"><div class="label">Morph response p50 / p95</div><div class="value sm" id="p-morph">—</div><div class="note"><span class="rng">last 6 h</span>, per request; p95 is the worst minute</div></div>
      </div>
    </section>
    <section class="panel" aria-labelledby="g-runs">
      <h3 class="group" id="g-runs">Work, <span class="rng">last 6 h</span></h3>
      <div class="kpis">
        <div class="kpi"><div class="label">Finished, <span class="rng">last 6 h</span></div><div class="value sm" id="r-finished">—</div><div class="note">PASS / PARTIAL / FAIL</div></div>
        <div class="kpi"><div class="label">Tokens, <span class="rng">last 6 h</span></div><div class="value" id="r-tokens">—</div><div class="note">all runs, billed</div></div>
        <div class="kpi"><div class="label">429s absorbed, <span class="rng">last 6 h</span></div><div class="value" id="r-429">—</div><div class="note">retried by the proxy</div></div>
      </div>
    </section>
  </div>

  <section class="group-block" aria-labelledby="g-capacity">
    <h3 class="group" id="g-capacity">Capacity vs workers</h3>
    <div class="pair">
      <section class="card" aria-labelledby="h-billed">
        <div class="card-head">
          <h2 id="h-billed">Morph total-token limit used per minute</h2>
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
      <div class="note-line pair-note">The left chart is total pool tokens per minute as a share of Morph's 10M limit: admitted tokens where the proxy recorded them, completion minutes elsewhere. The faint line is each minute; the bold line is the trailing 5-minute average of those minutes (the mean of that minute and the four before it). The dashed line is the limit; carets mark values above the 120% axis top, and the tooltip shows the true values.</div>
    </div>
  </section>

  <section class="card" aria-labelledby="h-tokens">
    <div class="card-head">
      <h2 id="h-tokens">Tokens billed per 5 min</h2>
      <div class="legend" id="lg-tokens"></div>
    </div>
    <div class="chart" id="c-tokens"></div>
    <div class="note-line" id="tok-mix">Mix: —</div>
  </section>

  <section class="grid3">
    <div class="card" aria-labelledby="h-kimi">
      <div class="card-head"><h2 id="h-kimi">Kimi uncached (est.)</h2></div>
      <div class="note-line"><span class="rng">last 6 h</span></div>
      <div class="chart" id="c-kimi"></div>
    </div>
    <div class="card" aria-labelledby="h-dsu">
      <div class="card-head"><h2 id="h-dsu">DeepSeek uncached (est.)</h2></div>
      <div class="note-line" id="dsu-sub">—</div>
      <div class="chart" id="c-dsu"></div>
    </div>
    <div class="card" aria-labelledby="h-429">
      <div class="card-head"><h2 id="h-429">Morph 429s</h2></div>
      <div class="note-line"><span class="rng">last 6 h</span></div>
      <div class="chart" id="c-429"></div>
    </div>
    <div class="card" aria-labelledby="h-wait">
      <div class="card-head"><h2 id="h-wait">Proxy wait p95</h2></div>
      <div class="note-line"><span class="rng">last 6 h</span></div>
      <div class="chart" id="c-wait"></div>
    </div>
  </section>

  <section class="card" aria-labelledby="h-upstream">
    <div class="card-head">
      <h2 id="h-upstream">Morph response time per request (s), p50 and p95</h2>
      <div class="legend"><span class="key"><span class="swatch" style="border-color: var(--s1)"></span>p50</span><span class="key"><span class="swatch" style="border-color: var(--s2)"></span>p95</span></div>
    </div>
    <div class="chart" id="c-upstream"></div>
    <div class="note-line" id="up-note"></div>
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
      <h2 id="h-top">Top consumers, <span class="rng">last 6 h</span></h2>
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
        <thead><tr><th>Time (UTC)</th><th>Event</th><th>Fields</th></tr></thead>
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
  var POLL_MS_BY_HOURS = { 1: 5000, 6: 5000, 24: 15000, 168: 60000 };
  var LOG_POLL_MS = 15000;
  var RANGE_NAME = { 1: "1h", 6: "6h", 24: "24h", 168: "7d" };
  var RANGE_LABEL = { 1: "last 1 h", 6: "last 6 h", 24: "last 24 h", 168: "last 7 d" };
  var NAME_RANGE = { "1h": 1, "6h": 6, "24h": 24, "7d": 168 };
  var COLORS = { s1: "var(--s1)", s2: "var(--s2)", s3: "var(--s3)", ink: "var(--ink)", muted: "var(--ink-3)" };
  var state = { hours: 6, data: null, seq: 0, lastFetch: 0, stale: null, timer: null };
  var log = { source: "proxy", page: 1, q: "", seq: 0, data: null, qTimer: null, fetchedAt: 0 };
  var charts = [];
  var tables = {};

  function $(id) { return document.getElementById(id); }
  function setText(id, value) { $(id).textContent = value; }
  function pad(n) { return n < 10 ? "0" + n : String(n); }
  var ET_FORMAT = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", hourCycle: "h23", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit"
  });
  function etParts(sec) {
    var parts = {};
    ET_FORMAT.formatToParts(new Date(sec * 1000)).forEach(function (p) { parts[p.type] = p.value; });
    return parts;
  }
  function hm(sec) { var p = etParts(sec); return p.hour + ":" + p.minute + " ET"; }
  function hms(sec) { var p = etParts(sec); return p.hour + ":" + p.minute + ":" + p.second + " ET"; }
  function dateHM(sec) { var p = etParts(sec); return p.month + "-" + p.day + " " + p.hour + ":" + p.minute + " ET"; }
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
  function truncateTo(v, digits) {
    var f = Math.pow(10, digits);
    return (Math.floor(v * f + 1e-9) / f).toFixed(digits);
  }
  function compact(n) {
    if (n === null || n === undefined) return "—";
    var a = Math.abs(n), sign = n < 0 ? "-" : "";
    var units = [[1e9, "B"], [1e6, "M"], [1e3, "K"]];
    for (var i = 0; i < units.length; i++) {
      if (a >= units[i][0]) {
        var v = a / units[i][0];
        return sign + truncateTo(v, v >= 100 ? 0 : 1) + units[i][1];
      }
    }
    return sign + String(Math.floor(a));
  }
  function exactCount(n) {
    return n === null || n === undefined ? "—" : Number(n).toLocaleString("en-US");
  }
  function setTile(id, n) {
    var el = $(id);
    el.textContent = compact(n);
    el.title = exactCount(n);
  }
  function setTileList(id, values) {
    var el = $(id);
    el.textContent = values.map(function (v) { return compact(v); }).join(" / ");
    el.title = values.map(function (v) { return exactCount(v); }).join(" / ");
  }
  function tokens(n) {
    return compact(n);
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
  function labelled(axis, label) { return Object.assign({}, axis, { label: label }); }
  function legendFor(el, series) {
    el.innerHTML = "";
    series.forEach(function (s) {
      var key = document.createElement("span");
      key.className = "key";
      var sw = document.createElement("span");
      sw.className = s.block ? "block" : "swatch";
      if (s.block) sw.style.background = s.color;
      else sw.style.borderColor = s.color;
      if (s.dashed) sw.style.borderStyle = "dashed";
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
    var steps = cfg.xSteps || Math.max(1, Math.min(6, Math.floor(iw / (sample * 1.5 + 8))));
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
      placeTip(tip, W, x, top);
    };
  }
  function placeTip(tip, W, x, top) {
    tip.style.display = "block";
    var tipW = tip.offsetWidth || 160;
    var tipX = x + 12;
    if (tipX + tipW > W) tipX = x - tipW - 12;
    tip.style.left = Math.max(0, tipX) + "px";
    tip.style.top = (top || 8) + "px";
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
      if (r.color) dot.style.background = r.color;
      else dot.style.visibility = "hidden";
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
    var m = cfg.margin ? { l: cfg.margin.l, r: cfg.margin.r, t: cfg.margin.t, b: cfg.margin.b } : {
      l: Math.max(44, Math.ceil(tickW) + 12),
      r: ends.length ? Math.min(Math.ceil(endW) + 24, Math.floor(W * 0.42)) : 12,
      t: 26,
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
    if (cfg.y.label) svgEl("text", { x: m.l, y: 12, class: "tick" }, svg).textContent = cfg.y.label;

    var refs = (cfg.refs || []).slice();
    refs.forEach(function (r) {
      svgEl("line", { x1: m.l, x2: W - m.r, y1: Y(r.v), y2: Y(r.v), stroke: "var(--ink-3)", "stroke-width": 1, "stroke-dasharray": "5 4" }, svg);
    });
    (cfg.offScale || []).forEach(function (text, i) {
      svgEl("text", {
        x: W - m.r - 6, y: m.t + 13 + i * 15, "text-anchor": "end", class: "direct",
        style: "fill: var(--ink-3); stroke: var(--surface); stroke-width: 3px; paint-order: stroke"
      }, svg).textContent = text;
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
        var attrs = {
          d: d.trim(), fill: "none", stroke: s.color, "stroke-width": s.width || 2,
          "stroke-opacity": s.opacity === undefined ? 1 : s.opacity, "stroke-linejoin": "round", "stroke-linecap": "round"
        };
        if (s.dash) attrs["stroke-dasharray"] = s.dash;
        if (d) svgEl("path", attrs, svg);
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
      showAt(sec);
      peersOf(host).forEach(function (peer) { peer.showAt(sec); });
    });
    function showAt(sec) {
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
    }
    hit.addEventListener("mouseenter", function () { cross.setAttribute("visibility", "visible"); });
    hit.addEventListener("mouseleave", function () { peersOf(host).forEach(function (peer) { peer.hide(); }); });
    host._hover = {
      showAt: showAt,
      hide: function () { cross.setAttribute("visibility", "hidden"); tip.style.display = "none"; }
    };
    if (cfg.group) host.setAttribute("data-hover-group", cfg.group);
    return svg;
  }

  function peersOf(host) {
    var group = host.getAttribute("data-hover-group");
    if (!group) return [];
    return Array.prototype.slice.call(document.querySelectorAll('[data-hover-group="' + group + '"]'))
      .filter(function (el) { return el !== host && el._hover; })
      .map(function (el) { return el._hover; });
  }

  function nearest(points, sec) {
    var best = null, bestD = Infinity;
    points.forEach(function (p) {
      var dist = Math.abs(p.t - sec);
      if (dist < bestD) { bestD = dist; best = p; }
    });
    return best;
  }

  function fmtTokens(n) {
    if (n === null || n === undefined) return "\u2014";
    var a = Math.abs(n), sign = n < 0 ? "-" : "";
    var units = [[1e9, "B"], [1e6, "M"], [1e3, "K"]];
    for (var i = 0; i < units.length; i++) {
      if (a >= units[i][0]) {
        var v = a / units[i][0];
        return sign + String(v >= 10 ? Math.round(v) : Math.round(v * 10) / 10) + units[i][1];
      }
    }
    return sign + String(Math.round(a));
  }
  function shareText(part, total) {
    if (!total) return "\u2014";
    var v = (part / total) * 100;
    return String(v >= 10 ? Math.round(v) : Math.round(v * 10) / 10) + "%";
  }
  function mixCaption(totals) {
    if (!totals || !totals.total) return "Mix: no tokens billed in this range";
    var s = totals.shares;
    return "Mix: cache read " + shareText(s.cache_read, 1) + " \u00b7 uncached input " + shareText(s.uncached, 1) +
      " \u00b7 output " + shareText(s.output, 1);
  }

  function drawTokenCombo(host, cfg) {
    host.innerHTML = "";
    var points = cfg.points || [];
    if (!points.length) { emptyChart(host); return; }
    var W = Math.max(260, Math.floor(host.clientWidth || 600));
    var H = cfg.height || 280;
    var svg = svgEl("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": cfg.title }, host);
    var left = cfg.axes.left, right = cfg.axes.right;
    var m = { l: 64, r: 48, t: 26, b: 24 };
    var iw = W - m.l - m.r;
    var ih = H - m.t - m.b;
    var plotBottom = m.t + ih;
    var x0 = cfg.x0, x1 = cfg.x1;
    var slot = (cfg.bucket_s / Math.max(1, x1 - x0)) * iw;
    var barW = Math.min(24, Math.max(1, slot - 2));
    function X(t) { return m.l + ((t - x0) / Math.max(1, x1 - x0)) * iw; }
    function center(p) { return X(p.t) + slot / 2; }
    function YL(v) { return plotBottom - (v / (left.max || 1)) * ih; }
    function YR(v) { return plotBottom - (v / (right.max || 1)) * ih; }

    left.ticks.forEach(function (v) {
      svgEl("line", { x1: m.l, x2: W - m.r, y1: YL(v), y2: YL(v), stroke: v === 0 ? "var(--axis)" : "var(--grid)", "stroke-width": 1 }, svg);
      svgEl("text", { x: m.l - 8, y: YL(v) + 4, "text-anchor": "end", class: "tick" }, svg).textContent = fmtTokens(v);
    });
    right.ticks.forEach(function (v) {
      svgEl("text", { x: W - m.r + 8, y: YR(v) + 4, "text-anchor": "start", class: "tick" }, svg).textContent = fmtTokens(v);
    });
    svgEl("text", { x: m.l, y: 12, class: "tick" }, svg).textContent = cfg.yLabel;
    svgEl("text", { x: W - 2, y: 12, "text-anchor": "end", class: "tick" }, svg).textContent = cfg.rightLabel;
    xTicks(svg, cfg, iw, H, m, X, cfg.tickFmt || hm);

    var limitY = YL(cfg.limit.value);
    svgEl("line", { x1: m.l, x2: W - m.r, y1: limitY, y2: limitY, stroke: "var(--ink-3)", "stroke-width": 1, "stroke-dasharray": "5 4" }, svg);
    svgEl("text", {
      x: m.l + 6, y: limitY - 6, class: "direct",
      style: "fill: var(--ink-3); stroke: var(--surface); stroke-width: 3px; paint-order: stroke"
    }, svg).textContent = cfg.limit.label;

    var band = svgEl("rect", { x: m.l, y: m.t, width: slot, height: ih, fill: "var(--ink-3)", "fill-opacity": 0.14, visibility: "hidden" }, svg);
    points.forEach(function (p) {
      if (!p.total) return;
      var cx = center(p);
      var top = YL(p.total), base = plotBottom;
      var r = Math.min(4, barW / 2, base - top);
      var l = cx - barW / 2, rt = cx + barW / 2;
      var d = "M" + l + "," + base + " L" + l + "," + (top + r) + " Q" + l + "," + top + " " + (l + r) + "," + top +
        " L" + (rt - r) + "," + top + " Q" + rt + "," + top + " " + rt + "," + (top + r) + " L" + rt + "," + base + " Z";
      svgEl("path", { d: d, fill: cfg.series[0].color }, svg);
    });

    var line = points.map(function (p, idx) {
      return (idx ? " L" : "M") + center(p).toFixed(1) + "," + YR(p.cumulative).toFixed(1);
    }).join("");
    svgEl("path", { d: line, fill: "none", stroke: "var(--surface)", "stroke-width": 4.5, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    svgEl("path", { d: line, fill: "none", stroke: COLORS.muted, "stroke-width": 1.5, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    var lastP = points[points.length - 1];
    svgEl("circle", { cx: center(lastP), cy: YR(lastP.cumulative), r: 4, fill: COLORS.muted, stroke: "var(--surface)", "stroke-width": 2 }, svg);

    var tip = document.createElement("div");
    tip.className = "tip";
    host.appendChild(tip);
    var hit = svgEl("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent" }, svg);
    function showAt(sec) {
      var p = nearest(points, sec);
      if (!p) return;
      band.setAttribute("x", X(p.t));
      band.setAttribute("visibility", "visible");
      var head = (cfg.tickFmt || hm)(p.t) + " \u2013 " + hm(p.t + cfg.bucket_s);
      fillTip(tip, head, [
        { color: cfg.series[0].color, text: "Total: " + fmtTokens(p.total) },
        { color: null, text: "Cache read: " + fmtTokens(p.cache_read) + " (" + shareText(p.cache_read, p.total) + ")" },
        { color: null, text: "Uncached input: " + fmtTokens(p.uncached) + " (" + shareText(p.uncached, p.total) + ")" },
        { color: null, text: "Output: " + fmtTokens(p.output) + " (" + shareText(p.output, p.total) + ")" },
        { color: COLORS.muted, text: "Cumulative (right axis): " + fmtTokens(p.cumulative) }
      ]);
      placeTip(tip, W, center(p), 8);
    }
    function hide() {
      band.setAttribute("visibility", "hidden");
      tip.style.display = "none";
    }
    hit.addEventListener("mousemove", function (ev) {
      var box = svg.getBoundingClientRect();
      var sec = x0 + ((ev.clientX - box.left - m.l) / Math.max(1, iw)) * (x1 - x0);
      showAt(sec);
      peersOf(host).forEach(function (peer) { peer.showAt(sec); });
    });
    hit.addEventListener("mouseleave", function () {
      hide();
      peersOf(host).forEach(function (peer) { peer.hide(); });
    });
    host._hover = { showAt: showAt, hide: hide };
    if (cfg.group) host.setAttribute("data-hover-group", cfg.group);
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

  function renderVerdict(d) {
    var v = d.verdict || {};
    var chip = $("v-chip");
    chip.textContent = v.chip || "—";
    chip.className = "chip " + (v.key === "morph_limit" || v.key === "morph_slow" || v.key === "laptop" ? "crit" : "ok");
    setText("v-text", v.text || "No data yet");
    setText("v-secondary", v.secondary || "");
    setText("v-room", (d.room || {}).text || "—");
  }

  function renderActivity(d) {
    var a = d.activity || {};
    [["5", a.tokens_5m], ["15", a.tokens_15m]].forEach(function (row) {
      var w = row[1] || {};
      setTile("a" + row[0] + "-tokens", w.tokens);
      var delta = w.delta === null || w.delta === undefined ? "no previous window" :
        (w.delta >= 0 ? "+" : "") + Math.round(w.delta * 100) + "% vs previous " + row[0] + " min";
      setText("a" + row[0] + "-note", count(w.requests) + " requests · " + delta);
    });
    var pm = a.pool_per_minute || {};
    setText("a5-min", pm.last_full_minute === undefined ? "—" :
      "last full minute: " + compact(pm.last_full_minute) + " (" + pct(pm.last_full_minute_frac, 0) + " of 10M) · avg/min: " + compact(pm.avg_5m));
    setText("a15-min", pm.avg_15m === undefined ? "—" : "avg/min: " + compact(pm.avg_15m));
    var dso = d.ds_output_5m_per_min;
    setTile("a-ds", dso === null || dso === undefined ? null : Math.round(dso));
  }

  function renderCapacity(d) {
    var cap = d.capacity || {};
    var limits = d.limits || {};
    var total = limits.total || {};
    var dsu = limits.uncached_ds || {};
    setText("cap-used", pct(total.range_avg_frac));
    setText("cap-binding", "of the " + compact(total.limit) + " per minute limit");
    setText("cap-ds-unc", pct(dsu.range_avg_frac));
    setText("cap-ds-unc-note", "of " + compact(dsu.limit) + " per minute");
    setTile("k-waiting", cap.waiting_now);
    var lanes = cap.waiting_by_lane || {};
    setText("k-waiting-note", Object.keys(lanes).length
      ? Object.keys(lanes).map(function (k) { return k + " " + lanes[k]; }).join(" · ")
      : "proxy status unavailable");
  }

  function renderRunKpis(d) {
    var rk = d.run_kpis || {};
    setTile("r-ds", rk.active_deepseek);
    setText("r-ds-note", "of cap " + count(rk.cap_deepseek));
    var queued = ((d.jobs || {}).by_state || {}).queued;
    setTile("r-queued", queued === undefined ? null : queued);
    setTile("r-filler", (d.jobs || {}).filler_running);
  }

  function renderPerf(d) {
    var pk = d.perf_kpis || {};
    setText("p-morph", seconds(pk.upstream_response_p50_ms) + " / " + seconds(pk.upstream_response_p95_ms));
    if (pk.ds_in_flight_now == null) { $("p-inflight").textContent = "—"; }
    else { setTileList("p-inflight", pk.inflight_limit == null ? [pk.ds_in_flight_now] : [pk.ds_in_flight_now, pk.inflight_limit]); }
  }

  function renderRange(d) {
    var rt = d.range_totals || {};
    var fin = rt.finished || {};
    setTileList("r-finished", ["PASS", "PARTIAL", "FAIL"].map(function (v) { return fin[v] || 0; }));
    setTile("r-tokens", (rt.tokens || {}).total);
    setTile("r-429", rt.r429);
  }

  function renderCharts(d) {
    var win = d.window || { start: 0, end: 0 };
    var bucket = d.bucket_s || 60;
    var unit = unitLabel(bucket);
    var fmtX = d.hours >= 168 ? dateHM : hm;
    var minutes = d.minutes || [];
    var axis = d.runs_axis;
    var tb = d.token_bucket_s || bucket;
    var tunit = unitLabel(tb);
    var workers = (d.workers || []).map(function (w) {
      return { t: w.ts, ds: w.ds_workers, kimi: w.kimi_workers, dsq: w.ds_queued, kimiq: w.kimi_queued, unsplit: w.queued_unsplit };
    });
    var firstSplit = workers.find(function (w) { return w.dsq !== null && w.dsq !== undefined; });
    var unsplitName = firstSplit ? "Queued, unsplit (before " + hm(firstSplit.t) + ")" : "Queued, unsplit";
    var PAIR_LAYOUT = { height: 260, margin: { l: 64, r: 16, t: 26, b: 24 } };
    var pctFmt = function (v, inline) { return inline ? (v * 100).toFixed(1) + "%" : Math.round(v * 100) + "%"; };
    var intFmt = function (v) { return Math.round(v).toLocaleString("en-US"); };
    charts.length = 0;

    var pctAxis = { min: 0, max: 1.2, clip: 1.2, ticks: [0, 0.2, 0.4, 0.6, 0.8, 1, 1.2], fmt: pctFmt };
    registerChart("c-billed", {
      title: "Morph total-token limit used per minute",
      kind: "line", bucket_s: bucket, tickFmt: fmtX, margin: PAIR_LAYOUT.margin, group: "capacity",
      x0: win.start, x1: win.end, height: PAIR_LAYOUT.height,
      points: minutes,
      series: [
        { key: "pct_total", name: "Per minute", color: COLORS.s1, width: 1.25, opacity: 0.4 },
        { key: "pct_total_avg5", name: "5-min average", color: COLORS.s2, width: 2.5 }
      ],
      y: labelled(pctAxis, "% of 10M tokens/min"),
      refs: [{ v: 1, label: "Morph limit (10M/min)" }],
      fmt: pctFmt
    });
    registerChart("c-workers", {
      title: "Active runs over time",
      kind: "line", bucket_s: bucket, tickFmt: fmtX, margin: PAIR_LAYOUT.margin, group: "capacity",
      x0: win.start, x1: win.end, height: PAIR_LAYOUT.height,
      points: workers,
      series: [
        { key: "ds", name: "DeepSeek runs", color: COLORS.s1 },
        { key: "kimi", name: "Kimi runs", color: COLORS.s2 },
        { key: "dsq", name: "DeepSeek queued", color: COLORS.s1, dash: "4 3" },
        { key: "kimiq", name: "Kimi queued", color: COLORS.s2, dash: "4 3" },
        { key: "unsplit", name: unsplitName, color: COLORS.muted, dash: "2 3", width: 1.25, opacity: 0.6 }
      ],
      y: labelled({ min: 0, max: axis.max, ticks: axis.ticks, fmt: intFmt }, "runs / jobs"),
      refs: axis.caps.filter(function (c) { return c.on_scale; }).map(function (c) { return { v: c.value, label: c.label }; }),
      offScale: axis.caps.filter(function (c) { return !c.on_scale; }).map(function (c) { return c.label + " \u2191 (off scale)"; }),
      fmt: intFmt
    });
    var mix = d.token_mix || [];
    var tokenLimit = d.token_limit || { value: 0 };
    registerChart("c-tokens", {
      title: "Tokens billed per " + tunit + ", with cumulative total on the right axis",
      tickFmt: fmtX, group: "capacity", bucket_s: tb, x0: win.start, x1: win.end, height: 280,
      points: mix,
      series: [{ key: "total", name: "Tokens billed", color: COLORS.s1 }],
      axes: d.token_axes || { left: { max: 1, ticks: [0, 1] }, right: { max: 1, ticks: [0, 1] } },
      limit: { value: tokenLimit.value, label: "Morph limit " + fmtTokens(tokenLimit.value) + " / " + tunit },
      yLabel: "tokens per " + tunit,
      rightLabel: "cumulative"
    }, drawTokenCombo);
    setText("h-tokens", "Tokens billed per " + tunit);
    setText("tok-mix", mixCaption(d.token_totals));
    legendFor($("lg-billed"), [
      { name: "Per minute", color: COLORS.s1 },
      { name: "5-min average", color: COLORS.s2 }
    ]);
    legendFor($("lg-workers"), [
      { name: "DeepSeek runs", color: COLORS.s1 },
      { name: "Kimi runs", color: COLORS.s2 },
      { name: "DeepSeek queued", color: COLORS.s1, dashed: true },
      { name: "Kimi queued", color: COLORS.s2, dashed: true },
      { name: unsplitName, color: COLORS.muted, dashed: true }
    ]);
    legendFor($("lg-tokens"), [
      { name: "Tokens billed", color: COLORS.s1, block: true },
      { name: "Cumulative (right axis)", color: COLORS.muted }
    ]);
    var smallLayout = { height: 200, margin: { l: 48, r: 12, t: 26, b: 24 } };
    var smallPctAxis = { min: 0, max: 2, clip: 2, ticks: [0, 1, 2], fmt: pctFmt };
    var smallChart = function (id, cfg) {
      return Object.assign({
        kind: "line", bucket_s: bucket, tickFmt: fmtX, xSteps: 2, margin: smallLayout.margin,
        x0: win.start, x1: win.end, height: smallLayout.height, points: minutes
      }, cfg);
    };
    var maxR = minutes.reduce(function (a, p) { return Math.max(a, p.r429_per_min); }, 0);
    var waitPoints = minutes.map(function (p) {
      return { t: p.t, wait: p.wait_p95_ms === null || p.wait_p95_ms === undefined ? null : p.wait_p95_ms / 1000 };
    });
    var maxW = waitPoints.reduce(function (a, p) { return p.wait === null ? a : Math.max(a, p.wait); }, 0);
    var waitTop = maxW > 120 ? 120 * Math.ceil(maxW / 120) : niceTicks(Math.max(maxW, 1), 2).max;
    var waitFmt = function (v, inline) {
      if (v >= 60) return (inline ? (v / 60).toFixed(1) : Math.round(v / 60)) + " m";
      return (inline ? v.toFixed(1) : Math.round(v)) + " s";
    };

    registerChart("c-kimi", smallChart("c-kimi", {
      title: "Kimi uncached, estimated percent of 24,290 per minute",
      series: [{ key: "pct_kimi_uncached", name: "Kimi uncached (estimate)", color: COLORS.s3 }],
      y: labelled(smallPctAxis, "% of 24,290 tokens/min"),
      refs: [{ v: 1, label: "limit" }],
      fmt: pctFmt
    }));
    var rej = d.ds_uncached_rejections || {};
    setText("dsu-sub", rej.count === null || rej.count === undefined
      ? "rejections not recorded"
      : "Morph rejections on this limit, " + RANGE_LABEL[d.hours] + ": " + count(rej.count) + (rej.partial ? " (proxy log tail only)" : ""));
    registerChart("c-dsu", smallChart("c-dsu", {
      title: "DeepSeek uncached input, estimated percent of 400k per minute",
      series: [{ key: "pct_ds_uncached", name: "DeepSeek uncached (estimate)", color: COLORS.s2 }],
      y: labelled(smallPctAxis, "% of 400k tokens/min"),
      refs: [{ v: 1, label: "limit" }],
      fmt: pctFmt
    }));
    registerChart("c-429", smallChart("c-429", {
      title: "Upstream 429s per minute",
      kind: "bar",
      series: [{ key: "r429_per_min", name: "429s per minute", color: "var(--crit)" }],
      y: labelled(niceTicks(Math.max(maxR, 1), 2), "429s per minute"),
      fmt: intFmt
    }));
    registerChart("c-wait", smallChart("c-wait", {
      title: "Wait p95 seconds per minute",
      points: waitPoints,
      series: [{ key: "wait", name: "p95 wait", color: COLORS.s1 }],
      y: labelled({ min: 0, max: waitTop, ticks: [0, waitTop / 2, waitTop], fmt: waitFmt }, "seconds"),
      fmt: waitFmt
    }));
    var upPoints = minutes.map(function (p) {
      var sec = function (v) { return v === null || v === undefined ? null : v / 1000; };
      return { t: p.t, p50: sec(p.upstream_p50_ms), p95: sec(p.upstream_p95_ms) };
    });
    var maxU = upPoints.reduce(function (a, p) { return p.p95 === null ? a : Math.max(a, p.p95); }, 0);
    registerChart("c-upstream", {
      title: "Morph response time per request, seconds per " + unit,
      kind: "line", bucket_s: bucket, tickFmt: fmtX, margin: PAIR_LAYOUT.margin, group: "capacity",
      x0: win.start, x1: win.end, height: 200,
      points: upPoints,
      series: [
        { key: "p50", name: "p50", color: COLORS.s1 },
        { key: "p95", name: "p95", color: COLORS.s2 }
      ],
      y: labelled(niceTicks(Math.max(maxU, 1), 3), "seconds per request"),
      fmt: function (v) { return (Math.abs(v) < 10 ? v.toFixed(1) : v.toFixed(0)) + " s"; }
    });
    var hasUpstream = upPoints.some(function (p) { return p.p50 !== null; });
    setText("up-note", hasUpstream ? "" : "No response times recorded in this range: the proxy started recording them later.");
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
    { label: "Start (ET)", text: true, value: function (r) { return r.t; }, cell: function (r) { return hm(r.t); } },
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
      cell.textContent = "No runs active " + RANGE_LABEL[d.hours];
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
    var rk = d.run_kpis || {};
    $("top-total").textContent = rk.active_lifetime_tokens === undefined ? "" :
      "Lifetime tokens of the " + count(rk.active_in_window) + " runs active " + RANGE_LABEL[d.hours] + ": " + tokens(rk.active_lifetime_tokens);
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
    log.fetchedAt = Date.now();
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
    renderVerdict(d);
    renderActivity(d);
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
    var data = state.data;
    var text = data && data.generated_at ? "updated " + hms(data.generated_at) : "loading";
    var errs = data && data.errors ? Object.keys(data.errors) : [];
    if (data && data.partial) text += " · partial summary, full one on the way";
    if (errs.length) text += " · partial: " + errs.join(", ");
    status.appendChild(document.createTextNode(text));
    if (data && data.generated_at) {
      var age = Date.now() / 1000 - data.generated_at;
      if (age > 2 * (data.cadence_s || 60)) {
        var aged = document.createElement("span");
        aged.className = "aged";
        aged.textContent = " · data " + Math.round(age) + " s old";
        status.appendChild(aged);
      }
    }
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
        var unchanged = state.data && state.data.hours === data.hours && state.data.generated_at === data.generated_at;
        state.data = data;
        state.stale = null;
        if (unchanged) renderStatus();
        else render(data);
      })
      .catch(function (err) {
        if (seq !== state.seq) return;
        state.stale = err && err.message ? err.message : "network error";
        renderStatus();
      });
    if (Date.now() - log.fetchedAt >= LOG_POLL_MS) refreshLog();
  }

  function setRangeButtons() {
    document.querySelectorAll(".range button").forEach(function (btn) {
      btn.setAttribute("aria-pressed", Number(btn.getAttribute("data-hours")) === state.hours ? "true" : "false");
    });
    document.querySelectorAll(".rng").forEach(function (el) {
      el.textContent = RANGE_LABEL[state.hours];
    });
  }

  function startPolling() {
    stopPolling();
    state.timer = setInterval(function () {
      if (document.visibilityState !== "visible") return;
      if (Date.now() - state.lastFetch < POLL_MS_BY_HOURS[state.hours] - 500) return;
      refresh();
    }, 1000);
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
    """The process table: pid and command per process. Raises when ps fails, so no sample is recorded.

    Environments (``ps -E``) are not read: reading them is the slow part of ps on a loaded laptop. Queue
    workers are counted from the jobs table instead (see sample_once).
    """
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
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


_BUDGET = threading.local()


@contextlib.contextmanager
def time_budget(seconds: float):
    """Bound the SQLite waits made on this thread inside the block. Reads past the budget raise TimeoutError."""
    previous = getattr(_BUDGET, "until", None)
    _BUDGET.until = time.monotonic() + seconds
    try:
        yield
    finally:
        _BUDGET.until = previous


def db_timeout() -> float:
    """The busy wait for one read: DB_TIMEOUT_S, cut to what is left of the time budget on this thread."""
    until = getattr(_BUDGET, "until", None)
    if until is None:
        return DB_TIMEOUT_S
    left = until - time.monotonic()
    if left <= 0:
        raise TimeoutError("time budget spent")
    return min(DB_TIMEOUT_S, left)


def readonly_connection(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite file read-only. Raises FileNotFoundError when it is missing."""
    if not path.exists():
        raise FileNotFoundError(f"{path.name} not found")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=db_timeout())
    connection.row_factory = sqlite3.Row
    return connection


def minutes_sql(columns: set[str]) -> str:
    """MINUTES_SQL with the upstream latency aggregates for the columns this database has. Older
    databases lack them, so those read as NULL and the chart shows a gap instead of failing."""

    def has(*names: str) -> bool:
        return all(name in columns for name in names)

    return MINUTES_SQL.format(
        upstream_ms_sum="SUM(COALESCE(upstream_ms_sum, 0))" if has("upstream_ms_sum") else "NULL",
        upstream_n="SUM(COALESCE(upstream_n, 0))" if has("upstream_n") else "NULL",
        upstream_p50_weighted=(
            "SUM(CASE WHEN upstream_p50_ms IS NOT NULL THEN upstream_p50_ms * COALESCE(upstream_n, 0) ELSE 0 END)"
            if has("upstream_p50_ms", "upstream_n") else "NULL"
        ),
        upstream_p50_weight=(
            "SUM(CASE WHEN upstream_p50_ms IS NOT NULL THEN COALESCE(upstream_n, 0) ELSE 0 END)"
            if has("upstream_p50_ms", "upstream_n") else "NULL"
        ),
        upstream_p95_ms="MAX(upstream_p95_ms)" if has("upstream_p95_ms") else "NULL",
        ds_p50_weighted=(
            "SUM(CASE WHEN model LIKE 'morph-dsv%' AND upstream_p50_ms IS NOT NULL THEN upstream_p50_ms * COALESCE(upstream_n, 0) ELSE 0 END)"
            if has("upstream_p50_ms", "upstream_n") else "NULL"
        ),
        ds_p50_weight=(
            "SUM(CASE WHEN model LIKE 'morph-dsv%' AND upstream_p50_ms IS NOT NULL THEN COALESCE(upstream_n, 0) ELSE 0 END)"
            if has("upstream_p50_ms", "upstream_n") else "NULL"
        ),
        ds_ttfb_p50_ms=(
            "MAX(CASE WHEN model LIKE 'morph-dsv%' THEN ttfb_p50_ms END)" if has("ttfb_p50_ms") else "NULL"
        ),
        inflight_limit="MAX(inflight_limit)" if has("inflight_limit") else "NULL",
    )


def read_minute_rows(agentq_db: Path, since: int, until: int) -> dict[int, dict]:
    """Summed proxy_minutes rows keyed by the epoch second of each complete minute in [since, until)."""
    connection = readonly_connection(agentq_db)
    try:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(proxy_minutes)")}
        rows = connection.execute(minutes_sql(columns), (minute_stamp(since), minute_stamp(until))).fetchall()
    finally:
        connection.close()
    return {minute_epoch(row["minute"]): dict(row) for row in rows}


def weighted_upstream_p50(row: dict) -> int | None:
    """Request-weighted p50 of the proxy's upstream response time over the rows summed into ``row``; None when none recorded one."""
    weight = int(row.get("upstream_p50_weight") or 0)
    if not weight:
        return None
    return round(int(row.get("upstream_p50_weighted") or 0) / weight)


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
        "ds_uncached": int(row.get("ds_uncached") or 0),
        "input": int(row.get("input") or 0),
        "cache_read": int(row.get("cache_read") or 0),
        "output": int(row.get("output") or 0),
        "ok": int(row.get("ok") or 0),
        "r429": int(row.get("r429") or 0),
        "r429_per_min": int(row.get("r429") or 0),
        "req": int(row.get("req") or 0),
        "wait_p50_weighted": int(row.get("wait_p50_weighted") or 0),
        "wait_p95_ms": row.get("wait_p95_ms"),
        "upstream_p50_ms": weighted_upstream_p50(row),
        "upstream_p95_ms": row.get("upstream_p95_ms"),
        "upstream_n": int(row.get("upstream_n") or 0),
        "pct_total": fraction(total, LIMITS["total"]),
        "pct_ds_output": fraction(ds_output, LIMITS["output"]),
        "pct_kimi_uncached": fraction(kimi_uncached, LIMITS["uncached"]),
        "pct_ds_uncached": fraction(int(row.get("ds_uncached") or 0), LIMITS["uncached_ds"]),
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


def range_average_fraction(raw: dict[int, dict], since: int, until: int, key: str, limit: int) -> float | None:
    """Mean billed fraction of a limit over the selected range, counted from the first minute the proxy recorded.

    None when the range holds no proxy minutes, so the tile shows a gap rather than a zero.
    """
    if not raw:
        return None
    first = max(min(raw), since)
    minutes = (until - first) // 60
    if minutes <= 0:
        return None
    billed = sum(int((raw.get(t) or {}).get(key) or 0) for t in range(first, until, 60))
    return fraction(billed / minutes, limit)


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
        ds_holds = [row["detail"] for row in connection.execute(QUEUED_DS_DETAIL_SQL).fetchall()]
        running = {row["res"]: row["n"] for row in connection.execute(RUNNING_BY_RES_SQL).fetchall()}
        filler_running = int(connection.execute(FILLER_RUNNING_SQL).fetchone()["n"])
    finally:
        connection.close()
    by_state: dict[str, int] = {}
    by_tier: dict[str, dict[str, int]] = {}
    for row in counts:
        by_state[row["state"]] = by_state.get(row["state"], 0) + row["n"]
        by_tier.setdefault(row["tier"], {})[row["state"]] = row["n"]
    return {
        "by_state": by_state, "by_tier": by_tier, "recent": [dict(row) for row in recent],
        "ds_holds": ds_holds, "running_by_res": running, "filler_running": filler_running,
    }


def read_queued_by_tier(agentq_db: Path) -> dict[str, int]:
    """Jobs queued right now, counted per tier. Read-only."""
    connection = readonly_connection(agentq_db)
    try:
        rows = connection.execute("SELECT tier, COUNT(*) AS n FROM jobs WHERE state = 'queued' GROUP BY tier").fetchall()
    finally:
        connection.close()
    return {row["tier"]: int(row["n"]) for row in rows}


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
    """Worker samples recorded at or after ``since``. Empty before the first sample exists.

    A database written before the per-tier queue split has no split columns: those read as None. A sample with
    a total but no split is reported as ``queued_unsplit``, so the chart never invents a per-tier figure.
    """
    if not dash_db.exists():
        return []
    connection = readonly_connection(dash_db)
    try:
        present = {row[1] for row in connection.execute("PRAGMA table_info(samples)").fetchall()}
        columns = ", ".join(c if c in present else f"NULL AS {c}" for c in SAMPLE_COLUMNS)
        rows = [dict(row) for row in connection.execute(f"SELECT {columns} FROM samples WHERE ts >= ? ORDER BY ts", (since,))]
    finally:
        connection.close()
    for row in rows:
        split = row["ds_queued"] is not None
        row["queued_unsplit"] = None if split or row["queued_jobs"] is None else row["queued_jobs"]
    return rows


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


def median_or_none(values: list[float]) -> float | None:
    """The median of ``values``, or None when there are none."""
    return statistics.median(values) if values else None


def capacity_headline(limits: dict, live: dict | None) -> dict:
    """The limit with the highest 5-minute billed fraction (the binding one), and requests waiting at the proxy."""
    present = [name for name in LIMIT_KEYS if name in limits]
    binding = max(present, key=lambda name: limits[name]["avg_5m_frac"]) if present else None
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


def add_trailing_average(points: list[dict], source: str, target: str, window: int) -> None:
    """Set ``target`` on each point to the mean of ``source`` over it and the window - 1 minutes before it.

    Points are consecutive minutes (``minute_series`` fills gaps with zero), so a window is a time window.
    The first window - 1 points have no full window and get None, which the chart draws as a gap.
    """
    for i, point in enumerate(points):
        if i < window - 1:
            point[target] = None
        else:
            point[target] = round(sum(p[source] for p in points[i - window + 1:i + 1]) / window, 4)


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
        p50s = [m["upstream_p50_ms"] for m in members if m.get("upstream_p50_ms") is not None]
        upstream_p95s = [m["upstream_p95_ms"] for m in members if m.get("upstream_p95_ms") is not None]
        bucket["upstream_p50_ms"] = round(sum(p50s) / len(p50s)) if p50s else None
        bucket["upstream_p95_ms"] = max(upstream_p95s) if upstream_p95s else None
        for key in PCT_KEYS:
            bucket[key] = round(sum(m[key] for m in members) / len(members), 4)
        bucket["r429_per_min"] = round(bucket["r429"] / len(members), 2)
        averaged = [m["pct_total_avg5"] for m in members if m.get("pct_total_avg5") is not None]
        bucket["pct_total_avg5"] = round(sum(averaged) / len(averaged), 4) if averaged else None
        buckets.append(bucket)
    return buckets


RUNS_AXIS_KEYS = ("ds_workers", "kimi_workers", "ds_queued", "kimi_queued", "queued_unsplit")
RUNS_AXIS_HEADROOM = 1.15
RUNS_AXIS_MIN = 10
RUNS_AXIS_STEPS = tuple(sorted({m * 10**k for k in range(7) for m in (1, 2, 5, 10, 20, 25, 50)}))
CAP_LABELS = {"deepseek": "DeepSeek cap", "kimi": "Kimi cap"}


def runs_axis(workers: list[dict], caps: dict[str, int]) -> dict:
    """Integer y axis for the active-runs chart, fitted to the plotted series.

    The top is the smallest whole-number ceiling of 1.15 x the largest plotted value (at least 10) that a
    whole-number step divides into 3 or 4 intervals (2 to 5 when no step fits). Each cap says whether it
    falls inside that axis; a cap above the top is flagged off scale instead of stretching the axis.
    """
    peak = max((w[key] for w in workers for key in RUNS_AXIS_KEYS if w.get(key)), default=0)
    want = max(RUNS_AXIS_MIN, math.ceil(peak * RUNS_AXIS_HEADROOM))
    options = [(step * math.ceil(want / step), -step, step, math.ceil(want / step)) for step in RUNS_AXIS_STEPS]
    preferred = [option for option in options if 3 <= option[3] <= 4] or [option for option in options if 2 <= option[3] <= 5]
    top, _, step, intervals = min(preferred)
    cap_rows = [
        {"value": caps[key], "label": f"{CAP_LABELS[key]} {caps[key]}", "on_scale": caps[key] <= top}
        for key in ("deepseek", "kimi") if key in caps
    ]
    return {"max": top, "ticks": [step * i for i in range(intervals + 1)], "caps": cap_rows}


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
            if not any(key in sample for sample in groups[start]):
                continue
            values = [sample.get(key) for sample in groups[start] if sample.get(key) is not None]
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


def run_kpis(runs: list[dict], since: int, caps: dict[str, int]) -> dict:
    """Active runs against their caps now, and the runs active since ``since`` with their lifetime totals.

    proxy_jobs keeps one total per job and model, not per minute, so a run's tokens cannot be cut to the
    selected range. The active-run figures are therefore lifetime totals of runs with activity in the range.
    """
    active_tiers = [run_tier(run) for run in runs if run["active"]]
    in_window = [run for run in runs if run["last_ts"] >= since]
    return {
        "active_deepseek": active_tiers.count("deepseek"),
        "active_kimi": active_tiers.count("kimi"),
        "cap_deepseek": caps["deepseek"],
        "cap_kimi": caps["kimi"],
        "active_in_window": len(in_window),
        "active_lifetime_tokens": sum(run["total_tokens"] for run in in_window),
        "median_tokens_per_run": median_or_none([run["total_tokens"] for run in in_window]),
        "median_requests_per_run": median_or_none([run["requests"] for run in in_window]),
    }


def in_flight_total(live: dict | None) -> int | None:
    """Upstream requests in flight right now across models, from the proxy's status; None when the proxy did not answer."""
    upstream = (live or {}).get("upstream")
    in_flight = upstream.get("in_flight") if isinstance(upstream, dict) else None
    if not isinstance(in_flight, dict):
        return None
    return sum(int(value) for value in in_flight.values())


def perf_kpis(raw: dict[int, dict], since: int, until: int, runs: list[dict], now: float, live: dict | None = None) -> dict:
    """Proxy wait, upstream time, 429 rate and request rate over the selected range. Wait p50 is request-weighted; p95 is the worst minute.
    The Morph response time uses the same weighting: p50 is request-weighted over the minutes, p95 the worst minute."""
    window = [raw[t] for t in range(since, until, 60) if t in raw]
    upstream_weight = sum(int(row.get("upstream_p50_weight") or 0) for row in window)
    upstream_p95s = [row["upstream_p95_ms"] for row in window if row.get("upstream_p95_ms") is not None]
    req_window = sum(row["req"] for row in window)
    p95s = [row["wait_p95_ms"] for row in window if row["wait_p95_ms"] is not None]
    upstream = [
        run["upstream_ms_per_req"]
        for run in runs
        if run["upstream_ms_per_req"] is not None and now - run["last_ts"] <= until - since
    ]
    req_60m = window_sum(raw, until, RATE_WINDOW_MINUTES, "req")
    return {
        "wait_p50_ms": sum(row["wait_p50_weighted"] for row in window) / req_window if req_window else None,
        "wait_p95_ms": max(p95s) if p95s else None,
        "upstream_response_p50_ms": (
            round(sum(int(row.get("upstream_p50_weighted") or 0) for row in window) / upstream_weight)
            if upstream_weight else None
        ),
        "upstream_response_p95_ms": max(upstream_p95s) if upstream_p95s else None,
        "in_flight_now": in_flight_total(live),
        "ds_in_flight_now": (live or {}).get("inflight_in_flight"),
        "inflight_limit": live_inflight_limit(live),
        "inflight_mode": (live or {}).get("inflight_mode"),
        "upstream_median_ms": median_or_none(upstream),
        "r429_rate_60m": window_sum(raw, until, RATE_WINDOW_MINUTES, "r429") / req_60m if req_60m else None,
        "requests_per_min_5m": window_sum(raw, until, LIMIT_WINDOW_MINUTES, "req") / LIMIT_WINDOW_MINUTES,
    }


TOKEN_KINDS = ("cache_read", "uncached", "output")
TOKEN_AXIS_MULTIPLES = (1, 2, 2.5, 5)


def token_mix(minutes: list[dict]) -> list[dict]:
    """Billed tokens per interval (cache read, uncached input and output) plus the running total from the range start."""
    running = 0
    mix = []
    for point in minutes:
        parts = {"cache_read": point["cache_read"], "uncached": point["input"], "output": point["output"]}
        total = sum(parts.values())
        running += total
        mix.append({"t": point["t"], "minute": point["minute"], "total": total, **parts, "cumulative": running})
    return mix


def token_totals(mix: list[dict]) -> dict:
    """Range totals by kind and each kind's share of the billed total (shares are None when nothing was billed)."""
    sums = {kind: sum(row[kind] for row in mix) for kind in TOKEN_KINDS}
    total = sum(sums.values())
    shares = {kind: (sums[kind] / total if total else None) for kind in TOKEN_KINDS}
    return {"total": total, **sums, "shares": shares}


def token_limit(per_minute: int, bucket_s: int) -> dict:
    """Morph's total-token limit for one chart bucket: the per-minute limit times the bucket's minutes."""
    minutes = bucket_s // 60
    return {"per_minute": per_minute, "bucket_minutes": minutes, "value": per_minute * minutes}


def nice_axis(peak: float) -> dict:
    """Zero-based axis whose top is the smallest 1, 2, 2.5 or 5 step (times a power of ten) covering ``peak`` in 3 to 5 steps."""
    peak = max(float(peak), 1.0)
    magnitude = 10 ** math.floor(math.log10(peak))
    best = None
    for scale in (magnitude / 10, magnitude, magnitude * 10):
        for multiple in TOKEN_AXIS_MULTIPLES:
            unit = multiple * scale
            steps = math.ceil(peak / unit)
            if unit >= 1 and 3 <= steps <= 5 and (best is None or steps * unit < best[0]):
                best = (steps * unit, unit, steps)
    if best is None:
        return {"max": peak, "ticks": [0, peak]}
    top, unit, steps = best
    return {"max": top, "ticks": [unit * i for i in range(steps + 1)]}


def token_axes(mix: list[dict], limit: dict) -> dict:
    """Left axis for billed tokens per bucket (at least 1.1 x the largest bucket and 1.05 x the limit) and the right cumulative axis."""
    peak_bucket = max((row["total"] for row in mix), default=0)
    cumulative_peak = mix[-1]["cumulative"] if mix else 0
    return {
        "left": nice_axis(max(1.1 * peak_bucket, 1.05 * limit["value"])),
        "right": nice_axis(cumulative_peak),
    }


def top_consumers(runs: list[dict], since: int, active_lifetime_tokens: int) -> list[dict]:
    """The runs active since ``since`` with the most lifetime tokens, each with its share of those runs' total."""
    in_window = sorted((run for run in runs if run["last_ts"] >= since), key=lambda run: run["total_tokens"], reverse=True)
    return [
        {
            "run": run["run"],
            "job": run["job"],
            "total_tokens": run["total_tokens"],
            "share": run["total_tokens"] / active_lifetime_tokens if active_lifetime_tokens else None,
        }
        for run in in_window[:TOP_CONSUMERS]
    ]


def _guard(name: str, errors: dict, fn, default, *args):
    """Run one data source; on failure record its error and return the default."""
    try:
        return fn(*args)
    except Exception as exc:
        errors[name] = f"{type(exc).__name__}: {exc}"[:ERROR_CHARS]
        return default


def resolve_limits(live: dict | None) -> dict[str, int]:
    """The limit per budget: the proxy's live figure where it gives one, else the documented constant."""
    limits = {name: LIMITS[name] for name in LIMIT_KEYS}
    budgets = (live or {}).get("budgets") if isinstance(live, dict) else None
    if isinstance(budgets, dict):
        for name in LIMIT_KEYS:
            budget = budgets.get(name)
            value = budget.get("limit") if isinstance(budget, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                limits[name] = int(value)
    return limits


def live_inflight_limit(live: dict | None) -> int | None:
    """The DeepSeek in-flight limit the proxy reports (shadow included), or None."""
    value = (live or {}).get("inflight_limit")
    return int(value) if isinstance(value, (int, float)) else None


def live_ttfb_baseline(live: dict | None, baseline_raw: dict[int, dict], until: int) -> int | None:
    """DeepSeek first-byte baseline: the proxy's long-run value when it reports one, else the median of
    the 6-hour per-minute first-byte p50s."""
    value = (live or {}).get("ttfb_baseline_ms")
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    values = [
        int(row["ds_ttfb_p50_ms"]) for stamp, row in baseline_raw.items()
        if stamp >= until - 6 * 3600 and row.get("ds_ttfb_p50_ms")
    ]
    return round(statistics.median(values)) if values else None


def read_calib_429s(home: Path) -> dict | None:
    """Calib log lines that name a 429 budget, from the proxy log tail: ``events`` as (budget, minute epoch) pairs.

    ``events`` is empty when the log holds no such line, which means the proxy did not record rejections.
    ``truncated`` says the tail does not reach back to the start of the log. None when the log is unreadable.
    """
    try:
        data, truncated = read_log_tail(Path(home) / "logs" / LOG_SOURCES["proxy"])
    except OSError:
        return None
    events: list[tuple[str, int]] = []
    for line in data.decode("utf-8", "replace").splitlines():
        entry = parse_log_line(line)
        if entry is None or entry["event"] != "calib":
            continue
        fields = {f["k"]: f["v"] for f in entry["fields"]}
        budget, stamp = fields.get("budget"), fields.get("minute")
        if not budget or budget == "None" or not stamp:
            continue
        try:
            events.append((budget, minute_epoch(stamp)))
        except ValueError:
            continue
    return {"events": events, "truncated": truncated}


def budget_429_minutes(home: Path, until: int) -> dict[str, set[int]]:
    """Minutes with an upstream 429 per budget name, from the proxy's calib log lines in the last 5 minutes.

    The proxy names the budget a 429 body states. A 429 with no stated budget is not counted.
    """
    read = read_calib_429s(home)
    if read is None:
        return {}
    start = until - LIMIT_WINDOW_MINUTES * 60
    found: dict[str, set[int]] = {}
    for budget, minute in read["events"]:
        if start <= minute < until:
            found.setdefault(budget, set()).add(minute)
    return found


def budget_rejections(read: dict | None, budget: str, since: int, until: int) -> dict:
    """Upstream 429s that named ``budget`` in [since, until), from ``read_calib_429s``.

    ``count`` is None when the log records no stated-budget 429 at all, so the page can say rejections are not recorded.
    ``partial`` says the log tail may not cover the whole range.
    """
    if read is None or not read["events"]:
        return {"count": None, "partial": False}
    count = sum(1 for name, minute in read["events"] if name == budget and since <= minute < until)
    return {"count": count, "partial": bool(read["truncated"])}


def minute_pool_tokens(raw: dict[int, dict], admitted: dict[int, int], minute: int) -> int:
    """Total-pool tokens for one minute: admitted tokens where proxy_calib has the minute, else completion tokens."""
    if minute in admitted:
        return int(admitted[minute])
    return int((raw.get(minute) or {}).get("total") or 0)


def pool_per_minute(raw: dict[int, dict], admitted: dict[int, int], until: int) -> dict:
    """Total-pool tokens per minute on the same basis as the main capacity line: the last full minute and 5 and 15 minute averages."""
    last = minute_pool_tokens(raw, admitted, until - 60)
    avg5 = sum(minute_pool_tokens(raw, admitted, t) for t in range(until - 300, until, 60)) / 5
    avg15 = sum(minute_pool_tokens(raw, admitted, t) for t in range(until - 900, until, 60)) / 15
    return {
        "last_full_minute": last,
        "last_full_minute_frac": fraction(last, LIMITS["total"]),
        "avg_5m": round(avg5),
        "avg_15m": round(avg15),
    }


def memory_free_pressure(now: float, run=subprocess.run) -> float | None:
    """Free system memory percent from macOS memory_pressure, or None; cached for MEMORY_CACHE_S."""
    cached = _MEMORY_CACHE
    if cached.get("at") is not None and 0 <= now - cached["at"] < MEMORY_CACHE_S:
        return cached["free"]
    try:
        out = run(["memory_pressure"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    match = MEMORY_LINE.search(out or "")
    cached["at"], cached["free"] = now, (float(match.group(1)) if match else None)
    return cached["free"]


_MEMORY_CACHE: dict = {}


def laptop_bars(home: Path) -> dict:
    """The bars the broker holds tests and light jobs at, from broker.json's ``laptop`` section over LAPTOP_BAR_DEFAULTS.

    The dashboard reads the file itself (it runs as a script, not as the agentq package), so
    LAPTOP_BAR_DEFAULTS mirrors the broker's defaults and a test keeps the two equal.
    """
    try:
        data = json.loads((Path(home) / "broker.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    section = data.get("laptop") if isinstance(data, dict) and isinstance(data.get("laptop"), dict) else {}
    bars = {}
    for name, default in LAPTOP_BAR_DEFAULTS.items():
        try:
            bars[name] = float(section.get(name, default))
        except (TypeError, ValueError):
            bars[name] = default
    return bars


def laptop_holds(details: list[str]) -> list[tuple[str, str]]:
    """The (gate, reason) pairs of queued DeepSeek jobs the broker holds on laptop headroom."""
    held = []
    for detail in details:
        if detail:
            gate, reason = split_hold(detail)
            if hold_group(gate, reason) == "laptop":
                held.append((gate, reason))
    return held


def laptop_pressure(laptop: dict, bars: dict) -> str | None:
    """The first light-job bar the laptop is past now (CPU busy, memory free, swap used), as text, or None.

    This is context for the secondary line only. The Laptop verdict needs a broker hold, because the
    broker's own admission decides whether DeepSeek work waits.
    """
    busy, free, swap = laptop.get("cpu_busy"), laptop.get("memory_free_pct"), laptop.get("swap_frac")
    if busy is not None and busy >= bars["light_max_cpu_busy"]:
        return f"CPU {busy * 100:.0f}% busy against the {bars['light_max_cpu_busy'] * 100:.0f}% light bar"
    if free is not None and free < bars["light_min_free_pct"]:
        return f"{free:.0f}% memory free against the {bars['light_min_free_pct']:.0f}% light floor"
    if swap is not None and swap >= bars["max_swap_frac"]:
        return f"swap {swap * 100:.0f}% used against the {bars['max_swap_frac'] * 100:.0f}% bar"
    return None


def laptop_reading(home: Path, now: float, memory=memory_free_pressure) -> dict:
    """Laptop CPU busy, swap used and free memory, from the broker's gauge file while fresh.

    Memory falls back to memory_pressure when the file is stale. Load per CPU is reported with the
    reading but never judged: the verdict uses CPU busy, memory and swap, as the broker does.
    """
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = None
    cpus = os.cpu_count() or 1
    try:
        gauge = json.loads((Path(home) / "slots" / "gauge.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        gauge = {}
    gauge = gauge if isinstance(gauge, dict) else {}

    def live(value_key: str, until_key: str) -> float | None:
        value, until = gauge.get(value_key), gauge.get(until_key)
        if isinstance(value, (int, float)) and isinstance(until, (int, float)) and now < until:
            return float(value)
        return None

    free = live("free", "free_until")
    if free is None:
        free = memory(now)
    return {
        "load1": load1,
        "cpus": cpus,
        "load_per_cpu": load1 / cpus if load1 is not None else None,
        "memory_free_pct": free,
        "cpu_busy": live("cpu_busy", "cpu_until"),
        "swap_frac": live("swap", "swap_until"),
        "bars": laptop_bars(home),
    }


def window_tokens(raw: dict[int, dict], until: int, minutes: int) -> dict:
    """Billed tokens and requests over the last ``minutes`` complete minutes, against the window before them."""
    current = window_sum(raw, until, minutes, "total")
    previous = sum(int((raw.get(t) or {}).get("total") or 0) for t in range(until - 2 * minutes * 60, until - minutes * 60, 60))
    return {
        "minutes": minutes,
        "tokens": current,
        "requests": window_sum(raw, until, minutes, "req"),
        "previous_tokens": previous,
        "delta": (current - previous) / previous if previous else None,
    }


def proxy_waiting_by_budget(live: dict | None) -> dict[str, int] | None:
    """DeepSeek requests waiting at the proxy, per DeepSeek budget, or None when the proxy is unreachable.

    A proxy that reports ``queued_by_family`` counts DeepSeek's own requests on each budget directly.
    An older proxy queues a request on every budget it charges, so a waiting Kimi request also shows in
    the total queue; that case subtracts Kimi's share, taken from the uncached queue. The output and
    uncached_ds budgets are DeepSeek-only.
    """
    budgets = live.get("budgets") if isinstance(live, dict) else None
    if not isinstance(budgets, dict):
        return None

    def queued(name: str) -> int:
        lanes = (budgets.get(name) or {}).get("queued")
        return sum(int(n) for n in lanes.values()) if isinstance(lanes, dict) else 0

    def deepseek_queued(name: str) -> int:
        by_family = (budgets.get(name) or {}).get("queued_by_family")
        if isinstance(by_family, dict):
            return int(by_family.get(DEEPSEEK_FAMILY) or 0)
        if name == "total":
            return max(0, queued("total") - queued("uncached"))
        return queued(name)

    return {name: deepseek_queued(name) for name in ("total", "output", "uncached_ds")}


def split_hold(detail: str) -> tuple[str, str]:
    """(gate, reason) from a queued job's detail, ``hold <gate>: <reason>``. A detail without that prefix has no gate."""
    match = HOLD_DETAIL.match(detail)
    return (match.group(1), match.group(2)) if match else ("", detail)


def hold_group(gate: str, reason: str) -> str:
    """The limit a held DeepSeek job waits on: a proxy budget, a workspace, an ssh slot, laptop load or memory, or other."""
    if reason.startswith("proxy "):
        return "proxy"
    if reason == "workspace busy":
        return "workspace"
    if gate == "ssh":
        return "ssh"
    if gate in ("tests", "light") and any(rx.search(reason) for rx in (CPU_HOLD, MEMORY_HOLD, SWAP_HOLD)):
        return "laptop"
    return "other"


def laptop_phrases(rows: list[tuple[str, str]]) -> list[str]:
    """One phrase per gate and limit kind, quoting the worst reading: the highest CPU busy or swap used,
    or the lowest free memory.

    The broker refreshes each held job's reason on its own tick, so the numbers differ a little across jobs.
    """
    buckets: dict[tuple[str, str], list] = {}
    for gate, reason in rows:
        cpu, memory, swap = CPU_HOLD.search(reason), MEMORY_HOLD.search(reason), SWAP_HOLD.search(reason)
        if cpu:
            kind, rank = "cpu", float(cpu.group(1))
            text = f"CPU {rank:.0f}% busy, limit {float(cpu.group(2)):.0f}%"
        elif swap:
            kind, rank = "swap", float(swap.group(1))
            text = f"swap {rank:.0f}% used, limit {float(swap.group(2)):.0f}%"
        else:
            kind, rank = "memory", -float(memory.group(1))
            text = f"memory {-rank:.0f}% free, floor {memory.group(2)}%"
        bucket = buckets.setdefault((gate, kind), [0, rank, text])
        bucket[0] += 1
        if rank > bucket[1]:
            bucket[1], bucket[2] = rank, text
    return [
        f"{count} {'test job' if gate == 'tests' else 'light job'}{'' if count == 1 else 's'} ({text})"
        for (gate, _), (count, _, text) in buckets.items()
    ]


def hold_body(group: str, gate: str, reason: str, ssh_in_use: int | None) -> tuple[str, str]:
    """(singular, plural) noun phrase for one held reason of a non-laptop group, without its count."""
    if group == "ssh":
        in_use = f" ({ssh_in_use} in use)" if ssh_in_use is not None else ""
        return f"job waiting for a production ssh slot{in_use}", f"jobs waiting for a production ssh slot{in_use}"
    if group == "proxy":
        return (
            f"DeepSeek job waiting at the Morph proxy ({reason})",
            f"DeepSeek jobs waiting at the Morph proxy ({reason})",
        )
    if group == "workspace":
        return "write job waiting for its workspace", "write jobs waiting for their workspace"
    detail = f"{gate}: {reason}" if gate else reason
    return f"job held ({detail})", f"jobs held ({detail})"


def held_verdict(details: list[str], ssh_in_use: int | None) -> tuple[str, str] | None:
    """(key, text) when ready DeepSeek jobs carry a broker hold, else None.

    The chip follows the largest group of held jobs (ties keep HOLD_GROUP_ORDER). Each group names its
    reasons with counts; the other groups follow after a semicolon.
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    for detail in details:
        if detail:
            gate, reason = split_hold(detail)
            groups.setdefault(hold_group(gate, reason), []).append((gate, reason))
    if not groups:
        return None
    primary = max(HOLD_GROUP_ORDER, key=lambda group: len(groups.get(group, ())))
    phrases = []
    for group in (primary, *(g for g in HOLD_GROUP_ORDER if g in groups and g != primary)):
        if group == "laptop":
            phrases.append("; ".join(laptop_phrases(groups[group])))
            continue
        counts: dict[tuple[str, str], int] = {}
        for gate, reason in groups[group]:
            body = hold_body(group, gate, reason, ssh_in_use)
            counts[body] = counts.get(body, 0) + 1
        phrases.append("; ".join(f"{n} {singular if n == 1 else plural}" for (singular, plural), n in counts.items()))
    if primary == "laptop":
        total = sum(len(rows) for rows in groups.values())
        return HOLD_GROUP_KEYS[primary], f"{total} DeepSeek jobs held for laptop headroom: {'; '.join(phrases)}."
    return HOLD_GROUP_KEYS[primary], f"{'; '.join(phrases)}."


def fill_near_target(raw: dict[int, dict], until: int, fill: dict | None) -> bool:
    """True when the fill controller is on and DeepSeek's own 5-minute billed average is at or above its low mark.

    Kimi and other models are excluded: the fill controller steers DeepSeek alone.
    """
    if not fill or not fill.get("enabled"):
        return False
    average = window_sum(raw, until, LIMIT_WINDOW_MINUTES, "ds_total") / LIMIT_WINDOW_MINUTES
    return average >= fill["low_frac"] * fill["target_billed_per_min"]


FILL_DEFAULTS = {"enabled": False, "target_billed_per_min": 8_000_000, "low_frac": 0.9}


def read_fill(home: Path) -> dict:
    """The fill controller's headline settings from broker.json's "fill" section (defaults mirror agentq/fill.py).

    The dashboard runs as a script, so it reads these three keys itself rather than importing agentq.fill.
    """
    try:
        data = json.loads((home / "broker.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    section = data.get("fill") if isinstance(data, dict) else None
    section = section if isinstance(section, dict) else {}
    enabled = section.get("enabled", FILL_DEFAULTS["enabled"])
    numbers = {}
    for name in ("target_billed_per_min", "low_frac"):
        try:
            numbers[name] = float(section[name])
        except (KeyError, TypeError, ValueError):
            numbers[name] = float(FILL_DEFAULTS[name])
    return {"enabled": bool(enabled), **numbers}


def ttfb_slow(raw: dict[int, dict], until: int, baseline_ms: int | None, limit: int | None) -> dict | None:
    """DeepSeek first-byte slowdown over the last 5 complete minutes, or None.

    Slow when at least TTFB_SLOW_MINUTES of those minutes have a DeepSeek first-byte p50 at or above
    TTFB_SLOW_FACTOR times the baseline. The text quotes the 5-minute median, not one minute.
    """
    five = range(until - LIMIT_WINDOW_MINUTES * 60, until, 60)
    values = [int(raw[t]["ds_ttfb_p50_ms"]) for t in five if t in raw and raw[t].get("ds_ttfb_p50_ms")]
    if not values or not baseline_ms:
        return None
    slow_minutes = sum(1 for v in values if v >= TTFB_SLOW_FACTOR * baseline_ms)
    if slow_minutes < TTFB_SLOW_MINUTES:
        return None
    return {
        "ttfb_ms": round(statistics.median(values)),
        "baseline_ms": baseline_ms,
        "slow_minutes": slow_minutes,
        "minutes": len(values),
        "limit": limit,
    }


def slow_text(slow: dict, *, shadow: bool) -> str:
    """The Morph slow headline. In shadow mode the limit is named as what it would be, not what it is."""
    base = f"Morph is slow: first byte {slow['ttfb_ms'] / 1000:.1f} s vs {slow['baseline_ms'] / 1000:.1f} s usual"
    if slow.get("limit") is None:
        return f"{base}; extra workers would only queue at Morph."
    if shadow:
        return f"{base}; extra workers would only queue at Morph (shadow: would limit to {slow['limit']} in flight)."
    return f"{base}; extra workers would only queue at Morph (limit {slow['limit']} in flight)."


def budget_429_in_window(budget_429s: dict[str, set[int]] | None, minutes: range) -> bool:
    """True when any Morph budget that the headline counts returned a 429 in these minutes."""
    return any(t in (budget_429s or {}).get(name, set()) for name in HEADLINE_429_BUDGETS for t in minutes)


def proxy_budget_waiting_ds(live: dict | None) -> int:
    """DeepSeek requests the proxy holds for a budget, not for the in-flight limit.

    A proxy that reports blocked_by_family counts only budget holds. An older proxy reports queues only,
    so its queued count stands in.
    """
    budgets = (live or {}).get("budgets")
    if not isinstance(budgets, dict):
        return 0
    if any(isinstance((b or {}).get("blocked_by_family"), dict) for b in budgets.values()):
        return sum(int((b or {}).get("blocked_by_family", {}).get(DEEPSEEK_FAMILY) or 0) for b in budgets.values() if isinstance(b, dict))
    waiting = proxy_waiting_by_budget(live)
    return sum(waiting.values()) if waiting is not None else 0


def verdict(
    raw: dict[int, dict],
    until: int,
    limits: dict[str, int],
    live: dict | None,
    *,
    kimi_jobs_waiting: int,
    budget_429s: dict[str, set[int]],
    laptop: dict,
    ds_jobs_ready: int | None,
    slow: dict | None = None,
    ds_holds: list[str] | None = None,
    ssh_in_use: int | None = None,
    filler_running: int = 0,
    fill: dict | None = None,
) -> dict:
    """What limits the DeepSeek pool, from the last 5 complete minutes. The first matching rule wins:
    Morph limit, Morph slow, Near capacity, Proxy pacing, Launching (which names a laptop hold as the Laptop
    chip when the broker holds ready DeepSeek jobs on laptop headroom), Headroom. Laptop pressure that the
    broker is not holding on goes to the secondary line, never to the headline.

    Launching names the broker's hold reason when ready DeepSeek jobs carry one (``ds_holds``, the queued
    jobs' detail strings), and says the broker is starting them otherwise.

    Only Morph's own budgets can make the Morph limit verdict. The total pool counts when its 5-minute average
    reached AVERAGE_SATURATION_FRAC of its limit, or when upstream 429s named it in MORPH_429_MINUTES of the 5
    minutes. DeepSeek uncached input is our estimate of Morph's count, so it counts only on 429s naming it.
    The peak minute is context only: it never decides the verdict. Kimi never decides the headline: its
    budget is not checked here, and waiting Kimi reviews add only the secondary line.

    Waiting work in the headline is DeepSeek only: requests queued at the proxy and DeepSeek jobs ready in
    agentq. Kimi waits never change the headline; they add a secondary line instead.
    """
    five = list(range(until - LIMIT_WINDOW_MINUTES * 60, until, 60))
    waiting = proxy_waiting_by_budget(live)
    ds_waiting = sum(waiting.values()) if waiting is not None else None
    kimi_waiting = kimi_jobs_waiting > 0
    hits = []
    for name in dict.fromkeys(HEADLINE_AVERAGE_BUDGETS + HEADLINE_429_BUDGETS):
        field = BUDGET_FIELDS[name]
        average = sum(int((raw.get(t) or {}).get(field) or 0) for t in five) / LIMIT_WINDOW_MINUTES / limits[name]
        peak = max(int((raw.get(t) or {}).get(field) or 0) for t in five) / limits[name]
        minutes_429 = sum(1 for t in five if t in budget_429s.get(name, set()))
        by_average = name in HEADLINE_AVERAGE_BUDGETS and average >= AVERAGE_SATURATION_FRAC
        by_429 = name in HEADLINE_429_BUDGETS and minutes_429 >= MORPH_429_MINUTES
        if by_average or by_429:
            hits.append((name, average, peak, minutes_429, by_average))
    pool = window_sum(raw, until, LIMIT_WINDOW_MINUTES, "total") / LIMIT_WINDOW_MINUTES / limits["total"]
    bars = laptop.get("bars") or LAPTOP_BAR_DEFAULTS
    secondary_parts = []
    if kimi_waiting:
        secondary_parts.append(
            f"Kimi reviews waiting: {kimi_jobs_waiting} "
            f"(Kimi's own {limits['uncached']:,}/min uncached limit)."
        )
    if hits:
        name, average, peak, minutes_429, by_average = hits[0]
        label = LIMIT_LABELS[name]
        context = f"peak minute {peak * 100:.0f}%"
        if by_average:
            detail = f"{label} averaged {average * 100:.0f}% of its limit over the last 5 minutes ({context})"
        else:
            detail = f"{label} returned 429s in {minutes_429} of the last 5 minutes ({context})"
        key, text = "morph_limit", f"Morph is limiting us: {detail}."
    elif slow is not None and proxy_budget_waiting_ds(live) == 0 and not budget_429_in_window(budget_429s, five):
        key = "morph_slow"
        text = slow_text(slow, shadow=(live or {}).get("inflight_mode") == "shadow")
    elif pool >= POOL_NEAR_CAPACITY_FRAC:
        key = "near_capacity"
        text = f"Near capacity: pool at {pool * 100:.0f}% of Morph's {limits['total'] // 1_000_000}M/min limit."
    elif filler_running and fill_near_target(raw, until, fill) and not ds_waiting and not ds_jobs_ready:
        key = "filling"
        text = f"Holding near target: {filler_running} filler runs filling the gap."
    elif ds_waiting:
        key = "proxy_pacing"
        budget = max((name for name in waiting if waiting[name] > 0), key=lambda name: waiting[name])
        text = (
            f"Proxy pacing: DeepSeek requests are waiting on {LIMIT_LABELS[budget]} "
            f"while the pool is at {pool * 100:.0f}%."
        )
    elif ds_jobs_ready:
        held = held_verdict(ds_holds or [], ssh_in_use)
        key, text = held or (
            "launching", f"Launching: {ds_jobs_ready} DeepSeek jobs are ready; the broker is starting them.",
        )
    else:
        key = "headroom"
        text = f"Headroom: pool at {pool * 100:.0f}% of Morph's limit; room for more work."
    pressure = laptop_pressure(laptop, bars)
    if pressure and key != "held_laptop":
        secondary_parts.append(f"Laptop pressure, not holding DeepSeek jobs: {pressure}.")
    secondary = " ".join(secondary_parts) or None
    return {"key": key, "chip": VERDICT_CHIPS[key], "text": text, "secondary": secondary}


def room(
    raw: dict[int, dict],
    until: int,
    limits: dict[str, int],
    *,
    active_ds_workers: float | None,
    laptop: dict,
) -> dict:
    """How many more busy DeepSeek workers fit: the smallest of three candidates, each in workers.

    Morph total and Morph uncached input (ds_uncached) use ROOM_FRAC of their limit less the 5-minute average
    per minute, divided by the per-worker rate. The laptop uses the light-job CPU busy bar: workers fit while
    CPU busy stays under that bar, each worker adding busy / workers of CPU. A candidate with no per-worker
    rate, or no CPU reading, is left out.
    """
    workers = active_ds_workers
    total5 = window_sum(raw, until, LIMIT_WINDOW_MINUTES, "total") / LIMIT_WINDOW_MINUTES
    uncached5 = window_sum(raw, until, LIMIT_WINDOW_MINUTES, "ds_uncached") / LIMIT_WINDOW_MINUTES
    candidates: dict[str, float] = {}
    if workers and workers > 0:
        if total5 > 0:
            candidates["total"] = (ROOM_FRAC * limits["total"] - total5) / (total5 / workers)
        if uncached5 > 0:
            candidates["uncached_ds"] = (ROOM_FRAC * limits["uncached_ds"] - uncached5) / (uncached5 / workers)
        busy = laptop.get("cpu_busy")
        bar = (laptop.get("bars") or LAPTOP_BAR_DEFAULTS)["light_max_cpu_busy"]
        if busy is not None and busy > 0:
            candidates["laptop"] = max(0.0, bar - busy) * workers / busy
    if not candidates:
        return {
            "workers": None, "bound": None, "active_ds_workers": workers,
            "text": "Room unknown: no DeepSeek workers active in the last 5 minutes.",
        }
    bound = min(candidates, key=candidates.get)
    count = math.floor(candidates[bound])
    label = ROOM_BOUND_LABELS[bound]
    if count <= 0:
        text = f"No room; adding workers will only queue ({label})."
    else:
        text = f"Room for about {count} more busy DeepSeek workers ({label})."
    return {
        "workers": count, "bound": bound, "active_ds_workers": workers,
        "text": text, "candidates": {k: round(v, 2) for k, v in candidates.items()},
    }


def build_summary(
    home: Path, hours: int = DEFAULT_HOURS, *, now: float | None = None, fetch_live=fetch_live_status, laptop: dict | None = None
) -> dict:
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
    run_rows = _guard("runs", errors, read_run_rows, [], agentq_db, int(now - RUN_LOOKBACK_S))
    caps = read_caps(home)
    targets = live_targets(live)
    resolved = resolve_limits(live)
    limits = {
        name: {
            "label": LIMIT_LABELS[name],
            "limit": LIMITS[name],
            "last_min_frac": last_minute_fraction(raw, until, LIMIT_KEYS[name], LIMITS[name]),
            "avg_5m_frac": average_fraction(raw, until, LIMIT_KEYS[name], LIMITS[name]),
            "range_avg_frac": range_average_fraction(raw, since, until, LIMIT_KEYS[name], LIMITS[name]),
            "target_frac": targets[name],
        }
        for name in LIMIT_KEYS
    }
    runs = shape_runs(run_rows, now)
    minute_points = minute_series(raw, since, until)
    apply_admitted_totals(minute_points, admitted)
    add_trailing_average(minute_points, "pct_total", "pct_total_avg5", LIMIT_WINDOW_MINUTES)
    minutes = bucket_minutes(minute_points, bucket)
    token_bucket = TOKEN_BUCKET_S[hours]
    token_minutes = bucket_minutes(minute_points, token_bucket)
    mix = token_mix(token_minutes)
    limit = token_limit(resolved["total"], token_bucket)
    range_verdicts = _guard("range_verdicts", errors, read_verdicts, {}, agentq_db, float(since), float(until))
    workers_bucketed = bucket_workers(samples, bucket)
    kpis = run_kpis(runs, since, caps)
    laptop_now = laptop if laptop is not None else laptop_reading(home, now)
    baseline_raw = raw if hours >= 6 else _guard("minutes_6h", errors, read_minute_rows, {}, agentq_db, until - 6 * 3600, until)
    worker_vals = [s["ds_workers"] for s in samples if until - LIMIT_WINDOW_MINUTES * 60 <= s["ts"] < until and s["ds_workers"] is not None]
    jobs_by_tier = (jobs or {}).get("by_tier") or {}
    verdict_now = verdict(
        raw, until, resolved, live,
        kimi_jobs_waiting=int((jobs_by_tier.get("pi-kimi") or {}).get("queued") or 0),
        budget_429s=budget_429_minutes(home, until),
        laptop=laptop_now,
        ds_jobs_ready=int((jobs_by_tier.get("pi-deepseek-flash") or {}).get("queued") or 0),
        slow=ttfb_slow(raw, until, live_ttfb_baseline(live, baseline_raw, until), live_inflight_limit(live)),
        ds_holds=(jobs or {}).get("ds_holds"),
        ssh_in_use=int((jobs or {}).get("running_by_res", {}).get("ssh", 0)) if jobs else None,
        filler_running=int((jobs or {}).get("filler_running") or 0),
        fill=read_fill(home),
    )
    room_now = room(
        raw, until, resolved,
        active_ds_workers=(sum(worker_vals) / len(worker_vals)) if worker_vals else None,
        laptop=laptop_now,
    )
    calib_429s = _guard("calib_429s", errors, read_calib_429s, None, home)
    activity = {
        "tokens_5m": window_tokens(raw, until, 5),
        "tokens_15m": window_tokens(raw, until, 15),
        "pool_per_minute": pool_per_minute(raw, admitted, until),
    }
    return {
        "generated_at": now,
        "hours": hours,
        "window": {"start": since, "end": until},
        "live": live,
        "limits": limits,
        "capacity": capacity_headline(limits, live),
        "r429_60m": recent_429s(raw, until),
        "ds_uncached_rejections": budget_rejections(calib_429s, "uncached_ds", since, until),
        "ds_output_5m_per_min": deepseek_output_rate(raw, until),
        "bucket_s": bucket,
        "minutes": minutes,
        "token_mix": mix,
        "token_totals": token_totals(mix),
        "token_bucket_s": token_bucket,
        "token_limit": limit,
        "token_axes": token_axes(mix, limit),
        "range_totals": range_totals(raw, since, until, runs, range_verdicts),
        "jobs": jobs,
        "workers": workers_bucketed,
        "runs_axis": runs_axis(workers_bucketed, caps),
        "caps": caps,
        "runs": table_runs(runs),
        "run_kpis": kpis,
        "verdict": verdict_now,
        "room": room_now,
        "activity": activity,
        "laptop": laptop_now,
        "perf_kpis": perf_kpis(raw, since, until, runs, now, live),
        "top_consumers": top_consumers(runs, since, kpis["active_lifetime_tokens"]),
        "errors": errors,
    }


def shape_runs(rows: list[dict], now: float) -> list[dict]:
    """Every run in the lookback, most recent activity first, with the untagged aggregate last."""
    return sorted((shape_run(row, now) for row in rows), key=lambda run: run["untagged"])


def table_runs(runs: list[dict]) -> list[dict]:
    """The rows the runs table shows: the newest RUN_ROWS tagged runs, then the untagged row, which is never cut."""
    tagged = [run for run in runs if not run["untagged"]]
    return tagged[:RUN_ROWS] + [run for run in runs if run["untagged"]]


def ensure_split_columns(connection: sqlite3.Connection) -> None:
    """Add the per-tier queue columns to a samples table written before they existed.

    Call inside a write transaction. A column another writer added first is not an error.
    """
    present = {row[1] for row in connection.execute("PRAGMA table_info(samples)").fetchall()}
    for column in SPLIT_COLUMNS:
        if column in present:
            continue
        try:
            connection.execute(f"ALTER TABLE samples ADD COLUMN {column} INT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc):
                raise


def write_sample(dash_db: Path, row: dict) -> None:
    """Store one worker sample and prune samples older than SAMPLE_KEEP_S. Never touches agentq.db.

    The schema check, the column migration, the insert and the prune share one IMMEDIATE transaction, so
    concurrent writers on an old-format file serialize instead of racing the ALTER.
    """
    dash_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(dash_db, timeout=DB_TIMEOUT_S, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(SAMPLES_DDL)
            ensure_split_columns(connection)
            connection.execute(SAMPLE_INSERT_SQL, tuple(row[column] for column in SAMPLE_COLUMNS))
            connection.execute("DELETE FROM samples WHERE ts < ?", (row["ts"] - SAMPLE_KEEP_S,))
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()


def read_running_queue(agentq_db: Path) -> int:
    """Queue jobs running right now: each one is a queue worker. Read-only."""
    connection = readonly_connection(agentq_db)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM jobs WHERE state = 'running'").fetchone()[0])
    finally:
        connection.close()


def sample_once(home: Path, ps_runner=run_ps, now: float | None = None) -> dict:
    """Take one worker sample. Raises when ps fails, so the gap shows instead of a false zero.

    ps no longer reports environments, so queue workers come from the jobs table when it is readable, and
    the environment count is the fallback. Direct workers are every other launch_pi process.
    """
    home = Path(home)
    now = time.time() if now is None else now
    row = {"ts": now, **parse_workers(ps_runner()), "queued_jobs": None, "ds_queued": None, "kimi_queued": None}
    total = row["queue_workers"] + row["direct_workers"]
    try:
        row["queue_workers"] = read_running_queue(home / "agentq.db")
        row["direct_workers"] = max(0, total - row["queue_workers"])
    except (sqlite3.Error, FileNotFoundError):
        pass
    try:
        by_tier = read_queued_by_tier(home / "agentq.db")
    except (sqlite3.Error, FileNotFoundError):
        by_tier = None
    if by_tier is not None:
        row["queued_jobs"] = sum(by_tier.values())
        row["ds_queued"] = by_tier.get(DS_TIER, 0)
        row["kimi_queued"] = by_tier.get(KIMI_TIER, 0)
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


class BackgroundReader(threading.Thread):
    """Keeps the last value of one slow reader (a subprocess or an HTTP probe) current on its own thread.

    The request path only reads ``latest()``, so a slow reader never holds up a response. Reads run only
    while ``active()`` is true, and ``wake`` starts one at once when a page asks for data.
    """

    def __init__(self, name: str, read, active, unread=None, cadence_s: float = READER_CADENCE_S, clock=time.time) -> None:
        super().__init__(name=f"agentq-dash-{name}", daemon=True)
        self.read = read
        self.active = active
        self.unread = unread
        self.cadence_s = cadence_s
        self.clock = clock
        self.value = None
        self.at: float | None = None
        self.wake = threading.Event()
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            if self.active():
                self.refresh()
            self.wake.wait(self.cadence_s)
            self.wake.clear()

    def refresh(self) -> None:
        try:
            value = self.read()
        except Exception:
            value = None
        self.value, self.at = value, self.clock()

    def latest(self):
        """The last value read, at once. ``unread`` until the first read has finished; never waits on a read."""
        return self.value if self.at is not None else self.unread

    def age(self, now: float) -> float | None:
        return None if self.at is None else round(max(0.0, now - self.at), 1)

    def stop(self) -> None:
        self.stop_event.set()
        self.wake.set()


class SummaryCache:
    """Serves the summary JSON as stored bytes, computed off the request path.

    Computes run on their own threads and never under a lock: the lock guards only the stored bodies and
    the in-flight markers, and a request never waits for a compute or a reader. A range with no stored body
    gets a DB-only partial summary, computed under REQUEST_BUDGET_S and stored with ``partial`` set; the full
    compute replaces it. A watchdog starts a fresh compute for any range whose data is older than
    WATCHDOG_FACTOR times its cadence, logging once per episode. Nothing is computed while nobody has asked
    for the page in IDLE_AFTER_S.
    """

    def __init__(self, home: Path, fetch_live=fetch_live_status, clock=time.time) -> None:
        self.home = Path(home)
        self.clock = clock
        self.last_request: float | None = None
        self.bodies: dict[int, tuple[bytes, float, bool]] = {}
        self.inflight: dict[int, object] = {}
        self.stale_logged: set[int] = set()
        self.forced_at: dict[int, float] = {}
        self.threads: list[threading.Thread] = []
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.live = BackgroundReader("live", fetch_live, self.active, clock=clock)
        self.laptop = BackgroundReader(
            "laptop", lambda: laptop_reading(self.home, time.time()), self.active, unread=laptop_unread(), clock=clock
        )
        self.thread = threading.Thread(target=self._loop, name="agentq-dash-summary", daemon=True)

    def start(self) -> None:
        self.live.start()
        self.laptop.start()
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake.set()
        self.live.stop()
        self.laptop.stop()

    def touch(self) -> None:
        """Record that someone is looking, and wake the workers so the data is current within a tick."""
        self.last_request = self.clock()
        self.wake.set()
        self.live.wake.set()
        self.laptop.wake.set()

    def active(self) -> bool:
        return self.last_request is not None and self.clock() - self.last_request <= IDLE_AFTER_S

    def get(self, hours: int) -> tuple[bytes, float]:
        """The stored JSON bytes and generated_at for one range. With nothing stored, a DB-only partial is built now."""
        self.touch()
        with self.lock:
            entry = self.bodies.get(hours)
        if entry is None:
            entry = self._store(hours, self._build(hours, REQUEST_BUDGET_S))
        return entry[0], entry[1]

    def _build(self, hours: int, budget: float) -> tuple[bytes, float, bool]:
        """One summary as JSON bytes, built with the SQLite waits bounded by ``budget`` seconds."""
        now = self.clock()
        with time_budget(budget):
            summary = build_summary(self.home, hours, now=now, fetch_live=self.live.latest, laptop=self.laptop.latest())
        summary["source_age_s"] = {"live": self.live.age(now), "laptop": self.laptop.age(now)}
        summary["cadence_s"] = RANGE_CADENCE_S[hours]
        partial = any("time budget" in message for message in summary["errors"].values())
        summary["partial"] = partial
        return json.dumps(summary).encode("utf-8"), now, partial

    def _store(self, hours: int, entry: tuple[bytes, float, bool]) -> tuple[bytes, float, bool]:
        """Keep the newest body, but never let a partial replace a full one."""
        with self.lock:
            current = self.bodies.get(hours)
            if current is None or (current[2] and not entry[2]) or (current[2] == entry[2] and entry[1] >= current[1]):
                self.bodies[hours] = entry
                if not entry[2]:
                    self.stale_logged.discard(hours)
                return entry
            return current

    def _start_compute(self, hours: int, force: bool = False) -> bool:
        """Start one full compute for a range on its own thread. Unless forced, skipped while one is in flight."""
        with self.lock:
            if hours in self.inflight and not force:
                return False
            token = object()
            self.inflight[hours] = token
        worker = threading.Thread(
            target=self._run_compute, args=(hours, token), name=f"agentq-dash-{hours}h", daemon=True
        )
        self.threads = [t for t in self.threads if t.is_alive()] + [worker]
        worker.start()
        return True

    def _run_compute(self, hours: int, token: object) -> None:
        try:
            self._store(hours, self._build(hours, BACKGROUND_BUDGET_S))
        except Exception as exc:
            sys.stderr.write(f"agentq dash: {hours}h summary skipped: {type(exc).__name__}: {exc}\n")
        finally:
            with self.lock:
                if self.inflight.get(hours) is token:
                    del self.inflight[hours]

    def wait_for_computes(self, timeout: float = 30.0) -> None:
        """Wait for the compute threads started so far (tests use this; the server never waits)."""
        for worker in list(self.threads):
            worker.join(timeout)

    def tick(self) -> None:
        """One worker pass: start the computes that are due, and run the watchdog. Never blocks on a compute."""
        if not self.active():
            return
        now = self.clock()
        for hours in RANGE_HOURS:
            if self.stop_event.is_set():
                return
            cadence = RANGE_CADENCE_S[hours]
            with self.lock:
                entry = self.bodies.get(hours)
            age = None if entry is None else now - entry[1]
            if entry is None or entry[2] or age >= cadence:
                self._start_compute(hours)
            if age is not None and age > WATCHDOG_FACTOR * cadence:
                self._watchdog(hours, age, cadence, now)

    def _watchdog(self, hours: int, age: float, cadence: float, now: float) -> None:
        """A range older than WATCHDOG_FACTOR x cadence: log it once per episode and recompute on a fresh thread."""
        if hours not in self.stale_logged:
            self.stale_logged.add(hours)
            sys.stderr.write(
                f"agentq dash: {hours}h summary is {age:.0f} s old (over {WATCHDOG_FACTOR:g}x its {cadence:g} s cadence); "
                "recomputing on a fresh worker\n"
            )
        if now - self.forced_at.get(hours, float("-inf")) >= cadence:
            self.forced_at[hours] = now
            self._start_compute(hours, force=True)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            self.tick()
            self.wake.wait(WORKER_TICK_S)
            self.wake.clear()


def laptop_unread() -> dict:
    """The laptop reading served before the first read lands: the CPU count is known, nothing else is."""
    return {
        "load1": None, "cpus": os.cpu_count() or 1, "load_per_cpu": None, "memory_free_pct": None,
        "cpu_busy": None, "swap_frac": None, "bars": dict(LAPTOP_BAR_DEFAULTS),
    }


def note_client_gone(server: "DashServer") -> None:
    """Log that a client hung up, at most once per GONE_LOG_S. A browser that stops waiting is not a server error."""
    now = time.time()
    if now - server.gone_logged_at >= GONE_LOG_S:
        server.gone_logged_at = now
        sys.stderr.write("agentq dash: client closed the connection early (logged at most once a minute)\n")


class Handler(BaseHTTPRequestHandler):
    """GET / serves the page; GET /api/summary?hours=N serves the JSON summary."""

    server_version = "agentq-dash"

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if parts.path == "/":
            self.server.summaries.touch()
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
            body, _ = self.server.summaries.get(hours)
            self._send(200, "application/json", body)
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

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            note_client_gone(self.server)
            self.close_connection = True

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


class DashServer(ThreadingHTTPServer):
    """The dashboard's HTTP server on 127.0.0.1, with the summary cache and its workers attached."""

    def __init__(self, address: tuple[str, int], home: Path, fetch_live) -> None:
        super().__init__(address, Handler)
        self.home = Path(home)
        self.gone_logged_at = float("-inf")
        self.summaries = SummaryCache(self.home, fetch_live)
        self.summaries.start()

    def server_close(self) -> None:
        self.summaries.stop()
        super().server_close()


def make_server(home: Path, port: int, *, fetch_live=fetch_live_status) -> DashServer:
    """A server bound to 127.0.0.1. Port 0 picks a free port."""
    return DashServer(("127.0.0.1", port), home, fetch_live)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentq-dash", description="Live agentq dashboard on 127.0.0.1.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--home", default=os.environ.get("AGENTQ_HOME", DEFAULT_HOME))
    args = parser.parse_args(argv)
    faulthandler.register(signal.SIGUSR1, all_threads=True)
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
