"""Process/manifest placeholders never become a "model not recorded" bucket.

A worker-source row (muse, glm, grok) with no model, no verdict, and no
usage is a marker: a launch that never produced anything, or a live
process seen before its session/manifest/usage match. Markers stay out of
the model legends, the live model counts, the finished counts, and the run
lists, while real zero-token runs (a verdict) and honest unknowns (tokens
but no model) keep their rows. Synthetic fixtures plus one node proof that
static/models.js still labels a real unknown "model not recorded".
"""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault(
    "AGENT_DASH_WORKSTREAMS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fixtures",
        "bundles",
    ),
)

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash.models import run_model_key  # noqa: E402
from dash.store import Store, is_marker_run  # noqa: E402

STATIC = ROOT / "static"

SPARK = "muse-spark-1.3-contributor"
NOW = 1_792_000_000.0


def memory(path: Path) -> Store:
    return Store(path / "dash.sqlite")


def run(run_id, source="muse", tier="muse", status="done", **extra):
    row = {
        "run_id": run_id,
        "source": source,
        "tier": tier,
        "label": run_id.rsplit(":", 1)[-1],
        "status": status,
        "started_at": NOW - 300,
        "ended_at": NOW - 100,
    }
    row.update(extra)
    return row


def usage(run_id, model=SPARK, input=10, output=5, mid="m1"):
    return {
        "ts": NOW - 200,
        "session_id": "sess",
        "run_id": run_id,
        "model": model,
        "tool": "muse",
        "message_id": mid,
        "input": input,
        "output": output,
        "cache_read": 0,
        "cache_write": 0,
        "reasoning": 0,
    }


class MarkerPredicateTests(unittest.TestCase):
    def test_bare_worker_row_is_a_marker(self):
        self.assertTrue(is_marker_run(run("muse:s:ws9-x")))
        self.assertTrue(is_marker_run(run("grok:c:ws9-y", source="grok", tier="grok")))
        self.assertTrue(is_marker_run(run("glm:g:ws9-z", source="glm", tier="glm")))

    def test_model_verdict_or_tokens_is_not_a_marker(self):
        self.assertFalse(is_marker_run(run("muse:s:a", model=SPARK)))
        self.assertFalse(is_marker_run(run("muse:s:b", verdict="PASS")))
        self.assertFalse(
            is_marker_run(run("muse:s:c", tokens={"input": 3, "output": 0, "cache_read": 0, "cache_write": 0}))
        )

    def test_shell_and_claude_are_never_markers(self):
        self.assertFalse(is_marker_run(run("shell:p:job", source="shell", tier=None)))
        self.assertFalse(is_marker_run(run("claude-main:s:m", source="claude-main")))


class MarkerCountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def seed(self):
        live_marker = run("muse:live:starting", status="running", started_at=NOW - 60, ended_at=None)
        live_real = run("muse:live:ws9-live", status="running", started_at=NOW - 60, ended_at=None, model=SPARK)
        done_marker = run("muse:done:ws9-gone")
        done_real = run("muse:done:ws9-done", model=SPARK)
        honest = run("muse:done:ws9-honest")
        for row in (live_marker, live_real, done_marker, done_real, honest):
            self.store.upsert_run(row)
        self.store.add_usage(usage("muse:live:ws9-live", mid="m-live"))
        self.store.add_usage(usage("muse:done:ws9-done", mid="m-done"))
        self.store.add_usage(usage("muse:done:ws9-honest", model=None, input=7, output=0, mid="m-honest"))
        return live_marker, live_real, done_marker, done_real, honest

    def test_live_markers_skip_model_buckets_but_count_live(self):
        self.seed()
        snap = self.store.snapshot(NOW, "all")
        counts = snap["counts"]
        # Both live processes are real activity.
        self.assertEqual(counts["live_agents"], 2)
        self.assertEqual(counts["live_by_tool"]["muse"], 2)
        # Only the modeled run lends a model bucket: no muse-unknown.
        self.assertEqual(counts["live_by_model"], {run_model_key({"model": SPARK, "tier": "muse", "source": "muse"}): 1})
        self.assertNotIn("muse-unknown", counts["live_by_model"])

    def test_finished_counts_skip_done_markers(self):
        self.seed()
        snap = self.store.snapshot(NOW, "all")
        # Two finished agents (the modeled run and the honest unknown);
        # the placeholder that never ran is not one.
        self.assertEqual(snap["counts"]["finished_in_range"], 2)

    def test_lists_exclude_markers_but_keep_honest_unknown(self):
        live_marker, _live_real, done_marker, _done_real, honest = self.seed()
        snap = self.store.snapshot(NOW, "all")
        running_ids = [row["run_id"] for row in snap["running"]]
        recent_ids = [row["run_id"] for row in snap["recent"]]
        self.assertNotIn(live_marker["run_id"], running_ids)
        self.assertNotIn(done_marker["run_id"], recent_ids)
        self.assertIn("muse:live:ws9-live", running_ids)
        self.assertIn(honest["run_id"], recent_ids)
        listed = [row["run_id"] for row in self.store.query_runs(NOW, None, None, None, 100)]
        self.assertNotIn(live_marker["run_id"], listed)
        self.assertNotIn(done_marker["run_id"], listed)
        self.assertIn(honest["run_id"], listed)

    def test_concurrency_has_no_marker_lane(self):
        self.store.upsert_run(run("muse:done:ws9-gone"))
        self.store.upsert_run(
            run("muse:live:starting", status="running", started_at=NOW - 60, ended_at=None)
        )
        points = self.store.query_concurrency(NOW, None, NOW - 3600, 60)
        keys = set()
        for point in points:
            keys.update((point.get("models") or {}).keys())
        self.assertNotIn("muse-unknown", keys)

    def test_concurrency_keeps_the_honest_unknown_lane(self):
        self.store.upsert_run(run("muse:done:ws9-honest"))
        self.store.add_usage(usage("muse:done:ws9-honest", model=None, input=7, output=0, mid="m-honest"))
        points = self.store.query_concurrency(NOW, None, NOW - 3600, 60)
        keys = set()
        for point in points:
            keys.update((point.get("models") or {}).keys())
        # Tokens with no recorded model honestly read as "model not
        # recorded": the marker rule never hides real usage.
        self.assertIn("muse-unknown@muse", keys)


class MarkerMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_manifest_twin_merges_into_the_worker_row_with_its_model(self):
        self.store.upsert_run(
            {
                "run_id": "muse:parent1:ws9-x@1792353581",
                "source": "muse",
                "tier": "muse",
                "model": SPARK,
                "label": "ws9-x",
                "status": "done",
                "started_at": 1792353581.0,
                "ended_at": 1792353600.0,
                "clock_source": "manifest",
            }
        )
        self.store.upsert_run(
            {
                "run_id": "muse:11111111-1111-4111-8111-111111111111:ws9-x",
                "source": "muse",
                "tier": "muse",
                "label": "ws9-x",
                "status": "running",
                "started_at": 1792353590.0,
            }
        )
        merged = self.store.merge_manifest_rows("muse")
        self.assertEqual(merged, 1)
        rows = [dict(row) for row in self.store._conn.execute("SELECT * FROM runs").fetchall()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], SPARK)
        self.assertFalse(is_marker_run({**rows[0], "tokens": {}}))


