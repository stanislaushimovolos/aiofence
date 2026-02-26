import asyncio

import pytest


def _active_handles(loop: asyncio.AbstractEventLoop) -> int:
    return sum(1 for h in loop._scheduled if not h.cancelled())


@pytest.fixture(autouse=True)
async def _check_asyncio_invariants():
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None

    cancelling_before = task.cancelling()
    handles_before = _active_handles(loop)
    tasks_before = len(asyncio.all_tasks())

    yield

    assert task.cancelling() == cancelling_before, (
        f"cancelling() counter leaked: {cancelling_before} -> {task.cancelling()}"
    )
    assert _active_handles(loop) == handles_before, (
        f"scheduled handles leaked: {handles_before} -> {_active_handles(loop)}"
    )
    assert len(asyncio.all_tasks()) == tasks_before, (
        f"tasks leaked: {tasks_before} -> {len(asyncio.all_tasks())}"
    )
