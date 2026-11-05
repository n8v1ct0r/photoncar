"""IMU mount alignment: recover a known (roll, pitch, yaw) mount from synthetic drives."""

from __future__ import annotations

import math

import numpy as np
import pytest

from photoncar.config import Config, load_config
from photoncar.estimate.alignment import AlignmentError, ImuArrays, apply, estimate_mount_rotation
from photoncar.records import ImuSample
from photoncar.units import G0

T0 = 3_000_000_000_000
IMU_HZ = 104.0
GPS_HZ = 10.0


@pytest.fixture(scope="module")
def cfg() -> Config:
    return load_config()


def mount(roll_deg: float, pitch_deg: float, yaw_deg: float) -> np.ndarray:
    """Sensor-to-vehicle rotation Rz(yaw) Ry(pitch) Rx(roll)."""
    r, p, y = (math.radians(a) for a in (roll_deg, pitch_deg, yaw_deg))
    rx = np.array([[1, 0, 0], [0, math.cos(r), -math.sin(r)], [0, math.sin(r), math.cos(r)]])
    ry = np.array([[math.cos(p), 0, math.sin(p)], [0, 1, 0], [-math.sin(p), 0, math.cos(p)]])
    rz = np.array([[math.cos(y), -math.sin(y), 0], [math.sin(y), math.cos(y), 0], [0, 0, 1]])
    out: np.ndarray = rz @ ry @ rx
    return out


# (duration s, longitudinal accel m/s^2, yaw rate rad/s)
LAUNCH_AND_STOP = [(5.0, 0.0, 0.0), (6.0, 0.3 * G0, 0.0), (3.0, 0.0, 0.0), (4.0, 0.0, 0.25)]
BRAKE_ONLY = [(2.0, 0.0, 0.0), (20.0 / (0.5 * G0), -0.5 * G0, 0.0), (4.0, 0.0, 0.0)]


def profile(
    plan: list[tuple[float, float, float]], v0: float, stop_g: float | None
) -> list[tuple[float, float, float]]:
    """Append a straight stop from the plan's end speed, then 3 s at rest."""
    v = v0 + sum(d * a for d, a, _ in plan)
    out = list(plan)
    if stop_g is not None and v > 0:
        out += [(v / (stop_g * G0), -stop_g * G0, 0.0), (3.0, 0.0, 0.0)]
    return out


