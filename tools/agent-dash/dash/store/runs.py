"""Run rows: upserts, merge aliases, attribution glue, run reads.

Owns run and session writes, grok manifest merge aliases, muse
repairs, run attribution (via ``dash.attribution``), and run
reads. Public methods run under the facade lock; ``_locked``
helpers assume it and never take it.
"""

from __future__ import annotations

import time as _time

from dash import inherit as _inherit
from dash.attribution import attribute, clean_label, is_hub_label
from dash.liveness import PROCESS_GONE_GRACE_S

from .running import _annotate_lanes

from .util import (_ESTIMATE_TOOL, MERGE_WINDOW_S, RUN_COLS,
                 RUN_INPUT, RUN_VIEW_COLS, SESSION_COLS, TOKEN_FIELDS,
                 _collapse_workflows, _drop_launch_echoes,
                 _epoch, _in_ws, _manifest_family, _same, _zeros,
                 is_marker_run, is_prod_job_run)


class RunsMixin:
        def _refresh_attribution_snapshot(self) -> None:
            """Refresh map and bundle inputs before entering store critical sections."""
            from dash.attribution import rules_snapshot

            self._attribution_rules = rules_snapshot()
            self._train_branches = _inherit.train_branches()

        def _attribute_locked(self, label, session_id, parent_session_id, run_id=None):
            """Explicit attribution, else workstream inheritance (caller holds the lock)."""
            workstream, confidence = attribute(
                label,
                session_id,
                parent_session_id=parent_session_id,
                rules=getattr(self, "_attribution_rules", None),
            )
            if workstream is not None:
                return workstream, confidence
            return _inherit.resolve(
                self, label, session_id, parent_session_id, run_id=run_id,
                branches=getattr(self, "_train_branches", None),
            )

        def refresh_workstream_inheritance(self, now: float) -> int:
            """Recompute inherited/inferred rows; never touch explicit ones."""
            self._refresh_attribution_snapshot()
            with self._lock:
                changed = _inherit.refresh(
                    self, now, rules=self._attribution_rules,
                    branches=self._train_branches,
                )
                if changed:
                    self._seq += 1
                    self._data_seq += 1
                    self._note_data_write()
                    self._note_view_write()
                return changed

        def upsert_session(self, row: dict) -> None:
            session_id = row.get("session_id")
            if not session_id:
                raise ValueError("session_id required")
            self._refresh_attribution_snapshot()
            with self._lock:
                existing = self._one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
                merged = dict(existing or {})
                merged["session_id"] = session_id
                for key in ("tool", "cwd", "started_at", "status", "parent_session_id", "label", "ws_hint"):
                    if key in row:
                        merged[key] = row[key]
                merged["label"] = clean_label(merged.get("label"))
                workstream, confidence = self._attribute_locked(
                    merged.get("label"),
                    merged.get("session_id"),
                    merged.get("parent_session_id"),
                )
                if (
                    workstream is None
                    and confidence is None
                    and existing
                    and existing.get("ws_confidence") in ("inherited", "inferred")
                    and not is_hub_label(merged.get("label"))
                ):
                    # No evidence right now is not evidence of absence: a
                    # settled inference survives the write, and the inherit
                    # refresh (with poll-now evidence) remains the sole
                    # clearer. Explicit attributions always overwrite. Hub
                    # labels are exempt: inherited work never survives on
                    # hub-scoped work.
                    workstream = existing.get("workstream")
                    confidence = existing.get("ws_confidence")
                merged["workstream"] = workstream
                merged["ws_confidence"] = confidence
                if existing and _same(existing, merged, SESSION_COLS):
                    return
                self._write_session(merged)
                self._seq += 1
                self._data_seq += 1
                self._note_data_write()
                self._note_view_write()
        def upsert_run(self, row: dict) -> None:
            run_id = row.get("run_id")
            if not run_id:
                raise ValueError("run_id required")
            self._refresh_attribution_snapshot()
            with self._lock:
                if isinstance(run_id, str) and _manifest_family(run_id) is not None:
                    target = self._resolve_alias_locked(run_id)
                    if target != run_id and self._upsert_aliased_locked(run_id, target, row):
                        return
                existing = self._one("SELECT * FROM runs WHERE run_id=?", (run_id,))
                merged = dict(existing or {})
                merged["run_id"] = run_id
                for key in RUN_INPUT:
                    if key not in row:
                        continue
                    if (
                        key == "started_at"
                        and row[key] is None
                        and existing
                        and existing.get("started_at") is not None
                    ):
                        continue
                    merged[key] = row[key]
                merged["label"] = clean_label(merged.get("label"))
                if not merged.get("status"):
                    merged["status"] = "unknown"
                workstream, confidence = self._attribute_locked(
                    merged.get("label"),
                    merged.get("session_id"),
                    merged.get("parent_session_id"),
                    run_id=merged.get("run_id"),
                )
                if (
                    workstream is None
                    and confidence is None
                    and existing
                    and existing.get("ws_confidence") in ("inherited", "inferred")
                    and not is_hub_label(merged.get("label"))
                ):
                    # No evidence right now is not evidence of absence: a
                    # settled inference survives the write, and the inherit
                    # refresh (with poll-now evidence) remains the sole
                    # clearer. Explicit attributions always overwrite. Hub
                    # labels are exempt: inherited work never survives on
                    # hub-scoped work.
                    workstream = existing.get("workstream")
                    confidence = existing.get("ws_confidence")
                merged["workstream"] = workstream
                merged["ws_confidence"] = confidence
                if merged.get("tier") == merged.get("source") and merged.get("source") in (
                    "pi",
                    "generic",
                ):
                    # The tier still names the source: the row was
                    # inserted model-less and the model arrived later
                    # (backfill, re-ingest). Retier from the model
                    # family so pi rows do not read as their own bucket.
                    fixed = _family_tier(merged.get("source"), merged.get("model"))
                    if fixed:
                        merged["tier"] = fixed
                if "out_bytes" in row:
                    old = (existing or {}).get("out_bytes") or 0
                    new = row.get("out_bytes") or 0
                    merged["out_growing"] = 1 if existing and new > old else 0
                elif "out_growing" not in merged:
                    merged["out_growing"] = 0
                changed_ws = not existing or existing.get("workstream") != merged.get("workstream")
                if existing and _same(existing, merged, RUN_COLS):
                    return
                view_changed = not existing or not _same(existing, merged, RUN_VIEW_COLS)
                if _is_manifest_twin(merged):
                    twin_id = self._find_merge_target_locked(merged)
                    if twin_id is not None:
                        dst = self._one("SELECT * FROM runs WHERE run_id=?", (twin_id,))
                        if dst is not None:
                            family = _manifest_family(run_id)
                            revive = (
                                _time.time()
                                if family in ("muse", "glm")
                                else None
                            )
                            self._fold_locked(run_id, twin_id, merged, dict(dst), revive_at=revive)
                            return
                self._write_run(merged)
                if changed_ws:
                    for hour in self._hours_for_run(run_id):
                        self._rebuild_hour(hour)
                self._seq += 1
                self._data_seq += 1
                self._note_data_write()
                if view_changed:
                    self._note_view_write()
        def reassign_run(self, src_id: str, dst_id: str) -> bool:
            """Move usage from ``src_id`` onto ``dst_id``, fill empty fields, delete ``src_id``."""
            if not src_id or not dst_id or src_id == dst_id:
                return False
            with self._lock:
                src = self._one("SELECT * FROM runs WHERE run_id=?", (src_id,))
                dst = self._one("SELECT * FROM runs WHERE run_id=?", (dst_id,))
                if not src or not dst:
                    return False
                merged = dict(dst)
                for key in (
                    "model",
                    "started_at",
                    "clock_source",
                    "verdict",
                    "out_bytes",
                    "err_bytes",
                    "out_path",
                    "pid",
                ):
                    if merged.get(key) in (None, "") and src.get(key) not in (None, ""):
                        merged[key] = src[key]
                workstream, confidence = self._attribute_locked(
                    merged.get("label"),
                    merged.get("session_id"),
                    merged.get("parent_session_id"),
                    run_id=merged.get("run_id"),
                )
                merged["workstream"] = workstream
                merged["ws_confidence"] = confidence
                hours = self._hours_for_run(src_id) | self._hours_for_run(dst_id)
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.execute(
                        "UPDATE usage_events SET run_id=? WHERE run_id=?",
                        (dst_id, src_id),
                    )
                    self._move_rollup_locked(src_id, dst_id)
                    self._conn.execute(
                        "UPDATE process_samples SET run_id=? WHERE run_id=?",
                        (dst_id, src_id),
                    )
                    last = self._conn.execute(
                        "SELECT MAX(ts) AS ts FROM usage_events WHERE run_id=? AND ts IS NOT NULL AND COALESCE(tool, '') != ?",
                        (dst_id, _ESTIMATE_TOOL),
                    ).fetchone()
                    if (
                        merged.get("status") != "running"
                        and merged.get("ended_at") is not None
                        and last
                        and last["ts"] is not None
                        and float(last["ts"]) > float(merged["ended_at"])
                    ):
                        merged["ended_at"] = float(last["ts"])
                    self._write_run(merged)
                    self._conn.execute("DELETE FROM runs WHERE run_id=?", (src_id,))
                    for hour in hours:
                        self._rebuild_hour(hour)
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                self._seq += 1
                self._data_seq += 1
                self._note_data_write()
                self._note_view_write()
                self._bump_usage_run(src_id)
                self._bump_usage_run(dst_id)
                self._bump_run_version(src_id)
            return True
        def repair_grok_runs(self, now: float, window_s: float = MERGE_WINDOW_S) -> tuple[int, int]:
            """Fill NULL grok models from usage and fold manifest twins into chat rows.

            The cursor source calls this every poll. The manifest merge runs
            every time (it is one indexed scan and converges to no work); the
            usage backfill runs at most once a minute. Every fold records a
            durable alias, so the first poll after a restart backfills aliases
            for whatever the manifest re-read resurrected and later restarts
            resolve instead of re-inserting. Returns ``(models_filled, merged)``.
            """
            return self.repair_manifest_runs("grok", now, window_s)
        def repair_manifest_runs(self, source: str, now: float, window_s: float = MERGE_WINDOW_S) -> tuple[int, int]:
            """Fill NULL ``source`` models from usage and fold manifest twins.

            Same repair as ``repair_grok_runs`` for one source family: the
            muse source calls this with ``"muse"`` every poll, the GLM
            source with ``"glm"``. Returns ``(models_filled, merged)``.
            """
            merged = self.merge_manifest_rows(source, window_s, now=now if source != "grok" else None)
            filled = self.backfill_tool_models(source, now)
            return filled, merged
        def backfill_grok_models(self, now: float) -> int:
            """Fill a NULL-model grok run from its latest usage event model.

            Usage-line models win everywhere else, so a run that learned its
            model at the end keeps it here too. Runs with no usage model stay
            NULL ("model not recorded"). Returns how many rows changed.
            """
            return self.backfill_tool_models("grok", now)
        def backfill_tool_models(self, source: str, now: float) -> int:
            """Fill a NULL-model ``source`` run from its latest usage event model.

            Same backfill as ``backfill_grok_models`` for one source family.
            Returns how many rows changed.
            """
            now = float(now)
            with self._lock:
                row = self._conn.execute(
                    "SELECT value FROM meta WHERE key=?", (f"{source}_models_ts",)
                ).fetchone()
                if row and row["value"]:
                    try:
                        if now - float(row["value"]) < 60.0:
                            return 0
                    except (TypeError, ValueError):
                        pass
                pending = [
                    dict(item)
                    for item in self._conn.execute(
                        "SELECT run_id FROM runs WHERE source=? AND (model IS NULL OR model='')",
                        (source,),
                    ).fetchall()
                ]
            filled = 0
            for item in pending:
                with self._lock:
                    hit = self._conn.execute(
                        """SELECT model FROM usage_events
                           WHERE run_id=? AND model IS NOT NULL AND model != ''
                           ORDER BY ts IS NULL, ts DESC LIMIT 1""",
                        (item["run_id"],),
                    ).fetchone()
                if not hit:
                    continue
                self.upsert_run({"run_id": item["run_id"], "model": hit["model"]})
                filled += 1
            with self._lock:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"{source}_models_ts", str(now)),
                )
            return filled
        def merge_grok_manifests(self, window_s: float = MERGE_WINDOW_S) -> int:
            """Fold launcher manifest rows into the chat row they record.

            A ``cursor-sessions.tsv`` row becomes ``grok:<id>:cursor/<label>``
            while the Cursor chat itself is ``grok:<chat-uuid>:<label>``. A
            manifest row whose label matches a non-manifest grok row that
            started within ``window_s`` (default 15 minutes, wider than the ten
            minutes views use) is that same run: the merged row keeps the
            earliest start, the chat row's status, the model, and the
            manifest's out path; usage and samples move over, the manifest
            row is deleted, and a durable alias (manifest id to chat id) is
            recorded in the same transaction so a restart re-ingest of the TSV
            row gap-fills the chat row instead of resurrecting the manifest
            row. A manifest row with no twin stays. Returns how many rows
            merged.
            """
            return self.merge_manifest_rows("grok", window_s)
        def merge_manifest_rows(self, source: str, window_s: float = MERGE_WINDOW_S, now=None) -> int:
            """Fold launcher manifest rows of one source into their worker row.

            Same merge as ``merge_grok_manifests`` for one source family. A
            muse ``muse-sessions.tsv`` row without a session uuid becomes
            ``muse:<parent>:<label>@<epoch>`` while the session itself is
            ``muse:<uuid>:<label>`` (likewise ``glm``): a manifest row whose
            label matches a non-manifest row of the same source that started
            within ``window_s`` is that same run and folds the same way
            (earliest start, gap-fill, usage and samples moved, durable alias
            in the same transaction). ``now`` enables the liveness revival
            for muse/glm (a running manifest row with fresh process evidence
            reopens a wrongly closed worker row); the grok path never revives,
            exactly as before. Returns how many rows merged.
            """
            revive = now if source in ("muse", "glm") else None
            with self._lock:
                manifests = [
                    row
                    for row in (
                        dict(item)
                        for item in self._conn.execute(
                            "SELECT * FROM runs WHERE source=?", (source,)
                        ).fetchall()
                    )
                    if _is_manifest_twin(row)
                ]
            merged = 0
            for manifest in manifests:
                with self._lock:
                    twins = [
                        row
                        for row in (
                            dict(item)
                            for item in self._conn.execute(
                                "SELECT * FROM runs WHERE source=? AND label=? AND run_id != ?",
                                (source, manifest.get("label"), manifest["run_id"]),
                            ).fetchall()
                        )
                        if not _is_manifest_twin(row)
                    ]
                twin = _nearest_twin(manifest, twins, window_s)
                if twin is None:
                    continue
                if self._merge_run_into(manifest["run_id"], twin["run_id"], revive_at=revive):
                    merged += 1
            return merged
        def resolve_run_id(self, run_id: str) -> str:
            """Live run id for ``run_id``, following merge aliases.

            A manifest row folded into its chat row keeps its id as an alias,
            so a restart re-ingest of the same TSV row resolves to the chat row
            instead of resurrecting a deleted row. Unknown ids return unchanged.
            """
            if _manifest_family(run_id) is None:
                return run_id
            with self._lock:
                return self._resolve_alias_locked(run_id)
        def _resolve_alias_locked(self, run_id: str) -> str:
            """Follow ``run_aliases`` to the live row. Caller holds the lock."""
            current = run_id
            seen = set()
            while isinstance(current, str) and current not in seen:
                seen.add(current)
                hit = self._conn.execute(
                    "SELECT target_run_id FROM run_aliases WHERE alias_run_id=?",
                    (current,),
                ).fetchone()
                if not hit or not hit["target_run_id"]:
                    break
                current = hit["target_run_id"]
            return current
        def _find_merge_target_locked(self, manifest: dict):
            """Worker row the merge would fold ``manifest`` into, or None.

            Same rule as ``merge_manifest_rows``: the nearest non-manifest
            same-label row of the same source starting within
            ``MERGE_WINDOW_S``. Caller holds the lock.
            """
            label = manifest.get("label")
            run_id = manifest.get("run_id")
            source = manifest.get("source")
            if not label or not run_id or not source:
                return None
            twins = [
                dict(item)
                for item in self._conn.execute(
                    "SELECT * FROM runs WHERE source=? AND label=? AND run_id != ?",
                    (source, label, run_id),
                ).fetchall()
                if not _is_manifest_twin(dict(item))
            ]
            best = _nearest_twin(manifest, twins, MERGE_WINDOW_S)
            return best["run_id"] if best is not None else None
        def _upsert_aliased_locked(self, alias_id: str, target_id: str, row: dict) -> bool:
            """Gap-fill the merge target for a write to an aliased run id.

            Returns True when the target exists and absorbed the write (no row
            is created for the alias). Returns False when the target is gone:
            the dangling alias is dropped and the caller falls through to the
            normal upsert. Caller holds the lock.
            """
            dst = self._one("SELECT * FROM runs WHERE run_id=?", (target_id,))
            if dst is None:
                self._conn.execute(
                    "DELETE FROM run_aliases WHERE alias_run_id=?", (alias_id,)
                )
                return False
            src_row = self._one("SELECT * FROM runs WHERE run_id=?", (alias_id,))
            base = dict(src_row) if src_row else {}
            for key, value in row.items():
                if value is not None:
                    base[key] = value
            base["run_id"] = alias_id
            family = _manifest_family(alias_id)
            revive = _time.time() if family in ("muse", "glm") else None
            self._fold_locked(alias_id, target_id, base, dict(dst), revive_at=revive)
            return True
        def _merge_run_into(self, src_id: str, dst_id: str, revive_at=None) -> bool:
            """Fold manifest row ``src_id`` into worker row ``dst_id`` and delete it.

            ``revive_at`` (an epoch) enables the liveness revival for
            muse/glm pairs; the grok path never revives.
            """
            if not src_id or not dst_id or src_id == dst_id:
                return False
            with self._lock:
                src = self._one("SELECT * FROM runs WHERE run_id=?", (src_id,))
                dst = self._one("SELECT * FROM runs WHERE run_id=?", (dst_id,))
                if not src or not dst:
                    return False
                self._fold_locked(src_id, dst_id, src, dst, revive_at=revive_at)
            return True
        def _fold_revive_locked(self, alias_id: str, target_id: str, merged: dict, now: float) -> bool:
            """True when fresh process evidence says the folded run is alive.

            A manifest row the source marked ``running`` carries a liveness
            claim: the source only emits that with fresh evidence (out growth
            or a live label). When a process sample for either folded id —
            or for their label, when no newer open same-label row owns it —
            is fresher than ``PROCESS_GONE_GRACE_S``, the target's closed
            status is stale (a quiet file closed a live worker) and the
            merge reopens it. Caller holds the lock.
            """
            try:
                now = float(now)
            except (TypeError, ValueError):
                return False
            try:
                hit = self._conn.execute(
                    "SELECT MAX(ts) AS ts FROM process_samples WHERE run_id=? OR run_id=?",
                    (alias_id, target_id),
                ).fetchone()
                if hit and hit["ts"] is not None and now - float(hit["ts"]) <= PROCESS_GONE_GRACE_S:
                    return True
                label = merged.get("label")
                if not label:
                    return False
                hit = self._conn.execute(
                    "SELECT MAX(ts) AS ts FROM process_samples WHERE label=?", (label,)
                ).fetchone()
                if hit is None or hit["ts"] is None:
                    return False
                if now - float(hit["ts"]) > PROCESS_GONE_GRACE_S:
                    return False
                twin = self.newer_open_label_row(
                    label, merged.get("source"), merged.get("started_at"), target_id
                )
                return twin is None
            except (TypeError, ValueError, AttributeError):
                return False
        def _fold_locked(self, alias_id: str, target_id: str, src: dict, dst: dict, revive_at=None) -> None:
            """Fold manifest data into its worker row and record the alias.

            The alias row is written in the same transaction that moves the
            usage and samples and deletes the manifest row, so a restart can
            never see the row without the alias. ``src`` may be a stored row
            (merge pass) or a fresh re-ingest patch overlaid on it (restart
            re-read); when no stored row exists the usage moves are no-ops.
            ``revive_at`` reopens a wrongly closed muse/glm worker row when
            the folded manifest row is running with fresh process evidence;
            without it the target status is never touched, exactly as before.
            Caller holds the lock.
            """
            merged = _fold_manifest_patch(dst, src)
            if (
                revive_at is not None
                and _manifest_family(alias_id) in ("muse", "glm")
                and src.get("status") == "running"
                and merged.get("status") not in ("running", "queued")
                and self._fold_revive_locked(alias_id, target_id, merged, revive_at)
            ):
                merged["status"] = "running"
                merged["ended_at"] = None
            workstream, confidence = self._attribute_locked(
                merged.get("label"),
                merged.get("session_id"),
                merged.get("parent_session_id"),
                run_id=merged.get("run_id"),
            )
            merged["workstream"] = workstream
            merged["ws_confidence"] = confidence
            hours = self._hours_for_run(alias_id) | self._hours_for_run(target_id)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE usage_events SET run_id=? WHERE run_id=?",
                    (target_id, alias_id),
                )
                self._move_rollup_locked(alias_id, target_id)
                self._conn.execute(
                    "UPDATE process_samples SET run_id=? WHERE run_id=?",
                    (target_id, alias_id),
                )
                self._write_run(merged)
                self._conn.execute("DELETE FROM runs WHERE run_id=?", (alias_id,))
                self._bump_run_version(alias_id)
                self._conn.execute(
                    """INSERT INTO run_aliases(alias_run_id, target_run_id, merged_at)
                       VALUES(?,?,?)
                       ON CONFLICT(alias_run_id) DO UPDATE SET
                         target_run_id=excluded.target_run_id,
                         merged_at=excluded.merged_at""",
                    (alias_id, target_id, _time.time()),
                )
                for hour in hours:
                    self._rebuild_hour(hour)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._seq += 1
            self._data_seq += 1
            self._note_view_write()
            self._bump_usage_run(alias_id)
            self._bump_usage_run(target_id)
        def repair_muse_orphans(self) -> int:
            """Move orphan muse usage onto the labeled run and delete the orphan."""
            with self._lock:
                rows = [
                    dict(row)
                    for row in self._conn.execute("SELECT * FROM runs WHERE source='muse'").fetchall()
                ]
            moved = 0
            for src_id, dst_id in muse_orphan_pairs(rows):
                if self.reassign_run(src_id, dst_id):
                    moved += 1
            return moved
        def repair_commandcode_orphans(self) -> int:
            """Move orphan Command Code usage onto the labeled run and delete the orphan.

            Usage that arrives before the manifest links the label is
            filed under ``commandcode:<sid>:<sid>``; once the TSV row
            names the label, the usage belongs on
            ``commandcode:<sid>:<label>``. Same pattern as
            :meth:`repair_muse_orphans`.
            """
            with self._lock:
                rows = [
                    dict(row)
                    for row in self._conn.execute("SELECT * FROM runs WHERE source='commandcode'").fetchall()
                ]
            moved = 0
            for src_id, dst_id in commandcode_orphan_pairs(rows):
                if self.reassign_run(src_id, dst_id):
                    moved += 1
            return moved
        def repair_stale_tiers(self) -> int:
            """Retier pi/generic runs whose tier still names the source.

            One-time healing for rows inserted model-less (tier fell
            back to the source name) and later given a model without a
            tier update: the family tier comes from the model, so the
            rows stop reading as their own ``pi`` bucket. Idempotent:
            a second pass finds nothing to change. Returns how many
            rows changed.
            """
            with self._lock:
                rows = [
                    dict(row)
                    for row in self._conn.execute(
                        "SELECT run_id, source, tier, model FROM runs "
                        "WHERE source IN ('pi', 'generic') AND tier = source "
                        "AND model IS NOT NULL AND model != ''"
                    ).fetchall()
                ]
            changed = 0
            for row in rows:
                fixed = _family_tier(row.get("source"), row.get("model"))
                if not fixed:
                    continue
                self.upsert_run({"run_id": row["run_id"], "tier": fixed})
                changed += 1
            return changed
        def repair_muse_clocks(self) -> int:
            """Fill a missing muse start from the earliest usage event.

            A finished row whose end is earlier than its latest usage event is
            raised to that event. A running row stays open. Returns how many
            rows changed.
            """
            with self._lock:
                runs = [
                    dict(row)
                    for row in self._conn.execute(
                        "SELECT run_id, status, started_at, ended_at, clock_source FROM runs WHERE source='muse'"
                    )
                ]
            changed = 0
            for run in runs:
                with self._lock:
                    usage = self._conn.execute(
                        """SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts
                           FROM usage_events
                           WHERE run_id=? AND ts IS NOT NULL AND COALESCE(tool, '') != ?""",
                        (run["run_id"], _ESTIMATE_TOOL),
                    ).fetchone()
                patch = {"run_id": run["run_id"]}
                if run.get("started_at") is None and usage and usage["first_ts"] is not None:
                    patch["started_at"] = float(usage["first_ts"])
                    if not run.get("clock_source"):
                        patch["clock_source"] = "muse_log"
                if (
                    run.get("status") != "running"
                    and run.get("ended_at") is not None
                    and usage
                    and usage["last_ts"] is not None
                    and float(usage["last_ts"]) > float(run["ended_at"])
                ):
                    patch["ended_at"] = float(usage["last_ts"])
                if len(patch) == 1:
                    continue
                self.upsert_run(patch)
                changed += 1
            return changed
        def find_marker_runs(self) -> list:
            """Placeholder run rows safe to prune: worker-source rows with no model, no verdict, no usage history, and no merge-alias reference, never currently running.

            This is the durable twin of :func:`is_marker_run`: the read-time
            predicate works off run views (whose ``tokens`` fold stands in
            for the usage leg), while this lists the exact stored rows the
            prune command may delete. A row with any usage event stays even
            when its billed tokens are zero; so does a running row, a row
            named by ``run_aliases``, a ``killed``/``died_early`` row (which
            reads as ``died``), and of course any row with tokens, a
            model, or a verdict.
            """
            with self._lock:
                rows = [
                    dict(row)
                    for row in self._conn.execute(
                        """SELECT run_id, source, label, status, started_at, ended_at
                           FROM runs
                           WHERE source IN ('muse', 'glm', 'grok')
                             AND (model IS NULL OR model = '')
                             AND (verdict IS NULL OR verdict = '')
                             AND status != 'running'
                             AND status NOT IN ('killed', 'died_early')
                             AND NOT EXISTS (
                                 SELECT 1 FROM usage_events
                                 WHERE usage_events.run_id = runs.run_id)
                             AND NOT EXISTS (
                                 SELECT 1 FROM run_aliases
                                 WHERE alias_run_id = runs.run_id
                                    OR target_run_id = runs.run_id)"""
                    ).fetchall()
                ]
            return sorted(rows, key=lambda row: row["run_id"] or "")
        def prune_marker_runs(self, dry_run: bool = True) -> dict:
            """Delete placeholder run rows, or list them when ``dry_run``.

            Every candidate from :meth:`find_marker_runs` is re-verified
            in Python before deletion (no model, no verdict, no usage
            billed tokens, not running), so a row that gained evidence
            since the listing survives. Returns ``{"markers", "deleted",
            "backup"}`` with the listed rows, the deleted count (0 on a
            dry run), and no backup path (the caller copies the database
            first). Usage buckets are untouched: markers never have usage
            events, so token and cost totals cannot move.
            """
            markers = self.find_marker_runs()
            if dry_run:
                return {"markers": markers, "deleted": 0, "backup": None}
            deleted = 0
            with self._lock:
                for row in markers:
                    current = self._one(
                        "SELECT * FROM runs WHERE run_id=?", (row["run_id"],)
                    )
                    if current is None:
                        continue
                    if current.get("model") or current.get("verdict"):
                        continue
                    if (current.get("status") or "") == "running":
                        continue
                    usage = self._conn.execute(
                        """SELECT COALESCE(SUM(COALESCE(input_tokens, 0)
                                            + COALESCE(output_tokens, 0)
                                            + COALESCE(cache_read, 0)
                                            + COALESCE(cache_write, 0)), 0) AS tokens
                           FROM usage_events WHERE run_id=?""",
                        (row["run_id"],),
                    ).fetchone()
                    if usage and (usage["tokens"] or 0):
                        continue
                    alias = self._conn.execute(
                        """SELECT 1 FROM run_aliases
                           WHERE alias_run_id=? OR target_run_id=?""",
                        (row["run_id"], row["run_id"]),
                    ).fetchone()
                    if alias:
                        continue
                    self._conn.execute(
                        "DELETE FROM runs WHERE run_id=?", (row["run_id"],)
                    )
                    deleted += 1
                self._conn.commit()
                self._seq += 1
                self._data_seq += 1
                self._note_data_write()
                self._note_view_write()
                if deleted:
                    self._bump_run_epoch()
            return {"markers": markers, "deleted": deleted, "backup": None}
        def query_runs(self, now: float, ws: str | None, since: float | None, until: float | None, limit: int, include_jobs: bool = False) -> list:
            """Gantt input. Open bars come from the one filtered running set, so a
            stale row the snapshot hides never shows as an open bar here. Production
            (dagster) rows are left out unless ``include_jobs`` is set; lead and
            shell rows stay in."""
            probes = self._probe_liveness(file_mtimes=True)
            check = self._probe_checker(probes)
            with self._lock:
                raw, usage, parents = self._scoped_usage()
                context = self._context_by_run()
                rss = self._rss_maps(now)
                # raw rows are already fresh dicts; evidence consumers
                # only read them, so no second copy.
                usage_max, sample_max, pid_starts = self._open_evidence_locked(raw)
                live_open, shadows = self._live_open_with_shadows_locked(
                    now, raw, check, usage_max, sample_max, pid_starts)
                stalled = self._stalled_locked(
                    now, raw, check, usage_max, sample_max, probes["mtimes"]
                )
                usage_versions = dict(self._usage_fold_versions)
                children_of: dict = {}
                for child, parent in parents.items():
                    children_of.setdefault(parent, []).append(child)
                live_or_shadow = live_open | set(shadows)
                rows = []
                for row in raw:
                    # Cheap raw-row gates first: views are only built for
                    # rows the old code would keep (identical predicates on
                    # identical values: views copy these fields verbatim).
                    # The marker check stays on the view: it needs the
                    # usage fold, which raw rows do not carry.
                    if row["run_id"] in parents:
                        continue
                    if not include_jobs and (row.get("source") or "") == "dagster":
                        continue
                    if not _in_ws(row, ws):
                        continue
                    status = row.get("status")
                    if (
                        status in ("running", "queued")
                        and row["run_id"] not in live_or_shadow
                    ):
                        continue
                    if since is not None and (row.get("ended_at") or now) < since:
                        continue
                    if until is not None and (row.get("started_at") or 0) > until:
                        continue
                    view = self._cached_view(
                        row, now, usage, rss, usage_versions, context, stalled,
                        children_of,
                    )
                    if is_marker_run(view):
                        continue
                    rows.append(view)
                rows = _annotate_lanes(rows, live_or_shadow)
            rows = _collapse_workflows(_drop_launch_echoes(rows), now)
            rows.sort(key=lambda run: run["started_at"] or 0)
            return rows[-limit:]


