"""The drive writer: 1 SQLite file per drive.

- ``journal_mode=WAL`` and ``synchronous=FULL``: readers never block the writer, and a
  committed batch survives a power cut.
- 1 writer. Records buffer in memory and commit as 1 transaction whenever record time has
  advanced ``batch_ms`` past the previous commit, so a crash loses at most about 1 batch.
- Tables are append-only; indexes are built at close.
- A clean close builds the 10 Hz rollup (unless ``write_timeline`` already wrote it), runs
  ``wal_checkpoint(TRUNCATE)``, and sets ``closed_cleanly = 1``. Nothing else sets it.

Values that SQLite cannot keep are refused at ``write`` time with ``ValueError`` (NaN in a
NOT NULL column, a non-service-01 OBD poll, 2 citations sharing signal and role), so a bad
record never poisons a batch.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from types import TracebackType

import numpy as np
from numpy.typing import ArrayLike

from photoncar.records import (
    CanFrame,
    DriveStat,
    Event,
    GpsFix,
    ImuSample,
    Lap,
    ObdPoll,
    Record,
    Sample,
    StateEstimate,
    record_time,
)
from photoncar.signals import SIGNALS
from photoncar.store.meta import STATE_COLUMNS, DriveMeta, drive_schema_sql, state_row
from photoncar.store.rollup import TimelineRow, build_timeline_10hz, load_rollup_inputs

JOURNAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024

INDEXES: dict[str, str] = {
    "raw_can_t": "raw_can (t_ns)",
    "samples_signal_t": "samples (signal_id, t_ns)",
    "gps_fixes_t": "gps_fixes (t_ns)",
    "imu_samples_t": "imu_samples (t_ns)",
    "obd_polls_t": "obd_polls (req_t_ns)",
    "state_t": "state (t_ns)",
    "events_t": "events (t_start_ns)",
    "laps_t": "laps (t_start_ns)",
}

_INSERT: dict[str, str] = {
    "signals": "INSERT INTO signals (signal_id, name, unit, source, nominal_hz, origin) "
    "VALUES (?, ?, ?, ?, ?, ?)",
    "raw_can": "INSERT INTO raw_can (t_ns, bus, arb_id, is_ext, dlc, data, is_tx) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)",
    "samples": "INSERT INTO samples (t_ns, signal_id, value) VALUES (?, ?, ?)",
    "gps_fixes": "INSERT INTO gps_fixes (t_ns, rx_t_ns, utc_ns, lat_deg, lon_deg, alt_m, "
    "speed_mps, course_rad, hdop, fix_type, n_sats) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    "imu_samples": "INSERT INTO imu_samples (t_ns, ax, ay, az, gx, gy, gz, temp_k) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    "obd_polls": "INSERT INTO obd_polls (req_t_ns, resp_t_ns, service, pid, ecu, status, raw, "
    "value, unit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
    "state": f"INSERT INTO state ({STATE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    "events": "INSERT INTO events (event_id, kind, rule_id, rule_version, severity, t_start_ns, "
    "t_end_ns, value, threshold, unit, message, resolved, synthetic) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    "event_citations": "INSERT INTO event_citations (event_id, signal, source, role, "
    "t_start_ns, t_end_ns, stat, stat_value, unit, n) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    "laps": "INSERT INTO laps (line_id, lap_no, t_start_ns, t_end_ns, duration_ns, valid) "
    "VALUES (?, ?, ?, ?, ?, ?)",
}

_TIMELINE_INSERT = (
    "INSERT OR REPLACE INTO timeline_10hz (t_ns, speed_mps, rpm, throttle, coolant_k, "
    "accel_long_mps2, accel_lat_mps2, x_m, y_m, dr) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

Row = tuple[object, ...]


def _finite(name: str, x: float) -> float:
    if not math.isfinite(x):
        raise ValueError(f"{name} must be finite, got {x!r}")
    return float(x)


def _nullable(name: str, x: float | None) -> float | None:
    """NaN means "no value" and is stored as NULL. Infinity is refused."""
    if x is None or math.isnan(x):
        return None
    if math.isinf(x):
        raise ValueError(f"{name} must not be infinite")
    return float(x)


def _signal_source(name: str) -> str:
    prefix = name.split(".", 1)[0]
    return prefix if prefix in ("can", "obd", "gps", "imu") else "derived"


def _record_end(rec: Record) -> int:
    if isinstance(rec, Event | Lap):
        return rec.t_end_ns
    if isinstance(rec, ObdPoll):
        return max(rec.req_t_ns, rec.t_ns)
    return record_time(rec)


class DriveWriter:
    """Write 1 drive to a new SQLite file. Use from 1 thread."""

    def __init__(self, path: Path | str, meta: DriveMeta, batch_ms: int = 1000) -> None:
        if batch_ms <= 0:
            raise ValueError("batch_ms must be positive")
        self.path = Path(path)
        if self.path.exists():
            raise FileExistsError(f"drive file already exists: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.meta = meta
        self._batch_ns = batch_ms * 1_000_000
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"could not enable WAL, journal_mode is {mode}")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT_BYTES}")
        self._conn.executescript(drive_schema_sql())

        self._pending: dict[str, list[Row]] = {table: [] for table in _INSERT}
        self._signal_ids: dict[str, int] = {}
        self._next_event_id = 1
        self._t_min: int | None = None
        self._t_max: int | None = None
        self._high_ns: int | None = None
        self._batch_start_ns: int | None = None
        self._timeline_written = False
        self._closed = False

        for name in meta.synthetic_signals:
            self._signal_id(name)
        rot = None if meta.imu_mount_rot is None else json.dumps(meta.imu_mount_rot)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT INTO drive (drive_id, source, dataset_ref, boot_id, t_start_ns, t_end_ns, "
                "utc_offset_ns, utc_fit_rms_ns, vehicle_profile, dbc_name, dbc_sha256, "
                "imu_mount_rot, schema_version, closed_cleanly) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    meta.drive_id,
                    meta.source,
                    meta.dataset_ref,
                    meta.boot_id,
                    meta.t_start_ns,
                    meta.t_end_ns,
                    meta.utc_offset_ns,
                    meta.utc_fit_rms_ns,
                    meta.vehicle_profile,
                    meta.dbc_name,
                    meta.dbc_sha256,
                    rot,
                    meta.schema_version,
                ),
            )
            self._insert_pending()
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._clear_pending()

    # -- context manager -------------------------------------------------------------------

    def __enter__(self) -> DriveWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close(clean=exc_type is None)

    # -- introspection ---------------------------------------------------------------------

    def pragmas(self) -> dict[str, int | str]:
        """The live connection's durability settings."""
        self._check_open()
        return {
            "journal_mode": str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]),
            "synchronous": int(self._conn.execute("PRAGMA synchronous").fetchone()[0]),
            "journal_size_limit": int(
                self._conn.execute("PRAGMA journal_size_limit").fetchone()[0]
            ),
        }

    @property
    def closed(self) -> bool:
        return self._closed

    # -- writing ---------------------------------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("drive writer is closed")

    def _signal_id(self, name: str) -> int:
        sid = self._signal_ids.get(name)
        if sid is not None:
            return sid
        sid = len(self._signal_ids) + 1
        self._signal_ids[name] = sid
        sig = SIGNALS.get(name)
        if name in self.meta.synthetic_signals:
            origin = "synthetic"
        elif sig is not None:
            origin = "catalog"
        else:
            origin = "unlisted"
        unit = sig.unit if sig is not None else ""
        source = sig.source if sig is not None else _signal_source(name)
        nominal_hz = sig.nominal_hz if sig is not None else None
        self._pending["signals"].append((sid, name, unit, source, nominal_hz, origin))
        return sid

    def _event_rows(self, ev: Event) -> None:
        seen: set[tuple[str, str]] = set()
        for c in ev.citations:
            key = (c.signal, c.role)
            if key in seen:
                raise ValueError(
                    f"event {ev.kind} has 2 {c.role} citations on {c.signal}; the schema keeps 1"
                )
            seen.add(key)
            _finite("citation stat_value", c.stat_value)
        eid = self._next_event_id
        self._next_event_id += 1
        self._pending["events"].append(
            (
                eid,
                ev.kind,
                ev.rule_id,
                ev.rule_version,
                ev.severity,
                ev.t_start_ns,
                ev.t_end_ns,
                _finite("event value", ev.value),
                _nullable("event threshold", ev.threshold),
                ev.unit,
                ev.message,
                int(ev.resolved),
                int(ev.synthetic),
            )
        )
        for c in ev.citations:
            self._pending["event_citations"].append(
                (
                    eid,
                    c.signal,
                    c.source,
                    c.role,
                    c.t_start_ns,
                    c.t_end_ns,
                    c.stat,
                    float(c.stat_value),
                    c.unit,
                    c.n,
                )
            )

    def write(self, rec: Record) -> None:
        """Buffer 1 record; commit the batch once record time passes the batch interval."""
        self._check_open()
        p = self._pending
        match rec:
            case CanFrame():
                p["raw_can"].append(
                    (
                        rec.t_ns,
                        rec.bus,
                        rec.arb_id,
                        int(rec.is_ext),
                        rec.dlc,
                        bytes(rec.data),
                        int(rec.is_tx),
                    )
                )
            case ObdPoll():
                if rec.service != 1:
                    raise ValueError(f"only service 01 polls are stored, got {rec.service}")
                p["obd_polls"].append(
                    (
                        rec.req_t_ns,
                        rec.resp_t_ns,
                        rec.service,
                        rec.pid,
                        rec.ecu,
                        rec.status,
                        bytes(rec.raw),
                        _nullable("obd value", rec.value),
                        rec.unit,
                    )
                )
            case GpsFix():
                p["gps_fixes"].append(
                    (
                        rec.t_ns,
                        rec.rx_t_ns,
                        rec.utc_ns,
                        _finite("lat_deg", rec.lat_deg),
                        _finite("lon_deg", rec.lon_deg),
                        _nullable("alt_m", rec.alt_m),
                        _nullable("speed_mps", rec.speed_mps),
                        _nullable("course_rad", rec.course_rad),
                        _nullable("hdop", rec.hdop),
                        rec.fix_type,
                        rec.n_sats,
                    )
                )
            case ImuSample():
                p["imu_samples"].append(
                    (
                        rec.t_ns,
                        _finite("ax", rec.ax),
                        _finite("ay", rec.ay),
                        _finite("az", rec.az),
                        _finite("gx", rec.gx),
                        _finite("gy", rec.gy),
                        _finite("gz", rec.gz),
                        _nullable("temp_k", rec.temp_k),
                    )
                )
            case Sample():
                value = _finite(f"sample {rec.signal}", rec.value)
                p["samples"].append((rec.t_ns, self._signal_id(rec.signal), value))
            case StateEstimate():
                p["state"].append(state_row(rec))
            case Event():
                self._event_rows(rec)
            case Lap():
                p["laps"].append(
                    (
                        rec.line_id,
                        rec.lap_no,
                        rec.t_start_ns,
                        rec.t_end_ns,
                        rec.duration_ns,
                        int(rec.valid),
                    )
                )
            case _:
                raise TypeError(f"cannot store {type(rec).__name__}")

        t = record_time(rec)
        end = _record_end(rec)
        self._t_min = t if self._t_min is None else min(self._t_min, t)
        self._t_max = end if self._t_max is None else max(self._t_max, end)
        self._high_ns = t if self._high_ns is None else max(self._high_ns, t)
        if self._batch_start_ns is None:
            self._batch_start_ns = t
        if self._high_ns - self._batch_start_ns >= self._batch_ns:
            self.flush()
            self._batch_start_ns = self._high_ns

    def write_many(self, records: Iterable[Record]) -> None:
        for rec in records:
            self.write(rec)

    def _insert_pending(self) -> None:
        for table, rows in self._pending.items():
            if rows:
                self._conn.executemany(_INSERT[table], rows)

    def flush(self) -> None:
        """Commit every buffered record now, in 1 transaction."""
        self._check_open()
        if not any(self._pending.values()):
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._insert_pending()
            self._conn.execute(
                "UPDATE drive SET t_start_ns = ?, t_end_ns = ?", (self._t_min, self._t_max)
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._clear_pending()

    def _clear_pending(self) -> None:
        for rows in self._pending.values():
            rows.clear()

    def _transaction(self, sql: str, rows: Iterable[Row]) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.executemany(sql, rows)
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def write_stats(self, stats: Iterable[DriveStat]) -> None:
        """Store drive-level stats (replacing any with the same stat_id). Commits at once."""
        self._check_open()
        rows = [
            (s.stat_id, s.label, _finite(f"stat {s.stat_id}", s.value), s.unit, s.display)
            for s in stats
        ]
        self._transaction(
            "INSERT OR REPLACE INTO drive_stats (stat_id, label, value, unit, display) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )

    def write_timeline(self, rows: Iterable[TimelineRow]) -> None:
        """Store 10 Hz rollup rows. When called, close() does not rebuild the rollup."""
        self._check_open()
        data = [
            (
                r.t_ns,
                _nullable("speed_mps", r.speed_mps),
                _nullable("rpm", r.rpm),
                _nullable("throttle", r.throttle),
                _nullable("coolant_k", r.coolant_k),
                _nullable("accel_long_mps2", r.accel_long_mps2),
                _nullable("accel_lat_mps2", r.accel_lat_mps2),
                _nullable("x_m", r.x_m),
                _nullable("y_m", r.y_m),
                int(r.dr),
            )
            for r in rows
        ]
        self._transaction(_TIMELINE_INSERT, data)
        self._timeline_written = True

    def set_mount_rotation(self, rot: ArrayLike) -> None:
        """Store the 3x3 sensor-to-vehicle IMU rotation (row-major JSON). Commits at once."""
        self._check_open()
        arr = np.asarray(rot, dtype=np.float64)
        if arr.shape != (3, 3) or not bool(np.all(np.isfinite(arr))):
            raise ValueError("mount rotation must be a finite 3x3 matrix")
        text = json.dumps([[float(v) for v in row] for row in arr.tolist()])
        self._transaction("UPDATE drive SET imu_mount_rot = ?", [(text,)])

    def close(self, clean: bool = True) -> None:
        """Commit, build indexes, and close. Only ``clean=True`` sets ``closed_cleanly``.

        A clean close also builds the 10 Hz rollup when ``write_timeline`` was not called and
        truncates the WAL.
        """
        if self._closed:
            return
        try:
            self.flush()
            if clean and not self._timeline_written:
                self.write_timeline(build_timeline_10hz(load_rollup_inputs(self._conn)))
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for name, target in INDEXES.items():
                    self._conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
                if clean:
                    self._conn.execute("UPDATE drive SET closed_cleanly = 1")
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            if clean:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self._conn.close()
            self._closed = True
