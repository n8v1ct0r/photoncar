"""Interface checks (can0 listen-only, can1 one-shot and error counters) and SocketCAN capture."""

from __future__ import annotations

import json
import platform
import threading
import uuid
from collections import deque
from collections.abc import Iterator, Sequence
from typing import Any, cast

import can
import pytest

from photoncar.clock import SimClock
from photoncar.records import CanFrame
from photoncar.safety.listen_only import (
    LinkQueryError,
    ListenOnlyStatus,
    check_listen_only,
    find_link,
    parse_ip_link_json,
)
from photoncar.safety.request_link import (
    ErrorCounters,
    LinkFault,
    RequestLinkStatus,
    check_one_shot,
    counters_fell,
    counters_rose,
    judge_one_shot,
    parse_error_counters,
    read_error_counters,
    recheck_request_link,
)
from photoncar.sources.socketcan import ListenOnlyError, SocketCanSource

S = 1_000_000_000
REAL_OFFSET_NS = 1_695_500_000 * S
"""CLOCK_REALTIME minus CLOCK_MONOTONIC in the timestamp tests."""


def _can_link(
    ifname: str, *, flags: list[str], operstate: str, ctrlmode: Any, state: str
) -> dict[str, Any]:
    """The shape of one entry of `ip -details -json link show` for an mcp251xfd channel."""
    info_data: dict[str, Any] = {
        "state": state,
        "berr_counter": {"tx": 0, "rx": 0},
        "restart_ms": 0,
        "bittiming": {
            "bitrate": 500000,
            "sample_point": "0.875",
            "tq": 25,
            "prop_seg": 34,
            "phase_seg1": 35,
            "phase_seg2": 10,
            "sjw": 1,
            "brp": 1,
        },
        "bittiming_const": {
            "name": "mcp251xfd",
            "tseg1": {"min": 2, "max": 256},
            "tseg2": {"min": 1, "max": 128},
            "sjw": {"min": 1, "max": 128},
            "brp": {"min": 1, "max": 256},
            "brp_inc": 1,
        },
        "clock": 40000000,
    }
    if ctrlmode is not None:
        info_data["ctrlmode"] = ctrlmode
    return {
        "ifindex": 3,
        "ifname": ifname,
        "flags": flags,
        "mtu": 16,
        "qdisc": "pfifo_fast",
        "operstate": operstate,
        "linkmode": "DEFAULT",
        "group": "default",
        "txqlen": 10,
        "link_type": "can",
        "promiscuity": 0,
        "allmulti": 0,
        "min_mtu": 0,
        "max_mtu": 0,
        "linkinfo": {"info_kind": "can", "info_data": info_data},
        "num_tx_queues": 1,
        "num_rx_queues": 1,
        "gso_max_size": 65536,
        "gso_max_segs": 65535,
        "parentbus": "spi",
        "parentdev": "spi0.0",
    }


UP = ["NOARP", "UP", "LOWER_UP", "ECHO"]
LISTEN_ONLY_UP = json.dumps(
    [_can_link("can0", flags=UP, operstate="UP", ctrlmode=["LISTEN-ONLY"], state="ERROR-ACTIVE")]
)
NORMAL_UP = json.dumps(
    [_can_link("can1", flags=UP, operstate="UP", ctrlmode=["ONE-SHOT"], state="ERROR-ACTIVE")]
)
NO_CTRLMODE_UP = json.dumps(
    [_can_link("can0", flags=UP, operstate="UP", ctrlmode=None, state="ERROR-ACTIVE")]
)
LISTEN_ONLY_DOWN = json.dumps(
    [
        _can_link(
            "can0",
            flags=["NOARP", "ECHO"],
            operstate="DOWN",
            ctrlmode=["LISTEN-ONLY"],
            state="STOPPED",
        )
    ]
)
LISTEN_ONLY_STRING = json.dumps(
    [
        _can_link(
            "can0",
            flags=UP,
            operstate="UP",
            ctrlmode="LISTEN-ONLY,BERR-REPORTING",
            state="ERROR-ACTIVE",
        )
    ]
)
VCAN = json.dumps(
    [
        {
            "ifindex": 5,
            "ifname": "vcan0",
            "flags": ["NOARP", "UP", "LOWER_UP"],
            "mtu": 72,
            "operstate": "UNKNOWN",
            "link_type": "none",
            "linkinfo": {"info_kind": "vcan"},
        }
    ]
)


