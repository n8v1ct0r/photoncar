"""The TX allowlist, the token bucket, the latch, the runtime audit, and the bench pass rule."""

from __future__ import annotations

import importlib.util
import math
import random
import sys
from collections.abc import Mapping
from fractions import Fraction
from pathlib import Path
from types import ModuleType

import can
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from photoncar.clock import SimClock
from photoncar.config import Config, load_config
from photoncar.decode.obd_pids import request_frame
from photoncar.records import CanFrame
from photoncar.safety.tx_guard import (
    HARD_MAX_REQUEST_HZ,
    SUPPORT_PIDS,
    TokenBucket,
    TxAuditPending,
    TxGuard,
    TxRateLimited,
    TxViolation,
    allowlist_from_config,
    bucket_cap,
    validate_frame,
)

REPO = Path(__file__).resolve().parents[2]
ALLOWED = allowlist_from_config(load_config())
PAD5 = bytes([0xCC] * 5)
MS = 1_000_000
S = 1_000_000_000
PIDS = [0x0C, 0x0D, 0x11, 0x05, 0x04]


def frame(
    arb_id: int,
    data: bytes,
    *,
    ext: bool = False,
    rtr: bool = False,
    fd: bool = False,
    err: bool = False,
    dlc: int | None = None,
) -> can.Message:
    return can.Message(
        arbitration_id=arb_id,
        is_extended_id=ext,
        is_remote_frame=rtr,
        is_fd=fd,
        is_error_frame=err,
        data=data,
        dlc=len(data) if dlc is None else dlc,
    )


def hx(text: str) -> bytes:
    return bytes.fromhex(text)


def req(pid: int) -> bytes:
    return bytes([0x02, 0x01, pid]) + PAD5


def oracle(msg: can.Message, allowed: frozenset[int]) -> bool:
    """The exact allowed shape, written independently of the validator."""
    data = bytes(msg.data)
    if msg.is_extended_id or msg.is_remote_frame or msg.is_fd or msg.is_error_frame:
        return False
    if msg.bitrate_switch or msg.error_state_indicator:
        return False
    if msg.dlc != 8 or len(data) != 8 or data[:2] != b"\x02\x01" or data[3:] != PAD5:
        return False
    pid = data[2]
    if 0x7E0 <= msg.arbitration_id <= 0x7E7:
        return pid in allowed
    if msg.arbitration_id == 0x7DF:
        return pid in allowed and pid in (0x00, 0x20, 0x40)
    return False


# Allowed and forbidden tables.

ALLOWED_FRAMES = [
    *[(arb, pid) for arb in range(0x7E0, 0x7E8) for pid in sorted(ALLOWED)],
    (0x7DF, 0x00),
    (0x7DF, 0x20),
    (0x7DF, 0x40),
]


@pytest.mark.parametrize(("arb_id", "pid"), ALLOWED_FRAMES)
def test_allowed_frames_pass(arb_id: int, pid: int) -> None:
    validate_frame(frame(arb_id, req(pid)), ALLOWED)


FORBIDDEN = [
    ("flow control on 0x7E0", frame(0x7E0, hx("30 00 00") + PAD5), "flow control"),
    ("flow control on 0x7DF", frame(0x7DF, hx("30 08 0A") + PAD5), "flow control"),
    ("VIN 09 02", frame(0x7DF, hx("02 09 02") + PAD5), "service 09"),
    ("service 09 00", frame(0x7E0, hx("02 09 00") + PAD5), "service 09"),
    ("clear codes 04", frame(0x7DF, hx("01 04") + bytes([0xCC] * 6)), "service 04"),
    ("UDS 10 session", frame(0x7E0, hx("02 10 03") + PAD5), "0x10"),
    ("UDS 27 security", frame(0x7E0, hx("02 27 01") + PAD5), "0x27"),
    ("UDS 2E write", frame(0x7E0, hx("04 2E F1 90 01 CC CC CC")), "0x2E"),
    ("UDS 31 routine", frame(0x7E0, hx("04 31 01 FF 00 CC CC CC")), "0x31"),
    ("UDS 3E tester present", frame(0x7DF, hx("02 3E 00") + PAD5), "0x3E"),
    ("UDS 11 reset", frame(0x7E0, hx("02 11 01") + PAD5), "0x11"),
    ("UDS 14 clear", frame(0x7E0, hx("04 14 FF FF FF CC CC CC")), "0x14"),
    ("service 02 freeze frame", frame(0x7E0, hx("03 02 0C 00 CC CC CC CC")), "service 0x02"),
    ("service 03 read codes", frame(0x7E0, hx("01 03") + bytes([0xCC] * 6)), "service 0x03"),
    ("BMW tester ID 0x6F1", frame(0x6F1, req(0x0C)), "0x6F1"),
    ("RTR", frame(0x7E0, b"", rtr=True, dlc=8), "RTR"),
    ("CAN FD", frame(0x7E0, req(0x0C), fd=True), "FD"),
    ("29-bit functional", frame(0x18DB33F1, req(0x0C), ext=True), "29-bit"),
    ("29-bit with a request number", frame(0x7E0, req(0x0C), ext=True), "29-bit"),
    ("error frame", frame(0x7E0, req(0x0C), err=True), "error"),
    ("DLC 7", frame(0x7E0, hx("02 01 0C CC CC CC CC")), "DLC"),
    ("DLC 8 with 7 bytes", frame(0x7E0, hx("02 01 0C CC CC CC CC"), dlc=8), "DLC"),
    ("zero padding", frame(0x7E0, hx("02 01 0C 00 00 00 00 00")), "padding"),
    ("0x55 padding", frame(0x7E0, hx("02 01 0C") + bytes([0x55] * 5)), "padding"),
    ("last pad byte wrong", frame(0x7E0, hx("02 01 0C CC CC CC CC 00")), "padding"),
    ("PID not allowlisted", frame(0x7E0, req(0xA6)), "allowlist"),
    ("2 PIDs", frame(0x7E0, hx("03 01 0C 0D CC CC CC CC")), "1 PID"),
    ("6 PIDs", frame(0x7DF, hx("07 01 0C 0D 11 05 04 0F")), "1 PID"),
    ("functional data PID", frame(0x7DF, req(0x0C)), "support bitmaps only"),
    ("first frame", frame(0x7E0, hx("10 08 01 0C CC CC CC CC")), "single frames"),
    ("reply ID", frame(0x7E8, req(0x0C)), "not an OBD request ID"),
    ("unrelated ID", frame(0x123, req(0x0C)), "not an OBD request ID"),
]


