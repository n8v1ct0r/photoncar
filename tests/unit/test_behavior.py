"""Driver behavior known answers: thresholds on the 1.0 s mean, jerk, RPM bands, and shifts."""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pytest

from photoncar.config import Config, load_config, load_vehicle
from photoncar.detect.behavior import NO_GEARS_NOTE, BehaviorReport, analyze
from photoncar.units import G0

T0 = 555_000_000_000
RATE_HZ = 104.0


@pytest.fixture(scope="module")
def cfg() -> Config:
    return load_config()


@pytest.fixture(scope="module")
def vehicle() -> Config:
    return load_vehicle("e90_m3")


def times(duration_s: float) -> tuple[np.ndarray, np.ndarray]:
    k = np.arange(int(duration_s * RATE_HZ))
    t = T0 + np.round(k * (1e9 / RATE_HZ) + 1_234_567).astype(np.int64)
    return t, (t - T0) / 1e9


def longitudinal(
    accel_g: float, pulse_s: float, v0: float = 20.0, total_s: float = 9.0, at_s: float = 3.0
) -> tuple[np.ndarray, ...]:
    t, ts = times(total_s)
    a = np.where((ts >= at_s) & (ts < at_s + pulse_s), accel_g * G0, 0.0)
    v = v0 + np.concatenate(([0.0], np.cumsum(a[:-1] * np.diff(ts))))
    return t, ts, v, a


def run(
    cfg: Config,
    vehicle: Config,
    t: np.ndarray,
    v: np.ndarray,
    ax: np.ndarray,
    ay: np.ndarray | None = None,
    rpm: np.ndarray | None = None,
) -> BehaviorReport:
    lat = np.zeros(t.size) if ay is None else ay
    return analyze(t, v, ax, lat, rpm, cfg, vehicle)


def kinds(r: BehaviorReport, kind: str) -> list[str]:
    return [e.rule_id for e in r.events if e.kind == kind]


class TestThresholds:
    def test_hard_brake_at_030_g(self, cfg: Config, vehicle: Config) -> None:
        t, _ts, v, a = longitudinal(-0.30, 3.0)
        r = run(cfg, vehicle, t, v, a)
        brakes = [e for e in r.events if e.kind == "behavior.hard_brake"]
        assert len(brakes) == 1
        e = brakes[0]
        assert e.rule_id == "hard_brake"
        assert abs(e.value - 0.30) < 1e-9
        assert e.threshold == 0.30
        c = e.citations[0]
        assert c.role == "trigger"
        assert c.signal == "state.accel_long"
        assert c.stat == "min_window_mean"
        assert abs(c.stat_value + 0.30 * G0) < 1e-9
        # The first full 1.0 s mean inside the braking closes 1.0 s after braking starts.
        assert abs((e.t_start_ns - T0) / 1e9 - 3.0) < 2.0 / RATE_HZ
        assert abs((e.t_end_ns - T0) / 1e9 - 6.0) < 2.0 / RATE_HZ

    def test_just_under_hard_brake(self, cfg: Config, vehicle: Config) -> None:
        t, _ts, v, a = longitudinal(-0.2999, 3.0)
        assert kinds(run(cfg, vehicle, t, v, a), "behavior.hard_brake") == []

    def test_severe_brake_at_045_g(self, cfg: Config, vehicle: Config) -> None:
        t, _ts, v, a = longitudinal(-0.45, 2.0, v0=25.0)
        r = run(cfg, vehicle, t, v, a)
        assert kinds(r, "behavior.hard_brake") == ["severe_brake"]
        e = r.events[0]
        assert abs(e.value - 0.45) < 1e-9
        assert e.threshold == 0.45
        t, _ts, v, a = longitudinal(-0.4499, 2.0, v0=25.0)
        assert kinds(run(cfg, vehicle, t, v, a), "behavior.hard_brake") == ["hard_brake"]

    def test_short_spike_is_averaged_out(self, cfg: Config, vehicle: Config) -> None:
        # 0.5 g for 0.5 s gives a 1.0 s mean of 0.25 g: not a hard brake.
        t, _ts, v, a = longitudinal(-0.5, 0.5)
        assert kinds(run(cfg, vehicle, t, v, a), "behavior.hard_brake") == []

    def test_nothing_below_two_mps(self, cfg: Config, vehicle: Config) -> None:
        t, _ts, v, a = longitudinal(-0.5, 0.35, v0=1.9)
        assert run(cfg, vehicle, t, np.clip(v, 0, None), a).events == []

    def test_hard_accel_at_035_g(self, cfg: Config, vehicle: Config) -> None:
        t, _ts, v, a = longitudinal(0.35, 3.0)
        r = run(cfg, vehicle, t, v, a)
        assert kinds(r, "behavior.hard_accel") == ["hard_accel"]
        assert abs(r.events[0].value - 0.35) < 1e-9
        assert r.events[0].citations[0].stat == "max_window_mean"
        t, _ts, v, a = longitudinal(0.3499, 3.0)
        assert kinds(run(cfg, vehicle, t, v, a), "behavior.hard_accel") == []

    @pytest.mark.parametrize(
        ("g", "expected"),
        [(0.3999, []), (0.40, ["corner"]), (0.4999, ["corner"]), (0.50, ["corner_high"])],
    )
    def test_corner_tiers(
        self, cfg: Config, vehicle: Config, g: float, expected: list[str]
    ) -> None:
        t, ts = times(9.0)
        lat = np.where((ts >= 3.0) & (ts < 6.0), g * G0, 0.0)
        r = run(cfg, vehicle, t, np.full(t.size, 15.0), np.zeros(t.size), lat)
        assert kinds(r, "behavior.corner") == expected

    def test_right_hand_corner(self, cfg: Config, vehicle: Config) -> None:
        t, ts = times(9.0)
        lat = np.where((ts >= 3.0) & (ts < 6.0), -0.45 * G0, 0.0)
        r = run(cfg, vehicle, t, np.full(t.size, 15.0), np.zeros(t.size), lat)
        assert kinds(r, "behavior.corner") == ["corner"]
        c = r.events[0].citations[0]
        assert c.signal == "state.accel_lat"
        assert c.stat == "max_abs_window_mean"
        assert abs(c.stat_value + 0.45 * G0) < 1e-9
        assert abs(r.events[0].value - 0.45) < 1e-9