def _is_manifest_twin(row: dict) -> bool:
    """A launcher-manifest row.

    Cursor rows are ``grok:<id>:cursor/<label>`` (chat run ids are
    ``grok:<chat-uuid>:<label>`` and labels never produce a ``:cursor/``
    segment, so the marker names them exactly). Muse/glm rows without a
    session uuid are ``<source>:<parent>:<label>@<epoch>`` of the same
    source (worker-row labels never end in ``@<digits>``). Either marker
    needs ``clock_source == "manifest"``.
    """
    if row.get("clock_source") != "manifest":
        return False
    run_id = row.get("run_id")
    if not isinstance(run_id, str):
        return False
    if ":cursor/" in run_id:
        return True
    family = _manifest_family(run_id)
    return family is not None and row.get("source") == family


def _fold_manifest_patch(target: dict, incoming: dict) -> dict:
    """Gap-fill ``target`` (chat row) with ``incoming`` (manifest data).

    The merged row keeps the earliest start (taking the manifest clock with
    it), the chat row's status, and fills an empty model, verdict, out
    path, and byte counts from the manifest; a finished row extends its end
    to a later manifest end. Session, pid, label, and status are never
    touched, so a restart re-ingest of a merged row cannot reopen, restamp,
    or relabel the live row. Shared by the merge pass and the aliased
    re-ingest path so both fold identically.
    """
    merged = dict(target)
    src_start = _epoch(incoming.get("started_at"))
    dst_start = _epoch(target.get("started_at"))
    if src_start is not None and (dst_start is None or src_start < dst_start):
        merged["started_at"] = src_start
        if incoming.get("clock_source"):
            merged["clock_source"] = incoming["clock_source"]
    for key in ("model", "verdict", "out_path", "out_bytes", "err_bytes"):
        if merged.get(key) in (None, "", 0) and incoming.get(key) not in (None, "", 0):
            merged[key] = incoming[key]
    src_end = _epoch(incoming.get("ended_at"))
    dst_end = _epoch(target.get("ended_at"))
    if (
        merged.get("status") != "running"
        and src_end is not None
        and (dst_end is None or src_end > dst_end)
    ):
        merged["ended_at"] = src_end
    return merged


