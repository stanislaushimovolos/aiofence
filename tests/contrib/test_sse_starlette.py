"""
sse-starlette's EventSourceResponse runs its own receive loop, so it competes
with the disconnect watcher for the same channel. See
docs/receive-channel-conflicts.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress

import pytest
from fastapi import Depends, FastAPI
from sse_starlette.sse import EventSourceResponse

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


async def test__sse_response__when_client_disconnects__then_fence_cancelled() -> None:
    app = FastAPI()
    observed: list[bool] = []

    @app.get("/work", dependencies=[Depends(disconnect_fencing)])
    async def work() -> EventSourceResponse:
        async def events() -> AsyncIterator[str]:
            yield "first"
            with get_current_fencing().move_on_cancel() as fence:
                await asyncio.sleep(10)
            observed.append(fence.cancelled_by("disconnect"))
            yield "second"

        return EventSourceResponse(events(), ping=60)

    body = await call_app_body(app, receive=scripted_receive({"type": "http.disconnect"}))

    assert observed == [True]
    assert b"data: first" in body
    assert b"data: second" in body
