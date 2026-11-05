"""Live IMU: an ST LSM6DSOX on Linux I2C (``/dev/i2c-N``, ``I2C_RDWR``). Standard library only.

This module talks only to the IMU chip. It never touches a vehicle bus.

Registers (ST LSM6DSOX datasheet DS12814). WHO_AM_I, CTRL1_XL, CTRL2_G, the CTRL3_C reset
and BDU bits, the output data rate codes and the full-scale codes were cross-checked against
the Adafruit CircuitPython LSM6DS driver 4.6.3, and the FIFO tags 1 (gyroscope) and 2
(accelerometer) against ST's STM32duino LSM6DSOX library. The FIFO register addresses and
fields are from the datasheet register map:

- WHO_AM_I 0x0F reads 0x6C.
- CTRL3_C 0x12: SW_RESET bit 0 (clears itself), IF_INC bit 2 (address auto-increment),
  BDU bit 6.
- CTRL1_XL 0x10: ODR_XL bits 7:4, FS_XL bits 3:2 (00 = 2 g, 01 = 16 g, 10 = 4 g, 11 = 8 g).
- CTRL2_G 0x11: ODR_G bits 7:4, FS_G bits 3:2 (00 = 250, 01 = 500, 10 = 1000, 11 = 2000 dps),
  FS_125 bit 1 (125 dps).
- Rate codes: 0011 = 52 Hz, 0100 = 104 Hz, 0101 = 208 Hz, used for ODR and for batching.
- FIFO_CTRL3 0x09: BDR_GY bits 7:4, BDR_XL bits 3:0 (the rates at which each is batched).
- FIFO_CTRL4 0x0A: FIFO_MODE bits 2:0, 000 bypass (empties the FIFO), 110 continuous.
- FIFO_STATUS1 0x3A and FIFO_STATUS2 0x3B: DIFF_FIFO (unread words) in 0x3A and bits 1:0 of
  0x3B; FIFO_OVR_IA bit 6 and FIFO_OVR_LATCHED bit 3 of 0x3B flag an overrun.
- FIFO_DATA_OUT_TAG 0x78: TAG_SENSOR bits 7:3, TAG_CNT bits 2:1 (the time slot a word
  belongs to), then X, Y, Z as little-endian int16 in 0x79..0x7E. 1 word is 7 bytes and is
  read in 1 transfer.

Sensitivities (DS12814 table 2, "Mechanical characteristics", typical LA_So and G_So; the
same values as the Adafruit driver): accelerometer 0.061, 0.122, 0.244, 0.488 mg/LSB at 2, 4,
8, 16 g; gyroscope 4.375, 8.75, 17.50, 35, 70 mdps/LSB at 125, 250, 500, 1000, 2000 dps.
Samples come out in SI (m/s^2 with g = 9.80665 m/s^2, rad/s) in the chip's own axes.

Time base (docs/phase-1-plan.md section 5): the FIFO anchor plus 1/ODR. See ``FifoClock``.
"""

from __future__ import annotations

import ctypes
import fcntl
import math
import os
import struct
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from photoncar.clock import Clock
from photoncar.config import Config
from photoncar.records import ImuSample, Record
from photoncar.sources.base import SourceMeta
from photoncar.units import G0, NS_PER_S

REG_FIFO_CTRL3 = 0x09
REG_FIFO_CTRL4 = 0x0A
REG_WHO_AM_I = 0x0F
REG_CTRL1_XL = 0x10
REG_CTRL2_G = 0x11
REG_CTRL3_C = 0x12
REG_FIFO_STATUS1 = 0x3A
REG_FIFO_DATA_OUT_TAG = 0x78

WHO_AM_I_LSM6DSOX = 0x6C
ADDRESSES = frozenset({0x6A, 0x6B})
"""7-bit addresses: 0x6A with SDO/SA0 low, 0x6B with it high."""
CTRL3_SW_RESET = 0x01
CTRL3_IF_INC = 0x04
CTRL3_BDU = 0x40
FIFO_MODE_BYPASS = 0b000
FIFO_MODE_CONTINUOUS = 0b110
STATUS2_DIFF_HI = 0x03
STATUS2_OVERRUN = 0x40 | 0x08
FIFO_WORD_LEN = 7
TAG_GYRO = 0x01
TAG_ACCEL = 0x02

