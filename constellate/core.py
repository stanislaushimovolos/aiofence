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


class TriggerHandle(ABC):
    """
    Handle to an armed cancel source. Knows how to disarm itself.
    """

    @abstractmethod
    def disarm(self) -> None: ...


class Trigger(ABC):
    """
    Describes a cancellation trigger. Produces a TriggerHandle when armed.
    """

    @abstractmethod
    def check(self) -> CancelReason | None: ...

    @abstractmethod
    def arm(self, on_cancel: Callable[[CancelReason], None]) -> TriggerHandle: ...


class Fence:
    """
    Binds a Context to the running task. Owns all cancellation logic.
    """

    def __init__(self, *triggers: Trigger) -> None:
        self._triggers = triggers
        self._task: asyncio.Task[Any] | None = None
        self._guards: list[TriggerHandle] = []
        self._cancel_reasons: list[CancelReason] = []
        self._cancel_token: _CancelToken | None = None
        self._cancelling: int | None = None
        self._armed = False

    def __enter__(self) -> Self:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Fence must be used inside a task")

        self._task = task
        self._cancelling = task.cancelling()

        for source in self._triggers:
            reason = source.check()
            if reason is not None:
                self._cancel_reasons.append(reason)

        if self._cancel_reasons:
            self._cancel()
        else:
            self._guards = [
                source.arm(self._request_cancellation) for source in self._triggers
            ]
            self._armed = True

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> bool | None:
        if self._armed:
            for guard in self._guards:
                guard.disarm()

        if self._cancel_token is None:
            return None

        return self._cancel_token.resolve(exc_type, exc_val)

    def _request_cancellation(self, reason: CancelReason) -> None:
        self._cancel_reasons.append(reason)
        self._cancel()

    def _cancel(self) -> None:
        if self._cancel_token is not None:
            return

        if self._task is None or self._cancelling is None:
            raise RuntimeError("Fence._cancel() called before __enter__")

        reason = self._cancel_reasons[0]
        self._task.cancel(msg=reason.message)
        self._cancel_token = _CancelToken(self._task, reason, self._cancelling)


class _CancelToken:
    """
    Encapsulates one cancel/uncancel cycle using asyncio's counter protocol.
    """

    def __init__(
        self, task: asyncio.Task[Any], reason: CancelReason, cancelling: int
    ) -> None:
        self._task = task
        self._reason = reason
        self._cancelling = cancelling

    def resolve(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None
    ) -> bool | None:
        if exc_type is None:
            return None

        if self._task.uncancel() <= self._cancelling:
            if issubclass(exc_type, asyncio.CancelledError):
                if self._reason.cancel_type is CancelType.TIMEOUT:
                    raise TimeoutError(self._reason.message) from exc_val
                return None

            if self._reason.cancel_type is CancelType.TIMEOUT and exc_val is not None:
                _insert_timeout_error(exc_val, self._reason.message)
                if isinstance(exc_val, ExceptionGroup):
                    for exc in exc_val.exceptions:
                        _insert_timeout_error(exc, self._reason.message)
        return None


def _insert_timeout_error(exc_val: BaseException, message: str) -> None:
    while exc_val.__context__ is not None:
        if isinstance(exc_val.__context__, asyncio.CancelledError):
            te = TimeoutError(message)
            te.__context__ = te.__cause__ = exc_val.__context__
            exc_val.__context__ = te
            break
        exc_val = exc_val.__context__
