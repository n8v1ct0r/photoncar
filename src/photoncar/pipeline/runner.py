"""Run 1 drive through the pipeline: sources in, a closed drive file and an index entry out.

Streaming (every record, in time order from the merge described below):

1. Every source record is written to the drive file.
2. ``CanFrame``: decoded through the ``DbcDecoder`` when one is given; decoded ``Sample``s are
   written and handled like source samples.
3. ``ImuSample`` and ``GpsFix`` go to the ``StateEstimator``; each ``StateEstimate`` is written.
   Speed samples (``obd.speed``, ``can.wheel_speed``) go to it as well, except synthetic ones on
   a drive with measured data: a synthetic overlay never steers the estimate of a real drive.
   Without an IMU (a live OBD session) the estimator runs speed-only: 1 estimate per speed
   measurement, with no lateral values.
4. Every ``Sample`` goes to the ``RuleEngine``; each alert it emits is written.
5. With ``live``, a ``LiveFrame`` is published every ``1 / [live] stream_hz`` seconds of record
   time, carrying the highest-severity active alert (``RuleEngine.active``) and the frame fields
   fed by synthetic signals (``synthetic_fields``).

Live sources merge through ``merge_live`` on ``live_clock`` (the monotonic clock they stamp
records on): a source that goes quiet, such as a GPS without a fix, never stops the timeline for
longer than the reorder watermark. A record that still arrives out of order is stored but not
analysed, and counted as a ``late_record`` error. Replay and synthetic sources use
``merge_sources``.

The moving lock (live sources only; replay is never gated): ``moving`` is set true when the run
starts, before the first record, so a quiet bus keeps the parked views locked. It turns on
when the estimated speed reaches ``behavior.min_speed_mps`` (2 m/s), and turns off only after
the speed stays under ``performance.start_speed_mps`` (0.3 m/s) for ``estimator.stationary_s``
(2 s). With no speed estimate for ``estimator.max_dead_reckon_s`` (5 s), the device counts as
moving, so a lost speed source keeps the parked views locked. That check runs twice: on record
time at each record, and on a watchdog thread on ``live_clock`` every ``1 / [live] stream_hz``,
so it holds even when no record arrives at all. All values come from config.

IMU mount rotation:

- Replay and synthetic sources: a pre-pass reads the IMU samples and a reference speed (a
  source's ``speed_trace()``, else GPS speed, else wheel or OBD speed) and calls
  ``estimate_mount_rotation`` before streaming. Only measured sources feed it when the drive
  has any; a drive without a rest period or straight-line speed change keeps the identity
  rotation, and the report says why.
- Live sources cannot look ahead. With ``mount_rot`` given, live mode uses it from the start.
  Without it, the rotation is unknown, and an unknown rotation never steers the estimate: the
  IMU is recorded but not given to the estimator, which runs on speed alone. Every
  ``estimator.stationary_s`` of record time the run tries ``estimate_mount_rotation`` on the
  drive's own measured data so far (the last ``ALIGN_WINDOW_S``). From the first success the
  IMU feeds the estimator, starting at the next speed measurement so the speed it carries on
  from is fresh; a drive that ends first gets 1 last try. The drive stores the rotation it
  found (none if it found none), and the report says when it was found, or why not. Replaying
  the drive file later (``RecordedDriveSource`` is a replay source) runs the pre-pass on the
  whole drive.

At close: open rule episodes, performance runs (0 to 60, 60 to 0, peak lateral), driver
behavior, laps (1 ``LapTimer`` per line, on GPS positions in local meters from the first kept
fix), the baseline check against the index and then the baseline update (alerted windows left
out), the 10 Hz timeline, drive stats, ``writer.close()``, and the index entry.

Events are marked ``synthetic`` when the drive is synthetic or a trigger citation cites a
synthetic signal. A synthetic alert's message ends with "Synthetic fault." so the label shows
wherever the message does. On a synthetic drive the pipeline's own outputs (``state.*``) are
listed as synthetic signals too, since every input they come from is.

Live drives: the drive enters the index when it starts, with its wall-clock UTC start (the
device has an RTC), and gets its UTC end when it closes. The hours since the previous live
drive ended (``DriveIndex.rested_h``) go to the rule engine for the rested-voltage advisory;
unknown stays None, so the advisory stays off.

The drive's stored source is where its data came from (``drive_origin``): a stored drive
re-run through ``RecordedDriveSource`` keeps the source of that drive. A live drive re-analysed
is still a live drive, so it stays out of the demo data and the portfolio embed.
"""

from __future__ import annotations

import hashlib
import heapq
import math
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import chain
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from photoncar.api.live import LiveState, active_alert, alert_json, baseline_sustain_ns
from photoncar.clock import Clock, MonotonicClock
from photoncar.config import Config
from photoncar.decode.dbc import VENDOR_DIR, DbcDecoder
from photoncar.detect import baseline as bl
from photoncar.detect.behavior import BehaviorReport, analyze
from photoncar.detect.laps import LapTimer, StartFinishLine
from photoncar.detect.performance import peak_lateral, sixty_to_zero, zero_to_sixty
from photoncar.detect.rules import CLOSED_LOOP, RuleEngine
from photoncar.estimate.alignment import AlignmentError, ImuArrays, estimate_mount_rotation
from photoncar.estimate.kalman import OBD_SPEED, WHEEL_SPEED, StateEstimator
from photoncar.pipeline.merge import merge_live, merge_sources
from photoncar.pipeline.stats import DriveTotals, drive_stats
from photoncar.privacy.geo import enu_m
from photoncar.records import (
    CanFrame,
    Citation,
    DriveStat,
    Event,
    GpsFix,
    ImuSample,
    Lap,
    ObdPoll,
    Record,
    Sample,
    StateEstimate,
    record_time,
)
from photoncar.sources.base import Source, SourceMeta
from photoncar.store.index import DriveIndex, drive_summary
from photoncar.store.meta import DriveMeta, DriveSource, Matrix3, new_drive_id
from photoncar.store.reader import DriveReader
from photoncar.store.rollup import (
    CAN_MAX_GAP_NS,
    COOLANT_SOURCES,
    FAST_OBD_MAX_GAP_NS,
    MIN_KEPT_FIX_TYPE,
    RPM_SOURCES,
    SLOW_OBD_MAX_GAP_NS,
    interp_gap,
    ranked_sources,
)
from photoncar.store.writer import DriveWriter
from photoncar.summary.limits import WallClock
from photoncar.units import KELVIN_OFFSET, MPH, NS_PER_S

I64 = NDArray[np.int64]
F64 = NDArray[np.float64]

