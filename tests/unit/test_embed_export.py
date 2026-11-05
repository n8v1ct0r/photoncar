"""export_embed: the static portfolio embed of 1 drive (docs/contracts.md, "Portfolio embed").

The test drive is synthetic and written in code: 60 s driving east at 20 m/s from a fake origin
near lat 0, lon 0, stored as a public-dataset replay whose dataset reference carries a date, with
1 synthetic coolant alert and 1 performance event. The embed build is a stub folder, so these
tests need no web build.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import math
import re
import sys
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest

from photoncar.api.app import create_app
from photoncar.config import Config, load_config, load_home_zone
from photoncar.export import embed
from photoncar.export.embed import (
    EmbedRefused,
    export_embed,
)
from photoncar.privacy.geo import EARTH_MEAN_RADIUS_M
from photoncar.privacy.zones import trim_windows
from photoncar.records import Citation, DriveStat, Event, GpsFix
from photoncar.store.index import DriveIndex
from photoncar.store.meta import DriveMeta, DriveSource
from photoncar.store.reader import DriveReader
from photoncar.store.rollup import TimelineRow
from photoncar.store.writer import DriveWriter
from photoncar.summary.model_client import FakeSummaryClient
from photoncar.units import G0, c_to_k

if TYPE_CHECKING:
    from tests.asgi import AsgiClient
else:

    def _load_asgi() -> ModuleType:
        path = Path(__file__).resolve().parents[1] / "asgi.py"
        spec = importlib.util.spec_from_file_location("photoncar_tests_asgi_embed", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    AsgiClient = _load_asgi().AsgiClient

S = 1_000_000_000
T0 = 7 * S
DURATION_S = 60.0
SPEED_MPS = 20.0
DRIVE_ID = "3f2a9c41-6b7d-4e5f-8a90-1b2c3d4e5f60"
LAT0 = 0.00731942
LON0 = 0.00418277
DATASET_REF = "Example_1/b0c9d2329ad1606b|2018-08-02--08-34-47/40"
STATS = [
    DriveStat("drive.distance_mi", "Distance", 0.75, "mi", "0.75 mi"),
    DriveStat("perf.zero_to_sixty_s", "0 to 60 mph", 5.47, "s", "5.47 s"),
]


def _ns(t_s: float) -> int:
    return T0 + round(t_s * S)


def _lon(x_m: float) -> float:
    return LON0 + math.degrees(x_m / (EARTH_MEAN_RADIUS_M * math.cos(math.radians(LAT0))))


def _events() -> list[Event]:
    perf = Event(
        kind="perf.zero_to_sixty", rule_id="zero_to_sixty", rule_version=1, severity=0,
        t_start_ns=_ns(1.0), t_end_ns=_ns(6.47), value=5.47, threshold=None, unit="s",
        message="0 to 60 mph in 5.47 s.",
        citations=(
            Citation("state.speed", "Estimator", "trigger", _ns(1.0), _ns(6.47), "time_s", 5.47,
                     "s", 500),
        ),
    )  # fmt: skip
    coolant = Event(
        kind="alert.coolant_high", rule_id="coolant_high", rule_version=1, severity=2,
        t_start_ns=_ns(40.0), t_end_ns=_ns(55.0), value=c_to_k(106.0), threshold=c_to_k(105.0),
        unit="K", message="Coolant 106 °C, over 105 °C. Synthetic fault.",
        citations=(
            Citation("obd.coolant_temp", "OBD 0x05", "trigger", _ns(40.0), _ns(50.0), "min",
                     c_to_k(106.0), "K", 5),
        ),
        resolved=True,
        synthetic=True,
    )  # fmt: skip
    return [perf, coolant]


def _row(k: int) -> TimelineRow:
    t = k / 10
    return TimelineRow(
        t_ns=_ns(t), speed_mps=SPEED_MPS, rpm=2500.0, throttle=0.3,
        coolant_k=c_to_k(100.0 if t < 40 else 106.0), accel_long_mps2=0.0, accel_lat_mps2=0.0,
        x_m=SPEED_MPS * t, y_m=0.0, dr=False,
    )  # fmt: skip


KEEP = object()
"""``write_drive``'s default dataset reference: the public one for a replay, else none."""


