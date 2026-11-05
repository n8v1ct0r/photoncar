"""The 10 Hz timeline rollup behind the review UI (table ``timeline_10hz``).

Grid: 1 row every 100 ms, starting at the earliest rollup input (state, GPS, or a rollup
signal) and ending at or before the latest one.

Columns:

- ``speed_mps``, ``accel_long_mps2``, ``accel_lat_mps2``: mean of the state estimates in the
  centered 100 ms bucket around the row time. ``dr`` is true when any estimate in the bucket
  was dead reckoned. Lateral acceleration comes only from estimates made with an IMU; a bucket
  holding only speed-only estimates has none. Where a bucket has no estimate, speed falls back
  to GPS speed, then ``obd.speed``, then ``can.wheel_speed``, each interpolated as below.
- ``rpm``, ``throttle``, ``coolant_k``: linear interpolation between neighbouring samples, left
  blank across any gap longer than 3 nominal periods (the approved stale rule of 3 polls).
- Measured before synthetic: each column (and the speed fallback) reads the measured signals
  the drive has, and a synthetic signal (plan H8) only when the drive has no measured one for
  that column. A column never mixes the two, so a replay's timeline shows the measured
  ``can.engine_rpm``, not the synthetic ``obd.rpm`` overlay, as the report and the pipeline's
  live frames do.
- ``x_m``, ``y_m``: WGS84 ENU meters (east, north) from the first kept fix, interpolated the
  same way. A kept fix has ``fix_type >= 2`` (2D or better) and finite coordinates.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from photoncar.privacy.geo import enu_m
from photoncar.records import GpsFix

GRID_NS = 100_000_000
HALF_BUCKET_NS = GRID_NS // 2
MIN_KEPT_FIX_TYPE = 2

GPS_MAX_GAP_NS = 300_000_000  # 3 periods at the 10 Hz GPS rate
FAST_OBD_MAX_GAP_NS = 1_500_000_000  # 3 periods at the 2 Hz fast OBD group
SLOW_OBD_MAX_GAP_NS = 7_500_000_000  # 3 periods at 0.4 Hz (4 Hz shared by 10 slow PIDs)
CAN_MAX_GAP_NS = FAST_OBD_MAX_GAP_NS  # CAN signals: same bound as the fast OBD group

RPM_SOURCES: tuple[tuple[str, int], ...] = (
    ("obd.rpm", FAST_OBD_MAX_GAP_NS),
    ("can.engine_rpm", CAN_MAX_GAP_NS),
)
THROTTLE_SOURCES: tuple[tuple[str, int], ...] = (("obd.throttle", FAST_OBD_MAX_GAP_NS),)
COOLANT_SOURCES: tuple[tuple[str, int], ...] = (
    ("obd.coolant_temp", SLOW_OBD_MAX_GAP_NS),
    ("can.coolant_temp", CAN_MAX_GAP_NS),
)
SPEED_FALLBACK_SOURCES: tuple[tuple[str, int], ...] = (
    ("obd.speed", FAST_OBD_MAX_GAP_NS),
    ("can.wheel_speed", CAN_MAX_GAP_NS),
)
ROLLUP_SIGNALS: tuple[str, ...] = tuple(
    name for name, _ in RPM_SOURCES + THROTTLE_SOURCES + COOLANT_SOURCES + SPEED_FALLBACK_SOURCES
)

I64 = NDArray[np.int64]
F64 = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class TimelineRow:
    t_ns: int
    speed_mps: float | None
    rpm: float | None
    throttle: float | None
    coolant_k: float | None
    accel_long_mps2: float | None
    accel_lat_mps2: float | None
    x_m: float | None
    y_m: float | None
    dr: bool = False


@dataclass(frozen=True, slots=True, eq=False)
class RollupInputs:
    """Time-sorted arrays the rollup reads. GPS nulls are NaN."""

    state_t_ns: I64
    state_speed: F64
    state_accel_long: F64
    state_accel_lat: F64
    state_dr: NDArray[np.bool_]
    gps_t_ns: I64
    gps_lat: F64
    gps_lon: F64
    gps_speed: F64
    gps_fix_type: I64
    signals: Mapping[str, tuple[I64, F64]]
    synthetic: frozenset[str] = frozenset()
    """The drive's synthetic signals (``signals.origin = 'synthetic'``)."""


class SupportsRollup(Protocol):
    def rollup_inputs(self) -> RollupInputs: ...


def is_kept_fix(fix: GpsFix) -> bool:
    """The fix filter behind x/y: 2D or better, with finite coordinates."""
    return (
        fix.fix_type >= MIN_KEPT_FIX_TYPE
        and bool(np.isfinite(fix.lat_deg))
        and bool(np.isfinite(fix.lon_deg))
    )


