from __future__ import annotations

import asyncio
from asyncio import Event
from contextlib import suppress

from constellate.core import (
    CancelCallback,
    CancelReason,
    CancelType,
    Trigger,
    TriggerHandle,
)


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

    def arm(self, on_cancel: CancelCallback) -> TriggerHandle:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        reason = self._reason()
        # disarm() cancels the future — skip callback in that case
        fut.add_done_callback(
            lambda f: on_cancel(reason) if not f.cancelled() else None
        )
        self._event._waiters.append(fut)
        return EventHandle(self._event, fut)


class EventHandle(TriggerHandle):
    def __init__(self, event: Event, fut: asyncio.Future[None]) -> None:
        self._event = event
        self._fut = fut

    def disarm(self) -> None:
        # Event.set() resolves futures but doesn't remove them from _waiters
        with suppress(ValueError):
            self._event._waiters.remove(self._fut)

        if not self._fut.done():
            self._fut.cancel()
