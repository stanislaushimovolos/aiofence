from asyncio import Event

from constellate.base import (
    CancelReason,
    CancelSource,
    Context as _BaseContext,
    Guard,
    Scope,
    ScopeResult,
    SourceSet,
)
from constellate.sources import (
    EventCancelSource,
    EventGuard,
    EventTriggered,
    TimeoutExpired,
    TimeoutGuard,
    TimeoutSource,
)


class Context(_BaseContext):
    """Immutable description of cancellation sources, with builder methods."""

    def with_timeout(self, delay: float) -> "Context":
        return Context(*self.sources, TimeoutSource(delay))

    def with_cancel_on(self, event: Event) -> "Context":
        return Context(*self.sources, EventCancelSource(event))


__all__ = [
    "CancelReason",
    "CancelSource",
    "Context",
    "EventCancelSource",
    "EventGuard",
    "EventTriggered",
    "Guard",
    "Scope",
    "ScopeResult",
    "SourceSet",
    "TimeoutExpired",
    "TimeoutGuard",
    "TimeoutSource",
]