def synth(
    plan: list[tuple[float, float, float]],
    v0: float,
    rot: np.ndarray,
    *,
    seed: int | None = None,
    accel_noise: float = 0.0,
    gyro_noise: float = 0.0,
    gps_noise: float = 0.0,
) -> tuple[ImuArrays, np.ndarray, np.ndarray, np.ndarray]:
    """IMU in the sensor frame (level road) plus 10 Hz reference speed, and vehicle-frame f."""
    rng = np.random.default_rng(seed)
    total = sum(d for d, _, _ in plan)
    edges = np.cumsum([0.0] + [d for d, _, _ in plan])

    def state(ts: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        idx = np.clip(np.searchsorted(edges, ts, side="right") - 1, 0, len(plan) - 1)
        acc = np.array([plan[i][1] for i in idx])
        yaw = np.array([plan[i][2] for i in idx])
        v_start = v0 + np.concatenate(([0.0], np.cumsum([d * a for d, a, _ in plan])))
        v = v_start[idx] + acc * (ts - edges[idx])
        return np.clip(v, 0.0, None), acc, yaw

    k = np.arange(int(total * IMU_HZ))
    t = T0 + np.round(k * 1e9 / IMU_HZ + 2_345_678).astype(np.int64)
    ts = (t - T0) / 1e9
    v, a, r = state(ts)
    f_vehicle = np.column_stack((a, v * r, np.full(ts.size, G0)))
    g_vehicle = np.column_stack((np.zeros(ts.size), np.zeros(ts.size), r))
    f_sensor = f_vehicle @ rot  # rows: R^T @ f
    g_sensor = g_vehicle @ rot
    f_sensor = f_sensor + rng.normal(0.0, accel_noise, f_sensor.shape)
    g_sensor = g_sensor + rng.normal(0.0, gyro_noise, g_sensor.shape)
    kg = np.arange(int(total * GPS_HZ))
    tg = T0 + np.round(kg * 1e9 / GPS_HZ + 51_234_567).astype(np.int64)
    vg, _, _ = state((tg - T0) / 1e9)
    vg = np.abs(vg + rng.normal(0.0, gps_noise, vg.size))
    return ImuArrays(t, f_sensor, g_sensor), tg, vg, f_vehicle


def angle_deg(a: np.ndarray, b: np.ndarray) -> float:
    c = (np.trace(a @ b.T) - 1.0) / 2.0
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def test_recovers_5_minus3_30_exactly(cfg: Config) -> None:
    rot = mount(5.0, -3.0, 30.0)
    imu, tg, vg, f_vehicle = synth(profile(LAUNCH_AND_STOP, 0.0, 0.5), 0.0, rot)
    got = estimate_mount_rotation(imu, tg, vg, cfg)
    assert np.max(np.abs(got - rot)) < 1e-9
    assert np.max(np.abs(apply(got, imu.accel) - f_vehicle)) < 1e-9
    assert np.max(np.abs(got @ got.T - np.eye(3))) < 1e-12
    assert abs(np.linalg.det(got) - 1.0) < 1e-12


def test_recovers_with_noise_within_half_a_degree(cfg: Config) -> None:
    rot = mount(5.0, -3.0, 30.0)
    imu, tg, vg, _ = synth(
        profile(LAUNCH_AND_STOP, 0.0, 0.5),
        0.0,
        rot,
        seed=20260923,
        accel_noise=0.05,
        gyro_noise=0.002,
        gps_noise=0.05,
    )
    got = estimate_mount_rotation(imu, tg, vg, cfg)
    assert angle_deg(got, rot) < 0.5


@pytest.mark.parametrize("yaw_deg", [30.0, 210.0, -95.0])
def test_braking_samples_resolve_the_180_degree_ambiguity(cfg: Config, yaw_deg: float) -> None:
    # Only braking, no acceleration: the heading sign must come from the braking samples.
    rot = mount(5.0, -3.0, yaw_deg)
    imu, tg, vg, _ = synth(BRAKE_ONLY, 20.0, rot)
    got = estimate_mount_rotation(imu, tg, vg, cfg)
    assert np.max(np.abs(got - rot)) < 1e-9
    flipped = mount(5.0, -3.0, yaw_deg + 180.0)
    assert angle_deg(got, flipped) > 179.0


def test_apply_shapes() -> None:
    rot = mount(0.0, 0.0, 90.0)
    assert np.allclose(apply(rot, [1.0, 0.0, 0.0]), [0.0, 1.0, 0.0])
    assert apply(rot, np.ones((4, 3))).shape == (4, 3)
    with pytest.raises(ValueError):
        apply(np.eye(2), [1.0, 0.0])


def test_needs_rest(cfg: Config) -> None:
    rot = mount(5.0, -3.0, 30.0)
    imu, tg, vg, _ = synth([(6.0, 0.3 * G0, 0.0), (6.0, -0.3 * G0, 0.0)], 5.0, rot)
    with pytest.raises(AlignmentError, match="at rest"):
        estimate_mount_rotation(imu, tg, vg, cfg)


def test_needs_straight_line_speed_change(cfg: Config) -> None:
    rot = mount(5.0, -3.0, 30.0)
    imu, tg, vg, _ = synth([(4.0, 0.0, 0.0)], 0.0, rot)
    with pytest.raises(AlignmentError, match="straight-line"):
        estimate_mount_rotation(imu, tg, vg, cfg)


def test_imu_arrays_from_samples() -> None:
    s = [ImuSample(1, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0), ImuSample(2, 7.0, 8.0, 9.0, 1.0, 2.0, 3.0)]
    arr = ImuArrays.from_samples(s)
    assert arr.t_ns.tolist() == [1, 2]
    assert arr.accel[1].tolist() == [7.0, 8.0, 9.0]
    assert arr.gyro[0].tolist() == [4.0, 5.0, 6.0]
