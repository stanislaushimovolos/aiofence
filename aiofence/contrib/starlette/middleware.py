"""
ASGI middleware that owns the request's receive channel.

A receive channel has exactly one useful reader — ``receive()`` is a queue pop,
not a broadcast — and a request has several interested parties. This middleware
sits above all of them, reads the channel once, and replays what it read.

Install outermost. Rationale, install position and cost: docs/api.md; the three
load-bearing properties (replay, record-once, response-complete tracking):
docs/architecture.md.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from inspect import isawaitable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aiofence import bind_fencing, get_current_fencing
from aiofence.backends import CancelBackend, bind_backend
from aiofence.backends.anyio import AnyioBackend

from .api import DISCONNECT_CODE, DISCONNECT_EVENT_SCOPE_KEY, _publish

logger = logging.getLogger("aiofence.contrib.starlette")

# Called with the ASGI scope after a client left mid-request; sync or async.
type DisconnectCallback = Callable[[Scope], Awaitable[None] | None]

# Asked once per http request, before it runs: own this channel or stand aside?
type WatchPredicate = Callable[[Scope], bool]


class DisconnectMiddleware:
    """
    Own the receive channel for one request and publish its disconnect event.

    Args:
        app: Next ASGI application in the stack.
        backend: How fences below cancel their task, for the whole request —
            tasks it spawns included. Defaults to ``AnyioBackend``, so the
            shields anyio-based libraries (httpx, Starlette) wrap their cleanup
            in hold; pass ``NativeBackend()`` for asyncio's own ``task.cancel()``.
            Takes precedence over ``set_default_backend`` for the request;
            ``Fence(backend=...)`` still wins. Trade-offs: docs/architecture.md.
        fencing_code: Code the disconnect event is bound under, via
            ``get_current_fencing().event(event, code=fencing_code)``, for the
            whole request. Defaults to ``DISCONNECT_CODE``, which is what
            ``disconnect_fencing`` uses too — the two dedupe onto one entry.
        on_disconnect: Sync or async callback for logging and metrics, called
            with the ASGI scope once a request the client left has finished.
            It runs after routing, so the scope carries ``endpoint``,
            ``path_params`` and — under FastAPI — ``route``, whose ``path`` is
            the template rather than the request path. A completed response is
            never a disconnect, so it does not fire on the happy path. Raising
            from it is logged, not propagated.
        watch: Decides per request whether to own its channel. Asked only about
            http requests this instance would otherwise take, before the
            application runs — ownership cannot be retrofitted, so there is
            nothing to decide later. Declining costs a request its event, its
            fencing binding and everything read through them, so the default
            watches every request. Raising from it is propagated: nothing has
            been sent yet, and guessing an answer would be worse.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        backend: CancelBackend | None = None,
        fencing_code: str = DISCONNECT_CODE,
        on_disconnect: DisconnectCallback | None = None,
        watch: WatchPredicate | None = None,
    ) -> None:
        self.app = app
        self.backend: CancelBackend = backend if backend is not None else AnyioBackend()
        self.fencing_code = fencing_code
        self.on_disconnect = on_disconnect
        self.watch: WatchPredicate = watch or _watch_every_request

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Non-http scopes answer their own disconnect forever; a second install
        # would be a second reader. Both pass through — one reader, one event.
        # Neither is the predicate's call, so both are settled before it is asked.
        if scope["type"] != "http" or DISCONNECT_EVENT_SCOPE_KEY in scope or not self.watch(scope):
            await self.app(scope, receive, send)
            return

        channel = _RequestChannel(receive)
        channel.start()
        try:
            with _publish(scope, channel.disconnect_event):
                await self._call_app(scope, channel, send)
        finally:
            await channel.aclose()
            # Only a client that left sets the event, so it is the whole
            # condition — a completed response never reaches here set.
            if channel.disconnect_event.is_set():
                await self._notify(scope)

    async def _notify(self, scope: Scope) -> None:
        if self.on_disconnect is None:
            return

        try:
            outcome = self.on_disconnect(scope)
            if isawaitable(outcome):
                await outcome
        except Exception:
            # The response has already gone out; observability must not break it.
            logger.exception("aiofence on_disconnect callback failed")

    async def _call_app(self, scope: Scope, channel: _RequestChannel, send: Send) -> None:
        # Ambient, so a bare Fence below is disconnect-aware and exception
        # handlers still see it. Work that must outlive the client fences on a
        # fresh Fencing() instead.
        fencing = get_current_fencing().event(channel.disconnect_event, code=self.fencing_code)
        with bind_backend(self.backend), bind_fencing(fencing):
            await self.app(scope, channel.receive, channel.wrap_send(send))


