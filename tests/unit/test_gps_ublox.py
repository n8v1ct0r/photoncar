"""u-blox NEO-M9N source: UBX framing, NAV-PVT decoding, configuration, time base, serial port.

Every byte fixture is built here with this file's own checksum helper and field offsets, so the
parser is never checked against itself. Coordinates are near the fake origin (lat 0, lon 0).
"""

from __future__ import annotations

import contextlib
import itertools
import math
import os
import struct
import termios
import threading
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from photoncar.clock import MonotonicClock, SimClock
from photoncar.config import Config, load_config
from photoncar.records import GpsFix
from photoncar.sources.gps_ublox import (
    GpsUbloxSource,
    GpsUnavailable,
    PosixSerialPort,
    SerialClosed,
    UbxFrame,
    UbxParser,
    UtcAnchor,
    ack_result,
    config_pairs,
    decode_nav_pvt,
    fix_from_nav_pvt,
    key_value_bytes,
    measurement_period_ms,
    ubx_checksum,
    valset_payload,
)

S = 1_000_000_000
MS = 1_000_000
UTC0 = datetime(2024, 2, 29, 12, 34, 56, tzinfo=UTC)
UTC0_NS = int(UTC0.timestamp()) * S + 250 * MS
MONO0 = 5_000 * S
"""The monotonic time of UTC0_NS in these tests: mono = utc - UTC0_NS + MONO0."""
CFG = load_config()


# Fixture builders (independent of the module under test).


def fletcher(body: bytes) -> bytes:
    ck_a = ck_b = 0
    for x in body:
        ck_a = (ck_a + x) % 256
        ck_b = (ck_b + ck_a) % 256
    return bytes([ck_a, ck_b])


def frame(cls: int, msg_id: int, payload: bytes) -> bytes:
    body = bytes([cls, msg_id, len(payload) & 0xFF, len(payload) >> 8]) + payload
    return b"\xb5\x62" + body + fletcher(body)


PVT_OFFSETS: dict[str, tuple[int, str]] = {
    "itow": (0, "<I"),
    "year": (4, "<H"),
    "month": (6, "B"),
    "day": (7, "B"),
    "hour": (8, "B"),
    "minute": (9, "B"),
    "second": (10, "B"),
    "valid": (11, "B"),
    "t_acc": (12, "<I"),
    "nano": (16, "<i"),
    "fix_type": (20, "B"),
    "flags": (21, "B"),
    "flags2": (22, "B"),
    "num_sv": (23, "B"),
    "lon": (24, "<i"),
    "lat": (28, "<i"),
    "height": (32, "<i"),
    "hmsl": (36, "<i"),
    "h_acc": (40, "<I"),
    "v_acc": (44, "<I"),
    "vel_n": (48, "<i"),
    "vel_e": (52, "<i"),
    "vel_d": (56, "<i"),
    "g_speed": (60, "<i"),
    "head_mot": (64, "<i"),
    "s_acc": (68, "<I"),
    "head_acc": (72, "<I"),
    "pdop": (76, "<H"),
    "flags3": (78, "<H"),
    "head_veh": (84, "<i"),
}
"""NAV-PVT field offsets from the u-blox interface description."""

PVT_DEFAULTS: dict[str, int] = {
    "itow": 216_000_250,
    "valid": 0x07,
    "t_acc": 30,
    "fix_type": 3,
    "flags": 0x01,
    "flags2": 0xE0,
    "num_sv": 12,
    "lon": 1_234_567,
    "lat": -765_432,
    "height": 99_999,
    "hmsl": 12_345,
    "h_acc": 1_500,
    "v_acc": 2_500,
    "vel_n": 1,
    "vel_e": 7_777,
    "vel_d": -2,
    "g_speed": 13_410,
    "head_mot": 9_000_000,
    "s_acc": 300,
    "head_acc": 500_000,
    "pdop": 123,
    "flags3": 0,
    "head_veh": 4_500_000,
}
"""Neighbouring fields hold distinct values, so reading the wrong offset shows."""


