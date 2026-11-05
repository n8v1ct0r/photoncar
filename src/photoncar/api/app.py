"""The Photoncar HTTP server: the JSON API of docs/contracts.md and the web app's files.

``create_app(cfg, data_dir, *, summary_client=None, clock=None, web_dist=None)``:

- ``summary_client`` defaults to ``AnthropicSummaryClient`` (real model, key from
  ``ANTHROPIC_API_KEY``). ``FakeSummaryClient`` runs only when passed here explicitly.
- ``clock`` is wall-clock UTC nanoseconds for the summary rate limits and the daily spend day
  (default ``WallClock``).
- ``web_dist`` is the built web app (default ``web/dist``). Any non-API ``GET`` serves the file
  at that path, or ``index.html`` so the web app's own router can handle it.
- ``app.state.live`` is the ``LiveState`` the pipeline uses to set ``moving`` and ``mode`` and
  to publish live frames.

Every API error is ``{"error": "<plain sentence>"}`` with a matching status. Drive files are
opened read-only per request. Blocking routes are plain functions, which FastAPI runs in its
thread pool.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.exceptions import HTTPException as StarletteHTTPException

from photoncar import __version__
from photoncar.api.guard import TOKEN_ENV, ApiGuard, is_api_path
from photoncar.api.legend import legend
from photoncar.api.live import LiveState, ReplayData
from photoncar.api.views import (
    drive_detail,
    drive_list_item,
    events_json,
    list_item_from_file,
    stat_json,
)
from photoncar.clock import Clock
from photoncar.config import REPO_ROOT, Config, ConfigError
from photoncar.export.laps_v0 import export_drive_laps
from photoncar.export.report import drive_report
from photoncar.store.index import DriveIndex, DriveListItem
from photoncar.store.reader import DriveReader
from photoncar.summary.limits import WallClock
from photoncar.summary.model_client import AnthropicSummaryClient, SummaryClient
from photoncar.summary.service import SummaryError, SummaryService

log = logging.getLogger(__name__)

DEFAULT_WEB_DIST = REPO_ROOT / "web" / "dist"
DRIVE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
MAX_STREAM_SPEED = 100.0

NO_DRIVE = "No drive with that ID."
NOT_FOUND = "Not found."
PRIVACY_MISSING = (
    "Exports need the privacy file. Copy config/privacy.example.toml to "
    "config/privacy.local.toml and set the home center."
)
STATUS_MESSAGES = {
    400: "The request is not valid.",
    404: NOT_FOUND,
    405: "That method is not allowed here.",
    413: "The request is too large.",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str, *, retry_after_s: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after_s = retry_after_s


def _clean(x: Any) -> Any:
    """JSON has no NaN or infinity: send null instead."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_clean(v) for v in x]
    return x


