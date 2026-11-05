"""Synthetic drives and the OBD overlay: ordering, rates, scripted known answers, determinism.

Known answers are closed forms computed here, not read from the module under test:
0 to 60 mph at 0.5 g takes 26.8224 / (0.5 g0) = 5.470247230 s; 60 to 0 mph at 1.0 g covers
26.8224^2 / (2 g0) = 36.68128983 m; a lap of the oval (2 x 300 m straights, 2 half circles of
radius 100 m) at 25 m/s takes (600 + 200 pi) / 25 = 49.13274123 s with 25^2 / 100 = 6.25 m/s^2
of lateral acceleration in the half circles.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from itertools import pairwise

import numpy as np
import pytest

from photoncar.config import Config, ConfigError, load_config, load_vehicle
from photoncar.records import GpsFix, ImuSample, ObdPoll, Record, Sample, record_time
from photoncar.signals import OBD_PID_TO_SIGNAL, SIGNALS
from photoncar.sources.base import SourceMeta
from photoncar.sources.synthetic import (
    SpeedTrace,
    SyntheticDriveSource,
    SyntheticObdSource,
    poll_schedule,
    synthetic_drive,
    to_latlon,
    to_local,
)
from photoncar.units import G0, KELVIN_OFFSET, MPH, MPH_60, NS_PER_S

SEED = 1234
ZERO_TO_SIXTY_S = 5.470247230
SIXTY_TO_ZERO_M = 36.68128983
LAP_S = 49.13274123
LAT_ACCEL = 6.25
# The assumed driveline in synthetic.py: gear ratios, final drive, tire radius (m).
GEARS = (4.06, 2.40, 1.58, 1.19, 1.00, 0.87)
FINAL_DRIVE = 3.85
TIRE_R = 0.335

Drive = tuple[SyntheticDriveSource, list[Record]]


@pytest.fixture(scope="module")
def track() -> Drive:
    src = synthetic_drive(SEED, "track_day")
    return src, list(src.records())


def _s(src: SyntheticDriveSource, t_ns: int) -> float:
    return (t_ns - src.t0_ns) / NS_PER_S


def _gps(recs: Sequence[Record]) -> list[GpsFix]:
    return [r for r in recs if isinstance(r, GpsFix)]


def _imu(recs: Sequence[Record]) -> list[ImuSample]:
    return [r for r in recs if isinstance(r, ImuSample)]


def _polls(recs: Sequence[Record]) -> list[ObdPoll]:
    return [r for r in recs if isinstance(r, ObdPoll)]


def _samples(recs: Sequence[Record], signal: str) -> list[Sample]:
    return [r for r in recs if isinstance(r, Sample) and r.signal == signal]


def test_known_answers_match_closed_forms() -> None:
    assert abs(MPH_60 / (0.5 * G0) - ZERO_TO_SIXTY_S) < 5e-10
    assert abs(MPH_60**2 / (2 * G0) - SIXTY_TO_ZERO_M) < 5e-9
    assert abs((600 + 200 * math.pi) / 25 - LAP_S) < 5e-9


def test_records_are_time_ordered(track: Drive) -> None:
    src, recs = track
    times = [record_time(r) for r in recs]
    assert all(a <= b for a, b in pairwise(times))
    assert times[0] == src.t0_ns
    assert times[-1] <= src.t_end_ns


def test_rates(track: Drive) -> None:
    src, recs = track
    for stream, hz in ((_gps(recs), 10.0), (_imu(recs), 104.0)):
        span_s = (stream[-1].t_ns - stream[0].t_ns) / NS_PER_S
        assert (len(stream) - 1) / span_s == pytest.approx(hz, abs=1e-6)
        assert span_s == pytest.approx(src.duration_s, abs=1.0 / hz)
    polls = _polls(recs)
    span_s = (polls[-1].req_t_ns - polls[0].req_t_ns) / NS_PER_S
    per_pid = Counter(p.pid for p in polls)
    for pid in (0x0C, 0x0D, 0x11):
        assert per_pid[pid] / span_s == pytest.approx(2.0, abs=0.01)
    slow = sum(n for pid, n in per_pid.items() if pid not in (0x0C, 0x0D, 0x11))
    assert slow / span_s == pytest.approx(4.0, abs=0.02)
    gaps = np.diff([p.req_t_ns for p in polls])
    assert set(gaps.tolist()) == {100_000_000}  # 10 requests/s, the config cap


def test_poll_order_follows_config_schedule(track: Drive) -> None:
    _, recs = track
    cfg = load_config()
    slow = [int(p) for p in cfg.get("obd.slow_pids") if int(p) != 0x10]  # E90: no MAF sensor
    fast = [int(p) for p in cfg.get("obd.fast_pids")]
    pids = [p.pid for p in _polls(recs)]
    expected: list[int] = []
    k_slow = 0
    while len(expected) < len(pids):
        expected += fast
        expected += [slow[k_slow % len(slow)], slow[(k_slow + 1) % len(slow)]]
        k_slow += 2
    assert pids == expected[: len(pids)]
    assert 0x10 not in pids


def test_standing_start_at_half_g(track: Drive) -> None:
    src, recs = track
    launch = src.phase("launch")
    assert launch.accel_mps2 == pytest.approx(0.5 * G0, abs=1e-12)
    t_launch = src.t0_ns + round(launch.t_start_s * NS_PER_S)
    truth = src.truth(np.array([t_launch + round(ZERO_TO_SIXTY_S * NS_PER_S)]))
    assert truth.speed_mps[0] == pytest.approx(MPH_60, abs=1e-6)
    assert launch.v_end_mps == pytest.approx(70 * MPH, abs=1e-9)

    gps = _gps(recs)
    at_rest = [g.speed_mps for g in gps if g.t_ns < t_launch]
    assert len(at_rest) == 50 and max(at_rest) < 0.5  # 5 s at rest, GPS noise 0.03 m/s
    # GPS: interpolate the first crossing of 60 mph; tolerance 0.03 s (noise over 4.9 m/s^2).
    t = np.array([_s(src, g.t_ns) for g in gps])
    v = np.array([g.speed_mps for g in gps])
    i = int(np.flatnonzero((t > launch.t_start_s) & (v >= MPH_60))[0])
    t60 = t[i - 1] + (MPH_60 - v[i - 1]) / (v[i] - v[i - 1]) * (t[i] - t[i - 1])
    assert t60 - launch.t_start_s == pytest.approx(ZERO_TO_SIXTY_S, abs=0.03)
    # IMU: mean longitudinal specific force during the launch, noise 0.03 m/s^2 per sample.
    ax = [s.ax for s in _imu(recs) if launch.t_start_s + 0.02 < _s(src, s.t_ns) < launch.t_end_s]
    assert len(ax) > 600
    assert float(np.mean(ax)) == pytest.approx(0.5 * G0, abs=0.005)


def test_sixty_to_zero_at_one_g(track: Drive) -> None:
    src, recs = track
    brake = src.phase("brake")
    assert brake.accel_mps2 == pytest.approx(-G0, abs=1e-12)
    assert brake.v_start_mps > MPH_60
    t60 = brake.t_start_s + (brake.v_start_mps - MPH_60) / G0
    tr = src.truth(src.t0_ns + np.rint(np.array([t60, brake.t_end_s]) * NS_PER_S).astype(np.int64))
    assert tr.x_m[1] - tr.x_m[0] == pytest.approx(SIXTY_TO_ZERO_M, abs=1e-5)
    assert tr.speed_mps[1] == pytest.approx(0.0, abs=1e-8)  # t rounded to 1 ns at 1 g
    # GPS positions (noise 0.15 m) interpolated at the same 2 times: within 0.5 m.
    gps = _gps(recs)
    t = np.array([_s(src, g.t_ns) for g in gps])
    x, _ = to_local(np.array([g.lat_deg for g in gps]), np.array([g.lon_deg for g in gps]))
    assert float(np.interp(brake.t_end_s, t, x) - np.interp(t60, t, x)) == pytest.approx(
        SIXTY_TO_ZERO_M, abs=0.5
    )
    ax = [s.ax for s in _imu(recs) if brake.t_start_s + 0.02 < _s(src, s.t_ns) < brake.t_end_s]
    assert float(np.mean(ax)) == pytest.approx(-G0, abs=0.006)


def _crossings(src: SyntheticDriveSource, gps: list[GpsFix]) -> list[float]:
    line = src.start_finish
    assert line is not None
    lat = np.array([g.lat_deg for g in gps])
    lon = np.array([g.lon_deg for g in gps])
    x, y = to_local(lat, lon)
    t = np.array([_s(src, g.t_ns) for g in gps])
    out = []
    for i in range(len(t) - 1):
        if (x[i] - line.x1) * (x[i + 1] - line.x1) < 0:
            f = (line.x1 - x[i]) / (x[i + 1] - x[i])
            yc = y[i] + f * (y[i + 1] - y[i])
            if min(line.y1, line.y2) <= yc <= max(line.y1, line.y2):
                out.append(float(t[i] + f * (t[i + 1] - t[i])))
    return out


def test_three_laps_of_known_length(track: Drive) -> None:
    src, recs = track
    assert src.track_length_m == pytest.approx(600 + 200 * math.pi, abs=1e-9)
    assert src.lap_speed_mps == 25.0
    crossings = _crossings(src, _gps(recs))
    assert len(crossings) == 4  # 3 laps
    laps = np.diff(crossings)
    assert laps.tolist() == pytest.approx([LAP_S] * 3, abs=0.02)  # GPS noise 0.15 m at 25 m/s
    # Truth: the car is on the line at each crossing time, heading west on the top straight.
    line = src.start_finish
    assert line is not None
    t_first = src.phase("lap_1").t_start_s
    tr = src.truth(
        src.t0_ns + np.rint((t_first + LAP_S * np.arange(4)) * NS_PER_S).astype(np.int64)
    )
    assert tr.x_m.tolist() == pytest.approx([line.x1] * 4, abs=1e-6)
    assert np.all((tr.y_m > line.y1) & (tr.y_m < line.y2))
    assert np.cos(tr.heading_rad).tolist() == pytest.approx([-1.0] * 4, abs=1e-9)


def test_cornering_lateral_accel_and_yaw(track: Drive) -> None:
    src, recs = track
    lap = [p for p in src.phases if p.name.startswith("lap_")]
    t_lo, t_hi = lap[0].t_start_s, lap[-1].t_end_s
    imu = [s for s in _imu(recs) if t_lo < _s(src, s.t_ns) < t_hi]
    tr = src.truth(np.array([s.t_ns for s in imu]))
    in_corner = tr.accel_lat_mps2 > 0
    assert set(np.round(tr.accel_lat_mps2, 9).tolist()) == {0.0, LAT_ACCEL}
    ay = np.array([s.ay for s in imu])
    gz = np.array([s.gz for s in imu])
    assert float(ay[in_corner].mean()) == pytest.approx(LAT_ACCEL, abs=0.005)
    assert float(gz[in_corner].mean()) == pytest.approx(0.25, abs=0.001)
    assert float(ay[~in_corner].mean()) == pytest.approx(0.0, abs=0.005)
    az = np.array([s.az for s in _imu(recs)])
    assert float(az.mean()) == pytest.approx(G0, abs=0.002)


def test_coolant_excursion_over_105_for_20_s(track: Drive) -> None:
    src, recs = track
    assert src.coolant_excursion is not None
    hot_start, hot_end, peak = src.coolant_excursion
    assert (hot_end - hot_start, peak) == (20.0, 106.0)
    coolant = _samples(recs, "obd.coolant_temp")
    hot = [c for c in coolant if c.value > 105.0 + KELVIN_OFFSET]
    assert len(hot) >= 8  # 20 s / 2.25 s per coolant poll
    assert all(hot_start <= _s(src, c.t_ns) < hot_end for c in hot)
    assert max(c.value for c in coolant) == pytest.approx(106.0 + KELVIN_OFFSET, abs=1e-9)
    in_window = [c for c in coolant if hot_start + 2.3 <= _s(src, c.t_ns) < hot_end]
    assert all(c.value == pytest.approx(106.0 + KELVIN_OFFSET, abs=1e-9) for c in in_window)
    before = [c.value - KELVIN_OFFSET for c in coolant if _s(src, c.t_ns) < hot_start - 10]
    assert before and all(88.0 <= c <= 92.0 for c in before)
    after = [c.value - KELVIN_OFFSET for c in coolant if _s(src, c.t_ns) >= hot_end]
    assert after and max(after) <= 101.0  # clears below 102 °C at once
    assert _s(src, coolant[-1].t_ns) - hot_end > 30.0  # time left for the rule to clear


def test_obd_values_plausible(track: Drive) -> None:
    src, recs = track
    by: dict[str, list[Sample]] = defaultdict(list)
    for r in recs:
        if isinstance(r, Sample):
            by[r.signal].append(r)
    for sig in ("obd.stft_b1", "obd.stft_b2", "obd.ltft_b1", "obd.ltft_b2"):
        assert by[sig] and all(abs(s.value) <= 0.03 for s in by[sig])
    assert all(13.9 <= s.value <= 14.3 for s in by["obd.module_voltage"])
    assert all(18.0 <= s.value - KELVIN_OFFSET <= 38.0 for s in by["obd.iat"])
    assert all(0.0 <= s.value <= 1.0 for s in by["obd.throttle"] + by["obd.load"])
    assert all(750.0 <= s.value <= 8300.0 for s in by["obd.rpm"])
    # Speed: whole km/h of the true speed at the response time.
    spd = by["obd.speed"]
    tr = src.truth(np.array([s.t_ns for s in spd]))
    assert np.max(np.abs(np.array([s.value for s in spd]) - tr.speed_mps)) <= 0.5 / 3.6 + 1e-6
    # RPM sits on 1 of the 6 gear ratios once the clutch is out (above 11 m/s).
    rpm = by["obd.rpm"]
    tr = src.truth(np.array([s.t_ns for s in rpm]))
    moving = tr.speed_mps > 11.0
    per_ratio = 60.0 / (2 * math.pi * TIRE_R) * FINAL_DRIVE
    ratios = np.array([s.value for s in rpm])[moving] / (tr.speed_mps[moving] * per_ratio)
    nearest = np.min(np.abs(ratios[:, None] / np.array(GEARS)[None, :] - 1.0), axis=1)
    assert moving.sum() > 300 and float(nearest.max()) < 0.005
    # Throttle wide open during the launch, closed while braking; open loop under full load.
    launch, brake = src.phase("launch"), src.phase("brake")
    thr_launch = [s.value for s in by["obd.throttle"] if launch.t_start_s < _s(src, s.t_ns) < 11.0]
    thr_brake = [s.value for s in by["obd.throttle"] if brake.t_start_s < _s(src, s.t_ns) < 18.5]
    assert thr_launch and min(thr_launch) >= 0.9
    assert thr_brake and max(thr_brake) == 0.0
    assert {s.value for s in by["obd.fuel_system"]} == {2.0, 4.0}


def _decode(pid: int, raw: bytes) -> float:
    """J1979 formulas from docs/phase-1-plan.md section 3, in SI."""
    a = raw[0]
    ab = 256 * raw[0] + raw[1] if len(raw) > 1 else a
    formulas = {
        0x03: float(a),
        0x04: a / 255,
        0x05: a - 40 + KELVIN_OFFSET,
        0x06: a / 128 - 1,
        0x07: a / 128 - 1,
        0x08: a / 128 - 1,
        0x09: a / 128 - 1,
        0x0C: ab / 4,
        0x0D: a / 3.6,
        0x0F: a - 40 + KELVIN_OFFSET,
        0x11: a / 255,
        0x42: ab / 1000,
    }
    return formulas[pid]


def test_polls_carry_j1979_bytes_and_matching_samples(track: Drive) -> None:
    _, recs = track
    for i, r in enumerate(recs):
        if not isinstance(r, ObdPoll):
            continue
        assert (r.status, r.ecu, r.service) == ("ok", 0x7E8, 1)
        assert r.resp_t_ns is not None and r.value is not None
        assert 12_000_000 <= r.resp_t_ns - r.req_t_ns <= 40_000_000
        assert r.value == pytest.approx(_decode(r.pid, r.raw), abs=1e-12)
        signal = OBD_PID_TO_SIGNAL[r.pid]
        assert r.unit == SIGNALS[signal].unit
        nxt = recs[i + 1]
        assert nxt == Sample(r.resp_t_ns, signal, r.value)


def test_synthetic_signals_complete(track: Drive) -> None:
    src, recs = track
    produced = {r.signal for r in recs if isinstance(r, Sample)}
    meta = set(src.meta.synthetic_signals)
    assert produced <= meta
    assert {n for n in meta if n.startswith("obd.")} == produced
    assert {"gps.speed", "imu.accel_x", "imu.gyro_z"} <= meta
    assert set(src.obd.meta.synthetic_signals) == produced
    assert src.meta.kind == "synthetic"


def test_deterministic_for_a_seed(track: Drive) -> None:
    _, recs = track
    assert list(synthetic_drive(SEED, "track_day").records()) == recs
    other = synthetic_drive(SEED + 1, "track_day")
    other_recs = list(other.records())
    assert [p.name for p in other.phases] == [p.name for p in track[0].phases]
    assert other_recs != recs
    assert [record_time(r) for r in _gps(other_recs)] == [record_time(r) for r in _gps(recs)]


def test_gps_near_fake_origin(track: Drive) -> None:
    src, recs = track
    gps = _gps(recs)
    assert src.origin_deg == (0.0, 0.0)
    assert max(abs(g.lat_deg) for g in gps) < 0.02
    assert max(abs(g.lon_deg) for g in gps) < 0.02
    # The first fix is the origin exactly, so a rollup frame from the first kept fix matches.
    assert (gps[0].lat_deg, gps[0].lon_deg) == (0.0, 0.0)
    xs = np.array([0.0, 123.4, -987.6, 1500.0])
    ys = np.array([0.0, -45.6, 789.0, 400.0])
    lat, lon = to_latlon(xs, ys)
    bx, by = to_local(lat, lon)
    assert bx.tolist() == pytest.approx(xs.tolist(), abs=1e-6)
    assert by.tolist() == pytest.approx(ys.tolist(), abs=1e-6)
    # WGS84 at the equator: meridian radius a(1 - e^2) = 6,335,439.327 m, prime vertical
    # a = 6,378,137 m, so 0.001 degree is 110.5743 m north and 111.3195 m east.
    x, y = to_local(np.array([0.001, 0.0]), np.array([0.0, 0.001]))
    assert float(y[0]) == pytest.approx(110.5743, abs=1e-4)
    assert float(x[1]) == pytest.approx(111.3195, abs=1e-4)


def test_commute_scenario() -> None:
    a = synthetic_drive(5, "commute")
    recs = list(a.records())
    times = [record_time(r) for r in recs]
    assert all(x <= y for x, y in pairwise(times))
    assert recs == list(synthetic_drive(5, "commute").records())
    b = synthetic_drive(6, "commute")
    assert b.duration_s != a.duration_s  # the seed picks the plan
    assert a.start_finish is None and a.coolant_excursion is None
    coolant = _samples(recs, "obd.coolant_temp")
    assert coolant and max(c.value for c in coolant) < 100.0 + KELVIN_OFFSET
    gps = _gps(recs)
    stopped = [g for g in gps if g.speed_mps < 0.5]
    assert len(stopped) >= 50  # rests at the start, at the light, and at the end
    assert max(g.speed_mps for g in gps) < 28.0
    assert (len(gps) - 1) / ((gps[-1].t_ns - gps[0].t_ns) / NS_PER_S) == pytest.approx(10.0)


def test_unknown_scenario() -> None:
    with pytest.raises(ValueError, match="unknown scenario"):
        synthetic_drive(1, "drag_race")


class _GpsOnly:
    """A minimal Source: GPS at 10 Hz, speed ramping 0 to 20 m/s over 20 s."""

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(name="gps-only", kind="replay", dataset_ref="some/segment")

    def records(self) -> Iterator[Record]:
        for k in range(201):
            yield GpsFix(
                t_ns=5 * NS_PER_S + k * 100_000_000,
                rx_t_ns=5 * NS_PER_S + k * 100_000_000,
                utc_ns=None,
                lat_deg=0.0,
                lon_deg=0.0,
                alt_m=0.0,
                speed_mps=k * 0.1,
                course_rad=0.0,
                hdop=1.0,
                fix_type=3,
                n_sats=10,
            )


def test_overlay_on_a_replay_source() -> None:
    obd = SyntheticObdSource(_GpsOnly(), seed=3)
    assert obd.meta.kind == "synthetic"
    assert obd.meta.dataset_ref == "some/segment"
    assert obd.meta.name == "synthetic-obd:gps-only"
    recs = list(obd.records())
    polls = _polls(recs)
    assert len(polls) == 200  # 20 s at 10 requests/s
    assert polls[0].req_t_ns == 5 * NS_PER_S
    for p in polls:
        if p.pid == 0x0D:
            assert p.resp_t_ns is not None
            true_v = (p.resp_t_ns - 5 * NS_PER_S) / NS_PER_S  # 1 m/s per s
            assert p.value == pytest.approx(round(true_v * 3.6) / 3.6, abs=1e-9)
    produced = {r.signal for r in recs if isinstance(r, Sample)}
    assert produced == set(obd.meta.synthetic_signals)
    assert list(SyntheticObdSource(_GpsOnly(), seed=3).records()) == recs


def test_overlay_from_trace_with_maf_vehicle() -> None:
    cfg = load_config()
    veh = Config({"vehicle": {**load_vehicle("e90_m3").raw["vehicle"], "has_maf_sensor": True}})
    t = np.arange(0, 60 * NS_PER_S + 1, NS_PER_S // 10, dtype=np.int64)
    v = np.full(len(t), 20.0)
    obd = SyntheticObdSource(SpeedTrace(t, v), seed=9, cfg=cfg, vehicle=veh)
    assert 0x10 in obd.pids
    maf = _samples(list(obd.records()), "obd.maf")
    assert maf and all(0.0 < s.value < 0.4 for s in maf)  # kg/s


def test_schedule_rejects_bad_config() -> None:
    raw = load_config().raw
    not_multiple = Config({**raw, "obd": {**raw["obd"], "slow_hz_total": 3.0}})
    with pytest.raises(ConfigError, match="multiple"):
        poll_schedule(not_multiple)
    too_fast = Config({**raw, "obd": {**raw["obd"], "fast_hz_each": 4.0}})
    with pytest.raises(ConfigError, match="max_request_hz"):
        poll_schedule(too_fast)
