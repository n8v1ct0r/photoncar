"""LSM6DSOX source: register protocol, FIFO pairing, SI conversion, time stamps, I2C_RDWR.

``FakeI2cBus`` (tests only) models the chip at the register level: WHO_AM_I, the control
registers, the software reset, address auto-increment, and a FIFO of 7-byte tagged words
with its unread count and overrun flags. Expected register values and sensitivities are
typed here from the datasheet tables, not imported from the module under test.
"""

from __future__ import annotations

import ctypes
import errno
import itertools
import math
import struct
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from photoncar.clock import SimClock
from photoncar.config import Config, load_config
from photoncar.records import ImuSample
from photoncar.sources.imu_lsm6dsox import (
    FifoClock,
    I2cMsg,
    I2cRdwrData,
    ImuUnavailable,
    LinuxI2cBus,
    Lsm6dsoxSource,
    imu_settings,
)
from photoncar.units import G0

S = 1_000_000_000
MS = 1_000_000
T0 = 100 * S
CFG = load_config()
P_104 = round(S / 104)
"""1 sample period at 104 Hz, in ns."""

GYRO, ACCEL, TIMESTAMP = 0x01, 0x02, 0x04
DEFAULTS = {0x0F: 0x6C, 0x12: 0x04}
"""Register reset values that matter here: WHO_AM_I, and CTRL3_C with IF_INC set."""


class FakeI2cBus:
    """An LSM6DSOX at ``address`` on an I2C bus.

    ``stuck`` maps a register to the value it always reads. ``reset_reads`` is how many reads
    of CTRL3_C still show SW_RESET after a reset. ``capacity`` is the FIFO size in words; past
    it, the oldest word is lost and the overrun flags set.
    """

    def __init__(
        self,
        *,
        address: int = 0x6A,
        who_am_i: int = 0x6C,
        capacity: int = 512,
        stuck: dict[int, int] | None = None,
        reset_reads: int = 1,
    ) -> None:
        self.address = address
        self.who_am_i = who_am_i
        self.capacity = capacity
        self.stuck = stuck or {}
        self.reset_reads = reset_reads
        self.regs = bytearray(0x80)
        self._defaults()
        self.fifo: deque[bytes] = deque()
        self.overrun = False
        self.slot = 0
        self.writes: list[tuple[int, int]] = []
        self.resetting = 0
        self.fail = False
        self.closed = False

    def _defaults(self) -> None:
        self.regs[:] = bytes(0x80)
        for reg, value in DEFAULTS.items():
            self.regs[reg] = value
        self.regs[0x0F] = self.who_am_i

    def _check(self, addr: int) -> None:
        if self.fail:
            raise OSError(errno.EIO, "Input/output error")
        if addr != self.address:
            raise OSError(errno.ENXIO, "No such device or address")

    # The bus interface.

    def write(self, addr: int, data: bytes) -> None:
        self._check(addr)
        reg, *values = data
        for i, value in enumerate(values):
            self._set(reg + i, value)

    def write_read(self, addr: int, data: bytes, n: int) -> bytes:
        self._check(addr)
        assert len(data) == 1
        reg = data[0]
        if reg == 0x78:
            assert n == 7, "1 FIFO word per read"
            assert self.fifo, "read past the FIFO's unread count"
            return self.fifo.popleft()
        if reg == 0x3A:
            assert n == 2 and self.regs[0x12] & 0x04, "FIFO_STATUS1..2 needs IF_INC"
            count = len(self.fifo)
            status2 = (count >> 8) & 0x03
            if self.overrun:
                status2 |= 0x08  # FIFO_OVR_LATCHED, cleared by this read
                if count >= self.capacity:
                    status2 |= 0x40  # FIFO_OVR_IA
                self.overrun = False
            return bytes((count & 0xFF, status2))
        step = 1 if self.regs[0x12] & 0x04 else 0
        return bytes(self._get(reg + i * step) for i in range(n))

    def close(self) -> None:
        self.closed = True

    # The chip.

    def _set(self, reg: int, value: int) -> None:
        self.writes.append((reg, value))
        if reg == 0x12 and value & 0x01:
            self._defaults()
            self.fifo.clear()
            self.overrun = False
            self.resetting = self.reset_reads
            return
        self.regs[reg] = value
        if reg == 0x0A and value & 0x07 == 0:
            self.fifo.clear()  # bypass mode empties the FIFO

    def _get(self, reg: int) -> int:
        if reg in self.stuck:
            return self.stuck[reg]
        if reg == 0x12 and self.resetting:
            self.resetting -= 1
            return self.regs[reg] | 0x01
        return self.regs[reg]

    def batching(self) -> bool:
        return self.regs[0x0A] & 0x07 == 0b110 and self.regs[0x09] != 0

    def word(self, tag: int, xyz: tuple[int, int, int], cnt: int | None = None) -> None:
        """Append 1 tagged word in time slot ``cnt`` (default: the current slot)."""
        c = self.slot if cnt is None else cnt
        self._append(bytes(((tag << 3) | (c << 1),)) + struct.pack("<hhh", *xyz))

    def sample(
        self,
        accel: tuple[int, int, int],
        gyro: tuple[int, int, int],
        *,
        accel_first: bool = False,
    ) -> None:
        """The chip batches 1 accelerometer and 1 gyroscope word in the next time slot."""
        assert self.batching(), "the FIFO is not batching"
        words = [(ACCEL, accel), (GYRO, gyro)]
        for tag, xyz in words if accel_first else words[::-1]:
            self.word(tag, xyz)
        self.slot = (self.slot + 1) % 4

    def _append(self, word: bytes) -> None:
        if len(self.fifo) >= self.capacity:
            self.fifo.popleft()
            self.overrun = True
        self.fifo.append(word)