SYNTHETIC_FAULT = "Synthetic fault."
GEAR_FROM_SIGNAL = "behavior.gear.from"
GEAR_TO_SIGNAL = "behavior.gear.to"
GEAR_SOURCE = "Speed and rpm"
FUEL_SYSTEM = "obd.fuel_system"
TRIMS = frozenset({"obd.stft_b1", "obd.ltft_b1", "obd.stft_b2", "obd.ltft_b2"})
ANALYSIS_RPM = ("can.engine_rpm", "obd.rpm")
"""RPM for behavior and baseline bins, in order of preference (measured signals first)."""
ANALYSIS_COOLANT = ("can.coolant_temp", "obd.coolant_temp")
SPEED = "state.speed"
DERIVED_SIGNALS = ("state.speed", "state.accel_long", "state.accel_lat")
"""The pipeline's own outputs: synthetic on a synthetic drive, since all their inputs are."""
BASELINE_SIGNALS = (
    "obd.coolant_temp",
    "obd.stft_b1",
    "obd.ltft_b1",
    "obd.stft_b2",
    "obd.ltft_b2",
    "obd.module_voltage",
    "obd.rpm",
    "can.engine_rpm",
    "can.coolant_temp",
)
"""Signals that keep a per-car baseline. Only measured samples teach it, binned by measured rpm
and coolant; synthetic values are unknown to it, so a bin that needs them is excluded."""
MAX_GAP_NS: dict[str, int] = {
    "obd.rpm": FAST_OBD_MAX_GAP_NS,
    "obd.speed": FAST_OBD_MAX_GAP_NS,
    "obd.coolant_temp": SLOW_OBD_MAX_GAP_NS,
    FUEL_SYSTEM: SLOW_OBD_MAX_GAP_NS,
    "can.engine_rpm": CAN_MAX_GAP_NS,
    "can.coolant_temp": CAN_MAX_GAP_NS,
}
"""Longest gap a value is carried across (3 nominal periods, the approved stale rule)."""
SPEED_MAX_GAP_NS = CAN_MAX_GAP_NS
"""Estimated speed at a sample time: interpolated across gaps up to 1.5 s."""
ALIGN_WINDOW_S = 120.0
"""Live mount calibration looks at the last 120 s of the drive (proposed): enough for a stop
and the launch after it, and a bound on what a drive without either keeps in memory."""


class PipelineError(RuntimeError):
    """The sources broke the pipeline contract."""


# -- sources ------------------------------------------------------------------------------------


@runtime_checkable
class HasImuSamples(Protocol):
    def imu_samples(self) -> Iterator[ImuSample]: ...


@runtime_checkable
class HasSpeedTrace(Protocol):
    def speed_trace(self) -> tuple[I64, F64]: ...


@runtime_checkable
class HasOrigin(Protocol):
    """A source that re-plays stored data and knows where that data first came from."""

    @property
    def origin(self) -> DriveSource: ...


class SegmentChain:
    """Consecutive recordings of 1 trip (comma2k19 segments of a route), played in order.

    Neighbouring segments may overlap by a few milliseconds, so the join merges the end of each
    segment with the start of the next in record time. Only 2 segments are open at a time.
    """

    def __init__(self, parts: Sequence[Source], *, name: str, dataset_ref: str | None) -> None:
        if not parts:
            raise ValueError("a segment chain needs at least 1 part")
        kinds = {p.meta.kind for p in parts}
        if len(kinds) != 1:
            raise ValueError(f"segment chain parts disagree on kind: {sorted(kinds)}")
        synthetic = sorted({s for p in parts for s in p.meta.synthetic_signals})
        self._parts = tuple(parts)
        self._meta = SourceMeta(
            name=name,
            kind=parts[0].meta.kind,
            dataset_ref=dataset_ref,
            synthetic_signals=tuple(synthetic),
        )

    @property
    def meta(self) -> SourceMeta:
        return self._meta

    @property
    def parts(self) -> tuple[Source, ...]:
        return self._parts

    def records(self) -> Iterator[Record]:
        cur: Iterator[Record] = iter(self._parts[0].records())
        for part in self._parts[1:]:
            nxt = iter(part.records())
            head = next(nxt, None)
            if head is None:
                continue
            t_head = record_time(head)
            tail: list[Record] = []
            for rec in cur:
                if record_time(rec) < t_head:
                    yield rec
                else:
                    tail.append(rec)
                    tail.extend(cur)
                    break
            if tail:
                t_tail_end = record_time(tail[-1])
                lead = [head]
                for rec in nxt:
                    lead.append(rec)
                    if record_time(rec) > t_tail_end:
                        break
                yield from heapq.merge(tail, lead, key=record_time)
                cur = nxt
            else:
                cur = chain([head], nxt)
        yield from cur

    def speed_trace(self) -> tuple[I64, F64]:
        """The parts' speed traces joined in time order, with repeated times dropped."""
        ts: list[I64] = []
        vs: list[F64] = []
        for p in self._parts:
            if not isinstance(p, HasSpeedTrace):
                raise TypeError(f"{p.meta.name} has no speed trace")
            t, v = p.speed_trace()
            ts.append(np.asarray(t, dtype=np.int64))
            vs.append(np.asarray(v, dtype=np.float64))
        return _strict(np.concatenate(ts), np.concatenate(vs))

    def imu_samples(self) -> Iterator[ImuSample]:
        for p in self._parts:
            if not isinstance(p, HasImuSamples):
                raise TypeError(f"{p.meta.name} has no IMU sample stream")
            yield from p.imu_samples()


def _strict(t: I64, v: F64) -> tuple[I64, F64]:
    """Sort by time (stable) and keep the first sample of each time."""
    order = np.argsort(t, kind="stable")
    t, v = t[order], v[order]
    if t.size > 1:
        keep = np.concatenate(([True], np.diff(t) > 0))
        t, v = t[keep], v[keep]
    return t, v


# -- configuration of a run ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LapLine:
    """A start and finish line in the drive's local meters (east and north of the first kept
    GPS fix), with the lap timer's guards."""

    line: StartFinishLine
    min_lap_s: float
    min_dist_m: float


@dataclass(frozen=True, slots=True)
class RunReport:
    drive_id: str
    path: Path
    label: str
    records_in: dict[str, int]
    """Source records by type."""
    records_out: dict[str, int]
    """Records the pipeline produced by type (decoded samples, estimates, events, laps)."""
    data_span_s: float
    wall_s: float
    realtime_factor: float
    """Data span divided by wall time."""
    stage_s: dict[str, float]
    errors: dict[str, int]
    notes: tuple[str, ...]
    mount_rotation: Matrix3 | None
    """The rotation the drive ended with; None when a live drive never found one."""
    stats: tuple[tuple[str, str], ...] = ()
    """(stat_id, display) of every drive stat written."""

    @property
    def n_errors(self) -> int:
        return sum(self.errors.values())

    @property
    def n_records(self) -> int:
        return sum(self.records_in.values()) + sum(self.records_out.values())

    @property
    def records_per_s(self) -> float:
        return self.n_records / self.wall_s if self.wall_s > 0 else math.inf


def _rank(kinds: set[DriveSource]) -> DriveSource:
    if "live" in kinds:
        return "live"
    if "replay" in kinds:
        return "replay"
    return "synthetic"


