from __future__ import annotations

import asyncio
from asyncio import Event
from typing import Callable

from ..core import CancelReason, CancelSource, Guard


class EventTriggered(CancelReason):
    def __init__(self, event: Event) -> None:
        self.event = event

    def __repr__(self) -> str:
        return f"EventTriggered({self.event!r})"


class EventGuard(Guard):
    def __init__(self, event: Event, fut: asyncio.Future) -> None:
        self._event = event
        self._fut = fut

    def disarm(self) -> None:
        if not self._fut.done():
            try:
                self._event._waiters.remove(self._fut)
            except ValueError:
                pass
            self._fut.cancel()


class EventCancelSource(CancelSource):
    def __init__(self, event: Event) -> None:
        self._event = event

    def check(self) -> CancelReason | None:
        if self._event.is_set():
            return EventTriggered(self._event)
        return None

    def arm(self, on_cancel: Callable[[CancelReason], None]) -> Guard:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        reason = EventTriggered(self._event)
        fut.add_done_callback(
            lambda f: on_cancel(reason) if not f.cancelled() else None
        )
        self._event._waiters.append(fut)
        return EventGuard(self._event, fut)
