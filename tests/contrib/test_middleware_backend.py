"""
Which cancel backend fences below ``DisconnectMiddleware`` use.

The contrib-wide fixture that routes the middleware's default through the
parametrised backend is overridden here: these tests are about that default.
"""

# ASGI callbacks have fixed signatures — unused parameters are structural.
# ruff: noqa: ARG001

from __future__ import annotations

import asyncio

import pytest
from starlette.types import ASGIApp, Receive, Scope, Send

from aiofence import Fence, get_current_fencing
from aiofence.backends import CancelBackend, NativeBackend, get_default_backend
from aiofence.backends.anyio import AnyioBackend
from aiofence.contrib.starlette import DisconnectMiddleware

from .server_harness import FakeServer, respond, run_app


@pytest.fixture(autouse=True)
def _middleware_backend() -> None:
    """Leave the middleware's default alone in this module."""


def ambient_fence_app(seen: list[CancelBackend]) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        with get_current_fencing().move_on_cancel() as fence:
            seen.append(fence._backend)
        await respond(send)

    return app


def bare_fence_app(seen: list[CancelBackend]) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(Fence()._backend)
        await respond(send)

    return app


def spawning_app(seen: list[CancelBackend]) -> ASGIApp:
    async def build() -> None:
        seen.append(Fence()._backend)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await asyncio.create_task(build())
        await respond(send)

    return app


async def test__middleware__when_no_backend_given__then_fences_below_use_anyio() -> None:
    seen: list[CancelBackend] = []

    await run_app(DisconnectMiddleware(ambient_fence_app(seen)), FakeServer())

    assert len(seen) == 1
    assert isinstance(seen[0], AnyioBackend)


async def test__middleware__when_backend_given__then_fences_below_use_it() -> None:
    seen: list[CancelBackend] = []
    native = NativeBackend()

    await run_app(DisconnectMiddleware(ambient_fence_app(seen), backend=native), FakeServer())

    assert seen == [native]


async def test__middleware__when_bare_fence_below__then_uses_middleware_backend() -> None:
    seen: list[CancelBackend] = []
    middleware = DisconnectMiddleware(bare_fence_app(seen))

    await run_app(middleware, FakeServer())

    assert seen == [middleware.backend]


async def test__middleware__when_handler_spawns_task__then_its_fences_use_middleware_backend() -> (
    None
):
    seen: list[CancelBackend] = []
    middleware = DisconnectMiddleware(spawning_app(seen))

    await run_app(middleware, FakeServer())

    assert seen == [middleware.backend]


async def test__middleware__when_request_done__then_process_default_restored() -> None:
    seen: list[CancelBackend] = []
    before = get_default_backend()

    await run_app(DisconnectMiddleware(bare_fence_app(seen)), FakeServer())

    assert Fence()._backend is before
