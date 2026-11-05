"""Lap v0 export (schema, privacy, path rules) and the drive report."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema
import numpy as np
import pytest

from photoncar.config import ConfigError, HomeZone, load_config
from photoncar.export.laps_v0 import (
    SAMPLE_KEYS,
    SCHEMA_TAG,
    ExportPathError,
    export_drive_laps,
    export_laps,
    lap_schema,
    validate_lap,
)
from photoncar.export.report import drive_report
from photoncar.privacy.geo import EARTH_MEAN_RADIUS_M, enu_m
from photoncar.records import Citation, DriveStat, Event, GpsFix, Lap, Sample, StateEstimate
from photoncar.store.meta import DriveMeta
from photoncar.store.reader import DriveReader
from photoncar.store.writer import DriveWriter

S = 1_000_000_000
MS = 1_000_000

# A circular track, radius 100 m, driven counterclockwise at 25 m/s. The start and finish line
# crosses the circle at angle 0 (100 m east of the center), where the car heads north.
CENTER = (10.0, 20.0)  # lat, lon of the track center (synthetic)
TRACK_R = 100.0
SPEED = 25.0
LAP_S = 2 * math.pi * TRACK_R / SPEED
THETA0 = -math.pi / 6  # the drive starts 30 degrees before the line
T_CROSS0 = (math.pi / 6) * TRACK_R / SPEED
DRIVE_ID = "3f1c9a52-7d7e-4c1b-9b0e-5a2f6c8d4e11"


def _latlon(x: float, y: float) -> tuple[float, float]:
    lat = CENTER[0] + math.degrees(y / EARTH_MEAN_RADIUS_M)
    lon = CENTER[1] + math.degrees(x / (EARTH_MEAN_RADIUS_M * math.cos(math.radians(CENTER[0]))))
    return lat, lon


def _pos(t_s: float) -> tuple[float, float]:
    th = THETA0 + SPEED * t_s / TRACK_R
    return TRACK_R * math.cos(th), TRACK_R * math.sin(th)


def _track_drive(path: Path, *, clean: bool = True) -> None:
    end_s = T_CROSS0 + 3 * LAP_S + 2.0
    w = DriveWriter(path, DriveMeta(drive_id=DRIVE_ID, source="synthetic"))
    for k in range(int(end_s / 0.02) + 1):  # state at 50 Hz
        t_ns = k * 20 * MS
        if k % 5 == 0:  # GPS at 10 Hz
            lat, lon = _latlon(*_pos(t_ns / S))
            w.write(GpsFix(t_ns, t_ns, None, lat, lon, 50.0, SPEED, 0.0, 0.8, 3, 12))
        if k % 25 == 0:  # OBD fast group at 2 Hz
            w.write(Sample(t_ns, "obd.rpm", 5000.0))
            w.write(Sample(t_ns, "obd.throttle", 0.5))
        w.write(
            StateEstimate(
                t_ns, SPEED, 0.0, SPEED**2 / TRACK_R, 6.25, SPEED / TRACK_R, 0.01, 0.01, "gps+imu"
            )
        )
    dur = round(LAP_S * S)
    for i in range(3):
        t0 = round((T_CROSS0 + i * LAP_S) * S)
        w.write(Lap("sf1", i + 1, t0, t0 + dur, dur, True))
    w.close(clean=clean)


def _local_to_enu(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Local circle-centered meters to the drive's ENU frame (origin: the first fix)."""
    lat0, lon0 = _latlon(*_pos(0.0))
    lats, lons = zip(*(_latlon(x, y) for x, y in points), strict=True)
    east, north = enu_m(list(lats), list(lons), lat0, lon0)
    return [(float(e), float(n)) for e, n in zip(east, north, strict=True)]


def _keys(obj: object) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | set().union(*(_keys(v) for v in obj.values()))
    return set()


@pytest.fixture
def far_home() -> HomeZone:
    return HomeZone(CENTER[0] + 0.5, CENTER[1], 500.0)  # about 55 km north of the track


def test_schema_is_valid_json_schema_2020_12() -> None:
    schema = lap_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["properties"]["schema"] == {"const": "photoncar.lap/v0"}
    assert set(schema["properties"]["samples"]["required"]) == set(SAMPLE_KEYS)