@pytest.mark.parametrize(
    ("msg", "reason"), [(m, r) for _, m, r in FORBIDDEN], ids=[n for n, _, _ in FORBIDDEN]
)
def test_forbidden_frames_raise(msg: can.Message, reason: str) -> None:
    with pytest.raises(TxViolation) as exc:
        validate_frame(msg, ALLOWED)
    assert reason in exc.value.reason


def test_forbidden_table_agrees_with_oracle() -> None:
    for _, msg, _ in FORBIDDEN:
        assert not oracle(msg, ALLOWED)


# Exhaustive and fuzzed shape checks against the oracle.


def _accepted(msg: can.Message, allowed: frozenset[int]) -> bool:
    try:
        validate_frame(msg, allowed)
    except TxViolation:
        return False
    return True


def test_every_id_and_pid_in_the_diagnostic_range() -> None:
    for arb_id in range(0x700, 0x800):
        for pid in range(256):
            msg = frame(arb_id, req(pid))
            assert _accepted(msg, ALLOWED) == oracle(msg, ALLOWED), (hex(arb_id), hex(pid))


def test_every_single_byte_change_of_a_valid_frame() -> None:
    base = req(0x0C)
    for pos in range(8):
        for value in range(256):
            data = bytearray(base)
            data[pos] = value
            msg = frame(0x7E0, bytes(data))
            assert _accepted(msg, ALLOWED) == oracle(msg, ALLOWED), (pos, hex(value))


_ids = st.one_of(
    st.integers(min_value=0, max_value=0x7FF),
    st.sampled_from([0x7DF, *range(0x7E0, 0x7F0), 0x6F1]),
)


@st.composite
def _frames(draw: st.DrawFn) -> can.Message:
    kind = draw(st.sampled_from(["random", "near", "valid"]))
    ext = draw(st.booleans()) if kind == "random" else False
    arb_id = draw(st.integers(min_value=0, max_value=0x1FFFFFFF)) if ext else draw(_ids)
    if kind == "random":
        data = draw(st.binary(min_size=0, max_size=8))
    else:
        pid = draw(st.sampled_from(sorted(ALLOWED)))
        data = req(pid)
        if kind == "near":
            pos = draw(st.integers(min_value=0, max_value=7))
            b = bytearray(data)
            b[pos] = draw(st.integers(min_value=0, max_value=255))
            data = bytes(b)
    rtr = kind == "random" and draw(st.booleans()) and draw(st.booleans())
    fd = kind == "random" and draw(st.booleans()) and draw(st.booleans())
    return frame(arb_id, data, ext=ext, rtr=rtr, fd=fd)


@settings(max_examples=3000)
@given(msg=_frames(), extra=st.sets(st.integers(min_value=0, max_value=255), max_size=4))
def test_fuzz_validator_accepts_only_the_exact_shape(msg: can.Message, extra: set[int]) -> None:
    allowed = ALLOWED - frozenset(extra)
    assert _accepted(msg, allowed) == oracle(msg, allowed)


# Token bucket.


def test_bucket_known_answer_10hz() -> None:
    clock = SimClock(0)
    bucket = TokenBucket(10.0, clock=clock)
    ok_times = []
    for ms in range(0, 10_001):
        clock.set(ms * MS)
        if bucket.try_take():
            ok_times.append(ms)
    assert ok_times == list(range(0, 10_001, 100))  # 101 = 10 Hz * 10 s + burst 1


