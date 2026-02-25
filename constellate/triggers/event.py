from __future__ import annotations

import asyncio
from asyncio import Event
from collections.abc import Callable

from constellate.core import CancelReason, CancelType, Trigger, TriggerHandle


class EventHandle(TriggerHandle):
    def __init__(self, event: Event, fut: asyncio.Future[None]) -> None:
        self._event = event
        self._fut = fut

    def disarm(self) -> None:
        if not self._fut.done():
            self._event._waiters.remove(self._fut)
            self._fut.cancel()


class EventTrigger(Trigger):
    def __init__(self, event: Event) -> None:
        self._event = event

    def _reason(self) -> CancelReason:
        return CancelReason(
            message=f"event {self._event!r} triggered",
            cancel_type=CancelType.CANCELLED,
        )

    def check(self) -> CancelReason | None:
        if self._event.is_set():
            return self._reason()
        return None

    def arm(self, on_cancel: Callable[[CancelReason], None]) -> TriggerHandle:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        reason = self._reason()
        fut.add_done_callback(
            lambda f: on_cancel(reason) if not f.cancelled() else None
        )
        self._event._waiters.append(fut)
        return EventHandle(self._event, fut)
