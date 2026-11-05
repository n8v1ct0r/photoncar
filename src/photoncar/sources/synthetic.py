"""Synthetic sources: an OBD overlay built from a speed trace, and scripted known-answer drives.

Nothing here is measured. Every signal a synthetic source produces is listed in
``meta.synthetic_signals`` so the store, the UI, and exports can label it.

``SyntheticObdSource`` gives a drive that has no OBD-II data (comma2k19 replay) plausible service
01 polls on the ``config [obd]`` schedule: rpm from speed through an assumed 6-speed gear set,
throttle and load from acceleration, coolant near 90 °C, intake air temperature, fuel trims within
3 %, and module voltage near 14.1 V. Values pass through the SAE J1979 byte encoding, so they are
quantized as a real ECU reply would be (speed in whole km/h, coolant in whole °C).

``synthetic_drive(seed, scenario)`` scripts a whole drive with closed-form kinematics:

- ``"track_day"`` (fixed kinematics; the seed only changes sensor noise and OBD details): 5 s at
  rest, a standing start at a constant 0.5 g to 70 mph, 4 s of cruise, a stop from 70 mph at a
  constant 1.0 g, 3 s at rest, a pull away at 0.3 g to 25 m/s, then 3 laps at a constant 25 m/s
  of a counterclockwise oval (2 straights of 300 m, 2 half circles of radius 100 m) timed by a
  start and finish line in the middle of the top straight, then a stop at 0.5 g and 5 s at rest.
  Coolant reads 106 °C for exactly 20 s, starting 20 s after the first line crossing.
- ``"commute"``: stop and go on streets with 3 turns and a traffic light stop. The seed picks
  cruise speeds and durations.

Local frame: x east, y north, meters, with the drive starting at rest at (0, 0) heading east.
GPS maps it to latitude and longitude around a fake origin at (0.0, 0.0), which is open ocean and
never a real place. The mapping is the WGS84 east/north tangent plane at the origin, the same
plane a store rollup builds from its first kept fix, and the first fix sits exactly on the origin,
so rollup x/y and this frame agree.
GPS runs at 10 Hz and the IMU at 104 Hz. The IMU is mounted aligned with the vehicle (x forward,
y left, z up) and reports specific force, so it reads about +9.81 m/s^2 on z at rest.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from photoncar.config import Config, ConfigError, load_config, load_vehicle
from photoncar.records import GpsFix, ImuSample, ObdPoll, Record, Sample, record_time
from photoncar.signals import OBD_PID_TO_SIGNAL, SIGNALS
from photoncar.sources.base import Source, SourceMeta
from photoncar.store.meta import GPS_FIX
from photoncar.units import G0, KELVIN_OFFSET, MPH, NS_PER_S

F64 = NDArray[np.float64]
I64 = NDArray[np.int64]
CoolantProfile = Callable[[float], float]

WGS84_A_M: Final = 6_378_137.0
WGS84_F: Final = 1.0 / 298.257223563
WGS84_E2: Final = WGS84_F * (2.0 - WGS84_F)
ORIGIN_LAT_DEG: Final = 0.0
ORIGIN_LON_DEG: Final = 0.0
T0_NS: Final = 1_000_000_000
GPS_HZ: Final = 10.0
IMU_HZ: Final = 104.0
TRACE_HZ: Final = 100.0
OBD_ECU: Final = 0x7E8
MAF_PID: Final = 0x10
SCENARIOS: Final = ("track_day", "commute")

# Sensor noise (1 sigma), fixed by the seed.
IMU_ACCEL_NOISE: Final = 0.03  # m/s^2
IMU_GYRO_NOISE: Final = 0.002  # rad/s
GPS_POS_NOISE_M: Final = 0.15
GPS_SPEED_NOISE: Final = 0.03  # m/s
GPS_HDOP: Final = 0.8
GPS_SATS: Final = 14
GPS_ALT_M: Final = 10.0

# J1979 PID 0x03 fuel system status values.
FUEL_CLOSED_LOOP: Final = 2
FUEL_OPEN_LOOP_LOAD: Final = 4


# --- local meters <-> latitude and longitude ----------------------------------------------


def _ecef(lat_deg: F64, lon_deg: F64) -> tuple[F64, F64, F64]:
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    n = WGS84_A_M / np.sqrt(1.0 - WGS84_E2 * np.sin(lat) ** 2)
    return (
        n * np.cos(lat) * np.cos(lon),
        n * np.cos(lat) * np.sin(lon),
        n * (1.0 - WGS84_E2) * np.sin(lat),
    )


def to_local(
    lat_deg: F64, lon_deg: F64, lat0: float = ORIGIN_LAT_DEG, lon0: float = ORIGIN_LON_DEG
) -> tuple[F64, F64]:
    """East and north meters of each point in the WGS84 tangent plane at (lat0, lon0)."""
    x, y, z = _ecef(np.asarray(lat_deg, np.float64), np.asarray(lon_deg, np.float64))
    x0, y0, z0 = _ecef(np.array([lat0]), np.array([lon0]))
    dx, dy, dz = x - x0[0], y - y0[0], z - z0[0]
    sp, cp = math.sin(math.radians(lat0)), math.cos(math.radians(lat0))
    sl, cl = math.sin(math.radians(lon0)), math.cos(math.radians(lon0))
    return -sl * dx + cl * dy, -sp * cl * dx - sp * sl * dy + cp * dz


def to_latlon(
    x_m: F64, y_m: F64, lat0: float = ORIGIN_LAT_DEG, lon0: float = ORIGIN_LON_DEG
) -> tuple[F64, F64]:
    """Inverse of ``to_local`` for points on the ellipsoid, by fixed-point iteration."""
    x = np.asarray(x_m, dtype=np.float64)
    y = np.asarray(y_m, dtype=np.float64)
    s0 = math.sin(math.radians(lat0))
    m = WGS84_A_M * (1.0 - WGS84_E2) / (1.0 - WGS84_E2 * s0 * s0) ** 1.5  # meridian radius
    n = WGS84_A_M / math.sqrt(1.0 - WGS84_E2 * s0 * s0)  # prime vertical radius
    k_lat = math.degrees(1.0 / m)
    k_lon = math.degrees(1.0 / (n * math.cos(math.radians(lat0))))
    lat = lat0 + y * k_lat
    lon = lon0 + x * k_lon
    for _ in range(6):
        ex, ny = to_local(lat, lon, lat0, lon0)
        lat = lat + (y - ny) * k_lat
        lon = lon + (x - ex) * k_lon
    return lat, lon


# --- SAE J1979 service 01 byte encoding for the PIDs this module produces ---------------------


def _byte(v: float) -> int:
    return min(max(round(v), 0), 255)


def _u16(v: float) -> bytes:
    raw = min(max(round(v), 0), 0xFFFF)
    return bytes((raw >> 8, raw & 0xFF))


def _encode_pid(pid: int, value: float) -> bytes:
    """Data bytes (A, B, ...) for an SI value. Inverse of ``_decode_pid``."""
    if pid == 0x03:
        return bytes((_byte(value), 0))
    if pid in (0x04, 0x11):
        return bytes((_byte(value * 255.0),))
    if pid in (0x05, 0x0F):
        return bytes((_byte(value - KELVIN_OFFSET + 40.0),))
    if pid in (0x06, 0x07, 0x08, 0x09):
        return bytes((_byte(value * 128.0 + 128.0),))
    if pid == 0x0C:
        return _u16(value * 4.0)
    if pid == 0x0D:
        return bytes((_byte(value * 3.6),))
    if pid == MAF_PID:
        return _u16(value * 1000.0 * 100.0)
    if pid == 0x42:
        return _u16(value * 1000.0)
    raise ValueError(f"no synthetic encoding for PID 0x{pid:02X}")


def _decode_pid(pid: int, data: bytes) -> float:
    """SI value of the data bytes: K, m/s, ratio, kg/s, V; 0x03 is the system 1 enum."""
    a = data[0]
    ab = (data[0] << 8) | data[1] if len(data) > 1 else a
    if pid == 0x03:
        return float(a)
    if pid in (0x04, 0x11):
        return a / 255.0
    if pid in (0x05, 0x0F):
        return a - 40.0 + KELVIN_OFFSET
    if pid in (0x06, 0x07, 0x08, 0x09):
        return a / 128.0 - 1.0
    if pid == 0x0C:
        return ab / 4.0
    if pid == 0x0D:
        return a / 3.6
    if pid == MAF_PID:
        return ab / 100.0 / 1000.0
    if pid == 0x42:
        return ab / 1000.0
    raise ValueError(f"no synthetic decoding for PID 0x{pid:02X}")


# --- poll schedule ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PollSchedule:
    """The OBD request order from ``config [obd]``, 1 request in flight.

    Each cycle of ``len(slots)`` requests polls every fast PID once, then fills the remaining
    slots round robin from the slow group. With the shipped config: 10 requests/s, cycle
    [0x0C, 0x0D, 0x11, slow, slow] every 0.5 s, so 2 Hz per fast PID and 4 Hz for the slow group.
    """

    period_ns: int
    fast: tuple[int, ...]
    slow: tuple[int, ...]
    slow_slots_per_cycle: int
    timeout_ns: int

    def pids(self, n: int) -> list[int]:
        out: list[int] = []
        k_slow = 0
        cycle = len(self.fast) + self.slow_slots_per_cycle
        for k in range(n):
            slot = k % cycle
            if slot < len(self.fast):
                out.append(self.fast[slot])
            else:
                out.append(self.slow[k_slow % len(self.slow)])
                k_slow += 1
        return out


def poll_schedule(cfg: Config, supported: frozenset[int] | None = None) -> PollSchedule:
    def pick(key: str) -> tuple[int, ...]:
        pids = tuple(int(p) for p in cfg.get(key))
        return pids if supported is None else tuple(p for p in pids if p in supported)

    fast, slow = pick("obd.fast_pids"), pick("obd.slow_pids")
    fast_hz = float(cfg.get("obd.fast_hz_each"))
    slow_hz = float(cfg.get("obd.slow_hz_total"))
    max_hz = float(cfg.get("obd.max_request_hz"))
    if not slow:
        slow_hz = 0.0
    total_hz = fast_hz * len(fast) + slow_hz
    if total_hz <= 0 or total_hz > max_hz + 1e-9:
        raise ConfigError(f"OBD schedule needs 0 < {total_hz} Hz <= max_request_hz {max_hz}")
    if fast:
        slow_slots = round(slow_hz / fast_hz)
        if abs(slow_slots * fast_hz - slow_hz) > 1e-9:
            raise ConfigError("obd.slow_hz_total must be a whole multiple of obd.fast_hz_each")
    else:
        slow_slots = 1
    timeout_ns = round(float(cfg.get("obd.response_timeout_s")) * NS_PER_S)
    return PollSchedule(round(NS_PER_S / total_hz), fast, slow, slow_slots, timeout_ns)


def supported_pids(cfg: Config, vehicle: Config | None) -> frozenset[int]:
    """Config PIDs this synthetic ECU answers. A vehicle profile without a MAF drops 0x10."""
    pids = {int(p) for p in cfg.get("obd.fast_pids")} | {int(p) for p in cfg.get("obd.slow_pids")}
    if vehicle is not None:
        try:
            has_maf = vehicle.get("vehicle.has_maf_sensor")
        except ConfigError:
            has_maf = True
        if has_maf is False:
            pids.discard(MAF_PID)
    return frozenset(pids)


# --- engine model -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Driveline:
    """A plausible 6-speed manual driveline. Assumed values, not taken from any data sheet."""

    gear_ratios: tuple[float, ...] = (4.06, 2.40, 1.58, 1.19, 1.00, 0.87)
    final_drive: float = 3.85
    tire_radius_m: float = 0.335
    idle_rpm: float = 750.0
    redline_rpm: float = 8300.0
    downshift_rpm: float = 1200.0

    def rpm_per_mps(self, gear: int) -> float:
        ratio = self.gear_ratios[gear - 1] * self.final_drive
        return ratio * 60.0 / (2.0 * math.pi * self.tire_radius_m)


class _EngineModel:
    """Engine and ECU values from speed and acceleration. Call ``advance`` once per request."""

    def __init__(self, rng: np.random.Generator, driveline: Driveline) -> None:
        self.rng = rng
        self.dl = driveline
        self.gear = 1
        self.rpm = driveline.idle_rpm
        self.throttle = 0.0
        self.ambient_c = float(rng.uniform(18.0, 28.0))
        self.ltft = (float(rng.uniform(-0.02, 0.02)), float(rng.uniform(-0.02, 0.02)))
        self.coolant_phase = float(rng.uniform(0.0, 2.0 * math.pi))

    def advance(self, v: float, a: float) -> None:
        dl = self.dl
        demand = min(max(a / (0.5 * G0), 0.0), 1.0)
        drag = 0.15 + 0.0004 * v * v  # m/s^2, rolling plus aero, assumed
        self.throttle = 0.0 if a < -0.3 else min(max((a + drag) / (0.55 * G0), 0.0), 1.0)
        if v < 0.5:
            self.gear = 1
            self.rpm = dl.idle_rpm
            return
        upshift = 2500.0 + 4000.0 * demand
        n = len(dl.gear_ratios)
        while self.gear < n and v * dl.rpm_per_mps(self.gear) > upshift:
            self.gear += 1
        while self.gear > 1 and v * dl.rpm_per_mps(self.gear) < dl.downshift_rpm:
            self.gear -= 1
        rpm = v * dl.rpm_per_mps(self.gear)
        if self.gear == 1 and demand > 0.3:
            rpm = max(rpm, 2000.0 + 2500.0 * demand)  # clutch slip on a launch
        self.rpm = min(max(rpm, dl.idle_rpm), dl.redline_rpm)

    def value(
        self, pid: int, t_s: float, v: float, a: float, coolant: CoolantProfile | None
    ) -> float:
        """SI value before J1979 quantization."""
        overrun = a < -0.3 and v > 3.0
        if pid == 0x0C:
            return self.rpm
        if pid == 0x0D:
            return max(v, 0.0)
        if pid == 0x11:
            return self.throttle
        if pid == 0x04:
            load = 0.08 if overrun else 0.2 + 0.8 * self.throttle
            return min(max(load + float(self.rng.normal(0.0, 0.01)), 0.0), 1.0)
        if pid == 0x03:
            return float(
                FUEL_OPEN_LOOP_LOAD if overrun or self.throttle > 0.8 else FUEL_CLOSED_LOOP
            )
        if pid == 0x05:
            c = (
                coolant(t_s)
                if coolant is not None
                else 90.0 + 1.0 * math.sin(2.0 * math.pi * t_s / 300.0 + self.coolant_phase)
            )
            return c + KELVIN_OFFSET
        if pid == 0x0F:
            soak = 10.0 * math.exp(-max(v, 0.0) / 8.0)
            return self.ambient_c + soak + KELVIN_OFFSET
        if pid in (0x06, 0x08):
            return float(np.clip(self.rng.normal(0.0, 0.008), -0.027, 0.027))
        if pid in (0x07, 0x09):
            return self.ltft[0 if pid == 0x07 else 1]
        if pid == 0x42:
            return float(np.clip(14.1 + self.rng.normal(0.0, 0.05), 13.9, 14.3))
        if pid == MAF_PID:
            # 4.0 L, 4-stroke: air per second = rpm / 120 * displacement * density * fill.
            return self.rpm / 120.0 * 0.004 * 1.184 * (0.2 + 0.8 * self.throttle)
        raise ValueError(f"no synthetic model for PID 0x{pid:02X}")


# --- speed traces -------------------------------------------------------------------------------


@dataclass(frozen=True)
class SpeedTrace:
    """A speed trace to derive OBD values from. ``accel_mps2`` is derived when omitted."""

    t_ns: I64
    speed_mps: F64
    accel_mps2: F64 | None = None
    name: str = "trace"
    dataset_ref: str | None = None


@runtime_checkable
class HasSpeedTrace(Protocol):
    def speed_trace(self) -> tuple[I64, F64]: ...


SPEED_SAMPLE_SIGNALS: Final = ("can.wheel_speed", "obd.speed")


def trace_from_source(base: Source) -> SpeedTrace:
    """Speed from ``base.speed_trace()`` when offered, else its GPS fixes, else speed samples."""
    if isinstance(base, HasSpeedTrace):
        t, v = base.speed_trace()
        return SpeedTrace(
            np.asarray(t, np.int64),
            np.asarray(v, np.float64),
            None,
            base.meta.name,
            base.meta.dataset_ref,
        )
    gps_t: list[int] = []
    gps_v: list[float] = []
    smp_t: list[int] = []
    smp_v: list[float] = []
    for r in base.records():
        if isinstance(r, GpsFix) and math.isfinite(r.speed_mps):
            gps_t.append(r.t_ns)
            gps_v.append(r.speed_mps)
        elif isinstance(r, Sample) and r.signal in SPEED_SAMPLE_SIGNALS:
            smp_t.append(r.t_ns)
            smp_v.append(r.value)
    t_list, v_list = (gps_t, gps_v) if gps_t else (smp_t, smp_v)
    if len(t_list) < 2:
        raise ValueError(f"source {base.meta.name} has no speed to derive OBD values from")
    return SpeedTrace(
        np.asarray(t_list, np.int64),
        np.asarray(v_list, np.float64),
        None,
        base.meta.name,
        base.meta.dataset_ref,
    )


def _accel_from_speed(t_ns: I64, v: F64, window_s: float = 1.0) -> tuple[F64, F64]:
    """(grid seconds, accel) on a uniform 20 Hz grid, smoothed by a centered moving average."""
    t = (t_ns - t_ns[0]).astype(np.float64) / NS_PER_S
    dt = 0.05
    grid = np.arange(0.0, t[-1] + dt / 2, dt)
    vg = np.interp(grid, t, v)
    acc = np.gradient(vg, dt) if len(grid) > 1 else np.zeros_like(grid)
    k = max(1, round(window_s / dt) | 1)
    kernel = np.ones(k)
    num = np.convolve(acc, kernel, mode="same")
    den = np.convolve(np.ones_like(acc), kernel, mode="same")
    return grid, num / den


# --- the OBD overlay ----------------------------------------------------------------------------


def obd_signal_names(pids: Sequence[int]) -> tuple[str, ...]:
    return tuple(sorted(OBD_PID_TO_SIGNAL[p] for p in pids))


class SyntheticObdSource:
    """Service 01 ``ObdPoll`` and ``Sample`` records consistent with a drive's speed and accel.

    ``base`` is a ``Source`` (its ``speed_trace()`` if it has one, else its GPS speed) or a
    ``SpeedTrace``. ``ObdPoll.raw`` holds the data bytes (A, B, ...) after the service and PID
    bytes, so decoding ``raw`` gives back ``value``. Every poll succeeds (status "ok") with a
    12 to 40 ms response time. ``coolant_c`` overrides the coolant model: seconds since the trace
    start to °C.
    """

    def __init__(
        self,
        base: Source | SpeedTrace,
        seed: int,
        *,
        cfg: Config | None = None,
        vehicle: Config | None = None,
        coolant_c: CoolantProfile | None = None,
        driveline: Driveline | None = None,
    ) -> None:
        self._base = base
        self.seed = seed
        self._cfg = cfg or load_config()
        veh = vehicle
        if veh is None:
            veh = load_vehicle(str(self._cfg.get("vehicle.profile")))
        self.schedule = poll_schedule(self._cfg, supported_pids(self._cfg, veh))
        self.pids = tuple(sorted(set(self.schedule.fast) | set(self.schedule.slow)))
        self._coolant = coolant_c
        self._driveline = driveline or Driveline()
        if isinstance(base, SpeedTrace):
            name, ref = base.name, base.dataset_ref
        else:
            name, ref = base.meta.name, base.meta.dataset_ref
        self._meta = SourceMeta(
            name=f"synthetic-obd:{name}",
            kind="synthetic",
            dataset_ref=ref,
            synthetic_signals=obd_signal_names(self.pids),
        )

    @property
    def meta(self) -> SourceMeta:
        return self._meta

    def records(self) -> Iterator[ObdPoll | Sample]:
        trace = self._base if isinstance(self._base, SpeedTrace) else trace_from_source(self._base)
        t_ns = np.asarray(trace.t_ns, dtype=np.int64)
        v = np.asarray(trace.speed_mps, dtype=np.float64)
        if len(t_ns) < 2:
            raise ValueError("a speed trace needs at least 2 points")
        order = np.argsort(t_ns, kind="stable")
        t_ns, v = t_ns[order], v[order]
        t_rel = (t_ns - t_ns[0]).astype(np.float64) / NS_PER_S
        if trace.accel_mps2 is not None:
            a_t, a_v = t_rel, np.asarray(trace.accel_mps2, dtype=np.float64)[order]
        else:
            a_t, a_v = _accel_from_speed(t_ns, v)

        rng = np.random.default_rng(self.seed)
        model = _EngineModel(rng, self._driveline)
        sched = self.schedule
        span_ns = int(t_ns[-1] - t_ns[0])
        n_req = span_ns // sched.period_ns
        max_latency_ns = min(40_000_000, sched.timeout_ns - 1)
        latency = rng.integers(12_000_000, max_latency_ns, size=n_req, endpoint=True)
        for k, pid in enumerate(sched.pids(n_req)):
            req = int(t_ns[0]) + k * sched.period_ns
            resp = req + int(latency[k])
            ts = (resp - int(t_ns[0])) / NS_PER_S
            vk = float(np.interp(ts, t_rel, v))
            ak = float(np.interp(ts, a_t, a_v))
            model.advance(vk, ak)
            raw = _encode_pid(pid, model.value(pid, ts, vk, ak, self._coolant))
            value = _decode_pid(pid, raw)
            signal = OBD_PID_TO_SIGNAL[pid]
            yield ObdPoll(
                req_t_ns=req,
                resp_t_ns=resp,
                pid=pid,
                ecu=OBD_ECU,
                status="ok",
                raw=raw,
                value=value,
                unit=SIGNALS[signal].unit,
            )
            yield Sample(t_ns=resp, signal=signal, value=value)


# --- scripted drives ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Phase:
    """A constant-acceleration piece of a scripted drive. Times are seconds from drive start."""

    name: str
    t_start_s: float
    duration_s: float
    v_start_mps: float
    accel_mps2: float
    s_start_m: float

    @property
    def t_end_s(self) -> float:
        return self.t_start_s + self.duration_s

    @property
    def v_end_mps(self) -> float:
        return self.v_start_mps + self.accel_mps2 * self.duration_s

    @property
    def length_m(self) -> float:
        return self.v_start_mps * self.duration_s + 0.5 * self.accel_mps2 * self.duration_s**2


@dataclass(frozen=True, slots=True)
class PathSegment:
    """A straight (curvature 0) or a circular arc (curvature 1/R, positive turning left)."""

    s_start_m: float
    length_m: float
    curvature: float
    x0: float
    y0: float
    heading0: float

    def end_pose(self) -> tuple[float, float, float]:
        h0, k, d = self.heading0, self.curvature, self.length_m
        if k == 0.0:
            return self.x0 + d * math.cos(h0), self.y0 + d * math.sin(h0), h0
        h1 = h0 + k * d
        return (
            self.x0 + (math.sin(h1) - math.sin(h0)) / k,
            self.y0 - (math.cos(h1) - math.cos(h0)) / k,
            h1,
        )


@dataclass(frozen=True, slots=True)
class StartFinishLineM:
    """A start and finish line in the drive's local frame (meters, x east, y north)."""

    line_id: str
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass(frozen=True, slots=True)
class Truth:
    """Exact kinematics at the requested times (vehicle frame accelerations, heading from +x)."""

    x_m: F64
    y_m: F64
    heading_rad: F64
    speed_mps: F64
    accel_long_mps2: F64
    accel_lat_mps2: F64
    yaw_rate_rps: F64


