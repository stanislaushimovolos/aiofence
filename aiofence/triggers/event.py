from __future__ import annotations

import asyncio
from asyncio import Event
from contextlib import suppress

from aiofence.core import (
    CancelCallback,
    CancelReason,
    CancelType,
    Trigger,
    TriggerHandle,
)


class EventTrigger(Trigger):
    def __init__(self, event: Event, *, code: str | None = None) -> None:
        self._event = event
        self._code = code

    def _reason(self) -> CancelReason:
        return CancelReason(
            message=f"event {self._event!r} triggered",
            cancel_type=CancelType.EVENT,
            code=self._code,
        )

    def check(self) -> CancelReason | None:
        if self._event.is_set():
            return self._reason()
        return None

    def arm(self, on_cancel: CancelCallback) -> TriggerHandle:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        reason = self._reason()
        handle = EventHandle(self._event, fut)
        fut.add_done_callback(lambda _: on_cancel(reason) if not handle.disarmed else None)
        self._event._waiters.append(fut)
        return handle


class EventHandle(TriggerHandle):
    def __init__(self, event: Event, fut: asyncio.Future[None]) -> None:
        self._event = event
        self._fut = fut
        self._disarmed = False

    @property
    def disarmed(self) -> bool:
        return self._disarmed

    def disarm(self) -> None:
        self._disarmed = True
        # Event.set() resolves futures but doesn't remove them from _waiters
        with suppress(ValueError):
            self._event._waiters.remove(self._fut)

        if not self._fut.done():
            self._fut.cancel()
