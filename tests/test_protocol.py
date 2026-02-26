import asyncio

import pytest

from constellate import (
    EventTrigger,
    Fence,
    FenceCancelled,
    FenceTimeout,
    TimeoutTrigger,
)
from constellate.core import CancelType

# --- FenceTimeout (TIMEOUT triggers) ---


async def test__FenceTimeout__timeout_fires__raised_with_reasons():
    with pytest.raises(FenceTimeout) as exc_info, Fence(TimeoutTrigger(0)):
        await asyncio.sleep(1)

    assert len(exc_info.value.reasons) == 1
    assert exc_info.value.reasons[0].cancel_type is CancelType.TIMEOUT


async def test__FenceTimeout__caught_by_except_TimeoutError():
    with pytest.raises(TimeoutError), Fence(TimeoutTrigger(0)):
        await asyncio.sleep(1)


async def test__FenceTimeout__not_caught_by_except_CancelledError():
    with pytest.raises(FenceTimeout):  # noqa: PT012
        try:
            with Fence(TimeoutTrigger(0)):
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pytest.fail("FenceTimeout should not be caught by except CancelledError")


# --- FenceCancelled (CANCELLED triggers) ---


async def test__FenceCancelled__event_fires__raised_with_reasons():
    event = asyncio.Event()
    event.set()

    with pytest.raises(FenceCancelled) as exc_info, Fence(EventTrigger(event)):
        await asyncio.sleep(1)

    assert len(exc_info.value.reasons) == 1
    assert exc_info.value.reasons[0].cancel_type is CancelType.CANCELLED


async def test__FenceCancelled__caught_by_except_CancelledError():
    event = asyncio.Event()
    event.set()

    with pytest.raises(asyncio.CancelledError), Fence(EventTrigger(event)):
        await asyncio.sleep(1)


# --- fence.cancelled / fence.reasons after exception ---


async def test__Fence__cancelled_and_reasons_populated_after_FenceTimeout():
    fence = Fence(TimeoutTrigger(0))

    with pytest.raises(FenceTimeout), fence:
        await asyncio.sleep(1)

    assert fence.cancelled
    assert len(fence.reasons) == 1


async def test__Fence__cancelled_and_reasons_populated_after_FenceCancelled():
    event = asyncio.Event()
    event.set()
    fence = Fence(EventTrigger(event))

    with pytest.raises(FenceCancelled), fence:
        await asyncio.sleep(1)

    assert fence.cancelled
    assert len(fence.reasons) == 1


# --- Nested fences ---


async def test__Fence__nested__inner_timeout__outer_unaffected():
    inner = Fence(TimeoutTrigger(0))
    outer = Fence(TimeoutTrigger(10))

    with pytest.raises(FenceTimeout), outer, inner:
        await asyncio.sleep(1)

    assert inner.cancelled
    assert not outer.cancelled


async def test__Fence__nested__outer_timeout__inner_doesnt_claim():
    event = asyncio.Event()  # never fires
    inner = Fence(EventTrigger(event))

    with pytest.raises(FenceTimeout), Fence(TimeoutTrigger(0)) as outer, inner:
        await asyncio.sleep(1)

    assert outer.cancelled
    assert not inner.cancelled