def write_drive(
    data_dir: Path,
    *,
    drive_id: str = DRIVE_ID,
    source: DriveSource = "replay",
    dataset_ref: object = KEEP,
    rows: list[TimelineRow] | None = None,
) -> None:
    path = data_dir / "drives" / f"{drive_id}.sqlite"
    ref = DATASET_REF if source == "replay" else None
    meta = DriveMeta(
        drive_id=drive_id,
        source=source,
        dataset_ref=ref
        if dataset_ref is KEEP
        else (None if dataset_ref is None else str(dataset_ref)),
        vehicle_profile="e90_m3",
        synthetic_signals=("obd.coolant_temp",),
    )
    n = int(DURATION_S * 10)
    with DriveWriter(path, meta) as w:
        for k in range(n + 1):
            t = k / 10
            w.write(
                GpsFix(_ns(t), _ns(t), None, LAT0, _lon(SPEED_MPS * t), 10.0, SPEED_MPS,
                       math.pi / 2, 0.8, 3, 12)
            )  # fmt: skip
        for ev in _events():
            w.write(ev)
        w.write_stats(STATS)
        w.write_timeline(rows if rows is not None else [_row(k) for k in range(n + 1)])
    with DriveIndex(data_dir / "index.sqlite") as index, DriveReader(path) as reader:
        index.register(reader.meta(), "Replay: test drive")
        index.update_from_reader(reader)