@pytest.mark.parametrize("clean", [True, False])
def test_export_laps_writes_valid_relative_files(
    tmp_path: Path, far_home: HomeZone, clean: bool
) -> None:
    drive = tmp_path / "data" / "drives" / "d.sqlite"
    _track_drive(drive, clean=clean)
    base = tmp_path / "data"
    line = _local_to_enu([(95.0, 0.0), (125.0, 0.0)])
    x1, y1 = line[0]
    x2, y2 = line[1]
    (cross,) = _local_to_enu([(100.0, 0.0)])
    mid = ((x1 + x2) / 2, (y1 + y2) / 2)
    with DriveReader(drive) as r:
        paths = export_laps(
            r, far_home, base / "exports", base=base, lines={"sf1": (x1, y1, x2, y2)}
        )
    assert len(paths) == 3
    validator = jsonschema.Draft202012Validator(lap_schema())
    n = math.floor(LAP_S * 10) + 1
    for i, p in enumerate(sorted(paths), 1):
        assert p.parent == (base / "exports").resolve()
        text = p.read_text(encoding="utf-8")
        doc: dict[str, Any] = json.loads(text)
        validator.validate(doc)
        validate_lap(doc)
        assert DRIVE_ID not in text and DRIVE_ID not in p.name
        assert f"{CENTER[0]:.4f}" not in text  # no absolute coordinates
        assert _keys(doc) == {
            "schema",
            "lap_no",
            "line_id",
            "duration_ms",
            "valid",
            "source",
            "samples",
            *SAMPLE_KEYS,
        }
        assert doc["schema"] == SCHEMA_TAG
        assert (doc["lap_no"], doc["line_id"], doc["valid"], doc["source"]) == (
            i,
            "sf1",
            True,
            "synthetic",
        )
        assert doc["duration_ms"] == round(LAP_S * 1000)
        smp = doc["samples"]
        assert smp["t_ms"] == [100 * k for k in range(n)]
        assert smp["speed_kph"] == [90.0] * n
        assert smp["dist_m"][-1] == pytest.approx(SPEED * 0.1 * (n - 1), abs=1e-3)
        assert smp["rpm"] == [5000.0] * n
        assert smp["throttle_pct"] == [50.0] * n
        assert smp["brake"] == [None] * n and smp["gear"] == [None] * n
        # The lap starts on the line, 10 m west of the midpoint of a 30 m line.
        assert smp["x"][0] == pytest.approx(-10.0, abs=0.1)
        # Everywhere on the lap, x/y match the true path in the line's frame. Tolerance 2 cm:
        # the chord between 10 Hz positions on a 100 m circle sags up to 7.8 mm.
        t_start = T_CROSS0 + (i - 1) * LAP_S
        ks = [0, n // 4, n // 2, 3 * n // 4, n - 1]
        truth = _local_to_enu([_pos(t_start + 0.1 * k) for k in ks])
        for k, (ex, ey) in zip(ks, truth, strict=True):
            assert smp["x"][k] == pytest.approx(ex - mid[0], abs=0.02)
            assert smp["y"][k] == pytest.approx(ey - mid[1], abs=0.02)
        assert cross[0] - mid[0] == pytest.approx(truth[0][0] - mid[0], abs=1e-6)


def test_without_line_geometry_the_origin_is_the_first_crossing(
    tmp_path: Path, far_home: HomeZone
) -> None:
    base = tmp_path / "data"
    _track_drive(base / "d.sqlite")
    with DriveReader(base / "d.sqlite") as r:
        paths = export_laps(r, far_home, base / "exports" / "track", base=base)
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(paths)]
    assert docs[0]["samples"]["x"][0] == 0.0 and docs[0]["samples"]["y"][0] == 0.0
    for d in docs[1:]:  # later laps start at the same point on the line, within a chord error
        assert math.hypot(d["samples"]["x"][0], d["samples"]["y"][0]) < 0.02


def test_laps_touching_the_privacy_zone_are_not_exported(tmp_path: Path) -> None:
    base = tmp_path / "data"
    _track_drive(base / "d.sqlite")
    home_on_track = HomeZone(CENTER[0], CENTER[1], 500.0)
    with DriveReader(base / "d.sqlite") as r:
        assert export_laps(r, home_on_track, base / "exports", base=base) == []
    assert not (base / "exports").exists()


def test_exports_refuse_any_path_outside_data_exports(tmp_path: Path, far_home: HomeZone) -> None:
    base = tmp_path / "data"
    _track_drive(base / "d.sqlite")
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / "exports").mkdir()
    (base / "exports" / "link").symlink_to(outside, target_is_directory=True)
    with DriveReader(base / "d.sqlite") as r:
        for bad in (
            tmp_path / "elsewhere",
            base,
            base / "exports" / ".." / "sneaky",
            base / "exports" / "link",
            base / "exports-2",
        ):
            with pytest.raises(ExportPathError):
                export_laps(r, far_home, bad, base=base)
    assert not (tmp_path / "elsewhere").exists() and not any(outside.iterdir())
    assert not (base / "sneaky").exists()


