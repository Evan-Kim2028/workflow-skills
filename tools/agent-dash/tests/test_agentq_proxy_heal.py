"""Hang auto-heal for the proxy: the healer's decisions against a stub clock and a stub launchctl runner, the probe
against a real local HTTP stub, and the broker tick's independence from the healer."""

import http.server
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentq import broker as B  # noqa: E402

UID = 501
TARGET = f"gui/{UID}/com.agentq.proxy"
RUNNING = (0, "state = running\n\tpid = 4242\n")
SETTINGS = {"enabled": True, "fail_s": 90.0, "min_interval_s": 600.0, "probe_s": 10.0, "corroborate_lag_ms": 2000.0}


class Clock:
    def __init__(self, at: float = 1000.0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


class HealTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.clock = Clock()
        self.kind = "timeout"
        self.doc: dict | None = None
        self.tcp_ok = False
        self.launchd = RUNNING
        self.kick_rc = 0
        self.kick_text = ""
        self.calls: list[list[str]] = []
        self.logs: list[str] = []

    def runner(self, args):
        self.calls.append(list(args))
        if args[0] == "print":
            return self.launchd
        return (self.kick_rc, self.kick_text)

    def healer(self, **overrides) -> B.ProxyHealer:
        settings = {**SETTINGS, **overrides}
        healer = B.ProxyHealer(
            settings,
            probe=lambda: (self.kind, self.doc),
            runner=self.runner,
            tcp_connect=lambda: self.tcp_ok,
            uid=UID,
            now=self.clock,
            log=self.logs.append,
            state_path=self.home / B.HEAL_STATE_FILE,
        )
        self.addCleanup(healer.stop)
        return healer

    def kicks(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "kickstart"]

    def hang(self) -> None:
        self.kind, self.doc = "timeout", None


class DecisionTest(HealTest):
    def test_a_healthy_proxy_is_never_restarted(self) -> None:
        self.kind, self.doc = "ok", {"loop_lag_ms": {"max_60s_ms": 0}}
        healer = self.healer()
        for at in (1000, 1200, 1500):
            self.clock.at = at
            healer.step()
        self.assertEqual(self.kicks(), [])
        self.assertEqual(self.logs, [])

    def test_a_hung_and_unreachable_proxy_is_restarted_once_with_one_log_line(self) -> None:
        self.hang()
        healer = self.healer()
        for at in (1000, 1050):
            self.clock.at = at
            healer.step()
        self.assertEqual(self.kicks(), [])
        self.clock.at = 1095
        healer.step()
        self.assertEqual(self.kicks(), [["kickstart", "-k", TARGET]])
        self.assertEqual(self.logs, ["proxy unresponsive for 95 s; restarting"])
        self.clock.at = 1105
        healer.step()
        self.assertEqual(len(self.kicks()), 1)
        self.assertEqual(len(self.logs), 1)

    def test_the_interval_survives_a_broker_restart(self) -> None:
        self.hang()
        first = self.healer()
        for at in (1000, 1095):
            self.clock.at = at
            first.step()
        self.assertEqual(len(self.kicks()), 1)
        self.assertEqual(json.loads((self.home / B.HEAL_STATE_FILE).read_text())["last_attempt"], 1095)

        second = self.healer()
        for at in (1190, 1280, 1600):
            self.clock.at = at
            second.step()
        self.assertEqual(len(self.kicks()), 1)
        self.clock.at = 1700
        second.step()
        self.assertEqual(len(self.kicks()), 2)

    def test_a_restart_needs_fail_s_of_continuous_failure(self) -> None:
        self.hang()
        healer = self.healer()
        healer.step()
        self.clock.at = 1050
        self.kind, self.doc = "ok", {}
        healer.step()
        self.kind = "timeout"
        self.clock.at = 1060
        healer.step()
        self.clock.at = 1149
        healer.step()
        self.assertEqual(self.kicks(), [])
        self.clock.at = 1151
        healer.step()
        self.assertEqual(len(self.kicks()), 1)

    def test_a_job_that_is_not_loaded_or_has_no_pid_is_never_touched(self) -> None:
        self.hang()
        for launchd in ((113, ""), (0, "state = waiting\n")):
            with self.subTest(launchd=launchd):
                self.launchd = launchd
                self.calls.clear()
                healer = self.healer()
                healer.step()
                self.clock.at += 500
                healer.step()
                self.assertEqual(self.kicks(), [])

    def test_a_disabled_healer_never_probes_or_restarts(self) -> None:
        probes: list[int] = []
        healer = B.ProxyHealer(
            {**SETTINGS, "enabled": False},
            probe=lambda: probes.append(1) or ("timeout", None),
            runner=self.runner,
            tcp_connect=lambda: False,
            uid=UID,
            now=self.clock,
            log=self.logs.append,
            state_path=self.home / B.HEAL_STATE_FILE,
        )
        self.clock.at += 1000
        healer.step()
        self.assertEqual(probes, [])
        self.assertEqual(self.calls, [])


class CorroborationTest(HealTest):
    def test_refused_with_the_port_answering_and_no_lag_evidence_is_never_restarted(self) -> None:
        self.kind, self.doc = "refused", None
        self.tcp_ok = True
        healer = self.healer()
        healer.last_ok_doc = {"loop_lag_ms": {"max_60s_ms": 0}}
        for at in (1000, 1095, 1400):
            self.clock.at = at
            healer.step()
        self.assertEqual(self.kicks(), [])

    def test_a_refused_port_that_does_not_connect_is_restarted(self) -> None:
        self.kind = "refused"
        self.tcp_ok = False
        healer = self.healer()
        for at in (1000, 1095):
            self.clock.at = at
            healer.step()
        self.assertEqual(len(self.kicks()), 1)

    def test_a_silent_proxy_whose_last_status_showed_loop_lag_is_restarted(self) -> None:
        self.hang()
        self.tcp_ok = True
        healer = self.healer()
        healer.last_ok_doc = {"loop_lag_ms": {"max_60s_ms": 5000}}
        for at in (1000, 1095):
            self.clock.at = at
            healer.step()
        self.assertEqual(len(self.kicks()), 1)


class SlowButAnsweringTest(HealTest):
    def test_a_status_answered_within_ten_seconds_is_never_restarted(self) -> None:
        stub = StatusStub("hang", hang_s=6.0)
        self.addCleanup(stub.close)
        kind, _ = B.probe_status(stub.url, B.HEAL_PROBE_S)
        self.assertEqual(kind, "ok")
        self.kind, self.doc = kind, {"loop_lag_ms": {"max_60s_ms": 20000}}
        healer = self.healer()
        for at in (1000, 1095, 1400):
            self.clock.at = at
            healer.step()
        self.assertEqual(self.kicks(), [])

    def test_no_answer_within_ten_seconds_is_unhealthy(self) -> None:
        stub = StatusStub("hang", hang_s=B.HEAL_PROBE_S + 1.0)
        self.addCleanup(stub.close)
        started = time.monotonic()
        kind, doc = B.probe_status(stub.url, B.HEAL_PROBE_S)
        self.assertEqual((kind, doc), ("timeout", None))
        self.assertLess(time.monotonic() - started, B.HEAL_PROBE_S + 3)


class FailedRestartTest(HealTest):
    def test_a_failed_kickstart_is_an_attempt_not_a_success(self) -> None:
        self.hang()
        self.kick_rc, self.kick_text = 1, "Could not find service"
        healer = self.healer()
        for at in (1000, 1095):
            self.clock.at = at
            healer.step()
        self.assertEqual(len(self.kicks()), 1)
        self.assertIn("proxy restart failed: rc=1 Could not find service", self.logs)
        self.assertIsNotNone(healer.unhealthy_since)
        self.assertEqual(json.loads((self.home / B.HEAL_STATE_FILE).read_text())["last_result"], "failed:1")
        self.clock.at = 1105
        healer.step()
        self.assertEqual(len(self.kicks()), 1)
        self.clock.at = 1700
        healer.step()
        self.assertEqual(len(self.kicks()), 2)
        self.kick_rc = 0
        self.clock.at = 2300
        healer.step()
        self.assertIsNone(healer.unhealthy_since)

    def test_a_launchctl_timeout_is_reported_with_code_124(self) -> None:
        with mock.patch.object(B.subprocess, "run", side_effect=subprocess.TimeoutExpired(["launchctl"], 15)):
            code, text = B.launchctl(["kickstart", "-k", TARGET])
        self.assertEqual(code, 124)
        self.assertIn("timed out", text)

    def test_a_missing_launchctl_is_reported_with_code_127(self) -> None:
        with mock.patch.object(B.subprocess, "run", side_effect=FileNotFoundError("launchctl")):
            code, text = B.launchctl(["print", TARGET])
        self.assertEqual(code, 127)
        self.assertIn("unavailable", text)

    def test_a_timed_out_kickstart_is_logged_as_failed_and_not_as_a_restart(self) -> None:
        self.hang()
        self.kick_rc, self.kick_text = 124, "launchctl timed out after 15 s"
        healer = self.healer()
        for at in (1000, 1095):
            self.clock.at = at
            healer.step()
        self.assertIn("proxy restart failed: rc=124 launchctl timed out after 15 s", self.logs)
        self.assertNotIn("proxy restart requested", self.logs)


class PersistenceTest(HealTest):
    def test_an_unreadable_state_file_reads_as_no_attempt(self) -> None:
        (self.home / B.HEAL_STATE_FILE).write_text("{not json")
        healer = self.healer()
        self.assertIsNone(healer.last_attempt)

    def test_the_settings_default_on_and_read_overrides(self) -> None:
        self.assertEqual(B.load_config(self.home)["proxy_heal"], {
            "enabled": True, "fail_s": 90.0, "min_interval_s": 600.0, "probe_s": 10.0, "corroborate_lag_ms": 2000.0,
        })
        (self.home / "broker.json").write_text(json.dumps({"proxy_heal": {"enabled": False, "fail_s": 30, "probe_s": 5}}))
        settings = B.load_config(self.home)["proxy_heal"]
        self.assertEqual((settings["enabled"], settings["fail_s"], settings["probe_s"]), (False, 30.0, 5.0))


class LatestTest(HealTest):
    def test_the_tick_reads_a_fresh_probe_and_never_a_stale_one(self) -> None:
        self.kind, self.doc = "ok", {"saturated": False}
        healer = self.healer()
        healer.step()
        self.assertEqual(healer.latest(), ({"saturated": False}, True))
        self.kind = "timeout"
        self.clock.at += 1
        healer.step()
        self.assertEqual(healer.latest(), (None, True))
        self.clock.at += 30
        self.assertEqual(healer.latest(), (None, False))


class StatusStub:
    """A local status endpoint that answers, or answers only after hang_s seconds."""

    def __init__(self, mode: str, hang_s: float = 0.0) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if mode == "hang":
                    time.sleep(hang_s)
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/agentq/status"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class BrokerTickTest(HealTest):
    def test_the_tick_never_waits_on_the_healer_and_never_probes(self) -> None:
        probed_on: list[str] = []

        def slow_probe():
            probed_on.append(threading.current_thread().name)
            time.sleep(0.3)
            return "timeout", None

        healer = B.ProxyHealer(
            {**SETTINGS, "probe_s": 0.05},
            probe=slow_probe, runner=self.runner, tcp_connect=lambda: False, uid=UID,
            now=time.time, log=self.logs.append, state_path=self.home / B.HEAL_STATE_FILE,
        )
        self.addCleanup(healer.stop)
        healer.start()
        time.sleep(0.1)
        broker = B.Broker(self.home, launcher=mock.Mock(), now=time.time, proxy=lambda: None, healer=healer)
        durations = []
        for _ in range(5):
            started = time.monotonic()
            broker.tick()
            durations.append(time.monotonic() - started)
        self.assertTrue(all(d < 0.05 for d in durations), durations)
        self.assertTrue(probed_on)
        self.assertTrue(all(name == "agentq-proxy-heal" for name in probed_on), probed_on)

    def test_the_broker_status_comes_from_a_fresh_healer_probe(self) -> None:
        self.kind, self.doc = "ok", {"saturated": True}
        healer = self.healer()
        healer.now = time.time
        healer.step()
        proxy = mock.Mock(return_value=None)
        broker = B.Broker(self.home, launcher=mock.Mock(), now=time.time, proxy=proxy, healer=healer)
        self.assertEqual(broker._proxy_status(), {"saturated": True})
        proxy.assert_not_called()

    def test_a_broker_without_a_healer_probes_the_proxy_itself(self) -> None:
        proxy = mock.Mock(return_value={"saturated": False})
        broker = B.Broker(self.home, launcher=mock.Mock(), now=time.time, proxy=proxy)
        self.assertEqual(broker._proxy_status(), {"saturated": False})
        proxy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
