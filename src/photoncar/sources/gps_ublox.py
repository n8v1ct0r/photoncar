"""Live GPS: a u-blox NEO-M9N on its USB serial port (CDC-ACM). Standard library only.

This module talks only to the GPS module. It never touches a vehicle bus.

Protocol (u-blox M9 interface description; every key ID, message ID and NAV-PVT offset below
was cross-checked against the pyubx2 1.3.7 tables):

- A UBX frame is ``0xB5 0x62``, class, id, a little-endian U2 payload length, the payload,
  then ``CK_A CK_B``: the 8-bit Fletcher checksum over class, id, length and payload.
- Configuration is 1 UBX-CFG-VALSET (0x06 0x8A), version 0, RAM layer only, so a power cycle
  restores the receiver's own saved settings and nothing is written to its flash. Keys:
  - CFG-RATE-MEAS 0x30210001 (U2, ms) = 1000 / ``gps.rate_hz`` (100 ms at 10 Hz).
  - CFG-RATE-NAV 0x30210002 (U2) = 1 navigation solution per measurement.
  - CFG-MSGOUT-UBX_NAV_PVT_USB 0x20910009 (U1) = 1 NAV-PVT per solution on USB.
  - CFG-USBOUTPROT-UBX 0x10780001 (L) = 1 and CFG-USBOUTPROT-NMEA 0x10780002 (L) = 0, so the
    USB port carries UBX only. The parser still skips NMEA text if any arrives.
  The receiver confirms with UBX-ACK-ACK (0x05 0x01) or refuses with UBX-ACK-NAK (0x05 0x00),
  each naming 0x06 0x8A in its payload.
- UBX-NAV-PVT (0x01 0x07) has a 92-byte payload. Fields read: iTOW (0, U4 ms), year .. sec
  (4 .. 10), valid (11: validDate 0x01, validTime 0x02, fullyResolved 0x04), nano (16, I4 ns),
  fixType (20), flags (21: gnssFixOK 0x01), numSV (23), lon and lat (24 and 28, I4 1e-7 deg),
  hMSL (36, I4 mm), gSpeed (60, I4 mm/s), headMot (64, I4 1e-5 deg), pDOP (76, U2 0.01) and
  flags3 (78: invalidLlh 0x01).

Fix to ``GpsFix``: a fix is kept only when gnssFixOK is set, fixType is 2 (2D), 3 (3D) or 4
(GNSS plus dead reckoning), and invalidLlh is clear; the rest are counted and dropped, so a
gap in fixes means no GPS. NAV-PVT carries no HDOP, so ``hdop`` holds pDOP. PDOP squared is
HDOP squared plus VDOP squared, so this overstates HDOP and never understates it. ``utc_ns``
comes from the date and time fields when validDate, validTime and fullyResolved are all set.

Time base (docs/phase-1-plan.md section 5): ``rx_t_ns`` is the monotonic time the read that
completed the frame returned. The monotonic clock minus UTC plus the delivery delay is
``rx_t_ns - utc_ns``; its lowest value over the last 30 s (the plan's "GPS fit 30 s") carries
the least delay, and ``t_ns = utc_ns`` plus that value. A fix without valid UTC takes
``t_ns = rx_t_ns``. Fix times never go backwards and never pass their receive time.
"""

from __future__ import annotations

import calendar
import math
import os
import select
import struct
import termios
import threading
import tty
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from photoncar.clock import Clock
from photoncar.config import Config
from photoncar.records import GpsFix, Record
from photoncar.sources.base import SourceMeta
from photoncar.units import NS_PER_S, s_to_ns

SYNC = b"\xb5\x62"
HEADER_LEN = 6
CHECKSUM_LEN = 2
MAX_PAYLOAD = 1024
"""The longest payload the parser waits for (proposed). NAV-PVT is 92 bytes and ACK is 2; a
longer length field is treated as garbage, so 1 corrupt length cannot stall the stream."""

CLS_NAV, ID_NAV_PVT = 0x01, 0x07
CLS_ACK, ID_ACK_NAK, ID_ACK_ACK = 0x05, 0x00, 0x01
CLS_CFG, ID_CFG_VALSET = 0x06, 0x8A
NAV_PVT_LEN = 92

