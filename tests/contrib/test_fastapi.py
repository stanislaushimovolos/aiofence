from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, FastAPI
from starlette.requests import Request
from starlette.responses import StreamingResponse

from aiofence import Fencing, get_current_fencing
from aiofence.contrib.fastapi import DisconnectEvent, DisconnectFencing
from aiofence.contrib.starlette import disconnect_fencing, disconnect_fencing_dependency

from .asgi_harness import (
    bound_codes,
    call_app,
    call_app_body,
    http_scope,
    post_scope,
    scripted_receive,
)

# --- binding via FastAPI's dependencies=[...] ---


async def test__route_dependencies__when_declared__then_fencing_bound_in_handler() -> None:
    app = FastAPI()

    @app.get("/work", dependencies=[Depends(disconnect_fencing)])
    async def work() -> dict[str, Any]:
        return {"codes": bound_codes()}

    assert await call_app(app) == {"codes": ["disconnect"]}


async def test__app_dependencies__when_declared__then_fencing_bound_in_handler() -> None:
    app = FastAPI(dependencies=[Depends(disconnect_fencing)])

    @app.get("/work")
    async def work() -> dict[str, Any]:
        return {"codes": bound_codes()}

    assert await call_app(app) == {"codes": ["disconnect"]}


async def test__router_dependencies__when_custom_code__then_uses_it() -> None:
    router = APIRouter(dependencies=[Depends(disconnect_fencing_dependency(code="client_gone"))])

    @router.get("/work")
    async def work() -> dict[str, Any]:
        return {"codes": bound_codes()}

    app = FastAPI()
    app.include_router(router)

    assert await call_app(app) == {"codes": ["client_gone"]}


async def test__route_dependencies__when_nested_callee__then_fencing_propagates() -> None:
    app = FastAPI()

    async def service() -> list[str | None]:
        return bound_codes()

    @app.get("/work", dependencies=[Depends(disconnect_fencing)])
    async def work() -> dict[str, Any]:
        return {"codes": await service()}

    assert await call_app(app) == {"codes": ["disconnect"]}


async def test__route_dependencies__when_declared_twice__then_solved_once() -> None:
    """One callable, one FastAPI cache key — this proves caching, not sharing."""
    app = FastAPI()
    solved: list[str] = []

    async def counted(request: Request) -> AsyncIterator[Fencing]:
        solved.append("x")
        async for fencing in disconnect_fencing(request):
            yield fencing

    @app.get("/work", dependencies=[Depends(counted), Depends(counted)])
    async def work() -> dict[str, Any]:
        return {"codes": bound_codes()}

    assert await call_app(app) == {"codes": ["disconnect"]}
    assert solved == ["x"]


# --- two distinct dependencies on one request ---


async def test__distinct_dependencies__when_declared__then_share_one_event() -> None:
    """Two callables FastAPI cannot collapse by cache key — real sharing, not caching."""
    app = FastAPI()
    client_gone = disconnect_fencing_dependency(code="client_gone")

    @app.get("/work", dependencies=[Depends(disconnect_fencing), Depends(client_gone)])
    async def work() -> dict[str, Any]:
        events = {id(entry.event) for entry in get_current_fencing()._events}
        return {"events": len(events)}

    assert await call_app(app) == {"events": 1}


async def test__distinct_dependencies__when_codes_differ__then_both_bound() -> None:
    app = FastAPI()
    client_gone = disconnect_fencing_dependency(code="client_gone")

    @app.get("/work", dependencies=[Depends(disconnect_fencing), Depends(client_gone)])
    async def work() -> dict[str, Any]:
        return {"codes": bound_codes()}

    assert await call_app(app) == {"codes": ["client_gone", "disconnect"]}


