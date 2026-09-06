import asyncio
import logging

import pytest

from aiofence import CancelReason, Fence
from tests.helpers import deadline_in


def allow(_: CancelReason) -> bool:
    return True


def decline(_: CancelReason) -> bool:
    return False


def raising(_: CancelReason) -> bool:
    raise ValueError("policy boom")


def decline_code(code: str):
    return lambda reason: reason.code != code


@pytest.fixture
def set_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


@pytest.fixture
def two_events() -> tuple[asyncio.Event, asyncio.Event]:
    return asyncio.Event(), asyncio.Event()


# --- Policy allows ---


async def test__fence__when_policy_allows__then_cancelled():
    with Fence(deadline=deadline_in(0), deadline_code="to", policy=allow) as fence:
        await asyncio.sleep(10)

    assert fence.suppressed
    assert fence.cancelled_by("to")
    assert fence.declined_reasons == ()


async def test__fence__when_no_policy__then_declined_reasons_empty():
    with Fence(deadline=deadline_in(0)) as fence:
        await asyncio.sleep(10)

    assert fence.cancelled
    assert fence.declined_reasons == ()
    assert not fence.declined_by("to")


# --- Policy declines ---


async def test__fence__when_policy_declines_pre_check__then_body_runs_on(set_event):
    reached_after_await = False

    with Fence(events=[(set_event, "ev")], policy=decline) as fence:
        await asyncio.sleep(0)
        reached_after_await = True

    assert reached_after_await
    assert not fence.cancelled
    assert not fence.suppressed
    assert fence.declined_by("ev")
    assert len(fence.declined_reasons) == 1


async def test__fence__when_policy_declines_live_trigger__then_body_runs_on():
    event = asyncio.Event()
    reached_after_await = False

    async def fire() -> None:
        event.set()

    task = asyncio.create_task(fire())
    with Fence(events=[(event, "ev")], policy=decline) as fence:
        await asyncio.sleep(0.01)
        reached_after_await = True
    await task

    assert reached_after_await
    assert not fence.cancelled
    assert fence.declined_by("ev")


async def test__fence__when_policy_declines__then_declined_by_other_code_false(set_event):
    with Fence(events=[(set_event, "ev")], policy=decline) as fence:
        await asyncio.sleep(0)

    assert not fence.declined_by("other")


async def test__fence__when_policy_declines__then_reason_recorded_intact(set_event):
    with Fence(events=[(set_event, "ev")], policy=decline) as fence:
        await asyncio.sleep(0)

    reason = fence.declined_reasons[0]
    assert reason.code == "ev"
    assert reason.message == f"event {set_event!r} set"


# --- Pre-check declined still arms ---


async def test__fence__when_pre_check_declined_and_live_timeout__then_timeout_fires(set_event):
    with Fence(
        deadline=deadline_in(0.01),
        deadline_code="to",
        events=[(set_event, "ev")],
        policy=decline_code("ev"),
    ) as fence:
        await asyncio.sleep(10)

    assert fence.suppressed
    assert fence.cancelled_by("to")
    assert fence.declined_by("ev")
    assert not fence.cancelled_by("ev")


async def test__fence__when_pre_check_declined__then_no_cancel_scheduled(set_event):
    with Fence(events=[(set_event, "ev")], policy=decline) as fence:
        assert not fence._cancel_sent
        assert fence.declined_by("ev")


# --- Mixed decisions across events ---


async def test__fence__when_two_live_triggers_one_declined__then_allowed_cancels(two_events):
    declined_event, allowed_event = two_events

    async def fire() -> None:
        declined_event.set()
        await asyncio.sleep(0.01)
        allowed_event.set()

    task = asyncio.create_task(fire())
    events = [(declined_event, "a"), (allowed_event, "b")]
    with Fence(events=events, policy=decline_code("a")) as fence:
        await asyncio.sleep(10)
    await task

    assert fence.suppressed
    assert fence.cancelled_by("b")
    assert not fence.cancelled_by("a")
    assert fence.declined_by("a")


async def test__fence__when_cancel_delivered_then_second_declined__then_recorded_no_second_cancel():
    event = asyncio.Event()

    async def fire() -> None:
        event.set()

    task = asyncio.create_task(fire())
    with Fence(events=[(event, "a"), (event, "b")], policy=decline_code("b")) as fence:
        await asyncio.sleep(10)
    await task

    assert fence.suppressed
    assert fence.cancel_reasons == (fence.cancel_reasons[0],)
    assert fence.cancelled_by("a")
    assert fence.declined_by("b")


# --- Policy invocation ---


async def test__fence__when_reason_produced__then_policy_called_once_with_reason(set_event):
    seen: list[CancelReason] = []

    def policy(reason: CancelReason) -> bool:
        seen.append(reason)
        return True

    with Fence(events=[(set_event, "ev")], policy=policy):
        await asyncio.sleep(10)

    assert len(seen) == 1
    assert seen[0].code == "ev"


async def test__fence__when_policy_not_needed__then_not_called():
    calls = 0

    def policy(_: CancelReason) -> bool:
        nonlocal calls
        calls += 1
        return True

    with Fence(deadline=deadline_in(100), policy=policy):
        pass

    assert calls == 0


# --- Policy raises ---


async def test__fence__when_policy_raises_on_live_trigger__then_cancel_delivered_and_logged(caplog):
    with caplog.at_level(logging.ERROR, logger="aiofence"):
        with Fence(deadline=deadline_in(0.001), deadline_code="to", policy=raising) as fence:
            await asyncio.sleep(10)

    assert fence.suppressed
    assert fence.cancelled_by("to")
    assert fence.declined_reasons == ()
    assert any("policy boom" in r.getMessage() or r.exc_info for r in caplog.records)


async def test__fence__when_policy_raises_in_pre_check__then_cancel_scheduled_and_logged(caplog):
    with caplog.at_level(logging.ERROR, logger="aiofence"):
        with Fence(deadline=deadline_in(0), deadline_code="to", policy=raising) as fence:
            await asyncio.sleep(10)

    assert fence.suppressed
    assert fence.cancelled_by("to")
    assert len(caplog.records) == 1


async def test__fence__when_policy_raises_in_pre_check__then_next_fence_enters_normally():
    with Fence(deadline=deadline_in(0), policy=raising):
        await asyncio.sleep(10)

    with Fence(deadline=deadline_in(100)) as following:
        await asyncio.sleep(0)

    assert not following.cancelled
