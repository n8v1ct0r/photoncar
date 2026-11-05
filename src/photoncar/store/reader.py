"""Read a drive file.

The reader opens the file read-only (``mode=ro``), so it works while the writer is still
running and after a crash: it sees every committed batch.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterator, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray

from photoncar.records import (
    CanFrame,
    Citation,
    CitationRole,
    DriveStat,
    Event,
    GpsFix,
    ImuSample,
    Lap,
    ObdPoll,
    ObdStatus,
    Sample,
    StateEstimate,
)
from photoncar.store.meta import (
    GPS_FIX,
    STATE_COLUMNS,
    DriveMeta,
    DriveSource,
    Matrix3,
    Row3,
    state_from_row,
)
from photoncar.store.rollup import (
    COOLANT_SOURCES,
    MIN_KEPT_FIX_TYPE,
    RPM_SOURCES,
    SPEED_FALLBACK_SOURCES,
    THROTTLE_SOURCES,
    RollupInputs,
    TimelineRow,
    build_timeline_10hz,
    load_rollup_inputs,
    ranked_sources,
)
from photoncar.units import G0, KELVIN_OFFSET, NS_PER_S

ALERTS_WITHOUT_TRIGGER_SQL = (
    "SELECT event_id FROM events WHERE kind LIKE 'alert.%' AND event_id NOT IN "
    "(SELECT event_id FROM event_citations WHERE role = 'trigger')"
)
"""The citation audit (hard rule 8). Must return 0 rows for every drive."""

TIMELINE_KEYS = (
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
)

F64 = NDArray[np.float64]
_SOURCES: tuple[DriveSource, ...] = ("live", "replay", "synthetic")
_ROLES: tuple[CitationRole, ...] = ("trigger", "context")
_STATUSES: tuple[ObdStatus, ...] = ("ok", "timeout", "nrc")


def _nan(x: float | None) -> float:
    return float("nan") if x is None else float(x)


def _opt(x: float) -> float | None:
    return None if np.isnan(x) else float(x)


def _matrix(text: str | None) -> Matrix3 | None:
    if text is None:
        return None
    rows = json.loads(text)
    if len(rows) != 3 or any(len(r) != 3 for r in rows):
        raise ValueError("imu_mount_rot is not a 3x3 matrix")
    r0, r1, r2 = (cast(Row3, tuple(float(v) for v in r)) for r in rows)
    return (r0, r1, r2)


POSITION_KEYS = ("x_m", "y_m")
"""Timeline columns decimated as samples of their own rows (see ``decimate_minmax``)."""


def decimate_minmax(
    t: F64, cols: Mapping[str, F64], max_points: int, *, sampled: Collection[str] = ()
) -> tuple[F64, dict[str, F64]]:
    """Min/max-preserving decimation to at most ``max_points`` points.

    Rows split into ``max_points // 2`` contiguous buckets. Each bucket emits 2 points, at the
    bucket's first and last time; each column puts its bucket min and max in those 2 slots, in
    the order they occurred. So every bucket's min and max survive for every column. NaN
    means no value; a bucket with no values stays NaN.

    A column named in ``sampled`` instead keeps the values of the bucket's first and last rows,
    so each of its points is the value at that point's own time. Positions need that: a min
    and a max moved onto another row's time would put the car where it was not at that time,
    and x and y taken from different rows would make a place it never was.
    """
    if max_points < 2:
        raise ValueError("max_points must be at least 2")
    n = t.size
    if n <= max_points:
        return t.copy(), {k: v.copy() for k, v in cols.items()}
    b = max_points // 2
    edges = (np.arange(b + 1, dtype=np.int64) * n) // b
    starts, ends = edges[:-1], edges[1:]
    t_out = np.empty(2 * b, dtype=np.float64)
    t_out[0::2] = t[starts]
    t_out[1::2] = t[ends - 1]
    bucket = np.repeat(np.arange(b), ends - starts)
    idx = np.arange(n)
    out: dict[str, F64] = {}
    for name, v in cols.items():
        if name in sampled:
            col = np.empty(2 * b, dtype=np.float64)
            col[0::2] = v[starts]
            col[1::2] = v[ends - 1]
            out[name] = col
            continue
        mn = np.fmin.reduceat(v, starts)
        mx = np.fmax.reduceat(v, starts)
        i_min = np.minimum.reduceat(np.where(v == mn[bucket], idx, n), starts)
        i_max = np.minimum.reduceat(np.where(v == mx[bucket], idx, n), starts)
        min_first = i_min <= i_max
        col = np.empty(2 * b, dtype=np.float64)
        col[0::2] = np.where(min_first, mn, mx)
        col[1::2] = np.where(min_first, mx, mn)
        out[name] = col
    return t_out, out


class DriveReader:
    """Read-only access to 1 drive file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"no drive file at {self.path}")
        uri = self.path.resolve().as_uri() + "?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        self._conn.execute("PRAGMA query_only=ON")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> DriveReader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- drive-level -----------------------------------------------------------------------

    def meta(self) -> DriveMeta:
        row = self._conn.execute(
            "SELECT drive_id, source, dataset_ref, boot_id, t_start_ns, t_end_ns, utc_offset_ns, "
            "utc_fit_rms_ns, vehicle_profile, dbc_name, dbc_sha256, imu_mount_rot, "
            "schema_version, closed_cleanly FROM drive LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValueError(f"{self.path} has no drive row")
        source = row[1]
        if source not in _SOURCES:
            raise ValueError(f"unknown drive source {source!r}")
        synthetic = tuple(
            r[0]
            for r in self._conn.execute(
                "SELECT name FROM signals WHERE origin = 'synthetic' ORDER BY signal_id"
            )
        )
        return DriveMeta(
            drive_id=row[0],
            source=cast(DriveSource, source),
            dataset_ref=row[2],
            boot_id=row[3],
            t_start_ns=row[4],
            t_end_ns=row[5],
            utc_offset_ns=row[6],
            utc_fit_rms_ns=row[7],
            vehicle_profile=row[8],
            dbc_name=row[9],
            dbc_sha256=row[10],
            imu_mount_rot=_matrix(row[11]),
            schema_version=row[12],
            closed_cleanly=bool(row[13]),
            synthetic_signals=synthetic,
        )

    def signals(self) -> list[str]:
        return [r[0] for r in self._conn.execute("SELECT name FROM signals ORDER BY signal_id")]

    def stats(self) -> list[DriveStat]:
        return [
            DriveStat(stat_id=r[0], label=r[1], value=r[2], unit=r[3], display=r[4])
            for r in self._conn.execute(
                "SELECT stat_id, label, value, unit, display FROM drive_stats ORDER BY rowid"
            )
        ]

    # -- events and laps -------------------------------------------------------------------

    def events_with_ids(self) -> list[tuple[int, Event]]:
        """Events with their stored IDs and citations, ordered by start time."""
        cites: dict[int, list[Citation]] = {}
        for r in self._conn.execute(
            "SELECT event_id, signal, source, role, t_start_ns, t_end_ns, stat, stat_value, "
            "unit, n FROM event_citations ORDER BY event_id, rowid"
        ):
            role = r[3]
            if role not in _ROLES:
                raise ValueError(f"unknown citation role {role!r}")
            cites.setdefault(r[0], []).append(
                Citation(
                    signal=r[1],
                    source=r[2],
                    role=cast(CitationRole, role),
                    t_start_ns=r[4],
                    t_end_ns=r[5],
                    stat=r[6],
                    stat_value=r[7],
                    unit=r[8],
                    n=r[9],
                )
            )
        out: list[tuple[int, Event]] = []
        for r in self._conn.execute(
            "SELECT event_id, kind, rule_id, rule_version, severity, t_start_ns, t_end_ns, value, "
            "threshold, unit, message, resolved, synthetic FROM events "
            "ORDER BY t_start_ns, event_id"
        ):
            out.append(
                (
                    r[0],
                    Event(
                        kind=r[1],
                        rule_id=r[2],
                        rule_version=r[3],
                        severity=r[4],
                        t_start_ns=r[5],
                        t_end_ns=r[6],
                        value=r[7],
                        threshold=r[8],
                        unit=r[9],
                        message=r[10],
                        citations=tuple(cites.get(r[0], ())),
                        resolved=bool(r[11]),
                        synthetic=bool(r[12]),
                    ),
                )
            )
        return out

    def events(self) -> list[Event]:
        return [ev for _, ev in self.events_with_ids()]

    def alerts_without_trigger(self) -> list[int]:
        """IDs of alerts lacking a trigger citation. Always empty for a valid drive."""
        return [r[0] for r in self._conn.execute(ALERTS_WITHOUT_TRIGGER_SQL)]

    def laps(self) -> list[Lap]:
        return [
            Lap(
                line_id=r[0],
                lap_no=r[1],
                t_start_ns=r[2],
                t_end_ns=r[3],
                duration_ns=r[4],
                valid=bool(r[5]),
            )
            for r in self._conn.execute(
                "SELECT line_id, lap_no, t_start_ns, t_end_ns, duration_ns, valid FROM laps "
                "ORDER BY t_start_ns, lap_no"
            )
        ]

    # -- raw and derived streams -----------------------------------------------------------

    def samples(self, signal: str) -> list[Sample]:
        return [
            Sample(t_ns=r[0], signal=signal, value=r[1])
            for r in self._conn.execute(
                "SELECT s.t_ns, s.value FROM samples s JOIN signals g ON s.signal_id = g.signal_id "
                "WHERE g.name = ? ORDER BY s.t_ns, s.rowid",
                (signal,),
            )
        ]

    def sample_arrays(self, signal: str) -> tuple[NDArray[np.int64], F64]:
        rows = self._conn.execute(
            "SELECT s.t_ns, s.value FROM samples s JOIN signals g ON s.signal_id = g.signal_id "
            "WHERE g.name = ? ORDER BY s.t_ns, s.rowid",
            (signal,),
        ).fetchall()
        t = np.array([r[0] for r in rows], dtype=np.int64)
        v = np.array([r[1] for r in rows], dtype=np.float64)
        return t, v

    def gps_fixes(self) -> list[GpsFix]:
        return [
            GpsFix(
                t_ns=r[0],
                rx_t_ns=r[1],
                utc_ns=r[2],
                lat_deg=r[3],
                lon_deg=r[4],
                alt_m=_nan(r[5]),
                speed_mps=_nan(r[6]),
                course_rad=_nan(r[7]),
                hdop=_nan(r[8]),
                fix_type=-1 if r[9] is None else r[9],
                n_sats=0 if r[10] is None else r[10],
            )
            for r in self._conn.execute(
                "SELECT t_ns, rx_t_ns, utc_ns, lat_deg, lon_deg, alt_m, speed_mps, course_rad, "
                "hdop, fix_type, n_sats FROM gps_fixes ORDER BY t_ns, rowid"
            )
        ]

    def iter_imu_samples(self) -> Iterator[ImuSample]:
        for r in self._conn.execute(
            "SELECT t_ns, ax, ay, az, gx, gy, gz, temp_k FROM imu_samples ORDER BY t_ns, rowid"
        ):
            yield ImuSample(
                t_ns=r[0], ax=r[1], ay=r[2], az=r[3], gx=r[4], gy=r[5], gz=r[6], temp_k=r[7]
            )

    def iter_can_frames(self) -> Iterator[CanFrame]:
        for r in self._conn.execute(
            "SELECT t_ns, bus, arb_id, is_ext, dlc, data, is_tx FROM raw_can ORDER BY t_ns, rowid"
        ):
            yield CanFrame(
                t_ns=r[0],
                bus=r[1],
                arb_id=r[2],
                is_ext=bool(r[3]),
                dlc=r[4],
                data=bytes(r[5]),
                is_tx=bool(r[6]),
            )

    def obd_polls(self) -> list[ObdPoll]:
        out: list[ObdPoll] = []
        for r in self._conn.execute(
            "SELECT req_t_ns, resp_t_ns, service, pid, ecu, status, raw, value, unit "
            "FROM obd_polls ORDER BY req_t_ns, rowid"
        ):
            if r[5] not in _STATUSES:
                raise ValueError(f"unknown OBD status {r[5]!r}")
            out.append(
                ObdPoll(
                    req_t_ns=r[0],
                    resp_t_ns=r[1],
                    service=r[2],
                    pid=r[3],
                    ecu=r[4],
                    status=cast(ObdStatus, r[5]),
                    raw=bytes(r[6]),
                    value=r[7],
                    unit=r[8],
                )
            )
        return out

    def state(self) -> list[StateEstimate]:
        """State estimates in time order. Lateral fields of a speed-only estimate are NaN."""
        return [
            state_from_row(r)
            for r in self._conn.execute(f"SELECT {STATE_COLUMNS} FROM state ORDER BY t_ns, rowid")
        ]

    # -- timeline --------------------------------------------------------------------------

    def rollup_inputs(self) -> RollupInputs:
        return load_rollup_inputs(self._conn)

    def timeline_rows(self) -> list[TimelineRow]:
        """The stored 10 Hz rows, or rows built on the fly when the drive never closed."""
        rows = [
            TimelineRow(
                t_ns=r[0],
                speed_mps=r[1],
                rpm=r[2],
                throttle=r[3],
                coolant_k=r[4],
                accel_long_mps2=r[5],
                accel_lat_mps2=r[6],
                x_m=r[7],
                y_m=r[8],
                dr=bool(r[9]),
            )
            for r in self._conn.execute(
                "SELECT t_ns, speed_mps, rpm, throttle, coolant_k, accel_long_mps2, "
                "accel_lat_mps2, x_m, y_m, dr FROM timeline_10hz ORDER BY t_ns"
            )
        ]
        return rows if rows else build_timeline_10hz(self)

    def _has(self, sql: str, params: tuple[object, ...] = ()) -> bool:
        return bool(self._conn.execute(f"SELECT EXISTS ({sql})", params).fetchone()[0])

    def _has_samples(self, name: str) -> bool:
        return self._has(
            "SELECT 1 FROM samples s JOIN signals g ON s.signal_id = g.signal_id WHERE g.name = ?",
            (name,),
        )

    def timeline_sources(self) -> dict[str, str | None]:
        """The catalog signal that feeds each timeline column, or None when nothing does.

        The same choice the rollup makes: speed and longitudinal accel from the estimator
        (``state.*``), speed else from GPS, else the measured OBD or wheel speed; lateral accel
        only from estimates made with an IMU; rpm, throttle, and coolant from a measured signal
        when the drive has one, else the synthetic one; positions from the GPS fixes.
        """
        synthetic = set(self.meta().synthetic_signals)
        present = {
            name
            for name, _ in RPM_SOURCES + THROTTLE_SOURCES + COOLANT_SOURCES + SPEED_FALLBACK_SOURCES
            if self._has_samples(name)
        }

        def first(sources: tuple[tuple[str, int], ...]) -> str | None:
            ranked = ranked_sources(sources, synthetic, present)
            return ranked[0][0] if ranked else None

        has_state = self._has("SELECT 1 FROM state")
        has_lateral = self._has("SELECT 1 FROM state WHERE '+' || inputs || '+' LIKE '%+imu+%'")
        has_fix = self._has("SELECT 1 FROM gps_fixes WHERE fix_type >= ?", (MIN_KEPT_FIX_TYPE,))
        if has_state:
            speed: str | None = "state.speed"
        elif has_fix:
            speed = "gps.speed"
        else:
            speed = first(SPEED_FALLBACK_SOURCES)
        return {
            "speed_mps": speed,
            "rpm": first(RPM_SOURCES),
            "throttle": first(THROTTLE_SOURCES),
            "coolant_c": first(COOLANT_SOURCES),
            "accel_long_g": "state.accel_long" if has_state else None,
            "accel_lat_g": "state.accel_lat" if has_lateral else None,
            "x_m": GPS_FIX if has_fix else None,
            "y_m": GPS_FIX if has_fix else None,
        }

    def timeline(
        self, max_points: int = 1500, *, trim_ns: Sequence[tuple[int, int]] = ()
    ) -> dict[str, Any]:
        """The review timeline in API units, decimated to at most ``max_points`` points.

        Keys: ``t_s`` (seconds from drive start), ``speed_mps``, ``rpm``, ``throttle`` (0..1),
        ``coolant_c``, ``accel_long_g``, ``accel_lat_g``, ``x_m``, ``y_m`` (null where no
        value), ``dr`` (booleans), and ``sources`` (``timeline_sources``). Positions are
        decimated as samples of their own rows (``POSITION_KEYS``), the other values as min and
        max.

        ``trim_ns`` (the privacy trim windows, closed intervals of ``t_ns``) empties every row
        inside a window before decimation: its values become null and ``dr`` false. So no value
        from inside a window reaches any point, whichever time the decimation gives it.
        """
        rows = self.timeline_rows()
        t_start = self.meta().t_start_ns
        t0 = t_start if t_start is not None else (rows[0].t_ns if rows else 0)
        t_ns = np.array([r.t_ns for r in rows], dtype=np.int64)
        t = np.array([(r.t_ns - t0) / NS_PER_S for r in rows], dtype=np.float64)
        cols: dict[str, F64] = {
            "speed_mps": np.array([_nan(r.speed_mps) for r in rows], dtype=np.float64),
            "rpm": np.array([_nan(r.rpm) for r in rows], dtype=np.float64),
            "throttle": np.array([_nan(r.throttle) for r in rows], dtype=np.float64),
            "coolant_c": np.array([_nan(r.coolant_k) for r in rows], dtype=np.float64)
            - KELVIN_OFFSET,
            "accel_long_g": np.array([_nan(r.accel_long_mps2) for r in rows], dtype=np.float64)
            / G0,
            "accel_lat_g": np.array([_nan(r.accel_lat_mps2) for r in rows], dtype=np.float64) / G0,
            "x_m": np.array([_nan(r.x_m) for r in rows], dtype=np.float64),
            "y_m": np.array([_nan(r.y_m) for r in rows], dtype=np.float64),
            "dr": np.array([1.0 if r.dr else 0.0 for r in rows], dtype=np.float64),
        }
        if trim_ns:
            inside = np.zeros(t_ns.shape, dtype=np.bool_)
            for lo, hi in trim_ns:
                inside |= (t_ns >= lo) & (t_ns <= hi)
            for name, col in cols.items():
                col[inside] = 0.0 if name == "dr" else np.nan
        t_out, out = decimate_minmax(t, cols, max_points, sampled=POSITION_KEYS)
        result: dict[str, Any] = {"t_s": [float(x) for x in t_out]}
        for key in TIMELINE_KEYS[1:-1]:
            result[key] = [_opt(x) for x in out[key]]
        result["dr"] = [bool(x > 0.5) for x in out["dr"]]
        result["sources"] = self.timeline_sources()
        return result
