import asyncio

import pytest

from constellate import EventTrigger, Fence, TimeoutTrigger

# --- Fence inside TaskGroup body ---


async def test__fence__when_trigger_fires_in_tg_body__then_tg_exits_normally():
    result = None

    async with asyncio.TaskGroup() as tg:
        tg.create_task(asyncio.sleep(0))  # dummy child

        fence = Fence(TimeoutTrigger(0.001))
        with fence:
            await asyncio.sleep(1)

        result = "continued"

    assert fence.cancelled
    assert result == "continued"


async def test__fence__when_pretriggered_in_tg_body__then_tg_exits_normally():
    result = None

    async with asyncio.TaskGroup() as tg:
        tg.create_task(asyncio.sleep(0))

        fence = Fence(TimeoutTrigger(0))
        with fence:
            await asyncio.sleep(1)

        result = "continued"

    assert fence.cancelled
    assert result == "continued"


# --- Fence inside child task ---


async def test__fence__when_trigger_fires_in_child_task__then_tg_unaffected():
    child_result = None

    async def child():
        nonlocal child_result
        fence = Fence(TimeoutTrigger(0.001))
        with fence:
            await asyncio.sleep(1)
        child_result = fence.cancelled

    async with asyncio.TaskGroup() as tg:
        tg.create_task(child())

    assert child_result is True


async def test__fence__when_child_fails_while_another_fenced__then_yields_to_tg():
    fence_cancelled = None
    fence_suppressed = None

    async def fenced_child():
        nonlocal fence_cancelled, fence_suppressed
        fence = Fence(EventTrigger(asyncio.Event()))  # never fires
        with fence:
            await asyncio.sleep(10)
        fence_cancelled = fence.cancelled
        fence_suppressed = True  # should not reach

    async def failing_child():
        await asyncio.sleep(0.01)
        raise ValueError("boom")

    with pytest.raises(ExceptionGroup) as exc_info:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(fenced_child())
            tg.create_task(failing_child())

    assert exc_info.group_contains(ValueError)
    assert fence_cancelled is None  # never reached — CancelledError propagated
    assert fence_suppressed is None


# --- TaskGroup inside Fence ---


async def test__fence__when_tg_child_fails_inside_fence__then_excgroup_propagates():
    async def failing():
        raise ValueError("boom")

    fence = Fence(EventTrigger(asyncio.Event()))  # never fires

    with pytest.raises(ExceptionGroup) as exc_info:
        with fence:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(failing())

    assert exc_info.group_contains(ValueError)
    assert not fence.cancelled


async def test__fence__when_trigger_fires_while_tg_active__then_fence_suppresses():
    child_was_cancelled = None

    async def long_child():
        nonlocal child_was_cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            child_was_cancelled = True
            raise

    fence = Fence(TimeoutTrigger(0.01))
    with fence:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(long_child())

    assert fence.cancelled
    assert child_was_cancelled is True


# --- Simultaneous: Fence trigger + child failure ---


async def test__fence__when_trigger_and_child_fail_simultaneously__then_excgroup():
    fence_cancelled = None

    async def failing():
        raise ValueError("boom")

    async def fenced_body():
        nonlocal fence_cancelled
        fence = Fence(TimeoutTrigger(0))  # pre-triggered
        try:
            with fence:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(failing())
        finally:
            fence_cancelled = fence.cancelled

    task = asyncio.get_running_loop().create_task(fenced_body())

    with pytest.raises(ExceptionGroup) as exc_info:
        await task

    assert exc_info.group_contains(ValueError)
    assert fence_cancelled is True


# --- Multiple children with independent fences ---


async def test__fence__when_multiple_children_with_fence__then_independent():
    results: dict[str, bool] = {}

    async def child_with_fence(name: str, delay: float):
        fence = Fence(TimeoutTrigger(delay))
        with fence:
            await asyncio.sleep(1)
        results[name] = fence.cancelled

    async with asyncio.TaskGroup() as tg:
        tg.create_task(child_with_fence("fast", 0.001))
        tg.create_task(child_with_fence("slow", 0.01))
        tg.create_task(child_with_fence("never", 10))

    assert results["fast"] is True
    assert results["slow"] is True
    assert results["never"] is False
