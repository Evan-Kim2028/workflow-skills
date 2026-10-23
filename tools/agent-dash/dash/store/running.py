"""Running set, liveness, stale/idle/unseen logic.

Owns the one filtered running set, liveness evidence, and
stale/idle/unseen logic (via ``dash.liveness``). Public
methods run under the facade lock; ``_locked`` helpers assume
it and never take it.
"""

from __future__ import annotations

from dash.liveness import (DAGSTER_ACTIVE_FRESH_S,
                      REAP_IDLE_S,
                      STALE_IDLE_S,
                      STALLED_AFTER_S,
                      STALLED_SOURCES,
                      dead_run_patch,
                      pid_alive as _pid_alive,
                      proc_started_ok,
                      row_shows_quota,
                      sibling_err_path,
                      stale_grace_s,)

from .util import (WORKER_SOURCES, WRAPPER_SOURCES)


class RunningMixin:
        def set_dagster_active(self, run_ids, now: float, full_page: bool = False) -> None:
            """Remember the latest successful Dagster active set for stale checks."""
            import json as _json

            payload = _json.dumps(
                {"ids": sorted(set(run_ids)), "now": float(now), "full_page": bool(full_page)}
            )
            with self._lock:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('dagster_active', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (payload,),
                )
        def get_dagster_active(self):
            """Latest successful active set, or None when no poll has succeeded."""
            import json as _json

            with self._lock:
                row = self._conn.execute(
                    "SELECT value FROM meta WHERE key='dagster_active'"
                ).fetchone()
            if not row or not row["value"]:
                return None
            try:
                data = _json.loads(row["value"])
            except (TypeError, ValueError):
                return None
            if not isinstance(data, dict):
                return None
            return data
        def note_dagster_seen(self, run_ids, now: float) -> None:
            """Record when each active Dagster run was last seen (for close-out)."""
            if not run_ids:
                return
            with self._lock:
                self._conn.executemany(
                    "INSERT INTO dagster_seen(run_id, last_seen) VALUES(?, ?) "
                    "ON CONFLICT(run_id) DO UPDATE SET last_seen=excluded.last_seen",
                    [(rid, float(now)) for rid in set(run_ids)],
                )
        def dagster_last_seen(self, run_id: str):
            with self._lock:
                row = self._conn.execute(
                    "SELECT last_seen FROM dagster_seen WHERE run_id=?", (run_id,)
                ).fetchone()
            return float(row["last_seen"]) if row and row["last_seen"] is not None else None
        def dagster_open_runs(self) -> list:
            """Stored Dagster rows still marked running or queued."""
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM runs WHERE source='dagster' AND status IN ('running','queued')"
                ).fetchall()
            return [dict(row) for row in rows]
        def reap_dead_runs(self, now: float, idle_s: float | None = None, sources=None) -> list:
            """Close running rows whose process died without writing a finish.

            A row still marked ``running``/``queued`` whose stored pid is
            gone and whose last evidence (usage heartbeats, process
            samples, out/err file mtimes) is older than ``idle_s``
            (``REAP_IDLE_S``, 10 minutes, config ``reaper.idle_minutes``)
            is closed in place: ``died_early`` with verdict ``DEAD``
            (``ended_at`` at the last activity), or verdict ``QUOTA`` when
            the logs match a rate/quota limit — a Grok Build run killed by
            a provider rate limit never writes a finish line, so the
            failure reason comes from its err log. The record comes from
            ``dash.liveness.dead_run_patch``, which the source reconcile
            pass and the stale close write too, so every orphan closes
            the same way. A row whose pid is
            alive is never touched, and neither are Dagster rows (their
            close-out follows the active set), slices (they follow their
            primary), or rows with no pid at all. One lock hold covers
            the liveness checks, the file stats, and the writes, so a
            poll cannot revive a row between the check and the close.
            Returns one ``{run_id, source, label, verdict, idle_s}`` dict
            per reaped row.
            """
            import os as _os

            now = float(now)
            if idle_s is None:
                idle_s = REAP_IDLE_S
            idle_s = float(idle_s)
            wanted_sources = set(sources or ())
            with self._lock:
                rows = [
                    dict(row)
                    for row in self._conn.execute(
                        "SELECT * FROM runs WHERE status IN ('running','queued') "
                        "AND pid IS NOT NULL AND slice_of IS NULL AND source != 'dagster'"
                    ).fetchall()
                ]
                if wanted_sources:
                    rows = [row for row in rows if (row.get("source") or "") in wanted_sources]
                if not rows:
                    return []
                usage_max = self._usage_max_locked([row["run_id"] for row in rows])
                sample_max = self._sample_max_locked(rows)
                reaped = []
                for row in rows:
                    pid = _int_pid(row.get("pid"))
                    if pid is None:
                        continue
                    try:
                        if _pid_alive(pid):
                            continue
                    except Exception:
                        continue
                    stamps = []
                    started = row.get("started_at")
                    if isinstance(started, (int, float)) and not isinstance(started, bool):
                        stamps.append(float(started))
                    hit = usage_max.get(row["run_id"])
                    if hit is not None:
                        stamps.append(float(hit))
                    hit = sample_max.get(row["run_id"])
                    if hit is not None:
                        stamps.append(float(hit))
                    hit = sample_max.get(f"pid:{pid}")
                    if hit is not None:
                        stamps.append(float(hit))
                    log_paths = [path for path in (row.get("out_path"), sibling_err_path(row.get("out_path"))) if path]
                    for path in log_paths:
                        try:
                            stamps.append(float(_os.stat(path).st_mtime))
                        except OSError:
                            continue
                    if not stamps:
                        continue
                    last = max(stamps)
                    quiet = now - last
                    if quiet < idle_s:
                        continue
                    merged = dict(row)
                    merged.update(
                        dead_run_patch(
                            row, last, quota=row_shows_quota(row)
                        )
                    )
                    self._write_run(merged)
                    self._seq += 1
                    self._data_seq += 1
                    self._note_data_write()
                    self._note_view_write()
                    reaped.append(
                        {
                            "run_id": row["run_id"],
                            "source": row.get("source"),
                            "label": row.get("label"),
                            "verdict": merged["verdict"],
                            "idle_s": round(quiet, 1),
                        }
                    )
                return reaped
        def stale_running(self, now: float, pid_alive_fn=None) -> list:
            """Running rows with no liveness evidence, each with a reason.

            Reasons: a dead pid; a Dagster run absent from the latest successful
            active set; or no source evidence (usage, process sample, start) for
            longer than STALE_IDLE_S. Dagster rows are stale only when the active
            set is fresh and not a full page; a failed or full-page poll reports
            nothing. An alive-but-idle lead session (``claude-main`` with a live
            pid, quiet for longer than STALE_IDLE_S) is an intended state, not
            stale: it is hidden from views but never reported here and never
            closed. Returns a list of ``(run, reason)``.
            """
            with self._lock:
                rows = [
                    dict(row)
                    for row in self._conn.execute(
                        "SELECT * FROM runs WHERE status IN ('running','queued')"
                    ).fetchall()
                ]
                return self._stale_locked(now, rows, pid_alive_fn=pid_alive_fn)
        def _stale_locked(self, now: float, rows: list, pid_alive_fn=None, usage_max=None, sample_max=None, pid_starts=None) -> list:
            """Stale subset of ``rows``. Caller holds the lock.

            ``usage_max``/``sample_max``/``pid_starts`` are shared
            liveness evidence: pass them when one top-level call
            already computed them for the same open rows (each is a
            GROUP BY over the usage/process tables); None computes
            them here, exactly as before.
            """
            now = float(now)
            check = pid_alive_fn or _pid_alive
            dagster_active = self._dagster_active_locked()
            dag_ids = None
            dag_fresh = False
            if dagster_active is not None:
                try:
                    poll_time = float(dagster_active.get("now") or 0)
                except (TypeError, ValueError):
                    poll_time = 0
                full = bool(dagster_active.get("full_page"))
                if not full and now - poll_time <= DAGSTER_ACTIVE_FRESH_S:
                    dag_fresh = True
                    dag_ids = set(dagster_active.get("ids") or [])
            open_rows = [
                row for row in rows if (row.get("status") in ("running", "queued"))
            ]
            if not open_rows:
                return []
            if usage_max is None:
                usage_max = self._usage_max_locked([row["run_id"] for row in open_rows])
            if sample_max is None:
                sample_max = self._sample_max_locked(open_rows)
            stale = []
            if pid_starts is None:
                pid_starts = self._pid_start_locked([row.get("pid") for row in open_rows])
            for row in open_rows:
                if row.get("slice_of"):
                    # A slice is never stale on its own: it follows its
                    # primary's liveness (see the live-open set), so a
                    # quiet slice is hidden with its session, never
                    # reported as a mismatch.
                    continue
                pid_start = pid_starts.get(_int_pid(row.get("pid")))
                if _is_idle_lead(row, now, usage_max, sample_max, check, pid_start):
                    continue
                reason = self._stale_reason_locked(
                    row, now, dag_ids, dag_fresh, usage_max, sample_max, check, pid_start
                )
                if reason:
                    stale.append((dict(row), reason))
            return stale
        def _idle_locked(self, now: float, rows: list, pid_alive_fn=None, usage_max=None, sample_max=None, pid_starts=None) -> list:
            """Alive-but-idle lead sessions. Caller holds the lock.

            A ``claude-main`` row with a live pid and no source evidence for
            longer than STALE_IDLE_S is an intended interactive state: the lead
            is still attached but has written nothing. It stays out of every
            view like a stale row, but it is never a mismatch and it is never
            closed, so a later transcript write returns it to Running now.

            Shared-evidence parameters behave exactly as in
            :meth:`_stale_locked`.
            """
            now = float(now)
            check = pid_alive_fn or _pid_alive
            open_rows = [
                row for row in rows if (row.get("status") in ("running", "queued"))
            ]
            if not open_rows:
                return []
            if usage_max is None:
                usage_max = self._usage_max_locked([row["run_id"] for row in open_rows])
            if sample_max is None:
                sample_max = self._sample_max_locked(open_rows)
            if pid_starts is None:
                pid_starts = self._pid_start_locked([row.get("pid") for row in open_rows])
            return [
                dict(row)
                for row in open_rows
                if _is_idle_lead(
                    row, now, usage_max, sample_max, check,
                    pid_starts.get(_int_pid(row.get("pid"))),
                )
            ]
        def _stalled_locked(self, now: float, rows: list, pid_alive_fn=None, usage_max=None, sample_max=None) -> dict:
            """Open worker rows stuck on a live process, by run id to idle seconds.

            A ``STALLED_SOURCES`` row whose pid is still alive but whose
            output evidence (usage heartbeats, out-file growth; never
            process sightings, which a stuck process keeps emitting) is
            older than ``STALLED_AFTER_S`` is stalled, not live. A row
            that only just started is never stalled; a row with no output
            evidence at all is left to the stale path. Caller holds the
            lock. File stats stay bounded: only open worker rows stat.

            ``usage_max`` is shared liveness evidence (see
            :meth:`_stale_locked`); ``sample_max`` is accepted and ignored
            (stalled rows never read process sightings), so one call site
            can pass the same pair to every evidence consumer.
            """
            import os as _os

            now = float(now)
            check = pid_alive_fn or _pid_alive
            open_rows = [
                row for row in rows
                if row.get("status") in ("running", "queued")
                and (row.get("source") or "") in STALLED_SOURCES
            ]
            if not open_rows:
                return {}
            if usage_max is None:
                usage_max = self._usage_max_locked([row["run_id"] for row in open_rows])
            found = {}
            for row in open_rows:
                pid = row.get("pid")
                if pid is None:
                    continue
                try:
                    alive = bool(check(pid))
                except Exception:
                    alive = False
                if not alive:
                    continue
                stamps = []
                started = row.get("started_at")
                if isinstance(started, (int, float)) and not isinstance(started, bool):
                    stamps.append(float(started))
                hit = usage_max.get(row.get("run_id"))
                if hit is not None:
                    stamps.append(float(hit))
                out_path = row.get("out_path")
                if isinstance(out_path, str) and out_path:
                    try:
                        stamps.append(float(_os.stat(out_path).st_mtime))
                    except OSError:
                        pass
                if not stamps:
                    continue
                idle = now - max(stamps)
                if idle > STALLED_AFTER_S:
                    found[row["run_id"]] = idle
            return found
        def _pid_start_locked(self, pids: list) -> dict:
            """Process start epoch by pid from the latest sample. Caller holds the lock.

            One ``LIMIT 1`` lookup per pid instead of one global
            ``ORDER BY ts`` over every sample those pids ever wrote: the
            (pid, ts) index seeks straight to each pid's latest sample
            instead of sorting hundreds of thousands of history rows.
            Same row per pid (the max-ts one; ties carry the same
            sampler-measured values), so the starts are identical.
            """
            wanted = []
            for pid in pids or ():
                try:
                    wanted.append(int(pid))
                except (TypeError, ValueError):
                    continue
            if not wanted:
                return {}
            found = {}
            for pid in sorted(set(wanted)):
                row = self._conn.execute(
                    "SELECT pid, ts, elapsed_s FROM process_samples "
                    "WHERE pid=? AND ts IS NOT NULL AND elapsed_s IS NOT NULL "
                    "ORDER BY ts DESC LIMIT 1",
                    (pid,),
                ).fetchone()
                if row is None:
                    continue
                try:
                    found[pid] = float(row["ts"]) - float(row["elapsed_s"])
                except (TypeError, ValueError):
                    continue
            return found
        def _live_open_ids_locked(self, now: float, rows: list, pid_alive_fn=None, usage_max=None, sample_max=None, pid_starts=None) -> set:
            """Run ids of open rows with liveness evidence. Caller holds the lock.

            This is the one filtered running set every view uses: the snapshot
            running list, ``counts.live_agents``, ``/api/workstreams``
            ``live_agents``, and the timeline's open bars all derive from it, so
            no view can show a run another hides. Idle leads are excluded here
            too, so they stay out of Running now exactly as before. A
            ``claude-workflow`` wrapper shadowed by a live worker row on the
            same label (one lane, one agent) is excluded here as well; the
            worker carries the lane.
            """
            live, _shadows = self._live_open_with_shadows_locked(
                now, rows, pid_alive_fn, usage_max, sample_max, pid_starts)
            return live
        def _live_open_with_shadows_locked(self, now: float, rows: list, pid_alive_fn=None, usage_max=None, sample_max=None, pid_starts=None):
            """The one running set plus the lane-shadow map. Caller holds the lock.

            ``shadows`` maps a shadowed wrapper run id to its live worker run
            id. It is computed against the pre-subtraction open set, so pass
            that open set (``live | set(shadows)``) — not the subtracted set —
            to ``_lane_shadows``/``_annotate_lanes``.
            """
            open_rows = [
                row for row in rows if (row.get("status") in ("running", "queued"))
            ]
            if open_rows:
                if usage_max is None:
                    usage_max = self._usage_max_locked([row["run_id"] for row in open_rows])
                if sample_max is None:
                    sample_max = self._sample_max_locked(open_rows)
                if pid_starts is None:
                    pid_starts = self._pid_start_locked([row.get("pid") for row in open_rows])
            else:
                usage_max, sample_max, pid_starts = {}, {}, {}
            stale_ids = {
                run["run_id"]
                for run, _reason in self._stale_locked(
                    now, rows, pid_alive_fn, usage_max=usage_max, sample_max=sample_max,
                    pid_starts=pid_starts,
                )
            }
            idle_ids = {
                row["run_id"]
                for row in self._idle_locked(
                    now, rows, pid_alive_fn, usage_max=usage_max, sample_max=sample_max,
                    pid_starts=pid_starts,
                )
            }
            open_ids = {
                row["run_id"]
                for row in rows
                if row.get("status") in ("running", "queued")
                and not row.get("slice_of")
                and row.get("run_id") not in stale_ids
                and row.get("run_id") not in idle_ids
            }
            # Slices carry no liveness of their own: a slice is live
            # exactly when its primary is. Lead sources are never
            # counted agents, so slices cannot inflate live_agents no
            # matter how many show in the running list.
            for row in rows:
                if (
                    row.get("status") in ("running", "queued")
                    and row.get("slice_of")
                    and row.get("slice_of") in open_ids
                ):
                    open_ids.add(row["run_id"])
            shadows = _lane_shadows(rows, open_ids)
            return open_ids - set(shadows), shadows
        def _stale_reason_locked(
            self, run: dict, now: float, dag_ids, dag_fresh: bool,
            usage_max: dict, sample_max: dict, check, pid_start=None,
        ):
            source = run.get("source") or ""
            status = run.get("status")
            run_id = run.get("run_id")
            if status not in ("running", "queued"):
                return None
            if source == "dagster":
                if status not in ("running", "queued"):
                    return None
                if dag_ids is None or not dag_fresh:
                    return None
                short = run_id[len("dagster:"):] if run_id.startswith("dagster:") else run_id
                if short not in dag_ids and run_id not in dag_ids:
                    return "dagster run missing from the latest successful active set"
                return None
            pid = run.get("pid")
            dead_pid = False
            reused_pid = False
            if pid is not None:
                try:
                    alive = bool(check(pid))
                except Exception:
                    alive = False
                dead_pid = not alive
                if alive and not _proc_is_ours(
                    run, pid_start, _session_evidence(run, usage_max, sample_max)
                ):
                    # The pid is alive but started after the session's
                    # latest action: the run's agent is gone. This reads
                    # stale (and closes through the normal path) instead
                    # of hiding behind a recycled pid.
                    reused_pid = True
            stamps = []
            started = run.get("started_at")
            if isinstance(started, (int, float)) and not isinstance(started, bool):
                stamps.append(float(started))
            hit = usage_max.get(run_id)
            if hit is not None:
                stamps.append(float(hit))
            hit = sample_max.get(run_id)
            if hit is not None:
                stamps.append(float(hit))
            if pid is not None:
                hit = sample_max.get(f"pid:{pid}")
                if hit is not None:
                    stamps.append(float(hit))
            last = max(stamps) if stamps else None
            if reused_pid:
                return f"pid {pid} is a newer process, not the run's agent (pid reused)"
            if dead_pid:
                if last is not None and now - last <= stale_grace_s(source):
                    return None
                if (
                    source == "claude-main"
                    and last is not None
                    and now - last <= _lead_fresh_s()
                ):
                    # A dead registry pid with fresh session evidence is
                    # a rolled CLI, not a dead session: the registry
                    # lags the restart while the transcript is ground
                    # truth, so this stays open until writing stops.
                    return None
                return f"dead pid {pid}"
            if last is None:
                return None
            if now - last > STALE_IDLE_S:
                return f"no source evidence for longer than {int(STALE_IDLE_S // 3600)}h"
            return None
        def _dagster_active_locked(self):
            import json as _json

            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='dagster_active'"
            ).fetchone()
            if not row or not row["value"]:
                return None
            try:
                data = _json.loads(row["value"])
            except (TypeError, ValueError):
                return None
            return data if isinstance(data, dict) else None
        def _usage_max_locked(self, run_ids: list) -> dict:
            if not run_ids:
                return {}
            found = {}
            step = 500
            for index in range(0, len(run_ids), step):
                chunk = run_ids[index:index + step]
                marks = ",".join("?" for _ in chunk)
                for row in self._conn.execute(
                    f"SELECT run_id, MAX(ts) AS ts FROM usage_events "
                    f"WHERE run_id IN ({marks}) GROUP BY run_id",
                    chunk,
                ).fetchall():
                    if row["ts"] is not None:
                        found[row["run_id"]] = float(row["ts"])
            return found
        def _sample_max_locked(self, rows: list) -> dict:
            """Latest process-sample ts by run id and by pid."""
            run_ids = [row.get("run_id") for row in rows if row.get("run_id")]
            pids = [row.get("pid") for row in rows if row.get("pid") is not None]
            found = {}
            if run_ids:
                step = 500
                for index in range(0, len(run_ids), step):
                    chunk = run_ids[index:index + step]
                    marks = ",".join("?" for _ in chunk)
                    for row in self._conn.execute(
                        f"SELECT run_id, MAX(ts) AS ts FROM process_samples "
                        f"WHERE run_id IN ({marks}) GROUP BY run_id",
                        chunk,
                    ).fetchall():
                        if row["run_id"] and row["ts"] is not None:
                            found[row["run_id"]] = float(row["ts"])
            if pids:
                uniq = sorted({int(pid) for pid in pids})
                step = 500
                for index in range(0, len(uniq), step):
                    chunk = uniq[index:index + step]
                    marks = ",".join("?" for _ in chunk)
                    for row in self._conn.execute(
                        f"SELECT pid, MAX(ts) AS ts FROM process_samples "
                        f"WHERE pid IN ({marks}) GROUP BY pid",
                        chunk,
                    ).fetchall():
                        if row["ts"] is not None:
                            found[f"pid:{int(row['pid'])}"] = float(row["ts"])
            return found
        def _unseen_workers_locked(self, now: float, shown_all: list, processes) -> list:
            """``unseen worker`` mismatches: live worker, no running worker row.

            A live worker process (``[label:]``, a muse prompt-file label, or a
            ``run_worker.py`` argv label) with no ``running``/``queued`` worker
            row (``muse``/``glm``/``grok``) on that label is invisible on the
            dashboard: one mismatch per label. Only the label is reported; argv
            beyond it is never stored or printed. Caller holds the lock.

            Grace, like the stale check: a worker younger than two poll
            intervals of its source is not yet a mismatch — by process age, or
            when this dashboard first sampled it within that window (a fresh
            dashboard has only just met it, e.g. ramp-up transients). Older,
            longer-seen workers with no running row still fail the check.
            """
            have = set()
            for run in shown_all:
                if (
                    run.get("status") in ("running", "queued")
                    and run.get("source") in WORKER_SOURCES
                    and run.get("label")
                ):
                    have.add(run["label"])
            pending: dict = {}
            for proc in processes or []:
                if not isinstance(proc, dict):
                    continue
                label = proc.get("label")
                if not label or label in have or label in pending:
                    continue
                pending[label] = proc
            first_seen = self._first_sample_ts_locked(set(pending)) if pending else {}
            out = []
            for label in sorted(pending):
                proc = pending[label]
                grace = stale_grace_s(_unseen_source(proc))
                try:
                    age = now - float(proc.get("started_at"))
                except (TypeError, ValueError):
                    age = None
                if age is not None and age < grace:
                    continue
                seen_at = first_seen.get(label)
                if seen_at is not None and now - seen_at < grace:
                    continue
                comm = proc.get("comm") or "worker"
                pid = proc.get("pid")
                detail = f"{comm} pid {pid}" if pid is not None else str(comm)
                out.append(f"unseen worker: {label} ({detail})")
            return out
        def _first_sample_ts_locked(self, labels: set) -> dict:
            """First process-sample time per label. Caller holds the lock."""
            wanted = [label for label in labels if isinstance(label, str) and label]
            if not wanted:
                return {}
            marks = ",".join("?" for _ in wanted)
            rows = self._conn.execute(
                "SELECT label, MIN(ts) AS first_ts FROM process_samples "
                f"WHERE label IN ({marks}) GROUP BY label",
                tuple(wanted),
            ).fetchall()
            return {
                row["label"]: float(row["first_ts"])
                for row in rows
                if row["first_ts"] is not None
            }


