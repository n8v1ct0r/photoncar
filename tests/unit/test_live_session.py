"""The live CAN session: the only public way to poll a real bus.

Runs on private python-can virtual buses with captured ``ip`` output and a pretend Linux.
One virtual channel stands for the car's OBD wire: ``can0`` (capture), ``can1`` (requests)
and the bench ECU emulator all sit on it, so ``can0`` sees every frame ``can1`` sends, as the
two HAT channels do on the OBD port. These tests run in real time, about 1 s each.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import platform
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import can
import pytest

from photoncar.clock import MonotonicClock
from photoncar.config import Config, load_config
from photoncar.records import CanFrame, ObdPoll, Record, Sample, record_time
from photoncar.safety.request_link import ErrorCounters
from photoncar.safety.tx_guard import REQUEST_IDS, TxGuard, validate_frame
from photoncar.sources import obd
from photoncar.sources.obd import (
    LiveCanSession,
    LiveCanUnavailable,
    ObdPoller,
    ObdPollingOff,
    open_live_can_session,
)

REPO = Path(__file__).resolve().parents[2]
BASE = load_config()


def _load_emulator() -> ModuleType:
    path = REPO / "scripts" / "bench" / "ecu_emulator.py"
    spec = importlib.util.spec_from_file_location("bench_ecu_emulator_live", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


emulator_mod = _load_emulator()


def _cfg(**can_keys: object) -> Config:
    raw = {k: dict(v) if isinstance(v, Mapping) else v for k, v in BASE.raw.items()}
    raw["can"].update({"obd_polling": True, **can_keys})
    return Config(raw)


def _link(
    ifname: str,
    ctrlmode: Sequence[str],
    *,
    restart_ms: int = 0,
    bitrate: int = 500000,
    up: bool = True,
    berr: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    """One entry of ``ip -details [-statistics] -json link show`` for an mcp251xfd channel."""
    return {
        "ifname": ifname,
        "flags": ["NOARP", "UP", "LOWER_UP", "ECHO"] if up else ["NOARP", "ECHO"],
        "operstate": "UP" if up else "DOWN",
        "link_type": "can",
        "linkinfo": {
            "info_kind": "can",
            "info_data": {
                "ctrlmode": list(ctrlmode),
                "state": "ERROR-ACTIVE" if up else "STOPPED",
                "berr_counter": {"tx": berr[0], "rx": berr[1]},
                "restart_ms": restart_ms,
                "bittiming": {"bitrate": bitrate, "sample_point": "0.875"},
                "clock": 40000000,
            },
            "info_xstats": {"restarts": 0, "bus_error": 0, "error_warning": 0, "bus_off": 0},
        },
    }


CAN0 = _link("can0", ["LISTEN-ONLY"])
CAN1 = _link("can1", ["ONE-SHOT"])


class IpStub:
    """Stands in for the ``ip`` binary. ``can1_counters`` feeds successive statistics reads.

    ``can1_stats``, when given, returns the whole can1 entry for the nth statistics read.
    """

    def __init__(
        self,
        can0: Callable[[int], dict[str, Any]] = lambda _: CAN0,
        can1: dict[str, Any] = CAN1,
        can1_counters: Sequence[tuple[int, int]] = ((0, 0),),
        can1_stats: Callable[[int], dict[str, Any]] | None = None,
    ) -> None:
        self.can0 = can0
        self.can1 = can1
        self.counters = list(can1_counters)
        self.can1_stats = can1_stats
        self.calls: list[list[str]] = []
        self._lock = threading.Lock()

    def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        with self._lock:
            self.calls.append(list(argv))
            ifname = argv[-1]
            if ifname == "can0":
                n = sum(1 for c in self.calls if c[-1] == "can0")
                return 0, json.dumps([self.can0(n)])
            if "-statistics" in argv:
                n = sum(1 for c in self.calls if c[-1] == "can1" and "-statistics" in c)
                if self.can1_stats is not None:
                    return 0, json.dumps([self.can1_stats(n)])
                berr = self.counters[min(n, len(self.counters)) - 1]
                return 0, json.dumps([_link("can1", ["ONE-SHOT"], berr=berr)])
            return 0, json.dumps([self.can1])


class TapBus:
    """A capture bus wrapper that can slip an injected frame (an error frame) into recv."""

    def __init__(self, inner: can.BusABC) -> None:
        self.inner = inner
        self.inject: deque[can.Message] = deque()

    def recv(self, timeout: float | None = None) -> can.Message | None:
        if self.inject:
            return self.inject.popleft()
        return self.inner.recv(timeout=min(timeout if timeout is not None else 0.05, 0.05))

    def shutdown(self) -> None:
        self.inner.shutdown()


class Wire:
    """A virtual OBD wire with the bench ECU emulator answering on it."""

    def __init__(self) -> None:
        self.channel = f"photoncar-test-{uuid.uuid4().hex}"
        self.opened: list[str] = []
        self.buses: list[Any] = []
        self.ecu = emulator_mod.EcuEmulator(emulator_mod.DEFAULT_VALUES)
        self._ecu_bus = can.Bus(interface="virtual", channel=self.channel)
        self._stop = threading.Event()
        self._server = threading.Thread(
            target=self.ecu.serve, args=(self._ecu_bus, self._stop), daemon=True
        )
        self._server.start()
        self.tap: TapBus | None = None

    def factory(
        self, *, capture_channel: str | None = None, tap: bool = False
    ) -> Callable[[str], Any]:
        def open_bus(ifname: str) -> Any:
            self.opened.append(ifname)
            channel = capture_channel if ifname == "can0" and capture_channel else self.channel
            bus: Any = can.Bus(interface="virtual", channel=channel)
            if ifname == "can0" and tap:
                bus = self.tap = TapBus(bus)
            self.buses.append(bus)
            return bus

        return open_bus

    @property
    def requests(self) -> list[can.Message]:
        return [m for m in list(self.ecu.received) if m.arbitration_id in REQUEST_IDS]

    def close(self) -> None:
        self._stop.set()
        self._server.join(timeout=2.0)
        self._ecu_bus.shutdown()


@pytest.fixture
def wire() -> Iterator[Wire]:
    w = Wire()
    yield w
    w.close()


class Collector:
    """Runs ``session.records()`` on a thread, as the pipeline would."""

    def __init__(self, session: LiveCanSession) -> None:
        self.session = session
        self.records: list[Record] = []
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            for rec in self.session.records():
                self.records.append(rec)
        except BaseException as exc:
            self.error = exc

    def finish(self) -> list[Record]:
        self.session.stop()
        self.thread.join(timeout=5.0)
        assert not self.thread.is_alive()
        assert self.error is None
        return self.records


def _wait_for(predicate: Callable[[], bool], timeout_s: float) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _open(wire: Wire, ip: IpStub | None = None, **factory: Any) -> LiveCanSession:
    return open_live_can_session(
        _cfg(),
        MonotonicClock(),
        runner=ip or IpStub(),
        bus_factory=wire.factory(**factory),
        system="Linux",
    )


# Refusals before any bus opens.


def test_session_refuses_while_polling_is_off(wire: Wire) -> None:
    ip = IpStub()
    with pytest.raises(ObdPollingOff, match="obd_polling"):
        open_live_can_session(BASE, MonotonicClock(), runner=ip, bus_factory=wire.factory())
    with pytest.raises(ObdPollingOff, match="capture interface"):
        open_live_can_session(
            _cfg(request_interface="can0"),
            MonotonicClock(),
            runner=ip,
            bus_factory=wire.factory(),
            system="Linux",
        )
    assert ip.calls == []
    assert wire.opened == []


def test_session_is_unavailable_off_linux(wire: Wire) -> None:
    ip = IpStub()
    with pytest.raises(LiveCanUnavailable, match="not linux") as exc:
        open_live_can_session(
            _cfg(), MonotonicClock(), runner=ip, bus_factory=wire.factory(), system="Darwin"
        )
    assert isinstance(exc.value, RuntimeError)
    assert str(exc.value) == "Live CAN is off: can0 is not confirmed listen-only (not linux)."
    assert ip.calls == []
    assert wire.opened == []


@pytest.mark.skipif(platform.system() == "Linux", reason="checks the real non-Linux path")
def test_session_is_unavailable_on_this_machine() -> None:
    with pytest.raises(LiveCanUnavailable, match="not linux"):
        open_live_can_session(_cfg(), MonotonicClock())


def test_session_refuses_when_can0_is_not_listen_only(wire: Wire) -> None:
    ip = IpStub(can0=lambda _: _link("can0", ["ONE-SHOT"]))
    with pytest.raises(LiveCanUnavailable, match=r"can0 is not confirmed listen-only"):
        _open(wire, ip)
    assert wire.opened == []


@pytest.mark.parametrize(
    ("can1", "reason"),
    [
        (_link("can1", ["BERR-REPORTING"]), "one-shot is off"),
        (_link("can1", ["ONE-SHOT"], restart_ms=100), "restart-ms is 100, not 0"),
        (_link("can1", ["ONE-SHOT"], bitrate=250000), "bitrate is 250000, not 500000"),
        (_link("can1", ["ONE-SHOT"], up=False), "interface is down"),
        (_link("can1", ["LISTEN-ONLY"]), "listen-only is on, so every request would be dropped"),
    ],
)
def test_session_refuses_when_can1_is_not_one_shot(
    wire: Wire, can1: dict[str, Any], reason: str
) -> None:
    ip = IpStub(can1=can1)
    with pytest.raises(LiveCanUnavailable) as exc:
        _open(wire, ip)
    assert str(exc.value) == f"Live CAN is off: can1 is not ready for requests ({reason})."
    assert wire.opened == []  # checked before the request bus opens
    assert ["ip", "-details", "-json", "link", "show", "can1"] in ip.calls


def test_session_refuses_when_the_can1_error_counters_are_unreadable(wire: Wire) -> None:
    class NoCounters(IpStub):
        def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
            if "-statistics" in argv:
                return 1, ""
            return super().__call__(argv)

    with pytest.raises(LiveCanUnavailable, match="error counters cannot be read"):
        _open(wire, NoCounters())
    assert wire.opened == []


@pytest.mark.parametrize(("berr", "counts"), [((8, 0), "tx 8, rx 0"), ((0, 5), "tx 0, rx 5")])
def test_session_refuses_when_the_can1_error_counters_are_not_zero(
    wire: Wire, berr: tuple[int, int], counts: str
) -> None:
    # The counters count down on their own after errors, and the link watch reads any fall as
    # a reset, so a session that started above 0 would turn TX off by itself.
    ip = IpStub(can1_counters=[berr])
    with pytest.raises(LiveCanUnavailable) as exc:
        _open(wire, ip)
    assert str(exc.value) == (
        f"Live CAN is off: the can1 error counters are not 0 ({counts}). They count down on "
        "their own. Start again once both read 0."
    )
    assert wire.opened == []


def test_session_refuses_a_rate_over_the_hard_ceiling(wire: Wire) -> None:
    raw = {k: dict(v) if isinstance(v, Mapping) else v for k, v in _cfg().raw.items()}
    raw["obd"]["max_request_hz"] = 50.0
    with pytest.raises(ObdPollingOff, match="rate_hz"):
        open_live_can_session(
            Config(raw),
            MonotonicClock(),
            runner=IpStub(),
            bus_factory=wire.factory(),
            system="Linux",
        )
    assert wire.opened == []


# The poller cannot run on a real bus outside a session.


def test_obd_poller_outside_a_session_refuses() -> None:
    clock = MonotonicClock()
    guard = TxGuard.from_config(BASE, clock)
    real_like = cast(can.BusABC, object())  # stands in for a socketcan bus
    with pytest.raises(ObdPollingOff, match="open_live_can_session"):
        ObdPoller(real_like, guard, BASE, clock)
    with pytest.raises(ObdPollingOff, match="virtual bus"):
        ObdPoller(real_like, guard, BASE, clock, virtual_bus_for_tests=True)
    bus = can.Bus(interface="virtual", channel=f"photoncar-test-{uuid.uuid4().hex}")
    try:
        with pytest.raises(ObdPollingOff, match="open_live_can_session"):
            ObdPoller(bus, guard, BASE, clock)
        ObdPoller(bus, guard, BASE, clock, virtual_bus_for_tests=True)
    finally:
        bus.shutdown()


def test_a_session_cannot_be_built_by_hand() -> None:
    clock = MonotonicClock()
    with pytest.raises(LiveCanUnavailable, match="open_live_can_session"):
        LiveCanSession(
            object(),
            cfg=_cfg(),
            clock=clock,
            guard=TxGuard.from_config(BASE, clock),
            capture_if="can0",
            request_if="can1",
            capture_bus=cast(can.BusABC, object()),
            request_bus=cast(can.BusABC, object()),
            check_capture=lambda _: pytest.fail("no check may run"),
            read_request_link=lambda: pytest.fail("no can1 state may be read"),
            counters=ErrorCounters(0, 0),
        )


# Running sessions.


def test_live_session_polls_and_audits_every_frame(wire: Wire) -> None:
    ip = IpStub()
    session = _open(wire, ip)
    assert session.meta.kind == "live"
    run = Collector(session)
    assert _wait_for(lambda: len(wire.requests) >= 8, 3.0)
    records = run.finish()
    guard = session.guard
    assert not guard.disabled, guard.reason
    frames = [r for r in records if isinstance(r, CanFrame)]
    requests = [f for f in frames if f.arb_id in REQUEST_IDS]
    replies = [f for f in frames if 0x7E8 <= f.arb_id <= 0x7EF]
    assert len(requests) == len(wire.requests) >= 8  # the capture bus saw every request
    assert replies
    assert guard.audited == len(frames)  # every can0 frame went through the audit
    for f in requests:
        validate_frame(
            can.Message(arbitration_id=f.arb_id, is_extended_id=f.is_ext, data=f.data),
            guard.allowed_pids,
        )
    polls = [r for r in records if isinstance(r, ObdPoll)]
    assert [p.pid for p in polls[:3]] == [0x00, 0x20, 0x40]
    assert all(p.status == "ok" for p in polls)
    assert {s.signal for s in records if isinstance(s, Sample)} >= {"obd.rpm"}
    times = [record_time(r) for r in records]
    assert times == sorted(times)


def test_polling_never_starts_without_capture_wired_to_the_audit(wire: Wire) -> None:
    # can0 passes the check at open, then fails the re-check when capture starts.
    def can0(n: int) -> dict[str, Any]:
        return CAN0 if n <= 2 else _link("can0", ["ONE-SHOT"])

    session = _open(wire, IpStub(can0=can0))
    run = Collector(session)
    assert _wait_for(lambda: not run.thread.is_alive(), 3.0)  # records() ends on its own
    records = run.finish()
    assert records == []
    assert wire.requests == []
    assert session.guard.disabled
    assert session.guard.reason == "audit: capture on the listen-only bus stopped"


def test_a_capture_bus_that_misses_the_wire_stops_tx_after_1_request(wire: Wire) -> None:
    # can0 opens on a different channel, so it never sees what can1 sends.
    session = _open(wire, capture_channel=f"photoncar-test-{uuid.uuid4().hex}")
    run = Collector(session)
    assert _wait_for(lambda: session.guard.disabled, 3.0)
    assert _wait_for(lambda: not session.polling, 3.0)
    time.sleep(0.3)
    run.finish()
    assert len(wire.requests) == 1
    assert session.guard.reason is not None
    assert "was not seen on the capture bus" in session.guard.reason


def test_an_error_frame_on_can0_stops_tx(wire: Wire) -> None:
    session = _open(wire, tap=True)
    run = Collector(session)
    assert _wait_for(lambda: len(wire.requests) >= 4, 3.0)
    assert wire.tap is not None
    wire.tap.inject.append(can.Message(arbitration_id=0x20000004, is_error_frame=True))
    assert _wait_for(lambda: session.guard.disabled, 2.0)
    assert _wait_for(lambda: not session.polling, 2.0)
    sent = len(wire.requests)
    time.sleep(0.3)
    run.finish()
    assert len(wire.requests) == sent
    assert session.guard.reason is not None
    assert "error frame on the capture bus" in session.guard.reason


def test_a_rise_in_the_can1_error_counters_stops_tx(wire: Wire) -> None:
    # Reads: at open, right before the first request, then the watch's ticks.
    ip = IpStub(can1_counters=[(0, 0), (0, 0), (8, 0)])
    session = _open(wire, ip)
    run = Collector(session)
    assert _wait_for(lambda: len(wire.requests) >= 3, 3.0)
    assert _wait_for(lambda: session.guard.disabled, 3.0)  # read at about 1 Hz
    assert _wait_for(lambda: not session.polling, 2.0)
    sent = len(wire.requests)
    time.sleep(0.3)
    records = run.finish()
    assert len(wire.requests) == sent
    assert session.guard.reason == "can1 error counters rose: tx errors 0 to 8"
    stats = [c for c in ip.calls if "-statistics" in c]
    assert stats[0] == ["ip", "-details", "-statistics", "-json", "link", "show", "can1"]
    assert any(isinstance(r, CanFrame) for r in records)  # capture kept running


class ForkFails(IpStub):
    """From the ``fail_from``-th can1 statistics read on, the read fails as ``subprocess.run``
    does when fork hits EAGAIN. Read 1 is at open, read 2 right before the first request, and
    read 3 on the watch's first tick."""

    def __init__(self, fail_from: int = 3) -> None:
        super().__init__()
        self.fail_from = fail_from

    def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        out = super().__call__(argv)
        with self._lock:
            n = sum(1 for c in self.calls if "-statistics" in c)
        if "-statistics" in argv and n >= self.fail_from:
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
        return out


