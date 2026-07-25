from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.types import Message, Receive, Scope

from aiofence import Fencing, get_current_fencing
from aiofence.contrib.fastapi import DisconnectEvent, DisconnectFencing
from aiofence.contrib.starlette import disconnect_fencing, disconnect_fencing_dependency


def http_scope(path: str = "/work", spec_version: str = "2.3") -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


def post_scope(content_length: int, path: str = "/work") -> Scope:
    return {
        **http_scope(path),
        "method": "POST",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(content_length).encode()),
        ],
    }


def scripted_receive(*messages: Message) -> Receive:
    pending = list(messages)

    async def receive() -> Message:
        if pending:
            return pending.pop(0)
        await asyncio.Event().wait()  # client stays connected, forever
        raise AssertionError  # pragma: no cover

    return receive


async def call_app(
    app: FastAPI,
    *,
    scope: Scope | None = None,
    receive: Receive | None = None,
) -> Any:
    return json.loads(await call_app_body(app, scope=scope, receive=receive))


async def call_app_body(
    app: FastAPI,
    *,
    scope: Scope | None = None,
    receive: Receive | None = None,
) -> bytes:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    # A stalled receive channel hangs rather than fails — bound it so CI can't block.
    async with asyncio.timeout(5):
        await app(scope or http_scope(), receive or scripted_receive(), send)

    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def bound_codes() -> list[str | None]:
    return [entry.code for entry in get_current_fencing()._events]


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


async def test__route_dependencies__when_also_declared_as_param__then_single_trigger() -> None:
    app = FastAPI()

    @app.get("/work", dependencies=[Depends(disconnect_fencing)])
    async def work(_: Annotated[Fencing, Depends(disconnect_fencing)]) -> dict[str, Any]:
        return {"codes": bound_codes()}

    assert await call_app(app) == {"codes": ["disconnect"]}


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
