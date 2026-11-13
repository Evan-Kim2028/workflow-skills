import asyncio
import contextlib
import http.client
import io
import json
import os
import plistlib
import shutil
import re
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentq.proxy import (
    BLOCK_CHARS,
    DEFAULT_CONFIG,
    EX_CONFIG,
    PREFIX_STORE_JOBS,
    PROXY_MINUTES_DDL,
    ROW_WAIT_SAMPLES,
    Client,
    Entry,
    Governor,
    LOG_PATH,
    RotatingLog,
    INFLIGHT,
    LANE_CACHE_S,
    ARMED,
    SUSPENDED,
    UNARMED,
    InflightLimiter,
    Proxy,
    body_snippet,
    default_log,
    family_of,
    file_log,
    load_key,
    main,
    merge_config,
    minute_stamp,
    parse_stated_limit,
    prefix_blocks,
    LARGE_BODY,
    MINUTE_ROW_COLUMNS,
    prepare_body,
    prepare_body_async,
    read_calib_counts,
    read_minute_counts,
    read_request,
    seconds_to_next_minute,
    tokens_for_chars,
    write_calib_rows,
    FINAL_RESERVE_S,
    UpstreamPool,
    read_minute_counts,
    read_only_uri,
    write_calib_rows,
    write_inflight_minute_rows,
    write_minute_rows,
)

KEY = "SECRET-KEY-123"
USAGE_5000_PROMPT_1000_CACHED_100_OUT = {
    "prompt_tokens": 5000,
    "completion_tokens": 100,
    "prompt_tokens_details": {"cached_tokens": 1000},
}
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
        self.assertEqual(body["budgets"]["uncached_ds"]["limit"], 400_000)
        self.assertEqual(body["budgets"]["uncached_ds"]["limit_type"], "uncached")
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
        if self.proxy.flush_task is not None:
            await self.proxy.flush_task
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


class PerFamilyBlockTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.gov = Governor(Path(tmp.name), config_with(start_target=0.95), log=quiet)

    def occupy(self, budget: str, amount: int) -> None:
        self.gov.budgets[budget].entries.append(Entry(at=self.gov.clock(), amount=amount, lane="normal"))

    def queue(self, *tickets) -> None:
        for ticket in tickets:
            self.gov.enqueue(ticket)

    def budget_status(self, name: str) -> dict:
        return self.gov.status()["budgets"][name]

    def test_family_of_maps_the_two_gated_families_and_leaves_the_rest_as_other(self) -> None:
        self.assertEqual(family_of(MODEL_OUT), "morph-dsv41flash")
        self.assertEqual(family_of(MODEL_KIMI), "morph-kimik31")
        self.assertEqual(family_of("morph-other-model"), "other")

    def test_kimi_blocked_on_its_own_uncached_limit_counts_under_uncached_not_total(self) -> None:
        self.occupy("uncached", 23_000)
        self.queue(*(make_ticket(self.gov, job=f"kimi-{i}", model=MODEL_KIMI, text="A" * 4096) for i in range(20)))
        self.gov.pump()
        total = self.budget_status("total")
        self.assertEqual(total["queued_by_family"]["morph-kimik31"], 20)
        self.assertEqual(total["blocked_by_family"]["morph-kimik31"], 0)
        self.assertEqual(self.budget_status("uncached")["blocked_by_family"]["morph-kimik31"], 20)

    def test_deepseek_blocked_by_total_counts_under_total_and_nothing_else(self) -> None:
        self.occupy("total", int(self.gov.budgets["total"].cap()) - 1)
        self.queue(*(make_ticket(self.gov, job=f"ds-{i}", model=MODEL_OUT, text="x", max_tokens=8) for i in range(10)))
        self.gov.pump()
        self.assertEqual(self.budget_status("total")["blocked_by_family"]["morph-dsv41flash"], 10)
        self.assertEqual(self.budget_status("output")["blocked_by_family"]["morph-dsv41flash"], 0)
        self.assertEqual(self.budget_status("uncached_ds")["blocked_by_family"]["morph-dsv41flash"], 0)

    def test_deepseek_blocked_by_uncached_ds_counts_under_uncached_ds_not_total(self) -> None:
        self.occupy("uncached_ds", int(self.gov.budgets["uncached_ds"].cap()))
        self.queue(*(make_ticket(self.gov, job=f"ds-{i}", model=MODEL_OUT, text="x" * 400, max_tokens=8) for i in range(4)))
        self.gov.pump()
        self.assertEqual(self.budget_status("uncached_ds")["blocked_by_family"]["morph-dsv41flash"], 4)
        self.assertEqual(self.budget_status("total")["blocked_by_family"]["morph-dsv41flash"], 0)
        self.assertEqual(self.budget_status("total")["queued_by_family"]["morph-dsv41flash"], 4)

    def test_queued_counts_a_kimi_ticket_on_total_but_blocked_counts_it_under_uncached(self) -> None:
        self.occupy("uncached", 23_000)
        kimi = make_ticket(self.gov, job="kimi-a", model=MODEL_KIMI, text="A" * 4096)
        deepseek = make_ticket(self.gov, job="ds-a", model=MODEL_OUT, text="x", max_tokens=8)
        self.queue(kimi, deepseek)
        self.gov.pump()
        self.assertTrue(deepseek.holds)
        total = self.budget_status("total")
        self.assertEqual(total["queued_by_family"], {"morph-dsv41flash": 0, "morph-kimik31": 1, "other": 0})
        self.assertEqual(total["blocked_by_family"], {"morph-dsv41flash": 0, "morph-kimik31": 0, "other": 0})
        self.assertEqual(self.budget_status("uncached")["blocked_by_family"]["morph-kimik31"], 1)

    def test_admitted_ticket_leaves_both_counts(self) -> None:
        ticket = make_ticket(self.gov, job="ds-ok", model=MODEL_OUT, text="x", max_tokens=8)
        self.queue(ticket)
        self.gov.pump()
        self.assertTrue(ticket.holds)
        self.assertEqual(self.budget_status("total")["queued_by_family"]["morph-dsv41flash"], 0)
        self.assertEqual(self.budget_status("total")["blocked_by_family"]["morph-dsv41flash"], 0)

    def test_backing_off_ticket_counts_under_the_budget_its_429_named(self) -> None:
        ticket = make_ticket(self.gov, job="ds-429", model=MODEL_OUT, text="x", max_tokens=8)
        self.queue(ticket)
        self.gov.learn_from_429(ticket, "Limit: 70000 output tokens/minute.")
        ticket.not_before = self.gov.clock() + 100.0
        self.gov.pump()
        self.assertEqual(ticket.backoff_by, "output")
        self.assertEqual(self.budget_status("output")["blocked_by_family"]["morph-dsv41flash"], 1)
        self.assertEqual(self.budget_status("total")["blocked_by_family"]["morph-dsv41flash"], 0)

    def test_backing_off_ticket_whose_429_names_no_budget_is_not_counted_as_blocked(self) -> None:
        ticket = make_ticket(self.gov, job="ds-429", model=MODEL_OUT, text="x", max_tokens=8)
        self.queue(ticket)
        self.gov.learn_from_429(ticket, "upstream hiccup")
        ticket.not_before = self.gov.clock() + 100.0
        self.gov.pump()
        self.assertIsNone(ticket.backoff_by)
        self.assertEqual(self.budget_status("total")["queued_by_family"]["morph-dsv41flash"], 1)
        self.assertEqual(self.budget_status("total")["blocked_by_family"]["morph-dsv41flash"], 0)


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

    def test_plist_restarts_quickly_at_interactive_priority(self) -> None:
        plist = plistlib.loads(PLIST.read_bytes())
        self.assertTrue(plist["KeepAlive"])
        self.assertEqual(plist["ThrottleInterval"], 5)
        self.assertEqual(plist["ProcessType"], "Interactive")
        self.assertEqual(plist["ExitTimeOut"], 40)


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

    def test_deepseek_traffic_shares_the_prefix_store_and_gets_an_uncached_estimate(self) -> None:
        first = make_ticket(self.gov, job="job-ds", model=MODEL_OUT, text="D" * 4096)
        self.assertEqual(set(first.estimates), {"total", "output", "uncached_ds"})
        self.assertEqual(first.estimates["uncached_ds"], 1024)
        second = make_ticket(self.gov, job="job-ds", model=MODEL_OUT, text="D" * 4096 + "E" * 400)
        self.assertEqual(second.estimates["uncached_ds"], 100)
        self.assertEqual(set(self.gov.prefixes), {"job-ds"})

    def test_prefix_store_holds_256_jobs_and_a_lookup_stays_cheap(self) -> None:
        shared = "S" * (BLOCK_CHARS * 200)
        started = time.perf_counter()
        for index in range(PREFIX_STORE_JOBS + 44):
            make_ticket(self.gov, job=f"job-{index}", model=MODEL_OUT, text=shared + str(index) * 300)
        per_lookup = (time.perf_counter() - started) / (PREFIX_STORE_JOBS + 44)
        self.assertEqual(len(self.gov.prefixes), PREFIX_STORE_JOBS)
        self.assertLess(per_lookup, 0.02, f"{per_lookup * 1000:.2f} ms per lookup with 256 stored entries")

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


class PerModelUncachedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "agentq"
        self.events: list[tuple[str, dict]] = []
        self.gov = Governor(self.home, config_with(), log=lambda event, **fields: self.events.append((event, fields)))

    def admit(self, ticket) -> None:
        self.gov.enqueue(ticket)
        self.gov.pump()
        self.assertTrue(ticket.holds)

    def test_alternating_stated_limits_leave_each_model_budget_at_its_own_limit(self) -> None:
        deepseek = make_ticket(self.gov, job="ds", model=MODEL_OUT, text="x", max_tokens=8)
        kimi = make_ticket(self.gov, job="kimi", model=MODEL_KIMI, text="x", max_tokens=8)
        for _ in range(3):
            self.gov.learn_from_429(deepseek, "Limit: 400000 uncached input tokens/minute.")
            self.gov.learn_from_429(kimi, "Limit: 24290 uncached input tokens/minute.")
        self.assertEqual(self.gov.budgets["uncached_ds"].limit, 400_000)
        self.assertEqual(self.gov.budgets["uncached"].limit, 24_290)
        self.assertNotIn("limit_adopted", [event for event, _ in self.events])

    def test_deepseek_body_never_rewrites_kimi_and_kimi_body_never_rewrites_deepseek(self) -> None:
        deepseek = make_ticket(self.gov, job="ds", model=MODEL_OUT, text="x", max_tokens=8)
        kimi = make_ticket(self.gov, job="kimi", model=MODEL_KIMI, text="x", max_tokens=8)
        self.gov.learn_from_429(deepseek, "Limit: 380000 uncached input tokens/minute.")
        self.assertEqual(self.gov.budgets["uncached"].limit, 24_290)
        self.gov.learn_from_429(kimi, "Limit: 22000 uncached input tokens/minute.")
        self.assertEqual(self.gov.budgets["uncached"].limit, 22_000)
        self.assertEqual(self.gov.budgets["uncached_ds"].limit, 380_000)
        adopted = [(fields["budget"], fields["new"]) for event, fields in self.events if event == "limit_adopted"]
        self.assertEqual(adopted, [("uncached_ds", 380_000), ("uncached", 22_000)])

    def test_stated_limit_type_with_no_budget_for_the_model_is_not_adopted(self) -> None:
        kimi = make_ticket(self.gov, job="kimi", model=MODEL_KIMI, text="x", max_tokens=8)
        self.gov.learn_from_429(kimi, "Limit: 60000 output tokens/minute.")
        self.assertEqual(self.gov.budgets["output"].limit, 70_000)
        self.assertLess(self.gov.budgets["uncached"].target, 0.98)
        calib = [fields for event, fields in self.events if event == "calib"]
        self.assertEqual(calib[0]["limit_type"], "output")
        self.assertIsNone(calib[0]["budget"])

    def test_deepseek_ticket_is_charged_to_its_uncached_budget_total_and_output(self) -> None:
        ticket = make_ticket(self.gov, job="ds", model=MODEL_OUT, text="x" * 4096, max_tokens=8)
        self.assertEqual(set(ticket.estimates), {"total", "output", "uncached_ds"})
        self.admit(ticket)
        self.gov.finish_ok(ticket, USAGE_5000_PROMPT_1000_CACHED_100_OUT)
        now = self.gov.clock()
        self.assertEqual(self.gov.budgets["uncached_ds"].used(now, self.gov.window), 4000)
        self.assertEqual(self.gov.budgets["total"].used(now, self.gov.window), 5100)
        self.assertEqual(self.gov.budgets["output"].used(now, self.gov.window), 100)
        self.assertEqual(self.gov.budgets["uncached"].used(now, self.gov.window), 0)

    def test_kimi_ticket_is_charged_to_its_uncached_budget_and_total_only(self) -> None:
        ticket = make_ticket(self.gov, job="kimi", model=MODEL_KIMI, text="x" * 4096, max_tokens=8)
        self.assertEqual(set(ticket.estimates), {"total", "uncached"})
        self.admit(ticket)
        self.gov.finish_ok(ticket, USAGE_5000_PROMPT_1000_CACHED_100_OUT)
        now = self.gov.clock()
        self.assertEqual(self.gov.budgets["uncached"].used(now, self.gov.window), 4000)
        self.assertEqual(self.gov.budgets["total"].used(now, self.gov.window), 5100)
        self.assertEqual(self.gov.budgets["uncached_ds"].used(now, self.gov.window), 0)

    def test_429_names_the_budget_it_applied_to_in_the_calib_log(self) -> None:
        deepseek = make_ticket(self.gov, job="ds", model=MODEL_OUT, text="x", max_tokens=8)
        self.gov.learn_from_429(deepseek, "Limit: 400000 uncached input tokens/minute.")
        calib = [fields for event, fields in self.events if event == "calib"]
        self.assertEqual(calib[0]["budget"], "uncached_ds")
        self.assertEqual(calib[0]["limit_type"], "uncached")

    def test_status_lists_the_new_budget_with_its_limit_type(self) -> None:
        budgets = self.gov.status()["budgets"]
        self.assertEqual(budgets["uncached_ds"]["limit"], 400_000)
        self.assertEqual(budgets["uncached_ds"]["limit_type"], "uncached")
        self.assertEqual(budgets["uncached"]["limit_type"], "uncached")
        self.assertEqual(budgets["total"]["limit_type"], "total")

    def test_new_budget_follows_the_global_window_default_and_per_budget_overrides_apply(self) -> None:
        global_calendar = Governor(self.home, config_with(window_mode="calendar"), log=quiet)
        self.assertEqual(global_calendar.budgets["uncached_ds"].window_mode, "calendar")
        owner = Governor(self.home, config_with(budgets={"total": {"window_mode": "calendar"}}), log=quiet)
        self.assertEqual(owner.budgets["total"].window_mode, "calendar")
        self.assertEqual(owner.budgets["total"].limit, 10_000_000)
        self.assertEqual(owner.budgets["uncached_ds"].window_mode, "rolling")
        self.assertEqual(owner.budgets["uncached_ds"].limit, 400_000)

    def test_unknown_limit_type_is_refused_at_start(self) -> None:
        with self.assertRaises(ValueError):
            Governor(self.home, config_with(budgets={"bogus": {"limit": 1, "limit_type": "tokens_per_day"}}), log=quiet)


class OldStateWithoutDeepSeekUncachedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "agentq"
        self.home.mkdir(parents=True)

    def test_state_file_from_before_uncached_ds_loads_and_keeps_kimi_limit(self) -> None:
        old = {
            "saved_at": time.time(),
            "budgets": {
                "total": {"limit": 10_000_000, "target_frac": 0.9, "window_used": 0},
                "output": {"limit": 70_000, "target_frac": 0.8, "window_used": 0},
                "uncached": {"limit": 23_000, "target_frac": 0.85, "window_used": 0},
            },
        }
        (self.home / "proxy_state.json").write_text(json.dumps(old))
        gov = Governor(self.home, config_with(start_target=0.98), log=quiet)
        self.assertEqual(gov.budgets["uncached"].limit, 23_000)
        self.assertAlmostEqual(gov.budgets["uncached"].target, 0.85)
        self.assertEqual(gov.budgets["uncached_ds"].limit, 400_000)
        self.assertEqual(gov.budgets["uncached_ds"].target, 0.98)
        gov.save_state()
        saved = json.loads((self.home / "proxy_state.json").read_text())["budgets"]
        self.assertEqual(saved["uncached_ds"]["limit"], 400_000)


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
            "INSERT INTO proxy_minutes (minute, model, req, ok, r429, input, cache_read, output, uncached_est, "
            "wait_p50_ms, wait_p95_ms, target) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
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


WALL_MINUTE_START = 1_700_000_040.0


class Clocks:
    """A monotonic clock and a wall clock that move together, so a test can cross a UTC :00."""

    def __init__(self, wall: float) -> None:
        self.mono = 0.0
        self.now_wall = wall

    def clock(self) -> float:
        return self.mono

    def wall(self) -> float:
        return self.now_wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.now_wall += seconds


def timed_governor(home: Path, wall: float, log=quiet, **settings) -> tuple[Governor, Clocks]:
    clocks = Clocks(wall)
    gov = Governor(home, config_with(**settings), clock=clocks.clock, wall=clocks.wall, log=log)
    return gov, clocks


def admit(gov: Governor, ticket) -> None:
    gov.enqueue(ticket)
    gov.pump()
    assert ticket.holds, "ticket was not admitted"


class CalendarWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def test_calendar_used_resets_at_the_next_utc_minute(self) -> None:
        gov, clocks = timed_governor(self.home, WALL_MINUTE_START + 50, window_mode="calendar")
        ticket = make_ticket(gov, text="x" * 400)
        admit(gov, ticket)
        gov.finish_ok(ticket, {"prompt_tokens": 100, "completion_tokens": 50})
        self.assertEqual(gov.budgets["total"].window_mode, "calendar")
        self.assertEqual(gov._used(gov.budgets["total"]), 150)
        clocks.advance(9.5)
        self.assertEqual(gov._used(gov.budgets["total"]), 150)
        clocks.advance(0.5)
        self.assertEqual(gov._used(gov.budgets["total"]), 0)

    def test_rolling_default_keeps_the_charge_for_a_full_window(self) -> None:
        gov, clocks = timed_governor(self.home, WALL_MINUTE_START + 50)
        ticket = make_ticket(gov, text="x" * 400)
        admit(gov, ticket)
        gov.finish_ok(ticket, {"prompt_tokens": 100, "completion_tokens": 50})
        clocks.advance(20)
        self.assertEqual(gov._used(gov.budgets["total"]), 150)
        self.assertEqual(gov.budgets["total"].window_mode, "rolling")

    def test_a_full_calendar_budget_retries_at_the_next_minute(self) -> None:
        gov, clocks = timed_governor(self.home, WALL_MINUTE_START + 50, window_mode="calendar", budgets={"total": {"limit": 1000}})
        first = make_ticket(gov, text="x" * 400)
        admit(gov, first)
        refused = make_ticket(gov, text="x" * 400)
        gov.enqueue(refused)
        gov.pump()
        self.assertEqual(refused.holds, [])
        delay = gov.retry_delay(refused, gov.clock())
        self.assertGreaterEqual(delay, 10.0)
        self.assertLessEqual(delay, 10.0 + 1.5)
        clocks.advance(10)
        gov.pump()
        self.assertTrue(refused.holds, "the refused ticket is admitted once the minute turns over")

    def test_a_rolling_budget_still_waits_for_completed_charges_to_age_out(self) -> None:
        gov, clocks = timed_governor(self.home, WALL_MINUTE_START + 50, budgets={"total": {"limit": 200}})
        first = make_ticket(gov, text="x" * 400)
        admit(gov, first)
        gov.finish_ok(first, {"prompt_tokens": 100, "completion_tokens": 50})
        refused = make_ticket(gov, text="x" * 400)
        self.assertEqual(refused.estimates["total"], 150)
        clocks.advance(10)
        delay = gov.retry_delay(refused, gov.clock())
        self.assertGreaterEqual(delay, 50.0)
        self.assertLessEqual(delay, 50.0 + 2.0)

    def test_seconds_to_next_minute_is_zero_exactly_on_the_minute(self) -> None:
        self.assertEqual(seconds_to_next_minute(WALL_MINUTE_START), 0.0)
        self.assertEqual(seconds_to_next_minute(WALL_MINUTE_START + 50), 10.0)

    def test_budget_overrides_set_window_mode_and_basis_per_budget(self) -> None:
        gov, _ = timed_governor(self.home, WALL_MINUTE_START, budgets={"total": {"window_mode": "calendar"}})
        self.assertEqual(gov.budgets["total"].window_mode, "calendar")
        self.assertEqual(gov.budgets["total"].limit, 10_000_000)
        self.assertEqual(gov.budgets["output"].window_mode, "rolling")
        self.assertEqual(gov.budgets["output"].charge_basis, "billed")

    def test_unknown_modes_are_refused_at_start(self) -> None:
        with self.assertRaises(ValueError):
            timed_governor(self.home, WALL_MINUTE_START, window_mode="hourly")
        with self.assertRaises(ValueError):
            timed_governor(self.home, WALL_MINUTE_START, budgets={"total": {"charge_basis": "guess"}})

    def test_status_reports_window_mode_basis_and_seconds_to_reset(self) -> None:
        gov, _ = timed_governor(self.home, WALL_MINUTE_START + 45, window_mode="calendar")
        budgets = gov.status()["budgets"]
        self.assertEqual(budgets["total"]["window_mode"], "calendar")
        self.assertEqual(budgets["total"]["charge_basis"], "billed")
        self.assertEqual(budgets["total"]["seconds_to_reset"], 15.0)
        self.assertEqual(budgets["output"]["window_mode"], "calendar")
        rolling, _ = timed_governor(self.home, WALL_MINUTE_START + 45)
        self.assertNotIn("seconds_to_reset", rolling.status()["budgets"]["total"])


class ChargeBasisTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def test_prompt_plus_max_keeps_the_reservation_after_completion(self) -> None:
        gov, _ = timed_governor(self.home, WALL_MINUTE_START + 5, budgets={"total": {"charge_basis": "prompt_plus_max"}})
        ticket = make_ticket(gov, text="x" * 400, max_tokens=800)
        self.assertEqual(ticket.estimates["total"], 100 + 800)
        admit(gov, ticket)
        gov.finish_ok(ticket, {"prompt_tokens": 100, "completion_tokens": 50})
        self.assertEqual(gov._used(gov.budgets["total"]), 900)
        self.assertEqual(gov._used(gov.budgets["output"]), 50)

    def test_prompt_plus_max_without_a_requested_max_charges_the_prompt_only(self) -> None:
        gov, _ = timed_governor(self.home, WALL_MINUTE_START + 5, budgets={"total": {"charge_basis": "prompt_plus_max"}})
        ticket = make_ticket(gov, text="x" * 400)
        self.assertEqual(ticket.estimates["total"], 100)

    def test_billed_default_is_replaced_by_real_usage(self) -> None:
        gov, _ = timed_governor(self.home, WALL_MINUTE_START + 5)
        ticket = make_ticket(gov, text="x" * 400, max_tokens=800)
        admit(gov, ticket)
        gov.finish_ok(ticket, {"prompt_tokens": 100, "completion_tokens": 50})
        self.assertEqual(gov._used(gov.budgets["total"]), 150)

    def test_a_released_reservation_is_not_charged(self) -> None:
        gov, _ = timed_governor(self.home, WALL_MINUTE_START + 5, budgets={"total": {"charge_basis": "prompt_plus_max"}})
        ticket = make_ticket(gov, text="x" * 400, max_tokens=800)
        admit(gov, ticket)
        gov.release(ticket)
        self.assertEqual(gov._used(gov.budgets["total"]), 0)


class CalibrationTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.logged: list[tuple[str, dict]] = []

    def capture(self, event: str, **fields) -> None:
        self.logged.append((event, fields))

    def test_admitted_and_billed_counts_accumulate_per_minute_and_family(self) -> None:
        gov, _ = timed_governor(self.home, WALL_MINUTE_START + 17.5, log=self.capture)
        ticket = make_ticket(gov, text="x" * 400, max_tokens=800)
        admit(gov, ticket)
        gov.finish_ok(ticket, {"prompt_tokens": 100, "completion_tokens": 50})
        record = gov.calib[minute_stamp(WALL_MINUTE_START)]
        self.assertEqual(record["overall"], {"req": 1, "prompt_est": 100, "max_tokens": 800, "billed": 150})
        self.assertEqual(record["families"]["morph-dsv41flash"]["billed"], 150)

    def test_a_429_records_the_first_second_in_the_minute_and_logs_calib(self) -> None:
        gov, clocks = timed_governor(self.home, WALL_MINUTE_START + 17.5, log=self.capture)
        ticket = make_ticket(gov, text="x" * 400, max_tokens=800)
        gov.learn_from_429(ticket, "Limit: 10000000 tokens/minute. Please retry.")
        clocks.advance(12)
        gov.learn_from_429(ticket, "Limit: 10000000 tokens/minute.")
        record = gov.calib[minute_stamp(WALL_MINUTE_START)]
        self.assertEqual(record["first_429_s"], 17.5)
        self.assertEqual(record["r429"], 2)
        calls = [fields for event, fields in self.logged if event == "calib"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["budget"], "total")
        self.assertEqual(calls[0]["sec"], 17.5)
        self.assertEqual(calls[1]["first_429_s"], 17.5)

    def test_a_body_naming_no_budget_logs_none(self) -> None:
        gov, _ = timed_governor(self.home, WALL_MINUTE_START + 3, log=self.capture)
        gov.learn_from_429(make_ticket(gov, text="x"), "service_overloaded")
        calls = [fields for event, fields in self.logged if event == "calib"]
        self.assertIsNone(calls[0]["budget"])

    def test_the_minute_row_is_written_and_a_restart_reseeds_it(self) -> None:
        gov, clocks = timed_governor(self.home, WALL_MINUTE_START + 17.5)
        ticket = make_ticket(gov, text="x" * 400, max_tokens=800)
        admit(gov, ticket)
        gov.finish_ok(ticket, {"prompt_tokens": 100, "completion_tokens": 50})
        gov.learn_from_429(ticket, "Limit: 10000000 tokens/minute.")
        write_calib_rows(self.home / "agentq.db", gov.take_calib_rows())
        connection = sqlite3.connect(self.home / "agentq.db")
        stored = connection.execute(
            "SELECT minute, req, prompt_est, max_tokens, billed, first_429_s, r429 FROM proxy_calib"
        ).fetchall()
        connection.close()
        self.assertEqual(stored, [(minute_stamp(WALL_MINUTE_START), 1, 100, 800, 150, 17.5, 1)])

        restarted, _ = timed_governor(self.home, WALL_MINUTE_START + 25)
        seeded = restarted.calib_record(minute_stamp(WALL_MINUTE_START))
        self.assertEqual(seeded["overall"], {"req": 1, "prompt_est": 100, "max_tokens": 800, "billed": 150})
        self.assertEqual(seeded["first_429_s"], 17.5)
        self.assertEqual(seeded["r429"], 1)

    def test_minutes_older_than_three_minutes_are_forgotten(self) -> None:
        gov, clocks = timed_governor(self.home, WALL_MINUTE_START + 1)
        gov.calib_record(minute_stamp(WALL_MINUTE_START))
        clocks.advance(240)
        gov.take_calib_rows()
        self.assertNotIn(minute_stamp(WALL_MINUTE_START), gov.calib)

    def test_read_calib_counts_is_none_without_a_row(self) -> None:
        self.assertIsNone(read_calib_counts(self.home / "missing.db", "2023-11-14T22:14"))


class Calib429EndToEndTest(ProxyCase):
    async def test_a_429_is_logged_as_calib_and_the_minute_row_is_flushed(self) -> None:
        self.fake.script = [{"status": 429, "body": "Limit: 10000000 tokens/minute."}]
        status, _ = await self.post(self.chat(MODEL_OUT), job="calib-job")
        self.assertEqual(status, 200)
        self.assertIn("calib", self.events())
        await self.proxy.flush()
        connection = sqlite3.connect(self.home / "agentq.db")
        rows = connection.execute("SELECT r429, first_429_s FROM proxy_calib").fetchall()
        connection.close()
        self.assertEqual(rows[0][0], 1)
        self.assertGreaterEqual(rows[0][1], 0.0)
        self.assertLess(rows[0][1], 60.0)


if __name__ == "__main__":
    unittest.main()


OLD_MINUTES_DDL = """
CREATE TABLE proxy_minutes (
    minute TEXT NOT NULL, model TEXT NOT NULL,
    req INT, ok INT, r429 INT,
    input INT, cache_read INT, output INT, uncached_est INT,
    wait_p50_ms INT, wait_p95_ms INT,
    target REAL,
    PRIMARY KEY (minute, model)
)
"""
LATENCY_COLUMNS = ("upstream_ms_sum", "upstream_n", "upstream_p50_ms", "upstream_p95_ms", "ttfb_p50_ms", "in_flight_max")
WALL_MINUTE = 1_700_000_040.0


class UpstreamLatencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "agentq"
        self.home.mkdir(parents=True)
        self.now = [1000.0]
        self.wall = [WALL_MINUTE]
        self.gov = self.governor()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def governor(self) -> Governor:
        return Governor(
            self.home,
            config_with(),
            clock=lambda: self.now[0],
            wall=lambda: self.wall[0],
            log=quiet,
        )

    def request(self, gov: Governor, upstream_s: float, ttfb_s: float, model: str = MODEL_OUT) -> None:
        ticket = make_ticket(gov, model=model)
        gov.upstream_started(ticket)
        self.now[0] += upstream_s
        gov.finish_ok(ticket, None, upstream_s=upstream_s, ttfb_s=ttfb_s)
        gov.upstream_finished(ticket)

    def minute_tuple(self, gov: Governor, model: str = MODEL_OUT) -> tuple:
        stamp = minute_stamp(self.wall[0])
        rows = [row for row in gov.take_minute_rows() if row[0] == stamp and row[1] == model]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_latency_sums_and_percentiles_are_kept_per_minute(self) -> None:
        for step in range(1, 11):
            self.request(self.gov, step / 10, step / 100)
        row = self.minute_tuple(self.gov)
        self.assertEqual(len(row), len(MINUTE_ROW_COLUMNS))
        upstream_sum, upstream_n, p50, p95, ttfb50, in_flight = row[12:18]
        self.assertEqual(upstream_sum, 5500)
        self.assertEqual(upstream_n, 10)
        self.assertEqual(p50, 600)
        self.assertEqual(p95, 1000)
        self.assertEqual(ttfb50, 60)
        self.assertEqual(in_flight, 1)

    def test_a_request_that_is_not_successful_adds_no_latency(self) -> None:
        ticket = make_ticket(self.gov)
        self.gov.upstream_started(ticket)
        self.now[0] += 0.5
        self.gov.upstream_finished(ticket)
        self.gov.release(ticket)
        row = self.minute_tuple(self.gov)
        self.assertEqual(row[12:14], (0, 0))
        self.assertIsNone(row[14])
        self.assertIsNone(row[15])

    def test_samples_stop_at_the_row_cap_but_sums_keep_counting(self) -> None:
        for _ in range(ROW_WAIT_SAMPLES + 5):
            self.request(self.gov, 0.2, 0.1)
        row = self.minute_tuple(self.gov)
        self.assertEqual(row[13], ROW_WAIT_SAMPLES + 5)
        self.assertEqual(row[12], (ROW_WAIT_SAMPLES + 5) * 200)
        self.assertEqual(row[14], 200)

    def test_in_flight_max_is_the_peak_seen_in_the_minute(self) -> None:
        first = make_ticket(self.gov, job="a")
        second = make_ticket(self.gov, job="b")
        third = make_ticket(self.gov, job="c")
        for ticket in (first, second, third):
            self.gov.upstream_started(ticket)
        self.gov.upstream_finished(first)
        self.gov.upstream_started(make_ticket(self.gov, job="d"))
        self.assertEqual(self.gov.in_flight, {MODEL_OUT: 3})
        for ticket in (second, third):
            self.gov.upstream_finished(ticket)
        self.assertEqual(self.minute_tuple(self.gov)[17], 3)

    def test_a_new_minute_starts_its_peak_from_the_requests_still_in_flight(self) -> None:
        held = make_ticket(self.gov, job="held")
        self.gov.upstream_started(held)
        self.wall[0] += 60
        self.request(self.gov, 0.1, 0.05)
        self.assertEqual(self.minute_tuple(self.gov)[17], 2)

    def test_seed_carries_the_sums_and_restarts_the_percentiles_empty(self) -> None:
        self.request(self.gov, 1.0, 0.2)
        self.request(self.gov, 2.0, 0.4)
        rows = self.gov.take_minute_rows()
        write_minute_rows(self.gov.db_path, rows)
        restarted = self.governor()
        seeded = restarted.row(MODEL_OUT)
        self.assertEqual(seeded["upstream_ms_sum"], 3000)
        self.assertEqual(seeded["upstream_n"], 2)
        self.assertEqual(seeded["upstream_samples"], [])
        self.request(restarted, 0.5, 0.1)
        row = self.minute_tuple(restarted)
        self.assertEqual(row[12:14], (3500, 3))
        self.assertEqual(row[14], 500)

    def test_old_schema_gains_the_latency_columns_and_keeps_its_rows(self) -> None:
        db = self.gov.db_path
        connection = sqlite3.connect(db)
        connection.execute(OLD_MINUTES_DDL)
        connection.execute(
            "INSERT INTO proxy_minutes (minute, model, req, ok, r429, input, cache_read, output, uncached_est, "
            "wait_p50_ms, wait_p95_ms, target) VALUES ('2026-11-01T10:00', ?, 4, 4, 0, 10, 0, 20, 0, 1, 2, 0.9)",
            (MODEL_OUT,),
        )
        connection.commit()
        connection.close()
        self.request(self.gov, 0.3, 0.1)
        write_minute_rows(db, self.gov.take_minute_rows())
        write_minute_rows(db, [])
        connection = sqlite3.connect(db)
        present = {row[1] for row in connection.execute("PRAGMA table_info(proxy_minutes)")}
        old = connection.execute(
            "SELECT req, output, upstream_n FROM proxy_minutes WHERE minute = '2026-11-01T10:00'"
        ).fetchone()
        connection.close()
        self.assertTrue(set(LATENCY_COLUMNS) <= present)
        self.assertEqual(old, (4, 20, None))

    def test_old_schema_seed_reads_zero_latency(self) -> None:
        db = self.gov.db_path
        connection = sqlite3.connect(db)
        connection.execute(OLD_MINUTES_DDL)
        connection.execute(
            "INSERT INTO proxy_minutes (minute, model, req, ok, r429, input, cache_read, output, uncached_est, "
            "wait_p50_ms, wait_p95_ms, target) VALUES (?, ?, 7, 7, 0, 0, 0, 0, 0, 0, 0, 0.9)",
            (minute_stamp(WALL_MINUTE), MODEL_OUT),
        )
        connection.commit()
        connection.close()
        seeded = read_minute_counts(db, minute_stamp(WALL_MINUTE), MODEL_OUT)
        self.assertEqual(seeded["req"], 7)
        self.assertEqual(seeded["upstream_n"], 0)
        self.assertEqual(seeded["upstream_ms_sum"], 0)

    def test_status_reports_in_flight_per_model_and_the_last_minute_percentiles(self) -> None:
        for _ in range(2):
            self.gov.upstream_started(make_ticket(self.gov, model=MODEL_OUT))
        self.gov.upstream_started(make_ticket(self.gov, model=MODEL_KIMI))
        self.assertEqual(self.gov.status()["upstream"]["in_flight"], {MODEL_OUT: 2, MODEL_KIMI: 1})
        self.gov.upstream_finished(make_ticket(self.gov, model=MODEL_KIMI))
        self.request(self.gov, 1.0, 0.1)
        self.request(self.gov, 3.0, 0.1)
        upstream = self.gov.status()["upstream"]
        self.assertEqual(upstream["in_flight"], {MODEL_OUT: 2})
        self.assertEqual(upstream["last_60s"][MODEL_OUT], {"n": 2, "p50_ms": 3000, "p95_ms": 3000})
        self.now[0] += 61
        self.assertEqual(self.gov.status()["upstream"]["last_60s"], {})


class ProxyLatencyTest(ProxyCase):
    async def test_a_successful_request_is_timed_and_reported_in_status(self) -> None:
        status, _ = await self.post({"model": MODEL_OUT, "messages": []}, job="job1")
        self.assertEqual(status, 200)
        status, text = await asyncio.to_thread(get_json, self.port, "/agentq/status")
        upstream = json.loads(text)["upstream"]
        self.assertEqual(upstream["in_flight"], {})
        self.assertEqual(upstream["last_60s"][MODEL_OUT]["n"], 1)
        row = self.gov.row(MODEL_OUT)
        self.assertEqual(row["upstream_n"], 1)
        self.assertGreaterEqual(row["in_flight_max"], 1)


