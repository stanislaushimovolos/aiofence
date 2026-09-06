from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any


class CancelHandle(ABC):
    """
    One fence's cancellation, entered against one task.

    `cancel(message)` — deliver the cancel to the task; called at most once.
    `exit(exc_type, exc_val)` — balance whatever `cancel` did and report
    whether the exception leaving the fence body is ours to suppress.
    Always called on fence exit, cancel or not.
    `set_deadline(when)` — the fence's tightest pending timer, as an absolute
    `loop.time()`, or `math.inf` once none is pending. Advertisement only:
    the fence's trigger does the cancelling. A backend with nowhere to show
    it ignores the call.
    """

    @abstractmethod
    def cancel(self, message: str) -> None: ...

    @abstractmethod
    def set_deadline(self, when: float) -> None: ...

    @abstractmethod
    def exit(self, exc_type: type[BaseException] | None, exc_val: BaseException | None) -> bool: ...


class CancelBackend(ABC):
    """
    Decides *how* a fence cancels its task. Triggers, policy and reasons
    are the fence's; the backend only owns delivery and ownership.

    `enter(task)` — the task's outermost fence. `enter_nested(task)` — a
    fence entered while another is still active on the same task. A backend
    that cannot settle ownership between the two raises `RuntimeError`
    there instead of returning a handle.
    """

    @abstractmethod
    def enter(self, task: asyncio.Task[Any]) -> CancelHandle: ...

    @abstractmethod
    def enter_nested(self, task: asyncio.Task[Any]) -> CancelHandle: ...
