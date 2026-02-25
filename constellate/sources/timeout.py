from __future__ import annotations

import asyncio
from typing import Callable

from ..core import CancelReason, CancelSource, Guard


class TimeoutExpired(CancelReason):
    def __init__(self, delay: float) -> None:
        self.delay = delay

    def exception(self) -> TimeoutError:
        return TimeoutError(f"timed out after {self.delay}s")

    def __repr__(self) -> str:
        return f"TimeoutExpired({self.delay})"


class TimeoutGuard(Guard):
    def __init__(self, handle: asyncio.TimerHandle) -> None:
        self._handle = handle

    def disarm(self) -> None:
        self._handle.cancel()


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
