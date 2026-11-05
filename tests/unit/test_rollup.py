"""The 10 Hz rollup and the local ENU frame."""

from __future__ import annotations

import math
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest

from photoncar.privacy.geo import SPHERE_ERROR_BOUND, WGS84_A_M, WGS84_E2, enu_m, haversine_m
from photoncar.records import GpsFix, Sample, StateEstimate
from photoncar.store.meta import DriveMeta
from photoncar.store.reader import DriveReader
from photoncar.store.rollup import (
    GRID_NS,
    RollupInputs,
    build_timeline_10hz,
    enu_origin,
    interp_gap,
    is_kept_fix,
)
from photoncar.store.writer import DriveWriter

MS = 1_000_000
S = 1_000_000_000
I64 = np.int64


def test_enu_known_answers_at_the_equator() -> None:
    east, north = enu_m([0.0], [0.001], 0.0, 0.0)
    assert east[0] == pytest.approx(WGS84_A_M * math.sin(math.radians(0.001)), abs=1e-6)
    assert north[0] == pytest.approx(0.0, abs=1e-9)
    east, north = enu_m([0.001], [0.0], 0.0, 0.0)
    # Meridian radius of curvature at the equator is a(1 - e^2): 110.574 m per 0.001 degree.
    assert north[0] == pytest.approx(WGS84_A_M * (1 - WGS84_E2) * math.radians(0.001), abs=1e-6)
    assert east[0] == pytest.approx(0.0, abs=1e-9)
    assert north[0] == pytest.approx(110.574, abs=1e-3)


@pytest.mark.parametrize("lat0", [-52.0, -10.0, 23.5, 40.0, 61.0])
def test_enu_distance_matches_haversine_within_the_sphere_error_bound(lat0: float) -> None:
    lon0 = 30.0
    angles = np.linspace(0.0, 2 * math.pi, 16, endpoint=False)
    lat = lat0 + 0.009 * np.sin(angles)
    lon = lon0 + 0.012 * np.cos(angles)
    east, north = enu_m(lat, lon, lat0, lon0)
    for i in range(angles.size):
        d = haversine_m(lat0, lon0, float(lat[i]), float(lon[i]))
        assert math.hypot(east[i], north[i]) == pytest.approx(d, rel=SPHERE_ERROR_BOUND)
    e_n, n_n = enu_m([lat0 + 0.01], [lon0], lat0, lon0)
    assert abs(e_n[0]) < 1e-6 and n_n[0] > 0  # due north is +y
    e_e, n_e = enu_m([lat0], [lon0 + 0.01], lat0, lon0)
    assert e_e[0] > 0 and abs(n_e[0]) < 0.5  # due east is +x (small curvature drop)


def _inputs(
    *,
    state_t: list[int],
    speed: list[float],
    dr: list[bool] | None = None,
    gps_t: list[int] | None = None,
    lat: list[float] | None = None,
    lon: list[float] | None = None,
    gps_speed: list[float] | None = None,
    fix_type: list[int] | None = None,
    signals: dict[str, tuple[list[int], list[float]]] | None = None,
    synthetic: frozenset[str] = frozenset(),
) -> RollupInputs:
    n = len(state_t)
    g = gps_t or []
    return RollupInputs(
        state_t_ns=np.array(state_t, dtype=I64),
        state_speed=np.array(speed, dtype=np.float64),
        state_accel_long=np.full(n, 1.0),
        state_accel_lat=np.full(n, -0.5),
        state_dr=np.array(dr if dr is not None else [False] * n, dtype=np.bool_),
        gps_t_ns=np.array(g, dtype=I64),
        gps_lat=np.array(lat or [], dtype=np.float64),
        gps_lon=np.array(lon or [], dtype=np.float64),
        gps_speed=np.array(gps_speed or [math.nan] * len(g), dtype=np.float64),
        gps_fix_type=np.array(fix_type or [3] * len(g), dtype=I64),
        signals={
            k: (np.array(t, dtype=I64), np.array(v, dtype=np.float64))
            for k, (t, v) in (signals or {}).items()
        },
        synthetic=synthetic,
    )


def test_rollup_grid_bucket_means_and_dr() -> None:
    # State at 104 Hz for 10 s; speed ramps so bucket means are checkable; dr from 5 s to 6 s.
    t = [round(i * S / 104) for i in range(1040)]
    speed = [0.5 * ti / S for ti in t]
    dr = [5 * S <= ti < 6 * S for ti in t]
    rows = build_timeline_10hz(_inputs(state_t=t, speed=speed, dr=dr))
    assert len(rows) == 100  # 0 .. 9.9 s (the last estimate is at 9.990 s)
    assert all(b.t_ns - a.t_ns == GRID_NS for a, b in pairwise(rows))
    ta = np.array(t)
    for row in rows[1:-1]:
        in_bucket = (ta >= row.t_ns - 50 * MS) & (ta < row.t_ns + 50 * MS)
        assert row.speed_mps == pytest.approx(float(np.mean(np.array(speed)[in_bucket])), abs=1e-12)
        assert row.dr == bool(np.any(np.array(dr)[in_bucket]))
        assert row.accel_long_mps2 == pytest.approx(1.0, abs=1e-12)
        assert row.accel_lat_mps2 == pytest.approx(-0.5, abs=1e-12)
    assert [r.t_ns for r in rows if r.dr] == [ns * 100 * MS for ns in range(50, 61)]
    assert all(r.x_m is None and r.rpm is None for r in rows)