def imu_cfg(**imu: object) -> Config:
    raw = dict(CFG.raw)
    raw["imu"] = {**CFG.section("imu"), **imu}
    return Config(raw)


def open_imu(
    bus: FakeI2cBus | None = None, clock: SimClock | None = None, **imu: object
) -> tuple[Lsm6dsoxSource, FakeI2cBus, SimClock]:
    bus = bus or FakeI2cBus()
    clock = clock or SimClock(T0)
    return Lsm6dsoxSource(imu_cfg(**imu), clock, bus=bus), bus, clock


# Settings from config, against the datasheet tables.

ACCEL_TABLE = {2: (0b00, 0.061), 4: (0b10, 0.122), 8: (0b11, 0.244), 16: (0b01, 0.488)}
GYRO_TABLE = {
    125: (0b00, 1, 4.375),
    250: (0b00, 0, 8.75),
    500: (0b01, 0, 17.50),
    1000: (0b10, 0, 35.0),
    2000: (0b11, 0, 70.0),
}
"""dps: (FS_G, FS_125, mdps/LSB)."""


@pytest.mark.parametrize("fs", sorted(ACCEL_TABLE))
def test_accel_scale_bits_and_sensitivity(fs: int) -> None:
    code, mg = ACCEL_TABLE[fs]
    s = imu_settings(imu_cfg(accel_fs_g=fs))
    assert s.ctrl1_xl == (0b0100 << 4) | (code << 2)
    assert s.accel_mps2_per_lsb == pytest.approx(mg / 1000 * G0, rel=1e-12)


@pytest.mark.parametrize("fs", sorted(GYRO_TABLE))
def test_gyro_scale_bits_and_sensitivity(fs: int) -> None:
    code, fs125, mdps = GYRO_TABLE[fs]
    s = imu_settings(imu_cfg(gyro_fs_dps=fs))
    assert s.ctrl2_g == (0b0100 << 4) | (code << 2) | (fs125 << 1)
    assert s.gyro_rps_per_lsb == pytest.approx(math.radians(mdps / 1000), rel=1e-12)


@pytest.mark.parametrize(("rate", "code"), [(52.0, 0b0011), (104.0, 0b0100), (208.0, 0b0101)])
def test_rate_sets_odr_and_batch_rate(rate: float, code: int) -> None:
    s = imu_settings(imu_cfg(rate_hz=rate))
    assert s.ctrl1_xl >> 4 == s.ctrl2_g >> 4 == code
    assert s.fifo_ctrl3 == (code << 4) | code
    assert s.period_ns == round(S / rate)


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("rate_hz", 100.0, "imu.rate_hz"),
        ("accel_fs_g", 3, "imu.accel_fs_g"),
        ("gyro_fs_dps", 300, "imu.gyro_fs_dps"),
        ("address", 0x50, "imu.address"),
    ],
)
def test_bad_settings_fail_before_any_bus_traffic(key: str, value: object, match: str) -> None:
    bus = FakeI2cBus()
    with pytest.raises(ValueError, match=match):
        Lsm6dsoxSource(imu_cfg(**{key: value}), SimClock(T0), bus=bus)
    assert bus.writes == []


# Configuration.