def drive_kind(metas: Iterable[SourceMeta]) -> DriveSource:
    """How the run streams: live if any source is live, else replay if any is a replay, else
    synthetic."""
    return _rank({m.kind for m in metas})


def drive_origin(sources: Iterable[Source]) -> DriveSource:
    """Where the drive's data came from, ranked as ``drive_kind``. A source that re-plays a
    stored drive (``HasOrigin``) counts as that drive's source, so a live drive re-run through
    ``RecordedDriveSource`` stays live."""
    return _rank({s.origin if isinstance(s, HasOrigin) else s.meta.kind for s in sources})


def dbc_sha256(name: str) -> str:
    return hashlib.sha256((VENDOR_DIR / name).read_bytes()).hexdigest()


def drive_meta(
    sources: Sequence[Source],
    *,
    vehicle_profile: str | None,
    dbc_name: str | None = None,
    drive_id: str | None = None,
) -> DriveMeta:
    """The drive row for a run over ``sources``. Its source is ``drive_origin``. Every source's
    synthetic signals are listed."""
    metas = [s.meta for s in sources]
    refs = [m.dataset_ref for m in metas if m.dataset_ref is not None]
    kind = drive_origin(sources)
    names = {name for m in metas for name in m.synthetic_signals}
    if kind == "synthetic":
        names.update(DERIVED_SIGNALS)
    synthetic = sorted(names)
    return DriveMeta(
        drive_id=drive_id or new_drive_id(),
        source=kind,
        dataset_ref=refs[0] if refs else None,
        vehicle_profile=vehicle_profile,
        dbc_name=dbc_name,
        dbc_sha256=dbc_sha256(dbc_name) if dbc_name else None,
        synthetic_signals=tuple(synthetic),
    )


def open_writer(data_dir: Path, meta: DriveMeta, cfg: Config) -> DriveWriter:
    """A writer for ``<data_dir>/drives/<drive_id>.sqlite``."""
    path = Path(data_dir) / "drives" / f"{meta.drive_id}.sqlite"
    return DriveWriter(path, meta, batch_ms=int(cfg.get("store.batch_ms")))


# -- mount rotation pre-pass -------------------------------------------------------------------


def _as_matrix(rot: NDArray[np.float64]) -> Matrix3:
    r = [tuple(float(x) for x in row) for row in np.asarray(rot, dtype=np.float64).tolist()]
    return ((r[0][0], r[0][1], r[0][2]), (r[1][0], r[1][1], r[1][2]), (r[2][0], r[2][1], r[2][2]))


def estimate_rotation(sources: Sequence[Source], cfg: Config) -> tuple[F64, str | None]:
    """(rotation, note). The note says why the identity was kept, or is None."""
    measured = [s for s in sources if s.meta.kind != "synthetic"]
    use = measured or list(sources)
    imu: list[ImuSample] = []
    traces: list[tuple[I64, F64]] = []
    gps: list[tuple[int, float]] = []
    wheel: list[tuple[int, float]] = []
    for s in use:
        fast = isinstance(s, HasImuSamples) and isinstance(s, HasSpeedTrace)
        if fast:
            assert isinstance(s, HasImuSamples) and isinstance(s, HasSpeedTrace)
            imu.extend(s.imu_samples())
            traces.append(s.speed_trace())
            continue
        for r in s.records():
            if isinstance(r, ImuSample):
                imu.append(r)
            elif isinstance(r, GpsFix) and r.fix_type > 0 and math.isfinite(r.speed_mps):
                gps.append((r.t_ns, r.speed_mps))
            elif isinstance(r, Sample) and r.signal in (WHEEL_SPEED, OBD_SPEED):
                wheel.append((r.t_ns, r.value))
    if traces:
        t = np.concatenate([np.asarray(a, dtype=np.int64) for a, _ in traces])
        v = np.concatenate([np.asarray(b, dtype=np.float64) for _, b in traces])
    else:
        pairs = gps or wheel
        t = np.array([p[0] for p in pairs], dtype=np.int64)
        v = np.array([p[1] for p in pairs], dtype=np.float64)
    finite = np.isfinite(v)
    t, v = _strict(t[finite], v[finite])
    if not imu:
        return np.eye(3), "Mount rotation: identity. The drive has no IMU samples."
    imu.sort(key=lambda s: s.t_ns)
    try:
        rot = estimate_mount_rotation(ImuArrays.from_samples(imu), t, v, cfg)
    except AlignmentError as e:
        return np.eye(3), f"Mount rotation: identity. The estimate {e}."
    return rot, None