def _nearest_twin(manifest: dict, twins: list, window_s: float):
    """Closest non-manifest twin by start time within ``window_s``.

    Both starts must be known. Shadow links (one row's parent pointing at
    the other run) are not twins.
    """
    base = _epoch(manifest.get("started_at"))
    if base is None or not manifest.get("label"):
        return None
    best = None
    best_delta = None
    for twin in twins:
        if twin.get("run_id") == manifest.get("run_id"):
            continue
        if twin.get("parent_session_id") == manifest.get("run_id"):
            continue
        if manifest.get("parent_session_id") == twin.get("run_id"):
            continue
        start = _epoch(twin.get("started_at"))
        if start is None:
            continue
        delta = abs(start - base)
        if delta <= window_s and (best is None or delta < best_delta):
            best = twin
            best_delta = delta
    return best


def muse_orphan_pairs(runs: list) -> list:
    """Return ``(orphan_run_id, labeled_run_id)`` for muse sessions that have both.

    An orphan is an unlabeled ``muse:<sid>:<sid>`` row. The labeled side is the
    latest sibling that has a label.
    """
    groups: dict = {}
    for row in runs:
        source = row.get("source")
        if source not in (None, "muse"):
            rid = row.get("run_id")
            if not (isinstance(rid, str) and rid.startswith("muse:")):
                continue
        sid = row.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        groups.setdefault(sid.lower(), []).append(row)
    pairs = []
    for group in groups.values():
        labeled = [row for row in group if row.get("label")]
        orphans = [row for row in group if _muse_orphan(row)]
        if not labeled or not orphans:
            continue
        labeled.sort(key=lambda row: row.get("started_at") or 0, reverse=True)
        dst = labeled[0].get("run_id")
        if not dst:
            continue
        for src in orphans:
            src_id = src.get("run_id")
            if src_id and src_id != dst:
                pairs.append((src_id, dst))
    return pairs


