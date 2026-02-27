from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Self


class CancelType(Enum):
    CANCELLED = auto()
    TIMEOUT = auto()


@dataclass(frozen=True, kw_only=True, slots=True)
class CancelReason:
    message: str
    cancel_type: CancelType


CancelCallback = Callable[[CancelReason], None]


class Trigger(ABC):
    """
    Defines a cancellation condition.

    `check()` — synchronous pre-check; if the condition is already met,
    cancellation is scheduled immediately without arming.

    `arm(callback)` — starts async monitoring (callbacks, timers, etc).
    Returns a `TriggerHandle` responsible for cleanup.
    """

    @abstractmethod
    def check(self) -> CancelReason | None: ...

    @abstractmethod
    def arm(self, on_cancel: CancelCallback) -> TriggerHandle: ...


class TriggerHandle(ABC):
    """
    A live cancellation watch returned by `Trigger.arm()`.

    `disarm()` — stops monitoring and cleans up resources.
    """

    @abstractmethod
    def disarm(self) -> None: ...


class Fence:
    """
    Sync context manager that arms triggers against the current task.

    Suppression semantics (follows anyio CancelScope model):
    __exit__ always suppresses CancelledError — never raises, never
    propagates. Caller inspects `fence.cancelled` / `fence.reasons` after
    the block. This keeps the cancel counter balanced and avoids
    CancelledError-with-counter-zero, which would confuse TaskGroup
    and nested asyncio.timeout scopes.
    """

    def __init__(self, *triggers: Trigger) -> None:
        self._triggers = triggers
        self._current_task: asyncio.Task[Any] | None = None
        self._exit_handlers: list[TriggerHandle] = []
        self._cancel_reasons: list[CancelReason] = []
        self._cancel_token: _CancelToken | None = None
        self._cancelling: int | None = None
        self._armed = False
        self._exited = False

    @property
    def cancelled(self) -> bool:
        return len(self._cancel_reasons) > 0

    @property
    def reasons(self) -> tuple[CancelReason, ...]:
        return tuple(self._cancel_reasons)

    def __enter__(self) -> Self:
        if self._exited or self._current_task is not None:
            raise RuntimeError("Fence cannot be reused")

        task = asyncio.current_task()
        assert task is not None  # noqa: S101
        self._current_task = task
        self._cancelling = task.cancelling()

        for source in self._triggers:
            reason = source.check()
            if reason is not None:
                self._cancel_reasons.append(reason)

        if self._cancel_reasons:
            self._cancel()
            return self

        self._exit_handlers = [source.arm(self._request_cancellation) for source in self._triggers]
        self._armed = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> bool:
        self._exited = True
        if self._armed:
            for guard in self._exit_handlers:
                guard.disarm()

        if self._cancel_token is None:
            return False

        return self._cancel_token.resolve(exc_type)

    def _request_cancellation(self, reason: CancelReason) -> None:
        self._cancel_reasons.append(reason)
        self._cancel()

    def _cancel(self) -> None:
        # task was already canceled, prevent double cancellation
        if self._cancel_token is not None:
            return

        if self._current_task is None or self._cancelling is None:
            raise RuntimeError("Fence._cancel() called before __enter__")

        self._cancel_token = _CancelToken.schedule(
            self._current_task, self._cancel_reasons[0].message, self._cancelling
        )


class _CancelToken:
    """
    Encapsulates one cancel/uncancel cycle using asyncio's counter protocol.

    Defers `task.cancel()` via `call_soon` to avoid setting `_must_cancel`
    when called from within the task's own synchronous execution.
    """

    def __init__(
        self,
        task: asyncio.Task[Any],
        cancelling: int,
    ) -> None:
        self._task = task
        self._cancelling = cancelling
        self._fired = False
        self._handle: asyncio.Handle | None = None

    @classmethod
    def schedule(
        cls,
        task: asyncio.Task[Any],
        msg: str,
        cancelling: int,
    ) -> _CancelToken:
        token = cls(task, cancelling)
        token._handle = asyncio.get_running_loop().call_soon(token._fire, msg)
        return token

    def resolve(self, exc_type: type[BaseException] | None) -> bool:
        """
        Balance the cancel counter and decide whether to suppress.

        Returns True to suppress the exception (CancelledError is ours),
        False to let it propagate.
        """

        # body completed before cancel was delivered — rescind
        if not self._fired:
            if self._handle is not None:
                self._handle.cancel()
                self._handle = None
            return False  # nothing to suppress

        remaining = self._task.uncancel()
        # suppress our CancelledError, propagate everything else
        return (
            remaining <= self._cancelling
            and exc_type is not None
            and issubclass(exc_type, asyncio.CancelledError)
        )

    def _fire(self, msg: str) -> None:
        self._fired = True
        self._handle = None
        self._task.cancel(msg=msg)
