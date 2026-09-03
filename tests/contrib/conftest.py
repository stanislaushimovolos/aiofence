import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from functools import partialmethod

import pytest

from aiofence.backends import CancelBackend
from aiofence.contrib.starlette import DisconnectMiddleware


@pytest.fixture(autouse=True)
def _middleware_backend(monkeypatch: pytest.MonkeyPatch, _cancel_backend: CancelBackend) -> None:
    """
    ``DisconnectMiddleware`` defaults to anyio whatever the process default is,
    which would pin every contrib test to one backend. Route its default through
    the parametrised one instead; an explicit ``backend=`` still wins.
    """
    init = partialmethod(DisconnectMiddleware.__init__, backend=_cancel_backend)
    monkeypatch.setattr(DisconnectMiddleware, "__init__", init)


@pytest.fixture
async def drop_sse_shutdown_watcher() -> AsyncIterator[None]:
    """
    sse-starlette starts one shutdown watcher per thread that outlives the
    request by design. Cancel it so the suite-wide asyncio invariant check stays
    per-test; the watcher's own finally clears its started flag.

    Opt in with ``pytestmark = pytest.mark.usefixtures(...)`` — autouse would
    reap genuinely leaked tasks in every other contrib module too.
    """
    before = asyncio.all_tasks()

    yield

    for task in asyncio.all_tasks() - before:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
