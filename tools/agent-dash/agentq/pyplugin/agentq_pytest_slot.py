"""pytest plugin: a pytest run takes the laptop-wide ``pytest`` slot, whatever interpreter starts it.

The PATH shims in ``agentq/shims`` only see ``pytest`` typed by name. A pi worker
that runs ``<venv>/bin/python -m pytest`` by absolute path bypasses them, so this
plugin takes the same slot from inside pytest. It is loaded with ``-p
agentq_pytest_slot``, and this folder must be on ``PYTHONPATH``; ``launch_pi.py``
sets both for pi workers (see its module docstring).

Hooks:

- ``pytest_configure``: in the main process only (xdist workers and runs under a
  held slot skip it), take a ``pytest`` slot with the admission rule and max wait
  the shims use. The record names ``AGENTQ_RUN`` (or ``AGENTQ_JOB``) and the pytest
  arguments.
- ``pytest_unconfigure``: release the slot.
- ``pytest_cmdline_main``: lower xdist's parsed worker count to the cap. xdist's own
  ``pytest_cmdline_main`` is ``tryfirst`` and resolves ``-n auto`` / ``-n logical``
  (from ``PYTEST_XDIST_AUTO_NUM_WORKERS`` or the CPU count) and builds
  ``config.option.tx``; this hook runs after it, so an explicit ``-n 8``, ``-n auto``
  with a large environment value, and ``-n logical`` all end up at the cap. ``-n 0``,
  ``-n 1`` and no ``-n`` are left alone. One stderr line names the lowering.

Environment read: ``AGENTQ_HOME`` (the agentq home), ``AGENTQ_RUN`` / ``AGENTQ_JOB``
(the label), ``AGENTQ_SLOTS_PY`` (overrides where ``slots.py`` is loaded from),
``AGENTQ_PYTEST_SLOT_HELD`` (set when this process tree already holds a pytest
slot; the shim sets it for its child). On acquire the plugin sets it too, so a
pytest started from inside this run does not wait for a second slot.
``LOR_CI_XDIST`` is the xdist worker cap (default 2); ``launch_pi.py`` sets it to 2
when unset.

The plugin never fails a run. Any error logs one line to stderr and the run goes
on without a slot. Stdlib only, Python 3.9+, and it imports nothing from agentq
until a slot is wanted: ``slots.py`` is loaded by file path and its package root
is put on ``sys.path`` only for that import.
"""

import importlib.util
import os
import shlex
import sys
from pathlib import Path

HELD_ENV = "AGENTQ_PYTEST_SLOT_HELD"
SLOTS_PY_ENV = "AGENTQ_SLOTS_PY"
XDIST_CAP_ENV = "LOR_CI_XDIST"
DEFAULT_XDIST_CAP = 2
RUN_ENV = "AGENTQ_RUN"
JOB_ENV = "AGENTQ_JOB"
HERE = Path(__file__).resolve()
DEFAULT_SLOTS_PY = HERE.parents[1] / "slots.py"

_holder = None
_set_held_flag = False


def _note(message):
    """One line to stderr. The plugin's only output on an error path."""
    try:
        print(f"agentq pytest slot: {message}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _is_xdist_worker(config):
    """True in an xdist worker process; the main process owns the slot for the whole run."""
    return hasattr(config, "workerinput") or "PYTEST_XDIST_WORKER" in os.environ


def _load_agentq(environ):
    """``(slots, db)`` modules from the agentq package that ``slots.py`` belongs to.

    ``slots.py`` is loaded by file path (``AGENTQ_SLOTS_PY`` or the copy next to this
    file). Its package root is on ``sys.path`` only while it imports. Raises on any
    failure; the caller logs it.
    """
    path = Path(environ.get(SLOTS_PY_ENV) or DEFAULT_SLOTS_PY)
    root = str(path.resolve().parents[1])
    added = root not in sys.path
    if added:
        sys.path.append(root)
    try:
        spec = importlib.util.spec_from_file_location("agentq_pytest_slots", str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path}")
        slots = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = slots
        spec.loader.exec_module(slots)
        db = importlib.import_module("agentq.db")
    finally:
        if added and root in sys.path:
            sys.path.remove(root)
    return slots, db


def _run_label(environ):
    return (environ.get(RUN_ENV) or environ.get(JOB_ENV) or "-").strip()


def _pytest_args(config):
    params = getattr(config, "invocation_params", None)
    args = getattr(params, "args", None)
    return list(args) if args is not None else sys.argv[1:]


def _xdist_cap(environ):
    """The worker cap from ``LOR_CI_XDIST``; 2 when unset, blank or not a positive int."""
    raw = (environ.get(XDIST_CAP_ENV) or "").strip()
    if not raw:
        return DEFAULT_XDIST_CAP
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_XDIST_CAP
    return value if value >= 1 else DEFAULT_XDIST_CAP


def _clamp_xdist(config, environ):
    """Lower xdist's parsed worker count to the cap. Returns ``(before, after)`` or None.

    ``config.option.numprocesses`` is already a number here: xdist's ``tryfirst``
    ``pytest_cmdline_main`` turned ``auto``/``logical`` into one and built
    ``config.option.tx`` from it. Both are lowered, since xdist's ``pytest_configure``
    reads ``tx`` to decide whether distribution is on.
    """
    option = getattr(config, "option", None)
    if option is None:
        return None
    num = getattr(option, "numprocesses", None)
    if not isinstance(num, int) or isinstance(num, bool) or num <= 1:
        return None
    cap = _xdist_cap(environ)
    if num <= cap:
        return None
    option.numprocesses = cap
    tx = getattr(option, "tx", None)
    if isinstance(tx, list):
        option.tx = tx[:cap]
    return num, cap


def _take_slot(config, environ):
    """Acquire the pytest slot for this run. Returns the Holder (slotted or not)."""
    slots, db = _load_agentq(environ)
    home = db.home_dir(environ)
    limits = slots.load_limits(home)
    admit = slots.pytest_admission(limits, slots.Gauges(home, ttl_s=limits.gauge_ttl_s))
    command = shlex.join(["pytest", *_pytest_args(config)])
    return slots.acquire("pytest", limit=slots.kind_limit(limits, "pytest"), home=home,
                         label=_run_label(environ), command=command, admit=admit, warn=_note)


def pytest_configure(config):
    """Take the pytest slot for the main process, unless this run already holds one."""
    global _holder, _set_held_flag
    try:
        if _is_xdist_worker(config) or (os.environ.get(HELD_ENV) or "").strip():
            return
        _holder = _take_slot(config, os.environ)
        os.environ[HELD_ENV] = "1"
        _set_held_flag = True
    except Exception as exc:
        _note(f"no slot taken, running without one ({exc})")


def pytest_cmdline_main(config):
    """Cap xdist's worker count after xdist resolves ``-n auto`` / ``-n logical``."""
    try:
        lowered = _clamp_xdist(config, os.environ)
        if lowered:
            _note(f"xdist workers lowered from {lowered[0]} to {lowered[1]} ({XDIST_CAP_ENV})")
    except Exception as exc:
        _note(f"xdist cap skipped ({exc})")


def pytest_unconfigure(config):
    """Release the slot taken in ``pytest_configure``."""
    global _holder, _set_held_flag
    try:
        if _holder is not None:
            _holder.release()
        if _set_held_flag:
            os.environ.pop(HELD_ENV, None)
    except Exception as exc:
        _note(f"release failed ({exc})")
    finally:
        _holder = None
        _set_held_flag = False