@dataclass
class _PlanBuilder:
    phases: list[Phase] = field(default_factory=list)
    segments: list[PathSegment] = field(default_factory=list)
    t: float = 0.0
    v: float = 0.0
    s: float = 0.0
    pose: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def _add(self, name: str, duration: float, accel: float, curvature: float = 0.0) -> None:
        if duration <= 0.0:
            raise ValueError(f"phase {name} needs a positive duration")
        ph = Phase(name, self.t, duration, self.v, accel, self.s)
        self.phases.append(ph)
        length = ph.length_m
        if length > 0.0:
            x, y, h = self.pose
            seg = PathSegment(self.s, length, curvature, x, y, h)
            self.segments.append(seg)
            self.pose = seg.end_pose()
        self.t += duration
        self.s += length
        self.v = ph.v_end_mps

    def rest(self, duration: float, name: str = "rest") -> None:
        if self.v != 0.0:
            raise ValueError("rest needs the car stopped")
        self._add(name, duration, 0.0)

    def speed_to(self, v_target: float, accel_mag: float, name: str) -> None:
        dv = v_target - self.v
        a = math.copysign(accel_mag, dv)
        self._add(name, abs(dv) / accel_mag, a)
        self.v = v_target

    def cruise(self, duration: float, name: str = "cruise") -> None:
        self._add(name, duration, 0.0)

    def straight(self, distance: float, name: str = "straight") -> None:
        self._add(name, distance / self.v, 0.0)

    def arc(self, radius: float, angle_rad: float, name: str = "arc") -> None:
        self._add(
            name, radius * abs(angle_rad) / self.v, 0.0, math.copysign(1.0 / radius, angle_rad)
        )


