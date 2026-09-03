from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from .core import CancelPolicy, CancelReason, Fence, Trigger
from .triggers import EventTrigger, TimeoutTrigger

_current_fencing: ContextVar[Fencing | None] = ContextVar("current_fencing", default=None)

_UNSET: Any = object()


@dataclass(frozen=True, kw_only=True, slots=True)
class _EventEntry:
    event: asyncio.Event
    code: str | None = None


class FenceCancelled(Exception):  # noqa: N818
    """
    Raised by ``Fencing.raise_on_cancel()`` when a trigger fires.

    Attributes:
        suppressed: True if the Fence caught and suppressed a
            CancelledError. False if a trigger fired but the body
            completed before cancellation was delivered.
        cancel_reasons: All trigger reasons that fired.
        declined_reasons: Reasons the fence's policy rejected; they never
            cancelled anything.
    """

    def __init__(
        self,
        cancel_reasons: tuple[CancelReason, ...],
        *,
        suppressed: bool,
        declined_reasons: tuple[CancelReason, ...] = (),
    ) -> None:
        self.cancel_reasons = cancel_reasons
        self.suppressed = suppressed
        self.declined_reasons = declined_reasons
        super().__init__(self._format_message())

    def cancelled_by(self, code: str) -> bool:
        return any(r.code == code for r in self.cancel_reasons)

    def declined_by(self, code: str) -> bool:
        return any(r.code == code for r in self.declined_reasons)

    def _format_message(self) -> str:
        if len(self.cancel_reasons) == 1:
            return self.cancel_reasons[0].message
        return "; ".join(r.message for r in self.cancel_reasons)


class Fencing:
    """
    Immutable builder that accumulates cancellation conditions
    and materializes them into a Fence.

    Calling ``.timeout()`` anchors the builder to a point in time,
    making it per-operation: ``bind_fencing()`` refuses it.
    """

    __slots__ = ("_anchored", "_deadline", "_deadline_code", "_events", "_policy")

    def __init__(
        self,
        *,
        _deadline: float | None = None,
        _deadline_code: str | None = None,
        _events: tuple[_EventEntry, ...] = (),
        _policy: CancelPolicy | None = None,
        _anchored: bool = False,
    ) -> None:
        self._events = _events
        self._deadline = _deadline
        self._deadline_code = _deadline_code
        self._policy = _policy
        self._anchored = _anchored

    def timeout(self, delay: float | None, *, code: str | None = None) -> Fencing:
        """
        Add a relative timeout. Eagerly resolves to an absolute deadline.
        Anchors the Fencing to this moment, so ``bind_fencing()`` refuses it;
        use ``.deadline()`` for a budget shared through the context.

        Args:
            delay: Seconds until cancellation. ``None`` adds nothing and
                   returns the builder unchanged, as ``asyncio.timeout(None)``
                   does — for an optional timeout coming from configuration.
            code: Machine-readable identifier for programmatic matching
                  via ``fence.cancelled_by(code)``.
        """
        if delay is None:
            return self

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
        Add an event-based cancellation condition.

        Entries are deduplicated on the ``(event, code)`` pair: registering
        the same event under a different code keeps both, and each is
        reported independently by ``fence.cancelled_by(code)``.

        Args:
            event: asyncio.Event that triggers cancellation when set.
            code: Machine-readable identifier for programmatic matching
                  via ``fence.cancelled_by(code)``.
        """
        new_entry = _EventEntry(code=code, event=event)
        existing = tuple(e for e in self._events if e != new_entry)
        return self._derive(_events=(new_entry, *existing))

    def guard(self, policy: CancelPolicy) -> Fencing:
        """
        Consult ``policy`` for every reason before its cancel is delivered.
        ``True`` delivers, ``False`` declines: the reason is recorded in
        ``fence.declined_reasons`` and nothing is cancelled.

        Guards compose with AND — a guard added to a builder that already
        has one can only decline more, never less — and are inherited by
        every Fencing derived from this one.

        Args:
            policy: Sync, cheap callable; it runs inside the event loop
                    callback. An exception is logged and counts as ``True``.
        """
        if self._policy is None:
            return self._derive(_policy=policy)

        return self._derive(_policy=_both(self._policy, policy))

    def unless(self, precondition: Callable[[], bool], *, code: str | None = None) -> Fencing:
        """
        Decline cancellation while ``precondition()`` holds at fire time.

        Args:
            precondition: Sync callable checked once per reason.
            code: Only reasons carrying this code are subject to the
                  precondition; other reasons cancel as usual. Omitted,
                  every reason on the fence is — timeouts included.
        """

        def policy(reason: CancelReason) -> bool:
            if code is not None and reason.code != code:
                return True
            return not precondition()

        return self.guard(policy)

    @contextmanager
    def raise_on_cancel(self) -> Generator[Fence]:
        """
        Context manager that raises ``FenceCancelled`` on cancellation.
        """
        fence = self._build_fence()
        with fence:
            yield fence

        if fence.cancelled:
            raise FenceCancelled(
                fence.cancel_reasons,
                suppressed=fence.suppressed,
                declined_reasons=fence.declined_reasons,
            )

    @contextmanager
    def move_on_cancel(self) -> Generator[Fence]:
        """
        Context manager that suppresses CancelledError on exit.
        Caller inspects ``fence.suppressed`` / ``fence.cancelled`` after the block.

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
        triggers: list[Trigger] = []
        if self._deadline is not None:
            loop = asyncio.get_running_loop()
            remaining = max(0.0, self._deadline - loop.time())
            triggers.append(TimeoutTrigger(delay=remaining, code=self._deadline_code))

        triggers.extend(EventTrigger(event=e.event, code=e.code) for e in self._events)
        return Fence(*triggers, policy=self._policy)

    def _derive(
        self,
        *,
        _deadline: float | None = _UNSET,
        _deadline_code: str | None = _UNSET,
        _events: tuple[_EventEntry, ...] = _UNSET,
        _policy: CancelPolicy | None = _UNSET,
        _anchored: bool = _UNSET,
    ) -> Fencing:
        return Fencing(
            _deadline=self._deadline if _deadline is _UNSET else _deadline,
            _deadline_code=self._deadline_code if _deadline_code is _UNSET else _deadline_code,
            _events=self._events if _events is _UNSET else _events,
            _policy=self._policy if _policy is _UNSET else _policy,
            _anchored=self._anchored if _anchored is _UNSET else _anchored,
        )


def _both(first: CancelPolicy, second: CancelPolicy) -> CancelPolicy:
    return lambda reason: first(reason) and second(reason)


def on_timeout(delay: float | None, *, code: str | None = None) -> Fencing:
    """
    Create a Fencing with a relative timeout (anchored, per-operation).

    Args:
        delay: Seconds until cancellation. ``None`` yields an empty Fencing.
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


def get_current_fencing() -> Fencing:
    """
    Return the current Fencing from context, or an empty
    Fencing if none is bound.
    """
    return _current_fencing.get() or Fencing()


@contextmanager
def bind_fencing(fencing: Fencing) -> Generator[None, None, None]:
    """
    Set the given Fencing as current for this context.
    Inner code can read it with ``get_current_fencing()``.

    Raises ``RuntimeError`` for a Fencing anchored by ``.timeout()``:
    a relative timeout is per-operation, a shared budget is ``.deadline()``.
    """
    if fencing._anchored:
        raise RuntimeError(
            "A Fencing built with .timeout() is per-operation and cannot be bound "
            "as context. Use .deadline(loop.time() + delay) for a shared budget."
        )
    token = _current_fencing.set(fencing)
    try:
        yield
    finally:
        _current_fencing.reset(token)
