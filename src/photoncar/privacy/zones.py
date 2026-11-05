"""The home privacy zone and the time windows an export must drop (hard rule 7).

A trim window is a closed interval ``(t0_ns, t1_ns)`` of record time during which the vehicle
may have been within R of home. Every record of every stream inside a window is dropped.

How windows are found, from the GPS fixes alone:

- Between 2 consecutive fixes a and b, the vehicle travels a path of length at most
  ``l = max(d(a, b), V * dt)``, where V is ``MAX_SPEED_BOUND_MPS``. A point p on that path
  inside the zone needs ``d(a, h) + d(b, h) - 2R < l`` (triangle inequality). When that holds,
  the whole interval ``[t_a, t_b]`` is a window. A run of in-zone fixes therefore becomes a
  window reaching out to the neighbouring fixes on both sides, so no in-zone point survives
  interpolation, and a path that clips the zone between 2 outside fixes is caught as well.
- Before the first fix and after the last fix the position is unknown. Records there are kept
  only when the vehicle could not have reached the zone at speed V in the time available.
- With no usable fix at all, the whole drive is 1 window.

The radius used here is R padded by ``SPHERE_ERROR_BOUND`` (0.6%), so the guarantee holds for
ellipsoidal distances too, not only for the spherical haversine.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from photoncar.config import HomeZone
from photoncar.privacy.geo import SPHERE_ERROR_BOUND, haversine_m
from photoncar.records import GpsFix
from photoncar.units import NS_PER_S

__all__ = [
    "MAX_SPEED_BOUND_MPS",
    "T_MAX",
    "T_MIN",
    "Window",
    "haversine_m",
    "in_zone",
    "merge_windows",
    "trim_windows",
]

Window = tuple[int, int]

T_MIN = -(2**63)
T_MAX = 2**63 - 1

MAX_SPEED_BOUND_MPS = 100.0
"""Upper bound on vehicle speed used to bound travel between fixes (360 km/h).

Deliberately above any road car in scope, so the bound only ever trims more, never less.
"""

_SLACK_M = 1e-3  # rounding slack on the reachability test, in meters


def in_zone(lat_deg: float, lon_deg: float, zone: HomeZone) -> bool:
    """True when the point is within the zone radius of home (the boundary counts as inside)."""
    return haversine_m(lat_deg, lon_deg, zone.lat_deg, zone.lon_deg) <= zone.radius_m


def merge_windows(windows: Iterable[Window]) -> list[Window]:
    """Sort windows and merge any that overlap or touch. Empty windows (t1 < t0) are dropped."""
    ordered = sorted((max(T_MIN, a), min(T_MAX, b)) for a, b in windows if b >= a)
    merged: list[Window] = []
    for a, b in ordered:
        if merged and a <= merged[-1][1]:
            if b > merged[-1][1]:
                merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def _travel_ns(distance_m: float, speed_mps: float) -> int:
    """Time needed to cover ``distance_m`` at ``speed_mps``, rounded down (conservative)."""
    return math.floor(max(distance_m, 0.0) / speed_mps * NS_PER_S)


def trim_windows(
    gps_fixes: Iterable[GpsFix],
    zone: HomeZone,
    *,
    max_speed_mps: float = MAX_SPEED_BOUND_MPS,
) -> list[Window]:
    """Merged, sorted windows during which the vehicle may have been within R of home."""
    if max_speed_mps <= 0:
        raise ValueError("max_speed_mps must be positive")
    fixes = sorted(
        (f for f in gps_fixes if math.isfinite(f.lat_deg) and math.isfinite(f.lon_deg)),
        key=lambda f: f.t_ns,
    )
    if not fixes:
        return [(T_MIN, T_MAX)]
    r = zone.radius_m * (1.0 + SPHERE_ERROR_BOUND)
    dist = [haversine_m(f.lat_deg, f.lon_deg, zone.lat_deg, zone.lon_deg) for f in fixes]

    windows: list[Window] = []
    first, last = fixes[0], fixes[-1]
    windows.append((T_MIN, first.t_ns - _travel_ns(dist[0] - r, max_speed_mps)))
    for i in range(len(fixes) - 1):
        a, b = fixes[i], fixes[i + 1]
        dt_s = (b.t_ns - a.t_ns) / NS_PER_S
        path_m = max(haversine_m(a.lat_deg, a.lon_deg, b.lat_deg, b.lon_deg), max_speed_mps * dt_s)
        if dist[i] + dist[i + 1] - 2.0 * r <= path_m + _SLACK_M:
            windows.append((a.t_ns, b.t_ns))
    windows.append((last.t_ns + _travel_ns(dist[-1] - r, max_speed_mps), T_MAX))
    return merge_windows(windows)
