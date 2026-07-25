from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress

from starlette.requests import Request
from starlette.types import Scope

from aiofence import Fencing, bind_fencing, get_current_fencing

from .middleware import get_disconnect_event

_SCOPE_KEY = "aiofence.disconnect_watch"
_DEFAULT_CODE = "disconnect"

_logger = logging.getLogger(__name__)


async def disconnect_event(request: Request) -> AsyncGenerator[asyncio.Event]:
    """
    FastAPI dependency that yields an ``asyncio.Event`` set on client disconnect.

    Usage::

        @app.get("/search")
        async def handler(gone: asyncio.Event = Depends(disconnect_event)):
            if gone.is_set():
                return partial

    Install ``DisconnectMiddleware``: this dependency then borrows the event it
    published. Without it the dependency holds the ASGI receive channel itself,
    which makes raw body reads unsafe, puts it in competition with streaming
    responses for the same messages, and fires the event when the response
    completes — not only when the client leaves.
    See docs/disconnect-watcher-analysis.md.
    """
    async with _shared_disconnect_event(request) as event:
        yield event


async def disconnect_fencing(request: Request) -> AsyncGenerator[Fencing]:
    """
    FastAPI dependency that cancels the current Fencing when the client disconnects.

    Shares one receive loop per request with ``disconnect_event`` — registers
    that event on ``get_current_fencing()`` under ``code="disconnect"`` and
    binds the result as the active context. For a different code use
    ``disconnect_fencing_dependency(code=...)``.

    Usage::

        @app.get("/render")
        async def handler(fencing: Fencing = Depends(disconnect_fencing)):
            with fencing.move_on_cancel() as fence:
                frames = await render_scene()
            if fence.cancelled_by("disconnect"):
                ...

    Install ``DisconnectMiddleware``: this dependency then borrows the event it
    published. Without it the dependency holds the ASGI receive channel itself,
    which makes raw body reads unsafe, puts it in competition with streaming
    responses for the same messages, and fires the trigger when the response
    completes — not only when the client leaves.
    See docs/disconnect-watcher-analysis.md.
    """
    async with _bound_fencing(request, _DEFAULT_CODE) as fencing:
        yield fencing


def disconnect_fencing_dependency(
    *,
    code: str = _DEFAULT_CODE,
) -> Callable[[Request], AsyncGenerator[Fencing]]:
    """
    Build a ``disconnect_fencing`` dependency that uses a custom code.

    This is the only way to set a custom code. ``disconnect_fencing`` takes no
    ``code`` argument on purpose: FastAPI would expose it as a client-settable
    query parameter on every fenced route.

    Carries the same caveats as ``disconnect_fencing``. Layering two of these
    with different codes on one request is supported — they share the single
    per-request event, and every code is reported.

    Usage::

        ClientGone = Depends(disconnect_fencing_dependency(code="client_gone"))

        @app.get("/render")
        async def handler(fencing: Fencing = ClientGone):
            ...
    """

    async def dependency(request: Request) -> AsyncGenerator[Fencing]:
        async with _bound_fencing(request, code) as fencing:
            yield fencing

    return dependency


@asynccontextmanager
async def _bound_fencing(request: Request, code: str) -> AsyncIterator[Fencing]:
    async with _shared_disconnect_event(request) as event:
        fencing = get_current_fencing().event(event, code=code)
        with bind_fencing(fencing):
            yield fencing


@asynccontextmanager
async def _shared_disconnect_event(request: Request) -> AsyncIterator[asyncio.Event]:
    """
    Per-request disconnect event, cached in the ASGI scope.

    With ``DisconnectMiddleware`` installed the event is already published — the
    middleware owns the channel for the whole request, so we borrow its event
    and start nothing. Without it, whoever asks first opens the watch loop and
    stashes it; everyone after that gets the same event back. Keeping it to a
    single loop matters: parallel readers on one channel divide the messages
    between them, so neither is guaranteed to see the disconnect.

    The cache entry lives exactly as long as the watch behind it — see
    ``_DisconnectWatch``.
    """
    published = get_disconnect_event(request.scope)
    if published is not None:
        yield published
        return

    watch = _acquire_watch(request)
    try:
        yield watch.event
    finally:
        await _release_watch(request.scope, watch)


def _acquire_watch(request: Request) -> _DisconnectWatch:
    watch: _DisconnectWatch | None = request.scope.get(_SCOPE_KEY)
    if watch is None:
        watch = _DisconnectWatch(request)
        request.scope[_SCOPE_KEY] = watch

    watch.acquire()
    return watch


async def _release_watch(scope: Scope, watch: _DisconnectWatch) -> None:
    if not watch.release():
        return

    # Uncache before suspending, so anyone entering during the await below
    # starts a fresh watch instead of adopting the one being torn down.
    if scope.get(_SCOPE_KEY) is watch:
        del scope[_SCOPE_KEY]

    await watch.stop()


class _DisconnectWatch:
    """
    One receive loop per request, shared by every entrant.

    Lifetime is reference counted: the first entrant starts the listener, the
    last one out stops it and drops the scope entry. Without the count, a
    short-lived entrant tearing down under a long-lived one would leave the
    survivor holding an event that can never fire.
    """

    __slots__ = ("_users", "event", "listener")

    def __init__(self, request: Request) -> None:
        self.event = asyncio.Event()
        self._users = 0
        self.listener = asyncio.create_task(_listen_disconnect(request, self.event))

    def acquire(self) -> None:
        self._users += 1

    def release(self) -> bool:
        """
        Drop one user. True means the caller was the last one out.
        """
        self._users -= 1
        return self._users <= 0

    async def stop(self) -> None:
        self.listener.cancel()
        with suppress(asyncio.CancelledError):
            await self.listener


async def _listen_disconnect(request: Request, event: asyncio.Event) -> None:
    try:
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                event.set()
                return
    except Exception:
        # Nobody can act on this: the dependency has already yielded, and
        # re-raising at teardown lands after the response was sent. Log it and
        # stay down — the event can no longer fire for this request.
        _logger.exception("aiofence disconnect watcher stopped; disconnect will not be reported")