def test_rollup_xy_starts_at_the_first_kept_fix_and_interpolates_signals() -> None:
    lat0, lon0 = 0.0, 0.0
    gps_t = [i * 100 * MS for i in range(60)]
    east_per_fix = 2.0  # 20 m/s
    lat = [lat0] * 60
    lon = [lon0 + math.degrees(i * east_per_fix / WGS84_A_M) for i in range(60)]
    fix_type = [0, 1] + [3] * 58  # the first 2 fixes are not kept
    lat[0], lon[0] = 45.0, 45.0  # garbage position on a no-fix report
    inp = _inputs(
        state_t=[],
        speed=[],
        gps_t=gps_t,
        lat=lat,
        lon=lon,
        gps_speed=[20.0] * 60,
        fix_type=fix_type,
        signals={
            "obd.rpm": ([0, 500 * MS, 1000 * MS, 4000 * MS], [1000.0, 2000.0, 3000.0, 4000.0]),
            "obd.coolant_temp": ([0, 2500 * MS, 5000 * MS], [360.0, 362.5, 365.0]),
            "obd.throttle": ([0, 500 * MS], [0.1, 0.3]),
        },
    )
    assert enu_origin(inp) == (lat[2], lon[2])
    rows = build_timeline_10hz(inp)
    assert len(rows) == 60 and rows[0].t_ns == 0
    assert rows[0].x_m is None and rows[1].x_m is None  # before the first kept fix
    assert rows[2].x_m == pytest.approx(0.0, abs=1e-9)
    east, north = enu_m(lat[2:], lon[2:], lat[2], lon[2])
    for i in range(2, 60):
        assert rows[i].x_m == pytest.approx(float(east[i - 2]), abs=1e-9)
        assert rows[i].y_m == pytest.approx(float(north[i - 2]), abs=1e-9)
        assert rows[i].speed_mps == pytest.approx(20.0, abs=1e-12)  # GPS speed fallback
    assert rows[40].x_m == pytest.approx(76.0, abs=1e-3)
    # rpm: linear between samples, blank across the 3 s gap (over the 1.5 s bound).
    assert rows[2].rpm == pytest.approx(1400.0, abs=1e-9)
    assert rows[10].rpm == pytest.approx(3000.0, abs=1e-9)
    assert all(rows[i].rpm is None for i in range(11, 40))
    assert rows[40].rpm == pytest.approx(4000.0, abs=1e-9)
    # coolant: slow group, 2.5 s gaps are within the 7.5 s bound.
    assert rows[30].coolant_k == pytest.approx(363.0, abs=1e-9)
    assert rows[51].coolant_k is None  # after the last sample
    assert rows[3].throttle == pytest.approx(0.22, abs=1e-12)
    assert rows[6].throttle is None


def test_rollup_prefers_state_speed_and_falls_back_per_row() -> None:
    state_t = [i * 10 * MS for i in range(100, 200)]  # state only from 1.0 s to 1.99 s
    inp = _inputs(
        state_t=state_t,
        speed=[30.0] * 100,
        signals={"obd.speed": ([0, 3000 * MS], [10.0, 10.0])},
    )
    rows = build_timeline_10hz(inp)
    by_t = {r.t_ns: r for r in rows}
    assert by_t[1500 * MS].speed_mps == 30.0
    assert by_t[500 * MS].speed_mps is None  # obd.speed has a 3 s gap: over the bound
    inp2 = _inputs(
        state_t=state_t,
        speed=[30.0] * 100,
        signals={"obd.speed": ([i * 500 * MS for i in range(7)], [10.0] * 7)},
    )
    by_t2 = {r.t_ns: r for r in build_timeline_10hz(inp2)}
    assert by_t2[500 * MS].speed_mps == pytest.approx(10.0, abs=1e-12)
    assert by_t2[1500 * MS].speed_mps == 30.0
    assert by_t2[2500 * MS].speed_mps == pytest.approx(10.0, abs=1e-12)


