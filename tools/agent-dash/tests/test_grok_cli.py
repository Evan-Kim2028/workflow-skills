"""Tests for dash/sources/grok_cli.py. Synthetic fixtures plus one bounded smoke."""
import os

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load(name):
    path = os.path.join(ROOT, "dash", "sources", name + ".py")
    spec = importlib.util.spec_from_file_location("dash_sources_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


grok = load("grok_cli")

SID = "99999999-9999-4999-8999-999999999999"
OTHER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
HUB = "00000000-0000-0000-0000-000000000002"


class Store:
    def __init__(self):
        self.offsets = {}
        self.runs = {}
        self.usage = {}
        self.sessions = {}
        self.usage_n = 0

    def get_offset(self, path):
        return self.offsets.get(path)

    def set_offset(self, path, inode, offset, size, mtime):
        self.offsets[path] = (inode, offset, size, mtime)

    def upsert_run(self, row):
        cur = dict(self.runs.get(row["run_id"], {}))
        for key, value in row.items():
            if value is not None:
                cur[key] = value
        self.runs[row["run_id"]] = cur

    def add_usage(self, row):
        self.usage_n += 1
        key = (row["tool"], row["session_id"], row["message_id"])
        self.usage[key] = dict(row)

    def upsert_session(self, row):
        cur = dict(self.sessions.get(row["session_id"], {}))
        for key, value in row.items():
            if value is not None:
                cur[key] = value
        self.sessions[row["session_id"]] = cur


def summed(store, sid=None):
    total = {k: 0 for k in ("input", "output", "cache_read", "cache_write", "reasoning")}
    for row in store.usage.values():
        if sid is not None and row["session_id"] != sid:
            continue
        for key in total:
            total[key] += row[key] or 0
    return total


def inference(sid, loop, prompt, cached, completion, reasoning, ts, pid=4242):
    obj = {
        "ts": ts,
        "pid": pid,
        "sid": sid,
        "msg": "shell.turn.inference_done",
        "ctx": {
            "loop_index": loop,
            "prompt_tokens": prompt,
            "cached_prompt_tokens": cached,
            "completion_tokens": completion,
            "reasoning_tokens": reasoning,
        },
    }
    return (json.dumps(obj) + "\n").encode()


def turn(number, inp, cached, out, write, reasoning, model="glm-5.3"):
    return {
        "turnNumber": number,
        "endedAt": "2026-10-16T12:27:11+00:00",
        "inputTokens": inp,
        "cachedReadTokens": cached,
        "outputTokens": out,
        "cacheCreationTokens": write,
        "reasoningTokens": reasoning,
        "primaryModelId": model,
        "modelUsage": {model: {"inputTokens": inp, "cachedReadTokens": cached, "outputTokens": out}},
    }


class GrokCliTests(unittest.TestCase):
    def setUp(self):
        grok.last_error = None

    def poll(self, store, cfg, now, quiet=True):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            grok.poll(store, cfg, now)
        if quiet:
            self.assertEqual(buf.getvalue(), "")
        return buf.getvalue()

    def tree(self, tmp, manifest, usage=None, log=None, sid=SID):
        sessions = os.path.join(tmp, "sessions")
        sdir = os.path.join(sessions, "cwd", sid)
        os.makedirs(sdir)
        other = os.path.join(sessions, "cwd", OTHER)
        os.makedirs(other)
        with open(os.path.join(other, "usage.json"), "w") as fh:
            fh.write(json.dumps({"session": {"inputTokens": 123456}, "turns": []}))
        with open(os.path.join(other, "chat_history.jsonl"), "w") as fh:
            fh.write('{"type":"user","content":"secret-body"}\n')
        pads = os.path.join(tmp, "pads")
        scratch = os.path.join(pads, "encoded", HUB, "scratchpad")
        os.makedirs(scratch)
        with open(os.path.join(scratch, "glm-sessions.tsv"), "w") as fh:
            fh.write(manifest)
        log_path = os.path.join(tmp, "unified.jsonl")
        if log is not None:
            with open(log_path, "wb") as fh:
                fh.write(log)
        if usage is not None:
            with open(os.path.join(sdir, "usage.json"), "w") as fh:
                json.dump(usage, fh)
        cfg = {
            "roots": {
                "grok_sessions": sessions,
                "grok_log": log_path,
                "scratchpads": pads,
            },
            "read_budget": {"per_file": 1024 * 1024, "per_tick": 4 * 1024 * 1024},
        }
        return cfg, log_path, os.path.join(sdir, "usage.json"), other

    def test_names_and_manifest_run(self):
        self.assertEqual(grok.NAME, "grok_cli")
        self.assertEqual(grok.INTERVAL_S, 2)
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _log, _usage, _other = self.tree(
                tmp,
                "# c\n\nlabel\tuuid\tepoch\tmodel\n"
                "glm-review\t%s\t1792153370\tglm-5.3\n" % SID,
            )
            cfg["roots"]["scratch"] = cfg["roots"].pop("scratchpads")
            cfg["read_budgets"] = cfg.pop("read_budget")
            store = Store()
            self.poll(store, cfg, 1792154000)
            run = store.runs["glm:%s:glm-review" % SID]
            self.assertEqual(run["source"], "glm")
            self.assertEqual(run["tier"], "glm")
            self.assertEqual(run["label"], "glm-review")
            self.assertEqual(run["model"], "glm-5.3")
            self.assertEqual(run["session_id"], SID)
            self.assertEqual(run["parent_session_id"], HUB)
            self.assertEqual(run["started_at"], 1792153370.0)
            self.assertEqual(run["clock_source"], "manifest")
            self.assertNotIn("status", run)
            self.assertNotIn("verdict", run)
            self.assertEqual(store.sessions[SID]["tool"], "glm")
            self.assertEqual(store.usage, {})

    def test_usage_json_stores_uncached_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = {
                "updatedAt": "2026-10-16T12:27:11+00:00",
                "session": {
                    "inputTokens": 100,
                    "cachedReadTokens": 40,
                    "outputTokens": 7,
                    "cacheCreationTokens": 2,
                    "reasoningTokens": 3,
                    "primaryModelId": "glm-5.3",
                    "modelUsage": {"glm-5.3": {"inputTokens": 100, "cachedReadTokens": 40}},
                },
                "turns": [turn(1, 100, 40, 7, 2, 3)],
            }
            cfg, _log, usage_path, other = self.tree(
                tmp, "glm-review\t%s\t1792153370\tglm-5.3-flash\n" % SID, usage=doc
            )
            store = Store()
            self.poll(store, cfg, 1792154000)
            total = summed(store, SID)
            self.assertEqual(total["input"], 60)
            self.assertEqual(total["cache_read"], 40)
            self.assertEqual(total["output"], 7)
            self.assertEqual(total["cache_write"], 2)
            self.assertEqual(total["reasoning"], 3)
            self.assertEqual(total["input"], 100 - 40)
            self.assertNotEqual(total["input"], 200)
            row = next(iter(store.usage.values()))
            self.assertEqual(row["message_id"], "turn:1")
            self.assertEqual(row["tool"], "glm")
            self.assertEqual(row["run_id"], "glm:%s:glm-review" % SID)
            self.assertEqual(store.runs[row["run_id"]]["model"], "glm-5.3")
            self.assertFalse(any(OTHER in key or "chat_history" in key for key in store.offsets))
            self.assertNotIn("secret-body", repr(store.usage))
            calls = store.usage_n
            self.poll(store, cfg, 1792154002)
            self.assertEqual(store.usage_n, calls)
            self.assertTrue(os.path.isfile(usage_path))
            self.assertTrue(os.path.isdir(other))

    def test_turns_are_not_added_to_the_session_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = {
                "session": {
                    "inputTokens": 999,
                    "cachedReadTokens": 999,
                    "outputTokens": 999,
                    "cacheCreationTokens": 999,
                    "reasoningTokens": 999,
                    "primaryModelId": "glm-5.3",
                    "modelUsage": {"glm-5.3": {"inputTokens": 999}},
                },
                "turns": [turn(1, 10, 1, 2, 3, 4), turn(2, 25, 4, 5, 6, 7)],
            }
            cfg, _log, _usage, _other = self.tree(
                tmp, "glm-review\t%s\t1792153370\tglm-5.3\n" % SID, usage=doc
            )
            store = Store()
            self.poll(store, cfg, 1792154000)
            total = summed(store, SID)
            self.assertEqual(total["input"], 30)
            self.assertEqual(total["cache_read"], 5)
            self.assertEqual(total["output"], 7)
            self.assertEqual(total["cache_write"], 9)
            self.assertEqual(total["reasoning"], 11)
            ids = {row["message_id"] for row in store.usage.values()}
            self.assertEqual(ids, {"turn:1", "turn:2"})

    def test_unified_fallback_stores_uncached_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = inference(SID, 0, 10, 4, 2, 1, "2026-10-16T12:00:00Z", pid=4242)
            log += inference(SID, 1, 30, 5, 3, 2, "2026-10-16T12:00:01Z", pid=4242)
            log += b'{"msg":"shell.turn.inference_done","sid":"not-json"\n'
            cfg, log_path, _usage, _other = self.tree(
                tmp, "glm-review\t%s\t1792153370\tglm-5.3\n" % SID, log=log
            )
            store = Store()
            self.poll(store, cfg, 1792154000)
            total = summed(store, SID)
            self.assertEqual(total["input"], 31)
            self.assertEqual(total["cache_read"], 9)
            self.assertEqual(total["output"], 5)
            self.assertEqual(total["reasoning"], 3)
            self.assertEqual(total["input"], 40 - 9)
            self.assertEqual(store.runs["glm:%s:glm-review" % SID]["pid"], 4242)
            calls = store.usage_n
            self.poll(store, cfg, 1792154002)
            self.assertEqual(store.usage_n, calls)
            with open(log_path, "ab") as fh:
                fh.write(inference(SID, 2, 1, 0, 1, 0, "2026-10-16T12:00:02Z"))
            self.poll(store, cfg, 1792154004)
            self.assertEqual(summed(store, SID)["input"], 32)

    def test_usage_json_present_first_ignores_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = inference(SID, 0, 1000, 1000, 50, 10, "2026-10-16T12:00:00Z")
            doc = {"session": {"inputTokens": 999}, "turns": [turn(1, 7, 1, 1, 0, 0)]}
            cfg, _log, _usage, _other = self.tree(
                tmp, "glm-review\t%s\t1792153370\tglm-5.3\n" % SID, usage=doc, log=log
            )
            store = Store()
            self.poll(store, cfg, 1792154000)
            self.assertEqual(summed(store, SID)["input"], 6)
            ids = {row["message_id"] for row in store.usage.values()}
            self.assertEqual(ids, {"turn:1"})
            self.assertNotIn("grok_cli:seen:" + SID, store.offsets)

    def test_usage_json_supersedes_unified_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = inference(SID, 0, 10, 4, 2, 1, "2026-10-16T12:00:00Z")
            log += inference(SID, 1, 30, 5, 3, 2, "2026-10-16T12:00:01Z")
            cfg, _log, usage_path, _other = self.tree(
                tmp, "glm-review\t%s\t1792153370\tglm-5.3\n" % SID, log=log
            )
            store = Store()
            self.poll(store, cfg, 1792154000)
            self.assertEqual(summed(store, SID)["input"], 31)
            doc = {"session": {"inputTokens": 999}, "turns": [turn(1, 7, 1, 1, 0, 0)]}
            with open(usage_path, "w") as fh:
                json.dump(doc, fh)
            self.poll(store, cfg, 1792154002)
            self.assertEqual(summed(store, SID)["input"], 6)
            self.assertEqual(summed(store, SID)["cache_read"], 1)
            inference_rows = [
                row for row in store.usage.values() if row["message_id"].startswith("inference:")
            ]
            self.assertEqual(len(inference_rows), 2)
            self.assertTrue(all(row["input"] == 0 and row["cache_read"] == 0 for row in inference_rows))
            self.assertIn("grok_cli:auth:" + SID, store.offsets)
            calls = store.usage_n
            self.poll(store, cfg, 1792154004)
            self.assertEqual(store.usage_n, calls)
            self.assertEqual(summed(store, SID)["input"], 6)

    def test_missing_root(self):
        store = Store()
        with tempfile.TemporaryDirectory() as tmp:
            self.poll(
                store,
                {
                    "roots": {
                        "grok_sessions": "/no/such/grok-sessions",
                        "grok_log": "/no/such/unified.jsonl",
                        "scratchpads": "/no/such/scratch",
                    },
                    # Isolate from the machine's registered run folders.
                    "state_dir": os.path.join(tmp, "state"),
                },
                1792154000,
            )
        self.assertEqual(store.runs, {})

    def test_encoded_cwd_root_attributes_glm_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            enc = os.path.join(tmp, "enc")
            pad = os.path.join(enc, HUB, "scratchpad")
            os.makedirs(pad)
            with open(os.path.join(pad, "glm-sessions.tsv"), "w") as fh:
                fh.write("glm-review\t%s\t1792153370\tglm-5.3\n" % SID)
            sessions = os.path.join(tmp, "sessions")
            os.makedirs(sessions)
            cfg = {
                "roots": {
                    "grok_sessions": sessions,
                    "grok_log": os.path.join(tmp, "missing.jsonl"),
                    "scratchpads": enc,
                },
                "read_budgets": {"per_file": 1024 * 1024, "per_tick": 1024 * 1024},
            }
            store = Store()
            self.poll(store, cfg, 1792154000)
            run = store.runs["glm:%s:glm-review" % SID]
            self.assertEqual(run["model"], "glm-5.3")
            self.assertEqual(run["tier"], "glm")
            self.assertEqual(run["label"], "glm-review")
            self.assertEqual(run["parent_session_id"], HUB)
            self.assertEqual(run["started_at"], 1792153370.0)

    def test_registered_run_dir_is_scanned_without_scratch_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            rundir = os.path.join(tmp, "ws8")
            os.makedirs(rundir)
            with open(os.path.join(rundir, "glm-sessions.tsv"), "w") as fh:
                fh.write("ws8-build\t%s\t1792153370\tglm-5.3\n" % SID)
            state = os.path.join(tmp, "state")
            os.makedirs(state)
            with open(os.path.join(state, "run-dirs.txt"), "w") as fh:
                fh.write(rundir + "\n")
            sessions = os.path.join(tmp, "sessions")
            os.makedirs(sessions)
            cfg = {
                "roots": {
                    "grok_sessions": sessions,
                    "grok_log": os.path.join(tmp, "missing.jsonl"),
                    "scratchpads": [],
                },
                "state_dir": state,
                "read_budgets": {"per_file": 1024 * 1024, "per_tick": 1024 * 1024},
            }
            store = Store()
            self.poll(store, cfg, 1792154000)
            run = store.runs["glm:%s:ws8-build" % SID]
            self.assertEqual(run["label"], "ws8-build")
            self.assertEqual(run["source"], "glm")

    def test_nested_child_folder_manifest_is_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            enc = os.path.join(tmp, "enc")
            nested = os.path.join(enc, HUB, "scratchpad", "runs")
            os.makedirs(nested)
            with open(os.path.join(nested, "glm-sessions.tsv"), "w") as fh:
                fh.write("ws8-nested\t%s\t1792153370\tglm-5.3\n" % SID)
            pads = list(grok._manifest_dirs(enc, "glm-sessions.tsv", None))
            self.assertEqual(len(pads), 1)
            self.assertEqual(pads[0], (os.path.realpath(nested), HUB))
            sessions = os.path.join(tmp, "sessions")
            os.makedirs(sessions)
            cfg = {
                "roots": {
                    "grok_sessions": sessions,
                    "grok_log": os.path.join(tmp, "missing.jsonl"),
                    "scratchpads": enc,
                },
                "read_budgets": {"per_file": 1024 * 1024, "per_tick": 1024 * 1024},
            }
            store = Store()
            self.poll(store, cfg, 1792154000)
            run = store.runs["glm:%s:ws8-nested" % SID]
            self.assertEqual(run["parent_session_id"], HUB)

    @unittest.skipUnless(os.environ.get("AGENT_DASH_SMOKE") == "1", "set AGENT_DASH_SMOKE=1 to read a local glm usage file")
    def test_smoke_real_usage_json(self):
        sessions = os.environ.get("AGENT_DASH_SMOKE_GROK_SESSIONS")
        sid = os.environ.get("AGENT_DASH_SMOKE_GLM_SID")
        if not sessions or not sid or not os.path.isdir(sessions):
            self.skipTest("AGENT_DASH_SMOKE_GROK_SESSIONS and AGENT_DASH_SMOKE_GLM_SID are not set")
        found = False
        for enc in os.scandir(sessions):
            if os.path.isfile(os.path.join(enc.path, sid, "usage.json")):
                found = True
                break
        self.assertTrue(found)


if __name__ == "__main__":
    unittest.main()
