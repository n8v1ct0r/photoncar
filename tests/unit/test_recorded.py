"""RecordedDriveSource over a drive file built in tmp_path from store/schema.sql."""

from __future__ import annotations

import hashlib
import math
import sqlite3
from contextlib import closing
from itertools import pairwise
from pathlib import Path

import pytest

from photoncar.records import (
    CanFrame,
    Citation,
    Event,
    GpsFix,
    ImuSample,
    Lap,
    ObdPoll,
    Sample,
    StateEstimate,
    record_time,
)
from photoncar.sources.recorded import RecordedDriveSource

SCHEMA = Path(__file__).resolve().parents[2] / "src" / "photoncar" / "store" / "schema.sql"


def _drive_file(path: Path, *, source: str = "replay") -> Path:
    with closing(sqlite3.connect(path)) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA.read_text())
        con.execute(
            "INSERT INTO drive (drive_id, source, dataset_ref, t_start_ns, t_end_ns, "
            "vehicle_profile, schema_version, closed_cleanly) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("d-1", source, "Chunk_9/abc|def/3", 1000, 9000, "e90_m3", 1, 1),
        )
        con.executemany(
            "INSERT INTO signals (signal_id, name, unit, source, nominal_hz, origin) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, "obd.coolant_temp", "K", "obd", None, "PID 0x05"),
                (2, "obd.rpm", "rpm", "obd", 2.0, "synthetic overlay"),
                (3, "can.wheel_speed", "m/s", "can", None, "WHEEL_SPEEDS"),
                (4, "behavior.jerk_long", "m/s^3", "derived", 104.0, "estimator"),
            ],
        )
        # Rows are inserted out of time order on purpose.
        con.executemany(
            "INSERT INTO raw_can VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (3000, 1, 0x24, 0, 8, b"\x01" * 8, 0),
                (1000, 0, 0x1C4, 0, 8, b"\x02" * 8, 0),
                (3000, 0, 0x18DAF110, 1, 8, b"\x03" * 8, 1),
            ],
        )
        con.executemany(
            "INSERT INTO obd_polls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (2500, None, 1, 0x0C, None, "timeout", b"", None, "rpm"),
                (1500, 1520, 1, 0x05, 0x7E8, "ok", bytes([130]), 363.15, "K"),
            ],
        )
        con.executemany(
            "INSERT INTO samples VALUES (?, ?, ?)",
            [(2700, 4, 1.5), (2600, 3, 10.0), (1520, 1, 363.15), (1510, 2, 800.0)],
        )
        con.execute(
            "INSERT INTO gps_fixes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (2000, 2050, None, 0.001, 0.002, None, 5.0, 1.0, None, 3, 12),
        )
        con.executemany(
            "INSERT INTO imu_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1010, 0.1, 0.2, 9.8, 0.01, 0.02, 0.03, None),
                (1000, 0.0, 0.0, 9.81, 0.0, 0.0, 0.0, 300.0),
            ],
        )
        con.execute(
            "INSERT INTO state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (4000, 12.0, 0.5, 0.1, 0.12, 0.01, 0.2, 0.3, "gps+imu", 1),
        )
        con.executemany(
            "INSERT INTO events (event_id, kind, rule_id, rule_version, severity, t_start_ns, "
            "t_end_ns, value, threshold, unit, message, resolved, synthetic) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "alert.coolant_high", "coolant_high", 1, 2, 4500, 6000, 379.15, 378.15, "K",
                 "Coolant 106 °C, over 105 °C.", 1, 1),
                (2, "perf.zero_to_sixty", "zero_to_sixty", 1, 0, 3500, 8000, 5.47, None, "s",
                 "0 to 60 mph in 5.47 s.", 0, 0),
            ],
        )  # fmt: skip
        con.executemany(
            "INSERT INTO event_citations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "obd.coolant_temp", "OBD 0x05", "trigger", 4500, 6000, "min", 379.15, "K", 9),
                (1, "state.speed", "Estimator", "context", 4500, 6000, "mean", 25.0, "m/s", 150),
            ],
        )
        con.execute(
            "INSERT INTO laps VALUES (?, ?, ?, ?, ?, ?, ?)", (1, "sf", 1, 5000, 6000, 1000, 1)
        )
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_inputs_in_time_order(tmp_path: Path) -> None:
    path = _drive_file(tmp_path / "d-1.sqlite")
    recs = list(RecordedDriveSource(path).records())
    assert [record_time(r) for r in recs] == [
        1000, 1000, 1010, 1510, 1520, 1520, 2000, 2500, 2600, 3000, 3000
    ]  # fmt: skip
    gps = recs[6]
    assert isinstance(gps, GpsFix)
    assert (gps.t_ns, gps.rx_t_ns, gps.utc_ns, gps.lat_deg, gps.lon_deg) == (
        2000,
        2050,
        None,
        0.001,
        0.002,
    )
    assert (gps.speed_mps, gps.course_rad, gps.fix_type, gps.n_sats) == (5.0, 1.0, 3, 12)
    assert math.isnan(gps.alt_m) and math.isnan(gps.hdop)  # NULL columns
    assert recs[:6] + recs[7:] == [
        CanFrame(1000, 0, 0x1C4, False, 8, b"\x02" * 8),
        ImuSample(1000, 0.0, 0.0, 9.81, 0.0, 0.0, 0.0, 300.0),
        ImuSample(1010, 0.1, 0.2, 9.8, 0.01, 0.02, 0.03, None),
        Sample(1510, "obd.rpm", 800.0),
        ObdPoll(1500, 1520, 0x05, 0x7E8, "ok", bytes([130]), 363.15, "K"),
        Sample(1520, "obd.coolant_temp", 363.15),
        ObdPoll(2500, None, 0x0C, None, "timeout", b"", None, "rpm"),
        Sample(2600, "can.wheel_speed", 10.0),
        CanFrame(3000, 1, 0x24, False, 8, b"\x01" * 8),
        CanFrame(3000, 0, 0x18DAF110, True, 8, b"\x03" * 8, is_tx=True),
    ]
    assert not any(isinstance(r, StateEstimate | Event | Lap) for r in recs)


