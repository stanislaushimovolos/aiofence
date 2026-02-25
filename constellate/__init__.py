from asyncio import Event

from .core import (
    Cancellation,
    CancelReason,
    CancelSource,
    CancelType,
    Guard,
    Scope,
)
from .core import (
    Context as _BaseContext,
)
from .sources import (
    EventCancelSource,
    EventGuard,
    TimeoutGuard,
    TimeoutSource,
)


class Context(_BaseContext):
    """
    Immutable description of cancellation sources, with builder methods.
    """

    def with_timeout(self, delay: float) -> "Context":
        return Context(*self.sources, TimeoutSource(delay))

    def with_cancel_on(self, event: Event) -> "Context":
        return Context(*self.sources, EventCancelSource(event))


__all__ = [
    "CancelReason",
    "CancelSource",
    "CancelType",
    "Cancellation",
    "Context",
    "EventCancelSource",
    "EventGuard",
    "Guard",
    "Scope",
    "TimeoutGuard",
    "TimeoutSource",
]
