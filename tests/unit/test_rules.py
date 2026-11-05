"""Rule engine: injected fault series hit each on-delay, clear hold, gate, and stale edge."""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from photoncar.config import Config, load_config
from photoncar.detect.rules import RuleEngine
from photoncar.records import Event, Sample
from photoncar.units import c_to_k

T0 = 42_000_000_000_000


@pytest.fixture(scope="module")
def cfg() -> Config:
    return load_config()


def ns(s: float) -> int:
    return T0 + round(s * 1e9)


def series(signal: str, start_s: float, end_s: float, step_s: float, value: float) -> list[Sample]:
    """Samples from start to end inclusive at ``step_s``, built on integer ns."""
    n = round((end_s - start_s) / step_s)
    return [Sample(ns(start_s) + round(i * step_s * 1e9), signal, value) for i in range(n + 1)]


def run(engine: RuleEngine, streams: Iterable[list[Sample]], end_s: float) -> list[Event]:
    merged = sorted((s for st in streams for s in st), key=lambda s: (s.t_ns, s.signal))
    out: list[Event] = []
    for s in merged:
        out += engine.push(s)
    out += engine.finish(ns(end_s))
    for e in out:
        if e.kind.startswith("alert."):
            assert any(c.role == "trigger" for c in e.citations)
        keys = [(c.signal, c.role) for c in e.citations]
        assert len(keys) == len(set(keys)), "store key (event, signal, role) must be unique"
    return out


def only(events: list[Event], rule_id: str) -> list[Event]:
    return [e for e in events if e.rule_id == rule_id]


def coolant(*parts: tuple[float, float, float], step: float = 0.1) -> list[Sample]:
    out: list[Sample] = []
    for a, b, c in parts:
        out += series("obd.coolant_temp", a, b, step, c_to_k(c))
    return out


class TestCoolant:
    def test_9_9_s_over_does_not_fire(self, cfg: Config) -> None:
        s = coolant((90.0, 99.9, 90.0), (100.0, 109.9, 105.9), (110.0, 200.0, 104.0))
        s = [x for x in s if x.t_ns >= ns(0)]
        events = run(RuleEngine(cfg), [s], 200.0)
        assert only(events, "coolant_high") == []

    def test_10_0_s_over_fires_and_clears_after_30_s(self, cfg: Config) -> None:
        s = coolant(
            (100.0, 110.0, 105.9),
            (110.1, 149.9, 104.0),
            (150.0, 190.0, 101.9),
        )
        events = run(RuleEngine(cfg), [s], 200.0)
        alerts = only(events, "coolant_high")
        assert len(alerts) == 1
        e = alerts[0]
        assert e.kind == "alert.coolant_high"
        assert e.severity == 2
        assert e.resolved
        assert e.t_start_ns == ns(100.0)
        assert e.t_end_ns == ns(150.0)
        assert e.message == "Coolant 106 °C, over 105 °C."
        assert abs(e.value - c_to_k(105.9)) < 1e-9
        assert e.threshold == c_to_k(105.0)
        trig = [c for c in e.citations if c.role == "trigger"]
        assert len(trig) == 1
        c = trig[0]
        assert c.signal == "obd.coolant_temp"
        assert c.source == "OBD 0x05"
        assert (c.t_start_ns, c.t_end_ns) == (ns(100.0), ns(110.0))
        assert c.stat == "min"
        assert abs(c.stat_value - c_to_k(105.9)) < 1e-9
        assert c.unit == "K"
        assert c.n == 101

    def test_clear_needs_the_full_30_s(self, cfg: Config) -> None:
        s = coolant((100.0, 110.0, 106.0), (110.1, 139.9, 101.9), (140.0, 200.0, 103.0))
        events = run(RuleEngine(cfg), [s], 200.0)
        (e,) = only(events, "coolant_high")
        assert not e.resolved
        assert e.t_end_ns == ns(200.0)

    def test_clear_at_exactly_30_s(self, cfg: Config) -> None:
        s = coolant((100.0, 110.0, 106.0), (110.1, 140.1, 101.9), (140.2, 200.0, 103.0))
        (e,) = only(run(RuleEngine(cfg), [s], 200.0), "coolant_high")
        assert e.resolved
        assert e.t_end_ns == ns(110.1)

    def test_hysteresis_band_holds_the_alert(self, cfg: Config) -> None:
        s = coolant((100.0, 110.0, 106.0), (110.1, 300.0, 102.0))
        (e,) = only(run(RuleEngine(cfg), [s], 300.0), "coolant_high")
        assert not e.resolved

    def test_stale_gap_resets_the_on_delay(self, cfg: Config) -> None:
        s = coolant((100.0, 104.0, 106.0), (112.0, 121.9, 106.0))
        events = run(RuleEngine(cfg), [s], 130.0)
        assert only(events, "coolant_high") == []
        (stale,) = only(events, "stale")
        assert stale.citations[0].stat_value == pytest.approx(8.0, abs=1e-9)

    def test_refire_inside_realert_interval_is_one_alert(self, cfg: Config) -> None:
        episode = [(100.0, 110.0, 106.0), (110.1, 150.0, 101.0)]
        again = [(250.0, 260.0, 107.0), (260.1, 300.0, 101.0)]
        events = run(RuleEngine(cfg), [coolant(*episode, *again, step=0.5)], 400.0)
        (e,) = only(events, "coolant_high")
        assert e.t_start_ns == ns(100.0)
        assert e.t_end_ns == ns(260.1)  # the first sample back under 102 °C
        assert e.message == "Coolant 107 °C, over 105 °C."

    def test_refire_after_realert_interval_is_a_new_alert(self, cfg: Config) -> None:
        engine = RuleEngine(cfg)
        first = coolant((100.0, 110.0, 106.0), (110.5, 170.0, 101.0), step=0.5)
        between = coolant((170.5, 450.0, 95.0), step=0.5)
        second = coolant((450.5, 461.0, 106.0), step=0.5)
        emitted_at: list[int] = []
        events: list[Event] = []
        for smp in first + between + second:
            got = engine.push(smp)
            if got:
                emitted_at.append(smp.t_ns)
            events += got
        events += engine.finish(ns(470.0))
        alerts = only(events, "coolant_high")
        assert len(alerts) == 2
        assert alerts[0].resolved and not alerts[1].resolved
        # The first alert is emitted once 300 s have passed since it resolved at 140.5 s.
        assert emitted_at == [ns(441.0)]

    def test_message_keeps_a_decimal_when_rounding_would_hide_the_excess(self, cfg: Config) -> None:
        s = coolant((100.0, 110.0, 105.2))
        (e,) = only(run(RuleEngine(cfg), [s], 120.0), "coolant_high")
        assert e.message == "Coolant 105.2 °C, over 105 °C."