def test_derived_outputs_on_request(tmp_path: Path) -> None:
    path = _drive_file(tmp_path / "d-1.sqlite")
    recs = list(RecordedDriveSource(path, derived=True).records())
    times = [record_time(r) for r in recs]
    assert all(a <= b for a, b in pairwise(times))
    assert Sample(2700, "behavior.jerk_long", 1.5) in recs
    state = [r for r in recs if isinstance(r, StateEstimate)]
    assert state == [StateEstimate(4000, 12.0, 0.5, 0.1, 0.12, 0.01, 0.2, 0.3, "gps+imu", True)]
    events = [r for r in recs if isinstance(r, Event)]
    assert [e.kind for e in events] == ["perf.zero_to_sixty", "alert.coolant_high"]
    alert = events[1]
    assert alert.citations == (
        Citation("obd.coolant_temp", "OBD 0x05", "trigger", 4500, 6000, "min", 379.15, "K", 9),
        Citation("state.speed", "Estimator", "context", 4500, 6000, "mean", 25.0, "m/s", 150),
    )
    assert (alert.resolved, alert.synthetic, alert.severity, alert.threshold) == (
        True,
        True,
        2,
        378.15,
    )
    assert events[0].threshold is None and events[0].citations == ()
    assert [r for r in recs if isinstance(r, Lap)] == [Lap("sf", 1, 5000, 6000, 1000, True)]


def test_meta(tmp_path: Path) -> None:
    src = RecordedDriveSource(_drive_file(tmp_path / "d-1.sqlite"))
    assert src.meta.name == "recorded:d-1"
    assert src.meta.kind == "replay"
    assert src.meta.dataset_ref == "Chunk_9/abc|def/3"
    assert src.meta.synthetic_signals == ("obd.rpm",)
    assert (src.drive_id, src.t_start_ns, src.t_end_ns, src.vehicle_profile) == (
        "d-1",
        1000,
        9000,
        "e90_m3",
    )
    synth = RecordedDriveSource(_drive_file(tmp_path / "s.sqlite", source="synthetic"))
    assert synth.meta.kind == "synthetic"


def test_opens_read_only(tmp_path: Path) -> None:
    path = _drive_file(tmp_path / "d-1.sqlite")
    before = _sha(path)
    path.chmod(0o444)
    try:
        recs = list(RecordedDriveSource(path, derived=True).records())
    finally:
        path.chmod(0o644)
    assert len(recs) == 16
    assert _sha(path) == before


def test_missing_or_empty_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        RecordedDriveSource(tmp_path / "nope.sqlite")
    empty = tmp_path / "empty.sqlite"
    with closing(sqlite3.connect(empty)) as con:
        con.executescript(SCHEMA.read_text())
    with pytest.raises(ValueError, match="no drive row"):
        RecordedDriveSource(empty)
