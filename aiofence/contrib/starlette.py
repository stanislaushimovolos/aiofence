from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress

from starlette.requests import Request

from aiofence import Fencing, bind_fencing, get_current_fencing

_SCOPE_KEY = "aiofence.disconnect_event"


async def disconnect_event(request: Request) -> AsyncGenerator[asyncio.Event]:
    """
    FastAPI dependency that yields an ``asyncio.Event`` set on client disconnect.

    Usage::

        @app.get("/search")
        async def handler(gone: asyncio.Event = Depends(disconnect_event)):
            if gone.is_set():
                return partial

    Holding this event means holding the ASGI receive channel, which rules out
    raw body reads and streaming responses. See the caveats in docs/api.md.
    """
    async with _shared_disconnect_event(request) as event:
        yield event


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

        @app.get("/render")
        async def handler(fencing: Fencing = Depends(disconnect_fencing)):
            with fencing.move_on_cancel() as fence:
                frames = await render_scene()
            if fence.cancelled_by("disconnect"):
                ...

    Holding this fencing means holding the ASGI receive channel, which rules
    out raw body reads and streaming responses. See the caveats in docs/api.md.
    """
    async with _bound_fencing(request, code) as fencing:
        yield fencing


def disconnect_fencing_dependency(
    *,
    code: str = "disconnect",
) -> Callable[[Request], AsyncGenerator[Fencing]]:
    """
    Build a ``disconnect_fencing`` dependency that uses a custom code.

    Carries the same caveats as ``disconnect_fencing``.

    Usage::

        ClientGone = Depends(disconnect_fencing_dependency(code="client_gone"))

        @app.get("/render")
        async def handler(fencing: Fencing = ClientGone):
            ...
    """

    async def dependency(request: Request) -> AsyncGenerator[Fencing]:
        async with _bound_fencing(request, code) as fencing:
            yield fencing

    return dependency


@asynccontextmanager
async def _bound_fencing(request: Request, code: str) -> AsyncIterator[Fencing]:
    async with _shared_disconnect_event(request) as event:
        fencing = get_current_fencing().event(event, code=code)
        with bind_fencing(fencing):
            yield fencing


@asynccontextmanager
async def _shared_disconnect_event(request: Request) -> AsyncIterator[asyncio.Event]:
    """
    Per-request disconnect event, cached in the ASGI scope.

    Whoever asks first opens the watch loop and stashes the event; everyone
    after that gets the same object back. Keeping it to a single loop matters:
    parallel readers on one channel divide the messages between them, so
    neither is guaranteed to see the disconnect.
    """
    existing: asyncio.Event | None = request.scope.get(_SCOPE_KEY)
    if existing is not None:
        yield existing
        return

    event = asyncio.Event()
    request.scope[_SCOPE_KEY] = event
    listener = asyncio.create_task(_listen_disconnect(request, event))

    try:
        yield event
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
