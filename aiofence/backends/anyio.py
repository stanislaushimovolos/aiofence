from __future__ import annotations

import asyncio
from typing import Any

from anyio._backends._asyncio import CancelScope as _AsyncioCancelScope

from .abc import CancelBackend, CancelHandle


class AnyioBackend(CancelBackend):
    """
    Cancels through an `anyio.CancelScope` per fence. The default backend.

    anyio delivers the cancel only while the task is suspended on a pending
    future, retries every loop tick until the scope exits, and skips awaits
    inside a shielded child scope. Libraries written for anyio's model —
    httpx/httpcore, Starlette — therefore see the fence exactly as they see
    `anyio.fail_after`. See docs/architecture.md, "Cancel Backends".

    The fence's tightest timeout is advertised as the scope's deadline, so
    `anyio.current_effective_deadline()` below the fence reports it. The
    scope never fires on that deadline itself — the fence's own timer does,
    with its reason and through the policy. See `_AdvertisedScope`.

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

        self._scope = _AdvertisedScope()
        self._scope.__enter__()

    def cancel(self, message: str) -> None:
        self._scope.cancel(message)

    def set_deadline(self, when: float) -> None:
        self._scope.deadline = when

    def exit(self, exc_type: type[BaseException] | None, exc_val: BaseException | None) -> bool:
        return bool(self._scope.__exit__(exc_type, exc_val, None))


class _AdvertisedScope(_AsyncioCancelScope):
    """
    An asyncio-backend `anyio.CancelScope` whose deadline is read but never
    acted on. `_timeout` is anyio's only consumer that cancels on it; every
    other reader — `current_effective_deadline`, `checkpoint_if_cancelled` —
    only inspects it.
    """

    __slots__ = ()

    def _timeout(self) -> None:
        pass