VALSET_VERSION = 0x00
LAYER_RAM = 0x01
KEY_RATE_MEAS = 0x30210001
KEY_RATE_NAV = 0x30210002
KEY_MSGOUT_NAV_PVT_USB = 0x20910009
KEY_USBOUTPROT_UBX = 0x10780001
KEY_USBOUTPROT_NMEA = 0x10780002
KEY_VALUE_BYTES = {0x01: 1, 0x02: 1, 0x03: 2, 0x04: 4, 0x05: 8}
"""Value size by bits 28..30 of a key ID; a 1-bit value (code 0x01) takes 1 byte."""

MAX_RATE_HZ = 25.0
"""The NEO-M9N's top navigation rate (docs/phase-1-plan.md section 2.1)."""
ACK_TIMEOUT_S = 1.0
"""How long configuration waits for ACK-ACK or ACK-NAK (proposed)."""
READ_TIMEOUT_S = 0.2
READ_SIZE = 4096
WRITE_TIMEOUT_S = 1.0
OFFSET_WINDOW_NS = 30 * NS_PER_S
"""The lower-envelope window: the plan's "GPS fit 30 s" (section 13, approved)."""

VALID_DATE, VALID_TIME, FULLY_RESOLVED = 0x01, 0x02, 0x04
UTC_VALID = VALID_DATE | VALID_TIME | FULLY_RESOLVED
GNSS_FIX_OK = 0x01
INVALID_LLH = 0x01
USABLE_FIX_TYPES = frozenset({2, 3, 4})

_NAV_PVT = struct.Struct("<IHBBBBBBIiBBBBiiiiIIiiiiiIIHH")
"""The first 80 bytes of NAV-PVT, iTOW through flags3."""


class GpsUnavailable(RuntimeError):
    """The GPS did not answer or refused its configuration."""


class SerialClosed(OSError):
    """The serial device went away (end of file on a read)."""


# UBX framing. Pure: no I/O and no clock.


def ubx_checksum(data: bytes | bytearray) -> tuple[int, int]:
    """8-bit Fletcher checksum over class, id, length and payload: ``(CK_A, CK_B)``."""
    a = b = 0
    for byte in data:
        a = (a + byte) & 0xFF
        b = (b + a) & 0xFF
    return a, b


def ubx_frame(cls: int, msg_id: int, payload: bytes) -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("a UBX payload holds at most 65535 bytes")
    body = struct.pack("<BBH", cls, msg_id, len(payload)) + payload
    return SYNC + body + bytes(ubx_checksum(body))


@dataclass(frozen=True, slots=True)
class UbxFrame:
    cls: int
    msg_id: int
    payload: bytes


