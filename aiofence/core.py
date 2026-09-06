from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Self
from weakref import WeakKeyDictionary

from .backends import CancelBackend, CancelHandle, get_default_backend


class CancelType(Enum):
    EVENT = auto()
    TIMEOUT = auto()
    EXTERNAL = auto()


@dataclass(frozen=True, kw_only=True, slots=True)
class CancelReason:
    """
    Immutable record describing why cancellation occurred.
    """

    # Human-readable description, e.g. "timed out after 5s"
    message: str
    # Category of cancellation (TIMEOUT, EVENT, or EXTERNAL).
    cancel_type: CancelType
    # Optional machine-readable identifier for programmatic matching.
    # The `code` the deadline or event was declared with. Works with StrEnum.
    # A cancel the fence did not deliver is recorded under `EXTERNAL_CODE`.
    code: str | None = None


EXTERNAL_CODE = "external"
_EXTERNAL_REASON = CancelReason(
    message="cancelled from outside the fence",
    cancel_type=CancelType.EXTERNAL,
    code=EXTERNAL_CODE,
)


CancelPolicy = Callable[[CancelReason], bool]

logger = logging.getLogger("aiofence")


class Fence:
    """
    Sync context manager that cancels the current task when its deadline
    passes or one of its events is set.

    `deadline` is an absolute `loop.time()` value; `events` are
    `(event, code)` pairs, each reported under its own code. A deadline
    already past or an event already set at `__enter__` cancels the body
    at its first `await`.

    Suppression semantics (follows anyio CancelScope model):
    __exit__ suppresses the CancelledError its own deadline or event caused
    — never raises, never propagates it. Caller inspects `fence.suppressed`
    / `fence.cancel_reasons` after the block. This keeps the cancel counter
    balanced and avoids CancelledError-with-counter-zero, which would
    confuse TaskGroup and nested asyncio.timeout scopes.

    `policy` is consulted once per reason before the cancel is delivered.
    A reason it rejects is recorded in `declined_reasons` and does not
    cancel; a policy that raises is logged and treated as accepting.

    A `CancelledError` that leaves the body and is not the fence's own —
    an outer scope, an `asyncio.timeout()`, a `task.cancel()` from
    elsewhere — propagates as it always has, and is recorded as a
    `CancelType.EXTERNAL` reason under `EXTERNAL_CODE`. The policy is not
    consulted: it is not ours to decline.

    `backend` decides how the cancel reaches the task. Defaults to the
    process-wide default (`aiofence.set_default_backend`). A Fence entered
    while another is active on the same task is nested: the inner fence's
    backend decides whether it can be.
    """

    def __init__(
        self,
        *,
        deadline: float | None = None,
        deadline_code: str | None = None,
        events: Iterable[tuple[asyncio.Event, str | None]] = (),
        policy: CancelPolicy | None = None,
        backend: CancelBackend | None = None,
    ) -> None:
        self._deadline = deadline
        self._deadline_code = deadline_code
        self._events = tuple(events)
        self._policy = policy
        self._backend = backend if backend is not None else get_default_backend()
        self._current_task: asyncio.Task[Any] | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._watches: list[_EventWatch] = []
        self._cancel_reasons: list[CancelReason] = []
        self._declined_reasons: list[CancelReason] = []
        self._handle: CancelHandle | None = None
        self._cancel_sent = False
        self._exited = False
        self._suppressed = False

    @property
    def task(self) -> asyncio.Task[Any]:
        """
        The task this fence is entered on. Raises `RuntimeError` outside
        the `with` block.
        """
        if self._current_task is None:
            raise RuntimeError("Fence is not entered")
        return self._current_task

    @property
    def handle(self) -> CancelHandle:
        """
        The backend's handle for this fence. Raises `RuntimeError` outside
        the `with` block.
        """
        if self._handle is None:
            raise RuntimeError("Fence is not entered")
        return self._handle

    @property
    def suppressed(self) -> bool:
        """
        True if the Fence caught and suppressed a CancelledError.
        False if nothing fired, or if something fired but the body
        completed before cancellation was delivered.
        """
        return self._suppressed

    @property
    def cancelled(self) -> bool:
        """
        True if the deadline passed or an event was set, even if the body
        completed before cancellation was delivered, or if a cancel from
        outside the fence tore the body down (`cancelled_by(EXTERNAL_CODE)`).
        """
        return len(self._cancel_reasons) > 0

    @property
    def cancel_reasons(self) -> tuple[CancelReason, ...]:
        return tuple(self._cancel_reasons)

    def cancelled_by(self, code: str) -> bool:
        return any(r.code == code for r in self._cancel_reasons)

    @property
    def declined_reasons(self) -> tuple[CancelReason, ...]:
        """
        Reasons that fired but were rejected by the policy. Never cancel.
        """
        return tuple(self._declined_reasons)

    def declined_by(self, code: str) -> bool:
        return any(r.code == code for r in self._declined_reasons)

    def __enter__(self) -> Self:
        if self._exited or self._current_task is not None:
            raise RuntimeError("Fence cannot be reused")

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError(
                "Fence needs a running asyncio task to cancel, and was entered "
                "outside one — from a loop callback, or from sync code running in "
                "a worker thread (e.g. a FastAPI `def` handler)."
            )

        if _active_fences.has(task):
            self._handle = self._backend.enter_nested(task)
        else:
            self._handle = self._backend.enter(task)
        self._current_task = task
        _active_fences.push(self)

        now = asyncio.get_running_loop().time()
        for reason in self._due(now):
            self._admit(reason)

        if self._cancel_reasons:
            self._cancel()
            return self

        self._arm(now)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> bool:
        self._exited = True
        self._disarm()

        try:
            self._suppressed = self.handle.exit(exc_type, exc_val)
            if _is_cancelled(exc_val) and not self._suppressed:
                self._cancel_reasons.append(_EXTERNAL_REASON)
            return self._suppressed
        finally:
            _active_fences.pop(self)

            # remove references to allow GC collect objects
            self._current_task = None
            self._handle = None

    def _due(self, now: float) -> list[CancelReason]:
        due = []
        if self._deadline is not None and self._deadline <= now:
            due.append(_timeout_reason(self._deadline, now, self._deadline_code))
        due.extend(_event_reason(event, code) for event, code in self._events if event.is_set())
        return due

    def _arm(self, now: float) -> None:
        if self._deadline is not None and self._deadline > now:
            reason = _timeout_reason(self._deadline, now, self._deadline_code)
            loop = asyncio.get_running_loop()
            self._timer = loop.call_at(self._deadline, self._on_fire, reason)
            self.handle.set_deadline(self._deadline)

        self._watches = [
            _EventWatch(event, self._on_fire, _event_reason(event, code))
            for event, code in self._events
            if not event.is_set()
        ]

    def _disarm(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

        for watch in self._watches:
            watch.disarm()
        self._watches = []

    def _on_fire(self, reason: CancelReason) -> None:
        if not self._admit(reason):
            if reason.cancel_type is CancelType.TIMEOUT:
                self.handle.set_deadline(math.inf)
            return

        if not self._cancel_sent:
            self._cancel()

    def _admit(self, reason: CancelReason) -> bool:
        """
        Route a reason through the policy into `cancel_reasons` or
        `declined_reasons`. Returns True when the reason should cancel.
        """
        if self._accepts(reason):
            self._cancel_reasons.append(reason)
            return True

        self._declined_reasons.append(reason)
        return False

    def _accepts(self, reason: CancelReason) -> bool:
        if self._policy is None:
            return True

        try:
            return self._policy(reason)
        except Exception:
            logger.exception("Cancel policy raised for %r; delivering the cancel", reason)
            return True

    def _cancel(self) -> None:
        """
        Deliver the first accepted reason through the backend. Callers
        send at most once; later accepted reasons are recorded only.
        """
        self._cancel_sent = True
        self.handle.cancel(self._cancel_reasons[0].message)


def _timeout_reason(deadline: float, now: float, code: str | None) -> CancelReason:
    return CancelReason(
        message=f"timed out after {max(0.0, deadline - now):.3g}s",
        cancel_type=CancelType.TIMEOUT,
        code=code,
    )


def _event_reason(event: asyncio.Event, code: str | None) -> CancelReason:
    return CancelReason(
        message=f"event {event!r} set",
        cancel_type=CancelType.EVENT,
        code=code,
    )


def _is_cancelled(exc: BaseException | None) -> bool:
    if isinstance(exc, asyncio.CancelledError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return exc.subgroup(asyncio.CancelledError) is not None
    return False


class _EventWatch:
    """
    Fires `on_fire(reason)` from the loop when `event` is set. A future
    subscribed straight to `Event._waiters` — what `Event.wait()` does
    internally — instead of a watcher task.
    """

    def __init__(
        self,
        event: asyncio.Event,
        on_fire: Callable[[CancelReason], None],
        reason: CancelReason,
    ) -> None:
        self._event = event
        self._fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._disarmed = False
        self._fut.add_done_callback(lambda _: None if self._disarmed else on_fire(reason))
        event._waiters.append(self._fut)

    def disarm(self) -> None:
        self._disarmed = True
        # Event.set() resolves futures but doesn't remove them from _waiters
        with suppress(ValueError):
            self._event._waiters.remove(self._fut)

        if not self._fut.done():
            self._fut.cancel()


class _ActiveFences:
    """
    Fences currently entered on each task, outermost first. A task has a
    stack only while at least one fence is active on it.
    """

    def __init__(self) -> None:
        self._stacks: WeakKeyDictionary[asyncio.Task[Any], list[Fence]] = WeakKeyDictionary()

    def has(self, task: asyncio.Task[Any]) -> bool:
        return bool(self._stacks.get(task))

    def push(self, fence: Fence) -> None:
        self._stacks.setdefault(fence.task, []).append(fence)

    def pop(self, fence: Fence) -> None:
        stack = self._stacks[fence.task]
        stack.remove(fence)
        if not stack:
            del self._stacks[fence.task]


_active_fences = _ActiveFences()
