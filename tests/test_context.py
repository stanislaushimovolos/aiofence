from __future__ import annotations

import asyncio

from aiofence import Fencing, bind_fencing

# --- current() without context ---


async def test__current__when_no_context__then_returns_empty_fencing() -> None:
    fencing = Fencing.current()
    assert isinstance(fencing, Fencing)
    assert fencing._timeouts == ()
    assert fencing._explicit_triggers == ()
    assert fencing._deadline is None


# --- current() inside bind_fencing ---


async def test__current__when_inside_bind__then_returns_stored_fencing() -> None:
    original = Fencing().timeout(5, code="db")
    with bind_fencing(original):
        assert Fencing.current() is original


async def test__bind_fencing__when_exited__then_context_restored() -> None:
    with bind_fencing(Fencing().timeout(5)):
        pass
    fencing = Fencing.current()
    assert fencing._timeouts == ()


async def test__bind_fencing__when_cancelled__then_context_restored() -> None:
    original = Fencing().timeout(5, code="outer")
    with bind_fencing(original):
        with bind_fencing(Fencing().timeout(0, code="inner")):
            with Fencing.current().move_on_cancel() as fence:
                await asyncio.sleep(10)
            assert fence.cancelled
        assert Fencing.current() is original


# --- Nesting ---


async def test__bind_fencing__when_nested__then_inner_overrides() -> None:
    outer = Fencing().timeout(10, code="outer")
    inner = Fencing().timeout(1, code="inner")

    with bind_fencing(outer):
        assert Fencing.current() is outer
        with bind_fencing(inner):
            assert Fencing.current() is inner
        assert Fencing.current() is outer


# --- Task inheritance ---


async def test__bind_fencing__when_create_task__then_child_inherits() -> None:
    inherited = None

    async def child() -> None:
        nonlocal inherited
        inherited = Fencing.current()

    original = Fencing().timeout(5, code="parent")
    with bind_fencing(original):
        task = asyncio.create_task(child())
        await task

    assert inherited is original


async def test__bind_fencing__when_child_rebinds__then_parent_unaffected() -> None:
    child_seen = None

    async def child() -> None:
        nonlocal child_seen
        with bind_fencing(Fencing().timeout(1, code="child")):
            child_seen = Fencing.current()

    original = Fencing().timeout(5, code="parent")
    with bind_fencing(original):
        task = asyncio.create_task(child())
        await task
        assert Fencing.current() is original

    assert child_seen is not original
    assert child_seen._timeouts == ((1, "child"),)


# --- Integration with move_on_cancel ---


async def test__current__move_on_cancel__when_timeout_fires__then_cancelled() -> None:
    with bind_fencing(Fencing().timeout(0, code="ctx")):
        with Fencing.current().move_on_cancel() as fence:
            await asyncio.sleep(10)
        assert fence.cancelled
        assert fence.cancelled_by("ctx")


async def test__current__when_extended_with_event__then_works() -> None:
    ev = asyncio.Event()
    ev.set()

    with bind_fencing(Fencing().timeout(100)):
        with Fencing.current().event(ev, code="ev").move_on_cancel() as fence:
            await asyncio.sleep(10)
        assert fence.cancelled_by("ev")


async def test__inherited_event__when_fires__then_cancels_inner_fence() -> None:
    ev = asyncio.Event()
    result = None

    async def worker() -> None:
        nonlocal result
        with Fencing.current().move_on_cancel() as fence:
            await asyncio.sleep(10)
        result = fence

    with bind_fencing(Fencing().event(ev, code="disconnect")):
        task = asyncio.create_task(worker())
        await asyncio.sleep(0)
        ev.set()
        await task

    assert result is not None
    assert result.cancelled
    assert result.cancelled_by("disconnect")


# --- Chaining from empty context ---


async def test__current__when_no_context__then_chains_normally() -> None:
    with Fencing.current().timeout(0, code="chained").move_on_cancel() as fence:
        await asyncio.sleep(10)
    assert fence.cancelled
    assert fence.cancelled_by("chained")
