"""
Starlette / ASGI integration: one reader for the request's receive channel.

``.middleware`` holds ``DisconnectMiddleware``, which owns the channel and
publishes the request's disconnect event. ``.api`` holds what it published —
the constants and the two readers — for everything below it.

Import from this package; the split is internal.
"""

from .api import (
    DISCONNECT_CODE,
    DISCONNECT_EVENT_SCOPE_KEY,
    get_disconnect_event,
    require_disconnect_event,
)
from .middleware import DisconnectCallback, DisconnectMiddleware, WatchPredicate

__all__ = [
    "DISCONNECT_CODE",
    "DISCONNECT_EVENT_SCOPE_KEY",
    "DisconnectCallback",
    "DisconnectMiddleware",
    "WatchPredicate",
    "get_disconnect_event",
    "require_disconnect_event",
]