@pytest.mark.parametrize(
    ("text", "ifname", "ok", "reason"),
    [
        (LISTEN_ONLY_UP, "can0", True, "listen-only is on"),
        (LISTEN_ONLY_STRING, "can0", True, "listen-only is on"),
        (NORMAL_UP, "can1", False, "listen-only is off"),
        (NO_CTRLMODE_UP, "can0", False, "listen-only is off"),
        (LISTEN_ONLY_DOWN, "can0", False, "interface is down"),
        (VCAN, "vcan0", False, "not a CAN interface"),
        (LISTEN_ONLY_UP, "can1", False, "interface not found"),
        ("{not json", "can0", False, "unreadable ip output"),
        ('{"ifname": "can0"}', "can0", False, "unreadable ip output"),
    ],
)
def test_parse_captured_ip_output(text: str, ifname: str, ok: bool, reason: str) -> None:
    assert parse_ip_link_json(text, ifname) == ListenOnlyStatus(ok, reason)


class FakeIp:
    """Stands in for the `ip` binary: returns captured output and records the argv."""

    def __init__(self, code: int, out: str) -> None:
        self.code = code
        self.out = out
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        self.calls.append(list(argv))
        return self.code, self.out


def test_check_runs_a_read_only_ip_query() -> None:
    ip = FakeIp(0, LISTEN_ONLY_UP)
    status = check_listen_only("can0", runner=ip, system="Linux")
    assert status == ListenOnlyStatus(True, "listen-only is on")
    assert ip.calls == [["ip", "-details", "-json", "link", "show", "can0"]]


def test_check_reports_ip_failures() -> None:
    assert check_listen_only("can0", runner=FakeIp(1, ""), system="Linux") == ListenOnlyStatus(
        False, "ip exited with code 1"
    )

    def missing(argv: Sequence[str]) -> tuple[int, str]:
        raise FileNotFoundError("ip")

    assert check_listen_only("can0", runner=missing, system="Linux").reason == (
        "ip command not found"
    )


def test_check_refuses_odd_interface_names_without_running_ip() -> None:
    ip = FakeIp(0, LISTEN_ONLY_UP)
    assert check_listen_only("can0 type can", runner=ip, system="Linux").reason == (
        "invalid interface name"
    )
    assert ip.calls == []


def test_check_is_false_off_linux() -> None:
    ip = FakeIp(0, LISTEN_ONLY_UP)
    assert check_listen_only("can0", runner=ip, system="Darwin") == ListenOnlyStatus(
        False, "not linux"
    )
    assert ip.calls == []


@pytest.mark.skipif(platform.system() == "Linux", reason="checks the real non-Linux path")
def test_check_on_this_machine_is_not_linux() -> None:
    assert check_listen_only("can0") == ListenOnlyStatus(False, "not linux")


# The request interface (can1): one-shot at startup, error counters while polling.


def _can1(
    *,
    ctrlmode: Any = ("ONE-SHOT",),
    restart_ms: Any = 0,
    bitrate: Any = 500000,
    up: bool = True,
    operstate: str | None = None,
    berr: tuple[int, int] = (0, 0),
    xstats: dict[str, int] | None = None,
) -> dict[str, Any]:
    flags = UP if up else ["NOARP", "ECHO"]
    state = operstate or ("UP" if up else "DOWN")
    link = _can_link(
        "can1",
        flags=flags,
        operstate=state,
        ctrlmode=list(ctrlmode) if isinstance(ctrlmode, tuple) else ctrlmode,
        state="ERROR-ACTIVE" if up else "STOPPED",
    )
    info_data = link["linkinfo"]["info_data"]
    info_data["berr_counter"] = {"tx": berr[0], "rx": berr[1]}
    if restart_ms is None:
        del info_data["restart_ms"]
    else:
        info_data["restart_ms"] = restart_ms
    if bitrate is None:
        del info_data["bittiming"]["bitrate"]
    else:
        info_data["bittiming"]["bitrate"] = bitrate
    if xstats is not None:
        link["linkinfo"]["info_xstats"] = xstats
    return link


def test_one_shot_accepts_the_provisioned_can1() -> None:
    assert judge_one_shot(_can1(), 500000) == RequestLinkStatus(True, "one-shot is on")
    modes = _can1(ctrlmode="ONE-SHOT,BERR-REPORTING")
    assert judge_one_shot(modes, 500000).ok


