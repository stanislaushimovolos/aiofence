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
    bind_fencing,
    get_current_fencing,
    on_deadline,
    on_event,
    on_timeout,
)

# --- Immutability ---


async def test__timeout__when_called__then_returns_new_instance() -> None:
    base = Fencing()
    derived = base.timeout(5)
    assert derived is not base


async def test__timeout__when_called__then_anchored() -> None:
    derived = Fencing().timeout(5)
    assert derived._anchored is True


async def test__deadline__when_called__then_returns_new_instance() -> None:
    base = Fencing()
    derived = base.deadline(asyncio.get_running_loop().time() + 100)
    assert derived is not base


async def test__deadline__when_called__then_not_anchored() -> None:
    derived = Fencing().deadline(asyncio.get_running_loop().time() + 100)
    assert derived._anchored is False


def test__event__when_called__then_returns_new_instance() -> None:
    base = Fencing()
    derived = base.event(asyncio.Event())
    assert derived is not base


async def test__base__when_derived__then_base_unchanged() -> None:
    base = Fencing()
    base.timeout(5, code="db")
    assert base._deadline is None
    assert base._events == ()


# --- Simple timeout ---


async def test__timeout__then_fence_has_one_timeout_trigger() -> None:
    with Fencing().timeout(5).move_on_cancel() as fence:
        assert len(fence._triggers) == 1
        trigger = fence._triggers[0]
        assert isinstance(trigger, TimeoutTrigger)
        assert trigger._delay == pytest.approx(5, abs=0.1)


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
        assert trigger._delay == pytest.approx(10, abs=0.1)


async def test__deadline__when_looser_added_second__then_original_wins() -> None:
    loop = asyncio.get_running_loop()
    now = loop.time()
    ctx = Fencing().deadline(now + 10, code="tight").deadline(now + 100, code="loose")

    with ctx.move_on_cancel() as fence:
        trigger = fence._triggers[0]
        assert trigger._code == "tight"
        assert trigger._delay == pytest.approx(10, abs=0.1)


# --- Timeout merging ---


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


async def test__event__when_same_event_different_codes__then_both_kept() -> None:
    ev = asyncio.Event()
    ctx = Fencing().event(ev, code="a").event(ev, code="b")

    with ctx.move_on_cancel() as fence:
        event_triggers = [t for t in fence._triggers if isinstance(t, EventTrigger)]
        assert {t._code for t in event_triggers} == {"a", "b"}


async def test__event__when_same_event_different_codes__then_both_codes_reported() -> None:
    ev = asyncio.Event()
    ctx = Fencing().event(ev, code="a").event(ev, code="b")

    async def fire() -> None:
        ev.set()

    task = asyncio.create_task(fire())
    with ctx.move_on_cancel() as fence:
        await asyncio.sleep(10)
    await task

    assert fence.cancelled_by("a")
    assert fence.cancelled_by("b")


async def test__event__when_pre_set_and_two_codes__then_both_codes_reported() -> None:
    ev = asyncio.Event()
    ev.set()

    with Fencing().event(ev, code="a").event(ev, code="b").move_on_cancel() as fence:
        await asyncio.sleep(10)

    assert fence.cancelled_by("a")
    assert fence.cancelled_by("b")


# --- Anchored reuse ---


async def test__anchored__when_entered_twice__then_raises() -> None:
    ctx = Fencing().timeout(100)

    with ctx.move_on_cancel():
        pass

    with pytest.raises(RuntimeError, match="already been used"):
        with ctx.move_on_cancel():
            pass


async def test__not_anchored__when_entered_twice__then_works() -> None:
    ctx = Fencing().deadline(asyncio.get_running_loop().time() + 100)

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


async def test__move_on_cancel__when_pre_triggered__then_triggered_before_await() -> None:
    with Fencing().timeout(0).move_on_cancel() as fence:
        assert fence.cancelled


# --- Empty Fencing ---


async def test__empty__when_no_conditions__then_not_cancelled() -> None:
    with Fencing().move_on_cancel() as fence:
        await asyncio.sleep(0)

    assert not fence.cancelled
    assert not fence.suppressed


