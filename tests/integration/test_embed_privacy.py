"""The portfolio embed keeps 0 positions within R of home with decimation active (hard rule 7).

A synthetic drive of 1,500 s at 10 Hz gives 15,000 timeline rows, so the embed's 1,500 points
come from buckets of 20 rows (2 s). The route dips into the home zone and out again inside 1
bucket, as a short stop at home would. The home is a fake point in the open ocean.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from photoncar.config import Config, load_config, load_vehicle
from photoncar.export.embed import TIMELINE_POINTS, export_embed
from photoncar.pipeline.runner import run_to_data_dir
from photoncar.privacy.geo import enu_m
from photoncar.records import GpsFix, Record
from photoncar.sources.base import SourceMeta
from photoncar.store.reader import DriveReader
from photoncar.units import NS_PER_S

HOME_LAT, HOME_LON = 0.5, 0.5
M_PER_DEG = 111_320.0
RADIUS_M = 50.0
N = 15_000
BUCKET = N // (TIMELINE_POINTS // 2)
FAR = np.array([5000.0, 5000.0])


def _path(dip_row: int) -> list[tuple[float, float]]:
    """East and north of home: in from far away, through home in 10 rows, and out again."""
    a, b, home = np.array([60.0, 60.0]), np.array([45.0, 45.0]), np.zeros(2)
    out: list[tuple[float, float]] = []
    for i in range(N):
        if i < dip_row:
            p = FAR + (a - FAR) * i / dip_row
        elif i <= dip_row + 5:
            p = a + (home - a) * (i - dip_row) / 5
        elif i <= dip_row + 10:
            p = home + (b - home) * (i - dip_row - 5) / 5
        else:
            p = b + (FAR - b) * (i - dip_row - 10) / (N - dip_row - 10)
        out.append((float(p[0]), float(p[1])))
    return out


class Route:
    def __init__(self, dip_row: int) -> None:
        self._xy = _path(dip_row)

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(name="synthetic:route", kind="synthetic")

    def records(self) -> Iterator[Record]:
        cos_lat = math.cos(math.radians(HOME_LAT))
        for i, (x, y) in enumerate(self._xy):
            t = NS_PER_S + i * NS_PER_S // 10
            yield GpsFix(
                t_ns=t,
                rx_t_ns=t,
                utc_ns=None,
                lat_deg=HOME_LAT + y / M_PER_DEG,
                lon_deg=HOME_LON + x / (M_PER_DEG * cos_lat),
                alt_m=0.0,
                speed_mps=10.0,
                course_rad=0.0,
                hdop=1.0,
                fix_type=3,
                n_sats=9,
            )


def _cfg() -> Config:
    base = load_config()
    raw: dict[str, Any] = {k: dict(v) if isinstance(v, Mapping) else v for k, v in base.raw.items()}
    raw["privacy"]["radius_km"] = RADIUS_M / 1000.0
    return Config(raw)


@pytest.mark.parametrize("offset", [3, 7])
def test_no_exported_position_within_r_of_home(tmp_path: Path, offset: int) -> None:
    dip_row = 350 * BUCKET + offset  # the dip starts and ends inside bucket 350
    cfg = _cfg()
    data = tmp_path / "data"
    rep = run_to_data_dir(
        [Route(dip_row)],
        cfg,
        load_vehicle(str(cfg.get("vehicle.profile"))),
        data,
        vehicle_profile=None,
        label="Synthetic route",
    )
    with DriveReader(rep.path) as r:
        first = next(f for f in r.gps_fixes() if f.fix_type >= 2)
        assert len(r.timeline_rows()) == N
    east, north = enu_m(np.array([HOME_LAT]), np.array([HOME_LON]), first.lat_deg, first.lon_deg)
    hx, hy = float(east[0]), float(north[0])
    privacy = tmp_path / "privacy.local.toml"
    privacy.write_text(f"[home]\nlat_deg = {HOME_LAT}\nlon_deg = {HOME_LON}\n")
    dist = tmp_path / "dist-embed"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>stub</title>")
    out = tmp_path / "embed"
    report = export_embed(
        data, rep.drive_id, out, cfg=cfg, web_dist_embed=dist, privacy_path=privacy
    )
    assert report.trim_windows_s  # the dip was trimmed
    tl = json.loads((out / "data" / "timeline.json").read_text())
    assert len(tl["t_s"]) == TIMELINE_POINTS
    near = [
        (t, x, y)
        for t, x, y in zip(tl["t_s"], tl["x_m"], tl["y_m"], strict=True)
        if x is not None and y is not None and math.hypot(x - hx, y - hy) < RADIUS_M
    ]
    assert near == []