@pytest.mark.parametrize(
    ("link", "reason"),
    [
        (_can1(ctrlmode=("BERR-REPORTING",)), "one-shot is off"),
        (_can1(ctrlmode=None), "one-shot is off"),
        (_can1(restart_ms=100), "restart-ms is 100, not 0"),
        (_can1(restart_ms=None), "restart-ms is missing"),
        (_can1(bitrate=250000), "bitrate is 250000, not 500000"),
        (_can1(bitrate=None), "bitrate is missing"),
        (_can1(up=False), "interface is down"),
        (_can1(operstate="DOWN"), "interface is down"),  # bus-off: flags UP, carrier off
        (_can1(ctrlmode=("LISTEN-ONLY",)), "listen-only is on, so every request would be dropped"),
        (
            _can1(ctrlmode=("LISTEN-ONLY", "ONE-SHOT")),
            "listen-only is on, so every request would be dropped",
        ),
        (json.loads(VCAN)[0], "not a CAN interface"),
    ],
)
def test_one_shot_refuses(link: dict[str, Any], reason: str) -> None:
    assert judge_one_shot(link, 500000) == RequestLinkStatus(False, reason)


def test_one_shot_check_runs_a_read_only_ip_query() -> None:
    ip = FakeIp(0, json.dumps([_can1()]))
    assert check_one_shot("can1", 500000, runner=ip, system="Linux").ok
    assert ip.calls == [["ip", "-details", "-json", "link", "show", "can1"]]
    wrong = FakeIp(0, json.dumps([_can1(restart_ms=100)]))
    assert check_one_shot("can1", 500000, runner=wrong, system="Linux") == RequestLinkStatus(
        False, "restart-ms is 100, not 0"
    )


def test_one_shot_check_is_false_off_linux_and_on_ip_failure() -> None:
    ip = FakeIp(0, json.dumps([_can1()]))
    assert check_one_shot("can1", 500000, runner=ip, system="Darwin") == RequestLinkStatus(
        False, "not linux"
    )
    assert ip.calls == []
    assert check_one_shot("can1", 500000, runner=FakeIp(2, ""), system="Linux").reason == (
        "ip exited with code 2"
    )
    assert check_one_shot("can1", 500000, runner=FakeIp(0, LISTEN_ONLY_UP), system="Linux") == (
        RequestLinkStatus(False, "interface not found")
    )


def test_error_counters_are_read_with_statistics() -> None:
    xstats = {"restarts": 0, "bus_error": 3, "arbitration_lost": 0, "error_warning": 1}
    ip = FakeIp(0, json.dumps([_can1(berr=(8, 2), xstats=xstats)]))
    counters = read_error_counters("can1", runner=ip, system="Linux")
    assert ip.calls == [["ip", "-details", "-statistics", "-json", "link", "show", "can1"]]
    assert counters == ErrorCounters(
        8, 2, (("bus_error", 3), ("error_warning", 1), ("restarts", 0))
    )


def test_error_counters_missing_raise() -> None:
    link = _can1()
    del link["linkinfo"]["info_data"]["berr_counter"]
    with pytest.raises(LinkQueryError, match="no error counters"):
        parse_error_counters(link)
    with pytest.raises(LinkQueryError, match="not a CAN interface"):
        parse_error_counters(json.loads(VCAN)[0])
    with pytest.raises(LinkQueryError, match="not linux"):
        read_error_counters("can1", runner=FakeIp(0, "[]"), system="Darwin")


def test_counters_rose() -> None:
    zero = ErrorCounters(0, 0, (("bus_error", 0), ("bus_off", 0)))
    assert counters_rose(zero, zero) is None
    assert counters_rose(ErrorCounters(16, 8), ErrorCounters(15, 7)) is None  # decay is fine
    assert counters_rose(zero, ErrorCounters(8, 0, (("bus_error", 0), ("bus_off", 0)))) == (
        "tx errors 0 to 8"
    )
    assert counters_rose(zero, ErrorCounters(0, 1, (("bus_error", 1), ("bus_off", 0)))) == (
        "rx errors 0 to 1, bus error 0 to 1"
    )
    # A rise in the kernel event count alone, after the TX counter already decayed back.
    assert counters_rose(zero, ErrorCounters(0, 0, (("bus_error", 2), ("bus_off", 0)))) == (
        "bus error 0 to 2"
    )


