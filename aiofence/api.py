from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from .core import Fence, Trigger
from .triggers import EventTrigger, TimeoutTrigger

_UNSET: Any = object()


class Fencing:
    """
    Immutable builder that accumulates cancellation conditions
    and materializes them into a Fence.
    """

    __slots__ = ("_deadline", "_deadline_code", "_explicit_triggers", "_timeouts")

    def __init__(
        self,
        *,
        _deadline: float | None = None,
        _deadline_code: str | None = None,
        _timeouts: tuple[tuple[float, str | None], ...] = (),
        _explicit_triggers: tuple[Trigger, ...] = (),
    ) -> None:
        self._deadline = _deadline
        self._deadline_code = _deadline_code
        self._timeouts = _timeouts
        self._explicit_triggers = _explicit_triggers

    def timeout(self, delay: float, *, code: str | None = None) -> Fencing:
        """
        Add a relative timeout. Resolved to a deadline at __enter__ time.
        Multiple timeouts are merged — the shortest wins.

        Args:
            delay: Seconds until cancellation.
            code: Machine-readable identifier for programmatic matching
                  via ``fence.cancelled_by(code)``.
        """
        return self._derive(_timeouts=(*self._timeouts, (delay, code)))

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
        return self._derive()

    def event(self, event: asyncio.Event, *, code: str | None = None) -> Fencing:
        """
        Add an event-based cancellation condition. If the same event is
        registered again, the code is overridden (last wins).

        Args:
            event: asyncio.Event that triggers cancellation when set.
            code: Machine-readable identifier for programmatic matching
                  via ``fence.cancelled_by(code)``.
        """
        # checking if event was already registered
        existing = None
        for t in self._explicit_triggers:
            if isinstance(t, EventTrigger) and t._event is event:
                existing = t
                break

        new_trigger = EventTrigger(event, code=code)
        if existing is None:
            return self._derive(_triggers=(*self._explicit_triggers, new_trigger))

        # override existing trigger with new code
        triggers = tuple(new_trigger if t is existing else t for t in self._explicit_triggers)
        return self._derive(_triggers=triggers)

    def trigger(self, trigger: Trigger) -> Fencing:
        """
        Add a custom trigger. Custom triggers are never merged or deduplicated.

        Args:
            trigger: A Trigger instance to arm when the Fence is entered.
        """
        return self._derive(_triggers=(*self._explicit_triggers, trigger))

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
        loop = asyncio.get_running_loop()
        now = loop.time()

        effective_deadline = self._deadline
        effective_code = self._deadline_code

        # choose the tightest deadline condition
        for delay, code in self._timeouts:
            dl = now + delay
            if effective_deadline is None or dl < effective_deadline:
                effective_deadline = dl
                effective_code = code

        triggers: list[Trigger] = []
        if effective_deadline is not None:
            remaining = max(0.0, effective_deadline - now)
            triggers.append(TimeoutTrigger(remaining, code=effective_code))

        triggers.extend(self._explicit_triggers)
        return Fence(*triggers)

    def _derive(
        self,
        *,
        _deadline: float | None = _UNSET,
        _deadline_code: str | None = _UNSET,
        _timeouts: tuple[tuple[float, str | None], ...] = _UNSET,
        _triggers: tuple[Trigger, ...] = _UNSET,
    ) -> Fencing:
        return Fencing(
            _deadline=self._deadline if _deadline is _UNSET else _deadline,
            _deadline_code=self._deadline_code if _deadline_code is _UNSET else _deadline_code,
            _timeouts=self._timeouts if _timeouts is _UNSET else _timeouts,
            _explicit_triggers=self._explicit_triggers if _triggers is _UNSET else _triggers,
        )
