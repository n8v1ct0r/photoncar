"""open_live_sensors: Linux only, plain messages for a missing or unready device, and the
sensor modules stay on the standard library and away from any vehicle bus."""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from photoncar.clock import Clock, SimClock
from photoncar.config import Config, load_config
from photoncar.records import Record
from photoncar.sources.base import SourceMeta
from photoncar.sources.gps_ublox import GpsUnavailable
from photoncar.sources.imu_lsm6dsox import ImuUnavailable
from photoncar.sources.live_sensors import (
    LiveSensor,
    LiveSensorsUnavailable,
    Opener,
    open_live_sensors,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "photoncar"
SENSOR_MODULES = ["sources/gps_ublox.py", "sources/imu_lsm6dsox.py", "sources/live_sensors.py"]
ALLOWED_PHOTONCAR = {
    "photoncar.clock",
    "photoncar.config",
    "photoncar.records",
    "photoncar.units",
    "photoncar.sources.base",
    "photoncar.sources.gps_ublox",
    "photoncar.sources.imu_lsm6dsox",
}
CFG = load_config()


class StubSensor:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False
        self.stopped = False

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(name=self.name, kind="live")

    def records(self) -> Iterator[Record]:
        return iter(())

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


def opener(sensor: LiveSensor) -> Opener:
    def open_it(cfg: Config, clock: Clock) -> LiveSensor:
        return sensor

    return open_it


def failing(exc: Exception) -> Opener:
    def open_it(cfg: Config, clock: Clock) -> LiveSensor:
        raise exc

    return open_it


def never(cfg: Config, clock: Clock) -> LiveSensor:
    raise AssertionError("must not open a device")


def cfg_with(section: str, **values: object) -> Config:
    raw = dict(CFG.raw)
    raw[section] = {**CFG.section(section), **values}
    return Config(raw)


def plain(message: str) -> None:
    assert message.endswith(".")
    assert "–" not in message and "—" not in message
    assert message[0].isupper()


def test_returns_gps_then_imu() -> None:
    gps, imu = StubSensor("gps"), StubSensor("imu")
    out = open_live_sensors(
        CFG, SimClock(0), system="Linux", open_gps=lambda c, k: gps, open_imu=lambda c, k: imu
    )
    assert out == [gps, imu]
    assert not gps.closed and not imu.closed


@pytest.mark.parametrize("system", ["Darwin", "Windows"])
def test_refuses_other_systems_before_opening_anything(system: str) -> None:
    with pytest.raises(LiveSensorsUnavailable) as err:
        open_live_sensors(CFG, SimClock(0), system=system, open_gps=never, open_imu=never)
    assert str(err.value) == f"Live GPS and IMU need Linux. This system is {system}."
    plain(str(err.value))


def test_missing_gps_device_is_named(tmp_path: Path) -> None:
    missing = tmp_path / "ttyACM9"
    with pytest.raises(LiveSensorsUnavailable) as err:
        open_live_sensors(cfg_with("gps", device=str(missing)), SimClock(0), system="Linux")
    assert str(err.value) == f"The GPS is missing: {missing} does not exist."
    assert isinstance(err.value.__cause__, FileNotFoundError)


def test_missing_imu_device_is_named_and_the_gps_is_closed() -> None:
    gps = StubSensor("gps")
    cfg = cfg_with("imu", i2c_bus=250)
    with pytest.raises(LiveSensorsUnavailable) as err:
        open_live_sensors(cfg, SimClock(0), system="Linux", open_gps=lambda c, k: gps)
    assert str(err.value) == "The IMU is missing: /dev/i2c-250 does not exist."
    assert gps.closed


@pytest.mark.parametrize(
    ("which", "exc", "message"),
    [
        (
            "gps",
            PermissionError(13, "Permission denied"),
            "The GPS at /dev/ttyACM0 needs read and write permission.",
        ),
        (
            "gps",
            OSError(16, "Resource busy"),
            "The GPS at /dev/ttyACM0 did not open (Resource busy).",
        ),
        (
            "gps",
            GpsUnavailable("The GPS on /dev/ttyACM0 refused its settings (UBX-ACK-NAK)."),
            "The GPS on /dev/ttyACM0 refused its settings (UBX-ACK-NAK).",
        ),
        (
            "gps",
            ValueError("gps.rate_hz must be above 0 and at most 25. It is 40.0."),
            "The GPS settings are not valid: gps.rate_hz must be above 0 and at most 25. "
            "It is 40.0.",
        ),
        (
            "imu",
            ImuUnavailable("No IMU answered at 0x6a on /dev/i2c-1 (Remote I/O error)."),
            "No IMU answered at 0x6a on /dev/i2c-1 (Remote I/O error).",
        ),
        (
            "imu",
            PermissionError(13, "Permission denied"),
            "The IMU at /dev/i2c-1 needs read and write permission.",
        ),
    ],
)
def test_open_errors_become_plain_messages(which: str, exc: Exception, message: str) -> None:
    gps = StubSensor("gps")
    open_gps = failing(exc) if which == "gps" else opener(gps)
    open_imu = failing(exc) if which == "imu" else never
    with pytest.raises(LiveSensorsUnavailable) as err:
        open_live_sensors(
            CFG,
            SimClock(0),
            system="Linux",
            open_gps=open_gps,
            open_imu=open_imu,
        )
    assert str(err.value) == message
    assert err.value.__cause__ is exc
    plain(str(err.value))
    assert gps.closed is (which == "imu")


def test_real_gps_rate_check_runs_before_the_device_opens(tmp_path: Path) -> None:
    cfg = cfg_with("gps", rate_hz=40.0, device=str(tmp_path / "ttyACM9"))
    with pytest.raises(LiveSensorsUnavailable, match=r"^The GPS settings are not valid: "):
        open_live_sensors(cfg, SimClock(0), system="Linux")


def _imports(tree: ast.Module) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module
            out.append(node.module)
    return out


@pytest.mark.parametrize("rel", SENSOR_MODULES)
def test_sensor_modules_use_only_the_standard_library_and_no_bus(rel: str) -> None:
    tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
    for name in _imports(tree):
        root = name.split(".")[0]
        if root == "photoncar":
            assert name in ALLOWED_PHOTONCAR, name
        else:
            assert root in sys.stdlib_module_names, name
        assert root not in {"can", "socket", "cantools", "serial", "smbus", "smbus2"}, name
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)]
    assert not any(isinstance(s, str) and ("can0" in s or "can1" in s) for s in strings)
