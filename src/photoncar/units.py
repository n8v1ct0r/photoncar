"""Unit constants and conversions. Internal values are SI; convert only at UI and export edges."""

from __future__ import annotations

import math

G0 = 9.80665  # standard gravity, m/s^2
MPH = 0.44704  # m/s per mph (exact)
KPH = 1.0 / 3.6  # m/s per km/h
MPH_60 = 60.0 * MPH  # 26.8224 m/s
FOOT = 0.3048  # m (exact)
MILE = 1609.344  # m (exact)
KELVIN_OFFSET = 273.15
NS_PER_S = 1_000_000_000


def mps_to_mph(v: float) -> float:
    return v / MPH


def mph_to_mps(v: float) -> float:
    return v * MPH


def mps_to_kph(v: float) -> float:
    return v * 3.6


def kph_to_mps(v: float) -> float:
    return v / 3.6


def c_to_k(c: float) -> float:
    return c + KELVIN_OFFSET


def k_to_c(k: float) -> float:
    return k - KELVIN_OFFSET


def mps2_to_g(a: float) -> float:
    return a / G0


def g_to_mps2(g: float) -> float:
    return g * G0


def s_to_ns(s: float) -> int:
    return round(s * NS_PER_S)


def ns_to_s(ns: int) -> float:
    return ns / NS_PER_S


# Display text. The web formats the same values with ``milesText`` and ``durationText``
# (web/src/shared/format.ts); every display string the server builds matches them.


def _fixed(x: float, digits: int) -> str:
    """``x`` with ``digits`` decimals and comma thousands, never "-0.00" (the web's ``fixed``)."""
    if round(abs(x) * 10**digits) == 0:
        x = 0.0
    return f"{x:,.{digits}f}"


def miles_text(m: float) -> str:
    """A distance in miles: "11.27 mi", with 1 decimal from 100 mi up ("123.5 mi")."""
    mi = m / MILE
    return f"{_fixed(mi, 2 if mi < 100 else 1)} mi"


def duration_text(s: float) -> str:
    """A duration: "42 s", "15 min 0 s", or "1 h 3 min", from whole seconds (half up)."""
    whole = math.floor(s + 0.5)
    if whole < 60:
        return f"{whole} s"
    h, rest = divmod(whole, 3600)
    m, sec = divmod(rest, 60)
    return f"{h} h {m} min" if h > 0 else f"{m} min {sec} s"
