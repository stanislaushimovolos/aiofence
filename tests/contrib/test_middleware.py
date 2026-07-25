"""
Core behaviour of ``DisconnectMiddleware`` against raw ASGI apps.

Framework integration lives in ``test_middleware_starlette.py``,
``test_middleware_dependencies.py`` and ``test_middleware_sse.py``. Findings
referenced by ID come from docs/disconnect-watcher-analysis.md.
"""

# ASGI callbacks have fixed signatures — unused parameters are structural.
# ruff: noqa: ARG001

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import pytest
from starlette.types import Message, Receive, Scope, Send

from aiofence import get_current_fencing
from aiofence.contrib.starlette import (
    DISCONNECT_CODE,
    DISCONNECT_EVENT_SCOPE_KEY,
    DisconnectMiddleware,
    get_disconnect_event,
    require_disconnect_event,
)

from .asgi_harness import bound_codes
from .server_harness import (
    DeliveryMode,
    FakeServer,
    SendError,
    read_body,
    respond,
    run_app,
    serve,
    wait_for,
    wait_until,
)


def published_event(scope: Scope) -> asyncio.Event:
    event = scope[DISCONNECT_EVENT_SCOPE_KEY]
    assert isinstance(event, asyncio.Event)
    return event


# --- scope contract ---


async def test__middleware__when_http_scope__then_publishes_event_at_documented_key() -> None:
    seen: list[object] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["aiofence.disconnect_event"])
        await respond(send)

    assert DISCONNECT_EVENT_SCOPE_KEY == "aiofence.disconnect_event"

    await run_app(DisconnectMiddleware(app), FakeServer())

    assert len(seen) == 1
    assert isinstance(seen[0], asyncio.Event)


async def test__get_disconnect_event__when_middleware_installed__then_returns_published_event() -> (
    None
):
    seen: list[asyncio.Event | None] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(get_disconnect_event(scope))
        seen.append(published_event(scope))
        await respond(send)

    await run_app(DisconnectMiddleware(app), FakeServer())

    assert seen[0] is seen[1]


async def test__get_disconnect_event__when_middleware_absent__then_returns_none() -> None:
    server = FakeServer()

    assert get_disconnect_event(server.scope) is None


# --- ambient lookup: no scope, no request ---


async def test__get_disconnect_event__when_called_without_scope__then_returns_published_event() -> (
    None
):
    seen: list[asyncio.Event | None] = []

    async def nested_callee() -> None:
        seen.append(get_disconnect_event())

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await nested_callee()
        seen.append(published_event(scope))
        await respond(send)

    await run_app(DisconnectMiddleware(app), FakeServer())

    assert seen[0] is seen[1]


async def test__get_disconnect_event__when_called_outside_a_request__then_returns_none() -> None:
    assert get_disconnect_event() is None


async def test__get_disconnect_event__when_request_ended__then_binding_released() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await respond(send)

    await run_app(DisconnectMiddleware(app), FakeServer())

    assert get_disconnect_event() is None


async def test__require_disconnect_event__when_middleware_absent__then_raises() -> None:
    with pytest.raises(RuntimeError, match="DisconnectMiddleware"):
        require_disconnect_event()


async def test__require_disconnect_event__when_called_without_scope__then_returns_event() -> None:
    seen: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(require_disconnect_event())
        seen.append(published_event(scope))
        await respond(send)

    await run_app(DisconnectMiddleware(app), FakeServer())

    assert seen[0] is seen[1]


async def test__get_disconnect_event__when_client_disconnects__then_ambient_event_set() -> None:
    server = FakeServer()
    entered = asyncio.Event()
    observed: list[bool] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        event = require_disconnect_event()
        entered.set()
        await wait_for(event)
        observed.append(event.is_set())
        await respond(send)

    async with serve(DisconnectMiddleware(app), server):
        await wait_for(entered)
        server.disconnect()

    assert observed == [True]


# --- D1: "response finished" is not "client left" ---


