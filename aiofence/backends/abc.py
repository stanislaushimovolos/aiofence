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
    """

    @abstractmethod
    def cancel(self, message: str) -> None: ...

    @abstractmethod
    def exit(self, exc_type: type[BaseException] | None, exc_val: BaseException | None) -> bool: ...


class CancelBackend(ABC):
    """
    Decides *how* a fence cancels its task. Triggers, policy and reasons
    are the fence's; the backend only owns delivery and ownership.
    """

    @abstractmethod
    def enter(self, task: asyncio.Task[Any]) -> CancelHandle: ...