def _int_pid(pid):
    try:
        return int(pid)
    except (TypeError, ValueError):
        return None


def _lead_fresh_s() -> float:
    """Fresh-evidence bound for a lead session, in seconds.

    Mirrors the transcript window the claude source uses to call a
    main session still-writing. A lead with newer evidence than this
    is alive no matter what its registry pid says.
    """
    try:
        from dash.sources.claude import MAIN_RUNNING_MTIME_S

        return float(MAIN_RUNNING_MTIME_S)
    except (ImportError, TypeError, ValueError):
        return 180.0


def _session_evidence(run: dict, usage_max: dict, sample_max: dict):
    """Latest session-authored evidence for a run, or None.

    The run start, its usage events, and its run-linked process
    samples: everything proving the session itself acted. Bare pid
    sightings (``pid:<pid>``) are process evidence, not session
    evidence, so they never count here.
    """
    stamps = []
    started = run.get("started_at")
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        stamps.append(float(started))
    run_id = run.get("run_id")
    hit = (usage_max or {}).get(run_id)
    if hit is not None:
        stamps.append(float(hit))
    hit = (sample_max or {}).get(run_id)
    if hit is not None:
        stamps.append(float(hit))
    return max(stamps) if stamps else None


def _proc_is_ours(run: dict, pid_start, evidence=None) -> bool:
    """True when the process start fits the run's latest evidence.

    A process that was already running when the session last acted
    cannot be newer than the run's agent: CLI restarts roll the
    process while a long-lived lead session continues, so the start
    clock alone would misread the current process as recycled. Without
    evidence (or without a process start) there is nothing to compare
    and the process reads as ours, exactly as before.
    """
    if pid_start is None:
        return True
    if evidence is None:
        evidence = _session_evidence(run, {}, {})
        if evidence is None:
            return True
    try:
        return proc_started_ok(pid_start, float(evidence))
    except (TypeError, ValueError):
        return True


