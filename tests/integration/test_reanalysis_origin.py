"""A live drive re-analysed from its file is still a live drive (contracts: owner drives never
ship). The owner drive here is synthetic: GPS near a fake origin in the open ocean."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from photoncar.clock import SimClock
from photoncar.config import Config, load_config, load_vehicle
from photoncar.export.embed import EmbedRefused, export_embed
from photoncar.pipeline.runner import RunReport, run_to_data_dir
from photoncar.records import GpsFix, Record, Sample
from photoncar.sources.base import SourceMeta
from photoncar.sources.recorded import RecordedDriveSource
from photoncar.sources.synthetic import synthetic_drive
from photoncar.store.index import DriveIndex
from photoncar.store.reader import DriveReader
from photoncar.units import NS_PER_S

HOUR = 3600 * NS_PER_S
UTC0 = 1_789_992_000 * NS_PER_S  # 2026-09-21 12:00:00 UTC


class OwnerDrive:
    """A live drive: 60 s north at 11 m/s with GPS and OBD speed at 10 Hz."""

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(name="live:can0+can1", kind="live")

    def records(self) -> Iterator[Record]:
        for i in range(600):
            t = NS_PER_S + i * 100_000_000
            yield GpsFix(
                t_ns=t,
                rx_t_ns=t,
                utc_ns=None,
                lat_deg=0.01 + i * 1e-5,
                lon_deg=0.02,
                alt_m=0.0,
                speed_mps=11.0,
                course_rad=0.0,
                hdop=1.0,
                fix_type=3,
                n_sats=9,
            )
            yield Sample(t_ns=t, signal="obd.speed", value=11.0)


@pytest.fixture
def cfg() -> Config:
    return load_config()


def _run(cfg: Config, data: Path, sources: list[object], label: str, utc: SimClock) -> RunReport:
    return run_to_data_dir(
        sources,  # type: ignore[arg-type]
        cfg,
        load_vehicle(str(cfg.get("vehicle.profile"))),
        data,
        vehicle_profile=None,
        label=label,
        utc=utc,
    )


def test_a_reanalysed_live_drive_stays_live(cfg: Config, tmp_path: Path) -> None:
    utc = SimClock(UTC0)
    owner = _run(cfg, tmp_path, [OwnerDrive()], "Live drive", utc)
    src = RecordedDriveSource(owner.path)
    assert (src.meta.kind, src.origin) == ("replay", "live")  # plays as a replay, came from the car

    utc.advance(2 * HOUR)
    again = _run(cfg, tmp_path, [src], "Re-analysis", utc)
    with DriveReader(again.path) as r:
        meta = r.meta()
        assert meta.source == "live"
        assert meta.dataset_ref is None
        assert len(r.gps_fixes()) == 600
    with DriveIndex(tmp_path / "index.sqlite") as idx:
        item = idx.get(again.drive_id)
        assert item is not None and item.source == "live"
        # Streamed as a replay: it has no UTC times of its own, so the rest since the owner's
        # last real drive still counts from that drive's end.
        assert idx.rested_h(UTC0 + 24 * HOUR) == pytest.approx(24.0)


def test_the_embed_refuses_a_reanalysed_live_drive(cfg: Config, tmp_path: Path) -> None:
    data = tmp_path / "data"
    owner = _run(cfg, data, [OwnerDrive()], "Live drive", SimClock(UTC0))
    again = _run(cfg, data, [RecordedDriveSource(owner.path)], "Re-analysis", SimClock(UTC0))
    privacy = tmp_path / "privacy.local.toml"
    privacy.write_text("[home]\nlat_deg = 45.0\nlon_deg = 45.0\n")  # far from the drive
    dist = tmp_path / "dist-embed"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>stub</title>")
    for drive_id in (owner.drive_id, again.drive_id):
        out = tmp_path / f"embed-{drive_id}"
        with pytest.raises(EmbedRefused, match="recorded live"):
            export_embed(data, drive_id, out, cfg=cfg, web_dist_embed=dist, privacy_path=privacy)
        assert not out.exists()


def test_reanalysis_keeps_every_other_origin(cfg: Config, tmp_path: Path) -> None:
    track = _run(
        cfg, tmp_path, [synthetic_drive(3, "commute", cfg=cfg)], "Synthetic commute", SimClock(0)
    )
    again = _run(cfg, tmp_path, [RecordedDriveSource(track.path)], "Re-analysis", SimClock(0))
    with DriveReader(again.path) as r:
        assert r.meta().source == "synthetic"
