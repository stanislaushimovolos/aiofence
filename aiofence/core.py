from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Self
from weakref import WeakKeyDictionary

from .backends import CancelBackend, CancelHandle, get_default_backend


class CancelType(Enum):
    EVENT = auto()
    TIMEOUT = auto()


@dataclass(frozen=True, kw_only=True, slots=True)
class CancelReason:
    """
    Immutable record describing why cancellation occurred.
    """

    # Human-readable description, e.g. "timed out after 5s"
    message: str
    # Category of cancellation (TIMEOUT or EVENT).
    cancel_type: CancelType
    # Optional machine-readable identifier for programmatic matching.
    # Set via trigger's `code` param. Works with StrEnum for type safety.
    code: str | None = None


CancelCallback = Callable[[CancelReason], None]
CancelPolicy = Callable[[CancelReason], bool]

logger = logging.getLogger("aiofence")

_active_fences: WeakKeyDictionary[asyncio.Task[Any], list[Fence]] = WeakKeyDictionary()


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
    propagates. Caller inspects `fence.suppressed` / `fence.cancel_reasons` after
    the block. This keeps the cancel counter balanced and avoids
    CancelledError-with-counter-zero, which would confuse TaskGroup
    and nested asyncio.timeout scopes.

    `policy` is consulted once per reason before the cancel is delivered.
    A reason it rejects is recorded in `declined_reasons` and does not
    cancel; a policy that raises is logged and treated as accepting.

    `backend` decides how the cancel reaches the task. Defaults to the
    process-wide default (`aiofence.set_default_backend`), which is
    `AnyioBackend`. A Fence entered while another is active on the same
    task is nested: the inner fence's backend decides whether it can be.
    """

    def __init__(
        self,
        *triggers: Trigger,
        policy: CancelPolicy | None = None,
        backend: CancelBackend | None = None,
    ) -> None:
        self._triggers = triggers
        self._policy = policy
        self._backend = backend if backend is not None else get_default_backend()
        self._current_task: asyncio.Task[Any] | None = None
        self._exit_handlers: list[TriggerHandle] = []
        self._cancel_reasons: list[CancelReason] = []
        self._declined_reasons: list[CancelReason] = []
        self._handle: CancelHandle | None = None
        self._cancel_sent = False
        self._armed = False
        self._exited = False
        self._suppressed = False

    @property
    def suppressed(self) -> bool:
        """
        True if the Fence caught and suppressed a CancelledError.
        False if no trigger fired, or if the trigger fired but the body
        completed before cancellation was delivered.
        """
        return self._suppressed

    @property
    def cancelled(self) -> bool:
        """
        True if any trigger fired, even if the body completed
        before cancellation was delivered.
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

        self._handle = self._enter_backend(task, _active_fences.get(task))
        self._current_task = task
        _active_fences.setdefault(task, []).append(self)

        for source in self._triggers:
            reason = source.check()
            if reason is not None:
                self._admit(reason)

        if self._cancel_reasons:
            self._cancel()
            return self

        self._exit_handlers = [source.arm(self._on_trigger) for source in self._triggers]
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

        try:
            self._suppressed = self._require_handle().exit(exc_type, exc_val)
            return self._suppressed
        finally:
            if self._current_task is not None:
                _leave_stack(self._current_task, self)

            # remove references to allow GC collect objects
            self._current_task = None
            self._handle = None
            self._exit_handlers = []

    def _enter_backend(self, task: asyncio.Task[Any], stack: list[Fence] | None) -> CancelHandle:
        if not stack:
            return self._backend.enter(task)
        return self._backend.enter_nested(task, stack[-1]._require_handle())

    def _on_trigger(self, reason: CancelReason) -> None:
        if not self._admit(reason) or self._cancel_sent:
            return

        if asyncio.current_task() is self._current_task:
            raise asyncio.InvalidStateError(
                "Trigger callback fired synchronously inside the task. "
                "Trigger.arm() callbacks must fire from the event loop, not inline."
            )

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
        self._require_handle().cancel(self._cancel_reasons[0].message)

    def _require_handle(self) -> CancelHandle:
        if self._handle is None:
            raise RuntimeError("Fence used before __enter__")
        return self._handle


def _leave_stack(task: asyncio.Task[Any], fence: Fence) -> None:
    stack = _active_fences.get(task)
    if stack is None:
        return

    stack.remove(fence)
    if not stack:
        del _active_fences[task]