def test_a_failed_can1_error_counter_read_stops_tx(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H2: an OSError is not a LinkQueryError, and it must not end the watch while polling
    # goes on with no error counter check.
    monkeypatch.setattr(obd, "LINK_WATCH_S", 0.1)
    session = _open(wire, ForkFails())
    run = Collector(session)
    assert _wait_for(lambda: session.guard.disabled, 3.0)
    assert _wait_for(lambda: not session.polling, 2.0)
    sent = len(wire.requests)
    time.sleep(0.3)
    records = run.finish()
    assert len(wire.requests) == sent
    assert session.guard.reason == (
        f"CAN link watch failed: [Errno {errno.EAGAIN}] Resource temporarily unavailable"
    )
    assert any(isinstance(r, CanFrame) for r in records)  # capture kept running


def test_a_failed_can1_read_before_the_first_request_stops_tx(wire: Wire) -> None:
    session = _open(wire, ForkFails(fail_from=2))
    run = Collector(session)
    assert _wait_for(lambda: session.guard.disabled, 3.0)
    assert _wait_for(lambda: not session.polling, 2.0)
    time.sleep(0.3)
    run.finish()
    assert wire.requests == []
    assert session.guard.reason == (
        f"CAN link watch failed: [Errno {errno.EAGAIN}] Resource temporarily unavailable"
    )


def test_polling_stops_when_the_link_watch_ends(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A watch that ends mid-session without turning TX off still stops every later request.
    def quits_early(self: LiveCanSession) -> None:
        _wait_for(lambda: len(wire.requests) >= 3, 3.0)

    monkeypatch.setattr(LiveCanSession, "_watch_links", quits_early)
    session = _open(wire)
    run = Collector(session)
    assert _wait_for(lambda: session.guard.disabled, 3.0)
    assert _wait_for(lambda: not session.polling, 2.0)
    sent = len(wire.requests)
    time.sleep(0.3)
    run.finish()
    assert sent >= 3
    assert len(wire.requests) == sent
    assert session.guard.reason == "CAN link watch stopped"


def test_any_exit_of_the_link_watch_stops_tx(wire: Wire, monkeypatch: pytest.MonkeyPatch) -> None:
    # Even an exit that no except clause catches turns TX off on the way out.
    class ExitsOnRead(IpStub):
        def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
            out = super().__call__(argv)
            with self._lock:
                n = sum(1 for c in self.calls if "-statistics" in c)
            if n >= 2:
                raise SystemExit(1)
            return out

    monkeypatch.setattr(obd, "LINK_WATCH_S", 0.01)
    session = _open(wire, ExitsOnRead())
    try:
        with pytest.raises(SystemExit):
            session._watch_links()
        assert session.guard.disabled
        assert session.guard.reason == "CAN link watch stopped"
    finally:
        session.stop()


# Both links are judged again right before the first request, not 1 s into polling.


def test_can0_is_rechecked_right_before_the_first_request(wire: Wire) -> None:
    # 3 can0 reads pass before polling (open, source, capture start); the 4th fails.
    ip = IpStub(can0=lambda n: CAN0 if n <= 3 else _link("can0", ["ONE-SHOT"]))
    session = _open(wire, ip)  # the watch ticks every LINK_WATCH_S = 1 s
    run = Collector(session)
    assert _wait_for(lambda: session.guard.disabled, 3.0)
    assert _wait_for(lambda: not session.polling, 2.0)
    time.sleep(0.3)
    run.finish()
    assert wire.requests == []
    assert session.guard.reason == "can0 is no longer confirmed listen-only (listen-only is off)"


def test_can1_is_rechecked_right_before_the_first_request(wire: Wire) -> None:
    ip = IpStub(can1_counters=[(0, 0), (0, 3)])  # the 2nd read is the one before the request
    session = _open(wire, ip)
    run = Collector(session)
    assert _wait_for(lambda: session.guard.disabled, 3.0)
    assert _wait_for(lambda: not session.polling, 2.0)
    time.sleep(0.3)
    run.finish()
    assert wire.requests == []
    assert session.guard.reason == "can1 error counters rose: rx errors 0 to 3"


def test_the_first_request_goes_out_after_a_passing_recheck(wire: Wire) -> None:
    ip = IpStub()
    session = _open(wire, ip)
    run = Collector(session)
    assert _wait_for(lambda: len(wire.requests) >= 1, 3.0)
    run.finish()
    # Read at open, then again before the first request, before the watch's first tick.
    stats = [c for c in ip.calls if c[-1] == "can1" and "-statistics" in c]
    assert len(stats) >= 2
    assert sum(1 for c in ip.calls if c[-1] == "can0") >= 4


# The link watch re-judges both interfaces on every tick (plan section 1, H2).


class Flip:
    """Hands out ``good`` until ``flip()``, then ``bad``: a link changed mid-session."""

    def __init__(self, good: dict[str, Any], bad: dict[str, Any]) -> None:
        self.good, self.bad = good, bad
        self.flipped = threading.Event()

    def __call__(self, _n: int) -> dict[str, Any]:
        return self.bad if self.flipped.is_set() else self.good

    def flip(self) -> None:
        self.flipped.set()


def _run_until_tx_off(wire: Wire, ip: IpStub, flip: Flip) -> tuple[LiveCanSession, list[Record]]:
    """Start a session, change a link once requests flow, and return once TX is off."""
    session = _open(wire, ip)
    run = Collector(session)
    assert _wait_for(lambda: len(wire.requests) >= 3, 3.0)
    assert not session.guard.disabled, session.guard.reason
    flip.flip()
    assert _wait_for(lambda: session.guard.disabled, 3.0)
    assert _wait_for(lambda: not session.polling, 2.0)
    sent = len(wire.requests)
    time.sleep(0.3)
    records = run.finish()
    assert len(wire.requests) == sent
    assert any(isinstance(r, CanFrame) for r in records)  # capture kept running
    return session, records


CAN1_CHANGES = [
    (_link("can1", ["BERR-REPORTING"]), "one-shot is off"),
    (_link("can1", ["ONE-SHOT"], restart_ms=100), "restart-ms is 100, not 0"),
    (
        _link("can1", ["ONE-SHOT", "LISTEN-ONLY"]),
        "listen-only is on, so every request would be dropped",
    ),
    (_link("can1", ["ONE-SHOT"], bitrate=250000), "bitrate is 250000, not 500000"),
    (_link("can1", ["ONE-SHOT"], up=False), "interface is down"),
]


@pytest.mark.parametrize(("bad", "reason"), CAN1_CHANGES)
def test_a_can1_mode_change_mid_session_stops_tx(
    wire: Wire, monkeypatch: pytest.MonkeyPatch, bad: dict[str, Any], reason: str
) -> None:
    monkeypatch.setattr(obd, "LINK_WATCH_S", 0.05)
    flip = Flip(CAN1, bad)
    session, _ = _run_until_tx_off(wire, IpStub(can1_stats=flip), flip)
    assert session.guard.reason == f"can1 is no longer ready for requests ({reason})"


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        (_link("can0", ["ONE-SHOT"]), "listen-only is off"),
        (_link("can0", ["LISTEN-ONLY"], up=False), "interface is down"),
    ],
)
def test_can0_leaving_listen_only_mid_session_stops_tx(
    wire: Wire, monkeypatch: pytest.MonkeyPatch, bad: dict[str, Any], reason: str
) -> None:
    monkeypatch.setattr(obd, "LINK_WATCH_S", 0.05)
    flip = Flip(CAN0, bad)
    ip = IpStub(can0=flip)
    session, _ = _run_until_tx_off(wire, ip, flip)
    assert session.guard.reason == f"can0 is no longer confirmed listen-only ({reason})"
    # 3 reads before polling (open, source, capture start); the watch made the rest.
    assert sum(1 for c in ip.calls if c[-1] == "can0") >= 4


