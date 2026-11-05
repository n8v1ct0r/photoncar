"""Replay a Photoncar drive file (``store/schema.sql``) for re-analysis.

The file is opened read-only. By default only the captured inputs come back: ``CanFrame``,
``ObdPoll``, ``Sample`` (signals whose catalog source is not "derived"), ``GpsFix``, and
``ImuSample``. With ``derived=True`` the analysis outputs stored in the file come back as well:
``StateEstimate``, ``Event`` (with its citations), ``Lap``, and derived samples. Records are merged
in non-decreasing record time; ties keep the table order above.

NULL columns read back as NaN for floats (``alt_m``, ``speed_mps``, ``course_rad``, ``hdop``) and
0 for ``fix_type`` and ``n_sats``. The schema has no column for ``synthetic_signals``; signals
whose ``origin`` starts with "synthetic" are reported there.

The source streams as a replay (``meta.kind`` "replay", or "synthetic" for a synthetic drive):
it plays stored data, never the car. ``origin`` is the stored drive's own source, so a drive
re-analysed from a live drive is stored as live again (``pipeline.runner.drive_origin``) and
never passes for public data.
"""

from __future__ import annotations

import heapq
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

from photoncar.records import (
    CanFrame,
    Citation,
    Event,
    GpsFix,
    ImuSample,
    Lap,
    ObdPoll,
    Record,
    Sample,
    record_time,
)
from photoncar.sources.base import SourceMeta
from photoncar.store.meta import STATE_COLUMNS, DriveSource, state_from_row

_ORIGINS: dict[str, DriveSource] = {"live": "live", "replay": "replay", "synthetic": "synthetic"}


def _f(v: float | None) -> float:
    return math.nan if v is None else float(v)


class RecordedDriveSource:
    """A ``Source`` over a stored drive file."""

    def __init__(self, path: Path, *, derived: bool = False) -> None:
        self._path = Path(path)
        if not self._path.is_file():
            raise FileNotFoundError(f"drive file not found: {self._path}")
        self._derived = derived
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT drive_id, source, dataset_ref, t_start_ns, t_end_ns, vehicle_profile "
                "FROM drive LIMIT 1"
            ).fetchone()
            if row is None:
                raise ValueError(f"{self._path} has no drive row")
            synthetic = tuple(
                r[0]
                for r in con.execute(
                    "SELECT name FROM signals WHERE lower(origin) LIKE 'synthetic%' ORDER BY name"
                )
            )
        if row[1] not in _ORIGINS:
            raise ValueError(f"{self._path} has an unknown drive source {row[1]!r}")
        self.drive_id: str = row[0]
        self.drive_source: str = row[1]
        self._origin: DriveSource = _ORIGINS[row[1]]
        self.t_start_ns: int | None = row[3]
        self.t_end_ns: int | None = row[4]
        self.vehicle_profile: str | None = row[5]
        self._meta = SourceMeta(
            name=f"recorded:{self.drive_id}",
            kind="synthetic" if self.drive_source == "synthetic" else "replay",
            dataset_ref=row[2],
            synthetic_signals=synthetic,
        )

    @property
    def meta(self) -> SourceMeta:
        return self._meta

    @property
    def origin(self) -> DriveSource:
        """The stored drive's source: where its data first came from."""
        return self._origin

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path.resolve().as_uri() + "?mode=ro", uri=True)

    def records(self) -> Iterator[Record]:
        with closing(self._connect()) as con:
            streams: list[Iterator[Record]] = [
                self._can(con),
                self._obd(con),
                self._samples(con),
                self._gps(con),
                self._imu(con),
            ]
            if self._derived:
                streams += [self._state(con), self._events(con), self._laps(con)]
            yield from heapq.merge(*streams, key=record_time)

    def _can(self, con: sqlite3.Connection) -> Iterator[Record]:
        for t, bus, arb, ext, dlc, data, tx in con.execute(
            "SELECT t_ns, bus, arb_id, is_ext, dlc, data, is_tx FROM raw_can ORDER BY t_ns, rowid"
        ):
            yield CanFrame(t, bus, arb, bool(ext), dlc, bytes(data), bool(tx))

    def _obd(self, con: sqlite3.Connection) -> Iterator[Record]:
        for req, resp, service, pid, ecu, status, raw, value, unit in con.execute(
            "SELECT req_t_ns, resp_t_ns, service, pid, ecu, status, raw, value, unit "
            "FROM obd_polls ORDER BY coalesce(resp_t_ns, req_t_ns), rowid"
        ):
            yield ObdPoll(req, resp, pid, ecu, status, bytes(raw), value, unit, service)

    def _samples(self, con: sqlite3.Connection) -> Iterator[Record]:
        where = "" if self._derived else "WHERE g.source != 'derived' "
        for t, name, value in con.execute(
            "SELECT s.t_ns, g.name, s.value FROM samples s "
            "JOIN signals g ON g.signal_id = s.signal_id "
            f"{where}ORDER BY s.t_ns, s.rowid"
        ):
            yield Sample(t, name, value)

    def _gps(self, con: sqlite3.Connection) -> Iterator[Record]:
        for row in con.execute(
            "SELECT t_ns, rx_t_ns, utc_ns, lat_deg, lon_deg, alt_m, speed_mps, course_rad, hdop, "
            "fix_type, n_sats FROM gps_fixes ORDER BY t_ns, rowid"
        ):
            t, rx, utc, lat, lon, alt, speed, course, hdop, fix, sats = row
            yield GpsFix(
                t, rx, utc, lat, lon, _f(alt), _f(speed), _f(course), _f(hdop), fix or 0, sats or 0
            )

    def _imu(self, con: sqlite3.Connection) -> Iterator[Record]:
        for t, ax, ay, az, gx, gy, gz, temp in con.execute(
            "SELECT t_ns, ax, ay, az, gx, gy, gz, temp_k FROM imu_samples ORDER BY t_ns, rowid"
        ):
            yield ImuSample(t, ax, ay, az, gx, gy, gz, temp)

    def _state(self, con: sqlite3.Connection) -> Iterator[Record]:
        for row in con.execute(f"SELECT {STATE_COLUMNS} FROM state ORDER BY t_ns, rowid"):
            yield state_from_row(row)

    def _events(self, con: sqlite3.Connection) -> Iterator[Record]:
        cites: dict[int, list[Citation]] = defaultdict(list)
        for eid, *c in con.execute(
            "SELECT event_id, signal, source, role, t_start_ns, t_end_ns, stat, stat_value, "
            "unit, n FROM event_citations ORDER BY event_id, rowid"
        ):
            cites[eid].append(Citation(*c))
        for row in con.execute(
            "SELECT event_id, kind, rule_id, rule_version, severity, t_start_ns, t_end_ns, value, "
            "threshold, unit, message, resolved, synthetic FROM events "
            "ORDER BY t_start_ns, event_id"
        ):
            eid, kind, rule_id, ver, sev, t0, t1, value, thr, unit, msg, resolved, synth = row
            yield Event(
                kind,
                rule_id,
                ver,
                sev,
                t0,
                t1,
                value,
                thr,
                unit,
                msg,
                citations=tuple(cites.get(eid, ())),
                resolved=bool(resolved),
                synthetic=bool(synth),
            )

    def _laps(self, con: sqlite3.Connection) -> Iterator[Record]:
        for line_id, lap_no, t0, t1, dur, valid in con.execute(
            "SELECT line_id, lap_no, t_start_ns, t_end_ns, duration_ns, valid FROM laps "
            "ORDER BY t_start_ns, lap_id"
        ):
            yield Lap(line_id, lap_no, t0, t1, dur, bool(valid))
