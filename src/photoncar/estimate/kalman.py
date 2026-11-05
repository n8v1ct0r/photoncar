"""Vehicle state estimation: a 2-state longitudinal Kalman filter plus a yaw-rate bias filter,
and a speed-only filter for when no IMU is there.

Filter A, state [v, b]: speed and a slowly varying longitudinal accel bias (sensor bias plus
road grade). It predicts at the IMU rate with v += (a_x - b) dt and updates on GPS speed,
OBD speed (PID 0x0D), or CAN wheel speed. Every update is gated at NIS < ``nis_gate``.

Rest: once every speed measurement for ``estimator.stationary_s`` has been under
``performance.start_speed_mps`` and the longitudinal accel stays within
``estimator.align_min_accel_mps2`` of the bias, each IMU sample applies a zero-velocity update
(an exact v = 0 constraint, which also teaches the bias). Filter B re-estimates the gyro
z bias as the mean over the current standstill; yaw rate r = gyro_z - b_z.

Outputs: speed, a_long = a_x - b, lateral accel v * r (primary) and a_y (cross-check).

Speed-only mode (a live OBD session has no IMU; the car-agnostic baseline). When no IMU sample
arrived within ``rules.shared.stale_after_polls`` IMU periods (3 / 104 Hz = 28.8 ms, the
approved stale rule), counted from the first record the estimator saw when no IMU sample has
come yet, each speed measurement emits an estimate from filter S instead, state
[v, a]: speed and longitudinal accel, a constant-accel model driven by white jerk
(``EstimatorTuning.speed_only_jerk_psd``), updated and gated like filter A. So a_long is the
accel the filter derives from the filtered speed. At rest (the same speed rule as above, with
no accel check since nothing measures accel) v = 0 and a = 0. Lateral accel, the IMU
cross-check, and yaw rate are unknown without an IMU: they are NaN, and ``inputs`` has no
"imu" part. Filter A's speed follows filter S, so an IMU that comes back continues from it.

GPS dropouts: ``dr`` is True whenever no GPS fix arrived within ``rules.shared.stale_after_polls``
GPS periods. With no speed measurement of any kind for longer than
``estimator.max_dead_reckon_s`` the estimator stops emitting and restarts from the next speed
measurement, so it never publishes a speed older than that limit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from photoncar.config import Config
from photoncar.records import GpsFix, ImuSample, Record, Sample, StateEstimate
from photoncar.units import G0

OBD_SPEED = "obd.speed"
WHEEL_SPEED = "can.wheel_speed"
OBD_SPEED_STEP = 1.0 / 3.6
"""PID 0x0D resolution: 1 km/h in m/s."""


@dataclass(frozen=True, slots=True)
class EstimatorTuning:
    """Noise model. These are filter tuning values, not alert thresholds.

    ``accel_sigma_mps2`` is the per-sample longitudinal accel noise the filter assumes
    (vibration plus sensor noise at the IMU rate). ``gps_speed_sigma_mps`` is the receiver's
    speed accuracy (NEO-M9N datasheet: 0.05 m/s). The OBD speed variance is derived from its
    1 km/h step and the poll timeout, not tuned.

    Speed-only mode: ``speed_only_jerk_psd`` ((m/s^3)^2 s) lets the accel change by
    sqrt(64 * 0.5) = 5.7 m/s^2 (0.58 g) at 1 sigma over 1 fast OBD poll period (0.5 s), so a
    brake onset of up to about 1 g passes the NIS gate. At cruise it leaves about 0.03 g of
    noise on a 1 s mean of the accel from 2 Hz OBD speed. ``speed_only_accel_sigma_mps2`` is
    the accel prior whenever filter S starts or leaves rest: unknown, 1 g at 1 sigma.
    """

    accel_sigma_mps2: float = 0.5
    bias_rw_mps2_per_sqrt_s: float = 0.01
    bias_init_sigma_mps2: float = 0.5
    gps_speed_sigma_mps: float = 0.05
    wheel_speed_sigma_mps: float = 0.1
    max_consecutive_rejects: int = 3
    speed_only_jerk_psd: float = 64.0
    speed_only_accel_sigma_mps2: float = G0


@dataclass(slots=True)
class _SpeedFilter:
    """Filter S, state [v, a] with covariance [[pvv, pva], [pva, paa]]."""

    t: int
    v: float
    a: float
    pvv: float
    pva: float
    paa: float

    def predict(self, t: int, q_jerk: float) -> None:
        if t <= self.t:
            return
        dt = (t - self.t) / 1e9
        self.t = t
        self.v += self.a * dt
        pvv, pva, paa = self.pvv, self.pva, self.paa
        self.pvv = pvv + 2.0 * dt * pva + dt * dt * paa + q_jerk * dt**3 / 3.0
        self.pva = pva + dt * paa + q_jerk * dt * dt / 2.0
        self.paa = paa + q_jerk * dt

    def update(self, z: float, r: float) -> None:
        s = self.pvv + r
        y = z - self.v
        kv, ka = self.pvv / s, self.pva / s
        self.v += kv * y
        self.a += ka * y
        pvv, pva, paa = self.pvv, self.pva, self.paa
        self.pvv = (1.0 - kv) * pvv
        self.pva = (1.0 - kv) * pva
        self.paa = paa - ka * pva

    def nis(self, z: float, r: float) -> float:
        y = z - self.v
        return y * y / (self.pvv + r)

    def rest(self, paa: float) -> None:
        self.v, self.a = 0.0, 0.0
        self.pvv, self.pva, self.paa = 0.0, 0.0, paa


def _rotation(mount_rot: NDArray[np.float64]) -> NDArray[np.float64]:
    rot = np.asarray(mount_rot, dtype=np.float64)
    if rot.shape != (3, 3) or not np.allclose(rot @ rot.T, np.eye(3), atol=1e-6):
        raise ValueError("mount_rot must be a 3x3 rotation matrix")
    return rot


class StateEstimator:
    """Push records in time order. Each IMU sample yields one ``StateEstimate`` once running;
    without a recent IMU sample, each speed measurement yields one (speed-only mode)."""

    def __init__(
        self,
        cfg: Config,
        mount_rot: NDArray[np.float64] | None = None,
        *,
        tuning: EstimatorTuning | None = None,
    ) -> None:
        self._rot = _rotation(np.eye(3) if mount_rot is None else mount_rot)
        self._tune = tuning or EstimatorTuning()
        est = cfg.section("estimator")
        self._stationary_ns = round(float(est["stationary_s"]) * 1e9)
        self._nis_gate = float(est["nis_gate"])
        self._max_dr_ns = round(float(est["max_dead_reckon_s"]) * 1e9)
        self._motion_accel = float(est["align_min_accel_mps2"])
        self._rest_speed = float(cfg.get("performance.start_speed_mps"))
        polls = int(cfg.get("rules.shared.stale_after_polls"))
        self._gps_stale_ns = round(polls / float(cfg.get("gps.rate_hz")) * 1e9)
        self._obd_stale_ns = round(polls / float(cfg.get("obd.fast_hz_each")) * 1e9)
        imu_hz = float(cfg.get("imu.rate_hz"))
        self._imu_stale_ns = round(polls / imu_hz * 1e9)
        timeout = float(cfg.get("obd.response_timeout_s"))
        self._obd_latency_sq = timeout * timeout / 3.0  # E[tau^2], tau uniform on [0, timeout]
        self._q_accel = self._tune.accel_sigma_mps2**2 / imu_hz  # PSD, (m/s^2)^2 s
        self._q_bias = self._tune.bias_rw_mps2_per_sqrt_s**2
        self._paa0 = self._tune.speed_only_accel_sigma_mps2**2

        self._running = False
        self._t: int | None = None
        self._v = 0.0
        self._b = 0.0
        self._pvv = 0.0
        self._pvb = 0.0
        self._pbb = self._tune.bias_init_sigma_mps2**2
        self._a_x: float | None = None
        self._last_speed_t: int | None = None
        self._last_gps_t: int | None = None
        self._last_obd_t: int | None = None
        self._last_wheel_t: int | None = None
        self._last_imu_t: int | None = None
        self._first_t: int | None = None
        self._low_since: int | None = None
        self._rejects: dict[str, int] = {}
        self._rest = False
        self._gz_sum = 0.0
        self._gz_n = 0
        self._bz = 0.0
        self._so: _SpeedFilter | None = None
        self._so_rejects: dict[str, int] = {}

    def set_mount_rotation(self, mount_rot: NDArray[np.float64]) -> None:
        """Use ``mount_rot`` for every IMU sample from now on (a live drive finds its rotation
        after it starts)."""
        self._rot = _rotation(mount_rot)

    @property
    def bias(self) -> float:
        """Current longitudinal accel bias estimate (m/s^2)."""
        return self._b

    @property
    def imu_stale_s(self) -> float:
        """How long without an IMU sample before speed-only mode (s)."""
        return self._imu_stale_ns / 1e9

    def push(self, rec: Record) -> list[StateEstimate]:
        if self._first_t is None and isinstance(rec, ImuSample | GpsFix | Sample):
            self._first_t = rec.t_ns
        if isinstance(rec, ImuSample):
            self._last_imu_t = rec.t_ns
            self._so = None  # filter A takes over; its speed followed filter S
            return self._on_imu(rec)
        if isinstance(rec, GpsFix):
            if rec.fix_type > 0 and math.isfinite(rec.speed_mps):
                self._last_gps_t = rec.t_ns
                return self._on_speed(rec.t_ns, rec.speed_mps, "gps")
            return []
        if isinstance(rec, Sample) and math.isfinite(rec.value):
            if rec.signal == OBD_SPEED:
                self._last_obd_t = rec.t_ns
                return self._on_speed(rec.t_ns, rec.value, "obd")
            if rec.signal == WHEEL_SPEED:
                self._last_wheel_t = rec.t_ns
                return self._on_speed(rec.t_ns, rec.value, "wheel")
        return []

    def _variance(self, source: str, accel: float) -> float:
        if source == "gps":
            return self._tune.gps_speed_sigma_mps**2
        if source == "wheel":
            return self._tune.wheel_speed_sigma_mps**2
        return OBD_SPEED_STEP**2 / 12.0 + accel * accel * self._obd_latency_sq

    def _on_speed(self, t: int, z: float, source: str) -> list[StateEstimate]:
        if z > self._rest_speed:
            self._low_since = None
        elif self._low_since is None:
            self._low_since = t
        stale = self._last_speed_t is not None and t - self._last_speed_t > self._max_dr_ns
        self._last_speed_t = t
        imu_t = self._last_imu_t if self._last_imu_t is not None else self._first_t
        if imu_t is None or t - imu_t > self._imu_stale_ns:
            return self._speed_only(t, z, source, stale)
        a = abs(self._a_x - self._b) if self._a_x is not None else 0.0
        self._update_a(t, z, self._variance(source, a), source, stale)
        return []

    # Filter A ------------------------------------------------------------------------------

    def _predict(self, t: int) -> None:
        if self._t is None:
            self._t = t
            return
        if t <= self._t:
            return
        dt = (t - self._t) / 1e9
        self._t = t
        if not self._running:
            return
        if self._a_x is not None:
            self._v += (self._a_x - self._b) * dt
        pvv, pvb, pbb = self._pvv, self._pvb, self._pbb
        self._pvv = pvv - 2.0 * dt * pvb + dt * dt * pbb + self._q_accel * dt
        self._pvb = pvb - dt * pbb
        self._pbb = pbb + self._q_bias * dt

    def _update_a(self, t: int, z: float, r: float, source: str, stale: bool) -> None:
        if not self._running or stale:
            self._start(t, z, r)
            return
        self._predict(t)
        y = z - self._v
        s = self._pvv + r
        if y * y / s > self._nis_gate:
            n = self._rejects.get(source, 0) + 1
            self._rejects[source] = n
            if n >= self._tune.max_consecutive_rejects:
                self._rejects[source] = 0
                self._v, self._pvv, self._pvb = z, r, 0.0
            return
        self._rejects[source] = 0
        kv, kb = self._pvv / s, self._pvb / s
        self._v += kv * y
        self._b += kb * y
        pvv, pvb, pbb = self._pvv, self._pvb, self._pbb
        self._pvv = (1.0 - kv) * pvv
        self._pvb = (1.0 - kv) * pvb
        self._pbb = pbb - kb * pvb

    def _start(self, t: int, z: float, r: float) -> None:
        self._running = True
        self._t = t
        self._v = z
        self._pvv = r
        self._pvb = 0.0
        self._rejects.clear()

    def _zupt(self) -> None:
        """Exact zero-velocity constraint; the cross covariance moves the bias."""
        if self._pvv > 0.0:
            kb = self._pvb / self._pvv
            self._b -= kb * self._v
            self._pbb -= kb * self._pvb
        self._v = 0.0
        self._pvv = 0.0
        self._pvb = 0.0

    # Filter S (speed only) -----------------------------------------------------------------

    def _speed_only(self, t: int, z: float, source: str, stale: bool) -> list[StateEstimate]:
        so = self._so
        if so is None and self._running and not stale and self._t is not None:
            # The IMU stopped: continue from filter A's speed and last accel.
            a0 = self._a_x - self._b if self._a_x is not None else 0.0
            so = _SpeedFilter(self._t, self._v, a0, self._pvv, 0.0, self._paa0)
            self._so_rejects.clear()
        if so is None or stale:
            so = _SpeedFilter(
                t=t, v=z, a=0.0, pvv=self._variance(source, 0.0), pva=0.0, paa=self._paa0
            )
            self._so_rejects.clear()
        else:
            so.predict(t, self._tune.speed_only_jerk_psd)
            r = self._variance(source, so.a)
            if so.nis(z, r) > self._nis_gate:
                n = self._so_rejects.get(source, 0) + 1
                self._so_rejects[source] = n
                if n >= self._tune.max_consecutive_rejects:
                    self._so_rejects[source] = 0
                    so.v, so.pvv, so.pva = z, r, 0.0
            else:
                self._so_rejects[source] = 0
                so.update(z, r)
        rest = self._low_since is not None and t - self._low_since >= self._stationary_ns
        if rest:
            so.rest(self._paa0)
        self._so = so
        # Filter A follows, so an IMU that comes back continues from this speed. Its last
        # accel reading is stale: drop it rather than integrate it across the gap.
        self._running = True
        self._t = t
        self._v, self._pvv, self._pvb = so.v, so.pvv, 0.0
        self._a_x = None
        self._rest = False
        dr = self._last_gps_t is None or t - self._last_gps_t > self._gps_stale_ns
        return [
            StateEstimate(
                t_ns=t,
                speed_mps=max(so.v, 0.0),
                accel_long_mps2=so.a,
                accel_lat_mps2=math.nan,
                accel_lat_imu_mps2=math.nan,
                yaw_rate_rps=math.nan,
                var_speed=so.pvv,
                var_accel=so.paa,
                inputs=self._inputs(t, dr, rest, imu=False),
                dr=dr,
            )
        ]

    # IMU samples ---------------------------------------------------------------------------

    def _on_imu(self, s: ImuSample) -> list[StateEstimate]:
        t = s.t_ns
        if (
            self._running
            and self._last_speed_t is not None
            and t - self._last_speed_t > self._max_dr_ns
        ):
            self._running = False
            self._rest = False
        self._predict(t)
        acc = self._rot @ np.array((s.ax, s.ay, s.az))
        gyr = self._rot @ np.array((s.gx, s.gy, s.gz))
        ax, ay, gz = float(acc[0]), float(acc[1]), float(gyr[2])
        self._a_x = ax
        if not self._running:
            return []
        rest = (
            self._low_since is not None
            and t - self._low_since >= self._stationary_ns
            and abs(ax - self._b) < self._motion_accel
        )
        if rest:
            if not self._rest:
                self._gz_sum, self._gz_n = 0.0, 0
            self._zupt()
            self._gz_sum += gz
            self._gz_n += 1
            self._bz = self._gz_sum / self._gz_n
        self._rest = rest
        r_yaw = gz - self._bz
        speed = max(self._v, 0.0)
        dr = self._last_gps_t is None or t - self._last_gps_t > self._gps_stale_ns
        return [
            StateEstimate(
                t_ns=t,
                speed_mps=speed,
                accel_long_mps2=ax - self._b,
                accel_lat_mps2=speed * r_yaw,
                accel_lat_imu_mps2=ay,
                yaw_rate_rps=r_yaw,
                var_speed=self._pvv,
                var_accel=self._pbb + self._tune.accel_sigma_mps2**2,
                inputs=self._inputs(t, dr, rest, imu=True),
                dr=dr,
            )
        ]

    def _inputs(self, t: int, dr: bool, rest: bool, *, imu: bool) -> str:
        parts = ["imu"] if imu else []
        if not dr:
            parts.append("gps")
        if self._last_obd_t is not None and t - self._last_obd_t <= self._obd_stale_ns:
            parts.append("obd")
        if self._last_wheel_t is not None and t - self._last_wheel_t <= self._obd_stale_ns:
            parts.append("wheel")
        if rest:
            parts.append("zupt")
        return "+".join(parts)
