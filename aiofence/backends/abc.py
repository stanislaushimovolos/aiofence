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

    `enter(task)` — the task's outermost fence. `enter_nested(task, parent)`
    — a fence entered while `parent` is still active on the same task.
    A backend that cannot settle ownership between the two refuses, which
    is what the default does.
    """

    @abstractmethod
    def enter(self, task: asyncio.Task[Any]) -> CancelHandle: ...

    def enter_nested(self, task: asyncio.Task[Any], parent: CancelHandle) -> CancelHandle:  # noqa: ARG002
        message = (
            f"{type(self).__name__} does not support nested Fences on one task. "
            "Nest under AnyioBackend — the default — or bind it for this context with "
            "`bind_backend(AnyioBackend())`. "
            "See https://github.com/stanislaushimovolos/aiofence/issues/12"
        )
        raise RuntimeError(message)
