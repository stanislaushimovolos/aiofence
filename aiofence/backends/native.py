from __future__ import annotations

import asyncio
from typing import Any

from .base import CancelHandle


class NativeBackend:
    """
    Cancels through asyncio's own protocol: one `task.cancel()`, balanced
    by one `uncancel()` on exit, with ownership settled by the
    `task.cancelling()` counter snapshot taken on entry.
    """

    def enter(self, task: asyncio.Task[Any]) -> CancelHandle:
        return _NativeHandle(task, task.cancelling())


class _NativeHandle:
    def __init__(self, task: asyncio.Task[Any], cancelling: int) -> None:
        self._task = task
        self._cancelling = cancelling
        self._delivered = False
        self._scheduled: asyncio.Handle | None = None

    def cancel(self, message: str) -> None:
        # 3.12: uncancel() doesn't clear _must_cancel, so task.cancel() from
        # inside the task's own step would force a spurious CancelledError at
        # the next await. Defer via call_soon so cancel() finds _fut_waiter
        # set and never touches _must_cancel.
        if asyncio.current_task() is self._task:
            loop = asyncio.get_running_loop()
            self._scheduled = loop.call_soon(self._deliver, message)
        else:
            self._deliver(message)

    def exit(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,  # noqa: ARG002 — the message matters to other backends
    ) -> bool:
        if not self._delivered:
            self._rescind()
            return False

        remaining = self._task.uncancel()
        return (
            remaining <= self._cancelling
            and exc_type is not None
            and issubclass(exc_type, asyncio.CancelledError)
        )

    def _deliver(self, message: str) -> None:
        self._delivered = True
        self._scheduled = None
        self._task.cancel(msg=message)

    def _rescind(self) -> None:
        if self._scheduled is not None:
            self._scheduled.cancel()
            self._scheduled = None
