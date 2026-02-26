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


async def test__fence_timeout__when_fires__then_raised_with_reasons():
    with pytest.raises(FenceTimeout) as exc_info:
        with Fence(TimeoutTrigger(0)):
            await asyncio.sleep(1)

    assert len(exc_info.value.reasons) == 1
    assert exc_info.value.reasons[0].cancel_type is CancelType.TIMEOUT


@pytest.mark.parametrize("catch", [FenceCancelled, asyncio.CancelledError])
async def test__fence_cancelled__when_event_fires__then_body_not_reached(
    catch: type,
):
    event = asyncio.Event()
    event.set()
    reached = False

    with pytest.raises(catch):  # noqa: PT012
        with Fence(EventTrigger(event)):
            reached = True
            await asyncio.sleep(1)

    assert not reached


async def test__fence_cancelled__when_event_fires__then_raised_with_reasons():
    event = asyncio.Event()
    event.set()

    with pytest.raises(FenceCancelled) as exc_info:
        with Fence(EventTrigger(event)):
            await asyncio.sleep(1)

    assert len(exc_info.value.reasons) == 1
    assert exc_info.value.reasons[0].cancel_type is CancelType.CANCELLED


# --- Runtime trigger fire (not pre-set) ---


async def test__fence_cancelled__when_event_set_during_body__then_raises():
    event = asyncio.Event()
    asyncio.get_running_loop().call_soon(event.set)

    with pytest.raises(FenceCancelled):
        with Fence(EventTrigger(event)):
            await asyncio.sleep(1)


async def test__fence_timeout__when_fires_during_body__then_raises():
    with pytest.raises(FenceTimeout):
        with Fence(TimeoutTrigger(0.001)):
            await asyncio.sleep(1)


# --- fence.cancelled / fence.reasons after exception ---


@pytest.mark.parametrize(
    ("trigger", "exc_type"),
    [
        (TimeoutTrigger(0), FenceTimeout),
        (EventTrigger(asyncio.Event()), FenceCancelled),
    ],
    ids=["timeout", "event"],
)
async def test__fence__when_trigger_fires__then_cancelled_and_reasons_populated(
    trigger: TimeoutTrigger | EventTrigger,
    exc_type: type,
):
    if isinstance(trigger, EventTrigger):
        trigger._event.set()

    fence = Fence(trigger)

    with pytest.raises(exc_type):
        with fence:
            await asyncio.sleep(1)

    assert fence.cancelled
    assert len(fence.reasons) == 1


# --- Nested fences ---


async def test__fence__when_inner_timeout_fires__then_outer_unaffected():
    inner = Fence(TimeoutTrigger(0))
    outer = Fence(TimeoutTrigger(10))

    with pytest.raises(FenceTimeout), outer, inner:
        await asyncio.sleep(1)

    assert inner.cancelled
    assert not outer.cancelled


async def test__fence__when_outer_timeout_fires__then_inner_doesnt_claim():
    event = asyncio.Event()  # never fires
    outer = Fence(TimeoutTrigger(0.01))
    inner = Fence(EventTrigger(event))

    with pytest.raises(FenceTimeout), outer, inner:
        await asyncio.sleep(1)

    assert outer.cancelled
    assert not inner.cancelled
