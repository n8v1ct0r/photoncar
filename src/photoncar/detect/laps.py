"""Lap timing against a start and finish line in local ENU meters.

Each new position forms a step from the previous one. A step that crosses the line segment
gives a crossing time by linear interpolation along the step (exact for straight-line motion
at constant speed). The first counted crossing starts lap 1 and sets the travel direction.
Later crossings count only in that direction, and only when both the minimum lap time and
the minimum distance since the last counted crossing have passed; anything else (a wiggle
back and forth across the line) is ignored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from photoncar.records import Lap


@dataclass(frozen=True, slots=True)
class StartFinishLine:
    line_id: str
    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        if self.x1 == self.x2 and self.y1 == self.y2:
            raise ValueError("a start and finish line needs 2 distinct points")


class LapTimer:
    """Push positions in time order; returns a ``Lap`` each time a lap completes."""

    def __init__(self, line: StartFinishLine, min_lap_s: float, min_dist_m: float) -> None:
        if min_lap_s < 0 or min_dist_m < 0:
            raise ValueError("min_lap_s and min_dist_m must be >= 0")
        self._line = line
        self._min_lap_ns = round(min_lap_s * 1e9)
        self._min_dist = float(min_dist_m)
        self._prev: tuple[int, float, float] | None = None
        self._last_cross_ns: int | None = None
        self._direction = 0
        self._dist = 0.0
        self._lap_no = 0

    @property
    def lap_no(self) -> int:
        """Number of completed laps so far."""
        return self._lap_no

    def push(self, t_ns: int, x_m: float, y_m: float) -> Lap | None:
        prev = self._prev
        self._prev = (t_ns, x_m, y_m)
        if prev is None:
            return None
        t0, x0, y0 = prev
        if t_ns <= t0:
            raise ValueError("positions must arrive in increasing time order")
        step = math.hypot(x_m - x0, y_m - y0)
        hit = self._cross(x0, y0, x_m, y_m)
        if hit is None:
            self._dist += step
            return None
        s, direction = hit
        t_cross = t0 + round(s * (t_ns - t0))
        before = self._dist + s * step
        after = (1.0 - s) * step
        if self._last_cross_ns is None:
            self._start(t_cross, direction, after)
            return None
        if (
            direction != self._direction
            or t_cross - self._last_cross_ns < self._min_lap_ns
            or before < self._min_dist
        ):
            self._dist += step
            return None
        start_ns = self._last_cross_ns
        end_ns = t_cross
        self._lap_no += 1
        self._last_cross_ns = t_cross
        self._dist = after
        return Lap(
            line_id=self._line.line_id,
            lap_no=self._lap_no,
            t_start_ns=start_ns,
            t_end_ns=end_ns,
            duration_ns=end_ns - start_ns,
            valid=True,
        )

    def _start(self, t_cross: int, direction: int, after: float) -> None:
        self._last_cross_ns = t_cross
        self._direction = direction
        self._dist = after

    def _cross(self, x0: float, y0: float, x1: float, y1: float) -> tuple[float, int] | None:
        """Fraction along the step where it crosses the line, and the crossing direction.

        A step crosses when its start is strictly on one side and its end is on the other
        side or on the line, so a point landing exactly on the line counts once.
        """
        ln = self._line
        dx, dy = ln.x2 - ln.x1, ln.y2 - ln.y1
        side0 = dx * (y0 - ln.y1) - dy * (x0 - ln.x1)
        side1 = dx * (y1 - ln.y1) - dy * (x1 - ln.x1)
        if side0 == 0.0 or ((side0 < 0.0) == (side1 < 0.0) and side1 != 0.0):
            return None
        s = side0 / (side0 - side1)
        px = x0 + s * (x1 - x0)
        py = y0 + s * (y1 - y0)
        u = ((px - ln.x1) * dx + (py - ln.y1) * dy) / (dx * dx + dy * dy)
        if u < 0.0 or u > 1.0:
            return None
        return s, (1 if side0 < 0.0 else -1)
