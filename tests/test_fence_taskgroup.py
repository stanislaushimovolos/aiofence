import asyncio

import pytest

from aiofence import EXTERNAL_CODE, EventTrigger, Fence, TimeoutTrigger

# --- Fence inside TaskGroup body ---


async def test__fence__when_trigger_fires_in_tg_body__then_tg_exits_normally():
    result = None

    async with asyncio.TaskGroup() as tg:
        tg.create_task(asyncio.sleep(0))  # dummy child

        fence = Fence(TimeoutTrigger(0.001))
        with fence:
            await asyncio.sleep(1)

        result = "continued"

    assert fence.suppressed
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

    assert fence.suppressed
    assert fence.cancelled
    assert result == "continued"


# --- Fence inside child task ---


async def test__fence__when_trigger_fires_in_child_task__then_tg_unaffected():
    child_result = None
    child_cancelled = None

    async def child():
        nonlocal child_result, child_cancelled
        fence = Fence(TimeoutTrigger(0.001))
        with fence:
            await asyncio.sleep(1)
        child_result = fence.suppressed
        child_cancelled = fence.cancelled

    async with asyncio.TaskGroup() as tg:
        tg.create_task(child())

    assert child_result is True
    assert child_cancelled is True


async def test__fence__when_child_fails_while_another_fenced__then_yields_to_tg():
    was_suppressed = None
    fence_suppressed = None

    async def fenced_child():
        nonlocal was_suppressed, fence_suppressed
        fence = Fence(EventTrigger(asyncio.Event()))  # never fires
        with fence:
            await asyncio.sleep(10)
        was_suppressed = fence.suppressed
        fence_suppressed = True  # should not reach

    async def failing_child():
        await asyncio.sleep(0.01)
        raise ValueError("boom")

    with pytest.raises(ExceptionGroup) as exc_info:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(fenced_child())
            tg.create_task(failing_child())

    assert exc_info.group_contains(ValueError)
    assert was_suppressed is None  # never reached — CancelledError propagated
    assert fence_suppressed is None


async def test__fence__when_child_fails_while_body_fenced__then_yields_to_tg():
    was_suppressed = None
    reached_after_fence = False

    async def failing_child():
        await asyncio.sleep(0.01)
        raise ValueError("boom")

    with pytest.raises(ExceptionGroup) as exc_info:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(failing_child())

            fence = Fence(EventTrigger(asyncio.Event()))  # never fires
            with fence:
                await asyncio.sleep(10)
            reached_after_fence = True
            was_suppressed = fence.suppressed

    assert exc_info.group_contains(ValueError)
    assert not reached_after_fence  # TG cancelled the body
    assert was_suppressed is None  # never reached


async def _trigger_fires_during_tg_teardown() -> Fence:
    cancel_event = asyncio.Event()
    fence = Fence(EventTrigger(cancel_event))

    async def failing_child():
        await asyncio.sleep(0.01)
        cancel_event.set()  # fires Fence's trigger in the same tick the TG cancels us
        raise ValueError("boom")

    with pytest.raises(ExceptionGroup) as exc_info:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(failing_child())
            with fence:
                await asyncio.sleep(10)

    assert exc_info.group_contains(ValueError)
    return fence


@pytest.mark.backend("native")
async def test__fence__when_trigger_fires_during_tg_teardown__then_yields_to_tg():
    fence = await _trigger_fires_during_tg_teardown()

    assert fence.suppressed is False  # counter says the TG's cancel is outstanding
    assert fence.cancelled is True


@pytest.mark.backend("anyio")
async def test__fence__when_trigger_fires_during_tg_teardown__then_suppresses():
    fence = await _trigger_fires_during_tg_teardown()

    assert fence.suppressed is True  # anyio sees only its own cancel; the TG's is absorbed
    assert not fence.cancelled_by(EXTERNAL_CODE)


