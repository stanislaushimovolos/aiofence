from __future__ import annotations

import asyncio
from typing import Callable


class CancelReason:
    """
    Base class for why a context was cancelled.
    """

    def exception(self) -> BaseException | None:
        """
        The exception this reason resolves to, or None to suppress silently.
        """
        return None


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
        self._task: asyncio.Task | None = None
        self._cancel_msg: object | None = None
        self._guards: list[Guard] = []
        self._cancel_reasons: list[CancelReason] = []
        self._armed = False
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return len(self._cancel_reasons) > 0

    @property
    def reasons(self) -> list[CancelReason]:
        return list(self._cancel_reasons)

    def __enter__(self) -> Scope:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Scope must be used inside a task")

        self._task = task
        self._cancel_msg = object()

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

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool | None:
        if self._armed:
            for guard in self._guards:
                guard.disarm()

        if not self._cancel_reasons:
            return None

        if exc_type is asyncio.CancelledError and _is_our_cancellation(
            exc_val, self._cancel_msg
        ):
            exc = self._cancel_reasons[0].exception()
            if exc is not None:
                raise exc from exc_val
            return True

        return None

    def _request_cancellation(self, reason: CancelReason) -> None:
        self._cancel_reasons.append(reason)
        self._cancel()

    def _cancel(self) -> None:
        if not self._cancelled:
            self._cancelled = True
            self._task.cancel(msg=self._cancel_msg)


def _is_our_cancellation(exc: BaseException | None, msg: object | None) -> bool:
    if exc is None or msg is None:
        return False
    return exc.args and exc.args[0] is msg
