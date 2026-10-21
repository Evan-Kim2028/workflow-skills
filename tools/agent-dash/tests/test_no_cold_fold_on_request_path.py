"""No cold fold on the request path: full-table recomputes stay behind caches.

A request-path call that reaches an uncached full scan outside its
cached wrapper recomputes the whole table cold on every page tick —
the outage this guards. Two checks: a static call-graph rule over
dash/server.py into dash/ plus a runtime counter proving a one-row
write refolds at most the changed run.
"""

import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dash"


def cold_folds():
    """Uncached full-scan functions and the cached wrapper each must go through.

    ``_usage_by_run_uncached`` folds all 100k usage rows;
    ``classify_runs`` classifies every run against rules, taskmap and
    plans; ``_all_runs`` reads the whole runs table. Their wrappers
    (``_usage_by_run``, ``build_phases``/``by_phase_all_time``,
    the snapshot/query payload caches) bound the recompute; anything
    else that calls them puts a cold fold back on the tick path.
    """
    return {
        "_usage_by_run_uncached": ("_usage_by_run",),
        "classify_runs": ("build_phases", "_collect_labels"),
        "_all_runs": (
            "query_concurrency",
            "_mismatches_locked",
            "_scoped_usage",
            "by_phase_all_time",
            "phase_events",
        ),
    }


def cached_gateways():
    """Request-path entries whose own cache bounds any recompute inside.

    The graph walk stops at these: a handler may only reach a cold
    fold by passing through one of them (or a wrapper in
    cold_folds). Adding a new payload cache means listing it here.
    """
    return {
        "snapshot",
        "snapshot_cached",
        "query_workstreams",
        "query_runs",
        "query_concurrency",
        "query_usage",
        "usage_model_stats",
        "build_state",
        "build_phases",
        "by_phase_all_time",
        "_usage_by_run",
        "_classify_runs_cached",
        "state_identity",
        "ws_rollup",
    }


def documented_cold_paths():
    """Request paths allowed to scan without a result cache, and why.

    ``reconcile_numbers`` serves the diagnostic /api/reconcile check:
    it takes the live worker listing per request, so a result cache
    would serve stale liveness verdicts. Measured 0.20 s on the
    300 MB live-shaped copy; the endpoint is content-ETag'd and is not
    on the 2 s page tick. ``query_sessions`` serves the on-demand
    /api/sessions dump (no page tick fetches it): one bounded pass at
    0.45 s on the same copy, content-ETag'd, caching a 600 KB body per
    data version would only trade compute for memory. Anything reaching
    a cold fold outside the gateways above and this map fails the test.
    """
    return {"reconcile_numbers", "query_sessions"}


def _iter_py():
    for path in sorted(DASH.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{node.name}.{item.name}"] = item
                    out.setdefault(item.name, item)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, node)
    return out


def _called(func):
    names = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


class NoColdFoldTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.funcs = {}
        for path in _iter_py():
            try:
                found = _functions(path)
            except SyntaxError:
                continue
            for name, func in found.items():
                cls.funcs.setdefault(name, func)
        server = ast.parse((DASH / "server.py").read_text(encoding="utf-8"))
        handler = next(
            node for node in server.body if isinstance(node, ast.ClassDef) and node.name == "Handler"
        )
        cls.roots = [f"Handler.{item.name}" for item in handler.body if isinstance(item, ast.FunctionDef)]

    def _reachable_cold(self, root):
        gateways = cached_gateways() | documented_cold_paths()
        folds = cold_folds()
        seen = set()
        hits = []
        stack = [root]
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen.add(name)
            if name != root and name in gateways:
                continue
            if name != root:
                for cold in folds:
                    if name == cold or name.endswith(f".{cold}"):
                        hits.append((root, name))
            func = self.funcs.get(name)
            if func is None:
                continue
            for callee in _called(func):
                if callee in self.funcs and callee not in seen:
                    stack.append(callee)
        return hits

    def test_request_path_reaches_no_cold_fold_outside_caches(self):
        problems = []
        for root in self.roots:
            problems.extend(self._reachable_cold(root))
        self.assertEqual(
            problems,
            [],
            "request-path calls reach uncached full scans outside a cache: "
            + ", ".join(f"{root} -> {cold}" for root, cold in problems),
        )

    def test_uncached_fold_callers_are_only_their_wrappers(self):
        folds = cold_folds()
        callers: dict[str, set[str]] = {cold: set() for cold in folds}
        for name, func in self.funcs.items():
            for callee in _called(func):
                if callee in callers:
                    callers[callee].add(name)
        problems = []
        for cold, allowed in folds.items():
            for caller in sorted(callers[cold]):
                short = caller.split(".")[-1]
                if short not in allowed and caller not in allowed:
                    problems.append(f"{caller} -> {cold}")
        self.assertEqual(
            problems,
            [],
            "uncached fold called outside its wrapper: " + ", ".join(problems),
        )


class IncrementalRefoldTest(unittest.TestCase):
    def test_one_row_write_refolds_at_most_the_changed_run(self):
        from unittest import mock

        from dash.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "dash.sqlite")
            try:
                for i in range(3):
                    store.upsert_run(
                        {
                            "run_id": f"guard-r{i}",
                            "source": "claude",
                            "status": "done",
                            "session_id": f"guard-s{i}",
                        }
                    )
                    store.add_usage(
                        {
                            "ts": 1000.0 + i,
                            "session_id": f"guard-s{i}",
                            "run_id": f"guard-r{i}",
                            "model": "claude-opus-4-6",
                            "tool": "claude",
                            "message_id": f"guard-m{i}",
                            "input": 10,
                            "output": 5,
                        }
                    )
                now = 2000.0
                store.snapshot(now, "all")
                refolded = []
                real = Store._refold_runs_locked

                def spy(self, run_ids):
                    refolded.extend(run_ids)
                    return real(self, run_ids)

                with mock.patch.object(Store, "_refold_runs_locked", spy):
                    store.add_usage(
                        {
                            "ts": 1500.0,
                            "session_id": "guard-s1",
                            "run_id": "guard-r1",
                            "model": "claude-opus-4-6",
                            "tool": "claude",
                            "message_id": "guard-m9",
                            "input": 7,
                            "output": 3,
                        }
                    )
                    store.snapshot(now, "all")
                self.assertEqual(sorted(set(refolded)), ["guard-r1"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