def warm_closed_loop(
    end_s: float, *, fuel: float = 2.0, temp_c: float = 90.0
) -> list[list[Sample]]:
    return [
        series("obd.fuel_system", 0.0, end_s, 1.0, fuel),
        series("obd.coolant_temp", 0.2, end_s, 1.0, c_to_k(temp_c)),
    ]


def trims(bank: int, stft: float, ltft: float, a: float, b: float) -> list[list[Sample]]:
    return [
        series(f"obd.stft_b{bank}", a, b, 1.0, stft),
        series(f"obd.ltft_b{bank}", a + 0.5, b + 0.5, 1.0, ltft),
    ]


class TestTrims:
    def test_warn_fires_after_30_s_outside_10_pct(self, cfg: Config) -> None:
        streams = warm_closed_loop(80.0) + trims(1, 0.08, 0.04, 10.0, 70.0)
        (e,) = only(run(RuleEngine(cfg), streams, 80.0), "trim_warn")
        assert e.kind == "alert.trim_warn"
        assert e.t_start_ns == ns(10.5)
        assert e.message == "Bank 1 fuel trim +12%, outside ±10%."
        trig = {c.signal: c for c in e.citations if c.role == "trigger"}
        assert set(trig) == {"obd.stft_b1", "obd.ltft_b1"}
        for c in trig.values():
            assert (c.t_start_ns, c.t_end_ns) == (ns(10.5), ns(40.5))
            assert c.stat == "mean"
        assert abs(trig["obd.stft_b1"].stat_value - 0.08) < 1e-12
        assert abs(trig["obd.ltft_b1"].stat_value - 0.04) < 1e-12
        assert trig["obd.stft_b1"].n == 30
        assert trig["obd.ltft_b1"].n == 31

    def test_warn_29_s_does_not_fire(self, cfg: Config) -> None:
        streams = warm_closed_loop(80.0) + trims(1, 0.08, 0.04, 10.0, 39.0)
        streams += [series("obd.stft_b1", 40.0, 80.0, 1.0, 0.0)]
        streams += [series("obd.ltft_b1", 40.5, 80.5, 1.0, 0.04)]
        assert only(run(RuleEngine(cfg), streams, 81.0), "trim_warn") == []

    def test_open_loop_gate(self, cfg: Config) -> None:
        streams = warm_closed_loop(80.0, fuel=1.0) + trims(1, 0.08, 0.04, 10.0, 70.0)
        assert only(run(RuleEngine(cfg), streams, 80.0), "trim_warn") == []

    @pytest.mark.parametrize(("temp_c", "fires"), [(84.0, False), (85.0, True)])
    def test_coolant_gate(self, cfg: Config, temp_c: float, fires: bool) -> None:
        streams = warm_closed_loop(80.0, temp_c=temp_c) + trims(1, 0.08, 0.04, 10.0, 70.0)
        assert bool(only(run(RuleEngine(cfg), streams, 80.0), "trim_warn")) is fires

    def test_negative_trim_message(self, cfg: Config) -> None:
        streams = warm_closed_loop(80.0) + trims(2, -0.07, -0.06, 10.0, 70.0)
        (e,) = only(run(RuleEngine(cfg), streams, 80.0), "trim_warn")
        assert e.message == "Bank 2 fuel trim -13%, outside ±10%."
        assert e.value == pytest.approx(-0.13, abs=1e-12)

    def test_critical_fires_after_10_s_outside_25_pct(self, cfg: Config) -> None:
        streams = warm_closed_loop(40.0) + trims(2, 0.27, 0.0, 10.0, 25.0)
        streams += trims(2, 0.0, 0.0, 26.0, 40.0)
        events = run(RuleEngine(cfg), streams, 40.0)
        assert only(events, "trim_warn") == []
        (e,) = only(events, "trim_critical")
        assert e.severity == 2
        assert e.message == "Bank 2 short-term fuel trim +27%, outside ±25%."
        assert e.resolved
        (c,) = e.citations
        assert c.signal == "obd.stft_b2"
        assert c.source == "OBD 0x08"
        assert c.stat == "min_abs"
        assert abs(c.stat_value - 0.27) < 1e-12
        assert (c.t_start_ns, c.t_end_ns) == (ns(10.0), ns(20.0))
        assert c.n == 11

    def test_critical_9_s_does_not_fire(self, cfg: Config) -> None:
        streams = warm_closed_loop(40.0) + trims(2, 0.27, 0.0, 10.0, 19.0)
        streams += trims(2, 0.0, 0.0, 20.0, 40.0)
        assert only(run(RuleEngine(cfg), streams, 40.0), "trim_critical") == []