class TestJerk:
    def test_linear_ramps_give_constant_jerk(self, cfg: Config, vehicle: Config) -> None:
        t, ts = times(10.0)
        ax = 0.5 * (ts - 5.0)
        ay = -0.2 * (ts - 5.0)
        r = run(cfg, vehicle, t, np.full(t.size, 20.0), ax, ay)
        assert r.jerk_long_mean is not None and r.jerk_long_p95 is not None
        assert r.jerk_lat_mean is not None and r.jerk_lat_p95 is not None
        assert abs(r.jerk_long_mean - 0.5) < 1e-6
        assert abs(r.jerk_long_p95 - 0.5) < 1e-6
        assert abs(r.jerk_lat_mean - 0.2) < 1e-6
        assert abs(r.jerk_lat_p95 - 0.2) < 1e-6

    def test_no_moving_samples_gives_none(self, cfg: Config, vehicle: Config) -> None:
        t, _ts = times(5.0)
        r = run(cfg, vehicle, t, np.zeros(t.size), np.zeros(t.size))
        assert r.jerk_long_mean is None and r.jerk_lat_p95 is None


class TestRpmBands:
    def test_seconds_sum_to_duration(self, cfg: Config, vehicle: Config) -> None:
        rng = np.random.default_rng(3)
        t, _ts = times(30.0)
        rpm = rng.uniform(-100.0, 9000.0, t.size)  # includes values outside the band edges
        r = run(cfg, vehicle, t, np.full(t.size, 10.0), np.zeros(t.size), rpm=rpm)
        edges = vehicle.get("rpm.bands")
        assert [(lo, hi) for lo, hi, _ in r.rpm_band_s] == list(pairwise(edges))
        total = sum(s for _, _, s in r.rpm_band_s)
        assert abs(total - (int(t[-1]) - int(t[0])) / 1e9) < 1e-9

    def test_constant_rpm_lands_in_one_band(self, cfg: Config, vehicle: Config) -> None:
        t, _ts = times(10.0)
        r = run(cfg, vehicle, t, np.full(t.size, 10.0), np.zeros(t.size), rpm=np.full(t.size, 3500))
        by_band = {(lo, hi): s for lo, hi, s in r.rpm_band_s}
        assert abs(by_band[(3000, 4000)] - (int(t[-1]) - int(t[0])) / 1e9) < 1e-9
        assert sum(s for s in by_band.values()) == by_band[(3000, 4000)]

    def test_no_rpm_no_bands(self, cfg: Config, vehicle: Config) -> None:
        t, _ts = times(3.0)
        r = run(cfg, vehicle, t, np.full(t.size, 10.0), np.zeros(t.size))
        assert r.rpm_band_s == [] and r.shifts == [] and r.gear_note is None


