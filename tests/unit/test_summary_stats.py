"""The model input holds computed stats only: no GPS, no dates, no identifiers."""

from __future__ import annotations

import json
import math

import pytest

from photoncar.records import DriveStat
from photoncar.summary.stats import exclusion_reason, stats_json, summary_input

KEPT = [
    DriveStat("perf.zero_to_sixty_s", "0 to 60 mph", 5.47, "s", "5.47 s"),
    DriveStat("perf.peak_lateral_g", "Peak lateral", 0.46, "g", "0.46 g"),
    DriveStat("behavior.jerk_lat_mean", "Mean lateral jerk", 1.25, "m/s^3", "1.25 m/s³"),
    DriveStat("behavior.rpm_band.3000_4000", "3000 to 4000 rpm", 42.0, "s", "42 s"),
    DriveStat("lap.best_s", "Best lap", 83.4, "s", "1:23.4"),
    DriveStat("drive.distance_m", "Distance", 1234.5, "m", "1,234.5 m"),
]

# Built at runtime so no tracked line holds a VIN shape or a precise coordinate pair.
VIN_TEXT = "WBSWD9C5" + "8AP123456"
LAT_TEXT = "40." + "123456"


@pytest.mark.parametrize(
    ("stat", "reason"),
    [
        (DriveStat("gps.start_lat", "Start", 40.1, "deg", "40.1°"), "location"),
        (DriveStat("gps.end_lon", "End", -74.2, "deg", "-74.2°"), "location"),
        (DriveStat("drive.start_position", "Start", 1.0, "", "here"), "location"),
        (DriveStat("drive.home_distance_m", "From home", 300.0, "m", "300 m"), "location"),
        (DriveStat("drive.peak", "Peak", 1.0, "", LAT_TEXT), "location (coordinate precision)"),
        (DriveStat("drive.date", "Date", 0.0, "", "today"), "date"),
        (DriveStat("drive.started_utc", "Start", 1.0, "s", "1 s"), "date"),
        (DriveStat("drive.start", "Start", 1.0, "epoch_s", "1 s"), "date"),
        (DriveStat("drive.when", "When", 0.0, "", "2026-09-23"), "date"),
        (DriveStat("drive.when2", "When", 0.0, "", "9/23/2026"), "date"),
        (DriveStat("drive.when3", "When", 0.0, "", "Sep 23"), "date"),
        (DriveStat("drive.drive_id", "Drive", 0.0, "", "x"), "identifier"),
        (DriveStat("vehicle.vin", "Car", 0.0, "", "x"), "identifier"),
        (DriveStat("drive.ref", "Ref", 0.0, "", "3f1c9a52-7d7e-4c1b-9b0e-5a2f6c8d4e11"),
         "identifier"),
        (DriveStat("drive.car", "Car", 0.0, "", VIN_TEXT), "identifier"),
        (DriveStat("drive.source", "Source", 0.0, "", "b0c9d2329ad1606b"), "identifier"),
        (DriveStat("drive.odd", "Odd", math.nan, "", "n/a"), "value is not finite"),
    ],
)  # fmt: skip
def test_each_risky_stat_is_left_out(stat: DriveStat, reason: str) -> None:
    assert exclusion_reason(stat) == reason
    assert summary_input([*KEPT, stat]) == summary_input(KEPT)


def test_ordinary_stats_pass_unchanged_with_only_5_fields() -> None:
    items = summary_input(KEPT)
    assert [i["id"] for i in items] == [s.stat_id for s in KEPT]
    for item, stat in zip(items, KEPT, strict=True):
        assert item == {
            "id": stat.stat_id,
            "label": stat.label,
            "value": stat.value,
            "unit": stat.unit,
            "display": stat.display,
        }


def test_lateral_is_not_mistaken_for_latitude() -> None:
    assert exclusion_reason(DriveStat("state.accel_lat_p95", "Lateral", 0.3, "g", "0.3 g")) is None


def test_stats_json_is_canonical_and_round_trips() -> None:
    items = summary_input(KEPT)
    text = stats_json(items)
    assert json.loads(text) == items
    assert text == stats_json(summary_input(KEPT))
    assert text.startswith(
        '[{"id":"perf.zero_to_sixty_s","label":"0 to 60 mph","value":5.47,"unit":"s",'
        '"display":"5.47 s"},'
    )
    assert "m/s³" in text  # not escaped: the model reads the units as written
