"""
``DisconnectMiddleware`` against real Starlette / FastAPI stacks.

Covers the rival readers and the raw-body shapes the dependency cannot serve:
D5 (body theft), D6 (``StreamingResponse``), D9 (``BaseHTTPMiddleware``),
D13 (exception handlers) and the ``BackgroundTasks`` half of D1.
See docs/disconnect-watcher-analysis.md.
"""

# Starlette callbacks have fixed signatures — unused parameters are structural.
# ruff: noqa: ARG001

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

import pytest
from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Scope

from aiofence import Fence, get_current_fencing
from aiofence.contrib.starlette import DisconnectMiddleware, get_disconnect_event

from .asgi_harness import bound_codes
from .server_harness import (
    FakeServer,
    ReceiveProbe,
    run_app,
    serve,
    wait_for,
)

PAYLOAD = b'{"query":"IMPORTANT PAYLOAD"}'

Endpoint = Callable[[Request], Awaitable[Response]]


def starlette_app(
    endpoint: Endpoint,
    *,
    methods: Sequence[str] = ("GET",),
    middleware: Sequence[Middleware] = (),
    exception_handlers: dict | None = None,
) -> Starlette:
    return Starlette(
        routes=[Route("/work", endpoint, methods=list(methods))],
        middleware=[
            Middleware(DisconnectMiddleware),
            *middleware,
        ],
        exception_handlers=exception_handlers,
    )


# --- D1: background tasks run after the response completes ---


async def test__background_task__when_response_completes__then_fence_not_cancelled() -> None:
    outcome: dict[str, bool] = {}

    async def work() -> None:
        with get_current_fencing().move_on_cancel() as fence:
            await asyncio.sleep(0.02)
        outcome["completed"] = not fence.cancelled

    async def endpoint(request: Request) -> Response:
        return Response(b"ok", background=BackgroundTask(work))

    await run_app(starlette_app(endpoint), FakeServer())

    assert outcome == {"completed": True}


async def test__background_task__when_client_left_before_response__then_fence_cancelled() -> None:
    server = FakeServer()
    waiting = asyncio.Event()
    outcome: dict[str, bool] = {}

    async def work() -> None:
        with get_current_fencing().move_on_cancel() as fence:
            await asyncio.sleep(10)
        outcome["cancelled"] = fence.cancelled_by("disconnect")

    async def endpoint(request: Request) -> Response:
        waiting.set()
        await wait_for(request.scope["aiofence.disconnect_event"])
        return Response(b"ok", background=BackgroundTask(work))

    async with serve(starlette_app(endpoint), server):
        await wait_for(waiting)
        server.disconnect()

    assert outcome == {"cancelled": True}


# --- D5: raw body reads ---


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(PAYLOAD, id="one-chunk"),
        pytest.param([PAYLOAD[:12], PAYLOAD[12:]], id="two-chunks"),
        pytest.param([PAYLOAD[:6], PAYLOAD[6:20], PAYLOAD[20:]], id="three-chunks"),
    ],
)
async def test__request_body__when_read_after_suspension__then_returns_exact_bytes(
    body: bytes | list[bytes],
) -> None:
    read: list[bytes] = []

    async def endpoint(request: Request) -> Response:
        await asyncio.sleep(0.01)
        read.append(await request.body())
        return Response(b"ok")

    app = starlette_app(endpoint, methods=["POST"])
    await run_app(app, FakeServer(method="POST", body=body))

    assert read == [PAYLOAD]


async def test__request_stream__when_multi_chunk__then_yields_every_chunk() -> None:
    read: list[bytes] = []

    async def endpoint(request: Request) -> Response:
        await asyncio.sleep(0.01)
        read.extend([chunk async for chunk in request.stream() if chunk])
        return Response(b"ok")

    app = starlette_app(endpoint, methods=["POST"])
    await run_app(app, FakeServer(method="POST", body=[b"head-", b"middle-", b"tail"]))

    assert read == [b"head-", b"middle-", b"tail"]