async def test__app_and_router_dependencies__when_codes_differ__then_both_bound() -> None:
    """The layering docs/api.md recommends: app-wide default plus a router override."""
    router = APIRouter(dependencies=[Depends(disconnect_fencing_dependency(code="client_gone"))])

    @router.get("/work")
    async def work() -> dict[str, Any]:
        return {"codes": bound_codes()}

    app = FastAPI(dependencies=[Depends(disconnect_fencing)])
    app.include_router(router)

    assert await call_app(app) == {"codes": ["client_gone", "disconnect"]}


async def test__app_and_router_dependencies__when_disconnect__then_both_codes_cancel() -> None:
    router = APIRouter(dependencies=[Depends(disconnect_fencing_dependency(code="client_gone"))])

    @router.get("/work")
    async def work() -> dict[str, Any]:
        with get_current_fencing().move_on_cancel() as fence:
            await asyncio.sleep(10)

        return {
            "disconnect": fence.cancelled_by("disconnect"),
            "client_gone": fence.cancelled_by("client_gone"),
        }

    app = FastAPI(dependencies=[Depends(disconnect_fencing)])
    app.include_router(router)

    result = await call_app(app, receive=scripted_receive({"type": "http.disconnect"}))

    assert result == {"disconnect": True, "client_gone": True}


async def test__aliases__when_both_declared__then_single_receive_loop() -> None:
    app = FastAPI()
    receive = scripted_receive()

    @app.get("/work")
    async def work(fencing: DisconnectFencing, gone: DisconnectEvent) -> dict[str, Any]:  # noqa: ARG001
        await asyncio.sleep(0)
        return {"calls": receive.calls}

    assert await call_app(app, receive=receive) == {"calls": 1}


async def test__app_dependencies__when_mixed_with_aliases__then_one_shared_trigger() -> None:
    app = FastAPI(dependencies=[Depends(disconnect_fencing)])

    @app.get("/work")
    async def work(fencing: DisconnectFencing, gone: DisconnectEvent) -> dict[str, Any]:
        return {"codes": bound_codes(), "shared": gone is fencing._events[0].event}

    assert await call_app(app) == {"codes": ["disconnect"], "shared": True}


# --- dependency aliases ---


async def test__disconnect_fencing_alias__when_used__then_binds_fencing() -> None:
    app = FastAPI()

    @app.get("/work")
    async def work(fencing: DisconnectFencing) -> dict[str, Any]:
        return {"param": [e.code for e in fencing._events], "context": bound_codes()}

    assert await call_app(app) == {"param": ["disconnect"], "context": ["disconnect"]}


async def test__disconnect_event_alias__when_client_disconnects__then_event_set() -> None:
    app = FastAPI()

    @app.get("/work")
    async def work(gone: DisconnectEvent) -> dict[str, bool]:
        await asyncio.sleep(0)
        return {"gone": gone.is_set()}

    result = await call_app(app, receive=scripted_receive({"type": "http.disconnect"}))

    assert result == {"gone": True}


async def test__disconnect_event_alias__when_client_stays__then_event_unset() -> None:
    app = FastAPI()

    @app.get("/work")
    async def work(gone: DisconnectEvent) -> dict[str, bool]:
        await asyncio.sleep(0)
        return {"gone": gone.is_set()}

    assert await call_app(app) == {"gone": False}


# --- cancellation ---


async def test__route_dependencies__when_client_disconnects__then_fence_cancelled() -> None:
    app = FastAPI()
    observed: list[bool] = []

    @app.get("/work", dependencies=[Depends(disconnect_fencing)])
    async def work() -> dict[str, bool]:
        with get_current_fencing().move_on_cancel() as fence:
            await asyncio.sleep(10)

        observed.append(fence.cancelled_by("disconnect"))
        return {"ok": True}

    result = await call_app(app, receive=scripted_receive({"type": "http.disconnect"}))

    assert observed == [True]
    assert result == {"ok": True}


async def test__route_dependencies__when_client_stays__then_fence_not_cancelled() -> None:
    app = FastAPI()
    observed: list[bool] = []

    @app.get("/work", dependencies=[Depends(disconnect_fencing)])
    async def work() -> dict[str, bool]:
        with get_current_fencing().move_on_cancel() as fence:
            await asyncio.sleep(0)

        observed.append(fence.cancelled)
        return {"ok": True}

    await call_app(app)

    assert observed == [False]


