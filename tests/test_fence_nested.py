import asyncio

import pytest

from aiofence import EXTERNAL_CODE, EventTrigger, Fence, FenceCancelled, Fencing, TimeoutTrigger

pytestmark = pytest.mark.backend("anyio")

# --- Basic nesting ---


async def test__nested_fence__when_inner_triggers__then_inner_suppresses_outer_continues():
    with Fence(TimeoutTrigger(10)) as outer:
        with Fence(TimeoutTrigger(0)) as inner:
            await asyncio.sleep(10)
        assert inner.suppressed
        assert inner.cancelled

    assert not outer.suppressed
    assert not outer.cancelled


async def test__nested_fence__when_shared_trigger__then_inner_propagates_outer_suppresses():
    shutdown = asyncio.Event()

    async def body():
        shutdown.set()
        await asyncio.sleep(10)

    with Fence(EventTrigger(shutdown)) as outer, Fence(EventTrigger(shutdown)) as inner:
        await body()

    assert inner.cancelled
    assert not inner.suppressed
    assert outer.cancelled
    assert outer.suppressed


async def test__nested_fence__when_both_fire_different_triggers__then_both_suppress_own():
    shutdown = asyncio.Event()

    with Fence(EventTrigger(shutdown)) as outer:
        with Fence(TimeoutTrigger(0)) as inner:
            await asyncio.sleep(10)
        assert inner.suppressed

        shutdown.set()
        await asyncio.sleep(10)

    assert outer.suppressed
    assert outer.cancelled


# --- External cancellation ---


async def test__nested_fence__when_external_cancel__then_propagates_through_both():
    fence_inner_suppressed = None
    fence_outer_suppressed = None

    async def task_body():
        nonlocal fence_inner_suppressed, fence_outer_suppressed
        event = asyncio.Event()
        try:
            with Fence(EventTrigger(event)) as outer:
                with Fence(EventTrigger(event)) as inner:
                    await asyncio.sleep(10)
        finally:
            fence_inner_suppressed = inner.suppressed
            fence_outer_suppressed = outer.suppressed

    task = asyncio.get_running_loop().create_task(task_body())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not fence_inner_suppressed
    assert not fence_outer_suppressed


async def test__nested_fence__when_external_and_inner_trigger__then_external_propagates():
    fence_cancelled = None
    fence_suppressed = None

    async def task_body():
        nonlocal fence_cancelled, fence_suppressed
        try:
            with Fence(TimeoutTrigger(0)) as fence, Fence(TimeoutTrigger(10)):
                await asyncio.sleep(10)
        finally:
            fence_cancelled = fence.cancelled
            fence_suppressed = fence.suppressed

    task = asyncio.get_running_loop().create_task(task_body())
    asyncio.get_running_loop().call_soon(task.cancel)

    with pytest.raises(asyncio.CancelledError):
        await task

    assert fence_cancelled
    assert not fence_suppressed


async def test__nested_fence__when_external_cancel_no_triggers__then_both_record_external():
    fence_inner_cancelled = None
    fence_outer_cancelled = None

    async def task_body():
        nonlocal fence_inner_cancelled, fence_outer_cancelled
        event = asyncio.Event()
        try:
            with Fence(EventTrigger(event)) as outer:
                with Fence(TimeoutTrigger(10)) as inner:
                    await asyncio.sleep(10)
        finally:
            fence_inner_cancelled = inner.cancelled_by(EXTERNAL_CODE)
            fence_outer_cancelled = outer.cancelled_by(EXTERNAL_CODE)

    task = asyncio.get_running_loop().create_task(task_body())
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert fence_inner_cancelled
    assert fence_outer_cancelled


# --- Three-deep nesting ---


async def test__nested_fence__when_three_deep_inner_triggers__then_only_inner_suppresses():
    with Fence(TimeoutTrigger(10)) as outer:
        with Fence(TimeoutTrigger(10)) as middle:
            with Fence(TimeoutTrigger(0)) as inner:
                await asyncio.sleep(10)
            assert inner.suppressed
        assert not middle.suppressed
    assert not outer.suppressed


async def test__nested_fence__when_three_deep_shared_trigger__then_outer_suppresses():
    shutdown = asyncio.Event()

    with Fence(EventTrigger(shutdown)) as outer, Fence(EventTrigger(shutdown)) as middle:
        with Fence(EventTrigger(shutdown)) as inner:
            shutdown.set()
            await asyncio.sleep(10)

    assert inner.cancelled
    assert not inner.suppressed
    assert middle.cancelled
    assert not middle.suppressed
    assert outer.cancelled
    assert outer.suppressed


