"""Lead-session workstream slices: reads and maintenance writes.

Owns the per-workstream slice rows for lead sessions that worked
several workstreams (``dash/lead_split.py`` holds the pure rules).
Public methods run under the facade lock; ``_locked`` helpers assume
it and never take it.

Data model: the existing ``claude-main:<sid>:main`` row stays the
primary. Each material second workstream gains one slice run
``claude-main:<sid>:main@WS<n>`` whose usage rows are moved out of
the primary (never copied), so every token is counted once
everywhere. ``runs.slice_of`` links a slice to its primary run id.
Slices carry source ``claude-main`` with no parent, so the ``lead``
phase rule classifies them as planning like the primary, and they
inherit the primary's status and model. Lead sources are never
counted agents, so slices cannot inflate ``live_agents``.
"""

from __future__ import annotations

from dash import lead_split
from dash.lead_split import BILLED_KINDS

from .util import TOKEN_FIELDS, WORKER_SOURCES


class SplitMixin:
        def split_rules_version(self, session_id: str):
            """Stored signal-rules version for ``session_id``, or None."""
            from dash import lead_split

            if not isinstance(session_id, str) or not session_id.strip():
                return None
            key = f"lead_signal_rules:{session_id.strip()}"
            with self._lock:
                try:
                    self._conn.execute(
                        "CREATE TABLE IF NOT EXISTS meta"
                        " (key TEXT PRIMARY KEY, value TEXT)"
                    )
                    row = self._conn.execute(
                        "SELECT value FROM meta WHERE key=?", (key,)
                    ).fetchone()
                except Exception:
                    return None
            if row is None:
                return None
            try:
                return int(row[0])
            except (TypeError, ValueError):
                return None
        def ensure_split_rules(self, session_id: str) -> bool:
            """Rebuild one session's slices/votes when rules changed.

            Compares the stored per-session marker against
            ``lead_split.SIGNAL_RULES_VERSION``; on mismatch (or a
            missing marker) moves that session's slice rows back to
            the primary, nulls its per-turn ``ws_split``, deletes its
            slice runs, its subagent votes and spawn links, and its
            file offsets, then stamps the current version. The next
            bytes read replay the same transcript to the same
            conservation: no token column is touched, rows are only
            reassigned. Returns True when a rebuild ran. Never
            raises.
            """
            from dash import lead_split

            if not isinstance(session_id, str) or not session_id.strip():
                return False
            sid = session_id.strip()
            try:
                version = int(lead_split.SIGNAL_RULES_VERSION)
            except (TypeError, ValueError):
                return False
            with self._lock:
                try:
                    self._conn.execute(
                        "CREATE TABLE IF NOT EXISTS meta"
                        " (key TEXT PRIMARY KEY, value TEXT)"
                    )
                    row = self._conn.execute(
                        "SELECT value FROM meta WHERE key=?",
                        (f"lead_signal_rules:{sid}",),
                    ).fetchone()
                    try:
                        stored = int(row[0]) if row is not None else None
                    except (TypeError, ValueError):
                        stored = None
                    if stored == version:
                        return False
                    self._rebuild_split_locked(sid)
                    self._conn.execute(
                        "INSERT OR REPLACE INTO meta (key, value)"
                        " VALUES (?, ?)",
                        (f"lead_signal_rules:{sid}", str(version)),
                    )
                    self._conn.commit()
                except Exception:
                    try:
                        self._conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    return False
                self._seq += 1
                self._data_seq += 1
                self._note_data_write()
                return True
        def _rebuild_split_locked(self, sid: str) -> None:
            """Clean rebuild of one session's split state.

            Caller holds the lock and stamps the version after. Moves
            every usage row back to the primary run and clears its
            per-turn workstream, deletes slice runs, subagent votes
            and spawn links, and the session's file offsets so the
            next ingest replays the same bytes. Rebuilds the touched
            hourly buckets. Token columns are never written: rows are
            only reassigned, so the rebuild conserves every token.
            """
            self._ensure_subagent_tables_locked()
            try:
                hours = {
                    row[0]
                    for row in self._conn.execute(
                        "SELECT DISTINCT CAST(ts / 3600 AS INTEGER) * 3600"
                        " FROM usage_events"
                        " WHERE session_id=? AND ts IS NOT NULL",
                        (sid,),
                    ).fetchall()
                    if row[0] is not None
                }
            except Exception:
                hours = set()
            primary_id = f"claude-main:{sid}:main"
            try:
                self._conn.execute(
                    "UPDATE usage_events SET run_id=?, ws_split=NULL"
                    " WHERE session_id=?",
                    (primary_id, sid),
                )
                self._conn.execute(
                    "DELETE FROM runs WHERE slice_of=?", (primary_id,)
                )
                self._conn.execute(
                    "DELETE FROM subagent_votes WHERE run_id LIKE ?",
                    (f"claude-subagent:{sid}:%",),
                )
                self._conn.execute(
                    "DELETE FROM subagent_spawn WHERE parent_session_id=?",
                    (sid,),
                )
                self._conn.execute(
                    "DELETE FROM file_offsets WHERE path LIKE ?",
                    (f"%{sid}%",),
                )
            except Exception:
                pass
            for hour in hours:
                try:
                    self._rebuild_hour(int(hour))
                except Exception:
                    pass
        def has_live_child(self, session_id: str) -> bool:
            """True when an open run names ``session_id`` as its parent.

            Covers running subagents/workflow steps (whose
            ``parent_session_id`` is the lead session) and linked worker
            lanes. Slices never count: their parent is null.
            """
            if not isinstance(session_id, str) or not session_id.strip():
                return False
            with self._lock:
                hit = self._conn.execute(
                    """SELECT 1 FROM runs
                       WHERE parent_session_id=? AND status IN ('running','queued')
                       LIMIT 1""",
                    (session_id.strip(),),
                ).fetchone()
            return hit is not None
        def last_split_ws(self, session_id: str):
            """Newest persisted per-turn workstream for ``session_id``.

            Seeds the ingest cursor after a restart so the next turn
            continues the previous topic instead of falling back to the
            primary. Workstream numbers only, never text.
            """
            if not isinstance(session_id, str) or not session_id.strip():
                return None
            with self._lock:
                try:
                    hit = self._conn.execute(
                        """SELECT ws_split FROM usage_events
                           WHERE session_id=? AND ws_split IS NOT NULL
                           ORDER BY ts DESC, rowid DESC LIMIT 1""",
                        (session_id.strip(),),
                    ).fetchone()
                except Exception:
                    return None
            return lead_split.clean_ws(hit["ws_split"]) if hit else None
        def link_lane_run(self, label: str, parent_session_id: str) -> int:
            """Adopt parentless worker runs of one launched lane label.

            When a lead transcript records the launch (a Bash command
            carrying the lane's prompt-file label), the lane runs with
            that label and no parent gain this session as
            ``parent_session_id`` — established from labels only, never
            prompt text. Only worker sources (muse/glm/grok) are
            adopted: an explicit label workstream is never overwritten
            (rule (a) wins), and parentless non-worker rows are left
            alone. Returns how many rows were adopted. Idempotent.
            """
            from dash.attribution import clean_label as _clean

            text = _clean(label)
            parent = parent_session_id.strip() if isinstance(parent_session_id, str) else ""
            if not text or not parent:
                return 0
            marks = ",".join("?" for _ in WORKER_SOURCES)
            with self._lock:
                try:
                    cur = self._conn.execute(
                        f"""UPDATE runs SET parent_session_id=?
                            WHERE label=? AND (parent_session_id IS NULL OR parent_session_id='')
                              AND source IN ({marks})""",
                        (parent, text, *sorted(WORKER_SOURCES)),
                    )
                    self._conn.commit()
                    changed = cur.rowcount or 0
                except Exception:
                    try:
                        self._conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    return 0
                if changed:
                    self._seq += 1
                    self._data_seq += 1
                    self._note_data_write()
                    # parent_session_id feeds run views but bypasses
                    # _write_run: invalidate fragments by epoch.
                    self._bump_run_epoch()
                return changed
        def apply_lead_slices(self, session_id: str) -> dict:
            """Ensure per-workstream slice rows for one lead session.

            Reads the persisted per-turn workstreams (``ws_split``),
            decides the material slices
            (:func:`dash.lead_split.material_slices`), upserts the
            slice runs mirroring the primary's status/model/clock, and
            moves the primary's rows for those workstreams onto the
            slices. Moves only: conservation holds to the token, per
            kind and per model. New turns only add; a restart replays
            the same bytes to the same slices. Never un-splits.
            Returns ``{ws: run_id}`` for the material slices.
            """
            if not isinstance(session_id, str) or not session_id.strip():
                return {}
            sid = session_id.strip()
            primary_id = f"claude-main:{sid}:main"
            with self._lock:
                return self._apply_slices_locked(sid, primary_id)
        def _apply_slices_locked(self, sid: str, primary_id: str) -> dict:
            try:
                sums = self._conn.execute(
                    """SELECT ws_split AS ws, model,
                              SUM(COALESCE(input_tokens,0)) AS input,
                              SUM(COALESCE(output_tokens,0)) AS output,
                              SUM(COALESCE(cache_read,0)) AS cache_read,
                              SUM(COALESCE(cache_write,0)) AS cache_write,
                              SUM(COALESCE(reasoning,0)) AS reasoning
                       FROM usage_events
                       WHERE session_id=? AND ws_split IS NOT NULL
                       GROUP BY ws_split, model""",
                    (sid,),
                ).fetchall()
            except Exception:
                return {}
            per_ws: dict[str, int] = {}
            for row in sums:
                ws = lead_split.clean_ws(row["ws"])
                if not ws:
                    continue
                billed = sum(int(row[kind] or 0) for kind in BILLED_KINDS)
                per_ws[ws] = per_ws.get(ws, 0) + billed
            if not per_ws:
                return {}
            session = self._one(
                "SELECT workstream FROM sessions WHERE session_id=?", (sid,)
            )
            primary = self._one("SELECT * FROM runs WHERE run_id=?", (primary_id,))
            primary_ws = (session or {}).get("workstream")
            primary_ws, wanted = lead_split.material_slices(per_ws, primary_ws)
            if not wanted:
                return {}
            out: dict[str, str] = {}
            for ws in wanted:
                slice_id = lead_split.slice_run_id(sid, ws)
                label = lead_split.slice_label(ws, sid)
                patch = {
                    "run_id": slice_id,
                    "source": "claude-main",
                    "session_id": sid,
                    "parent_session_id": None,
                    "label": label or None,
                    "slice_of": primary_id,
                }
                if primary:
                    for key in ("tier", "model", "status", "started_at", "ended_at", "clock_source"):
                        if primary.get(key) is not None:
                            patch[key] = primary[key]
                    if primary.get("status") in ("running", "queued"):
                        patch["ended_at"] = None
                self._write_slice_locked(patch)
                try:
                    hours = {
                        row[0]
                        for row in self._conn.execute(
                            """SELECT DISTINCT CAST(ts / 3600 AS INTEGER) * 3600
                               FROM usage_events
                               WHERE session_id=? AND ws_split=? AND run_id=? AND ts IS NOT NULL""",
                            (sid, ws, primary_id),
                        ).fetchall()
                        if row[0] is not None
                    }
                    self._conn.execute(
                        """UPDATE usage_events SET run_id=?
                           WHERE session_id=? AND ws_split=? AND run_id=?""",
                        (slice_id, sid, ws, primary_id),
                    )
                    for hour in hours:
                        self._rebuild_hour(int(hour))
                except Exception:
                    try:
                        self._conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    continue
                out[ws] = slice_id
            try:
                self._conn.commit()
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                return {}
            if out:
                self._seq += 1
                self._data_seq += 1
                self._note_data_write()
            return out
        def _write_slice_locked(self, patch: dict) -> None:
            """Upsert a slice run with explicit attribution. Caller holds the lock."""
            from dash.attribution import clean_label as _clean

            merged = self._one("SELECT * FROM runs WHERE run_id=?", (patch["run_id"],)) or {}
            merged = dict(merged)
            merged["run_id"] = patch["run_id"]
            for key in (
                "source", "tier", "model", "label", "session_id",
                "parent_session_id", "status", "started_at", "ended_at",
                "clock_source", "slice_of",
            ):
                if patch.get(key) is not None:
                    merged[key] = patch[key]
            merged["label"] = _clean(merged.get("label"))
            # Slices are always parentless leads: the ``lead`` phase rule
            # needs no parent, and a parent would fold slice tokens away.
            merged["parent_session_id"] = None
            if not merged.get("status"):
                merged["status"] = "unknown"
            workstream, confidence = self._attribute_locked(
                merged.get("label"),
                merged.get("session_id"),
                merged.get("parent_session_id"),
            )
            merged["workstream"] = workstream
            merged["ws_confidence"] = confidence
            self._write_run(merged)
        def _ensure_subagent_tables_locked(self) -> None:
            """Create the subagent spawn-link and vote tables once.

            Caller holds the lock. ``CREATE TABLE IF NOT EXISTS`` keeps
            existing databases working with no migration step.
            """
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS subagent_spawn (
                     tool_use_id TEXT PRIMARY KEY,
                     run_id TEXT,
                     parent_session_id TEXT,
                     parent_mid TEXT,
                     spawn_direct TEXT,
                     spawn_cursor TEXT
                   )"""
            )
            self._conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_subagent_spawn_run
                   ON subagent_spawn(run_id)"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS subagent_votes (
                     run_id TEXT PRIMARY KEY,
                     voted_offset INTEGER NOT NULL DEFAULT 0,
                     vote_seq INTEGER NOT NULL DEFAULT 0,
                     votes_json TEXT NOT NULL DEFAULT '{}'
                   )"""
            )
        def note_spawn_turn(
            self,
            tool_use_id: str,
            parent_session_id: str,
            parent_mid: str,
            spawn_direct: str | None,
            spawn_cursor: str | None,
        ) -> None:
            """Record which main-session turn spawned an Agent tool call.

            ``spawn_direct`` is the spawning turn's own signal vote (or
            None); ``spawn_cursor`` is the split-cursor assignment at that
            turn (inherited-within-20, or None). Workstream numbers only,
            never text. Idempotent: one row per tool use id, last write
            wins per column. Restarts replay the same bytes to the same
            row.
            """
            from dash import lead_split

            if not isinstance(tool_use_id, str) or not tool_use_id:
                return
            with self._lock:
                self._ensure_subagent_tables_locked()
                try:
                    self._conn.execute(
                        """INSERT INTO subagent_spawn
                           (tool_use_id, parent_session_id, parent_mid,
                            spawn_direct, spawn_cursor)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(tool_use_id) DO UPDATE SET
                             parent_session_id=excluded.parent_session_id,
                             parent_mid=excluded.parent_mid,
                             spawn_direct=excluded.spawn_direct,
                             spawn_cursor=excluded.spawn_cursor""",
                        (
                            tool_use_id,
                            parent_session_id if isinstance(parent_session_id, str) else None,
                            parent_mid if isinstance(parent_mid, str) else None,
                            lead_split.clean_ws(spawn_direct),
                            lead_split.clean_ws(spawn_cursor),
                        ),
                    )
                    self._conn.commit()
                except Exception:
                    try:
                        self._conn.execute("ROLLBACK")
                    except Exception:
                        pass
        def note_spawn_agent(self, run_id: str, tool_use_id: str) -> None:
            """Link a subagent run to the tool use that spawned it.

            Called with the ``toolUseId`` from the subagent sidecar and
            with the ``agentId`` from the parent's tool result (both are
            ids, never text). Either side may arrive first; the row
            joins whichever comes later. Idempotent.
            """
            from dash import lead_split

            if lead_split.parse_subagent_run(run_id) is None:
                return
            if not isinstance(tool_use_id, str) or not tool_use_id:
                return
            with self._lock:
                self._ensure_subagent_tables_locked()
                try:
                    self._conn.execute(
                        """INSERT INTO subagent_spawn (tool_use_id, run_id)
                           VALUES (?, ?)
                           ON CONFLICT(tool_use_id) DO UPDATE SET
                             run_id=excluded.run_id""",
                        (tool_use_id, run_id),
                    )
                    self._conn.commit()
                except Exception:
                    try:
                        self._conn.execute("ROLLBACK")
                    except Exception:
                        pass
        def add_subagent_votes(
            self,
            run_id: str,
            ordered: list,
            start_offset: int,
            end_offset: int,
        ) -> None:
            """Add one chunk's own-signal votes for a subagent run.

            ``ordered`` is the occurrence-ordered workstream vote list for
            the newly read bytes ``[start_offset, end_offset)`` (numbers
            only). Chunks are contiguous from the file offset, so votes
            normally extend the row; a backward offset (rewrite) resets
            the counters first. Deterministic and restart-safe: replaying
            the same bytes reproduces the same counters.
            """
            from dash import lead_split
            import json as _json

            if lead_split.parse_subagent_run(run_id) is None:
                return
            try:
                start = int(start_offset)
                end = int(end_offset)
            except (TypeError, ValueError):
                return
            if end < start:
                return
            votes: dict = {}
            seq = 0
            with self._lock:
                self._ensure_subagent_tables_locked()
                try:
                    row = self._one(
                        "SELECT voted_offset, vote_seq, votes_json"
                        " FROM subagent_votes WHERE run_id=?",
                        (run_id,),
                    )
                    if row is not None:
                        try:
                            voted = int(row["voted_offset"])
                        except (TypeError, ValueError):
                            voted = 0
                        try:
                            seq = int(row["vote_seq"])
                        except (TypeError, ValueError):
                            seq = 0
                        if start != voted:
                            # Rewrite or gap: recount from this chunk.
                            voted, seq, votes = 0, 0, {}
                        else:
                            try:
                                raw = _json.loads(row["votes_json"] or "{}")
                            except (ValueError, TypeError):
                                raw = {}
                            if isinstance(raw, dict):
                                for key, value in raw.items():
                                    ws = lead_split.clean_ws(key)
                                    if ws is None:
                                        continue
                                    try:
                                        number, first = value
                                        votes[ws] = [int(number), int(first)]
                                    except (TypeError, ValueError):
                                        continue
                    for ws in ordered or []:
                        hit = lead_split.clean_ws(ws)
                        if hit is None:
                            continue
                        if hit in votes:
                            votes[hit][0] += 1
                        else:
                            votes[hit] = [1, seq]
                            seq += 1
                    self._conn.execute(
                        """INSERT INTO subagent_votes
                           (run_id, voted_offset, vote_seq, votes_json)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(run_id) DO UPDATE SET
                             voted_offset=excluded.voted_offset,
                             vote_seq=excluded.vote_seq,
                             votes_json=excluded.votes_json""",
                        (run_id, end, seq, _json.dumps(votes, sort_keys=True)),
                    )
                    self._conn.commit()
                except Exception:
                    try:
                        self._conn.execute("ROLLBACK")
                    except Exception:
                        pass
        def _resolve_subagent_locked(self, run_id: str) -> tuple:
            """A subagent's workstream from its spawning turn, or nothing.

            Caller holds the lock. Order: the spawning turn's own signal;
            else the majority of the subagent's own signals; else the
            split-cursor value at the spawn; else nothing (the caller
            falls back to the parent-session rule). Confidence is always
            ``inherited``. Workstream numbers only, never text.
            """
            from dash import attribution, lead_split
            import json as _json

            if lead_split.parse_subagent_run(run_id) is None:
                return None, None
            try:
                self._ensure_subagent_tables_locked()
                link = self._one(
                    "SELECT spawn_direct, spawn_cursor FROM subagent_spawn"
                    " WHERE run_id=?",
                    (run_id,),
                )
                if link is None:
                    return None, None
                direct = lead_split.clean_ws(link["spawn_direct"])
                if direct:
                    return direct, attribution.INHERITED
                votes = self._one(
                    "SELECT votes_json FROM subagent_votes WHERE run_id=?",
                    (run_id,),
                )
                if votes is not None:
                    try:
                        raw = _json.loads(votes["votes_json"] or "{}")
                    except (ValueError, TypeError):
                        raw = {}
                    majority = lead_split.pick_counts(raw if isinstance(raw, dict) else {})
                    if majority:
                        return majority, attribution.INHERITED
                cursor = lead_split.clean_ws(link["spawn_cursor"])
                if cursor:
                    return cursor, attribution.INHERITED
            except Exception:
                return None, None
            return None, None
        def resolve_subagent(self, run_id: str) -> tuple:
            """Public wrapper for :meth:`_resolve_subagent_locked`."""
            with self._lock:
                return self._resolve_subagent_locked(run_id)
        def refresh_linked_subagents(self, tool_use_ids) -> int:
            """Re-attribute subagent runs linked to these tool uses.

            The parent chunk carrying a spawn (or its tool result) often
            arrives after the subagent file, so the run row first lands
            unassigned; this pass re-resolves it once the link completes.
            Usage rows are keyed by message id, so re-attribution moves
            workstream columns only and never double counts. Returns how
            many runs changed workstream.
            """
            ids = [
                tid for tid in (tool_use_ids or [])
                if isinstance(tid, str) and tid
            ]
            if not ids:
                return 0
            changed = 0
            with self._lock:
                self._ensure_subagent_tables_locked()
                try:
                    run_ids: list = []
                    for tid in ids:
                        rows = self._conn.execute(
                            "SELECT run_id FROM subagent_spawn"
                            " WHERE tool_use_id=? AND run_id IS NOT NULL",
                            (tid,),
                        ).fetchall()
                        run_ids.extend(row[0] for row in rows if row[0])
                except Exception:
                    return 0
            for run_id in run_ids:
                try:
                    before = self.get_run(run_id)
                    if before is None:
                        # Link without a run row yet (subagent file not
                        # ingested): the ingest upsert resolves it later.
                        continue
                    self.upsert_run({"run_id": run_id})
                    after = self.get_run(run_id)
                except Exception:
                    continue
                if (
                    before is not None
                    and after is not None
                    and after.get("workstream") != before.get("workstream")
                ):
                    changed += 1
            return changed
