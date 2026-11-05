"""Record types that flow through the pipeline.

Every record carries ``t_ns``: CLOCK_MONOTONIC nanoseconds on the device, or dataset
boot-time nanoseconds in replay. All physical values are SI (m, s, m/s, m/s^2, rad, rad/s,
K, V, kg/s). Ratios are 0..1 (throttle, load) or signed fractions (fuel trims: +0.10 = +10%).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ObdStatus = Literal["ok", "timeout", "nrc"]
CitationRole = Literal["trigger", "context"]
SourceKind = Literal["can", "obd", "gps", "imu", "derived"]


@dataclass(frozen=True, slots=True)
class CanFrame:
    t_ns: int
    bus: int
    arb_id: int
    is_ext: bool
    dlc: int
    data: bytes
    is_tx: bool = False


@dataclass(frozen=True, slots=True)
class ObdPoll:
    """One service 01 request and its reply (or timeout)."""

    req_t_ns: int
    resp_t_ns: int | None
    pid: int
    ecu: int | None
    status: ObdStatus
    raw: bytes
    value: float | None
    unit: str
    service: int = 1

    @property
    def t_ns(self) -> int:
        return self.resp_t_ns if self.resp_t_ns is not None else self.req_t_ns


@dataclass(frozen=True, slots=True)
class GpsFix:
    t_ns: int
    rx_t_ns: int
    utc_ns: int | None
    lat_deg: float
    lon_deg: float
    alt_m: float
    speed_mps: float
    course_rad: float
    hdop: float
    fix_type: int
    n_sats: int


@dataclass(frozen=True, slots=True)
class ImuSample:
    """Raw sensor-frame IMU sample. Alignment to the vehicle frame happens in estimate/."""

    t_ns: int
    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float
    temp_k: float | None = None


@dataclass(frozen=True, slots=True)
class Sample:
    """A decoded scalar signal value, named by the signal catalog (e.g. "obd.coolant_temp")."""

    t_ns: int
    signal: str
    value: float


@dataclass(frozen=True, slots=True)
class StateEstimate:
    """Vehicle-frame state: x forward, y left, z up."""

    t_ns: int
    speed_mps: float
    accel_long_mps2: float
    accel_lat_mps2: float
    accel_lat_imu_mps2: float
    yaw_rate_rps: float
    var_speed: float
    var_accel: float
    inputs: str
    dr: bool = False


@dataclass(frozen=True, slots=True)
class Citation:
    """Evidence for an event: which signal, over which window, and what it showed (rule 8)."""

    signal: str
    source: str
    role: CitationRole
    t_start_ns: int
    t_end_ns: int
    stat: str
    stat_value: float
    unit: str
    n: int


@dataclass(frozen=True, slots=True)
class Event:
    """An alert, a performance run, or a driver-behavior event.

    ``kind`` is dotted: "alert.coolant_high", "alert.baseline", "perf.zero_to_sixty",
    "perf.sixty_to_zero", "perf.peak_lateral", "behavior.hard_brake", "behavior.hard_accel",
    "behavior.corner", "behavior.shift", "lap". ``severity`` is the caution-mark count 1..3
    (0 for non-alert events such as performance runs).
    """

    kind: str
    rule_id: str
    rule_version: int
    severity: int
    t_start_ns: int
    t_end_ns: int
    value: float
    threshold: float | None
    unit: str
    message: str
    citations: tuple[Citation, ...] = field(default_factory=tuple)
    resolved: bool = False
    synthetic: bool = False

    def __post_init__(self) -> None:
        if self.t_end_ns <= self.t_start_ns:
            raise ValueError("event window must have t_end_ns > t_start_ns")
        if self.kind.startswith("alert.") and not any(c.role == "trigger" for c in self.citations):
            raise ValueError("an alert needs at least 1 trigger citation (hard rule 8)")
        if not 0 <= self.severity <= 3:
            raise ValueError("severity must be 0..3")


@dataclass(frozen=True, slots=True)
class Lap:
    line_id: str
    lap_no: int
    t_start_ns: int
    t_end_ns: int
    duration_ns: int
    valid: bool


@dataclass(frozen=True, slots=True)
class DriveStat:
    """A computed drive-level stat. The model summary sees only these (build item 11).

    ``display`` is the exact string the UI shows and the summary must quote, e.g. "0.46 g".
    """

    stat_id: str
    label: str
    value: float
    unit: str
    display: str


Record = CanFrame | ObdPoll | GpsFix | ImuSample | Sample | StateEstimate | Event | Lap


def record_time(r: Record) -> int:
    """The time a record sorts by on the single timeline."""
    if isinstance(r, Event | Lap):
        return r.t_start_ns
    return r.t_ns
