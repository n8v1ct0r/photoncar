"""OBD-II PID decoding: known answers, exhaustive round trips, frames, bitmaps."""

from __future__ import annotations

import logging
import math
import random

import can
import pytest
from hypothesis import given
from hypothesis import strategies as st

from photoncar.config import load_config
from photoncar.decode.obd_pids import (
    PID_DEFS,
    SUPPORT_PIDS,
    bitmaps_for,
    decode_pid,
    encode_pid,
    parse_negative_response,
    parse_response,
    request_frame,
    supported_pids,
)
from photoncar.signals import OBD_PID_TO_SIGNAL, SIGNALS


def reply(arb_id: int, payload: bytes, *, ext: bool = False) -> can.Message:
    data = bytes([len(payload)]) + payload
    data += bytes([0x55] * (8 - len(data)))
    return can.Message(arbitration_id=arb_id, is_extended_id=ext, data=data, dlc=8)


# Known answers. Exact where the arithmetic is exact in binary floating point; otherwise
# the tolerance is stated.


def test_rpm_known_answer() -> None:
    assert decode_pid(0x0C, bytes([0x1A, 0xF8])) == 1726.0


def test_coolant_known_answer() -> None:
    # 0x7B = 123 -> 83 degC = 356.15 K (tolerance 1e-9 K).
    assert decode_pid(0x05, bytes([0x7B])) == pytest.approx(356.15, abs=1e-9)


def test_intake_air_zero_c() -> None:
    assert decode_pid(0x0F, bytes([0x28])) == pytest.approx(273.15, abs=1e-9)


def test_speed_known_answer() -> None:
    # 0x64 = 100 km/h = 27.777... m/s (relative tolerance 1e-12).
    assert decode_pid(0x0D, bytes([0x64])) == pytest.approx(100 / 3.6, rel=1e-12)


@pytest.mark.parametrize(
    ("raw", "expected"), [(0x80, 0.0), (0xA0, 0.25), (0x00, -1.0), (0xFF, 0.9921875)]
)
@pytest.mark.parametrize("pid", [0x06, 0x07, 0x08, 0x09])
def test_trim_known_answers(pid: int, raw: int, expected: float) -> None:
    assert decode_pid(pid, bytes([raw])) == expected


def test_module_voltage_known_answer() -> None:
    assert decode_pid(0x42, bytes([0x37, 0x5B])) == pytest.approx(14.171, abs=1e-12)


def test_maf_known_answer() -> None:
    # 0x01F4 = 500 -> 5.00 g/s = 0.005 kg/s (tolerance 1e-15 kg/s).
    assert decode_pid(0x10, bytes([0x01, 0xF4])) == pytest.approx(0.005, abs=1e-15)


def test_ratio_known_answers() -> None:
    assert decode_pid(0x04, bytes([0xFF])) == 1.0
    assert decode_pid(0x11, bytes([0x33])) == pytest.approx(0.2, abs=1e-15)
    assert decode_pid(0x11, bytes([0x00])) == 0.0


def test_fuel_system_reports_system_1_enum() -> None:
    assert decode_pid(0x03, bytes([0x02, 0x00])) == 2.0
    assert decode_pid(0x03, bytes([0x04, 0x01])) == 4.0


def test_bitmap_decodes_to_word() -> None:
    assert decode_pid(0x00, bytes([0xBE, 0x1F, 0xA8, 0x13])) == float(0xBE1FA813)


def test_short_or_unknown_payload_is_none() -> None:
    assert decode_pid(0x0C, bytes([0x1A])) is None
    assert decode_pid(0x05, b"") is None
    assert decode_pid(0xA6, bytes([1, 2, 3, 4])) is None


# Coverage of the catalog and config.


def test_pid_defs_cover_bitmaps_and_every_config_pid() -> None:
    obd = load_config().section("obd")
    wanted = SUPPORT_PIDS | set(obd["fast_pids"]) | set(obd["slow_pids"])
    assert wanted <= set(PID_DEFS)


def test_pid_units_match_signal_catalog() -> None:
    for pid, d in PID_DEFS.items():
        if pid in SUPPORT_PIDS:
            assert d.unit == "bitmap"
            continue
        assert d.name == OBD_PID_TO_SIGNAL[pid]
        assert d.unit == SIGNALS[d.name].unit, hex(pid)


# Round trips.


def _raw_values(pid: int) -> list[int]:
    d = PID_DEFS[pid]
    if pid == 0x03:
        return [a << 8 for a in range(256)]  # byte B (fuel system 2) is not reported
    if d.n_bytes <= 2:
        return list(range(d.raw_max + 1))
    rng = random.Random(0x0B0)
    return [0, d.raw_max, *(rng.randrange(d.raw_max + 1) for _ in range(5000))]


@pytest.mark.parametrize("pid", sorted(PID_DEFS))
def test_every_pid_round_trips_raw_decode_encode(pid: int) -> None:
    d = PID_DEFS[pid]
    for raw in _raw_values(pid):
        data = raw.to_bytes(d.n_bytes, "big")
        value = decode_pid(pid, data)
        assert value is not None
        assert encode_pid(pid, value) == data, (hex(pid), raw)


