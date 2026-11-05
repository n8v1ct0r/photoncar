"""Opens the live GPS and IMU for ``photoncar record``. Standard library only.

These sources talk only to the GPS module and the IMU chip. They never touch a vehicle bus.
The integration layer merges them with ``LiveCanSession``.
"""

from __future__ import annotations

import platform
from collections.abc import Callable
from typing import Protocol

from photoncar.clock import Clock
from photoncar.config import Config, ConfigError
from photoncar.sources.base import Source
from photoncar.sources.gps_ublox import GpsUbloxSource, GpsUnavailable
from photoncar.sources.imu_lsm6dsox import ImuUnavailable, Lsm6dsoxSource


class LiveSensorsUnavailable(RuntimeError):
    """A live sensor cannot start: the OS is not Linux, or a device is missing or not ready."""


class LiveSensor(Source, Protocol):
    def stop(self) -> None: ...

    def close(self) -> None: ...


Opener = Callable[[Config, Clock], LiveSensor]
_OPEN_ERRORS = (GpsUnavailable, ImuUnavailable, OSError, ValueError, ConfigError)


def open_live_sensors(
    cfg: Config,
    clock: Clock,
    *,
    system: str | None = None,
    open_gps: Opener = GpsUbloxSource,
    open_imu: Opener = Lsm6dsoxSource,
) -> list[Source]:
    """Open and configure the GPS, then the IMU. Returns ``[gps, imu]``.

    Raises ``LiveSensorsUnavailable`` with a plain message on a non-Linux OS or when either
    device is missing, does not open, or does not take its settings. If the IMU fails, the
    GPS is closed first. ``system`` and the openers exist for tests (a pretend OS, fakes).
    """
    current = system or platform.system()
    if current != "Linux":
        raise LiveSensorsUnavailable(f"Live GPS and IMU need Linux. This system is {current}.")
    try:
        gps = open_gps(cfg, clock)
    except _OPEN_ERRORS as exc:
        raise LiveSensorsUnavailable(_why("GPS", _gps_path(cfg), exc)) from exc
    try:
        imu = open_imu(cfg, clock)
    except _OPEN_ERRORS as exc:
        gps.close()
        raise LiveSensorsUnavailable(_why("IMU", _imu_path(cfg), exc)) from exc
    return [gps, imu]


def _gps_path(cfg: Config) -> str:
    try:
        return str(cfg.get("gps.device"))
    except ConfigError:
        return "its device"


def _imu_path(cfg: Config) -> str:
    try:
        return f"/dev/i2c-{int(cfg.get('imu.i2c_bus'))}"
    except (ConfigError, TypeError, ValueError):
        return "its I2C bus"


def _why(name: str, path: str, exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return f"The {name} is missing: {path} does not exist."
    if isinstance(exc, PermissionError):
        return f"The {name} at {path} needs read and write permission."
    if isinstance(exc, GpsUnavailable | ImuUnavailable):
        return str(exc)
    if isinstance(exc, OSError):
        detail = exc.strerror or str(exc)
        return f"The {name} at {path} did not open ({detail})."
    return f"The {name} settings are not valid: {exc}"
