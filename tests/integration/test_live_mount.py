"""A live drive finds its IMU mount rotation from its own data; until then the IMU is recorded
but never steers the estimate.

The drive is scripted: 5 s parked, 3 m/s^2 to 21 m/s, 8 s of cruise, a 0.82 g stop (8 m/s^2),
then parked. OBD speed comes at 2 Hz in 1 km/h steps and the IMU at 104 Hz, with the board
either aligned with the car or turned 90 degrees in yaw (the sensor's y axis points forward).
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from photoncar.api.live import LiveState
from photoncar.config import Config, load_config, load_vehicle
from photoncar.pipeline.runner import RunReport, run_to_data_dir
from photoncar.records import Event, ImuSample, Record, Sample
from photoncar.sources.base import SourceMeta
from photoncar.store.reader import DriveReader
from photoncar.units import G0, NS_PER_S

IMU_HZ = 104.0
END_S = 35.0
BRAKE_MPS2 = 8.0
TOP_MPS = 21.0
BRAKE_START_S = 20.0
YAW_90 = np.array([[0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
"""Sensor to vehicle for the turned board: vehicle forward is sensor y, left is sensor -x."""


def accel(t: float) -> float:
    if t < 5.0:
        return 0.0
    if t < 12.0:
        return 3.0
    if t < BRAKE_START_S:
        return 0.0
    if t < BRAKE_START_S + TOP_MPS / BRAKE_MPS2:
        return -BRAKE_MPS2
    return 0.0


def speed(t: float) -> float:
    if t < 5.0:
        return 0.0
    if t < 12.0:
        return 3.0 * (t - 5.0)
    if t < BRAKE_START_S:
        return TOP_MPS
    return max(0.0, TOP_MPS - BRAKE_MPS2 * (t - BRAKE_START_S))


class LiveDrive:
    """A live-kind source, yielded as fast as it is read (record times are scripted)."""

    def __init__(self, turned: bool, *, parked_start: bool = True, end_s: float = END_S) -> None:
        self._turned = turned
        self._parked_start = parked_start
        self._end_s = end_s

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(name="live:can0+can1", kind="live")

    def records(self) -> Iterator[Record]:
        next_obd = 0.0
        for i in range(int(self._end_s * IMU_HZ)):
            t = i / IMU_HZ
            t_ns = NS_PER_S + round(t * NS_PER_S)
            a = accel(t) if self._parked_start else 0.0
            ax, ay = (0.0, a) if self._turned else (a, 0.0)
            yield ImuSample(t_ns, ax, ay, G0, 0.0, 0.0, 0.0, 298.15)
            if t >= next_obd:
                v = speed(t) if self._parked_start else 15.0
                yield Sample(t_ns, "obd.speed", round(v * 3.6) / 3.6)
                next_obd += 0.5


@pytest.fixture
def cfg() -> Config:
    return load_config()


def _run(cfg: Config, tmp_path: Path, source: LiveDrive) -> RunReport:
    return run_to_data_dir(
        [source],
        cfg,
        load_vehicle(str(cfg.get("vehicle.profile"))),
        tmp_path,
        vehicle_profile=None,
        label="Live drive",
        live=LiveState(),
    )


def _events(rep: RunReport, kind: str) -> list[Event]:
    with DriveReader(rep.path) as r:
        return [e for e in r.events() if e.kind == kind]


@pytest.mark.parametrize("turned", [False, True])
def test_the_hard_brake_is_found_whichever_way_the_imu_is_mounted(
    cfg: Config, tmp_path: Path, turned: bool
) -> None:
    rep = _run(cfg, tmp_path, LiveDrive(turned))
    (brake,) = _events(rep, "behavior.hard_brake")
    assert brake.value == pytest.approx(BRAKE_MPS2 / G0, abs=0.03)
    assert brake.t_start_ns / NS_PER_S - 1.0 == pytest.approx(BRAKE_START_S, abs=1.0)
    # The launch is 0.31 g, under the 0.35 g hard-accel limit: no event from the handover.
    assert _events(rep, "behavior.hard_accel") == []
    found = [n for n in rep.notes if n.startswith("Mount rotation found ")]
    assert found == [
        "Mount rotation found 6.0 s into the drive, from its own rest and straight-line speed "
        "change. Before that the IMU was recorded but not used."
    ]
    expect = YAW_90 if turned else np.eye(3)
    assert rep.mount_rotation is not None
    assert np.allclose(np.array(rep.mount_rotation), expect, atol=0.02)
    with DriveReader(rep.path) as r:
        stored = r.meta().imu_mount_rot
        states = r.state()
    assert stored is not None and np.allclose(np.array(stored), expect, atol=0.02)
    # The IMU joins only after the rotation is known.
    first_imu = min(s.t_ns for s in states if "imu" in s.inputs.split("+"))
    assert (first_imu - NS_PER_S) / NS_PER_S >= 6.0


def test_an_unknown_mount_never_steers_the_estimate(cfg: Config, tmp_path: Path) -> None:
    """No rest in the whole drive (it starts at cruise): the rotation stays unknown, and the
    IMU is recorded but left out, so nothing is analysed on a wrong axis."""
    rep = _run(cfg, tmp_path, LiveDrive(turned=True, parked_start=False))
    assert rep.mount_rotation is None
    assert (
        "Mount rotation: unknown. The estimate needs at least 2 s at rest. The IMU was "
        "recorded but not used."
    ) in rep.notes
    assert (
        "IMU not used: speed and longitudinal accel come from speed measurements alone. "
        "No lateral g, cornering, or lateral jerk."
    ) in rep.notes
    with DriveReader(rep.path) as r:
        assert r.meta().imu_mount_rot is None
        assert sum(1 for _ in r.iter_imu_samples()) == int(END_S * IMU_HZ)
        states = r.state()
    assert states and all("imu" not in s.inputs.split("+") for s in states)
    assert all(math.isnan(s.accel_lat_mps2) for s in states)


def test_a_rotation_found_only_at_the_end_is_stored_but_not_used(
    cfg: Config, tmp_path: Path
) -> None:
    """The drive ends 0.9 s into its launch, before the next try: the last try over the final
    data finds the rotation, too late for the IMU to feed the estimate."""
    rep = _run(cfg, tmp_path, LiveDrive(turned=True, end_s=5.9))
    assert (
        "Mount rotation found at the end of the drive, too late to use. The IMU was recorded "
        "but not used."
    ) in rep.notes
    assert rep.mount_rotation is not None
    assert np.allclose(np.array(rep.mount_rotation), YAW_90, atol=0.05)
    with DriveReader(rep.path) as r:
        stored = r.meta().imu_mount_rot
        states = r.state()
    assert stored is not None and np.allclose(np.array(stored), YAW_90, atol=0.05)
    assert states and all("imu" not in s.inputs.split("+") for s in states)