RATE_CODES = {52.0: 0b0011, 104.0: 0b0100, 208.0: 0b0101}
ACCEL_SCALES = {2: (0b00, 0.061), 4: (0b10, 0.122), 8: (0b11, 0.244), 16: (0b01, 0.488)}
"""Full scale in g: (FS_XL code, sensitivity in mg/LSB)."""
GYRO_SCALES = {
    125: (0b0010, 4.375),
    250: (0b0000, 8.75),
    500: (0b0100, 17.50),
    1000: (0b1000, 35.0),
    2000: (0b1100, 70.0),
}
"""Full scale in dps: (CTRL2_G bits 3:0, that is FS_G and FS_125, sensitivity in mdps/LSB)."""

POLL_S = 0.05
"""Time between FIFO reads (proposed): about 5 samples of each sensor per read at 104 Hz."""
RESET_POLLS = 10
RESET_POLL_S = 0.001

I2C_RDWR = 0x0707
"""linux/i2c-dev.h: combined transfers, 1 or more messages with repeated starts."""
I2C_M_RD = 0x0001
"""linux/i2c.h: this message reads."""


class ImuUnavailable(RuntimeError):
    """The IMU did not answer, is not an LSM6DSOX, or did not take its settings."""


# Settings.


@dataclass(frozen=True, slots=True)
class ImuSettings:
    rate_hz: float
    ctrl1_xl: int
    ctrl2_g: int
    fifo_ctrl3: int
    accel_mps2_per_lsb: float
    gyro_rps_per_lsb: float

    @property
    def period_ns(self) -> int:
        return round(NS_PER_S / self.rate_hz)


def imu_settings(cfg: Config) -> ImuSettings:
    rate_hz = float(cfg.get("imu.rate_hz"))
    accel_fs = cfg.get("imu.accel_fs_g")
    gyro_fs = cfg.get("imu.gyro_fs_dps")
    if rate_hz not in RATE_CODES:
        raise ValueError(f"imu.rate_hz must be 52, 104 or 208. It is {rate_hz:g}.")
    if accel_fs not in ACCEL_SCALES:
        raise ValueError(f"imu.accel_fs_g must be 2, 4, 8 or 16. It is {accel_fs}.")
    if gyro_fs not in GYRO_SCALES:
        raise ValueError(f"imu.gyro_fs_dps must be 125, 250, 500, 1000 or 2000. It is {gyro_fs}.")
    rate = RATE_CODES[rate_hz]
    fs_xl, mg_per_lsb = ACCEL_SCALES[accel_fs]
    fs_g, mdps_per_lsb = GYRO_SCALES[gyro_fs]
    return ImuSettings(
        rate_hz=rate_hz,
        ctrl1_xl=(rate << 4) | (fs_xl << 2),
        ctrl2_g=(rate << 4) | fs_g,
        fifo_ctrl3=(rate << 4) | rate,
        accel_mps2_per_lsb=mg_per_lsb * 1e-3 * G0,
        gyro_rps_per_lsb=math.radians(mdps_per_lsb * 1e-3),
    )


# I2C.


class I2cBus(Protocol):
    def write_read(self, addr: int, data: bytes, n: int) -> bytes:
        """Write ``data``, then read ``n`` bytes, in 1 transfer with a repeated start."""
        ...

    def write(self, addr: int, data: bytes) -> None: ...

    def close(self) -> None: ...


class I2cMsg(ctypes.Structure):
    """``struct i2c_msg`` from linux/i2c.h."""

    _fields_ = (
        ("addr", ctypes.c_uint16),
        ("flags", ctypes.c_uint16),
        ("len", ctypes.c_uint16),
        ("buf", ctypes.POINTER(ctypes.c_uint8)),
    )


class I2cRdwrData(ctypes.Structure):
    """``struct i2c_rdwr_ioctl_data`` from linux/i2c-dev.h."""

    _fields_ = (
        ("msgs", ctypes.POINTER(I2cMsg)),
        ("nmsgs", ctypes.c_uint32),
    )


Ioctl = Callable[[int, int, Any], Any]
_Buffer = ctypes.Array[ctypes.c_uint8]