_RANGES: dict[int, tuple[float, float, float]] = {
    # pid: (lo, hi, one raw step in SI)
    0x04: (0.0, 1.0, 1 / 255),
    0x05: (233.15, 488.15, 1.0),
    0x06: (-1.0, 0.9921875, 1 / 128),
    0x0C: (0.0, 16383.75, 0.25),
    0x0D: (0.0, 255 / 3.6, 1 / 3.6),
    0x10: (0.0, 0.65535, 1e-5),
    0x11: (0.0, 1.0, 1 / 255),
    0x42: (0.0, 65.535, 0.001),
}


@given(pid=st.sampled_from(sorted(_RANGES)), frac=st.floats(min_value=0.0, max_value=1.0))
def test_encode_then_decode_is_within_half_a_step(pid: int, frac: float) -> None:
    lo, hi, step = _RANGES[pid]
    value = lo + frac * (hi - lo)
    decoded = decode_pid(pid, encode_pid(pid, value))
    assert decoded is not None
    assert abs(decoded - value) <= step / 2 + 1e-9


def test_encode_rejects_out_of_range_and_non_finite() -> None:
    with pytest.raises(ValueError):
        encode_pid(0x0D, -1.0)
    with pytest.raises(ValueError):
        encode_pid(0x05, 1000.0)
    with pytest.raises(ValueError):
        encode_pid(0x0C, math.nan)
    with pytest.raises(ValueError):
        encode_pid(0xA6, 1.0)


# Frames.


def test_request_frame_shape() -> None:
    msg = request_frame(0x0C)
    assert msg.arbitration_id == 0x7DF
    assert not msg.is_extended_id
    assert not msg.is_remote_frame
    assert not msg.is_fd
    assert msg.dlc == 8
    assert bytes(msg.data) == bytes([0x02, 0x01, 0x0C, 0xCC, 0xCC, 0xCC, 0xCC, 0xCC])
    assert request_frame(0x05, 0x7E0).arbitration_id == 0x7E0


def test_request_frame_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        request_frame(0x100)
    with pytest.raises(ValueError):
        request_frame(0x0C, 0x18DB33F1)


@pytest.mark.parametrize("arb_id", [0x7E8, 0x7EF])
def test_parse_response_single_frame(arb_id: int) -> None:
    assert parse_response(reply(arb_id, bytes([0x41, 0x0C, 0x1A, 0xF8]))) == (
        0x0C,
        bytes([0x1A, 0xF8]),
    )


def test_parse_response_rejects_other_shapes() -> None:
    good = bytes([0x41, 0x05, 0x7B])
    assert parse_response(reply(0x7E7, good)) is None  # a request ID
    assert parse_response(reply(0x7F0, good)) is None
    assert parse_response(reply(0x7E8, good, ext=True)) is None
    assert parse_response(reply(0x7E8, bytes([0x41, 0x05]))) is None  # payload too short
    assert parse_response(reply(0x7E8, bytes([0x41, 0x0C, 0x1A]))) is None  # rpm needs 2
    first_frame = can.Message(
        arbitration_id=0x7E8,
        is_extended_id=False,
        data=bytes([0x10, 0x14, 0x49, 0x02, 0x01, 0x57, 0x42, 0x41]),
    )
    assert parse_response(first_frame) is None
    rtr = can.Message(arbitration_id=0x7E8, is_extended_id=False, is_remote_frame=True, dlc=8)
    assert parse_response(rtr) is None


def test_negative_response_is_none_and_logged_as_nrc(caplog: pytest.LogCaptureFixture) -> None:
    msg = reply(0x7E8, bytes([0x7F, 0x01, 0x12]))
    with caplog.at_level(logging.INFO, logger="photoncar.decode.obd_pids"):
        assert parse_response(msg) is None
    assert any(r.getMessage().startswith("nrc 0x12") for r in caplog.records)
    assert parse_negative_response(msg) == (0x01, 0x12)
    assert parse_negative_response(reply(0x7E8, bytes([0x41, 0x05, 0x7B]))) is None


# Support bitmaps.


def test_supported_pids_known_answer() -> None:
    # The bitmap BE 1F A8 13 (SAE J1979 example).
    got = supported_pids({0x00: bytes([0xBE, 0x1F, 0xA8, 0x13])})
    expected = {0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11}
    expected |= {0x13, 0x15, 0x1C, 0x1F, 0x20}
    assert got == expected


def test_supported_pids_uses_each_base() -> None:
    got = supported_pids(
        {0x00: bytes([0, 0, 0, 1]), 0x20: bytes([0, 0, 0, 1]), 0x40: bytes([0x40, 0, 0, 0])}
    )
    assert got == {0x20, 0x40, 0x42}


def test_supported_pids_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        supported_pids({0x10: bytes(4)})
    with pytest.raises(ValueError):
        supported_pids({0x00: bytes(3)})


@given(st.sets(st.integers(min_value=1, max_value=0x60), min_size=1))
def test_bitmaps_for_round_trips(pids: set[int]) -> None:
    expected = set(pids)
    if max(pids) > 0x20:
        expected.add(0x20)
    if max(pids) > 0x40:
        expected.add(0x40)
    assert supported_pids(bitmaps_for(pids)) == expected