class MarkerSourceTests(unittest.TestCase):
    def test_muse_manifest_model_column_parses(self):
        from dash.sources import muse as muse_mod

        rows = muse_mod._parse_manifest("ws9-x\t11111111-1111-4111-8111-111111111111\t1792353581\t%s\n" % SPARK)
        self.assertEqual(rows[0]["model"], SPARK)
        rows = muse_mod._parse_manifest("ws9-x\t11111111-1111-4111-8111-111111111111\t1792353581\n")
        self.assertIsNone(rows[0]["model"])

    def test_muse_upsert_uses_the_manifest_model(self):
        from dash.sources import muse as muse_mod

        seen = {}

        class FakeStore:
            def get_run(self, run_id):
                return None

            def upsert_run(self, row):
                seen.update(row)

            def upsert_session(self, row):
                pass

        sid = "11111111-1111-4111-8111-111111111111"
        manifests = {sid.lower(): {"label": "ws9-x", "session_id": sid, "started_at": 1792353581.0, "model": SPARK}}
        muse_mod._upsert_run(FakeStore(), sid, manifests, None, None, False, None, processes=[])
        self.assertEqual(seen["model"], SPARK)

    def test_muse_bare_intent_creates_no_run(self):
        from dash.sources import muse as muse_mod

        muse_mod.reset_state()

        class FakeStore:
            def __init__(self):
                self.runs = {}

            def get_run(self, run_id):
                return self.runs.get(run_id)

            def upsert_run(self, row):
                self.runs[row["run_id"]] = row

            def upsert_session(self, row):
                pass

        store = FakeStore()
        sid = "11111111-1111-4111-8111-111111111111"
        manifests = {sid.lower(): {"label": "ws9-x", "session_id": sid, "started_at": 1792353581.0, "model": None}}
        muse_mod._upsert_run(store, sid, manifests, None, None, False, None, processes=[])
        self.assertEqual(store.runs, {})


class PruneMarkersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = memory(self.root)
        self.marker = run("muse:done:ws9-gone")
        self.store.upsert_run(self.marker)
        self.store.upsert_run(run("muse:done:ws9-done", model=SPARK))
        self.store.upsert_run(run("muse:done:ws9-pass", verdict="PASS"))
        kept_usage = run("muse:done:ws9-used")
        self.store.upsert_run(kept_usage)
        self.store.add_usage(usage("muse:done:ws9-used", model=None, mid="m-used"))
        self.store.upsert_run(run("muse:live:ws9-live", status="running", started_at=NOW - 5, ended_at=None))
        self.store.upsert_run(run("shell:p:job", source="shell", tier=None))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_dry_run_lists_but_deletes_nothing(self):
        report = self.store.prune_marker_runs(dry_run=True)
        self.assertEqual([row["run_id"] for row in report["markers"]], ["muse:done:ws9-gone"])
        self.assertEqual(report["deleted"], 0)
        self.assertIsNotNone(self.store.get_run("muse:done:ws9-gone"))

    def test_cli_yes_backs_up_and_deletes_only_markers(self):
        import importlib.util

        path = ROOT / "agent_dash.py"
        spec = importlib.util.spec_from_file_location("agent_dash_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        env = {
            "AGENT_DASH_DB": str(self.root / "dash.sqlite"),
            "AGENT_DASH_STATE_DIR": str(self.root / "state"),
        }
        old = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        try:
            self.store.close()
            from io import StringIO
            import contextlib

            buf = StringIO()
            with contextlib.redirect_stdout(buf):
                code = mod.cmd_runs(["prune-markers"])
            self.assertEqual(code, 0)
            self.assertIn("dry run", buf.getvalue())
            self.assertEqual(len(list(self.root.glob("dash.sqlite.bak-markers-*"))), 0)
            buf = StringIO()
            with contextlib.redirect_stdout(buf):
                code = mod.cmd_runs(["prune-markers", "--yes"])
            self.assertEqual(code, 0)
            backups = list(self.root.glob("dash.sqlite.bak-markers-*"))
            self.assertEqual(len(backups), 1)
            self.assertIn("1 deleted", buf.getvalue())
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self.store = memory(self.root)
        self.assertIsNone(self.store.get_run("muse:done:ws9-gone"))
        for run_id in (
            "muse:done:ws9-done",
            "muse:done:ws9-pass",
            "muse:done:ws9-used",
            "muse:live:ws9-live",
            "shell:p:job",
        ):
            self.assertIsNotNone(self.store.get_run(run_id), run_id)