class LinuxI2cBus:
    """``/dev/i2c-N`` through ``fcntl.ioctl(fd, I2C_RDWR, ...)``. ``ioctl`` exists for tests."""

    def __init__(self, path: str, *, ioctl: Ioctl = fcntl.ioctl) -> None:
        self.path = path
        self._fd = os.open(path, os.O_RDWR)
        self._ioctl = ioctl

    def write_read(self, addr: int, data: bytes, n: int) -> bytes:
        rbuf = (ctypes.c_uint8 * n)()
        self._transfer([(addr, 0, _buffer(data)), (addr, I2C_M_RD, rbuf)])
        return bytes(rbuf)

    def write(self, addr: int, data: bytes) -> None:
        self._transfer([(addr, 0, _buffer(data))])

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def _transfer(self, parts: list[tuple[int, int, _Buffer]]) -> None:
        msgs = (I2cMsg * len(parts))()
        for msg, (addr, flags, buf) in zip(msgs, parts, strict=True):
            msg.addr = addr
            msg.flags = flags
            msg.len = len(buf)
            msg.buf = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8))
        rdwr = I2cRdwrData(ctypes.cast(msgs, ctypes.POINTER(I2cMsg)), len(parts))
        self._ioctl(self._fd, I2C_RDWR, rdwr)


def _buffer(data: bytes) -> _Buffer:
    return (ctypes.c_uint8 * len(data)).from_buffer_copy(data)


# Timing.


class FifoClock:
    """Stamps FIFO samples: the FIFO anchor plus 1/ODR (docs/phase-1-plan.md section 5).

    The newest sample in a read was taken within 1 period before the read. The first read
    anchors that sample half a period before the read. Each later read continues the chain
    1 period per sample from the last stamp, then moves it back inside that 1-period window
    if it has left it. So stamps keep exact 1/ODR spacing while the chip's rate matches, and
    never drift more than 1 period from the reads when it does not. ``reset`` (after a FIFO
    overrun) anchors afresh. Stamps never go backwards.
    """

    def __init__(self, period_ns: int) -> None:
        self._period = period_ns
        self._next: int | None = None
        self._last: int | None = None

    def reset(self) -> None:
        self._next = None

    def stamps(self, n: int, t_read_ns: int) -> list[int]:
        if n <= 0:
            return []
        p = self._period
        if self._next is None:
            newest = t_read_ns - p // 2
        else:
            newest = min(max(self._next + (n - 1) * p, t_read_ns - p), t_read_ns)
        out = [newest - (n - 1 - k) * p for k in range(n)]
        if self._last is not None:
            out = [max(t, self._last) for t in out]
        self._next = newest + p
        self._last = out[-1]
        return out


# The source.

_Xyz = tuple[int, int, int]