def privacy_file(tmp: Path, lat: float, lon: float) -> Path:
    p = tmp / "privacy.local.toml"
    p.write_text(f"[home]\nlat_deg = {lat}\nlon_deg = {lon}\n", encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHOTONCAR_API_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def cfg() -> Config:
    return load_config()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    write_drive(d)
    return d


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist-embed"
    (d / "assets").mkdir(parents=True)
    (d / "fonts").mkdir()
    (d / "index.html").write_text(
        "<!doctype html><title>Photoncar</title><script src='./assets/app.js'></script>",
        encoding="utf-8",
    )
    (d / "assets" / "app.js").write_text("console.log(1);", encoding="utf-8")
    (d / "assets" / "app.css").write_text("body{margin:0}", encoding="utf-8")
    (d / "fonts" / "Overpass-Variable.woff2").write_bytes(b"\x00" * 64)
    return d


@pytest.fixture
def far_home(tmp_path: Path) -> Path:
    """A home far from the drive: the trim removes nothing."""
    return privacy_file(tmp_path, -45.0, 170.0)


def api_json(data_dir: Path, path: str, **query: str | float) -> Any:
    app = create_app(load_config(), data_dir, summary_client=FakeSummaryClient())
    r = asyncio.run(AsgiClient(app).get(path, query=query))
    assert r.status == 200, (path, r.status, r.text)
    return r.json()


def read(out: Path, name: str) -> Any:
    return json.loads((out / "data" / name).read_text(encoding="utf-8"))


# -- what it writes -----------------------------------------------------------------------------


def test_writes_the_api_bodies_and_the_build(
    data_dir: Path, dist: Path, far_home: Path, tmp_path: Path, cfg: Config
) -> None:
    out = tmp_path / "embed"
    rep = export_embed(data_dir, DRIVE_ID, out, cfg=cfg, web_dist_embed=dist, privacy_path=far_home)
    base = f"/api/drives/{DRIVE_ID}"
    assert read(out, "timeline.json") == api_json(data_dir, f"{base}/timeline", max_points=1500)
    assert read(out, "events.json") == api_json(data_dir, f"{base}/events")
    assert read(out, "report.json") == api_json(data_dir, f"{base}/report")
    assert read(out, "legend.json") == api_json(data_dir, "/api/legend")
    assert read(out, "drives.json") == api_json(data_dir, "/api/drives")
    detail = api_json(data_dir, base)
    assert detail["drive"]["dataset_ref"] == DATASET_REF
    detail["drive"]["dataset_ref"] = "comma2k19"  # the 1 field the embed rewrites: it has a date
    assert read(out, "drive.json") == detail
    assert read(out, "trim.json") == {"windows_s": []}

    frames = read(out, "live_frames.json")
    assert len(frames) == 601 == rep.n_frames  # 60 s at 10 Hz, both ends
    assert rep.window_s == (0.0, DURATION_S)
    for f in (frames[0], frames[520], frames[-1]):
        assert f == api_json(data_dir, f"{base}/live_frame", t_s=f["t_s"])
    assert frames[520]["alert"]["synthetic"] is True  # known at 50 s, the end of its trigger

    assert (out / "index.html").read_text(encoding="utf-8").startswith("<!doctype html>")
    assert (out / "assets" / "app.js").is_file()
    assert (out / "fonts" / "Overpass-Variable.woff2").is_file()
    assert rep.code_bytes == len("console.log(1);") + len("body{margin:0}")
    assert rep.font_bytes == 64
    assert rep.json_bytes == sum(p.stat().st_size for p in (out / "data").iterdir())
    assert rep.has_legend


def test_long_drive_window_opens_before_the_worst_alert(
    data_dir: Path,
    dist: Path,
    far_home: Path,
    tmp_path: Path,
    cfg: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embed, "MAX_WINDOW_S", 20.0)
    rep = export_embed(
        data_dir, DRIVE_ID, tmp_path / "embed", cfg=cfg, web_dist_embed=dist, privacy_path=far_home
    )
    # The coolant alert becomes known at the end of its trigger window, 50 s in; the window
    # opens 20 s before that, capped so it still fits in the drive.
    assert rep.window_s == (30.0, 50.0)
    frames = read(tmp_path / "embed", "live_frames.json")
    assert frames[0]["t_s"] == pytest.approx(30.0) and frames[-1]["t_s"] == pytest.approx(50.0)


# -- no location, no dates, no other IDs ---------------------------------------------------------


def test_output_holds_no_latitude_longitude_or_dates(
    data_dir: Path, dist: Path, far_home: Path, tmp_path: Path, cfg: Config
) -> None:
    out = tmp_path / "embed"
    export_embed(data_dir, DRIVE_ID, out, cfg=cfg, web_dist_embed=dist, privacy_path=far_home)
    texts = [p.read_text(encoding="utf-8") for p in sorted(out.rglob("*.json"))]
    assert len(texts) == 8
    whole = "\n".join(texts)
    assert not re.search(r'"(lat|lon|lng|latitude|longitude)(_deg)?"', whole)
    assert f"{LAT0:.6f}"[:8] not in whole  # "0.007319"
    assert f"{LON0:.6f}"[:8] not in whole  # "0.004182"
    assert not re.search(r"(19|20)\d\d-\d\d-\d\d", whole)
    assert "2018" not in whole and "b0c9d2329ad1606b" not in whole
    ids = set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", whole))
    assert ids == {DRIVE_ID}


def test_other_drives_stay_out(
    data_dir: Path, dist: Path, far_home: Path, tmp_path: Path, cfg: Config
) -> None:
    other = "9e8d7c6b-5a49-4382-a1b0-c9d8e7f6a5b4"
    write_drive(data_dir, drive_id=other, source="synthetic")
    out = tmp_path / "embed"
    export_embed(data_dir, DRIVE_ID, out, cfg=cfg, web_dist_embed=dist, privacy_path=far_home)
    assert [d["drive_id"] for d in read(out, "drives.json")["drives"]] == [DRIVE_ID]
    assert other not in "".join(p.read_text(encoding="utf-8") for p in out.rglob("*.json"))


def test_privacy_trim_runs_on_positions_events_and_frames(
    data_dir: Path, dist: Path, tmp_path: Path, cfg: Config
) -> None:
    """Home at the start point: the first 500 m (25 s at 20 m/s) sits inside the zone."""
    home = privacy_file(tmp_path, LAT0, LON0)
    out = tmp_path / "embed"
    rep = export_embed(data_dir, DRIVE_ID, out, cfg=cfg, web_dist_embed=dist, privacy_path=home)
    windows = read(out, "trim.json")["windows_s"]
    assert len(windows) == 1 and windows[0][0] == 0.0
    trim_end = windows[0][1]
    assert 25.0 <= trim_end < 26.0
    assert rep.trim_windows_s == [(0.0, trim_end)]

    tl = read(out, "timeline.json")
    for i, t in enumerate(tl["t_s"]):
        inside = t <= trim_end
        for key in ("x_m", "y_m", "speed_mps", "rpm", "coolant_c"):
            assert (tl[key][i] is None) == inside, (key, t)
    kept = [x for x in tl["x_m"] if x is not None]
    assert min(kept) > 500.0  # 0 kept points within R of home

    boxes = [e["box_no"] for e in read(out, "events.json")["events"]]
    assert boxes == [2]  # the 0 to 60 run inside the zone is dropped; box numbers stay
    frames = read(out, "live_frames.json")
    assert frames and all(f["t_s"] > trim_end for f in frames)


def test_an_in_zone_extreme_never_lands_on_a_point_outside_the_zone(
    dist: Path, tmp_path: Path, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trim empties rows before the timeline is decimated. A min/max bucket that straddles
    the end of a trim window would otherwise carry an in-zone extreme to its last point, whose
    time is outside the window, where a trim after decimation cannot see it."""
    home = privacy_file(tmp_path, LAT0, LON0)
    probe = tmp_path / "probe"
    write_drive(probe)
    with DriveReader(probe / "drives" / f"{DRIVE_ID}.sqlite") as reader:
        # The first window covers the start; the other one opens after the last fix.
        lo_ns, hi_ns = trim_windows(reader.gps_fixes(), load_home_zone(cfg, home))[0]
    rows = [_row(k) for k in range(int(DURATION_S * 10) + 1)]
    last_in = max(i for i, r in enumerate(rows) if lo_ns <= r.t_ns <= hi_ns)
    spike = dataclasses.replace(
        rows[last_in], speed_mps=77.7, rpm=7777.0, throttle=0.777, coolant_k=c_to_k(177.0),
        accel_long_mps2=7.77, accel_lat_mps2=-7.77, dr=True,
    )  # fmt: skip
    rows[last_in] = spike
    # A decimation whose bucket holding the spike runs past the window end.
    n = len(rows)
    points = next(
        m for m in range(20, 200, 2)
        if any(a <= last_in < b - 1 for a, b in pairwise((np.arange(m // 2 + 1) * n) // (m // 2)))
    )  # fmt: skip
    monkeypatch.setattr(embed, "TIMELINE_POINTS", points)
    data = tmp_path / "data"
    write_drive(data, rows=rows)
    out = tmp_path / "embed"
    export_embed(data, DRIVE_ID, out, cfg=cfg, web_dist_embed=dist, privacy_path=home)
    tl = read(out, "timeline.json")
    assert len(tl["t_s"]) == points
    for key, value in (("speed_mps", 77.7), ("rpm", 7777.0), ("throttle", 0.777),
                       ("coolant_c", 177.0), ("accel_long_g", 7.77 / G0),
                       ("accel_lat_g", -7.77 / G0)):  # fmt: skip
        assert not any(v is not None and abs(v - value) < 1e-6 for v in tl[key]), key
    assert not any(tl["dr"])


# -- refusals ----------------------------------------------------------------------------------


def test_refuses_a_live_drive(dist: Path, far_home: Path, tmp_path: Path, cfg: Config) -> None:
    d = tmp_path / "data"
    write_drive(d, source="live")
    with pytest.raises(EmbedRefused, match="recorded live"):
        export_embed(d, DRIVE_ID, tmp_path / "out", cfg=cfg, web_dist_embed=dist,
                     privacy_path=far_home)  # fmt: skip
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "ref",
    [
        None,
        "my-own-log/1",
        "Example_1/0123456789abcdef|2024-01-01--00-00-00/1",  # not a dataset car
        "Chunk_2/b0c9d2329ad1606b|2018-11-11--13-08-44",  # no segment
    ],
)
def test_refuses_a_replay_that_is_not_public_data(
    ref: str | None, dist: Path, far_home: Path, tmp_path: Path, cfg: Config
) -> None:
    d = tmp_path / "data"
    write_drive(d, source="replay", dataset_ref=ref)
    with pytest.raises(EmbedRefused) as exc:
        export_embed(d, DRIVE_ID, tmp_path / "out", cfg=cfg, web_dist_embed=dist,
                     privacy_path=far_home)  # fmt: skip
    assert str(exc.value) == embed.NOT_PUBLIC
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("source", "ref"),
    [
        ("synthetic", None),
        ("synthetic", "synthetic:track_day:0"),
        ("replay", "Example_1/b0c9d2329ad1606b|2018-08-02--08-34-47/40"),
        ("replay", "Chunk_2/b0c9d2329ad1606b|2018-11-11--13-08-44/9..23"),
        ("replay", "Chunk_6/99c94dc769b5d96e|2018-07-12--16-42-23/9"),
        ("replay", "b0c9d2329ad1606b|2018-08-02--08-34-47/40"),  # read from outside the root
    ],
)
def test_ships_synthetic_drives_and_public_replays(
    source: DriveSource, ref: str | None, dist: Path, far_home: Path, tmp_path: Path, cfg: Config
) -> None:
    d = tmp_path / "data"
    write_drive(d, source=source, dataset_ref=ref)
    rep = export_embed(d, DRIVE_ID, tmp_path / "out", cfg=cfg, web_dist_embed=dist,
                       privacy_path=far_home)  # fmt: skip
    assert rep.drive_id == DRIVE_ID


def test_refuses_without_the_privacy_file(
    data_dir: Path, dist: Path, tmp_path: Path, cfg: Config
) -> None:
    with pytest.raises(EmbedRefused, match="privacy file"):
        export_embed(data_dir, DRIVE_ID, tmp_path / "out", cfg=cfg, web_dist_embed=dist,
                     privacy_path=tmp_path / "missing.toml")  # fmt: skip
    assert not (tmp_path / "out").exists()


def test_refuses_json_over_budget(
    data_dir: Path,
    dist: Path,
    far_home: Path,
    tmp_path: Path,
    cfg: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embed, "JSON_BUDGET", 10_000)
    with pytest.raises(EmbedRefused, match=r"over the 0\.01 MB budget"):
        export_embed(data_dir, DRIVE_ID, tmp_path / "out", cfg=cfg, web_dist_embed=dist,
                     privacy_path=far_home)  # fmt: skip
    assert not (tmp_path / "out").exists()


def test_refuses_code_over_budget(
    data_dir: Path, dist: Path, far_home: Path, tmp_path: Path, cfg: Config
) -> None:
    (dist / "assets" / "big.js").write_text("x" * (embed.CODE_BUDGET + 1), encoding="utf-8")
    with pytest.raises(EmbedRefused, match=r"over the 250\.0 KB budget"):
        export_embed(data_dir, DRIVE_ID, tmp_path / "out", cfg=cfg, web_dist_embed=dist,
                     privacy_path=far_home)  # fmt: skip
    assert not (tmp_path / "out").exists()


def test_refuses_unknown_drive_missing_build_and_busy_folder(
    data_dir: Path, dist: Path, far_home: Path, tmp_path: Path, cfg: Config
) -> None:
    with pytest.raises(EmbedRefused, match="No drive with that ID"):
        export_embed(data_dir, "0" * 8 + "-nope", tmp_path / "a", cfg=cfg, web_dist_embed=dist,
                     privacy_path=far_home)  # fmt: skip
    with pytest.raises(EmbedRefused, match="No drive with that ID"):
        export_embed(data_dir, "../index", tmp_path / "a", cfg=cfg, web_dist_embed=dist,
                     privacy_path=far_home)  # fmt: skip
    with pytest.raises(EmbedRefused, match="build:embed"):
        export_embed(data_dir, DRIVE_ID, tmp_path / "a", cfg=cfg,
                     web_dist_embed=tmp_path / "nothing", privacy_path=far_home)  # fmt: skip
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.txt").write_text("owner file", encoding="utf-8")
    with pytest.raises(EmbedRefused, match="not empty"):
        export_embed(data_dir, DRIVE_ID, busy, cfg=cfg, web_dist_embed=dist,
                     privacy_path=far_home)  # fmt: skip
    assert [p.name for p in busy.iterdir()] == ["keep.txt"]


def test_scan_refuses_dates_coordinates_and_foreign_ids() -> None:
    with pytest.raises(EmbedRefused, match="date"):
        embed._scan("x.json", b'{"label": "Drive on 2026-09-21"}', DRIVE_ID)
    with pytest.raises(EmbedRefused, match="latitude or longitude"):
        embed._scan("x.json", b'{"lat_deg": 1.0}', DRIVE_ID)
    with pytest.raises(EmbedRefused, match="ID other"):
        embed._scan("x.json", b'{"id": "9e8d7c6b-5a49-4382-a1b0-c9d8e7f6a5b4"}', DRIVE_ID)
    embed._scan("x.json", f'{{"drive_id": "{DRIVE_ID}"}}'.encode(), DRIVE_ID)
