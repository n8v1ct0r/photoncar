"""Driver behavior: hard braking, hard acceleration, cornering, jerk, RPM bands, and shifts.

Thresholds come from ``[behavior]`` in config and are applied to the trailing
``behavior.window_s`` mean, which matches the insurers' "mph in 1 s" definition and ignores
single-sample spikes. Nothing is flagged below ``behavior.min_speed_mps``.

Shift points come from the speed to RPM ratio k = v / rpm. In a fixed gear k is constant, so
gears show up as clusters of k. A cluster counts as a gear only if k stays within
``shift_ratio_tol`` of it for at least ``shift_dwell_s`` while the RPM sweeps more than the
tolerance band (so a continuously variable ratio at steady RPM is not mistaken for a gear).
Gear numbers are ranks of the detected clusters by ratio (1 = lowest v / rpm seen), not the
gearbox's own numbering.

The clusters count as gears only when they are plausible: no more of them than the vehicle
profile's ``vehicle.max_gears`` (``UNKNOWN_MAX_GEARS`` when the profile has none), and stable,
with at least ``MIN_GEAR_COVERAGE`` of the samples that have a ratio inside a cluster. A slipping
torque converter or a CVT smears the ratio into many clusters and leaves much of the drive
between them. When either check fails, no shift is reported and ``gear_note`` says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise

import numpy as np
from numpy.typing import ArrayLike

from photoncar.config import Config, ConfigError
from photoncar.detect._series import (
    NUM_EPS,
    BoolArray,
    FloatArray,
    IntArray,
    as_times,
    as_values,
    centered_mean,
    count_in,
    runs,
    seconds,
    segments,
    to_ns,
    trailing_mean,
)
from photoncar.records import Citation, Event
from photoncar.signals import label
from photoncar.units import G0

RULE_VERSION = 1
NO_GEARS_NOTE = "No discrete gears detected (automatic, CVT, or too little data)."
UNKNOWN_MAX_GEARS = 10
"""Gear bound for a profile without ``vehicle.max_gears`` (an unknown gearbox)."""
MIN_GEAR_COVERAGE = 0.6
"""[proposed] Share of the samples with a v / rpm ratio that must fall inside a detected
cluster. Measured 2026-09-26: synthetic drives 0.92 to 0.95; the comma2k19 RAV4 sample 0.40
and its default route 0.82 (with 13 clusters)."""

ACCEL_LONG = "state.accel_long"
ACCEL_LAT = "state.accel_lat"


@dataclass(frozen=True, slots=True)
class Shift:
    t_ns: int
    rpm: float
    from_gear: int
    to_gear: int


@dataclass(frozen=True, slots=True)
class BehaviorReport:
    events: list[Event] = field(default_factory=list)
    jerk_long_mean: float | None = None
    jerk_long_p95: float | None = None
    jerk_lat_mean: float | None = None
    jerk_lat_p95: float | None = None
    rpm_band_s: list[tuple[float, float, float]] = field(default_factory=list)
    shifts: list[Shift] = field(default_factory=list)
    gear_ratios: list[float] = field(default_factory=list)
    """Detected v / rpm cluster centers in (m/s) per rpm, ascending (gear 1 first). Empty
    when the clusters are not plausible gears (see the module docstring)."""
    gear_note: str | None = None


@dataclass(frozen=True, slots=True)
class _Tier:
    rule_id: str
    kind: str
    threshold_g: float
    title: str


def analyze(
    t_ns: ArrayLike,
    speed_mps: ArrayLike,
    accel_long: ArrayLike,
    accel_lat: ArrayLike | None,
    rpm: ArrayLike | None,
    cfg: Config,
    vehicle: Config,
    *,
    rpm_signal: str = "obd.rpm",
) -> BehaviorReport:
    """Analyze aligned arrays (one row per estimator sample). ``rpm`` may be None.

    ``accel_lat`` is None when nothing measured it (a drive with no IMU): cornering events and
    lateral jerk are then left out, never reported as zero.
    """
    t = as_times(t_ns)
    n = t.size
    v = as_values(speed_mps, n, "speed_mps")
    ax = as_values(accel_long, n, "accel_long")
    ay = None if accel_lat is None else as_values(accel_lat, n, "accel_lat")
    r = None if rpm is None else as_values(rpm, n, "rpm")
    b = cfg.section("behavior")
    window = float(b["window_s"])
    min_speed = float(b["min_speed_mps"])
    stale = int(cfg.get("rules.shared.stale_after_polls"))
    corner_g = [float(x) for x in b["corner_g"]]
    brake_tiers = [
        _Tier("hard_brake", "behavior.hard_brake", float(b["hard_brake_g"]), "Hard braking"),
        _Tier("severe_brake", "behavior.hard_brake", float(b["severe_brake_g"]), "Severe braking"),
    ]
    accel_tiers = [
        _Tier("hard_accel", "behavior.hard_accel", float(b["hard_accel_g"]), "Hard acceleration")
    ]
    corner_tiers = [
        _Tier("corner", "behavior.corner", corner_g[0], "Cornering"),
        _Tier("corner_high", "behavior.corner", corner_g[1], "Hard cornering"),
    ]

    events: list[Event] = []
    jl: list[FloatArray] = []
    jt: list[FloatArray] = []
    for seg in segments(t, stale):
        ts_int = t[seg]
        if ts_int.size < 2:
            continue
        t_ref = int(ts_int[0])
        ts = seconds(ts_int, t_ref)
        moving = v[seg] >= min_speed - NUM_EPS
        m_long, ok = trailing_mean(ts, ax[seg], window)
        gate = ok & moving
        events += _threshold_events(ts_int, ts, t_ref, -m_long, gate, window, brake_tiers, -1)
        events += _threshold_events(ts_int, ts, t_ref, m_long, gate, window, accel_tiers, 1)
        jl.append(_jerk(ts, ax[seg], window, moving))
        if ay is not None:
            m_lat, _ = trailing_mean(ts, ay[seg], window)
            events += _threshold_events(
                ts_int, ts, t_ref, np.abs(m_lat), gate, window, corner_tiers, 0, signed=m_lat
            )
            jt.append(_jerk(ts, ay[seg], window, moving))

    j_long = np.abs(np.concatenate(jl)) if jl else np.zeros(0)
    j_lat = np.abs(np.concatenate(jt)) if jt else np.zeros(0)

    bands: list[tuple[float, float, float]] = []
    shifts: list[Shift] = []
    ratios: list[float] = []
    note: str | None = None
    if r is not None:
        bands = _rpm_bands(t, r, vehicle)
        ratios = _gear_ratios(t, v, r, cfg, stale)
        if not _plausible_gears(ratios, v, r, cfg, vehicle):
            ratios = []
        if len(ratios) < 2:
            note = NO_GEARS_NOTE
        else:
            shift_events, shifts = _shifts(t, v, r, ratios, cfg, stale, rpm_signal)
            events += shift_events

    events.sort(key=lambda e: (e.t_start_ns, e.kind))
    return BehaviorReport(
        events=events,
        jerk_long_mean=_mean(j_long),
        jerk_long_p95=_p95(j_long),
        jerk_lat_mean=_mean(j_lat),
        jerk_lat_p95=_p95(j_lat),
        rpm_band_s=bands,
        shifts=shifts,
        gear_ratios=ratios,
        gear_note=note,
    )


def _mean(x: FloatArray) -> float | None:
    return float(np.mean(x)) if x.size else None


def _p95(x: FloatArray) -> float | None:
    return float(np.percentile(x, 95)) if x.size else None


def _threshold_events(
    t: IntArray,
    ts: FloatArray,
    t_ref: int,
    level_mps2: FloatArray,
    gate: BoolArray,
    window: float,
    tiers: list[_Tier],
    sign: int,
    *,
    signed: FloatArray | None = None,
) -> list[Event]:
    """One event per run where the window mean reaches the lowest tier.

    ``level_mps2`` is the magnitude compared with the tiers (decel for braking). The event's
    tier is the highest one its peak reaches. ``sign`` picks the citation stat: -1 cites the
    most negative mean, 1 the largest, 0 the largest absolute value.
    """
    lowest = tiers[0].threshold_g
    level_g = np.where(gate, level_mps2 / G0, -np.inf)
    hit = level_g >= lowest - NUM_EPS
    out: list[Event] = []
    for a, b in runs(hit):
        i_peak = a + int(np.argmax(level_g[a : b + 1]))
        peak_g = float(level_g[i_peak])
        tier = tiers[0]
        for cand in tiers:
            if peak_g >= cand.threshold_g - NUM_EPS:
                tier = cand
        lo_ns = to_ns(t_ref, float(ts[a]) - window)
        hi_ns = int(t[b])
        if hi_ns <= lo_ns:
            hi_ns = lo_ns + 1
        if sign < 0:
            stat, stat_value = "min_window_mean", -peak_g * G0
        elif sign > 0:
            stat, stat_value = "max_window_mean", peak_g * G0
        else:
            stat = "max_abs_window_mean"
            stat_value = float(signed[i_peak]) if signed is not None else peak_g * G0
        signal = ACCEL_LAT if sign == 0 else ACCEL_LONG
        cite = Citation(
            signal=signal,
            source=label(signal),
            role="trigger",
            t_start_ns=lo_ns,
            t_end_ns=hi_ns,
            stat=stat,
            stat_value=stat_value,
            unit="m/s^2",
            n=count_in(t, lo_ns, hi_ns),
        )
        out.append(
            Event(
                kind=tier.kind,
                rule_id=tier.rule_id,
                rule_version=RULE_VERSION,
                severity=0,
                t_start_ns=lo_ns,
                t_end_ns=hi_ns,
                value=peak_g,
                threshold=tier.threshold_g,
                unit="g",
                message=f"{tier.title} {peak_g:.2f} g.",
                citations=(cite,),
            )
        )
    return out


def _jerk(ts: FloatArray, a: FloatArray, window: float, moving: BoolArray) -> FloatArray:
    """Jerk (m/s^3) of the centered window mean of ``a``, on moving samples with full windows."""
    if ts.size < 3:
        return np.zeros(0)
    smooth, ok = centered_mean(ts, a, window)
    keep = ok & moving
    if int(np.count_nonzero(ok)) < 3:
        return np.zeros(0)
    j = np.full(ts.size, np.nan)
    idx = np.flatnonzero(ok)
    j[idx] = np.gradient(smooth[idx], ts[idx])
    # The first and last valid samples use one-sided differences; drop them.
    edge = np.zeros(ts.size, dtype=np.bool_)
    edge[idx[0]] = True
    edge[idx[-1]] = True
    keep &= ~edge
    out: FloatArray = j[keep]
    return out


def _rpm_bands(t: IntArray, rpm: FloatArray, vehicle: Config) -> list[tuple[float, float, float]]:
    """Seconds spent in each RPM band from the vehicle profile.

    Each sample holds its RPM until the next sample. Values below the first edge count in the
    first band and values at or above the last edge count in the last band, so the seconds of
    samples with an RPM value sum to their duration.
    """
    try:
        edges = [float(e) for e in vehicle.get("rpm.bands")]
    except ConfigError:
        return []
    if len(edges) < 2 or t.size < 2:
        return []
    dt_ns = np.diff(t)
    r = rpm[:-1]
    known = np.isfinite(r)
    idx = np.searchsorted(np.asarray(edges), r[known], side="right") - 1
    idx = np.clip(idx, 0, len(edges) - 2)
    ns = np.zeros(len(edges) - 1, dtype=np.int64)
    np.add.at(ns, idx, dt_ns[known])
    return [(edges[i], edges[i + 1], int(ns[i]) / 1e9) for i in range(len(edges) - 1)]


def _ratio(v: FloatArray, rpm: FloatArray, min_speed: float) -> tuple[FloatArray, BoolArray]:
    ok = np.isfinite(rpm) & (rpm > 0.0) & (v >= min_speed - NUM_EPS)
    k = np.full(v.size, np.nan)
    k[ok] = v[ok] / rpm[ok]
    return k, ok


def _in_band(k: FloatArray, center: float, tol: float) -> BoolArray:
    with np.errstate(invalid="ignore"):
        out: BoolArray = np.abs(k / center - 1.0) <= tol + NUM_EPS
    return out


def _gear_ratios(
    t: IntArray, v: FloatArray, rpm: FloatArray, cfg: Config, stale: int
) -> list[float]:
    b = cfg.section("behavior")
    tol = float(b["shift_ratio_tol"])
    dwell_ns = round(float(b["shift_dwell_s"]) * 1e9)
    min_speed = float(b["min_speed_mps"])
    k, ok = _ratio(v, rpm, min_speed)
    if int(np.count_nonzero(ok)) < 2:
        return []
    logk = np.log(k[ok])
    width = math.log1p(tol)
    lo, hi = float(np.min(logk)), float(np.max(logk))
    nbins = max(math.ceil((hi - lo) / width), 1)
    counts, edges = np.histogram(logk, bins=nbins, range=(lo, lo + nbins * width))
    padded = np.concatenate(([0], counts, [0]))
    peaks = [
        i
        for i in range(nbins)
        if counts[i] > 0 and counts[i] >= padded[i] and counts[i] >= padded[i + 2]
    ]
    peaks.sort(key=lambda i: -int(counts[i]))
    seg_id = np.zeros(t.size, dtype=np.int64)
    for j, seg in enumerate(segments(t, stale)):
        seg_id[seg] = j
    accepted: list[float] = []
    band_ratio = (1.0 + tol) / (1.0 - tol)
    for i in peaks:
        center = math.exp(0.5 * (edges[i] + edges[i + 1]))
        for _ in range(3):
            members = k[ok][_in_band(k[ok], center, tol)]
            if members.size == 0:
                break
            center = float(np.median(members))
        if any(abs(center / c - 1.0) <= 2.0 * tol for c in accepted):
            continue
        inside = ok & _in_band(k, center, tol)
        for a, z in runs(inside):
            if seg_id[a] != seg_id[z] or int(t[z] - t[a]) < dwell_ns:
                continue
            rr = rpm[a : z + 1]
            if float(np.max(rr)) / float(np.min(rr)) > band_ratio:
                accepted.append(center)
                break
    return sorted(accepted)


def max_gears(vehicle: Config) -> int:
    """The profile's ``vehicle.max_gears``, or ``UNKNOWN_MAX_GEARS`` without one."""
    try:
        value = vehicle.get("vehicle.max_gears")
    except ConfigError:
        return UNKNOWN_MAX_GEARS
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(f"vehicle.max_gears must be a whole number of 1 or more, not {value!r}")
    return value