def test_counters_fell() -> None:
    zero = ErrorCounters(0, 0, (("bus_error", 0), ("bus_off", 0)))
    assert counters_fell(zero, zero) is None
    assert counters_fell(zero, ErrorCounters(8, 0, (("bus_error", 1), ("bus_off", 0)))) is None
    assert counters_fell(ErrorCounters(8, 0), ErrorCounters(0, 0)) == "tx errors 8 to 0"
    assert counters_fell(ErrorCounters(16, 8), ErrorCounters(15, 7)) == (
        "tx errors 16 to 15, rx errors 8 to 7"
    )
    restarted = ErrorCounters(0, 0, (("restarts", 1), ("bus_off", 1)))
    reset = ErrorCounters(0, 0, (("restarts", 0), ("bus_off", 1)))
    assert counters_fell(restarted, reset) == "restarts 1 to 0"
    assert counters_fell(restarted, ErrorCounters(0, 0, (("restarts", 1),))) == (
        "bus off is no longer reported"
    )


def test_recheck_request_link_passes_an_unchanged_can1() -> None:
    before = ErrorCounters(0, 0, (("bus_error", 0), ("error_warning", 0), ("restarts", 0)))
    link = _can1(xstats={"restarts": 0, "bus_error": 0, "error_warning": 0})
    assert recheck_request_link("can1", link, 500000, before) == before


@pytest.mark.parametrize(
    ("link", "reason"),
    [
        (_can1(ctrlmode=("BERR-REPORTING",)), "one-shot is off"),
        (_can1(restart_ms=100), "restart-ms is 100, not 0"),
        (_can1(bitrate=250000), "bitrate is 250000, not 500000"),
        (_can1(up=False), "interface is down"),
        (_can1(operstate="DOWN"), "interface is down"),
        (_can1(ctrlmode=("LISTEN-ONLY",)), "listen-only is on, so every request would be dropped"),
        (json.loads(VCAN)[0], "not a CAN interface"),
    ],
)
def test_recheck_request_link_faults_on_a_mode_change(link: dict[str, Any], reason: str) -> None:
    with pytest.raises(LinkFault) as exc:
        recheck_request_link("can1", link, 500000, ErrorCounters(0, 0))
    assert exc.value.reason == f"can1 is no longer ready for requests ({reason})"


@pytest.mark.parametrize(
    ("before", "link", "reason"),
    [
        (ErrorCounters(0, 0), _can1(berr=(8, 0)), "can1 error counters rose: tx errors 0 to 8"),
        (ErrorCounters(8, 0), _can1(berr=(0, 0)), "can1 error counters fell: tx errors 8 to 0"),
        (ErrorCounters(0, 3), _can1(berr=(0, 2)), "can1 error counters fell: rx errors 3 to 2"),
        (
            ErrorCounters(0, 0, (("restarts", 2),)),
            _can1(xstats={"restarts": 0}),
            "can1 error counters fell: restarts 2 to 0",
        ),
        (
            ErrorCounters(0, 0, (("bus_off", 1),)),
            _can1(xstats={"restarts": 0}),
            "can1 error counters fell: bus off is no longer reported",
        ),
    ],
)
def test_recheck_request_link_faults_on_any_counter_change(
    before: ErrorCounters, link: dict[str, Any], reason: str
) -> None:
    with pytest.raises(LinkFault) as exc:
        recheck_request_link("can1", link, 500000, before)
    assert exc.value.reason == reason


def test_recheck_request_link_faults_without_counters() -> None:
    link = _can1()
    del link["linkinfo"]["info_data"]["berr_counter"]
    with pytest.raises(LinkFault) as exc:
        recheck_request_link("can1", link, 500000, ErrorCounters(0, 0))
    assert exc.value.reason == "can1 error counters are unreadable: no error counters in ip output"


def test_find_link_errors() -> None:
    with pytest.raises(LinkQueryError, match="interface not found"):
        find_link("[]", "can1")


# The capture source.


@pytest.fixture
def buses() -> Iterator[tuple[can.BusABC, can.BusABC]]:
    channel = f"photoncar-test-{uuid.uuid4().hex}"
    capture = can.Bus(interface="virtual", channel=channel)
    peer = can.Bus(interface="virtual", channel=channel)
    yield capture, peer
    capture.shutdown()
    peer.shutdown()


def _status(ok: bool, reason: str) -> ListenOnlyStatus:
    return ListenOnlyStatus(ok, reason)


