"""
FastAPI dependencies over the event ``DisconnectMiddleware`` publishes.

``DisconnectEvent`` is the event itself, for handlers that pick their own
stopping point; ``DisconnectFencing`` is a ``Fencing`` carrying it, for handlers
that want to be cancelled. Both are optional — the middleware already binds the
fencing app-wide; these exist for per-route codes and explicit wiring.

All require the middleware installed outermost, and raise ``RuntimeError``
without it. Requires ``fastapi>=0.118``: 0.106-0.117 tear yield dependencies
down before the response is sent, unbinding the fencing for the streaming phase.

Usage, layering and limitations: docs/api.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends
from starlette.requests import Request

from aiofence import Fencing, bind_fencing, get_current_fencing

from .starlette import DISCONNECT_CODE, require_disconnect_event


async def disconnect_event(request: Request) -> asyncio.Event:
    """
    Dependency that returns the request's ``asyncio.Event``, set on client
    disconnect. ``DisconnectEvent`` is the ``Annotated`` alias for it.

    Requires ``DisconnectMiddleware``; raises ``RuntimeError`` without it.
    """
    return require_disconnect_event(request.scope)


async def disconnect_fencing(request: Request) -> AsyncGenerator[Fencing]:
    """
    Dependency that cancels the current Fencing when the client disconnects.

    Registers the request's disconnect event on ``get_current_fencing()`` under
    ``DISCONNECT_CODE`` and binds the result as the active context. Same event
    and code the middleware already binds, so the entries dedupe onto one; for
    a different code use ``disconnect_fencing_dependency(code=...)``.

    Requires ``DisconnectMiddleware``; raises ``RuntimeError`` without it.
    """
    async with _bound_fencing(request, DISCONNECT_CODE) as fencing:
        yield fencing


def disconnect_fencing_dependency(
    *,
    code: str = DISCONNECT_CODE,
) -> Callable[[Request], AsyncGenerator[Fencing]]:
    """
    Build a ``disconnect_fencing`` dependency that uses a custom code, e.g.
    ``Depends(disconnect_fencing_dependency(code="client_gone"))``.

    The only way to set one: a ``code`` kwarg on ``disconnect_fencing`` itself
    would become a client-settable query parameter on every fenced route.

    Layering several is supported — one event, every code reported.
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
