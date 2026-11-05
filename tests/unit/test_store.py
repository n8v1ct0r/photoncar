"""Drive writer and reader: durability settings, batching, crash recovery, round trips."""

from __future__ import annotations

import dataclasses
import math
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from photoncar.records import (
    CanFrame,
    Citation,
    DriveStat,
    Event,
    GpsFix,
    ImuSample,
    Lap,
    ObdPoll,
    Record,
    Sample,
    StateEstimate,
)
from photoncar.store.meta import DriveMeta
from photoncar.store.reader import ALERTS_WITHOUT_TRIGGER_SQL, DriveReader, decimate_minmax
from photoncar.store.rollup import TimelineRow
from photoncar.store.writer import INDEXES, JOURNAL_SIZE_LIMIT_BYTES, DriveWriter
from photoncar.units import G0, KELVIN_OFFSET

MS = 1_000_000
S = 1_000_000_000


def _meta(**kw: Any) -> DriveMeta:
    return dataclasses.replace(DriveMeta(drive_id="drive-test-1", source="synthetic"), **kw)


def _same(a: object, b: object) -> bool:
    """Dataclass equality that treats NaN as equal to NaN."""
    if type(a) is not type(b) or not dataclasses.is_dataclass(a):
        return a == b
    for f in dataclasses.fields(a):
        x, y = getattr(a, f.name), getattr(b, f.name)
        if isinstance(x, float) and isinstance(y, float) and math.isnan(x) and math.isnan(y):
            continue
        if isinstance(x, tuple) and isinstance(y, tuple):
            if len(x) != len(y) or not all(_same(p, q) for p, q in zip(x, y, strict=True)):
                return False
            continue
        if x != y:
            return False
    return True


def _index_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as c:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def _alert(t0_ns: int, t1_ns: int) -> Event:
    return Event(
        kind="alert.coolant_high",
        rule_id="coolant.high",
        rule_version=1,
        severity=2,
        t_start_ns=t0_ns,
        t_end_ns=t1_ns,
        value=106.0,
        threshold=105.0,
        unit="°C",
        message="Coolant 106 °C, over 105 °C.",
        citations=(
            Citation(
                "obd.coolant_temp", "OBD 0x05", "trigger", t0_ns, t1_ns, "min", 379.15, "K", 12
            ),
            Citation("obd.rpm", "OBD 0x0C", "context", t0_ns, t1_ns, "mean", 3100.0, "rpm", 24),
        ),
    )


def test_writer_uses_wal_and_synchronous_full(tmp_path: Path) -> None:
    w = DriveWriter(tmp_path / "d.sqlite", _meta())
    p = w.pragmas()
    assert p["journal_mode"] == "wal"
    assert p["synchronous"] == 2  # FULL
    assert p["journal_size_limit"] == JOURNAL_SIZE_LIMIT_BYTES
    w.close()
    with sqlite3.connect(tmp_path / "d.sqlite") as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"  # persistent in the file


