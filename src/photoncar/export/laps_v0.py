"""Lap export in the Photoncar lap v0 format (docs/phase-1-plan.md section 5).

Each lap becomes 1 JSON file of columnar arrays at 10 Hz, on a grid aligned to the lap start
(``t_ms`` = 0, 100, 200, ...). Values come from the drive's 10 Hz timeline, interpolated only
between adjacent timeline rows.

- ``x``/``y`` are meters east and north of the start and finish line midpoint. The drive file
  does not store line geometry, so pass ``lines`` (line_id to x1, y1, x2, y2 in the drive's
  local frame) for the midpoint. Without it, the origin is the position at the first recorded
  crossing of that line, which lies on the line.
- ``brake`` and ``gear`` are null: no drive carries those signals yet.
- Privacy: a lap whose time range, widened by 1 row, touches any trim window is not exported.
- Every document is checked against ``lap.v0.schema.json`` (JSON Schema 2020-12) before its file
  is written, and files are written only under ``<data_dir>/exports/``.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import secrets
from collections.abc import Mapping
from importlib import resources
from itertools import pairwise
from pathlib import Path
from typing import Any

import jsonschema
import numpy as np
from numpy.typing import NDArray

from photoncar.config import Config, HomeZone, data_dir, load_home_zone
from photoncar.privacy.export_filter import WindowSet
from photoncar.privacy.zones import trim_windows
from photoncar.store.reader import DriveReader
from photoncar.store.rollup import GRID_NS, TimelineRow, interp_gap
from photoncar.units import NS_PER_S

log = logging.getLogger(__name__)

SCHEMA_TAG = "photoncar.lap/v0"
SAMPLE_PERIOD_NS = 100_000_000
SAMPLE_PERIOD_MS = SAMPLE_PERIOD_NS // 1_000_000
SAMPLE_KEYS = ("t_ms", "dist_m", "speed_kph", "throttle_pct", "brake", "gear", "rpm", "x", "y")

LineGeometry = tuple[float, float, float, float]
F64 = NDArray[np.float64]


class ExportPathError(ValueError):
    """The requested output directory is outside ``<data_dir>/exports``."""


@functools.cache
def _schema_text() -> str:
    return (
        resources.files("photoncar.export")
        .joinpath("schemas", "lap.v0.schema.json")
        .read_text(encoding="utf-8")
    )


def lap_schema() -> dict[str, Any]:
    """The lap v0 JSON Schema (a fresh copy)."""
    doc: dict[str, Any] = json.loads(_schema_text())
    return doc


def validate_lap(doc: Mapping[str, Any]) -> None:
    """Raise if ``doc`` is not a valid lap v0 document.

    JSON Schema checks the fields and types; this also checks what the schema cannot express:
    every sample array has the same length and ``t_ms`` steps by 100 from 0.
    """
    jsonschema.Draft202012Validator(lap_schema()).validate(doc)
    samples = doc["samples"]
    lengths = {len(samples[k]) for k in SAMPLE_KEYS}
    if len(lengths) != 1:
        raise ValueError("lap v0 sample arrays must all have the same length")
    t_ms = samples["t_ms"]
    if t_ms[0] != 0 or any(b - a != SAMPLE_PERIOD_MS for a, b in pairwise(t_ms)):
        raise ValueError("lap v0 t_ms must start at 0 and step by 100")


def exports_root(base: Path | None = None) -> Path:
    return (base if base is not None else data_dir()) / "exports"


def resolve_export_dir(out_dir: Path | str, base: Path | None = None) -> Path:
    """Resolve ``out_dir`` and refuse anything outside ``<data_dir>/exports``."""
    root = exports_root(base).resolve()
    target = Path(out_dir).resolve()
    if target != root and not target.is_relative_to(root):
        raise ExportPathError(f"exports go only under {root}, not {target}")
    return target


def _round(x: float, digits: int) -> float | None:
    return None if np.isnan(x) else round(float(x), digits)


def _column(rows: list[TimelineRow], attr: str) -> F64:
    vals = (getattr(r, attr) for r in rows)
    return np.array([np.nan if v is None else v for v in vals], dtype=np.float64)


def lap_documents(
    reader: DriveReader,
    zone: HomeZone,
    *,
    lines: Mapping[str, LineGeometry] | None = None,
) -> list[dict[str, Any]]:
    """Build the lap v0 documents for a drive, without writing anything."""
    meta = reader.meta()
    windows = WindowSet(trim_windows(reader.gps_fixes(), zone))
    rows = reader.timeline_rows()
    laps = reader.laps()
    if not rows or not laps:
        return []
    t = np.array([r.t_ns for r in rows], dtype=np.int64)
    speed = _column(rows, "speed_mps")
    rpm = _column(rows, "rpm")
    throttle = _column(rows, "throttle")
    x = _column(rows, "x_m")
    y = _column(rows, "y_m")

    def at(values: F64, times: NDArray[np.int64]) -> F64:
        return interp_gap(t, values, times, GRID_NS)

    origins: dict[str, tuple[float, float] | None] = {}
    for lap in laps:
        if lap.line_id in origins:
            continue
        if lines is not None and lap.line_id in lines:
            x1, y1, x2, y2 = lines[lap.line_id]
            origins[lap.line_id] = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
        else:
            t_cross = np.array([lap.t_start_ns], dtype=np.int64)
            ox, oy = float(at(x, t_cross)[0]), float(at(y, t_cross)[0])
            origins[lap.line_id] = None if np.isnan(ox) or np.isnan(oy) else (ox, oy)

    docs: list[dict[str, Any]] = []
    for lap in laps:
        if windows.overlaps(lap.t_start_ns - GRID_NS, lap.t_end_ns + GRID_NS):
            log.info("Lap %d not exported: it touches the privacy zone.", lap.lap_no)
            continue
        n = (lap.t_end_ns - lap.t_start_ns) // SAMPLE_PERIOD_NS + 1
        grid = lap.t_start_ns + np.arange(n, dtype=np.int64) * SAMPLE_PERIOD_NS
        v = at(speed, grid)
        if bool(np.any(np.isnan(v))):
            log.info("Lap %d not exported: speed is missing inside the lap.", lap.lap_no)
            continue
        dt_s = SAMPLE_PERIOD_NS / NS_PER_S
        dist = np.concatenate(([0.0], np.cumsum((v[1:] + v[:-1]) / 2.0 * dt_s)))
        origin = origins[lap.line_id]
        if origin is None:
            xs = np.full(n, np.nan)
            ys = np.full(n, np.nan)
        else:
            xs = at(x, grid) - origin[0]
            ys = at(y, grid) - origin[1]
        rpm_g = at(rpm, grid)
        thr_g = at(throttle, grid) * 100.0
        docs.append(
            {
                "schema": SCHEMA_TAG,
                "lap_no": lap.lap_no,
                "line_id": lap.line_id,
                "duration_ms": round(lap.duration_ns / 1_000_000),
                "valid": lap.valid,
                "source": meta.source,
                "samples": {
                    "t_ms": [int(k) * SAMPLE_PERIOD_MS for k in range(n)],
                    "dist_m": [round(float(d), 3) for d in dist],
                    "speed_kph": [round(float(s) * 3.6, 3) for s in v],
                    "throttle_pct": [_round(p, 2) for p in thr_g],
                    "brake": [None] * n,
                    "gear": [None] * n,
                    "rpm": [_round(r, 1) for r in rpm_g],
                    "x": [_round(a, 3) for a in xs],
                    "y": [_round(b, 3) for b in ys],
                },
            }
        )
    return docs


def export_laps(
    reader: DriveReader,
    zone: HomeZone,
    out_dir: Path | str,
    *,
    base: Path | None = None,
    lines: Mapping[str, LineGeometry] | None = None,
) -> list[Path]:
    """Write 1 validated lap v0 file per exportable lap under ``<base>/exports/``.

    ``base`` is the data directory (default ``photoncar.config.data_dir()``). File names carry
    a random token and the lap's position, never a drive ID or a date.
    """
    target = resolve_export_dir(out_dir, base)
    texts: list[str] = []
    for doc in lap_documents(reader, zone, lines=lines):
        text = json.dumps(doc, separators=(",", ":"), allow_nan=False)
        validate_lap(json.loads(text))
        texts.append(text)
    if not texts:
        return []
    target.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(4)
    paths: list[Path] = []
    for i, text in enumerate(texts, 1):
        path = target / f"lap-{token}-{i:03d}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        paths.append(path)
    return paths


def export_drive_laps(
    reader: DriveReader,
    cfg: Config,
    *,
    base: Path | None = None,
    privacy_path: Path | None = None,
    lines: Mapping[str, LineGeometry] | None = None,
) -> list[Path]:
    """Export laps to ``<base>/exports/``. Refuses (``ConfigError``) without the privacy file."""
    zone = load_home_zone(cfg, privacy_path)
    return export_laps(reader, zone, exports_root(base), base=base, lines=lines)
