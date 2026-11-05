"""Access rules for every ``/api`` path, as pure ASGI middleware (it also covers streams).

1. Auth. With ``PHOTONCAR_API_TOKEN`` set, every call needs ``Authorization: Bearer <token>``
   (401 otherwise). With it unset, only loopback clients are answered (403 otherwise).
2. The moving lock. While ``moving`` is true, every route except ``/api/health`` and the live
   stream of the current drive (``/api/live/stream`` with no ``replay``) answers 423
   ``{"error": "Parked views open when the car stops."}``. A replay stream of a stored drive
   is a parked view, so it is locked too (and an open one ends when the car starts moving).

Non-API paths (the web app's files) pass through: the page must load to show the live view
and to ask for the token.
"""

from __future__ import annotations

import hmac
import ipaddress
from urllib.parse import parse_qs

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from photoncar.api.live import LiveState

TOKEN_ENV = "PHOTONCAR_API_TOKEN"
LOCK_EXEMPT_PATHS = frozenset({"/api/health"})
LIVE_STREAM = "/api/live/stream"
REPLAY_PARAM = "replay"
MOVING_MESSAGE = "Parked views open when the car stops."
TOKEN_MESSAGE = "A valid device token is needed."
LOOPBACK_MESSAGE = "This device answers only itself until a device token is set."


def is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.is_loopback
    return ip.is_loopback


def lock_applies(path: str, query_string: bytes = b"") -> bool:
    """True for every parked view: all but health and the current drive's live stream."""
    if path in LOCK_EXEMPT_PATHS:
        return False
    if path == LIVE_STREAM:
        query = parse_qs(query_string.decode("latin-1"), keep_blank_values=True)
        return REPLAY_PARAM in query
    return True


class ApiGuard:
    def __init__(self, app: ASGIApp, *, token: str | None, live: LiveState) -> None:
        self.app = app
        self.token = token or None
        self.live = live

    def _auth_error(self, scope: Scope) -> JSONResponse | None:
        if self.token is not None:
            header = Headers(scope=scope).get("authorization", "")
            scheme, _, value = header.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                value.strip().encode("utf-8"), self.token.encode("utf-8")
            ):
                return JSONResponse(
                    {"error": TOKEN_MESSAGE}, 401, headers={"WWW-Authenticate": "Bearer"}
                )
            return None
        client = scope.get("client")
        host = client[0] if client else None
        if not is_loopback(host):
            return JSONResponse({"error": LOOPBACK_MESSAGE}, 403)
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_api_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        denial = self._auth_error(scope)
        locked = lock_applies(scope["path"], scope.get("query_string", b""))
        if denial is None and self.live.moving and locked:
            denial = JSONResponse({"error": MOVING_MESSAGE}, 423)
        if denial is not None:
            await denial(scope, receive, send)
            return
        await self.app(scope, receive, send)