def test_round_trip_every_record_type(tmp_path: Path) -> None:
    path = tmp_path / "d.sqlite"
    records: list[Record] = [
        CanFrame(1 * MS, 0, 0x1A0, False, 8, bytes(range(8))),
        CanFrame(2 * MS, 1, 0x18DAF110, True, 3, b"\x01\x02\x03", is_tx=True),
        ObdPoll(3 * MS, 3 * MS + 400_000, 0x0C, 0x7E8, "ok", b"\x1a\xf8", 1726.0, "rpm"),
        ObdPoll(4 * MS, None, 0x05, None, "timeout", b"", None, "K"),
        GpsFix(5 * MS, 5 * MS + 10, 1_700_000_000 * S, 0.001, 0.002, 12.5, 20.0, 1.5, 0.8, 3, 11),
        GpsFix(6 * MS, 6 * MS + 10, None, 0.0011, 0.0021, math.nan, 19.5, math.nan, math.nan, 2, 5),
        ImuSample(7 * MS, 0.1, -0.2, 9.81, 0.01, -0.02, 0.03, temp_k=305.0),
        ImuSample(8 * MS, 0.2, -0.1, 9.80, 0.0, 0.0, 0.0),
        Sample(9 * MS, "obd.coolant_temp", 363.15),
        Sample(10 * MS, "can.custom_signal", -1.25),
        StateEstimate(11 * MS, 20.0, 0.5, -0.3, -0.29, 0.015, 0.04, 0.2, "gps+imu", dr=False),
        StateEstimate(12 * MS, 20.1, 0.4, -0.2, -0.21, 0.010, 0.05, 0.2, "imu", dr=True),
        _alert(13 * MS, 20 * MS),
        Event(
            "perf.zero_to_sixty",
            "perf.zero_to_sixty",
            1,
            0,
            14 * MS,
            19 * MS,
            5.470247230,
            None,
            "s",
            "0 to 60 mph in 5.47 s.",
            (
                Citation(
                    "state.speed", "Estimator", "trigger", 14 * MS, 19 * MS, "max", 26.8, "m/s", 5
                ),
                Citation(
                    "state.speed",
                    "Estimator",
                    "context",
                    14 * MS,
                    19 * MS,
                    "rollout_1ft_s",
                    5.1,
                    "s",
                    5,
                ),
            ),
            synthetic=True,
        ),
        Lap("sf1", 1, 15 * MS, 40 * MS, 25 * MS, True),
    ]
    stats = [
        DriveStat("peak_lateral_g", "Peak lateral", 0.46, "g", "0.46 g"),
        DriveStat("drive.distance_mi", "Distance", 7.64, "mi", "7.64 mi"),
    ]
    with DriveWriter(path, _meta(vehicle_profile="e90_m3", dataset_ref="seg/1")) as w:
        w.write_many(records)
        w.write_stats(stats)

    with DriveReader(path) as r:
        assert list(r.iter_can_frames()) == records[0:2]
        assert r.obd_polls() == records[2:4]
        fixes = r.gps_fixes()
        assert len(fixes) == 2 and all(
            _same(a, b) for a, b in zip(fixes, records[4:6], strict=True)
        )
        assert list(r.iter_imu_samples()) == records[6:8]
        assert r.samples("obd.coolant_temp") == [records[8]]
        assert r.samples("can.custom_signal") == [records[9]]
        assert r.state() == records[10:12]
        assert r.events() == [records[12], records[13]]
        assert r.laps() == [records[14]]
        assert r.stats() == stats
        meta = r.meta()
        assert meta.drive_id == "drive-test-1"
        assert meta.vehicle_profile == "e90_m3"
        assert meta.dataset_ref == "seg/1"
        assert meta.closed_cleanly is True
        assert meta.t_start_ns == 1 * MS
        assert meta.t_end_ns == 40 * MS  # the lap end is the latest time on record
        assert r.signals() == ["obd.coolant_temp", "can.custom_signal"]
    with sqlite3.connect(path) as c:
        rows = c.execute("SELECT name, unit, source, nominal_hz, origin FROM signals").fetchall()
    assert rows == [
        ("obd.coolant_temp", "K", "obd", None, "catalog"),
        ("can.custom_signal", "", "can", None, "unlisted"),
    ]


