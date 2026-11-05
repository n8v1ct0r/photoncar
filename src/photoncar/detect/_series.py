"""Array helpers shared by the detectors: validation, segments, crossings, and window means.

Times are ``t_ns`` int64 arrays. Arithmetic runs in float64 seconds relative to a reference
sample, so nanosecond timestamps far from zero keep full precision.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

import numpy as np
from numpy.typing import ArrayLike, NDArray

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]
BoolArray = NDArray[np.bool_]

NUM_EPS = 1e-9
"""Tolerance for threshold comparisons on computed means, in the threshold's own unit.

Window means built from cumulative sums carry rounding near 1e-12, so a series held exactly
at a threshold must still count as reaching it.
"""


EDGE_S = 1e-6
"""Window bounds widen by 1 us so a sample sitting on a bound (up to integer-ns rounding of
its timestamp) is included on both sides alike, which keeps centered windows symmetric."""


def as_times(t_ns: ArrayLike) -> IntArray:
    """Validate and return a 1-D, strictly increasing int64 time array."""
    t = np.asarray(t_ns, dtype=np.int64)
    if t.ndim != 1:
        raise ValueError("t_ns must be 1-D")
    if t.size > 1 and not bool(np.all(np.diff(t) > 0)):
        raise ValueError("t_ns must be strictly increasing")
    return t


def as_values(x: ArrayLike, n: int, name: str) -> FloatArray:
    """Validate and return a 1-D float64 array of length ``n``."""
    a = np.asarray(x, dtype=np.float64)
    if a.shape != (n,):
        raise ValueError(f"{name} must have shape ({n},), got {a.shape}")
    return a


def seconds(t: IntArray, t_ref: int) -> FloatArray:
    """Seconds since ``t_ref`` as float64."""
    return (t - np.int64(t_ref)).astype(np.float64) / 1e9


def to_ns(t_ref: int, s: float) -> int:
    """An absolute ``t_ns`` from seconds since ``t_ref``."""
    return int(t_ref) + round(s * 1e9)


def segments(t: IntArray, stale_polls: int) -> list[slice]:
    """Split a series where a gap exceeds ``stale_polls`` median sample periods.

    A detector never measures across such a gap: the samples inside it are missing, not
    constant.
    """
    n = t.size
    if n == 0:
        return []
    if n == 1:
        return [slice(0, 1)]
    dt = np.diff(t)
    limit = stale_polls * float(np.median(dt))
    cuts = np.flatnonzero(dt > limit) + 1
    bounds = [0, *cuts.tolist(), n]
    return [slice(a, b) for a, b in pairwise(bounds) if b > a]


def crossing_time(ta: float, va: float, tb: float, vb: float, level: float) -> float:
    """Linear interpolation of the time where v passes ``level`` between two samples."""
    if vb == va:
        return float(tb)
    return float(ta + (level - va) * (tb - ta) / (vb - va))


def count_in(t: IntArray, lo_ns: int, hi_ns: int) -> int:
    """Number of samples with ``lo_ns <= t <= hi_ns``."""
    lo = int(np.searchsorted(t, lo_ns, side="left"))
    hi = int(np.searchsorted(t, hi_ns, side="right"))
    return max(hi - lo, 0)


def window_mean(ts: FloatArray, x: FloatArray, lo: FloatArray, hi: FloatArray) -> FloatArray:
    """Mean of ``x`` over samples with ``lo[i] <= ts <= hi[i]`` for each i (closed windows)."""
    csum = np.concatenate(([0.0], np.cumsum(x)))
    a = np.searchsorted(ts, lo, side="left")
    b = np.searchsorted(ts, hi, side="right")
    n = b - a
    out = np.full(ts.size, np.nan)
    ok = n > 0
    out[ok] = (csum[b[ok]] - csum[a[ok]]) / n[ok]
    return out


def trailing_mean(ts: FloatArray, x: FloatArray, width_s: float) -> tuple[FloatArray, BoolArray]:
    """Mean over ``[t - width, t]`` and a mask of samples whose window lies inside the data."""
    mean = window_mean(ts, x, ts - width_s - EDGE_S, ts)
    valid = ts - width_s >= ts[0] - 1e-12 if ts.size else np.zeros(0, dtype=np.bool_)
    return mean, np.asarray(valid, dtype=np.bool_)


def centered_mean(ts: FloatArray, x: FloatArray, width_s: float) -> tuple[FloatArray, BoolArray]:
    """Mean over ``[t - width/2, t + width/2]`` and a mask of samples with a full window."""
    half = width_s / 2.0
    mean = window_mean(ts, x, ts - half - EDGE_S, ts + half + EDGE_S)
    if ts.size == 0:
        return mean, np.zeros(0, dtype=np.bool_)
    valid = (ts - half >= ts[0] - 1e-12) & (ts + half <= ts[-1] + 1e-12)
    return mean, np.asarray(valid, dtype=np.bool_)


def median3(x: FloatArray) -> FloatArray:
    """Centered 3-sample median. Removes isolated single-sample spikes; endpoints pass through."""
    if x.size < 3:
        return x.copy()
    out = x.copy()
    stacked = np.stack((x[:-2], x[1:-1], x[2:]))
    out[1:-1] = np.median(stacked, axis=0)
    return out


def runs(mask: BoolArray) -> list[tuple[int, int]]:
    """Inclusive index ranges ``(first, last)`` of each run of True values."""
    if mask.size == 0:
        return []
    m = np.concatenate(([False], mask.astype(np.bool_), [False]))
    d = np.diff(m.astype(np.int8))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1) - 1
    return [(int(a), int(b)) for a, b in zip(starts, ends, strict=True)]


def linear_fit(
    ts: Sequence[float] | FloatArray, vs: Sequence[float] | FloatArray
) -> tuple[float, float]:
    """Least-squares line ``v = slope * t + intercept`` computed about the mean time."""
    t = np.asarray(ts, dtype=np.float64)
    v = np.asarray(vs, dtype=np.float64)
    tm = float(np.mean(t))
    vm = float(np.mean(v))
    dt = t - tm
    den = float(np.dot(dt, dt))
    if den == 0.0:
        raise ValueError("linear fit needs at least 2 distinct times")
    slope = float(np.dot(dt, v - vm)) / den
    return slope, vm - slope * tm