class MarkerJsTests(unittest.TestCase):
    def test_models_js_label_for_real_unknown_is_unchanged(self):
        script = (
            "const fs=require('fs');"
            "global.window=globalThis;"
            "eval(fs.readFileSync(process.argv[1],'utf8'));"
            "const assert=require('assert');"
            "assert.strictEqual(window.ADM.nameOf('muse-unknown'),'model not recorded');"
            "assert.strictEqual(window.ADM.keyOf('', 'muse muse')[1],'muse-unknown');"
            "assert.strictEqual(window.ADM.keyOf('muse-spark-1.3-contributor')[1],'muse-spark-1.3');"
            "console.log('models unknown label proof ok');"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "models.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("models unknown label proof ok", proc.stdout)

    def test_tile_caption_reads_starting_never_not_recorded(self):
        harness = r'''
const fs = require("fs");
const assert = require("assert");
global.window = globalThis;
global.location = {search: ""};
function mkEl(tag) {
  return {
    tag: tag, attrs: {}, kids: [], style: {}, textContent: "",
    setAttribute: function (k, v) { this.attrs[k] = v; },
    getAttribute: function (k) { return this.attrs[k]; },
    appendChild: function (c) { this.kids.push(c); return c; },
    addEventListener: function () {},
  };
}
global.document = {
  createElement: function (t) { return mkEl(t); },
  createElementNS: function (ns, t) { return mkEl(t); },
  createTextNode: function (t) { return {text: t}; },
  getElementById: function () { return mkEl("div"); },
  documentElement: mkEl("html"),
};
eval(fs.readFileSync(process.argv[2], "utf8"));
eval(fs.readFileSync(process.argv[3], "utf8"));
eval(fs.readFileSync(process.argv[4], "utf8"));
eval(fs.readFileSync(process.argv[5], "utf8"));
eval(fs.readFileSync(process.argv[6], "utf8"));
eval(fs.readFileSync(process.argv[7], "utf8"));
global.state = {ws: "all", snapError: null, runs: [], conc: [], usage: []};
global.isAgent = function (r) { return r.source !== "claude-main" && r.source !== "shell" && !r.lane_shadow; };
global.cachedPct = function () { return 0; };
function xlines(node) {
  var out = [];
  (function walk(n) {
    if (n && n.attrs && n.attrs.class === "xline" && n.textContent) out.push(n.textContent);
    (n && n.kids || []).forEach(function (k) { if (k && k.kids) walk(k); });
  })(node);
  return out;
}
var snap = {
  running: [], counts: {live_agents: 12, live_by_tool: {grok: 0, muse: 12, glm: 0, claude: 0, deepseek: 0, other: 0}, live_by_model: {}, finished_in_range: 0, finished_by_verdict: {}},
  totals: {tokens: {input: 0, output: 0, cache_read: 0, cache_write: 0}, cost_usd: 0, unpriced_tokens: 0, unpriced_models: []},
  totals_all_time: {tokens: {input: 0, output: 0, cache_read: 0, cache_write: 0}, cost_usd: 0},
  prev_24h: null, last_activity: null, usage: [],
};
var cap = xlines(tiles(snap)).join(" | ");
assert.ok(/muse 12 \(starting 12\)/.test(cap), "starting caption missing: " + cap);
assert.ok(cap.indexOf("model not recorded") < 0, "marker leaked into caption: " + cap);
snap.counts.live_by_model = {"muse-spark-1.3": 5};
snap.counts.live_by_tool.muse = 7;
snap.counts.live_agents = 7;
var cap2 = xlines(tiles(snap)).join(" | ");
assert.ok(cap2.indexOf("starting 2") >= 0, "partial starting missing: " + cap2);
assert.ok(cap2.indexOf("model not recorded") < 0, "marker leaked into caption: " + cap2);
console.log("tile starting caption proof ok");
'''
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "tile_starting.js"
            harness_path.write_text(harness, encoding="utf-8")
            proc = subprocess.run(
                [
                    "node",
                    str(harness_path),
                    str(STATIC / "format.js"),
                    str(STATIC / "models.js"),
                    str(STATIC / "tokencache.js"),
                    str(STATIC / "core" / "el.js"),
                    str(STATIC / "cards" / "tiles.js"),
                    str(STATIC / "core" / "unattributed.js"),
                ],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("tile starting caption proof ok", proc.stdout)


if __name__ == "__main__":
    unittest.main()
