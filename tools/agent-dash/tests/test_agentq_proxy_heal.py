"""Hang auto-heal for the proxy: the healer's decisions against a stub clock and stub launchctl, and the
status probe against a real local HTTP stub that answers, refuses, or hangs."""

import http.server
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentq import broker as B  # noqa: E402

UID = 501
TARGET = f"gui/{UID}/com.agentq.proxy"
RUNNING = (0, "state = running\n\tpid = 4242\n")


class Clock:
    def __init__(self) -> None:
        self.at = 1000.0

    def __call__(self) -> float:
        return self.at


class ProxyHealerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.up = False
        self.calls: list[list[str]] = []
        self.logs: list[str] = []
        self.launchd = RUNNING

    def runner(self, args):
        self.calls.append(list(args))
        if args[0] == "print":
            return self.launchd
        return (0, "")

    def healer(self, enabled: bool = True) -> B.ProxyHealer:
        settings = {"enabled": enabled, "fail_s": 90.0, "min_interval_s": 600.0}
        return B.ProxyHealer(
            settings,
            probe=lambda: {"ok": True} if self.up else None,
            runner=self.runner,
            uid=UID,
            now=self.clock,
            log=self.logs.append,
        )

    def kicks(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "kickstart"]

    def test_a_healthy_proxy_is_never_restarted(self) -> None:
        self.up = True
        healer = self.healer()
        for _ in range(3):
            self.clock.at += 1000
            self.assertFalse(healer.check())
        self.assertEqual(self.kicks(), [])
        self.assertEqual(self.logs, [])

    def test_a_hung_proxy_is_restarted_once_after_fail_s_with_one_log_line(self) -> None:
        healer = self.healer()
        healer.check()
        self.clock.at += 60
        self.assertFalse(healer.check())
        self.clock.at += 35
        self.assertTrue(healer.check())
        self.assertEqual(self.kicks(), [["kickstart", "-k", TARGET]])
        self.assertEqual(self.logs, ["proxy unresponsive for 95 s; restarting"])
        self.clock.at += 5
        healer.check()
        self.assertEqual(len(self.kicks()), 1)
        self.assertEqual(len(self.logs), 1)

    def test_restarts_are_at_least_min_interval_apart(self) -> None:
        healer = self.healer()
        healer.check()
        self.clock.at += 95
        healer.check()
        self.clock.at += 595
        healer.check()
        self.assertEqual(len(self.kicks()), 1)
        self.clock.at += 95
        healer.check()
        self.assertEqual(len(self.kicks()), 2)

    def test_after_a_restart_the_proxy_must_fail_for_fail_s_again(self) -> None:
        healer = self.healer()
        healer.check()
        self.clock.at += 95
        healer.check()
        self.clock.at += 600
        healer.check()
        self.assertEqual(len(self.kicks()), 1)
        self.clock.at += 89
        healer.check()
        self.assertEqual(len(self.kicks()), 1)
        self.clock.at += 2
        healer.check()
        self.assertEqual(len(self.kicks()), 2)

    def test_a_job_that_is_not_loaded_is_never_touched(self) -> None:
        self.launchd = (113, "")
        healer = self.healer()
        healer.check()
        self.clock.at += 500
        self.assertFalse(healer.check())
        self.assertEqual(self.kicks(), [])
        self.assertEqual(self.logs, [])

    def test_a_loaded_job_without_a_running_pid_is_never_touched(self) -> None:
        self.launchd = (0, "state = waiting\n")
        healer = self.healer()
        healer.check()
        self.clock.at += 500
        self.assertFalse(healer.check())
        self.assertEqual(self.kicks(), [])

    def test_recovery_starts_a_new_episode_that_needs_fail_s_again(self) -> None:
        healer = self.healer()
        healer.check()
        self.clock.at += 50
        self.up = True
        healer.check()
        self.up = False
        self.clock.at += 10
        healer.check()
        self.clock.at += 80
        self.assertFalse(healer.check())
        self.clock.at += 11
        self.assertTrue(healer.check())

    def test_a_disabled_healer_never_probes_or_restarts(self) -> None:
        probes: list[int] = []
        healer = B.ProxyHealer(
            {"enabled": False, "fail_s": 1.0, "min_interval_s": 1.0},
            probe=lambda: probes.append(1),
            runner=self.runner,
            uid=UID,
            now=self.clock,
            log=self.logs.append,
        )
        self.clock.at += 1000
        self.assertFalse(healer.check())
        self.assertEqual(probes, [])
        self.assertEqual(self.calls, [])

    def test_the_settings_default_to_on_and_read_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(B.load_config(home)["proxy_heal"], {"enabled": True, "fail_s": 90.0, "min_interval_s": 600.0})
            Path(home, "broker.json").write_text(json.dumps({"proxy_heal": {"enabled": False, "fail_s": 30}}))
            self.assertEqual(B.load_config(home)["proxy_heal"], {"enabled": False, "fail_s": 30.0, "min_interval_s": 600.0})


class StatusStub:
    """A local status endpoint that answers, refuses (closed), or hangs for a given time."""

    def __init__(self, mode: str, hang_s: float = 0.0) -> None:
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if stub.mode == "hang":
                    time.sleep(hang_s)
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                return

        self.mode = mode
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/agentq/status"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class StatusProbeTest(unittest.TestCase):
    def test_an_answering_stub_is_healthy(self) -> None:
        stub = StatusStub("ok")
        self.addCleanup(stub.close)
        self.assertIsNotNone(B.proxy_status(stub.url, timeout=B.HEAL_PROBE_S))

    def test_a_refused_connection_is_unhealthy(self) -> None:
        stub = StatusStub("ok")
        url = stub.url
        stub.close()
        self.assertIsNone(B.proxy_status(url, timeout=B.HEAL_PROBE_S))

    def test_a_response_slower_than_the_probe_limit_is_unhealthy(self) -> None:
        stub = StatusStub("hang", hang_s=B.HEAL_PROBE_S + 0.7)
        self.addCleanup(stub.close)
        started = time.monotonic()
        self.assertIsNone(B.proxy_status(stub.url, timeout=B.HEAL_PROBE_S))
        self.assertLess(time.monotonic() - started, B.HEAL_PROBE_S + 2)


class BrokerTickHealTest(unittest.TestCase):
    def test_the_tick_runs_the_healer_and_survives_its_failure(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as home:
            healer = mock.Mock()
            healer.check.side_effect = RuntimeError("launchctl exploded")
            broker = B.Broker(home, launcher=mock.Mock(), now=lambda: 1000.0, proxy=lambda: None, healer=healer)
            broker.log = mock.Mock()
            broker.tick()
            healer.check.assert_called_once()
            logged = [call.args[0] for call in broker.log.call_args_list]
            self.assertTrue(any("proxy heal failed" in message for message in logged), logged)

    def test_a_broker_without_a_healer_never_restarts_anything(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as home:
            broker = B.Broker(home, launcher=mock.Mock(), now=lambda: 1000.0, proxy=lambda: None)
            self.assertIsNone(broker.healer)
            broker.tick()


if __name__ == "__main__":
    unittest.main()