def utc_fields(utc_ns: int) -> dict[str, int]:
    sec, nano = divmod(utc_ns, S)
    d = datetime.fromtimestamp(sec, UTC)
    return {
        "year": d.year,
        "month": d.month,
        "day": d.day,
        "hour": d.hour,
        "minute": d.minute,
        "second": d.second,
        "nano": nano,
    }


def nav_pvt(utc_ns: int = UTC0_NS, **fields: int) -> bytes:
    values = {**PVT_DEFAULTS, **utc_fields(utc_ns), **fields}
    payload = bytearray(92)
    for name, value in values.items():
        offset, fmt = PVT_OFFSETS[name]
        struct.pack_into(fmt, payload, offset, value)
    return bytes(payload)


def pvt_frame(utc_ns: int = UTC0_NS, **fields: int) -> bytes:
    return frame(0x01, 0x07, nav_pvt(utc_ns, **fields))


ACK = frame(0x05, 0x01, bytes([0x06, 0x8A]))
NAK = frame(0x05, 0x00, bytes([0x06, 0x8A]))
NMEA = b"$GNGGA,,,,,,0,00,99.99,,,,,,*56\r\n"
VALSET_10HZ = bytes.fromhex(
    "00010000"  # version 0, RAM layer, reserved
    "01002130" + "6400"  # CFG-RATE-MEAS = 100 ms
    "02002130" + "0100"  # CFG-RATE-NAV = 1
    "09009120" + "01"  # CFG-MSGOUT-UBX_NAV_PVT_USB = 1
    "01007810" + "01"  # CFG-USBOUTPROT-UBX = 1
    "02007810" + "00"  # CFG-USBOUTPROT-NMEA = 0
)


def gps_cfg(**gps: object) -> Config:
    raw = dict(CFG.raw)
    raw["gps"] = {**CFG.section("gps"), **gps}
    return Config(raw)


class FakeUbloxPort:
    """A scripted serial port. ``reads`` are (time, bytes) pairs returned in order, each at its
    time on ``clock``. A CFG-VALSET write queues ``reply``. With the script used up, a read
    calls ``on_empty`` (or raises SerialClosed with ``eof``) and otherwise waits out its timeout.
    """

    def __init__(
        self,
        clock: SimClock,
        reads: Iterable[tuple[int, bytes]] = (),
        *,
        reply: bytes | None = ACK,
        eof: bool = False,
    ) -> None:
        self.clock = clock
        self.reads = deque(reads)
        self.reply = reply
        self.eof = eof
        self.replies: deque[bytes] = deque()
        self.written: list[bytes] = []
        self.closed = False
        self.on_empty: Callable[[], None] | None = None

    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        if self.replies:
            return self.replies.popleft()
        if self.reads:
            t, data = self.reads.popleft()
            self.clock.set(max(t, self.clock.now_ns()))
            assert len(data) <= max_bytes
            return data
        if self.eof:
            raise SerialClosed("end of script")
        if self.on_empty is not None:
            self.on_empty()
        self.clock.advance(round(timeout_s * S))
        return b""

    def write(self, data: bytes) -> None:
        self.written.append(data)
        if data[2:4] == b"\x06\x8a" and self.reply is not None:
            self.replies.append(self.reply)

    def close(self) -> None:
        self.closed = True


def run(source: GpsUbloxSource, port: FakeUbloxPort) -> list[GpsFix]:
    port.on_empty = source.stop
    out = []
    for rec in source.records():
        assert isinstance(rec, GpsFix)
        out.append(rec)
    return out


# Checksum and framing.


@pytest.mark.parametrize(
    "known",
    [
        "b5 62 06 01 03 00 f0 01 00 fb 11",  # CFG-MSG, the widely quoted "GLL off" example
        "b5 62 0a 04 00 00 0e 34",  # MON-VER poll
    ],
)
def test_checksum_matches_published_frames(known: str) -> None:
    raw = bytes.fromhex(known)
    assert ubx_checksum(raw[2:-2]) == (raw[-2], raw[-1])
    assert UbxParser().feed(raw) == [UbxFrame(raw[2], raw[3], raw[6:-2])]


