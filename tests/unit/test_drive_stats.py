"""Drive stats displays use the UI's units: miles for distance, "15 min 0 s" for duration.

The web formats list and header values with ``milesText`` and ``durationText``
(web/src/shared/format.ts); every server-built display must read the same.
"""

from __future__ import annotations

import pytest

from photoncar.api.legend import legend
from photoncar.api.live import alert_text
from photoncar.config import load_config
from photoncar.detect.behavior import BehaviorReport
from photoncar.detect.rules import stale_message
from photoncar.pipeline.stats import DriveTotals, drive_stats
from photoncar.records import Citation, Event
from photoncar.summary.stats import summary_input
from photoncar.summary.validate import template_summary, validate
from photoncar.units import MILE, duration_text, miles_text


@pytest.mark.parametrize(
    ("meters", "text"),
    [
        (0.0, "0.00 mi"),
        (1609.344, "1.00 mi"),
        (11.27 * 1609.344, "11.27 mi"),
        (99.994 * 1609.344, "99.99 mi"),
        (99.996 * 1609.344, "100.00 mi"),  # 2 decimals below 100 mi, as the web decides
        (123.45 * 1609.344, "123.5 mi"),
        (1234.56 * 1609.344, "1,234.6 mi"),
    ],
)
def test_miles_text_matches_the_web(meters: float, text: str) -> None:
    assert miles_text(meters) == text


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0.0, "0 s"),
        (42.4, "42 s"),
        (59.5, "1 min 0 s"),  # rounds half up, as Math.round does
        (60.0, "1 min 0 s"),
        (900.0, "15 min 0 s"),
        (899.98, "15 min 0 s"),
        (3599.4, "59 min 59 s"),
        (3780.0, "1 h 3 min"),
    ],
)
def test_duration_text_matches_the_web(seconds: float, text: str) -> None:
    assert duration_text(seconds) == text


def test_distance_and_duration_stats_read_in_miles_and_minutes() -> None:
    stats = drive_stats(DriveTotals(900.0, 11.27 * MILE, 30.0), [], None)
    by_id = {s.stat_id: s for s in stats}
    distance = by_id["drive.distance_mi"]
    assert (distance.label, distance.unit, distance.display) == ("Distance", "mi", "11.27 mi")
    assert distance.value == pytest.approx(11.27)
    duration = by_id["drive.duration_s"]
    assert (duration.label, duration.unit, duration.display) == ("Duration", "s", "15 min 0 s")
    assert duration.value == 900.0
    assert not any(s.stat_id.endswith(("_km", "_min")) for s in stats)
    assert not any(s.display.endswith(" km") for s in stats)


def test_the_summary_checks_accept_the_new_displays() -> None:
    items = summary_input(drive_stats(DriveTotals(900.0, 11.27 * MILE, 30.0), [], None))
    summary = {
        "sentences": [
            {"text": "The drive covered 11.27 mi.", "stat_ids": ["drive.distance_mi"]},
            {"text": "It took 15 min 0 s.", "stat_ids": ["drive.duration_s"]},
            {"text": "Top speed was 67.1 mph.", "stat_ids": ["drive.max_speed_mph"]},
        ]
    }
    assert validate(summary, items) == []
    wrong = {"sentences": [*summary["sentences"][:2], {"text": "It was 18.14 km.", "stat_ids": [
        "drive.distance_mi"
    ]}]}  # fmt: skip
    assert validate(wrong, items) == ["sentence 3 has the number 18.14 that no cited stat shows"]
    template = template_summary(items)
    assert [s["text"] for s in template["sentences"][:2]] == [
        "Distance: 11.27 mi.",
        "Duration: 15 min 0 s.",
    ]
    assert validate(template, items) == []


def test_time_in_an_rpm_band_reads_in_minutes() -> None:
    behavior = BehaviorReport(rpm_band_s=[(1000.0, 2000.0, 612.3), (2000.0, 3000.0, 42.3)])
    by_id = {s.stat_id: s for s in drive_stats(DriveTotals(None, None, None), [], behavior)}
    assert by_id["behavior.rpm_band.1000_2000"].display == "10 min 12 s"
    assert by_id["behavior.rpm_band.1000_2000"].value == 612.3
    assert by_id["behavior.rpm_band.2000_3000"].display == "42 s"


def test_alert_texts_read_long_durations_in_minutes() -> None:
    assert stale_message("obd.coolant_temp", 12.4) == (
        "Coolant temperature stale, no update for 12 s."
    )
    assert stale_message("obd.coolant_temp", 612.0) == (
        "Coolant temperature stale, no update for 10 min 12 s."
    )
    s = 1_000_000_000
    ev = Event(
        kind="alert.coolant_high", rule_id="coolant_high", rule_version=1, severity=2,
        t_start_ns=0, t_end_ns=612 * s, value=379.15, threshold=378.15, unit="K",
        message="Coolant 106 °C, over 105 °C.",
        citations=(
            Citation("obd.coolant_temp", "OBD 0x05", "trigger", 0, 612 * s, "min", 379.15, "K", 9),
        ),
    )  # fmt: skip
    assert alert_text(ev) == ("Coolant 106 °C", "OBD 0x05 over 105 °C for 10 min 12 s")


def test_legend_durations_read_as_the_ui_does() -> None:
    rules = {r["rule_id"]: r for r in legend(load_config())["rules"]}
    running = rules["voltage_low_running"]["condition"]
    assert "for 1 min 0 s with the engine running" in running  # [rules.voltage_low_running] 60 s
    assert "The first 10 s after a start do not count." in running
    assert rules["coolant_high"]["condition"] == (
        "Coolant over 105 °C for 10 s. It clears after 30 s under 102 °C."
    )
