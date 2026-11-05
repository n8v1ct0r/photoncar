"""The portfolio embed: a static, read-only replay of 1 drive (contracts.md, "Portfolio embed").

``export_embed(data_dir, drive_id, out_dir, *, cfg, web_dist_embed, privacy_path=None)`` writes
``<out_dir>/data/*.json`` and then copies the embed build (``web/dist-embed``, made by
``pnpm build:embed``) into ``<out_dir>/``. The page reads the files through its ``StaticSource``.

The data files are the HTTP API response bodies for that drive, read through the same FastAPI app
the device serves (1 in-process ASGI ``GET`` per route), so nothing here computes them again:

- ``drives.json`` (``GET /api/drives``, holding only this drive), ``drive.json``,
  ``events.json``, ``report.json``, and ``legend.json`` when the server has ``/api/legend``.
- ``timeline.json``: ``DriveReader.timeline``, the function behind ``GET .../timeline``, with
  ``max_points`` = ``TIMELINE_POINTS`` (the value the review asks for) and the privacy trim
  applied before decimation (below).
- ``live_frames.json``: the ``LiveFrame`` list of 1 window of the drive (at most
  ``MAX_WINDOW_S`` seconds at ``[live] stream_hz``), from ``photoncar.api.live.ReplayData``,
  the live-frame logic behind ``/api/drives/{id}/live_frame``. The window opens a little before
  the most severe alert becomes known, so the tripmeter shows it; without alerts it opens at 0.
- ``trim.json``: ``{"windows_s": [[t0, t1], ...]}``, the privacy trim windows in seconds from
  the drive start, so the map can print trimmed stretches dotted.

What changes from the API bodies, and why:

- The privacy trim runs (hard rule 7): inside a trim window, every timeline value becomes null,
  events that touch the window are dropped, and no live frame falls in it. The timeline is
  trimmed row by row before it is decimated (``DriveReader.timeline(trim_ns=...)``), so a
  bucket that straddles a window edge can never carry an in-window min or max to a point
  outside the window; then every point inside a window is nulled again.
- ``drive.dataset_ref`` of a public-dataset replay becomes ``"comma2k19"``: the comma2k19
  reference names a route by its dongle ID and recording date, and the embed carries no dates
  and no IDs beyond the drive's own random one.

Refusals raise ``EmbedRefused`` with a plain sentence: an unknown drive, a live drive (owner
drives never ship), any other drive that is not synthetic or a replay of the public dataset
(``is_public_dataset_ref``), a missing or invalid privacy file, a missing embed build, an
output folder that is not empty, data that would hold latitude, longitude, dates, or other IDs,
and any output over budget. Budgets [proposed, owner to confirm]: JSON at most ``JSON_BUDGET``
bytes and JS plus CSS at most ``CODE_BUDGET`` bytes, both uncompressed; fonts ship as they are.
Every check runs before the first file is written.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI
from starlette.types import Message

from photoncar.api.app import DRIVE_ID_RE, ApiJSON, create_app
from photoncar.api.guard import TOKEN_ENV
from photoncar.api.live import ReplayData, fire_ns
from photoncar.config import Config, ConfigError, load_home_zone
from photoncar.privacy.export_filter import WindowSet
from photoncar.privacy.zones import trim_windows
from photoncar.sources.comma2k19 import is_public_dataset_ref
from photoncar.store.reader import DriveReader
from photoncar.units import NS_PER_S

JSON_BUDGET = 1_500_000
"""Bytes of JSON data, uncompressed [proposed, owner to confirm]."""
CODE_BUDGET = 250_000
"""Bytes of JS plus CSS, uncompressed [proposed, owner to confirm]."""
TIMELINE_POINTS = 1500
"""The ``max_points`` the review asks for (``TIMELINE_POINTS`` in web/src/shared/data.ts)."""
MAX_WINDOW_S = 120.0
LEAD_S = 20.0
"""Seconds of the window before the alert it opens on becomes known."""
DATASET_REF = "comma2k19"

LIST_KEYS = (
    "drive_id",
    "label",
    "source",
    "duration_s",
    "distance_m",
    "n_events",
    "n_alerts",
    "closed_cleanly",
)
TIMELINE_VALUE_KEYS = (
    "speed_mps",
    "rpm",
    "throttle",
    "coolant_c",
    "accel_long_g",
    "accel_lat_g",
    "x_m",
    "y_m",
)

NO_DRIVE = "No drive with that ID."
LIVE_REFUSED = "That drive was recorded live. Owner drives never ship in the embed."
NOT_PUBLIC = (
    "That drive is not a replay of the public dataset. Only public replays and synthetic "
    "drives ship in the embed."
)
PRIVACY_MISSING = (
    "The embed needs the privacy file, so the privacy trim can run. Copy "
    "config/privacy.example.toml to config/privacy.local.toml and set the home center."
)
NO_BUILD = "The embed build is missing. Run pnpm build:embed in web/ first."
NOT_EMPTY = "The output folder is not empty. Pick an empty or new folder."

DATE_RE = re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
LATLON_KEY_RE = re.compile(r'"(?:lat|lon|lng|latitude|longitude)(?:_deg)?"\s*:')


class EmbedRefused(ValueError):
    """The embed cannot be written; the message says why in 1 plain sentence."""


@dataclass(frozen=True, slots=True)
class EmbedReport:
    out_dir: Path
    drive_id: str
    data_files: dict[str, int]
    """Data file name to bytes."""
    json_bytes: int
    code_bytes: int
    """JS plus CSS of the copied build."""
    font_bytes: int
    window_s: tuple[float, float]
    n_frames: int
    trim_windows_s: list[tuple[float, float]] = field(default_factory=list)
    has_legend: bool = False


# -- the API, in process ----------------------------------------------------------------------


def _asgi_get(app: FastAPI, path: str, query: dict[str, str] | None = None) -> tuple[int, bytes]:
    """1 ``GET`` through the whole app (middleware included) from a loopback client."""
    headers: list[tuple[bytes, bytes]] = [(b"host", b"127.0.0.1")]
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": urlencode(query or {}).encode(),
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }

    async def run() -> tuple[int, bytes]:
        status = 0
        body = bytearray()
        requested = False

        async def receive() -> Message:
            nonlocal requested
            if not requested:
                requested = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            elif message["type"] == "http.response.body":
                body.extend(message.get("body", b""))

        await app(scope, receive, send)
        return status, bytes(body)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run())
    # Called from code that already runs an event loop: use a loop on its own thread.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, run()).result()


def _get_json(app: FastAPI, path: str, query: dict[str, str] | None = None) -> Any:
    status, body = _asgi_get(app, path, query)
    if status != 200:
        raise RuntimeError(f"GET {path} answered {status}: {body[:200]!r}")
    return json.loads(body)


def _render(content: Any) -> bytes:
    """The same bytes the API sends for ``content``."""
    return bytes(ApiJSON(content).body)


# -- privacy ----------------------------------------------------------------------------------


def _windows_s(
    windows: list[tuple[int, int]], t0_ns: int, duration_s: float
) -> list[tuple[float, float]]:
    """Trim windows in seconds from the drive start, clipped to the drive."""
    out: list[tuple[float, float]] = []
    for a, b in windows:
        lo = max(0.0, (a - t0_ns) / NS_PER_S)
        hi = min(duration_s, (b - t0_ns) / NS_PER_S)
        if hi >= lo and hi >= 0.0 and lo <= duration_s:
            out.append((lo, hi))
    return out


def _inside(t: float, windows: list[tuple[float, float]]) -> bool:
    return any(lo <= t <= hi for lo, hi in windows)


def _trim_timeline(tl: dict[str, Any], windows: list[tuple[float, float]]) -> dict[str, Any]:
    if not windows:
        return tl
    out = dict(tl)
    for key in TIMELINE_VALUE_KEYS:
        if key in out:
            out[key] = [
                None if _inside(t, windows) else v for t, v in zip(tl["t_s"], tl[key], strict=True)
            ]
    return out


def _trim_events(body: dict[str, Any], ws: WindowSet, t0_ns: int) -> dict[str, Any]:
    if not ws.windows:
        return body

    def touches(ev: dict[str, Any]) -> bool:
        lo = ev["t_start_s"]
        hi = ev["t_end_s"]
        for c in ev["citations"]:
            lo = min(lo, c["t_start_s"])
            hi = max(hi, c["t_end_s"])
        return ws.overlaps(t0_ns + round(lo * NS_PER_S), t0_ns + round(hi * NS_PER_S))

    return {**body, "events": [ev for ev in body["events"] if not touches(ev)]}


# -- live frames ------------------------------------------------------------------------------


def _frame_window(data: ReplayData) -> tuple[float, float]:
    duration = data.duration_s
    if duration <= MAX_WINDOW_S:
        return 0.0, duration
    start = 0.0
    if data.alerts:
        worst = max(data.alerts, key=lambda ev: (ev.severity, -ev.t_start_ns))
        known_s = (fire_ns(worst, data.sustain_ns) - data.t0_ns) / NS_PER_S
        start = min(max(0.0, known_s - LEAD_S), duration - MAX_WINDOW_S)
    return start, start + MAX_WINDOW_S


def _frames(
    data: ReplayData, stream_hz: float, windows: list[tuple[float, float]]
) -> tuple[list[dict[str, Any]], tuple[float, float]]:
    start, end = _frame_window(data)
    step_ns = round(NS_PER_S / stream_hz)
    start_ns = round(start * NS_PER_S)
    n = int((round(end * NS_PER_S) - start_ns) // step_ns) + 1
    frames: list[dict[str, Any]] = []
    for k in range(n):
        t = (start_ns + k * step_ns) / NS_PER_S
        if _inside(t, windows):
            continue
        frames.append(data.frame_at(t))
    return frames, (start, end)


# -- checks -----------------------------------------------------------------------------------


def _scan(name: str, raw: bytes, drive_id: str) -> None:
    text = raw.decode("utf-8")
    if LATLON_KEY_RE.search(text):
        raise EmbedRefused(f"{name} would hold latitude or longitude, so the embed stops.")
    if DATE_RE.search(text):
        raise EmbedRefused(f"{name} would hold a date, so the embed stops.")
    for m in UUID_RE.finditer(text):
        if m.group(0) != drive_id:
            raise EmbedRefused(
                f"{name} would hold an ID other than the drive's, so the embed stops."
            )


def _code_bytes(dist: Path) -> int:
    return sum(
        p.stat().st_size for p in dist.rglob("*") if p.is_file() and p.suffix in {".js", ".css"}
    )


def _font_bytes(dist: Path) -> int:
    return sum(p.stat().st_size for p in dist.rglob("*") if p.is_file() and p.suffix == ".woff2")


def _mb(n: int) -> str:
    return f"{n / 1_000_000:.2f} MB"


def _kb(n: int) -> str:
    return f"{n / 1000:.1f} KB"


# -- export -----------------------------------------------------------------------------------


def export_embed(
    data_dir: Path,
    drive_id: str,
    out_dir: Path,
    *,
    cfg: Config,
    web_dist_embed: Path,
    privacy_path: Path | None = None,
) -> EmbedReport:
    """Write the static embed of 1 drive into ``out_dir``. See the module docstring."""
    data_dir = Path(data_dir).resolve()
    out_dir = Path(out_dir)
    dist = Path(web_dist_embed)
    if not DRIVE_ID_RE.fullmatch(drive_id):
        raise EmbedRefused(NO_DRIVE)
    drive_file = data_dir / "drives" / f"{drive_id}.sqlite"
    if not drive_file.is_file():
        raise EmbedRefused(NO_DRIVE)
    if not (dist / "index.html").is_file():
        raise EmbedRefused(NO_BUILD)
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise EmbedRefused(NOT_EMPTY)

    with DriveReader(drive_file) as reader:
        meta = reader.meta()
        if meta.source == "live":
            raise EmbedRefused(LIVE_REFUSED)
        if meta.source != "synthetic" and not (
            meta.source == "replay" and is_public_dataset_ref(meta.dataset_ref)
        ):
            raise EmbedRefused(NOT_PUBLIC)
        try:
            zone = load_home_zone(cfg, privacy_path)
        except ConfigError:
            raise EmbedRefused(PRIVACY_MISSING) from None
        replay = ReplayData.load(reader, cfg)
        ws = WindowSet(trim_windows(reader.gps_fixes(), zone))
        # Rows inside a window are emptied before decimation, for every column.
        timeline = reader.timeline(TIMELINE_POINTS, trim_ns=ws.windows)

    windows = _windows_s(ws.windows, replay.t0_ns, replay.duration_s)
    stream_hz = float(cfg.get("live.stream_hz"))
    app = create_app(cfg, data_dir, web_dist=dist)
    base = f"/api/drives/{drive_id}"

    detail = _get_json(app, base)
    drive = dict(detail["drive"])
    if drive.get("source") == "replay" and drive.get("dataset_ref") is not None:
        drive["dataset_ref"] = DATASET_REF
    detail = {**detail, "drive": drive}
    listed = [d for d in _get_json(app, "/api/drives")["drives"] if d["drive_id"] == drive_id]
    item = listed[0] if listed else {k: drive[k] for k in LIST_KEYS}

    files: dict[str, bytes] = {
        "drives.json": _render({"drives": [item]}),
        "drive.json": _render(detail),
        "timeline.json": _render(_trim_timeline(timeline, windows)),
        "events.json": _render(_trim_events(_get_json(app, f"{base}/events"), ws, replay.t0_ns)),
        "report.json": _render(_get_json(app, f"{base}/report")),
    }
    status, legend = _asgi_get(app, "/api/legend")
    if status == 200:
        files["legend.json"] = legend
    frames, window = _frames(replay, stream_hz, windows)
    files["live_frames.json"] = _render(frames)
    files["trim.json"] = _render({"windows_s": [list(w) for w in windows]})

    for name, raw in files.items():
        _scan(name, raw, drive_id)
    json_bytes = sum(len(b) for b in files.values())
    if json_bytes > JSON_BUDGET:
        raise EmbedRefused(
            f"The embed data is {_mb(json_bytes)}, over the {_mb(JSON_BUDGET)} budget."
        )
    code = _code_bytes(dist)
    if code > CODE_BUDGET:
        raise EmbedRefused(f"The embed code is {_kb(code)}, over the {_kb(CODE_BUDGET)} budget.")

    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(dist, out_dir, dirs_exist_ok=True, ignore=shutil.ignore_patterns("data"))
    data_out = out_dir / "data"
    data_out.mkdir(exist_ok=True)
    for name, raw in files.items():
        (data_out / name).write_bytes(raw)
    return EmbedReport(
        out_dir=out_dir,
        drive_id=drive_id,
        data_files={name: len(raw) for name, raw in files.items()},
        json_bytes=json_bytes,
        code_bytes=code,
        font_bytes=_font_bytes(dist),
        window_s=window,
        n_frames=len(frames),
        trim_windows_s=windows,
        has_legend="legend.json" in files,
    )
