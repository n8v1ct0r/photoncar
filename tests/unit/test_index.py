"""The drive index: registration, summaries, listing order, and baselines."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from photoncar.records import Citation, Event, StateEstimate
from photoncar.store.index import (
    BaselineRow,
    DriveIndex,
    DriveListItem,
    DriveSummary,
    drive_summary,
)
from photoncar.store.meta import DriveMeta
from photoncar.store.reader import DriveReader
from photoncar.store.writer import DriveWriter

S = 1_000_000_000
MS = 1_000_000


def _drive(
    path: Path, drive_id: str, speed: float, seconds: int, extra: Sequence[Event] = ()
) -> None:
    trigger = Citation(
        "obd.coolant_temp", "OBD 0x05", "trigger", 1 * S, 3 * S, "min", 379.0, "K", 5
    )
    with DriveWriter(path, DriveMeta(drive_id=drive_id, source="replay")) as w:
        for i in range(seconds * 100 + 1):  # state at 100 Hz, t = 0 .. seconds
            w.write(StateEstimate(i * 10 * MS, speed, 0.0, 0.0, 0.0, 0.0, 0.1, 0.1, "gps+imu"))
        w.write(
            Event(
                "alert.coolant_high",
                "coolant.high",
                1,
                2,
                1 * S,
                3 * S,
                106.0,
                105.0,
                "°C",
                "Coolant 106 °C, over 105 °C.",
                (trigger,),
            )
        )
        w.write(
            Event(
                "behavior.corner",
                "behavior.corner",
                1,
                0,
                2 * S,
                4 * S,
                0.45,
                0.4,
                "g",
                "Corner 0.45 g.",
            )
        )
        w.write(
            Event(
                "behavior.hard_brake",
                "behavior.hard_brake",
                1,
                0,
                5 * S,
                6 * S,
                0.31,
                0.3,
                "g",
                "Hard brake 0.31 g.",
            )
        )
        for ev in extra:
            w.write(ev)


def test_summary_from_a_drive_file(tmp_path: Path) -> None:
    path = tmp_path / "d.sqlite"
    _drive(path, "d1", speed=20.0, seconds=60)
    with DriveReader(path) as r:
        s = drive_summary(r)
    assert s.distance_m == pytest.approx(1200.0, abs=1e-6)  # 20 m/s for 60 s
    assert (s.n_events, s.n_alerts) == (3, 1)
    assert (s.t_start_ns, s.t_end_ns) == (0, 60 * S)
    assert s.closed_cleanly is True


def test_n_alerts_leaves_out_synthetic_alerts(tmp_path: Path) -> None:
    """A scripted fault on synthetic data is not a measured alert: the drive list must not
    count it as one (plan H8)."""
    path = tmp_path / "d.sqlite"
    trigger = Citation(
        "obd.module_voltage", "OBD 0x42", "trigger", 10 * S, 30 * S, "gap_s", 20.0, "s", 2
    )
    synthetic = Event(
        "alert.stale",
        "stale",
        1,
        1,
        10 * S,
        30 * S,
        20.0,
        7.5,
        "s",
        "Module voltage stale, no update for 20 s. Synthetic fault.",
        (trigger,),
        synthetic=True,
    )
    _drive(path, "d1", speed=20.0, seconds=60, extra=(synthetic,))  # + 1 measured alert
    with DriveReader(path) as r:
        s = drive_summary(r)
        assert sum(1 for e in r.events() if e.kind.startswith("alert.")) == 2
    assert (s.n_events, s.n_alerts) == (4, 1)


def test_register_list_get_and_update(tmp_path: Path) -> None:
    drives = tmp_path / "drives"
    _drive(drives / "a.sqlite", "a", speed=10.0, seconds=30)
    _drive(drives / "b.sqlite", "b", speed=25.0, seconds=10)
    with DriveIndex(tmp_path / "index.sqlite") as idx:
        for name, label in (("a", "Replay, segment 1"), ("b", "Synthetic track day")):
            with DriveReader(drives / f"{name}.sqlite") as r:
                idx.register(r.meta(), label)
                idx.update_from_reader(r)
        items = idx.list()
        assert [i.drive_id for i in items] == ["b", "a"]  # newest first
        assert items[1] == DriveListItem(
            drive_id="a",
            label="Replay, segment 1",
            source="replay",
            duration_s=30.0,
            distance_m=pytest.approx(300.0, abs=1e-6),  # type: ignore[arg-type]
            n_events=3,
            n_alerts=1,
            closed_cleanly=True,
        )
        # Registering again refreshes the label but keeps the list position.
        with DriveReader(drives / "a.sqlite") as r:
            idx.register(r.meta(), "Renamed")
        assert [i.drive_id for i in idx.list()] == ["b", "a"]
        got = idx.get("a")
        assert got is not None and got.label == "Renamed" and got.distance_m == pytest.approx(300.0)
        assert idx.get("missing") is None
        with pytest.raises(KeyError):
            idx.update_summary("missing", DriveSummary(None, None, None, 0, 0, False))


def test_unknown_duration_and_distance_are_null(tmp_path: Path) -> None:
    with DriveIndex(tmp_path / "index.sqlite") as idx:
        idx.register(DriveMeta(drive_id="live-1", source="live"), "Live drive")
        item = idx.get("live-1")
    assert item is not None
    assert item.duration_s is None and item.distance_m is None
    assert (item.n_events, item.n_alerts, item.closed_cleanly) == (0, 0, False)


@dataclass(frozen=True)
class _Stats:
    """Stand-in for the analytics BaselineStats, matched by attribute names only."""

    signal: str
    bin: str
    median: float
    mad: float
    n_windows: int
    n_drives: int
    version: int


def test_baselines_put_and_get_latest(tmp_path: Path) -> None:
    with DriveIndex(tmp_path / "index.sqlite") as idx:
        idx.put_baselines(
            [
                _Stats("obd.coolant_temp", "warm_cruise", 363.0, 0.5, 60, 1, 1),
                _Stats("obd.stft_b1", "warm_idle", 0.01, 0.02, 30, 1, 1),
            ],
            drive_id="d1",
        )
        idx.put_baselines([_Stats("obd.coolant_temp", "warm_cruise", 363.4, 0.4, 120, 2, 2)], "d2")
        latest = idx.get_baselines()
        assert latest == [
            BaselineRow("obd.coolant_temp", "warm_cruise", 363.4, 0.4, 120, 2, 2, "d2"),
            BaselineRow("obd.stft_b1", "warm_idle", 0.01, 0.02, 30, 1, 1, "d1"),
        ]
        every = idx.get_baselines("obd.coolant_temp", latest=False)
        assert [b.version for b in every] == [1, 2]
        # Same (signal, bin, version) replaces.
        idx.put_baselines([_Stats("obd.stft_b1", "warm_idle", 0.02, 0.02, 31, 1, 1)], "d3")
        assert idx.get_baselines("obd.stft_b1") == [
            BaselineRow("obd.stft_b1", "warm_idle", 0.02, 0.02, 31, 1, 1, "d3")
        ]


def test_index_creates_every_shared_table(tmp_path: Path) -> None:
    DriveIndex(tmp_path / "index.sqlite").close()
    with sqlite3.connect(tmp_path / "index.sqlite") as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    assert {"drives_index", "baselines", "model_summaries", "spend_ledger"} <= tables
    assert mode == "wal"


H = 3600 * S
UTC0 = 1_789_992_000 * S  # 2026-09-21 12:00:00 UTC


def test_rested_hours_from_the_latest_live_drive(tmp_path: Path) -> None:
    with DriveIndex(tmp_path / "index.sqlite") as idx:
        assert idx.rested_h(UTC0) is None  # no earlier drive
        idx.register(DriveMeta(drive_id="live-1", source="live"), "Live drive")
        idx.set_utc("live-1", start_ns=UTC0 - 2 * H)
        assert idx.rested_h(UTC0) is None  # it never recorded an end
        idx.set_utc("live-1", end_ns=UTC0 - H)
        assert idx.rested_h(UTC0 + 29 * H) == pytest.approx(30.0)
        # A replay is the dataset's car, not this one: it does not end a rest.
        idx.register(DriveMeta(drive_id="replay-1", source="replay"), "Replay")
        assert idx.rested_h(UTC0 + 29 * H) == pytest.approx(30.0)
        # The drive asking is left out, and a later live drive without an end makes it unknown.
        idx.register(DriveMeta(drive_id="live-2", source="live"), "Live drive")
        idx.set_utc("live-2", start_ns=UTC0 + 29 * H)
        assert idx.rested_h(UTC0 + 29 * H, exclude="live-2") == pytest.approx(30.0)
        assert idx.rested_h(UTC0 + 30 * H) is None
        idx.set_utc("live-2", end_ns=UTC0 + 30 * H)
        assert idx.rested_h(UTC0 + 54 * H) == pytest.approx(24.0)
        assert idx.rested_h(UTC0 + 29 * H) is None  # the clock stepped back
        with pytest.raises(KeyError):
            idx.set_utc("missing", end_ns=UTC0)


def test_an_older_index_gains_the_utc_columns(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    with sqlite3.connect(path) as c:
        c.execute(
            "CREATE TABLE drives_index (drive_id TEXT PRIMARY KEY, source TEXT NOT NULL, "
            "label TEXT NOT NULL, t_start_ns INTEGER, t_end_ns INTEGER, distance_m REAL, "
            "n_events INTEGER NOT NULL DEFAULT 0, n_alerts INTEGER NOT NULL DEFAULT 0, "
            "closed_cleanly INTEGER NOT NULL DEFAULT 0, created_seq INTEGER NOT NULL) STRICT"
        )
        c.execute(
            "INSERT INTO drives_index VALUES ('old', 'live', 'Live drive', 0, 1, NULL, 0, 0, 1, 1)"
        )
    c.close()
    with DriveIndex(path) as idx:
        assert idx.rested_h(UTC0) is None
        idx.set_utc("old", end_ns=UTC0)
        assert idx.rested_h(UTC0 + 24 * H) == pytest.approx(24.0)
        assert [i.drive_id for i in idx.list()] == ["old"]
    DriveIndex(path).close()  # a second open finds the columns in place


def test_rested_hours_skip_a_live_drive_reanalysed_from_its_file(tmp_path: Path) -> None:
    """A re-analysed live drive keeps the live source but has no UTC times of its own: it
    neither ends nor starts a rest."""
    with DriveIndex(tmp_path / "index.sqlite") as idx:
        idx.register(DriveMeta(drive_id="live-1", source="live"), "Live drive")
        idx.set_utc("live-1", start_ns=UTC0 - 2 * H, end_ns=UTC0 - H)
        idx.register(DriveMeta(drive_id="again-1", source="live"), "Re-analysis")
        assert idx.rested_h(UTC0 + 23 * H) == pytest.approx(24.0)
        # A live drive recorded after it counts again, even before it ends.
        idx.register(DriveMeta(drive_id="live-2", source="live"), "Live drive")
        idx.set_utc("live-2", start_ns=UTC0 + 23 * H)
        assert idx.rested_h(UTC0 + 24 * H) is None
