from __future__ import annotations

import asyncio
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


class Guard:
    """
    Handle to an armed cancel source. Knows how to disarm itself.
    """

    def disarm(self) -> None:
        raise NotImplementedError


class CancelSource:
    """
    Describes a cancellation trigger. Produces a Guard when armed.
    """

    def check(self) -> CancelReason | None:
        """
        Pre-check: is this source already triggered?
        """
        return None

    def arm(self, on_cancel: Callable[[CancelReason], None]) -> Guard:
        raise NotImplementedError


class Context:
    """
    Immutable description of cancellation sources.
    """

    def __init__(self, *sources: CancelSource) -> None:
        self.sources = tuple(sources)


class Scope:
    """
    Binds a Context to the running task. Owns all cancellation logic.
    """

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self._task: asyncio.Task[Any] | None = None
        self._guards: list[Guard] = []
        self._cancel_reasons: list[CancelReason] = []
        self._cancellation: Cancellation | None = None
        self._armed = False

    @property
    def cancelled(self) -> bool:
        return self._cancellation is not None

    @property
    def reasons(self) -> list[CancelReason]:
        return list(self._cancel_reasons)

    def __enter__(self) -> Self:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Scope must be used inside a task")

        self._task = task

        for source in self._ctx.sources:
            reason = source.check()
            if reason is not None:
                self._cancel_reasons.append(reason)

        if self._cancel_reasons:
            self._cancel()
        else:
            self._guards = [
                source.arm(self._request_cancellation) for source in self._ctx.sources
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

        if self._cancellation is None:
            return None

        return self._cancellation.resolve(exc_type, exc_val)

    def _request_cancellation(self, reason: CancelReason) -> None:
        self._cancel_reasons.append(reason)
        self._cancel()

    def _cancel(self) -> None:
        if self._cancellation is None and self._task is not None:
            reason = self._cancel_reasons[0]
            cancelling = self._task.cancelling()
            self._task.cancel(msg=reason.message)
            self._cancellation = Cancellation(self._task, reason, cancelling)


class Cancellation:
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
