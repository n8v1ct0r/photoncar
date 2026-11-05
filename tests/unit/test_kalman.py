"""State estimator on synthetic drives: accuracy bound, dead reckoning, and zero-velocity updates.

Speed RMS bound (derived, then asserted):
    The filter models longitudinal accel noise as white with PSD q = sigma_a^2 / f_imu
    (sigma_a = 0.5 m/s^2, f_imu = 104 Hz) and GPS speed noise R = 0.05^2 (m/s)^2. Between
    10 Hz GPS updates the speed variance grows by Q = q * 0.1 s = 2.40e-4. The scalar
    steady-state Riccati solution is P+ = (-Q + sqrt(Q^2 + 4 Q R)) / 2 = 6.64e-4 after an
    update and P- = P+ + Q = 9.04e-4 just before the next. The synthetic IMU noise (0.05 m/s^2)
    is well under the modeled 0.5 m/s^2 and the GPS noise equals the modeled value, so the
    filter is conservative: the true mean square error stays under P-. Bound:
    RMS < sqrt(P-) = 0.0301 m/s, against 0.05 m/s for raw GPS.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import pytest

from photoncar.config import Config, load_config
from photoncar.detect.performance import zero_to_sixty
from photoncar.estimate.kalman import EstimatorTuning, StateEstimator
from photoncar.records import GpsFix, ImuSample, Record, Sample, StateEstimate, record_time
from photoncar.units import G0, MPH_60

T0 = 5_000_000_000_000
IMU_HZ = 104.0
GPS_NOISE = 0.05
ACCEL_NOISE = 0.05
ACCEL_BIAS = 0.03
GYRO_BIAS = 0.01

Plan = list[tuple[float, float, float]]  # (duration s, accel m/s^2, yaw rate rad/s)
Truth = Callable[[np.ndarray], tuple[np.ndarray, np.ndarray, np.ndarray]]

COMMUTE: Plan = [
    (5.0, 0.0, 0.0),
    (8.0, 0.3 * G0, 0.0),
    (10.0, 0.0, 0.0),
    (6.0, 0.0, 0.15),
    (5.0, 0.0, 0.0),
]
LAUNCH: Plan = [(5.0, 0.0, 0.0), (6.0, 0.5 * G0, 0.0), (3.0, 0.0, 0.0)]


@pytest.fixture(scope="module")
def cfg() -> Config:
    return load_config()


def synth(
    plan: Plan,
    seed: int,
    *,
    outage: tuple[float, float] | None = None,
    rot: np.ndarray | None = None,
    gps: bool = True,
) -> tuple[list[Record], Truth]:
    """IMU at 104 Hz and GPS speed at 10 Hz for a straight-line plan ending in a 0.5 g stop."""
    rng = np.random.default_rng(seed)
    v_end = sum(d * a for d, a, _ in plan)
    full = [*plan, (v_end / (0.5 * G0), -0.5 * G0, 0.0), (6.0, 0.0, 0.0)]
    edges = np.cumsum([0.0] + [d for d, _, _ in full])
    v_start = np.concatenate(([0.0], np.cumsum([d * a for d, a, _ in full])))

    def truth(t_ns: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        ts = (np.asarray(t_ns) - T0) / 1e9
        i = np.clip(np.searchsorted(edges, ts, side="right") - 1, 0, len(full) - 1)
        a = np.array([full[j][1] for j in i])
        r = np.array([full[j][2] for j in i])
        return np.clip(v_start[i] + a * (ts - edges[i]), 0.0, None), a, r

    ti = T0 + np.round(np.arange(int(edges[-1] * IMU_HZ)) * 1e9 / IMU_HZ + 1_111_111)
    ti = ti.astype(np.int64)
    v, a, r = truth(ti)
    n = ti.size
    f = np.column_stack(
        (
            a + ACCEL_BIAS + rng.normal(0, ACCEL_NOISE, n),
            v * r + rng.normal(0, ACCEL_NOISE, n),
            G0 + rng.normal(0, ACCEL_NOISE, n),
        )
    )
    g = np.column_stack((np.zeros(n), np.zeros(n), r + GYRO_BIAS + rng.normal(0, 0.002, n)))
    if rot is not None:
        f, g = f @ rot, g @ rot  # sensor = R^T vehicle
    recs: list[Record] = [
        ImuSample(int(ti[k]), *map(float, f[k]), *map(float, g[k])) for k in range(n)
    ]
    tg = T0 + np.round(np.arange(int(edges[-1] * 10)) * 1e8 + 37_000_000).astype(np.int64)
    vg, _, _ = truth(tg)
    vg = np.abs(vg + rng.normal(0, GPS_NOISE, vg.size))
    for k in range(tg.size if gps else 0):
        s = (tg[k] - T0) / 1e9
        if outage and outage[0] <= s < outage[1]:
            continue
        recs.append(
            GpsFix(int(tg[k]), int(tg[k]), None, 0.0, 0.0, 0.0, float(vg[k]), 0.0, 1.0, 3, 10)
        )
    recs.sort(key=record_time)
    return recs, truth


def estimate(cfg: Config, recs: list[Record], rot: np.ndarray | None = None) -> list[StateEstimate]:
    est = StateEstimator(cfg, rot)
    return [s for r in recs for s in est.push(r)]


def arrays(states: list[StateEstimate]) -> dict[str, np.ndarray]:
    return {
        "t": np.array([s.t_ns for s in states], dtype=np.int64),
        "v": np.array([s.speed_mps for s in states]),
        "var": np.array([s.var_speed for s in states]),
        "dr": np.array([s.dr for s in states]),
        "lat": np.array([s.accel_lat_mps2 for s in states]),
        "yaw": np.array([s.yaw_rate_rps for s in states]),
    }


def rms_bound() -> float:
    tune = EstimatorTuning()
    q = tune.accel_sigma_mps2**2 / IMU_HZ * 0.1
    r = tune.gps_speed_sigma_mps**2
    p_plus = (-q + math.sqrt(q * q + 4 * q * r)) / 2
    return math.sqrt(p_plus + q)


def test_speed_rms_error_under_derived_bound(cfg: Config) -> None:
    bound = rms_bound()
    assert abs(bound - 0.0301) < 5e-5
    recs, truth = synth(COMMUTE, seed=20260923)
    a = arrays(estimate(cfg, recs))
    v_true, _, _ = truth(a["t"])
    rms = math.sqrt(float(np.mean((a["v"] - v_true) ** 2)))
    assert rms < bound
    assert rms < GPS_NOISE
    # The first state comes at the first IMU sample after the first speed measurement.
    assert (a["t"][0] - T0) / 1e9 < 0.2
    assert np.all(np.diff(a["t"]) > 0)


def test_bias_and_yaw_bias_are_learned(cfg: Config) -> None:
    recs, truth = synth(COMMUTE, seed=5)
    est = StateEstimator(cfg)
    states = [s for r in recs for s in est.push(r)]
    assert abs(est.bias - ACCEL_BIAS) < 0.01
    a = arrays(states)
    v_true, _, r_true = truth(a["t"])
    turn = r_true > 0
    # Without the standstill gyro bias the lateral error would be v * 0.01 = 0.23 m/s^2.
    assert abs(float(np.mean(a["lat"][turn] - (v_true * r_true)[turn]))) < 0.02
    assert abs(float(np.mean(a["yaw"][turn] - r_true[turn]))) < 0.001


def test_zero_velocity_updates_hold_speed_at_zero(cfg: Config) -> None:
    recs, _truth = synth(COMMUTE, seed=9)
    states = estimate(cfg, recs)
    stationary_s = float(cfg.get("estimator.stationary_s"))
    rest = [s for s in states if stationary_s + 0.2 < (s.t_ns - T0) / 1e9 < 5.0]
    assert len(rest) > 250
    assert all(s.speed_mps == 0.0 for s in rest)
    assert all("zupt" in s.inputs for s in rest)
    moving = [s for s in states if 6.0 < (s.t_ns - T0) / 1e9 < 12.0]
    assert all("zupt" not in s.inputs and s.speed_mps > 0 for s in moving)


def test_gps_outage_sets_dr_and_metrics_see_it(cfg: Config) -> None:
    recs, _truth = synth(LAUNCH, seed=1, outage=(7.0, 9.5))
    a = arrays(estimate(cfg, recs))
    ts = (a["t"] - T0) / 1e9
    stale_s = 3 / float(cfg.get("gps.rate_hz"))
    last_fix, next_fix = 6.937, 9.537  # GPS fixes on either side of the outage
    in_outage = (ts > last_fix + stale_s + 0.01) & (ts < next_fix)
    clear = (ts > 5.0) & ((ts < last_fix + stale_s - 0.01) | (ts > next_fix + 0.01))
    assert bool(np.all(a["dr"][in_outage]))
    assert not bool(np.any(a["dr"][clear]))
    assert float(np.max(np.diff(ts))) < 0.011  # still emitting through the outage
    events = zero_to_sixty(a["t"], a["v"], cfg, var_speed=a["var"], dr=a["dr"])
    assert len(events) == 1
    e = events[0]
    assert abs(e.value / (MPH_60 / (0.5 * G0)) - 1.0) < 0.01
    (gps,) = [c for c in e.citations if c.signal == "gps.speed"]
    dr_s = next_fix - (last_fix + stale_s)
    assert gps.role == "context" and gps.stat == "missing_s"
    assert abs(gps.stat_value - dr_s) < 2.0 / IMU_HZ


def test_dead_reckoning_stops_after_the_limit(cfg: Config) -> None:
    recs, _truth = synth(COMMUTE, seed=3, outage=(14.0, 22.0))
    a = arrays(estimate(cfg, recs))
    ts = (a["t"] - T0) / 1e9
    limit = float(cfg.get("estimator.max_dead_reckon_s"))
    last_fix, next_fix = 13.937, 22.037
    assert not bool(np.any((ts > last_fix + limit + 0.01) & (ts < next_fix)))
    assert bool(np.any((ts > last_fix + limit - 0.05) & (ts <= last_fix + limit)))
    assert bool(np.any((ts > next_fix) & (ts < next_fix + 0.02)))


def test_mount_rotation_is_applied(cfg: Config) -> None:
    c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
    rot = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    plain, _ = synth(COMMUTE, seed=4)
    rotated, _ = synth(COMMUTE, seed=4, rot=rot)
    a = arrays(estimate(cfg, plain))
    b = arrays(estimate(cfg, rotated, rot))
    assert np.array_equal(a["t"], b["t"])
    assert float(np.max(np.abs(a["v"] - b["v"]))) < 1e-9
    assert float(np.max(np.abs(a["lat"] - b["lat"]))) < 1e-9


def test_obd_speed_only(cfg: Config) -> None:
    recs, truth = synth(COMMUTE, seed=2, gps=False)
    t_obd = T0 + np.arange(0, 60 * 2) * 500_000_000 + 40_000_000  # 2 Hz past the drive end
    v_obd, _, _ = truth(t_obd)
    step = 1.0 / 3.6
    obd: list[Record] = [
        Sample(int(t), "obd.speed", round(float(v) / step) * step)
        for t, v in zip(t_obd, v_obd, strict=True)
    ]
    merged = sorted(recs + obd, key=record_time)
    states = estimate(cfg, merged)
    a = arrays(states)
    v_true, _, _ = truth(a["t"])
    rms = math.sqrt(float(np.mean((a["v"] - v_true) ** 2)))
    assert rms < step / 2  # under the worst-case error of a single 1 km/h reading
    t_imu_end = max(r.t_ns for r in recs if isinstance(r, ImuSample))
    with_imu = [s for s in states if s.t_ns <= t_imu_end]
    after = [s for s in states if s.t_ns > t_imu_end]
    assert {s.inputs for s in with_imu} == {"imu+obd", "imu+obd+zupt"}
    # The OBD polls past the IMU's end: speed-only estimates, 1 per poll, the car at rest.
    assert len(after) == sum(1 for r in obd if record_time(r) > t_imu_end)
    assert {s.inputs for s in after} == {"obd+zupt"}
    assert all(s.speed_mps == 0.0 and math.isnan(s.accel_lat_mps2) for s in after)
    assert all(s.dr for s in states)


def test_no_state_before_a_speed_measurement(cfg: Config) -> None:
    est = StateEstimator(cfg)
    assert est.push(ImuSample(T0, 0.0, 0.0, G0, 0.0, 0.0, 0.0)) == []
    assert est.push(Sample(T0 + 1, "obd.speed", 0.0)) == []
    (s,) = est.push(ImuSample(T0 + 10_000_000, 0.0, 0.0, G0, 0.0, 0.0, 0.0))
    assert s.speed_mps == 0.0 and s.dr and s.inputs == "imu+obd"


def test_rejects_a_non_rotation(cfg: Config) -> None:
    with pytest.raises(ValueError):
        StateEstimator(cfg, np.diag([1.0, 1.0, 2.0]))


# -- speed-only mode (no IMU: a live OBD session) ---------------------------------------------

OBD_STEP = 1.0 / 3.6
OBD_TIMEOUT_S = 0.1


def obd_speed(truth: Truth, end_s: float, seed: int) -> list[Record]:
    """OBD speed at 2 Hz in 1 km/h steps, each value read up to the poll timeout before its
    response time (the latency the filter models)."""
    rng = np.random.default_rng(seed)
    t = T0 + np.arange(int(end_s * 2)) * 500_000_000 + 40_000_000
    read = t - np.round(rng.uniform(0.0, OBD_TIMEOUT_S, t.size) * 1e9).astype(np.int64)
    v, _, _ = truth(read)
    return [
        Sample(int(ti), "obd.speed", round(float(vi) / OBD_STEP) * OBD_STEP)
        for ti, vi in zip(t, v, strict=True)
    ]


def end_s(recs: list[Record]) -> float:
    return (max(record_time(r) for r in recs) - T0) / 1e9


def window(a: dict[str, np.ndarray], lo_s: float, hi_s: float) -> np.ndarray:
    ts = (a["t"] - T0) / 1e9
    mask: np.ndarray = (ts > lo_s) & (ts < hi_s)
    return mask


def test_speed_only_limit_is_the_stale_rule_on_the_imu_rate(cfg: Config) -> None:
    polls = int(cfg.get("rules.shared.stale_after_polls"))
    assert StateEstimator(cfg).imu_stale_s == pytest.approx(polls / IMU_HZ, abs=1e-9)


def test_speed_only_mode_from_obd_alone(cfg: Config) -> None:
    """OBD alone, the car-agnostic baseline: 1 estimate per speed poll, accel derived from the
    filtered speed, and no lateral values."""
    recs, truth = synth(COMMUTE, seed=11, gps=False)
    obd = obd_speed(truth, end_s(recs), seed=11)
    states = estimate(cfg, obd)
    # 1 per poll once the IMU is overdue: every poll after the first.
    assert [s.t_ns for s in states] == [record_time(r) for r in obd[1:]]
    assert {s.inputs for s in states} == {"obd", "obd+zupt"}
    assert all(s.dr for s in states)  # no GPS
    assert all(math.isnan(s.accel_lat_mps2) and math.isnan(s.accel_lat_imu_mps2) for s in states)
    assert all(math.isnan(s.yaw_rate_rps) for s in states)
    a = arrays(states)
    v_true, _, _ = truth(a["t"])
    rms = math.sqrt(float(np.mean((a["v"] - v_true) ** 2)))
    assert rms < OBD_STEP  # 1 km/h; seeds 0 to 19 measured at most 0.148 m/s
    # A single 1 km/h step 0.5 s apart differentiates to 0.057 g, so single estimates are
    # that noisy; means over a phase are close (seeds 0 to 19: at most 0.008 g off).
    acc = np.array([s.accel_long_mps2 for s in states])
    launch = window(a, 7.0, 12.9)  # 0.3 g from 5 s to 13 s; 2 s to settle
    assert float(np.mean(acc[launch])) == pytest.approx(0.3 * G0, abs=0.03 * G0)
    cruise = window(a, 15.0, 22.9)  # 0 g from 13 s to 23 s
    assert abs(float(np.mean(acc[cruise]))) < 0.03 * G0
    stationary_s = float(cfg.get("estimator.stationary_s"))
    rest = [s for s in states if stationary_s + 0.6 < (s.t_ns - T0) / 1e9 < 5.0]
    assert rest and all(
        (s.speed_mps, s.accel_long_mps2, s.inputs) == (0.0, 0.0, "obd+zupt") for s in rest
    )


def test_speed_only_mode_from_gps_alone(cfg: Config) -> None:
    recs, truth = synth(LAUNCH, seed=12)
    gps: list[Record] = [r for r in recs if isinstance(r, GpsFix)]
    states = estimate(cfg, gps)
    assert [s.t_ns for s in states] == [record_time(r) for r in gps[1:]]
    assert {s.inputs for s in states} == {"gps", "gps+zupt"}
    assert not any(s.dr for s in states)
    a = arrays(states)
    v_true, _, _ = truth(a["t"])
    rms = math.sqrt(float(np.mean((a["v"] - v_true) ** 2)))
    assert rms < GPS_NOISE  # no worse than raw GPS; seeds 0 to 19 measured at most 0.047
    (z60,) = zero_to_sixty(a["t"], a["v"], cfg, var_speed=a["var"], dr=a["dr"])
    # 0 to 60 from speed alone at 10 Hz: seeds 0 to 19 were at most 0.039 s off.
    assert z60.value == pytest.approx(MPH_60 / (0.5 * G0), abs=0.05)


def test_imu_dropout_switches_to_speed_only_and_back(cfg: Config) -> None:
    recs, truth = synth(COMMUTE, seed=13)
    cut = (15.0, 20.0)  # an IMU outage during the cruise
    kept = [
        r for r in recs if not (isinstance(r, ImuSample) and cut[0] <= (r.t_ns - T0) / 1e9 < cut[1])
    ]
    states = estimate(cfg, kept)
    a = arrays(states)
    outage = window(a, cut[0] + 0.03, cut[1])
    around = ~window(a, cut[0], cut[1] + 0.01)
    inside = [s for s, m in zip(states, outage, strict=True) if m]
    assert inside and {s.inputs for s in inside} == {"gps"}
    assert all(math.isnan(s.accel_lat_mps2) for s in inside)
    # GPS cadence inside the outage, IMU cadence around it.
    assert float(np.max(np.diff(a["t"][outage]))) == pytest.approx(1e8, abs=1e6)
    assert all("imu" in s.inputs for s, m in zip(states, around, strict=True) if m)
    v_true, _, _ = truth(a["t"])
    err = np.abs(a["v"] - v_true)
    assert float(np.max(err[window(a, 13.5, 23.0)])) < 0.15
    assert math.sqrt(float(np.mean(err**2))) < GPS_NOISE


def test_an_imu_sample_at_the_same_time_as_the_first_fix_is_not_missed(cfg: Config) -> None:
    """The IMU gets the stale limit from the first record to show up: a GPS fix merged just
    ahead of an IMU sample with the same time yields no speed-only estimate."""
    est = StateEstimator(cfg)
    assert est.push(GpsFix(T0, T0, None, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 3, 10)) == []
    (s,) = est.push(ImuSample(T0, 0.0, 0.0, G0, 0.0, 0.0, 0.0))
    assert s.inputs == "imu+gps"
    late = T0 + round(est.imu_stale_s * 1e9) + 1
    (only,) = est.push(GpsFix(late, late, None, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 3, 10))
    assert only.inputs == "gps" and math.isnan(only.accel_lat_mps2)
