"""ObdPoller against the bench ECU emulator on private python-can virtual buses.

Each test runs on a simulated clock: the poller steps every 2 ms, the emulator answers
within the same step, and a third bus logs every frame on the wire (the role can0 plays in
the car) and feeds it to the guard's runtime audit.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import can
import pytest

from photoncar.clock import MonotonicClock, SimClock
from photoncar.config import Config, load_config
from photoncar.records import CanFrame, ObdPoll, Record, Sample, record_time
from photoncar.safety.tx_guard import SUPPORT_PIDS, TxGuard, allowlist_from_config, validate_frame
from photoncar.signals import OBD_PID_TO_SIGNAL
from photoncar.sources.obd import ObdPoller, ObdPollingOff, open_live_can_session

REPO = Path(__file__).resolve().parents[2]
MS = 1_000_000
S = 1_000_000_000
STEP_NS = 2 * MS
CFG = load_config()
FAST = [0x0C, 0x0D, 0x11]


def _load_script(name: str) -> ModuleType:
    path = REPO / "scripts" / "bench" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"bench_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


emulator_mod = _load_script("ecu_emulator")
check_log_mod = _load_script("check_log")


@dataclass
class Rig:
    clock: SimClock
    guard: TxGuard
    poller: ObdPoller
    ecu: Any
    ecu_bus: can.BusABC
    log_bus: can.BusABC
    records: list[Record] = field(default_factory=list)
    wire: list[CanFrame] = field(default_factory=list)

    def run(self, seconds: float) -> None:
        end = self.clock.now_ns() + round(seconds * S)
        while self.clock.now_ns() < end:
            self.records += self.poller.step(self.clock.now_ns())
            self._log()
            self.ecu.pump(self.ecu_bus)
            self._log()
            self.clock.advance(STEP_NS)

    def _log(self) -> None:
        while (msg := self.log_bus.recv(timeout=0.0)) is not None:
            f = CanFrame(
                t_ns=self.clock.now_ns(),
                bus=0,
                arb_id=msg.arbitration_id,
                is_ext=msg.is_extended_id,
                dlc=msg.dlc,
                data=bytes(msg.data),
            )
            self.wire.append(f)
            self.guard.audit(f)

    @property
    def requests(self) -> list[CanFrame]:
        return [f for f in self.wire if 0x7DF <= f.arb_id <= 0x7E7]

    def polls(self, pid: int | None = None) -> list[ObdPoll]:
        return [r for r in self.records if isinstance(r, ObdPoll) and pid in (None, r.pid)]

    def samples(self, signal: str) -> list[Sample]:
        return [r for r in self.records if isinstance(r, Sample) and r.signal == signal]


@pytest.fixture
def make_rig() -> Iterator[Any]:
    opened: list[can.BusABC] = []

    def make(
        values: Mapping[int, float] | None = None,
        *,
        nrc: Mapping[int, int] | None = None,
        silent: tuple[int, ...] = (),
        guard_factory: Callable[[SimClock], TxGuard] | None = None,
        cfg: Config = CFG,
    ) -> Rig:
        channel = f"photoncar-test-{uuid.uuid4().hex}"
        buses = [can.Bus(interface="virtual", channel=channel) for _ in range(3)]
        opened.extend(buses)
        poller_bus, ecu_bus, log_bus = buses
        clock = SimClock(0)
        g = guard_factory(clock) if guard_factory else TxGuard.from_config(cfg, clock)
        ecu = emulator_mod.EcuEmulator(
            emulator_mod.DEFAULT_VALUES if values is None else values, nrc=nrc, silent=silent
        )
        poller = ObdPoller(poller_bus, g, cfg, clock, virtual_bus_for_tests=True)
        return Rig(clock, g, poller, ecu, ecu_bus, log_bus)

    yield make
    for bus in opened:
        bus.shutdown()


def _pid(f: CanFrame) -> int:
    return f.data[2]


def test_discovery_walks_the_bitmap_chain_on_0x7df(make_rig: Any) -> None:
    rig: Rig = make_rig()
    rig.run(1.0)
    first = rig.requests[:4]
    assert [(f.arb_id, _pid(f)) for f in first[:3]] == [(0x7DF, 0x00), (0x7DF, 0x20), (0x7DF, 0x40)]
    assert first[3].arb_id == 0x7E0
    assert all(f.arb_id == 0x7E0 for f in rig.requests[3:])
    assert rig.poller.supported == frozenset(emulator_mod.DEFAULT_VALUES) | {0x20, 0x40}
    bitmap_polls = [p for p in rig.polls() if p.pid in SUPPORT_PIDS]
    assert [(p.pid, p.status, p.unit) for p in bitmap_polls] == [
        (0x00, "ok", "bitmap"),
        (0x20, "ok", "bitmap"),
        (0x40, "ok", "bitmap"),
    ]
    assert not any(isinstance(r, Sample) and r.t_ns < bitmap_polls[-1].t_ns for r in rig.records)


def test_discovery_stops_when_no_higher_bitmap_is_supported(make_rig: Any) -> None:
    rig: Rig = make_rig({0x05: 363.15, 0x0C: 1726.0, 0x0D: 27.5})
    rig.run(2.0)
    assert [_pid(f) for f in rig.requests if f.arb_id == 0x7DF] == [0x00]
    assert rig.poller.polled_pids == (0x0C, 0x0D, 0x05)
    assert {_pid(f) for f in rig.requests if f.arb_id == 0x7E0} == {0x0C, 0x0D, 0x05}


def test_schedule_over_60_simulated_seconds(make_rig: Any) -> None:
    rig: Rig = make_rig()
    rig.run(60.0)
    polls = [f for f in rig.requests if f.arb_id == 0x7E0]
    counts = Counter(_pid(f) for f in polls)
    for pid in FAST:
        assert abs(counts[pid] - 120) <= 1, (hex(pid), counts[pid])  # about 2 Hz each
    slow_cfg = [int(p) for p in CFG.get("obd.slow_pids")]
    slow_supported = [p for p in slow_cfg if p in emulator_mod.DEFAULT_VALUES]
    slow = [_pid(f) for f in polls if _pid(f) not in FAST]
    assert abs(len(slow) - 240) <= 3  # about 4 Hz shared
    assert slow == [slow_supported[i % len(slow_supported)] for i in range(len(slow))]
    slow_counts = [counts[p] for p in slow_supported]
    assert max(slow_counts) - min(slow_counts) <= 1
    assert len(rig.requests) <= 10 * 60 + 1
    times = [f.t_ns for f in rig.requests]
    j = 0
    for i, t0 in enumerate(times):
        while j < len(times) and times[j] <= t0 + S:
            j += 1
        assert j - i <= 11  # 10 Hz * 1 s + burst 1
    for f in rig.requests:
        msg = can.Message(arbitration_id=f.arb_id, is_extended_id=f.is_ext, data=f.data)
        validate_frame(msg, rig.guard.allowed_pids)
    assert not rig.guard.disabled
    ok = [p for p in rig.polls() if p.status == "ok"]
    assert len(ok) == len(rig.requests)


def test_unsupported_pids_are_never_requested(make_rig: Any) -> None:
    values = {k: v for k, v in emulator_mod.DEFAULT_VALUES.items() if k not in (0x0F, 0x42)}
    rig: Rig = make_rig(values)
    rig.run(30.0)
    sent = {_pid(f) for f in rig.requests}
    assert 0x10 not in sent  # the S65 has no MAF sensor; the emulator leaves it out
    assert 0x0F not in sent
    assert 0x42 not in sent
    assert 0x40 not in sent  # nothing above 0x40 is supported, so no 0x40 bitmap request
    assert rig.poller.supported is not None
    assert {0x0F, 0x10, 0x42}.isdisjoint(rig.poller.supported)


def test_samples_carry_catalog_names_and_si_values(make_rig: Any) -> None:
    rig: Rig = make_rig()
    rig.run(5.0)
    rpm = rig.samples(OBD_PID_TO_SIGNAL[0x0C])
    coolant = rig.samples("obd.coolant_temp")
    assert rpm and all(s.value == 850.0 for s in rpm)
    assert coolant and all(abs(s.value - 363.15) < 1e-9 for s in coolant)
    for p in rig.polls(0x0C):
        assert (p.status, p.ecu, p.unit, p.raw) == ("ok", 0x7E8, "rpm", bytes([0x0D, 0x48]))
        assert p.resp_t_ns is not None and p.resp_t_ns > p.req_t_ns
    times = [record_time(r) for r in rig.records]
    assert times == sorted(times)


def test_timeouts_are_reported_and_polling_continues(make_rig: Any) -> None:
    rig: Rig = make_rig(silent=(0x05,))
    rig.run(10.0)
    polls = rig.polls(0x05)
    assert polls and all(p.status == "timeout" for p in polls)
    for p in polls:
        assert (p.resp_t_ns, p.ecu, p.value, p.raw) == (None, None, None, b"")
    assert rig.samples("obd.coolant_temp") == []
    assert len(rig.samples("obd.rpm")) >= 19
    # A timed-out request blocks the next one for the full response timeout (0.1 s).
    req_times = [f.t_ns for f in rig.requests]
    for f in rig.requests:
        if _pid(f) == 0x05 and f.arb_id == 0x7E0:
            later = [t for t in req_times if t > f.t_ns]
            assert not later or later[0] - f.t_ns >= 100 * MS


def test_negative_responses_are_logged_as_nrc(make_rig: Any) -> None:
    rig: Rig = make_rig(nrc={0x04: 0x31})
    rig.run(10.0)
    polls = rig.polls(0x04)
    assert polls and all(p.status == "nrc" for p in polls)
    assert all(p.raw == bytes([0x7F, 0x01, 0x31]) and p.value is None for p in polls)
    assert rig.samples("obd.load") == []


def test_no_ecu_answer_retries_discovery_every_5_s(make_rig: Any) -> None:
    rig: Rig = make_rig({})
    rig.ecu.bitmaps.clear()  # an ECU that never answers
    rig.run(12.0)
    assert [(f.arb_id, _pid(f)) for f in rig.requests] == [(0x7DF, 0x00)] * 3
    starts = [f.t_ns for f in rig.requests]
    assert starts[1] - starts[0] == pytest.approx(5.1 * S, abs=4 * MS)
    assert [p.status for p in rig.polls()] == ["timeout"] * 3
    assert rig.poller.supported is None


def test_a_guard_violation_stops_all_further_sends(make_rig: Any) -> None:
    pids = allowlist_from_config(CFG) - {0x11}
    rig: Rig = make_rig(guard_factory=lambda clock: TxGuard(pids, 10.0, clock))
    guard = rig.guard
    rig.run(10.0)
    assert guard.disabled
    assert guard.reason is not None and "0x11" in guard.reason
    sent = rig.requests
    assert 0x11 not in {_pid(f) for f in sent}  # the refused frame never reached the wire
    assert len(sent) == 5  # 3 bitmaps, 0x0C, 0x0D; then 0x11 is refused
    assert rig.poller.sent == 5


def test_an_audit_mismatch_stops_all_further_sends(make_rig: Any) -> None:
    rig: Rig = make_rig()
    rig.run(3.0)
    before = len(rig.requests)
    assert before > 20
    rig.guard.audit(CanFrame(rig.clock.now_ns(), 0, 0x7E0, False, 8, bytes([0x30]) + bytes(7)))
    assert rig.guard.disabled
    rig.run(10.0)
    assert len(rig.requests) == before


def test_an_error_frame_on_the_request_bus_stops_sends(make_rig: Any) -> None:
    rig: Rig = make_rig()
    rig.run(2.0)
    before = len(rig.requests)
    rig.ecu_bus.send(can.Message(arbitration_id=0x0, is_error_frame=True, data=bytes(8)))
    rig.run(5.0)
    assert rig.guard.disabled
    assert rig.guard.reason == "error frame on the request bus"
    assert len(rig.requests) <= before + 1


def test_a_disabled_guard_sends_nothing(make_rig: Any) -> None:
    rig: Rig = make_rig()
    rig.guard.disable("owner turned TX off")
    rig.run(5.0)
    assert rig.wire == []
    assert rig.poller.sent == 0


def test_a_slower_guard_throttles_without_latching(make_rig: Any) -> None:
    pids = allowlist_from_config(CFG)
    rig: Rig = make_rig(guard_factory=lambda clock: TxGuard(pids, 5.0, clock))
    guard = rig.guard
    rig.run(60.0)
    assert not guard.disabled
    assert 5 * 60 - 2 <= len(rig.requests) <= 5 * 60 + 1


def _cfg_with(**obd: object) -> Config:
    raw = {k: dict(v) if isinstance(v, Mapping) else v for k, v in CFG.raw.items()}
    raw["obd"].update(obd)
    return Config(raw)


def test_config_that_breaks_the_rules_is_refused() -> None:
    clock = SimClock(0)
    guard = TxGuard.from_config(CFG, clock)
    bus = can.Bus(interface="virtual", channel=f"photoncar-test-{uuid.uuid4().hex}")

    def poller(g: TxGuard, cfg: Config) -> ObdPoller:
        return ObdPoller(bus, g, cfg, clock, virtual_bus_for_tests=True)

    try:
        with pytest.raises(ValueError, match="max_in_flight"):
            poller(guard, _cfg_with(max_in_flight=2))
        with pytest.raises(ValueError, match="cap"):
            poller(guard, _cfg_with(fast_hz_each=3.0))
        with pytest.raises(ValueError, match="positive"):
            poller(guard, _cfg_with(slow_hz_total=0.0))
        with pytest.raises(ValueError, match="physical_id"):
            poller(guard, _cfg_with(physical_id=0x6F1))
        slow_cap = _cfg_with(max_request_hz=5.0, fast_hz_each=1.0, slow_hz_total=1.0)
        with pytest.raises(ValueError, match="guard rate"):
            poller(TxGuard({0x0C}, 10.0, clock), slow_cap)
        with pytest.raises(ValueError, match="rate_hz"):
            TxGuard({0x0C}, 20.0, clock)  # over the hard ceiling: refused before any poller
        # A config cap over the ceiling does not widen the schedule check: 12 Hz is refused.
        with pytest.raises(ValueError, match="cap"):
            poller(guard, _cfg_with(max_request_hz=50.0, fast_hz_each=2.0, slow_hz_total=6.0))
    finally:
        bus.shutdown()


def test_request_bus_stays_down_unless_polling_is_on() -> None:
    assert CFG.get("can.obd_polling") is False
    with pytest.raises(ObdPollingOff, match="off"):
        open_live_can_session(CFG, SimClock(0), system="Linux")
    raw = {k: dict(v) if isinstance(v, Mapping) else v for k, v in CFG.raw.items()}
    raw["can"].update(obd_polling=True, request_interface="can0")
    with pytest.raises(ObdPollingOff, match="capture interface"):
        open_live_can_session(Config(raw), SimClock(0), system="Linux")


def test_run_loop_in_real_time() -> None:
    channel = f"photoncar-test-{uuid.uuid4().hex}"
    poller_bus = can.Bus(interface="virtual", channel=channel)
    ecu_bus = can.Bus(interface="virtual", channel=channel)
    clock = MonotonicClock()
    guard = TxGuard.from_config(CFG, clock)
    ecu = emulator_mod.EcuEmulator(emulator_mod.DEFAULT_VALUES)
    serve_stop = threading.Event()
    server = threading.Thread(target=ecu.serve, args=(ecu_bus, serve_stop), daemon=True)
    server.start()
    poller = ObdPoller(poller_bus, guard, CFG, clock, virtual_bus_for_tests=True)
    stop = threading.Event()
    timer = threading.Timer(1.5, stop.set)
    t0 = time.monotonic()
    timer.start()
    try:
        records = list(poller.run(stop))
    finally:
        timer.cancel()
        serve_stop.set()
        server.join(timeout=2.0)
        poller_bus.shutdown()
        ecu_bus.shutdown()
    elapsed = time.monotonic() - t0
    assert not guard.disabled
    assert poller.supported is not None
    assert poller.sent <= 10 * elapsed + 1
    samples = [r for r in records if isinstance(r, Sample)]
    assert len(samples) >= 5
    assert {s.signal for s in samples} >= {"obd.rpm", "obd.speed"}


def test_poller_traffic_passes_the_bench_rule(make_rig: Any) -> None:
    rig: Rig = make_rig()
    rig.run(20.0)
    t0 = 1_695_500_000.0
    lines = [
        f"({t0 + f.t_ns / S:.6f}) can2 {f.arb_id:03X}#{f.data.hex().upper()}" for f in rig.wire
    ]
    report = check_log_mod.check_lines(lines, rig.guard.allowed_pids, 10.0)
    assert report.passed, report.failures
    assert report.requests == len(rig.requests) > 150
    assert report.replies == len(rig.wire) - len(rig.requests)
    assert report.flow_control == report.error_frames == report.other == 0


# The bench ECU emulator answers requests on whatever bus it opens, so it must never open
# the car's buses: it refuses the configured car interfaces, and any real interface needs
# --bench. The python-can virtual interface stays open for tests.


@dataclass
class Opener:
    """Stands in for the emulator's bus opener and records every bus it was asked for."""

    calls: list[tuple[str, str, int]] = field(default_factory=list)

    def __call__(self, interface: str, channel: str, bitrate: int) -> Any:
        self.calls.append((interface, channel, bitrate))
        return self

    def recv(self, timeout: float | None = None) -> can.Message | None:
        raise KeyboardInterrupt  # Ctrl+C at once: main closes the bus and returns

    def shutdown(self) -> None:
        return None