async def test__middleware__when_response_completes__then_disconnect_event_not_set() -> None:
    server = FakeServer()
    seen: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(published_event(scope))
        await respond(send)
        await wait_until(lambda: server.disconnects_delivered >= 1)

    body = await run_app(DisconnectMiddleware(app), server)

    assert body == b"ok"
    assert not seen[0].is_set()


async def test__middleware__when_response_completes__then_disconnect_replayed_without_event() -> (
    None
):
    """The stream really is over — downstream may read it, the event may not fire."""
    seen: list[str] = []
    events: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        events.append(published_event(scope))
        await read_body(receive)
        await respond(send)
        seen.append((await receive())["type"])

    await run_app(DisconnectMiddleware(app), FakeServer())

    assert seen == ["http.disconnect"]
    assert not events[0].is_set()


async def test__middleware__when_work_runs_after_response__then_fence_not_pre_triggered() -> None:
    server = FakeServer()
    outcome: dict[str, bool] = {}

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await respond(send)
        await wait_until(lambda: server.disconnects_delivered >= 1)
        with get_current_fencing().move_on_cancel() as fence:
            await asyncio.sleep(0.01)
        outcome["completed"] = not fence.cancelled

    await run_app(DisconnectMiddleware(app), server)

    assert outcome == {"completed": True}


async def test__middleware__when_client_disconnects_before_response__then_event_set() -> None:
    server = FakeServer()
    entered = asyncio.Event()
    seen: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(published_event(scope))
        entered.set()
        await wait_for(seen[0])
        await respond(send)

    async with serve(DisconnectMiddleware(app), server):
        await wait_for(entered)
        server.disconnect()

    assert seen[0].is_set()


async def test__middleware__when_client_disconnects_before_response__then_fence_cancelled() -> None:
    server = FakeServer()
    entered = asyncio.Event()
    observed: list[bool] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        entered.set()
        with get_current_fencing().move_on_cancel() as fence:
            await asyncio.sleep(10)
        observed.append(fence.cancelled_by("disconnect"))
        await respond(send)

    async with serve(DisconnectMiddleware(app), server):
        await wait_for(entered)
        server.disconnect()

    assert observed == [True]


async def test__middleware__when_client_disconnects_mid_stream__then_event_set() -> None:
    server = FakeServer()
    started = asyncio.Event()
    seen: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(published_event(scope))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        started.set()
        await wait_for(seen[0])
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async with serve(DisconnectMiddleware(app), server):
        await wait_for(started)
        server.disconnect()

    assert seen[0].is_set()


# --- D17: response trailers extend the response past the final body message ---


def start_with_trailers() -> Message:
    return {"type": "http.response.start", "status": 200, "headers": [], "trailers": True}


async def test__middleware__when_client_disconnects_before_trailers__then_event_set() -> None:
    server = FakeServer()
    seen: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(published_event(scope))
        await send(start_with_trailers())
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})
        server.disconnect()  # the client leaves while the trailers are still owed
        await wait_until(lambda: server.disconnects_delivered >= 1)
        await send({"type": "http.response.trailers", "headers": [], "more_trailers": False})

    await run_app(DisconnectMiddleware(app), server)

    assert seen[0].is_set()


async def test__middleware__when_client_disconnects_between_trailers__then_event_set() -> None:
    server = FakeServer()
    seen: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(published_event(scope))
        await send(start_with_trailers())
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})
        await send({"type": "http.response.trailers", "headers": [], "more_trailers": True})
        server.disconnect()
        await wait_until(lambda: server.disconnects_delivered >= 1)
        await send({"type": "http.response.trailers", "headers": [], "more_trailers": False})

    await run_app(DisconnectMiddleware(app), server)

    assert seen[0].is_set()


async def test__middleware__when_trailers_complete_the_response__then_event_not_set() -> None:
    server = FakeServer()
    seen: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(published_event(scope))
        await send(start_with_trailers())
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})
        await send({"type": "http.response.trailers", "headers": [], "more_trailers": False})
        await wait_until(lambda: server.disconnects_delivered >= 1)

    await run_app(DisconnectMiddleware(app), server)

    assert not seen[0].is_set()


