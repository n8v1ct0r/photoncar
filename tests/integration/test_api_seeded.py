"""seed-demo into a temporary folder, then every HTTP route over the stdlib ASGI client.

Needs the public dataset sample (``make fetch-dataset``). With the default route fetched too
(``make fetch-route``), the demo holds 3 drives and the route carries the scripted coolant
fault; without it, 2 drives.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI

import photoncar.config
from photoncar import cli
from photoncar.api.app import create_app
from photoncar.config import CONFIG_DIR, load_config
from photoncar.export.laps_v0 import validate_lap
from photoncar.pipeline.runner import SYNTHETIC_FAULT
from photoncar.sources.comma2k19 import default_dataset_root
from photoncar.summary.model_client import FakeSummaryClient

if TYPE_CHECKING:
    from tests.asgi import AsgiClient, Response
else:

    def _load_asgi() -> ModuleType:
        path = Path(__file__).resolve().parents[1] / "asgi.py"
        spec = importlib.util.spec_from_file_location("photoncar_tests_asgi_seeded", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    _asgi = _load_asgi()
    AsgiClient, Response = _asgi.AsgiClient, _asgi.Response

pytestmark = pytest.mark.dataset

ROOT = default_dataset_root()
HAS_ROUTE = (ROOT / cli.ROUTE_REF / "14").is_dir()


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@dataclass(frozen=True)
class Demo:
    data_dir: Path
    ids: dict[str, str]
    """Label to drive ID."""


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Demo:
    if not (ROOT / cli.SAMPLE_REF).is_dir():
        pytest.fail("The comma2k19 sample is missing. Run make fetch-dataset.")
    d = tmp_path_factory.mktemp("demo")
    assert cli.main(["seed-demo", "--data-dir", str(d)]) == 0
    ids: dict[str, str] = {}
    app = make_app(d, tmp_path_factory.mktemp("web"))
    body = run(AsgiClient(app).get("/api/drives")).json()
    for item in body["drives"]:
        ids[item["label"]] = item["drive_id"]
    return Demo(d, ids)


def make_app(data_dir: Path, web: Path) -> FastAPI:
    (web / "index.html").write_text("<!doctype html><title>Photoncar</title>", encoding="utf-8")
    return create_app(load_config(), data_dir, summary_client=FakeSummaryClient(), web_dist=web)


@pytest.fixture
def client(demo: Demo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsgiClient:
    monkeypatch.delenv("PHOTONCAR_API_TOKEN", raising=False)
    return AsgiClient(make_app(demo.data_dir, tmp_path))


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, d, ignore=shutil.ignore_patterns("privacy.local.toml"))
    monkeypatch.setattr(photoncar.config, "CONFIG_DIR", d)
    return d


def get(client: AsgiClient, path: str, **query: str | int | float) -> Response:
    return run(client.get(path, query=query or None))


def test_drive_list(client: AsgiClient) -> None:
    resp = get(client, "/api/drives")
    assert resp.status == 200
    drives = resp.json()["drives"]
    labels = [d["label"] for d in drives]
    expected = [cli.LABEL_SYNTHETIC["track_day"], cli.LABEL_SAMPLE]
    if HAS_ROUTE:
        expected.insert(1, cli.LABEL_ROUTE)
    assert labels == expected
    assert {d["source"] for d in drives} == {"synthetic", "replay"}
    assert all(d["closed_cleanly"] for d in drives)
    assert "live" not in {d["source"] for d in drives}


def test_health(client: AsgiClient) -> None:
    body = get(client, "/api/health").json()
    assert body["ok"] is True and body["moving"] is False and body["mode"] == "idle"


def test_every_drive_route(client: AsgiClient, demo: Demo) -> None:
    for label, drive_id in demo.ids.items():
        detail = get(client, f"/api/drives/{drive_id}")
        assert detail.status == 200, label
        body = detail.json()
        assert body["drive"]["label"] == label
        assert body["drive"]["synthetic_signals"]
        stat_ids = {s["stat_id"] for s in body["stats"]}
        assert {"drive.distance_mi", "drive.duration_s", "alerts.count"} <= stat_ids

        timeline = get(client, f"/api/drives/{drive_id}/timeline", max_points=500).json()
        assert 0 < len(timeline["t_s"]) <= 500
        assert set(timeline) >= {"speed_mps", "rpm", "coolant_c", "x_m", "y_m", "dr"}

        events = get(client, f"/api/drives/{drive_id}/events").json()["events"]
        boxed = [e["box_no"] for e in events if e["box_no"] is not None]
        assert boxed == list(range(1, len(boxed) + 1))  # roadbook events, numbered in order
        assert all((e["box_no"] is None) == (e["kind"] == "behavior.shift") for e in events)
        for e in events:
            if e["kind"].startswith("alert."):
                assert any(c["role"] == "trigger" for c in e["citations"])

        report = get(client, f"/api/drives/{drive_id}/report").json()
        assert set(report) == {"behavior", "performance", "laps"}

        frame = get(client, f"/api/drives/{drive_id}/live_frame", t_s=30.0).json()
        assert list(frame) == [
            "t_s", "speed_mph", "rpm", "coolant_c", "synthetic_fields", "alert"
        ]  # fmt: skip

        summary = run(client.post(f"/api/drives/{drive_id}/summary"))
        assert summary.status == 200, summary.text
        assert 3 <= len(summary.json()["sentences"]) <= 5


def test_sources_and_synthetic_fields_label_the_overlay(client: AsgiClient, demo: Demo) -> None:
    """On a public-dataset replay the measured CAN rpm feeds the timeline and the live frame;
    coolant comes only from the synthetic OBD overlay, so it is labeled synthetic. On the
    synthetic track day every column is synthetic."""
    replay = demo.ids[cli.LABEL_SAMPLE]
    detail = get(client, f"/api/drives/{replay}").json()["drive"]
    synthetic = set(detail["synthetic_signals"])
    sources = get(client, f"/api/drives/{replay}/timeline", max_points=500).json()["sources"]
    assert sources == {
        "speed_mps": "state.speed",
        "rpm": "can.engine_rpm",
        "throttle": "obd.throttle",
        "coolant_c": "obd.coolant_temp",
        "accel_long_g": "state.accel_long",
        "accel_lat_g": "state.accel_lat",
        "x_m": "gps.fix",
        "y_m": "gps.fix",
    }
    assert {c for c, s in sources.items() if s in synthetic} == {"throttle", "coolant_c"}
    frame = get(client, f"/api/drives/{replay}/live_frame", t_s=30.0).json()
    assert frame["synthetic_fields"] == ["coolant_c"]

    track = demo.ids[cli.LABEL_SYNTHETIC["track_day"]]
    synthetic = set(get(client, f"/api/drives/{track}").json()["drive"]["synthetic_signals"])
    sources = get(client, f"/api/drives/{track}/timeline", max_points=500).json()["sources"]
    assert all(s in synthetic for s in sources.values())
    frame = get(client, f"/api/drives/{track}/live_frame", t_s=30.0).json()
    assert frame["synthetic_fields"] == ["speed_mph", "rpm", "coolant_c"]


def test_track_day_report(client: AsgiClient, demo: Demo) -> None:
    drive_id = demo.ids[cli.LABEL_SYNTHETIC["track_day"]]
    report = get(client, f"/api/drives/{drive_id}/report").json()
    assert len(report["laps"]) == 3
    assert report["performance"]["zero_to_sixty"][0]["seconds"] == pytest.approx(5.47, abs=0.02)
    events = get(client, f"/api/drives/{drive_id}/events").json()["events"]
    alerts = [e for e in events if e["kind"].startswith("alert.")]
    assert [a["kind"] for a in alerts] == ["alert.coolant_high"]
    assert alerts[0]["synthetic"] is True


@pytest.mark.skipif(not HAS_ROUTE, reason="Optional route not fetched: make fetch-route")
def test_route_carries_the_scripted_synthetic_fault(client: AsgiClient, demo: Demo) -> None:
    drive_id = demo.ids[cli.LABEL_ROUTE]
    events = get(client, f"/api/drives/{drive_id}/events").json()["events"]
    alerts = [e for e in events if e["kind"].startswith("alert.")]
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["kind"] == "alert.coolant_high"
    assert alert["synthetic"] is True
    assert alert["message"] == f"Coolant 106 °C, over 105 °C. {SYNTHETIC_FAULT}"
    assert cli.FAULT_START_S <= alert["t_start_s"] <= cli.FAULT_START_S + 2.5
    trigger = [c for c in alert["citations"] if c["role"] == "trigger"]
    assert [(c["signal"], c["source"], c["stat"]) for c in trigger] == [
        ("obd.coolant_temp", "OBD 0x05", "min")
    ]
    frame = get(client, f"/api/drives/{drive_id}/live_frame", t_s=cli.FAULT_START_S + 15.0).json()
    # The live view labels the scripted fault too (plan H8, H13).
    assert frame["alert"]["headline"] == "Coolant 106 °C, synthetic fault"
    assert frame["alert"]["synthetic"] is True
    assert frame["alert"]["citation"].startswith("OBD 0x05 over 105 °C for ")
    # The counts the drive list and the model summary read leave the synthetic fault out of
    # the measured alerts and name it on its own.
    stats = {s["stat_id"]: s for s in get(client, f"/api/drives/{drive_id}").json()["stats"]}
    assert stats["alerts.count"]["display"] == "0"
    assert (
        stats["alerts.synthetic_count"]["label"],
        stats["alerts.synthetic_count"]["display"],
    ) == (
        "Synthetic alerts",
        "1",
    )
    listed = {d["drive_id"]: d for d in get(client, "/api/drives").json()["drives"]}
    assert listed[drive_id]["n_alerts"] == 0
    before = get(client, f"/api/drives/{drive_id}/live_frame", t_s=cli.FAULT_START_S - 20.0)
    assert before.json()["alert"] is None
    zero = get(client, f"/api/drives/{drive_id}/report").json()["performance"]["zero_to_sixty"]
    assert len(zero) == 1 and zero[0]["seconds"] == pytest.approx(24.40, abs=0.25)


def test_live_stream_replays_a_drive(client: AsgiClient, demo: Demo) -> None:
    drive_id = demo.ids[cli.LABEL_SAMPLE]

    async def first_events() -> list[Any]:
        out: list[Any] = []
        query: dict[str, str | int | float] = {"replay": drive_id, "speed": 100}
        async with client.stream("GET", "/api/live/stream", query=query) as resp:
            assert resp.status == 200
            async for ev in resp.events():
                out.append(ev)
                if len(out) == 4:
                    break
        return out

    events = run(first_events())
    assert events[0].event == "state" and events[0].json() == {"moving": False}
    assert [e.event for e in events[1:]] == ["frame"] * 3
    assert events[1].json()["t_s"] == 0.0


def test_export_needs_the_privacy_file(
    client: AsgiClient, demo: Demo, config_dir: Path, tmp_path: Path
) -> None:
    drive_id = demo.ids[cli.LABEL_SYNTHETIC["track_day"]]
    refused = run(client.post(f"/api/drives/{drive_id}/export", query={"kind": "laps"}))
    assert refused.status == 409
    (config_dir / "privacy.local.toml").write_text(
        "[home]\nlat_deg = 10.0\nlon_deg = 10.0\n", encoding="utf-8"
    )
    done = run(client.post(f"/api/drives/{drive_id}/export", query={"kind": "laps"}))
    assert done.status == 200, done.text
    files = done.json()["files"]
    assert len(files) == 3
    for f in files:
        assert f.startswith("exports/lap-")
        validate_lap(json.loads((demo.data_dir / f).read_text(encoding="utf-8")))


def test_web_app_and_unknown_routes(client: AsgiClient) -> None:
    page = get(client, "/drives/anything")
    assert page.status == 200 and "Photoncar" in page.text
    missing = get(client, "/api/drives/no-such-drive")
    assert missing.status == 404 and missing.json() == {"error": "No drive with that ID."}
