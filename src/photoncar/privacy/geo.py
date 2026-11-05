"""Geodesy helpers: great-circle distance and a local east/north plane.

Distances for the privacy zone use the haversine formula on a sphere of the IUGG mean Earth
radius (6,371,008.8 m). Against the WGS84 ellipsoid it reads up to 0.56% long (north-south near
the equator, where the meridian radius is a(1 - e^2) = 6,335,439 m) and up to 0.45% short
(near the poles). ``SPHERE_ERROR_BOUND`` rounds that up.

Local x/y use the WGS84 ENU tangent plane at an origin fix: geodetic to ECEF, then rotated into
east (x) and north (y). Heights are taken as 0, so x/y are horizontal meters.
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import ArrayLike, NDArray

EARTH_MEAN_RADIUS_M = 6_371_008.8
WGS84_A_M = 6_378_137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)
SPHERE_ERROR_BOUND = 0.006  # relative, haversine vs the WGS84 ellipsoid


def haversine_m(lat1_deg: float, lon1_deg: float, lat2_deg: float, lon2_deg: float) -> float:
    """Great-circle distance in meters between 2 points given in degrees."""
    p1 = math.radians(lat1_deg)
    p2 = math.radians(lat2_deg)
    dp = p2 - p1
    dl = math.radians(lon2_deg - lon1_deg)
    h = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * EARTH_MEAN_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def haversine_m_array(
    lat_deg: ArrayLike, lon_deg: ArrayLike, lat0_deg: float, lon0_deg: float
) -> NDArray[np.float64]:
    """Vectorized haversine distance from each point to 1 reference point."""
    p1 = np.radians(np.asarray(lat_deg, dtype=np.float64))
    lam1 = np.radians(np.asarray(lon_deg, dtype=np.float64))
    p0 = math.radians(lat0_deg)
    lam0 = math.radians(lon0_deg)
    h = np.sin((p1 - p0) / 2.0) ** 2 + math.cos(p0) * np.cos(p1) * np.sin((lam1 - lam0) / 2.0) ** 2
    out: NDArray[np.float64] = 2.0 * EARTH_MEAN_RADIUS_M * np.arcsin(np.minimum(1.0, np.sqrt(h)))
    return out


def _ecef(
    lat_rad: NDArray[np.float64], lon_rad: NDArray[np.float64]
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    sin_lat = np.sin(lat_rad)
    n = WGS84_A_M / np.sqrt(1.0 - WGS84_E2 * sin_lat**2)
    x = n * np.cos(lat_rad) * np.cos(lon_rad)
    y = n * np.cos(lat_rad) * np.sin(lon_rad)
    z = n * (1.0 - WGS84_E2) * sin_lat
    return x, y, z


def enu_m(
    lat_deg: ArrayLike, lon_deg: ArrayLike, lat0_deg: float, lon0_deg: float
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """East and north meters of each point in the WGS84 tangent plane at (lat0, lon0)."""
    lat = np.radians(np.atleast_1d(np.asarray(lat_deg, dtype=np.float64)))
    lon = np.radians(np.atleast_1d(np.asarray(lon_deg, dtype=np.float64)))
    p0 = np.array([math.radians(lat0_deg)])
    l0 = np.array([math.radians(lon0_deg)])
    x, y, z = _ecef(lat, lon)
    x0, y0, z0 = _ecef(p0, l0)
    dx, dy, dz = x - x0[0], y - y0[0], z - z0[0]
    sp, cp = math.sin(p0[0]), math.cos(p0[0])
    sl, cl = math.sin(l0[0]), math.cos(l0[0])
    east = -sl * dx + cl * dy
    north = -sp * cl * dx - sp * sl * dy + cp * dz
    return east, north
