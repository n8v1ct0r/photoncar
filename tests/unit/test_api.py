"""Every HTTP API route, over the stdlib ASGI client, against drive files written in tmp_path.

The test drive is synthetic and exact: 40 s long, speed 0.75 t m/s, rpm 1000 + 50 t, coolant
100 + 0.25 t °C, driving east from a fake origin near lat 0, lon 0. Its 10 Hz timeline is
written row by row, so every value the API derives from it has a known answer.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import importlib.util
import json
import math
import sys
import time
from collections.abc import Coroutine
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI

import photoncar
from photoncar.api.app import PRIVACY_MISSING, create_app
from photoncar.api.guard import lock_applies
from photoncar.api.live import LiveState
from photoncar.clock import SimClock
from photoncar.config import Config, load_config
from photoncar.export.laps_v0 import validate_lap
from photoncar.export.report import drive_report
from photoncar.privacy.geo import EARTH_MEAN_RADIUS_M
from photoncar.records import (
    Citation,
    CitationRole,
    DriveStat,
    Event,
    GpsFix,
    Lap,
    Sample,
    StateEstimate,
)
from photoncar.store.index import DriveIndex
from photoncar.store.meta import DriveMeta
from photoncar.store.reader import DriveReader
from photoncar.store.rollup import TimelineRow
from photoncar.store.writer import DriveWriter
from photoncar.summary.model_client import AnthropicSummaryClient, FakeSummaryClient
from photoncar.units import G0, MPH, c_to_k

if TYPE_CHECKING:
    from tests.asgi import AsgiClient, Response
else:

    def _load_asgi() -> ModuleType:
        # tests/ is not on sys.path while pytest collects this file, so load the client by path.
        path = Path(__file__).resolve().parents[1] / "asgi.py"
        spec = importlib.util.spec_from_file_location("photoncar_tests_asgi", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    _asgi = _load_asgi()
    AsgiClient, Response = _asgi.AsgiClient, _asgi.Response

S = 1_000_000_000
T0 = 5 * S  # the drive starts 5 s after boot
DURATION_S = 40.0
DRIVE_ID = "7b3f6a10-2c4d-4e8f-9a1b-3c5d7e9f0a12"
TOKEN = "test-token-0123456789"
NOON = 1_789_992_000 * S  # 2026-09-21 12:00:00 UTC, as epoch nanoseconds

STATS = [
    DriveStat("perf.zero_to_sixty_s", "0 to 60 mph", 34.76, "s", "34.76 s"),
    DriveStat("perf.peak_lateral_g", "Peak lateral", 0.12, "g", "0.12 g"),
    DriveStat("drive.distance_m", "Distance", 600.0, "m", "600 m"),
    DriveStat("behavior.hard_brakes", "Hard brakes", 1.0, "count", "1"),
]
GOOD = json.dumps(
    {
        "sentences": [
            {"text": "You reached 60 mph in 34.76 s.", "stat_ids": ["perf.zero_to_sixty_s"]},
            {"text": "The drive covered 600 m.", "stat_ids": ["drive.distance_m"]},
            {"text": "You braked hard 1 time.", "stat_ids": ["behavior.hard_brakes"]},
        ]
    }
)
BAD = json.dumps({"sentences": [{"text": "Nice.", "stat_ids": []}]})


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _ns(t_s: float) -> int:
    return T0 + round(t_s * S)


def _cite(
    signal: str,
    source: str,
    role: CitationRole,
    t0: float,
    t1: float,
    stat: str,
    value: float,
    unit: str,
    n: int,
) -> Citation:
    return Citation(signal, source, role, _ns(t0), _ns(t1), stat, value, unit, n)


def _events() -> list[Event]:
    # The trim alert is measured (its trims are not synthetic signals of this drive); the
    # coolant alert is a synthetic fault.
    trim = Event(
        kind="alert.trim_warn", rule_id="trim_warn", rule_version=1, severity=1,
        t_start_ns=_ns(2.0), t_end_ns=_ns(10.0), value=0.125, threshold=0.10, unit="ratio",
        message="Bank 1 fuel trim +12.5%, outside ±10%.",
        citations=(
            _cite("obd.stft_b1", "OBD 0x06", "trigger", 2.0, 6.0, "mean", 0.08, "ratio", 3),
            _cite("obd.ltft_b1", "OBD 0x07", "trigger", 2.0, 6.0, "mean", 0.045, "ratio", 3),
        ),
        resolved=True,
    )  # fmt: skip
    perf = Event(
        kind="perf.zero_to_sixty", rule_id="zero_to_sixty", rule_version=1, severity=0,
        t_start_ns=_ns(1.0), t_end_ns=_ns(35.76), value=34.76, threshold=None, unit="s",
        message="0 to 60 mph in 34.76 s.",
        citations=(
            _cite("state.speed", "Estimator", "trigger", 1.0, 35.76, "time_s", 34.76, "m/s", 1739),
            _cite("state.speed", "Estimator", "context", 1.0, 2.0, "rollout_1ft_s", 34.68,
                  "m/s", 50),
        ),
    )  # fmt: skip
    coolant = Event(
        kind="alert.coolant_high", rule_id="coolant_high", rule_version=1, severity=2,
        t_start_ns=_ns(22.5), t_end_ns=_ns(40.0), value=c_to_k(110.0), threshold=c_to_k(105.0),
        unit="K", message="Coolant 110 °C, over 105 °C.",
        citations=(
            _cite("obd.coolant_temp", "OBD 0x05", "trigger", 22.5, 32.5, "min", c_to_k(105.625),
                  "K", 5),
            _cite("obd.coolant_temp", "OBD 0x05", "context", 22.5, 40.0, "max", c_to_k(110.0),
                  "K", 8),
        ),
        synthetic=True,
    )  # fmt: skip
    brake = Event(
        kind="behavior.hard_brake", rule_id="hard_brake", rule_version=1, severity=0,
        t_start_ns=_ns(35.0), t_end_ns=_ns(36.0), value=0.35, threshold=0.30, unit="g",
        message="Hard brake, 0.35 g.",
        citations=(
            _cite("state.accel_long", "Estimator", "trigger", 35.0, 36.0, "mean", -3.43, "m/s^2",
                  50),
        ),
    )  # fmt: skip
    return [trim, perf, coolant, brake]  # stored IDs 1..4 in this order


def _row(k: int) -> TimelineRow:
    t = k / 10
    return TimelineRow(
        t_ns=T0 + k * 100_000_000, speed_mps=0.75 * t, rpm=1000.0 + 50.0 * t, throttle=0.3,
        coolant_k=c_to_k(100.0 + 0.25 * t), accel_long_mps2=0.75, accel_lat_mps2=0.0,
        x_m=0.375 * t * t, y_m=0.0, dr=False,
    )  # fmt: skip


def write_drive(data_dir: Path, drive_id: str = DRIVE_ID, *, register: bool = True) -> Path:
    path = data_dir / "drives" / f"{drive_id}.sqlite"
    meta = DriveMeta(
        drive_id=drive_id,
        source="synthetic",
        vehicle_profile="e90_m3",
        synthetic_signals=("obd.coolant_temp", "obd.rpm"),
    )
    with DriveWriter(path, meta) as w:
        for k in range(int(DURATION_S * 10) + 1):  # GPS and state at 10 Hz, heading east
            t = k / 10
            lon = math.degrees(0.375 * t * t / EARTH_MEAN_RADIUS_M)
            w.write(GpsFix(_ns(t), _ns(t), None, 0.0, lon, 10.0, 0.75 * t, 0.0, 0.8, 3, 12))
            w.write(StateEstimate(_ns(t), 0.75 * t, 0.75, 0.0, 0.0, 0.0, 0.01, 0.1, "imu+gps"))
            if k % 5 == 0:  # the fast OBD group at 2 Hz
                w.write(Sample(_ns(t), "obd.rpm", 1000.0 + 50.0 * t))
                w.write(Sample(_ns(t), "obd.throttle", 0.3))
            if k % 25 == 0:  # coolant in the slow group, 0.4 Hz
                w.write(Sample(_ns(t), "obd.coolant_temp", c_to_k(100.0 + 0.25 * t)))
        for ev in _events():
            w.write(ev)
        w.write(Lap("sf1", 1, _ns(10.0), _ns(30.0), 20 * S, True))
        w.write_stats(STATS)
        w.write_timeline([_row(k) for k in range(int(DURATION_S * 10) + 1)])
    if register:
        with DriveIndex(data_dir / "index.sqlite") as index, DriveReader(path) as reader:
            index.register(reader.meta(), "Test drive")
            index.update_from_reader(reader)
    return path


def cfg_with(**sections: dict[str, Any]) -> Config:
    raw = copy.deepcopy(dict(load_config().raw))
    for name, values in sections.items():
        raw[name] = {**raw[name], **values}
    return Config(raw)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHOTONCAR_API_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    write_drive(d)
    return d


@pytest.fixture
def web_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>stub</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1);", encoding="utf-8")
    return dist


def make_app(
    data_dir: Path,
    web_dist: Path,
    *,
    cfg: Config | None = None,
    client: Any = None,
    clock: SimClock | None = None,
) -> FastAPI:
    return create_app(
        cfg or load_config(),
        data_dir,
        summary_client=client if client is not None else FakeSummaryClient(),
        clock=clock or SimClock(NOON),
        web_dist=web_dist,
    )


def get(app: FastAPI, path: str, **kw: Any) -> Response:
    return run(AsgiClient(app).get(path, **kw))


def post(app: FastAPI, path: str, **kw: Any) -> Response:
    return run(AsgiClient(app).post(path, **kw))


def live_of(app: FastAPI) -> LiveState:
    state = app.state.live
    assert isinstance(state, LiveState)
    return state


# -- health, drives ------------------------------------------------------------------------


def test_health_reports_version_mode_and_moving(data_dir: Path, web_dist: Path) -> None:
    app = make_app(data_dir, web_dist)
    r = get(app, "/api/health")
    assert r.status == 200
    assert r.headers["content-type"] == "application/json"
    assert r.json() == {"ok": True, "version": photoncar.__version__, "mode": "idle",
                        "moving": False}  # fmt: skip
    live_of(app).set_mode("replay")
    assert get(app, "/api/health").json()["mode"] == "replay"
    with pytest.raises(ValueError):
        live_of(app).set_mode("parked")


def test_drive_list(data_dir: Path, web_dist: Path) -> None:
    body = get(make_app(data_dir, web_dist), "/api/drives").json()
    assert list(body) == ["drives"] and len(body["drives"]) == 1
    item = body["drives"][0]
    assert item.pop("distance_m") == pytest.approx(600.0, rel=1e-12)  # 0.375 * 40^2
    assert item == {
        "drive_id": DRIVE_ID,
        "label": "Test drive",
        "source": "synthetic",
        "duration_s": 40.0,
        "n_events": 4,
        "n_alerts": 1,  # the measured trim alert; the synthetic coolant fault is left out
        "closed_cleanly": True,
    }


def test_empty_data_dir_lists_no_drives(tmp_path: Path, web_dist: Path) -> None:
    r = get(make_app(tmp_path / "empty", web_dist), "/api/drives")
    assert (r.status, r.json()) == (200, {"drives": []})


def test_drive_detail_and_stats(data_dir: Path, web_dist: Path) -> None:
    body = get(make_app(data_dir, web_dist), f"/api/drives/{DRIVE_ID}").json()
    drive = body["drive"]
    assert drive["distance_m"] == pytest.approx(600.0, rel=1e-12)
    assert {k: v for k, v in drive.items() if k != "distance_m"} == {
        "drive_id": DRIVE_ID,
        "label": "Test drive",
        "source": "synthetic",
        "duration_s": 40.0,
        "n_events": 4,
        "n_alerts": 1,  # the measured trim alert; the synthetic coolant fault is left out
        "closed_cleanly": True,
        "vehicle_profile": "e90_m3",
        "dataset_ref": None,
        "synthetic_signals": ["obd.coolant_temp", "obd.rpm"],
    }
    assert body["stats"] == [
        {"stat_id": s.stat_id, "label": s.label, "value": s.value, "unit": s.unit,
         "display": s.display}
        for s in STATS
    ]  # fmt: skip


def test_an_unlisted_drive_file_is_labelled_by_its_id(data_dir: Path, web_dist: Path) -> None:
    other = "0a1b2c3d-0000-4000-8000-000000000001"
    write_drive(data_dir, other, register=False)
    drive = get(make_app(data_dir, web_dist), f"/api/drives/{other}").json()["drive"]
    assert (drive["label"], drive["n_events"], drive["duration_s"]) == (other, 4, 40.0)


# -- timeline, events, report ---------------------------------------------------------------


def test_timeline_known_answers(data_dir: Path, web_dist: Path) -> None:
    app = make_app(data_dir, web_dist)
    body = get(app, f"/api/drives/{DRIVE_ID}/timeline", query={"max_points": 1500}).json()
    assert list(body) == ["t_s", "speed_mps", "rpm", "throttle", "coolant_c", "accel_long_g",
                          "accel_lat_g", "x_m", "y_m", "dr", "sources"]  # fmt: skip
    # The signal behind each column; rpm and coolant are synthetic signals of this drive.
    assert body["sources"] == {
        "speed_mps": "state.speed",
        "rpm": "obd.rpm",
        "throttle": "obd.throttle",
        "coolant_c": "obd.coolant_temp",
        "accel_long_g": "state.accel_long",
        "accel_lat_g": "state.accel_lat",
        "x_m": "gps.fix",
        "y_m": "gps.fix",
    }
    assert len(body["t_s"]) == 401  # 40 s at 10 Hz, under max_points: no decimation
    i = 123
    assert body["t_s"][i] == pytest.approx(12.3, abs=1e-12)
    assert body["speed_mps"][i] == pytest.approx(0.75 * 12.3, abs=1e-12)
    assert body["rpm"][i] == pytest.approx(1615.0, abs=1e-9)
    assert body["throttle"][i] == 0.3
    assert body["coolant_c"][i] == pytest.approx(103.075, abs=1e-9)
    assert body["accel_long_g"][i] == pytest.approx(0.75 / G0, abs=1e-15)
    assert body["x_m"][i] == pytest.approx(0.375 * 12.3**2, abs=1e-9)
    assert set(body["dr"]) == {False}
    with DriveReader(data_dir / "drives" / f"{DRIVE_ID}.sqlite") as reader:
        assert body == json.loads(json.dumps(reader.timeline(1500)))


def test_timeline_decimates_and_validates_max_points(data_dir: Path, web_dist: Path) -> None:
    app = make_app(data_dir, web_dist)
    body = get(app, f"/api/drives/{DRIVE_ID}/timeline", query={"max_points": 100}).json()
    assert len(body["t_s"]) == 100
    assert max(v for v in body["speed_mps"] if v is not None) == pytest.approx(30.0)
    bad = get(app, f"/api/drives/{DRIVE_ID}/timeline", query={"max_points": 1})
    assert (bad.status, bad.json()) == (422, {"error": "The max_points value is not valid."})


def test_events_in_time_order_with_box_numbers_and_distance(data_dir: Path, web_dist: Path) -> None:
    events = get(make_app(data_dir, web_dist), f"/api/drives/{DRIVE_ID}/events").json()["events"]
    assert [(e["box_no"], e["event_id"], e["kind"]) for e in events] == [
        (1, 2, "perf.zero_to_sixty"),
        (2, 1, "alert.trim_warn"),
        (3, 3, "alert.coolant_high"),
        (4, 4, "behavior.hard_brake"),
    ]
    # Distance at each start: the integral of 0.75 t is 0.375 t^2.
    for e, t in zip(events, (1.0, 2.0, 22.5, 35.0), strict=True):
        assert e.pop("distance_m") == pytest.approx(0.375 * t * t, rel=1e-12)
    assert events[2] == {
        "event_id": 3,
        "box_no": 3,
        "kind": "alert.coolant_high",
        "rule_id": "coolant_high",
        "severity": 2,
        "t_start_s": 22.5,
        "t_end_s": 40.0,
        "value": c_to_k(110.0),
        "threshold": c_to_k(105.0),
        "unit": "K",
        "message": "Coolant 110 °C, over 105 °C.",
        "resolved": False,
        "synthetic": True,
        "citations": [
            {"signal": "obd.coolant_temp", "source": "OBD 0x05", "role": "trigger",
             "t_start_s": 22.5, "t_end_s": 32.5, "stat": "min", "stat_value": c_to_k(105.625),
             "unit": "K", "n": 5},
            {"signal": "obd.coolant_temp", "source": "OBD 0x05", "role": "context",
             "t_start_s": 22.5, "t_end_s": 40.0, "stat": "max", "stat_value": c_to_k(110.0),
             "unit": "K", "n": 8},
        ],
    }  # fmt: skip
    assert events[0]["threshold"] is None and events[0]["severity"] == 0


def test_box_numbers_count_roadbook_events_only(data_dir: Path, web_dist: Path) -> None:
    """Shifts stay in the events list for the report, but only roadbook events (alerts,
    performance runs, hard brakes, hard accelerations, corners, laps) get a box number."""
    drive_id = "5d2c8e1a-7b3f-4a6e-8c1d-9e0f2a4b6c8d"

    def ev(kind: str, rule_id: str, t_s: float) -> Event:
        return Event(
            kind=kind, rule_id=rule_id, rule_version=1, severity=0,
            t_start_ns=_ns(t_s), t_end_ns=_ns(t_s + 0.5), value=1.0, threshold=None, unit="g",
            message=f"{rule_id} at {t_s:g} s.",
            citations=(_cite("state.speed", "Estimator", "trigger", t_s, t_s + 0.5, "max", 1.0,
                             "m/s", 5),),
        )  # fmt: skip

    kinds = [
        ("behavior.shift", "shift", 1.0),
        ("perf.zero_to_sixty", "zero_to_sixty", 2.0),
        ("behavior.shift", "shift", 3.0),
        ("behavior.shift", "shift", 4.0),
        ("behavior.hard_accel", "hard_accel", 5.0),
        ("behavior.corner", "corner", 6.0),
        ("behavior.shift", "shift", 7.0),
        ("alert.coolant_high", "coolant_high", 8.0),
        ("behavior.hard_brake", "hard_brake", 9.0),
        ("perf.peak_lateral", "peak_lateral", 10.0),
    ]
    path = data_dir / "drives" / f"{drive_id}.sqlite"
    with DriveWriter(path, DriveMeta(drive_id=drive_id, source="replay")) as w:
        w.write(Sample(_ns(0.0), "obd.rpm", 1000.0))
        for kind, rule_id, t_s in kinds:
            w.write(ev(kind, rule_id, t_s))
        w.write_timeline([_row(k) for k in range(int(DURATION_S * 10) + 1)])
    events = get(make_app(data_dir, web_dist), f"/api/drives/{drive_id}/events").json()["events"]
    assert [(e["kind"], e["box_no"]) for e in events] == [
        ("behavior.shift", None),
        ("perf.zero_to_sixty", 1),
        ("behavior.shift", None),
        ("behavior.shift", None),
        ("behavior.hard_accel", 2),
        ("behavior.corner", 3),
        ("behavior.shift", None),
        ("alert.coolant_high", 4),
        ("behavior.hard_brake", 5),
        ("perf.peak_lateral", 6),
    ]


def test_report_is_the_stored_drive_report(data_dir: Path, web_dist: Path) -> None:
    body = get(make_app(data_dir, web_dist), f"/api/drives/{DRIVE_ID}/report").json()
    assert list(body) == ["behavior", "performance", "laps"]
    with DriveReader(data_dir / "drives" / f"{DRIVE_ID}.sqlite") as reader:
        assert body == json.loads(json.dumps(drive_report(reader)))
    assert body["behavior"]["hard_brakes"] == 1
    assert body["performance"]["zero_to_sixty"] == [
        {"t_start_s": 1.0, "seconds": 34.76, "rollout_seconds": 34.68}
    ]
    assert body["laps"] == [
        {"lap_no": 1, "line_id": "sf1", "t_start_s": 10.0, "duration_s": 20.0, "valid": True}
    ]


# -- live frames ---------------------------------------------------------------------------


def frame(app: FastAPI, t_s: float) -> dict[str, Any]:
    r = get(app, f"/api/drives/{DRIVE_ID}/live_frame", query={"t_s": t_s})
    assert r.status == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def test_live_frame_values_known_answers(data_dir: Path, web_dist: Path) -> None:
    app = make_app(data_dir, web_dist)
    f = frame(app, 12.3)
    assert list(f) == ["t_s", "speed_mph", "rpm", "coolant_c", "synthetic_fields", "alert"]
    assert f["synthetic_fields"] == ["rpm", "coolant_c"]  # fed by this drive's synthetic signals
    assert f["t_s"] == 12.3
    assert f["speed_mph"] == pytest.approx(0.75 * 12.3 / MPH, abs=1e-9)
    assert f["rpm"] == pytest.approx(1615.0, abs=1e-9)
    assert f["coolant_c"] == pytest.approx(103.075, abs=1e-9)
    assert f["alert"] is None
    between = frame(app, 12.35)  # between 2 rows: linear interpolation
    assert between["speed_mph"] == pytest.approx(0.75 * 12.35 / MPH, abs=1e-9)
    assert between["rpm"] == pytest.approx(1617.5, abs=1e-9)
    assert frame(app, 12.3) == f  # deterministic


def test_live_frame_alerts_follow_the_on_delay_and_severity(data_dir: Path, web_dist: Path) -> None:
    app = make_app(data_dir, web_dist)
    assert frame(app, 5.0)["alert"] is None  # trim onset at 2 s, fires at 6 s
    assert frame(app, 7.0)["alert"] == {
        "severity": 1,
        "headline": "Bank 1 fuel trim +12.5%",
        "citation": "OBD 0x06 and OBD 0x07 outside ±10% for 4 s",
        "synthetic": False,
    }
    assert frame(app, 10.0)["alert"] is None  # resolved at 10 s
    assert frame(app, 30.0)["alert"] is None  # coolant onset at 22.5 s, fires at 32.5 s
    # The coolant fault is synthetic: the flag and the headline both say so.
    coolant = {"severity": 2, "headline": "Coolant 110 °C, synthetic fault",
               "citation": "OBD 0x05 over 105 °C for 10 s", "synthetic": True}  # fmt: skip
    assert frame(app, 35.0)["alert"] == coolant
    assert frame(app, 40.0)["alert"] == coolant  # still on when the drive ended


def test_live_frame_shows_stale_and_baseline_alerts_only_once_known(
    data_dir: Path, web_dist: Path
) -> None:
    """A stale alert's trigger window and a baseline alert's run of windows span the whole
    alert, but neither is known at its start: stale once the gap passes the limit (the
    event's threshold), baseline once the departure has lasted ``baseline.sustain_s``."""
    drive_id = "0c6e2b8a-5f1d-4c3e-9b7a-2d4f6a8c0e11"
    stale = Event(
        kind="alert.stale", rule_id="stale", rule_version=1, severity=1,
        t_start_ns=_ns(5.0), t_end_ns=_ns(25.0), value=20.0, threshold=7.5, unit="s",
        message="Module voltage stale, no update for 20 s.",
        citations=(
            _cite("obd.module_voltage", "OBD 0x42", "trigger", 5.0, 25.0, "gap_s", 20.0, "s", 2),
        ),
        resolved=True,
    )  # fmt: skip
    baseline = Event(
        kind="alert.baseline", rule_id="baseline", rule_version=1, severity=1,
        t_start_ns=_ns(26.0), t_end_ns=_ns(40.0), value=4.2, threshold=4.0, unit="z",
        message="Coolant 95 °C, 4.2 sigma over its baseline of 90 °C.",
        citations=(
            _cite("obd.coolant_temp", "OBD 0x05", "trigger", 26.0, 40.0, "median", c_to_k(95.0),
                  "K", 28),
            _cite("obd.coolant_temp", "OBD 0x05", "context", 26.0, 40.0, "median 90 °C",
                  c_to_k(90.0), "K", 180),
        ),
    )  # fmt: skip
    path = data_dir / "drives" / f"{drive_id}.sqlite"
    with DriveWriter(path, DriveMeta(drive_id=drive_id, source="replay")) as w:
        w.write(Sample(_ns(0.0), "obd.rpm", 1000.0))  # the drive starts at 0 s
        w.write(stale)
        w.write(baseline)
        w.write_timeline([_row(k) for k in range(int(DURATION_S * 10) + 1)])
    app = make_app(data_dir, web_dist, cfg=cfg_with(baseline={"sustain_s": 5.0}))

    def alert(t_s: float) -> Any:
        r = get(app, f"/api/drives/{drive_id}/live_frame", query={"t_s": t_s})
        assert r.status == 200, r.text
        return r.json()["alert"]

    assert alert(5.0) is None and alert(12.5) is None  # 7.5 s without an update is not stale
    for t_s, gap in ((12.6, "8"), (20.0, "15"), (24.9, "20")):
        assert alert(t_s) == {
            "severity": 1,
            "headline": "Module voltage stale",
            "citation": f"OBD 0x42 no update for {gap} s",  # the gap so far, not the 20 s
            "synthetic": False,
        }
    assert alert(25.0) is None  # the update came
    assert alert(30.9) is None  # the departure started at 26 s and needs 5 s
    for t_s, held in ((31.0, "5"), (35.0, "9"), (40.0, "14")):
        assert alert(t_s) == {
            "severity": 1,
            "headline": "Coolant 95 °C",
            "citation": f"OBD 0x05 4.2 sigma over its baseline of 90 °C for {held} s",
            "synthetic": False,
        }


@pytest.mark.parametrize("t_s", ["-0.1", "40.1", "nan", "inf", "soon"])
def test_live_frame_rejects_times_outside_the_drive(
    data_dir: Path, web_dist: Path, t_s: str
) -> None:
    r = get(make_app(data_dir, web_dist), f"/api/drives/{DRIVE_ID}/live_frame",
            query={"t_s": t_s})  # fmt: skip
    assert r.status == 422
    assert r.json()["error"] in ("That time is outside this drive.", "The t_s value is not valid.")


def test_live_frame_needs_a_time(data_dir: Path, web_dist: Path) -> None:
    r = get(make_app(data_dir, web_dist), f"/api/drives/{DRIVE_ID}/live_frame")
    assert (r.status, r.json()) == (422, {"error": "The t_s value is missing."})


# -- the live stream -----------------------------------------------------------------------


async def _read_events(app: FastAPI, query: dict[str, Any], limit: int = 10_000) -> list[Any]:
    out: list[Any] = []
    async with AsgiClient(app).stream("GET", "/api/live/stream", query=query) as resp:
        assert resp.status == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for ev in resp.events():
            out.append((ev.event, ev.json()))
            if len(out) >= limit:
                break
    return out


def test_replay_stream_sends_every_frame_of_the_drive(data_dir: Path, web_dist: Path) -> None:
    app = make_app(data_dir, web_dist, cfg=cfg_with(live={"stream_hz": 200.0}))
    events = run(_read_events(app, {"replay": DRIVE_ID, "speed": 100}))
    assert events[0] == ("state", {"moving": False})
    frames = [data for name, data in events[1:]]
    assert {name for name, _ in events[1:]} == {"frame"}
    # speed 100 at 200 Hz: each frame advances 0.5 s of drive time, 0 to 40 s inclusive.
    assert [f["t_s"] for f in frames] == [k * 0.5 for k in range(81)]
    assert frames[25] == frame(app, 12.5)
    assert frames[70]["alert"]["headline"] == "Coolant 110 °C, synthetic fault"  # t = 35 s


def test_replay_stream_paces_at_the_configured_10_hz(data_dir: Path, web_dist: Path) -> None:
    app = make_app(data_dir, web_dist)  # live.stream_hz = 10 in config

    async def timed() -> list[float]:
        stamps: list[float] = []
        async with AsgiClient(app).stream(
            "GET", "/api/live/stream", query={"replay": DRIVE_ID, "speed": 1}
        ) as resp:
            async for ev in resp.events():
                if ev.event == "frame":
                    stamps.append(time.monotonic())
                    if len(stamps) == 4:
                        break
        return stamps

    stamps = run(timed())
    # 3 intervals at 10 Hz are 0.3 s; allow scheduler slack on a loaded machine.
    assert 0.25 <= stamps[-1] - stamps[0] <= 0.9


def test_replay_stream_ends_when_the_car_moves(data_dir: Path, web_dist: Path) -> None:
    """A replay is a parked view: once the car moves, it says so and the stream ends."""
    app = make_app(data_dir, web_dist, cfg=cfg_with(live={"stream_hz": 200.0}))

    async def scenario() -> list[tuple[str | None, Any]]:
        seen: list[tuple[str | None, Any]] = []
        async with AsgiClient(app).stream(
            "GET", "/api/live/stream", query={"replay": DRIVE_ID, "speed": 1}
        ) as resp:
            async for ev in resp.events():
                seen.append((ev.event, ev.json()))
                if len(seen) == 4:
                    live_of(app).set_moving(True)
        return seen

    seen = run(scenario())  # the loop ends because the stream does, 40 s of drive early
    assert seen[0] == ("state", {"moving": False})
    assert all(name == "frame" for name, _ in seen[1:-1])
    assert 4 <= len(seen) <= 6
    assert seen[-1] == ("state", {"moving": True})


def test_live_stream_forwards_published_frames(data_dir: Path, web_dist: Path) -> None:
    app = make_app(data_dir, web_dist, cfg=cfg_with(live={"stream_hz": 100.0}))
    published = {"t_s": 3.2, "speed_mph": 41.5, "rpm": 3200.0, "coolant_c": 91.0, "alert": None}

    async def scenario() -> list[tuple[str | None, Any]]:
        seen: list[tuple[str | None, Any]] = []
        async with AsgiClient(app).stream("GET", "/api/live/stream") as resp:
            async for ev in resp.events():
                seen.append((ev.event, ev.json()))
                if len(seen) == 1:
                    live_of(app).publish(published)
                elif len(seen) == 2:
                    live_of(app).set_moving(True)
                else:
                    break
        return seen

    assert run(scenario()) == [
        ("state", {"moving": False}),
        ("frame", published),
        ("state", {"moving": True}),
    ]


def test_stream_of_an_unknown_drive_is_a_json_404(data_dir: Path, web_dist: Path) -> None:
    r = get(make_app(data_dir, web_dist), "/api/live/stream", query={"replay": "nope"})
    assert (r.status, r.json()) == (404, {"error": "No drive with that ID."})
    r = get(make_app(data_dir, web_dist), "/api/live/stream", query={"speed": 0})
    assert (r.status, r.json()) == (422, {"error": "The speed value is not valid."})


# -- the moving lock -----------------------------------------------------------------------


def test_moving_locks_every_parked_view(data_dir: Path, web_dist: Path) -> None:
    """While moving, only health and the current drive's live stream answer. A replay stream
    of a stored drive is a parked view, however its query is written."""
    app = make_app(data_dir, web_dist, cfg=cfg_with(live={"stream_hz": 200.0}))
    live_of(app).set_moving(True)
    locked = {"error": "Parked views open when the car stops."}
    for path, query in (
        ("/api/drives", None),
        (f"/api/drives/{DRIVE_ID}", None),
        (f"/api/drives/{DRIVE_ID}/timeline", None),
        (f"/api/drives/{DRIVE_ID}/events", None),
        (f"/api/drives/{DRIVE_ID}/report", None),
        (f"/api/drives/{DRIVE_ID}/live_frame", {"t_s": 1}),
        ("/api/legend", None),
        ("/api/live/stream", {"replay": DRIVE_ID, "speed": 100}),
        ("/api/live/stream", {"speed": 1, "replay": ""}),
        ("/api/live/other", None),
    ):
        r = get(app, path, query=query)
        assert (r.status, r.json()) == (423, locked), (path, query)
    for path in (f"/api/drives/{DRIVE_ID}/summary", f"/api/drives/{DRIVE_ID}/export"):
        r = post(app, path, query={"kind": "laps"})
        assert (r.status, r.json()) == (423, locked), path
    health = get(app, "/api/health")
    assert (health.status, health.json()["moving"]) == (200, True)
    live_of(app).publish({"t_s": 1.0, "speed_mph": 30.0, "rpm": None, "coolant_c": None,
                          "synthetic_fields": [], "alert": None})  # fmt: skip
    events = run(_read_events(app, {}, limit=2))  # the current drive's live stream
    assert [name for name, _ in events] == ["state", "frame"]
    assert events[0] == ("state", {"moving": True})
    assert get(app, "/").status == 200  # the web app itself loads, to show the live view
    live_of(app).set_moving(False)
    assert get(app, "/api/drives").status == 200
    assert get(app, "/api/legend").status == 200


def test_the_lock_scope_reads_the_replay_parameter() -> None:
    assert not lock_applies("/api/health")
    assert not lock_applies("/api/live/stream")
    assert not lock_applies("/api/live/stream", b"speed=1")
    assert lock_applies("/api/live/stream", b"replay=abc&speed=1")
    assert lock_applies("/api/live/stream", b"speed=1&replay=")
    assert lock_applies("/api/live/stream", b"replay")
    assert lock_applies("/api/live/other")
    assert lock_applies("/api/legend")


def test_a_replay_stream_opened_as_the_car_moves_ends_at_once(
    data_dir: Path, web_dist: Path
) -> None:
    """The lock check passed, then the car moved before the stream started: no frame goes out."""
    app = make_app(data_dir, web_dist, cfg=cfg_with(live={"stream_hz": 200.0}))
    # The guard keeps the app's own live state (parked); the stream reads a moving one.
    app.state.ctx = dataclasses.replace(app.state.ctx, live=LiveState(moving=True))
    events = run(_read_events(app, {"replay": DRIVE_ID, "speed": 100}))
    assert events == [("state", {"moving": True})]


# -- the legend ----------------------------------------------------------------------------


def test_legend_is_built_from_config(data_dir: Path, web_dist: Path) -> None:
    cfg = cfg_with(privacy={"radius_km": 0.8}, obd={"max_request_hz": 8.0})
    raw = copy.deepcopy(dict(cfg.raw))
    raw["rules"]["coolant_high"]["threshold_c"] = 110.0
    body = get(make_app(data_dir, web_dist, cfg=Config(raw)), "/api/legend").json()
    assert set(body) == {"rules", "privacy_radius_km", "max_request_hz", "synthetic_note"}
    assert (body["privacy_radius_km"], body["max_request_hz"]) == (0.8, 8.0)
    rules = {r["rule_id"]: r for r in body["rules"]}
    assert list(rules) == [
        "coolant_high", "trim_warn", "trim_critical", "voltage_low_running",
        "voltage_low_rested", "stale", "baseline",
    ]  # fmt: skip
    assert rules["coolant_high"] == {
        "rule_id": "coolant_high",
        "title": "Coolant high",
        "condition": "Coolant over 110 °C for 10 s. It clears after 30 s under 102 °C.",
        "severity": 2,
        "advisory": False,
    }
    assert rules["voltage_low_rested"]["advisory"] is True
    assert "12.29 V (AGM battery)" in rules["voltage_low_rested"]["condition"]
    assert "24 h" in rules["voltage_low_rested"]["condition"]
    assert [r["severity"] for r in body["rules"]] == [2, 1, 2, 2, 1, 1, 1]
    text = " ".join([body["synthetic_note"], *(r["title"] + r["condition"] for r in body["rules"])])
    assert not any(d in text for d in ("\u2014", "\u2013"))


# -- auth ----------------------------------------------------------------------------------


def test_token_is_required_when_set(
    data_dir: Path, web_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PHOTONCAR_API_TOKEN", TOKEN)
    app = make_app(data_dir, web_dist)
    missing = get(app, "/api/health")
    assert (missing.status, missing.json()) == (401, {"error": "A valid device token is needed."})
    assert missing.headers["www-authenticate"] == "Bearer"
    wrong = get(app, "/api/health", headers={"Authorization": "Bearer nope"})
    assert wrong.status == 401
    basic = get(app, "/api/health", headers={"Authorization": f"Basic {TOKEN}"})
    assert basic.status == 401
    assert get(app, "/api/nope").status == 401  # unknown API paths are guarded too
    ok = run(
        AsgiClient(app, client=("192.168.4.20", 5555)).get(
            "/api/health", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    )
    assert ok.status == 200
    assert get(app, "/").status == 200  # the web app loads without the token
    stream = get(app, "/api/live/stream", query={"replay": "nope"})
    assert stream.status == 401


@pytest.mark.parametrize(
    ("host", "status"),
    [("127.0.0.1", 200), ("::1", 200), ("::ffff:127.0.0.1", 200), ("192.168.4.20", 403),
     ("10.0.0.7", 403), ("not-an-ip", 403)],
)  # fmt: skip
def test_without_a_token_only_loopback_is_answered(
    data_dir: Path, web_dist: Path, host: str, status: int
) -> None:
    app = make_app(data_dir, web_dist)
    r = run(AsgiClient(app, client=(host, 5555)).get("/api/drives"))
    assert r.status == status
    if status == 403:
        assert r.json() == {"error": "This device answers only itself until a device token is set."}
    web = run(AsgiClient(app, client=(host, 5555)).get("/"))
    assert web.status == 200


# -- errors --------------------------------------------------------------------------------


def test_errors_are_plain_sentences(data_dir: Path, web_dist: Path) -> None:
    app = make_app(data_dir, web_dist)
    cases = [
        ("GET", "/api/nope", 404, "Not found."),
        ("GET", "/api/drives/missing-drive", 404, "No drive with that ID."),
        ("GET", "/api/drives/..", 404, "No drive with that ID."),
        ("GET", "/api/drives/a.b", 404, "No drive with that ID."),
        ("PUT", "/api/health", 405, "That method is not allowed here."),
    ]
    for method, path, status, message in cases:
        r = run(AsgiClient(app).request(method, path))
        assert (r.status, r.json()) == (status, {"error": message}), path


# -- summary -------------------------------------------------------------------------------


def test_summary_from_the_fake_model_then_from_the_cache(data_dir: Path, web_dist: Path) -> None:
    fake = FakeSummaryClient([GOOD])
    app = make_app(data_dir, web_dist, client=fake)
    first = post(app, f"/api/drives/{DRIVE_ID}/summary")
    assert first.status == 200
    assert first.json() == {
        "sentences": json.loads(GOOD)["sentences"],
        "source": "model",
        "cached": False,
        "model": "fake-summary-model",
    }
    second = post(app, f"/api/drives/{DRIVE_ID}/summary").json()
    assert second["cached"] is True and second["sentences"] == first.json()["sentences"]
    assert len(fake.calls) == 1


def test_summary_falls_back_to_the_template(data_dir: Path, web_dist: Path) -> None:
    fake = FakeSummaryClient([BAD, BAD])
    body = post(make_app(data_dir, web_dist, client=fake), f"/api/drives/{DRIVE_ID}/summary").json()
    assert body == {
        "sentences": [
            {"text": "0 to 60 mph: 34.76 s.", "stat_ids": ["perf.zero_to_sixty_s"]},
            {"text": "Peak lateral: 0.12 g.", "stat_ids": ["perf.peak_lateral_g"]},
            {"text": "Distance: 600 m.", "stat_ids": ["drive.distance_m"]},
            {"text": "Hard brakes: 1.", "stat_ids": ["behavior.hard_brakes"]},
        ],
        "source": "template",
        "cached": False,
        "model": None,
    }
    assert len(fake.calls) == 2


def test_default_client_is_anthropic_and_needs_connection_without_a_key(
    data_dir: Path, web_dist: Path
) -> None:
    app = create_app(load_config(), data_dir, web_dist=web_dist, clock=SimClock(NOON))
    assert isinstance(app.state.summary.client, AnthropicSummaryClient)
    r = post(app, f"/api/drives/{DRIVE_ID}/summary")
    assert (r.status, r.json()) == (503, {"error": "Needs connection."})
    assert app.state.summary.ledger.spent() == 0


def test_summary_rate_limit_is_429_with_retry_after(data_dir: Path, web_dist: Path) -> None:
    clock = SimClock(NOON)
    app = make_app(data_dir, web_dist, client=FakeSummaryClient([BAD] * 20), clock=clock)
    for _ in range(5):
        assert post(app, f"/api/drives/{DRIVE_ID}/summary").status == 200
    r = post(app, f"/api/drives/{DRIVE_ID}/summary")
    assert (r.status, r.json()) == (429, {"error": "Too many summary requests. Try again soon."})
    assert r.headers["retry-after"] == "60"


def test_summary_spend_cap_is_429_until_utc_midnight(data_dir: Path, web_dist: Path) -> None:
    fake = FakeSummaryClient([GOOD])
    cfg = cfg_with(summary={"daily_cap_usd": 0.001})
    r = post(make_app(data_dir, web_dist, cfg=cfg, client=fake), f"/api/drives/{DRIVE_ID}/summary")
    assert r.status == 429
    assert r.json() == {"error": "The daily summary budget is spent. Try again tomorrow."}
    assert r.headers["retry-after"] == str(12 * 3600)
    assert fake.calls == []


def test_summary_of_a_missing_drive_is_404(data_dir: Path, web_dist: Path) -> None:
    r = post(make_app(data_dir, web_dist), "/api/drives/nope/summary")
    assert (r.status, r.json()) == (404, {"error": "No drive with that ID."})


# -- export --------------------------------------------------------------------------------


def test_export_refuses_without_the_privacy_file(
    data_dir: Path, web_dist: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(data_dir, web_dist)  # loads config/photoncar.toml before the patch below
    monkeypatch.setattr("photoncar.config.CONFIG_DIR", tmp_path / "no-config")
    r = post(app, f"/api/drives/{DRIVE_ID}/export",
             query={"kind": "laps"})  # fmt: skip
    assert (r.status, r.json()) == (409, {"error": PRIVACY_MISSING})
    assert not (data_dir / "exports").exists()


def test_export_writes_valid_laps_under_data_exports(
    data_dir: Path, web_dist: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    # A synthetic home far from the drive, so nothing is trimmed.
    (config_dir / "privacy.local.toml").write_text("[home]\nlat_deg = 45.0\nlon_deg = 45.0\n")
    app = make_app(data_dir, web_dist)  # loads config/photoncar.toml before the patch below
    monkeypatch.setattr("photoncar.config.CONFIG_DIR", config_dir)
    r = post(app, f"/api/drives/{DRIVE_ID}/export",
             query={"kind": "laps"})  # fmt: skip
    assert r.status == 200
    files = r.json()["files"]
    assert len(files) == 1
    assert files[0].startswith("exports/lap-") and files[0].endswith("-001.json")
    doc = json.loads((data_dir / files[0]).read_text())
    validate_lap(doc)
    assert (doc["lap_no"], doc["duration_ms"], len(doc["samples"]["t_ms"])) == (1, 20_000, 201)


def test_export_kind_must_be_laps(data_dir: Path, web_dist: Path) -> None:
    app = make_app(data_dir, web_dist)
    r = post(app, f"/api/drives/{DRIVE_ID}/export", query={"kind": "gpx"})
    assert (r.status, r.json()) == (422, {"error": "Only lap exports exist. Use kind=laps."})
    r = post(app, f"/api/drives/{DRIVE_ID}/export")
    assert (r.status, r.json()) == (422, {"error": "The kind value is missing."})


# -- the web app ---------------------------------------------------------------------------


def test_web_app_files_and_spa_fallback(data_dir: Path, web_dist: Path) -> None:
    app = make_app(data_dir, web_dist)
    index = "<!doctype html><title>stub</title>"
    root = get(app, "/")
    assert (root.status, root.text) == (200, index)
    assert root.headers["content-type"].startswith("text/html")
    deep = get(app, f"/drives/{DRIVE_ID}", query={"box": 2})
    assert (deep.status, deep.text) == (200, index)
    js = get(app, "/assets/app.js")
    assert (js.status, js.text) == (200, "console.log(1);")
    assert "javascript" in js.headers["content-type"]
    escape = get(app, "/../pyproject.toml")
    assert (escape.status, escape.text) == (200, index)  # never a file outside dist
    head = run(AsgiClient(app).request("HEAD", "/"))
    assert (head.status, head.body) == (200, b"")
    api = get(app, "/api/unknown/path")
    assert (api.status, api.json()) == (404, {"error": "Not found."})


def test_web_app_not_built(data_dir: Path, tmp_path: Path) -> None:
    r = get(make_app(data_dir, tmp_path / "missing-dist"), "/")
    assert (r.status, r.json()) == (404, {"error": "The web app is not built yet. Run make build."})
