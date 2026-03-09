from __future__ import annotations

import asyncio

from aiofence import Fencing, bind_fencing_defaults, get_fencing_defaults

# --- get_fencing_defaults() without context ---


async def test__get_fencing_defaults__when_no_context__then_returns_empty_fencing() -> None:
    fencing = get_fencing_defaults()
    assert isinstance(fencing, Fencing)
    assert fencing._deadline is None
    assert fencing._events == ()


# --- get_fencing_defaults() inside bind_fencing_defaults ---


async def test__get_fencing_defaults__when_inside_bind__then_returns_stored_fencing() -> None:
    original = Fencing().timeout(5, code="db")
    with bind_fencing_defaults(original):
        assert get_fencing_defaults() is original


async def test__bind_fencing_defaults__when_exited__then_context_restored() -> None:
    with bind_fencing_defaults(Fencing().timeout(5)):
        pass
    fencing = get_fencing_defaults()
    assert fencing._deadline is None


async def test__bind_fencing_defaults__when_cancelled__then_context_restored() -> None:
    original = Fencing().timeout(5, code="outer")
    with bind_fencing_defaults(original):
        with bind_fencing_defaults(Fencing().timeout(0, code="inner")):
            with get_fencing_defaults().move_on_cancel() as fence:
                await asyncio.sleep(10)
            assert fence.cancelled
        assert get_fencing_defaults() is original


# --- Nesting ---


async def test__bind_fencing_defaults__when_nested__then_inner_overrides() -> None:
    outer = Fencing().timeout(10, code="outer")
    inner = Fencing().timeout(1, code="inner")

    with bind_fencing_defaults(outer):
        assert get_fencing_defaults() is outer
        with bind_fencing_defaults(inner):
            assert get_fencing_defaults() is inner
        assert get_fencing_defaults() is outer


# --- Task inheritance ---


async def test__bind_fencing_defaults__when_create_task__then_child_inherits() -> None:
    inherited = None

    async def child() -> None:
        nonlocal inherited
        inherited = get_fencing_defaults()

    original = Fencing().timeout(5, code="parent")
    with bind_fencing_defaults(original):
        task = asyncio.create_task(child())
        await task

    assert inherited is original


async def test__bind_fencing_defaults__when_child_rebinds__then_parent_unaffected() -> None:
    child_seen = None

    async def child() -> None:
        nonlocal child_seen
        with bind_fencing_defaults(Fencing().timeout(1, code="child")):
            child_seen = get_fencing_defaults()

    original = Fencing().timeout(5, code="parent")
    with bind_fencing_defaults(original):
        task = asyncio.create_task(child())
        await task
        assert get_fencing_defaults() is original

    assert child_seen is not original
    assert child_seen._anchored is True
    assert child_seen._deadline_code == "child"


# --- Integration with move_on_cancel ---


async def test__get_fencing_defaults__move_on_cancel__when_timeout_fires__then_cancelled() -> None:
    with bind_fencing_defaults(Fencing().timeout(0, code="ctx")):
        with get_fencing_defaults().move_on_cancel() as fence:
            await asyncio.sleep(10)
        assert fence.cancelled
        assert fence.cancelled_by("ctx")


async def test__get_fencing_defaults__when_extended_with_event__then_works() -> None:
    ev = asyncio.Event()
    ev.set()

    with bind_fencing_defaults(Fencing().timeout(100)):
        with get_fencing_defaults().event(ev, code="ev").move_on_cancel() as fence:
            await asyncio.sleep(10)
        assert fence.cancelled_by("ev")


async def test__inherited_event__when_fires__then_cancels_inner_fence() -> None:
    ev = asyncio.Event()
    result = None

    async def worker() -> None:
        nonlocal result
        with get_fencing_defaults().move_on_cancel() as fence:
            await asyncio.sleep(10)
        result = fence

    with bind_fencing_defaults(Fencing().event(ev, code="disconnect")):
        task = asyncio.create_task(worker())
        await asyncio.sleep(0)
        ev.set()
        await task

    assert result is not None
    assert result.cancelled
    assert result.cancelled_by("disconnect")


# --- Chaining from empty context ---


async def test__get_fencing_defaults__when_no_context__then_chains_normally() -> None:
    with get_fencing_defaults().timeout(0, code="chained").move_on_cancel() as fence:
        await asyncio.sleep(10)
    assert fence.cancelled
    assert fence.cancelled_by("chained")
