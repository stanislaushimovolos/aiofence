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


async def test__fence__when_deeply_nested__then_all_counters_balanced():
    task = asyncio.current_task()
    outer = Fence(TimeoutTrigger(10))
    middle = Fence(TimeoutTrigger(10))
    inner = Fence(TimeoutTrigger(0.001))

    with outer:
        with middle:
            with inner:
                await asyncio.sleep(1)

    assert inner.cancelled
    assert not middle.cancelled
    assert not outer.cancelled
    assert task.cancelling() == 0


async def test__fence__when_inner_fence_inside_asyncio_timeout__then_both_independent():
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            inner = Fence(TimeoutTrigger(0.001))
            with inner:
                await asyncio.sleep(1)
            assert inner.cancelled
            await asyncio.sleep(1)  # outer timeout fires here


# --- External cancellation interop ---


async def test__fence__when_external_cancel__then_propagates():
    event = asyncio.Event()  # never fires
    reached_after_fence = False

    async def task_body():
        nonlocal reached_after_fence
        with Fence(EventTrigger(event)):
            await asyncio.sleep(10)
        reached_after_fence = True

    task = asyncio.get_running_loop().create_task(task_body())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not reached_after_fence
    assert task.cancelled()
    assert task.cancelling() == 1  # external cancel was never uncancelled


async def test__fence__when_external_and_trigger_both_fire__then_external_propagates():
    fence_cancelled = None

    async def task_body():
        nonlocal fence_cancelled
        fence = Fence(TimeoutTrigger(0))
        try:
            with fence:
                await asyncio.sleep(10)
        finally:
            fence_cancelled = fence.cancelled

    task = asyncio.get_running_loop().create_task(task_body())
    asyncio.get_running_loop().call_soon(task.cancel)

    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert task.cancelling() == 1  # fence uncancelled its own, external remains
    assert fence_cancelled  # fence's trigger also fired


async def test__fence__when_asyncio_timeout_nested_inside__then_timeout_raises():
    event = asyncio.Event()  # never fires

    with Fence(EventTrigger(event)) as fence:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.001):
                await asyncio.sleep(10)

    assert not fence.cancelled


async def test__fence__when_nested_inside_asyncio_timeout__then_timeout_propagates():
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            with Fence(TimeoutTrigger(0)) as fence:
                await asyncio.sleep(10)
            await asyncio.sleep(10)  # outer timeout fires here

    assert fence.cancelled


async def test__fence__when_prior_uncancel_cycle__then_counter_survives():
    fence_cancelled = None

    async def run():
        nonlocal fence_cancelled
        task = asyncio.current_task()

        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            task.uncancel()

        with Fence(TimeoutTrigger(0)) as fence:
            await asyncio.sleep(10)

        fence_cancelled = fence.cancelled
        assert task.cancelling() == 0

    inner = asyncio.get_running_loop().create_task(run())
    await asyncio.sleep(0)
    inner.cancel()
    await inner

    assert fence_cancelled


async def test__fence__when_cancel_called_inside_body__then_propagates():
    event = asyncio.Event()  # never fires
    reached_after_fence = False

    async def task_body():
        nonlocal reached_after_fence
        task = asyncio.current_task()
        with Fence(EventTrigger(event)):
            task.cancel()
            await asyncio.sleep(0)
        reached_after_fence = True

    inner = asyncio.get_running_loop().create_task(task_body())
    await asyncio.sleep(0)

    with pytest.raises(asyncio.CancelledError):
        await inner

    assert not reached_after_fence
    assert inner.cancelled()


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
