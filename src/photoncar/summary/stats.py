"""The only input the model summary sees: computed drive stats, with no GPS, dates, or IDs.

``summary_input`` passes exactly 5 fields per stat (``id``, ``label``, ``value``, ``unit``,
``display``), so nothing else a ``DriveStat`` might carry can leak. It then drops, as a second
line of defense, any stat that looks like location, a calendar date or time stamp, or an
identifier:

- location: an id token such as ``latitude``, ``longitude``, ``lng``, ``coord``, ``position``,
  ``location``, ``home``, or ``address``; or a ``lat`` / ``lon`` token on a stat measured in
  degrees (``jerk_lat`` in m/s^3 is lateral, not latitude, and stays); or a display or label
  with a number of 4 or more decimals, the precision of a coordinate;
- dates: an id token such as ``date``, ``timestamp``, ``utc``, ``epoch``, or ``weekday``; a
  time-stamp unit; or text shaped like a date (``2026-09-23``, ``9/23/2026``, ``Sep 23``);
- identifiers: an id token such as ``id``, ``vin``, ``uuid``, ``serial``, ``plate``, ``boot``,
  or ``dataset``; or text holding a UUID, a VIN-shaped string, or a long hex run.

Clock-shaped text (``1:23.4``) stays: lap times use it. Every dropped stat is logged by id and
reason only. A stat whose value is not finite is dropped too, since JSON cannot carry it.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Iterable
from typing import TypedDict

from photoncar.records import DriveStat

log = logging.getLogger(__name__)


class StatItem(TypedDict):
    """1 stat as the model sees it."""

    id: str
    label: str
    value: float
    unit: str
    display: str


_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

_LOCATION_TOKENS = frozenset(
    {
        "latitude",
        "longitude",
        "lng",
        "coord",
        "coords",
        "coordinate",
        "coordinates",
        "position",
        "location",
        "home",
        "address",
        "geohash",
    }
)
_LAT_LON_TOKENS = frozenset({"lat", "lon"})
_DEGREE_UNITS = frozenset({"deg", "degree", "degrees", "°", "deg_n", "deg_e"})
_DATE_TOKENS = frozenset(
    {"date", "datetime", "timestamp", "utc", "epoch", "weekday", "calendar", "wallclock"}
)
_DATE_UNITS = frozenset({"utc", "date", "datetime", "epoch_s", "epoch_ms", "epoch_ns", "iso8601"})
_ID_TOKENS = frozenset({"id", "ids", "vin", "uuid", "guid", "serial", "plate", "boot", "dataset"})

_PRECISE_NUMBER = re.compile(r"\d\.\d{4,}")
_ISO_DATE = re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b")
_SLASH_DATE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_MONTH_DATE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b",
    re.IGNORECASE,
)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_VIN_SHAPE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
_HEX_RUN = re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE)


def _tokens(stat_id: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN_SPLIT.split(stat_id.lower()) if t)


def _vin_like(text: str) -> bool:
    for m in _VIN_SHAPE.finditer(text.upper()):
        s = m.group(0)
        if any(c.isdigit() for c in s) and any(c.isalpha() for c in s):
            return True
    return False


def exclusion_reason(stat: DriveStat) -> str | None:
    """Why a stat must not reach the model, or None when it may."""
    tokens = _tokens(stat.stat_id)
    unit = stat.unit.strip().lower()
    text = f"{stat.label} {stat.display}"
    if not math.isfinite(stat.value):
        return "value is not finite"
    if tokens & _LOCATION_TOKENS:
        return "location"
    if tokens & _LAT_LON_TOKENS and unit in _DEGREE_UNITS:
        return "location"
    if _PRECISE_NUMBER.search(text):
        return "location (coordinate precision)"
    if tokens & _DATE_TOKENS or unit in _DATE_UNITS:
        return "date"
    if _ISO_DATE.search(text) or _SLASH_DATE.search(text) or _MONTH_DATE.search(text):
        return "date"
    if tokens & _ID_TOKENS:
        return "identifier"
    if _UUID.search(text) or _vin_like(text) or _HEX_RUN.search(text):
        return "identifier"
    return None


def summary_input(stats: Iterable[DriveStat]) -> list[StatItem]:
    """The stats the model may see, in their stored order."""
    out: list[StatItem] = []
    for s in stats:
        reason = exclusion_reason(s)
        if reason is not None:
            log.info("Stat %s left out of the summary input: %s.", s.stat_id, reason)
            continue
        out.append(
            StatItem(
                id=s.stat_id, label=s.label, value=float(s.value), unit=s.unit, display=s.display
            )
        )
    return out


def stats_json(items: Iterable[StatItem]) -> str:
    """The canonical JSON text of the stats, as sent to the model and stored in the cache."""
    return json.dumps(list(items), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
