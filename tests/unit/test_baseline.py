"""Baseline: window medians, MAD floor, arming, sustained |z| flags, alerted-window exclusion."""

from __future__ import annotations

import numpy as np
import pytest

from photoncar.config import Config, load_config
from photoncar.detect.baseline import (
    EXCLUDED,
    MAD_TO_SIGMA,
    BaselineStats,
    BaselineWindow,
    check,
    is_armed,
    operating_bins,
    to_windows,
    update,
    window_medians,
)
from photoncar.records import Citation, Event
from photoncar.units import c_to_k

T0 = 7_000_000_000_000
COOLANT = "obd.coolant_temp"


@pytest.fixture(scope="module")
def cfg() -> Config:
    return load_config()


def seconds_grid(n: int, step_s: float = 1.0) -> np.ndarray:
    return T0 + np.round(np.arange(n) * step_s * 1e9).astype(np.int64)


def windows(values: list[float], bin_label: str = "warm_idle") -> list[BaselineWindow]:
    return [
        BaselineWindow(COOLANT, bin_label, T0 + i * 10**10, T0 + (i + 1) * 10**10, v, 10)
        for i, v in enumerate(values)
    ]


def armed_coolant(cfg: Config, level_c: float = 90.0) -> list[BaselineStats]:
    stats: list[BaselineStats] = []
    for d in range(3):
        stats = update(stats, windows([c_to_k(level_c)] * 60), f"drive-{d}")
    (s,) = stats
    assert is_armed(s, cfg)
    return stats


def parse_baseline(stat: str) -> dict[str, str]:
    head, *pairs = stat.split(" ")
    assert head == "baseline"
    return dict(p.split("=", 1) for p in pairs)


class TestWindowMedians:
    def test_known_medians(self) -> None:
        t = seconds_grid(35)
        wm = window_medians(t, np.arange(35, dtype=float), 10.0)
        assert wm.median.tolist() == [4.5, 14.5, 24.5, 32.0]
        assert wm.n.tolist() == [10, 10, 10, 5]
        assert (wm.t_start_ns - T0).tolist() == [0, 10**10, 2 * 10**10, 3 * 10**10]
        assert (wm.t_end_ns - wm.t_start_ns).tolist() == [10**10] * 4
        assert wm.bins == ("all",) * 4

    def test_mixed_and_excluded_windows_are_dropped(self) -> None:
        t = seconds_grid(30)
        bins = ["a"] * 10 + ["a"] * 5 + ["b"] * 5 + [EXCLUDED] * 10
        wm = window_medians(t, np.ones(30), 10.0, bins)
        assert wm.bins == ("a",)
        assert (wm.t_start_ns - T0).tolist() == [0]

    def test_nan_values_are_ignored(self) -> None:
        t = seconds_grid(10)
        v = np.array([1.0, np.nan, 3.0, 5.0, np.nan, 7.0, 9.0, np.nan, 11.0, 13.0])
        wm = window_medians(t, v, 10.0)
        assert wm.median.tolist() == [7.0]
        assert wm.n.tolist() == [7]


class TestUpdate:
    def test_first_drive_is_exact(self) -> None:
        vals = [c_to_k(v) for v in (88.0, 90.0, 91.0, 95.0, 90.0)]
        (s,) = update([], windows(vals), "d1")
        assert s.median == pytest.approx(c_to_k(90.0), abs=1e-9)
        assert s.mad == pytest.approx(1.0, abs=1e-9)
        assert (s.n_windows, s.n_drives, s.version, s.updated_drive) == (5, 1, 1, "d1")

    def test_mad_floored_at_pid_resolution(self) -> None:
        (s,) = update([], windows([c_to_k(90.0)] * 60), "d1")
        assert s.mad == 1.0  # coolant PID step: 1 K
        trim = [BaselineWindow("obd.stft_b1", "warm_idle", T0, T0 + 10**10, 0.02, 10)] * 3
        (t,) = update([], trim, "d1")
        assert t.mad == 1.0 / 128.0

    def test_arming_needs_3_drives_and_180_windows(self, cfg: Config) -> None:
        stats: list[BaselineStats] = []
        for d in range(3):
            stats = update(stats, windows([c_to_k(90.0)] * 59), f"d{d}")
        assert stats[0].n_drives == 3 and stats[0].n_windows == 177
        assert not is_armed(stats[0], cfg)
        stats = update(stats, windows([c_to_k(90.0)] * 3), "d3")
        assert is_armed(stats[0], cfg)
        two: list[BaselineStats] = []
        for d in range(2):
            two = update(two, windows([c_to_k(90.0)] * 100), f"d{d}")
        assert two[0].n_windows == 200 and not is_armed(two[0], cfg)

    def test_combined_median_from_summary(self) -> None:
        (prev,) = update([], windows([c_to_k(90.0)] * 10), "d1")
        (s,) = update([prev], windows([c_to_k(92.0)] * 30), "d2")
        assert s.median == pytest.approx(c_to_k(92.0), abs=1e-9)  # 30 of 40 windows at 92 degC
        assert (s.n_windows, s.n_drives, s.version) == (40, 2, 2)

    def test_alerted_windows_are_excluded(self) -> None:
        t = seconds_grid(600)
        v = np.full(600, c_to_k(90.0))
        v[300:360] = c_to_k(110.0)
        alert = Event(
            kind="alert.coolant_high",
            rule_id="coolant_high",
            rule_version=1,
            severity=2,
            t_start_ns=int(t[300]),
            t_end_ns=int(t[359]),
            value=c_to_k(110.0),
            threshold=c_to_k(105.0),
            unit="K",
            message="Coolant 110 °C, over 105 °C.",
            citations=(
                Citation(
                    COOLANT, "OBD 0x05", "trigger", int(t[300]), int(t[310]), "min", 1.0, "K", 10
                ),
            ),
        )
        wins = to_windows(COOLANT, window_medians(t, v, 10.0), [alert])
        assert sum(w.alerted for w in wins) == 6
        (s,) = update([], wins, "d1")
        assert s.n_windows == 54
        assert s.median == pytest.approx(c_to_k(90.0), abs=1e-9)
        assert s.mad == 1.0
        (everything,) = update([], to_windows(COOLANT, window_medians(t, v, 10.0)), "d1")
        assert everything.n_windows == 60

    def test_same_drive_twice_is_refused(self) -> None:
        stats = update([], windows([c_to_k(90.0)] * 5), "d1")
        with pytest.raises(ValueError):
            update(stats, windows([c_to_k(90.0)] * 5), "d1")

    def test_untouched_bins_are_kept(self) -> None:
        stats = update([], windows([c_to_k(90.0)] * 5, "warm_idle"), "d1")
        stats = update(stats, windows([c_to_k(95.0)] * 5, "warm_moving"), "d2")
        by_bin = {s.bin: s for s in stats}
        assert by_bin["warm_idle"].version == 1 and by_bin["warm_idle"].updated_drive == "d1"
        assert by_bin["warm_moving"].version == 1 and by_bin["warm_moving"].n_drives == 1


