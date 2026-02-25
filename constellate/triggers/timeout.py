from __future__ import annotations

import asyncio
from collections.abc import Callable

from constellate.core import CancelReason, CancelType, Trigger, TriggerHandle


class TimeoutHandle(TriggerHandle):
    def __init__(self, handle: asyncio.TimerHandle) -> None:
        self._handle = handle

    def disarm(self) -> None:
        self._handle.cancel()


class TimeoutTrigger(Trigger):
    def __init__(self, delay: float) -> None:
        self._delay = delay

    def _reason(self) -> CancelReason:
        return CancelReason(
            message=f"timed out after {self._delay}s",
            cancel_type=CancelType.TIMEOUT,
        )

    def check(self) -> CancelReason | None:
        if self._delay <= 0:
            return self._reason()
        return None

    def arm(self, on_cancel: Callable[[CancelReason], None]) -> TriggerHandle:
        loop = asyncio.get_running_loop()
        reason = self._reason()
        handle = loop.call_at(
            loop.time() + self._delay,
            on_cancel,
            reason,
        )
        return TimeoutHandle(handle)
