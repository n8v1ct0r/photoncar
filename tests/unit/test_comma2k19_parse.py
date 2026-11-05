"""Comma2k19Source on small arrays written in the dataset's on-disk format (extension-less .npy)."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest
from numpy.typing import NDArray

from photoncar.records import CanFrame, GpsFix, ImuSample, record_time
from photoncar.sources.comma2k19 import Comma2k19Source, seconds_to_ns, utc_to_ns

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _save(root: Path, name: str, arr: NDArray[np.generic]) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:  # np.save(path) would append ".npy"
        np.save(f, arr)


def _segment(
    tmp_path: Path, *, gyro_t: NDArray[np.float64] | None = None, can_speed: bool = True
) -> Path:
    seg = tmp_path / "Chunk_9" / "0123456789abcdef|2000-01-01--00-00-00" / "3"
    # Raw CAN: bus 0, a TX echo, bus 1, another echo, bus 2 with a 29-bit id. Out of time order.
    _save(seg, "processed_log/CAN/raw_can/t", np.array([10.002, 10.0, 10.0005, 10.001, 10.0015]))
    _save(seg, "processed_log/CAN/raw_can/src", np.array([2, 0, 128, 1, 129], dtype=np.int64))
    _save(
        seg,
        "processed_log/CAN/raw_can/address",
        np.array([0x18DAF110, 0x1C4, 0x1C4, 0x24, 0x7E8], dtype=np.int64),
    )
    payloads = [
        b"\x11\x22\x33\x44\x55\x66\x77\x88",
        b"\x01\x02\x00\x00\x00\x00\x00\x00",  # stored as b"\x01\x02": trailing nulls dropped
        b"\xaa",
        b"",
        b"\xbb",
    ]
    _save(seg, "processed_log/CAN/raw_can/data", np.array(payloads, dtype="|S8"))
    # u-blox: lat, lon, speed, utc (ms), alt, bearing (deg). Row 2 is NaN, row 3 is (0, 0).
    ublox = np.array(
        [
            [0.001, 0.002, 12.5, 1_000_000_000_000.0, 7.0, 90.0],
            [0.0011, 0.0021, 12.75, 1_000_000_000_100.0, 7.5, 180.0],
            [math.nan, math.nan, 0.0, 1_000_000_000_200.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1_000_000_000_300.0, 0.0, 0.0],
        ]
    )
    _save(seg, "processed_log/GNSS/live_gnss_ublox/t", np.array([10.0001, 10.1001, 10.2, 10.3]))
    _save(seg, "processed_log/GNSS/live_gnss_ublox/value", ublox)
    # IMU in [forward, right, down]; accel at 100 Hz.
    at = np.array([10.0, 10.01, 10.02])
    _save(seg, "processed_log/IMU/accelerometer/t", at)
    _save(
        seg,
        "processed_log/IMU/accelerometer/value",
        np.array([[1.0, 2.0, -9.8], [1.5, -0.5, -9.7], [0.0, 0.0, -9.81]]),
    )
    gt = at if gyro_t is None else gyro_t
    gv = np.array([[0.1, 0.2, 0.3], [0.2, 0.4, 0.6], [0.3, 0.6, 0.9]])[: len(gt)]
    _save(seg, "processed_log/IMU/gyro/t", gt)
    _save(seg, "processed_log/IMU/gyro/value", gv)
    if can_speed:
        _save(seg, "processed_log/CAN/speed/t", np.array([10.05, 10.0]))
        _save(seg, "processed_log/CAN/speed/value", np.array([[12.0], [11.0]]))
    return seg


def test_can_frames_padded_bus_from_src_echoes_dropped(tmp_path: Path) -> None:
    src = Comma2k19Source(_segment(tmp_path), dataset_root=tmp_path)
    frames = list(src.can_frames())
    assert frames == [
        CanFrame(10_000_000_000, 0, 0x1C4, False, 8, b"\x01\x02\x00\x00\x00\x00\x00\x00"),
        CanFrame(10_001_000_000, 1, 0x24, False, 8, b"\x00" * 8),
        CanFrame(10_002_000_000, 2, 0x18DAF110, True, 8, b"\x11\x22\x33\x44\x55\x66\x77\x88"),
    ]
    counts = src.counts()
    assert counts.can_by_bus == {0: 1, 1: 1, 2: 1}
    assert counts.dropped_can_by_src == {128: 1, 129: 1}


def test_gps_fixes_units_and_dropped_rows(tmp_path: Path) -> None:
    src = Comma2k19Source(_segment(tmp_path), dataset_root=tmp_path)
    fixes = list(src.gps_fixes())
    assert len(fixes) == 2
    first = fixes[0]
    assert first.t_ns == 10_000_100_000 == first.rx_t_ns
    assert first.utc_ns == 1_000_000_000_000_000_000  # ms column to ns
    assert (first.lat_deg, first.lon_deg, first.alt_m, first.speed_mps) == (0.001, 0.002, 7.0, 12.5)
    assert first.course_rad == pytest.approx(math.pi / 2, abs=1e-12)
    assert math.isnan(first.hdop)
    assert (first.n_sats, first.fix_type) == (0, 3)
    assert fixes[1].course_rad == pytest.approx(math.pi, abs=1e-12)
    counts = src.counts()
    assert (counts.gps_fixes, counts.gps_dropped) == (2, 2)


def test_imu_axes_rotated_to_x_forward_y_left_z_up(tmp_path: Path) -> None:
    src = Comma2k19Source(_segment(tmp_path), dataset_root=tmp_path)
    imu = list(src.imu_samples())
    assert imu[0] == ImuSample(10_000_000_000, 1.0, -2.0, 9.8, 0.1, -0.2, -0.3)
    assert imu[1] == ImuSample(10_010_000_000, 1.5, 0.5, 9.7, 0.2, -0.4, -0.6)
    assert imu[2].az == pytest.approx(9.81)


def test_gyro_with_own_timestamps_is_interpolated(tmp_path: Path) -> None:
    seg = _segment(tmp_path, gyro_t=np.array([10.0, 10.02]))
    imu = list(Comma2k19Source(seg, dataset_root=tmp_path).imu_samples())
    # Gyro rows [0.1, 0.2, 0.3] at 10.00 s and [0.2, 0.4, 0.6] at 10.02 s: midpoint at 10.01 s.
    assert imu[1].gx == pytest.approx(0.15, abs=1e-12)
    assert imu[1].gy == pytest.approx(-0.3, abs=1e-12)
    assert imu[1].gz == pytest.approx(-0.45, abs=1e-12)


def test_records_merged_in_time_order(tmp_path: Path) -> None:
    src = Comma2k19Source(_segment(tmp_path), dataset_root=tmp_path)
    recs = list(src.records())
    times = [record_time(r) for r in recs]
    assert times == sorted(times)
    assert len(recs) == 3 + 2 + 3
    assert [type(r) for r in recs[:3]] == [CanFrame, ImuSample, GpsFix]


def test_meta_and_dataset_ref(tmp_path: Path) -> None:
    seg = _segment(tmp_path)
    ref = "Chunk_9/0123456789abcdef|2000-01-01--00-00-00/3"
    src = Comma2k19Source(seg, dataset_root=tmp_path)
    assert (src.meta.kind, src.meta.dataset_ref, src.meta.synthetic_signals) == ("replay", ref, ())
    outside = Comma2k19Source(seg, dataset_root=tmp_path / "elsewhere")
    assert outside.meta.dataset_ref == "0123456789abcdef|2000-01-01--00-00-00/3"


def test_speed_trace_prefers_can_speed_sorted(tmp_path: Path) -> None:
    src = Comma2k19Source(_segment(tmp_path), dataset_root=tmp_path)
    t, v = src.speed_trace()
    assert t.tolist() == [10_000_000_000, 10_050_000_000]
    assert v.tolist() == [11.0, 12.0]
    no_can = Comma2k19Source(_segment(tmp_path / "b", can_speed=False), dataset_root=tmp_path)
    t2, v2 = no_can.speed_trace()
    assert t2.tolist() == [10_000_100_000, 10_100_100_000]
    assert v2.tolist() == [12.5, 12.75]


def test_missing_files_fail_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="make fetch-dataset"):
        Comma2k19Source(tmp_path / "nope")
    seg = _segment(tmp_path)
    (seg / "processed_log/IMU/gyro/value").unlink()
    with pytest.raises(FileNotFoundError, match="gyro/value"):
        Comma2k19Source(seg)


def test_time_and_utc_conversions() -> None:
    assert seconds_to_ns(np.array([46408.58493039533, 0.5e-9, 1.5e-9])).tolist() == [
        46408584930395,
        0,
        2,
    ]
    assert utc_to_ns(1533226488299.0) == 1_533_226_488_299_000_000
    assert utc_to_ns(1533226488.299) == 1_533_226_488_299_000_000
    assert utc_to_ns(math.nan) is None


def _load_script(name: str) -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(SCRIPTS))


def test_fetch_helpers_urls_lfs_and_blob_ids() -> None:
    fetch = _load_script("fetch_comma2k19_sample")
    assert fetch.raw_url("Example_1/a|b/40/x").endswith("/Example_1/a%7Cb/40/x")
    assert fetch.media_url("a|b").startswith("https://media.githubusercontent.com/media/")
    oid = "ab" * 32
    pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize 123\n"
    parsed = fetch.parse_lfs_pointer(pointer.encode())
    assert (parsed.sha256, parsed.size) == (oid, 123)
    assert fetch.parse_lfs_pointer(b"\x93NUMPY") is None
    # git's id for an empty blob and for "hello\n" (git hash-object).
    assert fetch.git_blob_sha1(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert fetch.git_blob_sha1(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_route_fetch_zip_directory_parsing() -> None:
    route = _load_script("fetch_comma2k19_route")
    names = [
        "Chunk_2/0123456789abcdef|2000-01-01--00-00-00/9/processed_log/CAN/speed/t",
        "Chunk_2/0123456789abcdef|2000-01-01--00-00-00/9/video.hevc",
        "Chunk_2/0123456789abcdef|2000-01-01--00-00-00/10/global_pose/frame_times",
        "Chunk_2/0123456789abcdef|2000-01-01--00-00-00/10/processed_log/",
        "Chunk_2/fedcba9876543210|2000-01-01--00-00-00/1/processed_log/CAN/speed/t",
    ]
    cd = b""
    for i, name in enumerate(names):
        raw = name.encode()
        cd += b"PK\x01\x02" + bytes(12)
        cd += (0xDEADBEEF).to_bytes(4, "little")  # crc32
        cd += (100 + i).to_bytes(4, "little") + (200 + i).to_bytes(4, "little")
        cd += len(raw).to_bytes(2, "little") + bytes(12) + (1000 * i).to_bytes(4, "little")
        cd += raw
    entries = route.parse_central_directory(cd)
    assert [e.name for e in entries] == names
    assert (entries[1].crc32, entries[1].csize, entries[1].usize, entries[1].offset) == (
        0xDEADBEEF,
        101,
        201,
        1000,
    )
    members = route.route_members(entries, "0123456789abcdef|2000-01-01--00-00-00", None)
    assert {k: [e.name.rsplit("/", 1)[1] for e in v] for k, v in members.items()} == {
        9: ["t"],
        10: ["frame_times"],
    }
    assert route.parse_segments("9-11,14") == {9, 10, 11, 14}


def test_fetch_scripts_write_under_the_public_dataset_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fetch scripts put the dataset where the sources read it: PHOTONCAR_PUBLIC_DIR, not
    # a folder under PHOTONCAR_DATA_DIR (plan H14).
    monkeypatch.setenv("PHOTONCAR_DATA_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("PHOTONCAR_PUBLIC_DIR", str(tmp_path / "public"))
    fetch = _load_script("fetch_comma2k19_sample")
    route = _load_script("fetch_comma2k19_route")
    want = (tmp_path / "public" / "comma2k19").resolve()
    assert fetch.default_dest() == want
    assert route.default_dest() == want
    monkeypatch.delenv("PHOTONCAR_PUBLIC_DIR")
    assert fetch.default_dest() == Path(__file__).resolve().parents[2] / "data/public/comma2k19"


def test_nothing_builds_the_dataset_root_from_the_data_dir() -> None:
    # One helper names the public dataset root (photoncar.sources.comma2k19.public_dir).
    repo = SCRIPTS.parent
    pattern = 'data_dir() / "public"'
    offenders = [
        p.relative_to(repo).as_posix()
        for top in ("src", "scripts", "tests")
        for p in sorted((repo / top).rglob("*.py"))
        if pattern in p.read_text(encoding="utf-8") and p != Path(__file__).resolve()
    ]
    assert offenders == []


RAV4_SAMPLE = "Example_1/b0c9d2329ad1606b|2018-08-02--08-34-47/40"
RAV4_ROUTE = "Chunk_2/b0c9d2329ad1606b|2018-11-11--13-08-44"
CIVIC_SEGMENT = "Chunk_6/99c94dc769b5d96e|2018-07-12--16-42-23/9"


def test_the_dataset_cars_have_their_own_profiles() -> None:
    from photoncar.config import load_vehicle
    from photoncar.sources.comma2k19 import dataset_profile, is_public_dataset_ref

    assert dataset_profile(RAV4_SAMPLE) == "comma2k19_rav4"
    assert dataset_profile(f"{RAV4_ROUTE}/9..23") == "comma2k19_rav4"
    assert dataset_profile("b0c9d2329ad1606b|2018-08-02--08-34-47/40") == "comma2k19_rav4"
    assert dataset_profile(CIVIC_SEGMENT) == "comma2k19_civic"
    assert dataset_profile("Chunk_6/99c94dc769b5d96e_2018-07-12--16-42-23/9") == (
        "comma2k19_civic"  # unzip_msft_fs.py writes "_" where the route id has "|"
    )
    for other in (None, "0123456789abcdef|2024-01-01--00-00-00/1", "my-log/1", RAV4_ROUTE):
        assert dataset_profile(other) is None
        assert not is_public_dataset_ref(other)
    assert is_public_dataset_ref(RAV4_SAMPLE) and is_public_dataset_ref(CIVIC_SEGMENT)
    for name in ("comma2k19_rav4", "comma2k19_civic"):
        assert load_vehicle(name).get("vehicle.max_gears") == 10


def test_replays_run_under_the_dataset_car_profile(tmp_path: Path) -> None:
    from photoncar import cli
    from photoncar.config import load_config, load_vehicle

    cfg = load_config()
    ctx = cli.Context(cfg, load_vehicle("e90_m3"), "e90_m3", tmp_path, tmp_path / "public")
    rav4 = [tmp_path / "public" / RAV4_ROUTE / str(n) for n in (9, 10)]
    profile, vehicle = cli.dataset_vehicle(ctx, rav4)
    assert profile == "comma2k19_rav4"
    assert vehicle.get("vehicle.name") == "Toyota RAV4 Platinum (2017)"
    assert cli.dataset_vehicle(ctx, [tmp_path / CIVIC_SEGMENT])[0] == "comma2k19_civic"
    # A comma segment from any other car runs under the configured profile.
    own = tmp_path / "0123456789abcdef|2024-01-01--00-00-00" / "3"
    assert cli.dataset_vehicle(ctx, [own]) == ("e90_m3", ctx.vehicle)
    for mixed in ([rav4[0], tmp_path / CIVIC_SEGMENT], [rav4[0], own]):
        with pytest.raises(cli.Refused, match="more than 1 car"):
            cli.dataset_vehicle(ctx, mixed)
