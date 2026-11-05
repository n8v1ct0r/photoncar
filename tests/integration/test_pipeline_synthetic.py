"""The synthetic track day end to end through the pipeline and the store.

Known answers come from the scripted kinematics (``photoncar.sources.synthetic``): 0 to 60 mph
at a constant 0.5 g takes 5.470247230 s (5.117651427 s with 1 ft rollout), 60 to 0 mph at 1.0 g
takes 36.68128983 m, the oval's half circles give 25^2 / 100 = 6.25 m/s^2 of lateral
acceleration (0.637318 g), and a lap of 1228.3185 m at 25 m/s takes 49.13274123 s.

Tolerances, from the estimator and the sensor noise the scenario adds (GPS speed 0.03 m/s,
GPS position 0.15 m, IMU accel 0.03 m/s^2, all 1 sigma):

- 0 to 60 and rollout: 0.02 s. A fused speed error of 0.03 m/s at a 4.9 m/s^2 launch moves a
  crossing by 6 ms; across seeds 0 to 9 the largest error measured was 0.0061 s.
- 60 to 0: 0.15 m. A 0.05 m/s speed error at the 60 mph crossing moves the start by 5 ms at
  1.0 g, 0.14 m at 26.8 m/s; the largest error measured for seeds 0 to 9 was 0.058 m.
- Peak lateral: 0.01 g. v times yaw rate carries both noises; the 0.5 s mean leaves a bias of
  +0.002 to +0.0035 g across seeds 0 to 9.
- Lap time: 0.05 s. 0.15 m of position noise at 25 m/s is 6 ms per line crossing; the largest
  error measured for seeds 0 to 9 was 0.0101 s.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from photoncar.api.live import LiveState, ReplayData
from photoncar.clock import SimClock
from photoncar.config import Config, ConfigError, load_config, load_vehicle
from photoncar.detect.laps import StartFinishLine
from photoncar.detect.rules import RuleEngine
from photoncar.export.laps_v0 import export_drive_laps, validate_lap
from photoncar.export.report import drive_report
from photoncar.pipeline.runner import (
    SYNTHETIC_FAULT,
    LapLine,
    PipelineError,
    RunReport,
    SegmentChain,
    run_to_data_dir,
)
from photoncar.records import CanFrame, Event, ImuSample, ObdPoll, Record, Sample, record_time
from photoncar.sources.base import SourceMeta
from photoncar.sources.synthetic import SyntheticDriveSource, synthetic_drive
from photoncar.store.index import DriveIndex
from photoncar.store.reader import ALERTS_WITHOUT_TRIGGER_SQL, DriveReader
from photoncar.summary.stats import summary_input
from photoncar.units import G0, MPH, NS_PER_S, c_to_k

SEED = 7
Z60_S = 5.470247230
ROLLOUT_S = 5.117651427
S60_M = 36.68128983
LAT_G = 6.25 / G0
LAP_S = (2 * 300.0 + 2 * 3.141592653589793 * 100.0) / 25.0

Z60_TOL_S = 0.02
S60_TOL_M = 0.15
LAT_TOL_G = 0.01
LAP_TOL_S = 0.05


@pytest.fixture(scope="module")
def cfg() -> Config:
    return load_config()


@pytest.fixture(scope="module")
def vehicle() -> Config:
    return load_vehicle("e90_m3")


def lines_for(drive: SyntheticDriveSource) -> list[LapLine]:
    sf = drive.start_finish
    assert sf is not None and drive.track_length_m is not None
    line = StartFinishLine(sf.line_id, sf.x1, sf.y1, sf.x2, sf.y2)
    return [LapLine(line, 0.0, drive.track_length_m / 2)]


def replay(
    cfg: Config, vehicle: Config, data_dir: Path, seed: int, **kw: Any
) -> tuple[RunReport, SyntheticDriveSource]:
    drive = synthetic_drive(seed, "track_day", cfg=cfg, vehicle=vehicle)
    rep = run_to_data_dir(
        [drive],
        cfg,
        vehicle,
        data_dir,
        vehicle_profile="e90_m3",
        label="Synthetic track day",
        lines=lines_for(drive),
        **kw,
    )
    return rep, drive


@dataclass(frozen=True)
class Run:
    report: RunReport
    drive: SyntheticDriveSource
    data_dir: Path


@pytest.fixture(scope="module")
def run(cfg: Config, vehicle: Config, tmp_path_factory: pytest.TempPathFactory) -> Run:
    d = tmp_path_factory.mktemp("track_day")
    rep, drive = replay(cfg, vehicle, d, SEED)
    return Run(rep, drive, d)


@pytest.fixture
def reader(run: Run) -> Iterator[DriveReader]:
    with DriveReader(run.report.path) as r:
        yield r


def kinds(events: Sequence[Event], kind: str) -> list[Event]:
    return [e for e in events if e.kind == kind]


def run_events(run: Run) -> list[Event]:
    with DriveReader(run.report.path) as r:
        return r.events()


def test_run_report(run: Run) -> None:
    rep = run.report
    assert rep.n_errors == 0
    assert rep.records_in["ImuSample"] == rep.records_out["StateEstimate"]
    assert rep.records_in["GpsFix"] == 2182  # 10 Hz over 218.1 s
    assert rep.data_span_s == pytest.approx(run.drive.duration_s, abs=0.01)
    assert rep.realtime_factor == pytest.approx(rep.data_span_s / rep.wall_s)
    for stage in ("prepass", "write", "estimate", "rules", "performance", "behavior", "close"):
        assert stage in rep.stage_s
    with DriveIndex(run.data_dir / "index.sqlite") as index:
        items = index.list()
    # n_alerts counts measured alerts only: the scripted coolant fault is synthetic.
    assert [(i.label, i.source, i.closed_cleanly, i.n_events, i.n_alerts) for i in items] == [
        ("Synthetic track day", "synthetic", True, len(run_events(run)), 0)
    ]


def test_zero_to_sixty(reader: DriveReader) -> None:
    (ev,) = kinds(reader.events(), "perf.zero_to_sixty")
    assert ev.value == pytest.approx(Z60_S, abs=Z60_TOL_S)
    rollout = [c.stat_value for c in ev.citations if c.stat == "rollout_1ft_s"]
    assert rollout == [pytest.approx(ROLLOUT_S, abs=Z60_TOL_S)]
    assert ev.synthetic


def test_sixty_to_zero(reader: DriveReader) -> None:
    (ev,) = kinds(reader.events(), "perf.sixty_to_zero")
    assert ev.value == pytest.approx(S60_M, abs=S60_TOL_M)


def test_peak_lateral(reader: DriveReader) -> None:
    (ev,) = kinds(reader.events(), "perf.peak_lateral")
    assert ev.value == pytest.approx(LAT_G, abs=LAT_TOL_G)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_known_answers_hold_across_seeds(
    cfg: Config, vehicle: Config, tmp_path: Path, seed: int
) -> None:
    rep, _ = replay(cfg, vehicle, tmp_path, seed)
    with DriveReader(rep.path) as r:
        events = r.events()
        laps = r.laps()
    (z60,) = kinds(events, "perf.zero_to_sixty")
    (s60,) = kinds(events, "perf.sixty_to_zero")
    (lat,) = kinds(events, "perf.peak_lateral")
    assert z60.value == pytest.approx(Z60_S, abs=Z60_TOL_S)
    assert s60.value == pytest.approx(S60_M, abs=S60_TOL_M)
    assert lat.value == pytest.approx(LAT_G, abs=LAT_TOL_G)
    assert [lap.duration_ns / NS_PER_S for lap in laps] == [pytest.approx(LAP_S, abs=LAP_TOL_S)] * 3


def test_three_laps(reader: DriveReader, run: Run) -> None:
    laps = reader.laps()
    assert [(lap.lap_no, lap.line_id, lap.valid) for lap in laps] == [
        (1, "sf", True),
        (2, "sf", True),
        (3, "sf", True),
    ]
    for lap in laps:
        assert lap.duration_ns / NS_PER_S == pytest.approx(LAP_S, abs=LAP_TOL_S)
    assert laps[1].t_start_ns == laps[0].t_end_ns
    assert laps[2].t_start_ns == laps[1].t_end_ns


def test_coolant_alert_cites_its_window(reader: DriveReader, run: Run, cfg: Config) -> None:
    alerts = [e for e in reader.events() if e.kind.startswith("alert.")]
    assert [e.kind for e in alerts] == ["alert.coolant_high"]
    ev = alerts[0]
    assert ev.synthetic
    assert ev.message == f"Coolant 106 °C, over 105 °C. {SYNTHETIC_FAULT}"
    assert ev.resolved
    exc = run.drive.coolant_excursion
    assert exc is not None
    hot_start = run.drive.t0_ns + round(exc[0] * NS_PER_S)
    hot_end = run.drive.t0_ns + round(exc[1] * NS_PER_S)
    slow_period_ns = round(10 / 4.0 * NS_PER_S)  # 10 slow PIDs share 4 Hz: 1 coolant poll / 2.5 s
    assert hot_start <= ev.t_start_ns <= hot_start + slow_period_ns
    assert hot_end <= ev.t_end_ns <= hot_end + slow_period_ns
    (trigger,) = [c for c in ev.citations if c.role == "trigger"]
    assert (trigger.signal, trigger.source, trigger.stat) == ("obd.coolant_temp", "OBD 0x05", "min")
    assert trigger.stat_value == pytest.approx(c_to_k(106.0))
    assert trigger.t_start_ns == ev.t_start_ns
    on_ns = round(float(cfg.get("rules.coolant_high.on_s")) * NS_PER_S)
    assert on_ns <= trigger.t_end_ns - trigger.t_start_ns <= on_ns + slow_period_ns
    assert trigger.n >= 4


def test_citation_query_returns_no_rows(run: Run, reader: DriveReader) -> None:
    assert reader.alerts_without_trigger() == []
    con = sqlite3.connect(run.report.path)
    try:
        assert con.execute(ALERTS_WITHOUT_TRIGGER_SQL).fetchall() == []
        assert con.execute("SELECT count(*) FROM events WHERE kind LIKE 'alert.%'").fetchone() == (
            1,
        )
    finally:
        con.close()


def test_every_event_is_marked_synthetic(reader: DriveReader) -> None:
    events = reader.events()
    assert events
    assert all(e.synthetic for e in events)


def test_drive_stats_complete(reader: DriveReader, vehicle: Config) -> None:
    stats = {s.stat_id: s for s in reader.stats()}
    edges = [int(e) for e in vehicle.get("rpm.bands")]
    bands = {f"behavior.rpm_band.{lo}_{hi}" for lo, hi in pairwise(edges)}
    expected = {
        "drive.distance_mi",
        "drive.duration_s",
        "drive.max_speed_mph",
        "perf.zero_to_sixty_s",
        "perf.sixty_to_zero_m",
        "perf.peak_lateral_g",
        "behavior.hard_brakes",
        "behavior.hard_accels",
        "behavior.corners",
        "alerts.count",
        "alerts.synthetic_count",
        "behavior.jerk_long_mean",
        "behavior.jerk_long_p95",
        "behavior.jerk_lat_mean",
        "behavior.jerk_lat_p95",
        *bands,
    }
    assert set(stats) == expected
    for s in stats.values():
        if s.stat_id == "drive.duration_s" or s.stat_id.startswith("behavior.rpm_band."):
            # Time spent reads as the web shows durations: "42 s", "15 min 0 s", "1 h 3 min".
            assert re.fullmatch(r"\d+ s|\d+ min \d+ s|\d+ h \d+ min", s.display), s.display
        else:
            assert re.fullmatch(r"-?\d+(\.\d{1,3})?( [^\d]+)?", s.display), s.display
        assert "—" not in s.label and "–" not in s.label
    assert stats["perf.zero_to_sixty_s"].display == f"{stats['perf.zero_to_sixty_s'].value:.2f} s"
    assert stats["perf.peak_lateral_g"].display == f"{stats['perf.peak_lateral_g'].value:.2f} g"
    # The only alert is the scripted coolant fault: a synthetic alert, never a measured one.
    assert (stats["alerts.count"].display, stats["alerts.count"].label) == ("0", "Alerts")
    synth = stats["alerts.synthetic_count"]
    assert (synth.value, synth.display, synth.label) == (1.0, "1", "Synthetic alerts")
    model_sees = {i["id"]: i for i in summary_input(stats.values())}
    assert model_sees["alerts.count"]["display"] == "0"
    assert model_sees["alerts.synthetic_count"]["label"] == "Synthetic alerts"
    assert stats["drive.max_speed_mph"].value == pytest.approx(70.0, abs=0.5)
    band_s = sum(stats[b].value for b in bands)
    assert band_s == pytest.approx(stats["drive.duration_s"].value, abs=1.0)
    assert stats["drive.distance_mi"].display.endswith(" mi")


def test_report_reads_the_stats_and_gears(reader: DriveReader) -> None:
    report = drive_report(reader)
    b = report["behavior"]
    assert b["jerk_long_mean"] is not None and b["jerk_lat_p95"] is not None
    assert len(b["rpm_bands"]) == 9
    assert b["shifts"]
    for shift in b["shifts"]:
        assert isinstance(shift["from_gear"], int) and isinstance(shift["to_gear"], int)
        assert shift["from_gear"] != shift["to_gear"]
    perf = report["performance"]
    assert perf["zero_to_sixty"][0]["seconds"] == pytest.approx(Z60_S, abs=Z60_TOL_S)
    assert [lap["lap_no"] for lap in report["laps"]] == [1, 2, 3]


def test_lap_export_validates_against_lap_v0(
    reader: DriveReader, cfg: Config, tmp_path: Path
) -> None:
    privacy = tmp_path / "privacy.local.toml"
    with pytest.raises(ConfigError):
        export_drive_laps(reader, cfg, base=tmp_path, privacy_path=privacy)
    privacy.write_text("[home]\nlat_deg = 10.0\nlon_deg = 10.0\n", encoding="utf-8")
    files = export_drive_laps(reader, cfg, base=tmp_path, privacy_path=privacy)
    assert len(files) == 3
    for f in files:
        assert f.parent == tmp_path / "exports"
        doc = json.loads(f.read_text(encoding="utf-8"))
        validate_lap(doc)
        assert doc["source"] == "synthetic"
        assert doc["duration_ms"] == pytest.approx(LAP_S * 1000, abs=LAP_TOL_S * 1000)


def events_hash(path: Path) -> str:
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT event_id, kind, rule_id, rule_version, severity, t_start_ns, t_end_ns, value, "
            "threshold, unit, message, resolved, synthetic FROM events ORDER BY event_id"
        ).fetchall()
        cites = con.execute(
            "SELECT event_id, signal, source, role, t_start_ns, t_end_ns, stat, stat_value, unit, "
            "n FROM event_citations ORDER BY event_id, signal, role"
        ).fetchall()
    finally:
        con.close()
    return hashlib.sha256(repr((rows, cites)).encode("utf-8")).hexdigest()


def test_replay_twice_gives_identical_events(
    run: Run, cfg: Config, vehicle: Config, tmp_path: Path
) -> None:
    again, _ = replay(cfg, vehicle, tmp_path, SEED)
    assert again.drive_id != run.report.drive_id
    assert events_hash(again.path) == events_hash(run.report.path)


# -- live mode -----------------------------------------------------------------------------------


class RecordingLive(LiveState):
    """A LiveState that keeps every frame and every moving change."""

    def __init__(self) -> None:
        super().__init__()
        self.frames: list[dict[str, Any]] = []
        self.moving_log: list[bool] = []
        self.modes: list[str] = []

    def publish(self, frame: Any) -> None:
        super().publish(frame)
        self.frames.append(dict(frame))

    def set_moving(self, moving: bool) -> None:
        super().set_moving(moving)
        self.moving_log.append(moving)

    def set_mode(self, mode: str) -> None:
        super().set_mode(mode)
        self.modes.append(mode)


LIVE_SESSION_RECORDS = (CanFrame, ObdPoll, Sample)
"""The only record types LiveCanSession.records() yields."""


class LiveShaped:
    """A synthetic drive shaped like ``LiveCanSession``: a live source with no synthetic
    signals that yields CanFrame, ObdPoll and Sample records only (no IMU, no GPS), as a real
    OBD session does. Its OBD speed is the only speed the pipeline gets."""

    def __init__(self, drive: SyntheticDriveSource) -> None:
        self._drive = drive

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(name="live:can0+can1", kind="live")

    def records(self) -> Iterator[Record]:
        return (r for r in self._drive.records() if isinstance(r, LIVE_SESSION_RECORDS))


def test_live_mode_frames_and_moving_lock(cfg: Config, vehicle: Config, tmp_path: Path) -> None:
    drive = synthetic_drive(SEED, "track_day", cfg=cfg, vehicle=vehicle)
    live = RecordingLive()
    rep = run_to_data_dir(
        [LiveShaped(drive)],
        cfg,
        vehicle,
        tmp_path,
        vehicle_profile="e90_m3",
        label="Live drive",
        live=live,
    )
    assert rep.n_errors == 0
    assert set(rep.records_in) == {"ObdPoll", "Sample"}
    # No IMU: the estimator runs speed-only, 1 estimate per OBD speed poll once the IMU is
    # overdue (every poll after the first).
    polls = [r for r in LiveShaped(drive).records() if isinstance(r, Sample)]
    n_speed = sum(1 for r in polls if r.signal == "obd.speed")
    assert rep.records_out["StateEstimate"] == n_speed - 1
    assert live.modes == ["live", "idle"]
    assert live.mode == "idle" and not live.moving
    # Starts locked, unlocks after 2 s at rest, locks on the launch, and so on to the end:
    # OBD speed alone drives the lock.
    assert live.moving_log[0] is True
    assert live.moving_log[1:] == [False, True, False, True, False, False]
    # A frame goes out at the first record of each 1 / stream_hz period that has any record.
    period_ns = round(NS_PER_S / float(cfg.get("live.stream_hz")))
    times = [record_time(r) for r in LiveShaped(drive).records()]
    assert len(live.frames) == len({(t - times[0]) // period_ns for t in times})
    # Every frame shows speed from the second OBD speed poll on (2 Hz: within 1 s).
    assert all(f["t_s"] < 1.0 for f in live.frames if f["speed_mph"] is None)
    assert all(f["synthetic_fields"] == [] for f in live.frames)  # a live drive, all measured
    exc = drive.coolant_excursion
    assert exc is not None
    # Coolant polls come every 2.5 s: the first hot one lands up to 2.5 s after the excursion
    # starts, and the alert fires at the first poll 10 s after it, so by 15 s in at the latest.
    hot = [f for f in live.frames if exc[0] + 15.0 <= f["t_s"] <= exc[1]]
    cold = [f for f in live.frames if f["t_s"] < exc[0] - 10.0]
    assert hot and all(f["alert"] is not None for f in hot)
    assert {f["alert"]["headline"] for f in hot} == {"Coolant 106 °C"}
    assert all(f["alert"]["citation"].startswith("OBD 0x05 over 105 °C for ") for f in hot)
    assert all(f["alert"] is None for f in cold)
    assert max(f["speed_mph"] for f in live.frames if f["speed_mph"] is not None) == (
        pytest.approx(70.0, abs=0.5)
    )
    with DriveReader(rep.path) as r:
        assert r.meta().source == "live"
        assert r.meta().closed_cleanly
        events = r.events()
        stats = {s.stat_id for s in r.stats()}
        states = r.state()
        timeline = r.timeline(100_000)
    assert {s.inputs for s in states} == {"obd", "obd+zupt"}
    assert all(math.isnan(s.accel_lat_mps2) and math.isnan(s.yaw_rate_rps) for s in states)
    assert set(timeline["accel_lat_g"]) == {None}
    assert any(a is not None and a > 0.4 for a in timeline["accel_long_g"])  # the 0.5 g launch
    # The runs come from the speed-only estimates of OBD speed. OBD speed comes at 2 Hz in
    # 1 km/h steps, so a 60 mph crossing can move by up to 1 poll period (0.5 s) from the
    # scripted answer.
    (z60,) = kinds(events, "perf.zero_to_sixty")
    assert z60.value == pytest.approx(Z60_S, abs=0.5)
    (s60,) = kinds(events, "perf.sixty_to_zero")
    assert s60.value == pytest.approx(S60_M, abs=2.0)
    for ev in (z60, s60):
        assert {c.signal for c in ev.citations if c.role == "trigger"} == {"state.speed"}
    assert kinds(events, "behavior.hard_brake") and kinds(events, "behavior.hard_accel")
    assert kinds(events, "behavior.shift")
    assert not kinds(events, "behavior.corner") and not kinds(events, "perf.peak_lateral")
    assert {"behavior.jerk_long_mean", "behavior.jerk_long_p95"} <= stats
    assert not {"behavior.jerk_lat_mean", "perf.peak_lateral_g"} & stats
    assert (
        "No IMU: speed and longitudinal accel come from speed measurements alone. "
        "No lateral g, cornering, or lateral jerk."
    ) in rep.notes


class QuietBus:
    """A live source whose bus never carries a frame. It notes the lock when first read."""

    def __init__(self, live: LiveState) -> None:
        self._live = live
        self.moving_when_read: list[bool] = []

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(name="live:quiet", kind="live")

    def records(self) -> Iterator[Record]:
        self.moving_when_read.append(self._live.moving)
        yield from ()


def test_live_run_is_locked_before_the_first_record(
    cfg: Config, vehicle: Config, tmp_path: Path
) -> None:
    live = RecordingLive()
    quiet = QuietBus(live)
    with pytest.raises(PipelineError, match="no records"):
        run_to_data_dir(
            [quiet], cfg, vehicle, tmp_path, vehicle_profile="e90_m3", label="Live drive", live=live
        )
    assert quiet.moving_when_read == [True]
    assert live.moving_log == [True, True]  # locked at the start, kept locked after the failure
    assert live.moving and live.mode == "idle"


class ReplayWithOverlay:
    """A replay with measured CAN rpm and wheel speed at 10 Hz, plus a synthetic OBD overlay
    at 2 Hz that reads higher (the shape of a comma2k19 replay)."""

    RPM, OVERLAY_RPM = 1500.0, 3000.0

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(
            name="replay:overlay", kind="replay", synthetic_signals=("obd.rpm", "obd.speed")
        )

    def records(self) -> Iterator[Record]:
        t0 = 1_000 * NS_PER_S
        for i in range(300):  # 30 s
            t = t0 + i * NS_PER_S // 10
            yield Sample(t, "can.engine_rpm", self.RPM)
            yield Sample(t, "can.wheel_speed", 12.0)
            if i % 5 == 0:
                yield Sample(t + 1, "obd.rpm", self.OVERLAY_RPM)
                yield Sample(t + 1, "obd.speed", 20.0)


def test_timeline_and_live_frames_show_measured_rpm_over_the_synthetic_overlay(
    cfg: Config, vehicle: Config, tmp_path: Path
) -> None:
    live = RecordingLive()
    rep = run_to_data_dir(
        [ReplayWithOverlay()],
        cfg,
        vehicle,
        tmp_path,
        vehicle_profile="e90_m3",
        label="Replay",
        live=live,
    )
    rpm = ReplayWithOverlay.RPM
    assert {f["rpm"] for f in live.frames} == {rpm}
    # Speed comes from the estimator, speed-only here (no IMU): its first estimate is at the
    # second wheel speed sample, 0.1 s in, once the IMU is overdue. The first 2 frames go out
    # on rpm samples before it.
    assert [f["speed_mph"] for f in live.frames[:2]] == [None, None]
    assert all(f["speed_mph"] == pytest.approx(12.0 / MPH) for f in live.frames[2:])
    # rpm is measured; the synthetic overlay never feeds a frame.
    assert all(f["synthetic_fields"] == [] for f in live.frames)
    with DriveReader(rep.path) as r:
        assert set(r.meta().synthetic_signals) == {"obd.rpm", "obd.speed"}
        rows = r.timeline_rows()
        replay_data = ReplayData.load(r, cfg)
    assert {row.rpm for row in rows} == {rpm}
    assert {row.speed_mps for row in rows} == {12.0}
    assert {replay_data.frame_at(t / 10)["rpm"] for t in range(0, 290, 7)} == {rpm}


def test_replay_mode_publishes_frames_without_the_lock(
    cfg: Config, vehicle: Config, tmp_path: Path
) -> None:
    live = RecordingLive()
    rep, _ = replay(cfg, vehicle, tmp_path, SEED, live=live)
    assert live.modes == ["replay", "idle"]
    assert live.moving_log == [False]  # only the reset when the run ends
    assert len(live.frames) == pytest.approx(rep.data_span_s * 10.0, abs=1)


class VoltageCut:
    """The commute drive with the module voltage polls cut after ``CUT_S``: a stale alert."""

    CUT_S = 60.0

    def __init__(self, drive: SyntheticDriveSource) -> None:
        self._drive = drive

    @property
    def meta(self) -> SourceMeta:
        return self._drive.meta

    def records(self) -> Iterator[Record]:
        t0: int | None = None
        for r in self._drive.records():
            t = record_time(r)
            t0 = t if t0 is None else t0
            cut = t - t0 > self.CUT_S * NS_PER_S
            if cut and isinstance(r, Sample) and r.signal == "obd.module_voltage":
                continue
            yield r


def test_stored_live_frames_show_alerts_when_the_pipeline_did(
    cfg: Config, vehicle: Config, tmp_path: Path
) -> None:
    """The live_frame endpoint and the SSE replay are captures of the live view: at every
    frame the pipeline published, the stored drive gives the same alert, text included."""
    drive = synthetic_drive(SEED, "commute", cfg=cfg, vehicle=vehicle)
    live = RecordingLive()
    rep = run_to_data_dir(
        [VoltageCut(drive)], cfg, vehicle, tmp_path, vehicle_profile="e90_m3", label="x", live=live
    )
    with DriveReader(rep.path) as r:
        (stale,) = [e for e in r.events() if e.kind == "alert.stale"]
        t0 = r.meta().t_start_ns
        assert t0 is not None
        replay_data = ReplayData.load(r, cfg)
    limit_s = RuleEngine(cfg).stale_limit_s("obd.module_voltage")
    assert stale.threshold == pytest.approx(limit_s)
    assert stale.synthetic and (stale.t_end_ns - stale.t_start_ns) / NS_PER_S > 100.0
    with_alert = [f for f in live.frames if f["alert"] is not None]
    assert with_alert
    for f in live.frames:
        assert replay_data.frame_at(f["t_s"])["alert"] == f["alert"], f["t_s"]
    # The stale alert shows once the gap passes the limit, with the gap so far, and labeled.
    last_s = (stale.t_start_ns - t0) / NS_PER_S
    first = with_alert[0]
    assert last_s + limit_s < first["t_s"] <= last_s + limit_s + 0.1
    assert first["alert"] == {
        "severity": 1,
        "headline": "Module voltage stale, synthetic fault",
        "citation": f"OBD 0x42 no update for {first['t_s'] - last_s:.0f} s",
        "synthetic": True,
    }
    assert replay_data.frame_at(last_s + limit_s - 0.01)["alert"] is None


# -- RuleEngine.active ---------------------------------------------------------------------------


def test_rule_engine_active_is_read_only(cfg: Config) -> None:
    t0 = 1_000 * NS_PER_S
    engine = RuleEngine(cfg)
    reference = RuleEngine(cfg)
    emitted: list[Event] = []
    ref_emitted: list[Event] = []
    seen: dict[float, list[Event]] = {}
    for k in range(0, 1200):  # coolant every 0.1 s for 120 s: 106 °C from 10 s to 40 s
        t_s = k / 10.0
        c = 106.0 if 10.0 <= t_s < 40.0 else 90.0
        s = Sample(t0 + round(t_s * NS_PER_S), "obd.coolant_temp", c_to_k(c))
        emitted += engine.push(s)
        ref_emitted += reference.push(s)
        if t_s in (15.0, 20.0, 30.0, 45.0, 75.0):
            seen[t_s] = engine.active(s.t_ns)
    end = t0 + 120 * NS_PER_S
    emitted += engine.finish(end)
    ref_emitted += reference.finish(end)
    assert emitted == ref_emitted
    assert seen[15.0] == []  # inside the 10 s on-delay
    for t_s in (20.0, 30.0, 45.0):
        (ev,) = seen[t_s]
        assert ev.kind == "alert.coolant_high" and not ev.resolved
        assert ev.t_end_ns == t0 + round(t_s * NS_PER_S)
        assert [c.role for c in ev.citations] == ["trigger", "context"]
    assert seen[75.0] == []  # cleared after 30 s under 102 °C
    assert [e.kind for e in emitted] == ["alert.coolant_high"]


def test_rule_engine_active_reports_stale_signals(cfg: Config) -> None:
    t0 = 5_000 * NS_PER_S
    engine = RuleEngine(cfg)
    limit_s = engine.stale_limit_s("obd.coolant_temp")
    engine.push(Sample(t0, "obd.coolant_temp", c_to_k(90.0)))
    assert engine.active(t0 + round(limit_s * NS_PER_S)) == []
    (ev,) = engine.active(t0 + round((limit_s + 1.0) * NS_PER_S))
    assert ev.kind == "alert.stale" and ev.t_start_ns == t0 and not ev.resolved
    assert ev.citations[0].signal == "obd.coolant_temp"


# -- SegmentChain --------------------------------------------------------------------------------


class Part:
    def __init__(self, name: str, times_ms: Sequence[int]) -> None:
        self._meta = SourceMeta(name=name, kind="replay", dataset_ref=name)
        self._times = times_ms

    @property
    def meta(self) -> SourceMeta:
        return self._meta

    def records(self) -> Iterator[Record]:
        for i, t in enumerate(self._times):
            yield CanFrame(t * 1_000_000, 0, int(self._meta.name[-1]), False, 8, bytes([i] * 8))

    def imu_samples(self) -> Iterator[ImuSample]:
        return iter(())


def test_segment_chain_merges_overlapping_ends() -> None:
    a = Part("seg1", [0, 10, 20, 30, 40])
    b = Part("seg2", [25, 30, 35, 50])
    c = Part("seg3", [60, 70])
    chain = SegmentChain([a, b, c], name="route", dataset_ref="route/1..3")
    out = list(chain.records())
    times = [record_time(r) // 1_000_000 for r in out]
    assert times == [0, 10, 20, 25, 30, 30, 35, 40, 50, 60, 70]
    tie = [r for r in out if record_time(r) == 30_000_000]
    assert [r.arb_id for r in tie if isinstance(r, CanFrame)] == [1, 2]  # earlier part first
    assert chain.meta.kind == "replay"


# -- synthetic fields in live frames ---------------------------------------------------------


class ReplayWithSyntheticCoolant:
    """A replay with measured CAN rpm and wheel speed at 10 Hz and a synthetic OBD coolant
    overlay in the slow group (the shape of a comma2k19 replay's coolant)."""

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(
            name="replay:coolant", kind="replay", synthetic_signals=("obd.coolant_temp",)
        )

    def records(self) -> Iterator[Record]:
        t0 = 1_000 * NS_PER_S
        for i in range(300):  # 30 s
            t = t0 + i * NS_PER_S // 10
            yield Sample(t, "can.engine_rpm", 2000.0)
            yield Sample(t, "can.wheel_speed", 15.0)
            if i % 25 == 0:
                yield Sample(t + 1, "obd.coolant_temp", c_to_k(90.0))


def test_live_frames_name_the_fields_synthetic_signals_feed(
    cfg: Config, vehicle: Config, tmp_path: Path
) -> None:
    live = RecordingLive()
    rep = run_to_data_dir(
        [ReplayWithSyntheticCoolant()], cfg, vehicle, tmp_path, vehicle_profile="e90_m3",
        label="Replay", live=live,
    )  # fmt: skip
    with_coolant = [f for f in live.frames if f["coolant_c"] is not None]
    assert with_coolant and all(f["synthetic_fields"] == ["coolant_c"] for f in with_coolant)
    assert all(f["synthetic_fields"] == [] for f in live.frames if f["coolant_c"] is None)
    with DriveReader(rep.path) as r:
        stored = ReplayData.load(r, cfg)
    # The stored frame interpolates between coolant samples (the live one holds the latest),
    # so compare where it has a value: the same label.
    compared = [stored.frame_at(f["t_s"]) for f in with_coolant]
    compared = [g for g in compared if g["coolant_c"] is not None]
    assert len(compared) > 200
    assert all(g["synthetic_fields"] == ["coolant_c"] for g in compared)

    # A synthetic drive: every field it shows is synthetic, speed included.
    track = RecordingLive()
    replay(cfg, vehicle, tmp_path / "track", SEED, live=track)
    full = [f for f in track.frames if None not in (f["speed_mph"], f["rpm"], f["coolant_c"])]
    assert full and all(f["synthetic_fields"] == ["speed_mph", "rpm", "coolant_c"] for f in full)


# -- rested hours for the rested-voltage advisory ---------------------------------------------


class KeyOn:
    """A short live OBD session: module voltage 12.00 V at key-on (under the 12.29 V AGM
    limit), then the engine starts."""

    @property
    def meta(self) -> SourceMeta:
        return SourceMeta(name="live:can0+can1", kind="live")

    def records(self) -> Iterator[Record]:
        t0 = 50 * NS_PER_S
        yield Sample(t0, "obd.module_voltage", 12.0)
        yield Sample(t0 + NS_PER_S // 2, "obd.rpm", 0.0)
        for k in range(1, 11):
            t = t0 + k * NS_PER_S
            yield Sample(t, "obd.rpm", 800.0)
            yield Sample(t + 1, "obd.speed", 0.0)
            yield Sample(t + 2, "obd.module_voltage", 14.1)


def test_rested_voltage_advisory_uses_the_rest_since_the_last_live_drive(
    cfg: Config, vehicle: Config, tmp_path: Path
) -> None:
    hour = 3600 * NS_PER_S
    utc = SimClock(1_789_992_000 * NS_PER_S)  # 2026-09-21 12:00 UTC

    def drive() -> list[Event]:
        rep = run_to_data_dir(
            [KeyOn()], cfg, vehicle, tmp_path, vehicle_profile="e90_m3", label="Live drive",
            utc=utc,
        )  # fmt: skip
        with DriveReader(rep.path) as r:
            return [e for e in r.events() if e.kind == "alert.voltage_low_rested"]

    assert drive() == []  # the first drive: how long the car rested is unknown
    utc.advance(30 * hour)
    (advisory,) = drive()  # 30 h after the last drive ended, at least the 24 h the rule needs
    assert advisory.message == "Rested module voltage 12.00 V, under 12.29 V. Advisory only."
    utc.advance(1 * hour)
    assert drive() == []  # rested 1 h
    # A replay in between is the dataset's car: it neither ends nor starts a rest.
    replay(cfg, vehicle, tmp_path, SEED)
    utc.advance(25 * hour)
    assert len(drive()) == 1
    # A live drive that never closed has no end, so the next rest is unknown: no advisory.
    with pytest.raises(PipelineError):
        run_to_data_dir(
            [QuietBus(LiveState())], cfg, vehicle, tmp_path, vehicle_profile="e90_m3",
            label="Live drive", utc=utc,
        )  # fmt: skip
    utc.advance(30 * hour)
    assert drive() == []