async def test__nested_fence__when_three_deep_middle_triggers__then_middle_suppresses():
    middle_event = asyncio.Event()

    with Fence(TimeoutTrigger(10)) as outer:
        with Fence(EventTrigger(middle_event)) as middle:
            with Fence(EventTrigger(middle_event)):
                middle_event.set()
                await asyncio.sleep(10)
            # inner backed off, middle suppresses
        assert middle.suppressed

    assert not outer.suppressed
    assert not outer.cancelled


async def test__nested_fence__when_three_deep_shared_trigger__then_counter_balanced():
    task = asyncio.current_task()
    baseline = task.cancelling()
    shutdown = asyncio.Event()

    with Fence(EventTrigger(shutdown)), Fence(EventTrigger(shutdown)):
        with Fence(EventTrigger(shutdown)):
            shutdown.set()
            await asyncio.sleep(10)

    assert task.cancelling() == baseline


# --- Pre-triggered ---


async def test__nested_fence__when_inner_pre_triggered__then_inner_suppresses():
    with Fence(TimeoutTrigger(10)) as outer:
        with Fence(TimeoutTrigger(0)) as inner:
            await asyncio.sleep(0)
        assert inner.suppressed
        assert inner.cancelled

    assert not outer.suppressed
    assert not outer.cancelled


async def test__nested_fence__when_both_pre_triggered__then_outer_suppresses():
    with Fence(TimeoutTrigger(0)) as outer, Fence(TimeoutTrigger(0)) as inner:
        await asyncio.sleep(10)

    assert inner.cancelled
    assert not inner.suppressed
    assert outer.cancelled
    assert outer.suppressed


async def test__nested_fence__when_outer_pre_triggered_inner_not__then_outer_suppresses():
    with Fence(TimeoutTrigger(0)) as outer, Fence(TimeoutTrigger(10)) as inner:
        await asyncio.sleep(10)

    assert not inner.suppressed
    assert outer.cancelled
    assert outer.suppressed


# --- Sequential (no regression) ---


async def test__fence__when_sequential__then_independent():
    with Fence(TimeoutTrigger(0)) as first:
        await asyncio.sleep(10)

    with Fence(TimeoutTrigger(0)) as second:
        await asyncio.sleep(10)

    assert first.suppressed
    assert second.suppressed


async def test__nested_fence__when_sequential_inners__then_each_independent():
    with Fence(TimeoutTrigger(10)) as outer:
        with Fence(TimeoutTrigger(0)) as first:
            await asyncio.sleep(10)
        assert first.suppressed

        with Fence(TimeoutTrigger(0)) as second:
            await asyncio.sleep(10)
        assert second.suppressed

        await asyncio.sleep(0)

    assert not outer.suppressed
    assert not outer.cancelled


async def test__nested_fence__when_sequential_inners_second_clean__then_no_interference():
    with Fence(TimeoutTrigger(10)) as outer:
        with Fence(TimeoutTrigger(0)) as first:
            await asyncio.sleep(10)
        assert first.suppressed

        with Fence(TimeoutTrigger(10)) as second:
            await asyncio.sleep(0)
        assert not second.suppressed
        assert not second.cancelled

    assert not outer.suppressed


# --- Sync body ---


async def test__nested_fence__when_inner_pre_triggered_sync_body__then_no_cancel():
    with Fence(TimeoutTrigger(10)) as outer:
        with Fence(TimeoutTrigger(0)) as inner:
            pass
        assert not inner.suppressed
        assert inner.cancelled

    assert not outer.suppressed
    assert not outer.cancelled


async def test__nested_fence__when_both_pre_triggered_sync_body__then_no_cancel_delivered():
    with Fence(TimeoutTrigger(0)) as outer:
        with Fence(TimeoutTrigger(0)) as inner:
            pass
        # inner body sync, cancel rescinded
        assert inner.cancelled
        assert not inner.suppressed

    # outer body also completes before call_soon fires
    assert outer.cancelled
    assert not outer.suppressed


# --- asyncio.timeout interop ---


async def test__nested_fence__when_asyncio_timeout_inside__then_timeout_raises():
    event = asyncio.Event()

    with Fence(EventTrigger(event)) as outer, Fence(EventTrigger(event)) as inner:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.001):
                await asyncio.sleep(10)

    assert not inner.suppressed
    assert not outer.suppressed


async def test__nested_fence__when_asyncio_timeout_wraps__then_timeout_propagates():
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            with Fence(TimeoutTrigger(10)) as outer:
                with Fence(TimeoutTrigger(10)) as inner:
                    await asyncio.sleep(10)

    assert not inner.suppressed
    assert not outer.suppressed


