import asyncio

import pytest

from aiofence import EXTERNAL_CODE, EventTrigger, Fence, TimeoutTrigger

# --- Sequential fences ---


async def test__fence__when_sequential__then_allowed():
    first = Fence(TimeoutTrigger(0.001))
    second = Fence(TimeoutTrigger(0.001))

    with first:
        await asyncio.sleep(1)

    with second:
        await asyncio.sleep(1)

    assert first.suppressed
    assert first.cancelled
    assert second.suppressed
    assert second.cancelled


async def test__fence__when_inner_fence_inside_asyncio_timeout__then_both_independent():
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            inner = Fence(TimeoutTrigger(0.001))
            with inner:
                await asyncio.sleep(1)
            assert inner.suppressed
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
    fence_suppressed = None
    fence_cancelled = None

    async def task_body():
        nonlocal fence_suppressed, fence_cancelled
        fence = Fence(TimeoutTrigger(0))
        try:
            with fence:
                await asyncio.sleep(10)
        finally:
            fence_suppressed = fence.suppressed
            fence_cancelled = fence.cancelled

    task = asyncio.get_running_loop().create_task(task_body())
    asyncio.get_running_loop().call_soon(task.cancel)

    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert task.cancelling() == 1  # fence uncancelled its own, external remains
    assert not fence_suppressed  # trigger fired but fence didn't suppress (external cancel won)
    assert fence_cancelled  # trigger did fire


async def test__fence__when_external_cancel_with_fence__then_propagates():
    event = asyncio.Event()  # never fires
    fence_suppressed = None
    fence_cancelled = None
    reached_after_fence = False

    async def task_body():
        nonlocal reached_after_fence, fence_suppressed, fence_cancelled
        fence = Fence(EventTrigger(event))
        try:
            with fence:
                await asyncio.sleep(10)
            reached_after_fence = True
        finally:
            fence_suppressed = fence.suppressed
            fence_cancelled = fence.cancelled_by(EXTERNAL_CODE)

    task = asyncio.get_running_loop().create_task(task_body())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not reached_after_fence
    assert not fence_suppressed
    assert fence_cancelled
    assert task.cancelled()
    assert task.cancelling() == 1


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


async def test__fence__when_child_task_cancelled_inside_body__then_external_not_suppressed():
    event = asyncio.Event()  # never fires
    task = asyncio.current_task()

    with pytest.raises(asyncio.CancelledError):
        with Fence(EventTrigger(event)) as fence:
            child = asyncio.create_task(asyncio.sleep(10))
            await asyncio.sleep(0)
            child.cancel()
            await child

    assert not fence.suppressed
    assert fence.cancelled_by(EXTERNAL_CODE)
    assert task.cancelling() == 0


# --- asyncio.timeout interop ---


async def test__fence__when_asyncio_timeout_nested_inside__then_timeout_raises():
    event = asyncio.Event()  # never fires

    with Fence(EventTrigger(event)) as fence:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.001):
                await asyncio.sleep(10)

    assert not fence.suppressed
    assert not fence.cancelled


async def test__fence__when_asyncio_timeout_zero_nested_inside__then_timeout_raises():
    event = asyncio.Event()  # never fires

    with pytest.raises(TimeoutError):
        with Fence(EventTrigger(event)) as fence:
            async with asyncio.timeout(0):
                await asyncio.sleep(10)

    assert not fence.suppressed
    assert not fence.cancelled


async def test__fence__when_nested_inside_asyncio_timeout__then_timeout_propagates():
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            with Fence(TimeoutTrigger(0)) as fence:
                await asyncio.sleep(10)
            await asyncio.sleep(10)  # outer timeout fires here

    assert fence.suppressed
    assert fence.cancelled


async def test__fence__when_prior_uncancel_cycle__then_counter_survives():
    fence_suppressed = None
    fence_cancelled = None

    async def run():
        nonlocal fence_suppressed, fence_cancelled
        task = asyncio.current_task()

        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            task.uncancel()

        with Fence(TimeoutTrigger(0)) as fence:
            await asyncio.sleep(10)

        fence_suppressed = fence.suppressed
        fence_cancelled = fence.cancelled
        assert task.cancelling() == 0

    inner = asyncio.get_running_loop().create_task(run())
    await asyncio.sleep(0)
    inner.cancel()
    await inner

    assert fence_suppressed
    assert fence_cancelled
