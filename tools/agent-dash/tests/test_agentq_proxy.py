import asyncio
import http.client
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentq.proxy import (
    DEFAULT_CONFIG,
    Governor,
    Proxy,
    load_key,
    merge_config,
    parse_stated_limit,
    read_request,
)

KEY = "SECRET-KEY-123"
MODEL_OUT = "morph-dsv41flash-1017"
MODEL_KIMI = "morph-kimik31"


def quiet(event, **fields):
    return None


def config_with(**overrides):
    return merge_config(DEFAULT_CONFIG, overrides)


def make_db(home: Path, lanes: dict[str, str]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(home / "agentq.db")
    connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, lane TEXT)")
    connection.executemany("INSERT INTO jobs VALUES (?, ?)", list(lanes.items()))
    connection.commit()
    connection.close()


def post_json(port: int, body: dict, job: str, auth: str):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    headers = {"Content-Type": "application/json", "Authorization": auth}
    if job:
        headers["X-Agentq-Job"] = job
    connection.request("POST", "/v1/chat/completions", body=json.dumps(body), headers=headers)
    response = connection.getresponse()
    data = response.read().decode()
    connection.close()
    return response.status, data


def get_json(port: int, path: str):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    connection.request("GET", path)
    response = connection.getresponse()
    data = response.read().decode()
    connection.close()
    return response.status, data


async def wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.02)


class FakeMorph:
    """In-process upstream. Each script item is consumed by one request, in order."""

    def __init__(self) -> None:
        self.script: list[dict] = []
        self.requests: list[dict] = []
        self.default_usage = (100, 50)

    async def handle(self, reader, writer) -> None:
        try:
            _, path, headers, body = await read_request(reader)
            payload = json.loads(body)
            self.requests.append(
                {
                    "job": payload["messages"][0]["content"],
                    "auth": headers.get("authorization", ""),
                    "path": path,
                    "payload": payload,
                }
            )
            item = self.script.pop(0) if self.script else {}
            if item.get("hold") is not None:
                await item["hold"].wait()
            if "status" in item:
                raw = item["body"].encode()
                head = (
                    f"HTTP/1.1 {item['status']} Too Many Requests\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(raw)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
                writer.write(head + raw)
                await writer.drain()
                return
            prompt, completion = item.get("usage", self.default_usage)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            )
            events = [
                {"choices": [{"delta": {"content": "hi"}}]},
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "prompt_tokens_details": {"cached_tokens": 0},
                    },
                },
                "[DONE]",
            ]
            for event in events:
                text = event if isinstance(event, str) else f"{json.dumps(event)}"
                data = f"data: {text}\n\n".encode()
                writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, ValueError):
            return
        finally:
            writer.close()