def test_rollup_reads_measured_signals_before_synthetic_ones() -> None:
    # A replay: measured CAN rpm and wheel speed, with a synthetic OBD overlay that reads
    # higher. obd.rpm and obd.speed come first in their source lists, yet the measured signals
    # win. CAN rpm has a gap from 2 s to 4 s: it stays blank, never filled with synthetic rpm.
    can_t = [i * 100 * MS for i in range(21)] + [i * 100 * MS for i in range(41, 61)]
    obd_t = [i * 500 * MS for i in range(13)]
    signals = {
        "obd.rpm": (obd_t, [3000.0] * 13),
        "can.engine_rpm": (can_t, [1500.0] * len(can_t)),
        "obd.speed": (obd_t, [20.0] * 13),
        "can.wheel_speed": (can_t, [12.0] * len(can_t)),
        "obd.coolant_temp": (obd_t, [370.0] * 13),
    }
    synthetic = frozenset({"obd.rpm", "obd.speed", "obd.coolant_temp"})
    rows = build_timeline_10hz(_inputs(state_t=[], speed=[], signals=signals, synthetic=synthetic))
    by_t = {r.t_ns: r for r in rows}
    measured = [by_t[i * 100 * MS] for i in (*range(21), *range(41, 61))]
    assert {r.rpm for r in measured} == {1500.0} and {r.speed_mps for r in measured} == {12.0}
    gap = [by_t[i * 100 * MS] for i in range(21, 41)]
    assert all(r.rpm is None and r.speed_mps is None for r in gap)
    # The drive has no measured coolant: the synthetic one is all there is, so it shows.
    assert {r.coolant_k for r in rows} == {370.0}
    # Without the synthetic labels (a real car polling OBD), the source order applies again.
    plain = build_timeline_10hz(_inputs(state_t=[], speed=[], signals=signals))
    assert {r.rpm for r in plain} == {3000.0}


def test_rollup_reads_the_synthetic_labels_from_the_drive_file(tmp_path: Path) -> None:
    path = tmp_path / "d.sqlite"
    meta = DriveMeta(drive_id="r2", source="replay", synthetic_signals=("obd.rpm",))
    with DriveWriter(path, meta) as w:
        for i in range(30):  # 3 s: CAN rpm at 10 Hz, the synthetic OBD rpm at 2 Hz
            w.write(Sample(i * 100 * MS, "can.engine_rpm", 1200.0))
            if i % 5 == 0:
                w.write(Sample(i * 100 * MS, "obd.rpm", 2400.0))
    with DriveReader(path) as r:
        assert r.rollup_inputs().synthetic == frozenset({"obd.rpm"})
        rows = r.timeline_rows()
        timeline = r.timeline()
    assert len(rows) == 30 and {row.rpm for row in rows} == {1200.0}
    assert set(timeline["rpm"]) == {1200.0}


def test_rollup_rejects_unsorted_input_and_handles_empty() -> None:
    assert build_timeline_10hz(_inputs(state_t=[], speed=[])) == []
    with pytest.raises(ValueError, match="not sorted"):
        build_timeline_10hz(_inputs(state_t=[2, 1], speed=[0.0, 0.0]))


def test_interp_gap_edges() -> None:
    t = np.array([0, 100, 400], dtype=I64)
    v = np.array([0.0, 10.0, 40.0])
    grid = np.array([-1, 0, 50, 100, 250, 400, 401], dtype=I64)
    out = interp_gap(t, v, grid, 150)
    assert np.isnan(out[0]) and np.isnan(out[4]) and np.isnan(out[6])
    assert out[1:4].tolist() == [0.0, 5.0, 10.0]
    assert out[5] == 40.0  # exact hit on the last sample
    one = interp_gap(np.array([7], dtype=I64), np.array([3.0]), np.array([6, 7, 8], dtype=I64), 10)
    assert np.isnan(one[0]) and one[1] == 3.0 and np.isnan(one[2])


def test_rollup_is_built_at_clean_close_and_matches_a_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "d.sqlite"
    with DriveWriter(path, DriveMeta(drive_id="r1", source="synthetic")) as w:
        for i in range(520):  # 5 s of state at 104 Hz, GPS at 10 Hz, rpm at 2 Hz
            t = round(i * S / 104)
            if i % 52 == 0:
                w.write(Sample(t, "obd.rpm", 2000.0 + i))
            w.write(StateEstimate(t, 15.0, 0.2, 0.1, 0.1, 0.0, 0.1, 0.1, "gps+imu"))
        for k in range(50):
            fix = GpsFix(k * 100 * MS, k * 100 * MS, None, 0.0, k * 1e-5, 0.0, 15.0, 0.0, 1.0, 3, 9)
            assert is_kept_fix(fix)
            w.write(fix)
    with DriveReader(path) as r:
        stored = r.timeline_rows()
        rebuilt = build_timeline_10hz(r)
    assert len(stored) == 50
    assert stored == rebuilt
    assert stored[0].x_m == 0.0 and stored[49].x_m == pytest.approx(
        WGS84_A_M * math.sin(math.radians(49e-5)), abs=1e-6
    )
    assert not is_kept_fix(GpsFix(0, 0, None, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0, 1, 4))
    assert not is_kept_fix(GpsFix(0, 0, None, math.nan, 1.0, 0.0, 0.0, 0.0, 1.0, 3, 4))