async def test__request_stream__when_body_arrives_after_headers__then_yields_every_chunk() -> None:
    server = FakeServer(method="POST", defer_body=True, content_length=len(PAYLOAD))
    reading = asyncio.Event()
    read: list[bytes] = []

    async def endpoint(request: Request) -> Response:
        reading.set()
        read.extend([chunk async for chunk in request.stream() if chunk])
        return Response(b"ok")

    async with serve(starlette_app(endpoint, methods=["POST"]), server):
        await wait_for(reading)
        await asyncio.sleep(0.01)
        await server.feed_body(PAYLOAD[:12], more_body=True)
        await server.feed_body(PAYLOAD[12:], more_body=False)

    assert read == [PAYLOAD[:12], PAYLOAD[12:]]


async def test__request_stream__when_client_leaves_mid_upload__then_raises_client_disconnect() -> (
    None
):
    """
    Reachable because we replay the disconnect: Starlette turns it into
    ``ClientDisconnect`` rather than leaving the iterator parked.
    """
    _, raised = await _upload_cut_mid_stream()

    assert raised == ["ClientDisconnect"]


async def test__request_stream__when_client_leaves_mid_upload__then_keeps_chunks_read_so_far() -> (
    None
):
    read, _ = await _upload_cut_mid_stream()

    assert read == [b"head-"]


async def _upload_cut_mid_stream() -> tuple[list[bytes], list[str]]:
    """Feed one chunk, let the handler consume it, then drop the client."""
    server = FakeServer(method="POST", defer_body=True, content_length=len(PAYLOAD))
    first_chunk = asyncio.Event()
    read: list[bytes] = []
    raised: list[str] = []

    async def endpoint(request: Request) -> Response:
        try:
            async for chunk in request.stream():
                if chunk:
                    read.append(chunk)
                    first_chunk.set()
        except ClientDisconnect:
            raised.append(ClientDisconnect.__name__)
        return Response(b"ok")

    async with serve(starlette_app(endpoint, methods=["POST"]), server):
        await server.feed_body(b"head-", more_body=True)
        await wait_for(first_chunk)
        server.disconnect()

    return read, raised


async def test__request_body__when_body_arrives_after_headers__then_returns_exact_bytes() -> None:
    server = FakeServer(method="POST", defer_body=True, content_length=len(PAYLOAD))
    reading = asyncio.Event()
    read: list[bytes] = []

    async def endpoint(request: Request) -> Response:
        reading.set()
        read.append(await request.body())
        return Response(b"ok")

    async with serve(starlette_app(endpoint, methods=["POST"]), server):
        await wait_for(reading)
        await asyncio.sleep(0.01)
        await server.feed_body(PAYLOAD[:12], more_body=True)
        await server.feed_body(PAYLOAD[12:], more_body=False)

    assert read == [PAYLOAD]


async def test__fastapi_handler__when_request_is_the_only_param__then_body_read_intact() -> None:
    """No declared body param means FastAPI never pre-reads and caches it."""
    app = FastAPI()
    app.add_middleware(DisconnectMiddleware)

    @app.post("/work")
    async def work(request: Request) -> dict[str, str]:
        await asyncio.sleep(0.01)
        return {"raw": (await request.body()).decode(), "codes": ",".join(map(str, bound_codes()))}

    body = await run_app(app, FakeServer(method="POST", body=PAYLOAD))

    assert json.loads(body) == {"raw": PAYLOAD.decode(), "codes": "disconnect"}


# --- fenced body reads ---


async def test__fenced_body_read__when_client_left_first__then_raises_client_disconnect() -> None:
    """
    Nothing to wake: the fence's cancel is only scheduled, and both the buffered
    chunk and the recorded disconnect are answered without suspending, so
    Starlette raises before the cancel can land. Same as reading unfenced under
    plain uvicorn, whose ``receive()`` also skips its ``await`` once disconnected.
    """
    outcome = await _fenced_upload_cut()

    assert outcome == {"client_disconnect": True}


async def _fenced_upload_cut() -> dict[str, bool]:
    """Cut the client mid-upload, before the fenced read is entered."""
    server = FakeServer(method="POST", defer_body=True, content_length=len(PAYLOAD))
    reading = asyncio.Event()
    cut = asyncio.Event()
    outcome: dict[str, bool] = {}

    async def endpoint(request: Request) -> Response:
        reading.set()
        await wait_for(cut)

        try:
            with get_current_fencing().move_on_cancel() as fence:
                await request.body()
        except ClientDisconnect:
            outcome["client_disconnect"] = True
            return Response(b"ok")

        outcome["cancelled_by_disconnect"] = fence.cancelled_by("disconnect")
        outcome["suppressed"] = fence.suppressed
        return Response(b"ok")

    async with serve(starlette_app(endpoint, methods=["POST"]), server):
        await wait_for(reading)
        await server.feed_body(PAYLOAD[:5], more_body=True)
        await asyncio.sleep(0.01)
        server.disconnect()
        await asyncio.sleep(0.01)
        cut.set()

    return outcome