class Lsm6dsoxSource:
    """Yields ``ImuSample`` records from an LSM6DSOX FIFO. Opening it configures the chip.

    Raises ``ImuUnavailable`` when the chip does not answer, is not an LSM6DSOX, or does not
    read back a setting; OSError when ``/dev/i2c-N`` does not open; ValueError for a bad
    ``[imu]`` setting. ``records()`` raises ``ImuUnavailable`` with a plain message if the bus
    fails mid-drive. ``stop()`` ends ``records()`` within ``POLL_S``. A ``bus`` injected for
    tests is never closed by this class.
    """

    def __init__(self, cfg: Config, clock: Clock, *, bus: I2cBus | None = None) -> None:
        self._settings = imu_settings(cfg)
        address = cfg.get("imu.address")
        if address not in ADDRESSES:
            raise ValueError(f"imu.address must be 0x6A or 0x6B. It is {address}.")
        self._addr = int(address)
        self._path = f"/dev/i2c-{int(cfg.get('imu.i2c_bus'))}"
        self._clock = clock
        self._owned = bus is None
        self._i2c: I2cBus = bus if bus is not None else LinuxI2cBus(self._path)
        self._closed = False
        self._stop = threading.Event()
        self._timer = FifoClock(self._settings.period_ns)
        self._slot_cnt: int | None = None
        self._slot: dict[int, _Xyz] = {}
        self.samples = 0
        self.fifo_overruns = 0
        self.unpaired_words = 0
        self.other_words = 0
        self.failure: str | None = None
        try:
            self._configure()
        except BaseException:
            self.close()
            raise

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(name=f"imu:{self._path}@{self._addr:#04x}", kind="live")

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        if self._owned and not self._closed:
            self._i2c.close()
        self._closed = True

    def records(self) -> Iterator[Record]:
        try:
            while not self._stop.is_set():
                try:
                    batch = self.read_fifo()
                except OSError as exc:
                    self.failure = f"The IMU on {self._path} stopped ({exc})."
                    raise ImuUnavailable(self.failure) from exc
                yield from batch
                self._stop.wait(POLL_S)
        finally:
            self.close()

    def read_fifo(self) -> list[ImuSample]:
        """Read every unread FIFO word; return the complete samples, oldest first."""
        status = self._read(REG_FIFO_STATUS1, 2)
        t_read_ns = self._clock.now_ns()
        if status[1] & STATUS2_OVERRUN:
            self.fifo_overruns += 1
            self.unpaired_words += len(self._slot)
            self._slot.clear()
            self._slot_cnt = None
            self._timer.reset()
        n_words = status[0] | ((status[1] & STATUS2_DIFF_HI) << 8)
        pairs: list[tuple[_Xyz, _Xyz]] = []
        for _ in range(n_words):
            pair = self._take(self._read(REG_FIFO_DATA_OUT_TAG, FIFO_WORD_LEN))
            if pair is not None:
                pairs.append(pair)
        s = self._settings
        out = [
            ImuSample(
                t_ns=t,
                ax=accel[0] * s.accel_mps2_per_lsb,
                ay=accel[1] * s.accel_mps2_per_lsb,
                az=accel[2] * s.accel_mps2_per_lsb,
                gx=gyro[0] * s.gyro_rps_per_lsb,
                gy=gyro[1] * s.gyro_rps_per_lsb,
                gz=gyro[2] * s.gyro_rps_per_lsb,
            )
            for t, (accel, gyro) in zip(
                self._timer.stamps(len(pairs), t_read_ns), pairs, strict=True
            )
        ]
        self.samples += len(out)
        return out

    def _take(self, word: bytes) -> tuple[_Xyz, _Xyz] | None:
        """Pair accelerometer and gyroscope words by time slot (TAG_CNT)."""
        tag = word[0] >> 3
        cnt = (word[0] >> 1) & 0x03
        if tag not in (TAG_GYRO, TAG_ACCEL):
            self.other_words += 1
            return None
        if cnt != self._slot_cnt or tag in self._slot:
            # A new time slot while the last one is incomplete: its partner word was lost.
            self.unpaired_words += len(self._slot)
            self._slot.clear()
            self._slot_cnt = cnt
        x, y, z = struct.unpack_from("<hhh", word, 1)
        self._slot[tag] = (x, y, z)
        if len(self._slot) < 2:
            return None
        pair = (self._slot[TAG_ACCEL], self._slot[TAG_GYRO])
        self._slot.clear()
        self._slot_cnt = None
        return pair

    def _configure(self) -> None:
        try:
            who = self._read(REG_WHO_AM_I, 1)[0]
        except OSError as exc:
            raise ImuUnavailable(
                f"No IMU answered at {self._addr:#04x} on {self._path} ({exc})."
            ) from exc
        if who != WHO_AM_I_LSM6DSOX:
            raise ImuUnavailable(
                f"The chip at {self._addr:#04x} on {self._path} is not an LSM6DSOX "
                f"(WHO_AM_I reads {who:#04x}, expected {WHO_AM_I_LSM6DSOX:#04x})."
            )
        # A software reset first: every other register back to its default (FIFO off, no
        # compression, the full-scale codes above), whatever ran before.
        self._write(REG_CTRL3_C, CTRL3_SW_RESET)
        for _ in range(RESET_POLLS):
            if not self._read(REG_CTRL3_C, 1)[0] & CTRL3_SW_RESET:
                break
            time.sleep(RESET_POLL_S)
        else:
            raise ImuUnavailable(f"The IMU on {self._path} did not finish its reset.")
        s = self._settings
        for reg, value in (
            (REG_CTRL3_C, CTRL3_BDU | CTRL3_IF_INC),
            (REG_FIFO_CTRL4, FIFO_MODE_BYPASS),
            (REG_FIFO_CTRL3, s.fifo_ctrl3),
            (REG_CTRL1_XL, s.ctrl1_xl),
            (REG_CTRL2_G, s.ctrl2_g),
            (REG_FIFO_CTRL4, FIFO_MODE_CONTINUOUS),
        ):
            self._write(reg, value)
            got = self._read(reg, 1)[0]
            if got != value:
                raise ImuUnavailable(
                    f"The IMU on {self._path} did not keep a setting: register {reg:#04x} "
                    f"reads {got:#04x} after writing {value:#04x}."
                )

    def _read(self, reg: int, n: int) -> bytes:
        data = self._i2c.write_read(self._addr, bytes((reg,)), n)
        if len(data) != n:
            raise OSError(f"short I2C read: {len(data)} of {n} bytes")
        return data

    def _write(self, reg: int, value: int) -> None:
        self._i2c.write(self._addr, bytes((reg, value)))