class BacklogReserveTest(unittest.TestCase):
    """A backlog ticket waits once its budget passes backlog_max_frac of the target; normal work keeps the rest."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.gov = Governor(Path(tmp.name), config_with(), log=quiet)

    def occupy(self, amount: int) -> None:
        self.gov.budgets["total"].entries.append(Entry(at=self.gov.clock(), amount=amount, lane="normal"))

    def test_backlog_waits_past_the_reserve_while_normal_work_still_fits(self) -> None:
        self.occupy(8_900_000)
        backlog = make_ticket(self.gov, job="bl", model=MODEL_OUT, text="x", max_tokens=8, lane="backlog")
        normal = make_ticket(self.gov, job="nm", model=MODEL_OUT, text="x", max_tokens=8, lane="normal")
        self.gov.enqueue(backlog)
        self.gov.enqueue(normal)
        self.gov.pump()
        self.assertIn(backlog, self.gov.queues["backlog"])
        self.assertNotIn(normal, self.gov.queues["normal"])
        self.assertEqual(backlog.blocked_by, "total")

    def test_the_reserve_does_not_mark_the_budget_saturated(self) -> None:
        self.occupy(8_900_000)
        self.gov.enqueue(make_ticket(self.gov, job="bl", model=MODEL_OUT, text="x", max_tokens=8, lane="backlog"))
        self.gov.pump()
        self.assertFalse(self.gov.budgets["total"].saturated)
        self.assertFalse(self.gov.budgets["total"].saturated_since_tick)

    def test_backlog_below_the_reserve_is_admitted(self) -> None:
        self.occupy(8_000_000)
        backlog = make_ticket(self.gov, job="bl", model=MODEL_OUT, text="x", max_tokens=8, lane="backlog")
        self.gov.enqueue(backlog)
        self.gov.pump()
        self.assertNotIn(backlog, self.gov.queues["backlog"])

    def test_an_empty_budget_always_admits_one_backlog_request(self) -> None:
        backlog = make_ticket(self.gov, job="bl", model=MODEL_OUT, text="x", max_tokens=8, lane="backlog")
        self.gov.enqueue(backlog)
        self.gov.pump()
        self.assertNotIn(backlog, self.gov.queues["backlog"])

    def test_a_reserve_hold_does_not_set_the_global_saturated_flag(self) -> None:
        self.occupy(8_900_000)
        backlog = make_ticket(self.gov, job="bl", model=MODEL_OUT, text="x", max_tokens=8, lane="backlog")
        self.gov.enqueue(backlog)
        backlog.queued_at -= 10
        self.gov.pump()
        self.assertFalse(self.gov.budgets["total"].saturated)
        self.assertFalse(self.gov.status()["saturated"])

    def test_a_capacity_hold_still_sets_the_global_saturated_flag(self) -> None:
        self.occupy(9_800_000)
        normal = make_ticket(self.gov, job="nm", model=MODEL_OUT, text="x", max_tokens=8, lane="normal")
        self.gov.enqueue(normal)
        normal.queued_at -= 10
        self.gov.pump()
        self.assertTrue(self.gov.budgets["total"].saturated)
        self.assertTrue(self.gov.status()["saturated"])


INFLIGHT_FAST = {"min_limit": 16, "max_limit": 96, "interval_s": 10.0, "short_window_s": 30.0, "min_samples": 3}


class InflightLimiterMathTest(unittest.TestCase):
    """The gradient controller and its latched state, driven by adjust() calls with an explicit clock."""

    def limiter(self, **settings) -> InflightLimiter:
        return InflightLimiter({**INFLIGHT_FAST, "mode": "enforce", **settings}, now=0.0)

    def feed(self, lim: InflightLimiter, now: float, ttfb_s: float, count: int = 5) -> None:
        for k in range(count):
            lim.observe(ttfb_s, now - k)

    def run_intervals(self, lim: InflightLimiter, start: float, seconds_each: float, ttfb_s: float, intervals: int) -> float:
        now = start
        for _ in range(intervals):
            now += lim.interval_s
            self.feed(lim, now, ttfb_s)
            lim.adjust(now)
        return now

    def test_steady_latency_arms_after_two_adjusts_and_holds_the_maximum(self) -> None:
        lim = self.limiter()
        self.run_intervals(lim, 0.0, 10, 5.0, 1)
        self.assertEqual(lim.state, UNARMED)
        self.assertIsNone(lim.current_limit())
        self.run_intervals(lim, 10.0, 10, 5.0, 12)
        self.assertTrue(lim.active())
        self.assertEqual(lim.current_limit(), 96)

    def test_rising_latency_pulls_the_limit_down(self) -> None:
        lim = self.limiter()
        now = self.run_intervals(lim, 0.0, 10, 5.0, 6)
        before = lim.current_limit()
        self.run_intervals(lim, now, 10, 12.0, 10)
        self.assertLess(lim.current_limit(), before)

    def test_recovering_latency_lets_the_limit_rise_again(self) -> None:
        lim = self.limiter()
        now = self.run_intervals(lim, 0.0, 10, 5.0, 6)
        now = self.run_intervals(lim, now, 10, 15.0, 15)
        low = lim.current_limit()
        self.run_intervals(lim, now, 10, 5.0, 15)
        self.assertGreater(lim.current_limit(), low)

    def test_the_reviewer_sequence_never_flaps_between_adjusts(self) -> None:
        """Armed by the adjusts at t10 and t20. Samples from t40 expire, so the adjust at t80 suspends. A sparse batch
        at t85 arrives between adjusts and does not re-arm. The adjust at t90 counts one qualifying window (streak 1),
        and the adjust at t100 re-arms. No change happens between adjusts."""
        lim = self.limiter(min_samples=3)
        self.run_intervals(lim, 0.0, 10, 5.0, 4)
        self.assertEqual(lim.current_limit(), 96)
        self.assertTrue(lim.adjust(80.0))
        self.assertEqual(lim.state, SUSPENDED)
        self.assertIsNone(lim.current_limit())
        self.feed(lim, 85.0, 5.0, count=3)
        self.assertIsNone(lim.current_limit())
        self.assertFalse(lim.enforcing(0.0))
        self.assertFalse(lim.adjust(88.0))
        self.assertIsNone(lim.current_limit())
        self.assertFalse(lim.adjust(90.0))
        self.assertEqual(lim.state, SUSPENDED)
        self.assertTrue(lim.adjust(100.0))
        self.assertEqual(lim.state, ARMED)
        self.assertEqual(lim.current_limit(), 96)

    def test_a_suspended_limit_stays_suspended_across_adjusts_without_fresh_samples(self) -> None:
        lim = self.limiter()
        now = self.run_intervals(lim, 0.0, 10, 5.0, 4)
        for k in range(5):
            lim.adjust(now + 60 + 10 * k)
            self.assertIsNone(lim.current_limit())
        self.assertEqual(lim.state, SUSPENDED)
        self.assertIsNotNone(lim.baseline_s)

    def test_no_samples_fails_open_and_the_limit_never_moves(self) -> None:
        lim = self.limiter()
        self.assertFalse(lim.adjust(10.0))
        self.assertFalse(lim.active())
        self.assertIsNone(lim.current_limit())
        self.assertFalse(lim.enforcing(10.0))

    def test_fewer_than_min_samples_is_still_no_data(self) -> None:
        lim = self.limiter()
        lim.observe(30.0, 10.0)
        self.assertIsNone(lim.short_run_s(10.0))
        self.assertFalse(lim.adjust(10.0))
        self.assertIsNone(lim.current_limit())

    def test_the_limit_is_clamped_to_the_minimum_under_extreme_latency(self) -> None:
        lim = self.limiter()
        now = self.run_intervals(lim, 0.0, 10, 5.0, 3)
        self.run_intervals(lim, now, 10, 500.0, 30)
        self.assertEqual(lim.current_limit(), 16)

    def test_the_limit_never_exceeds_the_maximum(self) -> None:
        lim = self.limiter(max_limit=40)
        self.run_intervals(lim, 0.0, 10, 5.0, 40)
        self.assertEqual(lim.current_limit(), 40)

    def test_off_computes_nothing(self) -> None:
        lim = self.limiter(mode="off")
        self.run_intervals(lim, 0.0, 10, 5.0, 3)
        self.assertFalse(lim.active())
        self.assertIsNone(lim.current_limit())

    def test_shadow_computes_but_never_enforces(self) -> None:
        lim = self.limiter(mode="shadow")
        self.run_intervals(lim, 0.0, 10, 5.0, 3)
        self.assertTrue(lim.active())
        self.assertFalse(lim.enforcing(0.0))
        self.assertEqual(lim.arm_at(0.0), "shadow")

    def test_ab_arms_alternate_on_calendar_periods(self) -> None:
        lim = self.limiter(ab_period_s=600)
        lim.state = ARMED
        lim.baseline_s = 5.0
        self.assertEqual(lim.arm_at(0.0), "enforce")
        self.assertEqual(lim.arm_at(599.0), "enforce")
        self.assertEqual(lim.arm_at(600.0), "off")
        self.assertEqual(lim.arm_at(1200.0), "enforce")
        self.assertTrue(lim.enforcing(0.0))
        self.assertFalse(lim.enforcing(600.0))

    def test_bad_settings_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            InflightLimiter({"mode": "sometimes"}, now=0.0)
        with self.assertRaises(ValueError):
            InflightLimiter({"ab_period_s": 90}, now=0.0)
        with self.assertRaises(ValueError):
            InflightLimiter({"min_limit": 50, "max_limit": 20}, now=0.0)


class InflightAdmissionTest(unittest.TestCase):
    """Lane order and the shadow/enforce split, on a governor whose limiter is armed at a cap of one."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.mono = [1000.0]
        self.wall_now = [WALL_MINUTE]

    def governor(self, mode: str = "enforce", cap: int = 1, armed: bool = True, **settings) -> Governor:
        gov = Governor(
            self.home, config_with(inflight={"mode": mode, **settings}),
            clock=lambda: self.mono[0], wall=lambda: self.wall_now[0], log=quiet,
        )
        if armed:
            gov.inflight.baseline_s = 5.0
            gov.inflight.limit = float(cap)
            gov.inflight.state = ARMED
            for k in range(3):
                gov.inflight.observe(5.0, self.mono[0] - k * 0.1)
        return gov

    def test_expired_samples_admit_everything_even_with_a_cap_of_one(self) -> None:
        gov = self.governor()
        first = make_ticket(gov, job="a", model=MODEL_OUT)
        admit(gov, first)
        second = make_ticket(gov, job="b", model=MODEL_OUT)
        third = make_ticket(gov, job="c", model=MODEL_OUT)
        self.mono[0] += 120
        gov.adjust_inflight()
        gov.enqueue(second)
        gov.enqueue(third)
        gov.pump()
        self.assertTrue(second.holds)
        self.assertTrue(third.holds)
        self.assertNotIn(INFLIGHT, (second.last_refusal, third.last_refusal))
        self.assertIsNone(gov.status()["inflight_limit"])

    def test_under_the_cap_backlog_waits_and_urgent_is_admitted_first(self) -> None:
        gov = self.governor()
        backlog = make_ticket(gov, job="bl", model=MODEL_OUT, lane="backlog")
        normal = make_ticket(gov, job="nm", model=MODEL_OUT, lane="normal")
        urgent = make_ticket(gov, job="ug", model=MODEL_OUT, lane="urgent")
        for ticket in (backlog, normal, urgent):
            gov.enqueue(ticket)
        gov.pump()
        self.assertTrue(urgent.holds)
        self.assertFalse(normal.holds)
        self.assertFalse(backlog.holds)
        self.assertEqual(normal.last_refusal, INFLIGHT)
        self.assertEqual(backlog.last_refusal, INFLIGHT)
        self.assertEqual(gov.ds_flight, 1)
        self.assertEqual(gov.status()["inflight_queued"], 2)

    def test_a_kimi_request_is_not_held_by_the_deepseek_cap(self) -> None:
        gov = self.governor()
        first = make_ticket(gov, job="a", model=MODEL_OUT, lane="urgent")
        kimi = make_ticket(gov, job="k", model=MODEL_KIMI, text="x")
        gov.enqueue(first)
        gov.enqueue(kimi)
        gov.pump()
        self.assertTrue(first.holds)
        self.assertTrue(kimi.holds)

    def test_a_held_request_is_admitted_once_the_flight_ends(self) -> None:
        gov = self.governor()
        first = make_ticket(gov, job="a", model=MODEL_OUT)
        second = make_ticket(gov, job="b", model=MODEL_OUT)
        admit(gov, first)
        gov.enqueue(second)
        gov.pump()
        self.assertFalse(second.holds)
        gov.leave_flight(first)
        gov.pump()
        self.assertTrue(second.holds)

    def test_shadow_admits_everything_and_counts_what_would_have_waited(self) -> None:
        gov = self.governor(mode="shadow")
        tickets = [make_ticket(gov, job=f"j{k}", model=MODEL_OUT) for k in range(3)]
        for ticket in tickets:
            gov.enqueue(ticket)
        gov.pump()
        self.assertTrue(all(t.holds for t in tickets))
        row = gov.row(MODEL_OUT)
        self.assertEqual(row["inflight_over"], 2)

    def test_the_off_arm_of_an_ab_run_admits_everything(self) -> None:
        gov = self.governor(ab_period_s=600)
        gov.clock = lambda: 1.0
        gov.wall = lambda: 600.0 + 5
        gov.inflight.last_adjust = 0.0
        tickets = [make_ticket(gov, job=f"j{k}", model=MODEL_OUT) for k in range(2)]
        for ticket in tickets:
            gov.enqueue(ticket)
        gov.pump()
        self.assertTrue(all(t.holds for t in tickets))
        self.assertEqual(gov.row(MODEL_OUT)["inflight_over"], 1)

    def test_no_measurement_means_no_cap(self) -> None:
        gov = self.governor(armed=False)
        tickets = [make_ticket(gov, job=f"j{k}", model=MODEL_OUT) for k in range(3)]
        for ticket in tickets:
            gov.enqueue(ticket)
        gov.pump()
        self.assertTrue(all(t.holds for t in tickets))
        self.assertIsNone(gov.status()["inflight_limit"])

    def test_status_reports_the_limit_mode_and_first_byte_readings(self) -> None:
        gov = self.governor(mode="shadow")
        status = gov.status()
        self.assertEqual(status["inflight_limit"], 1)
        self.assertEqual(status["inflight_max"], 96)
        self.assertEqual(status["inflight_mode"], "shadow")
        self.assertFalse(status["inflight_enforcing"])
        self.assertEqual(status["ttfb_baseline_ms"], 5000)
        self.assertEqual(status["ttfb_short_ms"], 5000)

    def test_the_minute_row_carries_limit_mode_arm_and_the_latency_split(self) -> None:
        gov = self.governor()
        ticket = make_ticket(gov, job="a", model=MODEL_OUT)
        admit(gov, ticket)
        ticket.started_above = True
        gov.finish_ok(ticket, None, upstream_s=1.0, ttfb_s=0.5)
        gov.upstream_finished(ticket)
        gov.release(ticket)
        stamp = minute_stamp(gov.wall())
        row = next(r for r in gov.take_minute_rows() if r[0] == stamp and r[1] == MODEL_OUT)
        columns = dict(zip(MINUTE_ROW_COLUMNS, row))
        self.assertEqual(columns["inflight_limit"], 1)
        self.assertEqual(columns["inflight_mode"], "enforce")
        self.assertEqual(columns["inflight_arm"], "enforce")
        self.assertEqual(columns["ttfb_gt_p50_ms"], 500)
        self.assertIsNone(columns["ttfb_le_p50_ms"])

    def test_enter_and_leave_flight_are_idempotent(self) -> None:
        gov = self.governor()
        ticket = make_ticket(gov, job="a", model=MODEL_OUT)
        gov.enter_flight(ticket)
        gov.enter_flight(ticket)
        self.assertEqual(gov.ds_flight, 1)
        gov.leave_flight(ticket)
        gov.leave_flight(ticket)
        self.assertEqual(gov.ds_flight, 0)


