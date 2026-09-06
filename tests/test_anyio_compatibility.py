"""
What the fence and anyio can see of each other.

Issue 1: a cancel the fence does not own — an outer anyio scope, an outer
``asyncio.timeout``, or a plain ``task.cancel()`` — tears the body down, but
the fence records nothing. After the block it is indistinguishable from a
fence whose body completed. It should be recorded as a reason under
``EXTERNAL_CODE`` that propagates instead of being suppressed.

Issue 2: ``TimeoutTrigger`` is a bare loop timer on a deadline-less scope, so
``anyio.current_effective_deadline()`` inside the fence does not reflect it.
"""

import asyncio
import math

import anyio
import pytest

from aiofence import (
    EXTERNAL_CODE,
    CancelReason,
    CancelType,
    EventTrigger,
    Fence,
    Fencing,
    TimeoutTrigger,
)

pytestmark = pytest.mark.backend("anyio")


class BodyError(Exception):
    pass


# --- Issue 1: external cancellation is invisible to the fence ---


async def test__fence__when_outer_anyio_scope_cancels__then_external_reason_recorded():
    event = asyncio.Event()

    with anyio.move_on_after(0.01):
        with Fence(EventTrigger(event)) as fence:
            await asyncio.sleep(10)

    assert fence.cancelled_by(EXTERNAL_CODE)
    assert fence.cancel_reasons[0].cancel_type is CancelType.EXTERNAL
    assert not fence.suppressed


async def test__fence__when_outer_asyncio_timeout_cancels__then_external_reason_recorded():
    event = asyncio.Event()

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            with Fence(EventTrigger(event)) as fence:
                await asyncio.sleep(10)

    assert fence.cancelled_by(EXTERNAL_CODE)
    assert not fence.suppressed


async def test__fence__when_task_cancelled_from_outside__then_external_reason_recorded():
    event = asyncio.Event()
    entered = asyncio.Event()
    fences: list[Fence] = []

    async def worker() -> None:
        with Fence(EventTrigger(event)) as fence:
            fences.append(fence)
            entered.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(worker())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert fences[0].cancelled_by(EXTERNAL_CODE)


async def test__fence__when_body_completes__then_no_reason():
    event = asyncio.Event()

    with Fence(EventTrigger(event)) as fence:
        await asyncio.sleep(0)

    assert not fence.cancelled


async def test__fence__when_own_trigger_fires__then_no_external_reason():
    with Fence(TimeoutTrigger(0.01)) as fence:
        await asyncio.sleep(10)

    assert fence.suppressed
    assert not fence.cancelled_by(EXTERNAL_CODE)


async def test__fence__when_body_raises__then_no_reason():
    event = asyncio.Event()

    with pytest.raises(BodyError):
        with Fence(EventTrigger(event)) as fence:
            raise BodyError

    assert not fence.cancelled


async def test__fence__when_outer_and_own_trigger_both_fire__then_both_recorded_not_suppressed():
    with anyio.move_on_after(0.01):
        with Fence(TimeoutTrigger(0.01)) as fence:
            await asyncio.sleep(10)

    assert fence.cancelled_by(EXTERNAL_CODE)
    assert any(r.cancel_type is CancelType.TIMEOUT for r in fence.cancel_reasons)
    assert not fence.suppressed


async def test__fence__when_outer_cancels__then_policy_not_consulted():
    event = asyncio.Event()
    seen: list[CancelReason] = []

    def policy(reason: CancelReason) -> bool:
        seen.append(reason)
        return False

    with anyio.move_on_after(0.01):
        with Fence(EventTrigger(event), policy=policy) as fence:
            await asyncio.sleep(10)

    assert seen == []
    assert fence.cancelled_by(EXTERNAL_CODE)
    assert fence.declined_reasons == ()


# --- Issue 2: the fence timeout is invisible to anyio's effective deadline ---


def _remaining() -> float:
    return anyio.current_effective_deadline() - asyncio.get_running_loop().time()


async def test__timeout_trigger__when_alone__then_effective_deadline_matches():
    with Fence(TimeoutTrigger(1)):
        assert _remaining() == pytest.approx(1, abs=0.05)


async def test__timeout_trigger__when_outer_anyio_deadline_is_looser__then_fence_deadline_wins():
    loop = asyncio.get_running_loop()

    with anyio.CancelScope(deadline=loop.time() + 5):
        with Fence(TimeoutTrigger(1)):
            await asyncio.sleep(0)
            assert _remaining() == pytest.approx(1, abs=0.05)


async def test__timeout_trigger__when_outer_anyio_deadline_is_tighter__then_outer_wins():
    loop = asyncio.get_running_loop()

    with anyio.CancelScope(deadline=loop.time() + 0.5):
        with Fence(TimeoutTrigger(1)):
            await asyncio.sleep(0)
            assert _remaining() == pytest.approx(0.5, abs=0.05)


async def test__timeout_trigger__when_fence_exits__then_effective_deadline_restored():
    with Fence(TimeoutTrigger(1)):
        pass

    assert anyio.current_effective_deadline() == math.inf


async def test__fencing_timeout__when_entered__then_effective_deadline_matches():
    with Fencing().timeout(1).move_on_cancel():
        assert _remaining() == pytest.approx(1, abs=0.05)


async def test__fencing_deadline__when_entered__then_effective_deadline_matches():
    when = asyncio.get_running_loop().time() + 1

    with Fencing().deadline(when).move_on_cancel():
        assert anyio.current_effective_deadline() == pytest.approx(when, abs=0.05)


async def test__timeout_trigger__when_fired__then_effective_deadline_is_past():
    with Fence(TimeoutTrigger(0.01)):
        try:
            await asyncio.sleep(10)
        finally:
            assert anyio.current_effective_deadline() == -math.inf