def test_bucket_burst() -> None:
    clock = SimClock(0)
    bucket = TokenBucket(2.0, 3, clock=clock)
    assert [bucket.try_take() for _ in range(4)] == [True, True, True, False]
    assert bucket.ns_until_token() == 500 * MS
    clock.advance(499 * MS)
    assert not bucket.try_take()
    clock.advance(1 * MS)
    assert bucket.try_take()


def test_bucket_rejects_bad_parameters() -> None:
    clock = SimClock(0)
    for rate in (0.0, -1.0, math.nan, math.inf):
        with pytest.raises(ValueError):
            TokenBucket(rate, clock=clock)
    with pytest.raises(ValueError):
        TokenBucket(10.0, 0, clock=clock)


@pytest.mark.parametrize("burst", [1, 3])
def test_bucket_does_not_bank_idle_time(burst: int) -> None:
    clock = SimClock(0)
    bucket = TokenBucket(10.0, burst, clock=clock)
    clock.advance(60 * S)
    assert [bucket.try_take() for _ in range(burst + 5)] == [True] * burst + [False] * 5


_gaps_ms = st.one_of(
    st.integers(min_value=0, max_value=400),
    st.just(0),
    st.integers(min_value=1_000, max_value=30_000),
)


@settings(max_examples=400)
@given(
    rate=st.floats(min_value=0.2, max_value=25.0),
    burst=st.integers(min_value=1, max_value=5),
    events=st.lists(
        st.tuples(_gaps_ms, st.integers(min_value=1, max_value=40)), min_size=1, max_size=400
    ),
)
def test_bucket_never_exceeds_rate_in_any_10s_window(
    rate: float, burst: int, events: list[tuple[int, int]]
) -> None:
    clock = SimClock(0)
    bucket = TokenBucket(rate, burst, clock=clock)
    taken: list[int] = []
    for gap, tries in events:
        clock.advance(gap * MS)
        for _ in range(tries):
            if bucket.try_take():
                taken.append(clock.now_ns())
    bound = Fraction(rate) * 10 + burst
    j = 0
    for i, t0 in enumerate(taken):
        while j < len(taken) and taken[j] <= t0 + 10 * S:
            j += 1
        assert j - i <= bound


def _peak(times: list[int], window_ns: int) -> int:
    """The most times in any closed window of ``window_ns``. ``times`` is sorted."""
    peak, lo = 0, 0
    for hi, t in enumerate(times):
        while times[lo] < t - window_ns:
            lo += 1
        peak = max(peak, hi - lo + 1)
    return peak


@settings(max_examples=300)
@given(
    rate=st.one_of(st.just(10.0), st.floats(min_value=0.2, max_value=10.0)),
    events=st.lists(
        st.tuples(_gaps_ms, st.integers(min_value=1, max_value=40)), min_size=1, max_size=300
    ),
)
def test_bucket_cap_bounds_the_guard_bucket_in_1_s_and_10_s(
    rate: float, events: list[tuple[int, int]]
) -> None:
    clock = SimClock(0)
    bucket = TokenBucket(rate, clock=clock)
    taken: list[int] = []
    for gap, tries in events:
        clock.advance(gap * MS)
        for _ in range(tries):
            if bucket.try_take():
                taken.append(clock.now_ns())
    for window_s in (1, 10):
        assert _peak(taken, window_s * S) <= bucket_cap(rate, window_s)


def test_bucket_cap_is_exactly_what_the_bucket_can_emit() -> None:
    # A bucket asked every 1 ms from full emits 11 requests in the closed 1 s [0, 1 s]
    # and 101 in the closed 10 s [0, 10 s]: the caps are reached, so none is loose.
    clock = SimClock(0)
    bucket = TokenBucket(10.0, clock=clock)
    taken: list[int] = []
    for ms in range(10_001):
        clock.set(ms * MS)
        if bucket.try_take():
            taken.append(ms * MS)
    assert _peak(taken, S) == bucket_cap(10.0, 1) == 11
    assert _peak(taken, 10 * S) == bucket_cap(10.0, 10) == 101


# The guard.


def test_guard_latches_off_on_first_violation_and_never_re_enables() -> None:
    clock = SimClock(0)
    guard = TxGuard(ALLOWED, 10.0, clock)
    guard.check_and_consume(request_frame(0x0C, 0x7E0))
    with pytest.raises(TxViolation):
        guard.check_and_consume(frame(0x7DF, bytes([0x02, 0x09, 0x02]) + PAD5))
    assert guard.disabled
    assert guard.reason is not None and "service 09" in guard.reason
    for _ in range(5):
        clock.advance(3600 * S)
        with pytest.raises(TxViolation, match="TX is off"):
            guard.check_and_consume(request_frame(0x0C, 0x7E0))
    guard.disable("a later reason")
    assert guard.reason is not None and "service 09" in guard.reason
    assert not hasattr(guard, "enable")
    with pytest.raises(AttributeError):
        guard.disabled = False  # type: ignore[misc]