def test_parser_reads_back_to_back_frames() -> None:
    p = UbxParser()
    frames = p.feed(pvt_frame() + ACK)
    assert [(f.cls, f.msg_id, len(f.payload)) for f in frames] == [(1, 7, 92), (5, 1, 2)]
    assert (p.frames, p.bad_checksums, p.skipped_bytes, p.pending) == (2, 0, 0, 0)


def test_parser_skips_garbage_and_nmea_between_frames() -> None:
    p = UbxParser()
    stream = b"\x00\xff" + NMEA + pvt_frame() + b"\xb5\x00\x62" + NMEA + ACK + b"\xb5"
    frames = p.feed(stream)
    assert [(f.cls, f.msg_id) for f in frames] == [(1, 7), (5, 1)]
    assert p.skipped_bytes == 2 + len(NMEA) + 3 + len(NMEA)
    assert p.pending == 1  # a trailing 0xB5 may start the next sync pair


def test_parser_drops_a_bad_checksum_and_keeps_the_next_frame() -> None:
    bad = bytearray(pvt_frame())
    bad[40] ^= 0x01
    p = UbxParser()
    frames = p.feed(bytes(bad) + ACK)
    assert [(f.cls, f.msg_id) for f in frames] == [(5, 1)]
    assert p.bad_checksums == 1


def test_parser_drops_a_truncated_frame_and_keeps_the_next_one() -> None:
    cut = pvt_frame()[:50]
    p = UbxParser()
    assert p.feed(cut) == []
    assert p.pending == 50
    frames = p.feed(pvt_frame(itow=7) + pvt_frame(itow=8))
    assert [decode_nav_pvt(f.payload).itow_ms for f in frames] == [7, 8]
    assert p.bad_checksums == 1


def test_parser_does_not_wait_on_an_impossible_length() -> None:
    p = UbxParser()
    frames = p.feed(b"\xb5\x62\x01\x07\xff\xff" + ACK)
    assert [(f.cls, f.msg_id) for f in frames] == [(5, 1)]
    assert p.bad_lengths == 1
    assert p.pending == 0


def test_parser_handles_a_sync_pair_split_across_reads() -> None:
    raw = pvt_frame()
    p = UbxParser()
    assert p.feed(raw[:1]) == []
    assert p.feed(raw[1:2]) == []
    assert p.feed(raw[2:5]) == []
    assert len(p.feed(raw[5:])) == 1


STREAM = NMEA + pvt_frame(itow=1) + b"\x13\x37" + ACK + pvt_frame(itow=2) + NAK


@given(st.lists(st.integers(min_value=0, max_value=len(STREAM)), max_size=12))
def test_parser_output_does_not_depend_on_read_boundaries(cuts: list[int]) -> None:
    edges = [0, *sorted(cuts), len(STREAM)]
    p = UbxParser()
    frames = [f for a, b in itertools.pairwise(edges) for f in p.feed(STREAM[a:b])]
    assert frames == UbxParser().feed(STREAM)
    assert [(f.cls, f.msg_id) for f in frames] == [(1, 7), (5, 1), (1, 7), (5, 0)]


@given(st.lists(st.binary(max_size=40).filter(lambda b: b"\xb5" not in b), min_size=3, max_size=3))
def test_parser_recovers_every_frame_from_any_garbage(noise: list[bytes]) -> None:
    stream = noise[0] + pvt_frame(itow=1) + noise[1] + ACK + noise[2]
    frames = UbxParser().feed(stream)
    assert [(f.cls, f.msg_id) for f in frames] == [(1, 7), (5, 1)]


def test_ack_result() -> None:
    p = UbxParser()
    ack, nak, other, nav = p.feed(ACK + NAK + frame(0x05, 0x01, b"\x06\x01") + pvt_frame())
    assert ack_result(ack, 0x06, 0x8A) is True
    assert ack_result(nak, 0x06, 0x8A) is False
    assert ack_result(other, 0x06, 0x8A) is None
    assert ack_result(nav, 0x06, 0x8A) is None


# NAV-PVT.