@dataclass(frozen=True, slots=True)
class _Plan:
    phases: tuple[Phase, ...]
    segments: tuple[PathSegment, ...]
    start_finish: StartFinishLineM | None = None
    track_length_m: float | None = None
    lap_speed_mps: float | None = None
    coolant_excursion: tuple[float, float, float] | None = None


# track_day constants (the scripted kinematics; the known answers follow from them).
LAUNCH_G: Final = 0.5
BRAKE_G: Final = 1.0
TOP_SPEED_MPS: Final = 70.0 * MPH
LAP_SPEED_MPS: Final = 25.0
OVAL_STRAIGHT_M: Final = 300.0
OVAL_RADIUS_M: Final = 100.0
LINE_HALF_WIDTH_M: Final = 20.0
LAP_COUNT: Final = 3
COOLANT_PEAK_C: Final = 106.0
COOLANT_HOLD_S: Final = 20.0
COOLANT_DELAY_S: Final = 20.0


def _track_day_plan() -> _Plan:
    b = _PlanBuilder()
    b.rest(5.0)
    b.speed_to(TOP_SPEED_MPS, LAUNCH_G * G0, "launch")
    b.cruise(4.0)
    b.speed_to(0.0, BRAKE_G * G0, "brake")
    b.rest(3.0)
    b.speed_to(LAP_SPEED_MPS, 0.3 * G0, "pull_away")
    entry_x, entry_y, _ = b.pose
    # Out lap to the line in the middle of the top straight.
    b.straight(OVAL_STRAIGHT_M, "out_lap")
    b.arc(OVAL_RADIUS_M, math.pi, "out_lap")
    b.straight(OVAL_STRAIGHT_M / 2, "out_lap")
    first_crossing_s = b.t
    for lap in range(1, LAP_COUNT + 1):
        name = f"lap_{lap}"
        b.straight(OVAL_STRAIGHT_M / 2, name)
        b.arc(OVAL_RADIUS_M, math.pi, name)
        b.straight(OVAL_STRAIGHT_M, name)
        b.arc(OVAL_RADIUS_M, math.pi, name)
        b.straight(OVAL_STRAIGHT_M / 2, name)
    b.speed_to(0.0, 0.5 * G0, "stop")
    b.rest(5.0, "rest_end")
    line_x = entry_x + OVAL_STRAIGHT_M / 2
    line_y = entry_y + 2 * OVAL_RADIUS_M
    line = StartFinishLineM(
        "sf", line_x, line_y - LINE_HALF_WIDTH_M, line_x, line_y + LINE_HALF_WIDTH_M
    )
    t_hot = first_crossing_s + COOLANT_DELAY_S
    return _Plan(
        tuple(b.phases),
        tuple(b.segments),
        start_finish=line,
        track_length_m=2 * OVAL_STRAIGHT_M + 2 * math.pi * OVAL_RADIUS_M,
        lap_speed_mps=LAP_SPEED_MPS,
        coolant_excursion=(t_hot, t_hot + COOLANT_HOLD_S, COOLANT_PEAK_C),
    )


