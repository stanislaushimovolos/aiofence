"""
What ``DisconnectMiddleware`` publishes, and how anything below it reads back.

One ``asyncio.Event`` per request, in the ASGI scope for code holding a request
and in a ``ContextVar`` for code merely inside one. ``DISCONNECT_CODE`` is the
code it is bound under — import it instead of retyping ``"disconnect"``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from starlette.types import Scope

DISCONNECT_EVENT_SCOPE_KEY = "aiofence.disconnect_event"

DISCONNECT_CODE = "disconnect"


def get_disconnect_event(scope: Scope | None = None) -> asyncio.Event | None:
    """
    Return the request's disconnect event, or ``None`` when
    ``DisconnectMiddleware`` is not installed.

    Ambient by default, so code below the middleware can ask without being
    handed the request. Pass a ``scope`` when one is at hand — an exception
    handler, or a task the request did not spawn itself.

    Callers that cannot work without it use ``require_disconnect_event``.
    """
    event = _CURRENT_EVENT.get() if scope is None else scope.get(DISCONNECT_EVENT_SCOPE_KEY)
    return event if isinstance(event, asyncio.Event) else None


def require_disconnect_event(scope: Scope | None = None) -> asyncio.Event:
    """
    Return the request's disconnect event, or raise ``RuntimeError`` when
    ``DisconnectMiddleware`` is not installed.

    Same lookup as ``get_disconnect_event``. It raises rather than falling back
    to watching the channel itself, which would steal messages from the rest of
    the stack and could not tell a departed client from a completed response.
    """
    event = get_disconnect_event(scope)
    if event is None:
        raise RuntimeError(_MISSING_MIDDLEWARE)
    return event


@contextmanager
def _publish(scope: Scope, event: asyncio.Event) -> Iterator[None]:
    # Both copies dropped on exit — nothing may hold an event whose reader is gone.
    scope[DISCONNECT_EVENT_SCOPE_KEY] = event
    bound = _CURRENT_EVENT.set(event)
    try:
        yield
    finally:
        _CURRENT_EVENT.reset(bound)
        scope.pop(DISCONNECT_EVENT_SCOPE_KEY, None)


_CURRENT_EVENT: ContextVar[asyncio.Event | None] = ContextVar(
    "aiofence_disconnect_event", default=None
)

_MISSING_MIDDLEWARE = (
    "aiofence disconnect signalling requires DisconnectMiddleware. Install it "
    "outermost: FastAPI(middleware=[Middleware(DisconnectMiddleware)]) or, "
    "equivalently, as the last app.add_middleware(DisconnectMiddleware) call."
)