def _muse_orphan(row: dict) -> bool:
    if row.get("label"):
        return False
    sid = row.get("session_id")
    rid = row.get("run_id")
    if not isinstance(sid, str) or not isinstance(rid, str):
        return False
    parts = rid.split(":")
    if len(parts) != 3 or parts[0] != "muse":
        return False
    return parts[1].lower() == sid.lower() and parts[2].lower() == sid.lower()


def commandcode_orphan_pairs(runs: list) -> list:
    """Return ``(orphan_run_id, labeled_run_id)`` for Command Code sessions that have both.

    An orphan is an unlabeled ``commandcode:<sid>:<sid>`` row: usage
    that arrived before the manifest linked the label. The labeled side
    is the latest sibling that has a label.
    """
    groups: dict = {}
    for row in runs:
        source = row.get("source")
        if source not in (None, "commandcode"):
            rid = row.get("run_id")
            if not (isinstance(rid, str) and rid.startswith("commandcode:")):
                continue
        sid = row.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        groups.setdefault(sid.lower(), []).append(row)
    pairs = []
    for group in groups.values():
        labeled = [row for row in group if row.get("label")]
        orphans = [row for row in group if _commandcode_orphan(row)]
        if not labeled or not orphans:
            continue
        labeled.sort(key=lambda row: row.get("started_at") or 0, reverse=True)
        dst = labeled[0].get("run_id")
        if not dst:
            continue
        for src in orphans:
            src_id = src.get("run_id")
            if src_id and src_id != dst:
                pairs.append((src_id, dst))
    return pairs