async def test__fenced_body_read__when_body_complete_before_the_cut__then_bytes_intact() -> None:
    """A fully buffered body is handed over from the buffer, so nothing suspends."""
    server = FakeServer(method="POST", defer_body=True, content_length=len(PAYLOAD))
    cut = asyncio.Event()
    read: list[bytes] = []

    async def endpoint(request: Request) -> Response:
        await wait_for(cut)
        with get_current_fencing().move_on_cancel():
            read.append(await request.body())
        return Response(b"ok")

    async with serve(starlette_app(endpoint, methods=["POST"]), server):
        await server.feed_body(PAYLOAD, more_body=False)
        await asyncio.sleep(0.01)
        server.disconnect()
        await asyncio.sleep(0.01)
        cut.set()

    assert read == [PAYLOAD]


# --- D6: Starlette's own StreamingResponse listener ---


def streaming_app(
    *,
    suspend: bool,
    fences: list[Fence],
    in_fence: asyncio.Event,
    downstream: list[str],
) -> Starlette:
    async def body() -> AsyncIterator[bytes]:
        yield b"start;"
        with get_current_fencing().move_on_cancel() as fence:
            fences.append(fence)
            in_fence.set()
            await asyncio.sleep(10)
        yield b"end;"

    async def endpoint(request: Request) -> Response:
        if suspend:
            await asyncio.sleep(0)
        return StreamingResponse(body())

    return starlette_app(endpoint, middleware=[Middleware(ReceiveProbe, seen=downstream)])


@pytest.mark.parametrize("suspend", [False, True], ids=["no-suspend", "suspend"])
@pytest.mark.parametrize("spec_version", ["2.0", "2.3", None], ids=["2.0", "2.3", "absent"])
async def test__streaming_response__when_rival_listener__then_both_readers_see_disconnect(
    spec_version: str | None,
    suspend: bool,  # noqa: FBT001
) -> None:
    """
    Below spec 2.4 ``StreamingResponse`` parks its own listener on the channel.
    Which of the two acts on the disconnect first is a scheduling matter — that
    both are told is not.
    """
    server = FakeServer(spec_version=spec_version)
    in_fence = asyncio.Event()
    fences: list[Fence] = []
    downstream: list[str] = []

    app = streaming_app(
        suspend=suspend,
        fences=fences,
        in_fence=in_fence,
        downstream=downstream,
    )
    async with serve(app, server):
        await wait_for(in_fence)
        server.disconnect()

    assert fences[0].cancelled_by("disconnect")  # our fencing saw it
    # listen_for_disconnect drains the empty body message first, then gets the replay
    assert downstream == ["http.request", "http.disconnect"]


@pytest.mark.parametrize("suspend", [False, True], ids=["no-suspend", "suspend"])
async def test__streaming_response__when_spec_is_2_4__then_fencing_sees_disconnect(
    suspend: bool,  # noqa: FBT001
) -> None:
    server = FakeServer(spec_version="2.4")
    in_fence = asyncio.Event()
    fences: list[Fence] = []
    downstream: list[str] = []

    app = streaming_app(
        suspend=suspend,
        fences=fences,
        in_fence=in_fence,
        downstream=downstream,
    )
    async with serve(app, server):
        await wait_for(in_fence)
        server.disconnect()

    assert fences[0].cancelled_by("disconnect")
    assert downstream == []  # no rival reader on this branch
    assert server.response_body == b"start;end;"  # so the body resumes and finishes


