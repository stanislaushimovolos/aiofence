import asyncio

import pytest

from aiofence import Fence, TimeoutTrigger


async def test__fence__when_no_sources_async__then_protocol_intact():
    with Fence() as fence:
        await asyncio.sleep(0)

    assert not fence.suppressed
    assert not fence.cancelled


async def test__fence__when_no_sources_sync__then_protocol_intact():
    with Fence() as fence:
        pass

    assert not fence.suppressed
    assert not fence.cancelled


async def test__fence__when_zero_timeout__then_body_interrupted_at_await():
    reached_before_await = False
    reached_after_await = False

    with Fence(TimeoutTrigger(0)) as fence:
        reached_before_await = True
        await asyncio.sleep(0)
        reached_after_await = True

    assert fence.suppressed
    assert fence.cancelled
    assert reached_before_await
    assert not reached_after_await


async def test__fence__when_zero_timeout_sync_body__then_body_completes():
    reached = False

    with Fence(TimeoutTrigger(0)) as fence:
        reached = True

    assert not fence.suppressed
    assert fence.cancelled
    assert reached


async def test__fence__when_body_raises__then_exception_propagates():
    with pytest.raises(ValueError, match="boom"):
        with Fence() as fence:
            raise ValueError("boom")

    assert not fence.suppressed
    assert not fence.cancelled

    fence = Fence()
    with fence:
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="cannot be reused"):
        with fence:
            pass


async def test__fence__cancel_before_enter__then_raises():
    fence = Fence()
    with pytest.raises(RuntimeError, match="not entered"):
        fence._cancel()


async def test__fence_task__when_not_entered__then_raises():
    fence = Fence()
    with pytest.raises(RuntimeError, match="not entered"):
        _ = fence.task


async def test__fence_handle__when_not_entered__then_raises():
    fence = Fence()
    with pytest.raises(RuntimeError, match="not entered"):
        _ = fence.handle


async def test__fence_task_and_handle__when_entered__then_current_task_and_backend_handle():
    with Fence() as fence:
        assert fence.task is asyncio.current_task()
        assert fence.handle is fence._handle


async def test__fence_task_and_handle__when_exited__then_raise():
    with Fence() as fence:
        pass

    with pytest.raises(RuntimeError, match="not entered"):
        _ = fence.task
    with pytest.raises(RuntimeError, match="not entered"):
        _ = fence.handle
