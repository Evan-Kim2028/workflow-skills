"""SQLite WAL store: runs, usage, samples, sessions, offsets, hourly buckets.

Package facade. ``Store`` is the same class as before, composed from one
mixin per concern; ``from dash.store import X`` keeps working unchanged for
every name the old module exposed.

Ownership: the facade holds the single ``threading.RLock`` (created in
``BaseMixin.__init__``) and the connection. Concern modules expose mixin
methods plus ``_locked``-style module functions that assume the lock and
take the connection (or explicit args); they never create or take their own
lock. Public ``Store`` methods keep their exact signatures, return shapes,
and lock behaviour.

Patch targets: tests that stub internals patch the module that owns the
name (``dash.store.runs``, ``dash.store.summarize``, ...) or the ``Store``
attribute itself; the names re-exported here are the same objects, kept so
existing ``from dash.store import X`` imports keep working.
"""

from __future__ import annotations

import time as _time

from dash.attribution import attribute, clean_label
from dash.liveness import (
    DAGSTER_ACTIVE_FRESH_S,
    PROCESS_GONE_GRACE_S,
    STALE_IDLE_S,
    pid_alive as _pid_alive,
    stale_grace_s,
)
from dash.models import model_key, run_model_key
from dash.pricing import cost as price_cost
from dash.pricing import series_key
from dash.pricing import status as price_status

from .base import BaseMixin, _doctor_api_row, _doctor_visible, _offset_path_gone
from .queries import QueriesMixin
from .running import (
    RunningMixin,
    _annotate_lanes,
    _is_idle_lead,
    _lane_shadows,
    _unseen_source,
)
from .runs import (
    RunsMixin,
    _fold_manifest_patch,
    _is_manifest_twin,
    _merge_usage,
    _muse_orphan,
    _nearest_twin,
    _parent_map,
    _rollup_usage,
    muse_orphan_pairs,
)
from .summarize import (
    SummarizeMixin,
    _agent_counts,
    _duplicate_run_mismatches,
    _empty_scope,
    _last_activity,
    _overlap,
    _run_overlaps_window,
    _scope_numbers,
    _scope_tab,
    _sum_cells,
    _usage_unpriced,
    _ws_row,
)
from .usage import UsageMixin, _fold_events
from .util import (
    BILLED_FIELDS,
    FINISHED,
    MERGE_WINDOW_S,
    PRUNE_BATCH,
    RECENT_S,
    RUN_COLS,
    RUN_INPUT,
    RANGE_S,
    SAMPLE_FRESH_S,
    SAMPLE_KEEP_S,
    SESSION_COLS,
    TOKEN_FIELDS,
    TOOL_KEYS,
    UNASSIGNED,
    WORKER_SOURCES,
    WRAPPER_SOURCES,
    WS_STATUSES,
    _SEV_ORDER,
    _WF,
    _ESTIMATE_TOOL,
    _active,
    _billed_total,
    _collapse_workflows,
    _drop_launch_echoes,
    _elapsed,
    _epoch,
    _hour,
    _in_ws,
    _is_counted_agent,
    _local_midnight,
    _manifest_family,
    _money,
    _one_run_per_process,
    _opt_int,
    _same,
    _sum_costs,
    _tool_key,
    _ws_sort,
    _zeros,
    is_prod_job_run,
)

__all__ = [
    "Store",
    "TOOL_KEYS",
    "FINISHED",
    "RANGE_S",
    "MERGE_WINDOW_S",
    "UNASSIGNED",
    "SAMPLE_FRESH_S",
    "RECENT_S",
    "SAMPLE_KEEP_S",
    "TOKEN_FIELDS",
    "BILLED_FIELDS",
    "PRUNE_BATCH",
    "RUN_COLS",
    "RUN_INPUT",
    "SESSION_COLS",
    "WS_STATUSES",
    "WORKER_SOURCES",
    "WRAPPER_SOURCES",
    "is_prod_job_run",
    "muse_orphan_pairs",
    "_ESTIMATE_TOOL",
    "_WF",
    "_SEV_ORDER",
    "_hour",
    "_opt_int",
    "_same",
    "_epoch",
    "_zeros",
    "_money",
    "_elapsed",
    "_in_ws",
    "_billed_total",
    "_local_midnight",
    "_manifest_family",
    "_tool_key",
    "_is_counted_agent",
    "_drop_launch_echoes",
    "_one_run_per_process",
    "_active",
    "_sum_costs",
    "_ws_sort",
    "_collapse_workflows",
    "_offset_path_gone",
    "_doctor_api_row",
    "_doctor_visible",
    "_is_manifest_twin",
    "_fold_manifest_patch",
    "_nearest_twin",
    "_overlap",
    "_duplicate_run_mismatches",
    "_muse_orphan",
    "_parent_map",
    "_rollup_usage",
    "_merge_usage",
    "_fold_events",
    "_is_idle_lead",
    "_lane_shadows",
    "_annotate_lanes",
    "_unseen_source",
    "_scope_numbers",
    "_sum_cells",
    "_usage_unpriced",
    "_run_overlaps_window",
    "_last_activity",
    "_scope_tab",
    "_ws_row",
    "_empty_scope",
    "_agent_counts",
    "price_cost",
    "series_key",
    "price_status",
    "_pid_alive",
    "PROCESS_GONE_GRACE_S",
    "stale_grace_s",
    "DAGSTER_ACTIVE_FRESH_S",
    "STALE_IDLE_S",
    "_time",
    "attribute",
    "clean_label",
    "model_key",
    "run_model_key",
]


class Store(
    BaseMixin,
    RunsMixin,
    UsageMixin,
    RunningMixin,
    SummarizeMixin,
    QueriesMixin,
):
    """SQLite-backed dashboard store behind one re-entrant lock.

    All behaviour lives in the concern mixins; this class only fixes the
    method resolution order. See this package's docstring for the
    ownership and locking rules.
    """