async def test__streaming_response__when_send_raises_on_2_4__then_fence_sees_disconnect() -> None:
    """
    On that branch ``StreamingResponse`` watches nothing and leans entirely on
    ``send`` failing, which it converts to ``ClientDisconnect``. The suppressed
    fence lets the generator resume, so its last chunk is what hits the closed
    connection — and the middleware must have set the event regardless.
    """
    server = FakeServer(spec_version="2.4", raise_on_send=True)
    in_fence = asyncio.Event()
    fences: list[Fence] = []

    async def body() -> AsyncIterator[bytes]:
        yield b"start;"
        with get_current_fencing().move_on_cancel() as fence:
            fences.append(fence)
            in_fence.set()
            await asyncio.sleep(10)
        yield b"end;"

    stream = body()

    async def endpoint(request: Request) -> Response:
        return StreamingResponse(stream)

    with pytest.raises(ClientDisconnect):
        async with serve(starlette_app(endpoint), server):
            await wait_for(in_fence)
            server.disconnect()

    await stream.aclose()  # the failed send leaves the generator parked on its last yield

    assert fences[0].cancelled_by("disconnect")


@pytest.mark.parametrize(
    "spec_version", ["2.0", "2.3", "2.4", None], ids=["2.0", "2.3", "2.4", "absent"]
)
async def test__streaming_response__when_client_stays__then_every_chunk_reaches_the_server(
    spec_version: str | None,
) -> None:
    """
    The disconnect cases dominate this file; nothing else proves the middleware
    stays out of the way when the client never leaves. A ``wrap_send`` that
    mistook an early ``more_body`` chunk for the end of the response would pass
    every other test here.
    """
    server = FakeServer(spec_version=spec_version)

    async def body() -> AsyncIterator[bytes]:
        for chunk in (b"a;", b"b;", b"c;"):
            yield chunk
            await asyncio.sleep(0)

    async def endpoint(request: Request) -> Response:
        return StreamingResponse(body())

    await run_app(starlette_app(endpoint), server)

    assert server.response_body == b"a;b;c;"
    assert server.status == 200


async def test__streaming_response__when_client_stays__then_disconnect_event_not_set() -> None:
    events: list[asyncio.Event] = []

    async def body() -> AsyncIterator[bytes]:
        yield b"a;"
        await asyncio.sleep(0)
        yield b"b;"

    async def endpoint(request: Request) -> Response:
        event = get_disconnect_event(request.scope)
        assert event is not None
        events.append(event)
        return StreamingResponse(body())

    await run_app(starlette_app(endpoint), FakeServer())

    assert not events[0].is_set()


# --- D9: BaseHTTPMiddleware ---


class BodyReadingMiddleware(BaseHTTPMiddleware):
    """Reads the body after ``call_next`` — the shape that trips reentrancy."""

    def __init__(self, app: object, seen: list[bytes]) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.seen = seen

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        self.seen.append(await request.body())
        return response


async def test__base_http_middleware__when_below_and_reads_body_late__then_body_intact() -> None:
    seen: list[bytes] = []

    async def endpoint(request: Request) -> Response:
        return Response(b"ok")

    app = starlette_app(
        endpoint,
        methods=["POST"],
        middleware=[Middleware(BodyReadingMiddleware, seen=seen)],
    )
    body = await run_app(app, FakeServer(method="POST", body=[PAYLOAD[:12], PAYLOAD[12:]]))

    assert seen == [PAYLOAD]
    assert body == b"ok"


async def test__base_http_middleware__when_below__then_disconnect_reaches_the_fence() -> None:
    server = FakeServer()
    entered = asyncio.Event()
    observed: list[bool] = []

    async def endpoint(request: Request) -> Response:
        entered.set()
        with get_current_fencing().move_on_cancel() as fence:
            await asyncio.sleep(10)
        observed.append(fence.cancelled_by("disconnect"))
        return Response(b"ok")

    app = starlette_app(
        endpoint, middleware=[Middleware(BaseHTTPMiddleware, dispatch=_passthrough)]
    )
    async with serve(app, server):
        await wait_for(entered)
        server.disconnect()

    assert observed == [True]


async def test__base_http_middleware__when_above__then_disconnect_reaches_the_fence() -> None:
    server = FakeServer()
    entered = asyncio.Event()
    observed: list[bool] = []

    async def endpoint(request: Request) -> Response:
        entered.set()
        with get_current_fencing().move_on_cancel() as fence:
            await asyncio.sleep(10)
        observed.append(fence.cancelled_by("disconnect"))
        return Response(b"ok")

    app = Starlette(
        routes=[Route("/work", endpoint)],
        middleware=[
            Middleware(BaseHTTPMiddleware, dispatch=_passthrough),
            Middleware(DisconnectMiddleware),
        ],
    )
    async with serve(app, server):
        await wait_for(entered)
        server.disconnect()

    assert observed == [True]