# --- FenceCancelled ---


def test__fence_cancelled__when_single_reason__then_str_is_message() -> None:
    reason = CancelReason(message="timed out after 5s", cancel_type=CancelType.TIMEOUT, code="db")
    exc = FenceCancelled((reason,), suppressed=True)
    assert str(exc) == "timed out after 5s"


def test__fence_cancelled__when_multiple_reasons__then_str_joins() -> None:
    r1 = CancelReason(message="timed out after 5s", cancel_type=CancelType.TIMEOUT, code="db")
    r2 = CancelReason(message="event set", cancel_type=CancelType.EVENT, code="shutdown")
    exc = FenceCancelled((r1, r2), suppressed=True)
    assert str(exc) == "timed out after 5s; event set"


def test__fence_cancelled__cancelled_by__when_match__then_true() -> None:
    reason = CancelReason(message="timed out", cancel_type=CancelType.TIMEOUT, code="db")
    exc = FenceCancelled((reason,), suppressed=True)
    assert exc.cancelled_by("db")


def test__fence_cancelled__cancelled_by__when_no_match__then_false() -> None:
    reason = CancelReason(message="timed out", cancel_type=CancelType.TIMEOUT, code="db")
    exc = FenceCancelled((reason,), suppressed=True)
    assert not exc.cancelled_by("other")


def test__fence_cancelled__is_exception__not_cancelled_error() -> None:
    exc = FenceCancelled((), suppressed=False)
    assert isinstance(exc, Exception)
    assert not isinstance(exc, asyncio.CancelledError)


def test__fence_cancelled__has_cancel_reasons_and_suppressed() -> None:
    reason = CancelReason(message="timed out", cancel_type=CancelType.TIMEOUT, code="db")
    exc = FenceCancelled((reason,), suppressed=True)
    assert exc.cancel_reasons == (reason,)
    assert exc.suppressed is True


def test__fence_cancelled__when_not_suppressed__then_suppressed_is_false() -> None:
    reason = CancelReason(message="timed out", cancel_type=CancelType.TIMEOUT)
    exc = FenceCancelled((reason,), suppressed=False)
    assert exc.suppressed is False
    assert len(exc.cancel_reasons) == 1


# --- raise_on_cancel ---


async def test__raise_on_cancel__when_timeout_fires__then_raises() -> None:
    with pytest.raises(FenceCancelled):
        with Fencing().timeout(0).raise_on_cancel():
            await asyncio.sleep(10)


async def test__raise_on_cancel__when_no_cancel__then_no_exception() -> None:
    with Fencing().timeout(100).raise_on_cancel() as fence:
        pass

    assert not fence.suppressed
    assert not fence.cancelled


async def test__raise_on_cancel__when_raised__then_has_reasons() -> None:
    with pytest.raises(FenceCancelled) as exc_info:
        with Fencing().timeout(0, code="db").raise_on_cancel():
            await asyncio.sleep(10)

    assert len(exc_info.value.cancel_reasons) == 1
    assert exc_info.value.cancel_reasons[0].code == "db"


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
    assert not fence.suppressed
    assert not fence.cancelled


async def test__raise_on_cancel__when_pretriggered_sync_body__then_still_raises() -> None:
    ev = asyncio.Event()
    ev.set()

    with pytest.raises(FenceCancelled) as exc_info:
        with Fencing().event(ev).raise_on_cancel():
            x = 1 + 1  # noqa: F841

    assert exc_info.value.suppressed is False


async def test__raise_on_cancel__when_raised__then_has_cancel_reasons_and_suppressed() -> None:
    with pytest.raises(FenceCancelled) as exc_info:
        with Fencing().timeout(0, code="db").raise_on_cancel():
            await asyncio.sleep(10)

    assert len(exc_info.value.cancel_reasons) == 1
    assert exc_info.value.cancel_reasons[0].code == "db"
    assert exc_info.value.suppressed is True


# --- move_on_cancel ---


async def test__move_on_cancel__when_pretriggered_sync_body__then_not_suppressed() -> None:
    ev = asyncio.Event()
    ev.set()

    with Fencing().event(ev).move_on_cancel() as fence:
        x = 1 + 1  # noqa: F841

    assert fence.suppressed is False
    assert fence.cancelled is True
    assert len(fence.cancel_reasons) == 1


