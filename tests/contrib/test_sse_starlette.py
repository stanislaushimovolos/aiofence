"""
D7 on the fallback path — no ``DisconnectMiddleware`` installed.

The dependency's watcher is then a rival reader of the same channel that
``EventSourceResponse`` reads unconditionally, and it is parked first. On a
server that delivers ``http.disconnect`` once, the watcher consumes it: the
fencing fires, and sse-starlette's own close handling never runs.

``tests/contrib/test_middleware_sse.py`` covers the same endpoint with the
middleware installed, where both readers are told.
See docs/disconnect-watcher-analysis.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi import Depends, FastAPI
from sse_starlette.sse import EventSourceResponse
from starlette.types import Message

from aiofence import get_current_fencing
from aiofence.contrib.starlette import disconnect_fencing

from .asgi_harness import call_app_body, scripted_receive

pytestmark = pytest.mark.usefixtures("drop_sse_shutdown_watcher")


def sse_app(fences_cancelled: list[bool], closed: list[str]) -> FastAPI:
    app = FastAPI()

    async def on_close(message: Message) -> None:
        closed.append(message["type"])

    @app.get("/work", dependencies=[Depends(disconnect_fencing)])
    async def work() -> EventSourceResponse:
        async def events() -> AsyncIterator[str]:
            yield "first"
            with get_current_fencing().move_on_cancel() as fence:
                await asyncio.sleep(10)
            fences_cancelled.append(fence.cancelled_by("disconnect"))
            yield "second"

        return EventSourceResponse(events(), ping=60, client_close_handler_callable=on_close)

    return app


async def test__sse_response__when_no_middleware__then_fence_still_cancelled() -> None:
    cancelled: list[bool] = []
    app = sse_app(cancelled, [])

    body = await call_app_body(app, receive=scripted_receive({"type": "http.disconnect"}))

    assert cancelled == [True]
    assert b"data: first" in body
    assert b"data: second" in body


async def test__sse_response__when_no_middleware__then_close_handler_never_fires() -> None:
    """The watcher took the only disconnect, so the response's listener parks."""
    closed: list[str] = []
    app = sse_app([], closed)

    await call_app_body(app, receive=scripted_receive({"type": "http.disconnect"}))

    assert closed == []
