"""Replay source for one comma2k19 segment, read from its processed_log numpy arrays.

On-disk layout of a segment directory (each file is a ``.npy`` array saved without the extension;
``t`` arrays are seconds of the recording device's boot time):

- ``processed_log/CAN/raw_can/{t, src, address, data}``: float64, int64, int64, ``|S8``. numpy
  drops trailing null bytes of ``|S8`` values, so payloads are padded back to 8 bytes. The true
  DLC is not stored, so every frame gets DLC 8. ``src`` 0..127 is the bus number; ``src`` 128 and
  up marks frames the logger sent itself (TX echoes). Those are dropped and counted.
- ``processed_log/GNSS/live_gnss_ublox/{t, value}``: value columns are latitude (deg), longitude
  (deg), speed (m/s), UTC timestamp, altitude (m), bearing (deg). The README says the UTC column is
  in seconds; the pinned data holds milliseconds (1.533e12, steps of 100 at 10 Hz). Values above
  1e11 are read as milliseconds. The array has no HDOP, satellite count, or fix type: HDOP is NaN,
  the satellite count is 0, and fix type 3 is assumed for every row with finite coordinates.
- ``processed_log/IMU/{accelerometer, gyro}/{t, value}``: m/s^2 and rad/s in [forward, right,
  down]. Rotated here to x forward, y left, z up (x, -y, -z). At rest the z axis reads about +g.
  When the gyro has its own timestamps, it is linearly interpolated onto the accelerometer's.
- ``processed_log/CAN/speed/{t, value}``: vehicle speed from CAN (m/s), used as the speed trace.

``t_ns = round(t * 1e9)``. Records come out merged in non-decreasing ``t_ns``.

The dataset's 2 cars have vehicle profiles of their own (``dataset_profile``), found by the
device ID that starts every route name: ``b0c9d2329ad1606b`` is the 2017 Toyota RAV4
(``comma2k19_rav4``) and ``99c94dc769b5d96e`` the 2016 Honda Civic (``comma2k19_civic``). A
dataset reference with one of those IDs is public data (``is_public_dataset_ref``).

The public dataset lives under its own root, apart from the data folder: ``data/public`` in the
repo, or ``PHOTONCAR_PUBLIC_DIR``. ``PHOTONCAR_DATA_DIR`` moves drives and exports only, so a
capture run with a scratch data folder still finds the dataset (plan H14).
"""

from __future__ import annotations

import heapq
import math
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
from numpy.typing import NDArray

from photoncar.config import REPO_ROOT
from photoncar.records import CanFrame, GpsFix, ImuSample, Record, record_time
from photoncar.sources.base import SourceMeta

F64 = NDArray[np.float64]
I64 = NDArray[np.int64]

RAW_CAN: Final = "processed_log/CAN/raw_can"
UBLOX: Final = "processed_log/GNSS/live_gnss_ublox"
ACCEL: Final = "processed_log/IMU/accelerometer"
GYRO: Final = "processed_log/IMU/gyro"
CAN_SPEED: Final = "processed_log/CAN/speed"
REQUIRED: Final = (
    f"{RAW_CAN}/t",
    f"{RAW_CAN}/src",
    f"{RAW_CAN}/address",
    f"{RAW_CAN}/data",
    f"{UBLOX}/t",
    f"{UBLOX}/value",
    f"{ACCEL}/t",
    f"{ACCEL}/value",
    f"{GYRO}/t",
    f"{GYRO}/value",
)
TX_ECHO_SRC_MIN: Final = 128
MAX_STD_ID: Final = 0x7FF
ASSUMED_FIX_TYPE: Final = 3
UTC_MS_THRESHOLD: Final = 1e11
PUBLIC_DIR_ENV: Final = "PHOTONCAR_PUBLIC_DIR"
DATASET_FOLDER: Final = "comma2k19"
DATASET_PROFILES: Final = {
    "b0c9d2329ad1606b": "comma2k19_rav4",
    "99c94dc769b5d96e": "comma2k19_civic",
}
"""Device ID to vehicle profile. The comma2k19 README (pinned commit): "The dongle_id of the
RAV4 is `b0c9d2329ad1606b` and that of the civic is `99c94dc769b5d96e`." The paper
(arXiv:1812.05752, section IV.A) names them a 2017 Toyota RAV4 Platinum and a 2016 Honda Civic
Touring."""
DATASET_REF_RE: Final = re.compile(
    r"(?:(?:Example|Chunk)_\d+/)?(?P<device>[0-9a-f]{16})[|_]\d{4}-\d{2}-\d{2}--\d{2}-\d{2}-\d{2}"
    r"/\d+(?:\.\.\d+)?"
)
"""A segment or route reference: ``[Chunk_N/]<device>|<start>/<segment>[..<last>]``. Folders
unzipped on a filesystem without ``|`` hold ``_`` there (the README's ``unzip_msft_fs.py``)."""


