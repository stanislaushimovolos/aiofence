"""
Framework integrations. Requires ``starlette``; never imported by the core package.
"""

from .starlette import (
    DISCONNECT_CODE,
    DISCONNECT_EVENT_SCOPE_KEY,
    DisconnectMiddleware,
    get_disconnect_event,
    require_disconnect_event,
)

__all__ = [
    "DISCONNECT_CODE",
    "DISCONNECT_EVENT_SCOPE_KEY",
    "DisconnectMiddleware",
    "get_disconnect_event",
    "require_disconnect_event",
]