# A fall in the tx or rx error counter cannot happen in a session: it starts only at 0
# (test_session_refuses_when_the_can1_error_counters_are_not_zero), and any rise turns TX off
# first. The fall check itself is covered in test_listen_only.py (test_counters_fell,
# test_recheck_request_link_faults_on_any_counter_change); in a session, a reset of the
# restart count is the fall that can still happen.


def test_a_reset_of_the_can1_restart_count_stops_tx(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(obd, "LINK_WATCH_S", 0.05)
    good = _link("can1", ["ONE-SHOT"])
    good["linkinfo"]["info_xstats"]["restarts"] = 1
    flip = Flip(good, _link("can1", ["ONE-SHOT"]))
    session, _ = _run_until_tx_off(wire, IpStub(can1_stats=flip), flip)
    assert session.guard.reason == "can1 error counters fell: restarts 1 to 0"


def test_the_link_watch_rereads_both_interfaces_every_tick(
    wire: Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(obd, "LINK_WATCH_S", 0.05)
    ip = IpStub()
    session = _open(wire, ip)
    run = Collector(session)
    assert _wait_for(lambda: len(wire.requests) >= 3, 3.0)
    time.sleep(0.5)
    run.finish()
    assert not session.guard.disabled, session.guard.reason
    stats = [c for c in ip.calls if c[-1] == "can1" and "-statistics" in c]
    can0 = [c for c in ip.calls if c[-1] == "can0"]
    assert len(stats) >= 5  # 1 at open, then about 1 per 0.05 s tick
    assert len(can0) >= 5
    assert can0[-1] == ["ip", "-details", "-json", "link", "show", "can0"]


def test_stop_before_records_closes_both_buses(wire: Wire) -> None:
    closed: list[str] = []

    class ClosingBus:
        def __init__(self, ifname: str) -> None:
            self.ifname = ifname
            self.inner = can.Bus(interface="virtual", channel=wire.channel)

        def recv(self, timeout: float | None = None) -> can.Message | None:
            return self.inner.recv(timeout=timeout)

        def send(self, msg: can.Message, timeout: float | None = None) -> None:
            self.inner.send(msg, timeout=timeout)

        def shutdown(self) -> None:
            closed.append(self.ifname)
            self.inner.shutdown()

    def factory(ifname: str) -> Any:
        wire.opened.append(ifname)
        return ClosingBus(ifname)

    session = open_live_can_session(
        _cfg(), MonotonicClock(), runner=IpStub(), bus_factory=factory, system="Linux"
    )
    assert wire.opened == ["can0", "can1"]
    session.stop()
    session.stop()
    assert sorted(closed) == ["can0", "can1"]
    assert list(session.records()) == []
    assert wire.requests == []


def test_stop_called_again_while_stopping_returns(wire: Wire) -> None:
    # The CLI calls stop() from its Ctrl+C handler, which runs on the thread that may already
    # be inside stop() (a second Ctrl+C, or one while records() closes the session).
    sessions: list[LiveCanSession] = []
    closed: list[str] = []

    class ReenteringBus:
        def __init__(self, ifname: str) -> None:
            self.ifname = ifname
            self.inner = can.Bus(interface="virtual", channel=wire.channel)

        def recv(self, timeout: float | None = None) -> can.Message | None:
            return self.inner.recv(timeout=timeout)

        def send(self, msg: can.Message, timeout: float | None = None) -> None:
            self.inner.send(msg, timeout=timeout)

        def shutdown(self) -> None:
            sessions[0].stop()  # the second Ctrl+C lands while the first stop closes buses
            closed.append(self.ifname)
            self.inner.shutdown()

    def factory(ifname: str) -> Any:
        return ReenteringBus(ifname)

    session = open_live_can_session(
        _cfg(), MonotonicClock(), runner=IpStub(), bus_factory=factory, system="Linux"
    )
    sessions.append(session)
    stopper = threading.Thread(target=session.stop, daemon=True)
    stopper.start()
    stopper.join(timeout=2.0)
    assert not stopper.is_alive(), "stop() hung when called again during stop()"
    assert sorted(closed) == ["can0", "can1"]