def engine_running(start_s: float, end_s: float, rpm: float = 800.0) -> list[Sample]:
    return series("obd.rpm", start_s, end_s, 0.5, rpm)


class TestVoltageRunning:
    def test_fires_60_s_after_the_10_s_ignore_window(self, cfg: Config) -> None:
        streams = [engine_running(0.0, 100.0), series("obd.module_voltage", 0.0, 100.0, 1.0, 12.0)]
        (e,) = only(run(RuleEngine(cfg), streams, 100.0), "voltage_low_running")
        assert e.t_start_ns == ns(10.0)
        assert e.message == "Module voltage 12.0 V, under 12.5 V."
        trig = [c for c in e.citations if c.role == "trigger"]
        assert len(trig) == 1
        assert (trig[0].t_start_ns, trig[0].t_end_ns) == (ns(10.0), ns(70.0))
        assert trig[0].stat == "max"
        assert trig[0].stat_value == 12.0
        assert trig[0].source == "OBD 0x42"

    def test_59_s_does_not_fire(self, cfg: Config) -> None:
        streams = [
            engine_running(0.0, 100.0),
            series("obd.module_voltage", 0.0, 69.0, 1.0, 12.0),
            series("obd.module_voltage", 70.0, 100.0, 1.0, 13.8),
        ]
        assert only(run(RuleEngine(cfg), streams, 100.0), "voltage_low_running") == []

    def test_low_only_inside_ignore_window(self, cfg: Config) -> None:
        streams = [
            engine_running(0.0, 100.0),
            series("obd.module_voltage", 0.0, 9.0, 1.0, 11.0),
            series("obd.module_voltage", 10.0, 100.0, 1.0, 14.0),
        ]
        assert only(run(RuleEngine(cfg), streams, 100.0), "voltage_low_running") == []

    def test_engine_off_is_gated(self, cfg: Config) -> None:
        streams = [
            engine_running(0.0, 100.0, 0.0),
            series("obd.module_voltage", 0.0, 100.0, 1.0, 12),
        ]
        assert only(run(RuleEngine(cfg), streams, 100.0), "voltage_low_running") == []