def test_events_come_back_with_citations_and_the_audit_finds_none(tmp_path: Path) -> None:
    path = tmp_path / "d.sqlite"
    late, early = _alert(50 * MS, 60 * MS), _alert(10 * MS, 30 * MS)
    corner = Event(
        "behavior.corner",
        "behavior.corner",
        1,
        0,
        20 * MS,
        25 * MS,
        0.42,
        0.4,
        "g",
        "Corner 0.42 g.",
    )
    with DriveWriter(path, _meta()) as w:
        for ev in (late, early, corner):
            w.write(ev)
    with DriveReader(path) as r:
        with_ids = r.events_with_ids()
        assert [ev for _, ev in with_ids] == [early, corner, late]  # ordered by t_start_ns
        assert [eid for eid, _ in with_ids] == [2, 3, 1]  # IDs follow write order
        assert with_ids[0][1].citations == early.citations
        assert r.alerts_without_trigger() == []

    # Negative control: the audit query does catch an alert stored without a trigger.
    with sqlite3.connect(path) as c:
        c.execute(
            "INSERT INTO events (event_id, kind, rule_id, rule_version, severity, t_start_ns, "
            "t_end_ns, value, unit, message) VALUES (99, 'alert.x', 'x', 1, 1, 1, 2, 0, '', 'x')"
        )
        c.execute(
            "INSERT INTO event_citations VALUES (99, 'obd.rpm', 'OBD 0x0C', 'context', 1, 2, "
            "'max', 1, 'rpm', 1)"
        )
        assert [r[0] for r in c.execute(ALERTS_WITHOUT_TRIGGER_SQL)] == [99]


def test_writer_refuses_values_the_schema_cannot_keep_and_stays_usable(tmp_path: Path) -> None:
    path = tmp_path / "d.sqlite"
    w = DriveWriter(path, _meta())
    with pytest.raises(ValueError, match="finite"):
        w.write(Sample(1, "obd.rpm", math.nan))
    with pytest.raises(ValueError, match="service 01"):
        w.write(ObdPoll(1, 2, 0x02, 0x7E8, "ok", b"\x00", 0.0, "", service=9))
    with pytest.raises(ValueError, match="finite"):
        w.write(StateEstimate(1, math.inf, 0, 0, 0, 0, 0, 0, "imu"))
    dup = Citation("obd.rpm", "OBD 0x0C", "trigger", 1, 2, "max", 1.0, "rpm", 1)
    with pytest.raises(ValueError, match="2 trigger citations"):
        w.write(Event("alert.x", "x", 1, 1, 1, 2, 1.0, None, "", "x", (dup, dup)))
    with pytest.raises(TypeError):
        w.write("not a record")  # type: ignore[arg-type]
    w.write(Sample(5, "obd.rpm", 800.0))
    w.close()
    with DriveReader(path) as r:
        assert r.samples("obd.rpm") == [Sample(5, "obd.rpm", 800.0)]
        assert r.events() == []
        assert r.obd_polls() == []
    with pytest.raises(FileExistsError):
        DriveWriter(path, _meta())
    with pytest.raises(RuntimeError, match="closed"):
        w.write(Sample(6, "obd.rpm", 1.0))


def test_commits_follow_record_time_and_a_live_reader_sees_each_batch(tmp_path: Path) -> None:
    path = tmp_path / "d.sqlite"
    w = DriveWriter(path, _meta(), batch_ms=1000)
    r = DriveReader(path)
    for i in range(100):  # 0 .. 990 ms: under 1 batch, nothing committed yet
        w.write(Sample(i * 10 * MS, "obd.rpm", float(i)))
    assert r.samples("obd.rpm") == []
    w.write(Sample(1000 * MS, "obd.rpm", 100.0))  # 1000 ms past the batch start: commit
    assert len(r.samples("obd.rpm")) == 101
    for i in range(101, 200):
        w.write(Sample(i * 10 * MS, "obd.rpm", float(i)))
    assert len(r.samples("obd.rpm")) == 101
    w.write(Sample(2000 * MS, "obd.rpm", 200.0))
    assert len(r.samples("obd.rpm")) == 201
    assert r.meta().t_end_ns == 2000 * MS
    assert r.meta().closed_cleanly is False
    w.close()
    assert r.meta().closed_cleanly is True
    r.close()


_CRASH_SCRIPT = """
import os, sys
from photoncar.records import ImuSample, Sample
from photoncar.store.meta import DriveMeta
from photoncar.store.writer import DriveWriter

w = DriveWriter(sys.argv[1], DriveMeta(drive_id="crash-1", source="synthetic"), batch_ms=1000)
for i in range(251):
    t = i * 10_000_000
    w.write(Sample(t, "obd.rpm", 1000.0 + i))
    w.write(ImuSample(t, 0.1, 0.2, 9.8, 0.0, 0.0, 0.01))
os._exit(0)
"""


