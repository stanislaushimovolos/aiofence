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


async def test__fence__when_zero_timeout__then_suppressed_and_cancelled():
    with Fence(TimeoutTrigger(0)) as fence:
        await asyncio.sleep(1)

    assert fence.cancelled


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


async def test__fence__when_reenter__then_raises_runtime_error():
    fence = Fence()
    with fence:
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="cannot be reused"):
        with fence:
            pass
