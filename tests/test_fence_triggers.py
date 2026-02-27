import asyncio

import pytest

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


async def test__fence__when_timeout_fires__then_suppressed_with_reasons():
    with Fence(TimeoutTrigger(0.001)) as fence:
        await asyncio.sleep(1)

    assert fence.cancelled
    assert len(fence.reasons) == 1
    assert fence.reasons[0].cancel_type is CancelType.TIMEOUT


async def test__fence__when_body_catches_cancelled_error__then_counter_balanced():
    task = asyncio.current_task()

    with Fence(TimeoutTrigger(0.001)) as fence:
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    assert fence.cancelled
    assert task.cancelling() == 0


async def test__fence__when_trigger_fires_and_body_raises__then_exception_propagates():
    task = asyncio.current_task()

    with pytest.raises(ValueError, match="boom"):
        with Fence(TimeoutTrigger(0.001)) as fence:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise ValueError("boom") from None

    assert fence.cancelled
    assert task.cancelling() == 0


async def test__fence__when_user_uncancels_inside_body__then_counter_balanced():
    task = asyncio.current_task()

    with Fence(TimeoutTrigger(0.001)) as fence:
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            task.uncancel()

    assert fence.cancelled
    assert task.cancelling() == 0


# --- Edge cases ---


async def test__fence__when_negative_timeout__then_suppressed():
    with Fence(TimeoutTrigger(-1)) as fence:
        await asyncio.sleep(1)

    assert fence.cancelled
    assert fence.reasons[0].cancel_type is CancelType.TIMEOUT


async def test__fence__when_disarm_after_trigger_fired__then_no_crash():
    event = asyncio.Event()
    trigger = EventTrigger(event)

    handle = trigger.arm(lambda reason: None)
    event.set()
    await asyncio.sleep(0)  # let the future resolve
    handle.disarm()  # should not crash


async def test__fence__when_event_set_inside_body__then_no_spurious_cancel_after_exit():
    event = asyncio.Event()

    with Fence(EventTrigger(event)) as fence:
        event.set()
        await asyncio.sleep(0)  # let callback fire

    assert fence.cancelled
    await asyncio.sleep(0)  # no spurious CancelledError after exit
    assert asyncio.current_task().cancelling() == 0


async def test__fence__when_multiple_triggers_fire__then_all_reasons_recorded():
    event1 = asyncio.Event()
    event2 = asyncio.Event()

    with Fence(EventTrigger(event1), EventTrigger(event2)) as fence:
        event1.set()
        event2.set()
        await asyncio.sleep(1)

    assert fence.cancelled
    assert len(fence.reasons) == 2
