"""Time sources. Every window, debounce and hold uses record time, never the wall clock."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now_ns(self) -> int: ...


class MonotonicClock:
    """The device clock: CLOCK_MONOTONIC nanoseconds."""

    def now_ns(self) -> int:
        return time.monotonic_ns()


class SimClock:
    """A settable clock for replay and tests. Advances only when told to."""

    def __init__(self, start_ns: int = 0) -> None:
        self._t = start_ns

    def now_ns(self) -> int:
        return self._t

    def set(self, t_ns: int) -> None:
        if t_ns < self._t:
            raise ValueError("SimClock cannot move backwards")
        self._t = t_ns

    def advance(self, dt_ns: int) -> None:
        if dt_ns < 0:
            raise ValueError("dt_ns must be >= 0")
        self._t += dt_ns