def test_guard_rate_limit_does_not_latch() -> None:
    clock = SimClock(0)
    guard = TxGuard(ALLOWED, 10.0, clock)
    guard.check_and_consume(request_frame(0x0C, 0x7E0))
    with pytest.raises(TxRateLimited):
        guard.check_and_consume(request_frame(0x0D, 0x7E0))
    assert not guard.disabled
    clock.advance(100 * MS)
    guard.check_and_consume(request_frame(0x0D, 0x7E0))


def test_guard_validates_before_rate() -> None:
    clock = SimClock(0)
    guard = TxGuard(ALLOWED, 10.0, clock)
    guard.check_and_consume(request_frame(0x0C, 0x7E0))
    with pytest.raises(TxViolation):
        guard.check_and_consume(frame(0x6F1, req(0x0C)))  # bucket is empty, still latches
    assert guard.disabled


def test_guard_rejects_out_of_range_pids() -> None:
    with pytest.raises(ValueError):
        TxGuard({0x0C, 0x100}, 10.0, SimClock(0))


def test_guard_from_config() -> None:
    cfg = load_config()
    guard = TxGuard.from_config(cfg, SimClock(0))
    obd = cfg.section("obd")
    assert guard.allowed_pids == SUPPORT_PIDS | set(obd["fast_pids"]) | set(obd["slow_pids"])
    assert guard.rate_hz == 10.0


def _cf(t_ns: int, arb_id: int, data: bytes, *, ext: bool = False) -> CanFrame:
    return CanFrame(t_ns=t_ns, bus=0, arb_id=arb_id, is_ext=ext, dlc=len(data), data=data)


def test_audit_passes_allowed_traffic_and_ignores_other_ids() -> None:
    guard = TxGuard(ALLOWED, 10.0, SimClock(0))
    guard.audit(_cf(0, 0x7DF, req(0x00)))
    guard.audit(_cf(100 * MS, 0x7E0, req(0x0C)))
    guard.audit(_cf(101 * MS, 0x7E8, bytes([0x04, 0x41, 0x0C, 0x1A, 0xF8, 0x55, 0x55, 0x55])))
    guard.audit(_cf(102 * MS, 0x123, bytes([0x30, 0x09, 0x02])))
    guard.audit(_cf(103 * MS, 0x1D0, bytes(8)))
    assert not guard.disabled


@pytest.mark.parametrize(
    ("arb_id", "data", "ext"),
    [
        (0x7E0, bytes([0x30, 0x00, 0x00]) + PAD5, False),
        (0x7DF, bytes([0x02, 0x09, 0x02]) + PAD5, False),
        (0x6F1, req(0x0C), False),
        (0x7E3, req(0xA6), False),
        (0x7E0, req(0x0C), True),
        (0x7E0, b"", False),
    ],
)
def test_audit_disables_on_a_mismatching_frame(arb_id: int, data: bytes, ext: bool) -> None:
    guard = TxGuard(ALLOWED, 10.0, SimClock(0))
    guard.audit(_cf(0, arb_id, data, ext=ext))
    assert guard.disabled
    assert guard.reason is not None and guard.reason.startswith("audit:")


def test_audit_disables_when_the_wire_rate_is_over_the_cap() -> None:
    guard = TxGuard(ALLOWED, 10.0, SimClock(0))
    for i in range(11):  # cap: what the bucket can emit, 10 Hz * 1 s + burst 1
        guard.audit(_cf(i * 80 * MS, 0x7E0, req(0x0C)))
    assert not guard.disabled
    guard.audit(_cf(11 * 80 * MS, 0x7E0, req(0x0C)))
    assert guard.disabled


