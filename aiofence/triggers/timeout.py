from __future__ import annotations

import asyncio

from aiofence.core import (
    CancelCallback,
    CancelReason,
    CancelType,
    Trigger,
    TriggerHandle,
)


class TimeoutHandle(TriggerHandle):
    def __init__(self, handle: asyncio.TimerHandle) -> None:
        self._handle = handle

    def disarm(self) -> None:
        self._handle.cancel()


class TimeoutTrigger(Trigger):
    def __init__(self, delay: float, *, code: str | None = None) -> None:
        self._delay = delay
        self._code = code

    def _reason(self) -> CancelReason:
        return CancelReason(
            message=f"timed out after {self._delay}s",
            cancel_type=CancelType.TIMEOUT,
            code=self._code,
        )

    def check(self) -> CancelReason | None:
        if self._delay <= 0:
            return self._reason()
        return None

    def arm(self, on_cancel: CancelCallback) -> TriggerHandle:
        loop = asyncio.get_running_loop()
        handle = loop.call_at(
            loop.time() + self._delay,
            on_cancel,
            self._reason(),
        )
        return TimeoutHandle(handle)
