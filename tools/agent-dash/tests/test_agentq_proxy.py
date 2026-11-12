import asyncio
import contextlib
import http.client
import io
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentq.proxy import (
    BLOCK_CHARS,
    DEFAULT_CONFIG,
    EX_CONFIG,
    PROXY_MINUTES_DDL,
    Client,
    Entry,
    Governor,
    Proxy,
    body_snippet,
    load_key,
    main,
    merge_config,
    minute_stamp,
    parse_stated_limit,
    prefix_blocks,
    LARGE_BODY,
    prepare_body,
    prepare_body_async,
    read_request,
    tokens_for_chars,
    write_minute_rows,
)

KEY = "SECRET-KEY-123"
MODEL_OUT = "morph-dsv41flash-1017"
MODEL_KIMI = "morph-kimik31"
PLIST = Path(__file__).resolve().parents[1] / "agentq" / "launchd" / "com.agentq.proxy.plist"


def quiet(event, **fields):
    return None


def config_with(**overrides):
    return merge_config(DEFAULT_CONFIG, overrides)


def make_ticket(gov, *, job="j", model=MODEL_OUT, text="x", max_tokens=None, now=None, lane="normal"):
    return gov.make_ticket(
        job=job,
        lane=lane,
        model=model,
        length=len(text),
        blocks=prefix_blocks(text),
        max_tokens=max_tokens,
        now=gov.clock() if now is None else now,
    )


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