def _kept_mask(inp: RollupInputs) -> NDArray[np.bool_]:
    mask: NDArray[np.bool_] = (
        (inp.gps_fix_type >= MIN_KEPT_FIX_TYPE)
        & np.isfinite(inp.gps_lat)
        & np.isfinite(inp.gps_lon)
    )
    return mask


def enu_origin(inp: RollupInputs) -> tuple[float, float] | None:
    """(lat, lon) of the first kept fix: the origin of every x/y in this drive."""
    idx = np.flatnonzero(_kept_mask(inp))
    if idx.size == 0:
        return None
    return float(inp.gps_lat[idx[0]]), float(inp.gps_lon[idx[0]])


def _i64(values: list[int]) -> I64:
    return np.array(values, dtype=np.int64)


def _f64(values: list[float | None]) -> F64:
    return np.array([np.nan if v is None else v for v in values], dtype=np.float64)


def load_rollup_inputs(conn: sqlite3.Connection) -> RollupInputs:
    """Read the rollup inputs from an open drive file connection."""
    st = conn.execute(
        "SELECT t_ns, speed_mps, accel_long_mps2, accel_lat_mps2, dr FROM state "
        "ORDER BY t_ns, rowid"
    ).fetchall()
    gps = conn.execute(
        "SELECT t_ns, lat_deg, lon_deg, speed_mps, fix_type FROM gps_fixes ORDER BY t_ns, rowid"
    ).fetchall()
    synthetic = frozenset(
        r[0] for r in conn.execute("SELECT name FROM signals WHERE origin = 'synthetic'")
    )
    signals: dict[str, tuple[I64, F64]] = {}
    for name in ROLLUP_SIGNALS:
        rows = conn.execute(
            "SELECT s.t_ns, s.value FROM samples s JOIN signals g ON s.signal_id = g.signal_id "
            "WHERE g.name = ? ORDER BY s.t_ns, s.rowid",
            (name,),
        ).fetchall()
        if rows:
            signals[name] = (_i64([r[0] for r in rows]), _f64([r[1] for r in rows]))
    return RollupInputs(
        state_t_ns=_i64([r[0] for r in st]),
        state_speed=_f64([r[1] for r in st]),
        state_accel_long=_f64([r[2] for r in st]),
        state_accel_lat=_f64([r[3] for r in st]),
        state_dr=np.array([bool(r[4]) for r in st], dtype=np.bool_),
        gps_t_ns=_i64([r[0] for r in gps]),
        gps_lat=_f64([r[1] for r in gps]),
        gps_lon=_f64([r[2] for r in gps]),
        gps_speed=_f64([r[3] for r in gps]),
        gps_fix_type=_i64([-1 if r[4] is None else r[4] for r in gps]),
        signals=signals,
        synthetic=synthetic,
    )


def bucket_mean(t_ns: I64, values: F64, grid_ns: I64, half_ns: int = HALF_BUCKET_NS) -> F64:
    """Mean of the finite values with ``g - half <= t < g + half`` for each grid time g.

    NaN where a bucket holds no finite value.
    """
    out = np.full(grid_ns.shape, np.nan, dtype=np.float64)
    if t_ns.size == 0:
        return out
    finite = np.isfinite(values)
    lo = np.searchsorted(t_ns, grid_ns - half_ns, side="left")
    hi = np.searchsorted(t_ns, grid_ns + half_ns, side="left")
    sums = np.concatenate(([0.0], np.cumsum(np.where(finite, values, 0.0))))
    counts = np.concatenate(([0], np.cumsum(finite.astype(np.int64))))
    n = counts[hi] - counts[lo]
    has = n > 0
    out[has] = (sums[hi[has]] - sums[lo[has]]) / n[has]
    return out


def interp_gap(t_ns: I64, values: F64, grid_ns: I64, max_gap_ns: int) -> F64:
    """Linear interpolation at each grid time, NaN outside the samples or across long gaps.

    A grid time equal to a sample time takes that sample's value. Otherwise it needs a sample
    on each side no more than ``max_gap_ns`` apart. NaN values propagate.
    """
    out = np.full(grid_ns.shape, np.nan, dtype=np.float64)
    n = t_ns.size
    if n == 0:
        return out
    k = np.minimum(np.searchsorted(t_ns, grid_ns, side="left"), n - 1)
    hit = t_ns[k] == grid_ns
    out[hit] = values[k[hit]]
    if n < 2:
        return out
    j = np.searchsorted(t_ns, grid_ns, side="right")
    jc = np.clip(j, 1, n - 1)
    left_t = t_ns[jc - 1]
    right_t = t_ns[jc]
    ok = (~hit) & (j >= 1) & (j < n) & ((right_t - left_t) <= max_gap_ns)
    if np.any(ok):
        span = (right_t[ok] - left_t[ok]).astype(np.float64)
        frac = (grid_ns[ok] - left_t[ok]).astype(np.float64) / span
        v0 = values[jc[ok] - 1]
        v1 = values[jc[ok]]
        out[ok] = v0 + frac * (v1 - v0)
    return out


