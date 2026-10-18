"""Runs without a recorded model hide behind a default-off toggle; totals never change.

Server: ``counts["unattributed"]`` (``{"runs", "tokens", "cost_usd"}``) rides
every ``/api/snapshot`` payload, computed in the one
``_unattributed_locked`` helper and cached with the snapshot. Client:
``static/core/unattributed.js`` owns the ``-unknown`` rule (the same one
``static/models.js`` labels "model not recorded"), the pure filter, the
muted note text, and the default-off sessionStorage flag; every model
legend/list builder routes through it. Synthetic fixtures plus node
proofs for the pure functions and the static rules.
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

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dash.store import Store  # noqa: E402

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


def functions(src):
    """Split a script into bodies by two-space 'function name(' headers."""
    heads = [m for m in re.finditer(r"\n  function (\w+)\s*\(", src)]
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(src)
        yield m.group(1), src[m.start():end]


class UnattributedCountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def seed(self):
        self.store.upsert_run(
            run("muse:live:known", status="running", started_at=NOW - 60, ended_at=None, model=SPARK)
        )
        self.store.upsert_run(
            run("muse:live:honest", status="running", started_at=NOW - 60, ended_at=None, verdict="PASS")
        )
        self.store.upsert_run(run("muse:done:known", model=SPARK))
        self.store.upsert_run(run("muse:done:honest"))
        self.store.upsert_run(run("muse:done:marker"))
        self.store.add_usage(usage("muse:live:known", input=100, output=50, mid="u-known-live"))
        self.store.add_usage(usage("muse:live:honest", model=None, input=70, output=0, mid="u-honest-live"))
        self.store.add_usage(usage("muse:done:known", input=40, output=10, mid="u-known-done"))
        self.store.add_usage(usage("muse:done:honest", model=None, input=7, output=0, mid="u-honest-done"))

    def test_counts_carry_the_additive_unattributed_field(self):
        self.seed()
        snap = self.store.snapshot(NOW, "all")
        unatt = snap["counts"]["unattributed"]
        self.assertEqual(unatt["runs"], 2)
        self.assertEqual(unatt["tokens"], 77)
        self.assertEqual(unatt["cost_usd"], 0.0)

    def test_totals_still_include_the_hidden_runs(self):
        self.seed()
        snap = self.store.snapshot(NOW, "all")
        billed = snap["totals"]["tokens"]
        total = billed["input"] + billed["output"] + billed["cache_read"] + billed["cache_write"]
        self.assertEqual(total, 100 + 50 + 70 + 40 + 10 + 7)
        self.assertEqual(snap["counts"]["finished_in_range"], 2)
        self.assertEqual(snap["counts"]["live_agents"], 2)

    def test_cached_snapshot_keeps_the_field(self):
        self.seed()
        first = self.store.snapshot_cached(NOW, "all")
        second = self.store.snapshot_cached(NOW, "all")
        self.assertEqual(first["counts"]["unattributed"], {"runs": 2, "tokens": 77, "cost_usd": 0.0})
        self.assertEqual(second["counts"]["unattributed"], first["counts"]["unattributed"])

    def test_no_unknowns_reads_zero(self):
        self.store.upsert_run(run("muse:done:known", model=SPARK))
        self.store.add_usage(usage("muse:done:known", mid="u-only"))
        snap = self.store.snapshot(NOW, "all")
        self.assertEqual(snap["counts"]["unattributed"], {"runs": 0, "tokens": 0, "cost_usd": 0.0})


class UnattributedNodeTests(unittest.TestCase):
    """Pure node proofs: the -unknown rule, the filter, billed sums, the
    note text with compact numbers, and the default-off flag."""

    HARNESS = r'''
const fs = require("fs");
const assert = require("assert");
const files = process.argv.slice(1);
function load(n) { eval(fs.readFileSync(files.find(function (p) { return p.endsWith("/" + n); }), "utf8")); }
var window = {};
load("format.js");
load("models.js");
var ADM = window.ADM;
var fmtN = window.AD.fmtN;
load("unattributed.js");
var U = window.AdUnatt;
// The rule matches models.js exactly: every -unknown key reads
// "model not recorded", and only those do.
["muse-unknown", "claude-unknown", "cursor-unknown", "glm-unknown"].forEach(function (k) {
  assert.strictEqual(U.isUnattributed(k), true, k);
  assert.strictEqual(ADM.nameOf(k), "model not recorded", k);
});
["muse-spark-1.3", "sonnet", "other", "unattributed", "WS9", ""].forEach(function (k) {
  assert.strictEqual(U.isUnattributed(k), false, JSON.stringify(k));
  assert.notStrictEqual(ADM.nameOf(k), "model not recorded", JSON.stringify(k));
});
// The pure filter drops unknowns and keeps the folded segment key.
assert.deepStrictEqual(
  U.filterKnown(["sonnet", "muse-unknown", "opus", "claude-unknown", "unattributed"]),
  ["sonnet", "opus", "unattributed"],
);
// Billed sums mirror the tile headline fields.
assert.strictEqual(U.billed({input: 1, output: 2, cache_read: 3, cache_write: 4}), 10);
assert.strictEqual(U.billed(null), 0);
// The note text carries compact numbers, singular and plural.
assert.strictEqual(U.noteText(12, 1200000, fmtN), "Hidden: 12 runs without a recorded model, 1.2M tokens");
assert.strictEqual(U.noteText(1, 5, fmtN), "Hidden: 1 run without a recorded model, 5 tokens");
assert.strictEqual(U.noteText(3, null, fmtN), "Hidden: 3 runs without a recorded model");
// Default off: no sessionStorage in node, and setShown never throws.
assert.strictEqual(U.shown("all"), false);
assert.strictEqual(U.setShown("all", true), true);
assert.strictEqual(U.shown("all"), false);
assert.strictEqual(U.scopeKey("WS9"), "agent-dash-unattributed:WS9");
console.log("unattributed node proof ok");
'''

    def test_pure_functions(self):
        proc = subprocess.run(
            ["node", "-e", self.HARNESS]
            + [str(STATIC / "format.js"), str(STATIC / "models.js"), str(STATIC / "core" / "unattributed.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertIn("unattributed node proof ok", proc.stdout)


class UnattributedStaticRulesTests(unittest.TestCase):
    """Every model legend/list builder routes through the shared helper;
    the flag persists in sessionStorage behind try/catch, default off."""

    BUILDERS = {
        "cards/activity.js": ["actTokenStrip", "actTimelineStrip", "actConcStrip", "actUnattControl"],
        "cards/tiles.js": ["tiles"],
        "core/el.js": ["modelLegend"],
    }

    def test_builders_go_through_the_helper(self):
        missing = []
        for name, builders in self.BUILDERS.items():
            bodies = dict(functions((STATIC / name).read_text()))
            for builder in builders:
                self.assertIn(builder, bodies, f"{name} lost {builder}")
                if "AdUnatt" not in bodies[builder]:
                    missing.append(f"{name}:{builder} lists models without the helper")
        self.assertEqual(missing, [], "\n".join(missing))

    def test_flag_storage_is_guarded_and_defaults_off(self):
        src = (STATIC / "core" / "unattributed.js").read_text()
        self.assertIn('getItem(scopeKey(ws)) === "1"', src)
        for name, body in functions(src):
            if "sessionStorage" in body:
                self.assertIn("try", body, f"{name} touches sessionStorage unguarded")
                self.assertIn("catch", body, f"{name} touches sessionStorage unguarded")

    def test_toggle_is_a_real_button(self):
        src = (STATIC / "core" / "unattributed.js").read_text()
        bodies = dict(functions(src))
        self.assertIn("toggleButton", bodies)
        body = bodies["toggleButton"]
        self.assertIn("aria-pressed", body)
        self.assertIn("Show unattributed", body)
        self.assertIn("Hide unattributed", body)


if __name__ == "__main__":
    unittest.main()