def _plausible_gears(
    ratios: list[float], v: FloatArray, rpm: FloatArray, cfg: Config, vehicle: Config
) -> bool:
    """No more clusters than the gearbox can have, holding enough of the drive."""
    if len(ratios) > max_gears(vehicle):
        return False
    if not ratios:
        return True
    tol = float(cfg.get("behavior.shift_ratio_tol"))
    k, ok = _ratio(v, rpm, float(cfg.get("behavior.min_speed_mps")))
    n_ok = int(np.count_nonzero(ok))
    if n_ok == 0:
        return True
    inside = np.zeros(v.size, dtype=np.bool_)
    for c in ratios:
        inside |= ok & _in_band(k, c, tol)
    return int(np.count_nonzero(inside)) >= MIN_GEAR_COVERAGE * n_ok


def _shifts(
    t: IntArray,
    v: FloatArray,
    rpm: FloatArray,
    ratios: list[float],
    cfg: Config,
    stale: int,
    rpm_signal: str,
) -> tuple[list[Event], list[Shift]]:
    b = cfg.section("behavior")
    tol = float(b["shift_ratio_tol"])
    dwell_ns = round(float(b["shift_dwell_s"]) * 1e9)
    lookback_ns = round(float(b["shift_lookback_s"]) * 1e9)
    min_speed = float(b["min_speed_mps"])
    k, ok = _ratio(v, rpm, min_speed)
    gear = np.zeros(t.size, dtype=np.int64)
    for g, c in enumerate(ratios, start=1):
        gear[ok & _in_band(k, c, tol)] = g
    events: list[Event] = []
    shifts: list[Shift] = []
    for seg in segments(t, stale):
        g_seg = gear[seg]
        t_seg = t[seg]
        spans: list[tuple[int, int, int]] = []  # (gear, first index, last index)
        for g in range(1, len(ratios) + 1):
            for a, z in runs(g_seg == g):
                if int(t_seg[z] - t_seg[a]) >= dwell_ns:
                    spans.append((g, a, z))
        spans.sort(key=lambda s: s[1])
        for (g0, _a0, z0), (g1, a1, _z1) in pairwise(spans):
            if g0 == g1:
                continue
            t_shift = int(t_seg[z0])
            lo_ns = t_shift - lookback_ns
            lo = int(np.searchsorted(t_seg, lo_ns, side="left"))
            window_rpm = rpm[seg][lo : z0 + 1]
            window_rpm = window_rpm[np.isfinite(window_rpm)]
            if window_rpm.size == 0:
                continue
            peak = float(np.max(window_rpm))
            hi_ns = int(t_seg[a1])
            shifts.append(Shift(t_shift, peak, g0, g1))
            cite = Citation(
                signal=rpm_signal,
                source=label(rpm_signal),
                role="trigger",
                t_start_ns=lo_ns,
                t_end_ns=t_shift if t_shift > lo_ns else lo_ns + 1,
                stat="max",
                stat_value=peak,
                unit="rpm",
                n=int(window_rpm.size),
            )
            events.append(
                Event(
                    kind="behavior.shift",
                    rule_id="shift",
                    rule_version=RULE_VERSION,
                    severity=0,
                    t_start_ns=lo_ns,
                    t_end_ns=max(hi_ns, lo_ns + 1),
                    value=peak,
                    threshold=None,
                    unit="rpm",
                    message=f"Shift {g0} to {g1} at {peak:,.0f} rpm.",
                    citations=(cite,),
                )
            )
    return events, shifts