async def test__move_on_cancel__when_pretriggered__then_cancelled_visible_in_body() -> None:
    with Fencing().timeout(0).move_on_cancel() as fence:
        result = "early_exit" if fence.cancelled else "normal"

    assert result == "early_exit"


# --- Factory functions ---


async def test__on_timeout__then_equivalent_to_fencing_timeout() -> None:
    with on_timeout(0).move_on_cancel() as fence:
        await asyncio.sleep(10)

    assert fence.suppressed
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

    assert fence.suppressed
    assert fence.cancelled


async def test__on_timeout__chaining__then_works() -> None:
    ev = asyncio.Event()
    ev.set()
    with on_timeout(0, code="to").event(ev, code="ev").move_on_cancel() as fence:
        await asyncio.sleep(10)
    assert fence.cancelled_by("to")


async def test__on_timeout__returns_anchored() -> None:
    result = on_timeout(5)
    assert result._anchored is True


# --- No running task ---


async def test__move_on_cancel__when_no_running_task__then_raises_runtime_error() -> None:
    errors: list[RuntimeError] = []

    def enter_outside_a_task() -> None:
        try:
            with Fencing().move_on_cancel():
                pass
        except RuntimeError as exc:
            errors.append(exc)

    asyncio.get_running_loop().call_soon(enter_outside_a_task)
    await asyncio.sleep(0)

    assert "needs a running asyncio task" in str(errors[0])


# --- guard ---


def test__guard__when_called__then_returns_new_instance() -> None:
    base = Fencing()
    derived = base.guard(lambda _: True)
    assert derived is not base
    assert base._policy is None


async def test__guard__then_fence_carries_policy() -> None:
    def policy(_: CancelReason) -> bool:
        return True

    with Fencing().guard(policy).move_on_cancel() as fence:
        assert fence._policy is policy


async def test__guard__when_declines__then_reason_declined() -> None:
    with Fencing().timeout(0, code="to").guard(lambda _: False).move_on_cancel() as fence:
        await asyncio.sleep(0)

    assert not fence.cancelled
    assert fence.declined_by("to")


async def test__guard__when_two_guards__then_and() -> None:
    fencing = Fencing().timeout(0, code="to").guard(lambda _: True).guard(lambda _: False)

    with fencing.move_on_cancel() as fence:
        await asyncio.sleep(0)

    assert fence.declined_by("to")


async def test__guard__when_first_declines__then_second_not_called() -> None:
    second_called = False

    def second(_: CancelReason) -> bool:
        nonlocal second_called
        second_called = True
        return True

    fencing = Fencing().timeout(0).guard(lambda _: False).guard(second)
    with fencing.move_on_cancel():
        await asyncio.sleep(0)

    assert not second_called


async def test__guard__when_bound_above__then_inherited_below() -> None:
    fencing = Fencing().guard(lambda _: False)

    with bind_fencing(fencing):
        with get_current_fencing().timeout(0, code="to").move_on_cancel() as fence:
            await asyncio.sleep(0)

    assert fence.declined_by("to")


async def test__guard__when_anchored__then_fresh_one_shot() -> None:
    anchored = Fencing().timeout(100)
    derived = anchored.guard(lambda _: True)

    with anchored.move_on_cancel():
        pass

    with derived.move_on_cancel() as fence:
        pass

    assert not fence.cancelled


# --- unless ---


@pytest.fixture
def state() -> dict[str, bool]:
    return {"done": False}


async def test__unless__when_precondition_false__then_cancels(state) -> None:
    fencing = Fencing().timeout(0, code="to").unless(lambda: state["done"], code="to")

    with fencing.move_on_cancel() as fence:
        await asyncio.sleep(10)

    assert fence.cancelled_by("to")
    assert not fence.declined_by("to")


async def test__unless__when_precondition_true__then_declined(state) -> None:
    state["done"] = True
    fencing = Fencing().timeout(0, code="to").unless(lambda: state["done"], code="to")

    with fencing.move_on_cancel() as fence:
        await asyncio.sleep(0)

    assert fence.declined_by("to")
    assert not fence.cancelled