def ranked_sources(
    sources: tuple[tuple[str, int], ...], synthetic: Collection[str], present: Collection[str]
) -> tuple[tuple[str, int], ...]:
    """The ``sources`` a column reads, in order: the measured ones in ``present``, or only when
    none is present, the synthetic ones. The result never mixes measured and synthetic."""
    have = tuple(s for s in sources if s[0] in present)
    measured = tuple(s for s in have if s[0] not in synthetic)
    return measured or have


def _first_available(inp: RollupInputs, sources: tuple[tuple[str, int], ...], grid_ns: I64) -> F64:
    out = np.full(grid_ns.shape, np.nan, dtype=np.float64)
    for name, gap in ranked_sources(sources, inp.synthetic, inp.signals):
        series = inp.signals[name]
        vals = interp_gap(series[0], series[1], grid_ns, gap)
        out = np.where(np.isnan(out), vals, out)
    return out


def _check_sorted(name: str, t_ns: I64) -> None:
    if t_ns.size > 1 and bool(np.any(np.diff(t_ns) < 0)):
        raise ValueError(f"rollup input {name} is not sorted by time")


def _opt(x: np.float64) -> float | None:
    return None if np.isnan(x) else float(x)


def build_timeline_10hz(src: RollupInputs | SupportsRollup) -> list[TimelineRow]:
    """Build the 10 Hz rows from a drive (reader) or from arrays."""
    inp = src if isinstance(src, RollupInputs) else src.rollup_inputs()
    times: list[I64] = [inp.state_t_ns, inp.gps_t_ns, *(s[0] for s in inp.signals.values())]
    _check_sorted("state", inp.state_t_ns)
    _check_sorted("gps", inp.gps_t_ns)
    for name, series in inp.signals.items():
        _check_sorted(name, series[0])
    present = [t for t in times if t.size]
    if not present:
        return []
    t0 = min(int(t[0]) for t in present)
    t1 = max(int(t[-1]) for t in present)
    grid = t0 + np.arange((t1 - t0) // GRID_NS + 1, dtype=np.int64) * GRID_NS

    speed = bucket_mean(inp.state_t_ns, inp.state_speed, grid)
    accel_long = bucket_mean(inp.state_t_ns, inp.state_accel_long, grid)
    accel_lat = bucket_mean(inp.state_t_ns, inp.state_accel_lat, grid)
    dr_mean = bucket_mean(inp.state_t_ns, inp.state_dr.astype(np.float64), grid)
    dr = np.nan_to_num(dr_mean, nan=0.0) > 0.0

    kept = _kept_mask(inp)
    x = np.full(grid.shape, np.nan, dtype=np.float64)
    y = np.full(grid.shape, np.nan, dtype=np.float64)
    if np.any(kept):
        kt = inp.gps_t_ns[kept]
        lat, lon = inp.gps_lat[kept], inp.gps_lon[kept]
        east, north = enu_m(lat, lon, float(lat[0]), float(lon[0]))
        x = interp_gap(kt, east, grid, GPS_MAX_GAP_NS)
        y = interp_gap(kt, north, grid, GPS_MAX_GAP_NS)
        gps_speed = interp_gap(kt, inp.gps_speed[kept], grid, GPS_MAX_GAP_NS)
        speed = np.where(np.isnan(speed), gps_speed, speed)
    speed = np.where(np.isnan(speed), _first_available(inp, SPEED_FALLBACK_SOURCES, grid), speed)
    rpm = _first_available(inp, RPM_SOURCES, grid)
    throttle = _first_available(inp, THROTTLE_SOURCES, grid)
    coolant = _first_available(inp, COOLANT_SOURCES, grid)

    return [
        TimelineRow(
            t_ns=int(grid[i]),
            speed_mps=_opt(speed[i]),
            rpm=_opt(rpm[i]),
            throttle=_opt(throttle[i]),
            coolant_k=_opt(coolant[i]),
            accel_long_mps2=_opt(accel_long[i]),
            accel_lat_mps2=_opt(accel_lat[i]),
            x_m=_opt(x[i]),
            y_m=_opt(y[i]),
            dr=bool(dr[i]),
        )
        for i in range(grid.size)
    ]
