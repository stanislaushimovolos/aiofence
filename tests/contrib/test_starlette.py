from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from aiofence import Fencing, bind_fencing
from aiofence.contrib.starlette import disconnect_event, disconnect_fencing


class MockRequest:
    """Simulates a Starlette Request backed by an asyncio.Queue."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()

    async def receive(self) -> dict[str, str]:
        return await self._queue.get()

    def disconnect(self) -> None:
        self._queue.put_nowait({"type": "http.disconnect"})


async def _use_dependency(request: MockRequest, **kwargs: str) -> AsyncGenerator[Fencing]:
    async for fencing in disconnect_fencing(request, **kwargs):  # type: ignore[arg-type]
        yield fencing


# --- disconnect_event ---


async def test__disconnect_event__when_client_disconnects__then_event_set() -> None:
    request = MockRequest()

    async for event in disconnect_event(request):  # type: ignore[arg-type]
        assert not event.is_set()
        request.disconnect()
        await asyncio.sleep(0)
        assert event.is_set()


async def test__disconnect_event__when_body_completes__then_listener_cleaned_up() -> None:
    request = MockRequest()

    async for event in disconnect_event(request):  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert not event.is_set()


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


async def test__disconnect_fencing__when_body_completes__then_listener_cleaned_up() -> None:
    request = MockRequest()

    async for fencing in _use_dependency(request):
        with fencing.move_on_cancel() as fence:
            await asyncio.sleep(0)

        assert not fence.cancelled


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

    async for fencing in _use_dependency(request, code="client_gone"):
        with fencing.move_on_cancel() as fence:
            request.disconnect()
            await asyncio.sleep(10)

        assert fence.cancelled_by("client_gone")
        assert not fence.cancelled_by("disconnect")


# --- Fencing.current() composition ---


async def test__disconnect_fencing__when_current_with_timeout_fires__then_timeout_wins() -> None:
    request = MockRequest()

    async for _ in _use_dependency(request):
        with Fencing.current().timeout(0, code="budget").move_on_cancel() as fence:
            await asyncio.sleep(10)

        assert fence.cancelled
        assert fence.cancelled_by("budget")
        assert not fence.cancelled_by("disconnect")


async def test__disconnect_fencing__when_current_with_disconnect_fires__then_disconnect_wins() -> (
    None
):
    request = MockRequest()

    async for _ in _use_dependency(request):
        with Fencing.current().timeout(10, code="budget").move_on_cancel() as fence:
            request.disconnect()
            await asyncio.sleep(10)

        assert fence.cancelled
        assert fence.cancelled_by("disconnect")
        assert not fence.cancelled_by("budget")
