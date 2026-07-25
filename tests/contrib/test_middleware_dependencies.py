"""
The existing dependencies on top of ``DisconnectMiddleware``.

The middleware owns the channel and the event; ``disconnect_event`` /
``disconnect_fencing`` must reuse it and must not start a watcher of their own.
Covers the D1 background-task repro, the D2 code collapse and the D3 scope-cache
lifetime with the middleware in place.
See docs/disconnect-watcher-analysis.md.

These tests reach into ``aiofence.contrib.starlette``, which is being fixed
separately for D2/D3 — they encode the end state, not today's behaviour.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI
from starlette.requests import Request

from aiofence import get_current_fencing
from aiofence.contrib.fastapi import DisconnectEvent, DisconnectFencing
from aiofence.contrib.middleware import (
    DISCONNECT_EVENT_SCOPE_KEY,
    DisconnectMiddleware,
    get_disconnect_event,
)
from aiofence.contrib.starlette import (
    disconnect_event,
    disconnect_fencing,
    disconnect_fencing_dependency,
)

from .asgi_harness import bound_codes
from .server_harness import FakeServer, run_app, serve, wait_for, wait_until

PAYLOAD = b'{"query":"IMPORTANT PAYLOAD"}'


def fenced_app(**kwargs: Any) -> FastAPI:
    app = FastAPI(**kwargs)
    app.add_middleware(DisconnectMiddleware)
    return app


@asynccontextmanager
async def entered_dependency(
    dependency: AsyncGenerator[asyncio.Event],
) -> AsyncIterator[asyncio.Event]:
    """Enter and tear down a yield dependency the way FastAPI's exit stack does."""
    async with aclosing(dependency) as generator:
        yield await anext(generator)


# --- the dependency reuses what the middleware published ---


async def test__disconnect_fencing__when_middleware_installed__then_reuses_published_event() -> (
    None
):
    app = fenced_app()

    @app.get("/work")
    async def work(fencing: DisconnectFencing, request: Request) -> dict[str, bool]:
        published = get_disconnect_event(request.scope)
        return {"shared": fencing._events[0].event is published}

    body = await run_app(app, FakeServer())

    assert json.loads(body) == {"shared": True}


async def test__disconnect_event__when_middleware_installed__then_reuses_published_event() -> None:
    app = fenced_app()

    @app.get("/work")
    async def work(gone: DisconnectEvent, request: Request) -> dict[str, bool]:
        return {"shared": gone is get_disconnect_event(request.scope)}

    body = await run_app(app, FakeServer())

    assert json.loads(body) == {"shared": True}


# --- D1: the response finishing is not a disconnect ---


async def test__disconnect_fencing__when_response_completes__then_background_task_survives() -> (
    None
):
    app = fenced_app(dependencies=[Depends(disconnect_fencing)])
    outcome: dict[str, bool] = {}

    async def work() -> None:
        with get_current_fencing().move_on_cancel() as fence:
            await asyncio.sleep(0.02)
        outcome["completed"] = not fence.cancelled
        outcome["by_disconnect"] = fence.cancelled_by("disconnect")

    @app.get("/work")
    async def handler(background: BackgroundTasks) -> dict[str, bool]:
        background.add_task(work)
        return {"ok": True}

    await run_app(app, FakeServer())

    assert outcome == {"completed": True, "by_disconnect": False}


async def test__disconnect_fencing__when_client_stays__then_event_never_set() -> None:
    app = fenced_app(dependencies=[Depends(disconnect_fencing)])
    server = FakeServer()
    seen: list[bool] = []

    @app.get("/work")
    async def handler(background: BackgroundTasks, request: Request) -> dict[str, bool]:
        event = get_disconnect_event(request.scope)
        assert event is not None

        async def observe() -> None:
            await wait_until(lambda: server.disconnects_delivered >= 1)
            seen.append(event.is_set())

        background.add_task(observe)
        return {"ok": True}

    await run_app(app, server)

    assert seen == [False]


async def test__disconnect_fencing__when_client_disconnects__then_fence_cancelled() -> None:
    app = fenced_app(dependencies=[Depends(disconnect_fencing)])
    server = FakeServer()
    entered = asyncio.Event()
    observed: list[bool] = []

    @app.get("/work")
    async def handler() -> dict[str, bool]:
        entered.set()
        with get_current_fencing().move_on_cancel() as fence:
            await asyncio.sleep(10)
        observed.append(fence.cancelled_by("disconnect"))
        return {"ok": True}

    async with serve(app, server):
        await wait_for(entered)
        server.disconnect()

    assert observed == [True]