# --- D18: ASGI 2.4 — send raises once the connection is closed ---


def response_start() -> Message:
    return {"type": "http.response.start", "status": 200, "headers": []}


def terminal_body() -> Message:
    return {"type": "http.response.body", "body": b"ok", "more_body": False}


async def test__middleware__when_send_raises_after_client_left__then_event_set() -> None:
    """
    The spec warns that ``send`` can fail before ``http.disconnect`` reaches a
    reader. That message completes the response, so a completion flag left to
    decide alone would read the disconnect as "stream ended".
    """
    server = FakeServer(raise_on_send=True)
    seen: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(published_event(scope))
        await send(response_start())
        server.disconnect()  # nothing is awaited after this, so the read loop stays parked
        with suppress(OSError):
            await send(terminal_body())

    await run_app(DisconnectMiddleware(app), server)

    assert seen[0].is_set()


async def test__middleware__when_send_raises__then_error_reaches_the_app() -> None:
    server = FakeServer(raise_on_send=True)
    caught: list[BaseException] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(response_start())
        server.disconnect()
        try:
            await send(terminal_body())
        except OSError as exc:
            caught.append(exc)

    await run_app(DisconnectMiddleware(app), server)

    assert [type(exc) for exc in caught] == [SendError]


async def test__middleware__when_send_raises__then_downstream_receive_answers_disconnect() -> None:
    """
    The connection is closed and no further body is coming, so a reader below
    must not park on a server that delivers its disconnect only once.
    """
    server = FakeServer(delivery=DeliveryMode.ONCE, raise_on_send=True)
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(response_start())
        server.disconnect()
        with suppress(OSError):
            await send(terminal_body())
        seen.append((await receive())["type"])

    await run_app(DisconnectMiddleware(app), server)

    assert seen == ["http.disconnect"]


async def test__middleware__when_send_fails_without_oserror__then_event_not_set() -> None:
    """A protocol error out of ``send`` is not evidence the client left."""
    server = FakeServer()
    seen: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(published_event(scope))
        await send(response_start())

    async def failing_send(message: Message) -> None:
        raise RuntimeError("Unexpected ASGI message")

    with pytest.raises(RuntimeError):
        await DisconnectMiddleware(app)(server.scope, server.receive, failing_send)

    assert not seen[0].is_set()


# --- response completion: pathsend, and completion as a latch ---


def pathsend() -> Message:
    return {"type": "http.response.pathsend", "path": "/srv/static/report.pdf"}


async def test__middleware__when_pathsend_completes_the_response__then_event_not_set() -> None:
    """``http.response.pathsend`` ends the response in place of a body message."""
    server = FakeServer()
    seen: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(published_event(scope))
        await send(response_start())
        await send(pathsend())
        await wait_until(lambda: server.disconnects_delivered >= 1)

    await run_app(DisconnectMiddleware(app), server)

    assert not seen[0].is_set()


async def test__middleware__when_pathsend_still_owes_trailers__then_event_set() -> None:
    server = FakeServer()
    seen: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(published_event(scope))
        await send(start_with_trailers())
        await send(pathsend())
        server.disconnect()  # the client leaves while the trailers are still owed
        await wait_until(lambda: server.disconnects_delivered >= 1)
        await send({"type": "http.response.trailers", "headers": [], "more_trailers": False})

    await run_app(DisconnectMiddleware(app), server)

    assert seen[0].is_set()


async def test__middleware__when_message_follows_the_completed_response__then_event_not_set() -> (
    None
):
    """
    Completion is a latch: a message sent past the end of the response — here
    trailers that were never promised — cannot reopen it and turn the server's
    "stream ended" disconnect into a client that left.
    """
    server = FakeServer()
    seen: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(published_event(scope))
        await send(response_start())
        await send(terminal_body())
        await send({"type": "http.response.trailers", "headers": [], "more_trailers": True})
        server.disconnect()
        await wait_until(lambda: server.disconnects_delivered >= 1)

    await run_app(DisconnectMiddleware(app), server)

    assert not seen[0].is_set()