def _is_idle_lead(run: dict, now: float, usage_max: dict, sample_max: dict, check, pid_start=None) -> bool:
    """True for an alive-but-idle interactive lead session.

    A ``claude-main`` row whose pid is still alive but whose newest source
    evidence (start, usage, process sample) is older than STALE_IDLE_S is
    an intended state: the lead is attached but has written nothing. It is
    hidden from views like a stale row, but it is never a mismatch and it
    is never closed. A dead pid is not idle (it stays a stale ``dead pid``
    mismatch), and neither is a pid that now belongs to a newer process
    (a stale ``pid reused`` mismatch instead); a row with no pid or no
    evidence at all is not idle either, so all are still reported exactly
    as before.
    """
    if (run.get("source") or "") != "claude-main":
        return False
    if run.get("status") not in ("running", "queued"):
        return False
    pid = run.get("pid")
    if pid is None:
        return False
    try:
        alive = bool(check(pid))
    except Exception:
        alive = False
    if not alive:
        return False
    if not _proc_is_ours(run, pid_start, _session_evidence(run, usage_max, sample_max)):
        return False
    run_id = run.get("run_id")
    stamps = []
    started = run.get("started_at")
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        stamps.append(float(started))
    hit = usage_max.get(run_id)
    if hit is not None:
        stamps.append(float(hit))
    hit = sample_max.get(run_id)
    if hit is not None:
        stamps.append(float(hit))
    hit = sample_max.get(f"pid:{pid}")
    if hit is not None:
        stamps.append(float(hit))
    if not stamps:
        return False
    return now - max(stamps) > STALE_IDLE_S


