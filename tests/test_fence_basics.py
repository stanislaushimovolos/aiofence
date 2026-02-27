import asyncio

import pytest

from constellate import Fence, TimeoutTrigger


async def test__fence__when_no_sources_async__then_protocol_intact():
    with Fence() as fence:
        await asyncio.sleep(0)

    assert not fence.cancelled


async def test__fence__when_no_sources_sync__then_protocol_intact():
    with Fence() as fence:
        pass

    assert not fence.cancelled


async def test__fence__when_zero_timeout__then_body_interrupted_at_await():
    reached_before_await = False
    reached_after_await = False

    with Fence(TimeoutTrigger(0)) as fence:
        reached_before_await = True
        await asyncio.sleep(0)
        reached_after_await = True

    assert fence.cancelled
    assert reached_before_await
    assert not reached_after_await


async def test__fence__when_zero_timeout_sync_body__then_body_completes():
    reached = False

    with Fence(TimeoutTrigger(0)) as fence:
        reached = True

    assert fence.cancelled
    assert reached


async def test__fence__when_body_raises__then_exception_propagates():
    with pytest.raises(ValueError, match="boom"):
        with Fence() as fence:
            raise ValueError("boom")

    assert not fence.cancelled

    fence = Fence()
    with fence:
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="cannot be reused"):
        with fence:
            pass


async def test__fence__cancel_before_enter__then_raises():
    fence = Fence()
    with pytest.raises(RuntimeError, match="before __enter__"):
        fence._schedule_cancel()
