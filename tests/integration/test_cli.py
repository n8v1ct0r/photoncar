"""The ``photoncar`` command: replay, serve, seed-demo refusals, export, export-embed, record.

No test here needs the public dataset. ``record`` is driven through a stand-in for
``photoncar.sources.obd.open_live_can_session`` (patched with ``raising=False``, so these tests
also run while that function is being added) and for ``open_live_sensors``. ``export-embed``
is driven through a stand-in for ``photoncar.export.embed.export_embed``, which the web build
writes (a stand-in module is used while that file does not exist yet).
"""

from __future__ import annotations

import importlib
import re
import shutil
import sqlite3
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

import photoncar.config
from photoncar import SAFETY_LINE, cli
from photoncar.config import CONFIG_DIR, REPO_ROOT, Config, load_config, load_vehicle
from photoncar.records import CanFrame, GpsFix, ImuSample, ObdPoll, Record, Sample
from photoncar.sources import live_sensors
from photoncar.sources.base import SourceMeta
from photoncar.sources.comma2k19 import default_dataset_root
from photoncar.sources.synthetic import synthetic_drive
from photoncar.store.index import DriveIndex
from photoncar.store.meta import DriveMeta
from photoncar.store.reader import DriveReader
from photoncar.store.writer import DriveWriter

BANNED_DASHES = ("—", "–")


def index_items(data_dir: Path) -> list[tuple[str, str, bool]]:
    with DriveIndex(data_dir / "index.sqlite") as index:
        return [(i.label, i.source, i.closed_cleanly) for i in index.list()]


def only_drive(data_dir: Path) -> Path:
    (path,) = sorted((data_dir / "drives").glob("*.sqlite"))
    return path