@pytest.fixture
def opener(monkeypatch: pytest.MonkeyPatch) -> Opener:
    fake = Opener()
    monkeypatch.setattr(emulator_mod, "open_bus", fake)
    return fake


@pytest.mark.parametrize("channel", ["can0", "can1", " can1 "])
@pytest.mark.parametrize("interface", ["socketcan", "virtual"])
@pytest.mark.parametrize("bench", [[], ["--bench"]])
def test_emulator_refuses_the_car_interfaces(
    opener: Opener,
    capsys: pytest.CaptureFixture[str],
    channel: str,
    interface: str,
    bench: list[str],
) -> None:
    argv = ["--interface", interface, "--channel", channel, *bench]
    assert emulator_mod.main(argv) == 2
    assert opener.calls == []
    err = capsys.readouterr().err
    assert f"{channel.strip()} is a car interface in the config" in err


def test_emulator_reads_the_car_interfaces_from_the_config(opener: Opener, tmp_path: Path) -> None:
    text = (REPO / "config" / "photoncar.toml").read_text(encoding="utf-8")
    assert 'request_interface = "can1"' in text
    cfg_path = tmp_path / "photoncar.toml"
    cfg_path.write_text(text.replace('request_interface = "can1"', 'request_interface = "obd1"'))
    base = ["--interface", "socketcan", "--bench", "--config", str(cfg_path)]
    assert emulator_mod.main([*base, "--channel", "obd1"]) == 2
    assert emulator_mod.main([*base, "--channel", "can0"]) == 2
    assert opener.calls == []
    assert emulator_mod.car_interfaces(CFG) == frozenset({"can0", "can1"})