class InflightMigrationTest(unittest.TestCase):
    OLD_DDL = """
    CREATE TABLE proxy_minutes (
        minute TEXT NOT NULL, model TEXT NOT NULL,
        req INT, ok INT, r429 INT, input INT, cache_read INT, output INT, uncached_est INT,
        wait_p50_ms INT, wait_p95_ms INT, target REAL, upstream_ms_sum INT, upstream_n INT,
        upstream_p50_ms INT, upstream_p95_ms INT, ttfb_p50_ms INT, in_flight_max INT,
        PRIMARY KEY (minute, model))
    """

    def test_an_older_table_gains_the_limiter_columns_and_keeps_its_rows(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "agentq.db"
        connection = sqlite3.connect(path)
        connection.execute(self.OLD_DDL)
        connection.execute("INSERT INTO proxy_minutes (minute, model, req) VALUES ('2026-11-01T00:00', 'morph-dsv41flash-1017', 7)")
        connection.commit()
        connection.close()
        row = ("2026-11-01T00:01", MODEL_OUT) + (1, 1, 0, 10, 0, 5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 8, "enforce", "enforce", 2, None, 250)
        write_minute_rows(path, [row])
        write_minute_rows(path, [row])
        connection = sqlite3.connect(path)
        columns = {r[1] for r in connection.execute("PRAGMA table_info(proxy_minutes)")}
        self.assertTrue({"inflight_limit", "inflight_mode", "inflight_arm", "inflight_over", "ttfb_le_p50_ms", "ttfb_gt_p50_ms"} <= columns)
        self.assertEqual(connection.execute("SELECT req FROM proxy_minutes WHERE minute = '2026-11-01T00:00'").fetchone()[0], 7)
        self.assertEqual(connection.execute("SELECT inflight_mode, inflight_over FROM proxy_minutes WHERE minute = '2026-11-01T00:01'").fetchone(), ("enforce", 2))
        connection.close()


class InflightMinuteRecordTest(unittest.TestCase):
    """The per-minute limiter record that the report reads: written with no DeepSeek traffic, carrying the
    mode, arm and limit the proxy was actually running."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.wall = [WALL_MINUTE]
        self.mono = [1000.0]

    def governor(self, **inflight) -> Governor:
        gov = Governor(
            self.home, config_with(inflight=inflight),
            clock=lambda: self.mono[0], wall=lambda: self.wall[0], log=quiet,
        )
        return gov

    def test_an_idle_armed_minute_is_recorded_with_its_mode_arm_and_limit(self) -> None:
        gov = self.governor(mode="shadow")
        gov.inflight.state = ARMED
        gov.inflight.limit = 40.0
        self.assertEqual(gov.inflight_minute_row(), (minute_stamp(WALL_MINUTE), "shadow", "shadow", 40, 0))

    def test_a_suspended_minute_records_no_limit(self) -> None:
        gov = self.governor(mode="shadow")
        self.assertEqual(gov.inflight_minute_row(), (minute_stamp(WALL_MINUTE), "shadow", "shadow", None, 0))

    def test_an_ab_enforce_minute_and_its_off_minute_are_recorded_by_the_running_arm(self) -> None:
        gov = self.governor(mode="enforce", ab_period_s=600)
        gov.inflight.state = ARMED
        gov.inflight.limit = 28.0
        period_start = WALL_MINUTE // 1200 * 1200  # an even period, so its first minute is the enforce arm
        self.wall[0] = period_start
        on = gov.inflight_minute_row()
        self.wall[0] = period_start + 600
        off = gov.inflight_minute_row()
        self.assertEqual((on[2], on[4]), ("enforce", 1))
        self.assertEqual((off[2], off[4]), ("off", 0))

    def test_the_writer_upserts_one_row_per_minute_and_creates_the_table_once(self) -> None:
        gov = self.governor(mode="shadow")
        db = gov.db_path
        row = gov.inflight_minute_row()
        write_inflight_minute_rows(db, [row])
        write_inflight_minute_rows(db, [row])
        connection = sqlite3.connect(db)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM inflight_minutes").fetchone()[0], 1)
        connection.close()




class StalledUpstreamTest(ProxyCase):
    """With upstream responses stalled, status still answers at once and new requests are still admitted."""

    async def wait_until(self, condition, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("condition not reached")
            await asyncio.sleep(0.01)

    async def test_status_answers_under_100_ms_while_requests_stall(self) -> None:
        releases = [asyncio.Event() for _ in range(3)]
        self.fake.script = [{"hold": event} for event in releases]
        stalled = [
            asyncio.create_task(self.post(self.chat(MODEL_OUT, stream=False), job=f"stall{k}")) for k in range(3)
        ]
        await self.wait_until(lambda: len(self.fake.requests) == 3)
        try:
            started = time.monotonic()
            status, text = await asyncio.to_thread(get_json, self.port, "/agentq/status")
            elapsed = time.monotonic() - started
            self.assertEqual(status, 200)
            self.assertLess(elapsed, 0.1)
            self.assertEqual(json.loads(text)["upstream"]["in_flight"][MODEL_OUT], 3)
        finally:
            for event in releases:
                event.set()
            await asyncio.gather(*stalled)

    async def test_a_new_request_is_admitted_while_others_stall(self) -> None:
        releases = [asyncio.Event() for _ in range(2)]
        self.fake.script = [{"hold": releases[0]}, {"hold": releases[1]}, {}]
        stalled = [
            asyncio.create_task(self.post(self.chat(MODEL_OUT, stream=False), job=f"stall{k}")) for k in range(2)
        ]
        await self.wait_until(lambda: len(self.fake.requests) == 2)
        try:
            status, _ = await asyncio.wait_for(self.post(self.chat(MODEL_OUT, stream=False), job="fresh"), 5)
            self.assertEqual(status, 200)
            self.assertEqual(len(self.fake.requests), 3)
        finally:
            for event in releases:
                event.set()
            await asyncio.gather(*stalled)

    async def test_store_writes_run_on_the_dedicated_writer_threads(self) -> None:
        name = await self.proxy.run_db(lambda: threading.current_thread().name)
        self.assertTrue(name.startswith("agentq-db"))

    async def test_a_flush_writes_on_the_writer_thread(self) -> None:
        seen: list[str] = []
        real = write_minute_rows

        def recorder(path, rows):
            seen.append(threading.current_thread().name)
            return real(path, rows)

        with mock.patch("agentq.proxy.write_minute_rows", recorder):
            self.gov.row(MODEL_OUT)["req"] += 1
            await settle_seeds(self.gov)
            await self.proxy.flush()
        self.assertTrue(seen and seen[0].startswith("agentq-db"))

    async def test_a_stuck_store_write_does_not_hold_up_admission(self) -> None:
        gate = threading.Event()

        def stuck(path, rows):
            gate.wait(5)

        with mock.patch("agentq.proxy.write_minute_rows", stuck):
            self.gov.row(MODEL_OUT)["req"] += 1
            self.proxy.schedule_flush()
            first = self.proxy.flush_task
            self.proxy.schedule_flush()
            self.assertIs(self.proxy.flush_task, first)
            started = time.monotonic()
            self.gov.pump()
            self.assertLess(time.monotonic() - started, 0.1)
            gate.set()
            await first


    async def test_the_maintenance_loop_keeps_admitting_while_a_store_write_is_stuck(self) -> None:
        gate = threading.Event()

        def stuck(path, rows):
            gate.wait(5)

        self.proxy.maintenance.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.proxy.maintenance
        with mock.patch("agentq.proxy.write_minute_rows", stuck), mock.patch("agentq.proxy.FLUSH_S", 0.0):
            self.gov.row(MODEL_OUT)["req"] += 1
            self.gov.state_dirty = True
            with mock.patch.object(self.gov, "pump", wraps=self.gov.pump) as pump:
                self.proxy.maintenance = asyncio.create_task(self.proxy.maintain())
                await asyncio.sleep(0.6)
                try:
                    self.assertGreaterEqual(pump.call_count, 2)
                finally:
                    gate.set()
                    self.proxy.maintenance.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self.proxy.maintenance


class CachedLaneTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.now = [1000.0]
        self.gov = Governor(Path(tmp.name), config_with(), clock=lambda: self.now[0], log=quiet)

    def test_an_unseen_job_has_no_cached_lane(self) -> None:
        self.assertIsNone(self.gov.cached_lane("job1"))

    def test_a_looked_up_job_is_answered_without_the_store(self) -> None:
        self.gov.lane_for("job1")
        with mock.patch("agentq.proxy.read_lane", side_effect=AssertionError("store read")):
            self.assertEqual(self.gov.cached_lane("job1"), "normal")

    def test_anonymous_jobs_are_normal_without_a_lookup(self) -> None:
        self.assertEqual(self.gov.cached_lane(""), "normal")

    def test_the_cached_lane_expires_after_lane_cache_s(self) -> None:
        self.gov.lane_for("job1")
        self.now[0] += LANE_CACHE_S + 1
        self.assertIsNone(self.gov.cached_lane("job1"))


class LoopLagTest(unittest.TestCase):
    def test_status_reports_the_latest_and_worst_lag_over_a_minute(self) -> None:
        now = [1000.0]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gov = Governor(Path(tmp.name), config_with(), clock=lambda: now[0], log=quiet)
        gov.note_loop_lag(0.02)
        gov.note_loop_lag(0.25)
        self.assertEqual(gov.status()["loop_lag_ms"], {"last_ms": 250, "max_60s_ms": 250})
        now[0] += 61
        gov.note_loop_lag(0.01)
        self.assertEqual(gov.status()["loop_lag_ms"], {"last_ms": 10, "max_60s_ms": 10})
class LogRotationTest(unittest.TestCase):
    """The proxy log must be bounded by size, not by launchd's stderr redirect."""

    def line(self, tag: str, size: int = 600) -> str:
        return f"{tag} {'x' * max(0, size - len(tag) - 1)}\n"

    def test_the_live_file_rotates_to_a_backup_at_the_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs" / "proxy.err.log"
            writer = RotatingLog(path, max_bytes=1024, backups=3)
            writer.write(self.line("first"))
            self.assertTrue(path.exists())
            self.assertEqual(writer.backup_path(1).exists(), False)
            writer.write(self.line("second"))
            writer.close()
            self.assertEqual(path.read_text(), "")
            self.assertIn("second", writer.backup_path(1).read_text())
            self.assertIn("first", writer.backup_path(1).read_text())

    def test_backups_are_capped_and_keep_the_newest_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proxy.err.log"
            writer = RotatingLog(path, max_bytes=1024, backups=2)
            for index in range(6):
                writer.write(self.line(f"line{index}"))
            writer.close()
            self.assertTrue(writer.backup_path(2).exists())
            self.assertFalse(writer.backup_path(3).exists())
            newest = writer.backup_path(1).read_text()
            self.assertIn("line4", newest)
            self.assertIn("line5", newest)
            self.assertIn("line3", writer.backup_path(2).read_text())

    def test_an_existing_oversized_file_rotates_on_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proxy.err.log"
            path.write_text(self.line("old", size=2048))
            writer = RotatingLog(path, max_bytes=1024, backups=3)
            writer.write(self.line("new"))
            writer.close()
            self.assertEqual(path.read_text(), self.line("new"))
            self.assertIn("old", writer.backup_path(1).read_text())

    def test_an_unwritable_path_records_the_error_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocked"
            blocker.write_text("a file where a directory is needed\n")
            writer = RotatingLog(blocker / "proxy.err.log", max_bytes=1024)
            writer.write("hello\n")  # must not raise into the governor
            self.assertIsInstance(writer.error, OSError)
            writer.write("again\n")
            self.assertIsInstance(writer.error, OSError)
            writer.flush()
            writer.close()

    def test_a_line_keeps_the_format_the_dashboard_parses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            log = file_log(home, config_with())
            log("request", job="j1", model=MODEL_OUT, lane="normal", status=200, attempts=1)
            log.writer.close()
            text = (home / LOG_PATH).read_text()
            match = re.match(
                r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) agentq-proxy request "
                r"job=j1 model=\S+ lane=normal status=200 attempts=1\n$",
                text,
            )
            self.assertIsNotNone(match, text)

    def test_an_empty_log_path_falls_back_to_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertIs(file_log(home, config_with(log_path="")), default_log)
            self.assertFalse((home / "logs").exists())

    def test_the_governor_opens_the_rotating_file_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            gov = Governor(home, config_with())
            self.addCleanup(gov.log.writer.close)
            gov.log("request", job="j2", model=MODEL_OUT, lane="normal", status=200, attempts=1)
            text = (home / LOG_PATH).read_text()
            self.assertIn(" agentq-proxy request job=j2 ", text)
            self.assertTrue(text.endswith("\n"))