# --- D5: body chunks are forwarded, not discarded ---


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(b'{"query":"IMPORTANT PAYLOAD"}', b'{"query":"IMPORTANT PAYLOAD"}', id="one"),
        pytest.param([b'{"a":', b"1,", b'"b":2}'], b'{"a":1,"b":2}', id="chunked"),
        pytest.param([b"", b"tail"], b"tail", id="empty-first-chunk"),
        pytest.param([b"head", b""], b"head", id="empty-last-chunk"),
    ],
)
async def test__middleware__when_body_queued__then_downstream_reads_exact_bytes(
    body: bytes | list[bytes],
    expected: bytes,
) -> None:
    read: list[bytes] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        read.append(await read_body(receive))
        await respond(send)

    await run_app(DisconnectMiddleware(app), FakeServer(method="POST", body=body))

    assert read == [expected]


async def test__middleware__when_downstream_reads_after_suspension__then_body_intact() -> None:
    read: list[bytes] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await asyncio.sleep(0.01)  # the channel reader is parked in receive() by now
        read.append(await read_body(receive))
        await respond(send)

    server = FakeServer(method="POST", body=b'{"query":"IMPORTANT PAYLOAD"}')
    await run_app(DisconnectMiddleware(app), server)

    assert read == [b'{"query":"IMPORTANT PAYLOAD"}']


async def test__middleware__when_body_arrives_late__then_downstream_reads_exact_bytes() -> None:
    server = FakeServer(method="POST", defer_body=True, content_length=29)
    reading = asyncio.Event()
    read: list[bytes] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        reading.set()
        read.append(await read_body(receive))
        await respond(send)

    async with serve(DisconnectMiddleware(app), server):
        await wait_for(reading)
        await asyncio.sleep(0.01)
        await server.feed_body(b'{"query":"IMPORTANT ', more_body=True)
        await server.feed_body(b'PAYLOAD"}', more_body=False)

    assert read == [b'{"query":"IMPORTANT PAYLOAD"}']


async def test__middleware__when_body_queued_before_disconnect__then_body_delivered_first() -> None:
    server = FakeServer(method="POST", body=[b"head", b"tail"])
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await asyncio.sleep(0.01)
        for _ in range(3):
            message = await receive()
            seen.append(message["type"])
        await respond(send)

    async with serve(DisconnectMiddleware(app), server):
        server.disconnect()

    assert seen == ["http.request", "http.request", "http.disconnect"]


# --- D8: one-shot servers ---


async def test__middleware__when_disconnect_delivered_once__then_replayed_to_every_reader() -> None:
    server = FakeServer(delivery=DeliveryMode.ONCE)
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await read_body(receive)
        seen.extend([(await receive())["type"] for _ in range(3)])
        await respond(send)

    async with serve(DisconnectMiddleware(app), server):
        server.disconnect()

    assert seen == ["http.disconnect"] * 3
    assert server.disconnects_delivered == 1
    assert server.parked == 0


async def test__middleware__when_disconnect_delivered_once__then_event_still_set() -> None:
    server = FakeServer(delivery=DeliveryMode.ONCE)
    seen: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(published_event(scope))
        await read_body(receive)
        await receive()
        await respond(send)

    async with serve(DisconnectMiddleware(app), server):
        server.disconnect()

    assert seen[0].is_set()


async def test__middleware__when_two_readers_park__then_both_get_disconnect() -> None:
    server = FakeServer(delivery=DeliveryMode.ONCE)
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await read_body(receive)
        async with asyncio.TaskGroup() as group:
            first = group.create_task(receive())
            second = group.create_task(receive())
        seen.extend(sorted([first.result()["type"], second.result()["type"]]))
        await respond(send)

    async with serve(DisconnectMiddleware(app), server):
        server.disconnect()

    assert seen == ["http.disconnect", "http.disconnect"]


# --- D4: a receive that raises ---


