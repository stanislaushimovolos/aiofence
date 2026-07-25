from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import Any

import pytest

from aiofence import Fencing, bind_fencing, get_current_fencing
from aiofence.contrib.starlette import (
    _SCOPE_KEY,
    _DisconnectWatch,
    disconnect_event,
    disconnect_fencing,
    disconnect_fencing_dependency,
)


class MockRequest:
    """Simulates a Starlette Request backed by an asyncio.Queue."""

    def __init__(self) -> None:
        self.scope: dict[str, Any] = {"type": "http"}
        self._queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        self.receive_calls = 0

    async def receive(self) -> dict[str, str]:
        self.receive_calls += 1
        return await self._queue.get()

    def disconnect(self) -> None:
        self._queue.put_nowait({"type": "http.disconnect"})


class BrokenRequest(MockRequest):
    """A receive channel that fails instead of delivering messages."""

    async def receive(self) -> dict[str, str]:
        self.receive_calls += 1
        raise RuntimeError("receive channel is gone")


async def _use_dependency(request: MockRequest) -> AsyncGenerator[Fencing]:
    async for fencing in disconnect_fencing(request):  # type: ignore[arg-type]
        yield fencing


async def _use_coded_dependency(request: MockRequest, code: str) -> AsyncGenerator[Fencing]:
    dependency = disconnect_fencing_dependency(code=code)
    async for fencing in dependency(request):  # type: ignore[arg-type]
        yield fencing


def _watch(request: MockRequest) -> _DisconnectWatch:
    watch: _DisconnectWatch = request.scope[_SCOPE_KEY]
    return watch


def _as_dependency(request: MockRequest) -> AbstractAsyncContextManager[asyncio.Event]:
    """
    Wraps the generator the way FastAPI's exit stack does, so teardown also
    runs when the body raises or is cancelled.
    """
    return asynccontextmanager(disconnect_event)(request)  # type: ignore[arg-type]


# --- disconnect_event ---


async def test__disconnect_event__when_client_disconnects__then_event_set() -> None:
    request = MockRequest()

    async for event in disconnect_event(request):  # type: ignore[arg-type]
        assert not event.is_set()
        request.disconnect()
        await asyncio.sleep(0)
        assert event.is_set()


async def test__disconnect_event__when_client_stays__then_event_unset() -> None:
    request = MockRequest()

    async for event in disconnect_event(request):  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert not event.is_set()


async def test__disconnect_event__when_body_completes__then_listener_cleaned_up() -> None:
    request = MockRequest()
    watches: list[_DisconnectWatch] = []

    async for _ in disconnect_event(request):  # type: ignore[arg-type]
        watches.append(_watch(request))
        await asyncio.sleep(0)

    assert watches[0].listener.done()


async def test__disconnect_event__when_body_completes__then_scope_key_released() -> None:
    request = MockRequest()

    async for _ in disconnect_event(request):  # type: ignore[arg-type]
        assert _SCOPE_KEY in request.scope

    assert _SCOPE_KEY not in request.scope


# --- shared per-request watcher ---


async def test__disconnect_event__when_used_twice__then_same_event_reused() -> None:
    request = MockRequest()

    async for outer in disconnect_event(request):  # type: ignore[arg-type]
        async for inner in disconnect_event(request):  # type: ignore[arg-type]
            assert inner is outer


async def test__disconnect_event__when_used_twice__then_single_receive_loop() -> None:
    request = MockRequest()

    async for _ in disconnect_event(request):  # type: ignore[arg-type]
        async for _ in disconnect_event(request):  # type: ignore[arg-type]
            await asyncio.sleep(0)
            assert request.receive_calls == 1


async def test__disconnect_event__when_mixed_with_fencing__then_both_see_disconnect() -> None:
    request = MockRequest()

    async for fencing in _use_dependency(request):
        async for event in disconnect_event(request):  # type: ignore[arg-type]
            with fencing.move_on_cancel() as fence:
                request.disconnect()
                await asyncio.sleep(10)

            assert event.is_set()
            assert fence.cancelled_by("disconnect")


# --- disconnect_fencing: disconnect fires ---


async def test__disconnect_fencing__when_client_disconnects__then_fence_cancelled() -> None:
    request = MockRequest()

    async for fencing in _use_dependency(request):
        with fencing.move_on_cancel() as fence:
            await asyncio.sleep(0)
            request.disconnect()
            await asyncio.sleep(10)

        assert fence.cancelled
        assert fence.cancelled_by("disconnect")


# --- body completes normally ---


async def test__disconnect_fencing__when_body_completes__then_fence_not_cancelled() -> None:
    request = MockRequest()

    async for fencing in _use_dependency(request):
        with fencing.move_on_cancel() as fence:
            await asyncio.sleep(0)

        assert not fence.cancelled


async def test__disconnect_fencing__when_body_completes__then_listener_cleaned_up() -> None:
    request = MockRequest()
    watches: list[_DisconnectWatch] = []

    async for fencing in _use_dependency(request):
        watches.append(_watch(request))
        with fencing.move_on_cancel():
            await asyncio.sleep(0)

    assert watches[0].listener.done()
    assert _SCOPE_KEY not in request.scope


# --- composable with timeout ---


async def test__disconnect_fencing__when_composed_with_timeout__then_timeout_wins() -> None:
    request = MockRequest()

    async for fencing in _use_dependency(request):
        with fencing.timeout(0, code="budget").move_on_cancel() as fence:
            await asyncio.sleep(10)

        assert fence.cancelled
        assert fence.cancelled_by("budget")
        assert not fence.cancelled_by("disconnect")