def test_export_refuses_without_the_privacy_file(tmp_path: Path) -> None:
    base = tmp_path / "data"
    _track_drive(base / "d.sqlite")
    cfg = load_config()
    with (
        DriveReader(base / "d.sqlite") as r,
        pytest.raises(ConfigError, match="privacy file missing"),
    ):
        export_drive_laps(r, cfg, base=base, privacy_path=tmp_path / "privacy.local.toml")
    assert not (base / "exports").exists()

    privacy = tmp_path / "privacy.local.toml"
    privacy.write_text(
        f"[home]\nlat_deg = {CENTER[0] + 0.5}\nlon_deg = {CENTER[1]}\n", encoding="utf-8"
    )
    with DriveReader(base / "d.sqlite") as r:
        paths = export_drive_laps(r, cfg, base=base, privacy_path=privacy)
    assert len(paths) == 3 and all(p.parent == (base / "exports").resolve() for p in paths)


def _valid_doc() -> dict[str, Any]:
    return {
        "schema": SCHEMA_TAG,
        "lap_no": 1,
        "line_id": "sf1",
        "duration_ms": 200,
        "valid": True,
        "source": "replay",
        "samples": {
            "t_ms": [0, 100, 200],
            "dist_m": [0.0, 2.0, 4.0],
            "speed_kph": [72.0, 72.0, 72.0],
            "throttle_pct": [10.0, None, 12.5],
            "brake": [None, None, None],
            "gear": [None, 3, 3],
            "rpm": [3000.0, 3010.0, None],
            "x": [0.0, 2.0, None],
            "y": [0.0, 0.0, None],
        },
    }


def test_validate_lap_rejects_every_violation() -> None:
    validate_lap(_valid_doc())
    bad: list[dict[str, Any]] = []
    mutations: tuple[Callable[[dict[str, Any]], object], ...] = (
        lambda d: d.update(drive_id="abc"),
        lambda d: d.update(date="2026-09-23"),
        lambda d: d["samples"].update(lat=[1.0, 1.0, 1.0]),
        lambda d: d.update(schema="photoncar.lap/v1"),
        lambda d: d.update(source="dataset"),
        lambda d: d.update(lap_no=0),
        lambda d: d["samples"].update(throttle_pct=[10.0, 101.0, 12.0]),
        lambda d: d["samples"].update(brake=[None, 1, None]),
        lambda d: d["samples"].pop("x"),
        lambda d: d["samples"].update(rpm=[1.0, 2.0]),  # unequal length
        lambda d: d["samples"].update(t_ms=[0, 100, 250]),  # not 10 Hz
    )
    for mutate in mutations:
        doc = _valid_doc()
        mutate(doc)
        bad.append(doc)
    for doc in bad:
        with pytest.raises((jsonschema.ValidationError, ValueError)):
            validate_lap(doc)


# -- report ------------------------------------------------------------------------------------


def _cit(signal: str, role: str, stat: str, value: float, t0: int, t1: int) -> Citation:
    return Citation(signal, "Estimator", role, t0, t1, stat, value, "", 1)  # type: ignore[arg-type]


def _ev(kind: str, t_s: float, value: float, cites: tuple[Citation, ...] = ()) -> Event:
    t0 = round(t_s * S)
    return Event(kind, kind, 1, 0, t0, t0 + 300 * MS, value, None, "", "Event.", cites)


