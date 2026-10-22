-- Frozen minimal schema of the release BEFORE the lead-split change.
--
-- Mirrors a live database from that release: ``runs`` has no
-- ``slice_of``, ``usage_events`` has no ``ws_split``, and neither
-- ``idx_runs_slice`` nor ``idx_usage_session`` exists. Opening this
-- with the current Store must add every column and index (see
-- tests/test_migration.py); it is the regression guard for the outage
-- where CREATE INDEX idx_runs_slice ran before the ALTER TABLE that
-- adds the column (sqlite3.OperationalError: no such column:
-- slice_of), which fresh-DB tests could not catch.
--
-- Refresh at each release with: python3 scripts/schema_snapshot.py
-- (it dumps the current fresh-DB schema; hand-remove the newest
-- columns/indexes to freeze the new "previous" baseline, or keep the
-- dump as-is to guard the current shape).
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  source TEXT,
  tier TEXT,
  model TEXT,
  label TEXT,
  session_id TEXT,
  parent_session_id TEXT,
  workstream TEXT,
  ws_confidence TEXT,
  status TEXT,
  verdict TEXT,
  started_at REAL,
  ended_at REAL,
  clock_source TEXT,
  out_bytes INTEGER,
  out_growing INTEGER DEFAULT 0,
  err_bytes INTEGER,
  pid INTEGER,
  out_path TEXT
);
CREATE TABLE usage_events (
  tool TEXT NOT NULL,
  session_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  ts REAL,
  run_id TEXT,
  model TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read INTEGER,
  cache_write INTEGER,
  cache_write_5m INTEGER,
  cache_write_1h INTEGER,
  reasoning INTEGER,
  est_cost_usd REAL,
  PRIMARY KEY (tool, session_id, message_id)
);
CREATE TABLE usage_buckets (
  hour_ts REAL NOT NULL,
  model TEXT NOT NULL,
  workstream TEXT NOT NULL,
  tool TEXT NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read INTEGER,
  cache_write INTEGER,
  reasoning INTEGER,
  missing INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL,
  PRIMARY KEY (hour_ts, model, workstream, tool)
);
CREATE TABLE process_samples (
  ts REAL NOT NULL,
  pid INTEGER NOT NULL,
  comm TEXT,
  rss_kb INTEGER,
  elapsed_s REAL,
  label TEXT,
  run_id TEXT,
  model TEXT
);
CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY,
  tool TEXT,
  cwd TEXT,
  started_at REAL,
  status TEXT,
  parent_session_id TEXT,
  label TEXT,
  workstream TEXT,
  ws_confidence TEXT,
  ws_hint TEXT
);
CREATE TABLE ws_counts (
  ws TEXT PRIMARY KEY,
  name TEXT,
  backlog INTEGER,
  todo INTEGER,
  doing INTEGER,
  review INTEGER,
  done INTEGER,
  cancelled INTEGER
);
CREATE TABLE file_offsets (
  path TEXT PRIMARY KEY,
  inode INTEGER NOT NULL,
  offset INTEGER NOT NULL,
  size INTEGER,
  mtime REAL
);
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE dagster_seen (
  run_id TEXT PRIMARY KEY,
  last_seen REAL
);
CREATE TABLE run_aliases (
  alias_run_id TEXT PRIMARY KEY,
  target_run_id TEXT NOT NULL,
  merged_at REAL NOT NULL
);
CREATE TABLE doctor_problems (
  id TEXT PRIMARY KEY,
  code TEXT,
  severity TEXT,
  scope TEXT,
  ws TEXT,
  title TEXT,
  detail TEXT,
  fix_command TEXT,
  safe_auto INTEGER DEFAULT 0,
  first_seen REAL,
  last_seen REAL,
  count INTEGER DEFAULT 1,
  resolved_at REAL,
  regressed INTEGER DEFAULT 0,
  items_json TEXT
);
CREATE TABLE doctor_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE INDEX idx_doctor_code ON doctor_problems(code);
CREATE INDEX idx_doctor_open ON doctor_problems(resolved_at);
CREATE INDEX idx_runs_status ON runs(status);
CREATE INDEX idx_runs_ended ON runs(ended_at);
CREATE INDEX idx_runs_ws ON runs(workstream);
CREATE INDEX idx_runs_pid ON runs(pid);
CREATE INDEX idx_runs_source ON runs(source);
CREATE INDEX idx_runs_label ON runs(label);
CREATE INDEX idx_runs_parent ON runs(parent_session_id);
CREATE INDEX idx_runs_session ON runs(session_id);
CREATE INDEX idx_usage_run ON usage_events(run_id);
CREATE INDEX idx_usage_ts ON usage_events(ts);
CREATE INDEX idx_samples_ts ON process_samples(ts);
CREATE INDEX idx_samples_pid ON process_samples(pid);
CREATE INDEX idx_samples_run ON process_samples(run_id);
CREATE INDEX idx_aliases_target ON run_aliases(target_run_id);
INSERT INTO runs (run_id, source, tier, model, label, session_id, workstream, ws_confidence, status, started_at, ended_at)
  VALUES ('prev-run-1', 'claude', 'opus', 'claude-opus-4-6', 'ws1-1 old row', 'prev-s-1', 'WS1', 'explicit', 'done', 1000.0, 1100.0);
INSERT INTO usage_events (tool, session_id, message_id, ts, run_id, model, input_tokens, output_tokens, cache_read, cache_write, reasoning, est_cost_usd)
  VALUES ('claude', 'prev-s-1', 'prev-m-1', 1050.0, 'prev-run-1', 'claude-opus-4-6', 100, 10, 0, 0, 0, 0.001);