def test_decode_nav_pvt_reads_each_field_at_its_offset() -> None:
    pvt = decode_nav_pvt(nav_pvt())
    assert pvt.itow_ms == 216_000_250
    assert (pvt.year, pvt.month, pvt.day) == (2024, 2, 29)
    assert (pvt.hour, pvt.minute, pvt.second, pvt.nano_ns) == (12, 34, 56, 250 * MS)
    assert (pvt.fix_type, pvt.num_sv, pvt.valid, pvt.flags, pvt.flags3) == (3, 12, 7, 1, 0)
    assert pvt.lon_deg == 1_234_567 / 1e7
    assert pvt.lat_deg == -765_432 / 1e7
    assert pvt.hmsl_m == 12.345
    assert pvt.ground_speed_mps == 13.41
    assert pvt.heading_rad == pytest.approx(math.pi / 2, abs=1e-12)
    assert pvt.pdop == 1.23
    assert pvt.utc_ns == UTC0_NS
    assert pvt.usable


def test_heading_is_radians_clockwise_from_north_in_0_to_2pi() -> None:
    assert decode_nav_pvt(nav_pvt(head_mot=0)).heading_rad == 0.0
    assert decode_nav_pvt(nav_pvt(head_mot=27_000_000)).heading_rad == pytest.approx(
        3 * math.pi / 2, abs=1e-12
    )
    assert decode_nav_pvt(nav_pvt(head_mot=-9_000_000)).heading_rad == pytest.approx(
        3 * math.pi / 2, abs=1e-12
    )


@pytest.mark.parametrize("length", [0, 84, 91, 93, 100])
def test_decode_nav_pvt_refuses_other_lengths(length: int) -> None:
    with pytest.raises(ValueError, match="NAV-PVT payload"):
        decode_nav_pvt(bytes(length))


@pytest.mark.parametrize(
    ("fields", "usable"),
    [
        ({}, True),
        ({"fix_type": 2}, True),
        ({"fix_type": 4}, True),
        ({"fix_type": 0, "flags": 0}, False),  # no fix
        ({"fix_type": 1}, False),  # dead reckoning only
        ({"fix_type": 5}, False),  # time only
        ({"flags": 0x00}, False),  # 3D but outside the DOP and accuracy masks
        ({"flags": 0x3C}, False),  # other flag bits set, gnssFixOK clear
        ({"flags3": 0x01}, False),  # invalidLlh
        ({"flags3": 0x1E}, True),  # lastCorrectionAge only
    ],
)
def test_usable_fix(fields: dict[str, int], usable: bool) -> None:
    assert decode_nav_pvt(nav_pvt(**fields)).usable is usable


@pytest.mark.parametrize(
    ("fields", "utc"),
    [
        ({"valid": 0x07}, UTC0_NS),
        ({"valid": 0x0F}, UTC0_NS),  # validMag does not matter
        ({"valid": 0x03}, None),  # not fully resolved: the leap seconds may be unknown
        ({"valid": 0x06}, None),  # no valid date
        ({"valid": 0x05}, None),  # no valid time
        ({"valid": 0x07, "month": 13}, None),
        ({"valid": 0x07, "month": 2, "day": 30}, None),
        ({"valid": 0x07, "hour": 24}, None),
        ({"valid": 0x07, "second": 61}, None),
        ({"valid": 0x07, "nano": 1_000_000_001}, None),
    ],
)
def test_utc_needs_valid_date_time_and_full_resolution(fields: dict[str, int], utc: int) -> None:
    assert decode_nav_pvt(nav_pvt(**fields)).utc_ns == utc


def test_utc_applies_a_negative_nano_field() -> None:
    # 12:34:56 minus 1 ms is how the receiver may report 12:34:55.999.
    pvt = decode_nav_pvt(nav_pvt(nano=-MS))
    assert pvt.utc_ns == int(UTC0.timestamp()) * S - MS


def test_fix_from_nav_pvt_is_si() -> None:
    fix = fix_from_nav_pvt(decode_nav_pvt(nav_pvt()), t_ns=11, rx_t_ns=12)
    assert fix == GpsFix(
        t_ns=11,
        rx_t_ns=12,
        utc_ns=UTC0_NS,
        lat_deg=-765_432 / 1e7,
        lon_deg=1_234_567 / 1e7,
        alt_m=12.345,
        speed_mps=13.41,
        course_rad=fix.course_rad,
        hdop=1.23,
        fix_type=3,
        n_sats=12,
    )
    assert fix.course_rad == pytest.approx(math.pi / 2, abs=1e-12)


