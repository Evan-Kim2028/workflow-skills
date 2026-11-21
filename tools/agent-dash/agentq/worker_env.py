"""The environment an agentq worker child runs in: command shims, pytest plugins, slot hosts.

Every worker backend whose shell commands should take agentq's laptop slots calls
``prepare_worker_environ`` before it spawns the agent (``launch_pi.py``, ``launch_codex.py``).
``shim_environ`` puts ``agentq/shims`` first on ``PATH`` and loads the pytest slot and cache
plugins, so a ``pytest`` run waits for a slot whether it is typed by name or reached through a
venv interpreter. ``AGENTQ_SHIMS=0`` skips it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from agentq import slots as agentq_slots

ROOT = Path(__file__).resolve().parents[1]
SHIMS_ENV = "AGENTQ_SHIMS"
SHIM_PYTHON_ENV = "AGENTQ_SHIM_PYTHON"
SHIMS_DIR = ROOT / "agentq" / "shims"
PYTEST_PLUGIN_DIR = ROOT / "agentq" / "pyplugin"
PYTEST_PLUGINS = ("agentq_pytest_slot", "agentq_pytest_cache")
XDIST_DEFAULTS = (("PYTEST_XDIST_AUTO_NUM_WORKERS", "2"), ("LOR_CI_XDIST", "2"))


def agentq_home():
    """``$AGENTQ_HOME``, else the broker's default state folder."""
    env = (os.environ.get("AGENTQ_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "state" / "agentq"


def shim_environ(environ, shims_dir=SHIMS_DIR, python=sys.executable, plugin_dir=PYTEST_PLUGIN_DIR):
    """Put the agentq command shims first on PATH for a worker child; ``AGENTQ_SHIMS=0`` skips it.

    The shims gate ssh to production hosts and pytest runs (see agentq/shims/dispatch.py).
    ``AGENTQ_SHIM_PYTHON`` names the interpreter the shims run their dispatcher with.
    The pytest slot plugin and its xdist caps are set in the same place (see the module
    docstring), under the same ``AGENTQ_SHIMS=0`` switch.
    """
    if (environ.get(SHIMS_ENV) or "").strip() == "0":
        return environ
    shims = str(shims_dir)
    rest = [part for part in (environ.get("PATH") or "").split(os.pathsep) if part and part != shims]
    environ["PATH"] = os.pathsep.join([shims, *rest])
    environ.setdefault(SHIM_PYTHON_ENV, python)
    plugin = str(plugin_dir)
    pythonpath = [part for part in (environ.get("PYTHONPATH") or "").split(os.pathsep) if part and part != plugin]
    environ["PYTHONPATH"] = os.pathsep.join([plugin, *pythonpath])
    addopts = (environ.get("PYTEST_ADDOPTS") or "").strip()
    for name in PYTEST_PLUGINS:
        if f"-p {name}" not in addopts:
            addopts = " ".join(part for part in (addopts, f"-p {name}") if part)
    environ["PYTEST_ADDOPTS"] = addopts
    named = [part for part in (environ.get("PYTEST_PLUGINS") or "").split(",") if part.strip()]
    environ["PYTEST_PLUGINS"] = ",".join([*named, *(name for name in PYTEST_PLUGINS if name not in named)])
    for key, value in XDIST_DEFAULTS:
        if not (environ.get(key) or "").strip():
            environ[key] = value
    return environ



def prepare_worker_environ(environ=os.environ):
    """Shim ``environ`` for a worker child and publish the gated ssh hosts the shims read."""
    shim_environ(environ)
    agentq_slots.write_ssh_hosts(agentq_home())
    return environ