def test_replay_synthetic_track_day(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(
        ["replay", "--synthetic", "track_day", "--seed", "3", "--data-dir", str(tmp_path)]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert index_items(tmp_path) == [("Synthetic track day", "synthetic", True)]
    assert out.startswith("Synthetic track day: drive ")
    assert "Errors: 0." in out
    assert re.search(r"^  Time by stage: prepass \d+\.\d{3} s, write \d+\.\d{3} s, ", out, re.M)
    assert "perf.zero_to_sixty_s: 5.47 s" in out
    assert not any(d in out for d in BANNED_DASHES)


def test_replay_rejects_mixed_sources(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["replay", "--synthetic", "commute", "--route", "--data-dir", str(tmp_path)])
    assert exc.value.code == 2


def test_replay_missing_segment_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "no-such-segment"
    code = cli.main(
        ["replay", "--dataset", "comma2k19", "--segment", str(missing), "--data-dir", str(tmp_path)]
    )
    assert code == cli.REFUSED
    assert capsys.readouterr().err.strip() == f"No comma2k19 segment at {missing}."


def test_serve_builds_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[Any, str, int]] = []

    def fake_run(app: Any, *, host: str, port: int, log_level: str) -> None:
        calls.append((app, host, port))

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.delenv("PHOTONCAR_API_TOKEN", raising=False)
    assert cli.main(["serve", "--data-dir", str(tmp_path)]) == 0
    ((app, host, port),) = calls
    assert (host, port) == ("127.0.0.1", 8000)
    assert app.title == "Photoncar"
    # Nothing records in this process: idle mode, where nothing moves.
    assert (app.state.live.mode, app.state.live.moving) == ("idle", False)
    out = capsys.readouterr().out
    assert SAFETY_LINE in out
    assert "No PHOTONCAR_API_TOKEN set. The API answers only this device." in out


def test_serve_live_starts_locked_and_stays_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On the car, next to a recorder in another process: no speed source here, so the parked
    views stay locked (plan H16, the contract's moving-lock scope)."""
    apps: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: apps.append(app))
    monkeypatch.delenv("PHOTONCAR_API_TOKEN", raising=False)
    assert cli.main(["serve", "--live", "--data-dir", str(tmp_path)]) == 0
    (app,) = apps
    assert (app.state.live.mode, app.state.live.moving) == ("live", True)
    out = capsys.readouterr().out
    assert out.startswith(f"{SAFETY_LINE}\nLive mode with no speed source here: parked views ")


# -- seed-demo refusals --------------------------------------------------------------------------


def write_live_drive(data_dir: Path) -> Path:
    meta = DriveMeta(drive_id="0f8b1c2d-3e4f-4a5b-8c6d-7e8f9a0b1c2d", source="live")
    path = data_dir / "drives" / f"{meta.drive_id}.sqlite"
    with DriveWriter(path, meta) as w:
        w.write_stats([])
    with DriveIndex(data_dir / "index.sqlite") as index, DriveReader(path) as r:
        index.register(r.meta(), "Live drive")
    return path


def test_seed_demo_refuses_a_folder_with_live_drives(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_live_drive(tmp_path)
    assert cli.main(["seed-demo", "--data-dir", str(tmp_path)]) == cli.REFUSED
    assert capsys.readouterr().err.strip() == cli.LIVE_HELD
    assert path.is_file()
    assert index_items(tmp_path) == [("Live drive", "live", True)]


def test_seed_demo_refuses_a_live_drive_missing_from_the_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_live_drive(tmp_path)
    (tmp_path / "index.sqlite").unlink()
    assert cli.main(["seed-demo", "--data-dir", str(tmp_path)]) == cli.REFUSED
    assert capsys.readouterr().err.strip() == cli.LIVE_HELD
    assert path.is_file()


def test_seed_demo_refuses_unknown_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stray = tmp_path / "drives" / "notes.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("keep me", encoding="utf-8")
    assert cli.main(["seed-demo", "--data-dir", str(tmp_path)]) == cli.REFUSED
    assert "was not written by Photoncar" in capsys.readouterr().err
    assert stray.read_text(encoding="utf-8") == "keep me"


def test_seed_demo_clears_only_earlier_demo_drives(tmp_path: Path) -> None:
    cli.main(["replay", "--synthetic", "commute", "--data-dir", str(tmp_path)])
    old = only_drive(tmp_path)
    files = cli.check_demo_dir(tmp_path)
    assert files == [old]
    con = sqlite3.connect(tmp_path / "index.sqlite")
    with con:
        con.execute("INSERT INTO spend_ledger (day_utc, usd_micros) VALUES ('2026-09-23', 1200)")
    con.close()
    cli.clear_demo_dir(tmp_path, files)
    assert not old.exists()
    assert index_items(tmp_path) == []
    con = sqlite3.connect(tmp_path / "index.sqlite")
    kept = con.execute("SELECT day_utc, usd_micros FROM spend_ledger").fetchall()
    con.close()
    assert kept == [("2026-09-23", 1200)]


# -- export --------------------------------------------------------------------------------------


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of config/ without any privacy file, used as the config folder."""
    d = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, d, ignore=shutil.ignore_patterns("privacy.local.toml"))
    monkeypatch.setattr(photoncar.config, "CONFIG_DIR", d)
    return d


def test_export_refuses_without_the_privacy_file(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    cli.main(["replay", "--synthetic", "track_day", "--data-dir", str(data)])
    drive_id = only_drive(data).stem
    capsys.readouterr()
    code = cli.main(["export", "--drive", drive_id, "--kind", "laps", "--data-dir", str(data)])
    assert code == cli.REFUSED
    assert capsys.readouterr().err.strip() == cli.PRIVACY_MISSING
    assert not (data / "exports").exists()


def test_export_writes_lap_files(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (config_dir / "privacy.local.toml").write_text(
        "[home]\nlat_deg = 10.0\nlon_deg = 10.0\n", encoding="utf-8"
    )
    data = tmp_path / "data"
    cli.main(["replay", "--synthetic", "track_day", "--data-dir", str(data)])
    drive_id = only_drive(data).stem
    capsys.readouterr()
    code = cli.main(["export", "--drive", drive_id, "--kind", "laps", "--data-dir", str(data)])
    assert code == 0
    lines = capsys.readouterr().out.split()
    assert len(lines) == 3
    assert all(line.startswith("exports/lap-") and line.endswith(".json") for line in lines)


def test_export_unknown_drive(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["export", "--drive", "nope", "--kind", "laps", "--data-dir", str(tmp_path)])
    assert code == cli.REFUSED
    assert capsys.readouterr().err.strip() == "No drive with that ID."


# -- record --------------------------------------------------------------------------------------


def test_record_stops_with_a_plain_message_on_this_machine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With the shipped config (OBD polling off) the live session never opens."""
    code = cli.main(["record", "--no-serve", "--data-dir", str(tmp_path)])
    assert code == cli.REFUSED
    err = capsys.readouterr().err.strip()
    assert err.startswith("Live recording cannot start here. ")
    assert not (tmp_path / "drives").exists()


def test_record_reports_an_unavailable_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from photoncar.sources import obd

    unavailable = getattr(obd, "LiveCanUnavailable", RuntimeError)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise unavailable("Live CAN is off: can0 is not confirmed listen-only (not linux).")

    monkeypatch.setattr(obd, "open_live_can_session", refuse, raising=False)
    code = cli.main(["record", "--no-serve", "--data-dir", str(tmp_path)])
    assert code == cli.REFUSED
    assert capsys.readouterr().err.strip() == (
        "Live recording cannot start here. "
        "Live CAN is off: can0 is not confirmed listen-only (not linux)."
    )


def test_record_serves_the_dashboard_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard thread starts with the moving lock on, before any record arrives."""

    class FakeServer:
        def __init__(self, config: uvicorn.Config) -> None:
            self.app: Any = config.app
            self.should_exit = False

        def run(self) -> None:
            return None

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    cfg = load_config()
    ctx = cli.Context(cfg, load_vehicle("e90_m3"), "e90_m3", tmp_path, tmp_path)
    server, live = cli._serve_in_thread(ctx, "127.0.0.1", 8000)
    assert isinstance(server, FakeServer)
    assert server.app.state.live is live
    assert (live.mode, live.moving) == ("live", True)


def no_sensors(monkeypatch: pytest.MonkeyPatch) -> None:
    """open_live_sensors as on a Mac: no GPS or IMU here."""

    def refuse(cfg: Config, clock: Any) -> list[Any]:
        raise live_sensors.LiveSensorsUnavailable(
            "Live GPS and IMU need Linux. This system is Darwin."
        )

    monkeypatch.setattr(live_sensors, "open_live_sensors", refuse)


class FakeSession:
    """Stands in for LiveCanSession: a short synthetic drive in the shape the real session
    yields (CanFrame, ObdPoll and Sample records only, no synthetic signals, no IMU, no GPS)."""

    def __init__(self) -> None:
        self._drive = synthetic_drive(5, "commute", cfg=load_config())
        self.stopped = 0

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(name="live:can0+can1", kind="live")

    def records(self) -> Iterator[Record]:
        return (r for r in self._drive.records() if isinstance(r, CanFrame | ObdPoll | Sample))

    def stop(self) -> None:
        self.stopped += 1


def test_record_runs_a_live_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from photoncar.sources import obd

    sessions: list[FakeSession] = []

    def open_session(*args: Any, **kwargs: Any) -> FakeSession:
        sessions.append(FakeSession())
        return sessions[-1]

    monkeypatch.setattr(obd, "open_live_can_session", open_session, raising=False)
    no_sensors(monkeypatch)
    code = cli.main(["record", "--no-serve", "--data-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith(
        f"{SAFETY_LINE}\n"
        "Recording CAN and OBD only. Live GPS and IMU need Linux. This system is Darwin.\n"
        "Recording. Press Ctrl+C to stop.\n"
        "Live drive: drive "
    )
    assert sessions[0].stopped >= 1
    assert index_items(tmp_path) == [("Live drive", "live", True)]
    # No IMU in a live OBD session: the estimator runs on OBD speed alone.
    assert "  No IMU: speed and longitudinal accel come from speed measurements alone. " in out
    with DriveReader(only_drive(tmp_path)) as r:
        assert r.meta().source == "live"
        assert {s.inputs for s in r.state()} == {"obd", "obd+zupt"}
        stats = {s.stat_id for s in r.stats()}
    assert {"drive.max_speed_mph", "behavior.jerk_long_mean", "behavior.hard_brakes"} <= stats


class FakeSensor:
    """Stands in for a live GPS or IMU source: that stream of the same synthetic drive the
    FakeSession plays, on the same clock. ``fail_after`` raises mid-drive."""

    def __init__(self, kind: type[GpsFix] | type[ImuSample], fail_after: int | None = None):
        self._kind = kind
        self._drive = synthetic_drive(5, "commute", cfg=load_config())
        self._fail_after = fail_after
        self.stopped = 0
        self.closed = 0
        self.yielded = 0

    @property
    def meta(self) -> SourceMeta:
        name = "gps:/dev/ttyACM0" if self._kind is GpsFix else "imu:/dev/i2c-1@0x6a"
        return SourceMeta(name=name, kind="live")

    def records(self) -> Iterator[Record]:
        for r in self._drive.records():
            if isinstance(r, self._kind):
                if self._fail_after is not None and self.yielded >= self._fail_after:
                    raise OSError("device unplugged")
                self.yielded += 1
                yield r

    def stop(self) -> None:
        self.stopped += 1

    def close(self) -> None:
        self.closed += 1


def run_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sensors: list[FakeSensor]) -> int:
    from photoncar.sources import obd

    monkeypatch.setattr(obd, "open_live_can_session", lambda *a, **k: FakeSession(), raising=False)
    monkeypatch.setattr(live_sensors, "open_live_sensors", lambda cfg, clock: list(sensors))
    return cli.main(["record", "--no-serve", "--data-dir", str(tmp_path)])


def test_record_merges_the_live_gps_and_imu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gps, imu = FakeSensor(GpsFix), FakeSensor(ImuSample)
    code = run_record(tmp_path, monkeypatch, [gps, imu])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith(f"{SAFETY_LINE}\nRecording. Press Ctrl+C to stop.\nLive drive: drive ")
    assert "Recording CAN and OBD only" not in out and "No IMU" not in out
    with DriveReader(only_drive(tmp_path)) as r:
        assert r.meta().source == "live" and r.meta().closed_cleanly
        assert len(r.gps_fixes()) == gps.yielded > 0
        assert sum(1 for _ in r.iter_imu_samples()) == imu.yielded > 0
        inputs = {s.inputs.split("+")[0] for s in r.state()}
    assert "imu" in inputs  # the estimator ran on the IMU
    assert (gps.stopped, imu.stopped) >= (1, 1)
    assert (gps.closed, imu.closed) == (1, 1)


def test_a_sensor_that_fails_mid_drive_ends_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gps = FakeSensor(GpsFix, fail_after=50)
    code = run_record(tmp_path, monkeypatch, [gps])
    out = capsys.readouterr().out
    assert code == 0
    assert "gps:/dev/ttyACM0 stopped: device unplugged. Recording goes on without it." in out
    assert index_items(tmp_path) == [("Live drive", "live", True)]
    with DriveReader(only_drive(tmp_path)) as r:
        assert len(r.gps_fixes()) == 50
        assert r.obd_polls()  # CAN and OBD went on to the end


# -- export-embed ----------------------------------------------------------------------------


def embed_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """photoncar.export.embed, or a stand-in while the web build has not written it."""
    try:
        mod = importlib.import_module("photoncar.export.embed")
    except ModuleNotFoundError:
        mod = types.ModuleType("photoncar.export.embed")
        monkeypatch.setitem(sys.modules, "photoncar.export.embed", mod)
    if not hasattr(mod, "EmbedRefused"):
        refused = type("EmbedRefused", (ValueError,), {})
        monkeypatch.setattr(mod, "EmbedRefused", refused, raising=False)
    return mod


def test_export_embed_calls_the_exporter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Report:
        json_bytes: int
        files: int

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake(*args: Any, **kwargs: Any) -> Report:
        calls.append((args, kwargs))
        return Report(json_bytes=1200, files=7)

    mod = embed_module(monkeypatch)
    monkeypatch.setattr(mod, "export_embed", fake, raising=False)
    out_dir = tmp_path / "out"
    data = tmp_path / "data"
    code = cli.main(
        ["export-embed", "--drive", "d-1", "--out", str(out_dir), "--data-dir", str(data)]
    )
    assert code == 0
    ((args, kwargs),) = calls
    assert args == (data.resolve(), "d-1", out_dir.resolve())
    assert set(kwargs) == {"cfg", "web_dist_embed"}
    assert isinstance(kwargs["cfg"], Config)
    assert kwargs["web_dist_embed"] == REPO_ROOT / "web" / "dist-embed"
    out = capsys.readouterr().out
    assert out.splitlines() == [
        f"Embed written to {out_dir.resolve()}.",
        "  json_bytes: 1200",
        "  files: 7",
    ]


def test_export_embed_refusal_is_a_plain_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = embed_module(monkeypatch)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise mod.EmbedRefused("Live drives never ship in the embed.")

    monkeypatch.setattr(mod, "export_embed", refuse, raising=False)
    code = cli.main(["export-embed", "--drive", "d-1", "--out", str(tmp_path / "out")])
    assert code == cli.REFUSED
    assert capsys.readouterr().err.strip() == "Live drives never ship in the embed."


# -- the public dataset root -----------------------------------------------------------------


def test_the_dataset_root_does_not_follow_the_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture (plan H14) runs with a scratch PHOTONCAR_DATA_DIR and must still find the
    public dataset under data/public."""
    monkeypatch.setenv("PHOTONCAR_DATA_DIR", str(tmp_path / "capture-data"))
    monkeypatch.delenv("PHOTONCAR_PUBLIC_DIR", raising=False)
    assert default_dataset_root() == REPO_ROOT / "data" / "public" / "comma2k19"
    monkeypatch.setenv("PHOTONCAR_PUBLIC_DIR", str(tmp_path / "public"))
    assert default_dataset_root() == (tmp_path / "public" / "comma2k19").resolve()


def test_seed_demo_reads_the_dataset_from_public_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PHOTONCAR_PUBLIC_DIR", raising=False)
    empty = tmp_path / "public"
    empty.mkdir()
    args = ["seed-demo", "--data-dir", str(tmp_path / "data"), "--public-dir", str(empty)]
    assert cli.main(args) == cli.REFUSED
    assert capsys.readouterr().err.strip() == cli.NO_SAMPLE
    ns = cli.build_parser().parse_args(args)
    assert cli._context(ns).dataset_root == (empty / "comma2k19").resolve()