def test_drive_report_reads_stored_events_stats_and_laps(tmp_path: Path) -> None:
    path = tmp_path / "d.sqlite"
    s2, s3 = 2 * S, 3 * S
    shift_cites = (
        Citation("obd.rpm", "OBD 0x0C", "trigger", s2, s3, "value", 7200.0, "rpm", 1),
        _cit("behavior.gear.from", "context", "from_gear", 2.0, s2, s3),
        _cit("behavior.gear.to", "context", "to_gear", 3.0, s2, s3),
    )
    z60 = (
        _cit("state.speed", "trigger", "max", 26.9, s2, s3),
        _cit("state.speed", "context", "rollout_1ft_s", 5.117651427, s2, s3),
    )
    alert_cite = (
        Citation("obd.coolant_temp", "OBD 0x05", "trigger", s2, s3, "min", 379.0, "K", 3),
    )
    with DriveWriter(path, DriveMeta(drive_id="rep", source="replay")) as w:
        w.write(StateEstimate(2 * S, 0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.1, "imu"))
        for t in (10.0, 20.0):
            w.write(_ev("behavior.hard_brake", t, 0.33))
        w.write(_ev("behavior.hard_accel", 11.0, 0.36))
        for t in (12.0, 13.0, 14.0):
            w.write(_ev("behavior.corner", t, 0.45))
        w.write(_ev("behavior.shift", 15.0, 7200.0, shift_cites))
        w.write(_ev("behavior.shift", 16.0, 6900.0))
        w.write(_ev("perf.zero_to_sixty", 30.0, 5.470247230, z60))
        w.write(_ev("perf.sixty_to_zero", 40.0, 36.68128983))
        w.write(_ev("perf.peak_lateral", 50.0, 0.46))
        w.write(_ev("perf.peak_lateral", 55.0, 0.52))
        w.write(
            Event(
                "alert.coolant_high",
                "coolant.high",
                1,
                2,
                s2,
                s3,
                106.0,
                105.0,
                "°C",
                "Coolant 106 °C, over 105 °C.",
                alert_cite,
            )
        )
        w.write(Lap("sf1", 1, 12 * S, 37 * S, 25 * S, True))
        w.write(Lap("sf1", 2, 37 * S, 63 * S, 26 * S, False))
        w.write_stats(
            [
                DriveStat("behavior.jerk_long_mean", "Jerk, long, mean", 1.2, "m/s^3", "1.2 m/s³"),
                DriveStat("behavior.jerk_long_p95", "Jerk, long, p95", 3.4, "m/s^3", "3.4 m/s³"),
                DriveStat("behavior.jerk_lat_mean", "Jerk, lat, mean", 0.8, "m/s^3", "0.8 m/s³"),
                DriveStat("behavior.jerk_lat_p95", "Jerk, lat, p95", 2.1, "m/s^3", "2.1 m/s³"),
                DriveStat("behavior.rpm_band.7000_8000", "7,000 to 8,000 rpm", 12.5, "s", "12.5 s"),
                DriveStat("behavior.rpm_band.0_1000", "0 to 1,000 rpm", 30.0, "s", "30 s"),
                DriveStat("behavior.rpm_band.1000_2000", "1,000 to 2,000 rpm", 45.0, "s", "45 s"),
            ]
        )
    with DriveReader(path) as r:
        report = drive_report(r)
    assert report == {
        "behavior": {
            "hard_brakes": 2,
            "hard_accels": 1,
            "corners": 3,
            "jerk_long_mean": 1.2,
            "jerk_long_p95": 3.4,
            "jerk_lat_mean": 0.8,
            "jerk_lat_p95": 2.1,
            "rpm_bands": [
                {"lo": 0.0, "hi": 1000.0, "seconds": 30.0},
                {"lo": 1000.0, "hi": 2000.0, "seconds": 45.0},
                {"lo": 7000.0, "hi": 8000.0, "seconds": 12.5},
            ],
            "shifts": [
                {"t_s": 13.0, "rpm": 7200.0, "from_gear": 2, "to_gear": 3},
                {"t_s": 14.0, "rpm": 6900.0, "from_gear": None, "to_gear": None},
            ],
        },
        "performance": {
            "zero_to_sixty": [
                {"t_start_s": 28.0, "seconds": 5.470247230, "rollout_seconds": 5.117651427}
            ],
            "sixty_to_zero": [{"t_start_s": 38.0, "meters": 36.68128983}],
            "peak_lateral_g": 0.52,
        },
        "laps": [
            {"lap_no": 1, "line_id": "sf1", "t_start_s": 10.0, "duration_s": 25.0, "valid": True},
            {"lap_no": 2, "line_id": "sf1", "t_start_s": 35.0, "duration_s": 26.0, "valid": False},
        ],
    }
    json.dumps(report, allow_nan=False)


def test_drive_report_of_an_empty_drive(tmp_path: Path) -> None:
    path = tmp_path / "d.sqlite"
    DriveWriter(path, DriveMeta(drive_id="empty", source="live")).close()
    with DriveReader(path) as r:
        report = drive_report(r)
    assert report["behavior"]["hard_brakes"] == 0
    assert report["behavior"]["jerk_lat_p95"] is None
    assert report["behavior"]["rpm_bands"] == [] and report["behavior"]["shifts"] == []
    assert report["performance"] == {
        "zero_to_sixty": [],
        "sixty_to_zero": [],
        "peak_lateral_g": None,
    }
    assert report["laps"] == []


def test_drive_report_refuses_a_malformed_rpm_band_stat(tmp_path: Path) -> None:
    path = tmp_path / "d.sqlite"
    with DriveWriter(path, DriveMeta(drive_id="bad", source="live")) as w:
        w.write_stats([DriveStat("behavior.rpm_band.high", "High", 1.0, "s", "1 s")])
    with DriveReader(path) as r, pytest.raises(ValueError, match="rpm band"):
        drive_report(r)


def test_numpy_free_output_types(tmp_path: Path, far_home: HomeZone) -> None:
    """Exported values are plain JSON types, not numpy scalars."""
    base = tmp_path / "data"
    _track_drive(base / "d.sqlite")
    with DriveReader(base / "d.sqlite") as r:
        (p, *_) = export_laps(r, far_home, base / "exports", base=base)
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert all(type(v) is float for v in doc["samples"]["speed_kph"])
    assert all(type(v) is int for v in doc["samples"]["t_ms"])
    assert not isinstance(doc["duration_ms"], np.integer)
