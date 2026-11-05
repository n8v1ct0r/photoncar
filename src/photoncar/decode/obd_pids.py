"""OBD-II service 01 PIDs: decode to SI, encode for the bench emulator, and frame helpers.

Formulas follow SAE J1979 as listed in docs/phase-1-plan.md section 3. Every decoded value
is SI: kelvin, m/s, kg/s, volts, a 0..1 ratio, or a signed fraction for fuel trims
(+0.25 means +25%). PID 0x03 returns the raw enum of fuel system 1. The support bitmaps
(0x00, 0x20, 0x40) decode to their 32-bit value as a float.

Only single-frame replies are handled. Photoncar never sends ISO-TP flow control, so a
multi-frame reply can never complete (hard rule 3).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import can

from photoncar.signals import OBD_PID_TO_SIGNAL
from photoncar.units import KELVIN_OFFSET, KPH

log = logging.getLogger(__name__)

SERVICE_CURRENT_DATA = 0x01
POSITIVE_RESPONSE = 0x41
NEGATIVE_RESPONSE = 0x7F
NRC_RESPONSE_PENDING = 0x78
REQUEST_PAD = 0xCC
RESPONSE_IDS = range(0x7E8, 0x7F0)
SUPPORT_PIDS: frozenset[int] = frozenset({0x00, 0x20, 0x40})


@dataclass(frozen=True, slots=True)
class PidDef:
    """One service 01 PID: payload size, SI unit, and the raw <-> SI mapping."""

    pid: int
    name: str
    n_bytes: int
    unit: str
    to_si: Callable[[int], float]
    from_si: Callable[[float], float]

    @property
    def raw_max(self) -> int:
        return (1 << (8 * self.n_bytes)) - 1


def _raw_identity(raw: int) -> float:
    return float(raw)


def _bitmap(pid: int) -> PidDef:
    return PidDef(pid, f"support_{pid:02x}", 4, "bitmap", _raw_identity, float)


def _temp(pid: int) -> PidDef:
    # A - 40 degC, stored in kelvin.
    return PidDef(
        pid,
        OBD_PID_TO_SIGNAL[pid],
        1,
        "K",
        lambda raw: raw - 40 + KELVIN_OFFSET,
        lambda k: k - KELVIN_OFFSET + 40,
    )


def _ratio_255(pid: int) -> PidDef:
    # 100A/255 %, stored as a 0..1 ratio.
    return PidDef(pid, OBD_PID_TO_SIGNAL[pid], 1, "ratio", lambda raw: raw / 255, lambda r: r * 255)


def _trim(pid: int) -> PidDef:
    # 100A/128 - 100 %, stored as a signed fraction.
    return PidDef(
        pid,
        OBD_PID_TO_SIGNAL[pid],
        1,
        "ratio",
        lambda raw: raw / 128 - 1.0,
        lambda f: (f + 1.0) * 128,
    )


def _fuel_system(raw: int) -> float:
    # Two bytes: A is fuel system 1, B is fuel system 2. Only system 1 is reported.
    return float(raw >> 8)


def _fuel_system_encode(value: float) -> float:
    return float(round(value) * 256)


PID_DEFS: dict[int, PidDef] = {
    d.pid: d
    for d in (
        _bitmap(0x00),
        _bitmap(0x20),
        _bitmap(0x40),
        PidDef(0x03, OBD_PID_TO_SIGNAL[0x03], 2, "enum", _fuel_system, _fuel_system_encode),
        _ratio_255(0x04),
        _temp(0x05),
        _trim(0x06),
        _trim(0x07),
        _trim(0x08),
        _trim(0x09),
        # (256A+B)/4 rpm.
        PidDef(0x0C, OBD_PID_TO_SIGNAL[0x0C], 2, "rpm", lambda raw: raw / 4, lambda v: v * 4),
        # A km/h, stored in m/s.
        PidDef(0x0D, OBD_PID_TO_SIGNAL[0x0D], 1, "m/s", lambda raw: raw * KPH, lambda v: v / KPH),
        _temp(0x0F),
        # (256A+B)/100 g/s, stored in kg/s.
        PidDef(
            0x10,
            OBD_PID_TO_SIGNAL[0x10],
            2,
            "kg/s",
            lambda raw: raw / 100 / 1000,
            lambda v: v * 1000 * 100,
        ),
        _ratio_255(0x11),
        # (256A+B)/1000 V.
        PidDef(0x42, OBD_PID_TO_SIGNAL[0x42], 2, "V", lambda raw: raw / 1000, lambda v: v * 1000),
    )
}


def decode_pid(pid: int, data: bytes) -> float | None:
    """Decode a service 01 payload (the bytes after the PID) to SI. None if unknown or short."""
    d = PID_DEFS.get(pid)
    if d is None or len(data) < d.n_bytes:
        return None
    return d.to_si(int.from_bytes(data[: d.n_bytes], "big"))


def encode_pid(pid: int, value: float) -> bytes:
    """Inverse of decode_pid, rounding to the nearest raw step. Raises ValueError out of range."""
    d = PID_DEFS.get(pid)
    if d is None:
        raise ValueError(f"unknown PID 0x{pid:02X}")
    if not math.isfinite(value):
        raise ValueError(f"PID 0x{pid:02X}: value must be finite")
    raw = round(d.from_si(value))
    if not 0 <= raw <= d.raw_max:
        raise ValueError(f"PID 0x{pid:02X}: {value} is outside the encodable range")
    return raw.to_bytes(d.n_bytes, "big")


def request_frame(pid: int, arb_id: int = 0x7DF) -> can.Message:
    """A single-PID service 01 request: [02 01 pid CC CC CC CC CC], DLC 8, 11-bit ID."""
    if not 0 <= pid <= 0xFF:
        raise ValueError(f"PID out of range: {pid}")
    if not 0 <= arb_id <= 0x7FF:
        raise ValueError(f"not an 11-bit ID: {arb_id:#x}")
    data = bytes([0x02, SERVICE_CURRENT_DATA, pid]) + bytes([REQUEST_PAD] * 5)
    return can.Message(arbitration_id=arb_id, is_extended_id=False, data=data, dlc=8)


def _single_frame_payload(msg: can.Message) -> bytes | None:
    """The ISO-TP single-frame payload of an 11-bit classic reply from 0x7E8..0x7EF."""
    if (
        msg.is_extended_id
        or msg.is_remote_frame
        or msg.is_error_frame
        or msg.is_fd
        or msg.arbitration_id not in RESPONSE_IDS
    ):
        return None
    data = bytes(msg.data)
    if not data or data[0] & 0xF0 != 0x00:
        return None
    length = data[0] & 0x0F
    if length == 0 or len(data) < 1 + length:
        return None
    return data[1 : 1 + length]


def parse_response(msg: can.Message) -> tuple[int, bytes] | None:
    """Parse a single-frame service 0x41 reply into (pid, data). None for anything else.

    Negative responses (0x7F) return None and are logged as ``nrc``.
    """
    payload = _single_frame_payload(msg)
    if payload is None:
        return None
    if payload[0] == NEGATIVE_RESPONSE:
        nrc = parse_negative_response(msg)
        if nrc is not None:
            log.info(
                "nrc 0x%02X for service 0x%02X from 0x%03X", nrc[1], nrc[0], msg.arbitration_id
            )
        return None
    if payload[0] != POSITIVE_RESPONSE or len(payload) < 2:
        return None
    pid = payload[1]
    data = payload[2:]
    d = PID_DEFS.get(pid)
    if d is not None and len(data) != d.n_bytes:
        return None
    return pid, data


def parse_negative_response(msg: can.Message) -> tuple[int, int] | None:
    """Parse a single-frame negative response into (requested service, NRC code)."""
    payload = _single_frame_payload(msg)
    if payload is None or len(payload) < 3 or payload[0] != NEGATIVE_RESPONSE:
        return None
    return payload[1], payload[2]


def supported_pids(bitmaps: Mapping[int, bytes]) -> set[int]:
    """PIDs marked supported in the 0x00 / 0x20 / 0x40 bitmaps.

    Bit 7 of byte 0 in the bitmap for ``base`` is PID ``base + 1``; bit 0 of byte 3 is
    ``base + 0x20`` (which is itself the next bitmap PID).
    """
    out: set[int] = set()
    for base, data in bitmaps.items():
        if base not in SUPPORT_PIDS:
            raise ValueError(f"not a support bitmap PID: 0x{base:02X}")
        if len(data) < 4:
            raise ValueError(f"bitmap 0x{base:02X} needs 4 bytes")
        word = int.from_bytes(data[:4], "big")
        for i in range(32):
            if word & (1 << (31 - i)):
                out.add(base + i + 1)
    return out


def bitmaps_for(pids: set[int] | frozenset[int]) -> dict[int, bytes]:
    """The support bitmaps an ECU supporting ``pids`` reports (for the emulator and tests).

    Bit 0 of a bitmap marks the next bitmap PID. It is set when any PID above the bitmap's
    range is supported, and a bitmap is present only when the previous one points to it.
    """
    out: dict[int, bytes] = {}
    for base in sorted(SUPPORT_PIDS):
        word = 0
        for p in pids:
            if base < p <= base + 0x20:
                word |= 1 << (31 - (p - base - 1))
        if any(p > base + 0x20 for p in pids):
            word |= 1
        out[base] = word.to_bytes(4, "big")
        if not word & 1:
            break
    return out
