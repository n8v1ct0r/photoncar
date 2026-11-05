"""The drive index (``data/index.sqlite``): the drive list and the per-signal baselines.

The server also keeps its model cache and spend ledger in this file; the index applies the
whole fixed schema so every table exists whichever process opens it first.

Live drives also keep their wall-clock UTC start and end (the device has an RTC), so the next
drive knows how long the car rested (``rested_h``). Replays and synthetic drives keep none: the
dataset's car is not this car. An index written before these columns existed gains them when
opened.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from types import TracebackType
from typing import Protocol, cast

from photoncar.store.meta import DriveMeta, DriveSource, index_schema_sql
from photoncar.store.reader import DriveReader
from photoncar.units import NS_PER_S

BUSY_TIMEOUT_MS = 5000
NS_PER_H = 3600 * NS_PER_S
ADDED_COLUMNS = (("utc_start_ns", "INTEGER"), ("utc_end_ns", "INTEGER"))
"""drives_index columns added after version 1 of the index schema."""


@dataclass(frozen=True, slots=True)
class DriveListItem:
    """1 row of ``GET /api/drives``. ``duration_s`` and ``distance_m`` are null when unknown."""

    drive_id: str
    label: str
    source: DriveSource
    duration_s: float | None
    distance_m: float | None
    n_events: int
    n_alerts: int
    closed_cleanly: bool


@dataclass(frozen=True, slots=True)
class DriveSummary:
    """The index fields computed from a drive file."""

    t_start_ns: int | None
    t_end_ns: int | None
    distance_m: float | None
    n_events: int
    n_alerts: int
    closed_cleanly: bool


@dataclass(frozen=True, slots=True)
class BaselineRow:
    signal: str
    bin: str
    median: float
    mad: float
    n_windows: int
    n_drives: int
    version: int
    updated_drive: str | None = None


class BaselineLike(Protocol):
    """Anything with the baseline stats fields (for example the analytics BaselineStats)."""

    @property
    def signal(self) -> str: ...
    @property
    def bin(self) -> str: ...
    @property
    def median(self) -> float: ...
    @property
    def mad(self) -> float: ...
    @property
    def n_windows(self) -> int: ...
    @property
    def n_drives(self) -> int: ...
    @property
    def version(self) -> int: ...


def drive_summary(reader: DriveReader) -> DriveSummary:
    """Distance is the trapezoid integral of the 10 Hz speed over rows where both ends exist.

    ``n_alerts`` counts alerts that measured signals prove; an alert raised on synthetic data
    (``synthetic``) is left out, as in the ``alerts.count`` drive stat.
    """
    meta = reader.meta()
    rows = reader.timeline_rows()
    distance: float | None = None
    for a, b in pairwise(rows):
        if a.speed_mps is None or b.speed_mps is None:
            continue
        step = (a.speed_mps + b.speed_mps) / 2.0 * (b.t_ns - a.t_ns) / NS_PER_S
        distance = step if distance is None else distance + step
    events = reader.events()
    return DriveSummary(
        t_start_ns=meta.t_start_ns,
        t_end_ns=meta.t_end_ns,
        distance_m=distance,
        n_events=len(events),
        n_alerts=sum(1 for e in events if e.kind.startswith("alert.") and not e.synthetic),
        closed_cleanly=meta.closed_cleanly,
    )


class DriveIndex:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(index_schema_sql())
        have = {r[1] for r in self._conn.execute("PRAGMA table_info(drives_index)")}
        for name, kind in ADDED_COLUMNS:
            if name in have:
                continue
            try:
                self._conn.execute(f"ALTER TABLE drives_index ADD COLUMN {name} {kind}")
            except sqlite3.OperationalError as e:  # another process added it first
                if "duplicate column" not in str(e):
                    raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> DriveIndex:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _run(self, sql: str, params: Iterable[object] = ()) -> int:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return cur.rowcount

    def register(self, meta: DriveMeta, label: str) -> None:
        """Add a drive, or refresh its label, source, and times if already listed."""
        self._run(
            "INSERT INTO drives_index (drive_id, source, label, t_start_ns, t_end_ns, "
            "closed_cleanly, created_seq) "
            "VALUES (?, ?, ?, ?, ?, ?, "
            "(SELECT COALESCE(MAX(created_seq), 0) + 1 FROM drives_index)) "
            "ON CONFLICT (drive_id) DO UPDATE SET source = excluded.source, "
            "label = excluded.label, t_start_ns = excluded.t_start_ns, "
            "t_end_ns = excluded.t_end_ns, closed_cleanly = excluded.closed_cleanly",
            (
                meta.drive_id,
                meta.source,
                label,
                meta.t_start_ns,
                meta.t_end_ns,
                int(meta.closed_cleanly),
            ),
        )

    def set_utc(
        self, drive_id: str, *, start_ns: int | None = None, end_ns: int | None = None
    ) -> None:
        """Store a drive's wall-clock UTC start and/or end, in ns since the epoch."""
        n = self._run(
            "UPDATE drives_index SET utc_start_ns = COALESCE(?, utc_start_ns), "
            "utc_end_ns = COALESCE(?, utc_end_ns) WHERE drive_id = ?",
            (start_ns, end_ns, drive_id),
        )
        if n == 0:
            raise KeyError(f"drive {drive_id} is not registered")

    def rested_h(self, utc_now_ns: int, *, exclude: str | None = None) -> float | None:
        """Hours since the latest live drive ended, or None when unknown.

        Only live drives with a UTC time count: a live drive re-analysed from its file keeps the
        live source but has no UTC start or end of its own, so it neither ends nor starts a rest.
        Unknown: no earlier live drive, the latest one has no recorded end (it never closed),
        or its end is after ``utc_now_ns`` (a clock step).
        """
        row = self._conn.execute(
            "SELECT utc_end_ns FROM drives_index WHERE source = 'live' "
            "AND (utc_start_ns IS NOT NULL OR utc_end_ns IS NOT NULL) AND drive_id IS NOT ? "
            "ORDER BY created_seq DESC LIMIT 1",
            (exclude,),
        ).fetchone()
        if row is None or row[0] is None or row[0] > utc_now_ns:
            return None
        return float((utc_now_ns - int(row[0])) / NS_PER_H)

    def update_summary(self, drive_id: str, summary: DriveSummary) -> None:
        n = self._run(
            "UPDATE drives_index SET t_start_ns = ?, t_end_ns = ?, distance_m = ?, n_events = ?, "
            "n_alerts = ?, closed_cleanly = ? WHERE drive_id = ?",
            (
                summary.t_start_ns,
                summary.t_end_ns,
                summary.distance_m,
                summary.n_events,
                summary.n_alerts,
                int(summary.closed_cleanly),
                drive_id,
            ),
        )
        if n == 0:
            raise KeyError(f"drive {drive_id} is not registered")

    def update_from_reader(self, reader: DriveReader) -> DriveSummary:
        summary = drive_summary(reader)
        self.update_summary(reader.meta().drive_id, summary)
        return summary

    @staticmethod
    def _item(r: tuple[object, ...]) -> DriveListItem:
        t0, t1 = r[3], r[4]
        duration = (t1 - t0) / NS_PER_S if isinstance(t0, int) and isinstance(t1, int) else None
        dist = r[5]
        return DriveListItem(
            drive_id=str(r[0]),
            label=str(r[2]),
            source=cast(DriveSource, r[1]),
            duration_s=duration,
            distance_m=float(dist) if isinstance(dist, int | float) else None,
            n_events=cast(int, r[6]),
            n_alerts=cast(int, r[7]),
            closed_cleanly=bool(r[8]),
        )

    _SELECT = (
        "SELECT drive_id, source, label, t_start_ns, t_end_ns, distance_m, n_events, n_alerts, "
        "closed_cleanly FROM drives_index"
    )

    def get(self, drive_id: str) -> DriveListItem | None:
        r = self._conn.execute(self._SELECT + " WHERE drive_id = ?", (drive_id,)).fetchone()
        return None if r is None else self._item(r)

    def put_baselines(self, rows: Iterable[BaselineLike], drive_id: str | None = None) -> None:
        """Store baseline stats; a row with the same (signal, bin, version) is replaced."""
        data = [
            (b.signal, b.bin, b.median, b.mad, b.n_windows, b.n_drives, b.version, drive_id)
            for b in rows
        ]
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.executemany(
                "INSERT OR REPLACE INTO baselines (signal, bin, median, mad, n_windows, n_drives, "
                "version, updated_drive) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                data,
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def get_baselines(self, signal: str | None = None, *, latest: bool = True) -> list[BaselineRow]:
        """Baselines ordered by signal and bin. ``latest`` keeps the highest version of each."""
        where = "" if signal is None else " WHERE b.signal = ?"
        params: tuple[object, ...] = () if signal is None else (signal,)
        if latest:
            sql = (
                "SELECT b.signal, b.bin, b.median, b.mad, b.n_windows, b.n_drives, b.version, "
                "b.updated_drive FROM baselines b JOIN (SELECT signal, bin, MAX(version) AS v "
                "FROM baselines GROUP BY signal, bin) m ON b.signal = m.signal AND b.bin = m.bin "
                "AND b.version = m.v" + where + " ORDER BY b.signal, b.bin"
            )
        else:
            sql = (
                "SELECT b.signal, b.bin, b.median, b.mad, b.n_windows, b.n_drives, b.version, "
                "b.updated_drive FROM baselines b" + where + " ORDER BY b.signal, b.bin, b.version"
            )
        return [BaselineRow(*r) for r in self._conn.execute(sql, params)]

    def list(self) -> list[DriveListItem]:
        """Every drive, newest registration first. Kept last so ``list`` stays the builtin above."""
        return [
            self._item(r) for r in self._conn.execute(self._SELECT + " ORDER BY created_seq DESC")
        ]
