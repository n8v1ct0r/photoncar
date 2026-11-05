"""Known-answer tests for 0 to 60, 60 to 0, and peak lateral g (plan section 7).

Noise-free cases assert within 1e-6 of the approved answers; noisy variants within 1%.
All data is synthetic: 104 Hz samples with a phase offset against large monotonic times.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from photoncar.config import Config, load_config
from photoncar.detect.performance import peak_lateral, sixty_to_zero, zero_to_sixty
from photoncar.records import Event
from photoncar.units import FOOT, G0, MPH_60

T0 = 1_234_567_890_123  # large monotonic start time, ns
PHASE_NS = 3_210_987  # sample phase offset against the motion start
RATE_HZ = 104.0


@pytest.fixture(scope="module")
def cfg() -> Config:
    return load_config()


def times(duration_s: float) -> tuple[np.ndarray, np.ndarray]:
    k = np.arange(int(duration_s * RATE_HZ))
    t = T0 + np.round(k * (1e9 / RATE_HZ) + PHASE_NS).astype(np.int64)
    return t, (t - T0) / 1e9


def launch(accel_g: float, rest_s: float, duration_s: float = 20.0) -> tuple[np.ndarray, ...]:
    t, ts = times(duration_s)
    tau = ts[0] + rest_s + 0.000371  # rest measured from the first sample; start between samples
    v = accel_g * G0 * np.clip(ts - tau, 0.0, None)
    return t, v


def stop(decel_g: float, v0: float = 30.0, brake_at: float = 3.0017) -> tuple[np.ndarray, ...]:
    t, ts = times(brake_at + v0 / (decel_g * G0) + 3.0)
    v = np.clip(v0 - decel_g * G0 * np.clip(ts - brake_at, 0.0, None), 0.0, None)
    return t, v


def assert_citations_unique(e: Event) -> None:
    keys = [(c.signal, c.role) for c in e.citations]
    assert len(keys) == len(set(keys)), "store key (event, signal, role) must be unique"


class TestZeroToSixty:
    def test_known_answer_half_g(self, cfg: Config) -> None:
        t, v = launch(0.5, rest_s=2.0)
        events = zero_to_sixty(t, v, cfg)
        assert len(events) == 1
        e = events[0]
        expected = MPH_60 / (0.5 * G0)
        assert abs(e.value - 5.470247230) < 1e-6
        assert abs(e.value - expected) < 1e-9
        rollout = {c.stat: c for c in e.citations}["rollout_1ft_s"]
        assert abs(rollout.stat_value - 5.117651427) < 1e-6
        assert abs(rollout.stat_value - (expected - math.sqrt(2 * FOOT / (0.5 * G0)))) < 1e-9
        assert e.kind == "perf.zero_to_sixty"
        assert e.unit == "s"
        assert e.severity == 0

    def test_trigger_citation_window(self, cfg: Config) -> None:
        t, v = launch(0.5, rest_s=2.0)
        e = zero_to_sixty(t, v, cfg)[0]
        trig = [c for c in e.citations if c.role == "trigger"]
        assert len(trig) == 1
        c = trig[0]
        assert c.signal == "state.speed"
        assert c.source == "Estimator"
        assert c.stat == "from_standstill_s"
        assert abs(c.stat_value - e.value) < 1e-12
        tau_ns = T0 + PHASE_NS + round((2.0 + 0.000371) * 1e9)
        assert abs(c.t_start_ns - tau_ns) <= 1
        assert abs((c.t_end_ns - c.t_start_ns) / 1e9 - e.value) < 2e-9
        assert c.n == int(np.count_nonzero((t >= c.t_start_ns) & (t <= c.t_end_ns)))
        assert_citations_unique(e)

    def test_needs_one_second_at_rest(self, cfg: Config) -> None:
        t, v = launch(0.5, rest_s=0.9)
        assert zero_to_sixty(t, v, cfg) == []
        t, v = launch(0.5, rest_s=1.0)
        assert len(zero_to_sixty(t, v, cfg)) == 1

    def test_run_that_never_reaches_sixty(self, cfg: Config) -> None:
        t, ts = times(20.0)
        v = np.clip(0.3 * G0 * np.clip(ts - 2.0, 0.0, None), 0.0, 20.0)
        assert zero_to_sixty(t, v, cfg) == []

    def test_noisy_within_one_percent(self, cfg: Config) -> None:
        rng = np.random.default_rng(20260923)
        t, v = launch(0.5, rest_s=2.0)
        noisy = v + rng.normal(0.0, 0.05, v.size)
        events = zero_to_sixty(t, noisy, cfg)
        assert len(events) == 1
        e = events[0]
        assert abs(e.value / 5.470247230 - 1.0) < 0.01
        rollout = {c.stat: c.stat_value for c in e.citations}["rollout_1ft_s"]
        assert abs(rollout / 5.117651427 - 1.0) < 0.01

    def test_high_variance_suppresses_run(self, cfg: Config) -> None:
        t, v = launch(0.5, rest_s=2.0)
        var = np.full(v.size, 0.01)
        assert len(zero_to_sixty(t, v, cfg, var_speed=var)) == 1
        limit = float(cfg.get("estimator.max_speed_var"))
        var[v.size // 3] = limit * 1.01
        assert zero_to_sixty(t, v, cfg, var_speed=var) == []

    def test_dead_reckoned_seconds_are_cited(self, cfg: Config) -> None:
        t, v = launch(0.5, rest_s=2.0)
        ts = (t - T0) / 1e9
        dr = (ts >= 4.0) & (ts < 5.5)
        e = zero_to_sixty(t, v, cfg, dr=dr)[0]
        gps = [c for c in e.citations if c.signal == "gps.speed"]
        assert len(gps) == 1
        assert gps[0].role == "context"
        assert gps[0].stat == "missing_s"
        assert abs(gps[0].stat_value - 1.5) < 2.0 / RATE_HZ
        assert_citations_unique(e)

    def test_gap_splits_the_run(self, cfg: Config) -> None:
        t, v = launch(0.5, rest_s=2.0)
        ts = (t - T0) / 1e9
        keep = (ts < 4.0) | (ts > 4.5)
        assert zero_to_sixty(t[keep], v[keep], cfg) == []


class TestSixtyToZero:
    @pytest.mark.parametrize(("g", "expected"), [(1.0, 36.68128983), (0.8, 45.85161228)])
    def test_known_answer(self, cfg: Config, g: float, expected: float) -> None:
        t, v = stop(g)
        events = sixty_to_zero(t, v, cfg)
        assert len(events) == 1
        e = events[0]
        assert abs(e.value - expected) < 1e-6
        assert abs(e.value - MPH_60**2 / (2 * g * G0)) < 1e-6
        trig = [c for c in e.citations if c.role == "trigger"]
        assert len(trig) == 1
        assert trig[0].signal == "state.speed"
        assert trig[0].stat == "distance_m"
        assert abs(trig[0].stat_value - e.value) < 1e-12
        crossing_s = 3.0017 + (30.0 - MPH_60) / (g * G0)
        stop_s = 3.0017 + 30.0 / (g * G0)
        assert abs((trig[0].t_start_ns - T0) / 1e9 - crossing_s) < 2e-9
        assert abs((trig[0].t_end_ns - T0) / 1e9 - stop_s) < 2e-9
        assert e.unit == "m"
        assert_citations_unique(e)

    @pytest.mark.parametrize(("g", "expected"), [(1.0, 36.68128983), (0.8, 45.85161228)])
    def test_noisy_within_one_percent(self, cfg: Config, g: float, expected: float) -> None:
        rng = np.random.default_rng(7)
        t, v = stop(g)
        events = sixty_to_zero(t, v + rng.normal(0.0, 0.05, v.size), cfg)
        assert len(events) == 1
        assert abs(events[0].value / expected - 1.0) < 0.01

    def test_gentle_stop_is_not_a_braking_run(self, cfg: Config) -> None:
        t, v = stop(0.29)
        assert sixty_to_zero(t, v, cfg) == []
        t, v = stop(0.3)
        assert len(sixty_to_zero(t, v, cfg)) == 1


class TestPeakLateral:
    @staticmethod
    def circle(v: float = 20.0, radius: float = 50.0) -> tuple[np.ndarray, ...]:
        t, ts = times(16.0)
        a = np.where((ts > 3.0) & (ts < 13.0), v * v / radius, 0.0)
        return t, ts, a

    def test_circle_known_answer(self, cfg: Config) -> None:
        t, _ts, a = self.circle()
        e = peak_lateral(t, a, cfg)
        assert e is not None
        assert abs(e.value - 20.0**2 / (50.0 * G0)) < 1e-6
        assert e.unit == "g"
        trig = [c for c in e.citations if c.role == "trigger"]
        assert len(trig) == 1
        assert trig[0].signal == "state.accel_lat"
        assert abs(trig[0].stat_value - 8.0) < 1e-9
        assert abs((trig[0].t_end_ns - trig[0].t_start_ns) / 1e9 - 0.5) < 1e-9
        assert_citations_unique(e)

    @pytest.mark.parametrize("where_s", [8.0, 1.5])
    def test_single_spike_leaves_peak_unchanged(self, cfg: Config, where_s: float) -> None:
        t, ts, a = self.circle()
        clean = peak_lateral(t, a, cfg)
        spiked = a.copy()
        spiked[int(np.searchsorted(ts, where_s))] = 2.0 * G0
        e = peak_lateral(t, spiked, cfg)
        assert clean is not None and e is not None
        assert e.value == clean.value

    def test_right_turn_uses_absolute_value(self, cfg: Config) -> None:
        t, _ts, a = self.circle()
        e = peak_lateral(t, -a, cfg)
        assert e is not None
        assert abs(e.value - 20.0**2 / (50.0 * G0)) < 1e-6

    def test_noisy_within_one_percent(self, cfg: Config) -> None:
        rng = np.random.default_rng(11)
        t, _ts, a = self.circle()
        e = peak_lateral(t, a + rng.normal(0.0, 0.01 * G0, a.size), cfg)
        assert e is not None
        assert abs(e.value / (20.0**2 / (50.0 * G0)) - 1.0) < 0.01

    def test_sustained_peak_citation(self, cfg: Config) -> None:
        t, _ts, a = self.circle()
        e = peak_lateral(t, a, cfg)
        assert e is not None
        held = [c for c in e.citations if c.role == "context"]
        assert len(held) == 1
        assert held[0].stat == "sustained_abs_mean"
        assert abs(held[0].stat_value - 8.0) < 1e-9
        assert abs((held[0].t_end_ns - held[0].t_start_ns) / 1e9 - 0.5) < 1e-9

    def test_short_burst_raises_peak_more_than_held_level(self, cfg: Config) -> None:
        t, ts, a = self.circle()
        a = a.copy()
        a[(ts > 6.0) & (ts < 6.2)] = 10.0  # a 0.2 s burst, shorter than the 0.5 s window
        e = peak_lateral(t, a, cfg)
        assert e is not None
        held = {c.stat: c.stat_value for c in e.citations}["sustained_abs_mean"]
        peak = e.value * G0
        # The burst lifts the 0.5 s mean by 2 m/s^2 times its overlap: about 0.8 at the peak,
        # and at most about 0.4 across any full 0.5 s span.
        assert 8.7 < peak < 8.85
        assert 8.3 < held < 8.5

    def test_empty_input(self, cfg: Config) -> None:
        assert peak_lateral(np.zeros(0, dtype=np.int64), np.zeros(0), cfg) is None
