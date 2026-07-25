"""
sse-starlette's EventSourceResponse runs its own receive loop, so it competes
with the disconnect watcher for the same channel. See docs/api.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress

import pytest
from fastapi import Depends, FastAPI
from sse_starlette.sse import EventSourceResponse
from starlette.types import Message

from aiofence import get_current_fencing
from aiofence.contrib.starlette import disconnect_fencing

from .asgi_harness import call_app_body, scripted_receive


@pytest.fixture(autouse=True)
async def _drop_sse_shutdown_watcher() -> AsyncIterator[None]:
    """
    sse-starlette starts one shutdown watcher per thread that outlives the
    request by design. Cancel it so the suite-wide asyncio invariant check
    stays per-test; the watcher's own finally clears its started flag.
    """
    before = asyncio.all_tasks()

    yield

    for task in asyncio.all_tasks() - before:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def sse_app(*, fenced: bool, closed: list[str], observed: list[bool]) -> FastAPI:
    app = FastAPI()

    async def on_close(_: Message) -> None:
        closed.append("closed")

    @app.get("/work", dependencies=[Depends(disconnect_fencing)] if fenced else [])
    async def work() -> EventSourceResponse:
        async def events() -> AsyncIterator[str]:
            yield "first"
            with get_current_fencing().move_on_cancel() as fence:
                await asyncio.sleep(10)
            observed.append(fence.cancelled_by("disconnect"))
            yield "second"

        return EventSourceResponse(events(), client_close_handler_callable=on_close, ping=60)

    return app


async def test__sse_response__when_fenced__then_fence_observes_disconnect() -> None:
    closed: list[str] = []
    observed: list[bool] = []
    app = sse_app(fenced=True, closed=closed, observed=observed)

    body = await call_app_body(app, receive=scripted_receive({"type": "http.disconnect"}))

    assert observed == [True]
    assert b"data: first" in body
    assert b"data: second" in body


async def test__sse_response__when_fenced__then_sse_close_handler_never_fires() -> None:
    """Known limitation: the watcher consumes the disconnect before sse-starlette sees it."""
    closed: list[str] = []
    observed: list[bool] = []
    app = sse_app(fenced=True, closed=closed, observed=observed)

    await call_app_body(app, receive=scripted_receive({"type": "http.disconnect"}))

    assert closed == []


async def test__sse_response__when_unfenced__then_sse_close_handler_fires() -> None:
    """Control for the test above — sse-starlette handles disconnect fine on its own."""
    closed: list[str] = []
    observed: list[bool] = []
    app = sse_app(fenced=False, closed=closed, observed=observed)

    await call_app_body(app, receive=scripted_receive({"type": "http.disconnect"}))

    assert closed == ["closed"]