async def test__nested_fence__when_inner_suppresses_then_asyncio_timeout__then_timeout_works():
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            with Fence(TimeoutTrigger(10)):
                with Fence(TimeoutTrigger(0)) as inner:
                    await asyncio.sleep(10)
                assert inner.suppressed
                await asyncio.sleep(10)  # asyncio.timeout fires here


# --- Counter balance ---


async def test__nested_fence__counter_balanced_after_exit():
    task = asyncio.current_task()
    baseline = task.cancelling()

    shutdown = asyncio.Event()

    with Fence(EventTrigger(shutdown)), Fence(EventTrigger(shutdown)):
        shutdown.set()
        await asyncio.sleep(10)

    assert task.cancelling() == baseline


# --- Buffering during cancellation ---


async def test__nested_fence__when_outer_fires_during_inner_cancel__then_outer_cancels_next():
    shutdown = asyncio.Event()
    between_fences_ran = False

    with Fence(EventTrigger(shutdown)) as outer:
        with Fence(TimeoutTrigger(0.001)) as inner:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                shutdown.set()
                raise
        between_fences_ran = True
        await asyncio.sleep(10)

    assert between_fences_ran
    assert inner.suppressed
    assert outer.cancelled
    assert outer.suppressed


async def test__nested_fence__when_multiple_triggers_fire_during_inner_cancel__then_all_recorded():
    event_a = asyncio.Event()
    event_b = asyncio.Event()

    with Fence(EventTrigger(event_a), EventTrigger(event_b)) as outer:
        with Fence(TimeoutTrigger(0.001)) as inner:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                event_a.set()
                event_b.set()
                raise
        await asyncio.sleep(10)

    assert inner.suppressed
    assert outer.cancelled
    assert outer.suppressed
    assert len(outer.cancel_reasons) == 2


# --- cancelled_by with nesting ---


async def test__nested_fence__cancelled_by_code_preserved():
    shutdown = asyncio.Event()

    with Fence(EventTrigger(shutdown, code="shutdown")) as outer:
        with Fence(TimeoutTrigger(0, code="timeout")) as inner:
            await asyncio.sleep(10)

    assert inner.cancelled_by("timeout")
    assert not inner.cancelled_by("shutdown")
    assert not outer.cancelled


async def test__nested_fence__when_shared_trigger_with_code__then_both_record_code():
    shutdown = asyncio.Event()

    with Fence(EventTrigger(shutdown, code="shutdown")) as outer:
        with Fence(EventTrigger(shutdown, code="shutdown")) as inner:
            shutdown.set()
            await asyncio.sleep(10)

    assert inner.cancelled_by("shutdown")
    assert outer.cancelled_by("shutdown")
    assert not inner.suppressed
    assert outer.suppressed


# --- Code between fences control flow ---


async def test__nested_fence__when_shared_trigger__then_code_between_fences_skipped():
    shutdown = asyncio.Event()
    between_fences = False

    with Fence(EventTrigger(shutdown)) as outer:
        with Fence(EventTrigger(shutdown)) as inner:
            shutdown.set()
            await asyncio.sleep(10)
        between_fences = True
        await asyncio.sleep(0)

    assert not between_fences
    assert not inner.suppressed
    assert outer.suppressed


async def test__nested_fence__when_inner_own_trigger__then_code_between_fences_runs():
    between_fences = False

    with Fence(TimeoutTrigger(10)) as outer:
        with Fence(TimeoutTrigger(0)) as inner:
            await asyncio.sleep(10)
        between_fences = True
        await asyncio.sleep(0)

    assert inner.suppressed
    assert between_fences
    assert not outer.suppressed


async def test__nested_fence__when_inner_suppresses__then_multiple_awaits_after_inner_work():
    steps = []

    with Fence(TimeoutTrigger(10)) as outer:
        with Fence(TimeoutTrigger(0)) as inner:
            await asyncio.sleep(10)
        steps.append("after_inner")
        await asyncio.sleep(0)
        steps.append("second_await")
        await asyncio.sleep(0)
        steps.append("third_await")

    assert inner.suppressed
    assert steps == ["after_inner", "second_await", "third_await"]
    assert not outer.suppressed


# --- Error propagation through nested fences ---


async def test__nested_fence__when_body_raises_value_error__then_propagates():
    with pytest.raises(ValueError, match="boom"), Fence(TimeoutTrigger(10)) as outer:
        with Fence(TimeoutTrigger(10)) as inner:
            raise ValueError("boom")

    assert not inner.suppressed
    assert not inner.cancelled
    assert not outer.suppressed
    assert not outer.cancelled


async def test__nested_fence__when_error_after_inner_suppresses__then_propagates():
    with pytest.raises(ValueError, match="cleanup failed"), Fence(TimeoutTrigger(10)) as outer:
        with Fence(TimeoutTrigger(0)) as inner:
            await asyncio.sleep(10)
        assert inner.suppressed
        raise ValueError("cleanup failed")

    assert not outer.suppressed