def _watch_every_request(scope: Scope) -> bool:  # noqa: ARG001
    return True


class _RequestChannel:
    """
    Sole reader of one request's receive channel: ``start`` runs the read loop,
    ``receive`` is what the app below sees, ``wrap_send`` follows the response
    on its way out and catches a connection the server reports closed,
    ``aclose`` stops the loop and wakes any waiting consumer.
    """

    def __init__(self, receive: Receive) -> None:
        self._receive = receive
        self._disconnect_event = asyncio.Event()
        self._body_buffer: deque[Message] = deque()
        self._waiters: list[asyncio.Future[None]] = []
        self._reader: asyncio.Task[None] | None = None
        self._response = _ResponsePhase()

        self._channel_ended = False

        self._failure: BaseException | None = None
        self._failure_reported = False

    @property
    def disconnect_event(self) -> asyncio.Event:
        return self._disconnect_event

    def start(self) -> None:
        self._reader = asyncio.create_task(self._read_channel())

    async def receive(self) -> Message:
        while True:
            # Body in order first; the disconnect is a side channel, answered
            # on every later call so no reader below can starve.
            if self._body_buffer:
                return self._body_buffer.popleft()
            if self._failure is not None:
                self._failure_reported = True
                raise self._failure  # re-raised where the app actually reads

            if self._channel_ended:
                return {"type": "http.disconnect"}

            await self._wait_for_activity()

    async def _read_channel(self) -> None:
        try:
            while True:
                message = await self._receive()
                if message["type"] == "http.disconnect":
                    self._record_disconnect()
                    return  # uvicorn re-delivers it forever; reading on would spin

                self._body_buffer.append(message)
                self._wake_waiters()
        except Exception as exc:  # noqa: BLE001
            # A broken channel is not evidence the client left — no event.
            self._failure = exc
            self._wake_waiters()

    async def aclose(self) -> None:
        await self._stop_reader()
        self._channel_ended = True
        self._wake_waiters()

        if self._failure is not None and not self._failure_reported:
            # Log, don't raise: the response has usually already gone out.
            logger.warning("aiofence receive channel failed and nothing read it: %r", self._failure)

    def wrap_send(self, send: Send) -> Send:
        async def watched_send(message: Message) -> None:
            # Before, not after: the read loop can only wake once the server has
            # processed this message, and by then the flag must already be set.
            self._response.advance(message)
            try:
                await send(message)
            except OSError:
                self._record_send_failure()
                raise

        return watched_send

    def _record_disconnect(self) -> None:
        # After the response's last message every server sends http.disconnect;
        # that means "stream ended", not "client left". Only here can they be told apart.
        self._end_channel(client_left=not self._response.complete)

    def _record_send_failure(self) -> None:
        # ASGI spec 2.4: an OSError out of send is the server saying the connection
        # is closed, and it can arrive before the disconnect message reaches the read
        # loop. No phase check — nothing follows the response's terminal message, so a
        # send the server refused is a response the client never got.
        self._end_channel(client_left=True)

    def _end_channel(self, *, client_left: bool) -> None:
        self._channel_ended = True
        # Set before waking: this queues a fence's trigger callback ahead of the
        # parked reader's wakeup, so a fence around that read is cancelled rather
        # than handed the reply Starlette turns into ClientDisconnect. A fence
        # entered after this point gets the exception — its cancel is only
        # scheduled, and answering from state here needs no suspension.
        if client_left:
            self._disconnect_event.set()

        self._wake_waiters()

    async def _stop_reader(self) -> None:
        if self._reader is None or self._reader.done():
            return

        self._reader.cancel()
        with suppress(asyncio.CancelledError):
            await self._reader

    def _wake_waiters(self) -> None:
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def _wait_for_activity(self) -> None:
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        await waiter


class _ResponsePhase:
    """
    How far the response has got, advanced by the channel's wrapped ``send`` and
    read to answer one question: had the response already ended? A disconnect
    after that point means "stream ended"; before it, the client left.
    """

    def __init__(self) -> None:
        self._trailers_promised = False
        self._complete = False

    @property
    def complete(self) -> bool:
        return self._complete

    def advance(self, message: Message) -> None:
        if self._complete:
            return

        # Promised trailers move the end of the response past the final body
        # message — a client leaving in between has genuinely left.
        kind = message["type"]
        if kind == "http.response.start":
            self._trailers_promised = bool(message.get("trailers", False))
        elif kind == "http.response.trailers":
            self._complete = not message.get("more_trailers", False)
        elif kind == "http.response.pathsend":
            self._complete = not self._trailers_promised
        elif kind == "http.response.body":
            self._complete = not message.get("more_body", False) and not self._trailers_promised
