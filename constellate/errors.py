from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import CancelReason


class FenceCancelled(asyncio.CancelledError):
    """Raised when a CANCELLED trigger fires. BaseException (like CancelledError)."""

    def __init__(self, reasons: tuple[CancelReason, ...]) -> None:
        self.reasons = reasons
        super().__init__(reasons[0].message if reasons else "cancelled")


class FenceTimeout(TimeoutError):  # noqa: N818
    """Raised when a TIMEOUT trigger fires. Regular Exception (like TimeoutError)."""

    def __init__(self, reasons: tuple[CancelReason, ...]) -> None:
        self.reasons = reasons
        super().__init__(reasons[0].message if reasons else "timed out")
