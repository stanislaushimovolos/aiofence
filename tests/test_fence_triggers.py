import asyncio

from constellate import EventTrigger, Fence, TimeoutTrigger
from constellate.core import CancelType

# --- Pre-triggered ---


async def test__fence__when_timeout_pre_triggered__then_suppressed_with_reasons():
    with Fence(TimeoutTrigger(0)) as fence:
        await asyncio.sleep(1)

    assert fence.cancelled
    assert len(fence.reasons) == 1
    assert fence.reasons[0].cancel_type is CancelType.TIMEOUT


async def test__fence__when_event_pre_set__then_suppressed_and_cancelled():
    event = asyncio.Event()
    event.set()

    with Fence(EventTrigger(event)) as fence:
        await asyncio.sleep(1)

    assert fence.cancelled
    assert fence.reasons[0].cancel_type is CancelType.CANCELLED


async def test__fence__when_event_pre_set__then_body_interrupted_at_await():
    event = asyncio.Event()
    event.set()
    reached_before_await = False
    reached_after_await = False

    with Fence(EventTrigger(event)) as fence:
        reached_before_await = True
        await asyncio.sleep(0)
        reached_after_await = True

    assert fence.cancelled
    assert reached_before_await
    assert not reached_after_await


async def test__fence__when_event_pre_set_sync_body__then_body_completes():
    event = asyncio.Event()
    event.set()
    reached = False

    with Fence(EventTrigger(event)) as fence:
        reached = True

    assert fence.cancelled
    assert reached


# --- Runtime trigger fire (not pre-set) ---


async def test__fence__when_event_set_during_body__then_suppressed():
    event = asyncio.Event()
    asyncio.get_running_loop().call_soon(event.set)

    with Fence(EventTrigger(event)) as fence:
        await asyncio.sleep(1)

    assert fence.cancelled


async def test__fence__when_timeout_fires__then_suppressed():
    with Fence(TimeoutTrigger(0.001)) as fence:
        await asyncio.sleep(1)

    assert fence.cancelled
    assert fence.reasons[0].cancel_type is CancelType.TIMEOUT


# --- fence.cancelled / fence.reasons after suppression ---


async def test__fence__when_trigger_fires__then_cancelled_and_reasons_populated():
    with Fence(TimeoutTrigger(0.001)) as fence:
        await asyncio.sleep(1)

    assert fence.cancelled
    assert len(fence.reasons) == 1


# --- Nested fences ---


async def test__fence__when_inner_timeout_fires__then_outer_unaffected():
    outer = Fence(TimeoutTrigger(10))
    inner = Fence(TimeoutTrigger(0.001))

    with outer:
        with inner:
            await asyncio.sleep(1)

    assert inner.cancelled
    assert not outer.cancelled


async def test__fence__when_outer_timeout_fires__then_inner_doesnt_claim():
    event = asyncio.Event()  # never fires
    outer = Fence(TimeoutTrigger(0.01))
    inner = Fence(EventTrigger(event))

    with outer:
        with inner:
            await asyncio.sleep(1)

    assert outer.cancelled
    assert not inner.cancelled
