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
        self.assertIn("runsOrError(limit: 200", seen["payload"]["query"])
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
        self.assertEqual(running, [])
        self.assertEqual(snap["counts"]["live_agents"], 0)
        self.assertEqual(snap["counts"]["prod_jobs"], 1)
        self.assertEqual(snap["counts"]["prod_jobs_finished"], 1)
        self.assertEqual(snap["counts"]["live_by_tool"], {"grok": 0, "muse": 0, "glm": 0, "claude": 0})
        self.assertEqual(snap["totals"]["tokens"], {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0})
        self.assertEqual(snap["totals"]["cost_usd"], 0.0)
        self.assertEqual(self.store._conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0], 0)
        recent = [row["label"] for row in snap["recent"]]
        self.assertEqual(recent, [])
        all_snap = self.store.snapshot(NOW, "all")
        self.assertEqual(all_snap["counts"]["prod_jobs"], 3)
        self.assertEqual(all_snap["counts"]["prod_jobs_finished"], 3)

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

    def _feed_with_by_id(self, active, recent, by_id_map, now=NOW):
        calls = []

        def fake(host, url, payload):
            body = json.loads(payload)
            calls.append(body["query"])
            if "DagsterRunsById" in body["query"]:
                ids = body["variables"]["ids"]
                results = [by_id_map[i] for i in ids if i in by_id_map]
                return json.dumps({"data": {"byId": {"__typename": "Runs", "results": results}}}), None
            return _payload(active, recent), None

        dagster._ssh = fake
        dagster.poll(self.store, CFG, now)
        return calls

    def test_close_out_by_id(self):
        live_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        gone_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        self.store.upsert_run(
            {
                "run_id": "dagster:" + live_id,
                "source": "dagster",
                "model": "test_job_alpha",
                "label": "test_job_alpha",
                "status": "running",
                "started_at": NOW - 5000,
                "clock_source": "dagster",
            }
        )
        self.store.upsert_run(
            {
                "run_id": "dagster:" + gone_id,
                "source": "dagster",
                "model": "test_job_beta",
                "label": "test_job_beta",
                "status": "running",
                "started_at": NOW - 4000,
                "clock_source": "dagster",
            }
        )
        active = [_run(live_id, "test_job_alpha", "STARTED")]
        done_raw = _run(gone_id, "test_job_beta", "SUCCESS", endTime=NOW - 60)
        calls = self._feed_with_by_id(active, [], {gone_id: done_raw})
        self.assertEqual(len(calls), 2)
        self.assertIn("runIds", calls[1])
        live = self.store.get_run("dagster:" + live_id)
        self.assertEqual(live["status"], "running")
        closed = self.store.get_run("dagster:" + gone_id)
        self.assertEqual(closed["status"], "finished")
        self.assertEqual(closed["verdict"], "SUCCESS")
        self.assertEqual(closed["ended_at"], NOW - 60)
        self.assertIsNone(dagster.last_error)
        snap = self.store.snapshot(NOW, "all")
        self.assertNotIn(
            "dagster:" + gone_id, [row["run_id"] for row in snap["running"]]
        )

    def test_close_out_old_terminal(self):
        gone_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        self.store.upsert_run(
            {
                "run_id": "dagster:" + gone_id,
                "source": "dagster",
                "model": "test_job_old",
                "label": "test_job_old",
                "status": "running",
                "started_at": NOW - 13 * 3600,
                "clock_source": "dagster",
            }
        )
        old_raw = _run(gone_id, "test_job_old", "FAILURE", endTime=NOW - 13 * 3600)
        self._feed_with_by_id([], [], {gone_id: old_raw})
        closed = self.store.get_run("dagster:" + gone_id)
        self.assertEqual(closed["status"], "finished")
        self.assertEqual(closed["verdict"], "FAILURE")
        self.assertEqual(closed["ended_at"], NOW - 13 * 3600)

    def test_full_page_guard_closes_nothing(self):
        gone_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        self.store.upsert_run(
            {
                "run_id": "dagster:" + gone_id,
                "source": "dagster",
                "model": "test_job_gamma",
                "label": "test_job_gamma",
                "status": "running",
                "started_at": NOW - 3000,
                "clock_source": "dagster",
            }
        )
        active = [
            _run("%08x-0000-4000-8000-%012x" % (i, i), "test_job_bulk", "STARTED")
            for i in range(200)
        ]
        calls = self._feed_with_by_id(active, [], {})
        self.assertEqual(len(calls), 1)
        kept = self.store.get_run("dagster:" + gone_id)
        self.assertEqual(kept["status"], "running")

    def test_failed_by_id_closes_nothing(self):
        gone_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        self.store.upsert_run(
            {
                "run_id": "dagster:" + gone_id,
                "source": "dagster",
                "model": "test_job_delta",
                "label": "test_job_delta",
                "status": "running",
                "started_at": NOW - 2000,
                "clock_source": "dagster",
            }
        )

        def fake(host, url, payload):
            body = json.loads(payload)
            if "DagsterRunsById" in body["query"]:
                return None, "Connection refused"
            return _payload([], []), None

        dagster._ssh = fake
        dagster.poll(self.store, CFG, NOW)
        kept = self.store.get_run("dagster:" + gone_id)
        self.assertEqual(kept["status"], "running")

    def test_unknown_run_closes_as_finished_unknown(self):
        gone_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
        seen_at = NOW - 3600
        self.store.upsert_run(
            {
                "run_id": "dagster:" + gone_id,
                "source": "dagster",
                "model": "test_job_epsilon",
                "label": "test_job_epsilon",
                "status": "running",
                "started_at": seen_at - 100,
                "clock_source": "dagster",
            }
        )
        self.store.note_dagster_seen(["dagster:" + gone_id], seen_at)
        self.store.set_dagster_active([], NOW - 7200, False)
        calls = self._feed_with_by_id([], [], {})
        self.assertEqual(len(calls), 2)
        closed = self.store.get_run("dagster:" + gone_id)
        self.assertEqual(closed["status"], "finished")
        self.assertEqual(closed["verdict"], "UNKNOWN")
        self.assertEqual(closed["ended_at"], seen_at)


if __name__ == "__main__":
    unittest.main()
