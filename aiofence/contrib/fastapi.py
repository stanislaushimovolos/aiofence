"""
FastAPI dependencies over the event ``DisconnectMiddleware`` publishes.

All of them require the middleware installed outermost — it owns the receive
channel and publishes the event they read. Without it they raise
``RuntimeError`` on the first request rather than watching the channel
themselves, which would steal messages from streaming responses and raw body
reads and would fire on response completion as well as on the client leaving.
See ``aiofence.contrib.starlette`` and docs/disconnect-watcher-analysis.md.

``DisconnectEvent`` is an ``asyncio.Event`` set once the client goes away, for
handlers that would rather pick their own stopping point than be interrupted::

    @app.get("/search")
    async def handler(gone: DisconnectEvent):
        hits = []
        for shard in shards:
            if gone.is_set():
                break
            hits += await query(shard)
        return hits

``DisconnectFencing`` is a ``Fencing`` carrying a disconnect trigger, also bound
as the ambient context so anything the handler calls picks it up from
``get_current_fencing()``::

    @app.get("/render")
    async def handler(fencing: DisconnectFencing):
        with fencing.timeout(30, code="budget").move_on_cancel() as fence:
            frames = await render_scene()

        if fence.cancelled_by("disconnect"):
            return Response(status_code=499)

Handlers that never touch the value should skip the parameter and declare
``dependencies=[Depends(disconnect_fencing)]`` on the route, router, or app.

Sync (``def``) handlers cannot enter a fence: ``Fence`` needs a running task and
FastAPI runs them in a worker thread.

Requires ``fastapi>=0.118`` (``pip install "aiofence[fastapi]"``); 0.106-0.117
tear yield dependencies down before the response is sent, which unbinds
``DisconnectFencing`` from the context for the whole streaming phase.

Plain Starlette and raw ASGI apps skip this module: install the middleware with
``fencing_code=...`` to bind app-wide, or read the event with
``get_disconnect_event()`` / ``require_disconnect_event()``, which take an
optional scope and otherwise answer for the ambient request.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends
from starlette.requests import Request

from aiofence import Fencing, bind_fencing, get_current_fencing

from .starlette import require_disconnect_event

_DEFAULT_CODE = "disconnect"


async def disconnect_event(request: Request) -> asyncio.Event:
    """
    Dependency that returns the request's ``asyncio.Event``, set on client
    disconnect.

    Usage::

        @app.get("/search")
        async def handler(gone: asyncio.Event = Depends(disconnect_event)):
            if gone.is_set():
                return partial

    Requires ``DisconnectMiddleware``; raises ``RuntimeError`` without it.
    """
    return require_disconnect_event(request.scope)


async def disconnect_fencing(request: Request) -> AsyncGenerator[Fencing]:
    """
    Dependency that cancels the current Fencing when the client disconnects.

    Registers the request's disconnect event on ``get_current_fencing()`` under
    ``code="disconnect"`` and binds the result as the active context, so
    anything the handler calls picks it up. For a different code use
    ``disconnect_fencing_dependency(code=...)``.

    Usage::

        @app.get("/render")
        async def handler(fencing: Fencing = Depends(disconnect_fencing)):
            with fencing.move_on_cancel() as fence:
                frames = await render_scene()
            if fence.cancelled_by("disconnect"):
                ...

    Requires ``DisconnectMiddleware``; raises ``RuntimeError`` without it.
    """
    async with _bound_fencing(request, _DEFAULT_CODE) as fencing:
        yield fencing


def disconnect_fencing_dependency(
    *,
    code: str = _DEFAULT_CODE,
) -> Callable[[Request], AsyncGenerator[Fencing]]:
    """
    Build a ``disconnect_fencing`` dependency that uses a custom code.

    This is the only way to set a custom code. ``disconnect_fencing`` takes no
    ``code`` argument on purpose: FastAPI would expose it as a client-settable
    query parameter on every fenced route.

    Layering two of these with different codes on one request is supported —
    they share the single per-request event, and every code is reported.

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


DisconnectEvent = Annotated[asyncio.Event, Depends(disconnect_event)]
DisconnectFencing = Annotated[Fencing, Depends(disconnect_fencing)]


@asynccontextmanager
async def _bound_fencing(request: Request, code: str) -> AsyncIterator[Fencing]:
    event = require_disconnect_event(request.scope)
    fencing = get_current_fencing().event(event, code=code)
    with bind_fencing(fencing):
        yield fencing