def _commute_plan(rng: np.random.Generator) -> _Plan:
    b = _PlanBuilder()
    b.rest(5.0)
    b.speed_to(float(rng.uniform(13.0, 16.0)), 0.20 * G0, "pull_away")
    b.cruise(float(rng.uniform(15.0, 25.0)))
    b.speed_to(6.0, 0.20 * G0, "slow_for_turn")
    b.arc(15.0, math.pi / 2, "left_turn")
    b.speed_to(float(rng.uniform(17.0, 20.0)), 0.18 * G0, "accelerate")
    b.cruise(float(rng.uniform(25.0, 40.0)))
    b.speed_to(0.0, 0.25 * G0, "stop_at_light")
    b.rest(float(rng.uniform(6.0, 12.0)), "wait_at_light")
    b.speed_to(float(rng.uniform(22.0, 27.0)), 0.22 * G0, "accelerate")
    b.arc(250.0, -math.pi / 6, "bend")
    b.cruise(float(rng.uniform(30.0, 45.0)))
    b.speed_to(6.0, 0.20 * G0, "slow_for_turn")
    b.arc(12.0, -math.pi / 2, "right_turn")
    b.speed_to(11.0, 0.15 * G0, "accelerate")
    b.cruise(10.0)
    b.speed_to(0.0, 0.20 * G0, "park")
    b.rest(5.0, "rest_end")
    return _Plan(tuple(b.phases), tuple(b.segments))


