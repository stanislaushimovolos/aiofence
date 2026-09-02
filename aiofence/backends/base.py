from __future__ import annotations

import asyncio
from typing import Any, Protocol


class CancelHandle(Protocol):
    """
    One fence's cancellation, entered against one task.

    `cancel(message)` — deliver the cancel to the task; called at most once.
    `exit(exc_type, exc_val)` — balance whatever `cancel` did and report
    whether the exception leaving the fence body is ours to suppress.
    Always called on fence exit, cancel or not.
    """

    def cancel(self, message: str) -> None: ...

    def exit(self, exc_type: type[BaseException] | None, exc_val: BaseException | None) -> bool: ...


class CancelBackend(Protocol):
    """
    Decides *how* a fence cancels its task. Triggers, policy and reasons
    are the fence's; the backend only owns delivery and ownership.
    """

    def enter(self, task: asyncio.Task[Any]) -> CancelHandle: ...
