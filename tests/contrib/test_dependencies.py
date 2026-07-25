"""
``aiofence.contrib.fastapi`` dependencies as pure readers of what
``DisconnectMiddleware`` published.

They own no receive loop, so the request here is a stub carrying nothing but a
scope. Behaviour against real Starlette / FastAPI stacks with the middleware
actually installed lives in ``test_middleware_dependencies.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from aiofence import Fencing, bind_fencing, get_current_fencing
from aiofence.contrib.fastapi import (
    disconnect_event,
    disconnect_fencing,
    disconnect_fencing_dependency,
)
from aiofence.contrib.starlette import DISCONNECT_EVENT_SCOPE_KEY

from .asgi_harness import bound_codes


class MockRequest:
    """A request whose scope carries the event the middleware would publish."""

    def __init__(self, *, published: bool = True) -> None:
        self.scope: dict[str, Any] = {"type": "http"}
        self.event = asyncio.Event()
        if published:
            self.scope[DISCONNECT_EVENT_SCOPE_KEY] = self.event

    def disconnect(self) -> None:
        self.event.set()


async def _use_dependency(request: MockRequest) -> AsyncGenerator[Fencing]:
    async for fencing in disconnect_fencing(request):  # type: ignore[arg-type]
        yield fencing


async def _use_coded_dependency(request: MockRequest, code: str) -> AsyncGenerator[Fencing]:
    dependency = disconnect_fencing_dependency(code=code)
    async for fencing in dependency(request):  # type: ignore[arg-type]
        yield fencing


# --- the middleware is required ---


async def test__disconnect_event__when_middleware_absent__then_raises() -> None:
    request = MockRequest(published=False)

    with pytest.raises(RuntimeError, match="DisconnectMiddleware"):
        await disconnect_event(request)  # type: ignore[arg-type]


async def test__disconnect_fencing__when_middleware_absent__then_raises() -> None:
    request = MockRequest(published=False)

    with pytest.raises(RuntimeError, match="DisconnectMiddleware"):
        await anext(disconnect_fencing(request))  # type: ignore[arg-type]


async def test__coded_dependency__when_middleware_absent__then_raises() -> None:
    request = MockRequest(published=False)
    dependency = disconnect_fencing_dependency(code="client_gone")

    with pytest.raises(RuntimeError, match="DisconnectMiddleware"):
        await anext(dependency(request))  # type: ignore[arg-type]


# --- disconnect_event ---


async def test__disconnect_event__when_middleware_installed__then_returns_published_event() -> None:
    request = MockRequest()

    event = await disconnect_event(request)  # type: ignore[arg-type]

    assert event is request.event


async def test__disconnect_event__when_client_disconnects__then_event_set() -> None:
    request = MockRequest()

    event = await disconnect_event(request)  # type: ignore[arg-type]
    request.disconnect()

    assert event.is_set()


# --- disconnect_fencing ---


async def test__disconnect_fencing__when_client_disconnects__then_fence_cancelled() -> None:
    request = MockRequest()

    async for fencing in _use_dependency(request):
        with fencing.move_on_cancel() as fence:
            request.disconnect()
            await asyncio.sleep(10)

        assert fence.cancelled
        assert fence.cancelled_by("disconnect")


async def test__disconnect_fencing__when_client_stays__then_fence_not_cancelled() -> None:
    request = MockRequest()

    async for fencing in _use_dependency(request):
        with fencing.move_on_cancel() as fence:
            await asyncio.sleep(0)

        assert not fence.cancelled


async def test__disconnect_fencing__when_composed_with_timeout__then_timeout_wins() -> None:
    request = MockRequest()

    async for fencing in _use_dependency(request):
        with fencing.timeout(0, code="budget").move_on_cancel() as fence:
            await asyncio.sleep(10)

        assert fence.cancelled_by("budget")
        assert not fence.cancelled_by("disconnect")


async def test__disconnect_fencing__when_outer_fencing__then_inherits() -> None:
    request = MockRequest()
    shutdown = asyncio.Event()
    shutdown.set()

    with bind_fencing(Fencing().event(shutdown, code="shutdown")):
        async for fencing in _use_dependency(request):
            with fencing.move_on_cancel() as fence:
                await asyncio.sleep(10)

            assert fence.cancelled_by("shutdown")


# --- ambient binding ---


async def test__disconnect_fencing__when_entered__then_bound_as_current() -> None:
    request = MockRequest()

    async for fencing in _use_dependency(request):
        assert get_current_fencing() is fencing


async def test__disconnect_fencing__when_torn_down__then_unbound() -> None:
    request = MockRequest()

    async for _ in _use_dependency(request):
        assert bound_codes() == ["disconnect"]

    assert bound_codes() == []


async def test__disconnect_fencing__when_bound__then_current_fencing_sees_disconnect() -> None:
    request = MockRequest()

    async for _ in _use_dependency(request):
        with get_current_fencing().timeout(10, code="budget").move_on_cancel() as fence:
            request.disconnect()
            await asyncio.sleep(10)

        assert fence.cancelled_by("disconnect")
        assert not fence.cancelled_by("budget")


# --- custom codes ---


async def test__coded_dependency__when_client_disconnects__then_uses_custom_code() -> None:
    request = MockRequest()

    async for fencing in _use_coded_dependency(request, code="client_gone"):
        with fencing.move_on_cancel() as fence:
            request.disconnect()
            await asyncio.sleep(10)

        assert fence.cancelled_by("client_gone")
        assert not fence.cancelled_by("disconnect")


async def test__two_codes__when_layered__then_both_registered() -> None:
    request = MockRequest()

    async for _ in _use_dependency(request):
        async for _ in _use_coded_dependency(request, code="client_gone"):
            assert bound_codes() == ["client_gone", "disconnect"]


async def test__two_codes__when_client_disconnects__then_both_reported() -> None:
    request = MockRequest()

    async for _ in _use_dependency(request):
        async for fencing in _use_coded_dependency(request, code="client_gone"):
            with fencing.move_on_cancel() as fence:
                request.disconnect()
                await asyncio.sleep(10)

            assert fence.cancelled_by("disconnect")
            assert fence.cancelled_by("client_gone")