async def test__middleware__when_receive_raises__then_error_surfaces_to_downstream_reader() -> None:
    boom = RuntimeError("Unexpected message received: http.weird")
    caught: list[BaseException] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await receive()
        except RuntimeError as exc:
            caught.append(exc)
        await respond(send)

    await run_app(DisconnectMiddleware(app), FakeServer(error=boom))

    assert caught == [boom]


async def test__middleware__when_receive_raises__then_disconnect_event_not_set() -> None:
    seen: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(published_event(scope))
        await asyncio.sleep(0.01)
        await respond(send)

    await run_app(DisconnectMiddleware(app), FakeServer(error=RuntimeError("channel")))

    assert not seen[0].is_set()


async def test__middleware__when_receive_raises_and_never_read__then_response_intact() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await asyncio.sleep(0.01)
        await respond(send, b"payload")

    server = FakeServer(error=RuntimeError("channel"))
    body = await run_app(DisconnectMiddleware(app), server)

    assert body == b"payload"
    assert server.status == 200


async def test__middleware__when_receive_raises_and_never_read__then_warning_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await asyncio.sleep(0.01)
        await respond(send)

    with caplog.at_level(logging.WARNING, logger="aiofence.contrib.starlette"):
        await run_app(DisconnectMiddleware(app), FakeServer(error=RuntimeError("channel")))

    ours = [r for r in caplog.records if r.name == "aiofence.contrib.starlette"]

    assert [r.levelno for r in ours] == [logging.WARNING]


async def test__middleware__when_app_and_receive_both_raise__then_app_exception_wins() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await asyncio.sleep(0.01)
        raise ValueError("ORIGINAL")

    with pytest.raises(ValueError, match="ORIGINAL"):
        await run_app(DisconnectMiddleware(app), FakeServer(error=RuntimeError("channel")))


# --- D16: non-http scopes, bounded queues ---


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
async def test__middleware__when_non_http_scope__then_channel_passed_through(
    scope_type: str,
) -> None:
    seen: dict[str, object] = {}

    async def app(scope: Scope, inner_receive: Receive, inner_send: Send) -> None:
        seen["receive"] = inner_receive
        seen["send"] = inner_send
        seen["published"] = DISCONNECT_EVENT_SCOPE_KEY in scope

    async def receive() -> Message:
        return {"type": f"{scope_type}.disconnect"}

    async def send(message: Message) -> None:
        return

    scope: Scope = {"type": scope_type}
    async with asyncio.timeout(1):
        await DisconnectMiddleware(app)(scope, receive, send)

    assert seen == {"receive": receive, "send": send, "published": False}


async def test__middleware__when_websocket_scope__then_no_receive_spin() -> None:
    calls = 0

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: Message) -> None:
        return

    async def app(scope: Scope, inner_receive: Receive, inner_send: Send) -> None:
        await inner_receive()
        await asyncio.sleep(0.02)

    async with asyncio.timeout(1):
        await DisconnectMiddleware(app)({"type": "websocket"}, receive, send)

    assert calls == 1


async def test__middleware__when_server_queue_is_bounded__then_feeding_never_stalls() -> None:
    server = FakeServer(method="POST", defer_body=True, max_queue_size=10, content_length=125)
    reading = asyncio.Event()
    fed = asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        reading.set()  # the app never reads the body at all
        await wait_for(fed)
        await respond(send)

    async with serve(DisconnectMiddleware(app), server):
        await wait_for(reading)
        for index in range(25):
            await server.feed_body(b"chunk", more_body=index < 24)
        fed.set()

    assert server.undelivered == 0


# --- lifecycle ---


async def test__middleware__when_request_completes__then_scope_key_released() -> None:
    server = FakeServer()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert DISCONNECT_EVENT_SCOPE_KEY in scope
        await respond(send)

    await run_app(DisconnectMiddleware(app), server)

    assert DISCONNECT_EVENT_SCOPE_KEY not in server.scope


async def test__middleware__when_app_raises__then_scope_key_released() -> None:
    server = FakeServer()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await run_app(DisconnectMiddleware(app), server)

    assert DISCONNECT_EVENT_SCOPE_KEY not in server.scope