# Configuration.


def test_valset_payload_for_10_hz() -> None:
    assert valset_payload(config_pairs(10.0)) == VALSET_10HZ


@pytest.mark.parametrize(("rate", "ms"), [(10.0, 100), (25.0, 40), (1.0, 1000), (4.0, 250)])
def test_measurement_period(rate: float, ms: int) -> None:
    assert measurement_period_ms(rate) == ms


@pytest.mark.parametrize("rate", [0.0, -1.0, 25.5, math.nan, math.inf, 0.01])
def test_measurement_period_refuses_bad_rates(rate: float) -> None:
    with pytest.raises(ValueError, match=r"gps\.rate_hz"):
        measurement_period_ms(rate)


@pytest.mark.parametrize(
    ("key", "size"),
    [(0x10780002, 1), (0x20910009, 1), (0x30210001, 2), (0x40520001, 4), (0x50360006, 8)],
)
def test_key_value_size_comes_from_the_key_id(key: int, size: int) -> None:
    assert key_value_bytes(key) == size


def test_key_value_size_refuses_an_unknown_size() -> None:
    with pytest.raises(ValueError):
        key_value_bytes(0x00000001)


def test_opening_writes_the_valset_and_waits_for_its_ack() -> None:
    clock = SimClock(MONO0)
    port = FakeUbloxPort(clock)
    GpsUbloxSource(gps_cfg(), clock, port=port)
    assert port.written == [frame(0x06, 0x8A, VALSET_10HZ)]
    assert not port.closed


def test_opening_skips_other_traffic_before_the_ack() -> None:
    clock = SimClock(MONO0)
    port = FakeUbloxPort(clock, reply=NMEA + pvt_frame() + ACK[:5])
    port.reads.append((MONO0, ACK[5:]))
    src = GpsUbloxSource(gps_cfg(), clock, port=port)
    assert src.parser.frames == 2


def test_opening_fails_plainly_on_nak() -> None:
    clock = SimClock(MONO0)
    port = FakeUbloxPort(clock, reply=NAK)
    with pytest.raises(GpsUnavailable, match=r"refused its settings \(UBX-ACK-NAK\)\.$"):
        GpsUbloxSource(gps_cfg(), clock, port=port)
    assert not port.closed  # an injected port stays open


def test_opening_fails_plainly_without_an_answer() -> None:
    clock = SimClock(MONO0)
    port = FakeUbloxPort(clock, reply=None)
    with pytest.raises(GpsUnavailable, match=r"did not confirm its settings within 1 s\.$"):
        GpsUbloxSource(gps_cfg(), clock, port=port)
    assert clock.now_ns() - MONO0 == S


def test_bad_rate_fails_before_any_write() -> None:
    clock = SimClock(MONO0)
    port = FakeUbloxPort(clock)
    with pytest.raises(ValueError):
        GpsUbloxSource(gps_cfg(rate_hz=40.0), clock, port=port)
    assert port.written == []


# Streaming.


def epoch(k: int) -> tuple[int, int]:
    """UTC and true monotonic time of navigation epoch k at 10 Hz."""
    utc = UTC0_NS + k * 100 * MS
    return utc, utc - UTC0_NS + MONO0