class LogPathConfigTest(unittest.TestCase):
    def test_rotation_settings_come_from_the_config(self) -> None:
        config = config_with(log_path="logs/custom.log", log_max_bytes=2048, log_backups=1)
        with tempfile.TemporaryDirectory() as tmp:
            log = file_log(Path(tmp), config)
            self.assertEqual(log.writer.max_bytes, 2048)
            self.assertEqual(log.writer.backups, 1)
            log.writer.close()

    def test_defaults_bound_the_file_at_ten_megabytes_and_three_backups(self) -> None:
        self.assertEqual(DEFAULT_CONFIG["log_path"], LOG_PATH)
        self.assertEqual(DEFAULT_CONFIG["log_max_bytes"], 10 * 1024 * 1024)
        self.assertEqual(DEFAULT_CONFIG["log_backups"], 3)


async def settle_seeds(gov: Governor, timeout: float = 5.0) -> None:
    """Wait until every seed requested off the loop has been merged."""
    deadline = time.monotonic() + timeout
    while gov.pending_seeds:
        if time.monotonic() > deadline:
            raise AssertionError(f"seeds still pending: {gov.pending_seeds}")
        await asyncio.sleep(0.01)


def block_loop(seconds: float) -> None:
    """A synchronous call that holds the event loop, as a slow blocking helper would."""
    time.sleep(seconds)


class LoopStallWatchdogTest(ProxyCase):
    overrides = {"stall_dump_s": 0.3}

    def stalls(self) -> list[dict]:
        return [fields for event, fields in self.logs if event == "loop_stall"]

    async def test_a_blocked_loop_is_reported_once_with_the_stack_that_blocked_it(self) -> None:
        await asyncio.sleep(0.1)
        self.assertEqual(self.stalls(), [])
        block_loop(1.2)
        await asyncio.sleep(0.6)
        stalls = self.stalls()
        self.assertEqual(len(stalls), 1)
        self.assertGreaterEqual(stalls[0]["ms"], 300)
        self.assertIn("block_loop", stalls[0]["stack"])
        self.assertNotIn("\n", stalls[0]["stack"])

    async def test_each_stall_episode_is_reported_once_and_a_recovered_loop_is_not(self) -> None:
        block_loop(1.0)
        await asyncio.sleep(0.6)
        await asyncio.sleep(0.6)
        self.assertEqual(len(self.stalls()), 1)
        block_loop(1.0)
        await asyncio.sleep(0.6)
        self.assertEqual(len(self.stalls()), 2)


class SeedOffLoopTest(ProxyCase):
    """A new minute counts at once; its stored seed is read on a writer thread and merged when it lands."""

    def stored_minute(self, req: int, minute: str | None = None) -> str:
        minute = minute or minute_stamp(self.gov.wall())
        write_minute_rows(self.gov.db_path, [(minute, MODEL_OUT, req, req, 0, 0, 0, 0, 0, 0, 0, 0.98, 0, 0, 0, 0, 0, 0, None, None, None, 0, None, None)])
        return minute

    async def test_counting_starts_at_once_and_the_seed_is_read_on_a_writer_thread(self) -> None:
        self.stored_minute(5)
        threads: list[str] = []
        real = read_minute_counts

        def recorder(path, minute, model):
            threads.append(threading.current_thread().name)
            return real(path, minute, model)

        with mock.patch("agentq.proxy.read_minute_counts", recorder):
            row = self.gov.row(MODEL_OUT)
            self.assertEqual(row["req"], 0)
            row["req"] += 2
            await settle_seeds(self.gov)
        self.assertEqual(row["req"], 7)
        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0].startswith("agentq-db"))

    async def test_a_pending_minute_is_not_flushed_until_its_seed_lands(self) -> None:
        minute = self.stored_minute(5)
        row = self.gov.row(MODEL_OUT)
        row["req"] += 2
        written: list[list] = []
        real = write_minute_rows

        def recorder(path, rows):
            written.append(list(rows))
            return real(path, rows)

        with mock.patch("agentq.proxy.write_minute_rows", recorder):
            await self.proxy.flush()
            self.assertEqual([r for batch in written for r in batch if r[0] == minute], [])
            await settle_seeds(self.gov)
            await self.proxy.flush()
        stored = [r for batch in written for r in batch if r[0] == minute]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0][2], 7)

    async def test_a_calibration_seed_is_merged_into_the_minute(self) -> None:
        minute = minute_stamp(self.gov.wall())
        write_calib_rows(self.gov.db_path, [(minute, 3, 300, 900, 1200, 12.5, 2)])
        record = self.gov.calib_record(minute)
        self.assertEqual(record["overall"]["req"], 0)
        record["overall"]["req"] += 1
        record["r429"] += 1
        await settle_seeds(self.gov)
        self.assertEqual(record["overall"]["req"], 4)
        self.assertEqual(record["overall"]["billed"], 1200)
        self.assertEqual(record["r429"], 3)
        self.assertEqual(record["first_429_s"], 12.5)

    async def test_a_failed_seed_read_releases_the_minute_and_is_logged(self) -> None:
        with mock.patch("agentq.proxy.read_minute_counts", side_effect=RuntimeError("boom")):
            row = self.gov.row(MODEL_OUT)
            row["req"] += 4
            await settle_seeds(self.gov)
        self.assertIn("seed_error", self.events())
        self.assertEqual(row["req"], 4)
        self.assertEqual(self.gov.pending_seeds, set())

    def test_the_resolved_store_path_is_computed_once(self) -> None:
        read_only_uri.cache_clear()
        read_only_uri(self.gov.db_path)
        read_only_uri(self.gov.db_path)
        self.assertEqual(read_only_uri.cache_info().hits, 1)


class KeepAliveMorph:
    """Upstream that serves many requests per connection, as Morph does. Each script item applies to one request:
    "ok" (a JSON 200), "truncate" (a stream cut short), "500" (an error), or "ok-close" (a 200 that closes the connection)."""

    def __init__(self) -> None:
        self.accepted = 0
        self.requests = 0
        self.modes: list[str] = []
        self.last_headers: dict[str, str] = {}

    async def handle(self, reader, writer) -> None:
        self.accepted += 1
        try:
            while True:
                try:
                    request = await read_request(reader)
                except (ConnectionError, ValueError, asyncio.IncompleteReadError):
                    break
                if request is None:
                    break
                self.requests += 1
                self.last_headers = request[2]
                mode = self.modes.pop(0) if self.modes else "ok"
                if mode == "drop":
                    break
                if mode == "truncate":
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n")
                    await writer.drain()
                    break
                if mode == "500":
                    writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 2\r\nConnection: close\r\n\r\nno")
                    await writer.drain()
                    break
                body = json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 0}}}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
                await writer.drain()
                if mode == "ok-close":
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()


class KeepAliveCase(ProxyCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.fake_server.close()
        self.morph = KeepAliveMorph()
        self.fake_server = await asyncio.start_server(self.morph.handle, "127.0.0.1", 0)
        self.proxy.upstream_url = f"http://127.0.0.1:{self.fake_server.sockets[0].getsockname()[1]}/v1"

    async def send(self, stream: bool = False):
        return await self.post(self.chat(MODEL_OUT, stream=stream, max_tokens=8), job="ka")


class UpstreamKeepAliveTest(KeepAliveCase):
    async def test_sequential_requests_share_one_upstream_connection(self) -> None:
        for _ in range(5):
            status, _ = await self.send()
            self.assertEqual(status, 200)
        self.assertEqual(self.morph.requests, 5)
        self.assertEqual(self.morph.accepted, 1)

    async def test_the_upstream_request_no_longer_asks_for_close(self) -> None:
        await self.send()
        self.assertNotIn("connection", self.morph.last_headers)

    async def test_a_server_error_response_is_not_reused(self) -> None:
        self.morph.modes = ["ok", "500", "ok"]
        await self.send()
        await self.send()
        await self.send()
        self.assertEqual(self.morph.requests, 3)
        self.assertEqual(self.morph.accepted, 2)

    async def test_a_stream_that_ends_abnormally_is_not_reused(self) -> None:
        self.morph.modes = ["truncate", "ok"]
        with contextlib.suppress(Exception):
            await self.send(stream=True)
        status, _ = await self.send()
        self.assertEqual(status, 200)
        self.assertEqual(self.morph.accepted, 2)

    async def test_a_connection_the_server_closed_while_idle_is_replaced_without_an_error(self) -> None:
        self.morph.modes = ["ok-close"]
        status, _ = await self.send()
        self.assertEqual(status, 200)
        await asyncio.sleep(0.05)
        status, _ = await self.send()
        self.assertEqual(status, 200)
        self.assertEqual(self.morph.accepted, 2)

    async def test_a_request_whose_bytes_reached_the_upstream_is_never_sent_again(self) -> None:
        self.morph.modes = ["ok", "drop"]
        await self.send()
        status, _ = await self.send()
        self.assertEqual(status, 502)
        self.assertEqual(self.morph.requests, 2)

    async def test_the_maintenance_tick_drops_a_connection_the_server_closed(self) -> None:
        self.morph.modes = ["ok-close"]
        await self.send()
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.proxy.pool.idle), 0)

    async def test_an_idle_connection_expires_after_the_idle_limit(self) -> None:
        self.proxy.pool.idle_s = 0.05
        await self.send()
        await asyncio.sleep(0.2)
        await self.send()
        self.assertEqual(self.morph.accepted, 2)


