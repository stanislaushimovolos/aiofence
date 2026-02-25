from asyncio import Event

from .core import (
    CancelReason,
    CancelType,
    Fence,
    Trigger,
    TriggerHandle,
)
from .triggers import (
    EventHandle,
    EventTrigger,
    TimeoutHandle,
    TimeoutTrigger,
)


class Context:
    """
    Immutable description of cancellation sources, with builder methods.
    """

    def __init__(self, *sources: Trigger) -> None:
        self.sources = sources

    def with_timeout(self, delay: float) -> "Context":
        return Context(*self.sources, TimeoutTrigger(delay))

    def with_cancel_on(self, event: Event) -> "Context":
        return Context(*self.sources, EventTrigger(event))

    def fence(self) -> Fence:
        return Fence(*self.sources)


__all__ = [
    "CancelReason",
    "CancelType",
    "EventHandle",
    "EventTrigger",
    "Fence",
    "TimeoutHandle",
    "TimeoutTrigger",
    "Trigger",
    "TriggerHandle",
]