def test_records_yield_usable_fixes_from_a_split_stream() -> None:
    utc = [epoch(k)[0] for k in range(5)]
    bad = bytearray(pvt_frame(utc[1]))
    bad[70] ^= 0xFF
    stream = [
        NMEA + pvt_frame(utc[0], itow=0),
        bytes(bad),
        pvt_frame(utc[2], itow=2, fix_type=0, flags=0),
        b"\x00" * 7 + pvt_frame(utc[3], itow=3),
        frame(0x05, 0x01, b"\x06\x01"),
        pvt_frame(utc[4], itow=4),
    ]
    blob = b"".join(stream)
    cuts = [0, 10, 150, 151, 300, 460, len(blob)]
    spans = list(itertools.pairwise(cuts))
    reads = [(epoch(i)[1] + 40 * MS, blob[a:b]) for i, (a, b) in enumerate(spans)]
    clock = SimClock(MONO0 - S)
    port = FakeUbloxPort(clock, reads)
    src = GpsUbloxSource(gps_cfg(), clock, port=port)
    fixes = run(src, port)

    ends = [sum(map(len, stream[: i + 1])) for i in range(len(stream))]

    def rx_of(end: int) -> int:
        i = next(i for i, (a, b) in enumerate(spans) if a < end <= b)
        return reads[i][0]

    assert [f.utc_ns for f in fixes] == [utc[0], utc[3], utc[4]]
    assert [f.rx_t_ns for f in fixes] == [rx_of(ends[0]), rx_of(ends[3]), rx_of(ends[5])]
    assert (src.fixes, src.unusable_fixes, src.parser.bad_checksums) == (3, 1, 1)
    assert all(f.t_ns <= f.rx_t_ns for f in fixes)
    assert [f.t_ns for f in fixes] == sorted(f.t_ns for f in fixes)
    assert all(f.lat_deg == -765_432 / 1e7 and f.fix_type == 3 for f in fixes)
    assert src.failure is None


def test_records_raise_a_plain_message_when_the_port_closes() -> None:
    clock = SimClock(MONO0)
    port = FakeUbloxPort(clock, [(MONO0 + 40 * MS, pvt_frame())], eof=True)
    src = GpsUbloxSource(gps_cfg(), clock, port=port)
    gen = src.records()
    assert isinstance(next(gen), GpsFix)
    with pytest.raises(GpsUnavailable) as err:
        next(gen)
    assert str(err.value) == "The GPS on /dev/ttyACM0 stopped (end of script)."
    assert isinstance(err.value.__cause__, SerialClosed)
    assert src.failure == str(err.value)


def test_records_stamp_fixes_on_the_lowest_delay() -> None:
    delays_ms = [30, 12, 45, 20, 12, 60]
    reads = []
    for k, d in enumerate(delays_ms):
        utc, mono = epoch(k)
        reads.append((mono + d * MS, pvt_frame(utc)))
    clock = SimClock(MONO0 - S)
    port = FakeUbloxPort(clock, reads)
    src = GpsUbloxSource(gps_cfg(), clock, port=port)
    fixes = run(src, port)
    lag_ms = [(f.t_ns - epoch(k)[1]) / MS for k, f in enumerate(fixes)]
    assert lag_ms == [30, 12, 12, 12, 12, 12]


# The time anchor.


def test_anchor_takes_the_lowest_offset_in_its_window() -> None:
    anchor = UtcAnchor(window_ns=30 * S)
    lags = []
    for k in range(400):  # 40 s at 10 Hz
        utc, mono = epoch(k)
        delay = 12 * MS if k == 5 else 20 * MS
        lags.append(anchor.stamp(utc, mono + delay) - mono)
    assert lags[:5] == [20 * MS] * 5
    assert set(lags[5:306]) == {12 * MS}  # fix 5 stays the minimum for 30 s of UTC
    assert set(lags[306:]) == {20 * MS}


def test_anchor_starts_over_when_utc_does_not_advance() -> None:
    anchor = UtcAnchor()
    for k in range(10):
        utc, mono = epoch(k)
        anchor.stamp(utc, mono + 10 * MS)
    # A leap second: the receiver repeats a UTC second, so the monotonic clock minus UTC
    # grows by 1 s. The old, lower offsets must not pull later fixes 1 s early.
    for k in range(10, 20):
        utc, mono = epoch(k)
        t = anchor.stamp(utc - S, mono + 10 * MS)
        assert t == mono + 10 * MS


def test_anchor_without_utc_uses_the_receive_time_and_never_goes_back() -> None:
    anchor = UtcAnchor()
    assert anchor.stamp(None, MONO0 + 500 * MS) == MONO0 + 500 * MS
    utc, mono = epoch(4)
    # UTC arrives with a smaller delay: the stamp would go back, so it holds.
    assert anchor.stamp(utc, mono + 5 * MS) == MONO0 + 500 * MS
    utc, mono = epoch(6)
    assert anchor.stamp(utc, mono + 5 * MS) == mono + 5 * MS


