from __future__ import annotations

import asyncio

import pytest

from aiofence import (
    CancelReason,
    CancelType,
    EventTrigger,
    FenceCancelled,
    Fencing,
    TimeoutTrigger,
    on_deadline,
    on_event,
    on_timeout,
    on_trigger,
)

# --- Immutability ---


def test__timeout__when_called__then_returns_new_instance() -> None:
    base = Fencing()
    derived = base.timeout(5)
    assert derived is not base


def test__deadline__when_called__then_returns_new_instance() -> None:
    base = Fencing()
    derived = base.deadline(100)
    assert derived is not base


def test__event__when_called__then_returns_new_instance() -> None:
    base = Fencing()
    derived = base.event(asyncio.Event())
    assert derived is not base


def test__trigger__when_called__then_returns_new_instance() -> None:
    base = Fencing()
    derived = base.trigger(EventTrigger(asyncio.Event()))
    assert derived is not base


def test__base__when_derived__then_base_unchanged() -> None:
    base = Fencing()
    base.timeout(5, code="db")
    assert base._timeouts == ()
    assert base._explicit_triggers == ()
    assert base._deadline is None


# --- Simple timeout ---


async def test__timeout__then_fence_has_one_timeout_trigger() -> None:
    with Fencing().timeout(5).move_on_cancel() as fence:
        assert len(fence._triggers) == 1
        trigger = fence._triggers[0]
        assert isinstance(trigger, TimeoutTrigger)
        assert trigger._delay == pytest.approx(5, abs=0.001)


async def test__timeout__with_code__then_code_preserved() -> None:
    with Fencing().timeout(5, code="db").move_on_cancel() as fence:
        trigger = fence._triggers[0]
        assert trigger._code == "db"


# --- Deadline ---


async def test__deadline__then_fence_has_timeout_trigger_with_remaining() -> None:
    loop = asyncio.get_running_loop()
    when = loop.time() + 100

    with Fencing().deadline(when).move_on_cancel() as fence:
        assert len(fence._triggers) == 1
        trigger = fence._triggers[0]
        assert isinstance(trigger, TimeoutTrigger)
        assert trigger._delay == pytest.approx(100, abs=0.1)


async def test__deadline__with_code__then_code_preserved() -> None:
    loop = asyncio.get_running_loop()

    with Fencing().deadline(loop.time() + 100, code="sla").move_on_cancel() as fence:
        trigger = fence._triggers[0]
        assert trigger._code == "sla"


# --- Deadline merging (eager) ---


async def test__deadline__when_tighter_added_second__then_tighter_wins() -> None:
    loop = asyncio.get_running_loop()
    now = loop.time()
    ctx = Fencing().deadline(now + 100, code="loose").deadline(now + 10, code="tight")

    with ctx.move_on_cancel() as fence:
        trigger = fence._triggers[0]
        assert trigger._code == "tight"
        assert trigger._delay == pytest.approx(10, abs=0.001)


async def test__deadline__when_looser_added_second__then_original_wins() -> None:
    loop = asyncio.get_running_loop()
    now = loop.time()
    ctx = Fencing().deadline(now + 10, code="tight").deadline(now + 100, code="loose")

    with ctx.move_on_cancel() as fence:
        trigger = fence._triggers[0]
        assert trigger._code == "tight"
        assert trigger._delay == pytest.approx(10, abs=0.001)


# --- Timeout merging (lazy) ---


async def test__timeout__when_multiple__then_shortest_wins() -> None:
    ctx = Fencing().timeout(100, code="long").timeout(5, code="short")

    with ctx.move_on_cancel() as fence:
        assert len(fence._triggers) == 1
        trigger = fence._triggers[0]
        assert trigger._code == "short"
        assert trigger._delay == pytest.approx(5, abs=0.1)


# --- Mixed deadline + timeout ---


async def test__mixed__when_deadline_tighter__then_deadline_wins() -> None:
    loop = asyncio.get_running_loop()
    ctx = Fencing().deadline(loop.time() + 5, code="dl").timeout(100, code="to")

    with ctx.move_on_cancel() as fence:
        assert len(fence._triggers) == 1
        trigger = fence._triggers[0]
        assert trigger._code == "dl"
        assert trigger._delay == pytest.approx(5, abs=0.1)