K = (0.0025, 0.004, 0.0055)  # v / rpm per gear, (m/s) per rpm
CLUTCH_S = 0.3


def gearbox() -> tuple[np.ndarray, np.ndarray, np.ndarray, list[float]]:
    """1 s idle, then gear 1 1500 to 7000 rpm, shift, gear 2 to 7000 rpm, shift, gear 3."""
    t, ts = times(16.0)
    v = np.zeros(t.size)
    rpm = np.full(t.size, 800.0)
    plan = [
        (1.0, 5.0, K[0], 1500.0, 7000.0),
        (6.0 + CLUTCH_S, 4.0, K[1], 17.5 / K[1], 7000.0),
        (10.3 + CLUTCH_S, 4.0, K[2], 28.0 / K[2], 6000.0),
    ]
    clutch_in: list[float] = []
    for i, (start, dur, k, r0, r1) in enumerate(plan):
        m = (ts >= start) & (ts < start + dur)
        rpm[m] = r0 + (r1 - r0) * (ts[m] - start) / dur
        v[m] = k * rpm[m]
        end = start + dur
        if i + 1 < len(plan):
            clutch_in.append(end)
            nxt = plan[i + 1]
            c = (ts >= end) & (ts < nxt[0])
            v[c] = k * r1
            rpm[c] = r1 + (nxt[3] - r1) * (ts[c] - end) / CLUTCH_S
    tail = ts >= plan[-1][0] + plan[-1][1]
    v[tail] = K[2] * 6000.0
    rpm[tail] = 6000.0
    return t, v, rpm, clutch_in


class TestShifts:
    def test_gear_clusters_and_shift_points(self, cfg: Config, vehicle: Config) -> None:
        t, v, rpm, clutch_in = gearbox()
        r = run(cfg, vehicle, t, v, np.zeros(t.size), rpm=rpm)
        assert r.gear_note is None
        assert len(r.gear_ratios) == 3
        for got, want in zip(r.gear_ratios, K, strict=True):
            assert abs(got / want - 1.0) < 1e-9
        assert [(s.from_gear, s.to_gear) for s in r.shifts] == [(1, 2), (2, 3)]
        per_sample_rpm = 1100.0 / RATE_HZ  # gear 1 climbs 1100 rpm/s; gear 2 656 rpm/s
        for s, t_in in zip(r.shifts, clutch_in, strict=True):
            assert 7000.0 - per_sample_rpm - 1e-6 <= s.rpm <= 7000.0
            assert -1.0 / RATE_HZ <= (s.t_ns - T0) / 1e9 - t_in <= 0.035
        shift_events = [e for e in r.events if e.kind == "behavior.shift"]
        assert [e.message for e in shift_events] == [
            f"Shift 1 to 2 at {r.shifts[0].rpm:,.0f} rpm.",
            f"Shift 2 to 3 at {r.shifts[1].rpm:,.0f} rpm.",
        ]
        for e, s in zip(shift_events, r.shifts, strict=True):
            c = e.citations[0]
            assert c.role == "trigger" and c.signal == "obd.rpm" and c.stat == "max"
            assert c.stat_value == s.rpm
            assert c.t_end_ns == s.t_ns
            assert c.t_end_ns - c.t_start_ns == round(0.3 * 1e9)

    def test_continuously_variable_ratio_has_no_gears(self, cfg: Config, vehicle: Config) -> None:
        t, ts = times(20.0)
        v = 5.0 + 1.5 * ts  # speed sweeps 5 to 35 m/s at a steady 2500 rpm
        r = run(cfg, vehicle, t, v, np.zeros(t.size), rpm=np.full(t.size, 2500.0))
        assert r.gear_ratios == []
        assert r.shifts == []
        assert r.gear_note == NO_GEARS_NOTE
        assert r.gear_note == "No discrete gears detected (automatic, CVT, or too little data)."

    def test_steady_cruise_is_insufficient_data(self, cfg: Config, vehicle: Config) -> None:
        t, _ts = times(20.0)
        r = run(
            cfg, vehicle, t, np.full(t.size, 25.0), np.zeros(t.size), rpm=np.full(t.size, 3000.0)
        )
        assert r.gear_note == NO_GEARS_NOTE
        assert r.shifts == []


# -- gear bounds: the vehicle profile's max_gears, and unstable clusters ----------------------

STEP = 1.2  # v / rpm ratio step between neighbouring gears


