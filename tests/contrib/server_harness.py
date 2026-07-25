"""
Server-fidelity ASGI harness.

``asgi_harness.scripted_receive`` is more forgiving than any real server, and
each divergence hides a finding (see docs/disconnect-watcher-analysis.md, "Test
gaps"). ``FakeServer`` models what the four production servers actually do:

* ``http.request`` framing with ``more_body``, fed upfront or mid-request
* ``http.disconnect`` once the response completes, on a client that never left
* repeated re-delivery (uvicorn) vs one-shot-then-park (hypercorn, daphne,
  granian)
* a ``receive`` that raises
* a bounded app queue (hypercorn's ``max_app_queue_size``, default 10)
* a selectable — or absent — ASGI ``spec_version``
* the response-trailers extension, where the response is not complete until the
  final ``http.response.trailers`` message rather than the final body one

The completion rule is deliberately reimplemented here rather than imported from
``aiofence.contrib.starlette``: the harness models what a server does, and
sharing the predicate with the code under test would make it agree by
construction.

Every helper is bounded by ``DEFAULT_TIMEOUT`` so a hang fails the test instead
of blocking CI.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from enum import StrEnum
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .asgi_harness import http_scope, post_scope

DEFAULT_TIMEOUT = 1.0


class DeliveryMode(StrEnum):
    """How the server hands out ``http.disconnect``."""

    REPEAT = "repeat"  # uvicorn: recomputed per call, every reader sees it
    ONCE = "once"  # hypercorn / daphne / granian: enqueued once, then park


class FakeServer:
    """
    One HTTP request as a real ASGI server would drive it.

    Args:
        body: Request body. ``bytes`` becomes a single ``http.request`` message
            (uvicorn sends one even for an empty GET body); a sequence becomes
            one message per chunk, ``more_body`` set on all but the last.
        defer_body: Queue nothing upfront — the test calls ``feed_body``, which
            models headers arriving well before the body.
        delivery: ``http.disconnect`` delivery semantics.
        spec_version: ASGI ``spec_version`` in the scope; ``None`` omits the
            ``asgi`` key entirely (daphne, Starlette's TestClient).
        max_queue_size: Bound on undelivered ``http.request`` messages.
            ``feed_body`` blocks while the bound is reached, like hypercorn.
        error: Raised by ``receive`` from the ``error_after``-th call on.
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        body: bytes | Sequence[bytes] = b"",
        method: str = "GET",
        path: str = "/work",
        defer_body: bool = False,
        delivery: DeliveryMode = DeliveryMode.REPEAT,
        spec_version: str | None = "2.3",
        max_queue_size: int | None = None,
        error: BaseException | None = None,
        error_after: int = 1,
        content_length: int | None = None,
    ) -> None:
        chunks = [body] if isinstance(body, bytes) else list(body)
        self.scope = _build_scope(
            method=method,
            path=path,
            spec_version=spec_version,
            content_length=sum(len(c) for c in chunks)
            if content_length is None
            else content_length,
        )
        self.sent: list[Message] = []
        self.receive_calls = 0
        self.parked = 0
        self.disconnects_delivered = 0
        self.response_complete = False

        self._delivery = delivery
        self._max_queue_size = max_queue_size
        self._error = error
        self._error_after = error_after
        self._client_gone = False
        self._expects_trailers = False
        self._waiters: list[asyncio.Future[None]] = []
        self._pending: deque[Message] = deque()
        if not defer_body:
            self._pending.extend(_frame(chunks))

    async def receive(self) -> Message:
        self.receive_calls += 1
        if self._error is not None and self.receive_calls >= self._error_after:
            raise self._error

        while True:
            if self._pending:
                message = self._pending.popleft()
                self._notify()
                return message

            if self._client_gone or self.response_complete:
                if self._delivery is DeliveryMode.REPEAT or self.disconnects_delivered == 0:
                    self.disconnects_delivered += 1
                    return {"type": "http.disconnect"}
                self.parked += 1
                await self._park()

            await self._wait()

    async def send(self, message: Message) -> None:
        self.sent.append(message)
        match message["type"]:
            case "http.response.start":
                self._expects_trailers = bool(message.get("trailers", False))
            case "http.response.body" | "http.response.pathsend":
                self.response_complete = not message.get("more_body", False) and (
                    not self._expects_trailers
                )
            case "http.response.trailers":
                self.response_complete = not message.get("more_trailers", False)
        if self.response_complete:
            self._notify()

    async def feed_body(self, chunk: bytes, *, more_body: bool = True) -> None:
        while self._max_queue_size is not None and len(self._pending) >= self._max_queue_size:
            await self._wait()

        self._pending.append({"type": "http.request", "body": chunk, "more_body": more_body})
        self._notify()

    def disconnect(self) -> None:
        self._client_gone = True
        self._notify()

    @property
    def response_body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.sent if m["type"] == "http.response.body")

    @property
    def status(self) -> int | None:
        for message in self.sent:
            if message["type"] == "http.response.start":
                return int(message["status"])
        return None

    @property
    def undelivered(self) -> int:
        return len(self._pending)

    def _notify(self) -> None:
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def _wait(self) -> None:
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        await waiter

    async def _park(self) -> None:
        await asyncio.get_running_loop().create_future()


