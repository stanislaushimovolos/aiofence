import asyncio

import pytest

from constellate import EventTrigger, Fence, TimeoutTrigger

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


async def test__fence__when_nested_fences_share_same_event__then_both_cancelled():
    event = asyncio.Event()
    outer = Fence(EventTrigger(event))
    inner = Fence(EventTrigger(event))
    reached_after_await = False
    reached_after_inner = False

    with outer:
        with inner:
            event.set()
            await asyncio.sleep(1)
            reached_after_await = True
        reached_after_inner = True

    assert inner.cancelled
    assert outer.cancelled
    assert not reached_after_await
    assert not reached_after_inner


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


async def test__fence__when_external_cancel_with_nested_fences__then_propagates():
    event1 = asyncio.Event()  # never fires
    event2 = asyncio.Event()  # never fires
    outer_cancelled = None
    inner_cancelled = None
    reached_after_inner = False

    async def task_body():
        nonlocal reached_after_inner, outer_cancelled, inner_cancelled
        outer = Fence(EventTrigger(event1))
        inner = Fence(EventTrigger(event2))
        try:
            with outer:
                with inner:
                    await asyncio.sleep(10)
                reached_after_inner = True
        finally:
            outer_cancelled = outer.cancelled
            inner_cancelled = inner.cancelled

    task = asyncio.get_running_loop().create_task(task_body())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not reached_after_inner
    assert not inner_cancelled
    assert not outer_cancelled
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


async def test__fence__when_child_task_cancelled_inside_body__then_fence_unaffected():
    event = asyncio.Event()  # never fires
    task = asyncio.current_task()

    with pytest.raises(asyncio.CancelledError):
        with Fence(EventTrigger(event)) as fence:
            child = asyncio.create_task(asyncio.sleep(10))
            await asyncio.sleep(0)
            child.cancel()
            await child

    assert not fence.cancelled
    assert task.cancelling() == 0


# --- asyncio.timeout interop ---


async def test__fence__when_asyncio_timeout_nested_inside__then_timeout_raises():
    event = asyncio.Event()  # never fires

    with Fence(EventTrigger(event)) as fence:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.001):
                await asyncio.sleep(10)

    assert not fence.cancelled


async def test__fence__when_asyncio_timeout_zero_nested_inside__then_timeout_raises():
    event = asyncio.Event()  # never fires

    with pytest.raises(TimeoutError):
        with Fence(EventTrigger(event)) as fence:
            async with asyncio.timeout(0):
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