def excursion(level_c: float, duration_s: int, total_s: int = 300) -> tuple[np.ndarray, ...]:
    t = seconds_grid(total_s)
    v = np.full(total_s, c_to_k(90.0))
    v[100 : 100 + duration_s] = c_to_k(level_c)
    return t, v


class TestCheck:
    def test_sustained_30_s_flags_with_baseline_citation(self, cfg: Config) -> None:
        stats = armed_coolant(cfg)
        t, v = excursion(96.0, 30)
        events = check(stats, t, v, ["warm_idle"] * t.size, cfg)
        assert len(events) == 1
        e = events[0]
        assert e.kind == "alert.baseline"
        assert e.severity == 1
        assert (e.t_start_ns, e.t_end_ns) == (int(t[100]), int(t[100]) + 30 * 10**9)
        z = 6.0 / (MAD_TO_SIGMA * 1.0)
        assert e.value == pytest.approx(z, abs=1e-9)
        assert e.threshold == 4.0
        assert e.message == (
            "Coolant temperature 96.0 °C, 4.05 sigma over its baseline of 90.0 °C."
        )
        trig = [c for c in e.citations if c.role == "trigger"]
        ctx = [c for c in e.citations if c.role == "context"]
        assert len(trig) == 1 and len(ctx) == 1
        assert trig[0].signal == COOLANT and trig[0].stat == "median"
        assert trig[0].stat_value == pytest.approx(c_to_k(96.0), abs=1e-9)
        assert trig[0].n == 30
        assert ctx[0].stat_value == pytest.approx(c_to_k(90.0), abs=1e-9)
        assert ctx[0].n == 180
        base = parse_baseline(ctx[0].stat)
        assert base["bin"] == "warm_idle"
        assert float(base["median"]) == pytest.approx(c_to_k(90.0), rel=1e-6)
        assert float(base["mad"]) == 1.0
        assert base["n_drives"] == "3"
        assert base["version"] == "3"

    def test_20_s_is_not_sustained(self, cfg: Config) -> None:
        t, v = excursion(96.0, 20)
        assert check(armed_coolant(cfg), t, v, ["warm_idle"] * t.size, cfg) == []

    def test_z_just_under_4(self, cfg: Config) -> None:
        t, v = excursion(95.9, 60)  # z = 5.9 / 1.4826 = 3.98
        assert check(armed_coolant(cfg), t, v, ["warm_idle"] * t.size, cfg) == []

    def test_negative_excursion(self, cfg: Config) -> None:
        t, v = excursion(83.0, 40)
        events = check(armed_coolant(cfg), t, v, ["warm_idle"] * t.size, cfg)
        assert len(events) == 1
        assert events[0].value < -4.0
        assert "under its baseline" in events[0].message

    def test_unarmed_baseline_never_flags(self, cfg: Config) -> None:
        stats = update([], windows([c_to_k(90.0)] * 500), "only-drive")
        t, v = excursion(120.0, 120)
        assert check(stats, t, v, ["warm_idle"] * t.size, cfg) == []

    def test_other_bin_is_not_compared(self, cfg: Config) -> None:
        t, v = excursion(96.0, 60)
        assert check(armed_coolant(cfg), t, v, ["warm_moving"] * t.size, cfg) == []


def test_operating_bins(cfg: Config) -> None:
    rpm = [0.0, 800.0, 800.0, 2500.0, float("nan"), 800.0, 800.0]
    cool = [c_to_k(c) for c in (20.0, 60.0, 90.0, 90.0, 90.0, float("nan"), 85.0)]
    speed = [0.0, 0.0, 0.0, 20.0, 0.0, 0.0, 1.9]
    assert operating_bins(rpm, cool, speed, cfg) == [
        "engine_off",
        "warm_up",
        "warm_idle",
        "warm_moving",
        "engine_off",
        EXCLUDED,
        "warm_idle",
    ]
    loop = [True, True, False, True, True, True, True]
    assert operating_bins(rpm, cool, speed, cfg, closed_loop=loop)[2] == EXCLUDED
