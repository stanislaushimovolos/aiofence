"""
D7 — sse-starlette's ``EventSourceResponse`` reads the channel unconditionally.

With the middleware replaying the disconnect, its listener and our fencing must
both fire: the close handler runs, pings stop, the generator aborts, *and*
``cancelled_by("disconnect")`` is True. The pre-middleware dependency starved
that listener — no close handler, generator running to completion — which is
what these expectations replace.
See docs/disconnect-watcher-analysis.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from sse_starlette.sse import EventSourceResponse
from starlette.types import Message

from aiofence import Fence, get_current_fencing
from aiofence.contrib.starlette import DisconnectMiddleware

from .server_harness import FakeServer, run_app, serve, wait_for

PING_INTERVAL = 0.02

pytestmark = pytest.mark.usefixtures("drop_sse_shutdown_watcher")


class SseProbe:
    """
    One SSE endpoint plus everything the test needs to observe about it.

    ``fenced`` picks the generator shape: one that opens a fence around its slow
    step — and so deliberately resumes after cancellation — or a plain one that
    only sse-starlette's own listener can stop.
    """

    def __init__(self, *, fenced: bool = True) -> None:
        self.fenced = fenced
        self.streaming = asyncio.Event()
        self.fences: list[Fence] = []
        self.closed: list[str] = []
        self.completed: list[bool] = []
        self.app = self._build_app()

    def _build_app(self) -> FastAPI:
        app = FastAPI()
        app.add_middleware(DisconnectMiddleware)

        async def on_close(message: Message) -> None:
            self.closed.append(message["type"])

        @app.get("/work")
        async def work() -> EventSourceResponse:
            return EventSourceResponse(
                self._events(),
                ping=PING_INTERVAL,
                client_close_handler_callable=on_close,
            )

        return app

    async def _events(self) -> AsyncIterator[str]:
        yield "first"
        if self.fenced:
            with get_current_fencing().move_on_cancel() as fence:
                self.fences.append(fence)
                self.streaming.set()
                await asyncio.sleep(10)
        else:
            self.streaming.set()
            await asyncio.sleep(10)
        self.completed.append(True)
        yield "second"


async def run_until_disconnect(probe: SseProbe, server: FakeServer) -> None:
    async with serve(probe.app, server):
        await wait_for(probe.streaming)
        server.disconnect()


async def test__sse_response__when_client_disconnects__then_close_handler_fires() -> None:
    probe, server = SseProbe(), FakeServer()

    await run_until_disconnect(probe, server)

    assert probe.closed == ["http.disconnect"]


async def test__sse_response__when_client_disconnects__then_fencing_sees_it() -> None:
    probe, server = SseProbe(), FakeServer()

    await run_until_disconnect(probe, server)

    assert probe.fences[0].cancelled_by("disconnect")


async def test__sse_response__when_unfenced_generator__then_aborted_on_disconnect() -> None:
    """Nothing suppresses the cancel, so sse-starlette's listener stops the stream."""
    probe, server = SseProbe(fenced=False), FakeServer()

    await run_until_disconnect(probe, server)

    assert probe.completed == []
    assert b"data: second" not in server.response_body


@pytest.mark.backend("native")  # anyio defers to the listener's cancelled task group
async def test__sse_response__when_fenced_generator__then_resumes_for_its_last_chunk() -> None:
    """
    ``move_on_cancel`` hands control back, so the generator emits its final
    chunk instead of being torn down mid-await.
    """
    probe, server = SseProbe(), FakeServer()

    await run_until_disconnect(probe, server)

    assert probe.completed == [True]
    assert b"data: second" in server.response_body


async def test__sse_response__when_client_disconnects__then_pings_stop() -> None:
    probe, server = SseProbe(), FakeServer()

    await run_until_disconnect(probe, server)
    pings_before = _ping_frames(server)
    await asyncio.sleep(PING_INTERVAL * 3)

    assert _ping_frames(server) == pings_before
    assert pings_before <= 1


async def test__sse_response__when_client_never_leaves__then_close_handler_silent() -> None:
    probe, server = SseProbe(), FakeServer()

    async with serve(probe.app, server) as task:
        await wait_for(probe.streaming)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert probe.closed == []


async def test__sse_response__when_stream_completes__then_every_event_reaches_the_server() -> None:
    """
    The disconnect cases dominate this file; nothing else proves an SSE stream
    that nobody interrupts still arrives intact through the wrapped ``send``.
    """
    app = FastAPI()
    app.add_middleware(DisconnectMiddleware)

    async def events() -> AsyncIterator[str]:
        for item in ("first", "second", "third"):
            yield item

    @app.get("/work")
    async def work() -> EventSourceResponse:
        return EventSourceResponse(events())

    server = FakeServer()
    await run_app(app, server)

    assert _data_frames(server) == [b"data: first", b"data: second", b"data: third"]


def _ping_frames(server: FakeServer) -> int:
    return sum(
        1
        for message in server.sent
        if message["type"] == "http.response.body" and message.get("body", b"").startswith(b":")
    )


def _data_frames(server: FakeServer) -> list[bytes]:
    return [line for line in server.response_body.splitlines() if line.startswith(b"data:")]
