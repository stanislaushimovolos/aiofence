from __future__ import annotations

import asyncio
from typing import Any

import anyio

from .abc import CancelBackend, CancelHandle


class AnyioBackend(CancelBackend):
    """
    Cancels through an `anyio.CancelScope` per fence.

    anyio delivers the cancel only while the task is suspended on a pending
    future, retries every loop tick until the scope exits, and skips awaits
    inside a shielded child scope. Libraries written for anyio's model —
    httpx/httpcore, Starlette — therefore see the fence exactly as they see
    `anyio.fail_after`. See docs/architecture.md, "Cancel Backends".
    """

    def enter(self, task: asyncio.Task[Any]) -> CancelHandle:
        return _ScopeHandle(task)


class _ScopeHandle(CancelHandle):
    def __init__(self, task: asyncio.Task[Any]) -> None:
        if asyncio.current_task() is not task:
            raise RuntimeError("AnyioBackend must be entered from the task it cancels")

        self._task = task
        self._cancelling = task.cancelling()
        self._scope = anyio.CancelScope()
        self._scope.__enter__()

    def cancel(self, message: str) -> None:
        self._scope.cancel(message)

    def exit(self, exc_type: type[BaseException] | None, exc_val: BaseException | None) -> bool:
        # anyio settles ownership against its own scope tree and message, not
        # asyncio's counter. A TaskGroup or asyncio.timeout that cancelled the
        # task meanwhile is invisible to it, so keep the counter rule on top:
        # anything still outstanding above our baseline is theirs to handle.
        caught = bool(self._scope.__exit__(exc_type, exc_val, None))
        return caught and self._task.cancelling() <= self._cancelling