def ladder(n_gears: int, *, cruise_s: float = 0.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """n gears, each swept 3000 to 6000 rpm in 2 s with a 0.3 s clutch between them. With
    ``cruise_s``, the drive then holds a continuously changing ratio (speed climbs at a steady
    2500 rpm) that belongs to no gear."""
    gear_s, clutch_s = 2.0, 0.3
    t, ts = times(n_gears * (gear_s + clutch_s) + cruise_s)
    v = np.zeros(t.size)
    rpm = np.full(t.size, 3000.0)
    for g in range(n_gears):
        k = 0.0025 * STEP**g
        start = g * (gear_s + clutch_s)
        m = (ts >= start) & (ts < start + gear_s)
        rpm[m] = 3000.0 + 1500.0 * (ts[m] - start)
        v[m] = k * rpm[m]
        c = (ts >= start + gear_s) & (ts < start + gear_s + clutch_s)
        v[c] = k * 6000.0
        rpm[c] = 6000.0 - (6000.0 - 6000.0 / STEP) * (ts[c] - start - gear_s) / clutch_s
    tail = ts >= n_gears * (gear_s + clutch_s)
    if tail.any():
        v_top = 0.0025 * STEP ** (n_gears - 1) * 6000.0
        rpm[tail] = 2500.0
        v[tail] = v_top * (1.0 + 0.02 * (ts[tail] - ts[tail][0]))
    return t, v, rpm


def profile(max_gears: int | None) -> Config:
    raw = {k: dict(v) for k, v in load_vehicle("e90_m3").raw.items()}
    if max_gears is None:
        raw["vehicle"].pop("max_gears", None)
    else:
        raw["vehicle"]["max_gears"] = max_gears
    return Config(raw)


class TestGearBounds:
    def test_every_profile_sets_max_gears(self) -> None:
        assert load_vehicle("e90_m3").get("vehicle.max_gears") == 7  # 6-speed manual, 7 M DCT
        assert load_vehicle("comma2k19_rav4").get("vehicle.max_gears") == 10  # gearbox unknown
        assert load_vehicle("comma2k19_civic").get("vehicle.max_gears") == 10  # gearbox unknown

    def test_up_to_max_gears_are_reported(self, cfg: Config, vehicle: Config) -> None:
        t, v, rpm = ladder(7)
        r = run(cfg, vehicle, t, v, np.zeros(t.size), rpm=rpm)
        assert r.gear_note is None
        assert len(r.gear_ratios) == 7
        assert [(s.from_gear, s.to_gear) for s in r.shifts] == [(g, g + 1) for g in range(1, 7)]
        assert len([e for e in r.events if e.kind == "behavior.shift"]) == 6

    def test_more_clusters_than_max_gears_report_no_shifts(
        self, cfg: Config, vehicle: Config
    ) -> None:
        t, v, rpm = ladder(8)
        r = run(cfg, vehicle, t, v, np.zeros(t.size), rpm=rpm)
        assert r.shifts == [] and r.gear_ratios == []
        assert [e for e in r.events if e.kind == "behavior.shift"] == []
        assert r.gear_note == NO_GEARS_NOTE
        # The same drive under a profile that allows 8 gears reports them.
        r8 = run(cfg, profile(8), t, v, np.zeros(t.size), rpm=rpm)
        assert len(r8.gear_ratios) == 8 and len(r8.shifts) == 7

    def test_a_profile_without_max_gears_allows_10(self, cfg: Config) -> None:
        t, v, rpm = ladder(10)
        assert len(run(cfg, profile(None), t, v, np.zeros(t.size), rpm=rpm).shifts) == 9
        t, v, rpm = ladder(11)
        r = run(cfg, profile(None), t, v, np.zeros(t.size), rpm=rpm)
        assert r.shifts == [] and r.gear_note == NO_GEARS_NOTE

    def test_unstable_clusters_report_no_shifts(self, cfg: Config, vehicle: Config) -> None:
        # 3 clean gears, then 3 times as long with a ratio no gear holds: most of the moving
        # time sits outside every cluster, as with a slipping torque converter.
        t, v, rpm = ladder(3, cruise_s=21.0)
        r = run(cfg, vehicle, t, v, np.zeros(t.size), rpm=rpm)
        assert r.shifts == [] and r.gear_ratios == []
        assert r.gear_note == NO_GEARS_NOTE
        # The gears alone are stable.
        t, v, rpm = ladder(3)
        assert len(run(cfg, vehicle, t, v, np.zeros(t.size), rpm=rpm).shifts) == 2