# --- Fence with no triggers ---


async def test__nested_fence__when_inner_has_no_triggers__then_noop():
    with Fence(TimeoutTrigger(10)) as outer:
        with Fence() as inner:
            await asyncio.sleep(0)
        assert not inner.suppressed
        assert not inner.cancelled

    assert not outer.suppressed


async def test__nested_fence__when_outer_has_no_triggers__then_inner_independent():
    with Fence() as outer:
        with Fence(TimeoutTrigger(0)) as inner:
            await asyncio.sleep(10)
        assert inner.suppressed

    assert not outer.suppressed
    assert not outer.cancelled


# --- Fence stack cleanup ---


async def test__nested_fence__when_inner_enter_fails__then_outer_stack_intact():
    reused = Fence(TimeoutTrigger(10))
    with reused:
        pass

    with Fence(TimeoutTrigger(10)) as outer:
        with pytest.raises(RuntimeError, match="cannot be reused"), reused:
            pass
        await asyncio.sleep(0)

    assert not outer.suppressed


# --- TaskGroup inside nested fences ---


async def test__nested_fence__when_taskgroup_child_cancelled__then_fence_unaffected():
    with Fence(TimeoutTrigger(10)) as outer, Fence(TimeoutTrigger(10)) as inner:
        with pytest.raises(asyncio.CancelledError):
            child = asyncio.create_task(asyncio.sleep(10))
            await asyncio.sleep(0)
            child.cancel()
            await child

    assert not inner.suppressed
    assert not outer.suppressed


# --- Fencing builder API with nesting ---


async def test__fencing__raise_on_cancel__with_nested_fence():
    shutdown = asyncio.Event()
    fencing = Fencing().event(shutdown)

    with pytest.raises(FenceCancelled), fencing.raise_on_cancel() as outer_fence:
        with Fence(TimeoutTrigger(0)) as inner:
            await asyncio.sleep(10)
        assert inner.suppressed
        shutdown.set()
        await asyncio.sleep(10)

    assert outer_fence.suppressed


async def test__fencing__move_on_cancel__with_nested_fence():
    shutdown = asyncio.Event()
    fencing = Fencing().event(shutdown, code="shutdown")

    with fencing.move_on_cancel() as outer_fence:
        with Fence(TimeoutTrigger(0)) as inner:
            await asyncio.sleep(10)
        assert inner.suppressed
        shutdown.set()
        await asyncio.sleep(10)

    assert outer_fence.suppressed
    assert outer_fence.cancelled_by("shutdown")


async def test__fencing__nested_raise_over_move_on_cancel():
    shutdown = asyncio.Event()

    with pytest.raises(FenceCancelled), Fencing().event(shutdown).raise_on_cancel():
        with Fencing().timeout(0).move_on_cancel() as inner:
            await asyncio.sleep(10)
        assert inner.suppressed
        shutdown.set()
        await asyncio.sleep(10)


async def test__fencing__nested_shared_event__outer_suppresses():
    shutdown = asyncio.Event()

    with Fencing().event(shutdown).move_on_cancel() as outer:
        with Fencing().event(shutdown).move_on_cancel() as inner:
            shutdown.set()
            await asyncio.sleep(10)

    assert inner.cancelled
    assert not inner.suppressed
    assert outer.cancelled
    assert outer.suppressed


async def test__fencing__move_on_cancel__with_sequential_inner_fences():
    with Fencing().timeout(10).move_on_cancel() as outer:
        with Fencing().timeout(0).move_on_cancel() as first:
            await asyncio.sleep(10)
        assert first.suppressed

        with Fencing().timeout(0).move_on_cancel() as second:
            await asyncio.sleep(10)
        assert second.suppressed

    assert not outer.suppressed


# --- Backend refusal ---


@pytest.mark.backend("native")
async def test__nested_fence__when_native_backend__then_inner_enter_raises():
    with Fence(TimeoutTrigger(10)) as outer:
        with pytest.raises(RuntimeError, match="NativeBackend does not support nested Fences"):
            with Fence(TimeoutTrigger(10)):
                await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert not outer.cancelled


@pytest.mark.backend("native")
async def test__nested_fence__when_native_refuses__then_outer_still_nests_later_anyio_fence():
    from aiofence import AnyioBackend

    with Fence(TimeoutTrigger(10)) as outer:
        with pytest.raises(RuntimeError, match="does not support nested"), Fence():
            pass

        with Fence(TimeoutTrigger(0), backend=AnyioBackend()) as inner:
            await asyncio.sleep(10)

    assert inner.suppressed
    assert not outer.cancelled