class _OnlineAlignment:
    """Live mount calibration from the drive's own measured data (see the module docstring).

    Keeps the IMU samples and the reference speeds (GPS speed of a fix, else measured wheel or
    OBD speed) of the last ``ALIGN_WINDOW_S`` of record time, and tries
    ``estimate_mount_rotation`` on them every ``estimator.stationary_s``.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.period_ns = round(float(cfg.get("estimator.stationary_s")) * NS_PER_S)
        self.window_ns = round(ALIGN_WINDOW_S * NS_PER_S)
        self.imu: deque[ImuSample] = deque()
        self.gps: deque[tuple[int, float]] = deque()
        self.speed: deque[tuple[int, float]] = deque()
        self.next_try_ns: int | None = None
        self.last_error = "was not tried"

    def add_imu(self, s: ImuSample) -> None:
        self.imu.append(s)
        self._trim(s.t_ns)

    def add_gps(self, t_ns: int, speed_mps: float) -> None:
        self.gps.append((t_ns, speed_mps))

    def add_speed(self, t_ns: int, speed_mps: float) -> None:
        self.speed.append((t_ns, speed_mps))

    def _trim(self, t_ns: int) -> None:
        cutoff = t_ns - self.window_ns
        for q in (self.imu, self.gps, self.speed):
            while q and _first_t(q[0]) < cutoff:
                q.popleft()

    def poll(self, t_ns: int, *, now: bool = False) -> F64 | None:
        """The rotation, once the data so far determines it; tried every period, or at once
        with ``now``."""
        if not now:
            if self.next_try_ns is None:
                self.next_try_ns = t_ns + self.period_ns
                return None
            if t_ns < self.next_try_ns:
                return None
        self.next_try_ns = t_ns + self.period_ns
        pairs = list(self.gps) if len(self.gps) >= 2 else list(self.speed)
        t = np.array([p[0] for p in pairs], dtype=np.int64)
        v = np.array([p[1] for p in pairs], dtype=np.float64)
        finite = np.isfinite(v)
        t, v = _strict(t[finite], v[finite])
        try:
            return estimate_mount_rotation(ImuArrays.from_samples(list(self.imu)), t, v, self.cfg)
        except AlignmentError as e:
            self.last_error = str(e)
            return None


def _first_t(item: ImuSample | tuple[int, float]) -> int:
    return item.t_ns if isinstance(item, ImuSample) else item[0]


# -- the run -------------------------------------------------------------------------------------


@dataclass
class _Series:
    t: list[int] = field(default_factory=list)
    v: list[float] = field(default_factory=list)

    def arrays(self) -> tuple[I64, F64]:
        return _strict(np.array(self.t, dtype=np.int64), np.array(self.v, dtype=np.float64))


@dataclass
class _StateLog:
    t: list[int] = field(default_factory=list)
    speed: list[float] = field(default_factory=list)
    accel_long: list[float] = field(default_factory=list)
    accel_lat: list[float] = field(default_factory=list)
    var_speed: list[float] = field(default_factory=list)
    dr: list[bool] = field(default_factory=list)

    def add(self, s: StateEstimate) -> None:
        if self.t and s.t_ns <= self.t[-1]:
            return
        self.t.append(s.t_ns)
        self.speed.append(s.speed_mps)
        self.accel_long.append(s.accel_long_mps2)
        self.accel_lat.append(s.accel_lat_mps2)
        self.var_speed.append(s.var_speed)
        self.dr.append(s.dr)


@dataclass(frozen=True, slots=True, eq=False)
class _Motion:
    """The series the performance, behavior, and baseline analysis run on."""

    t: I64
    speed: F64
    accel_long: F64
    accel_lat: F64 | None
    """None when nothing measured it (a drive with no IMU). NaN at speed-only estimates."""
    var_speed: F64 | None
    dr: NDArray[np.bool_] | None
    speed_signal: str


def _hold(t_src: I64, v_src: F64, t_query: I64, max_gap_ns: int) -> F64:
    """The latest value at or before each query time, NaN when older than ``max_gap_ns``."""
    out = np.full(t_query.shape, np.nan)
    if t_src.size == 0:
        return out
    j = np.searchsorted(t_src, t_query, side="right") - 1
    ok = j >= 0
    jc = np.clip(j, 0, t_src.size - 1)
    ok &= (t_query - t_src[jc]) <= max_gap_ns
    out[ok] = v_src[jc[ok]]
    return out


class _LiveFeed:
    """Live frames at ``[live] stream_hz`` of record time, and the moving lock.

    ``synthetic`` names the drive's synthetic signals: rpm and coolant read measured signals
    first, as the stored timeline does, and a frame lists the fields a synthetic signal fed
    (``synthetic_fields``). ``mark`` labels an alert raised on synthetic data.

    ``watch(clock)`` starts the lost-speed watchdog: a thread that checks every frame period of
    ``clock`` time whether a state estimate arrived within ``estimator.max_dead_reckon_s``,
    and locks when none did. The lock only ever closes from there. The record loop and the
    watchdog share ``_lock``.
    """

    def __init__(
        self,
        live: LiveState,
        cfg: Config,
        *,
        gate_moving: bool,
        t0_ns: int,
        synthetic: frozenset[str],
        mark: Callable[[Event], Event],
    ) -> None:
        self.live = live
        self.gate = gate_moving
        self.t0 = t0_ns
        self.synthetic = synthetic
        self.mark = mark
        self.sustain_ns = baseline_sustain_ns(cfg)
        self.period_ns = round(NS_PER_S / float(cfg.get("live.stream_hz")))
        self.next_ns = t0_ns
        self.on_mps = float(cfg.get("behavior.min_speed_mps"))
        self.rest_mps = float(cfg.get("performance.start_speed_mps"))
        self.rest_ns = round(float(cfg.get("estimator.stationary_s")) * NS_PER_S)
        self.lost_ns = round(float(cfg.get("estimator.max_dead_reckon_s")) * NS_PER_S)
        self.speed: tuple[int, float] | None = None
        self.last: dict[str, tuple[int, float]] = {}
        self.below_since: int | None = None
        self.moving = True  # a live run sets the lock before streaming starts
        self.frames = 0
        self._lock = threading.Lock()
        self._clock: Clock | None = None
        self._state_seen_ns = 0
        """``_clock`` time of the latest state estimate (of the watchdog start before any)."""

    def watch(self, clock: Clock) -> Callable[[], None]:
        """Start the lost-speed watchdog on ``clock`` (gated runs only). Returns its stop."""
        if not self.gate:
            return lambda: None
        stop = threading.Event()
        with self._lock:
            self._clock = clock
            self._state_seen_ns = clock.now_ns()
        period_s = self.period_ns / NS_PER_S

        def loop() -> None:
            while not stop.wait(period_s):
                self.check_lost(clock.now_ns())

        thread = threading.Thread(target=loop, name="photoncar-moving-watch", daemon=True)
        thread.start()

        def halt() -> None:
            stop.set()
            thread.join()

        return halt

    def check_lost(self, now_ns: int) -> None:
        """Lock when no state estimate arrived within ``max_dead_reckon_s`` of ``now_ns``."""
        with self._lock:
            if self._clock is not None and now_ns - self._state_seen_ns > self.lost_ns:
                self.below_since = None
                self._set_moving(True)

    def on_state(self, s: StateEstimate) -> None:
        t, speed_mps = s.t_ns, s.speed_mps
        self.speed = (t, speed_mps)
        if not self.gate:
            return
        with self._lock:
            if self._clock is not None:
                self._state_seen_ns = self._clock.now_ns()
            if speed_mps >= self.on_mps:
                self.below_since = None
                self._set_moving(True)
            elif speed_mps < self.rest_mps:
                if self.below_since is None:
                    self.below_since = t
                if t - self.below_since >= self.rest_ns:
                    self._set_moving(False)
            else:
                self.below_since = None

    def on_sample(self, s: Sample) -> None:
        self.last[s.signal] = (s.t_ns, s.value)

    def _set_moving(self, moving: bool) -> None:
        if moving != self.moving:
            self.moving = moving
            self.live.set_moving(moving)

    def _latest(self, sources: tuple[tuple[str, int], ...], t: int) -> tuple[float, str] | None:
        """(value, signal) of the freshest ranked source within its stale limit."""
        for name, gap in ranked_sources(sources, self.synthetic, self.last):
            last = self.last[name]
            if t - last[0] <= gap:
                return last[1], name
        return None

    def tick(self, t: int, rules: RuleEngine) -> None:
        if t < self.next_ns:
            return
        if self.gate and (self.speed is None or t - self.speed[0] > self.lost_ns):
            with self._lock:
                self.below_since = None
                self._set_moving(True)
        speed = None
        if self.speed is not None and t - self.speed[0] <= self.lost_ns:
            speed = self.speed[1] / MPH
        rpm = self._latest(RPM_SOURCES, t)
        coolant = self._latest(COOLANT_SOURCES, t)
        fed = {
            "speed_mph": None if speed is None else SPEED,
            "rpm": None if rpm is None else rpm[1],
            "coolant_c": None if coolant is None else coolant[1],
        }
        frame: dict[str, Any] = {
            "t_s": (t - self.t0) / NS_PER_S,
            "speed_mph": speed,
            "rpm": None if rpm is None else rpm[0],
            "coolant_c": None if coolant is None else coolant[0] - KELVIN_OFFSET,
            "synthetic_fields": [k for k, name in fed.items() if name in self.synthetic],
            "alert": alert_json(
                active_alert([self.mark(e) for e in rules.active(t)], t, self.sustain_ns)
            ),
        }
        self.live.publish(frame)
        self.frames += 1
        while self.next_ns <= t:
            self.next_ns += self.period_ns


class _Run:
    def __init__(
        self,
        sources: Sequence[Source],
        cfg: Config,
        writer: DriveWriter,
        vehicle: Config,
        index: DriveIndex | None,
        live: LiveState | None,
        decoder: DbcDecoder | None,
        lines: Sequence[LapLine],
        realtime: bool,
        label: str | None,
        mount_rot: NDArray[np.float64] | None,
        rested_h: float | None,
        utc: Clock | None,
        live_clock: Clock | None,
    ) -> None:
        if not sources:
            raise ValueError("run_drive needs at least 1 source")
        self.sources = list(sources)
        self.cfg = cfg
        self.writer = writer
        self.vehicle = vehicle
        self.index = index
        self.live = live
        self.decoder = decoder
        self.lines = list(lines)
        self.realtime = realtime
        self.meta = writer.meta
        self.kind = drive_kind(s.meta for s in self.sources)
        if live_clock is not None and self.kind != "live":
            raise ValueError("live_clock goes with live sources only")
        self.label = label or default_label(self.meta.source)
        self.mount_rot = mount_rot
        self.rested_h = rested_h
        self.utc = utc
        self.live_clock = live_clock
        self.align: _OnlineAlignment | None = None
        self.imu_seen = False
        self.rot_found_ns: int | None = None
        self.rot_found: F64 | None = None
        self.imu_waits = False
        """True from a live calibration until the next speed measurement reaches the estimator."""
        self.synthetic = set(self.meta.synthetic_signals)
        for s in self.sources:
            self.synthetic.update(s.meta.synthetic_signals)
        self.all_synthetic = self.kind == "synthetic"
        if self.all_synthetic:
            self.synthetic.update(DERIVED_SIGNALS)
        self.stage_ns: Counter[str] = Counter()
        self.records_in: Counter[str] = Counter()
        self.records_out: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()
        self.notes: list[str] = []
        self.events: list[Event] = []
        self.states = _StateLog()
        self.gps: list[tuple[int, float, float, int]] = []
        self.samples: dict[str, _Series] = {}
        self.pending_baselines: list[bl.BaselineStats] = []
        self.feed: _LiveFeed | None = None
        self.motion: _Motion | None = None

    # -- helpers ---------------------------------------------------------------------------

    def _write(self, rec: Record) -> bool:
        t = time.perf_counter_ns()
        try:
            self.writer.write(rec)
        except ValueError:
            self.errors["write_refused"] += 1
            return False
        finally:
            self.stage_ns["write"] += time.perf_counter_ns() - t
        return True

    def _mark(self, ev: Event) -> Event:
        synth = self.all_synthetic or any(
            c.role == "trigger" and c.signal in self.synthetic for c in ev.citations
        )
        if not synth:
            return ev
        message = ev.message
        if ev.kind.startswith("alert.") and not message.endswith(SYNTHETIC_FAULT):
            message = f"{message} {SYNTHETIC_FAULT}"
        return replace(ev, synthetic=True, message=message)

    def _emit(self, ev: Event) -> None:
        ev = self._mark(ev)
        if self._write(ev):
            self.events.append(ev)
            self.records_out["Event"] += 1

    def _measured(self, name: str) -> bool:
        return name not in self.synthetic and not self.all_synthetic

    def _late(self, rec: Record) -> None:
        """A live record that arrived after later ones went out: stored, not analysed."""
        self.records_in[type(rec).__name__] += 1
        self.errors["late_record"] += 1
        self._write(rec)

    def _sample(self, s: Sample, rules: RuleEngine, est: StateEstimator) -> None:
        if (
            s.signal in (OBD_SPEED, WHEEL_SPEED)
            and (self.all_synthetic or s.signal not in self.synthetic)
            and math.isfinite(s.value)
        ):
            if self.align is not None:
                self.align.add_speed(s.t_ns, s.value)
            t = time.perf_counter_ns()
            for st in est.push(s):
                self._state(st)
            self.stage_ns["estimate"] += time.perf_counter_ns() - t
            self.imu_waits = False
        t = time.perf_counter_ns()
        events = rules.push(s)
        self.stage_ns["rules"] += time.perf_counter_ns() - t
        for ev in events:
            self._emit(ev)
        series = self.samples.get(s.signal)
        if series is None:
            series = self.samples[s.signal] = _Series()
        series.t.append(s.t_ns)
        series.v.append(s.value)
        if self.feed is not None:
            self.feed.on_sample(s)

    def _state(self, st: StateEstimate) -> None:
        if self._write(st):
            self.records_out["StateEstimate"] += 1
            self.states.add(st)
            if self.feed is not None:
                self.feed.on_state(st)

    # -- stages ----------------------------------------------------------------------------

    def run(self) -> RunReport:
        wall0 = time.perf_counter_ns()
        if self.live is not None:
            self.live.set_mode("live" if self.kind == "live" else "replay")
            if self.kind == "live":
                # Locked before the first record: a quiet bus proves nothing about speed.
                self.live.set_moving(True)
        ok = False
        try:
            report = self._run(wall0)
            ok = True
            return report
        finally:
            if self.live is not None:
                self.live.set_mode("idle")
                if ok or self.kind != "live":
                    self.live.set_moving(False)
                else:
                    self.live.set_moving(True)

    def _run(self, wall0: int) -> RunReport:
        t = time.perf_counter_ns()
        rot: F64 | None
        if self.kind != "live":
            rot, note = estimate_rotation(self.sources, self.cfg)
            if note:
                self.notes.append(note)
        elif self.mount_rot is not None:
            rot = np.asarray(self.mount_rot, np.float64)
        else:
            rot = None  # unknown: found online from the drive's own data, if it can be
            self.align = _OnlineAlignment(self.cfg)
        if rot is not None:
            self.writer.set_mount_rotation(rot)
        self.stage_ns["prepass"] += time.perf_counter_ns() - t

        est = StateEstimator(self.cfg, rot)
        rules = RuleEngine(self.cfg, rested_h=self.rested_h)
        decode_errors0 = self.decoder.decode_errors if self.decoder is not None else 0
        clock = self.live_clock or MonotonicClock()
        stream = (
            merge_live(self.sources, clock, on_late=self._late)
            if self.kind == "live"
            else merge_sources(self.sources)
        )
        loop0 = time.perf_counter_ns()
        span = self._loop(stream, est, rules, clock)
        loop_ns = time.perf_counter_ns() - loop0
        inner = sum(
            self.stage_ns[k] for k in ("write", "decode", "estimate", "align", "rules", "live")
        )
        self.stage_ns["read_merge"] += max(loop_ns - inner, 0)
        if self.decoder is not None:
            self.errors["dbc_decode"] += self.decoder.decode_errors - decode_errors0
        if span is None:
            raise PipelineError("the sources yielded no records")
        t_first, t_last = span
        rot = self._rotation_notes(rot, t_first, t_last)

        t = time.perf_counter_ns()
        for ev in rules.finish(t_last):
            self._emit(ev)
        self.stage_ns["rules"] += time.perf_counter_ns() - t

        self.motion = self._motion()
        behavior = self._analyze(t_last)
        self._laps()
        t = time.perf_counter_ns()
        self._baseline(t_last)
        self.stage_ns["baseline"] += time.perf_counter_ns() - t
        stats = self._finish_file(behavior)
        t = time.perf_counter_ns()
        self._index()
        self.stage_ns["index"] += time.perf_counter_ns() - t

        wall_ns = time.perf_counter_ns() - wall0
        span_s = (t_last - t_first) / NS_PER_S
        wall_s = wall_ns / NS_PER_S
        return RunReport(
            drive_id=self.meta.drive_id,
            path=self.writer.path,
            label=self.label,
            records_in=dict(sorted(self.records_in.items())),
            records_out=dict(sorted(self.records_out.items())),
            data_span_s=span_s,
            wall_s=wall_s,
            realtime_factor=span_s / wall_s if wall_s > 0 else math.inf,
            stage_s={k: v / NS_PER_S for k, v in self.stage_ns.items()},
            errors={k: v for k, v in sorted(self.errors.items())},
            notes=tuple(self.notes),
            mount_rotation=None if rot is None else _as_matrix(rot),
            stats=tuple((s.stat_id, s.display) for s in stats),
        )

    def _loop(
        self, stream: Iterator[Record], est: StateEstimator, rules: RuleEngine, clock: Clock
    ) -> tuple[int, int] | None:
        """Stream every record through the pipeline. Returns (first, last) record time."""
        t_first: int | None = None
        t_last: int | None = None
        pace0 = time.perf_counter()
        halt_watch: Callable[[], None] | None = None
        try:
            for rec in stream:
                if isinstance(rec, StateEstimate | Event | Lap):
                    raise PipelineError(
                        f"sources must yield captured records only, not {type(rec).__name__}"
                    )
                t_rec = record_time(rec)
                if t_first is None:
                    t_first = t_rec
                    if self.live is not None:
                        self.feed = _LiveFeed(
                            self.live,
                            self.cfg,
                            gate_moving=self.kind == "live",
                            t0_ns=t_first,
                            synthetic=frozenset(self.synthetic),
                            mark=self._mark,
                        )
                        halt_watch = self.feed.watch(clock)
                t_last = t_rec if t_last is None else max(t_last, t_rec)
                if self.realtime:
                    wait = (t_rec - t_first) / NS_PER_S - (time.perf_counter() - pace0)
                    if wait > 0:
                        time.sleep(wait)
                self.records_in[type(rec).__name__] += 1
                if not self._write(rec):
                    continue
                if isinstance(rec, CanFrame):
                    if self.decoder is not None:
                        t = time.perf_counter_ns()
                        decoded = self.decoder.decode(rec)
                        self.stage_ns["decode"] += time.perf_counter_ns() - t
                        for s in decoded:
                            if self._write(s):
                                self.records_out["Sample"] += 1
                                self._sample(s, rules, est)
                elif isinstance(rec, ImuSample | GpsFix):
                    self._motion_record(rec, est)
                elif isinstance(rec, Sample):
                    self._sample(rec, rules, est)
                elif not isinstance(rec, ObdPoll):
                    raise PipelineError(f"unknown record type {type(rec).__name__}")
                if self.feed is not None:
                    t = time.perf_counter_ns()
                    self.feed.tick(t_rec, rules)
                    self.stage_ns["live"] += time.perf_counter_ns() - t
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
            if halt_watch is not None:
                halt_watch()
        if t_first is None or t_last is None:
            return None
        return t_first, t_last

    def _motion_record(self, rec: ImuSample | GpsFix, est: StateEstimator) -> None:
        """An IMU sample or a GPS fix. While a live drive's mount rotation is unknown the IMU
        is recorded only; the calibration gets it, and the estimator does not. Once the
        rotation is found, the IMU joins the estimator at the next speed measurement, so the
        speed it carries on from was measured just before."""
        speed_fix = isinstance(rec, GpsFix) and rec.fix_type > 0 and math.isfinite(rec.speed_mps)
        if isinstance(rec, GpsFix):
            self.gps.append((rec.t_ns, rec.lat_deg, rec.lon_deg, rec.fix_type))
            if self.align is not None and speed_fix:
                self.align.add_gps(rec.t_ns, rec.speed_mps)
        else:
            self.imu_seen = True
            if self.align is not None:
                t = time.perf_counter_ns()
                self.align.add_imu(rec)
                rot = self.align.poll(rec.t_ns)
                self.stage_ns["align"] += time.perf_counter_ns() - t
                if rot is None:
                    return
                self.align = None
                self.rot_found_ns = rec.t_ns
                self.rot_found = rot
                est.set_mount_rotation(rot)
                self.writer.set_mount_rotation(rot)
                self.imu_waits = True
            if self.imu_waits:
                return
        t = time.perf_counter_ns()
        states = est.push(rec)
        self.stage_ns["estimate"] += time.perf_counter_ns() - t
        for st in states:
            self._state(st)
        if speed_fix:
            self.imu_waits = False

    def _rotation_notes(self, rot: F64 | None, t_first: int, t_last: int) -> F64 | None:
        """The rotation the drive ended with, and a note on a live calibration. A drive that
        ends before its rotation is found gets 1 last try over its final data; a rotation
        found then is stored for the record, but came too late for the IMU to be used."""
        if self.rot_found_ns is not None:
            after_s = (self.rot_found_ns - t_first) / NS_PER_S
            self.notes.append(
                f"Mount rotation found {after_s:.1f} s into the drive, from its own rest and "
                "straight-line speed change. Before that the IMU was recorded but not used."
            )
            return self.rot_found
        if self.align is None or not self.imu_seen:
            return rot
        late = self.align.poll(t_last, now=True)
        if late is not None:
            self.writer.set_mount_rotation(late)
            self.notes.append(
                "Mount rotation found at the end of the drive, too late to use. The IMU was "
                "recorded but not used."
            )
            return late
        self.notes.append(
            f"Mount rotation: unknown. The estimate {self.align.last_error}. The IMU was "
            "recorded but not used."
        )
        return rot

    def _pick(self, names: Sequence[str]) -> str | None:
        """The first signal with samples, measured ones before synthetic ones."""
        present = [n for n in names if n in self.samples and self.samples[n].t]
        measured = [n for n in present if self._measured(n)]
        ranked = measured or present
        return ranked[0] if ranked else None

    def _motion(self) -> _Motion | None:
        """The state estimates. Lateral accel is None when no estimate had an IMU."""
        st = self.states
        if len(st.t) < 3:
            return None
        accel_lat = np.array(st.accel_lat, dtype=np.float64)
        return _Motion(
            t=np.array(st.t, dtype=np.int64),
            speed=np.array(st.speed, dtype=np.float64),
            accel_long=np.array(st.accel_long, dtype=np.float64),
            accel_lat=accel_lat if bool(np.any(np.isfinite(accel_lat))) else None,
            var_speed=np.array(st.var_speed, dtype=np.float64),
            dr=np.array(st.dr, dtype=np.bool_),
            speed_signal=SPEED,
        )

    def _analyze(self, t_last: int) -> BehaviorReport | None:
        m = self.motion
        if m is None:
            self.notes.append(
                "No performance or behavior analysis: the drive has fewer than 3 state estimates."
            )
            return None
        t, v, a_long, a_lat = m.t, m.speed, m.accel_long, m.accel_lat
        imu = None if a_lat is None else np.isfinite(a_lat)
        if imu is not None and int(np.count_nonzero(imu)) < 3:
            imu = a_lat = None
        if imu is None:
            lead = "IMU not used" if self.imu_seen else "No IMU"
            self.notes.append(
                f"{lead}: speed and longitudinal accel come from speed measurements alone. "
                "No lateral g, cornering, or lateral jerk."
            )
        elif not bool(np.all(imu)):
            gap_s = float(np.sum(np.diff(t)[~imu[1:]])) / NS_PER_S
            self.notes.append(
                f"IMU not used for {gap_s:.1f} s: those seconds have speed only, and driver "
                "behavior leaves them out."
            )

        t0 = time.perf_counter_ns()
        perf = zero_to_sixty(t, v, self.cfg, var_speed=m.var_speed, dr=m.dr, signal=m.speed_signal)
        perf += sixty_to_zero(t, v, self.cfg, var_speed=m.var_speed, dr=m.dr, signal=m.speed_signal)
        if imu is not None and a_lat is not None:
            # Lateral g and driver behavior run on the estimates made with the IMU.
            t, v, a_long, a_lat = t[imu], v[imu], a_long[imu], a_lat[imu]
            peak = peak_lateral(t, a_lat, self.cfg)
            if peak is not None:
                perf.append(peak)
        for ev in sorted(perf, key=lambda e: (e.t_start_ns, e.kind)):
            self._emit(self._clamp(ev, t_last))
        self.stage_ns["performance"] += time.perf_counter_ns() - t0

        t0 = time.perf_counter_ns()
        rpm_name = self._pick(ANALYSIS_RPM)
        rpm = None
        if rpm_name is not None:
            rt, rv = self.samples[rpm_name].arrays()
            rpm = interp_gap(rt, rv, t, MAX_GAP_NS[rpm_name])
        report = analyze(
            t,
            v,
            a_long,
            a_lat,
            rpm,
            self.cfg,
            self.vehicle,
            rpm_signal=rpm_name or "obd.rpm",
        )
        if report.gear_note is not None:
            self.notes.append(report.gear_note)
        lookback_ns = round(float(self.cfg.get("behavior.shift_lookback_s")) * NS_PER_S)
        shifts = {(s.t_ns - lookback_ns, s.rpm): s for s in report.shifts}
        for ev in report.events:
            if ev.kind == "behavior.shift":
                shift = shifts.get((ev.t_start_ns, ev.value))
                if shift is not None:
                    ev = replace(
                        ev,
                        citations=(
                            *ev.citations,
                            _gear_cite(GEAR_FROM_SIGNAL, ev, "from_gear", shift.from_gear),
                            _gear_cite(GEAR_TO_SIGNAL, ev, "to_gear", shift.to_gear),
                        ),
                    )
            self._emit(self._clamp(ev, t_last))
        self.stage_ns["behavior"] += time.perf_counter_ns() - t0
        return report

    @staticmethod
    def _clamp(ev: Event, t_last: int) -> Event:
        """End an event and its citations at the last record, never before their start."""
        if ev.t_end_ns <= t_last:
            return ev
        cites = tuple(
            replace(c, t_end_ns=max(min(c.t_end_ns, t_last), c.t_start_ns + 1))
            for c in ev.citations
        )
        return replace(ev, t_end_ns=max(t_last, ev.t_start_ns + 1), citations=cites)

    def _laps(self) -> None:
        t0 = time.perf_counter_ns()
        kept = [
            g
            for g in self.gps
            if g[3] >= MIN_KEPT_FIX_TYPE and math.isfinite(g[1]) and math.isfinite(g[2])
        ]
        if self.lines and len(kept) >= 2:
            lat = np.array([g[1] for g in kept], dtype=np.float64)
            lon = np.array([g[2] for g in kept], dtype=np.float64)
            east, north = enu_m(lat, lon, float(lat[0]), float(lon[0]))
            for spec in self.lines:
                timer = LapTimer(spec.line, spec.min_lap_s, spec.min_dist_m)
                last_t: int | None = None
                for (t_ns, _, _, _), x, y in zip(kept, east.tolist(), north.tolist(), strict=True):
                    if last_t is not None and t_ns <= last_t:
                        continue
                    last_t = t_ns
                    lap = timer.push(t_ns, x, y)
                    if lap is not None and self._write(lap):
                        self.records_out["Lap"] += 1
        self.stage_ns["laps"] += time.perf_counter_ns() - t0

    def _baseline(self, t_last: int) -> None:
        if self.index is None:
            return
        rpm_name = self._pick(ANALYSIS_RPM)
        if rpm_name is None or not self._measured(rpm_name):
            self.notes.append("Baseline: no measured rpm, so no operating bins. Nothing learned.")
            return
        rpm_t, rpm_v = self.samples[rpm_name].arrays()
        coolant_name = self._pick(ANALYSIS_COOLANT)
        cool: tuple[I64, F64, int] | None = None
        if coolant_name is not None and self._measured(coolant_name):
            ct, cv = self.samples[coolant_name].arrays()
            cool = (ct, cv, MAX_GAP_NS[coolant_name])
        fuel: tuple[I64, F64] | None = None
        if FUEL_SYSTEM in self.samples and self._measured(FUEL_SYSTEM):
            fuel = self.samples[FUEL_SYSTEM].arrays()
        st_t = self.motion.t if self.motion is not None else np.zeros(0, dtype=np.int64)
        st_v = self.motion.speed if self.motion is not None else np.zeros(0, dtype=np.float64)
        window_s = float(self.cfg.get("baseline.window_s"))
        learned: list[str] = []
        for sig in BASELINE_SIGNALS:
            if sig not in self.samples or not self._measured(sig):
                continue
            t, v = self.samples[sig].arrays()
            if t.size == 0:
                continue
            rpm_at = v if sig == rpm_name else interp_gap(rpm_t, rpm_v, t, MAX_GAP_NS[rpm_name])
            coolant_at = (
                interp_gap(cool[0], cool[1], t, cool[2])
                if cool is not None
                else np.full(t.shape, np.nan)
            )
            speed_at = interp_gap(st_t, st_v, t, SPEED_MAX_GAP_NS)
            closed = None
            if sig in TRIMS:
                fs = (
                    _hold(fuel[0], fuel[1], t, MAX_GAP_NS[FUEL_SYSTEM])
                    if fuel is not None
                    else np.full(t.shape, np.nan)
                )
                closed = fs == CLOSED_LOOP
            bins = bl.operating_bins(rpm_at, coolant_at, speed_at, self.cfg, closed_loop=closed)
            prev = [
                bl.BaselineStats(
                    signal=r.signal,
                    bin=r.bin,
                    median=r.median,
                    mad=r.mad,
                    n_windows=r.n_windows,
                    n_drives=r.n_drives,
                    version=r.version,
                    updated_drive=r.updated_drive or "",
                )
                for r in self.index.get_baselines(sig)
            ]
            for ev in bl.check(prev, t, v, bins, self.cfg, signal=sig):
                self._emit(self._clamp(ev, t_last))
            wm = bl.window_medians(t, v, window_s, bins)
            windows = bl.to_windows(sig, wm, self.events)
            new = bl.update(prev, windows, self.meta.drive_id)
            changed = [s for s in new if s.updated_drive == self.meta.drive_id]
            self.pending_baselines.extend(changed)
            if changed:
                learned.append(sig)
        if not learned:
            self.notes.append(
                "Baseline: no window qualified (bins need measured rpm and coolant). "
                "Nothing learned."
            )

    def _finish_file(self, behavior: BehaviorReport | None) -> list[DriveStat]:
        t0 = time.perf_counter_ns()
        self.writer.flush()
        with DriveReader(self.writer.path) as reader:
            rows = reader.timeline_rows()
        self.writer.write_timeline(rows)
        with DriveReader(self.writer.path) as reader:
            summary = drive_summary(reader)
        duration = (
            (summary.t_end_ns - summary.t_start_ns) / NS_PER_S
            if summary.t_start_ns is not None and summary.t_end_ns is not None
            else None
        )
        speeds = [r.speed_mps for r in rows if r.speed_mps is not None]
        totals = DriveTotals(
            duration_s=duration,
            distance_m=summary.distance_m,
            max_speed_mps=max(speeds) if speeds else None,
        )
        stats = drive_stats(totals, self.events, behavior)
        self.writer.write_stats(stats)
        self.stage_ns["timeline_stats"] += time.perf_counter_ns() - t0
        t0 = time.perf_counter_ns()
        self.writer.close()
        self.stage_ns["close"] += time.perf_counter_ns() - t0
        return stats

    def _index(self) -> None:
        if self.index is None:
            return
        with DriveReader(self.writer.path) as reader:
            self.index.register(reader.meta(), self.label)
            self.index.update_from_reader(reader)
        if self.utc is not None:
            self.index.set_utc(self.meta.drive_id, end_ns=self.utc.now_ns())
        if self.pending_baselines:
            self.index.put_baselines(self.pending_baselines, self.meta.drive_id)


def _gear_cite(signal: str, ev: Event, stat: str, gear: int) -> Citation:
    return Citation(
        signal=signal,
        source=GEAR_SOURCE,
        role="context",
        t_start_ns=ev.t_start_ns,
        t_end_ns=ev.t_end_ns,
        stat=stat,
        stat_value=float(gear),
        unit="gear",
        n=1,
    )


def default_label(kind: DriveSource) -> str:
    return {
        "live": "Live drive",
        "replay": "Replay: public dataset",
        "synthetic": "Synthetic drive",
    }[kind]


def run_drive(
    sources: Sequence[Source],
    cfg: Config,
    *,
    writer: DriveWriter,
    vehicle: Config,
    index: DriveIndex | None,
    live: LiveState | None = None,
    dbc_decoder: DbcDecoder | None = None,
    lines: Sequence[LapLine] | None = None,
    realtime: bool = False,
    label: str | None = None,
    mount_rot: NDArray[np.float64] | None = None,
    rested_h: float | None = None,
    utc: Clock | None = None,
    live_clock: Clock | None = None,
) -> RunReport:
    """Stream ``sources`` into ``writer`` and close the drive. See the module docstring.

    ``writer`` is open and owned by this call: it is closed on success (cleanly) and on error
    (not cleanly). ``index`` gets the drive entry and the baseline updates. ``realtime`` paces
    records to the wall clock. ``label`` names the drive in the index. ``rested_h`` is the time
    since the previous drive ended, for the rested-voltage advisory (unknown by default).
    ``utc`` (wall-clock UTC) stamps the drive's end in the index when it closes.
    ``live_clock`` (live sources only) is the clock the sources stamp records on, for the live
    merge and the lost-speed watchdog; a live run without it uses the monotonic clock.
    """
    try:
        run = _Run(
            sources,
            cfg,
            writer,
            vehicle,
            index,
            live,
            dbc_decoder,
            lines or (),
            realtime,
            label,
            mount_rot,
            rested_h,
            utc,
            live_clock,
        )
        return run.run()
    finally:
        if not writer.closed:
            writer.close(clean=False)


def run_to_data_dir(
    sources: Sequence[Source],
    cfg: Config,
    vehicle: Config,
    data_dir: Path,
    *,
    vehicle_profile: str | None,
    label: str,
    dbc_decoder: DbcDecoder | None = None,
    dbc_name: str | None = None,
    lines: Sequence[LapLine] | None = None,
    live: LiveState | None = None,
    realtime: bool = False,
    utc: Clock | None = None,
    live_clock: Clock | None = None,
) -> RunReport:
    """Open a writer and the index under ``data_dir``, run the drive, and close both.

    A live drive enters the index at once with its UTC start from ``utc`` (default the wall
    clock), and the rule engine gets the hours since the previous live drive ended. A stored
    live drive re-run from its file is a replay here: it keeps its live source but gets no UTC
    times of its own. ``live_clock`` goes to ``run_drive``.
    """
    meta = drive_meta(sources, vehicle_profile=vehicle_profile, dbc_name=dbc_name)
    writer = open_writer(data_dir, meta, cfg)
    with DriveIndex(Path(data_dir) / "index.sqlite") as index:
        rested_h = None
        clock = None
        if drive_kind(s.meta for s in sources) == "live":
            clock = utc or WallClock()
            start = clock.now_ns()
            rested_h = index.rested_h(start, exclude=meta.drive_id)
            index.register(meta, label)
            index.set_utc(meta.drive_id, start_ns=start)
        return run_drive(
            sources,
            cfg,
            writer=writer,
            vehicle=vehicle,
            index=index,
            live=live,
            dbc_decoder=dbc_decoder,
            lines=lines,
            realtime=realtime,
            label=label,
            rested_h=rested_h,
            utc=clock,
            live_clock=live_clock,
        )


def records_table(counts: Mapping[str, int]) -> str:
    """``Type n, Type n`` for plain output."""
    return ", ".join(f"{k} {v:,}" for k, v in counts.items())
