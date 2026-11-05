"""The drive report behind ``GET /api/drives/{id}/report``.

Everything here is read from what the drive file stores; nothing is recomputed.

- Counts come from events: ``behavior.hard_brake``, ``behavior.hard_accel``, ``behavior.corner``.
- Jerk stats and RPM bands come from drive stats: ``behavior.jerk_long_mean``,
  ``behavior.jerk_long_p95``, ``behavior.jerk_lat_mean``, ``behavior.jerk_lat_p95`` (m/s^3), and
  1 stat per band ``behavior.rpm_band.<lo>_<hi>`` (seconds). Missing stats read as null / empty.
- Shifts come from ``behavior.shift`` events: ``value`` is the rpm at the shift; the gears are
  the ``stat_value`` of citations whose ``stat`` is ``from_gear`` and ``to_gear`` (null if absent).
- Performance comes from ``perf.*`` events: 0 to 60 ``value`` in seconds with the rollout time in
  the citation stat ``rollout_1ft_s``; 60 to 0 ``value`` in meters; peak lateral ``value`` in g.
- Times are seconds from the drive start.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from photoncar.records import Event
from photoncar.store.reader import DriveReader
from photoncar.units import NS_PER_S

JERK_STATS: dict[str, str] = {
    "jerk_long_mean": "behavior.jerk_long_mean",
    "jerk_long_p95": "behavior.jerk_long_p95",
    "jerk_lat_mean": "behavior.jerk_lat_mean",
    "jerk_lat_p95": "behavior.jerk_lat_p95",
}
RPM_BAND_PREFIX = "behavior.rpm_band."
ROLLOUT_STAT = "rollout_1ft_s"
FROM_GEAR_STAT = "from_gear"
TO_GEAR_STAT = "to_gear"


def _cited(ev: Event, stat: str) -> float | None:
    for c in ev.citations:
        if c.stat == stat:
            return c.stat_value
    return None


def _gear(ev: Event, stat: str) -> int | None:
    v = _cited(ev, stat)
    return None if v is None else round(v)


def _band(stat_id: str) -> tuple[float, float]:
    lo, sep, hi = stat_id.removeprefix(RPM_BAND_PREFIX).partition("_")
    if not sep:
        raise ValueError(f"rpm band stat {stat_id!r} is not named {RPM_BAND_PREFIX}<lo>_<hi>")
    return float(lo), float(hi)


def drive_report(reader: DriveReader) -> dict[str, Any]:
    """``{behavior: BehaviorOut, performance: PerformanceOut, laps: [LapOut]}``."""
    meta = reader.meta()
    t0 = meta.t_start_ns if meta.t_start_ns is not None else 0

    def rel_s(t_ns: int) -> float:
        return (t_ns - t0) / NS_PER_S

    events = reader.events()
    stats = {s.stat_id: s for s in reader.stats()}
    kinds = Counter(e.kind for e in events)

    bands = sorted(
        ((*_band(sid), s.value) for sid, s in stats.items() if sid.startswith(RPM_BAND_PREFIX)),
        key=lambda b: (b[0], b[1]),
    )
    behavior: dict[str, Any] = {
        "hard_brakes": kinds["behavior.hard_brake"],
        "hard_accels": kinds["behavior.hard_accel"],
        "corners": kinds["behavior.corner"],
    }
    for key, stat_id in JERK_STATS.items():
        behavior[key] = stats[stat_id].value if stat_id in stats else None
    behavior["rpm_bands"] = [{"lo": lo, "hi": hi, "seconds": sec} for lo, hi, sec in bands]
    behavior["shifts"] = [
        {
            "t_s": rel_s(e.t_start_ns),
            "rpm": e.value,
            "from_gear": _gear(e, FROM_GEAR_STAT),
            "to_gear": _gear(e, TO_GEAR_STAT),
        }
        for e in events
        if e.kind == "behavior.shift"
    ]

    peaks = [e.value for e in events if e.kind == "perf.peak_lateral"]
    performance: dict[str, Any] = {
        "zero_to_sixty": [
            {
                "t_start_s": rel_s(e.t_start_ns),
                "seconds": e.value,
                "rollout_seconds": _cited(e, ROLLOUT_STAT),
            }
            for e in events
            if e.kind == "perf.zero_to_sixty"
        ],
        "sixty_to_zero": [
            {"t_start_s": rel_s(e.t_start_ns), "meters": e.value}
            for e in events
            if e.kind == "perf.sixty_to_zero"
        ],
        "peak_lateral_g": max(peaks) if peaks else None,
    }
    laps = [
        {
            "lap_no": lap.lap_no,
            "line_id": lap.line_id,
            "t_start_s": rel_s(lap.t_start_ns),
            "duration_s": lap.duration_ns / NS_PER_S,
            "valid": lap.valid,
        }
        for lap in reader.laps()
    ]
    return {"behavior": behavior, "performance": performance, "laps": laps}
