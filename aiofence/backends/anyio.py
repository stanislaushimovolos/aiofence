from __future__ import annotations

import asyncio
from typing import Any

import anyio

from .abc import CancelBackend, CancelHandle


class AnyioBackend(CancelBackend):
    """
    Cancels through an `anyio.CancelScope` per fence. The default backend.

    anyio delivers the cancel only while the task is suspended on a pending
    future, retries every loop tick until the scope exits, and skips awaits
    inside a shielded child scope. Libraries written for anyio's model —
    httpx/httpcore, Starlette — therefore see the fence exactly as they see
    `anyio.fail_after`. See docs/architecture.md, "Cancel Backends".

    Nested fences map onto nested scopes: anyio links them on its own
    per-task stack, so an inner fence backs off whenever an outer one has
    fired, and cleanup inside a cancelled outer is re-cancelled at every
    await. Scopes must exit in the order they were entered, on the task
    that entered them — a fence spanning a `yield` breaks that. See
    https://anyio.readthedocs.io/en/stable/cancellation.html#avoiding-cancel-scope-stack-corruption
    """

    def enter(self, task: asyncio.Task[Any]) -> CancelHandle:
        return _ScopeHandle(task)

    def enter_nested(self, task: asyncio.Task[Any]) -> CancelHandle:
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
