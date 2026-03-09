from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress

from starlette.requests import Request

from aiofence import Fencing, bind_fencing, get_current_fencing


async def disconnect_event(request: Request) -> AsyncGenerator[asyncio.Event]:
    """
    FastAPI dependency that yields an ``asyncio.Event`` set on client disconnect.

    Usage::

        @app.get("/stream")
        async def handler(event: asyncio.Event = Depends(disconnect_event)):
            await event.wait()
    """
    event = asyncio.Event()
    listener = asyncio.create_task(_listen_disconnect(request, event))
    try:
        yield event
    finally:
        listener.cancel()
        await asyncio.shield(_quiet_await(listener))


async def disconnect_fencing(
    request: Request,
    *,
    code: str = "disconnect",
) -> AsyncGenerator[Fencing]:
    """
    FastAPI dependency that cancels the current Fencing when the client disconnects.

    Builds on ``disconnect_event`` — adds the event to ``get_current_fencing()``
    and binds it as the active context.

    Usage::

        @app.get("/stream")
        async def handler(fencing: Fencing = Depends(disconnect_fencing)):
            with fencing.move_on_cancel() as fence:
                await long_work()
            if fence.cancelled_by("disconnect"):
                ...
    """
    async for event in disconnect_event(request):
        fencing = get_current_fencing().event(event, code=code)
        with bind_fencing(fencing):
            yield fencing


async def _listen_disconnect(request: Request, event: asyncio.Event) -> None:
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            event.set()
            return


async def _quiet_await(task: asyncio.Task[None]) -> None:
    with suppress(asyncio.CancelledError):
        await task
