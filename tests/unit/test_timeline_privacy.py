"""The review timeline under the privacy trim (hard rule 7).

Decimation must never carry a value from inside a trim window onto a point outside it, and a
position must be where the car was at that point's own time. Rows inside a window hold marker
values no row outside it has, so any leak shows as a marker in the output.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from photoncar.store.meta import DriveMeta
from photoncar.store.reader import DriveReader, decimate_minmax
from photoncar.store.rollup import TimelineRow
from photoncar.store.writer import DriveWriter
from photoncar.units import G0, NS_PER_S

T0 = 5 * NS_PER_S
STEP = NS_PER_S // 10
N = 6000
MARK = 1000.0
"""Inside a window every value is at least this; outside, every value is under it."""


def _row(i: int, inside: bool) -> TimelineRow:
    base = MARK if inside else 0.0
    return TimelineRow(
        t_ns=T0 + i * STEP,
        speed_mps=base + 10.0 + (i % 7),
        rpm=base + 800.0 + (i % 11),
        throttle=base + 0.2,
        coolant_k=base + 360.0,
        accel_long_mps2=(base + 0.1) * G0,
        accel_lat_mps2=(base + 0.2) * G0,
        x_m=base + (i % 1000) * 0.5,
        y_m=base + math.sin(i / 50.0) * 30.0,
        dr=inside,
    )


def _drive(path: Path, lo: int, hi: int) -> tuple[list[TimelineRow], tuple[int, int]]:
    rows = [_row(i, lo <= i <= hi) for i in range(N)]
    with DriveWriter(path, DriveMeta(drive_id="trim-test", source="synthetic")) as w:
        w.write_timeline(rows)
    return rows, (T0 + lo * STEP, T0 + hi * STEP)


@settings(max_examples=40, deadline=None)
@given(
    lo=st.integers(0, N - 1),
    length=st.integers(0, 400),
    max_points=st.integers(2, 3000),
)
def test_no_value_from_inside_a_trim_window_survives_decimation(
    tmp_path_factory: pytest.TempPathFactory, lo: int, length: int, max_points: int
) -> None:
    path = tmp_path_factory.mktemp("d") / "d.sqlite"
    hi = min(lo + length, N - 1)
    rows, window = _drive(path, lo, hi)
    with DriveReader(path) as r:
        tl = r.timeline(max_points, trim_ns=[window])
    for key in ("speed_mps", "rpm", "throttle", "coolant_c", "accel_long_g", "accel_lat_g"):
        vals = [v for v in tl[key] if v is not None]
        assert vals, key
        limit = MARK - 300.0 if key == "coolant_c" else MARK
        assert max(vals) < limit, key
    for key in ("x_m", "y_m"):
        assert all(v is None or v < MARK for v in tl[key]), key
    assert not any(tl["dr"])  # dr is set only inside the window, which is emptied
    t_lo = (window[0] - T0) / NS_PER_S
    t_hi = (window[1] - T0) / NS_PER_S
    for t, x in zip(tl["t_s"], tl["x_m"], strict=True):
        if t_lo - 1e-9 <= t <= t_hi + 1e-9:
            assert x is None
    assert len(rows) == N


@settings(max_examples=30, deadline=None)
@given(max_points=st.integers(2, 3000))
def test_positions_are_the_positions_at_their_own_times(max_points: int) -> None:
    """Every (t, x, y) point is 1 row of the drive, whatever the bucket's x and y extremes."""
    rng = np.random.default_rng(max_points)
    t = np.arange(N, dtype=np.float64) / 10.0
    x = np.cumsum(rng.normal(0.0, 3.0, N))
    y = np.cumsum(rng.normal(0.0, 3.0, N))
    speed = rng.normal(20.0, 5.0, N)
    t_out, out = decimate_minmax(
        t, {"x_m": x, "y_m": y, "speed_mps": speed}, max_points, sampled=("x_m", "y_m")
    )
    rows = np.searchsorted(t, t_out)
    assert np.array_equal(t[rows], t_out)
    assert np.array_equal(out["x_m"], x[rows]) and np.array_equal(out["y_m"], y[rows])
    if max_points < N:  # the other columns keep every bucket's min and max
        assert out["speed_mps"].max() == speed.max() and out["speed_mps"].min() == speed.min()


def test_the_reader_timeline_samples_positions(tmp_path: Path) -> None:
    """A dip to the side inside 1 bucket: min/max would show its extreme at the bucket's edge
    time; the timeline shows the positions at the edge rows instead."""
    rows = [
        TimelineRow(
            t_ns=T0 + i * STEP,
            speed_mps=10.0,
            rpm=None,
            throttle=None,
            coolant_k=None,
            accel_long_mps2=None,
            accel_lat_mps2=None,
            x_m=float(i),
            y_m=-50.0 if 1005 <= i <= 1010 else 0.0,  # bucket 50 holds rows 1000..1019
        )
        for i in range(N)
    ]
    path = tmp_path / "d.sqlite"
    with DriveWriter(path, DriveMeta(drive_id="dip", source="synthetic")) as w:
        w.write_timeline(rows)
    with DriveReader(path) as r:
        tl = r.timeline(600)
    assert min(v for v in tl["y_m"] if v is not None) == 0.0
    for t, x in zip(tl["t_s"], tl["x_m"], strict=True):
        assert x == round(t * 10.0)