class ApiJSON(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(
            _clean(content), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")


def error_response(status: int, message: str, retry_after_s: int | None = None) -> ApiJSON:
    headers = {"Retry-After": str(retry_after_s)} if retry_after_s is not None else None
    return ApiJSON({"error": message}, status_code=status, headers=headers)


def _param_name(loc: tuple[Any, ...]) -> str:
    names = [str(p) for p in loc if p not in ("query", "path", "body", "header")]
    return names[-1] if names else "request"


# -- per-app context -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApiContext:
    cfg: Config
    data_root: Path
    web_dist: Path
    stream_hz: float
    live: LiveState
    summary: SummaryService

    @property
    def drives_dir(self) -> Path:
        return self.data_root / "drives"

    @property
    def index_path(self) -> Path:
        return self.data_root / "index.sqlite"


def context(request: Request) -> ApiContext:
    ctx = request.app.state.ctx
    if not isinstance(ctx, ApiContext):
        raise RuntimeError("the app has no Photoncar context")
    return ctx


Ctx = Annotated[ApiContext, Depends(context)]


def drive_path(ctx: ApiContext, drive_id: str) -> Path:
    if not DRIVE_ID_RE.fullmatch(drive_id):
        raise ApiError(404, NO_DRIVE)
    path = ctx.drives_dir / f"{drive_id}.sqlite"
    if not path.is_file():
        raise ApiError(404, NO_DRIVE)
    return path


@contextmanager
def open_drive(ctx: ApiContext, drive_id: str) -> Iterator[DriveReader]:
    reader = DriveReader(drive_path(ctx, drive_id))
    try:
        yield reader
    finally:
        reader.close()


def index_item(ctx: ApiContext, drive_id: str) -> DriveListItem | None:
    if not ctx.index_path.is_file():
        return None
    with DriveIndex(ctx.index_path) as index:
        return index.get(drive_id)


@dataclass(frozen=True, slots=True)
class StreamRequest:
    replay: ReplayData | None
    speed: float


def stream_request(
    ctx: Ctx,
    replay: str | None = None,
    speed: Annotated[float, Query(gt=0, le=MAX_STREAM_SPEED, allow_inf_nan=False)] = 1.0,
) -> StreamRequest:
    """Load the replayed drive before the stream starts, so a bad ID is a plain JSON 404."""
    if replay is None:
        return StreamRequest(None, speed)
    with open_drive(ctx, replay) as reader:
        return StreamRequest(ReplayData.load(reader, ctx.cfg), speed)


# -- routes --------------------------------------------------------------------------------------


def health(ctx: Ctx) -> Response:
    live = ctx.live
    return ApiJSON({"ok": True, "version": __version__, "mode": live.mode, "moving": live.moving})


def list_drives(ctx: Ctx) -> Response:
    if not ctx.index_path.is_file():
        return ApiJSON({"drives": []})
    with DriveIndex(ctx.index_path) as index:
        items = index.list()
    return ApiJSON({"drives": [drive_list_item(i) for i in items]})


def get_drive(ctx: Ctx, drive_id: str) -> Response:
    with open_drive(ctx, drive_id) as reader:
        item = index_item(ctx, drive_id) or list_item_from_file(reader)
        return ApiJSON(
            {"drive": drive_detail(reader, item), "stats": [stat_json(s) for s in reader.stats()]}
        )


def get_timeline(
    ctx: Ctx, drive_id: str, max_points: Annotated[int, Query(ge=2, le=100_000)] = 1500
) -> Response:
    with open_drive(ctx, drive_id) as reader:
        return ApiJSON(reader.timeline(max_points))


def get_events(ctx: Ctx, drive_id: str) -> Response:
    with open_drive(ctx, drive_id) as reader:
        return ApiJSON({"events": events_json(reader)})


def get_report(ctx: Ctx, drive_id: str) -> Response:
    with open_drive(ctx, drive_id) as reader:
        return ApiJSON(drive_report(reader))


def get_live_frame(
    ctx: Ctx, drive_id: str, t_s: Annotated[float, Query(allow_inf_nan=False)]
) -> Response:
    with open_drive(ctx, drive_id) as reader:
        data = ReplayData.load(reader, ctx.cfg)
    if not 0.0 <= t_s <= data.duration_s:
        raise ApiError(422, "That time is outside this drive.")
    return ApiJSON(data.frame_at(t_s))


async def live_stream(
    ctx: Ctx, req: Annotated[StreamRequest, Depends(stream_request)]
) -> AsyncIterator[ServerSentEvent]:
    """``event: state`` with ``{moving}`` first and on every change; ``event: frame`` with a
    ``LiveFrame`` at ``[live] stream_hz``. A replay advances ``speed`` times real time and ends
    after its last frame; it is a parked view, so it also ends, after the state event, once the
    car moves. Without ``replay``, frames the pipeline publishes are forwarded."""
    live = ctx.live
    period = 1.0 / ctx.stream_hz
    loop = asyncio.get_running_loop()
    moving = live.moving
    yield ServerSentEvent(event="state", data={"moving": moving})
    if req.replay is not None:
        if moving:  # the car started after the lock check let the request through
            return
        data = req.replay
        step_ns = round(req.speed / ctx.stream_hz * 1e9)
        n_frames = round(data.duration_s * 1e9) // step_ns + 1
        start = loop.time()
        for k in range(n_frames):
            if k:
                await asyncio.sleep(max(0.0, start + k * period - loop.time()))
            if live.moving != moving:
                moving = live.moving
                yield ServerSentEvent(event="state", data={"moving": moving})
                if moving:
                    return
            yield ServerSentEvent(event="frame", data=_clean(data.frame_at(k * step_ns / 1e9)))
        return
    seen = 0
    while True:
        if live.moving != moving:
            moving = live.moving
            yield ServerSentEvent(event="state", data={"moving": moving})
        version, latest = live.latest()
        if latest is not None and version != seen:
            seen = version
            yield ServerSentEvent(event="frame", data=_clean(latest))
        await asyncio.sleep(period)


def get_legend(ctx: Ctx) -> Response:
    return ApiJSON(legend(ctx.cfg))


def post_summary(ctx: Ctx, drive_id: str, request: Request) -> Response:
    with open_drive(ctx, drive_id) as reader:
        stats = reader.stats()
    client_ip = request.client.host if request.client is not None else ""
    try:
        result = ctx.summary.summarize(stats, client_ip)
    except SummaryError as e:
        raise ApiError(e.status, e.message, retry_after_s=e.retry_after_s) from None
    return ApiJSON(result.to_json())


def post_export(ctx: Ctx, drive_id: str, kind: str) -> Response:
    """Lap v0 files under ``<data_dir>/exports/``, named relative to the data directory."""
    if kind != "laps":
        raise ApiError(422, "Only lap exports exist. Use kind=laps.")
    with open_drive(ctx, drive_id) as reader:
        try:
            paths = export_drive_laps(reader, ctx.cfg, base=ctx.data_root)
        except ConfigError:
            raise ApiError(409, PRIVACY_MISSING) from None
    files = [p.resolve().relative_to(ctx.data_root).as_posix() for p in paths]
    return ApiJSON({"files": files})


def web_app(ctx: Ctx, path: str) -> Response:
    if is_api_path("/" + path):
        raise ApiError(404, NOT_FOUND)
    dist = ctx.web_dist
    index_html = dist / "index.html"
    if not index_html.is_file():
        raise ApiError(404, "The web app is not built yet. Run make build.")
    if path:
        candidate = (dist / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(dist):
            return FileResponse(candidate)
    return FileResponse(index_html, media_type="text/html", headers={"Cache-Control": "no-cache"})


# -- error handlers ------------------------------------------------------------------------------


async def api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return error_response(exc.status, exc.message, exc.retry_after_s)


async def http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    return error_response(
        exc.status_code, STATUS_MESSAGES.get(exc.status_code, "The request failed.")
    )


async def validation_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    errors = exc.errors()
    first = errors[0] if errors else {}
    name = _param_name(tuple(first.get("loc", ())))
    if first.get("type") == "missing":
        return error_response(422, f"The {name} value is missing.")
    return error_response(422, f"The {name} value is not valid.")


async def server_error(request: Request, exc: Exception) -> Response:
    log.exception("Unhandled error on %s", request.url.path, exc_info=exc)
    return error_response(500, "The device hit an error. Try again.")


# -- the factory ---------------------------------------------------------------------------------


def create_app(
    cfg: Config,
    data_dir: Path,
    *,
    summary_client: SummaryClient | None = None,
    clock: Clock | None = None,
    web_dist: Path | None = None,
) -> FastAPI:
    data_root = Path(data_dir).resolve()
    stream_hz = float(cfg.get("live.stream_hz"))
    if not stream_hz > 0:
        raise ConfigError("live.stream_hz must be positive")
    live = LiveState()
    client = summary_client if summary_client is not None else AnthropicSummaryClient(cfg)
    ctx = ApiContext(
        cfg=cfg,
        data_root=data_root,
        web_dist=(web_dist if web_dist is not None else DEFAULT_WEB_DIST).resolve(),
        stream_hz=stream_hz,
        live=live,
        summary=SummaryService(cfg, data_root / "index.sqlite", client, clock or WallClock()),
    )
    app = FastAPI(
        title="Photoncar",
        version=__version__,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        default_response_class=ApiJSON,
    )
    app.state.ctx = ctx
    app.state.live = live
    app.state.summary = ctx.summary
    token = os.environ.get(TOKEN_ENV, "").strip() or None
    app.add_middleware(ApiGuard, token=token, live=live)

    app.add_exception_handler(ApiError, api_error)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(Exception, server_error)

    app.get("/api/health")(health)
    app.get("/api/drives")(list_drives)
    app.get("/api/drives/{drive_id}")(get_drive)
    app.get("/api/drives/{drive_id}/timeline")(get_timeline)
    app.get("/api/drives/{drive_id}/events")(get_events)
    app.get("/api/drives/{drive_id}/report")(get_report)
    app.get("/api/drives/{drive_id}/live_frame")(get_live_frame)
    app.get("/api/live/stream", response_class=EventSourceResponse)(live_stream)
    app.get("/api/legend")(get_legend)
    app.post("/api/drives/{drive_id}/summary")(post_summary)
    app.post("/api/drives/{drive_id}/export")(post_export)
    app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)(web_app)
    return app