async def test__mixed__when_timeout_tighter__then_timeout_wins() -> None:
    loop = asyncio.get_running_loop()
    ctx = Fencing().deadline(loop.time() + 100, code="dl").timeout(5, code="to")

    with ctx.move_on_cancel() as fence:
        assert len(fence._triggers) == 1
        trigger = fence._triggers[0]
        assert trigger._code == "to"
        assert trigger._delay == pytest.approx(5, abs=0.1)


# --- Event trigger ---


async def test__event__then_fence_has_event_trigger() -> None:
    ev = asyncio.Event()

    with Fencing().event(ev, code="shutdown").move_on_cancel() as fence:
        assert any(isinstance(t, EventTrigger) and t._event is ev for t in fence._triggers)


async def test__event__with_code__then_code_preserved() -> None:
    ev = asyncio.Event()

    with Fencing().event(ev, code="shutdown").move_on_cancel() as fence:
        event_trigger = next(t for t in fence._triggers if isinstance(t, EventTrigger))
        assert event_trigger._code == "shutdown"


# --- Duplicate events ---


async def test__event__when_same_event_same_code__then_collapses() -> None:
    ev = asyncio.Event()
    ctx = Fencing().event(ev, code="x").event(ev, code="x")

    with ctx.move_on_cancel() as fence:
        event_triggers = [t for t in fence._triggers if isinstance(t, EventTrigger)]
        assert len(event_triggers) == 1


async def test__event__when_same_event_different_codes__then_last_wins() -> None:
    ev = asyncio.Event()
    ctx = Fencing().event(ev, code="a").event(ev, code="b")

    with ctx.move_on_cancel() as fence:
        event_triggers = [t for t in fence._triggers if isinstance(t, EventTrigger)]
        assert len(event_triggers) == 1
        assert event_triggers[0]._code == "b"


# --- Custom trigger ---


async def test__trigger__then_trigger_in_fence() -> None:
    ev = asyncio.Event()
    custom = EventTrigger(ev, code="custom")

    with Fencing().trigger(custom).move_on_cancel() as fence:
        assert custom in fence._triggers


# --- Reusability ---


async def test__fencing__when_entered_twice__then_distinct_fences() -> None:
    ctx = Fencing().timeout(100)

    with ctx.move_on_cancel() as f1:
        pass

    with ctx.move_on_cancel() as f2:
        pass

    assert f1 is not f2


# --- Combined timeout + event ---


async def test__timeout_and_event__then_both_in_fence() -> None:
    ev = asyncio.Event()
    ctx = Fencing().timeout(5, code="to").event(ev, code="ev")

    with ctx.move_on_cancel() as fence:
        assert len(fence._triggers) == 2
        assert isinstance(fence._triggers[0], TimeoutTrigger)
        assert isinstance(fence._triggers[1], EventTrigger)


# --- Early exit ---


async def test__move_on_cancel__when_pre_triggered__then_cancelled_before_await() -> None:
    with Fencing().timeout(0).move_on_cancel() as fence:
        assert fence.cancelled


# --- Empty Fencing ---


async def test__empty__when_no_conditions__then_fence_has_no_triggers() -> None:
    with Fencing().move_on_cancel() as fence:
        assert fence._triggers == ()
        assert not fence.cancelled


# --- FenceCancelled ---


def test__fence_cancelled__when_single_reason__then_str_is_message() -> None:
    reason = CancelReason(message="timed out after 5s", cancel_type=CancelType.TIMEOUT, code="db")
    exc = FenceCancelled((reason,))
    assert str(exc) == "timed out after 5s"


def test__fence_cancelled__when_multiple_reasons__then_str_joins() -> None:
    r1 = CancelReason(message="timed out after 5s", cancel_type=CancelType.TIMEOUT, code="db")
    r2 = CancelReason(message="event set", cancel_type=CancelType.EVENT, code="shutdown")
    exc = FenceCancelled((r1, r2))
    assert str(exc) == "timed out after 5s; event set"