def test_power_loss_keeps_everything_up_to_the_last_batch_commit(tmp_path: Path) -> None:
    """The writer process dies without close (os._exit: no finalizers, no checkpoint)."""
    path = tmp_path / "crash.sqlite"
    subprocess.run([sys.executable, "-c", _CRASH_SCRIPT, str(path)], check=True, timeout=60)
    assert Path(str(path) + "-wal").exists()  # the WAL was never checkpointed
    with DriveReader(path) as r:
        rpm = r.samples("obd.rpm")
        # Commits happened when record time reached 1000 ms and 2000 ms. The sample at
        # 2000 ms triggered the second commit; the IMU sample at 2000 ms came after it.
        assert [s.value for s in rpm] == [1000.0 + i for i in range(201)]
        assert rpm[-1].t_ns == 2000 * MS
        imu = list(r.iter_imu_samples())
        assert len(imu) == 200 and imu[-1].t_ns == 1990 * MS
        meta = r.meta()
        assert meta.closed_cleanly is False
        assert (meta.t_start_ns, meta.t_end_ns) == (0, 2000 * MS)
        # No rollup was stored, so the reader builds it on the fly at 10 Hz.
        rows = r.timeline_rows()
        assert len(rows) == 21 and rows[-1].t_ns == 2000 * MS
        assert rows[5].rpm == pytest.approx(1050.0, abs=1e-9)
    assert _index_names(path).isdisjoint(INDEXES)  # indexes are built only at close


