"""The public dataset through the whole pipeline: the pinned segment and the full default route.

Needs ``make fetch-dataset`` (the pinned segment); the route tests also need ``make
fetch-route`` and skip without it.

The route's standing start. docs/dataset.md measures it on the raw CAN speed as 24.396 s: from
the last sample under 0.5 m/s (0.485 m/s) to the first sample at or over 60 mph. The pipeline's
``zero_to_sixty`` measures the fused speed instead: from v = 0 (the 0.3 m/s crossing extrapolated
back along the launch) to the interpolated 60 mph crossing. The two starts differ by the time the
car takes to reach 0.485 m/s, and the two ends by estimator lag. Measured on 2026-09-23: the fused
start is 0.158 s before the raw start sample, and the fused 60 mph crossing 0.020 s after the raw
interpolated crossing. The tests allow 0.3 s on the start gap, 0.05 s on the end, and 0.25 s on
the total.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from photoncar import cli
from photoncar.config import Config, load_config, load_vehicle
from photoncar.detect.behavior import NO_GEARS_NOTE
from photoncar.export.report import drive_report
from photoncar.pipeline.runner import RunReport
from photoncar.records import Event
from photoncar.sources.comma2k19 import Comma2k19Source, default_dataset_root
from photoncar.store.index import DriveIndex
from photoncar.store.reader import DriveReader
from photoncar.store.rollup import CAN_MAX_GAP_NS, interp_gap
from photoncar.units import MPH_60, NS_PER_S

pytestmark = pytest.mark.dataset

ROOT = default_dataset_root()
SAMPLE = ROOT / cli.SAMPLE_REF
ROUTE = ROOT / cli.ROUTE_REF
ROUTE_STANDING_START_S = 24.396
START_GAP_TOL_S = 0.3
END_TOL_S = 0.05
TOTAL_TOL_S = 0.25


@pytest.fixture(scope="module")
def cfg() -> Config:
    return load_config()


def context(cfg: Config, data_dir: Path) -> cli.Context:
    profile = str(cfg.get("vehicle.profile"))
    return cli.Context(cfg, load_vehicle(profile), profile, data_dir, ROOT)


@dataclass(frozen=True)
class Run:
    report: RunReport
    data_dir: Path


@pytest.fixture(scope="module")
def sample_run(cfg: Config, tmp_path_factory: pytest.TempPathFactory) -> Run:
    if not SAMPLE.is_dir():
        pytest.fail("The comma2k19 sample is missing. Run make fetch-dataset.")
    d = tmp_path_factory.mktemp("sample")
    rep = cli.replay_dataset(context(cfg, d), [SAMPLE], cli.LABEL_SAMPLE, seed=0)
    return Run(rep, d)


@pytest.fixture(scope="module")
def route_run(cfg: Config, tmp_path_factory: pytest.TempPathFactory) -> Run:
    if not (ROUTE / "14").is_dir():
        pytest.skip("Optional route not fetched: make fetch-route")
    d = tmp_path_factory.mktemp("route")
    rep = cli.replay_dataset(context(cfg, d), cli.segment_dirs(ROUTE), cli.LABEL_ROUTE, seed=0)
    return Run(rep, d)


@pytest.fixture
def sample_reader(sample_run: Run) -> Iterator[DriveReader]:
    with DriveReader(sample_run.report.path) as r:
        yield r


@pytest.fixture
def route_reader(route_run: Run) -> Iterator[DriveReader]:
    with DriveReader(route_run.report.path) as r:
        yield r


def test_sample_runs_with_no_errors(sample_run: Run) -> None:
    rep = sample_run.report
    assert rep.n_errors == 0
    assert rep.errors == {"dbc_decode": 0}
    assert rep.records_in == {
        "CanFrame": 53800 + 45000,
        "GpsFix": 579,
        "ImuSample": 6256,
        "ObdPoll": 599,
        "Sample": 599,
    }
    # WHEEL_SPEEDS and STEER_ANGLE_SENSOR at 4,974 frames each, ENGINE_RPM at 2,547.
    assert rep.records_out["Sample"] == 2 * 4974 + 2547
    assert rep.records_out["StateEstimate"] == 6256 - 1  # the first IMU sample predates speed
    assert rep.data_span_s == pytest.approx(60.0, abs=0.1)


def test_sample_drive_file(sample_run: Run, sample_reader: DriveReader) -> None:
    meta = sample_reader.meta()
    assert meta.source == "replay"
    assert meta.closed_cleanly
    assert meta.dataset_ref == cli.SAMPLE_REF
    assert meta.dbc_name == "toyota_new_mc_pt_generated.dbc"
    assert meta.vehicle_profile == "comma2k19_rav4"  # the dataset car, not the configured one
    assert meta.dbc_sha256 is not None and len(meta.dbc_sha256) == 64
    assert set(meta.synthetic_signals) == {
        "obd.coolant_temp",
        "obd.fuel_system",
        "obd.iat",
        "obd.load",
        "obd.ltft_b1",
        "obd.ltft_b2",
        "obd.maf",  # the RAV4 profile does not rule out a MAF sensor, as the E90's does
        "obd.module_voltage",
        "obd.rpm",
        "obd.speed",
        "obd.stft_b1",
        "obd.stft_b2",
        "obd.throttle",
    }
    assert sample_reader.alerts_without_trigger() == []
    for ev in sample_reader.events():
        cited = {c.signal for c in ev.citations if c.role == "trigger"}
        assert ev.synthetic == bool(cited & set(meta.synthetic_signals))
    stats = {s.stat_id: s.display for s in sample_reader.stats()}
    assert stats["drive.distance_mi"] == "0.62 mi"  # 1.00 km
    assert stats["drive.duration_s"] == "1 min 0 s"
    with DriveIndex(sample_run.data_dir / "index.sqlite") as index:
        (item,) = index.list()
    assert (item.label, item.source, item.closed_cleanly) == (cli.LABEL_SAMPLE, "replay", True)


def assert_plausible_gears(run: Run, reader: DriveReader) -> list[Event]:
    """0 shifts with the no-gears note, or shifts between at most max_gears gears, each cited
    on the measured CAN rpm."""
    profile = reader.meta().vehicle_profile
    assert profile is not None
    max_gears = int(load_vehicle(profile).get("vehicle.max_gears"))
    shifts = [e for e in reader.events() if e.kind == "behavior.shift"]
    gears = {int(c.stat_value) for e in shifts for c in e.citations if c.unit == "gear"}
    assert len(gears) <= max_gears and all(1 <= g <= max_gears for g in gears)
    assert all(e.citations[0].signal == "can.engine_rpm" and not e.synthetic for e in shifts)
    reported = drive_report(reader)["behavior"]["shifts"]
    assert len(reported) == len(shifts)
    if not shifts:
        assert NO_GEARS_NOTE in run.report.notes
    return shifts


def test_sample_decodes_real_rpm_for_behavior(sample_run: Run, sample_reader: DriveReader) -> None:
    t, rpm = sample_reader.sample_arrays("can.engine_rpm")
    assert t.size == 2547
    assert float(np.min(rpm)) > 900.0
    assert float(np.max(rpm)) < 2600.0
    assert_plausible_gears(sample_run, sample_reader)
    # The timeline (review, live frames) shows the same measured rpm, never the synthetic OBD
    # overlay, which reads differently (plan H8): blank where CAN rpm has a gap.
    rows = sample_reader.timeline_rows()
    want = interp_gap(t, rpm, np.array([r.t_ns for r in rows], dtype=np.int64), CAN_MAX_GAP_NS)
    got = np.array([np.nan if r.rpm is None else r.rpm for r in rows], dtype=np.float64)
    assert np.array_equal(np.isnan(got), np.isnan(want))
    assert np.allclose(got[~np.isnan(got)], want[~np.isnan(want)], rtol=0.0, atol=1e-9)


def test_route_gears_are_plausible(route_run: Run, route_reader: DriveReader) -> None:
    # Under the old E90 profile and no bound, this route gave 117 shifts between 13 "gears".
    assert route_reader.meta().vehicle_profile == "comma2k19_rav4"
    assert_plausible_gears(route_run, route_reader)


def test_route_runs_with_no_errors(route_run: Run) -> None:
    rep = route_run.report
    assert rep.n_errors == 0
    assert rep.records_in["CanFrame"] == 807127 + 675000
    assert rep.records_in["GpsFix"] == 4505
    assert rep.records_in["ImuSample"] == 93828
    assert rep.data_span_s == pytest.approx(900.0, abs=0.1)
    with DriveReader(rep.path) as r:
        assert r.meta().closed_cleanly
        assert r.meta().dataset_ref == f"{cli.ROUTE_REF}/9..23"
        assert r.alerts_without_trigger() == []


def raw_standing_start() -> tuple[int, int, float]:
    """(t of the last CAN speed sample under 0.5 m/s, interpolated 60 mph crossing, and the
    dataset's own measure: that sample to the first sample at or over 60 mph)."""
    t, v = Comma2k19Source(ROUTE / "14").speed_trace()
    j60 = int(np.flatnonzero(v >= MPH_60)[0])
    j0 = int(np.flatnonzero(v[:j60] < 0.5)[-1])
    frac = (MPH_60 - v[j60 - 1]) / (v[j60] - v[j60 - 1])
    t60 = int(t[j60 - 1] + round(frac * (t[j60] - t[j60 - 1])))
    return int(t[j0]), t60, (int(t[j60]) - int(t[j0])) / NS_PER_S


def test_route_standing_start_is_found(route_reader: DriveReader) -> None:
    runs = [e for e in route_reader.events() if e.kind == "perf.zero_to_sixty"]
    assert len(runs) == 1
    ev = runs[0]
    raw_start, raw_60, raw_s = raw_standing_start()
    assert raw_s == pytest.approx(ROUTE_STANDING_START_S, abs=0.001)
    assert 0 <= raw_start - ev.t_start_ns <= START_GAP_TOL_S * NS_PER_S
    assert abs(ev.t_end_ns - raw_60) <= END_TOL_S * NS_PER_S
    assert ev.value == pytest.approx(ROUTE_STANDING_START_S, abs=TOTAL_TOL_S)
    assert not ev.synthetic
    stats = {s.stat_id: s for s in route_reader.stats()}
    assert stats["perf.zero_to_sixty_s"].display == f"{ev.value:.2f} s"
