"""Performance runs: 0 to 60 mph, 60 to 0 mph braking distance, and peak lateral g.

Arrays in, ``Event`` records out. Speeds are the fused ``state.speed``; lateral acceleration
is ``state.accel_lat`` (v times yaw rate). Every event cites its evidence with a ``trigger``
citation on the signal it was measured from.

``zero_to_sixty`` and ``sixty_to_zero`` cite ``signal`` (default ``state.speed``); a drive with
no IMU runs them on a measured speed signal instead.

Estimator quality reaches these functions through two optional arrays aligned with the
samples: ``var_speed`` (a run is suppressed when it exceeds ``estimator.max_speed_var``) and
``dr`` (samples where GPS was missing; a kept run cites the missing seconds on ``gps.speed``).
A gap longer than ``rules.shared.stale_after_polls`` sample periods splits the series, so a
run never spans missing data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike

from photoncar.config import Config
from photoncar.detect._series import (
    BoolArray,
    FloatArray,
    IntArray,
    as_times,
    as_values,
    centered_mean,
    count_in,
    crossing_time,
    linear_fit,
    median3,
    seconds,
    segments,
    to_ns,
)
from photoncar.records import Citation, Event
from photoncar.signals import label
from photoncar.units import FOOT, G0, MPH_60

RULE_VERSION = 1

FIT_WINDOW_S = 0.3
"""Length of the linear fit that extrapolates a run's start (or end) to v = 0.

