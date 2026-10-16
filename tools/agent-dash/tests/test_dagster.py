"""Dagster source: mapping, tags, and a failed ssh that keeps the previous rows."""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault(
    "AGENT_DASH_WORKSTREAMS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"),
)

import json
import tempfile
import unittest
from pathlib import Path

from dash.sources import Registry
from dash.sources import dagster
from dash.store import Store

NOW = 1_792_186_000.0
SECRET = "DO_NOT_STORE_scope"
CFG = {
    "dagster": {
        "ssh_host": "dagster-host",
        "graphql_url": "http://127.0.0.1:3000/graphql",
        "enabled": True,
    }
}


def _run(run_id, job, status, **extra):
    row = {
        "runId": run_id,
        "jobName": job,
        "status": status,
        "startTime": NOW - 100,
        "endTime": None,
        "creationTime": NOW - 120,
        "tags": [],
    }
    row.update(extra)
    return row


def _payload(active, recent=None):
    def block(rows):
        return {"__typename": "Runs", "results": rows}

    return json.dumps({"data": {"active": block(active), "recent": block(recent or [])}})


class DagsterTests(unittest.TestCase):
    def setUp(self):
        self._ssh = dagster._ssh
        dagster._ssh = self._blocked
        dagster.last_error = None
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "dash.sqlite")

    def tearDown(self):
        dagster._ssh = self._ssh
        dagster.last_error = None
        self.store.close()
        self.tmp.cleanup()

    def _blocked(self, *args, **kwargs):
        raise AssertionError("ssh should not run")

    def _feed(self, active, recent=None, cfg=None):
        body = {}

        def fake(host, url, payload):
            body["host"] = host
            body["url"] = url
            body["payload"] = json.loads(payload)
            body["calls"] = body.get("calls", 0) + 1
            return _payload(active, recent), None

        dagster._ssh = fake
        dagster.poll(self.store, CFG if cfg is None else cfg, NOW)
        return body

    def test_maps_runs_labels_and_workstreams(self):
        tags = [
            {"key": "lor/ops_wave", "value": "ws1-109-tcgjp-2026-10-16"},
            {"key": "dagster/partition", "value": "2026-10-16"},
            {"key": "lor/slot_class", "value": "LIGHT"},
            {"key": "lor/claim_gib", "value": "9.5gib"},
            {"key": "lor/systemd_scope_unit", "value": SECRET},
            {"key": "dagster/code_location", "value": "secret-location"},
        ]
        active = [
            _run(
                "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "identity_tcgjp_refresh_job",
                "STARTED",
                tags=tags,
            ),
            _run(
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "catalog_load_job",
                "STARTED",
                tags=[{"key": "lor/slot_class", "value": "HEAVY"}],
            ),
            _run(
                "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "identity_match_job",
                "QUEUED",
                startTime=None,
                tags=[{"key": "lor/ops_wave", "value": "ws7-queue-2026-10-16"}],
            ),
        ]
        recent = [
            _run(
                "dddddddd-dddd-dddd-dddd-dddddddddddd",
                "core_sales_identity_overlay_job",
                "SUCCESS",
                endTime=NOW - 30,
                tags=[{"key": "lor/ops_wave", "value": "ws1-47-pc-v4-2026-10-16"}],
            ),
            _run(
                "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                "identity_extract_catalog_id_job",
                "FAILURE",
                endTime=NOW - 40,
            ),
            _run(
                "ffffffff-ffff-ffff-ffff-ffffffffffff",
                "lane_ebay_image_cache_job",
                "CANCELED",
                endTime=NOW - 50,
            ),
            _run(
                "11111111-1111-1111-1111-111111111111",
                "pg_load_job",
                "SUCCESS",
                endTime=NOW - 3 * 3600,
            ),
        ]
        seen = self._feed(active, recent)
        self.assertEqual(seen["calls"], 1)
        self.assertEqual(seen["host"], "dagster-host")
        self.assertEqual(seen["url"], "http://127.0.0.1:3000/graphql")
        self.assertIn("runsOrError(limit: 60", seen["payload"]["query"])
        self.assertIn("QUEUED, STARTING, STARTED", seen["payload"]["query"])
        self.assertAlmostEqual(seen["payload"]["variables"]["since"], NOW - 7200)
        self.assertIsNone(dagster.last_error)

        live = self.store.get_run("dagster:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual(live["source"], "dagster")
        self.assertEqual(live["model"], "identity_tcgjp_refresh_job")
        self.assertEqual(live["label"], "ws1-109-tcgjp-2026-10-16:2026-10-16")
        self.assertEqual(live["workstream"], "WS1")
        self.assertEqual(live["ws_confidence"], "regex")
        self.assertEqual(live["status"], "running")
        self.assertIsNone(live["verdict"])
        self.assertEqual(live["tier"], "LIGHT")
        self.assertEqual(live["started_at"], NOW - 100)
        self.assertIsNone(live["ended_at"])
        self.assertEqual(live["clock_source"], "dagster")
        blob = json.dumps(live)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("secret-location", blob)
        self.assertNotIn("9.5gib", blob)
        self.assertNotIn("systemd", blob)

        plain = self.store.get_run("dagster:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        self.assertEqual(plain["label"], "catalog_load_job")
        self.assertIsNone(plain["workstream"])
        self.assertEqual(plain["tier"], "HEAVY")
        self.assertEqual(plain["status"], "running")

        queued = self.store.get_run("dagster:cccccccc-cccc-cccc-cccc-cccccccccccc")
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["label"], "ws7-queue-2026-10-16")
        self.assertEqual(queued["workstream"], "WS7")
        self.assertEqual(queued["started_at"], NOW - 120)

        done = self.store.get_run("dagster:dddddddd-dddd-dddd-dddd-dddddddddddd")
        self.assertEqual(done["status"], "finished")
        self.assertEqual(done["verdict"], "SUCCESS")
        self.assertEqual(done["label"], "ws1-47-pc-v4-2026-10-16")
        self.assertEqual(done["workstream"], "WS1")
        failed = self.store.get_run("dagster:eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
        self.assertEqual(failed["verdict"], "FAILURE")
        self.assertEqual(failed["status"], "finished")
        canceled = self.store.get_run("dagster:ffffffff-ffff-ffff-ffff-ffffffffffff")
        self.assertEqual(canceled["verdict"], "CANCELED")
        self.assertIsNone(self.store.get_run("dagster:11111111-1111-1111-1111-111111111111"))

        snap = self.store.snapshot(NOW, "WS1")
        running = [row["label"] for row in snap["running"]]
        self.assertIn("ws1-109-tcgjp-2026-10-16:2026-10-16", running)
        self.assertNotIn("catalog_load_job", running)
        self.assertEqual(snap["counts"]["live_agents"], 0)
        self.assertEqual(snap["counts"]["prod_jobs"], 1)
        self.assertEqual(snap["counts"]["live_by_tool"], {"grok": 0, "muse": 0, "glm": 0, "claude": 0})
        self.assertEqual(snap["totals"]["tokens"], {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0})
        self.assertEqual(snap["totals"]["cost_usd"], 0.0)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0], 0)
        shown = snap["running"][0]
        self.assertAlmostEqual(shown["elapsed_s"], 100, delta=0.01)
        recent = {row["verdict"] for row in snap["recent"]}
        self.assertIn("SUCCESS", recent)
        all_snap = self.store.snapshot(NOW, "all")
        self.assertEqual(all_snap["counts"]["prod_jobs"], 3)

    def test_allowlisted_tags_only(self):
        tags = [
            {"key": "lor/ops_wave", "value": "ws1-47-pc-v4-2026-10-16"},
            {"key": "dagster/partition", "value": "part-a"},
            {"key": "lor/slot_class", "value": "INGEST"},
            {"key": "lor/claim_gib", "value": "9.5gib"},
            {"key": "lor/scope_cap_gib", "value": SECRET},
            {"key": "not-a-dict"},
            {"key": "lor/ops_wave", "value": "second-wave-ignored"},
        ]
        # The second wave item is a dict; the string is skipped. First wave wins.
        tags[5] = "nope"
        kept = dagster.select_tags(tags)
        self.assertEqual(
            kept,
            {
                "lor/ops_wave": "ws1-47-pc-v4-2026-10-16",
                "dagster/partition": "part-a",
                "lor/slot_class": "INGEST",
                "lor/claim_gib": "9.5gib",
            },
        )
        self.assertNotIn(SECRET, json.dumps(kept))

    def test_ssh_failure_keeps_previous_rows(self):
        self.store.upsert_run(
            {
                "run_id": "dagster:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "source": "dagster",
                "model": "catalog_load_job",
                "label": "ws1-47-pc-v4-2026-10-16",
                "tier": "HEAVY",
                "status": "running",
                "started_at": NOW - 50,
                "clock_source": "dagster",
            }
        )
        dagster._ssh = lambda *args, **kwargs: (None, "Connection refused")
        dagster.poll(self.store, CFG, NOW)
        row = self.store.get_run("dagster:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["label"], "ws1-47-pc-v4-2026-10-16")
        self.assertEqual(row["tier"], "HEAVY")
        self.assertEqual(dagster.last_error, "Connection refused")
        registry = Registry([dagster])
        registry.tick(self.store, CFG, NOW + 1, force=True)
        health = registry.health(NOW + 1)
        self.assertFalse(health["ok"])
        self.assertEqual(health["sources"][0]["name"], "dagster")
        self.assertEqual(health["sources"][0]["last_error"], "Connection refused")
        row = self.store.get_run("dagster:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual(row["status"], "running")

    def test_graphql_error_and_bad_json_do_not_raise(self):
        self.store.upsert_run(
            {
                "run_id": "dagster:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "source": "dagster",
                "label": "ws1-kept",
                "status": "running",
                "model": "catalog_load_job",
                "started_at": NOW - 10,
            }
        )
        dagster._ssh = lambda *args, **kwargs: (
            '{"data":{"active":{"__typename":"PythonError","message":"nope"},'
            '"recent":{"__typename":"Runs","results":[]}}}',
            None,
        )
        dagster.poll(self.store, CFG, NOW)
        self.assertEqual(self.store.get_run("dagster:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")["status"], "running")
        self.assertIn("nope", dagster.last_error)
        dagster._ssh = lambda *args, **kwargs: ("not-json", None)
        dagster.poll(self.store, CFG, NOW)
        self.assertIn("JSONDecodeError", dagster.last_error)
        self.assertEqual(self.store.get_run("dagster:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")["label"], "ws1-kept")

        def boom(*args, **kwargs):
            raise OSError("no ssh")

        dagster._ssh = boom
        dagster.poll(self.store, CFG, NOW)
        self.assertIn("OSError", dagster.last_error)
        self.assertEqual(self.store.get_run("dagster:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")["status"], "running")

    def test_disabled_skips_ssh(self):
        called = []
        dagster._ssh = lambda *args, **kwargs: called.append(1)
        dagster.poll(self.store, {"dagster": {"enabled": False}}, NOW)
        self.assertEqual(called, [])
        self.assertIsNone(dagster.last_error)

    def test_no_host_skips_ssh(self):
        called = []
        dagster._ssh = lambda *args, **kwargs: called.append(1)
        dagster.poll(self.store, {"dagster": {"enabled": True}}, NOW)
        dagster.poll(self.store, {}, NOW)
        self.assertEqual(called, [])
        self.assertIsNone(dagster.last_error)

    def test_ssh_argv(self):
        argv = dagster.ssh_argv("dagster-host", "http://127.0.0.1:3000/graphql")
        self.assertEqual(
            argv,
            [
                "ssh",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "BatchMode=yes",
                "dagster-host",
                'curl -s -m 20 -X POST -H "Content-Type: application/json" '
                "http://127.0.0.1:3000/graphql --data-binary @-",
            ],
        )

    def test_success_clears_health_error(self):
        dagster.last_error = "Connection refused"
        self._feed([])
        registry = Registry([dagster])
        registry.tick(self.store, CFG, NOW, force=True)
        health = registry.health(NOW)
        self.assertTrue(health["ok"])
        self.assertIsNone(health["sources"][0]["last_error"])


if __name__ == "__main__":
    unittest.main()
