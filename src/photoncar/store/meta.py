"""Drive metadata: the single row of a drive file's ``drive`` table, and the ``state`` row codec.

A state estimate made without an IMU (the estimator's speed-only mode) has no lateral
acceleration and no yaw rate: those fields are NaN and its ``inputs`` has no "imu" part. The
``state`` table stores them as NULL, and a NULL reads back as NaN.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from importlib import resources
from typing import Any, Literal

from photoncar.records import StateEstimate

DriveSource = Literal["live", "replay", "synthetic"]
Row3 = tuple[float, float, float]
Matrix3 = tuple[Row3, Row3, Row3]

SCHEMA_VERSION = 1

GPS_FIX = "gps.fix"
"""The GPS fixes as a signal name (plan section 3): the source of the timeline positions."""
IMU_INPUT = "imu"
STATE_COLUMNS = (
    "t_ns, speed_mps, accel_long_mps2, accel_lat_mps2, accel_lat_imu_mps2, yaw_rate_rps, "
    "var_speed, var_accel, inputs, dr"
)
"""The ``state`` columns in the order ``state_row`` writes and ``state_from_row`` reads."""


def lateral_known(inputs: str) -> bool:
    """True when the estimate had an IMU, so its lateral acceleration and yaw rate exist."""
    return IMU_INPUT in inputs.split("+")


def _finite(name: str, x: float) -> float:
    if not math.isfinite(x):
        raise ValueError(f"{name} must be finite, got {x!r}")
    return float(x)


def state_row(s: StateEstimate) -> tuple[object, ...]:
    """The ``state`` row for ``s``. Lateral values of an estimate made without an IMU are
    NULL. Raises ValueError for a value SQLite cannot keep: a non-finite value, or a lateral
    value on an estimate made without an IMU."""
    lateral = (s.accel_lat_mps2, s.accel_lat_imu_mps2, s.yaw_rate_rps)
    names = ("accel_lat_mps2", "accel_lat_imu_mps2", "yaw_rate_rps")
    lat: tuple[float | None, ...]
    if lateral_known(s.inputs):
        lat = tuple(_finite(n, v) for n, v in zip(names, lateral, strict=True))
    elif all(math.isnan(v) for v in lateral):
        lat = (None, None, None)
    else:
        raise ValueError(f"an estimate without an IMU has no lateral values, got {lateral!r}")
    return (
        s.t_ns,
        _finite("speed_mps", s.speed_mps),
        _finite("accel_long_mps2", s.accel_long_mps2),
        *lat,
        _finite("var_speed", s.var_speed),
        _finite("var_accel", s.var_accel),
        s.inputs,
        int(s.dr),
    )


def _nan_if_null(x: float | None) -> float:
    return math.nan if x is None else float(x)


def state_from_row(row: tuple[Any, ...]) -> StateEstimate:
    """A ``StateEstimate`` from a ``state`` row read with ``STATE_COLUMNS``. NULL is NaN."""
    t, speed, a_long, a_lat, a_lat_imu, yaw, var_v, var_a, inputs, dr = row
    return StateEstimate(
        t_ns=t,
        speed_mps=speed,
        accel_long_mps2=a_long,
        accel_lat_mps2=_nan_if_null(a_lat),
        accel_lat_imu_mps2=_nan_if_null(a_lat_imu),
        yaw_rate_rps=_nan_if_null(yaw),
        var_speed=var_v,
        var_accel=var_a,
        inputs=inputs,
        dr=bool(dr),
    )


@dataclass(frozen=True, slots=True)
class DriveMeta:
    """What a drive is and where it came from.

    ``synthetic_signals`` names every signal the source generated rather than measured. They are
    stored as rows of the ``signals`` table with ``origin = 'synthetic'``.
    ``imu_mount_rot`` is the sensor-to-vehicle rotation, row-major.
    """

    drive_id: str
    source: DriveSource
    dataset_ref: str | None = None
    boot_id: str | None = None
    vehicle_profile: str | None = None
    dbc_name: str | None = None
    dbc_sha256: str | None = None
    synthetic_signals: tuple[str, ...] = ()
    t_start_ns: int | None = None
    t_end_ns: int | None = None
    utc_offset_ns: int | None = None
    utc_fit_rms_ns: int | None = None
    imu_mount_rot: Matrix3 | None = None
    schema_version: int = SCHEMA_VERSION
    closed_cleanly: bool = False


def new_drive_id() -> str:
    """A random drive ID (UUID4). Drive files live at ``data/drives/<drive_id>.sqlite``."""
    return str(uuid.uuid4())


def drive_schema_sql() -> str:
    return resources.files("photoncar.store").joinpath("schema.sql").read_text(encoding="utf-8")


def index_schema_sql() -> str:
    return (
        resources.files("photoncar.store").joinpath("index_schema.sql").read_text(encoding="utf-8")
    )