class SyntheticDriveSource:
    """A scripted drive: ``GpsFix`` at 10 Hz, ``ImuSample`` at 104 Hz, OBD on the schedule."""

    def __init__(
        self,
        plan: _Plan,
        *,
        seed: int,
        scenario: str,
        cfg: Config | None = None,
        vehicle: Config | None = None,
        t0_ns: int = T0_NS,
    ) -> None:
        self.seed = seed
        self.scenario = scenario
        self.t0_ns = t0_ns
        self.origin_deg = (ORIGIN_LAT_DEG, ORIGIN_LON_DEG)
        self.phases = plan.phases
        self.segments = plan.segments
        self.start_finish = plan.start_finish
        self.track_length_m = plan.track_length_m
        self.lap_speed_mps = plan.lap_speed_mps
        self.coolant_excursion = plan.coolant_excursion
        last = plan.phases[-1]
        self.duration_s = last.t_end_s
        self.t_end_ns = t0_ns + round(self.duration_s * NS_PER_S)
        self._ph_t0 = np.array([p.t_start_s for p in plan.phases])
        self._ph_dur = np.array([p.duration_s for p in plan.phases])
        self._ph_v0 = np.array([p.v_start_mps for p in plan.phases])
        self._ph_a = np.array([p.accel_mps2 for p in plan.phases])
        self._ph_s0 = np.array([p.s_start_m for p in plan.phases])
        self._sg_s0 = np.array([g.s_start_m for g in plan.segments])
        self._sg_len = np.array([g.length_m for g in plan.segments])
        self._sg_k = np.array([g.curvature for g in plan.segments])
        self._sg_x0 = np.array([g.x0 for g in plan.segments])
        self._sg_y0 = np.array([g.y0 for g in plan.segments])
        self._sg_h0 = np.array([g.heading0 for g in plan.segments])

        grid_ns = self._grid_ns(TRACE_HZ)
        tr = self.truth(grid_ns)
        coolant = self._coolant_profile()
        self.obd = SyntheticObdSource(
            SpeedTrace(grid_ns, tr.speed_mps, tr.accel_long_mps2, f"synthetic:{scenario}"),
            seed,
            cfg=cfg,
            vehicle=vehicle,
            coolant_c=coolant,
        )
        # The fixes themselves too: "gps.fix" feeds the timeline positions.
        gps_imu = (*(n for n, s in SIGNALS.items() if s.source in ("gps", "imu")), GPS_FIX)
        self._meta = SourceMeta(
            name=f"synthetic:{scenario}:{seed}",
            kind="synthetic",
            dataset_ref=f"synthetic:{scenario}:seed={seed}",
            synthetic_signals=tuple(sorted(set(self.obd.meta.synthetic_signals) | set(gps_imu))),
        )

    @property
    def meta(self) -> SourceMeta:
        return self._meta

    def phase(self, name: str) -> Phase:
        """The first phase with this name."""
        for p in self.phases:
            if p.name == name:
                return p
        raise KeyError(name)

    def _coolant_profile(self) -> CoolantProfile | None:
        exc = self.coolant_excursion
        if exc is None:
            return None
        hot_start, hot_end, peak = exc

        def coolant(t_s: float) -> float:
            if hot_start <= t_s < hot_end:
                return peak
            if hot_start - 10.0 <= t_s < hot_start:
                return 90.0 + 14.0 * (t_s - (hot_start - 10.0)) / 10.0  # warms to 104 °C
            if hot_end <= t_s < hot_end + 20.0:
                return 101.0 - 11.0 * (t_s - hot_end) / 20.0  # cools from 101 °C
            return 90.0

        return coolant

    def _grid_ns(self, hz: float) -> I64:
        n = math.floor(self.duration_s * hz + 1e-9) + 1
        return self.t0_ns + np.rint(np.arange(n) * (NS_PER_S / hz)).astype(np.int64)

    def truth(self, t_ns: I64) -> Truth:
        t = (np.asarray(t_ns, dtype=np.int64) - self.t0_ns).astype(np.float64) / NS_PER_S
        i = np.clip(np.searchsorted(self._ph_t0, t, side="right") - 1, 0, len(self._ph_t0) - 1)
        dt = np.clip(t - self._ph_t0[i], 0.0, self._ph_dur[i])
        active = (t >= self._ph_t0[i]) & (t < self._ph_t0[i] + self._ph_dur[i])
        a = np.where(active, self._ph_a[i], 0.0)
        v = np.maximum(self._ph_v0[i] + self._ph_a[i] * dt, 0.0)
        s = self._ph_s0[i] + self._ph_v0[i] * dt + 0.5 * self._ph_a[i] * dt**2
        j = np.clip(np.searchsorted(self._sg_s0, s, side="right") - 1, 0, len(self._sg_s0) - 1)
        ds = np.clip(s - self._sg_s0[j], 0.0, self._sg_len[j])
        k = self._sg_k[j]
        h0 = self._sg_h0[j]
        straight = k == 0.0
        k_safe = np.where(straight, 1.0, k)
        h = h0 + k * ds
        x = np.where(
            straight,
            self._sg_x0[j] + ds * np.cos(h0),
            self._sg_x0[j] + (np.sin(h) - np.sin(h0)) / k_safe,
        )
        y = np.where(
            straight,
            self._sg_y0[j] + ds * np.sin(h0),
            self._sg_y0[j] - (np.cos(h) - np.cos(h0)) / k_safe,
        )
        return Truth(x, y, h, v, a, v * v * k, v * k)

    def records(self) -> Iterator[Record]:
        streams: list[Iterator[Record]] = [self._gps(), self._imu(), self.obd.records()]
        yield from heapq.merge(*streams, key=record_time)

    def _gps(self) -> Iterator[GpsFix]:
        rng = np.random.default_rng([self.seed, 1])
        t_ns = self._grid_ns(GPS_HZ)
        tr = self.truth(t_ns)
        n = len(t_ns)
        noise = rng.normal(0.0, GPS_POS_NOISE_M, (n, 2))
        noise[0] = 0.0  # the first fix is the origin of any "first kept fix" local frame
        x = tr.x_m + noise[:, 0]
        y = tr.y_m + noise[:, 1]
        speed = np.abs(tr.speed_mps + rng.normal(0.0, GPS_SPEED_NOISE, n))
        course = np.mod(math.pi / 2 - tr.heading_rad, 2 * math.pi)
        lat, lon = to_latlon(x, y)
        for t, la, lo, sp, co in zip(
            t_ns.tolist(), lat.tolist(), lon.tolist(), speed.tolist(), course.tolist(), strict=True
        ):
            yield GpsFix(
                t_ns=t,
                rx_t_ns=t,
                utc_ns=None,
                lat_deg=la,
                lon_deg=lo,
                alt_m=GPS_ALT_M,
                speed_mps=sp,
                course_rad=co,
                hdop=GPS_HDOP,
                fix_type=3,
                n_sats=GPS_SATS,
            )

    def _imu(self) -> Iterator[ImuSample]:
        rng = np.random.default_rng([self.seed, 2])
        t_ns = self._grid_ns(IMU_HZ)
        tr = self.truth(t_ns)
        n = len(t_ns)
        acc = rng.normal(0.0, IMU_ACCEL_NOISE, (n, 3))
        gyr = rng.normal(0.0, IMU_GYRO_NOISE, (n, 3))
        acc[:, 0] += tr.accel_long_mps2
        acc[:, 1] += tr.accel_lat_mps2
        acc[:, 2] += G0
        gyr[:, 2] += tr.yaw_rate_rps
        for t, a, g in zip(t_ns.tolist(), acc.tolist(), gyr.tolist(), strict=True):
            yield ImuSample(t_ns=t, ax=a[0], ay=a[1], az=a[2], gx=g[0], gy=g[1], gz=g[2])


def synthetic_drive(
    seed: int,
    scenario: str,
    *,
    cfg: Config | None = None,
    vehicle: Config | None = None,
) -> SyntheticDriveSource:
    """A scripted drive for ``scenario`` ("track_day" or "commute"), deterministic for a seed."""
    if scenario == "track_day":
        plan = _track_day_plan()
    elif scenario == "commute":
        plan = _commute_plan(np.random.default_rng([seed, 0]))
    else:
        raise ValueError(f"unknown scenario {scenario!r}; expected one of {SCENARIOS}")
    return SyntheticDriveSource(plan, seed=seed, scenario=scenario, cfg=cfg, vehicle=vehicle)
