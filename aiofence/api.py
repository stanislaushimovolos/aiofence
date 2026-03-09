from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from .core import CancelReason, Fence, Trigger
from .triggers import EventTrigger, TimeoutTrigger

_fencing_defaults: ContextVar[Fencing | None] = ContextVar("fencing_defaults", default=None)

_UNSET: Any = object()


@dataclass(frozen=True, kw_only=True, slots=True)
class _EventEntry:
    event: asyncio.Event
    code: str | None = None


class FenceCancelled(Exception):  # noqa: N818
    """
    Raised by ``Fencing.raise_on_cancel()`` when cancellation occurs.
    """

    def __init__(self, reasons: tuple[CancelReason, ...]) -> None:
        self.reasons = reasons
        super().__init__(self._format_message())

    def cancelled_by(self, code: str) -> bool:
        return any(r.code == code for r in self.reasons)

    def _format_message(self) -> str:
        if len(self.reasons) == 1:
            return self.reasons[0].message
        return "; ".join(r.message for r in self.reasons)


class Fencing:
    """
    Immutable builder that accumulates cancellation conditions
    and materializes them into a Fence.

    Calling ``.timeout()`` anchors the builder to a point in time,
    making it one-shot (raises on reuse).
    """

    __slots__ = ("_anchored", "_deadline", "_deadline_code", "_events", "_used")

    def __init__(
        self,
        *,
        _deadline: float | None = None,
        _deadline_code: str | None = None,
        _events: tuple[_EventEntry, ...] = (),
        _anchored: bool = False,
    ) -> None:
        self._events = _events
        self._deadline = _deadline
        self._deadline_code = _deadline_code
        self._anchored = _anchored
        self._used = False

    def timeout(self, delay: float, *, code: str | None = None) -> Fencing:
        """
        Add a relative timeout. Eagerly resolves to an absolute deadline.
        Makes the Fencing one-shot (raises on reuse).

        Args:
            delay: Seconds until cancellation.
            code: Machine-readable identifier for programmatic matching
                  via ``fence.cancelled_by(code)``.
        """
        loop = asyncio.get_running_loop()
        when = loop.time() + delay
        if self._deadline is None or when <= self._deadline:
            return self._derive(_anchored=True, _deadline=when, _deadline_code=code)

        return self._derive(_anchored=True)

    def deadline(self, when: float, *, code: str | None = None) -> Fencing:
        """
        Add an absolute monotonic deadline. Merged eagerly — the tightest wins.

        Args:
            when: Absolute monotonic time (``loop.time()`` based).
            code: Machine-readable identifier for programmatic matching
                  via ``fence.cancelled_by(code)``.
        """
        if self._deadline is None or when <= self._deadline:
            return self._derive(_deadline=when, _deadline_code=code)

        return self

    def event(self, event: asyncio.Event, *, code: str | None = None) -> Fencing:
        """
        Add an event-based cancellation condition. If the same event is
        registered again, the code is overridden (last wins).

        Args:
            event: asyncio.Event that triggers cancellation when set.
            code: Machine-readable identifier for programmatic matching
                  via ``fence.cancelled_by(code)``.
        """
        new_entry = _EventEntry(code=code, event=event)
        existing = tuple(e for e in self._events if e.event is not event)
        return self._derive(_events=(new_entry, *existing))

    @contextmanager
    def raise_on_cancel(self) -> Generator[Fence]:
        """
        Context manager that raises ``FenceCancelled`` on cancellation.
        """
        fence = self._build_fence()
        with fence:
            yield fence

        if fence.cancelled:
            raise FenceCancelled(fence.reasons)

    @contextmanager
    def move_on_cancel(self) -> Generator[Fence]:
        """
        Context manager that suppresses CancelledError on exit.
        Caller inspects ``fence.cancelled`` after the block.

        Unlike ``asyncio.timeout``, cancellation doesn't require waiting
        for the first ``await``. Check ``fence.cancelled`` at the top
        of the body for immediate early exit::

            with Fencing().timeout(5).move_on_cancel() as fence:
                if fence.cancelled:
                    return fallback()
                await work()
        """
        fence = self._build_fence()
        with fence:
            yield fence

    def _build_fence(self) -> Fence:
        if self._anchored:
            if self._used:
                raise RuntimeError(
                    "This Fencing has already been used. "
                    "Call .timeout() on the original Fencing to create a fresh anchor."
                )
            self._used = True

        triggers: list[Trigger] = []
        if self._deadline is not None:
            loop = asyncio.get_running_loop()
            remaining = max(0.0, self._deadline - loop.time())
            triggers.append(TimeoutTrigger(delay=remaining, code=self._deadline_code))

        triggers.extend(EventTrigger(event=e.event, code=e.code) for e in self._events)
        return Fence(*triggers)

    def _derive(
        self,
        *,
        _deadline: float | None = _UNSET,
        _deadline_code: str | None = _UNSET,
        _events: tuple[_EventEntry, ...] = _UNSET,
        _anchored: bool = _UNSET,
    ) -> Fencing:
        return Fencing(
            _deadline=self._deadline if _deadline is _UNSET else _deadline,
            _deadline_code=self._deadline_code if _deadline_code is _UNSET else _deadline_code,
            _events=self._events if _events is _UNSET else _events,
            _anchored=self._anchored if _anchored is _UNSET else _anchored,
        )


def on_timeout(delay: float, *, code: str | None = None) -> Fencing:
    """
    Create a Fencing with a relative timeout (anchored, one-shot).

    Args:
        delay: Seconds until cancellation.
        code: Machine-readable identifier for programmatic matching
              via ``fence.cancelled_by(code)``.
    """
    return Fencing().timeout(delay, code=code)


def on_deadline(when: float, *, code: str | None = None) -> Fencing:
    """
    Create a Fencing with an absolute monotonic deadline.

    Args:
        when: Absolute monotonic time (``loop.time()`` based).
        code: Machine-readable identifier for programmatic matching
              via ``fence.cancelled_by(code)``.
    """
    return Fencing().deadline(when, code=code)


def on_event(event: asyncio.Event, *, code: str | None = None) -> Fencing:
    """
    Create a Fencing with an event-based cancellation condition.

    Args:
        event: asyncio.Event that triggers cancellation when set.
        code: Machine-readable identifier for programmatic matching
              via ``fence.cancelled_by(code)``.
    """
    return Fencing().event(event, code=code)


def get_fencing_defaults() -> Fencing:
    """
    Return the Fencing defaults from the current context, or an empty
    Fencing if none is bound.
    """
    return _fencing_defaults.get() or Fencing()


@contextmanager
def bind_fencing_defaults(fencing: Fencing) -> Generator[None, None, None]:
    """
    Set the given Fencing as the defaults for the current context.
    Inner code can read it with ``get_fencing_defaults()``.
    """
    token = _fencing_defaults.set(fencing)
    try:
        yield
    finally:
        _fencing_defaults.reset(token)