@pytest.mark.parametrize("interface", ["socketcan", "slcan", "gs_usb", "pcan", ""])
def test_emulator_needs_bench_on_every_real_interface(
    opener: Opener, capsys: pytest.CaptureFixture[str], interface: str
) -> None:
    assert emulator_mod.main(["--interface", interface, "--channel", "vcan0"]) == 2
    assert opener.calls == []
    assert "--bench" in capsys.readouterr().err


def test_emulator_needs_bench_by_default(opener: Opener) -> None:
    assert emulator_mod.main(["--channel", "vcan0"]) == 2  # the default interface is socketcan
    assert opener.calls == []


def test_emulator_runs_on_a_bench_channel_with_the_flag(
    opener: Opener, capsys: pytest.CaptureFixture[str]
) -> None:
    assert emulator_mod.main(["--interface", "socketcan", "--channel", "vcan0", "--bench"]) == 0
    assert opener.calls == [("socketcan", "vcan0", 500000)]
    assert "Replies sent: 0" in capsys.readouterr().out


def test_emulator_runs_on_a_virtual_bus_without_the_flag(opener: Opener) -> None:
    channel = f"photoncar-test-{uuid.uuid4().hex}"
    assert emulator_mod.main(["--interface", "virtual", "--channel", channel]) == 0
    assert opener.calls == [("virtual", channel, 500000)]
