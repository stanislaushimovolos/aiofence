import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress

import pytest


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
