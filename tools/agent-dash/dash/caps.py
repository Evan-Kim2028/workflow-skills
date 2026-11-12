"""Live-worker caps for registry tiers, shared by the launcher and the doctor.

Two caps gate a launch. The pool is the live workers of one base model across
every tier that runs it (``pi-deepseek-flash`` and ``pi-deepseek-flash-low``
both run ``morph/morph-dsv41flash-1017``; a ``:<level>`` suffix is a thinking
level and is stripped). Its limit is the registry's ``model_caps`` entry for
the model when it has one, else the largest enabled tier ``cap`` among those
tiers, so a smaller tier's cap never throttles a bigger one. The tier is its
own pattern's live count against its own ``cap``.

``pgrep`` is a callable taking one pattern string and returning a live count.
The pool count passes one union pattern over the sharing tiers' patterns, so a
process that two patterns both match is counted once.
"""

from __future__ import annotations

import re

THINKING_SUFFIX = re.compile(r":[A-Za-z]+$")


def as_int(value):
    """``int(value)``, or None when it is not an integer."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def base_model(model) -> str:
    """A model id without a trailing ``:<level>`` thinking suffix.

    ``morph/morph-dsv41flash-1017:low`` runs the same model as
    ``morph/morph-dsv41flash-1017``; the level only sets its thinking budget.
    """
    return THINKING_SUFFIX.sub("", str(model or ""))


def model_caps(registry) -> dict:
    """``registry["model_caps"]`` keyed by base model id; malformed entries are dropped."""
    caps = registry.get("model_caps")
    if not isinstance(caps, dict):
        return {}
    found = {}
    for key, value in caps.items():
        cap = as_int(value)
        if cap is not None:
            found[base_model(key)] = cap
    return found


def sharing_workers(registry, model):
    """``(name, worker)`` for every registry tier whose base model is ``model``."""
    for name, worker in (registry.get("workers") or {}).items():
        if isinstance(worker, dict) and base_model(worker.get("model")) == model:
            yield name, worker


def pool_capacity(registry, model, pgrep):
    """``(live, limit)`` for the pool of one base model.

    ``live`` is one ``pgrep`` over the union of the sharing tiers' patterns.
    ``limit`` is the model's ``model_caps`` entry when it has one; otherwise
    the largest ``cap`` among the enabled sharing tiers. None when nothing
    caps the pool.
    """
    patterns = []
    caps = []
    for _name, worker in sharing_workers(registry, model):
        pattern = worker.get("pgrep") or ""
        if pattern and pattern not in patterns:
            patterns.append(pattern)
        cap = as_int(worker.get("cap"))
        if worker.get("enabled", True) and cap is not None:
            caps.append(cap)
    pinned = model_caps(registry)
    limit = pinned[model] if model in pinned else (max(caps) if caps else None)
    if not patterns:
        return 0, limit
    union = patterns[0] if len(patterns) == 1 else "|".join(f"({p})" for p in patterns)
    return pgrep(union), limit


def cap_block(registry, worker, pgrep):
    """Why a launch of ``worker`` is refused by a cap, or None when it may launch.

    The pool check comes first: the model's live workers across every sharing
    tier must stay below ``pool_capacity``'s limit. Then the tier check: this
    tier's own pattern count must stay below its own ``cap``. Returns
    ``(which, live, limit)`` with ``which`` of ``"pool"`` or ``"tier"``.
    """
    live, limit = pool_capacity(registry, base_model(worker.get("model")), pgrep)
    if limit is not None and live >= limit:
        return "pool", live, limit
    cap = as_int(worker.get("cap"))
    if cap is None:
        return None
    pattern = worker.get("pgrep") or ""
    own = pgrep(pattern) if pattern else 0
    if own >= cap:
        return "tier", own, cap
    return None


def over_caps(registry, pgrep):
    """Caps exceeded right now, for the doctor.

    Each finding is a dict with ``which`` (``"pool"`` or ``"tier"``), ``name``
    (the base model or the tier name), ``tiers`` (the tiers that count against
    it), ``live`` and ``limit``. A pool is over when its live count is above
    its limit; a tier is over when its own count is above its own cap.
    """
    workers = registry.get("workers") or {}
    findings = []
    models = []
    for worker in workers.values():
        if isinstance(worker, dict) and base_model(worker.get("model")):
            model = base_model(worker.get("model"))
            if model not in models:
                models.append(model)
    for model in models:
        live, limit = pool_capacity(registry, model, pgrep)
        if limit is not None and live > limit:
            tiers = [name for name, _worker in sharing_workers(registry, model)]
            findings.append({"which": "pool", "name": model, "tiers": tiers, "live": live, "limit": limit})
    for name, worker in workers.items():
        if not isinstance(worker, dict):
            continue
        cap = as_int(worker.get("cap"))
        pattern = worker.get("pgrep") or ""
        if cap is None or not pattern:
            continue
        live = pgrep(pattern)
        if live > cap:
            findings.append({"which": "tier", "name": name, "tiers": [name], "live": live, "limit": cap})
    return findings