def test_source_refuses_without_listen_only() -> None:
    with pytest.raises(ListenOnlyError, match="listen-only is off"):
        SocketCanSource("can1", SimClock(0), check=lambda _: _status(False, "listen-only is off"))


@pytest.mark.skipif(platform.system() == "Linux", reason="checks the real non-Linux path")
def test_source_refuses_on_this_machine() -> None:
    with pytest.raises(ListenOnlyError, match="not linux"):
        SocketCanSource("can0", SimClock(0))


def test_injected_bus_still_checked_unless_explicitly_unchecked(
    buses: tuple[can.BusABC, can.BusABC],
) -> None:
    capture, _ = buses
    with pytest.raises(ListenOnlyError):
        SocketCanSource("can0", SimClock(0), bus=capture, check=lambda _: _status(False, "x"))
    with pytest.raises(ListenOnlyError):
        SocketCanSource(
            "can0", SimClock(0), allow_unchecked_for_tests=True, check=lambda _: _status(False, "x")
        )


def _realtime(clock: SimClock, offset_ns: int = REAL_OFFSET_NS) -> Any:
    return lambda: clock.now_ns() + offset_ns


def test_source_yields_frames_and_skips_error_frames() -> None:
    channel = f"photoncar-test-{uuid.uuid4().hex}"
    capture = can.Bus(interface="virtual", channel=channel)
    peer = can.Bus(interface="virtual", channel=channel, preserve_timestamps=True)
    clock = SimClock(10 * S)
    errors: list[int] = []
    src = SocketCanSource(
        "can0",
        clock,
        bus=capture,
        allow_unchecked_for_tests=True,
        bus_index=1,
        realtime_ns=_realtime(clock),
        on_error_frame=errors.append,
    )
    assert src.meta.kind == "live"
    base = REAL_OFFSET_NS / S
    sent = [
        can.Message(arbitration_id=0x7E0, is_extended_id=False, data=bytes([2, 1, 0x0C])),
        can.Message(arbitration_id=0x123, is_error_frame=True, data=bytes(8)),
        can.Message(arbitration_id=0x18DAF110, is_extended_id=True, data=bytes([1, 2])),
        can.Message(arbitration_id=0x7DF, is_extended_id=False, is_remote_frame=True, dlc=8),
    ]
    for i, msg in enumerate(sent):
        msg.timestamp = base + 9.0 + i * 0.25  # kernel receive times 1.0 s to 0.25 s ago
        peer.send(msg)
    try:
        it = src.records()
        got = [next(it) for _ in range(3)]
        src.stop()
        assert list(it) == []
    finally:
        capture.shutdown()
        peer.shutdown()
    frames = [f for f in got if isinstance(f, CanFrame)]
    assert [(f.bus, f.arb_id, f.is_ext, f.dlc, f.data) for f in frames] == [
        (1, 0x7E0, False, 3, bytes([2, 1, 0x0C])),
        (1, 0x18DAF110, True, 2, bytes([1, 2])),
        (1, 0x7DF, False, 8, b""),
    ]
    expected = [9 * S, 9 * S + 500_000_000, 9 * S + 750_000_000]
    assert all(abs(f.t_ns - e) < 1_000 for f, e in zip(frames, expected, strict=True))
    assert src.error_frames == 1
    assert len(errors) == 1 and abs(errors[0] - (9 * S + 250_000_000)) < 1_000


class ScriptedBus:
    """A capture bus that sets the clock, then hands over one prepared message per recv."""

    def __init__(self, clock: SimClock, script: list[tuple[int, can.Message]]) -> None:
        self.clock = clock
        self.script = deque(script)
        self.closed = False

    def recv(self, timeout: float | None = None) -> can.Message | None:
        if not self.script:
            return None
        t_ns, msg = self.script.popleft()
        self.clock.set(t_ns)
        return msg

    def shutdown(self) -> None:
        self.closed = True


def _stamped(ts: float, arb_id: int = 0x1D0) -> can.Message:
    return can.Message(arbitration_id=arb_id, is_extended_id=False, data=bytes(8), timestamp=ts)


def _stamps(clock: SimClock, script: list[tuple[int, can.Message]], realtime: Any) -> list[int]:
    bus = cast(can.BusABC, ScriptedBus(clock, script))
    src = SocketCanSource(
        "can0", clock, bus=bus, allow_unchecked_for_tests=True, realtime_ns=realtime
    )
    it = src.records()
    out = [f.t_ns for f in (next(it) for _ in range(len(script))) if isinstance(f, CanFrame)]
    src.stop()
    assert list(it) == []
    return out