# --- raw body caveat ---


async def test__route_dependencies__when_body_param_declared__then_raw_read_uses_cache() -> None:
    """FastAPI caches the body before dependencies run, so the watcher never sees it."""
    app = FastAPI()

    @app.post("/work", dependencies=[Depends(disconnect_fencing)])
    async def work(payload: dict[str, int], request: Request) -> dict[str, Any]:
        await asyncio.sleep(0)  # watcher is parked in receive() by now
        return {"payload": payload, "raw": (await request.body()).decode()}

    body = b'{"a": 1}'
    result = await call_app(
        app,
        scope=post_scope(len(body)),
        receive=scripted_receive({"type": "http.request", "body": body, "more_body": False}),
    )

    assert result == {"payload": {"a": 1}, "raw": '{"a": 1}'}


# --- streaming responses ---


async def test__route_dependencies__when_streaming_response__then_fencing_bound_in_body() -> None:
    """Yield dependencies tear down after the body streams, so the context outlives it."""
    app = FastAPI()

    @app.get("/work", dependencies=[Depends(disconnect_fencing)])
    async def work() -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            yield json.dumps(bound_codes()).encode()

        return StreamingResponse(body())

    assert await call_app_body(app) == b'["disconnect"]'


async def test__streaming_response__when_server_owns_no_reader__then_fence_cancelled() -> None:
    """ASGI spec_version 2.4+: StreamingResponse skips its own listen_for_disconnect."""
    app = FastAPI()

    @app.get("/work", dependencies=[Depends(disconnect_fencing)])
    async def work() -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            yield b"start;"
            with get_current_fencing().move_on_cancel() as fence:
                await asyncio.sleep(10)
            yield f"disconnect={fence.cancelled_by('disconnect')};".encode()

        return StreamingResponse(body())

    result = await call_app_body(
        app,
        scope=http_scope(spec_version="2.4"),
        receive=scripted_receive({"type": "http.disconnect"}),
    )

    assert result == b"start;disconnect=True;"


# --- sync (def) handlers ---


async def test__sync_handler__when_fenced__then_ambient_codes_visible() -> None:
    app = FastAPI()

    @app.get("/work", dependencies=[Depends(disconnect_fencing)])
    def work() -> dict[str, Any]:
        return {"codes": bound_codes()}

    assert await call_app(app) == {"codes": ["disconnect"]}


async def test__sync_handler__when_entering_fence__then_raises_runtime_error() -> None:
    """The threadpool has no running loop, so a `def` handler cannot be fenced."""
    app = FastAPI()

    @app.get("/work", dependencies=[Depends(disconnect_fencing)])
    def work() -> dict[str, Any]:
        try:
            with get_current_fencing().move_on_cancel():
                pass
        except RuntimeError as exc:
            return {"error": type(exc).__name__, "message": str(exc)}
        return {"error": None, "message": ""}

    result = await call_app(app)

    assert result["error"] == "RuntimeError"
    assert "event loop" in result["message"]


# --- the code is not client-controllable ---


async def test__disconnect_fencing__when_code_in_query_string__then_ignored() -> None:
    """A `code` kwarg on the dependency would be a client-settable query param."""
    app = FastAPI(dependencies=[Depends(disconnect_fencing)])

    @app.get("/work")
    async def work() -> dict[str, Any]:
        return {"codes": bound_codes()}

    scope = {**http_scope(), "query_string": b"code=INJECTED"}

    assert await call_app(app, scope=scope) == {"codes": ["disconnect"]}


async def test__disconnect_fencing__when_route_declared__then_no_query_param_in_schema() -> None:
    app = FastAPI()

    @app.get("/work", dependencies=[Depends(disconnect_fencing)])
    async def work() -> dict[str, Any]:
        return {}

    assert "parameters" not in app.openapi()["paths"]["/work"]["get"]
