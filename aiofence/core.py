from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Self
from weakref import WeakKeyDictionary


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

_active_fences: WeakKeyDictionary[asyncio.Task[Any], Fence] = WeakKeyDictionary()


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
    """

    def __init__(self, *triggers: Trigger, policy: CancelPolicy | None = None) -> None:
        self._triggers = triggers
        self._policy = policy
        self._current_task: asyncio.Task[Any] | None = None
        self._exit_handlers: list[TriggerHandle] = []
        self._cancel_reasons: list[CancelReason] = []
        self._declined_reasons: list[CancelReason] = []
        self._cancel_token: _CancelToken | None = None
        self._cancelling: int | None = None
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

        if task in _active_fences:
            raise RuntimeError(
                "Nested Fences are not supported. "
                "See https://github.com/stanislaushimovolos/aiofence/issues/12"
            )

        _active_fences[task] = self
        self._current_task = task
        self._cancelling = task.cancelling()

        for source in self._triggers:
            reason = source.check()
            if reason is not None:
                self._admit(reason)

        if self._cancel_reasons:
            # 3.12: uncancel() doesn't clear _must_cancel, so calling
            # task.cancel() synchronously here would cause a spurious
            # CancelledError at the next await. Defer via call_soon so
            # cancel() finds _fut_waiter set and never touches _must_cancel.
            self._schedule_cancel()
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
            if self._cancel_token is None:
                return False

            self._suppressed = self._cancel_token.resolve(exc_type)
            return self._suppressed
        finally:
            if self._current_task is not None:
                _active_fences.pop(self._current_task, None)

            # remove references to allow GC collect objects
            self._current_task = None
            self._cancel_token = None
            self._exit_handlers = []

    def _on_trigger(self, reason: CancelReason) -> None:
        if self._admit(reason):
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
        Cancels task immediately. Must only be called from event loop
        callbacks (outside the task), where _fut_waiter is set and
        task.cancel() won't touch _must_cancel.
        """
        if self._cancel_token is not None:
            return

        if asyncio.current_task() is self._current_task:
            raise asyncio.InvalidStateError(
                "Trigger callback fired synchronously inside the task. "
                "Trigger.arm() callbacks must fire from the event loop, not inline."
            )

        task, cancelling = self._cancel_preconditions()
        self._cancel_token = _CancelToken.cancel(task, self._cancel_reasons[0].message, cancelling)

    def _schedule_cancel(self) -> None:
        """
        Defers task.cancel() via call_soon. Used from __enter__ (pre-trigger
        path) to avoid setting _must_cancel during synchronous execution.
        """
        task, cancelling = self._cancel_preconditions()
        self._cancel_token = _CancelToken.schedule_cancel(
            task, self._cancel_reasons[0].message, cancelling
        )

    def _cancel_preconditions(self) -> tuple[asyncio.Task[Any], int]:
        if self._current_task is None or self._cancelling is None:
            raise RuntimeError("Fence._cancel() called before __enter__")

        return self._current_task, self._cancelling


class _CancelToken:
    """
    Encapsulates one cancel/uncancel cycle using asyncio's counter protocol.

    Defers `task.cancel()` via `call_soon` to avoid setting `_must_cancel`
    when called from within the task's own synchronous execution.
    """

    def __init__(
        self,
        task: asyncio.Task[Any],
        cancelling: int,
    ) -> None:
        self._task = task
        self._cancelling = cancelling
        self._delivered = False
        self._handle: asyncio.Handle | None = None

    @classmethod
    def schedule_cancel(
        cls,
        task: asyncio.Task[Any],
        msg: str,
        cancelling: int,
    ) -> _CancelToken:
        token = cls(task, cancelling)
        token._handle = asyncio.get_running_loop().call_soon(token._deliver_cancellation, msg)
        return token

    @classmethod
    def cancel(
        cls,
        task: asyncio.Task[Any],
        msg: str,
        cancelling: int,
    ) -> _CancelToken:
        token = cls(task, cancelling)
        token._deliver_cancellation(msg)
        return token

    def resolve(self, exc_type: type[BaseException] | None) -> bool:
        """
        Balance the cancel counter and decide whether to suppress.

        Returns True to suppress the exception (CancelledError is ours),
        False to let it propagate.
        """

        # body completed before cancel was delivered — rescind
        if not self._delivered:
            if self._handle is not None:
                self._handle.cancel()
                self._handle = None
            return False  # nothing to suppress

        remaining = self._task.uncancel()
        # suppress our CancelledError, propagate everything else
        return (
            remaining <= self._cancelling
            and exc_type is not None
            and issubclass(exc_type, asyncio.CancelledError)
        )

    def _deliver_cancellation(self, msg: str) -> None:
        self._delivered = True
        self._handle = None
        self._task.cancel(msg=msg)
