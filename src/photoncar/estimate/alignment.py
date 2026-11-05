"""IMU mount alignment: the rotation from the sensor frame to the vehicle frame.

Vehicle frame: x forward, y left, z up. ``R`` maps sensor vectors to vehicle vectors:
``v_vehicle = R @ v_sensor``; its rows are the vehicle axes written in sensor coordinates.

1. Rest: samples where the reference speed stays under ``performance.start_speed_mps`` for
   at least ``estimator.stationary_s`` and the specific force stays within
   ``estimator.align_min_accel_mps2`` of that period's median. The mean specific force at
   rest points up (the accelerometer reads +g0 on a level, still sensor), which fixes roll
   and pitch.
2. Forward axis: moving, straight-line samples (turning acceleration v * |yaw rate| under
   ``estimator.align_min_accel_mps2``) with |dv/dt| of at least the same value. The heading
   in the horizontal plane is psi = atan2(sum(h2 * a_ref), sum(h1 * a_ref)), where h1 and h2
   are the horizontal specific force components and a_ref is the signed dv/dt of the
   reference speed. Braking samples have a_ref < 0 and a backward specific force, so they add
   with the same sign as acceleration samples: the sign of a_ref resolves the 180 degree
   ambiguity that an unsigned principal axis would leave.

Without noise on level ground the result is exact to rounding.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

from photoncar.config import Config
from photoncar.records import ImuSample

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]


class AlignmentError(ValueError):
    """The data cannot determine the mount rotation (no rest, or no straight-line accel)."""


@dataclass(frozen=True, slots=True)
class ImuArrays:
    """IMU samples as arrays: ``accel`` specific force (m/s^2) and ``gyro`` (rad/s), (N, 3)."""

    t_ns: IntArray
    accel: FloatArray
    gyro: FloatArray

    def __post_init__(self) -> None:
        n = self.t_ns.shape[0] if self.t_ns.ndim == 1 else -1
        if n < 0 or self.accel.shape != (n, 3) or self.gyro.shape != (n, 3):
            raise ValueError("ImuArrays needs t_ns (N,), accel (N, 3), gyro (N, 3)")

    @classmethod
    def from_samples(cls, samples: Sequence[ImuSample]) -> ImuArrays:
        t = np.fromiter((s.t_ns for s in samples), dtype=np.int64, count=len(samples))
        acc = np.array([(s.ax, s.ay, s.az) for s in samples], dtype=np.float64).reshape(-1, 3)
        gyr = np.array([(s.gx, s.gy, s.gz) for s in samples], dtype=np.float64).reshape(-1, 3)
        return cls(t, acc, gyr)


def apply(r: ArrayLike, xyz: ArrayLike) -> FloatArray:
    """Rotate sensor-frame vectors into the vehicle frame. ``xyz`` is (3,) or (N, 3)."""
    rot = np.asarray(r, dtype=np.float64)
    v = np.asarray(xyz, dtype=np.float64)
    if rot.shape != (3, 3):
        raise ValueError("R must be 3x3")
    if v.shape[-1] != 3 or v.ndim > 2:
        raise ValueError("xyz must be (3,) or (N, 3)")
    out: FloatArray = v @ rot.T
    return out


def _rest_mask(
    ts: FloatArray, speed: FloatArray, accel: FloatArray, v_rest: float, min_s: float, a_max: float
) -> NDArray[np.bool_]:
    """Samples in runs of at least ``min_s`` with the reference speed under ``v_rest``.

    Interpolated speed stays under ``v_rest`` for a moment after a launch starts and before a
    stop ends, so inside each run a sample also needs its specific force within ``a_max`` of
    the run's median: the car is still, not starting or finishing a stop.
    """
    below = speed < v_rest
    mask = np.zeros(ts.size, dtype=np.bool_)
    m = np.concatenate(([False], below, [False]))
    d = np.diff(m.astype(np.int8))
    for a, b in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1) - 1, strict=True):
        if ts[b] - ts[a] < min_s:
            continue
        run = accel[a : b + 1]
        quiet = np.linalg.norm(run - np.median(run, axis=0), axis=1) <= a_max
        mask[a : b + 1] = quiet
    return mask


def estimate_mount_rotation(
    imu: ImuArrays, speed_t_ns: ArrayLike, speed_mps: ArrayLike, cfg: Config
) -> FloatArray:
    """The 3x3 sensor-to-vehicle rotation from rest periods and straight-line speed changes.

    ``speed_t_ns`` / ``speed_mps`` is a reference speed (GPS, wheel, or OBD) in time order.
    Raises ``AlignmentError`` when the drive has no qualifying rest or no qualifying
    acceleration or braking.
    """
    st = np.asarray(speed_t_ns, dtype=np.int64)
    sv = np.asarray(speed_mps, dtype=np.float64)
    if st.ndim != 1 or sv.shape != st.shape or st.size < 2:
        raise AlignmentError("need at least 2 reference speed samples")
    if not bool(np.all(np.diff(st) > 0)):
        raise AlignmentError("reference speed times must be strictly increasing")
    if imu.t_ns.size < 2:
        raise AlignmentError("need IMU samples")
    est = cfg.section("estimator")
    v_rest = float(cfg.get("performance.start_speed_mps"))
    stationary_s = float(est["stationary_s"])
    a_min = float(est["align_min_accel_mps2"])

    t_ref = int(min(int(imu.t_ns[0]), int(st[0])))
    ti = (imu.t_ns - np.int64(t_ref)).astype(np.float64) / 1e9
    tsp = (st - np.int64(t_ref)).astype(np.float64) / 1e9
    covered = (ti >= tsp[0]) & (ti <= tsp[-1])
    v_at = np.interp(ti, tsp, sv)
    a_at = np.interp(ti, tsp, np.gradient(sv, tsp))

    rest = _rest_mask(ti, np.where(covered, v_at, np.inf), imu.accel, v_rest, stationary_s, a_min)
    if not bool(np.any(rest)):
        raise AlignmentError(f"needs at least {stationary_s:g} s at rest")
    f_rest = imu.accel[rest].mean(axis=0)
    norm = float(np.linalg.norm(f_rest))
    if norm == 0.0:
        raise AlignmentError("zero specific force at rest")
    up = f_rest / norm
    gyro_bias = imu.gyro[rest].mean(axis=0)

    yaw_rate = (imu.gyro - gyro_bias) @ up
    straight = v_at * np.abs(yaw_rate) < a_min
    use = covered & ~rest & (v_at >= v_rest) & straight & (np.abs(a_at) >= a_min)
    if not bool(np.any(use)):
        raise AlignmentError("needs straight-line acceleration or braking")

    # Horizontal basis: the sensor axis least aligned with up, projected onto the plane.
    axis = np.zeros(3)
    axis[int(np.argmin(np.abs(up)))] = 1.0
    h1 = axis - float(axis @ up) * up
    h1 /= float(np.linalg.norm(h1))
    h2 = np.cross(up, h1)
    f = imu.accel[use]
    a_ref = a_at[use]
    c = float(np.sum((f @ h1) * a_ref))
    s = float(np.sum((f @ h2) * a_ref))
    if math.hypot(c, s) == 0.0:
        raise AlignmentError("acceleration samples carry no heading")
    psi = math.atan2(s, c)
    fwd = math.cos(psi) * h1 + math.sin(psi) * h2
    left = np.cross(up, fwd)
    rot: FloatArray = np.vstack((fwd, left, up))
    return rot
