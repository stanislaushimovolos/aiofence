from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable


class CancelReason:
    """Base class for why a context was cancelled."""

    def exception(self) -> BaseException | None:
        """The exception this reason resolves to, or None to suppress silently."""
        return None


class Guard:
    """Handle to an armed cancel source. Knows how to disarm itself."""

    def disarm(self) -> None:
        raise NotImplementedError


class CancelSource:
    """Describes a cancellation trigger. Produces a Guard when armed."""

    def check(self) -> CancelReason | None:
        """Pre-check: is this source already triggered?"""
        return None

    def arm(self, on_cancel: Callable[[CancelReason], None]) -> Guard:
        raise NotImplementedError


@dataclass
class ScopeResult:
    """Collected cancel reasons + logic to resolve the exit exception."""

    cancel_reasons: list[CancelReason] = field(default_factory=list)
    cancel_msg: object | None = None

    @property
    def cancelled(self) -> bool:
        return len(self.cancel_reasons) > 0

    def resolve(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
    ) -> bool | None:
        """Handle the __exit__. Returns True to suppress, raises to replace, None to pass through."""
        if not self.cancel_reasons:
            return None

        if exc_type is asyncio.CancelledError and _is_our_cancellation(
            exc_val, self.cancel_msg
        ):
            exc = self.cancel_reasons[0].exception()
            if exc is not None:
                raise exc from exc_val
            return True

        return None


def _is_our_cancellation(exc: BaseException | None, msg: object | None) -> bool:
    """Check if a CancelledError was caused by us, via message matching."""
    if exc is None or msg is None:
        return False
    return exc.args and exc.args[0] is msg


class SourceSet:
    """Arms sources, collects cancel reasons, disarms on close."""

    def __init__(
        self,
        sources: tuple[CancelSource, ...],
        task: asyncio.Task,
        cancel_msg: object,
    ) -> None:
        self._task = task
        self._cancel_msg = cancel_msg
        self._cancel_reasons: list[CancelReason] = []
        self._guards: list[Guard] = []

        for source in sources:
            reason = source.check()
            if reason is not None:
                self._cancel_reasons.append(reason)

        if self._cancel_reasons:
            task.cancel(msg=cancel_msg)
        else:
            self._guards = [source.arm(self._on_cancel) for source in sources]

    def close(self) -> ScopeResult:
        for guard in self._guards:
            guard.disarm()
        self._guards.clear()
        return ScopeResult(
            cancel_reasons=self._cancel_reasons,
            cancel_msg=self._cancel_msg,
        )

    def _on_cancel(self, reason: CancelReason) -> None:
        self._cancel_reasons.append(reason)
        if len(self._cancel_reasons) == 1:
            self._task.cancel(msg=self._cancel_msg)


class Context:
    """Immutable description of cancellation sources."""

    def __init__(self, *sources: CancelSource) -> None:
        self.sources = tuple(sources)


class Scope:
    """Binds a Context to the running task."""

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self._source_set: SourceSet | None = None
        self._result: ScopeResult | None = None
        self._task: asyncio.Task | None = None
        self._cancel_msg: object | None = None

    @property
    def cancelled(self) -> bool:
        return self._result is not None and self._result.cancelled

    @property
    def reasons(self) -> list[CancelReason]:
        return self._result.cancel_reasons if self._result else []

    def __enter__(self) -> Scope:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Scope must be used inside a task")

        self._task = task
        self._cancel_msg = object()  # unique sentinel per scope
        self._source_set = SourceSet(self._ctx.sources, task, self._cancel_msg)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool | None:
        self._result = self._source_set.close()
        return self._result.resolve(exc_type, exc_val)