class UbxParser:
    """Splits a byte stream into checksum-verified UBX frames, in order.

    Anything that is not a frame (NMEA text, line noise, a cut frame) is skipped: the parser
    looks for the next sync pair. A frame whose checksum fails or whose length is over
    ``MAX_PAYLOAD`` is dropped, and the search restarts just after its sync pair, so a good
    frame inside the bad span is still found. Counters: ``frames``, ``bad_checksums``,
    ``bad_lengths``, ``skipped_bytes``.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.frames = 0
        self.bad_checksums = 0
        self.bad_lengths = 0
        self.skipped_bytes = 0

    @property
    def pending(self) -> int:
        """Bytes held while waiting for the rest of a frame."""
        return len(self._buf)

    def feed(self, data: bytes) -> list[UbxFrame]:
        buf = self._buf
        buf += data
        out: list[UbxFrame] = []
        while True:
            start = buf.find(SYNC)
            if start < 0:
                keep = 1 if buf.endswith(SYNC[:1]) else 0
                self.skipped_bytes += len(buf) - keep
                del buf[: len(buf) - keep]
                return out
            if start:
                self.skipped_bytes += start
                del buf[:start]
            if len(buf) < HEADER_LEN:
                return out
            length = buf[4] | (buf[5] << 8)
            if length > MAX_PAYLOAD:
                self.bad_lengths += 1
                self._skip_sync()
                continue
            end = HEADER_LEN + length + CHECKSUM_LEN
            if len(buf) < end:
                return out
            if ubx_checksum(buf[2 : HEADER_LEN + length]) != (buf[end - 2], buf[end - 1]):
                self.bad_checksums += 1
                self._skip_sync()
                continue
            out.append(UbxFrame(buf[2], buf[3], bytes(buf[HEADER_LEN : HEADER_LEN + length])))
            self.frames += 1
            del buf[:end]

    def _skip_sync(self) -> None:
        self.skipped_bytes += len(SYNC)
        del self._buf[: len(SYNC)]


def ack_result(frame: UbxFrame, cls: int, msg_id: int) -> bool | None:
    """True for ACK-ACK of ``(cls, msg_id)``, False for ACK-NAK of it, None for anything else."""
    if frame.cls != CLS_ACK or frame.payload[:2] != bytes((cls, msg_id)):
        return None
    if frame.msg_id == ID_ACK_ACK:
        return True
    if frame.msg_id == ID_ACK_NAK:
        return False
    return None


# Configuration.


def measurement_period_ms(rate_hz: float) -> int:
    """CFG-RATE-MEAS for a navigation rate: the nearest whole millisecond period."""
    if not math.isfinite(rate_hz) or not 0.0 < rate_hz <= MAX_RATE_HZ:
        raise ValueError(f"gps.rate_hz must be above 0 and at most 25. It is {rate_hz}.")
    period = round(1000.0 / rate_hz)
    if period > 0xFFFF:
        raise ValueError(f"gps.rate_hz {rate_hz} is too slow for the receiver.")
    return period


def key_value_bytes(key: int) -> int:
    code = (key >> 28) & 0x07
    if code not in KEY_VALUE_BYTES:
        raise ValueError(f"key {key:#010x} has no value size")
    return KEY_VALUE_BYTES[code]


def config_pairs(rate_hz: float) -> list[tuple[int, int]]:
    """The keys and values that set NAV-PVT output at ``rate_hz`` on USB."""
    return [
        (KEY_RATE_MEAS, measurement_period_ms(rate_hz)),
        (KEY_RATE_NAV, 1),
        (KEY_MSGOUT_NAV_PVT_USB, 1),
        (KEY_USBOUTPROT_UBX, 1),
        (KEY_USBOUTPROT_NMEA, 0),
    ]


def valset_payload(pairs: Sequence[tuple[int, int]], layers: int = LAYER_RAM) -> bytes:
    out = bytearray((VALSET_VERSION, layers, 0x00, 0x00))
    for key, value in pairs:
        out += struct.pack("<I", key) + value.to_bytes(key_value_bytes(key), "little")
    return bytes(out)


# NAV-PVT.


@dataclass(frozen=True, slots=True)
class NavPvt:
    """NAV-PVT fields in SI units (angles in degrees and radians as named)."""

    itow_ms: int
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    valid: int
    nano_ns: int
    fix_type: int
    flags: int
    num_sv: int
    lon_deg: float
    lat_deg: float
    hmsl_m: float
    ground_speed_mps: float
    heading_rad: float
    pdop: float
    flags3: int

    @property
    def usable(self) -> bool:
        return (
            bool(self.flags & GNSS_FIX_OK)
            and self.fix_type in USABLE_FIX_TYPES
            and not self.flags3 & INVALID_LLH
        )

    @property
    def utc_ns(self) -> int | None:
        """UTC nanoseconds since 1970 when the date and time are valid and fully resolved."""
        if self.valid & UTC_VALID != UTC_VALID:
            return None
        if not (1 <= self.month <= 12 and 0 <= self.hour <= 23 and 0 <= self.minute <= 59):
            return None
        if not 1 <= self.day <= calendar.monthrange(self.year, self.month)[1]:
            return None
        if not (0 <= self.second <= 60 and -NS_PER_S <= self.nano_ns <= NS_PER_S):
            return None
        fields = (self.year, self.month, self.day, self.hour, self.minute, self.second)
        return calendar.timegm((*fields, 0, 0, 0)) * NS_PER_S + self.nano_ns


def decode_nav_pvt(payload: bytes) -> NavPvt:
    if len(payload) != NAV_PVT_LEN:
        raise ValueError(f"NAV-PVT payload is {len(payload)} bytes, expected {NAV_PVT_LEN}")
    (
        itow,
        year,
        month,
        day,
        hour,
        minute,
        second,
        valid,
        _t_acc,
        nano,
        fix_type,
        flags,
        _flags2,
        num_sv,
        lon,
        lat,
        _height,
        hmsl,
        _h_acc,
        _v_acc,
        _vel_n,
        _vel_e,
        _vel_d,
        ground_speed,
        head_mot,
        _s_acc,
        _head_acc,
        pdop,
        flags3,
    ) = _NAV_PVT.unpack_from(payload)
    return NavPvt(
        itow_ms=itow,
        year=year,
        month=month,
        day=day,
        hour=hour,
        minute=minute,
        second=second,
        valid=valid,
        nano_ns=nano,
        fix_type=fix_type,
        flags=flags,
        num_sv=num_sv,
        lon_deg=lon / 1e7,
        lat_deg=lat / 1e7,
        hmsl_m=hmsl / 1000.0,
        ground_speed_mps=ground_speed / 1000.0,
        heading_rad=math.radians(head_mot / 1e5) % math.tau,
        pdop=pdop / 100.0,
        flags3=flags3,
    )


def fix_from_nav_pvt(pvt: NavPvt, *, t_ns: int, rx_t_ns: int) -> GpsFix:
    return GpsFix(
        t_ns=t_ns,
        rx_t_ns=rx_t_ns,
        utc_ns=pvt.utc_ns,
        lat_deg=pvt.lat_deg,
        lon_deg=pvt.lon_deg,
        alt_m=pvt.hmsl_m,
        speed_mps=pvt.ground_speed_mps,
        course_rad=pvt.heading_rad,
        hdop=pvt.pdop,
        fix_type=pvt.fix_type,
        n_sats=pvt.num_sv,
    )


class UtcAnchor:
    """Places fixes on the monotonic timeline from their UTC time and their receive time.

    ``rx_t_ns - utc_ns`` is the clock offset plus a delivery delay of 0 or more, so the lowest
    value over ``window_ns`` of UTC time is the best offset. UTC that does not move forward (a
    leap second, a receiver restart) starts the window over. Stamps never go backwards.
    """

    def __init__(self, window_ns: int = OFFSET_WINDOW_NS) -> None:
        self._window_ns = window_ns
        self._mins: deque[tuple[int, int]] = deque()
        """(utc_ns, offset_ns) with rising offsets: the front is the window minimum."""
        self._last_utc: int | None = None
        self._last_t: int | None = None

    def stamp(self, utc_ns: int | None, rx_t_ns: int) -> int:
        t = rx_t_ns if utc_ns is None else self._from_utc(utc_ns, rx_t_ns)
        if self._last_t is not None:
            t = max(t, self._last_t)
        self._last_t = t
        return t

    def _from_utc(self, utc_ns: int, rx_t_ns: int) -> int:
        mins = self._mins
        if self._last_utc is not None and utc_ns <= self._last_utc:
            mins.clear()
        self._last_utc = utc_ns
        offset = rx_t_ns - utc_ns
        while mins and mins[-1][1] >= offset:
            mins.pop()
        mins.append((utc_ns, offset))
        while mins[0][0] < utc_ns - self._window_ns:
            mins.popleft()
        return utc_ns + mins[0][1]


# The serial port.


class SerialPort(Protocol):
    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        """Up to ``max_bytes``; b"" on timeout. Raises OSError when the device is gone."""
        ...

    def write(self, data: bytes) -> None: ...

    def close(self) -> None: ...


def configure_raw(fd: int) -> None:
    """Raw 8-bit mode: no echo, no line editing, no signals, no CR or LF changes, no XON/XOFF.

    The speed stays as the driver set it: USB CDC-ACM ignores it.
    """
    attrs = termios.tcgetattr(fd)
    tty.cfmakeraw(attrs)
    attrs[2] |= termios.CLOCAL | termios.CREAD
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)


class PosixSerialPort:
    """A tty opened with ``os.open`` and set raw with ``termios``. Non-blocking, with select."""

    def __init__(self, device: str) -> None:
        fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            configure_raw(fd)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self.device = device

    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        ready, _, _ = select.select([self._fd], [], [], max(timeout_s, 0.0))
        if not ready:
            return b""
        try:
            data = os.read(self._fd, max_bytes)
        except BlockingIOError:
            return b""
        if not data:
            raise SerialClosed("the device closed")
        return data

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            _, ready, _ = select.select([], [self._fd], [], WRITE_TIMEOUT_S)
            if not ready:
                raise TimeoutError(f"{self.device} did not take data for {WRITE_TIMEOUT_S} s")
            try:
                view = view[os.write(self._fd, view) :]
            except BlockingIOError:
                continue

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


# The source.


class GpsUbloxSource:
    """Yields ``GpsFix`` records from a NEO-M9N. Opening it configures the receiver.

    Raises ``GpsUnavailable`` when the receiver does not confirm its configuration within
    ``ACK_TIMEOUT_S`` or refuses it, OSError when the device does not open, and ValueError
    for a bad ``gps.rate_hz``. ``records()`` raises ``GpsUnavailable`` with a plain message
    if the device goes away mid-drive. ``stop()`` ends ``records()`` within ``READ_TIMEOUT_S``.
    A ``port`` injected for tests is never closed by this class.
    """

    def __init__(self, cfg: Config, clock: Clock, *, port: SerialPort | None = None) -> None:
        self._device = str(cfg.get("gps.device"))
        pairs = config_pairs(float(cfg.get("gps.rate_hz")))
        self._clock = clock
        self._owned = port is None
        self._port: SerialPort = port if port is not None else PosixSerialPort(self._device)
        self._closed = False
        self._stop = threading.Event()
        self.parser = UbxParser()
        self._anchor = UtcAnchor()
        self.fixes = 0
        self.unusable_fixes = 0
        self.bad_nav_pvt = 0
        self.failure: str | None = None
        try:
            self._configure(pairs)
        except BaseException:
            self.close()
            raise

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(name=f"gps:{self._device}", kind="live")

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        if self._owned and not self._closed:
            self._port.close()
        self._closed = True

    def records(self) -> Iterator[Record]:
        try:
            while not self._stop.is_set():
                try:
                    chunk = self._port.read(READ_SIZE, READ_TIMEOUT_S)
                except OSError as exc:
                    self.failure = f"The GPS on {self._device} stopped ({exc})."
                    raise GpsUnavailable(self.failure) from exc
                if not chunk:
                    continue
                rx_t_ns = self._clock.now_ns()
                for frame in self.parser.feed(chunk):
                    fix = self._fix(frame, rx_t_ns)
                    if fix is not None:
                        yield fix
        finally:
            self.close()

    def _fix(self, frame: UbxFrame, rx_t_ns: int) -> GpsFix | None:
        if (frame.cls, frame.msg_id) != (CLS_NAV, ID_NAV_PVT):
            return None
        try:
            pvt = decode_nav_pvt(frame.payload)
        except ValueError:
            self.bad_nav_pvt += 1
            return None
        # Every NAV-PVT with valid UTC feeds the time anchor, even one without a usable fix.
        t_ns = self._anchor.stamp(pvt.utc_ns, rx_t_ns)
        if not pvt.usable:
            self.unusable_fixes += 1
            return None
        self.fixes += 1
        return fix_from_nav_pvt(pvt, t_ns=t_ns, rx_t_ns=rx_t_ns)

    def _configure(self, pairs: Sequence[tuple[int, int]]) -> None:
        self._port.write(ubx_frame(CLS_CFG, ID_CFG_VALSET, valset_payload(pairs)))
        deadline = self._clock.now_ns() + s_to_ns(ACK_TIMEOUT_S)
        while (remaining := deadline - self._clock.now_ns()) > 0:
            chunk = self._port.read(READ_SIZE, min(remaining / NS_PER_S, READ_TIMEOUT_S))
            for frame in self.parser.feed(chunk):
                ack = ack_result(frame, CLS_CFG, ID_CFG_VALSET)
                if ack is True:
                    return
                if ack is False:
                    raise GpsUnavailable(
                        f"The GPS on {self._device} refused its settings (UBX-ACK-NAK)."
                    )
        raise GpsUnavailable(
            f"The GPS on {self._device} did not confirm its settings within {ACK_TIMEOUT_S:g} s."
        )
