"""Drive-level stats: the numbers the review UI shows and the only input the model summary sees.

Every stat has an exact ``display`` string with at most 3 decimals ("0.64 g", "24.40 s"). The
summary validator string-matches numbers in model text against these strings, so the display
is fixed here and nowhere else.

Ids the report reads (``photoncar.export.report``): ``behavior.jerk_long_mean``,
``behavior.jerk_long_p95``, ``behavior.jerk_lat_mean``, ``behavior.jerk_lat_p95`` (m/s^3) and
``behavior.rpm_band.<lo>_<hi>`` (seconds). The rest:

- ``drive.distance_mi`` (miles, "11.27 mi"), ``drive.duration_s`` (seconds, shown as
  "15 min 0 s"), ``drive.max_speed_mph``: from the 10 Hz timeline, the same rows the drive
  index measures distance from. The displays read as the web shows the same values
  (``photoncar.units.miles_text`` and ``duration_text``).
- ``perf.zero_to_sixty_s`` and ``perf.sixty_to_zero_m``: the best run, only when a run exists.
- ``perf.peak_lateral_g``: only when a peak exists.
- ``behavior.hard_brakes``, ``behavior.hard_accels``, ``behavior.corners``: event counts.
- ``alerts.count`` ("Alerts"): alerts that measured signals prove. ``alerts.synthetic_count``
  ("Synthetic alerts"), only when there are any: alerts raised on synthetic data, such as the
  scripted fault on a public-dataset replay (plan H8 and H13). A synthetic alert is never in
  ``alerts.count``, so neither the review nor the model summary can report it as a measured
  one; the drive index counts ``n_alerts`` the same way.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from photoncar.detect.behavior import BehaviorReport
from photoncar.records import DriveStat, Event
from photoncar.units import MILE, MPH, duration_text, miles_text

JERK_UNIT = "m/s^3"
JERK_LABELS: dict[str, str] = {
    "jerk_long_mean": "Longitudinal jerk, mean",
    "jerk_long_p95": "Longitudinal jerk, 95th percentile",
    "jerk_lat_mean": "Lateral jerk, mean",
    "jerk_lat_p95": "Lateral jerk, 95th percentile",
}
COUNT_STATS: tuple[tuple[str, str, str], ...] = (
    ("behavior.hard_brakes", "Hard brakes", "behavior.hard_brake"),
    ("behavior.hard_accels", "Hard accelerations", "behavior.hard_accel"),
    ("behavior.corners", "Cornering events", "behavior.corner"),
)


@dataclass(frozen=True, slots=True)
class DriveTotals:
    """Drive-wide measures taken from the stored 10 Hz timeline."""

    duration_s: float | None
    distance_m: float | None
    max_speed_mps: float | None


def _num(value: float, decimals: int) -> str:
    return f"{value:.{decimals}f}"


def _band_edge(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def _stat(stat_id: str, label: str, value: float, unit: str, display: str) -> DriveStat:
    return DriveStat(stat_id=stat_id, label=label, value=float(value), unit=unit, display=display)


def drive_stats(
    totals: DriveTotals, events: Sequence[Event], behavior: BehaviorReport | None
) -> list[DriveStat]:
    """Every drive stat, in the order the UI lists them."""
    out: list[DriveStat] = []
    if totals.distance_m is not None:
        mi = totals.distance_m / MILE
        out.append(_stat("drive.distance_mi", "Distance", mi, "mi", miles_text(totals.distance_m)))
    if totals.duration_s is not None:
        s = totals.duration_s
        out.append(_stat("drive.duration_s", "Duration", s, "s", duration_text(s)))
    if totals.max_speed_mps is not None:
        mph = totals.max_speed_mps / MPH
        out.append(_stat("drive.max_speed_mph", "Top speed", mph, "mph", f"{_num(mph, 1)} mph"))

    z60 = [e.value for e in events if e.kind == "perf.zero_to_sixty"]
    if z60:
        best = min(z60)
        out.append(_stat("perf.zero_to_sixty_s", "0 to 60 mph", best, "s", f"{_num(best, 2)} s"))
    s60 = [e.value for e in events if e.kind == "perf.sixty_to_zero"]
    if s60:
        best = min(s60)
        out.append(
            _stat("perf.sixty_to_zero_m", "60 to 0 mph braking", best, "m", f"{_num(best, 1)} m")
        )
    peaks = [e.value for e in events if e.kind == "perf.peak_lateral"]
    if peaks:
        peak = max(peaks)
        out.append(_stat("perf.peak_lateral_g", "Peak lateral", peak, "g", f"{_num(peak, 2)} g"))

    for stat_id, label, kind in COUNT_STATS:
        n = sum(1 for e in events if e.kind == kind)
        out.append(_stat(stat_id, label, n, "count", str(n)))
    alerts = [e for e in events if e.kind.startswith("alert.")]
    n_alerts = sum(1 for e in alerts if not e.synthetic)
    out.append(_stat("alerts.count", "Alerts", n_alerts, "count", str(n_alerts)))
    n_synthetic = len(alerts) - n_alerts
    if n_synthetic:
        out.append(
            _stat(
                "alerts.synthetic_count",
                "Synthetic alerts",
                n_synthetic,
                "count",
                str(n_synthetic),
            )
        )

    if behavior is not None:
        for key, label in JERK_LABELS.items():
            value = getattr(behavior, key)
            if value is None:
                continue
            out.append(_stat(f"behavior.{key}", label, value, JERK_UNIT, f"{_num(value, 2)} m/s³"))
        for lo, hi, seconds in behavior.rpm_band_s:
            lo_t, hi_t = _band_edge(lo), _band_edge(hi)
            out.append(
                _stat(
                    f"behavior.rpm_band.{lo_t}_{hi_t}",
                    f"Time at {lo_t} to {hi_t} rpm",
                    seconds,
                    "s",
                    duration_text(seconds),
                )
            )
    return out
