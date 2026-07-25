"""
ASGI middleware that owns the request's receive channel.

Why middleware and not a dependency
-----------------------------------
An ASGI receive channel has exactly one useful reader: ``receive()`` is a queue
pop, not a broadcast. Anything else in the stack that reads it splits the
message stream with us. A dependency cannot fix that, because Starlette hands
``StreamingResponse`` / ``EventSourceResponse`` the raw ``receive`` captured
before any dependency runs — there is no reference for a dependency to wrap.
The middleware sits above the whole stack, reads the channel exactly once, and
replays what it read to everyone below.

Contract for ``http`` scopes
----------------------------
* Publishes an ``asyncio.Event`` at ``scope["aiofence.disconnect_event"]``
  (``DISCONNECT_EVENT_SCOPE_KEY``) and binds the same object for the request's
  context, so ``get_disconnect_event()`` answers with or without a scope —
  nothing below has to be handed the request to ask. Both are dropped when the
  request ends, so no caller can hold a reference to an event whose reader is
  gone.
* Runs one pump task per request, the sole caller of the server's ``receive``.
* Forwards every ``http.request`` message downstream, in order and unchanged,
  so ``request.body()`` / ``request.stream()`` keep working under fencing.
* Treats ``http.disconnect`` as a **terminal side channel**: it is latched, never
  queued behind body chunks, and re-delivered on every later ``receive()``.
  Servers that emit it exactly once (hypercorn, daphne, granian) therefore still
  feed Starlette's and sse-starlette's own disconnect listeners.
* Wraps ``send`` to track response completion. A disconnect observed after the
  response's terminal message means "response finished", not "client left", and
  must not set the event. This is the only place in the stack where the two can
  be told apart — every server collapses them into the same message.

``response_complete`` is flipped **before** the terminal message is passed to the
server's ``send``. The pump can only be woken by the server having processed
that message, so flipping first closes the window where a completion disconnect
would be misread as a client disconnect.

Which message is terminal depends on the response: the final ``http.response.body``
normally, but the final ``http.response.trailers`` when ``http.response.start``
promised ``trailers``. A disconnect arriving while trailers are still owed is a
real client disconnect, and setting the flag on the body message would swallow
it.

The pump stops as soon as it latches a disconnect. Reading past it would spin:
uvicorn re-delivers the message immediately and forever, and the whole
background-task phase runs inside the request.

What replay does and does not settle
------------------------------------
Both readers are told. Which one acts first stays a scheduling matter, and it no
longer decides whether the disconnect is seen at all. So a fenced streaming or
SSE body can legitimately outlive its rival listener's cancel scope: the fence
suppressed the cancellation on purpose and the generator emits its last chunk.
An unfenced body is still torn down by the rival listener as before.

Install position
----------------
Outermost. First entry of ``middleware=[...]``, or the last ``add_middleware``
call, so the middleware owns the server's own ``receive`` rather than another
middleware's wrapper. Installed below a ``BaseHTTPMiddleware`` it still works,
but it then owns that middleware's non-reentrant ``wrapped_receive``.

Installed twice, the outer instance wins: an inner instance finds the scope key
already present and passes the request through untouched, so there is still one
reader and one event.

Non-``http`` scopes (``lifespan``, ``websocket``) are passed through with the
original ``receive`` and ``send``, and no scope key is published. Wrapping them
would be a busy loop — a websocket scope answers ``websocket.disconnect``
immediately and forever, and it never equals ``http.disconnect``.

When ``receive`` raises
-----------------------
The pump latches the exception and re-raises it from the next downstream
``receive()`` call, at the point where the application actually reads. The
disconnect event is **not** set: a broken channel is not evidence the client
left, and setting it would turn a transport error into a spurious cancellation.
If nothing ever reads again, the error is logged at ``WARNING`` on
``aiofence.contrib.starlette`` when the request ends, rather than being
re-raised after the response has already gone out. The application's own
exception is never replaced.

Fencing binding
---------------
By default the middleware only owns the channel and publishes the event;
binding a ``Fencing`` is the dependency's job, which keeps codes selectable per
route. Pass ``fencing_code`` to bind app-wide instead — useful for plain ASGI
or plain Starlette, and it makes the binding visible to exception handlers,
which run outside FastAPI's dependency stacks. Both together are safe: they
share one event object.

``aiofence.contrib.fastapi`` has no fallback: its dependencies borrow the
published event and raise ``RuntimeError`` when there is none. Anything reading
the channel from below the middleware would steal messages from the rest of the
stack and could not tell a departed client from a completed response, so a
degraded mode would only be a quieter bug. Code that wants to *ask* rather than
require uses ``get_disconnect_event(scope)``, which returns ``None`` when the
middleware is absent.

asyncio only
------------
Uses ``asyncio.Event`` and ``asyncio.create_task`` directly. Starlette and
FastAPI are pure anyio and run under Trio, aiofence does not: ``Fencing.event()``
is typed against ``asyncio.Event``. Under Trio this fails loudly at request time.

Cost
----
One task per request, and the request body is buffered in memory for the
lifetime of the request whether or not the application reads it. That is
deliberate — draining the server's queue is what keeps a bounded server queue
(hypercorn's ``max_app_queue_size``, default 10) from stalling the connection —
but it does defeat server read backpressure on large uploads.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import ExitStack, suppress
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aiofence import bind_fencing, get_current_fencing

DISCONNECT_EVENT_SCOPE_KEY = "aiofence.disconnect_event"

_CURRENT_EVENT: ContextVar[asyncio.Event | None] = ContextVar(
    "aiofence_disconnect_event", default=None
)

_LOGGER = logging.getLogger("aiofence.contrib.starlette")


class DisconnectMiddleware:
    """
    Own the receive channel for one request and publish its disconnect event.

    Args:
        app: Next ASGI application in the stack.
        fencing_code: When set, the middleware also binds
            ``get_current_fencing().event(event, code=fencing_code)`` for the
            whole request. Left unset, binding is the dependency's job.
    """

    def __init__(self, app: ASGIApp, *, fencing_code: str | None = None) -> None:
        self.app = app
        self.fencing_code = fencing_code

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or DISCONNECT_EVENT_SCOPE_KEY in scope:
            await self.app(scope, receive, send)
            return

        channel = _RequestChannel(receive)
        scope[DISCONNECT_EVENT_SCOPE_KEY] = channel.event
        bound = _CURRENT_EVENT.set(channel.event)
        channel.start()
        try:
            await self._call_app(scope, channel, send)
        finally:
            _CURRENT_EVENT.reset(bound)
            scope.pop(DISCONNECT_EVENT_SCOPE_KEY, None)
            await channel.aclose()

    async def _call_app(self, scope: Scope, channel: _RequestChannel, send: Send) -> None:
        with ExitStack() as bindings:
            if self.fencing_code is not None:
                fencing = get_current_fencing().event(channel.event, code=self.fencing_code)
                bindings.enter_context(bind_fencing(fencing))

            await self.app(scope, channel.receive, channel.wrap_send(send))


def get_disconnect_event(scope: Scope | None = None) -> asyncio.Event | None:
    """
    Return the request's disconnect event, or ``None`` when
    ``DisconnectMiddleware`` is not installed.

    Called with no argument it reads the ambient request: the middleware binds
    the event for the whole request, so code anywhere below it can ask without
    being handed the request. Pass a ``scope`` when one is at hand — an
    exception handler, or anything reached from a task the request did not
    spawn itself.

    Use this to branch on whether disconnect signalling is available. Callers
    that cannot work without it should use ``require_disconnect_event``.
    """
    event = _CURRENT_EVENT.get() if scope is None else scope.get(DISCONNECT_EVENT_SCOPE_KEY)
    return event if isinstance(event, asyncio.Event) else None


def require_disconnect_event(scope: Scope | None = None) -> asyncio.Event:
    """
    Return the request's disconnect event, or raise ``RuntimeError`` when
    ``DisconnectMiddleware`` is not installed.

    Same lookup as ``get_disconnect_event`` — ambient by default, explicit with
    a ``scope``.

    There is no watch-it-yourself fallback: reading the receive channel from
    anywhere below the middleware steals messages from the rest of the stack
    and cannot tell a departed client from a completed response.
    """
    event = get_disconnect_event(scope)
    if event is None:
        raise RuntimeError(_MISSING_MIDDLEWARE)
    return event


_MISSING_MIDDLEWARE = (
    "aiofence disconnect signalling requires DisconnectMiddleware. Install it "
    "outermost: FastAPI(middleware=[Middleware(DisconnectMiddleware)]) or, "
    "equivalently, as the last app.add_middleware(DisconnectMiddleware) call."
)


class _RequestChannel:
    """
    Sole reader of one request's receive channel.

    ``start`` launches the pump, ``receive`` is what the application below sees,
    ``wrap_send`` returns the ``send`` that tracks response completion, and
    ``aclose`` reaps the pump and unparks any reader left waiting.
    """

    def __init__(self, receive: Receive) -> None:
        self._receive = receive
        self._event = asyncio.Event()
        self._body: deque[Message] = deque()
        self._readers: list[asyncio.Future[None]] = []
        self._pump: asyncio.Task[None] | None = None
        self._ended = False
        self._response_complete = False
        self._trailers_promised = False
        self._failure: BaseException | None = None
        self._failure_reported = False

    @property
    def event(self) -> asyncio.Event:
        return self._event

    def start(self) -> None:
        self._pump = asyncio.create_task(self._pump_channel())

    async def aclose(self) -> None:
        await self._reap_pump()
        self._ended = True
        self._wake_readers()
        if self._failure is not None and not self._failure_reported:
            _LOGGER.warning(
                "aiofence receive channel failed and nothing read it: %r", self._failure
            )

    async def receive(self) -> Message:
        while True:
            if self._body:
                return self._body.popleft()
            if self._failure is not None:
                self._failure_reported = True
                raise self._failure
            if self._ended:
                return {"type": "http.disconnect"}
            await self._park()

    def wrap_send(self, send: Send) -> Send:
        async def tracked_send(message: Message) -> None:
            # Before, not after: the pump can only wake once the server has
            # processed this message, and by then the flag must already be set.
            if self._completes_response(message):
                self._response_complete = True
            await send(message)

        return tracked_send

    async def _pump_channel(self) -> None:
        try:
            while True:
                message = await self._receive()
                if message["type"] == "http.disconnect":
                    self._latch_disconnect()
                    return
                self._body.append(message)
                self._wake_readers()
        except Exception as exc:  # noqa: BLE001
            self._failure = exc
            self._wake_readers()

    def _latch_disconnect(self) -> None:
        self._ended = True
        if not self._response_complete:
            self._event.set()
        self._wake_readers()

    async def _reap_pump(self) -> None:
        if self._pump is None or self._pump.done():
            return

        self._pump.cancel()
        with suppress(asyncio.CancelledError):
            await self._pump

    def _wake_readers(self) -> None:
        readers, self._readers = self._readers, []
        for reader in readers:
            if not reader.done():
                reader.set_result(None)

    async def _park(self) -> None:
        reader = asyncio.get_running_loop().create_future()
        self._readers.append(reader)
        await reader

    def _completes_response(self, message: Message) -> bool:
        kind = message["type"]
        if kind == "http.response.start":
            self._trailers_promised = bool(message.get("trailers", False))
            return False
        if kind == "http.response.trailers":
            return not message.get("more_trailers", False)
        if kind == "http.response.pathsend":
            return not self._trailers_promised
        return (
            kind == "http.response.body"
            and not message.get("more_body", False)
            and not self._trailers_promised
        )
