import asyncio

import pytest

from constellate import (
    CancelReason,
    CancelType,
    Fence,
    FenceCancelled,
    FenceTimeout,
    TimeoutTrigger,
)


async def test__fence__when_no_sources_async__then_protocol_intact():
    with Fence() as fence:
        await asyncio.sleep(0)

    assert not fence.cancelled


async def test__fence__when_no_sources_sync__then_protocol_intact():
    with Fence() as fence:
        pass

    assert not fence.cancelled


async def test__fence__when_zero_timeout__then_body_does_not_execute_async():
    reached = False
    with pytest.raises(FenceTimeout):  # noqa: PT012
        with Fence(TimeoutTrigger(0)):
            await asyncio.sleep(0)
            reached = True
    assert not reached


async def test__fence__when_zero_timeout__then_body_does_not_execute_sync():
    reached = False
    with pytest.raises(FenceTimeout):
        with Fence(TimeoutTrigger(0)):
            reached = True
    assert not reached


async def test__fence__when_reenter__then_raises_runtime_error():
    fence = Fence()
    with fence:
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="cannot be reused"):
        with fence:
            pass


def test__fence_timeout__when_raised__then_caught_by_timeout_error():
    reason = CancelReason(message="timed out", cancel_type=CancelType.TIMEOUT)
    with pytest.raises(TimeoutError):
        raise FenceTimeout(reasons=(reason,))


def test__fence_timeout__when_raised__then_not_caught_by_cancelled_error():
    reason = CancelReason(message="timed out", cancel_type=CancelType.TIMEOUT)
    with pytest.raises(FenceTimeout):  # noqa: PT012
        try:
            raise FenceTimeout(reasons=(reason,))
        except asyncio.CancelledError:
            pytest.fail("FenceTimeout should not be caught by CancelledError")


def test__fence_cancelled__when_raised__then_caught_by_cancelled_error():
    reason = CancelReason(message="cancelled", cancel_type=CancelType.CANCELLED)
    with pytest.raises(asyncio.CancelledError):
        raise FenceCancelled(reasons=(reason,))


def test__fence_cancelled__when_raised__then_not_caught_by_timeout_error():
    reason = CancelReason(message="cancelled", cancel_type=CancelType.CANCELLED)
    with pytest.raises(FenceCancelled):  # noqa: PT012
        try:
            raise FenceCancelled(reasons=(reason,))
        except TimeoutError:
            pytest.fail("FenceCancelled should not be caught by TimeoutError")