From the Phase 1 research note ("a linear fit of the first 0.3 s"). Not yet a config key.
"""

SPEED = "state.speed"
ACCEL_LAT = "state.accel_lat"
GPS_SPEED = "gps.speed"


@dataclass(frozen=True, slots=True)
class _Quality:
    var_speed: FloatArray | None
    dr: BoolArray | None
    max_var: float


def _quality(n: int, var_speed: ArrayLike | None, dr: ArrayLike | None, cfg: Config) -> _Quality:
    var = None if var_speed is None else as_values(var_speed, n, "var_speed")
    d = None
    if dr is not None:
        d = np.asarray(dr, dtype=np.bool_)
        if d.shape != (n,):
            raise ValueError(f"dr must have shape ({n},), got {d.shape}")
    return _Quality(var, d, float(cfg.get("estimator.max_speed_var")))


def _run_ok(q: _Quality, lo: int, hi: int) -> bool:
    """False when the speed variance passes the limit anywhere in samples lo..hi."""
    if q.var_speed is None:
        return True
    return float(np.max(q.var_speed[lo : hi + 1])) <= q.max_var


def _missing_gps_s(q: _Quality, t: IntArray, lo: int, hi: int) -> float:
    """Seconds of samples lo..hi that were dead-reckoned (GPS missing)."""
    if q.dr is None or hi <= lo:
        return 0.0
    dt = np.diff(t[lo : hi + 1]).astype(np.float64) / 1e9
    return float(np.sum(dt[q.dr[lo:hi]]))


def _gps_citation(t0_ns: int, t1_ns: int, missing_s: float, n: int) -> Citation:
    return Citation(
        signal=GPS_SPEED,
        source=label(GPS_SPEED),
        role="context",
        t_start_ns=t0_ns,
        t_end_ns=t1_ns,
        stat="missing_s",
        stat_value=missing_s,
        unit="s",
        n=n,
    )


def _distance_to(pts_t: list[float], pts_v: list[float], target_m: float) -> float | None:
    """Time at which the distance along a piecewise-linear speed path reaches ``target_m``.

    Each piece has linear speed, so its distance is quadratic in time and solved exactly.
    """
    remaining = target_m
    for i in range(len(pts_t) - 1):
        ta, tb = pts_t[i], pts_t[i + 1]
        va, vb = pts_v[i], pts_v[i + 1]
        h = tb - ta
        if h <= 0.0:
            continue
        piece = 0.5 * (va + vb) * h
        if piece < remaining:
            remaining -= piece
            continue
        alpha = (vb - va) / h
        disc = va * va + 2.0 * alpha * remaining
        s = 2.0 * remaining / (va + math.sqrt(max(disc, 0.0)))
        return ta + s
    return None


def zero_to_sixty(
    t_ns: ArrayLike,
    speed_mps: ArrayLike,
    cfg: Config,
    *,
    var_speed: ArrayLike | None = None,
    dr: ArrayLike | None = None,
    signal: str = SPEED,
) -> list[Event]:
    """0 to 60 mph runs. ``value`` is seconds from standstill.

    Arms after ``performance.arm_stationary_s`` below ``performance.start_speed_mps``. The
    start t0 is the upward crossing of the start speed, extrapolated to v = 0 with a linear
    fit over the next ``FIT_WINDOW_S``. The end is the interpolated crossing of 60 mph. The
    1 ft rollout time (60 mph time minus the time to cover 0.3048 m from t0) is a context
    citation with ``stat="rollout_1ft_s"``.
    """
    t = as_times(t_ns)
    v = as_values(speed_mps, t.size, "speed_mps")
    q = _quality(t.size, var_speed, dr, cfg)
    perf = cfg.section("performance")
    v_start = float(perf["start_speed_mps"])
    arm_s = float(perf["arm_stationary_s"])
    stale = int(cfg.get("rules.shared.stale_after_polls"))
    events: list[Event] = []
    for seg in segments(t, stale):
        base = seg.start or 0
        events.extend(_zero_to_sixty_segment(t[seg], v[seg], base, t, q, v_start, arm_s, signal))
    return events


def _zero_to_sixty_segment(
    t: IntArray,
    v: FloatArray,
    base: int,
    t_all: IntArray,
    q: _Quality,
    v_start: float,
    arm_s: float,
    signal: str,
) -> list[Event]:
    events: list[Event] = []
    n = t.size
    if n < 2:
        return events
    t_ref = int(t[0])
    ts = seconds(t, t_ref)
    below = v < v_start
    k = 1
    while k < n:
        if not (below[k - 1] and not below[k]):
            k += 1
            continue
        # k is the first sample at or above the start speed after a run below it.
        rest_first = k - 1
        while rest_first > 0 and below[rest_first - 1]:
            rest_first -= 1
        t_c = crossing_time(ts[k - 1], v[k - 1], ts[k], v[k], v_start)
        # Find the 60 mph crossing, aborting if the car falls back below the start speed.
        p = k
        while p < n and v[p] < MPH_60 and not below[p]:
            p += 1
        if p >= n or below[p]:
            k = max(p, k + 1)
            continue
        fit_hi = int(np.searchsorted(ts, t_c + FIT_WINDOW_S, side="right"))
        fit_idx = list(range(k, max(fit_hi, k + 1)))
        if len(fit_idx) < 2:
            fit_idx = [k - 1, k]
        slope, icpt = linear_fit(ts[fit_idx], v[fit_idx])
        if slope <= 0.0:
            k = p + 1
            continue
        t0 = min(-icpt / slope, t_c)
        armed = t0 - ts[rest_first] >= arm_s - 1e-12
        t60 = crossing_time(ts[p - 1], v[p - 1], ts[p], v[p], MPH_60)
        if not armed or not _run_ok(q, base + k - 1, base + p):
            k = p + 1
            continue
        pts_t = [t0, t_c, *ts[k:p].tolist(), t60]
        pts_v = [0.0, v_start, *v[k:p].tolist(), MPH_60]
        t_roll = _distance_to(pts_t, pts_v, FOOT)
        if t_roll is None:
            k = p + 1
            continue
        from_standstill = float(t60 - t0)
        rollout = float(t60 - t_roll)
        t0_ns, t60_ns, troll_ns = to_ns(t_ref, t0), to_ns(t_ref, t60), to_ns(t_ref, t_roll)
        cites = [
            Citation(
                signal=signal,
                source=label(signal),
                role="trigger",
                t_start_ns=t0_ns,
                t_end_ns=t60_ns,
                stat="from_standstill_s",
                stat_value=from_standstill,
                unit="s",
                n=count_in(t, t0_ns, t60_ns),
            ),
            Citation(
                signal=signal,
                source=label(signal),
                role="context",
                t_start_ns=troll_ns,
                t_end_ns=t60_ns,
                stat="rollout_1ft_s",
                stat_value=rollout,
                unit="s",
                n=count_in(t, troll_ns, t60_ns),
            ),
        ]
        missing = _missing_gps_s(q, t_all, base + k - 1, base + p)
        if missing > 0.0:
            cites.append(_gps_citation(t0_ns, t60_ns, missing, count_in(t, t0_ns, t60_ns)))
        events.append(
            Event(
                kind="perf.zero_to_sixty",
                rule_id="zero_to_sixty",
                rule_version=RULE_VERSION,
                severity=0,
                t_start_ns=t0_ns,
                t_end_ns=t60_ns,
                value=from_standstill,
                threshold=None,
                unit="s",
                message=(
                    f"0 to 60 mph in {from_standstill:.2f} s from standstill, "
                    f"{rollout:.2f} s with 1 ft rollout."
                ),
                citations=tuple(cites),
            )
        )
        k = p + 1
    return events


def sixty_to_zero(
    t_ns: ArrayLike,
    speed_mps: ArrayLike,
    cfg: Config,
    *,
    var_speed: ArrayLike | None = None,
    dr: ArrayLike | None = None,
    signal: str = SPEED,
) -> list[Event]:
    """60 to 0 mph braking runs. ``value`` is meters.

    Starts at the interpolated downward crossing of 60 mph. The end is the downward crossing
    of ``performance.start_speed_mps`` extrapolated to v = 0 with a linear fit over the
    preceding ``FIT_WINDOW_S``. Distance is the trapezoid integral of speed. A run counts
    only if its mean deceleration is at least ``performance.brake_min_decel_g``.
    """
    t = as_times(t_ns)
    v = as_values(speed_mps, t.size, "speed_mps")
    q = _quality(t.size, var_speed, dr, cfg)
    perf = cfg.section("performance")
    v_stop = float(perf["start_speed_mps"])
    min_decel = float(perf["brake_min_decel_g"]) * G0
    stale = int(cfg.get("rules.shared.stale_after_polls"))
    events: list[Event] = []
    for seg in segments(t, stale):
        base = seg.start or 0
        events.extend(_sixty_to_zero_segment(t[seg], v[seg], base, t, q, v_stop, min_decel, signal))
    return events


def _sixty_to_zero_segment(
    t: IntArray,
    v: FloatArray,
    base: int,
    t_all: IntArray,
    q: _Quality,
    v_stop: float,
    min_decel: float,
    signal: str,
) -> list[Event]:
    events: list[Event] = []
    n = t.size
    if n < 2:
        return events
    t_ref = int(t[0])
    ts = seconds(t, t_ref)
    k = 1
    while k < n:
        if not (v[k - 1] >= MPH_60 > v[k]):
            k += 1
            continue
        t_c = crossing_time(ts[k - 1], v[k - 1], ts[k], v[k], MPH_60)
        qi = k
        while qi < n and v[qi] >= v_stop and v[qi] < MPH_60:
            qi += 1
        if qi >= n or v[qi] >= MPH_60:
            k = max(qi, k + 1)
            continue
        # qi is the first sample below the stop speed; the crossing lies in (qi - 1, qi].
        t_x = crossing_time(ts[qi - 1], v[qi - 1], ts[qi], v[qi], v_stop)
        fit_lo = int(np.searchsorted(ts, t_x - FIT_WINDOW_S, side="left"))
        fit_idx = list(range(max(fit_lo, k), qi))
        if len(fit_idx) < 2:
            fit_idx = [qi - 1, qi]
        slope, icpt = linear_fit(ts[fit_idx], v[fit_idx])
        if slope >= 0.0:
            k = qi + 1
            continue
        t_end = float(max(-icpt / slope, t_x))
        mean_decel = float(MPH_60 / (t_end - t_c))
        if mean_decel < min_decel - 1e-12 or not _run_ok(q, base + k - 1, base + qi):
            k = qi + 1
            continue
        pts_t = np.array([t_c, *ts[k:qi].tolist(), t_x])
        pts_v = np.array([MPH_60, *v[k:qi].tolist(), v_stop])
        dist = float(np.sum(0.5 * (pts_v[1:] + pts_v[:-1]) * np.diff(pts_t)))
        dist += 0.5 * v_stop * (t_end - t_x)
        tc_ns, tend_ns = to_ns(t_ref, t_c), to_ns(t_ref, t_end)
        cites = [
            Citation(
                signal=signal,
                source=label(signal),
                role="trigger",
                t_start_ns=tc_ns,
                t_end_ns=tend_ns,
                stat="distance_m",
                stat_value=dist,
                unit="m",
                n=count_in(t, tc_ns, tend_ns),
            ),
            Citation(
                signal=signal,
                source=label(signal),
                role="context",
                t_start_ns=tc_ns,
                t_end_ns=tend_ns,
                stat="mean_decel_g",
                stat_value=mean_decel / G0,
                unit="g",
                n=count_in(t, tc_ns, tend_ns),
            ),
        ]
        missing = _missing_gps_s(q, t_all, base + k - 1, base + qi)
        if missing > 0.0:
            cites.append(_gps_citation(tc_ns, tend_ns, missing, count_in(t, tc_ns, tend_ns)))
        events.append(
            Event(
                kind="perf.sixty_to_zero",
                rule_id="sixty_to_zero",
                rule_version=RULE_VERSION,
                severity=0,
                t_start_ns=tc_ns,
                t_end_ns=tend_ns,
                value=dist,
                threshold=None,
                unit="m",
                message=f"60 to 0 mph in {dist:.1f} m.",
                citations=tuple(cites),
            )
        )
        k = qi + 1
    return events


def peak_lateral(t_ns: ArrayLike, accel_lat_mps2: ArrayLike, cfg: Config) -> Event | None:
    """Peak lateral acceleration in g.

    A centered 3-sample median removes isolated single-sample spikes, then a centered
    ``performance.lateral_window_s`` moving average smooths the series; the peak is the
    largest absolute value of that average. The sustained peak (the highest level the
    average held for a full window) is a context citation.
    """
    t = as_times(t_ns)
    a = as_values(accel_lat_mps2, t.size, "accel_lat_mps2")
    width = float(cfg.get("performance.lateral_window_s"))
    stale = int(cfg.get("rules.shared.stale_after_polls"))
    best: tuple[float, int, int, int] | None = None  # (abs mean, seg start, index, t_ref)
    sustained: tuple[float, int, int] | None = None  # (level, t_start_ns, t_end_ns)
    for seg in segments(t, stale):
        ts_int = t[seg]
        if ts_int.size < 3:
            continue
        t_ref = int(ts_int[0])
        ts = seconds(ts_int, t_ref)
        mean, valid = centered_mean(ts, median3(a[seg]), width)
        if not bool(np.any(valid)):
            continue
        mag = np.where(valid, np.abs(mean), -np.inf)
        i = int(np.argmax(mag))
        if best is None or mag[i] > best[0]:
            best = (float(mag[i]), seg.start or 0, i, t_ref)
        held = _sustained(ts, mag, width)
        if held is not None and (sustained is None or held[0] > sustained[0]):
            sustained = (held[0], to_ns(t_ref, held[1]), to_ns(t_ref, held[2]))
    if best is None:
        return None
    peak, start, i, t_ref = best
    t_peak = seconds(t[start + i : start + i + 1], t_ref)[0]
    lo_ns = to_ns(t_ref, t_peak - width / 2.0)
    hi_ns = to_ns(t_ref, t_peak + width / 2.0)
    cites = [
        Citation(
            signal=ACCEL_LAT,
            source=label(ACCEL_LAT),
            role="trigger",
            t_start_ns=lo_ns,
            t_end_ns=hi_ns,
            stat="peak_abs_mean",
            stat_value=peak,
            unit="m/s^2",
            n=count_in(t, lo_ns, hi_ns),
        )
    ]
    if sustained is not None:
        cites.append(
            Citation(
                signal=ACCEL_LAT,
                source=label(ACCEL_LAT),
                role="context",
                t_start_ns=sustained[1],
                t_end_ns=sustained[2],
                stat="sustained_abs_mean",
                stat_value=sustained[0],
                unit="m/s^2",
                n=count_in(t, sustained[1], sustained[2]),
            )
        )
    g = peak / G0
    return Event(
        kind="perf.peak_lateral",
        rule_id="peak_lateral",
        rule_version=RULE_VERSION,
        severity=0,
        t_start_ns=lo_ns,
        t_end_ns=hi_ns,
        value=g,
        threshold=None,
        unit="g",
        message=f"Peak lateral {g:.2f} g.",
        citations=tuple(cites),
    )


def _sustained(ts: FloatArray, mag: FloatArray, width: float) -> tuple[float, float, float] | None:
    """The largest level L such that ``mag >= L`` over some full window of ``width`` seconds.

    Sliding-window minimum with a monotonic queue over time-based windows ``[t_i, t_i + w]``.
    Returns ``(level, t_start, t_end)`` or None when no full window exists.
    """
    n = ts.size
    best: tuple[float, float, float] | None = None
    dq: list[int] = []
    head = 0
    j = 0
    for i in range(n):
        if ts[i] + width > ts[-1] + 1e-12:
            break
        while j < n and ts[j] <= ts[i] + width + 1e-12:
            while len(dq) > head and mag[dq[-1]] >= mag[j]:
                dq.pop()
            dq.append(j)
            j += 1
        while dq[head] < i:
            head += 1
        level = float(mag[dq[head]])
        if math.isfinite(level) and (best is None or level > best[0]):
            best = (level, float(ts[i]), float(ts[i] + width))
    return best