@pytest.mark.parametrize("precondition", [lambda: True, lambda: False])
async def test__unless__with_code__when_other_code__then_untouched(precondition) -> None:
    fencing = Fencing().timeout(0, code="to").unless(precondition, code="ev")

    with fencing.move_on_cancel() as fence:
        await asyncio.sleep(10)

    assert fence.cancelled_by("to")
    assert fence.declined_reasons == ()


async def test__unless__with_code__when_reason_has_no_code__then_untouched() -> None:
    with Fencing().timeout(0).unless(lambda: True, code="ev").move_on_cancel() as fence:
        await asyncio.sleep(10)

    assert fence.cancelled
    assert fence.declined_reasons == ()


async def test__unless__without_code__when_true__then_every_reason_declined() -> None:
    ev = asyncio.Event()
    ev.set()
    fencing = Fencing().timeout(0, code="to").event(ev, code="ev").unless(lambda: True)

    with fencing.move_on_cancel() as fence:
        await asyncio.sleep(0)

    assert not fence.cancelled
    assert fence.declined_by("to")
    assert fence.declined_by("ev")


async def test__unless__without_code__when_false__then_cancels() -> None:
    with Fencing().timeout(0, code="to").unless(lambda: False).move_on_cancel() as fence:
        await asyncio.sleep(10)

    assert fence.cancelled_by("to")


async def test__unless__when_chained_over_two_codes__then_each_declined() -> None:
    ev = asyncio.Event()
    ev.set()
    fencing = Fencing().timeout(0, code="to").event(ev, code="ev")
    for code in ("to", "ev"):
        fencing = fencing.unless(lambda: True, code=code)

    with fencing.move_on_cancel() as fence:
        await asyncio.sleep(0)

    assert fence.declined_by("to")
    assert fence.declined_by("ev")
    assert not fence.cancelled


async def test__unless__when_precondition_read_at_fire_time__then_late_flip_declines() -> None:
    ev = asyncio.Event()
    done = False
    fencing = Fencing().event(ev, code="ev").unless(lambda: done, code="ev")

    async def finish_then_disconnect() -> None:
        nonlocal done
        done = True
        ev.set()

    task = asyncio.create_task(finish_then_disconnect())
    with fencing.move_on_cancel() as fence:
        await asyncio.sleep(0.01)
    await task

    assert fence.declined_by("ev")
    assert not fence.cancelled


# --- raise_on_cancel + declined ---


async def test__raise_on_cancel__when_everything_declined__then_no_raise() -> None:
    with Fencing().timeout(0, code="to").guard(lambda _: False).raise_on_cancel() as fence:
        await asyncio.sleep(0)

    assert fence.declined_by("to")


async def test__raise_on_cancel__when_delivered__then_exception_carries_declined() -> None:
    ev = asyncio.Event()
    ev.set()
    fencing = Fencing().timeout(0, code="to").event(ev, code="ev").unless(lambda: True, code="ev")

    with pytest.raises(FenceCancelled) as exc_info:
        with fencing.raise_on_cancel():
            await asyncio.sleep(10)

    assert exc_info.value.cancelled_by("to")
    assert exc_info.value.declined_by("ev")
    assert len(exc_info.value.declined_reasons) == 1


def test__fence_cancelled__when_no_declined__then_defaults_empty() -> None:
    reason = CancelReason(message="timed out", cancel_type=CancelType.TIMEOUT, code="db")
    exc = FenceCancelled((reason,), suppressed=True)
    assert exc.declined_reasons == ()
    assert not exc.declined_by("db")


def test__fence_cancelled__declined_by__when_match__then_true() -> None:
    reason = CancelReason(message="timed out", cancel_type=CancelType.TIMEOUT, code="db")
    declined = CancelReason(message="event", cancel_type=CancelType.EVENT, code="ev")
    exc = FenceCancelled((reason,), suppressed=True, declined_reasons=(declined,))
    assert exc.declined_by("ev")
    assert not exc.cancelled_by("ev")
