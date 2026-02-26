from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Self

from .errors import FenceCancelled, FenceTimeout


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

    On enter — checks pre-conditions, then arms all triggers.
    On exit — disarms triggers and resolves any cancellation.
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
        if self._exited:
            raise RuntimeError("Fence cannot be reused")
        if self._current_task is not None:
            raise RuntimeError("Fence has already been entered")

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Fence must be used inside a task")

        self._current_task = task
        self._cancelling = task.cancelling()

        for source in self._triggers:
            reason = source.check()
            if reason is not None:
                self._cancel_reasons.append(reason)

        if self._cancel_reasons:
            reason = self._cancel_reasons[0]
            if reason.cancel_type is CancelType.TIMEOUT:
                raise FenceTimeout(self.reasons)

            raise FenceCancelled(self.reasons)

        self._exit_handlers = [
            source.arm(self._request_cancellation) for source in self._triggers
        ]
        self._armed = True

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        self._exited = True
        if self._armed:
            for guard in self._exit_handlers:
                guard.disarm()

        # no cancellation occurred
        if self._cancel_token is None:
            return

        self._cancel_token.resolve(exc_type, exc_val, self.reasons)

    def _request_cancellation(self, reason: CancelReason) -> None:
        self._cancel_reasons.append(reason)
        self._cancel()

    def _cancel(self) -> None:
        if self._cancel_token is not None:
            return

        if self._current_task is None or self._cancelling is None:
            raise RuntimeError("Fence._cancel() called before __enter__")

        self._cancel_token = _CancelToken.schedule(
            self._current_task, self._cancel_reasons[0], self._cancelling
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
        reason: CancelReason,
        cancelling: int,
    ) -> None:
        self._task = task
        self._reason = reason
        self._cancelling = cancelling

    @classmethod
    def schedule(
        cls,
        task: asyncio.Task[Any],
        reason: CancelReason,
        cancelling: int,
    ) -> _CancelToken:
        token = cls(task, reason, cancelling)
        asyncio.get_running_loop().call_soon(token._fire)
        return token

    def resolve(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        reasons: tuple[CancelReason, ...],
    ) -> None:
        # adopted from here
        # https://github.com/python/cpython/blob/v3.14.3/Lib/asyncio/timeouts.py#L110
        remaining = self._task.uncancel()
        # counter back to baseline — the CancelledError is ours, transform it
        if remaining <= self._cancelling and exc_type is not None:
            if issubclass(exc_type, asyncio.CancelledError):
                if self._reason.cancel_type is CancelType.TIMEOUT:
                    raise FenceTimeout(reasons) from exc_val
                raise FenceCancelled(reasons) from exc_val

            if self._reason.cancel_type is CancelType.TIMEOUT and exc_val is not None:
                _insert_timeout_error(exc_val, reasons)
                if isinstance(exc_val, ExceptionGroup):
                    for exc in exc_val.exceptions:
                        _insert_timeout_error(exc, reasons)

    def _fire(self) -> None:
        self._task.cancel(msg=self._reason.message)


def _insert_timeout_error(
    exc_val: BaseException, reasons: tuple[CancelReason, ...]
) -> None:
    # adopted from here
    # https://github.com/python/cpython/blob/v3.14.3/Lib/asyncio/timeouts.py#L133
    while exc_val.__context__ is not None:
        if isinstance(exc_val.__context__, asyncio.CancelledError):
            te = FenceTimeout(reasons)
            te.__context__ = te.__cause__ = exc_val.__context__
            exc_val.__context__ = te
            break
        exc_val = exc_val.__context__