def public_dir() -> Path:
    """The public dataset root: ``PHOTONCAR_PUBLIC_DIR``, else ``data/public`` in the repo."""
    env = os.environ.get(PUBLIC_DIR_ENV, "").strip()
    return Path(env).expanduser().resolve() if env else REPO_ROOT / "data" / "public"


def default_dataset_root() -> Path:
    """The comma2k19 folder under the public dataset root."""
    return public_dir() / DATASET_FOLDER


def dataset_profile(dataset_ref: str | None) -> str | None:
    """The vehicle profile of the dataset car a reference names, or None for any other."""
    m = DATASET_REF_RE.fullmatch(dataset_ref) if dataset_ref is not None else None
    return DATASET_PROFILES.get(m.group("device")) if m is not None else None


def is_public_dataset_ref(dataset_ref: str | None) -> bool:
    """True when the reference names segments of the public dataset (1 of its 2 cars)."""
    return dataset_profile(dataset_ref) is not None


def segment_profile(segment_dir: Path) -> str | None:
    """``dataset_profile`` of a segment folder, from its route folder name."""
    seg = Path(segment_dir)
    return dataset_profile(f"{seg.parent.name}/{seg.name}")


def seconds_to_ns(t: F64) -> I64:
    """``round(t * 1e9)`` for every element (round half to even, as Python's ``round``)."""
    return np.rint(np.asarray(t, dtype=np.float64) * 1e9).astype(np.int64)


def utc_to_ns(value: float) -> int | None:
    """UTC column to ns since the epoch. Values above 1e11 are milliseconds, others seconds.

    Rounded to whole microseconds first, which float64 holds exactly at these magnitudes.
    """
    if not math.isfinite(value):
        return None
    us_per_unit = 1e3 if value > UTC_MS_THRESHOLD else 1e6
    return round(value * us_per_unit) * 1000


@dataclass(frozen=True, slots=True)
class Comma2k19Counts:
    """What a segment holds after parsing: kept records per stream and what was dropped."""

    can_by_bus: dict[int, int]
    dropped_can_by_src: dict[int, int]
    gps_fixes: int
    gps_dropped: int
    imu_samples: int
    t_start_ns: int
    t_end_ns: int