class ProxyCase(unittest.IsolatedAsyncioTestCase):
    window = 2.0
    overrides: dict = {}

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "agentq"
        self.fake = FakeMorph()
        self.fake_server = await asyncio.start_server(self.fake.handle, "127.0.0.1", 0)
        upstream_port = self.fake_server.sockets[0].getsockname()[1]
        self.logs: list[tuple[str, dict]] = []
        settings = {
            "window_s": self.window,
            "retry_floor_s": 0.05,
            "retry_jitter_s": 0.05,
            "give_up_s": 30.0,
        }
        settings.update(self.overrides)
        self.config = config_with(**settings)
        self.gov = Governor(self.home, self.config, log=self.capture)
        self.proxy = Proxy(self.gov, f"http://127.0.0.1:{upstream_port}/v1", KEY)
        self.server = await self.proxy.start("127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.proxy.close()
        self.fake_server.close()
        self.tmp.cleanup()

    def capture(self, event: str, **fields) -> None:
        self.logs.append((event, fields))

    async def post(self, body: dict, job: str = "", auth: str = "Bearer client-junk"):
        if job:
            body["messages"] = [{"role": "user", "content": job}]
        return await asyncio.to_thread(post_json, self.port, body, job, auth)

    def chat(self, model: str, max_tokens: int = 800, stream: bool = True, text: str = "hello") -> dict:
        return {
            "model": model,
            "max_tokens": max_tokens,
            "stream": stream,
            "messages": [{"role": "user", "content": text}],
        }


class PassthroughTest(ProxyCase):
    async def test_streams_through_and_captures_usage(self) -> None:
        status, text = await self.post(self.chat(MODEL_OUT), job="job-a")
        self.assertEqual(status, 200)
        self.assertIn('"content": "hi"', text)
        self.assertIn("data: [DONE]", text)
        sent = self.fake.requests[0]
        self.assertEqual(sent["auth"], f"Bearer {KEY}")
        self.assertTrue(sent["payload"]["stream_options"]["include_usage"])
        self.assertEqual(sent["job"], "job-a")
        total = self.gov.budgets["total"]
        self.assertEqual(total.used(self.gov.clock(), self.window), 150)
        output = self.gov.budgets["output"]
        self.assertEqual(output.used(self.gov.clock(), self.window), 50)

    async def test_status_endpoint_reports_budgets(self) -> None:
        status, text = await asyncio.to_thread(get_json, self.port, "/agentq/status")
        self.assertEqual(status, 200)
        body = json.loads(text)
        self.assertEqual(body["budgets"]["output"]["limit"], 70_000)
        self.assertEqual(body["budgets"]["uncached"]["limit"], 24_290)
        self.assertEqual(set(body["budgets"]["total"]["queued"]), {"urgent", "normal", "backlog"})

    async def test_429_is_retried_and_never_reaches_client(self) -> None:
        self.fake.script = [
            {"status": 429, "body": '{"error":"rate_limited","message":"Limit: 70000 output tokens/minute."}'}
        ]
        status, text = await self.post(self.chat(MODEL_OUT), job="job-r")
        self.assertEqual(status, 200)
        self.assertIn("data: [DONE]", text)
        self.assertEqual(len(self.fake.requests), 2)
        self.assertIn("retry", [event for event, _ in self.logs])



class GiveUpTest(ProxyCase):
    overrides = {"window_s": 2.0, "give_up_s": 0.5}

    async def test_queued_request_gives_up_with_held_body(self) -> None:
        self.gov.budgets["output"].limit = 1000
        hold = asyncio.Event()
        self.fake.script = [{"usage": (100, 800), "hold": hold}]
        blocker = asyncio.create_task(self.post(self.chat(MODEL_OUT, 800), job="blocker"))
        await wait_for(lambda: len(self.fake.requests) == 1)
        status, text = await self.post(self.chat(MODEL_OUT, 800), job="waiter")
        hold.set()
        await blocker
        self.assertEqual(status, 429)
        self.assertEqual(text, "agentq: held 0.5 s, budget full")
        self.assertEqual([r["job"] for r in self.fake.requests], ["blocker"])

    async def test_retry_loop_gives_up_with_held_body(self) -> None:
        self.fake.script = [{"status": 429, "body": "Limit: 70000 output tokens/minute."}] * 200
        status, text = await self.post(self.chat(MODEL_OUT), job="looping")
        self.assertEqual(status, 429)
        self.assertEqual(text, "agentq: held 0.5 s, budget full")
        self.assertGreater(len(self.fake.requests), 1)


class ClientGoneTest(ProxyCase):
    overrides = {"window_s": 1.0}

    async def test_client_disconnect_while_queued_is_never_sent(self) -> None:
        self.gov.budgets["output"].limit = 1000
        hold = asyncio.Event()
        self.fake.script = [{"usage": (100, 800), "hold": hold}]
        blocker = asyncio.create_task(self.post(self.chat(MODEL_OUT, 800), job="blocker"))
        await wait_for(lambda: len(self.fake.requests) == 1)

        payload = json.dumps(
            {"model": MODEL_OUT, "max_tokens": 800, "stream": True, "messages": [{"role": "user", "content": "ghost"}]}
        ).encode()
        head = (
            "POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            f"X-Agentq-Job: ghost\r\nContent-Length: {len(payload)}\r\n\r\n"
        ).encode()
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(head + payload)
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.close()
        await asyncio.sleep(0.3)
        self.assertIn("client_gone", [event for event, _ in self.logs])

        after = asyncio.create_task(self.post(self.chat(MODEL_OUT, 800), job="after"))
        await asyncio.sleep(0.1)
        hold.set()
        results = await asyncio.gather(blocker, after)
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual([r["job"] for r in self.fake.requests], ["blocker", "after"])


class LearningTest(ProxyCase):
    async def test_target_shrinks_and_stated_limit_is_adopted(self) -> None:
        self.fake.script = [{"status": 429, "body": "Limit: 60000 output tokens/minute."}]
        status, _ = await self.post(self.chat(MODEL_OUT), job="job-t")
        self.assertEqual(status, 200)
        output = self.gov.budgets["output"]
        self.assertEqual(output.limit, 60_000)
        self.assertLess(output.target, 0.95)
        self.assertAlmostEqual(output.target, 0.95 * 0.97, delta=0.01)
        self.assertIn("limit_adopted", [event for event, _ in self.logs])
        state = json.loads((self.home / "proxy_state.json").read_text())
        self.assertEqual(state["budgets"]["output"]["limit"], 60_000)

    async def test_key_never_reaches_logs_state_or_status(self) -> None:
        self.fake.script = [{"status": 429, "body": "Limit: 60000 output tokens/minute."}]
        await self.post(self.chat(MODEL_OUT), job="job-k")
        _, status_text = await asyncio.to_thread(get_json, self.port, "/agentq/status")
        self.assertNotIn(KEY, json.dumps(self.logs, default=str))
        self.assertNotIn(KEY, status_text)
        for path in self.home.rglob("*"):
            if path.is_file():
                self.assertNotIn(KEY.encode(), path.read_bytes(), path.name)


class LaneTest(ProxyCase):
    overrides = {"window_s": 1.0}

    async def test_lane_order_under_tiny_budget(self) -> None:
        make_db(
            self.home,
            {"blocker": "normal", "backlog-job": "backlog", "normal-job": "normal", "urgent-job": "urgent"},
        )
        self.gov.budgets["output"].limit = 1000
        self.fake.default_usage = (100, 800)
        hold = asyncio.Event()
        self.fake.script = [{"usage": (100, 800), "hold": hold}]

        blocker = asyncio.create_task(self.post(self.chat(MODEL_OUT, 800), job="blocker"))
        await wait_for(lambda: len(self.fake.requests) == 1)
        queued = []
        for job in ("backlog-job", "normal-job", "urgent-job"):
            queued.append(asyncio.create_task(self.post(self.chat(MODEL_OUT, 800), job=job)))
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.fake.requests), 1)

        hold.set()
        results = await asyncio.gather(blocker, *queued)
        self.assertEqual([status for status, _ in results], [200, 200, 200, 200])
        order = [request["job"] for request in self.fake.requests[1:]]
        self.assertEqual(order, ["urgent-job", "normal-job", "backlog-job"])

    async def test_urgent_guard_keeps_half_budget_while_lower_lanes_wait(self) -> None:
        make_db(self.home, {"urgent-a": "urgent", "urgent-b": "urgent", "normal-c": "normal"})
        self.gov.budgets["output"].limit = 1000
        self.gov.budgets["output"].target = 1.0
        self.fake.default_usage = (100, 300)
        hold = asyncio.Event()
        self.fake.script = [{"usage": (100, 300), "hold": hold}]

        first = asyncio.create_task(self.post(self.chat(MODEL_OUT, 300), job="urgent-a"))
        await wait_for(lambda: len(self.fake.requests) == 1)
        third = asyncio.create_task(self.post(self.chat(MODEL_OUT, 800), job="normal-c"))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(self.post(self.chat(MODEL_OUT, 300), job="urgent-b"))
        await asyncio.sleep(0.4)
        self.assertEqual([r["job"] for r in self.fake.requests], ["urgent-a"])

        hold.set()
        results = await asyncio.gather(first, second, third)
        self.assertEqual([status for status, _ in results], [200, 200, 200])
        order = [r["job"] for r in self.fake.requests]
        self.assertEqual(order, ["urgent-a", "urgent-b", "normal-c"])


class EstimateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.gov = Governor(Path(self.tmp.name), config_with(), log=quiet)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_kimi_uncached_estimate_uses_job_prefix(self) -> None:
        first = self.gov.make_ticket(job="job-k", lane="normal", model=MODEL_KIMI, text="A" * 4000, max_tokens=None, now=0.0)
        self.assertEqual(set(first.estimates), {"total", "uncached"})
        self.assertEqual(first.estimates["uncached"], 1000)
        second = self.gov.make_ticket(
            job="job-k", lane="normal", model=MODEL_KIMI, text="A" * 4000 + "B" * 400, max_tokens=None, now=0.0
        )
        self.assertEqual(second.estimates["uncached"], 100)
        other = self.gov.make_ticket(
            job="job-other", lane="normal", model=MODEL_KIMI, text="A" * 4000 + "B" * 400, max_tokens=None, now=0.0
        )
        self.assertEqual(other.estimates["uncached"], 1100)

    def test_output_estimate_is_capped_by_max_tokens(self) -> None:
        ticket = self.gov.make_ticket(job="j", lane="normal", model=MODEL_OUT, text="x", max_tokens=300, now=0.0)
        self.assertEqual(ticket.estimates["output"], 300)

    def test_stated_limit_parsing(self) -> None:
        self.assertEqual(parse_stated_limit("Limit: 10000000 tokens/minute."), ("total", 10_000_000))
        self.assertEqual(parse_stated_limit("Limit: 70000 output tokens/minute."), ("output", 70_000))
        self.assertEqual(parse_stated_limit("Limit: 24290 uncached input tokens/minute."), ("uncached", 24_290))
        self.assertIsNone(parse_stated_limit('{"code":"service_overloaded"}'))


class StateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "agentq"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_state_persists_across_restart(self) -> None:
        config = config_with()
        first = Governor(self.home, config, log=quiet)
        ticket = first.make_ticket(job="j", lane="normal", model=MODEL_OUT, text="x", max_tokens=800, now=0.0)
        first.learn_from_429(ticket, "Limit: 60000 output tokens/minute.")

        restarted = Governor(self.home, config, log=quiet)
        output = restarted.budgets["output"]
        self.assertEqual(output.limit, 60_000)
        self.assertAlmostEqual(output.target, 0.95 * 0.97, places=4)
        self.assertEqual(restarted.budgets["total"].target, 0.95)

    def test_missing_db_means_normal_lane(self) -> None:
        empty = Governor(self.home, config_with(), log=quiet)
        self.assertEqual(empty.lane_for("job-x"), "normal")
        self.assertEqual(empty.lane_for(""), "normal")

    def test_lane_read_from_jobs_table(self) -> None:
        make_db(self.home, {"job-u": "urgent", "job-b": "backlog", "job-bad": "weird"})
        gov = Governor(self.home, config_with(), log=quiet)
        self.assertEqual(gov.lane_for("job-u"), "urgent")
        self.assertEqual(gov.lane_for("job-b"), "backlog")
        self.assertEqual(gov.lane_for("job-bad"), "normal")
        self.assertEqual(gov.lane_for("job-missing"), "normal")

    def test_aged_normal_ticket_promotes_to_urgent(self) -> None:
        gov = Governor(self.home, config_with(aging_s=900.0), log=quiet)
        old = gov.make_ticket(job="old", lane="normal", model=MODEL_OUT, text="x", max_tokens=8, now=0.0)
        fresh = gov.make_ticket(job="fresh", lane="normal", model=MODEL_OUT, text="x", max_tokens=8, now=1000.0)
        gov.enqueue(old)
        gov.enqueue(fresh)
        gov.promote_aged(now=1000.0)
        self.assertEqual(old.lane, "urgent")
        self.assertEqual(fresh.lane, "normal")
        self.assertEqual(list(gov.queues["urgent"]), [old])


class KeyTest(unittest.TestCase):
    def test_override_env_supplies_key(self) -> None:
        self.assertEqual(load_key({"AGENTQ_UPSTREAM_KEY": KEY}), KEY)


if __name__ == "__main__":
    unittest.main()