class UpstreamPoolTest(unittest.IsolatedAsyncioTestCase):
    async def connections(self, count: int):
        server_side: list[asyncio.StreamWriter] = []

        async def hold(reader, writer):
            server_side.append(writer)
            await reader.read()
            writer.close()

        server = await asyncio.start_server(hold, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        opened = [await asyncio.open_connection("127.0.0.1", port) for _ in range(count)]
        self.addCleanup(server.close)
        for _ in range(200):
            if len(server_side) >= count:
                break
            await asyncio.sleep(0.01)
        self.server_side = server_side
        return opened

    async def test_prune_closes_a_half_closed_connection_behind_a_fresh_head(self) -> None:
        pool = UpstreamPool(4, 30.0)
        conns = await self.connections(2)
        for reader, writer in conns:
            pool.give("k", reader, writer)
        self.server_side[1].close()
        await asyncio.sleep(0.02)
        self.assertTrue(conns[1][0].at_eof())
        pool.prune()
        await asyncio.sleep(0.01)
        self.assertEqual(len(pool.idle), 1)
        self.assertTrue(conns[1][1].is_closing())
        self.assertFalse(conns[0][1].is_closing())

    async def test_take_returns_only_the_connection_for_the_same_upstream(self) -> None:
        pool = UpstreamPool(4, 30.0)
        (reader, writer), = await self.connections(1)
        pool.give("https://a", reader, writer)
        self.assertIsNone(pool.take("https://b"))
        self.assertEqual(pool.take("https://a")[1], writer)
        self.assertIsNone(pool.take("https://a"))

    async def test_the_oldest_connection_is_closed_when_the_pool_is_full(self) -> None:
        pool = UpstreamPool(2, 30.0)
        conns = await self.connections(3)
        for reader, writer in conns:
            pool.give("k", reader, writer)
        self.assertEqual(len(pool.idle), 2)
        await asyncio.sleep(0.01)
        self.assertTrue(conns[0][1].is_closing())
        self.assertFalse(conns[2][1].is_closing())

    async def test_a_zero_sized_pool_keeps_nothing(self) -> None:
        pool = UpstreamPool(0, 30.0)
        (reader, writer), = await self.connections(1)
        pool.give("k", reader, writer)
        self.assertEqual(len(pool.idle), 0)

    async def test_expired_connections_are_closed_on_the_next_use(self) -> None:
        now = [0.0]
        pool = UpstreamPool(4, 30.0, clock=lambda: now[0])
        (reader, writer), = await self.connections(1)
        pool.give("k", reader, writer)
        now[0] = 31.0
        self.assertIsNone(pool.take("k"))
        await asyncio.sleep(0.01)
        self.assertTrue(writer.is_closing())

    async def test_close_all_empties_the_pool(self) -> None:
        pool = UpstreamPool(4, 30.0)
        conns = await self.connections(2)
        for reader, writer in conns:
            pool.give("k", reader, writer)
        pool.close_all()
        self.assertEqual(len(pool.idle), 0)
        await asyncio.sleep(0.01)
        self.assertTrue(all(writer.is_closing() for _, writer in conns))


class TlsHandshakeLabTest(unittest.IsolatedAsyncioTestCase):
    """Lab measurement: TLS handshakes to the upstream for 100 sequential requests, with reuse off and on."""

    async def asyncSetUp(self) -> None:
        if shutil.which("openssl") is None:
            self.skipTest("openssl not available for a test certificate")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cert, key = Path(tmp.name) / "c.pem", Path(tmp.name) / "k.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
             "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
            check=True, capture_output=True,
        )
        server_ctx = ssl.create_server_context(ssl.PROTOCOL_TLS_SERVER) if hasattr(ssl, "create_server_context") else ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert, key)
        self.morph = KeepAliveMorph()
        self.morph_server = await asyncio.start_server(self.morph.handle, "127.0.0.1", 0, ssl=server_ctx)
        self.addCleanup(self.morph_server.close)
        port = self.morph_server.sockets[0].getsockname()[1]
        self.tmp = Path(tmp.name)
        home = self.tmp / "agentq"
        self.gov = Governor(home, config_with(), log=quiet)
        self.proxy = Proxy(self.gov, f"https://127.0.0.1:{port}/v1", KEY)
        client_ctx = ssl.create_default_context(cafile=str(cert))
        self.proxy.ssl_context = client_ctx
        self.server = await self.proxy.start("127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.addCleanup(self.server.close)

    async def asyncTearDown(self) -> None:
        await self.proxy.close()

    async def run_requests(self, count: int) -> int:
        before = self.morph.accepted
        for _ in range(count):
            status, _ = await asyncio.to_thread(
                post_json, self.port, {"model": MODEL_OUT, "max_tokens": 8, "messages": [{"role": "user", "content": "lab"}]}, "lab", "Bearer x"
            )
            self.assertEqual(status, 200)
        return self.morph.accepted - before

    async def test_reuse_cuts_tls_handshakes_for_100_requests(self) -> None:
        self.proxy.pool.max_idle = 0
        before = await self.run_requests(100)
        self.proxy.pool.max_idle = 64
        after = await self.run_requests(100)
        print(f"\nTLS handshakes per 100 sequential requests: without reuse={before}, with reuse={after}")
        self.assertGreaterEqual(before, 100)
        self.assertLessEqual(after, 5)


class CloseDrainsSeedsTest(ProxyCase):
    """Shutdown lets pending seed reads land, so the final flush writes stored plus new counts."""

    def stored(self, minute: str) -> int:
        counts = read_minute_counts(self.gov.db_path, minute, MODEL_OUT)
        return counts["req"] if counts else 0

    async def test_close_waits_for_a_pending_row_seed_and_writes_the_sum(self) -> None:
        minute = minute_stamp(self.gov.wall())
        write_minute_rows(self.gov.db_path, [(minute, MODEL_OUT, 5, 5, 0, 0, 0, 0, 0, 0, 0, 0.98, 0, 0, 0, 0, 0, 0, None, None, None, 0, None, None)])
        row = self.gov.row(MODEL_OUT)
        row["req"] += 2
        await self.proxy.close()
        self.assertEqual(self.stored(minute), 7)

    async def test_close_waits_for_a_pending_calibration_seed(self) -> None:
        minute = minute_stamp(self.gov.wall())
        write_calib_rows(self.gov.db_path, [(minute, 3, 0, 0, 0, None, 0)])
        record = self.gov.calib_record(minute)
        record["overall"]["req"] += 1
        await self.proxy.close()
        connection = sqlite3.connect(self.gov.db_path)
        stored = connection.execute("SELECT req FROM proxy_calib WHERE minute = ?", (minute,)).fetchone()[0]
        connection.close()
        self.assertEqual(stored, 4)

    async def test_a_seed_that_never_lands_is_released_and_the_new_counts_still_write(self) -> None:
        minute = minute_stamp(self.gov.wall())
        release = threading.Event()

        def stuck(path, minute, model):
            release.wait(5)
            return None

        with mock.patch("agentq.proxy.read_minute_counts", stuck):
            row = self.gov.row(MODEL_OUT)
            row["req"] += 2
            await self.proxy.close(asyncio.get_running_loop().time() + FINAL_RESERVE_S + 0.5)
        release.set()
        self.assertEqual(self.gov.pending_seeds, set())
        self.assertIn("seed_timeout", self.events())
        self.assertEqual(self.stored(minute), 2)


class EntryPointTest(unittest.TestCase):
    """Each process launchd or the broker starts must import and print its help, from a clean directory."""

    def run_help(self, args: list[str], cwd: str) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        return subprocess.run([sys.executable, *args, "--help"], cwd=cwd, env=env, capture_output=True, text=True, timeout=30)

    def test_the_proxy_runs_as_launchd_runs_it_from_a_clean_directory(self) -> None:
        proxy = str(Path(__file__).resolve().parents[1] / "agentq" / "proxy.py")
        with tempfile.TemporaryDirectory() as cwd:
            result = self.run_help(["-u", proxy], cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)

    def test_the_proxy_also_runs_in_isolated_mode(self) -> None:
        proxy = str(Path(__file__).resolve().parents[1] / "agentq" / "proxy.py")
        with tempfile.TemporaryDirectory() as cwd:
            result = self.run_help(["-I", proxy], cwd)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_dashboard_runs_as_launchd_runs_it(self) -> None:
        dash = str(Path(__file__).resolve().parents[1] / "agentq" / "dash.py")
        with tempfile.TemporaryDirectory() as cwd:
            result = self.run_help(["-u", dash], cwd)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_broker_runs_as_its_launch_agent_runs_it(self) -> None:
        package_root = str(Path(__file__).resolve().parents[1])
        result = self.run_help(["-m", "agentq.cli"], package_root)
        self.assertEqual(result.returncode, 0, result.stderr)


class SharedShutdownDeadlineTest(ProxyCase):
    """One shutdown budget covers the request drain, the seed drain and the final writes."""

    async def wait_until(self, condition, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("condition not reached")
            await asyncio.sleep(0.01)

    async def test_request_and_seed_drains_share_one_deadline(self) -> None:
        hold = asyncio.Event()
        self.fake.script = [{"hold": hold}]
        gate = threading.Event()

        def stuck_seed(path, minute, model):
            gate.wait(10)
            return None

        loop = asyncio.get_running_loop()
        stuck = None
        try:
            with mock.patch("agentq.proxy.read_minute_counts", stuck_seed):
                stuck = asyncio.create_task(self.post(self.chat(MODEL_OUT, stream=False), job="stuck"))
                await self.wait_until(lambda: len(self.fake.requests) == 1)
                await asyncio.sleep(0.05)
                self.assertEqual(len(self.gov.pending_seeds), 1)
                started = loop.time()
                await self.proxy.shutdown(6.0)
                elapsed = loop.time() - started
        finally:
            gate.set()
            hold.set()
            if stuck is not None:
                with contextlib.suppress(Exception):
                    await stuck
        self.assertLess(elapsed, 6.5)
        self.assertGreaterEqual(elapsed, 2.5)

    async def test_both_writer_threads_stuck_still_persist_the_in_memory_counts_in_time(self) -> None:
        minute = minute_stamp(self.gov.wall())
        write_minute_rows(self.gov.db_path, [(minute, MODEL_OUT, 5, 5, 0, 0, 0, 0, 0, 0, 0, 0.98, 0, 0, 0, 0, 0, 0, None, None, None, 0, None, None)])
        write_calib_rows(self.gov.db_path, [(minute, 3, 0, 0, 0, None, 0)])
        gate = threading.Event()

        def stuck_minute(path, minute, model):
            gate.wait(10)
            return None

        def stuck_calib(path, minute):
            gate.wait(10)
            return None

        loop = asyncio.get_running_loop()
        with mock.patch("agentq.proxy.read_minute_counts", stuck_minute), mock.patch("agentq.proxy.read_calib_counts", stuck_calib):
            self.gov.row(MODEL_OUT)["req"] += 2
            self.gov.calib_record(minute)["overall"]["req"] += 1
            await asyncio.sleep(0.05)
            started = loop.time()
            try:
                await self.proxy.shutdown(6.0)
                elapsed = loop.time() - started
            finally:
                gate.set()
        self.assertLess(elapsed, 6.5)
        minute_counts = read_minute_counts(self.gov.db_path, minute, MODEL_OUT)
        self.assertEqual(minute_counts["req"], 2)
        connection = sqlite3.connect(self.gov.db_path)
        calib = connection.execute("SELECT req FROM proxy_calib WHERE minute = ?", (minute,)).fetchone()[0]
        connection.close()
        self.assertEqual(calib, 1)
        self.assertIn("seed_timeout", self.events())


class LaunchAgentTest(unittest.TestCase):
    def test_exit_timeout_covers_the_default_shutdown_budget_with_margin(self) -> None:
        with PLIST.open("rb") as handle:
            plist = plistlib.load(handle)
        self.assertEqual(plist["ExitTimeOut"], int(DEFAULT_CONFIG["shutdown_s"]) + 15)
