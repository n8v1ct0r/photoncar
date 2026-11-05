"""A small stdlib ASGI test client. httpx is not installed, so ``fastapi.testclient`` is out.

It speaks ASGI 3 HTTP directly to an app in the running event loop: ``request`` for plain
calls (JSON in, JSON or bytes out), and ``stream`` for streaming responses such as server-sent
events, which it parses incrementally. Leaving a ``stream`` block sends ``http.disconnect``,
as a browser closing the connection would.

The scope says ASGI spec 2.3, so Starlette's streaming responses listen for the disconnect.
Use it from ``asyncio.run(...)``; pytest-asyncio is not installed.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from starlette.types import ASGIApp, Message

DEFAULT_CLIENT = ("127.0.0.1", 50000)
_DONE: Message = {"type": "test.done"}


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> Any:
        return jsonlib.loads(self.body)


@dataclass(frozen=True, slots=True)
class SseEvent:
    event: str | None
    data: str
    id: str | None = None

    def json(self) -> Any:
        return jsonlib.loads(self.data)


class StreamResponse:
    """A response whose body arrives in chunks. Read ``chunks`` or ``events``, not both."""

    def __init__(
        self, status: int, headers: dict[str, str], queue: asyncio.Queue[Message], timeout_s: float
    ) -> None:
        self.status = status
        self.headers = headers
        self._queue = queue
        self._timeout_s = timeout_s
        self.finished = False

    async def chunks(self) -> AsyncIterator[bytes]:
        while not self.finished:
            message = await asyncio.wait_for(self._queue.get(), self._timeout_s)
            if message["type"] == "test.error":
                raise message["exc"]
            if message["type"] == "test.done":
                self.finished = True
                return
            if message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    yield body
                if not message.get("more_body", False):
                    self.finished = True

    async def events(self) -> AsyncIterator[SseEvent]:
        """Parse the body as ``text/event-stream``. Comment lines (``:``) are skipped."""
        buffer = ""
        async for chunk in self.chunks():
            buffer += chunk.decode("utf-8")
            buffer = buffer.replace("\r\n", "\n").replace("\r", "\n")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                event = _parse_block(block)
                if event is not None:
                    yield event


def _parse_block(block: str) -> SseEvent | None:
    name: str | None = None
    event_id: str | None = None
    data: list[str] = []
    for line in block.split("\n"):
        if not line or line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            name = value
        elif field == "data":
            data.append(value)
        elif field == "id":
            event_id = value
    if name is None and not data:
        return None
    return SseEvent(event=name, data="\n".join(data), id=event_id)


class AsgiClient:
    def __init__(
        self,
        app: ASGIApp,
        *,
        client: tuple[str, int] = DEFAULT_CLIENT,
        headers: Mapping[str, str] | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.app = app
        self.client = client
        self.headers = dict(headers or {})
        self.timeout_s = timeout_s

    def _scope(
        self,
        method: str,
        path: str,
        query: Mapping[str, str | int | float] | None,
        headers: Mapping[str, str] | None,
        body: bytes,
        content_type: str | None,
    ) -> dict[str, Any]:
        merged = {**self.headers, **(headers or {})}
        raw = [(b"host", b"testserver")]
        raw += [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in merged.items()]
        if content_type is not None:
            raw.append((b"content-type", content_type.encode("latin-1")))
        if body:
            raw.append((b"content-length", str(len(body)).encode("latin-1")))
        qs = urlencode({k: str(v) for k, v in (query or {}).items()})
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": path,
            "raw_path": quote(path).encode("latin-1"),
            "root_path": "",
            "query_string": qs.encode("latin-1"),
            "headers": raw,
            "client": self.client,
            "server": ("testserver", 80),
            "state": {},
        }

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str | int | float] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        body: bytes = b"",
    ) -> AsyncIterator[StreamResponse]:
        content_type = None
        if json is not None:
            body = jsonlib.dumps(json).encode("utf-8")
            content_type = "application/json"
        scope = self._scope(method, path, query, headers, body, content_type)
        queue: asyncio.Queue[Message] = asyncio.Queue()
        disconnect = asyncio.Event()
        request_sent = False

        async def receive() -> Message:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            await queue.put(message)

        async def run() -> None:
            try:
                await self.app(scope, receive, send)
            except Exception as exc:
                await queue.put({"type": "test.error", "exc": exc})
            else:
                await queue.put(_DONE)

        task = asyncio.create_task(run())
        try:
            start = await asyncio.wait_for(queue.get(), self.timeout_s)
            if start["type"] == "test.error":
                raise start["exc"]
            if start["type"] != "http.response.start":
                raise RuntimeError(f"the app sent {start['type']} before a response start")
            response_headers: dict[str, str] = {}
            for k, v in start.get("headers", []):
                key = k.decode("latin-1").lower()
                value = v.decode("latin-1")
                response_headers[key] = (
                    f"{response_headers[key]}, {value}" if key in response_headers else value
                )
            yield StreamResponse(int(start["status"]), response_headers, queue, self.timeout_s)
        finally:
            disconnect.set()
            try:
                await asyncio.wait_for(asyncio.shield(task), self.timeout_s)
            except TimeoutError:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str | int | float] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        body: bytes = b"",
    ) -> Response:
        async with self.stream(
            method, path, query=query, headers=headers, json=json, body=body
        ) as resp:
            chunks = [c async for c in resp.chunks()]
        return Response(resp.status, resp.headers, b"".join(chunks))

    async def get(
        self,
        path: str,
        *,
        query: Mapping[str, str | int | float] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        return await self.request("GET", path, query=query, headers=headers)

    async def post(
        self,
        path: str,
        *,
        query: Mapping[str, str | int | float] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
    ) -> Response:
        return await self.request("POST", path, query=query, headers=headers, json=json)