def post_raw_status(port: int, body: dict) -> tuple[int, str] | str:
    """Return (status, body), or the exception text when the proxy cut the response short."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    connection.request("POST", "/v1/chat/completions", body=json.dumps(body), headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    try:
        data = response.read().decode()
    except http.client.HTTPException as exc:
        return f"cut: {type(exc).__name__}"
    finally:
        connection.close()
    return response.status, data


async def wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.02)


class FakeMorph:
    """In-process upstream. Each script item is consumed by one request, in order.

    Item keys: status+body (a 429), hold (an Event the response waits for), usage (prompt, completion),
    close_now (close before any status line), truncate ("chunked" or "length": cut the body short).
    """

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
            if item.get("close_now"):
                return
            if item.get("truncate") == "length":
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 500\r\n"
                    b"Connection: close\r\n\r\n{\"usage\""
                )
                await writer.drain()
                return
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
            for index, event in enumerate(events):
                text = event if isinstance(event, str) else json.dumps(event)
                data = f"data: {text}\n\n".encode()
                writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                await writer.drain()
                if item.get("truncate") == "chunked" and index == 0:
                    return
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

    def events(self) -> list[str]:
        return [event for event, _ in self.logs]

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

    def total_used(self) -> int:
        return self.gov.budgets["total"].used(self.gov.clock(), self.window)


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
        self.assertEqual(self.total_used(), 150)
        self.assertEqual(self.gov.budgets["output"].used(self.gov.clock(), self.window), 50)

    async def test_status_endpoint_reports_budgets(self) -> None:
        status, text = await asyncio.to_thread(get_json, self.port, "/agentq/status")
        self.assertEqual(status, 200)
        body = json.loads(text)
        self.assertEqual(body["budgets"]["output"]["limit"], 70_000)
        self.assertEqual(body["budgets"]["uncached"]["limit"], 24_290)
        self.assertEqual(set(body["budgets"]["total"]["queued"]), {"urgent", "normal", "backlog"})
        self.assertEqual(body["errors"], {})

    async def test_429_is_retried_and_never_reaches_client(self) -> None:
        self.fake.script = [
            {"status": 429, "body": '{"error":"rate_limited","message":"Limit: 70000 output tokens/minute."}'}
        ]
        status, text = await self.post(self.chat(MODEL_OUT), job="job-r")
        self.assertEqual(status, 200)
        self.assertIn("data: [DONE]", text)
        self.assertEqual(len(self.fake.requests), 2)
        self.assertIn("retry", self.events())

    async def test_429_body_is_logged_as_one_truncated_line(self) -> None:
        self.fake.script = [{"status": 429, "body": 'Limit: 70000 output tokens/minute.\n"quoted" ' + "x" * 500}]
        await self.post(self.chat(MODEL_OUT), job="job-429")
        logged = [fields for event, fields in self.logs if event == "upstream_429"]
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["job"], "job-429")
        self.assertEqual(logged[0]["attempt"], 1)
        self.assertTrue(logged[0]["body"].startswith("Limit: 70000 output tokens/minute. "))
        self.assertLessEqual(len(logged[0]["body"]), 300)
        self.assertNotIn("\n", logged[0]["body"])
        self.assertNotIn('"', logged[0]["body"])


class LearningTest(ProxyCase):
    overrides = {"start_target": 0.95}

    async def test_target_shrinks_and_stated_limit_is_adopted(self) -> None:
        self.fake.script = [{"status": 429, "body": "Limit: 60000 output tokens/minute."}]
        status, _ = await self.post(self.chat(MODEL_OUT), job="job-t")
        self.assertEqual(status, 200)
        output = self.gov.budgets["output"]
        self.assertEqual(output.limit, 60_000)
        self.assertLess(output.target, 0.95)
        self.assertAlmostEqual(output.target, 0.95 * 0.97, delta=0.01)
        self.assertIn("limit_adopted", self.events())
        await self.proxy.flush_state()
        state = json.loads((self.home / "proxy_state.json").read_text())
        self.assertEqual(state["budgets"]["output"]["limit"], 60_000)

    async def test_key_never_reaches_logs_state_or_status(self) -> None:
        self.fake.script = [{"status": 429, "body": "Limit: 60000 output tokens/minute."}]
        await self.post(self.chat(MODEL_OUT), job="job-k")
        await self.proxy.flush_state()
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
        _, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(head + payload)
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.close()
        await asyncio.sleep(0.3)
        self.assertIn("client_gone", self.events())

        after = asyncio.create_task(self.post(self.chat(MODEL_OUT, 800), job="after"))
        await asyncio.sleep(0.1)
        hold.set()
        results = await asyncio.gather(blocker, after)
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual([r["job"] for r in self.fake.requests], ["blocker", "after"])


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


class TruncationTest(ProxyCase):
    async def test_truncated_chunked_stream_is_cut_not_completed(self) -> None:
        self.fake.script = [{"truncate": "chunked"}]
        outcome = await asyncio.to_thread(post_raw_status, self.port, self.chat(MODEL_OUT))
        self.assertEqual(outcome, "cut: IncompleteRead")
        self.assertEqual(self.total_used(), 0)
        self.assertEqual(self.gov.errors, {"truncated": 1})
        self.assertIn("upstream_failed", self.events())

    async def test_length_delimited_body_cut_short_is_not_completed(self) -> None:
        self.fake.script = [{"truncate": "length"}]
        outcome = await asyncio.to_thread(post_raw_status, self.port, self.chat(MODEL_OUT, stream=False))
        self.assertTrue(isinstance(outcome, str) and outcome.startswith("cut"), outcome)
        self.assertEqual(self.total_used(), 0)
        self.assertEqual(self.gov.errors, {"truncated": 1})


class UpstreamFailureTest(ProxyCase):
    async def test_upstream_closes_before_status_line_releases_charge(self) -> None:
        self.fake.script = [{"close_now": True}]
        status, text = await self.post(self.chat(MODEL_OUT), job="early-close")
        self.assertEqual(status, 502)
        self.assertEqual(self.total_used(), 0)
        self.assertEqual(self.gov.errors, {"bad_status_line": 1})


class IdleTimeoutTest(ProxyCase):
    overrides = {"upstream_idle_s": 0.3}

    async def test_idle_upstream_returns_504_and_releases_charge(self) -> None:
        hold = asyncio.Event()
        self.fake.script = [{"hold": hold}]
        try:
            status, text = await self.post(self.chat(MODEL_OUT), job="silent")
        finally:
            hold.set()
        self.assertEqual(status, 504)
        self.assertEqual(self.total_used(), 0)
        self.assertEqual(self.gov.errors, {"timeout": 1})


class ConnectTimeoutTest(ProxyCase):
    overrides = {"connect_timeout_s": 0.2}

    async def test_connect_timeout_returns_504_and_releases_charge(self) -> None:
        async def never_connects(*args, **kwargs):
            await asyncio.sleep(30)

        with mock.patch("agentq.proxy.asyncio.open_connection", new=never_connects):
            status, _ = await self.post(self.chat(MODEL_OUT), job="stuck-connect")
        self.assertEqual(status, 504)
        self.assertEqual(self.total_used(), 0)
        self.assertEqual(self.gov.errors, {"timeout": 1})


class AdmittedThenGoneTest(unittest.IsolatedAsyncioTestCase):
    async def test_client_closed_at_admission_releases_charge(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        events: list[str] = []
        gov = Governor(Path(tmp.name), config_with(), log=lambda event, **fields: events.append(event))
        proxy = Proxy(gov, "http://127.0.0.1:9/v1", KEY)
        ticket = make_ticket(gov, job="gone", max_tokens=800)
        ticket.future = asyncio.get_running_loop().create_future()
        gov.enqueue(ticket)
        gov.pump()
        self.assertTrue(ticket.holds)
        self.assertEqual(gov.budgets["total"].used(gov.clock(), gov.window), ticket.estimates["total"])

        reader = asyncio.StreamReader()
        reader.feed_eof()
        await proxy._serve(ticket, reader, Client(None), "POST", "/v1/chat/completions", {}, b"{}", False)

        self.assertEqual(gov.budgets["total"].used(gov.clock(), gov.window), 0)
        self.assertEqual(ticket.holds, [])
        self.assertIn("client_gone", events)


class BackoffTest(unittest.TestCase):
    def test_backing_off_head_does_not_block_its_lane(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gov = Governor(Path(tmp.name), config_with(), log=quiet)
        backing_off = make_ticket(gov, job="backing-off", max_tokens=8)
        backing_off.not_before = gov.clock() + 100.0
        ready = make_ticket(gov, job="ready", max_tokens=8)
        gov.enqueue(backing_off)
        gov.enqueue(ready)
        gov.pump()
        self.assertIsNotNone(ready.holds)
        self.assertTrue(ready.holds)
        self.assertEqual(list(gov.queues["normal"]), [backing_off])


class PerBudgetQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.gov = Governor(Path(tmp.name), config_with(start_target=0.95), log=quiet)

    def occupy(self, budget: str, amount: int, lane: str = "normal") -> None:
        self.gov.budgets[budget].entries.append(Entry(at=self.gov.clock(), amount=amount, lane=lane))

    def queue(self, *tickets) -> None:
        for ticket in tickets:
            self.gov.enqueue(ticket)

    def test_blocked_kimi_does_not_stop_deepseek_behind_it(self) -> None:
        self.occupy("uncached", 23_000)
        kimi = make_ticket(self.gov, job="kimi-a", model=MODEL_KIMI, text="A" * 4096)
        deepseek = make_ticket(self.gov, job="ds-a", model=MODEL_OUT, text="x", max_tokens=8)
        self.queue(kimi, deepseek)
        self.gov.pump()
        self.assertTrue(deepseek.holds)
        self.assertFalse(kimi.holds)
        self.assertEqual(list(self.gov.queues["normal"]), [kimi])
        self.assertTrue(self.gov.budgets["uncached"].saturated)

    def test_urgent_guard_block_does_not_hold_normal_on_the_same_budget(self) -> None:
        self.gov.budgets["output"].limit = 1000
        self.gov.budgets["output"].target = 1.0
        self.occupy("output", 500, lane="urgent")
        urgent = make_ticket(self.gov, job="urgent-next", model=MODEL_OUT, text="x", max_tokens=100, lane="urgent")
        normal = make_ticket(self.gov, job="normal-x", model=MODEL_OUT, text="x", max_tokens=100)
        self.queue(urgent, normal)
        self.gov.pump()
        self.assertFalse(urgent.holds)
        self.assertTrue(normal.holds)
        self.assertEqual(list(self.gov.queues["urgent"]), [urgent])

    def test_second_kimi_waits_behind_blocked_kimi_even_if_it_fits(self) -> None:
        self.occupy("uncached", 23_000)
        big = make_ticket(self.gov, job="kimi-big", model=MODEL_KIMI, text="A" * 4096)
        small = make_ticket(self.gov, job="kimi-small", model=MODEL_KIMI, text="x" * 40)
        self.queue(big, small)
        self.gov.pump()
        self.assertFalse(small.holds)
        self.assertEqual(list(self.gov.queues["normal"]), [big, small])

    def test_urgent_blocked_on_total_keeps_normal_needing_total_queued(self) -> None:
        self.occupy("total", 9_499_950)
        urgent = make_ticket(self.gov, job="urgent-ds", model=MODEL_OUT, text="x" * 400, max_tokens=8, lane="urgent")
        normal = make_ticket(self.gov, job="normal-ds", model=MODEL_OUT, text="x" * 400, max_tokens=8)
        self.queue(urgent)
        self.queue(normal)
        self.gov.pump()
        self.assertFalse(urgent.holds)
        self.assertFalse(normal.holds)
        self.assertEqual(list(self.gov.queues["urgent"]), [urgent])
        self.assertEqual(list(self.gov.queues["normal"]), [normal])

    def test_backlog_runs_when_waiting_normal_is_blocked_on_another_budget(self) -> None:
        self.occupy("uncached", 23_000)
        waiting = make_ticket(self.gov, job="kimi-n", model=MODEL_KIMI, text="A" * 4096)
        backlog = make_ticket(self.gov, job="ds-b", model=MODEL_OUT, text="x", max_tokens=8, lane="backlog")
        self.queue(waiting)
        self.queue(backlog)
        self.gov.pump()
        self.assertTrue(backlog.holds)
        self.assertEqual(list(self.gov.queues["normal"]), [waiting])

    def test_backlog_held_when_it_shares_the_blocked_budget(self) -> None:
        self.occupy("uncached", 23_000)
        waiting = make_ticket(self.gov, job="kimi-n", model=MODEL_KIMI, text="A" * 4096)
        backlog = make_ticket(self.gov, job="kimi-b", model=MODEL_KIMI, text="x" * 40, lane="backlog")
        self.queue(waiting)
        self.queue(backlog)
        self.gov.pump()
        self.assertFalse(backlog.holds)
        self.assertEqual(list(self.gov.queues["backlog"]), [backlog])

    def test_backing_off_ticket_holds_its_budgets_against_backlog(self) -> None:
        waiting = make_ticket(self.gov, job="ds-n", model=MODEL_OUT, text="x", max_tokens=8)
        waiting.not_before = self.gov.clock() + 100.0
        backlog = make_ticket(self.gov, job="ds-b", model=MODEL_OUT, text="x", max_tokens=8, lane="backlog")
        self.queue(waiting)
        self.queue(backlog)
        self.gov.pump()
        self.assertFalse(backlog.holds)
        self.assertEqual(list(self.gov.queues["backlog"]), [backlog])

    def test_body_snippet_is_one_line_and_capped(self) -> None:
        self.assertEqual(body_snippet(b'ab\n"cd\'', limit=300), "ab  cd ")
        self.assertEqual(len(body_snippet(b"x" * 500)), 300)


class HygieneTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "agentq"

    async def test_large_body_is_prepared_in_a_worker_thread(self) -> None:
        body = json.dumps(
            {"model": MODEL_OUT, "stream": True, "messages": [{"role": "user", "content": "y" * 300_000}]}
        ).encode()
        with mock.patch("agentq.proxy.asyncio.to_thread", wraps=asyncio.to_thread) as spy:
            payload, out_body, length, blocks = await prepare_body_async(body)
        self.assertTrue(spy.called)
        self.assertEqual(payload["model"], MODEL_OUT)
        self.assertIn(b'"include_usage":true', out_body)
        self.assertEqual(prepare_body(body)[2], length)

    def test_prefix_store_keeps_digests_not_prompt_text(self) -> None:
        gov = Governor(self.home, config_with(), log=quiet)
        text = "z" * 100_000
        make_ticket(gov, job="big", model=MODEL_KIMI, text=text)
        stored_length, stored_blocks = gov.prefixes["big"]
        self.assertEqual(stored_length, len(text))
        self.assertTrue(all(isinstance(block, bytes) and len(block) == 8 for block in stored_blocks))
        self.assertLess(sum(len(block) for block in stored_blocks), len(text) // 100)

    async def test_state_is_written_off_the_request_path(self) -> None:
        gov = Governor(self.home, config_with(), log=quiet)
        proxy = Proxy(gov, "http://127.0.0.1:9/v1", KEY)
        gov.learn_from_429(make_ticket(gov, max_tokens=800), "Limit: 60000 output tokens/minute.")
        self.assertTrue(gov.state_dirty)
        self.assertFalse(gov.state_path.exists())
        await proxy.flush_state()
        self.assertFalse(gov.state_dirty)
        self.assertEqual(json.loads(gov.state_path.read_text())["budgets"]["output"]["limit"], 60_000)


class DrainTest(ProxyCase):
    async def test_drain_lets_in_flight_stream_finish(self) -> None:
        hold = asyncio.Event()
        self.fake.script = [{"hold": hold}]
        in_flight = asyncio.create_task(self.post(self.chat(MODEL_OUT), job="streaming"))
        await wait_for(lambda: len(self.fake.requests) == 1)
        draining = asyncio.create_task(self.proxy.drain(5.0))
        await asyncio.sleep(0.3)
        self.assertFalse(draining.done())
        hold.set()
        started = time.monotonic()
        await draining
        self.assertLess(time.monotonic() - started, 4.0)
        status, _ = await in_flight
        self.assertEqual(status, 200)

    async def test_drain_cancels_stuck_stream_after_timeout(self) -> None:
        hold = asyncio.Event()
        self.fake.script = [{"hold": hold}]
        stuck = asyncio.create_task(self.post(self.chat(MODEL_OUT), job="stuck"))
        await wait_for(lambda: len(self.fake.requests) == 1)
        started = time.monotonic()
        await self.proxy.drain(0.3)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(self.proxy.active, set())
        hold.set()
        with contextlib.suppress(Exception):
            await stuck


class KeyTest(unittest.TestCase):
    def test_override_env_supplies_key(self) -> None:
        self.assertEqual(load_key({"AGENTQ_UPSTREAM_KEY": KEY}), KEY)

    def test_missing_key_exits_78_with_one_clear_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            failed = subprocess.CompletedProcess(args=[], returncode=44, stdout="", stderr="")
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {"AGENTQ_UPSTREAM_KEY": ""}), mock.patch(
                "agentq.proxy.subprocess.run", return_value=failed
            ), contextlib.redirect_stderr(stderr):
                code = main(["--home", tmp, "--port", "0"])
        self.assertEqual(code, EX_CONFIG)
        lines = stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("no Morph key", lines[0])
        self.assertNotIn(KEY, stderr.getvalue())

    def test_plist_throttles_restarts(self) -> None:
        plist = plistlib.loads(PLIST.read_bytes())
        self.assertTrue(plist["KeepAlive"])
        self.assertEqual(plist["ThrottleInterval"], 30)


class EstimateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.gov = Governor(Path(self.tmp.name), config_with(), log=quiet)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_kimi_uncached_estimate_uses_job_prefix(self) -> None:
        first = make_ticket(self.gov, job="job-k", model=MODEL_KIMI, text="A" * 4096)
        self.assertEqual(set(first.estimates), {"total", "uncached"})
        self.assertEqual(first.estimates["uncached"], 1024)
        second = make_ticket(self.gov, job="job-k", model=MODEL_KIMI, text="A" * 4096 + "B" * 400)
        self.assertEqual(second.estimates["uncached"], 100)
        other = make_ticket(self.gov, job="job-other", model=MODEL_KIMI, text="A" * 4096 + "C" * 400)
        self.assertEqual(other.estimates["uncached"], 100)
        identical = make_ticket(self.gov, job="job-other", model=MODEL_KIMI, text="A" * 4096 + "B" * 400)
        self.assertEqual(identical.estimates["uncached"], 0)

    def test_interleaved_anonymous_sessions_extend_their_own_entries(self) -> None:
        system = "S" * (BLOCK_CHARS * 20)
        estimates = []
        for turn in (1, 2, 3):
            for session in ("A", "B"):
                text = system + session * (1000 * turn)
                ticket = make_ticket(self.gov, job="none", model=MODEL_KIMI, text=text)
                estimates.append((session, turn, ticket.estimates["uncached"]))
        suffix = tokens_for_chars(1000)
        self.assertEqual(estimates[0], ("A", 1, tokens_for_chars(len(system) + 1000)))
        for session, turn, estimate in estimates[1:]:
            self.assertEqual(estimate, suffix, (session, turn))
        self.assertEqual(sum(key.startswith("anon:") for key in self.gov.prefixes), 2)

    def test_deepseek_traffic_leaves_prefix_store_empty(self) -> None:
        make_ticket(self.gov, job="job-ds", model=MODEL_OUT, text="D" * 4096)
        make_ticket(self.gov, job="none", model=MODEL_OUT, text="D" * 4096)
        self.assertEqual(len(self.gov.prefixes), 0)

    def test_anonymous_jobs_never_read_the_lane_database(self) -> None:
        make_db(self.gov.home, {"none": "urgent"})
        self.assertEqual(self.gov.lane_for("none"), "normal")
        self.assertEqual(self.gov.lane_for(""), "normal")

    def test_output_estimate_is_capped_by_max_tokens(self) -> None:
        ticket = make_ticket(self.gov, job="j", max_tokens=300)
        self.assertEqual(ticket.estimates["output"], 300)

    def test_stated_limit_parsing(self) -> None:
        self.assertEqual(parse_stated_limit("Limit: 10000000 tokens/minute."), ("total", 10_000_000))
        self.assertEqual(parse_stated_limit("Limit: 70000 output tokens/minute."), ("output", 70_000))
        self.assertEqual(parse_stated_limit("Limit: 24290 uncached input tokens/minute."), ("uncached", 24_290))
        self.assertIsNone(parse_stated_limit('{"code":"service_overloaded"}'))


class TargetCeilingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "agentq"
        self.home.mkdir(parents=True)
        self.now = [1000.0]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def governor(self, config: dict) -> Governor:
        return Governor(self.home, config, clock=lambda: self.now[0], log=quiet)

    def test_tick_raises_past_one_and_stops_at_the_ceiling(self) -> None:
        gov = self.governor(config_with(fast_until=0.9))
        output = gov.budgets["output"]
        output.target = 1.4
        output.saturated = True
        self.now[0] += 60
        gov.tick()
        self.assertAlmostEqual(output.target, 1.42, places=6)
        for _ in range(10):
            self.now[0] += 60
            gov.tick()
        self.assertEqual(output.target, 1.5)

    def test_tick_stops_at_one_when_an_older_config_has_no_ceiling(self) -> None:
        config = config_with(fast_until=0.9)
        del config["target_ceiling"]
        gov = self.governor(config)
        output = gov.budgets["output"]
        output.target = 0.99
        output.saturated = True
        self.now[0] += 60
        gov.tick()
        self.assertEqual(output.target, 1.0)

    def test_429_above_one_cuts_by_target_cut(self) -> None:
        gov = self.governor(config_with())
        gov.budgets["output"].target = 1.4
        gov.learn_from_429(make_ticket(gov, max_tokens=800), "Limit: 60000 output tokens/minute.")
        self.assertAlmostEqual(gov.budgets["output"].target, 1.4 * 0.97, places=6)

    def test_state_file_target_clamps_to_floor_and_ceiling(self) -> None:
        state = {"budgets": {"output": {"target_frac": 1.3}, "total": {"target_frac": 9.0}}}
        (self.home / "proxy_state.json").write_text(json.dumps(state))
        gov = self.governor(config_with())
        self.assertEqual(gov.budgets["output"].target, 1.3)
        self.assertEqual(gov.budgets["total"].target, 1.5)


class StateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "agentq"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_state_persists_across_restart(self) -> None:
        config = config_with(start_target=0.95)
        first = Governor(self.home, config, log=quiet)
        first.learn_from_429(make_ticket(first, max_tokens=800), "Limit: 60000 output tokens/minute.")
        first.save_state()

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
        old = make_ticket(gov, job="old", max_tokens=8, now=0.0)
        fresh = make_ticket(gov, job="fresh", max_tokens=8, now=1000.0)
        gov.enqueue(old)
        gov.enqueue(fresh)
        gov.promote_aged(now=1000.0)
        self.assertEqual(old.lane, "urgent")
        self.assertEqual(fresh.lane, "normal")
        self.assertEqual(list(gov.queues["urgent"]), [old])


class WarmRestartTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "agentq"
        self.clock = [1000.0]
        self.wall = [1_700_000_000.0]

    def governor(self) -> Governor:
        return Governor(
            self.home, config_with(), clock=lambda: self.clock[0], wall=lambda: self.wall[0], log=quiet
        )

    def save_state(self, budgets: dict, age: float) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        state = {"saved_at": self.wall[0] - age, "budgets": budgets}
        (self.home / "proxy_state.json").write_text(json.dumps(state))

    def test_restart_within_window_blocks_until_the_old_window_ages_out(self) -> None:
        self.save_state({"total": {"limit": 10_000_000, "target_frac": 0.95, "window_used": 7_000_000}}, age=10)
        gov = self.governor()
        ticket = make_ticket(gov, model=MODEL_OUT, text="x", max_tokens=8)
        ticket.estimates = {"total": 3_000_000}
        gov.enqueue(ticket)
        gov.pump()
        self.assertFalse(ticket.holds)
        self.clock[0] += 49.0
        self.wall[0] += 49.0
        gov.pump()
        self.assertFalse(ticket.holds)
        self.clock[0] += 1.0
        self.wall[0] += 1.0
        gov.pump()
        self.assertTrue(ticket.holds)

    def test_restart_after_a_full_window_seeds_nothing(self) -> None:
        self.save_state({"total": {"limit": 10_000_000, "target_frac": 0.95, "window_used": 7_000_000}}, age=90)
        gov = self.governor()
        self.assertEqual(gov.budgets["total"].entries, [])
        self.assertEqual(gov.budgets["total"].used(gov.clock(), gov.window), 0)

    def test_old_state_file_without_window_used_loads(self) -> None:
        self.save_state({"total": {"limit": 10_000_000, "target_frac": 0.8}}, age=5)
        gov = self.governor()
        self.assertEqual(gov.budgets["total"].target, 0.8)
        self.assertEqual(gov.budgets["total"].used(gov.clock(), gov.window), 0)

    def test_snapshot_includes_window_used_for_completed_and_in_flight_charges(self) -> None:
        gov = self.governor()
        gov.budgets["total"].entries.append(Entry(at=self.clock[0], amount=1_200, lane="normal", done=True))
        gov.budgets["total"].entries.append(Entry(at=self.clock[0], amount=300, lane="normal"))
        snapshot = gov.snapshot_state()
        self.assertEqual(snapshot["saved_at"], self.wall[0])
        self.assertEqual(snapshot["budgets"]["total"]["window_used"], 1_500)
        self.assertEqual(snapshot["budgets"]["output"]["window_used"], 0)

    async def test_shutdown_writes_state_even_when_nothing_is_dirty(self) -> None:
        gov = self.governor()
        gov.budgets["total"].entries.append(Entry(at=self.clock[0], amount=4_000, lane="normal", done=True))
        gov.state_dirty = False
        proxy = Proxy(gov, "http://127.0.0.1:9/v1", KEY)
        await proxy.close()
        saved = json.loads((self.home / "proxy_state.json").read_text())
        self.assertEqual(saved["budgets"]["total"]["window_used"], 4_000)


class FastRampTest(unittest.TestCase):
    STEP_S = 0.25

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.now = [1000.0]
        self.gov = Governor(Path(tmp.name), config_with(fast_until=0.9), clock=lambda: self.now[0], log=quiet)
        self.total = self.gov.budgets["total"]
        self.total.target = 0.6

    def run_for(self, seconds: float, saturated: bool = True) -> list[float]:
        """Tick the governor every STEP_S, as the maintenance loop does, and return the target seen each tick."""
        seen = []
        for _ in range(round(seconds / self.STEP_S)):
            self.now[0] += self.STEP_S
            self.total.saturated = saturated
            self.gov.tick()
            seen.append(self.total.target)
        return seen

    def test_saturated_budget_reaches_fast_until_in_six_intervals_and_stops_there(self) -> None:
        seen = self.run_for(59.75)
        self.assertAlmostEqual(seen[-1], 0.85, places=6)
        self.run_for(0.25)
        self.assertAlmostEqual(self.total.target, 0.9, places=6)
        self.assertLessEqual(max(seen), 0.9 + 1e-9)

    def test_above_fast_until_the_target_grows_one_step_per_window(self) -> None:
        self.run_for(60.0)
        self.assertAlmostEqual(self.total.target, 0.9, places=6)
        self.run_for(59.75)
        self.assertAlmostEqual(self.total.target, 0.9, places=6)
        self.run_for(0.25)
        self.assertAlmostEqual(self.total.target, 0.92, places=6)

    def test_429_blocks_the_next_fast_step_and_cuts_by_target_cut(self) -> None:
        self.run_for(20.0)
        self.assertAlmostEqual(self.total.target, 0.7, places=6)
        self.gov.learn_from_429(make_ticket(self.gov, max_tokens=8), "")
        self.assertAlmostEqual(self.total.target, 0.7 * 0.97, places=6)
        cut = self.total.target
        self.run_for(9.75)
        self.assertAlmostEqual(self.total.target, cut, places=6)
        self.run_for(30.0)
        self.assertAlmostEqual(self.total.target, cut, places=6)

    def test_unsaturated_budget_does_not_grow(self) -> None:
        seen = self.run_for(60.0, saturated=False)
        self.assertEqual(set(seen), {0.6})

    def test_demand_seen_by_a_pump_between_ticks_counts(self) -> None:
        self.total.entries.append(Entry(at=self.now[0], amount=10_000_000, lane="normal"))
        ticket = make_ticket(self.gov, max_tokens=8)
        self.gov.enqueue(ticket)
        self.gov.pump()
        self.assertTrue(self.total.saturated_since_tick)
        self.total.entries.clear()
        self.gov.pump()
        self.assertFalse(self.total.saturated)
        self.now[0] += 10.0
        self.gov.tick()
        self.assertAlmostEqual(self.total.target, 0.65, places=6)
        self.assertFalse(self.total.saturated_since_tick)


class CutPacingTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.now = [1000.0]
        self.gov = Governor(Path(tmp.name), config_with(), clock=lambda: self.now[0], log=quiet)
        self.total = self.gov.budgets["total"]
        self.total.target = 0.9

    def burst(self, count: int, spacing_s: float) -> None:
        for _ in range(count):
            self.now[0] += spacing_s
            self.gov.learn_from_429(make_ticket(self.gov, max_tokens=8), "service_overloaded")

    def test_a_burst_of_429s_within_a_few_seconds_is_one_cut(self) -> None:
        self.burst(8, 0.4)
        self.assertAlmostEqual(self.total.target, 0.9 * 0.97, places=9)
        self.assertAlmostEqual(self.total.fast_limit, 0.9 * 0.97, places=9)
        self.assertEqual(sum(row["r429"] for row in self.gov.minute_rows.values()), 8)

    def test_a_429_fifteen_seconds_after_the_cut_is_a_second_cut(self) -> None:
        self.burst(8, 0.4)
        self.now[0] += 15.0
        self.gov.learn_from_429(make_ticket(self.gov, max_tokens=8), "service_overloaded")
        self.assertAlmostEqual(self.total.target, 0.9 * 0.97 * 0.97, places=9)
        self.assertAlmostEqual(self.total.fast_limit, 0.9 * 0.97 * 0.97, places=9)

    def test_a_steady_stream_of_429s_still_cuts_once_per_interval(self) -> None:
        self.burst(4, 5.0)
        self.assertAlmostEqual(self.total.target, 0.9 * 0.97 * 0.97, places=9)


class FastLimitTest(unittest.TestCase):
    STEP_S = 0.25

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.now = [1000.0]
        self.gov = Governor(self.home, config_with(fast_until=0.9), clock=lambda: self.now[0], log=quiet)
        self.total = self.gov.budgets["total"]
        self.total.target = 0.6

    def run_for(self, seconds: float) -> list[float]:
        seen = []
        for _ in range(round(seconds / self.STEP_S)):
            self.now[0] += self.STEP_S
            self.total.saturated = True
            self.gov.tick()
            seen.append(self.total.target)
        return seen

    def cut_at_fast_top(self) -> float:
        self.run_for(50.0)
        self.assertAlmostEqual(self.total.target, 0.85, places=6)
        self.gov.learn_from_429(make_ticket(self.gov, max_tokens=8), "service_overloaded")
        self.assertAlmostEqual(self.total.fast_limit, 0.85 * 0.97, places=9)
        return self.total.target

    def test_after_a_cut_fast_steps_stop_at_the_cut_and_slow_steps_continue(self) -> None:
        cut = self.cut_at_fast_top()
        seen = self.run_for(120.0)
        self.assertLessEqual(max(seen), cut + 0.04 + 1e-9)
        self.assertAlmostEqual(self.total.target, cut + 0.04, places=6)

    def test_a_fast_step_never_overshoots_the_cut(self) -> None:
        cut = self.cut_at_fast_top()
        seen = self.run_for(59.75)
        self.assertTrue(all(value <= cut + 1e-9 for value in seen))

    def test_fast_limit_clears_after_thirty_quiet_minutes(self) -> None:
        self.cut_at_fast_top()
        self.run_for(30 * 60 - 0.25)
        self.assertIsNotNone(self.total.fast_limit)
        self.run_for(0.25)
        self.assertIsNone(self.total.fast_limit)

    def test_a_new_429_resets_the_quiet_clock(self) -> None:
        self.cut_at_fast_top()
        self.run_for(29 * 60)
        self.gov.learn_from_429(make_ticket(self.gov, max_tokens=8), "service_overloaded")
        self.run_for(30 * 60 - 0.25)
        self.assertIsNotNone(self.total.fast_limit)

    def test_fast_limit_round_trips_through_the_state_file(self) -> None:
        cut = self.cut_at_fast_top()
        self.gov.save_state()
        restarted = Governor(self.home, config_with(), log=quiet)
        self.assertAlmostEqual(restarted.budgets["total"].fast_limit, cut, places=6)
        self.assertIsNone(restarted.budgets["output"].fast_limit)

    def test_state_without_the_key_loads_as_no_fast_limit(self) -> None:
        self.gov.save_state()
        restarted = Governor(self.home, config_with(), log=quiet)
        self.assertIsNone(restarted.budgets["total"].fast_limit)


class MinuteSeedTest(unittest.TestCase):
    def test_restart_adds_new_requests_to_the_stored_minute_row(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        wall = 1_700_000_030.0
        minute = minute_stamp(wall)
        connection = sqlite3.connect(home / "agentq.db")
        connection.execute(PROXY_MINUTES_DDL)
        connection.execute(
            "INSERT INTO proxy_minutes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (minute, MODEL_OUT, 60, 58, 2, 1000, 0, 500, 900, 0, 0, 0.9),
        )
        connection.commit()
        connection.close()

        gov = Governor(home, config_with(), wall=lambda: wall, log=quiet)
        row = gov.row(MODEL_OUT)
        row["req"] += 5
        row["output"] += 100
        row["ok"] += 5
        write_minute_rows(home / "agentq.db", gov.take_minute_rows())

        connection = sqlite3.connect(home / "agentq.db")
        stored = connection.execute(
            "SELECT req, ok, r429, input, output FROM proxy_minutes WHERE minute = ? AND model = ?",
            (minute, MODEL_OUT),
        ).fetchone()
        connection.close()
        self.assertEqual(stored, (65, 63, 2, 1000, 600))

    def test_a_minute_with_no_stored_row_starts_from_zero(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gov = Governor(Path(tmp.name), config_with(), log=quiet)
        row = gov.row(MODEL_OUT)
        self.assertEqual(row["req"], 0)
        self.assertEqual(row["waits"], [])


def job_table(home: Path) -> dict:
    connection = sqlite3.connect(home / "agentq.db")
    try:
        found = connection.execute(
            "SELECT job, model, first_ts, last_ts, req, ok, r429, input, cache_read, output, "
            "wait_ms_total, upstream_ms_total FROM proxy_jobs"
        ).fetchall()
    except sqlite3.OperationalError:
        found = []
    finally:
        connection.close()
    names = ("job", "model", "first_ts", "last_ts", "req", "ok", "r429", "input", "cache_read", "output",
             "wait_ms_total", "upstream_ms_total")
    return {(row[0], row[1]): dict(zip(names, row)) for row in found}


class PerRunAccountingTest(ProxyCase):
    async def test_admitted_ok_and_429_are_counted_per_run(self) -> None:
        self.fake.script = [{"status": 429, "body": '{"message":"Limit: 70000 output tokens/minute."}'}]
        status, _ = await self.post(self.chat(MODEL_OUT), job="run-a")
        self.assertEqual(status, 200)
        await self.proxy.flush()
        row = job_table(self.home)[("run-a", MODEL_OUT)]
        self.assertEqual(row["req"], 2)
        self.assertEqual(row["ok"], 1)
        self.assertEqual(row["r429"], 1)
        self.assertEqual((row["input"], row["cache_read"], row["output"]), (100, 0, 50))
        self.assertGreaterEqual(row["wait_ms_total"], 0)
        self.assertGreaterEqual(row["upstream_ms_total"], 0)
        self.assertLessEqual(row["first_ts"], row["last_ts"])

    async def test_anonymous_requests_are_keyed_none(self) -> None:
        await self.post(self.chat(MODEL_OUT), job="")
        await self.proxy.flush()
        self.assertIn(("none", MODEL_OUT), job_table(self.home))

    async def test_repeated_flushes_add_and_a_second_process_adds(self) -> None:
        await self.post(self.chat(MODEL_OUT), job="run-b")
        await self.proxy.flush()
        await self.post(self.chat(MODEL_OUT), job="run-b")
        await self.proxy.flush()
        await self.proxy.flush()
        self.assertEqual(job_table(self.home)[("run-b", MODEL_OUT)]["req"], 2)

        second = Governor(self.home, self.config, log=self.capture)
        ticket = make_ticket(second, job="run-b", model=MODEL_OUT)
        second._admit(ticket, second.clock())
        second.finish_ok(ticket, {"prompt_tokens": 10, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 4}})
        await Proxy(second, self.proxy.upstream_url, KEY).flush()
        row = job_table(self.home)[("run-b", MODEL_OUT)]
        self.assertEqual((row["req"], row["ok"], row["output"], row["cache_read"]), (3, 3, 130, 4))

    async def test_a_failed_write_keeps_the_deltas_for_the_next_flush(self) -> None:
        await self.post(self.chat(MODEL_OUT), job="run-c")
        with mock.patch("agentq.proxy.write_job_rows", side_effect=sqlite3.OperationalError("locked")):
            await self.proxy.flush()
        self.assertIn("flush_error", self.events())
        self.assertNotIn(("run-c", MODEL_OUT), job_table(self.home))
        await self.proxy.flush()
        self.assertEqual(job_table(self.home)[("run-c", MODEL_OUT)]["req"], 1)

    async def test_restart_does_not_replay_flushed_counts(self) -> None:
        await self.post(self.chat(MODEL_OUT), job="run-d")
        await self.proxy.flush()
        restarted = Governor(self.home, self.config, log=self.capture)
        await Proxy(restarted, self.proxy.upstream_url, KEY).flush()
        self.assertEqual(job_table(self.home)[("run-d", MODEL_OUT)]["req"], 1)


class JobTimingTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.clock = [0.0]
        self.gov = Governor(self.home, config_with(), clock=lambda: self.clock[0], wall=lambda: 1_700_000_000.0, log=quiet)

    def test_wait_is_measured_from_the_last_enqueue_to_admission(self) -> None:
        ticket = make_ticket(self.gov, job="timed", now=0.0)
        self.clock[0] = 2.5
        self.gov._admit(ticket, 2.5)
        counters = self.gov.job_rows[("timed", MODEL_OUT)]
        self.assertEqual(counters["req"], 1)
        self.assertEqual(counters["wait_ms"], 2500)

    def test_upstream_time_is_summed_per_attempt(self) -> None:
        ticket = make_ticket(self.gov, job="timed", now=0.0)
        self.gov.note_upstream(ticket, 1.25)
        self.gov.note_upstream(ticket, 0.75)
        self.assertEqual(self.gov.job_rows[("timed", MODEL_OUT)]["upstream_ms"], 2000)

    def test_flush_rows_carry_the_counts_and_settle_clears_them(self) -> None:
        ticket = make_ticket(self.gov, job="job-s", now=0.0)
        self.gov._admit(ticket, 0.0)
        rows, snapshot = self.gov.take_job_rows()
        self.assertEqual(rows[0][:2], ("job-s", MODEL_OUT))
        self.gov.settle_job_rows(snapshot)
        self.assertEqual(self.gov.job_rows, {})

    def test_rows_recorded_during_a_write_survive_the_settle(self) -> None:
        ticket = make_ticket(self.gov, job="job-w", now=0.0)
        self.gov._admit(ticket, 0.0)
        _, snapshot = self.gov.take_job_rows()
        self.gov._admit(ticket, 1.0)
        self.gov.settle_job_rows(snapshot)
        self.assertEqual(self.gov.job_rows[("job-w", MODEL_OUT)]["req"], 1)


class MaxTokensCapTest(unittest.TestCase):
    CAPS = {"morph-dsv41flash": 8000}

    def body(self, model: str, **fields) -> bytes:
        payload = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
        payload.update(fields)
        return json.dumps(payload).encode()

    def test_max_tokens_above_the_cap_is_lowered_for_a_matching_prefix(self) -> None:
        payload, out, _, _ = prepare_body(self.body("morph-dsv41flash-1017", max_tokens=74000), self.CAPS)
        self.assertEqual(json.loads(out)["max_tokens"], 8000)
        self.assertEqual(payload["max_tokens"], 8000)

    def test_max_completion_tokens_is_capped_too(self) -> None:
        _, out, _, _ = prepare_body(self.body("morph-dsv41flash-1017", max_completion_tokens=9000), self.CAPS)
        self.assertEqual(json.loads(out)["max_completion_tokens"], 8000)

    def test_a_value_at_or_below_the_cap_and_other_models_pass_through_unchanged(self) -> None:
        below = self.body("morph-dsv41flash-1017", max_tokens=8000)
        self.assertIs(prepare_body(below, self.CAPS)[1], below)
        other = self.body("morph-kimik31", max_tokens=74000)
        self.assertIs(prepare_body(other, self.CAPS)[1], other)
        self.assertIs(prepare_body(other)[1], other)

    def test_cap_keeps_include_usage_on_streams(self) -> None:
        raw = self.body("morph-dsv41flash-1017", max_tokens=74000, stream=True)
        _, out, _, _ = prepare_body(raw, self.CAPS)
        decoded = json.loads(out)
        self.assertEqual(decoded["max_tokens"], 8000)
        self.assertTrue(decoded["stream_options"]["include_usage"])

    def test_large_bodies_take_the_threaded_path_with_the_cap(self) -> None:
        filler = "x" * (LARGE_BODY + 10)
        raw = self.body("morph-dsv41flash-1017", max_tokens=74000, filler=filler)
        payload, out, _, _ = asyncio.run(prepare_body_async(raw, self.CAPS))
        self.assertEqual(json.loads(out)["max_tokens"], 8000)
        self.assertEqual(payload["max_tokens"], 8000)

    def test_default_config_caps_nothing(self) -> None:
        self.assertEqual(DEFAULT_CONFIG["max_tokens_cap"], {})


class MaxTokensCapEndToEndTest(ProxyCase):
    overrides = {"max_tokens_cap": {MODEL_OUT: 100}}

    async def test_the_forwarded_request_carries_the_capped_value(self) -> None:
        await self.post(self.chat(MODEL_OUT, max_tokens=74000), job="cap-job")
        self.assertEqual(self.fake.requests[0]["payload"]["max_tokens"], 100)


if __name__ == "__main__":
    unittest.main()
