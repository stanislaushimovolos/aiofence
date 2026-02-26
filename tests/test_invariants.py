import asyncio

import pytest

from constellate import Fence, TimeoutTrigger


async def test__Fence__no_sources_async__protocol_intact():
    with Fence() as fence:
        await asyncio.sleep(0)

    assert not fence.cancelled


async def test__Fence__no_sources_sync__protocol_intact():
    with Fence() as fence:
        pass

    assert not fence.cancelled


async def test__Fence__zero_timeout__protocol_intact():
    with Fence(TimeoutTrigger(0)):
        pass


async def test__Fence__reenter__raises_runtime_error():
    fence = Fence()
    with fence:
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="cannot be reused"), fence:
        pass