def _lane_shadows(rows: list, live_ids: set) -> dict:
    """Wrapper run id -> worker run id for lanes with a live worker.

    A ``claude-workflow`` wrapper (the Haiku step a ws-wave lane runs in)
    and a ``muse``/``glm``/``grok`` worker row share the lane label. When
    the worker row is live, the lane counts once — the worker — and the
    wrapper id is shadowed out of the one running set. With no live
    worker row for the wrapper's label (still launching, or the worker
    died), the wrapper is not shadowed and counts as the lane itself.
    """
    live_worker: dict = {}
    for run in rows:
        if (
            run.get("run_id") in live_ids
            and run.get("source") in WORKER_SOURCES
            and run.get("label")
        ):
            live_worker.setdefault(run["label"], run["run_id"])
    shadows = {}
    for run in rows:
        if (
            run.get("run_id") in live_ids
            and run.get("source") in WRAPPER_SOURCES
            and run.get("label")
            and run["label"] in live_worker
        ):
            shadows[run["run_id"]] = live_worker[run["label"]]
    return shadows


def _annotate_lanes(views: list, live_open: set) -> list:
    """Mark lane shadows on run views (view-only; stored rows are untouched).

    A shadowed wrapper view carries ``lane_shadow: True`` so the timeline
    strip counts the lane once while the bar stays clickable. A live
    worker view with no parent names its wrapper run id as
    ``parent_session_id``, so Running now shows the worker via its
    workflow step.
    """
    shadows = _lane_shadows(views, live_open)
    if not shadows:
        return views
    worker_to_wrapper: dict = {}
    for wrapper_id, worker_id in shadows.items():
        worker_to_wrapper.setdefault(worker_id, wrapper_id)
    for view in views:
        run_id = view.get("run_id")
        if run_id in shadows:
            view["lane_shadow"] = True
        elif run_id in worker_to_wrapper and not view.get("parent_session_id"):
            view["parent_session_id"] = worker_to_wrapper[run_id]
    return views


def _unseen_source(proc: dict) -> str | None:
    """Worker source for a live-worker row, for the grace interval.

    Inverts the process listing's comm map; an unknown comm (a
    ``run_worker.py`` launcher, a bare ``[label:]`` token) takes the
    default interval. Every worker source polls at 2 s today, so this is
    4 s either way; the lookup keeps it honest if one diverges.
    """
    try:
        from dash.sources.process import SOURCE_COMMS
    except Exception:
        SOURCE_COMMS = {}
    comm = proc.get("comm") if isinstance(proc, dict) else None
    for source, comms in (SOURCE_COMMS or {}).items():
        try:
            if comm in comms:
                return source
        except TypeError:
            continue
    return None