def test_configure_resets_then_sets_104_hz_4_g_500_dps_and_a_continuous_fifo() -> None:
    _, bus, _ = open_imu()
    assert bus.writes[0] == (0x12, 0x01)  # SW_RESET first
    assert bus.regs[0x12] == 0x44  # BDU and IF_INC
    assert bus.regs[0x10] == 0x48  # CTRL1_XL: 104 Hz, 4 g
    assert bus.regs[0x11] == 0x44  # CTRL2_G: 104 Hz, 500 dps
    assert bus.regs[0x09] == 0x44  # FIFO_CTRL3: both batched at 104 Hz
    assert bus.regs[0x0A] == 0x06  # FIFO_CTRL4: continuous, no temperature, no timestamps
    modes = [v for r, v in bus.writes if r == 0x0A]
    assert modes == [0x00, 0x06]  # bypass empties the FIFO, then continuous
    assert bus.writes[-1] == (0x0A, 0x06)  # the FIFO starts last
    assert not bus.closed


def test_configure_empties_stale_fifo_data() -> None:
    bus = FakeI2cBus()
    bus.word(GYRO, (1, 2, 3))
    bus.word(ACCEL, (4, 5, 6))
    src, _, _ = open_imu(bus)
    assert bus.fifo == deque()
    assert src.read_fifo() == []


def test_configure_waits_for_the_reset_to_finish() -> None:
    _, bus, _ = open_imu(FakeI2cBus(reset_reads=3))
    assert bus.resetting == 0
    assert bus.regs[0x0A] == 0x06


def test_configure_fails_when_the_reset_never_ends() -> None:
    with pytest.raises(ImuUnavailable, match=r"did not finish its reset\.$"):
        open_imu(FakeI2cBus(reset_reads=100))


def test_wrong_chip_is_refused_plainly() -> None:
    bus = FakeI2cBus(who_am_i=0x69)
    with pytest.raises(ImuUnavailable) as err:
        open_imu(bus)
    assert str(err.value) == (
        "The chip at 0x6a on /dev/i2c-1 is not an LSM6DSOX (WHO_AM_I reads 0x69, expected 0x6c)."
    )
    assert bus.writes == []


def test_no_answer_at_the_address_is_refused_plainly() -> None:
    with pytest.raises(ImuUnavailable, match=r"^No IMU answered at 0x6b on /dev/i2c-1 \("):
        open_imu(FakeI2cBus(address=0x6A), address=0x6B)


def test_a_setting_that_does_not_stick_is_refused() -> None:
    with pytest.raises(ImuUnavailable, match=r"register 0x10 reads 0x00 after writing 0x48\.$"):
        open_imu(FakeI2cBus(stuck={0x10: 0x00}))


# Reading the FIFO.


def test_samples_are_si_in_the_sensor_frame() -> None:
    src, bus, _ = open_imu()
    bus.sample(accel=(1000, -2000, 8197), gyro=(100, -100, 5143))
    (s,) = src.read_fifo()
    mg, mdps = 0.122, 17.50  # 4 g and 500 dps, datasheet table 2
    assert (s.ax, s.ay, s.az) == pytest.approx(
        [1000 * mg / 1000 * G0, -2000 * mg / 1000 * G0, 8197 * mg / 1000 * G0], rel=1e-12
    )
    assert (s.gx, s.gy, s.gz) == pytest.approx(
        [math.radians(100 * mdps / 1000), -math.radians(1.75), math.radians(5143 * mdps / 1000)],
        rel=1e-12,
    )
    assert s.az == pytest.approx(G0, rel=2e-4)  # 1 g on z at rest
    assert s.temp_k is None


def test_full_scale_extremes_convert() -> None:
    src, bus, _ = open_imu(accel_fs_g=16, gyro_fs_dps=2000)
    bus.sample(accel=(32767, -32768, 0), gyro=(32767, -32768, 0))
    (s,) = src.read_fifo()
    assert s.ax == pytest.approx(32767 * 0.488e-3 * G0, rel=1e-12)
    assert s.ay == pytest.approx(-32768 * 0.488e-3 * G0, rel=1e-12)
    assert s.gy == pytest.approx(math.radians(-32768 * 70e-3), rel=1e-12)


@pytest.mark.parametrize("accel_first", [False, True])
def test_words_pair_by_time_slot_in_either_order(accel_first: bool) -> None:
    src, bus, _ = open_imu()
    for k in range(6):
        bus.sample(accel=(k, 0, 0), gyro=(0, k, 0), accel_first=accel_first)
    out = src.read_fifo()
    assert [round(s.ax / (0.122e-3 * G0)) for s in out] == list(range(6))
    assert [round(s.gy / math.radians(17.5e-3)) for s in out] == list(range(6))
    assert (src.unpaired_words, src.other_words, src.samples) == (0, 0, 6)