class Comma2k19Source:
    """A ``Source`` over one comma2k19 segment: ``CanFrame``, ``GpsFix``, ``ImuSample``."""

    def __init__(self, segment_dir: Path, *, dataset_root: Path | None = None) -> None:
        self._dir = Path(segment_dir)
        if not self._dir.is_dir():
            raise FileNotFoundError(
                f"comma2k19 segment not found: {self._dir}. Run make fetch-dataset."
            )
        missing = [name for name in REQUIRED if not (self._dir / name).is_file()]
        if missing:
            raise FileNotFoundError(f"comma2k19 segment {self._dir} is missing: {missing}")
        self._ref = _dataset_ref(self._dir, dataset_root or default_dataset_root())
        self._meta = SourceMeta(name=f"comma2k19:{self._ref}", kind="replay", dataset_ref=self._ref)

    @property
    def meta(self) -> SourceMeta:
        return self._meta

    @property
    def segment_dir(self) -> Path:
        return self._dir

    def _load(self, name: str) -> NDArray[np.generic]:
        arr: NDArray[np.generic] = np.load(self._dir / name, allow_pickle=False)
        return arr

    def _f64(self, name: str) -> F64:
        return np.asarray(self._load(name), dtype=np.float64)

    def records(self) -> Iterator[Record]:
        streams: list[Iterator[Record]] = [
            self.can_frames(),
            self.gps_fixes(),
            self.imu_samples(),
        ]
        yield from heapq.merge(*streams, key=record_time)

    def can_frames(self) -> Iterator[CanFrame]:
        t_ns, src, addr, data = self._raw_can()
        keep = (src >= 0) & (src < TX_ECHO_SRC_MIN)
        idx = np.flatnonzero(keep)
        idx = idx[np.argsort(t_ns[idx], kind="stable")]
        for t, bus, arb, payload in zip(
            t_ns[idx].tolist(),
            src[idx].tolist(),
            addr[idx].tolist(),
            data[idx].tolist(),
            strict=True,
        ):
            yield CanFrame(
                t_ns=t,
                bus=bus,
                arb_id=arb,
                is_ext=arb > MAX_STD_ID,
                dlc=8,
                data=bytes(payload).ljust(8, b"\x00"),
            )

    def _raw_can(self) -> tuple[I64, I64, I64, NDArray[np.bytes_]]:
        t_ns = seconds_to_ns(self._f64(f"{RAW_CAN}/t"))
        src = np.asarray(self._load(f"{RAW_CAN}/src"), dtype=np.int64)
        addr = np.asarray(self._load(f"{RAW_CAN}/address"), dtype=np.int64)
        data = np.asarray(self._load(f"{RAW_CAN}/data"), dtype=np.bytes_)
        if not (len(t_ns) == len(src) == len(addr) == len(data)):
            raise ValueError(f"raw_can arrays differ in length in {self._dir}")
        return t_ns, src, addr, data

    def _ublox(self) -> tuple[I64, F64, NDArray[np.bool_]]:
        t_ns = seconds_to_ns(self._f64(f"{UBLOX}/t"))
        value = self._f64(f"{UBLOX}/value").reshape(len(t_ns), -1)
        if value.shape[1] < 6:
            raise ValueError(f"live_gnss_ublox has {value.shape[1]} columns, expected 6")
        lat, lon = value[:, 0], value[:, 1]
        valid = np.isfinite(lat) & np.isfinite(lon) & ~((lat == 0.0) & (lon == 0.0))
        return t_ns, value, valid

    def gps_fixes(self) -> Iterator[GpsFix]:
        t_ns, value, valid = self._ublox()
        idx = np.flatnonzero(valid)
        idx = idx[np.argsort(t_ns[idx], kind="stable")]
        for t, row in zip(t_ns[idx].tolist(), value[idx].tolist(), strict=True):
            lat, lon, speed, utc, alt, bearing = row[:6]
            yield GpsFix(
                t_ns=t,
                rx_t_ns=t,
                utc_ns=utc_to_ns(utc),
                lat_deg=lat,
                lon_deg=lon,
                alt_m=alt,
                speed_mps=speed,
                course_rad=math.radians(bearing) if math.isfinite(bearing) else math.nan,
                hdop=math.nan,
                fix_type=ASSUMED_FIX_TYPE,
                n_sats=0,
            )

    def _imu(self) -> tuple[I64, F64, F64]:
        at = self._f64(f"{ACCEL}/t")
        av = self._f64(f"{ACCEL}/value").reshape(len(at), 3)
        gt = self._f64(f"{GYRO}/t")
        gv = self._f64(f"{GYRO}/value").reshape(len(gt), 3)
        order = np.argsort(at, kind="stable")
        at, av = at[order], av[order]
        if not np.array_equal(at, gt):
            g_order = np.argsort(gt, kind="stable")
            gt, gv = gt[g_order], gv[g_order]
            gv = np.column_stack([np.interp(at, gt, gv[:, k]) for k in range(3)])
        flip = np.array([1.0, -1.0, -1.0])
        return seconds_to_ns(at), av * flip, gv * flip

    def imu_samples(self) -> Iterator[ImuSample]:
        t_ns, acc, gyr = self._imu()
        for t, a, g in zip(t_ns.tolist(), acc.tolist(), gyr.tolist(), strict=True):
            yield ImuSample(t_ns=t, ax=a[0], ay=a[1], az=a[2], gx=g[0], gy=g[1], gz=g[2])

    def speed_trace(self) -> tuple[I64, F64]:
        """Vehicle speed (m/s): CAN speed when the segment has it, else u-blox speed."""
        if (self._dir / f"{CAN_SPEED}/t").is_file() and (
            self._dir / f"{CAN_SPEED}/value"
        ).is_file():
            t = self._f64(f"{CAN_SPEED}/t")
            v = self._f64(f"{CAN_SPEED}/value").reshape(len(t), -1)[:, 0]
        else:
            t_ns_u, value, valid = self._ublox()
            order = np.argsort(t_ns_u[valid], kind="stable")
            return t_ns_u[valid][order], value[valid, 2][order]
        order = np.argsort(t, kind="stable")
        return seconds_to_ns(t[order]), v[order]

    def counts(self) -> Comma2k19Counts:
        t_ns, src, _, _ = self._raw_can()
        kept = (src >= 0) & (src < TX_ECHO_SRC_MIN)
        buses, n_bus = np.unique(src[kept], return_counts=True)
        dropped, n_drop = np.unique(src[~kept], return_counts=True)
        g_t, _, valid = self._ublox()
        i_t, _, _ = self._imu()
        starts = [int(a.min()) for a in (t_ns, g_t, i_t) if a.size]
        ends = [int(a.max()) for a in (t_ns, g_t, i_t) if a.size]
        return Comma2k19Counts(
            can_by_bus=dict(zip(buses.tolist(), n_bus.tolist(), strict=True)),
            dropped_can_by_src=dict(zip(dropped.tolist(), n_drop.tolist(), strict=True)),
            gps_fixes=int(valid.sum()),
            gps_dropped=int((~valid).sum()),
            imu_samples=int(i_t.size),
            t_start_ns=min(starts) if starts else 0,
            t_end_ns=max(ends) if ends else 0,
        )


def _dataset_ref(segment_dir: Path, root: Path) -> str:
    """Segment path relative to the dataset root, or ``<route>/<segment>`` outside it."""
    seg = segment_dir.resolve()
    try:
        return seg.relative_to(root.resolve()).as_posix()
    except ValueError:
        return f"{seg.parent.name}/{seg.name}"
