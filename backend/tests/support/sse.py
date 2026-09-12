"""Read an SSE endpoint by speaking ASGI to it, because no test client can.

`httpx.ASGITransport` collects the whole response body before it returns anything, and
Starlette's `TestClient` runs the app on another thread's event loop — neither can read
frames one at a time from a stream that is designed never to end. So these tests call the
ASGI application themselves: chunks arrive in a queue as the app sends them, and closing
the connection delivers the `http.disconnect` a real client's departure would.

The scope deliberately claims ASGI spec version 2.1, which is what makes Starlette listen
for that disconnect instead of waiting for a send to fail.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass
class SSEEvent:
    id: str | None
    event: str | None
    data: dict[str, Any] | None
    raw: str

    @property
    def seq(self) -> int:
        assert self.id is not None, f"frame carried no id: {self.raw!r}"
        return int(self.id)

    @property
    def is_heartbeat(self) -> bool:
        return self.raw.startswith(":")


class SSEConnection:
    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._buffer = ""
        self.started = asyncio.Event()

    async def next_frame(self, timeout: float = 3.0) -> SSEEvent:
        """The next complete frame, waiting up to `timeout` for one to arrive."""
        async with asyncio.timeout(timeout):
            while "\n\n" not in self._buffer:
                chunk = await self._chunks.get()
                if chunk is None:
                    raise AssertionError(f"stream ended with {self._buffer!r} buffered")
                self._buffer += chunk.decode("utf-8")
        raw, self._buffer = self._buffer.split("\n\n", 1)
        return _parse_frame(raw)

    async def next_event(self, timeout: float = 3.0) -> SSEEvent:
        """The next frame that is an event rather than a heartbeat comment."""
        while True:
            frame = await self.next_frame(timeout)
            if not frame.is_heartbeat:
                return frame

    async def drain(self, duration: float = 0.6) -> list[SSEEvent]:
        """Every frame that arrives in the next `duration` seconds.

        A fixed window rather than "until it goes quiet": on a stream with heartbeats it
        never goes quiet, and waiting for a gap would wait forever.
        """
        frames: list[SSEEvent] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + duration
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return frames
            try:
                frames.append(await self.next_frame(remaining))
            except TimeoutError:
                return frames


def _parse_frame(raw: str) -> SSEEvent:
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if not line or line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        fields[name.strip()] = value.strip()
    data = fields.get("data")
    return SSEEvent(
        id=fields.get("id"),
        event=fields.get("event"),
        data=json.loads(data) if data else None,
        raw=raw,
    )


@dataclass
class AsgiResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


async def asgi_request(
    app: Any,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> AsgiResponse:
    """One ordinary request, on the loop the test is already running.

    `TestClient` would run the app on another thread's event loop, which is fine for its
    own requests and wrong here: the listener under test belongs to *this* loop.
    """
    parts = urlsplit(url)
    status: list[int] = []
    response_headers: dict[str, str] = {}
    body = bytearray()
    finished = asyncio.Event()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])
            response_headers.update(
                {key.decode().lower(): value.decode() for key, value in message.get("headers", [])}
            )
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                finished.set()

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": parts.path,
            "raw_path": parts.path.encode(),
            "query_string": parts.query.encode(),
            "root_path": "",
            "headers": [
                (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()
            ],
            "client": ("127.0.0.1", 54321),
            "server": ("127.0.0.1", 8787),
        },
        receive,
        send,
    )
    assert status, "the application never started a response"
    return AsgiResponse(status[0], response_headers, bytes(body))


@asynccontextmanager
async def open_sse(
    app: Any, url: str, *, headers: Mapping[str, str] | None = None
) -> AsyncIterator[SSEConnection]:
    """Open `url` against `app` and yield the live connection."""
    parts = urlsplit(url)
    connection = SSEConnection()
    disconnected = asyncio.Event()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": parts.path,
        "raw_path": parts.path.encode(),
        "query_string": parts.query.encode(),
        "root_path": "",
        "headers": [
            (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()
        ],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8787),
    }

    async def receive() -> dict[str, Any]:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            connection.status = message["status"]
            connection.headers = {
                key.decode().lower(): value.decode() for key, value in message.get("headers", [])
            }
            connection.started.set()
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                connection._chunks.put_nowait(body)
            if not message.get("more_body", False):
                connection._chunks.put_nowait(None)

    task = asyncio.create_task(app(scope, receive, send))
    try:
        started = asyncio.create_task(connection.started.wait())
        async with asyncio.timeout(5.0):
            await asyncio.wait({task, started}, return_when=asyncio.FIRST_COMPLETED)
        started.cancel()
        if task.done():
            task.result()  # the app failed before answering; re-raise what it raised
        yield connection
    finally:
        # Let the graceful path run first — the disconnect is what a real client sends,
        # and the stream's cleanup is part of what these tests are checking.
        disconnected.set()
        with suppress(Exception, asyncio.CancelledError):
            async with asyncio.timeout(2.0):
                await task
        if not task.done():
            task.cancel()
            with suppress(Exception, asyncio.CancelledError):
                async with asyncio.timeout(2.0):
                    await task