def test_a_slot_split_across_reads_pairs_once() -> None:
    src, bus, _ = open_imu()
    bus.word(GYRO, (0, 7, 0), cnt=2)
    assert src.read_fifo() == []
    bus.word(ACCEL, (7, 0, 0), cnt=2)
    (s,) = src.read_fifo()
    assert round(s.ax / (0.122e-3 * G0)) == 7
    assert src.unpaired_words == 0


def test_a_word_whose_partner_is_lost_is_dropped() -> None:
    src, bus, _ = open_imu()
    bus.word(GYRO, (0, 1, 0), cnt=0)  # its accel word is lost: the next slot starts
    bus.word(GYRO, (0, 2, 0), cnt=1)
    bus.word(ACCEL, (2, 0, 0), cnt=1)
    bus.word(ACCEL, (9, 0, 0), cnt=2)  # its gyro word is lost, and 4 slots later the
    bus.word(ACCEL, (3, 0, 0), cnt=2)  # counter repeats: the same sensor twice restarts
    bus.word(GYRO, (0, 3, 0), cnt=2)
    out = src.read_fifo()
    assert [round(s.gy / math.radians(17.5e-3)) for s in out] == [2, 3]
    assert [round(s.ax / (0.122e-3 * G0)) for s in out] == [2, 3]
    assert src.unpaired_words == 2


def test_other_tags_are_skipped() -> None:
    src, bus, _ = open_imu()
    bus.word(TIMESTAMP, (1, 2, 3))
    bus.sample(accel=(1, 1, 1), gyro=(2, 2, 2))
    assert len(src.read_fifo()) == 1
    assert src.other_words == 1


def test_an_overrun_is_counted_and_orphans_dropped() -> None:
    src, bus, _ = open_imu(FakeI2cBus(capacity=7))
    for k in range(6):  # 12 words into 7: the 5 oldest are lost, leaving 1 orphan accel word
        bus.sample(accel=(k, 0, 0), gyro=(0, k, 0))
    out = src.read_fifo()
    assert [round(s.ax / (0.122e-3 * G0)) for s in out] == [3, 4, 5]
    assert src.fifo_overruns == 1
    assert src.unpaired_words == 1


def test_an_io_error_raises_a_plain_message() -> None:
    src, bus, _ = open_imu()
    bus.sample(accel=(1, 1, 1), gyro=(2, 2, 2))
    gen = src.records()
    assert isinstance(next(gen), ImuSample)
    bus.fail = True
    with pytest.raises(ImuUnavailable) as err:
        next(gen)
    assert str(err.value) == "The IMU on /dev/i2c-1 stopped ([Errno 5] Input/output error)."
    assert isinstance(err.value.__cause__, OSError)
    assert src.failure == str(err.value)


def test_records_stop_on_request() -> None:
    src, bus, _ = open_imu()
    for k in range(3):
        bus.sample(accel=(k, 0, 0), gyro=(0, k, 0))
    gen = src.records()
    got = [next(gen) for _ in range(3)]
    src.stop()
    assert list(gen) == []
    assert all(isinstance(s, ImuSample) for s in got)
    stamps = [s.t_ns for s in got if isinstance(s, ImuSample)]
    assert stamps == sorted(stamps)
    assert not bus.closed  # an injected bus stays open
    assert src.meta.name == "imu:/dev/i2c-1@0x6a" and src.meta.kind == "live"


# Time stamps.


def test_first_read_anchors_the_newest_sample_half_a_period_back() -> None:
    src, bus, _ = open_imu()
    for _ in range(5):
        bus.sample(accel=(0, 0, 0), gyro=(0, 0, 0))
    out = src.read_fifo()
    newest = T0 - P_104 // 2
    assert [s.t_ns for s in out] == [newest - (4 - k) * P_104 for k in range(5)]


def test_stamps_continue_at_exactly_1_over_odr_across_reads() -> None:
    src, bus, clock = open_imu()
    stamps: list[int] = []
    for read in range(20):
        for _ in range(5):
            bus.sample(accel=(0, 0, 0), gyro=(0, 0, 0))
        clock.set(T0 + read * 5 * P_104)
        stamps += [s.t_ns for s in src.read_fifo()]
    assert len(stamps) == 100
    assert {b - a for a, b in itertools.pairwise(stamps)} == {P_104}