def test_anchor_never_stamps_after_the_receive_time() -> None:
    anchor = UtcAnchor()
    for k, d in enumerate([50, 5, 80, 1, 300, 2]):
        utc, mono = epoch(k)
        assert anchor.stamp(utc, mono + d * MS) <= mono + d * MS


# The POSIX serial port, on a pseudo terminal.


@pytest.fixture
def pty() -> Iterator[tuple[int, int, str]]:
    """A pseudo terminal: (master fd, slave fd, slave path). The source opens the path."""
    master, slave = os.openpty()
    path = os.ttyname(slave)
    try:
        yield master, slave, path
    finally:
        os.close(slave)
        with contextlib.suppress(OSError):
            os.close(master)


def read_exactly(port: PosixSerialPort, n: int) -> bytes:
    got = b""
    for _ in range(50):
        got += port.read(n - len(got), 0.1)
        if len(got) >= n:
            break
    return got


def test_posix_port_is_raw(pty: tuple[int, int, str]) -> None:
    master, slave, path = pty
    port = PosixSerialPort(path)
    try:
        iflag, oflag, cflag, lflag, _, _, _ = termios.tcgetattr(slave)
        assert lflag & (termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN) == 0
        assert iflag & (termios.ICRNL | termios.INLCR | termios.IGNCR | termios.IXON) == 0
        assert iflag & termios.ISTRIP == 0
        assert oflag & termios.OPOST == 0
        assert cflag & termios.CSIZE == termios.CS8
        assert cflag & termios.PARENB == 0
        assert cflag & (termios.CLOCAL | termios.CREAD) == termios.CLOCAL | termios.CREAD
        # Bytes that a cooked tty would change or eat: CR, LF, Ctrl-C, XON, XOFF, 0xB5.
        raw = b"\xb5\x62\r\n\x03\x11\x13\x00\xff"
        os.write(master, raw)
        assert read_exactly(port, len(raw)) == raw
        port.write(b"\xb5\x62\n\r")
        assert os.read(master, 16) == b"\xb5\x62\n\r"
        assert port.read(16, 0.05) == b""
    finally:
        port.close()
        port.close()


def test_posix_port_raises_when_the_device_goes_away(pty: tuple[int, int, str]) -> None:
    master, _, path = pty
    port = PosixSerialPort(path)
    try:
        os.close(master)
        with pytest.raises(OSError):
            for _ in range(20):
                port.read(16, 0.1)
    finally:
        port.close()


def test_posix_port_refuses_a_missing_device(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        PosixSerialPort(str(tmp_path / "ttyACM9"))


def test_source_configures_and_streams_over_a_real_tty(pty: tuple[int, int, str]) -> None:
    master, _, path = pty
    got: list[bytes] = []
    opened = threading.Event()

    def receiver() -> None:
        buf = b""
        while len(buf) < 8 + len(VALSET_10HZ):
            buf += os.read(master, 256)
        got.append(buf)
        os.write(master, NMEA + ACK)
        opened.wait(5)
        os.write(master, b"".join(pvt_frame(epoch(k)[0], itow=k) for k in range(3)))

    threading.Thread(target=receiver, daemon=True).start()
    src = GpsUbloxSource(gps_cfg(device=path), MonotonicClock())
    opened.set()
    guard = threading.Timer(5.0, src.stop)
    guard.start()
    fixes: list[GpsFix] = []
    try:
        for rec in src.records():
            assert isinstance(rec, GpsFix)
            fixes.append(rec)
            if len(fixes) == 3:
                src.stop()
    finally:
        guard.cancel()
    assert got == [frame(0x06, 0x8A, VALSET_10HZ)]
    assert [f.utc_ns for f in fixes] == [epoch(k)[0] for k in range(3)]
    assert [f.t_ns for f in fixes] == sorted(f.t_ns for f in fixes)
    assert all(f.t_ns <= f.rx_t_ns for f in fixes)
    assert src.meta.kind == "live"
