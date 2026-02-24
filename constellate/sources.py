from __future__ import annotations

import asyncio
from asyncio import Event
from typing import Callable

from constellate.base import CancelReason, CancelSource, Guard


class TimeoutExpired(CancelReason):
    def __init__(self, delay: float) -> None:
        self.delay = delay

    def exception(self) -> TimeoutError:
        return TimeoutError(f"timed out after {self.delay}s")

    def __repr__(self) -> str:
        return f"TimeoutExpired({self.delay})"


class EventTriggered(CancelReason):
    def __init__(self, event: Event) -> None:
        self.event = event

    def __repr__(self) -> str:
        return f"EventTriggered({self.event!r})"


class TimeoutGuard(Guard):
    def __init__(self, handle: asyncio.TimerHandle) -> None:
        self._handle = handle

    def disarm(self) -> None:
        self._handle.cancel()


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


class TimeoutSource(CancelSource):
    def __init__(self, delay: float) -> None:
        self._delay = delay

    def check(self) -> CancelReason | None:
        if self._delay <= 0:
            return TimeoutExpired(self._delay)
        return None

    def arm(self, on_cancel: Callable[[CancelReason], None]) -> Guard:
        loop = asyncio.get_running_loop()
        handle = loop.call_at(
            loop.time() + self._delay,
            on_cancel,
            TimeoutExpired(self._delay),
        )
        return TimeoutGuard(handle)


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
        fut.add_done_callback(lambda f: on_cancel(reason) if not f.cancelled() else None)
        self._event._waiters.append(fut)
        return EventGuard(self._event, fut)