class ReceiveProbe:
    """
    Records the message types the app below pulls out of the channel.

    Installed under the middleware, it shows what a downstream reader such as
    Starlette's ``listen_for_disconnect`` actually got handed.
    """

    def __init__(self, app: ASGIApp, seen: list[str]) -> None:
        self.app = app
        self.seen = seen

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def probed() -> Message:
            message = await receive()
            self.seen.append(message["type"])
            return message

        await self.app(scope, probed, send)


async def run_app(
    app: ASGIApp,
    server: FakeServer,
    *,
    timeout: float = DEFAULT_TIMEOUT,  # noqa: ASYNC109
) -> bytes:
    """Drive the app to completion and return the response body."""
    async with asyncio.timeout(timeout):
        await app(server.scope, server.receive, server.send)

    return server.response_body


@asynccontextmanager
async def serve(
    app: ASGIApp,
    server: FakeServer,
    *,
    timeout: float = DEFAULT_TIMEOUT,  # noqa: ASYNC109
) -> AsyncIterator[asyncio.Task[None]]:
    """
    Run the app in a task so the test can interleave — disconnect mid-handler,
    cancel from outside. The task is awaited on a clean exit and reaped
    otherwise, so nothing leaks past the block.

    If the block fails while the app has already crashed, the app's exception is
    the more useful one: a test waiting for a signal that a dead app will never
    send would otherwise only report its own timeout.
    """
    task = asyncio.create_task(app(server.scope, server.receive, server.send))
    try:
        yield task
        if not task.done():
            async with asyncio.timeout(timeout):
                await task
    except BaseException:
        failure = _app_failure(task)
        if failure is not None:
            raise failure from None
        raise
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        elif not task.cancelled():
            task.exception()  # mark retrieved so nothing warns at GC


async def wait_for(event: asyncio.Event, *, timeout: float = DEFAULT_TIMEOUT) -> None:  # noqa: ASYNC109
    async with asyncio.timeout(timeout):
        await event.wait()


async def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = DEFAULT_TIMEOUT,  # noqa: ASYNC109
) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0)


async def read_body(receive: Receive) -> bytes:
    """Drain ``http.request`` messages the way a raw ASGI app would."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return b"".join(chunks)
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks)


async def respond(send: Send, body: bytes = b"ok", *, status: int = 200) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


def _app_failure(task: asyncio.Task[None]) -> BaseException | None:
    if not task.done() or task.cancelled():
        return None
    return task.exception()


def _frame(chunks: Sequence[bytes]) -> list[Message]:
    return [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]


def _build_scope(
    *,
    method: str,
    path: str,
    spec_version: str | None,
    content_length: int,
) -> Scope:
    scope: dict[str, Any] = dict(
        post_scope(content_length, path) if method != "GET" else http_scope(path)
    )
    scope["method"] = method
    if spec_version is None:
        scope.pop("asgi", None)
    else:
        scope["asgi"] = {"version": "3.0", "spec_version": spec_version}
    return scope