def test_clean_close_sets_the_flag_builds_indexes_and_truncates_the_wal(tmp_path: Path) -> None:
    path = tmp_path / "d.sqlite"
    with DriveWriter(path, _meta()) as w:
        for i in range(30):
            w.write(StateEstimate(i * 100 * MS, 10.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.1, "imu"))
    assert set(INDEXES) <= _index_names(path)
    wal = Path(str(path) + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    with DriveReader(path) as r:
        assert r.meta().closed_cleanly is True
        with sqlite3.connect(path) as c:
            assert c.execute("SELECT COUNT(*) FROM timeline_10hz").fetchone()[0] == 30


def test_only_a_clean_close_sets_closed_cleanly(tmp_path: Path) -> None:
    a = tmp_path / "a.sqlite"
    w = DriveWriter(a, _meta())
    w.write(Sample(0, "obd.rpm", 900.0))
    w.close(clean=False)
    with DriveReader(a) as r:
        assert r.meta().closed_cleanly is False
        assert r.samples("obd.rpm") == [Sample(0, "obd.rpm", 900.0)]
    assert set(INDEXES) <= _index_names(a)

    b = tmp_path / "b.sqlite"
    with pytest.raises(RuntimeError, match="boom"), DriveWriter(b, _meta()) as w2:
        w2.write(Sample(0, "obd.rpm", 900.0))
        raise RuntimeError("boom")
    with DriveReader(b) as r:
        assert r.meta().closed_cleanly is False
        assert len(r.samples("obd.rpm")) == 1


def test_mount_rotation_and_synthetic_signals_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "d.sqlite"
    c, s = math.cos(0.3), math.sin(0.3)
    rot = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    meta = _meta(synthetic_signals=("obd.rpm", "obd.throttle"))
    with DriveWriter(path, meta) as w:
        w.set_mount_rotation(rot)
        w.write(Sample(0, "obd.rpm", 800.0))
        with pytest.raises(ValueError, match="3x3"):
            w.set_mount_rotation(np.eye(2))
    with DriveReader(path) as r:
        got = r.meta()
    assert got.synthetic_signals == ("obd.rpm", "obd.throttle")
    assert got.imu_mount_rot is not None
    assert np.array_equal(np.array(got.imu_mount_rot), rot)


def test_stats_replace_by_stat_id(tmp_path: Path) -> None:
    path = tmp_path / "d.sqlite"
    with DriveWriter(path, _meta()) as w:
        w.write_stats([DriveStat("peak_lateral_g", "Peak lateral", 0.41, "g", "0.41 g")])
        w.write_stats([DriveStat("peak_lateral_g", "Peak lateral", 0.46, "g", "0.46 g")])
    with DriveReader(path) as r:
        assert r.stats() == [DriveStat("peak_lateral_g", "Peak lateral", 0.46, "g", "0.46 g")]


def test_decimation_keeps_every_bucket_min_and_max() -> None:
    rng = np.random.default_rng(20260923)
    n = 10_000
    t = np.arange(n, dtype=np.float64) / 10.0
    v = rng.normal(0.0, 1.0, n)
    # 1 spike per chosen bucket (40 rows per bucket at 500 points), up and down.
    spikes = rng.choice(250, size=40, replace=False) * 40 + rng.integers(0, 40, size=40)
    v[spikes[:20]] = 50.0 + rng.random(20)
    v[spikes[20:]] = -50.0 - rng.random(20)
    w = rng.normal(5.0, 1.0, n)
    w[3000:3500] = np.nan  # a stretch with no value
    w[7000] = np.nan
    t_out, out = decimate_minmax(t, {"v": v, "w": w}, 500)
    b = 250
    assert t_out.size == 500
    edges = (np.arange(b + 1) * n) // b
    for k in range(b):
        lo, hi = edges[k], edges[k + 1]
        assert (t_out[2 * k], t_out[2 * k + 1]) == (t[lo], t[hi - 1])
        for name, col in (("v", v), ("w", w)):
            seg = col[lo:hi]
            pair = out[name][2 * k : 2 * k + 2]
            if np.all(np.isnan(seg)):
                assert np.all(np.isnan(pair))
                continue
            i_min, i_max = int(np.nanargmin(seg)), int(np.nanargmax(seg))
            expect = [seg[i_min], seg[i_max]] if i_min <= i_max else [seg[i_max], seg[i_min]]
            assert pair.tolist() == expect
    assert out["v"].max() == v.max() and out["v"].min() == v.min()
    assert set(np.round(out["v"][np.abs(out["v"]) > 40], 12)) == set(np.round(v[spikes], 12))

    t_same, same = decimate_minmax(t[:300], {"v": v[:300]}, 300)
    assert np.array_equal(t_same, t[:300]) and np.array_equal(same["v"], v[:300])
    with pytest.raises(ValueError):
        decimate_minmax(t, {"v": v}, 1)


def test_reader_timeline_converts_units_and_keeps_spikes(tmp_path: Path) -> None:
    path = tmp_path / "d.sqlite"
    rows = [
        TimelineRow(
            t_ns=5 * S + i * 100 * MS,
            speed_mps=40.0 if i == 1234 else 20.0,
            rpm=None if i < 10 else 3000.0,
            throttle=0.25,
            coolant_k=363.15,
            accel_long_mps2=G0 * 0.5,
            accel_lat_mps2=-G0 * 0.25,
            x_m=float(i),
            y_m=None,
            dr=i == 777,
        )
        for i in range(3000)
    ]
    with DriveWriter(path, _meta()) as w:
        w.write(Sample(5 * S, "obd.rpm", 3000.0))
        w.write_timeline(rows)
    with DriveReader(path) as r:
        full = r.timeline(max_points=5000)
        tl = r.timeline(max_points=100)
    assert len(full["t_s"]) == 3000
    assert full["t_s"][:3] == pytest.approx([0.0, 0.1, 0.2], abs=1e-12)
    assert full["coolant_c"][0] == pytest.approx(363.15 - KELVIN_OFFSET, abs=1e-9)
    assert full["accel_long_g"][0] == pytest.approx(0.5, abs=1e-12)
    assert full["accel_lat_g"][0] == pytest.approx(-0.25, abs=1e-12)
    assert full["rpm"][0] is None and full["y_m"][0] is None
    assert set(tl) == {
        "t_s",
        "speed_mps",
        "rpm",
        "throttle",
        "coolant_c",
        "accel_long_g",
        "accel_lat_g",
        "x_m",
        "y_m",
        "dr",
        "sources",
    }
    assert all(len(v) == 100 for k, v in tl.items() if k != "sources")
    # Only an rpm sample was written: the other columns have no signal behind them.
    assert tl["sources"] == {
        "speed_mps": None,
        "rpm": "obd.rpm",
        "throttle": None,
        "coolant_c": None,
        "accel_long_g": None,
        "accel_lat_g": None,
        "x_m": None,
        "y_m": None,
    }
    assert max(v for v in tl["speed_mps"] if v is not None) == 40.0  # the spike survives
    assert tl["dr"].count(True) == 1  # so does the single dead-reckoned row
    assert min(v for v in tl["x_m"] if v is not None) == 0.0
    assert max(v for v in tl["x_m"] if v is not None) == 2999.0


def test_speed_only_estimates_keep_no_lateral_values(tmp_path: Path) -> None:
    """An estimate made without an IMU has NaN lateral values. The state table keeps NULL;
    the reader, the rollup, and the recorded source give NaN back."""
    from photoncar.sources.recorded import RecordedDriveSource

    nan = math.nan
    imu = StateEstimate(100 * MS, 10.0, 0.5, 0.8, 0.79, 0.08, 0.01, 0.3, "imu+gps")
    speed_only = [
        StateEstimate(k * 100 * MS, 10.0, 0.5, nan, nan, nan, 0.01, 0.3, "obd")
        for k in range(2, 12)
    ]
    path = tmp_path / "d.sqlite"
    with DriveWriter(path, _meta()) as w:
        with pytest.raises(ValueError, match="no lateral values"):
            w.write(StateEstimate(1, 10.0, 0.0, 0.5, nan, nan, 0.01, 0.3, "obd"))
        for s in (imu, *speed_only):
            w.write(s)
    with DriveReader(path) as r:
        got = r.state()
        rows = r.timeline_rows()
    assert got[0] == imu
    assert all(_same(a, b) for a, b in zip(got[1:], speed_only, strict=True))
    assert rows[0].accel_lat_mps2 == pytest.approx(0.8)  # the IMU estimate's bucket
    assert all(row.accel_lat_mps2 is None for row in rows[1:])
    assert all(row.speed_mps == 10.0 and row.accel_long_mps2 == 0.5 for row in rows[1:])
    replayed = [x for x in RecordedDriveSource(path, derived=True).records()]
    states = [x for x in replayed if isinstance(x, StateEstimate)]
    assert all(_same(a, b) for a, b in zip(states, (imu, *speed_only), strict=True))


def test_speed_only_lateral_values_are_stored_as_null(tmp_path: Path) -> None:
    """A speed-only estimate stores NULL lateral values, not a placeholder, and a NULL reads
    back as NaN whatever the row's inputs say."""
    nan = math.nan
    speed_only = StateEstimate(200 * MS, 10.0, 0.5, nan, nan, nan, 0.01, 0.3, "obd")
    path = tmp_path / "d.sqlite"
    with DriveWriter(path, _meta()) as w:
        w.write(speed_only)
    lateral = "SELECT accel_lat_mps2, accel_lat_imu_mps2, yaw_rate_rps FROM state"
    con = sqlite3.connect(path)
    try:
        assert con.execute(lateral).fetchall() == [(None, None, None)]
        # A row whose inputs name the IMU but whose lateral values are NULL keeps them unknown.
        con.execute(
            "INSERT INTO state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (300 * MS, 11.0, 0.4, None, None, None, 0.01, 0.3, "imu+gps", 0),
        )
        con.commit()
    finally:
        con.close()
    with DriveReader(path) as r:
        got = r.state()
        rows = r.timeline_rows()
    assert _same(got[0], speed_only)
    assert all(math.isnan(x) for x in (got[1].accel_lat_mps2, got[1].yaw_rate_rps))
    assert all(row.accel_lat_mps2 is None for row in rows)