def _family_tier(source, model) -> str | None:
    """Family tier for a run whose tier still names its source.

    pi runs take the registry tier that names their model family
    (``pi-deepseek``, ``pi-beam``, ``pi-glm``), which is what
    ``run_worker.py`` records, so a healed row reads the same tier a
    fresh launch does; generic runs take the bare family. generic
    additionally takes the source's own tier names (opus, muse, ...)
    through a lazy import, the same pattern as ``dash.store.running``.
    Returns None when the model implies no better tier, so callers keep
    the old value. The family branch mirrors
    ``dash.sources.pi._model_tier`` (kept local so the store never
    imports a source at module level).
    """
    text = model.lower() if isinstance(model, str) else ""
    if not text:
        return None
    if source in ("pi", "generic"):
        # pi mirrors dash.sources.pi._model_tier's families (beam, glm,
        # deepseek), spelled as the registry names them; generic takes
        # the same families first (tier_of does not know deepseek) and
        # falls back to its own tier names below.
        for name in ("deepseek", "beam", "glm"):
            if name in text:
                return "pi-" + name if source == "pi" else name
    if source == "generic":
        from dash.sources.generic import tier_of

        tier = tier_of(model)
        return tier if tier != "generic" else None
    return None


def _commandcode_orphan(row: dict) -> bool:
    if row.get("label"):
        return False
    sid = row.get("session_id")
    rid = row.get("run_id")
    if not isinstance(sid, str) or not isinstance(rid, str):
        return False
    parts = rid.split(":")
    if len(parts) != 3 or parts[0] != "commandcode":
        return False
    return parts[1].lower() == sid.lower() and parts[2].lower() == sid.lower()


