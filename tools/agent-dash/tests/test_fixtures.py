"""Shape checks for the API fixtures, plus a small read of the example sessions."""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fixtures"))

import make_fixtures as fx  # noqa: E402

FIXTURES = ROOT / "fixtures"
SOURCES = {
    "claude-main", "claude-subagent", "claude-workflow",
    "muse", "glm", "grok", "shell",
}
TIERS = {"opus", "sonnet", "haiku", "muse", "glm", "grok"}
STATUSES = {"running", "done", "killed", "died_early", "unknown"}
VERDICTS = {
    "PASS", "PARTIAL", "BLOCKED", "APPROVE",
    "APPROVE_WITH_CHANGES", "REQUEST_CHANGES", None,
}
CLOCKS = {"transcript", "muse_log", "manifest", "mtime"}
CONFIDENCE = {"label", "regex", "session-default", None}
RUN_KEYS = {
    "run_id", "source", "tier", "model", "label", "workstream", "ws_confidence",
    "session_id", "parent_session_id", "status", "verdict", "started_at",
    "ended_at", "elapsed_s", "clock_source", "out_bytes", "out_growing",
    "err_bytes", "pid", "rss_kb", "tokens", "cost_usd",
}
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")
FILES = (
    "snapshot.json",
    "snapshot-WS1.json",
    "snapshot-WS2.json",
    "snapshot-WS7.json",
    "runs.json",
    "usage-model-hour.json",
    "usage-workstream-hour.json",
    "concurrency.json",
    "sessions.json",
    "workstreams.json",
    "workstreams-all.json",
    "project.json",
    "archive.json",
    "reconcile.json",
    "project.json",
    "decisions.json",
    "project/feed.json",
    "plan-WS1.json",
    "plan-WS2.json",
    "plan-WS7.json",
    "plan-WS9.json",
    "plan-WS10.json",
    "plan-WS11.json",
    "replay-WS1.json",
    "replay-WS2.json",
    "replay-WS7.json",
    "health.json",
)


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class FixtureShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = {name: load(name) for name in FILES}
        cls.runs = cls.docs["runs.json"]["runs"]
        cls.snap = cls.docs["snapshot.json"]
        cls.ws7 = cls.docs["snapshot-WS7.json"]

    def test_every_file_has_a_note(self):
        for name, doc in self.docs.items():
            self.assertIsInstance(doc.get("_note"), str, name)
            self.assertTrue(doc["_note"].strip(), name)

    def test_run_shape(self):
        ids = []
        for run in self.runs:
            extra = set(run) - RUN_KEYS - {"children"}
            missing = RUN_KEYS - set(run)
            self.assertFalse(extra, run.get("run_id"))
            self.assertFalse(missing, run.get("run_id"))
            self.assertIn(run["source"], SOURCES)
            self.assertIn(run["tier"], TIERS)
            self.assertIn(run["status"], STATUSES)
            self.assertIn(run["verdict"], VERDICTS)
            self.assertIn(run["clock_source"], CLOCKS)
            self.assertIn(run["ws_confidence"], CONFIDENCE)
            self.assertNotEqual(run["ws_confidence"], "label")
            self.assertIsInstance(run["run_id"], str)
            self.assertTrue(run["run_id"])
            self.assertIsInstance(run["label"], str)
            self.assertTrue(run["workstream"] is None or isinstance(run["workstream"], str))
            self.assertTrue(run["model"] is None or isinstance(run["model"], str))
            self.assertTrue(run["session_id"] is None or isinstance(run["session_id"], str))
            self.assertTrue(
                run["parent_session_id"] is None or isinstance(run["parent_session_id"], str)
            )
            self.assertTrue(num(run["started_at"]))
            self.assertTrue(run["ended_at"] is None or num(run["ended_at"]))
            self.assertTrue(num(run["elapsed_s"]))
            self.assertGreaterEqual(run["elapsed_s"], 0)
            self.assertIsInstance(run["out_growing"], bool)
            self.assertIsInstance(run["out_bytes"], int)
            self.assertIsInstance(run["err_bytes"], int)
            self.assertGreaterEqual(run["out_bytes"], 0)
            self.assertGreaterEqual(run["err_bytes"], 0)
            self.assertTrue(run["pid"] is None or isinstance(run["pid"], int))
            self.assertTrue(run["rss_kb"] is None or isinstance(run["rss_kb"], int))
            self.assertEqual(set(run["tokens"]), set(TOKEN_KEYS))
            for key in TOKEN_KEYS:
                self.assertIsInstance(run["tokens"][key], int)
                self.assertGreaterEqual(run["tokens"][key], 0)
            if run["tier"] in ("opus", "sonnet", "haiku"):
                self.assertTrue(num(run["cost_usd"]))
            else:
                self.assertIsNone(run["cost_usd"])
            if run["source"] == "claude-workflow":
                self.assertIsInstance(run.get("children"), int)
                self.assertGreaterEqual(run["children"], 1)
            else:
                self.assertNotIn("children", run)
            end = run["ended_at"] if run["ended_at"] is not None else self.snap["now"]
            expect = round(max(0.0, end - run["started_at"]), 1)
            self.assertAlmostEqual(run["elapsed_s"], expect, delta=0.11)
            ids.append(run["run_id"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_snapshot_shape(self):
        for doc in (self.snap, self.ws7):
            self.assertTrue(num(doc["now"]))
            self.assertIsInstance(doc["seq"], int)
            self.assertTrue(
                {"now", "seq", "counts", "running", "recent", "totals", "totals_all_time", "last_activity", "by_workstream"}
                <= set(doc) - {"_note"},
                f"snapshot misses required keys: {sorted(set(doc))}",
            )
            if "recent_shown" in doc:
                self.assertEqual(doc["recent_shown"], len(doc["recent"]))
            if "recent_total" in doc:
                self.assertGreaterEqual(doc["recent_total"], len(doc["recent"]))
            counts = doc["counts"]
            self.assertEqual(
                set(counts),
                {"live_agents", "live_by_tool", "finished_today", "finished_in_range", "finished_by_verdict"},
            )
            self.assertEqual(set(counts["live_by_tool"]), {"grok", "muse", "glm", "claude"})
            self.assertEqual(sum(counts["live_by_tool"].values()), counts["live_agents"])
            self.assertIsInstance(counts["finished_by_verdict"], dict)
            self.assertLessEqual(len(doc["recent"]), 50)
            self.assert_totals(doc["totals"])
            for run in doc["running"]:
                self.assertEqual(run["status"], "running")
                self.assertIsNone(run["ended_at"])
                self.assertNotIn("children", run)
            stamps = []
            for run in doc["recent"]:
                self.assertNotIn("children", run)
                self.assertIsNotNone(run["ended_at"])
                self.assertGreaterEqual(run["ended_at"], doc["now"] - 24 * 3600)
                self.assertLessEqual(run["ended_at"], doc["now"] + 5)
                stamps.append(run["ended_at"])
            self.assertEqual(stamps, sorted(stamps, reverse=True))
            for row in doc["by_workstream"]:
                self.assertEqual(
                    set(row),
                    {"ws", "live_agents", "finished_in_range", "finished_today", "tokens", "cost_usd", "tokens_all_time", "cost_usd_all_time", "last_activity"},
                )
                self.assertIsInstance(row["tokens"], int)
                self.assertTrue(num(row["cost_usd"]))
                self.assertIsInstance(row["tokens_all_time"], int)
                self.assertTrue(num(row["cost_usd_all_time"]))
        self.assertEqual(self.snap["now"], self.ws7["now"])
        self.assertIn("counts", self.snap)
        self.assertIn("counts", self.ws7)
        for run in self.ws7["running"] + self.ws7["recent"]:
            self.assertEqual(run["workstream"], "WS7")
        for row in self.ws7["by_workstream"]:
            self.assertEqual(row["ws"], "WS7")

    def test_totals_match_the_run_list(self):
        self.assert_rollups(self.snap, None)
        self.assert_rollups(self.ws7, "WS7")
        four = {key: 0 for key in TOKEN_KEYS}
        for row in self.docs["usage-model-hour.json"]["buckets"]:
            for key in TOKEN_KEYS:
                four[key] += row[key]
        tokens = self.snap["totals"]["tokens"]
        for key in TOKEN_KEYS:
            self.assertEqual(four[key], tokens[key])
        ws_four = {key: 0 for key in TOKEN_KEYS}
        for row in self.docs["usage-workstream-hour.json"]["buckets"]:
            for key in TOKEN_KEYS:
                ws_four[key] += row[key]
        self.assertEqual(ws_four, four)
        bucket_cost = sum(
            row["cost_usd"] for row in self.docs["usage-model-hour.json"]["buckets"]
        )
        self.assertAlmostEqual(bucket_cost, self.snap["totals"]["cost_usd"], delta=0.05)

    def assert_rollups(self, doc, ws):
        chosen = [
            run for run in self.runs
            if ws is None or run["workstream"] == ws
        ]
        tokens = {key: 0 for key in TOKEN_KEYS}
        cost = 0.0
        unpriced = 0
        for run in chosen:
            for key in TOKEN_KEYS:
                tokens[key] += run["tokens"][key]
            if run["cost_usd"] is None:
                unpriced += 1
            else:
                cost += run["cost_usd"]
        for key in TOKEN_KEYS:
            self.assertEqual(doc["totals"]["tokens"][key], tokens[key])
        self.assertEqual(doc["totals"]["unpriced_runs"], unpriced)
        self.assertAlmostEqual(doc["totals"]["cost_usd"], round(cost, 4), delta=0.001)
        rolled = sum(row["tokens"] for row in doc["by_workstream"])
        self.assertEqual(rolled, sum(tokens.values()))
        self.assertEqual(
            sum(row["live_agents"] for row in doc["by_workstream"]), doc["counts"]["live_agents"]
        )
        self.assertEqual(
            sum(row["finished_in_range"] for row in doc["by_workstream"]),
            doc["counts"]["finished_in_range"],
        )

    def assert_totals(self, totals):
        self.assertEqual(
            set(totals["tokens"]),
            {"input", "output", "cache_read", "cache_write", "reasoning"},
        )
        for key, value in totals["tokens"].items():
            self.assertIsInstance(value, int)
            self.assertGreaterEqual(value, 0)
        self.assertTrue(num(totals["cost_usd"]))
        self.assertIsInstance(totals["unpriced_runs"], int)

    def test_usage_buckets(self):
        for name in ("usage-model-hour.json", "usage-workstream-hour.json"):
            rows = self.docs[name]["buckets"]
            self.assertTrue(rows)
            stamps = []
            for row in rows:
                self.assertEqual(
                    set(row),
                    {"t", "key", "input", "output", "cache_read", "cache_write", "reasoning", "cost_usd"},
                )
                self.assertEqual(row["t"] % 3600, 0)
                self.assertIsInstance(row["key"], str)
                self.assertTrue(num(row["cost_usd"]))
                for key in TOKEN_KEYS:
                    self.assertIsInstance(row[key], int)
                stamps.append((row["t"], row["key"]))
            self.assertEqual(stamps, sorted(stamps))

    def test_concurrency(self):
        points = self.docs["concurrency.json"]["points"]
        self.assertGreater(len(points), 60)
        keys = {"t", "grok", "muse", "glm", "claude"}
        prev = None
        peak = 0
        for point in points:
            self.assertEqual(set(point), keys)
            self.assertEqual(point["t"] % 60, 0)
            if prev is not None:
                self.assertEqual(point["t"] - prev, 60)
            total = 0
            for tool in ("grok", "muse", "glm", "claude"):
                self.assertIsInstance(point[tool], int)
                self.assertGreaterEqual(point[tool], 0)
                total += point[tool]
            peak = max(peak, total)
            prev = point["t"]
        self.assertGreaterEqual(peak, 20)
        self.assertLessEqual(peak, 40)

    def test_sessions_and_workstreams(self):
        rows = self.docs["sessions.json"]["sessions"]
        self.assertGreaterEqual(len(rows), 4)
        seen = set()
        for row in rows:
            self.assertEqual(
                set(row),
                {
                    "session_id", "tool", "cwd", "label", "workstream",
                    "started_at", "status", "runs", "tokens", "cost_usd",
                },
            )
            self.assertIsInstance(row["session_id"], str)
            self.assertIsInstance(row["tool"], str)
            self.assertIsInstance(row["status"], str)
            self.assertIsInstance(row["runs"], int)
            self.assertIsInstance(row["tokens"], int)
            self.assertTrue(num(row["cost_usd"]))
            self.assertTrue(num(row["started_at"]))
            seen.add(row["session_id"])
        self.assertIn(fx.WS1, seen)
        self.assertIn(fx.PLAN, seen)
        board = self.docs["workstreams.json"]["workstreams"]
        codes = [row["ws"] for row in board]
        for code in ("WS0", "WS1", "WS2", "WS3", "WS4", "WS5", "WS6", "WS7"):
            self.assertIn(code, codes)
        done = backlog = 0
        for row in board:
            self.assertEqual(set(row["tk"]), set(fx.TK_KEYS))
            for key in fx.TK_KEYS:
                self.assertIsInstance(row["tk"][key], int)
            self.assertIsInstance(row["plan"], bool)
            self.assertIsInstance(row["live_agents"], int)
            self.assertIsInstance(row["finished_in_range"], int)
            self.assertIsInstance(row["tokens"], int)
            self.assertTrue(num(row["cost_usd"]))
            self.assertIsInstance(row["name"], str)
            self.assertTrue(row["last_activity"] is None or num(row["last_activity"]))
            if row["ws"].startswith("WS"):
                done += row["tk"]["done"]
                backlog += row["tk"]["backlog"]
            self.assertEqual(row["plan"], row["ws"] in fx.PLAN_WS)
        # workstreams.json is the default scope: archived/dropped BOARD
        # rows never land here, so the BOARD sums skip hidden states.
        visible = [
            counts
            for code, _name, counts in fx.BOARD
            if fx.WS_STATES.get(code, "active") not in fx.HIDDEN_STATES
        ]
        self.assertEqual(done, sum(counts[4] for counts in visible))
        self.assertEqual(backlog, sum(counts[0] for counts in visible))
        for row in board:
            if row["ws"] in fx.PLAN_WS:
                self.assertEqual(set(row["progress"]), {"done", "total", "counts", "pct"})
                self.assertEqual(row["progress"], self.docs[f"plan-{row['ws']}.json"]["progress"])
            else:
                self.assertIsNone(row["progress"])
        completed = [
            row
            for row in board
            if row["progress"] and row["progress"]["total"] > 0 and row["progress"]["done"] == row["progress"]["total"]
        ]
        self.assertTrue(completed, "the fixture preview needs a completed workstream for the tab filter")

    def test_plans_and_health(self):
        for name, ws in (("plan-WS1.json", "WS1"), ("plan-WS2.json", "WS2"), ("plan-WS7.json", "WS7")):
            plan = self.docs[name]
            self.assertEqual(plan["ws"], ws)
            self.assert_plan(plan)
            self.assertNotIn("Synthesized", plan["_note"])
            progress = plan["progress"]
            self.assertEqual(set(progress), {"done", "total", "counts", "pct"})
            self.assertEqual(set(progress["counts"]), {"todo", "wip", "pr", "prog", "done", "deferred"})
            self.assertEqual(progress["total"], sum(progress["counts"][k] for k in ("todo", "wip", "pr", "prog", "done")))
            tasks = [task for group in plan["groups"] for task in group["tasks"]]
            self.assertEqual(progress["done"], sum(1 for task in tasks if task["status"] == "done"))
            self.assertEqual(progress["total"], sum(1 for task in tasks if task["status"] != "deferred"))
        ws1 = self.docs["plan-WS1.json"]["progress"]
        self.assertEqual((ws1["done"], ws1["total"], ws1["pct"]), (2, 5, 40))
        tasks = [task for group in self.docs["plan-WS1.json"]["groups"] for task in group["tasks"]]
        gates = [task for task in tasks if task.get("kind") == "gate"]
        self.assertEqual(len(gates), 1)
        noted = [task for task in tasks if task.get("dep_notes")]
        self.assertEqual(len(noted), 1)
        self.assertTrue(set(noted[0]["dep_notes"]) <= set(noted[0].get("deps", [])))

    def test_plans_carry_map_layout(self):
        sys.path.insert(0, str(ROOT))
        from dash import maplayout as maplayout_mod

        for name in ("plan-WS1.json", "plan-WS2.json", "plan-WS7.json"):
            plan = self.docs[name]
            for key in ("layout", "map_warnings"):
                self.assertIn(key, plan, name)
            view = {key: plan[key] for key in ("ws", "cols", "groups", "edges", "phases", "trains")}
            self.assertEqual(plan["layout"], maplayout_mod.layout(view), name)
            self.assertEqual(plan["map_warnings"], maplayout_mod.map_warnings(view), name)
            layout = plan["layout"]
            self.assertEqual(
                set(layout),
                {"ws", "density", "ultra", "live_tasks", "deferred_tasks", "total_tasks",
                 "task_edges", "group_edges", "columns", "folded_rail", "groups", "labels",
                 "arrows", "trains", "phases_line", "balance"},
                name,
            )
            self.assertIn(layout["density"], ("comfortable", "compact"), name)
            self.assertIn(layout["arrows"]["mode"], ("all", "on-focus"), name)
            self.assertTrue(layout["phases_line"], name)

    def test_decisions_fixture(self):
        doc = self.docs["decisions.json"]
        self.assertTrue(doc["source"]["present"])
        rows = doc["decisions"]
        self.assertTrue(rows)
        for row in rows:
            for key in ("id", "title", "status", "owner", "date", "workstream", "area"):
                self.assertIn(key, row, row.get("id"))
        counts = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        self.assertEqual(doc["counts"], counts)
        dated = [row["date"] for row in rows if row["date"]]
        self.assertEqual(dated, sorted(dated, reverse=True))

    def test_feed_fixture(self):
        doc = self.docs["project/feed.json"]
        items = doc["feed"]
        self.assertTrue(items)
        self.assertEqual(doc["total"], len(items))
        self.assertLessEqual(len(items), doc["limit"])
        stamps = [item["ts"] for item in items]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertEqual(doc["earliest_ts"], min(stamps))
        for item in items:
            for key in ("ts", "day", "ws", "name", "kind", "text", "target"):
                self.assertIn(key, item, key)
            self.assertNotIn(item["kind"], ("doctor", "health"))

    def test_replay_fixtures(self):
        for ws in ("WS1", "WS2", "WS7"):
            replay = self.docs[f"replay-{ws}.json"]
            plan = self.docs[f"plan-{ws}.json"]
            self.assertEqual(replay["ws"], ws)
            known = {task["id"] for group in plan["groups"] for task in group["tasks"]}
            self.assertEqual(set(replay["tasks"]), known)
            stamps = [event["t"] for event in replay["events"]]
            self.assertEqual(stamps, sorted(stamps))
            self.assertTrue(replay["events"])
            self.assertEqual(replay["span"], {"start": min(stamps), "end": max(stamps)})
            inferred = [tid for tid, task in replay["tasks"].items() if task["inferred"]]
            self.assertEqual(replay["inferred"], len(inferred))
            self.assertEqual(
                replay["observed"],
                sum(1 for task in replay["tasks"].values() if task["done_at"] is not None and not task["inferred"]),
            )
            for tid, task in replay["tasks"].items():
                self.assertEqual(set(task), {"status", "first_active", "done_at", "inferred", "runs"})
                for run in task["runs"]:
                    self.assertEqual(
                        set(run),
                        {"start", "end", "family", "label", "verdict", "source", "status", "tokens", "clock"},
                    )
                    self.assertIn(run["family"], {"opus", "sonnet", "haiku", "muse", "glm", "grok", "other"})

    def test_reconcile_fixture(self):
        doc = self.docs["reconcile.json"]
        self.assertTrue(
            {"now", "since", "until", "all", "all_time", "rows", "unassigned", "mismatches"}
            <= set(doc) - {"_note"},
            f"reconcile misses required keys: {sorted(set(doc))}",
        )
        self.assertEqual(doc["mismatches"], [])
        self.assertEqual(set(doc["all"]), {"live_agents", "finished_in_range", "finished_today", "tokens", "cost_usd"})
        self.assertEqual(set(doc["all_time"]), {"tokens", "cost_usd"})
        everything = list(doc["rows"]) + [doc["unassigned"]]
        for metric in ("live_agents", "finished_in_range", "finished_today", "tokens"):
            self.assertEqual(sum(row[metric] for row in everything), doc["all"][metric])
        self.assertAlmostEqual(sum(row["cost_usd"] for row in everything), doc["all"]["cost_usd"], delta=0.05)
        self.assertEqual(
            sum(row["tokens_all_time"] for row in everything), doc["all_time"]["tokens"]
        )
        self.assertAlmostEqual(
            sum(row["cost_usd_all_time"] for row in everything), doc["all_time"]["cost_usd"], delta=0.05
        )
        self.assertEqual(sum(row["live_agents"] for row in everything), self.snap["counts"]["live_agents"])
        self.assertEqual(doc["all"]["tokens"], sum(self.snap["totals"]["tokens"][k] for k in TOKEN_KEYS))
        health = self.docs["health.json"]
        self.assertIs(health["ok"], True)
        names = []
        for source in health["sources"]:
            self.assertEqual(
                set(source),
                {"name", "last_poll", "last_error", "lag_s", "last_duration_s", "interval_s", "stalled"},
            )
            self.assertIsInstance(source["name"], str)
            self.assertTrue(num(source["last_poll"]))
            self.assertTrue(num(source["lag_s"]))
            self.assertTrue(source["last_error"] is None or isinstance(source["last_error"], str))
            self.assertTrue(num(source["last_duration_s"]))
            self.assertTrue(num(source["interval_s"]))
            self.assertIs(source["stalled"], False)
            names.append(source["name"])
        for required in ("claude", "manifests", "muse", "grok_cli", "cursor", "process", "hub"):
            self.assertIn(required, names)

    def assert_plan(self, plan):
        self.assertIsInstance(plan["cols"], list)
        self.assertGreaterEqual(len(plan["cols"]), 1)
        ids = set()
        for group in plan["groups"]:
            self.assertEqual(set(group), {"id", "col", "title", "tasks"})
            self.assertIsInstance(group["col"], int)
            ids.add(group["id"])
            for task in group["tasks"]:
                self.assertEqual(set(task) - {"deps", "kind", "dep_notes"}, {"id", "title", "status"})
                self.assertIsInstance(task["status"], str)
                self.assertTrue(task["status"])
        for edge in plan["edges"]:
            self.assertEqual(len(edge), 2)
            self.assertIn(edge[0], ids)
            self.assertIn(edge[1], ids)
        self.assertTrue(plan["phases"])
        for phase in plan["phases"]:
            self.assertEqual(set(phase), {"name", "goal", "gate"})

    def test_price_and_attribute(self):
        cost = fx.cost_usd("claude-opus-4", "opus", 1_000_000, 0, 0, 0)
        self.assertEqual(cost, 15.0)
        haiku = fx.cost_usd("claude-haiku-4", "haiku", 200_000, 0, 0, 0)
        self.assertEqual(haiku, 0.2)
        self.assertIsNone(fx.cost_usd("glm-4", "glm", 1000, 10, 0, 0))
        cases = (
            ("ws7-landing", None, "WS7", "regex"),
            ("ws1-12", None, "WS1", "regex"),
            ("impl WS1.4 now", None, "WS1", "regex"),
            ("g-review", None, "WS1", "regex"),
            ("dash-preview", fx.PLAN, "TOOLS", "regex"),
            ("plan-notes", fx.PLAN, "PLANNING", "session-default"),
            ("plain note", fx.WS1, "WS1", "session-default"),
            ("no-match", None, None, None),
        )
        for label, session, ws, confidence in cases:
            self.assertEqual(fx.attribute(label, session), (ws, confidence), label)

    def test_synthetic_plan_and_known_labels(self):
        plan = fx.synthetic_plan("WS9")
        self.assertEqual(len(plan["cols"]), 5)
        self.assertEqual(len(plan["groups"]), 6)
        count = sum(len(group["tasks"]) for group in plan["groups"])
        self.assertEqual(count, 25)
        statuses = {task["status"] for group in plan["groups"] for task in group["tasks"]}
        self.assertTrue({"done", "prog", "pr", "todo"} <= statuses)
        self.assert_plan(plan)
        self.assertIn("Synthesized", plan["_note"])
        by_label = {}
        for run in self.runs:
            by_label.setdefault(run["label"], []).append(run)
        expect = {
            "ws7-landing": ("WS7", "regex", "muse"),
            "ws1-12": ("WS1", "regex", "muse"),
            "g-review": ("WS1", "regex", "grok"),
            "dash-preview": ("TOOLS", "regex", "grok"),
            "plan-notes": ("PLANNING", "session-default", "muse"),
        }
        for label, (ws, confidence, source) in expect.items():
            hits = [run for run in by_label[label] if run["source"] == source]
            self.assertTrue(hits, label)
            self.assertEqual(hits[0]["workstream"], ws)
            self.assertEqual(hits[0]["ws_confidence"], confidence)


class ProjectViewFixtureTests(unittest.TestCase):
    """Shapes behind the project views: project.json, all-state
    workstreams, archive.json, and the planned fixture plans."""
    @classmethod
    def setUpClass(cls):
        cls.project = load("project.json")
        cls.wss = load("workstreams.json")["workstreams"]
        cls.wss_all = load("workstreams-all.json")["workstreams"]
        cls.archive = load("archive.json")
        cls.plans = {ws: load(f"plan-{ws}.json") for ws in ("WS9", "WS10", "WS11")}
    def test_project_shape(self):
        for key in ("name", "short", "description", "goals", "started", "target", "phases", "counts", "totals", "data_root_hash"):
            self.assertIn(key, self.project, key)
        self.assertTrue(self.project["name"], "offline preview needs a project name to land on")
        self.assertIsInstance(self.project["goals"], list)
        self.assertTrue(self.project["goals"])
        states = {phase["state"] for phase in self.project["phases"]}
        self.assertTrue(states <= {"planned", "active", "done"}, states)
        for phase in self.project["phases"]:
            for key in ("id", "title", "summary", "gate", "state"):
                self.assertIn(key, phase, key)
        counts = self.project["counts"]
        self.assertEqual(
            sum(counts.values()),
            len(self.wss_all),
            "counts by state must cover the all-states list",
        )
    def test_workstream_lifecycle_coverage(self):
        by_id = {row["ws"]: row for row in self.wss_all}
        seen = {row["state"] for row in self.wss_all}
        self.assertEqual(
            seen,
            {"planned", "active", "paused", "done", "archived", "dropped"},
            "the offline preview needs every lifecycle state",
        )
        default_ids = {row["ws"] for row in self.wss}
        for row in self.wss_all:
            for key in ("state", "state_changed_at", "archived_at", "depends_on", "milestone"):
                self.assertIn(key, row, row["ws"])
            self.assertIsInstance(row["depends_on"], list)
            if row["state"] in ("archived", "dropped"):
                self.assertNotIn(row["ws"], default_ids, "default scope keeps archived/dropped rows")
            else:
                self.assertIn(row["ws"], default_ids, "default scope drops a visible row")
            if row["state"] == "archived":
                self.assertTrue(num(row["archived_at"]), row["ws"])
        planned = [row for row in self.wss if by_id[row["ws"]]["state"] == "planned"]
        self.assertTrue(planned, "no planned rows in the default scope")
        for row in planned:
            self.assertTrue(row["depends_on"], f"{row['ws']} names nothing it waits on")
    def test_archive_shape(self):
        cards = self.archive["archive"]
        self.assertTrue(cards, "the offline Archive shelf needs a card")
        stamps = []
        for card in cards:
            for key in ("ws", "state", "frozen_at", "started_at", "finished_at", "duration_s",
                        "tasks", "trains", "approvals", "usage", "closing_report",
                        "id", "title", "archived_at", "report"):
                self.assertIn(key, card, card.get("id"))
            self.assertEqual(card["state"], "archived")
            self.assertTrue(num(card["archived_at"]))
            self.assertNotIn("/", card["report"] or "", "report is a basename, never a path")
            self.assertNotIn("\\", card["report"] or "", "report is a basename, never a path")
            stamps.append(card["archived_at"])
        self.assertEqual(stamps, sorted(stamps, reverse=True), "archive is newest first")
        totals = self.archive["totals"]
        self.assertEqual(totals["workstreams"], len(cards))
        for key in ("tokens", "cost_usd", "runs"):
            self.assertTrue(num(totals[key]), key)
    def test_planned_fixture_plans_have_gates(self):
        ws9 = self.plans["WS9"]
        gates = [task for group in ws9["groups"] for task in group["tasks"] if task.get("kind") == "gate"]
        self.assertTrue(gates, "the readiness table needs a gate to mark stale")
        for gate in gates:
            for key in ("signal", "confirmed_at", "fresh_hours"):
                self.assertIn(key, gate, gate.get("id"))
        self.assertEqual(ws9["progress"]["done"], 0)
        ws11 = self.plans["WS11"]
        self.assertEqual(ws11["progress"]["done"], ws11["progress"]["total"])
        self.assertGreater(ws11["progress"]["total"], 0)


class FixtureSmokeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("AGENT_DASH_SMOKE") == "1", "set AGENT_DASH_SMOKE=1 to compare against a local scratch root")
    def test_local_scratch_is_readable(self):
        root = os.environ.get("AGENT_DASH_SMOKE_SCRATCH")
        if not root or not os.path.isdir(root):
            self.skipTest("AGENT_DASH_SMOKE_SCRATCH is not set")
        self.assertTrue(os.path.isdir(root))


if __name__ == "__main__":
    unittest.main()
