"""Summaries, rollups, reconcile, mismatches, snapshot assembly.

Owns snapshot assembly and cache, rollups, reconcile, and
mismatch reporting. Reads merge/run-shape helpers from
``runs`` and lane helpers from ``running``; every method
runs under the facade lock.
"""

from __future__ import annotations

import copy as _copy

from dash import perfmon as _perf
from dash.models import HARNESS_NAMES, harness_of, run_model_key
from dash.outcomes import outcome_class, outcome_reason

from .runs import (_is_manifest_twin, _nearest_twin,
                   _parent_map, _rollup_usage)
from .running import _annotate_lanes, _lane_shadows
from .util import (BILLED_FIELDS, FINISHED, MERGE_WINDOW_S,
                 RANGE_S,
                 SAMPLE_FRESH_S, TOKEN_FIELDS,
                 TOOL_KEYS, UNASSIGNED, _active, _billed_total,
                 _drop_launch_echoes, _elapsed, _epoch,
                 _is_counted_agent, _local_midnight, _money,
                 _tool_key, _ws_sort, _zeros,
                 is_marker_run)


class SummarizeMixin:
        def snapshot(
            self,
            now: float,
            ws: str = "all",
            since: float | None = None,
            until: float | None = None,
            usage_group: str | None = None,
            recent_limit: int = 50,
            recent_offset: int = 0,
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

            ``counts["unattributed"]`` (``{"runs", "tokens", "cost_usd"}``)
            is additive only: counted agents (live now plus finished in the
            window) whose model key ends ``-unknown`` — the same rule the
            page labels "model not recorded" — with their windowed billed
            tokens and priced cost. Totals still include them; the page
            only folds their listed entries behind its default-off toggle.
            """
            if usage_group is not None and usage_group not in ("model", "workstream", "tool"):
                raise ValueError("bad usage_group")
            if isinstance(recent_limit, bool) or not isinstance(recent_limit, int) or not 1 <= recent_limit <= 5000:
                raise ValueError("bad limit")
            if isinstance(recent_offset, bool) or not isinstance(recent_offset, int) or recent_offset < 0:
                raise ValueError("bad recent_offset")
            probes = self._probe_liveness(file_mtimes=True)
            check = self._probe_checker(probes)
            with self._lock:
                shown_all, parents = self._shown_locked(now, probes)
                live_open, shadows = self._live_open_with_shadows_locked(
                    now, shown_all, check
                )
                shown_all = _annotate_lanes(shown_all, live_open | set(shadows))
                range_since = now - RANGE_S if since is None else float(since)
                range_until = float(now if until is None else until)
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
                # Running and recent ride the same grouped counting pass
                # as every other scope number (one walk, not three): only
                # the requested scope's lists sort here.
                running = sorted(
                    scope.get("running", []),
                    key=lambda run: run["started_at"] or 0, reverse=True)
                recent_all = sorted(
                    scope.get("recent", []),
                    key=lambda run: run["ended_at"] or 0, reverse=True)
                recent_total = len(recent_all)
                # Paged window over the full newest-first list: the default
                # page (offset 0, limit 50) is the historic shape, and the
                # total stays true no matter which page is served.
                recent = recent_all[recent_offset:recent_offset + recent_limit]
                scope_ws = None if ws in (None, "all") else ws
                # Own dict for the additive field below: the snapshot cache
                # keeps this payload, so it must not alias the roll.
                counts = dict(counts)
                counts["unattributed"] = self._unattributed_locked(
                    counts, recent_all, parents, scope_ws, roll["since"], roll["until"]
                )
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
                    "view_seq": self._view_seq,
                    "last_data_ts": self._last_data_ts,
                    "counts": counts,
                    "running": running,
                    "recent": recent,
                    "recent_shown": len(recent),
                    "recent_total": recent_total,
                    "recent_limit": recent_limit,
                    "recent_offset": recent_offset,
                    "totals": totals,
                    "prev_24h": dict(scope["prev_24h"]),
                    "totals_all_time": roll["all_time"][key]
                    if key in roll["all_time"]
                    else {"tokens": {field: 0 for field in TOKEN_FIELDS}, "cost_usd": 0.0},
                    "last_activity": scope.get("last_activity"),
                    "by_workstream": by_ws,
                }
                if usage_group is not None:
                    # Same scope and window as the totals above, same lock pass:
                    # no writer can land between the two reads.
                    response["usage"] = self._query_usage_locked(
                        usage_group, "hour", scope_ws, roll["since"], roll["until"]
                    )
                return response
        def _unattributed_locked(self, counts, recent_all, parents, scope_ws, range_since, range_until) -> dict:
            """Runs with tokens or verdicts whose model was never recorded.

            The one place that builds the additive ``counts["unattributed"]``
            field (``{"runs", "tokens", "cost_usd"}``). ``runs`` counts
            counted agents — live now (straight from ``live_by_model``, so
            the same dedup the tile caption hides) plus finished in the
            window (the same agent rule, status set, and parent-skip the
            finished count uses) — whose model key ends ``-unknown``, the
            same rule ``static/models.js`` labels "model not recorded".
            ``tokens`` and ``cost_usd`` sum the model-grouped usage cells
            with such keys over the same window, through the same
            ``_sum_cells`` helper as the totals, so the hidden share
            reconciles with the tile and the chart legend. Caller holds
            the lock. Cached with the snapshot payload, so the
            conditional-GET identity (key plus data sequence) covers it.
            """
            live = sum(
                num
                for key, num in (counts.get("live_by_model") or {}).items()
                if key.split("@", 1)[0].endswith("-unknown")
            )
            finished = 0
            for run in recent_all:
                if run["run_id"] in parents:
                    continue
                if not _is_counted_agent(run):
                    continue
                if run.get("status") not in FINISHED:
                    continue
                if run_model_key(run).endswith("-unknown"):
                    finished += 1
            cells = self._query_usage_locked("model", "hour", scope_ws, range_since, range_until)
            summed = _sum_cells(
                [cell for cell in cells if (cell.get("key") or "").split("@", 1)[0].endswith("-unknown")]
            )
            return {
                "runs": live + finished,
                "tokens": _billed_total(summed["tokens"]),
                "cost_usd": summed["cost_usd"],
            }

        def snapshot_key(
            self,
            ws: str = "all",
            since: float | None = None,
            until: float | None = None,
            usage_group: str | None = None,
            recent_limit: int = 50,
            recent_offset: int = 0,
        ) -> tuple:
            """Identity of a snapshot request for caches and ETags.

            Window edges quantize to the same 30 s grid ``snapshot_cached``
            uses, so the server's conditional tag names the exact entry the
            bytes would come from. Pair with ``(data_seq, seq)``. The recent
            page rides the same key: one page never serves another's bytes.
            """
            return (
                ws,
                None if since is None else int(float(since) // 30),
                None if until is None else int(float(until) // 30),
                usage_group,
                recent_limit,
                recent_offset,
            )
        def snapshot_cached(
            self,
            now: float,
            ws: str = "all",
            since: float | None = None,
            until: float | None = None,
            usage_group: str | None = None,
            recent_limit: int = 50,
            recent_offset: int = 0,
        ) -> dict:
            """Serve reads from the snapshot cache between writes.

            Keyed by the request params plus the data sequence: reads with
            no data writes in between return the cached payload without
            recomputing, so idle ticks cost nothing. ``now``, ``seq``,
            ``data_seq``, ``view_seq`` and ``last_data_ts`` refresh on a
            hit; run and
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
            key = self.snapshot_key(ws, since, until, usage_group, recent_limit, recent_offset)
            with self._lock:
                hit = self._snapshot_cache.get(key)
                if hit is not None and hit[0] == self._data_seq:
                    _perf.record_cache("snapshot", "hit")
                    payload = dict(hit[2])
                    payload["now"] = now
                    payload["seq"] = self._seq
                    payload["data_seq"] = self._data_seq
                    payload["view_seq"] = self._view_seq
                    payload["last_data_ts"] = self._last_data_ts
                    return payload
            payload = self.snapshot(now, ws, since, until, usage_group=usage_group, recent_limit=recent_limit, recent_offset=recent_offset)
            with self._lock:
                if len(self._snapshot_cache) > 32:
                    self._snapshot_cache.clear()
                self._snapshot_cache[key] = (self._data_seq, self._seq, dict(payload))
                _perf.record_cache("snapshot", "miss")
            return payload
        def ws_rollup(self, now: float, since: float | None = None, until: float | None = None) -> dict:
            """One locked pass over the same run list: all-scope counts and totals,
            one row per workstream, and the unassigned row.

            Row ``ws`` equals ``snapshot(now, ws, since, until)`` counts and totals.
            The rows plus unassigned sum to the all-scope numbers exactly.
            Served from a result cache keyed by the data sequence and a
            one-minute quantum: reads with no data writes in between get a
            copy without recomputing, so repeat archive loads cost a
            deepcopy, not a full pass. Callers must not mutate the
            result; the hit path copies before returning.
            """
            key = (
                self._data_seq,
                None if since is None else int(float(since) // 60),
                None if until is None else int(float(until) // 60),
                int(float(now) // 60),
            )
            probes = self._probe_liveness(file_mtimes=True)
            check = self._probe_checker(probes)
            with self._lock:
                hit = self._rollup_cache.get(key)
                if hit is not None:
                    _perf.record_cache("rollup", "hit")
                    return _copy.deepcopy(hit)
                shown_all, parents = self._shown_locked(now, probes)
                live_open = self._live_open_ids_locked(now, shown_all, check)
                usage_once = self._usage_by_run()
                roll = self._summarize_locked(
                    shown_all, parents, now, since, until, live_open, usage=usage_once
                )
                if len(self._rollup_cache) > 16:
                    self._rollup_cache.clear()
                self._rollup_cache[key] = roll
                _perf.record_cache("rollup", "miss")
                return _copy.deepcopy(roll)
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

            probes = self._probe_liveness(file_mtimes=True)
            check = self._probe_checker(probes)
            with self._lock:
                shown_all, parents = self._shown_locked(now, probes)
                # One evidence pass shared by the live-open set below and
                # the stale leg inside _mismatches_locked (same open rows,
                # same GROUP BYs, computed once).
                _umax, _smax, _pstarts = self._open_evidence_locked(shown_all)
                live_open = self._live_open_ids_locked(
                    now, shown_all, check, _umax, _smax, _pstarts
                )
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
                    evidence=(_umax, _smax, _pstarts),
                    liveness=probes,
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
        def _mismatches_locked(
            self, roll: dict, shown_all: list, now: float, tab_of,
            processes=None, evidence=None, liveness=None,
        ) -> list:
            """Every violated invariant for one rollup. Caller holds the lock.

            ``tab_of(ws)`` returns ``(counts, totals, all_time)`` from the same
            roll's already-computed scopes (``all_time`` is None when the roll
            holds no all-time scope for ``ws``); both callers pass
            ``_scope_tab`` over their own roll, so both agree with each other
            and emit ``/api/reconcile`` order: row sums, previous-window row
            sums (tokens to the token, cost to the cent), per-row tabs,
            all-time sums, unpriced models, stale running, unseen workers,
            then duplicate runs. ``processes`` is the live worker listing for
            the ``unseen worker`` check (``[]`` for none). ``evidence`` is the
            caller's shared liveness pass ``(usage_max, sample_max,
            pid_starts)`` over ``shown_all``; the stale leg reuses it instead
            of issuing the same GROUP BYs again.
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
            checker = self._probe_checker(liveness) if liveness is not None else None
            for run, reason in self._stale_locked(
                now, shown_all, checker,
                usage_max=None if evidence is None else evidence[0],
                sample_max=None if evidence is None else evidence[1],
                pid_starts=None if evidence is None else evidence[2],
            ):
                mismatches.append(f"stale running: {run.get('run_id')} ({reason})")
            mismatches.extend(self._unseen_workers_locked(now, shown_all, processes or []))
            mismatches.extend(
                _duplicate_run_mismatches([dict(row) for row in self._all_runs()])
            )
            return mismatches
        def _frag_key(self, row: dict, usage_v: int, rss_v, context_v, child_usage: tuple = ()) -> tuple:
            """Identity of one cached finished-row view fragment.

            Covers every input :meth:`_run_view` reads for a static row:
            the run write version (every ``_write_run`` bumps it), the
            folded usage version (post-fold, so refolds line up), the
            live workstream/confidence (the inherit refresh writes those
            directly), the current rss/context lookups, the epoch (merges,
            deletes, reassigns), and the children's usage versions for
            rollup parents (a child's write moves its tokens onto the
            parent). Two equal keys mean ``_run_view`` would return an
            equal dict, so the cached copy is exact.
            """
            run_id = row["run_id"]
            return (
                self._run_epoch,
                run_id,
                self._run_versions.get(run_id, 0),
                usage_v,
                row.get("workstream"),
                row.get("ws_confidence"),
                rss_v,
                context_v,
                child_usage,
            )

        def _cached_view(self, row, now: float, usage: dict, rss_pair: tuple,
                         usage_versions: dict, context: dict, stalled_map: dict,
                         children_of: dict | None = None) -> dict:
            """One run view, reusing the cached fragment for static rows.

            Open rows (and rows with no end time, whose elapsed clock
            ticks with ``now``) always recompute: their elapsed time,
            stalled flag, and liveness move with the clock. Ended rows
            are pure functions of the fragment key, so they recompute
            only when the key moved. Copies on a hit: the cache owns
            the stored dict (and its tokens), callers get their own.
            """
            status = row.get("status")
            if status in ("running", "queued") or row.get("ended_at") is None:
                return self._run_view(
                    row, now, usage, rss_pair, context, stalled_map.get(row["run_id"]))
            run_id = row["run_id"]
            by_run, by_pid = rss_pair
            rss_v = by_run.get(run_id)
            if rss_v is None and row.get("pid") is not None:
                rss_v = by_pid.get(row["pid"])
            context_v = context.get(run_id)
            usage_v = usage_versions.get(run_id, 0)
            child_usage: tuple = ()
            if children_of:
                kids = children_of.get(run_id)
                if kids:
                    child_usage = tuple(
                        sorted((kid, usage_versions.get(kid, 0)) for kid in kids)
                    )
            key = self._frag_key(row, usage_v, rss_v, context_v, child_usage)
            hit = self._view_frags.get(run_id)
            if hit is not None and hit[0] == key:
                view = dict(hit[1])
                tokens = hit[1].get("tokens")
                if isinstance(tokens, dict):
                    view["tokens"] = dict(tokens)
                return view
            view = self._run_view(
                row, now, usage, rss_pair, context, stalled_map.get(run_id))
            if len(self._view_frags) > 40000:
                self._view_frags.clear()
            self._view_frags[run_id] = (key, view)
            view = dict(view)
            tokens = view.get("tokens")
            if isinstance(tokens, dict):
                view["tokens"] = dict(tokens)
            return view

        def _open_evidence_locked(self, rows: list) -> tuple[dict, dict, dict]:
            """One shared liveness-evidence pass for a top-level call.

            ``(usage_max, sample_max, pid_starts)`` over the open rows,
            computed once and passed to the live-open set and the
            stalled map, instead of each issuing the same GROUP BYs.
            Caller holds the lock.
            """
            open_rows = [
                row for row in rows if (row.get("status") in ("running", "queued"))
            ]
            if not open_rows:
                return {}, {}, {}
            return (
                self._usage_max_locked([row["run_id"] for row in open_rows]),
                self._sample_max_locked(open_rows),
                self._pid_start_locked([row.get("pid") for row in open_rows]),
            )

        def _shown_locked(self, now: float, liveness=None) -> tuple[list, dict]:
            """All run views with child chats folded away and launch echoes dropped. Caller holds the lock."""
            raw, usage, parents = self._scoped_usage()
            context = self._context_by_run()
            rss = self._rss_maps(now)
            # raw rows are already fresh dicts (built in _scoped_usage)
            # and no evidence consumer mutates them, so no second copy.
            # Stalled rows never read process sightings, so only the
            # usage evidence is shared here; the sample GROUP BY stays
            # out of this path entirely.
            usage_max = self._usage_max_locked(
                [row["run_id"] for row in raw
                 if row.get("status") in ("running", "queued")]
            )
            checker = self._probe_checker(liveness) if liveness is not None else None
            file_mtimes = liveness.get("mtimes") if liveness is not None else None
            stalled = self._stalled_locked(
                now, raw, checker, usage_max, file_mtimes=file_mtimes
            )
            usage_versions = dict(self._usage_fold_versions)
            children_of: dict = {}
            for child, parent in parents.items():
                children_of.setdefault(parent, []).append(child)
            views = [
                self._cached_view(
                    row, now, usage, rss, usage_versions, context, stalled,
                    children_of,
                )
                for row in raw
            ]
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
            # Stalled flags ride in from _shown_locked (usage plus out-file
            # evidence); reuse them so one snapshot stats each file once.
            stalled = {
                run["run_id"]: run["stalled"]
                for run in shown_all
                if run.get("stalled")
            }
            range_since = now - RANGE_S if since is None else float(since)
            range_until = float(now if until is None else until)
            cells = self._query_usage_locked("workstream", "hour", None, range_since, range_until)
            buckets: dict[str, list] = {}
            for cell in cells:
                buckets.setdefault(cell["key"], []).append(cell)
            # One partition plus one counting pass for every scope: the
            # per-workstream member filters and the per-scope
            # ``_agent_counts`` walks used to cost O(scopes x runs).
            # The 24 hours immediately before the window, per scope, with
            # the same bucket sums and the same agent rule as the windowed
            # numbers, so a delta compares like with like. Tokens and cost
            # come from the same hourly cells over the earlier window (same
            # priced-cost fallback, so the same unpriced rule); finished is
            # the same finished-in-range count over that window; live is the
            # point-in-time overlap at the window start, not a peak.
            prev_since = range_since - RANGE_S
            prev_until = range_since
            live_at_start = _live_at_point_ids(shown_all, parents, prev_until, now)
            members_by_ws: dict[str, list] = {}
            for run in shown_all:
                members_by_ws.setdefault(run.get("workstream") or UNASSIGNED, []).append(run)
            if UNASSIGNED not in members_by_ws:
                members_by_ws[UNASSIGNED] = []
            workstreams = sorted(members_by_ws, key=_ws_sort)
            if UNASSIGNED not in workstreams:
                workstreams.append(UNASSIGNED)
            all_counts, counts_by_ws = _counts_many(
                shown_all, members_by_ws, parents, usage, now,
                range_since, range_until, prev_since, prev_until,
                live_open, stalled, live_at_start,
            )
            scopes: dict[str, dict] = {}
            all_cells = [cell for group in buckets.values() for cell in group]
            for ws in workstreams:
                summed = _sum_cells(buckets.get(ws, []))
                counts = counts_by_ws[ws]
                scopes[ws] = {
                    "runs": members_by_ws[ws],
                    "counts": counts["counts"],
                    "totals": {
                        "tokens": summed["tokens"],
                        "cost_usd": summed["cost_usd"],
                        "unpriced_runs": counts["unpriced_runs"],
                    },
                    "last_activity": counts["last_activity"],
                    "ws_confidence": counts["ws_confidence"],
                    "running": counts["running"],
                    "recent": counts["recent"],
                }
            summed_all = _sum_cells(all_cells)
            scopes["all"] = {
                "runs": shown_all,
                "counts": all_counts["counts"],
                "totals": {
                    "tokens": summed_all["tokens"],
                    "cost_usd": summed_all["cost_usd"],
                    "unpriced_runs": all_counts["unpriced_runs"],
                },
                "last_activity": all_counts["last_activity"],
                "ws_confidence": all_counts["ws_confidence"],
                "running": all_counts["running"],
                "recent": all_counts["recent"],
            }
            prev_cells = self._query_usage_locked("workstream", "hour", None, prev_since, prev_until)
            prev_buckets: dict[str, list] = {}
            for cell in prev_cells:
                prev_buckets.setdefault(cell["key"], []).append(cell)
            prev_all = _sum_cells(prev_cells)
            scopes["all"]["prev_24h"] = {
                "tokens": _billed_total(prev_all["tokens"]),
                "cost_usd": prev_all["cost_usd"],
                "finished": all_counts["prev_finished"],
                "live_agents": all_counts["point_live"],
            }
            for ws in workstreams:
                prev_summed = _sum_cells(prev_buckets.get(ws, []))
                counts = counts_by_ws[ws]
                scopes[ws]["prev_24h"] = {
                    "tokens": _billed_total(prev_summed["tokens"]),
                    "cost_usd": prev_summed["cost_usd"],
                    "finished": counts["prev_finished"],
                    "live_agents": counts["point_live"],
                }
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
        def _run_view(self, row, now: float, usage: dict, rss: tuple[dict, dict], context: dict | None = None, stalled=None) -> dict:
            data = dict(row)
            outcome_row = dict(data)
            if stalled:
                outcome_row["stalled"] = stalled
            cls, reason = _outcome_cached(outcome_row)
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
            harness = harness_of(data)
            return {
                "run_id": data["run_id"],
                "source": data.get("source"),
                "tier": data.get("tier"),
                "harness": harness,
                "harness_name": HARNESS_NAMES.get(harness, harness),
                "model": data.get("model"),
                "label": data.get("label"),
                "workstream": data.get("workstream"),
                "ws_confidence": data.get("ws_confidence"),
                "session_id": data.get("session_id"),
                "parent_session_id": data.get("parent_session_id"),
                "slice_of": data.get("slice_of"),
                "status": data.get("status"),
                "verdict": data.get("verdict"),
                "outcome_class": cls,
                "outcome_reason": reason,
                "stalled": stalled if stalled else False,
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


#: Memoized (outcome_class, outcome_reason) by the only inputs that
#: decide them: status, verdict, whether the end is confirmed, and the
#: stalled flag. The stalled *age* (a float) feeds only the live-stalled
#: reason sentence, so rows carrying a live stall bypass the memo and
#: compute fresh; every other row's pair is a pure function of the key.
#: Bounded: cleared when full, never unbounded across a long-lived server.
_OUTCOME_MEMO: dict = {}


def _outcome_cached(outcome_row: dict) -> tuple:
    """``(outcome_class, outcome_reason)`` for a run row, memoized.

    Pure wrapper over :func:`dash.outcomes.outcome_class` and
    :func:`dash.outcomes.outcome_reason`: equal keys mean both helpers
    would return equal values, so the cached pair is exact. Live
    stalled rows (whose reason names the stall age) always recompute.
    """
    status = outcome_row.get("status")
    stalled = outcome_row.get("stalled")
    try:
        if stalled and status in ("running", "queued") and outcome_row.get("ended_at") is None:
            cls = outcome_class(outcome_row)
            return cls, outcome_reason(outcome_row, cls)
        key = (
            status,
            outcome_row.get("verdict"),
            outcome_row.get("ended_at") is None,
            bool(stalled),
        )
        hash(key)
    except Exception:
        cls = outcome_class(outcome_row)
        return cls, outcome_reason(outcome_row, cls)
    hit = _OUTCOME_MEMO.get(key)
    if hit is None:
        if len(_OUTCOME_MEMO) > 8192:
            _OUTCOME_MEMO.clear()
        cls = outcome_class(outcome_row)
        hit = (cls, outcome_reason(outcome_row, cls))
        _OUTCOME_MEMO[key] = hit
    return hit


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


def _counts_many(shown_all: list, members_by_ws: dict, parents: dict, usage: dict,
                 now: float,
                 range_since: float, range_until: float,
                 prev_since: float, prev_until: float,
                 live_open=None, stalled=None, live_at_start: set | None = None) -> tuple:
    """One counting pass for the all-scope plus every workstream scope.

    Replaces ``O(scopes)`` ``_agent_counts`` walks (each over its own
    member list, plus a second full walk per scope for the previous
    window) with a single walk over ``shown_all`` accumulating into
    per-scope buckets. Predicates mirror :func:`_agent_counts`
    exactly, including the previous window's finished count (the only
    ``_prev_24h`` field that needs the run list) and the point-live
    dedup (same lane rule as :func:`_point_live_count`); every other
    ``prev_24h`` field comes from bucket cells as before.

    Returns ``(all_entry, by_ws)`` where each entry reads
    ``{"counts", "unpriced_runs", "prev_finished", "point_live"}`` and
    ``counts`` has exactly the :func:`_agent_counts` shape.
    """
    today = _local_midnight(now)
    stalled_ids = set(stalled or ())
    live_at_start = live_at_start or set()

    def _fresh_acc():
        return {
            "live_by": {key: 0 for key in TOOL_KEYS},
            "live_by_model": {},
            "seen": set(),
            "point_seen": set(),
            "live_n": 0,
            "stalled_n": 0,
            "prod_jobs": 0,
            "prod_jobs_finished": 0,
            "finished_today": 0,
            "finished_range": 0,
            "by_verdict": {},
            "unpriced": 0,
            "prev_finished": 0,
            "point_live": 0,
            "last": 0,
            "conf": set(),
            "running": [],
            "recent": [],
        }

    accs: dict = {}
    all_acc = _fresh_acc()

    for run in shown_all:
        run_id = run["run_id"]
        # Last-activity and confidence cover every row, children
        # included, exactly like _last_activity and _ws_row: update
        # before the parents skip below.
        ended_stamp = run.get("ended_at")
        started_stamp = run.get("started_at")
        conf_value = run.get("ws_confidence")
        ws = run.get("workstream") or UNASSIGNED
        acc = accs.get(ws)
        if acc is None:
            acc = _fresh_acc()
            accs[ws] = acc
        for target in (all_acc, acc):
            if ended_stamp and ended_stamp > target["last"]:
                target["last"] = ended_stamp
            if started_stamp and started_stamp > target["last"]:
                target["last"] = started_stamp
            if conf_value:
                target["conf"].add(conf_value)
        if run_id in parents:
            continue
        status = run.get("status")
        not_prod = run.get("source") != "dagster"
        for target in (all_acc, acc):
            if run.get("source") == "dagster" and status in ("running", "queued"):
                if live_open is None or run_id in live_open:
                    target["prod_jobs"] += 1
            if (
                run.get("source") == "dagster"
                and status != "running"
                and ended_stamp is not None
                and range_since <= float(ended_stamp) <= range_until
            ):
                target["prod_jobs_finished"] += 1
            # The page lists ride no agent-rule gate: any live non-job
            # non-marker open row runs, any ended non-job non-marker row
            # in the window is recent, exactly like the old filters.
            if status == "running":
                live_ok = live_open is None or run_id in live_open
                running_marker = is_marker_run(run) if live_ok else False
                if live_ok and not_prod and not running_marker:
                    target["running"].append(run)
                if not _is_counted_agent(run):
                    continue
                if not live_ok:
                    continue
                if run_id in stalled_ids:
                    target["stalled_n"] += 1
                    continue
                tool = _tool_key(run)
                key = (tool, run.get("label") or run_id)
                if key in target["seen"]:
                    continue
                target["seen"].add(key)
                if running_marker:
                    target["live_by"][tool] += 1
                    target["live_n"] += 1
                    continue
                target["live_by"][tool] += 1
                model = run_model_key(run)
                target["live_by_model"][model] = target["live_by_model"].get(model, 0) + 1
                target["live_n"] += 1
                continue
            marker = is_marker_run(run)
            if (
                ended_stamp is not None
                and not_prod
                and not marker
                and range_since <= float(ended_stamp) <= range_until
            ):
                target["recent"].append(run)
            if not _is_counted_agent(run):
                continue
            if marker:
                continue
            if status not in FINISHED or ended_stamp is None:
                continue
            ended = float(ended_stamp)
            if today <= ended <= now:
                target["finished_today"] += 1
            if range_since <= ended <= range_until:
                target["finished_range"] += 1
                cls = outcome_class(run)
                target["by_verdict"][cls] = target["by_verdict"].get(cls, 0) + 1
            if prev_since <= ended <= prev_until:
                target["prev_finished"] += 1
        if (
            _run_overlaps_window(run, range_since, range_until, now)
            and _usage_unpriced(usage.get(run_id))
        ):
            all_acc["unpriced"] += 1
            acc["unpriced"] += 1
        if run_id in live_at_start:
            for target in (all_acc, acc):
                lane = (_tool_key(run), run.get("label") or run_id)
                if lane not in target["point_seen"]:
                    target["point_seen"].add(lane)
                    target["point_live"] += 1

    def _entry(target):
        return {
            "counts": {
                "live_agents": target["live_n"],
                "live_by_tool": target["live_by"],
                "live_by_model": target["live_by_model"],
                "stalled": target["stalled_n"],
                "prod_jobs": target["prod_jobs"],
                "prod_jobs_finished": target["prod_jobs_finished"],
                "finished_today": target["finished_today"],
                "finished_in_range": target["finished_range"],
                "finished_by_verdict": target["by_verdict"],
            },
            "unpriced_runs": target["unpriced"],
            "prev_finished": target["prev_finished"],
            "point_live": target["point_live"],
            "last_activity": target["last"] or None,
            "ws_confidence": sorted(target["conf"]),
            "running": target["running"],
            "recent": target["recent"],
        }

    by_ws = {}
    for ws in members_by_ws:
        target = accs.get(ws)
        by_ws[ws] = _entry(target) if target is not None else _entry(_fresh_acc())
    return _entry(all_acc), by_ws


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
    stalled=None,
) -> dict:
    """Counts and totals for one scope (all, one workstream, or unassigned).

    Counts use the agent rule (lead sessions and Dagster jobs are not agents)
    and the window. Token and cost sums add up the ``/api/usage`` cells for the
    same window, so the snapshot, the tiles, and the chart legend agree.
    Live counts use ``live_open`` (the one filtered running set); stalled
    rows are counted separately under ``counts["stalled"]``.
    """
    counts = _agent_counts(runs, parents, now, since, until, live_open, stalled)
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
    if "last_activity" in scope:
        last_activity = scope["last_activity"]
    else:
        last_activity = _last_activity(scope["runs"])
    if "ws_confidence" in scope:
        ws_confidence = scope["ws_confidence"]
    else:
        ws_confidence = sorted(
            {
                run.get("ws_confidence")
                for run in scope["runs"]
                if run.get("ws_confidence")
            }
        )
    row = {
        "ws": ws,
        "live_agents": scope["counts"]["live_agents"],
        "finished_in_range": scope["counts"]["finished_in_range"],
        "finished_today": scope["counts"]["finished_today"],
        "tokens": _billed_total(scope["totals"]["tokens"]),
        "cost_usd": scope["totals"]["cost_usd"],
        "prev_24h": dict(scope.get("prev_24h") or _zero_prev()),
        "last_activity": last_activity,
        "ws_confidence": ws_confidence,
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


def _agent_counts(runs: list, parents: dict, now: float, since, until, live_open=None, stalled=None) -> dict:
    range_since = now - RANGE_S if since is None else float(since)
    range_until = float(now if until is None else until)
    today = _local_midnight(now)
    live_by = {key: 0 for key in TOOL_KEYS}
    live_by_model: dict = {}
    seen = set()
    live_n = 0
    stalled_n = 0
    prod_jobs = 0
    prod_jobs_finished = 0
    finished_today = 0
    finished_range = 0
    by_verdict = {}
    stalled_ids = set(stalled or ())
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
            if run["run_id"] in stalled_ids:
                # Stuck on a live process, not live: counted separately
                # with its own pill, never in the live-agents strip.
                stalled_n += 1
                continue
            tool = _tool_key(run)
            key = (tool, run.get("label") or run["run_id"])
            if key in seen:
                continue
            seen.add(key)
            if is_marker_run(run):
                # Live-presence only: a process seen before its session,
                # manifest, or usage match names no model, so it counts
                # toward the live total and its (known) tool but never
                # toward a model bucket. The tile caption reads the
                # remainder as "starting".
                live_by[tool] += 1
                live_n += 1
                continue
            live_by[tool] += 1
            model = run_model_key(run)
            live_by_model[model] = live_by_model.get(model, 0) + 1
            live_n += 1
            continue
        if is_marker_run(run):
            # A placeholder that never ran is not a finished agent, even
            # though its status reads done.
            continue
        if run.get("status") not in FINISHED or run.get("ended_at") is None:
            continue
        ended = float(run["ended_at"])
        if today <= ended <= now:
            finished_today += 1
        if range_since <= ended <= range_until:
            finished_range += 1
            # One derivation for every surface: the tile caption, the
            # Finished filters, the timeline, the drawer, and the legends
            # all read outcome classes, never raw verdicts, so a dead
            # session can never bucket as blocked (or finished).
            cls = outcome_class(run)
            by_verdict[cls] = by_verdict.get(cls, 0) + 1
    return {
        "live_agents": live_n,
        "live_by_tool": live_by,
        "live_by_model": live_by_model,
        "stalled": stalled_n,
        "prod_jobs": prod_jobs,
        "prod_jobs_finished": prod_jobs_finished,
        "finished_today": finished_today,
        "finished_in_range": finished_range,
        "finished_by_verdict": by_verdict,
    }
