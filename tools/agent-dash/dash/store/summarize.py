"""Summaries, rollups, reconcile, mismatches, snapshot assembly.

Owns snapshot assembly and cache, rollups, reconcile, and
mismatch reporting. Reads merge/run-shape helpers from
``runs`` and lane helpers from ``running``; every method
runs under the facade lock.
"""

from __future__ import annotations

from dash.models import run_model_key

from .runs import (_is_manifest_twin, _nearest_twin,
                   _parent_map, _rollup_usage)
from .running import _annotate_lanes, _lane_shadows
from .util import (BILLED_FIELDS, FINISHED, MERGE_WINDOW_S,
                 RANGE_S,
                 SAMPLE_FRESH_S, TOKEN_FIELDS,
                 TOOL_KEYS, UNASSIGNED, _active, _billed_total,
                 _drop_launch_echoes, _elapsed, _epoch, _in_ws,
                 _is_counted_agent, _local_midnight, _money,
                 _tool_key, _ws_sort, _zeros,
                 is_prod_job_run)


class SummarizeMixin:
        def snapshot(
            self,
            now: float,
            ws: str = "all",
            since: float | None = None,
            until: float | None = None,
            usage_group: str | None = None,
        ) -> dict:
            """Scope snapshot with counts, totals, and (optionally) usage buckets.

            When ``usage_group`` (``model`` | ``workstream`` | ``tool``) is given,
            the hourly usage buckets for the same scope and window ride along as
            ``usage``, read under the same lock pass as the totals, so the bucket
            token sum equals ``totals`` by construction. Without it the key is
            absent and the payload is the historic shape.

            ``prev_24h`` carries the same scope's numbers for the 24 hours
            immediately before the window (``[since - 24h, since]``; the
            default window makes that ``[now - 48h, now - 24h]``), computed
            with the same bucket sums and the same agent rule as the
            windowed totals, so a delta compares like with like. ``tokens``
            is billed tokens, ``cost_usd`` the priced cost under the same
            unpriced rule, ``finished`` the finished-in-range count for that
            earlier window, and ``live_agents`` the point-in-time count of
            agents overlapping the window start (``now - 24h`` by default),
            not a peak. Pruned data reads as whatever the database holds.
            """
            if usage_group is not None and usage_group not in ("model", "workstream", "tool"):
                raise ValueError("bad usage_group")
            with self._lock:
                shown_all, parents = self._shown_locked(now)
                live_open, shadows = self._live_open_with_shadows_locked(now, shown_all)
                shown_all = _annotate_lanes(shown_all, live_open | set(shadows))
                scoped = [run for run in shown_all if _in_ws(run, ws)]
                running = [
                    run
                    for run in scoped
                    if run["status"] == "running"
                    and run["run_id"] in live_open
                    and not is_prod_job_run(run)
                ]
                running.sort(key=lambda run: run["started_at"] or 0, reverse=True)
                range_since = now - RANGE_S if since is None else float(since)
                range_until = float(now if until is None else until)
                recent_all = [
                    run
                    for run in scoped
                    if not is_prod_job_run(run)
                    and run["status"] != "running"
                    and run["ended_at"] is not None
                    and range_since <= float(run["ended_at"]) <= range_until
                ]
                recent_all.sort(key=lambda run: run["ended_at"] or 0, reverse=True)
                recent_total = len(recent_all)
                recent = recent_all[:50]
                usage_once = self._usage_by_run()
                roll = self._summarize_locked(
                    shown_all, parents, now, since, until, live_open, usage=usage_once
                )
                if ws is None or ws == "all":
                    key = "all"
                    scope = roll["all"]
                    counts = scope["counts"]
                    totals = scope["totals"]
                    by_ws = list(roll["rows"])
                    if roll["has_unassigned"]:
                        by_ws.append(roll["unassigned"])
                else:
                    key = UNASSIGNED if ws == UNASSIGNED else ws
                    scope = roll["scopes"].get(key, _empty_scope())
                    counts = scope["counts"]
                    totals = scope["totals"]
                    wanted = [roll["unassigned"]] if key == UNASSIGNED and roll["has_unassigned"] else roll["rows"]
                    by_ws = [row for row in wanted if row["ws"] == key]
                scope_ws = None if ws in (None, "all") else ws
                unpriced = self._unpriced_models_locked(range_since, range_until, scope_ws)
                totals = {
                    **totals,
                    "unpriced_tokens": sum(entry["tokens"] for entry in unpriced),
                    "unpriced_models": [entry["model"] for entry in unpriced],
                }
                response = {
                    "now": now,
                    "seq": self._seq,
                    "data_seq": self._data_seq,
                    "last_data_ts": self._last_data_ts,
                    "counts": counts,
                    "running": running,
                    "recent": recent,
                    "recent_shown": len(recent),
                    "recent_total": recent_total,
                    "totals": totals,
                    "prev_24h": dict(scope["prev_24h"]),
                    "totals_all_time": roll["all_time"][key]
                    if key in roll["all_time"]
                    else {"tokens": {field: 0 for field in TOKEN_FIELDS}, "cost_usd": 0.0},
                    "last_activity": _last_activity(scope["runs"]),
                    "by_workstream": by_ws,
                }
                if usage_group is not None:
                    # Same scope and window as the totals above, same lock pass:
                    # no writer can land between the two reads.
                    response["usage"] = self._query_usage_locked(
                        usage_group, "hour", scope_ws, roll["since"], roll["until"]
                    )
                return response
        def snapshot_key(
            self,
            ws: str = "all",
            since: float | None = None,
            until: float | None = None,
            usage_group: str | None = None,
        ) -> tuple:
            """Identity of a snapshot request for caches and ETags.

            Window edges quantize to the same 30 s grid ``snapshot_cached``
            uses, so the server's conditional tag names the exact entry the
            bytes would come from. Pair with ``(data_seq, seq)``.
            """
            return (
                ws,
                None if since is None else int(float(since) // 30),
                None if until is None else int(float(until) // 30),
                usage_group,
            )
        def snapshot_cached(
            self,
            now: float,
            ws: str = "all",
            since: float | None = None,
            until: float | None = None,
            usage_group: str | None = None,
        ) -> dict:
            """Serve reads from the snapshot cache between writes.

            Keyed by the request params plus the data sequence: reads with
            no data writes in between return the cached payload without
            recomputing, so idle ticks cost nothing. ``now``, ``seq``,
            ``data_seq`` and ``last_data_ts`` refresh on a hit; run and
            liveness lists are as fresh as the last data write, which the
            stale badge covers. (``seq`` alone is not a gate: polling
            bumps it every cycle via process samples and counts while no
            served byte changes, so gating on it would miss on every tick
            of a live server.) Misses compute one full snapshot and fill
            the cache. ``prev_24h`` rides the same entry and the same
            invalidation as every other number: no extra key, same
            staleness bound.
            The window edges quantize to a 30 s grid for the key only (the
            compute still uses the exact edges): the page's ``since`` moves
            in the same grid, and without this every tick would miss on the
            key alone. A 24 h window moving by seconds is invisible in the
            sums. See :meth:`snapshot_key` for the shared key computation.
            """
            key = self.snapshot_key(ws, since, until, usage_group)
            with self._lock:
                hit = self._snapshot_cache.get(key)
                if hit is not None and hit[0] == self._data_seq:
                    payload = dict(hit[2])
                    payload["now"] = now
                    payload["seq"] = self._seq
                    payload["data_seq"] = self._data_seq
                    payload["last_data_ts"] = self._last_data_ts
                    return payload
            payload = self.snapshot(now, ws, since, until, usage_group=usage_group)
            with self._lock:
                if len(self._snapshot_cache) > 32:
                    self._snapshot_cache.clear()
                self._snapshot_cache[key] = (self._data_seq, self._seq, dict(payload))
            return payload
        def ws_rollup(self, now: float, since: float | None = None, until: float | None = None) -> dict:
            """One locked pass over the same run list: all-scope counts and totals,
            one row per workstream, and the unassigned row.

            Row ``ws`` equals ``snapshot(now, ws, since, until)`` counts and totals.
            The rows plus unassigned sum to the all-scope numbers exactly.
            """
            with self._lock:
                shown_all, parents = self._shown_locked(now)
                live_open = self._live_open_ids_locked(now, shown_all)
                usage_once = self._usage_by_run()
                return self._summarize_locked(
                    shown_all, parents, now, since, until, live_open, usage=usage_once
                )
        def reconcile_numbers(
            self,
            now: float,
            since: float | None = None,
            until: float | None = None,
            processes=None,
            archived_ws: set[str] | None = None,
        ) -> dict:
            """Cross-check every number once, under one lock. ``mismatches`` is empty when consistent.

            ``processes`` is the live worker listing for the ``unseen worker``
            check. The store never samples processes itself, so unit tests stay
            hermetic: pass an explicit list (``[]`` for none). The HTTP layer
            (``dash/server.py``) supplies the live listing per request. A
            worker younger than two poll intervals of its source — by process
            age, or first sampled here within that window — is not yet a
            mismatch.

            ``archived_ws`` is the set of workstream ids in a
            default-hidden lifecycle state (``archived``/``dropped``; the
            HTTP layer reads it from the bundles). When given, the payload
            gains ``archived`` (those rows summed, windowed plus
            lifetime) and the identity visible rows + archived +
            unassigned = all is checked like every other invariant. When
            None the payload keeps its historic shape.
            """
            from dash.workstates import archived_aggregate as _archived_aggregate
            from dash.workstates import check_archived_identity as _check_archived

            with self._lock:
                shown_all, parents = self._shown_locked(now)
                live_open = self._live_open_ids_locked(now, shown_all)
                usage_once = self._usage_by_run()
                roll = self._summarize_locked(
                    shown_all, parents, now, since, until, live_open, usage=usage_once
                )
                everything = list(roll["rows"]) + [roll["unassigned"]]
                mismatches = self._mismatches_locked(
                    roll,
                    shown_all,
                    now,
                    lambda ws: _scope_tab(roll, ws),
                    processes,
                )
                all_counts = roll["all"]["counts"]
                all_totals = roll["all"]["totals"]
                all_time = roll["all_time"]
                all_time_total = _billed_total(all_time["all"]["tokens"])
                # The Finished table shows the newest 50 and the page names the
                # truncation ("Newest 50 of 1,452"), so a large finished count is
                # visible, not a mismatch; the counts feed that subtitle.
                finished_total = all_counts["finished_in_range"]
                recent_shown = min(50, finished_total)
                payload = {
                    "now": now,
                    "since": roll["since"],
                    "until": roll["until"],
                    "prev_since": roll["prev_since"],
                    "prev_until": roll["prev_until"],
                    "all": {
                        "live_agents": all_counts["live_agents"],
                        "finished_in_range": all_counts["finished_in_range"],
                        "finished_today": all_counts["finished_today"],
                        "tokens": _billed_total(all_totals["tokens"]),
                        "cost_usd": all_totals["cost_usd"],
                        "prev_24h": dict(roll["all"]["prev_24h"]),
                    },
                    "all_time": {
                        "tokens": all_time_total,
                        "cost_usd": all_time["all"]["cost_usd"],
                    },
                    "rows": roll["rows"],
                    "unassigned": roll["unassigned"],
                    "recent_shown": recent_shown,
                    "recent_total": finished_total,
                    "mismatches": mismatches,
                }
                if archived_ws is not None:
                    archived = _archived_aggregate(roll["rows"], set(archived_ws))
                    payload["archived"] = archived
                    mismatches.extend(
                        _check_archived(
                            roll["rows"],
                            roll["unassigned"],
                            archived,
                            _billed_total(all_totals["tokens"]),
                            all_totals["cost_usd"],
                            all_time_total,
                            all_time["all"]["cost_usd"],
                        )
                    )
                return payload
        def _mismatches_locked(self, roll: dict, shown_all: list, now: float, tab_of, processes=None) -> list:
            """Every violated invariant for one rollup. Caller holds the lock.

            ``tab_of(ws)`` returns ``(counts, totals, all_time)`` from the same
            roll's already-computed scopes (``all_time`` is None when the roll
            holds no all-time scope for ``ws``); both callers pass
            ``_scope_tab`` over their own roll, so both agree with each other
            and emit ``/api/reconcile`` order: row sums, previous-window row
            sums (tokens to the token, cost to the cent), per-row tabs,
            all-time sums, unpriced models, stale running, unseen workers,
            then duplicate runs. ``processes`` is the live worker listing for
            the ``unseen worker`` check (``[]`` for none).
            """
            everything = list(roll["rows"]) + [roll["unassigned"]]
            all_counts = roll["all"]["counts"]
            all_totals = roll["all"]["totals"]
            mismatches: list[str] = []
            for metric in ("live_agents", "finished_in_range", "finished_today", "tokens"):
                if metric == "tokens":
                    expect: int = _billed_total(all_totals["tokens"])
                else:
                    expect = all_counts[metric]
                total = sum(row[metric] for row in everything)
                if total != expect:
                    mismatches.append(f"{metric}: workstream rows sum to {total}, overview shows {expect}")
            if abs(sum(row["cost_usd"] for row in everything) - all_totals["cost_usd"]) > 1e-6:
                mismatches.append(
                    "cost_usd: workstream rows sum to "
                    f"{sum(row['cost_usd'] for row in everything):.6f}, "
                    f"overview shows {all_totals['cost_usd']:.6f}"
                )
            prev_all = roll["all"].get("prev_24h") or {}
            prev_tokens_total = sum((row.get("prev_24h") or {}).get("tokens", 0) for row in everything)
            if prev_tokens_total != (prev_all.get("tokens", 0) or 0):
                mismatches.append(
                    "prev_24h_tokens: workstream rows sum to "
                    f"{prev_tokens_total}, "
                    f"overview shows {prev_all.get('tokens', 0)}"
                )
            prev_cost_total = sum((row.get("prev_24h") or {}).get("cost_usd", 0.0) for row in everything)
            if round(prev_cost_total, 2) != round(prev_all.get("cost_usd", 0.0) or 0.0, 2):
                mismatches.append(
                    "prev_24h_cost_usd: workstream rows sum to "
                    f"{prev_cost_total:.6f}, "
                    f"overview shows {(prev_all.get('cost_usd', 0.0) or 0.0):.6f}"
                )
            for row in roll["rows"]:
                tab_counts, tab_totals, tab_all_time = tab_of(row["ws"])
                for metric in ("live_agents", "finished_in_range", "finished_today"):
                    if row[metric] != tab_counts[metric]:
                        mismatches.append(
                            f"{row['ws']} {metric}: overview row shows {row[metric]}, "
                            f"tab shows {tab_counts[metric]}"
                        )
                if row["tokens"] != _billed_total(tab_totals["tokens"]):
                    mismatches.append(
                        f"{row['ws']} tokens: overview row shows {row['tokens']}, "
                        f"tab shows {_billed_total(tab_totals['tokens'])}"
                    )
                if abs(row["cost_usd"] - tab_totals["cost_usd"]) > 1e-6:
                    mismatches.append(
                        f"{row['ws']} cost_usd: overview row shows {row['cost_usd']:.6f}, "
                        f"tab shows {tab_totals['cost_usd']:.6f}"
                    )
                if tab_all_time is not None:
                    if row["tokens_all_time"] != _billed_total(tab_all_time["tokens"]):
                        mismatches.append(
                            f"{row['ws']} tokens_all_time: overview row shows {row['tokens_all_time']}, "
                            f"tab shows {_billed_total(tab_all_time['tokens'])}"
                        )
                    if abs(row["cost_usd_all_time"] - tab_all_time["cost_usd"]) > 1e-6:
                        mismatches.append(
                            f"{row['ws']} cost_usd_all_time: overview row shows {row['cost_usd_all_time']:.6f}, "
                            f"tab shows {tab_all_time['cost_usd']:.6f}"
                        )
            all_time = roll["all_time"]
            all_time_total = _billed_total(all_time["all"]["tokens"])
            if sum(row["tokens_all_time"] for row in everything) != all_time_total:
                mismatches.append(
                    "tokens_all_time: workstream rows sum to "
                    f"{sum(row['tokens_all_time'] for row in everything)}, "
                    f"overview shows {all_time_total}"
                )
            if (
                abs(
                    sum(row["cost_usd_all_time"] for row in everything)
                    - all_time["all"]["cost_usd"]
                )
                > 1e-6
            ):
                mismatches.append(
                    "cost_usd_all_time: workstream rows sum to "
                    f"{sum(row['cost_usd_all_time'] for row in everything):.6f}, "
                    f"overview shows {all_time['all']['cost_usd']:.6f}"
                )
            hours = (roll["until"] - roll["since"]) / 3600 if roll["until"] > roll["since"] else 0
            for entry in self._unpriced_models_locked(roll["since"], roll["until"], None):
                mismatches.append(
                    f"unpriced model: {entry['model']} ({entry['tokens']} tokens in {hours:g} h)"
                )
            for run, reason in self._stale_locked(now, shown_all):
                mismatches.append(f"stale running: {run.get('run_id')} ({reason})")
            mismatches.extend(self._unseen_workers_locked(now, shown_all, processes or []))
            mismatches.extend(
                _duplicate_run_mismatches([dict(row) for row in self._all_runs()])
            )
            return mismatches
        def _shown_locked(self, now: float) -> tuple[list, dict]:
            """All run views with child chats folded away and launch echoes dropped. Caller holds the lock."""
            raw, usage, parents = self._scoped_usage()
            context = self._context_by_run()
            rss = self._rss_maps(now)
            views = [self._run_view(row, now, usage, rss, context) for row in raw]
            shown = _drop_launch_echoes([view for view in views if view["run_id"] not in parents])
            return shown, parents
        def _summarize_locked(self, shown_all: list, parents: dict, now: float, since, until, live_open=None, usage=None) -> dict:
            """Counts, totals, and per-workstream rows from one run list. Caller holds the lock.

            Token and cost sums come from the same hourly usage buckets ``/api/usage``
            reads, counting a bucket when its hour overlaps ``[since, until]``. The
            tokens tile and the tokens-per-hour chart legend sum the same cells, so
            all three agree exactly. Live counts use ``live_open`` (the one filtered
            running set); when None it is computed here. ``usage`` is the folded
            per-run usage for unpriced checks; one call per snapshot, shared by
            every scope, so the snapshot costs O(scopes) Python filtering instead
            of O(scopes) full usage scans.
            """
            if live_open is None:
                live_open = self._live_open_ids_locked(now, shown_all)
            if usage is None:
                usage = self._usage_by_run()
            range_since = now - RANGE_S if since is None else float(since)
            range_until = float(now if until is None else until)
            cells = self._query_usage_locked("workstream", "hour", None, range_since, range_until)
            buckets: dict[str, list] = {}
            for cell in cells:
                buckets.setdefault(cell["key"], []).append(cell)
            scopes: dict[str, dict] = {}
            scopes["all"] = _scope_numbers(
                shown_all, parents, usage, now, since, until, range_since, range_until,
                [cell for group in buckets.values() for cell in group],
                live_open,
            )
            workstreams = sorted({run.get("workstream") or UNASSIGNED for run in shown_all}, key=_ws_sort)
            if UNASSIGNED not in workstreams:
                workstreams.append(UNASSIGNED)
            for ws in workstreams:
                members = [run for run in shown_all if (run.get("workstream") or UNASSIGNED) == ws]
                scopes[ws] = _scope_numbers(
                    members, parents, usage, now, since, until, range_since, range_until,
                    buckets.get(ws, []),
                    live_open,
                )
            # The 24 hours immediately before the window, per scope, with the
            # same bucket sums and the same agent rule as the windowed
            # numbers, so a delta compares like with like. Tokens and cost
            # come from the same hourly cells over the earlier window (same
            # priced-cost fallback, so the same unpriced rule); finished is
            # the same finished-in-range count over that window; live is the
            # point-in-time overlap at the window start, not a peak.
            prev_since = range_since - RANGE_S
            prev_until = range_since
            prev_cells = self._query_usage_locked("workstream", "hour", None, prev_since, prev_until)
            prev_buckets: dict[str, list] = {}
            for cell in prev_cells:
                prev_buckets.setdefault(cell["key"], []).append(cell)
            live_at_start = _live_at_point_ids(shown_all, parents, prev_until, now)
            scopes["all"]["prev_24h"] = _prev_24h(
                shown_all, parents, now, prev_since, prev_until, prev_cells, live_open, live_at_start,
            )
            for ws in workstreams:
                members = [run for run in shown_all if (run.get("workstream") or UNASSIGNED) == ws]
                scopes[ws]["prev_24h"] = _prev_24h(
                    members, parents, now, prev_since, prev_until, prev_buckets.get(ws, []), live_open, live_at_start,
                )
            # All-time tokens and cost per scope from the same bucket cells with no
            # window, summed by the same helper as the windowed numbers.
            everything = self._query_usage_locked("workstream", "hour", None, None, None)
            timeless: dict[str, list] = {}
            for cell in everything:
                timeless.setdefault(cell["key"] or UNASSIGNED, []).append(cell)
            all_time: dict[str, dict] = {}
            all_time["all"] = _sum_cells(everything)
            for ws in workstreams:
                all_time[ws] = _sum_cells(timeless.get(ws, []))
            rows = [_ws_row(ws, scopes[ws], all_time[ws]) for ws in workstreams if ws != UNASSIGNED]
            unassigned = _ws_row(UNASSIGNED, scopes[UNASSIGNED], all_time[UNASSIGNED])
            return {
                "since": range_since,
                "until": range_until,
                "prev_since": prev_since,
                "prev_until": prev_until,
                "all": scopes["all"],
                "scopes": scopes,
                "rows": rows,
                "unassigned": unassigned,
                "has_unassigned": bool(scopes[UNASSIGNED]["runs"]),
                "all_time": all_time,
            }
        def _scoped_usage(self):
            """Usage with child-chat tokens folded onto the parent run.

            Caller holds the lock. Child rows stay in ``raw``; ``parents`` maps
            a child run id to the run that should carry its tokens.
            """
            raw = [dict(row) for row in self._all_runs()]
            parents = _parent_map(raw)
            usage = _rollup_usage(self._usage_by_run(), parents)
            return raw, usage, parents
        def _rss_maps(self, now: float) -> tuple[dict, dict]:
            rows = self._conn.execute(
                """SELECT pid, rss_kb, run_id, ts FROM process_samples
                   WHERE ts >= ? ORDER BY ts DESC""",
                (now - SAMPLE_FRESH_S,),
            ).fetchall()
            by_run = {}
            by_pid = {}
            for row in rows:
                if row["run_id"] and row["run_id"] not in by_run:
                    by_run[row["run_id"]] = row["rss_kb"]
                if row["pid"] not in by_pid:
                    by_pid[row["pid"]] = row["rss_kb"]
            return by_run, by_pid
        def _run_view(self, row, now: float, usage: dict, rss: tuple[dict, dict], context: dict | None = None) -> dict:
            data = dict(row)
            present = data["run_id"] in usage
            folded, cost, _unpriced = usage.get(data["run_id"], (_zeros(), 0.0, False))
            by_run, by_pid = rss
            rss_kb = by_run.get(data["run_id"])
            if rss_kb is None and data.get("pid") is not None:
                rss_kb = by_pid.get(data["pid"])
            if present:
                tokens = {key: folded.get(key) for key in BILLED_FIELDS}
                cost_usd = None if cost is None else _money(cost)
            else:
                tokens = {key: 0 for key in BILLED_FIELDS}
                cost_usd = 0.0
            return {
                "run_id": data["run_id"],
                "source": data.get("source"),
                "tier": data.get("tier"),
                "model": data.get("model"),
                "label": data.get("label"),
                "workstream": data.get("workstream"),
                "ws_confidence": data.get("ws_confidence"),
                "session_id": data.get("session_id"),
                "parent_session_id": data.get("parent_session_id"),
                "status": data.get("status"),
                "verdict": data.get("verdict"),
                "started_at": data.get("started_at"),
                "ended_at": data.get("ended_at"),
                "elapsed_s": _elapsed(data, now),
                "clock_source": data.get("clock_source"),
                "out_bytes": data.get("out_bytes"),
                "out_growing": bool(data.get("out_growing")),
                "err_bytes": data.get("err_bytes"),
                "pid": data.get("pid"),
                "rss_kb": rss_kb,
                "tokens": tokens,
                "cost_usd": cost_usd,
                "context_tokens": None if not context or data["run_id"] not in context else context[data["run_id"]],
            }


def _duplicate_run_mismatches(runs: list, window_s: float = MERGE_WINDOW_S) -> list:
    """One mismatch per launcher-manifest row with an unmerged twin.

    Two rows with the same label overlapping in time from the same source
    are one run recorded twice: the manifest row the launcher wrote plus
    the worker's own row. The merge (``merge_manifest_rows``, per source
    family) folds them; whatever it leaves behind is reported here so it
    gets fixed instead of double-counted. Non-manifest siblings that share
    a label (a relaunch, a probe, workflow children) are legitimate and
    never flagged. The rule: a manifest row the merge would fold (a
    same-label non-manifest twin starting within ``window_s``) is skipped,
    because the next merge pass folds it; only a surviving manifest row
    with an overlapping same-source twin the merge would NOT fold, and not
    shadow-linked to it, is a mismatch.
    """
    by_key: dict = {}
    for row in runs:
        label = row.get("label")
        source = row.get("source")
        if not label or not source:
            continue
        by_key.setdefault((source, label), []).append(row)
    found = []
    for rows in by_key.values():
        manifests = [row for row in rows if _is_manifest_twin(row)]
        if not manifests:
            continue
        others = [row for row in rows if not _is_manifest_twin(row)]
        if not others:
            continue
        for manifest in manifests:
            if _nearest_twin(manifest, others, window_s) is not None:
                continue
            for twin in others:
                if twin.get("run_id") == manifest.get("run_id"):
                    continue
                if twin.get("parent_session_id") == manifest.get("run_id"):
                    continue
                if manifest.get("parent_session_id") == twin.get("run_id"):
                    continue
                if _overlap(manifest, twin):
                    found.append(
                        "duplicate run: %s overlaps %s (same label %r)"
                        % (manifest.get("run_id"), twin.get("run_id"), manifest.get("label"))
                    )
                    break
    return found


def _overlap(first: dict, second: dict) -> bool:
    start = _epoch(first.get("started_at"))
    end = _epoch(first.get("ended_at"))
    other_start = _epoch(second.get("started_at"))
    other_end = _epoch(second.get("ended_at"))
    low = float("-inf") if start is None else start
    high = float("inf") if end is None else end
    other_low = float("-inf") if other_start is None else other_start
    other_high = float("inf") if other_end is None else other_end
    return low <= other_high and other_low <= high


def _run_overlaps_window(run: dict, since: float, until: float, now: float) -> bool:
    """Same overlap rule as the run list: a run with no end time is still open."""
    if (run.get("ended_at") if run.get("ended_at") is not None else now) < since:
        return False
    return (run.get("started_at") or 0) <= until


def _scope_numbers(
    runs: list,
    parents: dict,
    usage: dict,
    now: float,
    since,
    until,
    range_since: float,
    range_until: float,
    cells: list,
    live_open=None,
) -> dict:
    """Counts and totals for one scope (all, one workstream, or unassigned).

    Counts use the agent rule (lead sessions and Dagster jobs are not agents)
    and the window. Token and cost sums add up the ``/api/usage`` cells for the
    same window, so the snapshot, the tiles, and the chart legend agree.
    Live counts use ``live_open`` (the one filtered running set).
    """
    counts = _agent_counts(runs, parents, now, since, until, live_open)
    summed = _sum_cells(cells)
    unpriced_runs = sum(
        1
        for run in runs
        if _run_overlaps_window(run, range_since, range_until, now)
        and _usage_unpriced(usage.get(run["run_id"]))
    )
    return {
        "runs": runs,
        "counts": counts,
        "totals": {
            "tokens": summed["tokens"],
            "cost_usd": summed["cost_usd"],
            "unpriced_runs": unpriced_runs,
        },
    }


def _sum_cells(cells: list) -> dict:
    """Token and cost sums over ``/api/usage`` cells, windowed or not.

    Both the windowed snapshot totals and the all-time totals go through
    this one helper, so they reconcile by construction.
    """
    tokens = {key: 0 for key in TOKEN_FIELDS}
    for cell in cells:
        for key in TOKEN_FIELDS:
            tokens[key] += cell.get(key) or 0
    priced = [
        cell["priced_cost_usd"] if cell["priced_cost_usd"] is not None else cell["cost_usd"]
        for cell in cells
    ]
    priced = [value for value in priced if value is not None]
    return {"tokens": tokens, "cost_usd": _money(sum(priced)) if priced else 0.0}


def _usage_unpriced(entry) -> bool:
    return bool(entry) and bool(entry[2])


def _last_activity(runs: list):
    last = 0
    for run in runs:
        for stamp in (run.get("ended_at"), run.get("started_at")):
            if stamp and stamp > last:
                last = stamp
    return last or None


def _scope_tab(roll: dict, ws: str):
    """``(counts, totals, all_time)`` for one workstream from its own roll.

    Reads the already-computed scopes, exactly as ``reconcile_numbers``
    did before the ``_mismatches_locked`` refactor; ``all_time`` is None
    when the roll holds no all-time scope for ``ws`` (the check skips
    those rows, it never compares against zeros).
    """
    scope = roll["scopes"].get(ws, _empty_scope())
    return scope["counts"], scope["totals"], roll["all_time"].get(ws)


def _zero_prev() -> dict:
    """Empty previous-window numbers, mirroring the ``prev_24h`` shape."""
    return {"tokens": 0, "cost_usd": 0.0, "finished": 0, "live_agents": 0}


def _prev_24h(members: list, parents: dict, now: float, prev_since: float, prev_until: float, prev_cells: list, live_open, live_at_start: set) -> dict:
    """Previous-window numbers for one scope, like with like.

    Token and cost sums go through the same ``_sum_cells`` helper over the
    earlier window's bucket cells (same priced-cost fallback, so the same
    unpriced rule); ``finished`` is the same ``_agent_counts``
    finished-in-range count over that window (agent runs only: lead
    sessions, shell rows, and Dagster jobs are not agents);
    ``live_agents`` is the point-in-time count of those members overlapping
    the window start, one lane one agent, not a peak.
    """
    finished = _agent_counts(members, parents, now, prev_since, prev_until, live_open)["finished_in_range"]
    summed = _sum_cells(prev_cells)
    return {
        "tokens": _billed_total(summed["tokens"]),
        "cost_usd": summed["cost_usd"],
        "finished": finished,
        "live_agents": _point_live_count(members, live_at_start),
    }


def _live_at_point_ids(runs: list, parents: dict, t0: float, now: float) -> set:
    """Run ids live at the point ``t0``, minus lane shadows.

    A counted agent overlaps ``t0`` by the same rule the concurrency chart
    uses (``_active``: started at or before ``t0``, still open past it); a
    ``claude-workflow`` wrapper shadowed by a live worker row on the same
    label counts once, the worker, exactly as the live-open set does today.
    Runs with no start time are never live at a point.
    """
    live = {
        run["run_id"]
        for run in runs
        if run["run_id"] not in parents
        and _is_counted_agent(run)
        and _active(run, t0, now)
    }
    return live - set(_lane_shadows(runs, live))


def _point_live_count(members: list, live_ids: set) -> int:
    """Point-in-time live agents for one scope's members.

    Same one-lane-one-agent dedup as the live count: two rows for one
    ``(tool, label)`` lane count once.
    """
    seen = set()
    count = 0
    for run in members:
        if run["run_id"] not in live_ids:
            continue
        key = (_tool_key(run), run.get("label") or run["run_id"])
        if key in seen:
            continue
        seen.add(key)
        count += 1
    return count


def _ws_row(ws: str, scope: dict, all_time: dict | None = None) -> dict:
    row = {
        "ws": ws,
        "live_agents": scope["counts"]["live_agents"],
        "finished_in_range": scope["counts"]["finished_in_range"],
        "finished_today": scope["counts"]["finished_today"],
        "tokens": _billed_total(scope["totals"]["tokens"]),
        "cost_usd": scope["totals"]["cost_usd"],
        "prev_24h": dict(scope.get("prev_24h") or _zero_prev()),
        "last_activity": _last_activity(scope["runs"]),
        "ws_confidence": sorted(
            {
                run.get("ws_confidence")
                for run in scope["runs"]
                if run.get("ws_confidence")
            }
        ),
    }
    if all_time is not None:
        row["tokens_all_time"] = _billed_total(all_time["tokens"])
        row["cost_usd_all_time"] = all_time["cost_usd"]
    return row


def _empty_scope() -> dict:
    return {
        "runs": [],
        "counts": {
            "live_agents": 0,
            "live_by_tool": {key: 0 for key in TOOL_KEYS},
            "live_by_model": {},
            "prod_jobs": 0,
            "prod_jobs_finished": 0,
            "finished_today": 0,
            "finished_in_range": 0,
            "finished_by_verdict": {},
        },
        "totals": {
            "tokens": {key: 0 for key in TOKEN_FIELDS},
            "cost_usd": 0.0,
            "unpriced_runs": 0,
        },
        "prev_24h": _zero_prev(),
    }


def _agent_counts(runs: list, parents: dict, now: float, since, until, live_open=None) -> dict:
    range_since = now - RANGE_S if since is None else float(since)
    range_until = float(now if until is None else until)
    today = _local_midnight(now)
    live_by = {key: 0 for key in TOOL_KEYS}
    live_by_model: dict = {}
    seen = set()
    live_n = 0
    prod_jobs = 0
    prod_jobs_finished = 0
    finished_today = 0
    finished_range = 0
    by_verdict = {}
    for run in runs:
        if run["run_id"] in parents:
            continue
        if run.get("source") == "dagster" and run.get("status") in ("running", "queued"):
            if live_open is None or run["run_id"] in live_open:
                prod_jobs += 1
        # Dagster terminal rows carry status "finished" (verdict SUCCESS /
        # FAILURE / CANCELED), outside the agent FINISHED tuple, so the
        # finished-jobs count uses its own predicate: any closed job row
        # with an end time inside the window.
        if (
            run.get("source") == "dagster"
            and run.get("status") != "running"
            and run.get("ended_at") is not None
            and range_since <= float(run["ended_at"]) <= range_until
        ):
            prod_jobs_finished += 1
        if not _is_counted_agent(run):
            continue
        if run.get("status") == "running":
            if live_open is not None and run["run_id"] not in live_open:
                continue
            tool = _tool_key(run)
            key = (tool, run.get("label") or run["run_id"])
            if key in seen:
                continue
            seen.add(key)
            live_by[tool] += 1
            model = run_model_key(run)
            live_by_model[model] = live_by_model.get(model, 0) + 1
            live_n += 1
            continue
        if run.get("status") not in FINISHED or run.get("ended_at") is None:
            continue
        ended = float(run["ended_at"])
        if today <= ended <= now:
            finished_today += 1
        if range_since <= ended <= range_until:
            finished_range += 1
            verdict = run.get("verdict") or "none"
            by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
    return {
        "live_agents": live_n,
        "live_by_tool": live_by,
        "live_by_model": live_by_model,
        "prod_jobs": prod_jobs,
        "prod_jobs_finished": prod_jobs_finished,
        "finished_today": finished_today,
        "finished_in_range": finished_range,
        "finished_by_verdict": by_verdict,
    }
