import asyncio

import pytest

from aiofence import EventTrigger, Fence, TimeoutTrigger
from aiofence.core import CancelCallback, CancelReason, CancelType, Trigger, TriggerHandle

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
    assert fence.reasons[0].cancel_type is CancelType.EVENT


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


async def test__fence__when_trigger_fires_and_finally_raises__then_exception_propagates():
    task = asyncio.current_task()

    with pytest.raises(ValueError, match="boom"):
        with Fence(TimeoutTrigger(0.001)) as fence:
            try:
                await asyncio.sleep(1)
            finally:
                raise ValueError("boom")

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


async def test__fence__when_event_set_inside_body__then_body_interrupted_at_await():
    event = asyncio.Event()
    reached = False

    with Fence(EventTrigger(event)) as fence:
        event.set()
        await asyncio.sleep(0)
        reached = True

    assert fence.cancelled
    assert not reached


async def test__fence__when_event_set_inside_body__then_no_spurious_cancel_after_exit():
    event = asyncio.Event()

    with Fence(EventTrigger(event)) as fence:
        event.set()
        await asyncio.sleep(0)  # let callback fire

    assert fence.cancelled
    await asyncio.sleep(0)  # no spurious CancelledError after exit
    assert asyncio.current_task().cancelling() == 0


async def test__fence__when_event_has_code__then_reason_carries_code():
    event = asyncio.Event()
    asyncio.get_running_loop().call_soon(event.set)

    with Fence(EventTrigger(event, code="shutdown")) as fence:
        await asyncio.sleep(1)

    assert fence.cancelled
    assert fence.reasons[0].code == "shutdown"


async def test__fence__when_timeout_has_code__then_reason_carries_code():
    with Fence(TimeoutTrigger(0, code="request_budget")) as fence:
        await asyncio.sleep(1)

    assert fence.cancelled
    assert fence.reasons[0].code == "request_budget"


async def test__fence__when_no_code__then_reason_code_is_none():
    with Fence(TimeoutTrigger(0)) as fence:
        await asyncio.sleep(1)

    assert fence.cancelled
    assert fence.reasons[0].code is None


async def test__fence__cancelled_by__when_code_matches__then_true():
    event = asyncio.Event()
    asyncio.get_running_loop().call_soon(event.set)

    with Fence(EventTrigger(event, code="disconnect")) as fence:
        await asyncio.sleep(1)

    assert fence.cancelled_by("disconnect")
    assert not fence.cancelled_by("shutdown")


async def test__fence__cancelled_by__when_multiple_triggers__then_matches_any():
    event1 = asyncio.Event()
    event2 = asyncio.Event()

    with Fence(
        EventTrigger(event1, code="shutdown"), EventTrigger(event2, code="disconnect")
    ) as fence:
        event1.set()
        event2.set()
        await asyncio.sleep(1)

    assert fence.cancelled_by("shutdown")
    assert fence.cancelled_by("disconnect")
    assert not fence.cancelled_by("timeout")


async def test__fence__cancelled_by__when_not_cancelled__then_false():
    with Fence() as fence:
        await asyncio.sleep(0)

    assert not fence.cancelled_by("anything")


async def test__fence__when_multiple_triggers_fire__then_all_reasons_recorded():
    event1 = asyncio.Event()
    event2 = asyncio.Event()

    with Fence(EventTrigger(event1), EventTrigger(event2)) as fence:
        event1.set()
        event2.set()
        await asyncio.sleep(1)

    assert fence.cancelled
    assert len(fence.reasons) == 2


async def test__fence__when_trigger_fires_inline__then_raises_invalid_state():
    class InlineTrigger(Trigger):
        def check(self) -> CancelReason | None:
            return None

        def arm(self, on_cancel: CancelCallback) -> TriggerHandle:
            on_cancel(CancelReason(message="inline", cancel_type=CancelType.EVENT))
            return _NoopHandle()

    class _NoopHandle(TriggerHandle):
        def disarm(self) -> None:
            pass

    with pytest.raises(asyncio.InvalidStateError, match="synchronously inside the task"):
        with Fence(InlineTrigger()):
            await asyncio.sleep(0)


async def test__fence__when_second_trigger_fires_during_cleanup__then_both_reasons_recorded():
    event1 = asyncio.Event()
    event2 = asyncio.Event()
    cleanup_done = False

    async def set_event2_later():
        await asyncio.sleep(0.01)
        event2.set()

    bg_task = asyncio.create_task(set_event2_later())  # noqa: RUF006, F841

    with Fence(EventTrigger(event1), EventTrigger(event2)) as fence:
        event1.set()
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            cleanup_done = True

    assert fence.cancelled
    assert cleanup_done
    assert len(fence.reasons) == 2
    assert asyncio.current_task().cancelling() == 0


async def test__fence__when_second_trigger_fires_after_fence__then_post_fence_async_works():
    event1 = asyncio.Event()
    event2 = asyncio.Event()

    async def set_event2_later():
        await asyncio.sleep(0.05)
        event2.set()

    bg_task = asyncio.create_task(set_event2_later())  # noqa: RUF006, F841

    with Fence(EventTrigger(event1), EventTrigger(event2)) as fence:
        event1.set()
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)

    # event2 fires here, after fence exited and disarmed triggers
    await asyncio.sleep(0.1)
    assert event1.is_set()
    assert event2.is_set()
    assert fence.cancelled
    assert len(fence.reasons) == 1
    assert asyncio.current_task().cancelling() == 0


async def test__fence__when_two_tasks_share_event__then_both_cancelled_and_waiters_cleaned():
    event = asyncio.Event()

    async def worker() -> Fence:
        with Fence(EventTrigger(event)) as fence:
            await asyncio.sleep(10)
        return fence

    t1 = asyncio.create_task(worker())
    t2 = asyncio.create_task(worker())
    await asyncio.sleep(0)

    assert len(event._waiters) == 2

    event.set()
    fence1, fence2 = await asyncio.gather(t1, t2)

    assert fence1.cancelled
    assert fence2.cancelled
    assert len(event._waiters) == 0
