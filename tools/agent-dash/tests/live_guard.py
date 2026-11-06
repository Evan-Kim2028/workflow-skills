"""Keep a test module off the live dashboard: its port and its state dir.

A module that starts, stops or reads dashboard state calls ``Guard`` from
its setUpModule and tearDownModule::

    from live_guard import Guard
    _GUARD = Guard()
    def setUpModule(): _GUARD.start()
    def tearDownModule(): _GUARD.stop()

``start()`` points AGENT_DASH_STATE_DIR, AGENT_DASH_DATA_ROOT and
AGENT_DASH_WORKSTREAMS at one temp tree and AGENT_DASH_PORT at a free port,
for every key the module has not already set to a safe value. It then
checks that the port the tool would use is not 8765 and that the state dir
is not the live default, and raises AssertionError before any test runs
when either still resolves to the live one. ``stop()`` restores the
environment and removes the temp tree.
"""

import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
TOOLS = TESTS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

LIVE_PORT = 8765
LIVE_STATE = Path.home() / ".local" / "share" / "agent-dash"
KEYS = ("AGENT_DASH_STATE_DIR", "AGENT_DASH_DATA_ROOT", "AGENT_DASH_WORKSTREAMS", "AGENT_DASH_PORT")


def _is_live_state(value):
    try:
        path = Path(value).expanduser().resolve()
    except OSError:
        return False
    live = LIVE_STATE.resolve() if LIVE_STATE.exists() else LIVE_STATE
    return path == live or live in path.parents


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Guard:
    def __init__(self):
        self._saved = None
        self._tmp = None

    def start(self):
        """Isolate the env, then verify it. Raises AssertionError on the live setup."""
        if self._saved is not None:
            raise AssertionError("live_guard: start() called twice")
        self._tmp = tempfile.mkdtemp(prefix="agent-dash-guard-")
        root = Path(self._tmp)
        wanted = {
            "AGENT_DASH_STATE_DIR": str(root / "state"),
            "AGENT_DASH_DATA_ROOT": str(root / "data"),
            "AGENT_DASH_WORKSTREAMS": str(root / "data" / "workstreams"),
            "AGENT_DASH_PORT": str(_free_port()),
        }
        self._saved = {key: os.environ.get(key) for key in KEYS}
        try:
            for key, value in wanted.items():
                if os.environ.get(key) is None:
                    os.environ[key] = value
            self.verify()
        except BaseException:
            self.stop()
            raise
        return self

    def verify(self):
        """Raise AssertionError if the tool would reach the live port or state dir."""
        import agent_dash

        port = agent_dash._port()
        if port == LIVE_PORT:
            raise AssertionError("live_guard: test would use the live port %d" % LIVE_PORT)
        state = os.environ.get("AGENT_DASH_STATE_DIR")
        if state is None or _is_live_state(state):
            raise AssertionError("live_guard: test would use the live state dir %s" % LIVE_STATE)

    def stop(self):
        if self._saved is not None:
            for key, value in self._saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self._saved = None
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None