# --- cancelled_by("disconnect") ---


async def test__disconnect_fencing__when_disconnect__then_cancelled_by_returns_true() -> None:
    request = MockRequest()

    async for fencing in _use_dependency(request):
        with fencing.move_on_cancel() as fence:
            request.disconnect()
            await asyncio.sleep(10)

        assert fence.cancelled_by("disconnect")


# --- inherits outer fencing context ---


async def test__disconnect_fencing__when_outer_fencing__then_inherits() -> None:
    request = MockRequest()
    event = asyncio.Event()
    event.set()

    with bind_fencing(Fencing().event(event, code="shutdown")):
        async for fencing in _use_dependency(request):
            with fencing.move_on_cancel() as fence:
                await asyncio.sleep(10)

            assert fence.cancelled
            assert fence.cancelled_by("shutdown")


# --- custom code ---


async def test__disconnect_fencing__when_custom_code__then_uses_it() -> None:
    request = MockRequest()

    async for fencing in _use_coded_dependency(request, code="client_gone"):
        with fencing.move_on_cancel() as fence:
            request.disconnect()
            await asyncio.sleep(10)

        assert fence.cancelled_by("client_gone")
        assert not fence.cancelled_by("disconnect")


# --- get_current_fencing() composition ---


async def test__disconnect_fencing__when_defaults_with_timeout_fires__then_timeout_wins() -> None:
    request = MockRequest()

    async for _ in _use_dependency(request):
        with get_current_fencing().timeout(0, code="budget").move_on_cancel() as fence:
            await asyncio.sleep(10)

        assert fence.cancelled
        assert fence.cancelled_by("budget")
        assert not fence.cancelled_by("disconnect")


async def test__disconnect_fencing__when_defaults_with_disconnect_fires__then_disconnect_wins() -> (
    None
):
    request = MockRequest()

    async for _ in _use_dependency(request):
        with get_current_fencing().timeout(10, code="budget").move_on_cancel() as fence:
            request.disconnect()
            await asyncio.sleep(10)

        assert fence.cancelled
        assert fence.cancelled_by("disconnect")
        assert not fence.cancelled_by("budget")


# --- watch lifetime ---


async def test__disconnect_event__when_reentered_after_teardown__then_event_still_fires() -> None:
    request = MockRequest()

    async for _ in disconnect_event(request):  # type: ignore[arg-type]
        await asyncio.sleep(0)

    async for second in disconnect_event(request):  # type: ignore[arg-type]
        request.disconnect()
        await asyncio.sleep(0)

        assert second.is_set()


async def test__disconnect_event__when_reentered_after_teardown__then_new_event() -> None:
    request = MockRequest()

    async with _as_dependency(request) as first:
        pass

    async with _as_dependency(request) as second:
        assert second is not first


async def test__disconnect_event__when_concurrent_entrants__then_survives_first_exit() -> None:
    request = MockRequest()
    short_entered, both_entered, short_done = (asyncio.Event() for _ in range(3))
    seen: list[bool] = []

    async def short_lived() -> None:
        async for _ in disconnect_event(request):  # type: ignore[arg-type]
            short_entered.set()
            await both_entered.wait()

        short_done.set()

    async def long_lived() -> None:
        async for event in disconnect_event(request):  # type: ignore[arg-type]
            both_entered.set()
            await short_done.wait()
            request.disconnect()
            async with asyncio.timeout(1):
                await event.wait()

            seen.append(True)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(short_lived())
        await short_entered.wait()  # the short-lived one owns the watch
        tg.create_task(long_lived())

    assert seen == [True]


async def test__disconnect_event__when_task_cancelled__then_listener_reaped() -> None:
    request = MockRequest()
    watches: list[_DisconnectWatch] = []

    async def use() -> None:
        async with _as_dependency(request):
            watches.append(_watch(request))
            await asyncio.sleep(10)

    task = asyncio.create_task(use())
    await asyncio.sleep(0)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert watches[0].listener.done()
    assert _SCOPE_KEY not in request.scope


# --- receive channel errors ---


async def test__disconnect_event__when_receive_raises__then_teardown_is_quiet() -> None:
    request = BrokenRequest()

    async for event in disconnect_event(request):  # type: ignore[arg-type]
        await asyncio.sleep(0)

        assert not event.is_set()


async def test__disconnect_event__when_receive_raises__then_error_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = BrokenRequest()

    with caplog.at_level(logging.ERROR, logger="aiofence.contrib.starlette"):
        async for _ in disconnect_event(request):  # type: ignore[arg-type]
            await asyncio.sleep(0)

    assert "receive channel is gone" in caplog.text


# --- two codes on one request ---


async def test__disconnect_fencing__when_two_codes__then_both_reported() -> None:
    request = MockRequest()

    async for _ in _use_dependency(request):
        async for fencing in _use_coded_dependency(request, code="client_gone"):
            with fencing.move_on_cancel() as fence:
                request.disconnect()
                await asyncio.sleep(10)

            assert fence.cancelled_by("disconnect")
            assert fence.cancelled_by("client_gone")


async def test__disconnect_fencing__when_two_codes__then_one_receive_loop() -> None:
    request = MockRequest()

    async for _ in _use_dependency(request):
        async for _ in _use_coded_dependency(request, code="client_gone"):
            await asyncio.sleep(0)

            assert request.receive_calls == 1