def test_frames_carry_the_kernel_receive_time_not_the_read_time() -> None:
    clock = SimClock(0)
    base = REAL_OFFSET_NS / S
    script = [
        (20 * S, _stamped(base + 19.5)),  # read 0.5 s after the kernel stamped it
        (20 * S + 1_000, _stamped(base + 19.75)),
        (21 * S, _stamped(base + 20.999)),
    ]
    got = _stamps(clock, script, _realtime(clock))
    expected = [19_500_000_000, 19_750_000_000, 20_999_000_000]
    assert all(abs(g - e) < 1_000 for g, e in zip(got, expected, strict=True)), got


def test_the_offset_is_resampled_once_per_second() -> None:
    clock = SimClock(0)
    offset = [REAL_OFFSET_NS]
    base = REAL_OFFSET_NS / S

    def realtime() -> int:
        return clock.now_ns() + offset[0]

    src = SocketCanSource(
        "can0",
        clock,
        bus=cast(can.BusABC, ScriptedBus(clock, [])),
        allow_unchecked_for_tests=True,
        realtime_ns=realtime,
    )
    clock.set(10 * S)
    assert abs(src.stamp(_stamped(base + 9.9)) - 9_900_000_000) < 1_000
    offset[0] += 2_000_000  # CLOCK_REALTIME slews 2 ms ahead of CLOCK_MONOTONIC
    clock.set(10 * S + 900_000_000)  # 0.9 s later: the old offset still applies
    assert abs(src.stamp(_stamped(base + 10.8)) - 10_800_000_000) < 1_000
    clock.set(11 * S)  # 1 s after the last sample: resampled
    assert abs(src.stamp(_stamped(base + 10.95 + 0.002)) - 10_950_000_000) < 1_000


def test_stamps_are_clamped_to_the_present_and_never_go_backwards() -> None:
    clock = SimClock(0)
    base = REAL_OFFSET_NS / S
    script = [
        (5 * S, _stamped(base + 4.0)),
        (5 * S + 10, _stamped(base + 3.0)),  # older than the last frame
        (6 * S, _stamped(base + 3600.0)),  # an hour in the future
        (6 * S + 10, can.Message(arbitration_id=0x1D0, data=bytes(8), timestamp=0.0)),
    ]
    got = _stamps(clock, script, _realtime(clock))
    assert abs(got[0] - 4 * S) < 1_000
    assert got[1] == got[0]
    assert got[2] == 6 * S
    assert got[3] == 6 * S + 10
    assert got == sorted(got)


def test_on_ready_runs_after_the_check_and_before_the_first_read() -> None:
    clock = SimClock(0)
    order: list[str] = []

    def check(ifname: str) -> ListenOnlyStatus:
        order.append("check")
        return _status(True, "listen-only is on")

    class Bus:
        def recv(self, timeout: float | None = None) -> can.Message | None:
            order.append("recv")
            return _stamped(REAL_OFFSET_NS / S)

        def shutdown(self) -> None:
            order.append("shutdown")

    src = SocketCanSource(
        "can0",
        clock,
        bus=cast(can.BusABC, Bus()),
        check=check,
        realtime_ns=_realtime(clock),
        on_ready=lambda: order.append("ready"),
    )
    it = src.records()
    next(it)
    src.stop()
    assert list(it) == []
    assert order == ["check", "check", "ready", "recv"]


def test_source_with_a_confirmed_check_reads_the_injected_bus(
    buses: tuple[can.BusABC, can.BusABC],
) -> None:
    capture, peer = buses
    calls: list[str] = []

    def ok(ifname: str) -> ListenOnlyStatus:
        calls.append(ifname)
        return _status(True, "listen-only is on")

    src = SocketCanSource("can0", SimClock(0), bus=capture, check=ok)
    peer.send(can.Message(arbitration_id=0x1D0, is_extended_id=False, data=bytes([0x96])))
    stopper = threading.Timer(0.5, src.stop)
    stopper.start()
    frames = list(src.records())
    stopper.cancel()
    assert [f.arb_id for f in frames if isinstance(f, CanFrame)] == [0x1D0]
    assert calls == ["can0", "can0"]  # at construction and again at start
