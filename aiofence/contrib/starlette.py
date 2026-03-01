from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress

from starlette.requests import Request

from aiofence import Fencing, bind_fencing


async def disconnect_fencing(
    request: Request,
    *,
    code: str = "disconnect",
) -> AsyncGenerator[Fencing]:
    """
    FastAPI dependency that cancels the current Fencing when the client disconnects.

    Usage::

        @app.get("/stream")
        async def handler(fencing: Fencing = Depends(disconnect_fencing)):
            with fencing.move_on_cancel() as fence:
                await long_work()
            if fence.cancelled_by("disconnect"):
                ...
    """
    disconnect = asyncio.Event()
    listener = asyncio.create_task(_listen_disconnect(request, disconnect))
    fencing = Fencing.current().event(disconnect, code=code)

    with bind_fencing(fencing):
        try:
            yield fencing
        finally:
            listener.cancel()
            await asyncio.shield(_quiet_await(listener))


async def _listen_disconnect(request: Request, event: asyncio.Event) -> None:
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            event.set()
            return


async def _quiet_await(task: asyncio.Task[None]) -> None:
    with suppress(asyncio.CancelledError):
        await task
