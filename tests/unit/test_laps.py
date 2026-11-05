"""Lap timer known answers: a stadium track at constant speed gives exactly L / v per lap."""

from __future__ import annotations

import math

import pytest

from photoncar.detect.laps import LapTimer, StartFinishLine
from photoncar.records import Lap

T0 = 987_654_321_000
STRAIGHT_M = 200.0
RADIUS_M = 50.0
LENGTH_M = 2 * STRAIGHT_M + 2 * math.pi * RADIUS_M
LINE_X = 100.0
LINE = StartFinishLine("sf", LINE_X, -10.0, LINE_X, 10.0)


def stadium(s: float) -> tuple[float, float]:
    """Position at arc length ``s``: bottom straight (+x), right turn, top straight, left turn."""
    s = s % LENGTH_M
    arc = math.pi * RADIUS_M
    if s < STRAIGHT_M:
        return s, 0.0
    s -= STRAIGHT_M
    if s < arc:
        th = -math.pi / 2 + s / RADIUS_M
        return STRAIGHT_M + RADIUS_M * math.cos(th), RADIUS_M + RADIUS_M * math.sin(th)
    s -= arc
    if s < STRAIGHT_M:
        return STRAIGHT_M - s, 2 * RADIUS_M
    s -= STRAIGHT_M
    th = math.pi / 2 + s / RADIUS_M
    return RADIUS_M * math.cos(th), RADIUS_M + RADIUS_M * math.sin(th)


def drive(v: float, s0: float, laps: float, rate_hz: float, phase: float) -> list[Lap]:
    timer = LapTimer(LINE, min_lap_s=10.0, min_dist_m=300.0)
    out: list[Lap] = []
    n = int((laps * LENGTH_M / v) * rate_hz)
    for k in range(n):
        t_ns = T0 + round((k + phase) * 1e9 / rate_hz)
        x, y = stadium(s0 + v * (t_ns - T0) / 1e9)
        lap = timer.push(t_ns, x, y)
        if lap is not None:
            out.append(lap)
    return out


@pytest.mark.parametrize(("v", "rate_hz", "phase"), [(30.0, 10.0, 0.37), (17.3, 9.68, 0.81)])
def test_stadium_laps_are_length_over_speed(v: float, rate_hz: float, phase: float) -> None:
    s0 = 20.0
    laps = drive(v, s0, 3.5, rate_hz, phase)  # crossings at 100 m + n L, n = 0..3
    assert [lap.lap_no for lap in laps] == [1, 2, 3]
    for n, lap in enumerate(laps):
        assert abs(lap.duration_ns / 1e9 - LENGTH_M / v) < 1e-6
        start_s = (LINE_X - s0 + n * LENGTH_M) / v
        assert abs((lap.t_start_ns - T0) / 1e9 - start_s) < 1e-6
        assert lap.t_end_ns - lap.t_start_ns == lap.duration_ns
        assert lap.line_id == "sf"
        assert lap.valid


def feed(timer: LapTimer, pts: list[tuple[float, float, float]]) -> list[Lap]:
    out = []
    for t, x, y in pts:
        lap = timer.push(T0 + round(t * 1e9), x, y)
        if lap is not None:
            out.append(lap)
    return out


WIGGLE = [
    (0.0, 90.0, 0.0),
    (0.1, 99.0, 0.0),
    (0.2, 100.5, 0.0),  # forward crossing: starts lap 1
    (0.3, 99.8, 0.0),  # back across the line
    (0.4, 100.9, 0.0),  # forward again 0.15 s later: a false re-crossing
    (10.0, 400.0, 0.0),
    (20.0, 400.0, 200.0),
    (30.0, -200.0, 200.0),
    (35.0, -200.0, 0.0),
    (39.0, 99.0, 0.0),
    (40.0, 101.0, 0.0),  # the real next crossing
]


def test_false_recrossing_is_ignored_by_min_lap_guard() -> None:
    laps = feed(LapTimer(LINE, min_lap_s=10.0, min_dist_m=100.0), WIGGLE)
    assert len(laps) == 1
    first = 0.1 + 0.1 * (1.0 / 1.5)
    assert abs((laps[0].t_start_ns - T0) / 1e9 - first) < 1e-9
    assert abs((laps[0].t_end_ns - T0) / 1e9 - 39.5) < 1e-9


def test_without_guards_the_wiggle_would_count() -> None:
    laps = feed(LapTimer(LINE, min_lap_s=0.0, min_dist_m=0.0), WIGGLE)
    assert len(laps) == 2
    second = 0.3 + 0.1 * (0.2 / 1.1)
    assert abs((laps[0].t_end_ns - T0) / 1e9 - second) < 1e-9


def test_min_distance_guard() -> None:
    # Crosses, loops around the end of the line in 40 m, and crosses again 60 s later.
    pts = [
        (0.0, 99.0, 0.0),
        (1.0, 101.0, 0.0),
        (10.0, 110.0, 15.0),
        (20.0, 90.0, 15.0),
        (59.0, 90.0, 0.0),
        (60.0, 101.0, 0.0),
    ]
    assert feed(LapTimer(LINE, min_lap_s=10.0, min_dist_m=100.0), pts) == []
    laps = feed(LapTimer(LINE, min_lap_s=10.0, min_dist_m=50.0), pts)
    assert len(laps) == 1


def test_missing_the_line_segment_is_not_a_crossing() -> None:
    pts = [(0.0, 99.0, 50.0), (1.0, 101.0, 50.0), (20.0, 99.0, 50.0), (40.0, 101.0, 50.0)]
    assert feed(LapTimer(LINE, min_lap_s=0.0, min_dist_m=0.0), pts) == []


def test_point_on_the_line_counts_once() -> None:
    timer = LapTimer(LINE, min_lap_s=0.0, min_dist_m=0.0)
    pts = [(0.0, 99.0, 0.0), (1.0, 100.0, 0.0), (2.0, 101.0, 0.0)]
    assert feed(timer, pts) == []
    assert timer.lap_no == 0


def test_rejects_degenerate_line_and_time_reversal() -> None:
    with pytest.raises(ValueError):
        StartFinishLine("x", 1.0, 1.0, 1.0, 1.0)
    timer = LapTimer(LINE, min_lap_s=0.0, min_dist_m=0.0)
    timer.push(T0 + 10, 0.0, 0.0)
    with pytest.raises(ValueError):
        timer.push(T0 + 10, 1.0, 0.0)