async def test__middleware__when_app_raises__then_exception_propagates_unchanged() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await asyncio.sleep(0.01)
        raise ValueError("ORIGINAL")

    with pytest.raises(ValueError, match="ORIGINAL"):
        await run_app(DisconnectMiddleware(app), FakeServer())


async def test__middleware__when_request_completes__then_no_tasks_leaked() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await read_body(receive)
        await respond(send)

    before = len(asyncio.all_tasks())
    await run_app(DisconnectMiddleware(app), FakeServer())

    assert len(asyncio.all_tasks()) == before


async def test__middleware__when_app_raises__then_no_tasks_leaked() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise ValueError("boom")

    before = len(asyncio.all_tasks())
    with pytest.raises(ValueError, match="boom"):
        await run_app(DisconnectMiddleware(app), FakeServer())

    assert len(asyncio.all_tasks()) == before


async def test__middleware__when_client_disconnects_mid_request__then_no_tasks_leaked() -> None:
    server = FakeServer()
    entered = asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        entered.set()
        await wait_for(published_event(scope))
        await respond(send)

    before = len(asyncio.all_tasks())
    async with serve(DisconnectMiddleware(app), server):
        await wait_for(entered)
        server.disconnect()

    assert len(asyncio.all_tasks()) == before


async def test__middleware__when_outer_task_cancelled__then_cancelled_error_propagates() -> None:
    server = FakeServer()
    entered = asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        entered.set()
        await asyncio.sleep(10)

    async with serve(DisconnectMiddleware(app), server) as task:
        await wait_for(entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test__middleware__when_outer_task_cancelled__then_no_tasks_leaked() -> None:
    server = FakeServer()
    entered = asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        entered.set()
        await asyncio.sleep(10)

    before = len(asyncio.all_tasks())
    async with serve(DisconnectMiddleware(app), server) as task:
        await wait_for(entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(asyncio.all_tasks()) == before


async def test__middleware__when_installed_once__then_runs_a_single_reader_task() -> None:
    counted: list[int] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        counted.append(len(asyncio.all_tasks()))
        await respond(send)

    before = len(asyncio.all_tasks())
    await run_app(DisconnectMiddleware(app), FakeServer())

    assert counted == [before + 1]


async def test__middleware__when_installed_twice__then_runs_a_single_reader_task() -> None:
    counted: list[int] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        counted.append(len(asyncio.all_tasks()))
        await respond(send)

    before = len(asyncio.all_tasks())
    await run_app(DisconnectMiddleware(DisconnectMiddleware(app)), FakeServer())

    assert counted == [before + 1]


async def test__middleware__when_installed_twice__then_same_event_shared() -> None:
    seen: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(published_event(scope))
        await respond(send)

    async def middle(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(published_event(scope))
        await DisconnectMiddleware(app)(scope, receive, send)

    await run_app(DisconnectMiddleware(middle), FakeServer())

    assert len(seen) == 2
    assert seen[0] is seen[1]


async def test__middleware__when_installed_twice__then_disconnect_reaches_the_app() -> None:
    server = FakeServer(delivery=DeliveryMode.ONCE)
    seen: list[asyncio.Event] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(published_event(scope))
        await read_body(receive)
        await receive()
        await respond(send)

    async with serve(DisconnectMiddleware(DisconnectMiddleware(app)), server):
        server.disconnect()

    assert seen[0].is_set()
    assert server.parked == 0


# --- fencing binding ---


async def test__middleware__when_fencing_code_set__then_fencing_bound_for_the_request() -> None:
    codes: list[list[str | None]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        codes.append(bound_codes())
        await respond(send)

    await run_app(DisconnectMiddleware(app, fencing_code="client_gone"), FakeServer())

    assert codes == [["client_gone"]]


async def test__middleware__when_fencing_code_omitted__then_default_code_bound() -> None:
    codes: list[list[str | None]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        codes.append(bound_codes())
        await respond(send)

    assert DISCONNECT_CODE == "disconnect"

    await run_app(DisconnectMiddleware(app), FakeServer())

    assert codes == [[DISCONNECT_CODE]]


async def test__middleware__when_fencing_code_set__then_bound_event_is_the_published_one() -> None:
    matched: list[bool] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        events = [entry.event for entry in get_current_fencing()._events]
        matched.append(events == [published_event(scope)])
        await respond(send)

    await run_app(DisconnectMiddleware(app), FakeServer())

    assert matched == [True]


async def test__middleware__when_request_ends__then_fencing_binding_released() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await respond(send)

    await run_app(DisconnectMiddleware(app), FakeServer())

    assert bound_codes() == []


# --- on_disconnect hook ---


async def test__on_disconnect__when_client_leaves_before_response__then_called_with_scope() -> None:
    server = FakeServer()
    entered = asyncio.Event()
    notified: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        entered.set()
        await wait_for(published_event(scope))
        await respond(send)

    async def hook(scope: Scope) -> None:
        notified.append(scope)

    middleware = DisconnectMiddleware(app, on_disconnect=hook)
    async with serve(middleware, server):
        await wait_for(entered)
        server.disconnect()

    assert [scope["path"] for scope in notified] == ["/work"]


async def test__on_disconnect__when_response_completes__then_not_called() -> None:
    notified: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await respond(send)

    async def hook(scope: Scope) -> None:
        notified.append(scope)

    await run_app(DisconnectMiddleware(app, on_disconnect=hook), FakeServer())

    assert notified == []


async def test__on_disconnect__when_hook_is_sync__then_called() -> None:
    server = FakeServer()
    entered = asyncio.Event()
    notified: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        entered.set()
        await wait_for(published_event(scope))

    async with serve(DisconnectMiddleware(app, on_disconnect=notified.append), server):
        await wait_for(entered)
        server.disconnect()

    assert len(notified) == 1


async def test__on_disconnect__when_hook_raises__then_response_intact() -> None:
    server = FakeServer()
    entered = asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        entered.set()
        await wait_for(published_event(scope))
        await respond(send, b"payload")

    def hook(scope: Scope) -> None:
        raise RuntimeError("metrics down")

    async with serve(DisconnectMiddleware(app, on_disconnect=hook), server):
        await wait_for(entered)
        server.disconnect()

    assert server.status == 200


async def test__on_disconnect__when_hook_raises__then_error_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = FakeServer()
    entered = asyncio.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        entered.set()
        await wait_for(published_event(scope))

    def hook(scope: Scope) -> None:
        raise RuntimeError("metrics down")

    with caplog.at_level(logging.ERROR, logger="aiofence.contrib.starlette"):
        async with serve(DisconnectMiddleware(app, on_disconnect=hook), server):
            await wait_for(entered)
            server.disconnect()

    assert [r.levelno for r in caplog.records] == [logging.ERROR]


async def test__on_disconnect__when_app_raises_after_disconnect__then_called() -> None:
    server = FakeServer()
    entered = asyncio.Event()
    notified: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        entered.set()
        await wait_for(published_event(scope))
        raise ValueError("ORIGINAL")

    async def hook(scope: Scope) -> None:
        notified.append(scope)

    with pytest.raises(ValueError, match="ORIGINAL"):
        async with serve(DisconnectMiddleware(app, on_disconnect=hook), server):
            await wait_for(entered)
            server.disconnect()

    assert len(notified) == 1


async def test__on_disconnect__when_receive_raises__then_not_called() -> None:
    """A broken channel is not evidence the client left."""
    notified: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await asyncio.sleep(0.01)
        await respond(send)

    middleware = DisconnectMiddleware(app, on_disconnect=notified.append)
    await run_app(middleware, FakeServer(error=RuntimeError("channel")))

    assert notified == []


async def test__on_disconnect__when_send_raises_after_client_left__then_called() -> None:
    server = FakeServer(raise_on_send=True)
    entered = asyncio.Event()
    notified: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        entered.set()
        server.disconnect()
        await asyncio.sleep(0)
        with suppress(SendError):
            await respond(send)

    async with serve(DisconnectMiddleware(app, on_disconnect=notified.append), server):
        await wait_for(entered)

    assert len(notified) == 1


# --- watch predicate ---


async def test__watch__when_predicate_accepts__then_request_watched() -> None:
    published: list[bool] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        published.append(DISCONNECT_EVENT_SCOPE_KEY in scope)
        await respond(send)

    await run_app(DisconnectMiddleware(app, watch=lambda scope: True), FakeServer())

    assert published == [True]


async def test__watch__when_predicate_declines__then_channel_passed_through() -> None:
    server = FakeServer()
    seen: dict[str, object] = {}

    async def app(scope: Scope, inner_receive: Receive, inner_send: Send) -> None:
        seen["receive"] = inner_receive
        seen["published"] = DISCONNECT_EVENT_SCOPE_KEY in scope
        await respond(inner_send)

    await run_app(DisconnectMiddleware(app, watch=lambda scope: False), server)

    assert seen == {"receive": server.receive, "published": False}


async def test__watch__when_predicate_declines__then_no_fencing_binding() -> None:
    codes: list[list[str | None]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        codes.append(bound_codes())
        await respond(send)

    await run_app(DisconnectMiddleware(app, watch=lambda scope: False), FakeServer())

    assert codes == [[]]


async def test__watch__when_omitted__then_every_http_request_watched() -> None:
    published: list[bool] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        published.append(DISCONNECT_EVENT_SCOPE_KEY in scope)
        await respond(send)

    await run_app(DisconnectMiddleware(app), FakeServer(path="/anything"))

    assert published == [True]


async def test__watch__when_called__then_receives_the_request_scope() -> None:
    asked: list[tuple[str, str]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await respond(send)

    def watch(scope: Scope) -> bool:
        asked.append((scope["method"], scope["path"]))
        return True

    await run_app(DisconnectMiddleware(app, watch=watch), FakeServer(path="/stream/42"))

    assert asked == [("GET", "/stream/42")]


async def test__watch__when_non_http_scope__then_predicate_not_called() -> None:
    """Scope type is settled first, so a predicate never has to guard it."""
    asked: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        return

    async def receive() -> Message:
        return {"type": "lifespan.shutdown"}

    async def send(message: Message) -> None:
        return

    def watch(scope: Scope) -> bool:
        asked.append(scope)
        return True

    middleware = DisconnectMiddleware(app, watch=watch)
    async with asyncio.timeout(1):
        await middleware({"type": "lifespan"}, receive, send)

    assert asked == []


async def test__watch__when_already_published__then_predicate_not_called() -> None:
    """The outer instance already owns the channel; the inner one has no say."""
    asked: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await respond(send)

    def watch(scope: Scope) -> bool:
        asked.append(scope)
        return True

    inner = DisconnectMiddleware(app, watch=watch)
    await run_app(DisconnectMiddleware(inner), FakeServer())

    assert asked == []


async def test__watch__when_predicate_raises__then_error_propagates() -> None:
    """Ownership is load-bearing — a broken predicate is not silently answered."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await respond(send)

    def watch(scope: Scope) -> bool:
        raise RuntimeError("BROKEN PREDICATE")

    with pytest.raises(RuntimeError, match="BROKEN PREDICATE"):
        await run_app(DisconnectMiddleware(app, watch=watch), FakeServer())


async def test__watch__when_predicate_declines__then_on_disconnect_not_called() -> None:
    notified: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await respond(send)

    middleware = DisconnectMiddleware(app, watch=lambda scope: False, on_disconnect=notified.append)
    await run_app(middleware, FakeServer())

    assert notified == []


async def test__require_disconnect_event__when_watch_declined__then_error_names_the_predicate() -> (
    None
):
    caught: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            require_disconnect_event(scope)
        except RuntimeError as exc:
            caught.append(str(exc))
        await respond(send)

    await run_app(DisconnectMiddleware(app, watch=lambda scope: False), FakeServer())

    assert "watch" in caught[0]