async def test__fence__when_outer_fence_wraps_tg_with_inner_fence__then_independent():
    inner_suppressed = None
    inner_cancelled = None

    async def child():
        nonlocal inner_suppressed, inner_cancelled
        inner = Fence(TimeoutTrigger(0.001))
        with inner:
            await asyncio.sleep(1)
        inner_suppressed = inner.suppressed
        inner_cancelled = inner.cancelled

    outer = Fence(EventTrigger(asyncio.Event()))  # never fires
    with outer:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(child())

    assert inner_suppressed is True
    assert inner_cancelled is True
    assert not outer.suppressed
    assert not outer.cancelled


# --- TaskGroup inside Fence ---


async def test__fence__when_tg_body_raises_inside_fence__then_excgroup_propagates():
    fence = Fence(EventTrigger(asyncio.Event()))  # never fires

    with pytest.raises(ExceptionGroup) as exc_info:
        with fence:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(asyncio.sleep(10))
                raise ValueError("body boom")

    assert exc_info.group_contains(ValueError)
    assert not fence.suppressed
    assert not fence.cancelled


async def test__fence__when_tg_child_fails_inside_fence__then_excgroup_propagates():
    async def failing():
        raise ValueError("boom")

    fence = Fence(EventTrigger(asyncio.Event()))  # never fires

    with pytest.raises(ExceptionGroup) as exc_info:
        with fence:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(failing())

    assert exc_info.group_contains(ValueError)
    assert not fence.suppressed
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
        # TG re-raises CancelledError (parent task was cancelled),
        # Fence suppresses it — no ExceptionGroup since child
        # only had CancelledError
        async with asyncio.TaskGroup() as tg:
            tg.create_task(long_child())

    assert fence.suppressed
    assert fence.cancelled
    assert child_was_cancelled is True


async def test__fence__when_tg_externally_cancelled_with_body_fenced__then_propagates():
    fence_suppressed = None
    fence_cancelled = None
    child_was_cancelled = None

    async def long_child():
        nonlocal child_was_cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            child_was_cancelled = True
            raise

    async def body():
        nonlocal fence_suppressed, fence_cancelled
        fence = Fence(EventTrigger(asyncio.Event()))  # never fires
        try:
            with fence:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(long_child())
                    await asyncio.sleep(10)
        finally:
            fence_suppressed = fence.suppressed
            fence_cancelled = fence.cancelled_by(EXTERNAL_CODE)

    task = asyncio.get_running_loop().create_task(body())
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert child_was_cancelled is True  # TG cancelled its child during teardown
    assert fence_suppressed is False  # Fence's trigger never fired
    assert fence_cancelled is True


# --- Simultaneous: Fence trigger + child failure ---


async def test__fence__when_trigger_and_child_fail_simultaneously__then_excgroup():
    fence_suppressed = None
    fence_cancelled = None

    async def failing():
        raise ValueError("boom")

    async def fenced_body():
        nonlocal fence_suppressed, fence_cancelled
        fence = Fence(TimeoutTrigger(0))  # pre-triggered
        try:
            with fence:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(failing())
        finally:
            fence_suppressed = fence.suppressed
            fence_cancelled = fence.cancelled

    task = asyncio.get_running_loop().create_task(fenced_body())

    with pytest.raises(ExceptionGroup) as exc_info:
        await task

    assert exc_info.group_contains(ValueError)
    assert fence_suppressed is False  # trigger fired but ExceptionGroup propagated, not suppressed
    assert fence_cancelled is True


# --- Multiple children with independent fences ---


async def test__fence__when_multiple_children_with_fence__then_independent():
    suppressed_results: dict[str, bool] = {}
    cancelled_results: dict[str, bool] = {}

    async def child_with_fence(name: str, delay: float):
        fence = Fence(TimeoutTrigger(delay))
        with fence:
            await asyncio.sleep(1)
        suppressed_results[name] = fence.suppressed
        cancelled_results[name] = fence.cancelled

    async with asyncio.TaskGroup() as tg:
        tg.create_task(child_with_fence("fast", 0.001))
        tg.create_task(child_with_fence("slow", 0.01))
        tg.create_task(child_with_fence("never", 10))

    assert suppressed_results["fast"] is True
    assert cancelled_results["fast"] is True
    assert suppressed_results["slow"] is True
    assert cancelled_results["slow"] is True
    assert suppressed_results["never"] is False
    assert cancelled_results["never"] is False