async def test__base_http_middleware__when_above__then_body_reaches_the_handler() -> None:
    read: list[bytes] = []

    async def endpoint(request: Request) -> Response:
        await asyncio.sleep(0.01)
        read.append(await request.body())
        return Response(b"ok")

    app = Starlette(
        routes=[Route("/work", endpoint, methods=["POST"])],
        middleware=[
            Middleware(BaseHTTPMiddleware, dispatch=_passthrough),
            Middleware(DisconnectMiddleware),
        ],
    )
    await run_app(app, FakeServer(method="POST", body=[PAYLOAD[:12], PAYLOAD[12:]]))

    assert read == [PAYLOAD]


# --- D13: exception handlers ---


async def test__exception_handler__when_handler_raises__then_event_readable_from_scope() -> None:
    seen: list[object] = []

    async def endpoint(request: Request) -> Response:
        raise HTTPException(status_code=418)

    async def teapot(request: Request, exc: Exception) -> Response:
        seen.append(get_disconnect_event(request.scope))
        return Response(b"teapot", status_code=418)

    app = starlette_app(endpoint, exception_handlers={HTTPException: teapot})
    server = FakeServer()
    await run_app(app, server)

    assert isinstance(seen[0], asyncio.Event)
    assert server.status == 418


async def test__exception_handler__when_handler_raises__then_fencing_still_bound() -> None:
    codes: list[list[str | None]] = []

    async def endpoint(request: Request) -> Response:
        raise HTTPException(status_code=418)

    async def teapot(request: Request, exc: Exception) -> Response:
        codes.append(bound_codes())
        return Response(b"teapot", status_code=418)

    app = starlette_app(endpoint, exception_handlers={HTTPException: teapot})
    await run_app(app, FakeServer())

    assert codes == [["disconnect"]]


async def test__exception_handler__when_client_left_first__then_event_is_set() -> None:
    server = FakeServer()
    entered = asyncio.Event()
    seen: list[bool] = []

    async def endpoint(request: Request) -> Response:
        entered.set()
        await wait_for(request.scope["aiofence.disconnect_event"])
        raise HTTPException(status_code=499, detail="client gone")

    async def gone(request: Request, exc: Exception) -> Response:
        event = get_disconnect_event(request.scope)
        seen.append(event is not None and event.is_set())
        return Response(b"gone", status_code=499)

    app = starlette_app(endpoint, exception_handlers={HTTPException: gone})
    async with serve(app, server):
        await wait_for(entered)
        server.disconnect()

    assert seen == [True]


async def _passthrough(request: Request, call_next: RequestResponseEndpoint) -> Response:
    return await call_next(request)


# --- on_disconnect sees the routed scope ---


async def test__on_disconnect__when_route_matched__then_scope_carries_route_template() -> None:
    """Routing mutates the scope in place, so the hook gets labels, not raw paths."""
    app = FastAPI()
    server = FakeServer(path="/items/42")
    entered = asyncio.Event()
    labels: list[tuple[str, str]] = []

    @app.get("/items/{item_id}")
    async def endpoint(item_id: int, request: Request) -> Response:
        entered.set()
        await wait_for(get_disconnect_event(request.scope))
        return Response("ok")

    def hook(scope: Scope) -> None:
        labels.append((scope["path"], scope["route"].path))

    async with serve(DisconnectMiddleware(app, on_disconnect=hook), server):
        await wait_for(entered)
        server.disconnect()

    assert labels == [("/items/42", "/items/{item_id}")]


async def test__watch__when_called__then_route_not_in_scope_yet() -> None:
    """The predicate runs above the router: raw path only, never a template."""
    app = FastAPI()
    keys: list[bool] = []

    @app.get("/items/{item_id}")
    async def endpoint(item_id: int) -> Response:
        return Response("ok")

    def watch(scope: Scope) -> bool:
        keys.append("route" in scope or "endpoint" in scope)
        return True

    await run_app(DisconnectMiddleware(app, watch=watch), FakeServer(path="/items/42"))

    assert keys == [False]