@pytest.mark.parametrize("span_ms", [1000, 990, 550])
def test_audit_fails_12_requests_inside_1_s(span_ms: int) -> None:
    # The bucket cannot emit 12 requests in a closed 1 s, so 12 on the wire means something
    # other than this guard is sending, or the wire timing is not what the guard allowed.
    guard = TxGuard(ALLOWED, 10.0, SimClock(0))
    for i in range(11):
        guard.audit(_cf(i * span_ms * MS // 11, 0x7E0, req(PIDS[i % 5])))
    assert not guard.disabled
    guard.audit(_cf(span_ms * MS, 0x7E0, req(PIDS[11 % 5])))
    assert guard.reason == "audit: 12 requests on the wire within 1 s, over the cap of 11"
    assert guard.disabled


@settings(max_examples=200)
@given(
    rate=st.one_of(st.just(10.0), st.floats(min_value=0.2, max_value=10.0)),
    events=st.lists(
        st.tuples(_gaps_ms, st.integers(min_value=1, max_value=40)), min_size=1, max_size=300
    ),
)
def test_audit_passes_everything_the_guard_lets_through(
    rate: float, events: list[tuple[int, int]]
) -> None:
    # The tightened audit has no false alarm on traffic the guard itself allowed, seen on
    # the wire at the moment it was allowed.
    clock = SimClock(0)
    guard = TxGuard(ALLOWED, rate, clock)
    n = 0
    for gap, tries in events:
        clock.advance(gap * MS)
        for _ in range(tries):
            msg = request_frame(PIDS[n % 5], 0x7E0)
            try:
                guard.check_and_consume(msg)
            except TxRateLimited:
                continue
            n += 1
            guard.audit(_cf(clock.now_ns(), 0x7E0, bytes(msg.data)))
    assert not guard.disabled, guard.reason


def test_audit_rate_window_slides() -> None:
    guard = TxGuard(ALLOWED, 10.0, SimClock(0))
    for i in range(600):
        guard.audit(_cf(i * 100 * MS, 0x7E0, req(0x0C)))
    assert not guard.disabled


def test_audit_rate_window_slides_over_10_s_at_exactly_10_hz() -> None:
    guard = TxGuard(ALLOWED, 10.0, SimClock(0))
    pids = [0x0C, 0x0D, 0x11, 0x05, 0x04]
    for i in range(600):  # 60 s of a correct 10 Hz poller, 5 PIDs in rotation
        guard.audit(_cf(i * 100 * MS, 0x7E0, req(pids[i % 5])))
    assert not guard.disabled


def test_audit_catches_10_5_hz_sustained_over_10_s() -> None:
    # 10.5 Hz puts at most 11 requests in any closed 1 s, within the 1 s cap of 11, so only
    # the 10 s window (cap 101) catches it.
    guard = TxGuard(ALLOWED, 10.0, SimClock(0))
    period_ns = S * 2 // 21
    for i in range(21):  # 2 s of 10.5 Hz
        guard.audit(_cf(i * period_ns, 0x7E0, req(PIDS[i % 5])))
    assert not guard.disabled
    for i in range(21, 21 * 10):
        guard.audit(_cf(i * period_ns, 0x7E0, req(PIDS[i % 5])))
    assert guard.reason is not None and "within 10 s" in guard.reason
    assert guard.disabled


def test_audit_catches_11_hz_on_the_1_s_window() -> None:
    guard = TxGuard(ALLOWED, 10.0, SimClock(0))
    for i in range(12):  # 1 s of 11 Hz: 12 frames in the closed 1 s
        guard.audit(_cf(i * (S // 11), 0x7E0, req(PIDS[i % 5])))
    assert guard.reason == "audit: 12 requests on the wire within 1 s, over the cap of 11"


def test_bucket_caps() -> None:
    assert bucket_cap(10.0, 1) == 11  # 10 Hz * 1 s + burst 1
    assert bucket_cap(10.0, 10) == 101  # 10 Hz * 10 s + burst 1
    assert bucket_cap(5.0, 10) == 51
    assert bucket_cap(7.5, 1) == 8


def test_audit_flags_a_retransmit_within_1_ms() -> None:
    guard = TxGuard(ALLOWED, 10.0, SimClock(0))
    guard.audit(_cf(0, 0x7E0, req(0x0C)))
    guard.audit(_cf(300_000, 0x7E0, req(0x0D)))  # a different request 0.3 ms later is fine
    assert not guard.disabled
    guard.audit(_cf(999_999, 0x7E0, req(0x0C)))
    assert guard.reason is not None and "retransmit" in guard.reason
    assert guard.disabled


def test_audit_allows_the_same_request_1_ms_later() -> None:
    guard = TxGuard(ALLOWED, 10.0, SimClock(0))
    guard.audit(_cf(0, 0x7E0, req(0x0C)))
    guard.audit(_cf(MS, 0x7E0, req(0x0C)))
    assert not guard.disabled


def test_audit_error_frame_disables() -> None:
    guard = TxGuard(ALLOWED, 10.0, SimClock(0))
    guard.audit_error_frame(5 * MS)
    assert guard.disabled
    assert guard.reason is not None and "error frame on the capture bus" in guard.reason


# The hard ceiling.


def test_hard_ceiling_is_the_approved_cap() -> None:
    assert HARD_MAX_REQUEST_HZ == 10.0
    assert float(load_config().get("obd.max_request_hz")) <= HARD_MAX_REQUEST_HZ


@pytest.mark.parametrize("rate", [10.01, 11.0, 20.0, 50.0, math.inf, math.nan, 0.0, -1.0])
def test_guard_refuses_a_rate_over_the_ceiling_or_not_positive(rate: float) -> None:
    with pytest.raises(ValueError, match="rate_hz"):
        TxGuard(ALLOWED, rate, SimClock(0))


@pytest.mark.parametrize("burst", [0, 2, 5])
def test_guard_refuses_a_burst_other_than_1(burst: int) -> None:
    with pytest.raises(ValueError, match="burst"):
        TxGuard(ALLOWED, 10.0, SimClock(0), burst=burst)


def _cfg_50_hz() -> Config:
    raw = {k: dict(v) if isinstance(v, Mapping) else v for k, v in load_config().raw.items()}
    raw["obd"]["max_request_hz"] = 50.0
    return Config(raw)


def test_guard_from_a_config_set_to_50_hz_refuses() -> None:
    with pytest.raises(ValueError, match=r"at most 10\.0 Hz"):
        TxGuard.from_config(_cfg_50_hz(), SimClock(0))


# The live-session rule: the capture audit must see each request before the next.


def _echo_guard(clock: SimClock) -> TxGuard:
    return TxGuard(ALLOWED, 10.0, clock, echo_timeout_ns=100 * MS)


def test_echo_guard_sends_nothing_until_the_audit_is_live() -> None:
    clock = SimClock(0)
    guard = _echo_guard(clock)
    for _ in range(3):
        with pytest.raises(TxAuditPending):
            guard.check_and_consume(request_frame(0x00, 0x7DF))
        clock.advance(500 * MS)
    assert not guard.disabled
    guard.mark_audit_live()
    guard.check_and_consume(request_frame(0x00, 0x7DF))  # the bucket was never drained


def test_echo_guard_waits_for_the_echo_then_continues() -> None:
    clock = SimClock(0)
    guard = _echo_guard(clock)
    guard.mark_audit_live()
    first = request_frame(0x0C, 0x7E0)
    guard.check_and_consume(first)
    clock.advance(50 * MS)
    with pytest.raises(TxAuditPending):
        guard.check_and_consume(request_frame(0x0D, 0x7E0))
    guard.audit(_cf(clock.now_ns(), 0x7E0, bytes(first.data)))
    clock.advance(50 * MS)
    guard.check_and_consume(request_frame(0x0D, 0x7E0))
    assert not guard.disabled


def test_echo_guard_latches_off_when_a_request_is_never_seen() -> None:
    clock = SimClock(0)
    guard = _echo_guard(clock)
    guard.mark_audit_live()
    guard.check_and_consume(request_frame(0x0C, 0x7E0))
    clock.advance(100 * MS)
    with pytest.raises(TxViolation, match="not seen on the capture bus"):
        guard.check_and_consume(request_frame(0x0D, 0x7E0))
    assert guard.disabled
    clock.advance(10 * S)
    with pytest.raises(TxViolation, match="TX is off"):
        guard.check_and_consume(request_frame(0x0D, 0x7E0))


def test_echo_guard_latches_off_on_a_request_it_did_not_pass() -> None:
    clock = SimClock(0)
    guard = _echo_guard(clock)
    guard.mark_audit_live()
    guard.audit(_cf(0, 0x7E0, req(0x0C)))
    assert guard.disabled
    assert guard.reason is not None and "not sent by Photoncar" in guard.reason


def test_echo_guard_counts_every_audited_frame() -> None:
    guard = _echo_guard(SimClock(0))
    guard.audit(_cf(0, 0x1D0, bytes(8)))
    guard.audit(_cf(MS, 0x7E8, bytes([0x04, 0x41, 0x0C, 0x1A, 0xF8, 0x55, 0x55, 0x55])))
    assert guard.audited == 2
    assert not guard.disabled


# The bench pass rule (scripts/bench/check_log.py).


def _load_script(name: str) -> ModuleType:
    path = REPO / "scripts" / "bench" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"bench_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


check_log = _load_script("check_log")
ecu_emulator = _load_script("ecu_emulator")


def _line(t_s: float, msg: can.Message) -> str:
    ident = f"{msg.arbitration_id:08X}" if msg.is_extended_id else f"{msg.arbitration_id:03X}"
    return f"({t_s:.6f}) can2 {ident}#{bytes(msg.data).hex().upper()}"


def _bench_log(seconds: int) -> list[str]:
    """A clean bench session: discovery, then 10 Hz of polls, each answered."""
    emu = ecu_emulator.EcuEmulator(ecu_emulator.DEFAULT_VALUES)
    t0 = 1_695_500_000.0
    pids = [0x00, 0x20, 0x40] + [0x0C, 0x0D, 0x11, 0x05, 0x04] * (2 * seconds)
    lines = []
    for i, pid in enumerate(pids):
        arb = 0x7DF if pid in (0x00, 0x20, 0x40) else 0x7E0
        request = request_frame(pid, arb)
        lines.append(_line(t0 + i * 0.1, request))
        answer = emu.respond(request)
        assert answer is not None
        lines.append(_line(t0 + i * 0.1 + 0.002, answer))
    return lines


def test_check_log_passes_a_clean_session() -> None:
    lines = _bench_log(10)
    report = check_log.check_lines(lines, ALLOWED, 10.0)
    assert report.passed, report.failures
    assert report.frames == len(lines) == 206
    assert report.requests == 103
    assert report.replies == 103
    assert report.flow_control == report.error_frames == report.other == 0
    assert report.max_requests_per_s == 11
    assert report.max_requests_per_10s == 101
    assert report.request_cap_per_10s == 102
    assert report.retransmits == 0
    text = check_log.format_report(report)
    assert "Result: pass" in text
    assert "Flow control frames: 0" in text
    assert "Retransmits: 0" in text
    assert "Most requests in 10 s: 101 (cap 102)" in text


@pytest.mark.parametrize(
    ("extra", "field", "count"),
    [
        ("(1695500000.050000) can2 7E8#3000000000000000", "flow_control", 1),
        ("(1695500000.050000) can2 7E0#3000000000000000", "flow_control", 1),
        ("(1695500000.050000) can2 20000004#0004000000000000", "error_frames", 1),
        ("(1695500000.050000) can2 7DF#020902CCCCCCCCCC", "rejected_requests", 1),
        ("(1695500000.050000) can2 6F1#02010CCCCCCCCCCC", "rejected_requests", 1),
        ("(1695500000.050000) can2 7E0#R", "rejected_requests", 1),
        ("(1695500000.050000) can2 7E0##102010CCCCCCCCCCC", "rejected_requests", 1),
        ("(1695500000.050000) can2 123#1122", "other", 1),
        ("(1695500000.050000) can2 18DAF110#02010CCCCCCCCCCC", "other", 1),
        ("this is not a frame", "unparsed_lines", 1),
    ],
)
def test_check_log_fails_on_each_forbidden_frame(extra: str, field: str, count: int) -> None:
    report = check_log.check_lines([*_bench_log(2), extra], ALLOWED, 10.0)
    assert getattr(report, field) == count
    assert not report.passed


def test_check_log_fails_over_the_wire_rate() -> None:
    t0 = 1_695_500_000.0
    lines = [_line(t0 + i * 0.07, request_frame(0x0C, 0x7E0)) for i in range(14)]
    report = check_log.check_lines(lines, ALLOWED, 10.0)
    assert report.max_requests_per_s == 14
    assert not report.passed


def test_check_log_fails_13_requests_inside_1_s() -> None:
    t0 = 1_695_500_000.0
    lines = [_line(t0 + i / 12, request_frame(PIDS[i % 5], 0x7E0)) for i in range(13)]
    report = check_log.check_lines(lines, ALLOWED, 10.0)
    assert report.max_requests_per_s == 13
    assert report.failures == ["13 requests within 1 s, over the cap of 12"]


def test_bench_caps_are_the_bucket_caps_plus_the_jitter_slack() -> None:
    assert check_log.BENCH_JITTER_SLACK == 1
    report = check_log.check_lines(_bench_log(2), ALLOWED, 10.0)
    assert report.request_cap_per_s == bucket_cap(10.0, 1) + 1 == 12
    assert report.request_cap_per_10s == bucket_cap(10.0, 10) + 1 == 102


def test_check_log_slack_covers_logger_jitter_on_a_legal_session() -> None:
    # 12 requests the guard allows: takes every 100 ms from 0 to 1.1 s. The echo rule lets
    # request 1 reach the wire as late as just before take 2, here at 99 ms. The logger
    # stamps it 0.6 ms late and the last one 0.4 ms early, on its own clock, so its log
    # shows all 12 inside the closed second [99.6 ms, 1099.6 ms]: the runtime audit on the
    # guard's clock would pass, and the bench rule must too.
    t0_us = 1_695_500_000 * 1_000_000
    wire_us = [99_000, *(k * 100_000 for k in range(1, 12))]
    error_us = [600, *([0] * 10), -400]
    lines = [
        f"({(t0_us + w + e) // 1_000_000}.{(t0_us + w + e) % 1_000_000:06d}) can2 "
        f"7E0#{bytes(request_frame(PIDS[k % 5], 0x7E0).data).hex().upper()}"
        for k, (w, e) in enumerate(zip(wire_us, error_us, strict=True))
    ]
    report = check_log.check_lines(lines, ALLOWED, 10.0)
    assert report.max_requests_per_s == 12
    assert report.passed, report.failures


def test_check_log_passes_a_jittered_clean_10_hz_log() -> None:
    # A clean 10 Hz session logged with up to 45 ms of timestamp error per frame.
    rng = random.Random(7)
    t0_us = 1_695_500_000 * 1_000_000
    lines = []
    for k in range(600):
        t_us = t0_us + k * 100_000 + rng.randint(-45_000, 45_000)
        msg = request_frame(PIDS[k % 5], 0x7E0)
        lines.append(f"({t_us // 1_000_000}.{t_us % 1_000_000:06d}) can2 7E0#{msg.data.hex()}")
    report = check_log.check_lines(lines, ALLOWED, 10.0)
    assert report.max_requests_per_s is not None and report.max_requests_per_s <= 12
    assert report.passed, report.failures


def test_check_log_fails_on_an_empty_log() -> None:
    report = check_log.check_lines(["", "   "], ALLOWED, 10.0)
    assert report.frames == 0
    assert not report.passed


def test_check_log_reads_the_default_candump_layout() -> None:
    lines = [
        "  can2  7DF   [8]  02 01 00 CC CC CC CC CC",
        "  can2  7E8   [8]  06 41 00 BE 1F A8 13 55",
        "  can2  7E0   [8]  remote request",
        "  can2  20000080   [8]  00 00 00 00 00 00 00 00   ERRORFRAME",
    ]
    report = check_log.check_lines(lines, ALLOWED, 10.0)
    assert (report.requests, report.replies, report.rejected_requests) == (1, 1, 1)
    assert report.error_frames == 1
    assert report.max_requests_per_s is None
    assert not report.passed
    assert "the log has no timestamps; rate not proven" in report.failures


def test_check_log_fails_a_clean_log_without_timestamps() -> None:
    lines = [
        "  can2  7DF   [8]  02 01 00 CC CC CC CC CC",
        "  can2  7E8   [8]  06 41 00 BE 1F A8 13 55",
    ]
    report = check_log.check_lines(lines, ALLOWED, 10.0)
    assert (report.requests, report.replies, report.other) == (1, 1, 0)
    assert report.failures == ["the log has no timestamps; rate not proven"]
    text = check_log.format_report(report)
    assert "Most requests in 1 s: not checked, the log has no timestamps" in text
    assert "Result: fail" in text


def test_check_log_fails_when_some_frames_have_no_timestamp() -> None:
    lines = [*_bench_log(2), "  can2  7E0   [8]  02 01 0C CC CC CC CC CC"]
    report = check_log.check_lines(lines, ALLOWED, 10.0)
    assert report.failures == ["1 frames have no timestamp; rate not proven"]


def test_check_log_flags_identical_requests_under_1_ms_apart() -> None:
    t0 = 1_695_500_000.0
    lines = [
        _line(t0, request_frame(0x0C, 0x7E0)),
        _line(t0 + 0.000300, request_frame(0x0C, 0x7E0)),  # retransmit
        _line(t0 + 0.000500, request_frame(0x0D, 0x7E0)),  # a different request: fine
        _line(t0 + 0.2, request_frame(0x0D, 0x7E0)),
        _line(t0 + 0.2 + 0.001, request_frame(0x0D, 0x7E0)),  # exactly 1 ms: not under
    ]
    report = check_log.check_lines(lines, ALLOWED, 10.0)
    assert report.retransmits == 1
    assert report.failures == ["1 retransmits"]


def test_check_log_fails_11_hz_sustained_on_the_10_s_window() -> None:
    t0_ns = 1_695_500_000 * S
    pids = [0x0C, 0x0D, 0x11, 0x05, 0x04]
    lines = []
    for i in range(11 * 30):
        t_ns = t0_ns + i * S // 11
        msg = request_frame(pids[i % 5], 0x7E0)
        lines.append(f"({t_ns // S}.{t_ns % S // 1000:06d}) can2 7E0#{bytes(msg.data).hex()}")
    report = check_log.check_lines(lines, ALLOWED, 10.0)
    assert report.max_requests_per_s is not None
    assert report.max_requests_per_s <= report.request_cap_per_s  # the 1 s rule alone passes
    assert report.max_requests_per_10s is not None and report.max_requests_per_10s >= 110
    assert report.failures == [
        f"{report.max_requests_per_10s} requests within 10 s, over the cap of 102"
    ]


def test_check_log_caps_the_config_rate_at_the_hard_ceiling(tmp_path: Path) -> None:
    t0 = 1_695_500_000.0
    fast = [_line(t0 + i * 0.05, request_frame(0x0C if i % 2 else 0x0D, 0x7E0)) for i in range(60)]
    report = check_log.check_lines(fast, ALLOWED, 50.0)  # 20 Hz on the wire, config says 50
    assert (report.rate_hz, report.request_cap_per_s, report.request_cap_per_10s) == (10.0, 12, 102)
    assert not report.passed
    cfg_path = tmp_path / "photoncar.toml"
    text = (REPO / "config" / "photoncar.toml").read_text(encoding="utf-8")
    assert "max_request_hz = 10.0" in text
    cfg_path.write_text(text.replace("max_request_hz = 10.0", "max_request_hz = 50.0"), "utf-8")
    log = tmp_path / "fast.log"
    log.write_text("\n".join(fast) + "\n", encoding="utf-8")
    assert check_log.main([str(log), "--config", str(cfg_path)]) == 1


def test_check_log_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = tmp_path / "good.log"
    good.write_text("\n".join(_bench_log(2)) + "\n", encoding="utf-8")
    assert check_log.main([str(good)]) == 0
    assert "Result: pass" in capsys.readouterr().out
    bad = tmp_path / "bad.log"
    bad.write_text(
        "\n".join([*_bench_log(2), "(1695500000.5) can2 7E0#3000000000000000"]) + "\n",
        encoding="utf-8",
    )
    assert check_log.main([str(bad)]) == 1
    out = capsys.readouterr().out
    assert "Result: fail" in out
    assert "Failure: 1 flow control frames" in out
