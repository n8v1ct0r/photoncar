"""Privacy zone, trim windows, and the export filter (hard rule 7).

The property test drives synthetic tracks with a known ground-truth path past a synthetic home
(radius 500 m), samples GPS and other streams from that path, and checks the exported result
against the truth, not only against the windows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from numpy.typing import NDArray

from photoncar.config import ConfigError, HomeZone, load_config, load_home_zone
from photoncar.privacy.export_filter import WindowSet, filter_for_export, record_extent
from photoncar.privacy.geo import (
    EARTH_MEAN_RADIUS_M,
    SPHERE_ERROR_BOUND,
    haversine_m,
    haversine_m_array,
)
from photoncar.privacy.zones import (
    MAX_SPEED_BOUND_MPS,
    T_MAX,
    T_MIN,
    in_zone,
    merge_windows,
    trim_windows,
)
from photoncar.records import CanFrame, Citation, Event, GpsFix, ImuSample, Lap, ObdPoll, Sample
from photoncar.store.rollup import TimelineRow

S = 1_000_000_000
MS = 1_000_000
RADIUS_M = 500.0
F64 = NDArray[np.float64]


# -- haversine ---------------------------------------------------------------------------------


def test_haversine_known_distances() -> None:
    r = EARTH_MEAN_RADIUS_M
    assert haversine_m(10.0, 20.0, 10.0, 20.0) == 0.0
    assert haversine_m(0.0, 0.0, 1.0, 0.0) == pytest.approx(r * math.pi / 180.0, abs=1e-6)
    assert haversine_m(0.0, 0.0, 0.0, 90.0) == pytest.approx(r * math.pi / 2.0, abs=1e-6)
    assert haversine_m(0.0, 0.0, 0.0, 180.0) == pytest.approx(r * math.pi, abs=1e-6)
    assert haversine_m(90.0, 0.0, -90.0, 0.0) == pytest.approx(r * math.pi, abs=1e-6)
    # 1 degree of longitude at 60 degrees latitude along the parallel is not a great circle;
    # the great-circle chord is shorter than r*cos(60)*pi/180 (111,195 * 0.5 = 55,597.5 m).
    d = haversine_m(60.0, 0.0, 60.0, 1.0)
    assert d < r * 0.5 * math.pi / 180.0
    assert d == pytest.approx(55_597.5, rel=1e-4)


@settings(max_examples=200, deadline=None, derandomize=True)
@given(
    st.floats(-80, 80),
    st.floats(-180, 180),
    st.floats(-80, 80),
    st.floats(-180, 180),
)
def test_haversine_agrees_with_the_spherical_law_of_cosines(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> None:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    c = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)
    ref = EARTH_MEAN_RADIUS_M * math.acos(max(-1.0, min(1.0, c)))
    d = haversine_m(lat1, lon1, lat2, lon2)
    if ref > 1000.0:  # the law of cosines is ill-conditioned below about 1 km
        assert d == pytest.approx(ref, rel=1e-6)
    assert d == pytest.approx(haversine_m(lat2, lon2, lat1, lon1), abs=1e-6)
    arr = haversine_m_array(np.array([lat2]), np.array([lon2]), lat1, lon1)
    assert float(arr[0]) == pytest.approx(d, abs=1e-6)


def test_in_zone_counts_the_boundary_as_inside() -> None:
    zone = HomeZone(12.0, 34.0, RADIUS_M)
    dlat = math.degrees(RADIUS_M / EARTH_MEAN_RADIUS_M)
    assert in_zone(12.0 + dlat * 0.999, 34.0, zone)
    assert not in_zone(12.0 + dlat * 1.001, 34.0, zone)
    assert in_zone(12.0, 34.0, zone)


# -- trim windows ------------------------------------------------------------------------------


def _north_fix(zone: HomeZone, t_ns: int, d_m: float) -> GpsFix:
    lat = zone.lat_deg + math.degrees(d_m / EARTH_MEAN_RADIUS_M)
    return GpsFix(t_ns, t_ns, None, lat, zone.lon_deg, 0.0, 10.0, 0.0, 1.0, 3, 9)


def _local_fix(zone: HomeZone, t_ns: int, x_m: float, y_m: float) -> GpsFix:
    lat, lon = _to_latlon(np.array([x_m]), np.array([y_m]), zone)
    return GpsFix(t_ns, t_ns, None, float(lat[0]), float(lon[0]), 0.0, 10.0, 0.0, 1.0, 3, 9)


def _covered(windows: list[tuple[int, int]], lo: int, hi: int) -> bool:
    return any(a <= lo and hi <= b for a, b in windows)


def test_trim_windows_extend_to_the_neighbouring_fixes() -> None:
    zone = HomeZone(-33.0, 151.0, RADIUS_M)
    dists = [2000.0, 1000.0, 400.0, 300.0, 1000.0, 2000.0]
    fixes = [_north_fix(zone, i * S, d) for i, d in enumerate(dists)]
    w = trim_windows(fixes, zone)
    r_pad = RADIUS_M * (1 + SPHERE_ERROR_BOUND)
    head_end = -round((2000.0 - r_pad) / MAX_SPEED_BOUND_MPS * S)
    tail_start = 5 * S + round((2000.0 - r_pad) / MAX_SPEED_BOUND_MPS * S)
    assert len(w) == 3
    assert w[0][0] == T_MIN and abs(w[0][1] - head_end) <= 10
    assert w[1] == (1 * S, 4 * S)  # fixes 2 and 3 are inside; their neighbours bound the window
    assert abs(w[2][0] - tail_start) <= 10 and w[2][1] == T_MAX


def test_trim_windows_catch_a_path_that_clips_the_zone_between_fixes() -> None:
    zone = HomeZone(48.0, 11.0, RADIUS_M)
    # Both fixes are 512 m from home; the straight path between them passes 400 m from home.
    a, b = _local_fix(zone, 0, -320.0, 400.0), _local_fix(zone, S, 320.0, 400.0)
    assert not in_zone(a.lat_deg, a.lon_deg, zone) and not in_zone(b.lat_deg, b.lon_deg, zone)
    assert _covered(trim_windows([a, b], zone), 0, S)


@pytest.mark.parametrize(("offset_m", "trimmed"), [(400.0, True), (505.0, True), (515.0, False)])
def test_guard_band_at_10_hz_is_about_8_m(offset_m: float, trimmed: bool) -> None:
    zone = HomeZone(0.0, 0.0, RADIUS_M)
    a = _local_fix(zone, 0, -3.0, offset_m)
    b = _local_fix(zone, 100 * MS, 3.0, offset_m)
    assert _covered(trim_windows([a, b], zone), 0, 100 * MS) is trimmed


def test_no_usable_fix_trims_everything_and_one_home_fix_too() -> None:
    zone = HomeZone(0.0, 0.0, RADIUS_M)
    assert trim_windows([], zone) == [(T_MIN, T_MAX)]
    bad = GpsFix(0, 0, None, math.nan, 0.0, 0.0, 0.0, 0.0, 1.0, 3, 1)
    assert trim_windows([bad], zone) == [(T_MIN, T_MAX)]
    assert trim_windows([_north_fix(zone, 5 * S, 10.0)], zone) == [(T_MIN, T_MAX)]
    with pytest.raises(ValueError):
        trim_windows([bad], zone, max_speed_mps=0.0)


def test_merge_windows() -> None:
    assert merge_windows([(5, 9), (1, 3), (3, 4), (8, 12), (20, 19)]) == [(1, 4), (5, 12)]


# -- export filter -----------------------------------------------------------------------------


def test_filter_uses_each_record_extent() -> None:
    windows = [(100, 200)]
    cit_in = Citation("obd.rpm", "OBD 0x0C", "trigger", 150, 160, "max", 7000.0, "rpm", 3)
    cit_out = Citation("obd.rpm", "OBD 0x0C", "trigger", 10, 50, "max", 7000.0, "rpm", 3)
    ev_touch = Event("alert.x", "x", 1, 1, 10, 50, 1.0, None, "", "x", (cit_in,))
    ev_clear = Event("alert.x", "x", 1, 1, 10, 50, 1.0, None, "", "x", (cit_out,))
    assert record_extent(ev_touch) == (10, 160)
    streams: dict[str, list[object]] = {
        "samples": [Sample(t, "obd.rpm", 1.0) for t in (99, 100, 200, 201)],
        "events": [ev_touch, ev_clear],
        "obd": [
            ObdPoll(90, 110, 0x0C, 0x7E8, "ok", b"", 1.0, "rpm"),
            ObdPoll(50, None, 0x0C, None, "timeout", b"", None, "rpm"),
        ],
        "laps": [Lap("sf", 1, 150, 400, 250, True), Lap("sf", 2, 400, 600, 200, True)],
        "timeline": [TimelineRow(150, None, None, None, None, None, None, None, None)],
    }
    out = filter_for_export(streams, windows)
    assert [s.t_ns for s in out["samples"] if isinstance(s, Sample)] == [99, 201]
    assert out["events"] == [ev_clear]
    assert out["obd"] == [streams["obd"][1]]
    assert out["laps"] == [streams["laps"][1]]
    assert out["timeline"] == []
    with pytest.raises(TypeError):
        filter_for_export({"x": [object()]}, windows)
    assert WindowSet([]).overlaps(T_MIN, T_MAX) is False


# -- property test on ground-truth tracks ------------------------------------------------------


def _to_latlon(x: F64, y: F64, zone: HomeZone) -> tuple[F64, F64]:
    lat = zone.lat_deg + np.degrees(y / EARTH_MEAN_RADIUS_M)
    lon = zone.lon_deg + np.degrees(
        x / (EARTH_MEAN_RADIUS_M * math.cos(math.radians(zone.lat_deg)))
    )
    return lat, lon


@dataclass(frozen=True)
class Track:
    """A constant-speed polyline in meters east/north of home, plus when GPS was available."""

    kind: str
    zone: HomeZone
    waypoints: tuple[tuple[float, float], ...]
    speed_mps: float
    gps_on_s: float
    gps_off_before_end_s: float
    gap_at: float
    gap_s: float

    def cumulative(self) -> F64:
        p = np.array(self.waypoints)
        return np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(p, axis=0).T))))

    @property
    def duration_s(self) -> float:
        return float(self.cumulative()[-1]) / self.speed_mps

    def truth(self, t_ns: NDArray[np.int64]) -> tuple[F64, F64]:
        cum = self.cumulative()
        p = np.array(self.waypoints)
        s = np.clip(t_ns / S * self.speed_mps, 0.0, cum[-1])
        return _to_latlon(np.interp(s, cum, p[:, 0]), np.interp(s, cum, p[:, 1]), self.zone)

    def distance_from_home(self, t_ns: NDArray[np.int64]) -> F64:
        lat, lon = self.truth(t_ns)
        return haversine_m_array(lat, lon, self.zone.lat_deg, self.zone.lon_deg)


def _polar(r: float, a: float) -> tuple[float, float]:
    return (r * math.cos(a), r * math.sin(a))


@st.composite
def tracks(draw: st.DrawFn) -> Track:
    zone = HomeZone(draw(st.floats(-60, 60)), draw(st.floats(-179, 179)), RADIUS_M)
    kind = draw(st.sampled_from(["through", "graze", "start_inside", "end_inside", "random"]))
    angle = st.floats(0.0, 2 * math.pi)
    theta = draw(angle)
    ux, uy = math.cos(theta), math.sin(theta)
    pts: list[tuple[float, float]]
    if kind in ("through", "graze"):
        off = draw(st.floats(0.0, 450.0) if kind == "through" else st.floats(480.0, 530.0))
        off *= draw(st.sampled_from([-1.0, 1.0]))
        cx, cy = -uy * off, ux * off
        pts = [(cx - 2500 * ux, cy - 2500 * uy), (cx + 2500 * ux, cy + 2500 * uy)]
    elif kind in ("start_inside", "end_inside"):
        pts = [_polar(draw(st.floats(0.0, 450.0)), theta)]
        for _ in range(draw(st.integers(1, 3))):
            pts.append(_polar(draw(st.floats(800.0, 2500.0)), draw(angle)))
        if kind == "end_inside":
            pts.reverse()
    else:
        coord = st.floats(-1500.0, 1500.0)
        pts = [(draw(coord), draw(coord)) for _ in range(draw(st.integers(2, 4)))]
    return Track(
        kind=kind,
        zone=zone,
        waypoints=tuple(pts),
        speed_mps=draw(st.floats(15.0, 60.0)),
        gps_on_s=draw(st.floats(0.0, 20.0)),
        gps_off_before_end_s=draw(st.floats(0.0, 20.0)),
        gap_at=draw(st.floats(0.0, 1.0)),
        gap_s=draw(st.one_of(st.just(0.0), st.floats(0.5, 40.0))),
    )


def _grid(t_end_ns: int, period_ns: int, phase_ns: int) -> NDArray[np.int64]:
    return np.arange(phase_ns, t_end_ns + 1, period_ns, dtype=np.int64)


def _streams(track: Track) -> dict[str, list[object]]:
    t_end = int(track.duration_s * S)
    gps_t = _grid(t_end, 100 * MS, 0)
    on, off = int(track.gps_on_s * S), t_end - int(track.gps_off_before_end_s * S)
    gap0 = on + int(track.gap_at * max(off - on, 0))
    gap1 = gap0 + int(track.gap_s * S)
    keep = (gps_t >= on) & (gps_t <= off) & ~((gps_t >= gap0) & (gps_t < gap1))
    gps_t = gps_t[keep]
    lat, lon = track.truth(gps_t)
    fixes: list[object] = [
        GpsFix(int(t), int(t), None, float(a), float(b), 0.0, track.speed_mps, 0.0, 0.9, 3, 10)
        for t, a, b in zip(gps_t, lat, lon, strict=True)
    ]
    imu: list[object] = [
        ImuSample(int(t), 0.0, 0.0, 9.8, 0.0, 0.0, 0.0) for t in _grid(t_end, 50 * MS, 13 * MS)
    ]
    obd: list[object] = [
        Sample(int(t), "obd.speed", track.speed_mps) for t in _grid(t_end, 500 * MS, 7 * MS)
    ]
    can: list[object] = [
        CanFrame(int(t), 0, 0x1A0, False, 8, bytes(8)) for t in _grid(t_end, 200 * MS, 37 * MS)
    ]
    rows: list[object] = [
        TimelineRow(int(t), track.speed_mps, None, None, None, 0.0, 0.0, None, None)
        for t in _grid(t_end, 100 * MS, 50 * MS)
    ]
    events: list[object] = []
    for t in _grid(t_end - 2 * S, 7 * S, 3 * S):
        t0 = int(t)
        cite = Citation(
            "state.speed", "Estimator", "trigger", t0 - S // 2, t0 + 2 * S, "max", 1.0, "m/s", 20
        )
        events.append(
            Event(
                "alert.test", "test", 1, 1, t0, t0 + 1_500 * MS, 1.0, 0.5, "m/s", "Test.", (cite,)
            )
        )
    return {"gps": fixes, "imu": imu, "obd": obd, "can": can, "timeline": rows, "events": events}


_HOME = HomeZone(51.0, 7.0, RADIUS_M)


@settings(
    max_examples=200,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(tracks())
# GPS drops out for 45 s, covering the whole pass through the zone (66.7 s to 100 s).
@example(Track("through", _HOME, ((-2500.0, 0.0), (2500.0, 0.0)), 30.0, 0.0, 0.0, 0.36, 45.0))
# Cold start: the car leaves home 20 s before the first fix.
@example(Track("start_inside", _HOME, ((0.0, 0.0), (2000.0, 1500.0)), 25.0, 20.0, 0.0, 0.0, 0.0))
# GPS lost 20 s before the car reaches home.
@example(Track("end_inside", _HOME, ((2000.0, -1500.0), (50.0, 0.0)), 25.0, 0.0, 20.0, 0.0, 0.0))
def test_export_filter_leaves_nothing_near_home(track: Track) -> None:
    streams = _streams(track)
    fixes = [f for f in streams["gps"] if isinstance(f, GpsFix)]
    windows = trim_windows(fixes, track.zone)
    ws = WindowSet(windows)
    out = filter_for_export(streams, windows)

    # 1. No exported GPS point is within R of home.
    for f in out["gps"]:
        assert isinstance(f, GpsFix)
        assert haversine_m(f.lat_deg, f.lon_deg, track.zone.lat_deg, track.zone.lon_deg) > RADIUS_M

    # 2. No exported record of any stream falls inside a trim window.
    for recs in out.values():
        assert not any(ws.contains_record(r) for r in recs)

    # 3. Ground truth: where the vehicle really was at every exported record time.
    for name, recs in out.items():
        if not recs:
            continue
        times: list[int] = []
        for r in recs:
            lo, hi = record_extent(r)
            times.extend(np.linspace(lo, hi, 11).astype(np.int64).tolist() if hi > lo else [lo])
        d = track.distance_from_home(np.array(times, dtype=np.int64))
        assert float(d.min()) > RADIUS_M, f"{name}: a record {d.min():.1f} m from home survived"

    # The track generator really does reach the zone in the kinds that should.
    if track.kind in ("through", "start_inside", "end_inside"):
        t = np.arange(0, int(track.duration_s * S), 20 * MS, dtype=np.int64)
        assert float(track.distance_from_home(t).min()) <= RADIUS_M


def test_filter_keeps_records_outside_the_guard_band() -> None:
    """Precision: with 10 Hz GPS and no dropouts, records over 520 m from home all survive."""
    track = Track(
        "through",
        HomeZone(40.0, -3.0, RADIUS_M),
        ((-3000.0, 0.0), (3000.0, 0.0)),
        30.0,
        0.0,
        0.0,
        0.0,
        0.0,
    )
    streams = _streams(track)
    fixes = [f for f in streams["gps"] if isinstance(f, GpsFix)]
    out = filter_for_export(streams, trim_windows(fixes, track.zone))
    imu_t = np.array([r.t_ns for r in streams["imu"] if isinstance(r, ImuSample)], dtype=np.int64)
    kept_t = {r.t_ns for r in out["imu"] if isinstance(r, ImuSample)}
    d = track.distance_from_home(imu_t)
    far = imu_t[d > 520.0]
    near = imu_t[d <= RADIUS_M]
    assert far.size > 0 and near.size > 0
    assert all(int(t) in kept_t for t in far)
    assert not any(int(t) in kept_t for t in near)
    assert len(out["imu"]) < len(streams["imu"])


# -- the privacy file --------------------------------------------------------------------------


def test_missing_privacy_file_raises_config_error(tmp_path: Path) -> None:
    cfg = load_config()
    with pytest.raises(ConfigError, match="privacy file missing"):
        load_home_zone(cfg, tmp_path / "privacy.local.toml")
    p = tmp_path / "privacy.local.toml"
    p.write_text("[home]\nlat_deg = 12.5\nlon_deg = -45.25\n", encoding="utf-8")
    zone = load_home_zone(cfg, p)
    assert zone == HomeZone(12.5, -45.25, 500.0)
