"""Event-loop stall watchdog: a side thread that names what the loop was doing when it stopped.

The proxy's maintenance tick calls ``beat()`` every few hundred milliseconds. If no beat arrives for
``dump_s`` seconds, the loop is blocked. The watchdog then writes the loop thread's Python stack once per
stall episode, as one ``loop_stall`` line, and clears the episode when beats resume.

The watchdog reads ``sys._current_frames()``, which is safe from another thread and does not stop the loop.
"""

from __future__ import annotations

import sys
import threading
import traceback
from typing import Callable


def formatted_stack(frame) -> str:
    """One line per frame, outermost first, joined with ' | '. Newlines never appear in the result."""
    if frame is None:
        return "unavailable"
    lines = [line.strip() for block in traceback.format_stack(frame) for line in block.splitlines()]
    return " | ".join(line for line in lines if line)


class StallWatchdog(threading.Thread):
    def __init__(
        self,
        *,
        loop_thread_id: int,
        dump_s: float,
        log: Callable[..., None],
        clock: Callable[[], float],
        interval_s: float = 0.25,
    ) -> None:
        super().__init__(name="agentq-watchdog", daemon=True)
        self.loop_thread_id = loop_thread_id
        self.dump_s = dump_s
        self.log = log
        self.clock = clock
        self.interval_s = interval_s
        self.beat_at = clock()
        self.episodes = 0
        self._episode_open = False
        self._halt = threading.Event()

    def beat(self) -> None:
        self.beat_at = self.clock()

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        while not self._halt.wait(self.interval_s):
            self.check()

    def check(self) -> None:
        """One look at the loop. Writes a stall line the first time a stall is seen; no-op otherwise."""
        lag = self.clock() - self.beat_at
        if lag <= self.dump_s:
            self._episode_open = False
            return
        if self._episode_open:
            return
        self._episode_open = True
        self.episodes += 1
        frame = sys._current_frames().get(self.loop_thread_id)
        self.log("loop_stall", ms=round(lag * 1000), stack=formatted_stack(frame))
