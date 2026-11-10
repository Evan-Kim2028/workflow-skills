import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dash.admission import LeaseError, LeaseStore, canonical_files, canonical_path


class AdmissionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._cwd = os.getcwd()
        os.chdir(self.root)
        self.state_dir = self.root / "state"
        self.store = LeaseStore(self.state_dir)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def claim(self, owner, files, ttl_s=900, now=None, store=None):
        return (store or self.store).claim(owner, "WS35", "task", files, ttl_s=ttl_s, now=now)

    def test_two_spellings_of_one_path_collide(self):
        first = self.claim("a", ["./src/mod.py"])
        self.assertTrue(first["ok"])
        second = self.claim("b", ["src//mod.py"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["reason"], "conflict")
        self.assertEqual(second["conflicts"][0]["files"], ["src/mod.py"])
        third = self.claim("c", ["src/../src/mod.py"])
        self.assertFalse(third["ok"])

    def test_symlinked_root_and_path_resolve_to_one_name(self):
        (self.root / "real").mkdir()
        (self.root / "real" / "mod.py").write_text("x")
        os.symlink(self.root / "real", self.root / "link")
        self.assertEqual(canonical_path("link/mod.py", self.root), "real/mod.py")
        self.assertEqual(canonical_path("real/mod.py", self.root), "real/mod.py")

    def test_disjoint_files_pass(self):
        self.assertTrue(self.claim("a", ["src/one.py"])["ok"])
        self.assertTrue(self.claim("b", ["src/two.py"])["ok"])
        self.assertEqual(len(self.store.active()), 2)

    def test_absolute_outside_and_glob_declarations_refuse(self):
        for bad in ["/etc/passwd", "../escape.py", "src/*.py", "src/[ab].py", "", "~/.bashrc"]:
            with self.subTest(bad=bad):
                with self.assertRaises(LeaseError):
                    canonical_path(bad, self.root)
        with self.assertRaises(LeaseError):
            canonical_files([], self.root)
        refused = self.claim("a", ["/etc/passwd"])
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["reason"], "invalid")
        self.assertEqual(self.store.active(), [])

    def test_concurrent_claimers_produce_exactly_one_winner(self):
        barrier = threading.Barrier(8)
        results = []
        lock = threading.Lock()

        def run(index):
            store = LeaseStore(self.state_dir)
            barrier.wait()
            result = store.claim(f"owner-{index}", "WS35", "task", ["src/shared.py"])
            with lock:
                results.append(result)

        threads = [threading.Thread(target=run, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [item for item in results if item["ok"]]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(self.store.active()), 1)

    def test_expired_lease_is_claimable(self):
        first = self.claim("a", ["src/mod.py"], ttl_s=10, now=1000.0)
        self.assertTrue(first["ok"])
        self.assertEqual(self.store.active(now=1005.0)[0]["lease_id"], first["lease_id"])
        self.assertEqual(self.store.active(now=1011.0), [])
        second = self.claim("b", ["src/mod.py"], now=1011.0)
        self.assertTrue(second["ok"])

    def test_renewed_lease_is_not_stolen(self):
        first = self.claim("a", ["src/mod.py"], ttl_s=10, now=1000.0)
        renewed = self.store.renew(first["lease_id"], "a", ttl_s=10, now=1005.0)
        self.assertTrue(renewed["ok"])
        self.assertEqual(renewed["expires_at"], 1015.0)
        stolen = self.claim("b", ["src/mod.py"], now=1012.0)
        self.assertFalse(stolen["ok"])
        self.assertEqual(stolen["reason"], "conflict")

    def test_dead_pid_lease_reaps_while_live_pid_lease_remains(self):
        dead = self.claim("dead", ["src/dead.py"])
        live = self.claim("live", ["src/live.py"])
        self.assertTrue(dead["ok"] and live["ok"])
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait()
        state_path = self.state_dir / "admission.json"
        state = json.loads(state_path.read_text())
        state["leases"][dead["lease_id"]]["pid"] = finished.pid
        state["leases"][live["lease_id"]]["pid"] = os.getpid()
        state_path.write_text(json.dumps(state))
        reaped = self.store.reap()
        self.assertEqual(reaped["reaped"], [dead["lease_id"]])
        remaining = [row["lease_id"] for row in self.store.active()]
        self.assertEqual(remaining, [live["lease_id"]])

    def test_release_and_reap_are_idempotent(self):
        lease = self.claim("a", ["src/mod.py"])
        first = self.store.release(lease["lease_id"], "a")
        self.assertTrue(first["ok"])
        self.assertTrue(first["released"])
        second = self.store.release(lease["lease_id"], "a")
        self.assertTrue(second["ok"])
        self.assertFalse(second["released"])
        self.assertEqual(self.store.reap()["reaped"], [])
        self.assertEqual(self.store.reap()["reaped"], [])

    def test_release_owner_drops_every_lease(self):
        self.claim("a", ["src/one.py"])
        self.claim("a", ["src/two.py"])
        self.claim("b", ["src/three.py"])
        released = self.store.release_owner("a")
        self.assertEqual(len(released["released"]), 2)
        self.assertEqual([row["owner"] for row in self.store.active()], ["b"])

    def test_global_cap_is_never_exceeded(self):
        first = self.store.admit_launch("a", "WS35", "t", ["src/one.py"], max_concurrent=2)
        second = self.store.admit_launch("b", "WS35", "t", ["src/two.py"], max_concurrent=2)
        third = self.store.admit_launch("c", "WS35", "t", ["src/three.py"], max_concurrent=2)
        self.assertEqual([first["reason"], second["reason"], third["reason"]], ["ok", "ok", "capacity"])
        self.assertFalse(third["ok"])
        self.assertEqual(len(self.store.active()), 2)
        self.store.release(first["lease_id"], "a")
        fourth = self.store.admit_launch("c", "WS35", "t", ["src/three.py"], max_concurrent=2)
        self.assertTrue(fourth["ok"])

    def test_admit_launch_reports_conflict_before_capacity(self):
        self.claim("a", ["src/mod.py"])
        result = self.store.admit_launch("b", "WS35", "t", ["src/mod.py"], max_concurrent=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "capacity")
        result = self.store.admit_launch("b", "WS35", "t", ["src/mod.py"], max_concurrent=5)
        self.assertEqual(result["reason"], "conflict")

    def test_owner_token_is_required(self):
        lease = self.claim("a", ["src/mod.py"])
        self.assertEqual(self.store.renew(lease["lease_id"], "b")["reason"], "owner")
        self.assertEqual(self.store.release(lease["lease_id"], "b")["reason"], "owner")
        self.assertEqual(len(self.store.active()), 1)
        self.assertTrue(self.store.renew(lease["lease_id"], "a")["ok"])
        self.assertTrue(self.store.release(lease["lease_id"], "a")["released"])

    def test_snapshot_reports_version_and_leases(self):
        lease = self.claim("a", ["src/mod.py"])
        body = self.store.snapshot()
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["leases"][0]["lease_id"], lease["lease_id"])
        self.assertEqual(body["leases"][0]["files"], ["src/mod.py"])
        self.assertEqual(body["leases"][0]["ws"], "WS35")

    def test_state_file_is_versioned_and_atomic(self):
        self.claim("a", ["src/mod.py"])
        state = json.loads((self.state_dir / "admission.json").read_text())
        self.assertEqual(state["version"], 1)
        self.assertEqual(len(state["leases"]), 1)
        self.assertEqual([name for name in os.listdir(self.state_dir) if name.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
