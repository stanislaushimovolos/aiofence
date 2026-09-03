import asyncio
from collections.abc import Iterator

import pytest

from aiofence import CancelBackend, NativeBackend, get_default_backend, set_default_backend
from aiofence.backends.anyio import AnyioBackend


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--uvloop", action="store_true", default=False, help="Run tests with uvloop")


@pytest.fixture(scope="session")
def event_loop_policy(request: pytest.FixtureRequest) -> asyncio.AbstractEventLoopPolicy:
    if request.config.getoption("--uvloop"):
        import uvloop

        return uvloop.EventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


BACKENDS: dict[str, type[CancelBackend]] = {"native": NativeBackend, "anyio": AnyioBackend}


@pytest.fixture(autouse=True, params=list(BACKENDS))
def _cancel_backend(request: pytest.FixtureRequest) -> Iterator[CancelBackend]:
    """
    Runs every test under both cancel backends. ``@pytest.mark.backend("native")``
    keeps a test on the backends it names.
    """
    only = request.node.get_closest_marker("backend")
    if only is not None and request.param not in only.args:
        pytest.skip(f"{request.param} backend")

    previous = get_default_backend()
    backend = BACKENDS[request.param]()
    set_default_backend(backend)
    yield backend
    set_default_backend(previous)


def _active_handles(loop: asyncio.AbstractEventLoop) -> int:
    scheduled = getattr(loop, "_scheduled", None)
    if scheduled is None:
        return -1
    return sum(1 for h in scheduled if not h.cancelled())


@pytest.fixture(autouse=True)
async def _check_asyncio_invariants() -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None

    cancelling_before = task.cancelling()
    handles_before = _active_handles(loop)
    tasks_before = len(asyncio.all_tasks())

    yield  # type: ignore[misc]

    assert task.cancelling() == cancelling_before, (
        f"cancelling() counter leaked: {cancelling_before} -> {task.cancelling()}"
    )
    if handles_before >= 0:
        assert _active_handles(loop) == handles_before, (
            f"scheduled handles leaked: {handles_before} -> {_active_handles(loop)}"
        )
    assert len(asyncio.all_tasks()) == tasks_before, (
        f"tasks leaked: {tasks_before} -> {len(asyncio.all_tasks())}"
    )