def _parent_map(runs: list) -> dict:
    """Child run id -> the run that should carry its tokens.

    A grok child is a chat whose ``parent_session_id`` is another grok run
    (or that run's chat id), or an unlabeled row for a chat that already has
    a labeled run. Hub session ids are not grok chat ids, so a launched agent
    keeps its own count.

    A muse shadow is the unlabeled ``muse:<uuid>:<uuid>`` row for a session
    that also has a labeled run. Its tokens roll onto that labeled run.
    A labeled muse run whose parent is a hub session id stays its own row.
    """
    by_id = {run["run_id"]: run for run in runs}
    grok_by_session = {}
    grok_by_sid = {}
    for run in runs:
        if run.get("source") != "grok" or not run.get("session_id"):
            continue
        grok_by_sid.setdefault(run["session_id"], []).append(run)
        prev = grok_by_session.get(run["session_id"])
        if prev is None or (not prev.get("label") and run.get("label")):
            grok_by_session[run["session_id"]] = run
    parents = {}
    for run in runs:
        if run.get("source") != "grok":
            continue
        parent = run.get("parent_session_id")
        host = by_id.get(parent) if parent else None
        if host and host["run_id"] != run["run_id"]:
            parents[run["run_id"]] = host["run_id"]
            continue
        if parent:
            host = grok_by_session.get(parent)
            if host and host["run_id"] != run["run_id"]:
                parents[run["run_id"]] = host["run_id"]
                continue
        if not run.get("label") and run.get("session_id"):
            labeled = [
                item
                for item in grok_by_sid.get(run["session_id"], [])
                if item.get("label") and item["run_id"] != run["run_id"]
            ]
            if labeled:
                parents[run["run_id"]] = labeled[0]["run_id"]
    muse_by_sid = {}
    for run in runs:
        if run.get("source") != "muse" or not run.get("session_id"):
            continue
        key = run["session_id"].lower() if isinstance(run["session_id"], str) else run["session_id"]
        muse_by_sid.setdefault(key, []).append(run)
    for run in runs:
        if run.get("source") != "muse":
            continue
        parent = run.get("parent_session_id")
        host = by_id.get(parent) if isinstance(parent, str) else None
        if host and host.get("source") == "muse" and host["run_id"] != run["run_id"]:
            parents[run["run_id"]] = host["run_id"]
            continue
        if run.get("label") or not run.get("session_id"):
            continue
        key = run["session_id"].lower() if isinstance(run["session_id"], str) else run["session_id"]
        labeled = [
            item
            for item in muse_by_sid.get(key, [])
            if item.get("label") and item["run_id"] != run["run_id"]
        ]
        if not labeled:
            continue
        labeled.sort(key=lambda item: item.get("started_at") or 0, reverse=True)
        parents[run["run_id"]] = labeled[0]["run_id"]
    return parents


def _rollup_usage(usage: dict, parents: dict) -> dict:
    for child, parent in parents.items():
        if child == parent or child not in usage:
            continue
        usage[parent] = _merge_usage(usage.get(parent), usage[child])
        usage[child] = (_zeros(), 0.0, False)
    return usage


def _merge_usage(left, right):
    if left is None:
        return right
    if right is None:
        return left
    left_tokens, left_cost, left_unpriced = left
    right_tokens, right_cost, right_unpriced = right
    folded = {}
    for key in TOKEN_FIELDS:
        left_value = left_tokens.get(key)
        right_value = right_tokens.get(key)
        if left_value is None and right_value is None:
            folded[key] = None
        else:
            folded[key] = (left_value or 0) + (right_value or 0)
    if left_cost is None and right_cost is None:
        cost = None
    else:
        cost = (left_cost or 0) + (right_cost or 0)
    return folded, cost, bool(left_unpriced or right_unpriced)