# --- D2: two codes on one request ---


async def test__two_codes__when_declared_on_app_and_router__then_both_registered() -> None:
    app = fenced_app(dependencies=[Depends(disconnect_fencing)])
    router = APIRouter(dependencies=[Depends(disconnect_fencing_dependency(code="client_gone"))])

    @router.get("/work")
    async def handler() -> dict[str, Any]:
        return {"codes": sorted(str(code) for code in bound_codes())}

    app.include_router(router)
    body = await run_app(app, FakeServer())

    assert json.loads(body) == {"codes": ["client_gone", "disconnect"]}


async def test__two_codes__when_client_disconnects__then_both_codes_fire() -> None:
    app = fenced_app(dependencies=[Depends(disconnect_fencing)])
    router = APIRouter(dependencies=[Depends(disconnect_fencing_dependency(code="client_gone"))])
    server = FakeServer()
    entered = asyncio.Event()
    observed: list[dict[str, bool]] = []

    @router.get("/work")
    async def handler() -> dict[str, bool]:
        entered.set()
        with get_current_fencing().move_on_cancel() as fence:
            await asyncio.sleep(10)
        observed.append(
            {
                "disconnect": fence.cancelled_by("disconnect"),
                "client_gone": fence.cancelled_by("client_gone"),
            }
        )
        return {"ok": True}

    app.include_router(router)
    async with serve(app, server):
        await wait_for(entered)
        server.disconnect()

    assert observed == [{"disconnect": True, "client_gone": True}]


# --- D3: no watcher to outlive, so re-entry is safe ---


async def test__disconnect_event__when_used_again_after_teardown__then_still_fires() -> None:
    app = fenced_app()
    server = FakeServer()
    entered = asyncio.Event()
    observed: list[bool] = []

    @app.get("/work")
    async def handler(request: Request) -> dict[str, bool]:
        async with entered_dependency(disconnect_event(request)) as first:
            assert not first.is_set()

        async with entered_dependency(disconnect_event(request)) as second:
            entered.set()
            await wait_for(second)
            observed.append(second.is_set())

        return {"ok": True}

    async with serve(app, server):
        await wait_for(entered)
        server.disconnect()

    assert observed == [True]


async def test__disconnect_event__when_two_concurrent_entrants__then_both_see_disconnect() -> None:
    app = fenced_app()
    server = FakeServer()
    entered = asyncio.Event()
    observed: list[str] = []

    async def short(request: Request) -> None:
        async with entered_dependency(disconnect_event(request)) as event:
            observed.append(f"short:{event.is_set()}")

    async def long_lived(request: Request) -> None:
        async with entered_dependency(disconnect_event(request)) as event:
            entered.set()
            await wait_for(event)
            observed.append(f"long:{event.is_set()}")

    @app.get("/work")
    async def handler(request: Request) -> dict[str, bool]:
        async with asyncio.TaskGroup() as group:
            group.create_task(short(request))
            group.create_task(long_lived(request))
        return {"ok": True}

    async with serve(app, server):
        await wait_for(entered)
        server.disconnect()

    assert observed == ["short:False", "long:True"]


async def test__scope_state__when_request_ends__then_event_released() -> None:
    app = fenced_app(dependencies=[Depends(disconnect_fencing)])
    server = FakeServer()

    @app.get("/work")
    async def handler() -> dict[str, bool]:
        return {"ok": True}

    await run_app(app, server)

    assert DISCONNECT_EVENT_SCOPE_KEY not in server.scope


# --- D5: the raw-body caveat is gone ---


async def test__raw_body__when_fencing_bound__then_request_body_intact() -> None:
    app = fenced_app(dependencies=[Depends(disconnect_fencing)])

    @app.post("/work")
    async def handler(request: Request) -> dict[str, str]:
        await asyncio.sleep(0.01)
        return {"raw": (await request.body()).decode()}

    body = await run_app(app, FakeServer(method="POST", body=[PAYLOAD[:12], PAYLOAD[12:]]))

    assert json.loads(body) == {"raw": PAYLOAD.decode()}