class TestVoltageRested:
    @staticmethod
    def key_on(volts: float) -> list[list[Sample]]:
        return [
            series("obd.rpm", 0.0, 5.5, 0.5, 0.0),
            series("obd.rpm", 6.0, 20.0, 0.5, 800.0),
            series("obd.module_voltage", 0.2, 20.2, 1.0, volts),
        ]

    def test_advisory_after_24_h_rest(self, cfg: Config) -> None:
        events = run(RuleEngine(cfg, rested_h=30.0), self.key_on(12.1), 21.0)
        (e,) = only(events, "voltage_low_rested")
        assert e.kind == "alert.voltage_low_rested"
        assert e.severity == 1
        assert (e.t_start_ns, e.t_end_ns) == (ns(0.2), ns(6.0))
        assert e.message == "Rested module voltage 12.10 V, under 12.29 V. Advisory only."
        (c,) = e.citations
        assert c.role == "trigger" and c.stat == "first" and c.stat_value == 12.1
        assert c.n == 6

    @pytest.mark.parametrize("rested_h", [None, 23.9])
    def test_needs_24_h_rest(self, cfg: Config, rested_h: float | None) -> None:
        events = run(RuleEngine(cfg, rested_h=rested_h), self.key_on(12.1), 21.0)
        assert only(events, "voltage_low_rested") == []

    def test_healthy_battery(self, cfg: Config) -> None:
        events = run(RuleEngine(cfg, rested_h=30.0), self.key_on(12.29), 21.0)
        assert only(events, "voltage_low_rested") == []

    def test_engine_already_running(self, cfg: Config) -> None:
        streams = [engine_running(0.0, 20.0), series("obd.module_voltage", 0.2, 20.2, 1.0, 12.0)]
        assert only(run(RuleEngine(cfg, rested_h=30.0), streams, 21.0), "voltage_low_rested") == []


class TestStale:
    def test_stale_after_three_poll_periods(self, cfg: Config) -> None:
        engine = RuleEngine(cfg)
        assert engine.stale_limit_s("obd.coolant_temp") == pytest.approx(7.5)
        assert engine.stale_limit_s("obd.rpm") == pytest.approx(1.5)
        streams = [
            series("obd.rpm", 0.0, 20.0, 0.5, 800.0),
            [Sample(ns(0.0), "obd.coolant_temp", c_to_k(90.0))],
            series("obd.coolant_temp", 12.0, 20.0, 2.5, c_to_k(90.0)),
        ]
        (e,) = only(run(engine, streams, 20.0), "stale")
        assert e.kind == "alert.stale"
        assert e.resolved
        assert (e.t_start_ns, e.t_end_ns) == (ns(0.0), ns(12.0))
        assert e.message == "Coolant temperature stale, no update for 12 s."
        (c,) = e.citations
        assert c.signal == "obd.coolant_temp" and c.stat == "gap_s" and c.stat_value == 12.0

    def test_exactly_three_periods_is_not_stale(self, cfg: Config) -> None:
        streams = [
            series("obd.rpm", 0.0, 20.0, 0.5, 800.0),
            series("obd.coolant_temp", 0.0, 15.0, 7.5, c_to_k(90.0)),
        ]
        assert only(run(RuleEngine(cfg), streams, 20.0), "stale") == []

    def test_still_stale_at_finish(self, cfg: Config) -> None:
        streams = [
            series("obd.rpm", 0.0, 30.0, 0.5, 800.0),
            [Sample(ns(0.0), "obd.coolant_temp", c_to_k(90.0))],
        ]
        (e,) = only(run(RuleEngine(cfg), streams, 30.0), "stale")
        assert not e.resolved
        assert e.t_end_ns == ns(30.0)

    def test_everything_stopping_together_is_not_stale(self, cfg: Config) -> None:
        streams = [
            series("obd.rpm", 0.0, 10.0, 0.5, 800.0),
            series("obd.coolant_temp", 0.0, 10.0, 2.5, c_to_k(90.0)),
        ]
        assert only(run(RuleEngine(cfg), streams, 60.0), "stale") == []


def test_ignores_unrelated_signals_and_rejects_push_after_finish(cfg: Config) -> None:
    engine = RuleEngine(cfg)
    assert engine.push(Sample(ns(0.0), "gps.speed", 3.0)) == []
    assert engine.push(Sample(ns(0.0), "obd.coolant_temp", float("nan"))) == []
    engine.finish(ns(1.0))
    with pytest.raises(RuntimeError):
        engine.push(Sample(ns(2.0), "obd.rpm", 800.0))