def test__fence_cancelled__cancelled_by__when_match__then_true() -> None:
    reason = CancelReason(message="timed out", cancel_type=CancelType.TIMEOUT, code="db")
    exc = FenceCancelled((reason,))
    assert exc.cancelled_by("db")


def test__fence_cancelled__cancelled_by__when_no_match__then_false() -> None:
    from aiofence import CancelReason, CancelType

    reason = CancelReason(message="timed out", cancel_type=CancelType.TIMEOUT, code="db")
    exc = FenceCancelled((reason,))
    assert not exc.cancelled_by("other")


def test__fence_cancelled__is_exception__not_cancelled_error() -> None:
    exc = FenceCancelled(())
    assert isinstance(exc, Exception)
    assert not isinstance(exc, asyncio.CancelledError)


# --- raise_on_cancel ---


async def test__raise_on_cancel__when_timeout_fires__then_raises() -> None:
    with pytest.raises(FenceCancelled):
        with Fencing().timeout(0).raise_on_cancel():
            await asyncio.sleep(10)


async def test__raise_on_cancel__when_no_cancel__then_no_exception() -> None:
    with Fencing().timeout(100).raise_on_cancel() as fence:
        pass
    assert not fence.cancelled


async def test__raise_on_cancel__when_raised__then_has_reasons() -> None:
    with pytest.raises(FenceCancelled) as exc_info:
        with Fencing().timeout(0, code="db").raise_on_cancel():
            await asyncio.sleep(10)

    assert len(exc_info.value.reasons) == 1
    assert exc_info.value.reasons[0].code == "db"


async def test__raise_on_cancel__when_raised__then_cancelled_by_works() -> None:
    with pytest.raises(FenceCancelled) as exc_info:
        with Fencing().timeout(0, code="db").raise_on_cancel():
            await asyncio.sleep(10)

    assert exc_info.value.cancelled_by("db")
    assert not exc_info.value.cancelled_by("other")


async def test__raise_on_cancel__when_single_reason__then_message_is_reason() -> None:
    with pytest.raises(FenceCancelled, match="timed out"):
        with Fencing().timeout(0).raise_on_cancel():
            await asyncio.sleep(10)


async def test__raise_on_cancel__when_pre_triggered__then_raises() -> None:
    ev = asyncio.Event()
    ev.set()

    with pytest.raises(FenceCancelled) as exc_info:
        with Fencing().timeout(0, code="to").event(ev, code="ev").raise_on_cancel():
            await asyncio.sleep(10)

    assert exc_info.value.cancelled_by("to")


async def test__raise_on_cancel__when_empty__then_no_exception() -> None:
    with Fencing().raise_on_cancel() as fence:
        pass
    assert not fence.cancelled


# --- Factory functions ---


async def test__on_timeout__then_equivalent_to_fencing_timeout() -> None:
    with on_timeout(0).move_on_cancel() as fence:
        await asyncio.sleep(10)
    assert fence.cancelled


async def test__on_timeout__with_code__then_code_preserved() -> None:
    with on_timeout(0, code="db").move_on_cancel() as fence:
        await asyncio.sleep(10)
    assert fence.cancelled_by("db")


async def test__on_event__then_equivalent_to_fencing_event() -> None:
    ev = asyncio.Event()
    ev.set()
    with on_event(ev, code="shutdown").move_on_cancel() as fence:
        await asyncio.sleep(10)
    assert fence.cancelled_by("shutdown")


async def test__on_deadline__then_equivalent_to_fencing_deadline() -> None:
    loop = asyncio.get_running_loop()
    with on_deadline(loop.time()).move_on_cancel() as fence:
        await asyncio.sleep(10)
    assert fence.cancelled


async def test__on_trigger__then_equivalent_to_fencing_trigger() -> None:
    ev = asyncio.Event()
    ev.set()
    with on_trigger(EventTrigger(ev, code="custom")).move_on_cancel() as fence:
        await asyncio.sleep(10)
    assert fence.cancelled_by("custom")


async def test__on_timeout__chaining__then_works() -> None:
    ev = asyncio.Event()
    ev.set()
    with on_timeout(0, code="to").event(ev, code="ev").move_on_cancel() as fence:
        await asyncio.sleep(10)
    assert fence.cancelled_by("to")