@pytest.mark.parametrize("rate_error", [-0.01, 0.0016, 0.01])
def test_stamps_follow_a_chip_off_its_nominal_rate(rate_error: float) -> None:
    """60 s of reads every 50 ms from a chip 1% slow, 0.16% fast, or 1% fast."""
    src, bus, clock = open_imu()
    true_period = P_104 / (1 + rate_error)
    produced = 0
    truth: list[float] = []
    stamps: list[int] = []
    for read in range(1, 1201):
        t_read = T0 + read * 50 * MS
        while T0 + (produced + 1) * true_period <= t_read:
            produced += 1
            truth.append(T0 + produced * true_period)
            bus.sample(accel=(0, 0, 0), gyro=(0, 0, 0))
        clock.set(t_read)
        batch = src.read_fifo()
        if batch:
            assert t_read - P_104 <= batch[-1].t_ns <= t_read
        stamps += [s.t_ns for s in batch]
    assert len(stamps) == len(truth) > 6000
    assert stamps == sorted(stamps)
    worst = max(abs(s - t) for s, t in zip(stamps, truth, strict=True))
    assert worst < 1.1 * P_104


def test_fifo_clock_reset_anchors_afresh_and_never_goes_back() -> None:
    fc = FifoClock(10 * MS)
    assert fc.stamps(3, 1000 * MS) == [975 * MS, 985 * MS, 995 * MS]
    fc.reset()
    # After an overrun the chain restarts from the read, but never before the last stamp.
    assert fc.stamps(2, 1001 * MS) == [995 * MS, 996 * MS]
    assert fc.stamps(0, 1100 * MS) == []


# Linux I2C_RDWR marshalling.


def test_ioctl_structs_match_the_kernel_layout() -> None:
    ptr = ctypes.sizeof(ctypes.c_void_p)
    assert I2cMsg.addr.offset == 0 and I2cMsg.flags.offset == 2 and I2cMsg.len.offset == 4
    assert I2cMsg.buf.offset == ptr  # after 3 u16 fields and alignment padding
    assert ctypes.sizeof(I2cMsg) == 2 * ptr
    assert I2cRdwrData.msgs.offset == 0 and I2cRdwrData.nmsgs.offset == ptr
    assert ctypes.sizeof(I2cRdwrData) == 2 * ptr


class KernelBridge:
    """Stands in for ``fcntl.ioctl``: decodes I2C_RDWR the way the kernel does, then runs the
    messages against a FakeI2cBus and writes read data back through the message pointers."""

    def __init__(self, chip: FakeI2cBus) -> None:
        self.chip = chip
        self.calls: list[list[tuple[int, int, int]]] = []

    def __call__(self, fd: int, request: int, arg: Any) -> int:
        assert request == 0x0707
        assert isinstance(arg, I2cRdwrData)
        msgs = [arg.msgs[i] for i in range(arg.nmsgs)]
        self.calls.append([(m.addr, m.flags, m.len) for m in msgs])
        if len(msgs) == 1:
            (w,) = msgs
            assert w.flags == 0
            self.chip.write(w.addr, bytes(w.buf[: w.len]))
        else:
            w, r = msgs
            assert (w.flags, r.flags) == (0, 0x0001)
            data = self.chip.write_read(w.addr, bytes(w.buf[: w.len]), r.len)
            for i, byte in enumerate(data):
                r.buf[i] = byte
        return 0


def test_source_runs_through_linux_i2c_rdwr(tmp_path: Path) -> None:
    dev = tmp_path / "i2c-1"
    dev.touch()
    chip = FakeI2cBus()
    bridge = KernelBridge(chip)
    bus = LinuxI2cBus(str(dev), ioctl=bridge)
    src = Lsm6dsoxSource(imu_cfg(), SimClock(T0), bus=bus)
    assert chip.regs[0x10] == 0x48 and chip.regs[0x0A] == 0x06
    assert bridge.calls[0] == [(0x6A, 0, 1), (0x6A, 1, 1)]  # WHO_AM_I: write reg, read 1
    assert bridge.calls[1] == [(0x6A, 0, 2)]  # SW_RESET: write reg and value
    chip.sample(accel=(10, 20, 30), gyro=(-1, -2, -3))
    (s,) = src.read_fifo()
    assert round(s.ay / (0.122e-3 * G0)) == 20
    assert round(s.gz / math.radians(17.5e-3)) == -3
    assert bridge.calls[-1] == [(0x6A, 0, 1), (0x6A, 1, 7)]  # 1 FIFO word
    bus.close()
    bus.close()


def test_linux_i2c_bus_needs_the_device(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        LinuxI2cBus(str(tmp_path / "i2c-9"))
