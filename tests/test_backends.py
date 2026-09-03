import asyncio
from typing import Any

import pytest

from aiofence import EventTrigger, Fence, TimeoutTrigger
from aiofence.backends import (
    CancelBackend,
    CancelHandle,
    NativeBackend,
    bind_backend,
    get_default_backend,
)
from aiofence.backends.anyio import AnyioBackend


class RecordingBackend(CancelBackend):
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._inner = NativeBackend()

    def enter(self, task: asyncio.Task[Any]) -> CancelHandle:
        self.calls.append(("enter", task))
        return RecordingHandle(self._inner.enter(task), self.calls)


class RecordingHandle(CancelHandle):
    def __init__(self, inner: CancelHandle, calls: list[tuple[str, Any]]) -> None:
        self._inner = inner
        self._calls = calls

    def cancel(self, message: str) -> None:
        self._calls.append(("cancel", message))
        self._inner.cancel(message)

    def exit(self, exc_type: type[BaseException] | None, exc_val: BaseException | None) -> bool:
        result = self._inner.exit(exc_type, exc_val)
        self._calls.append(("exit", result))
        return result


class SuppressingHandle(CancelHandle):
    def cancel(self, message: str) -> None:
        pass

    def exit(self, *_args: object) -> bool:
        return True


class SuppressingBackend(CancelBackend):
    def enter(self, *_args: object) -> CancelHandle:
        return SuppressingHandle()


@pytest.fixture
def backend() -> RecordingBackend:
    return RecordingBackend()


@pytest.fixture
def set_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


async def test__fence__when_entered__then_backend_enters_current_task(backend):
    with Fence(backend=backend):
        pass

    assert backend.calls[0] == ("enter", asyncio.current_task())


async def test__fence__when_nothing_fires__then_handle_exit_still_called(backend):
    with Fence(backend=backend) as fence:
        await asyncio.sleep(0)

    assert backend.calls[-1] == ("exit", False)
    assert not fence.suppressed


async def test__fence__when_trigger_fires__then_cancels_through_handle_with_reason(backend):
    with Fence(TimeoutTrigger(0.01), backend=backend) as fence:
        await asyncio.sleep(10)

    assert ("cancel", "timed out after 0.01s") in backend.calls
    assert backend.calls[-1] == ("exit", True)
    assert fence.suppressed


async def test__fence__when_pre_triggered__then_cancels_through_handle_from_enter(
    backend, set_event
):
    with Fence(EventTrigger(set_event), backend=backend) as fence:
        assert backend.calls[-1][0] == "cancel"
        await asyncio.sleep(10)

    assert fence.suppressed


async def test__fence__when_two_triggers_fire__then_handle_cancelled_once(backend):
    event1 = asyncio.Event()
    event2 = asyncio.Event()

    with Fence(EventTrigger(event1), EventTrigger(event2), backend=backend) as fence:
        event1.set()
        event2.set()
        await asyncio.sleep(1)

    cancels = [c for c in backend.calls if c[0] == "cancel"]
    assert len(cancels) == 1
    assert len(fence.cancel_reasons) == 2


async def test__fence__when_declined__then_handle_never_cancelled(backend, set_event):
    with Fence(EventTrigger(set_event), policy=lambda _: False, backend=backend) as fence:
        await asyncio.sleep(0)

    assert [c[0] for c in backend.calls] == ["enter", "exit"]
    assert not fence.cancelled


async def test__fence__suppressed__then_reflects_handle_exit_result():
    with Fence(backend=SuppressingBackend()) as fence:
        raise asyncio.CancelledError

    assert fence.suppressed


async def test__fence__when_no_backend_given__then_default_backend_is_used():
    fence = Fence()

    assert fence._backend is get_default_backend()


async def test__native_handle__when_cancelled_from_inside_task__then_delivered_at_next_await():
    task = asyncio.current_task()
    assert task is not None
    handle = NativeBackend().enter(task)

    handle.cancel("from inside")
    with pytest.raises(asyncio.CancelledError, match="from inside"):
        await asyncio.sleep(10)

    assert handle.exit(asyncio.CancelledError, asyncio.CancelledError("from inside"))
    assert task.cancelling() == 0


async def test__native_handle__when_body_finishes_before_delivery__then_cancel_rescinded():
    task = asyncio.current_task()
    assert task is not None
    handle = NativeBackend().enter(task)

    handle.cancel("too late")
    suppressed = handle.exit(None, None)
    await asyncio.sleep(0)

    assert not suppressed
    assert task.cancelling() == 0


async def test__native_handle__when_outer_cancel_also_pending__then_does_not_suppress():
    task = asyncio.current_task()
    assert task is not None
    handle = NativeBackend().enter(task)
    loop = asyncio.get_running_loop()

    loop.call_soon(handle.cancel, "mine")
    loop.call_soon(task.cancel, "outer")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.sleep(10)
    suppressed = handle.exit(asyncio.CancelledError, asyncio.CancelledError())

    assert not suppressed
    assert task.uncancel() == 0


async def test__anyio_backend__when_entered_for_another_task__then_raises():
    other = asyncio.create_task(asyncio.sleep(0))

    try:
        with pytest.raises(RuntimeError, match="task it cancels"):
            AnyioBackend().enter(other)
    finally:
        await other


@pytest.mark.backend("anyio")
async def test__anyio_handle__when_foreign_cancel_outstanding__then_propagates():
    task = asyncio.current_task()
    assert task is not None
    event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def cut_twice() -> None:
        event.set()  # fence's cancel is delivered next tick, with anyio's message
        loop.call_soon(task.cancel, "outer")  # same tick, right after it: counter goes to 2

    loop.call_soon(cut_twice)
    with pytest.raises(asyncio.CancelledError):
        with Fence(EventTrigger(event), backend=AnyioBackend()) as fence:
            await asyncio.sleep(10)

    assert fence.cancelled
    assert not fence.suppressed
    assert task.uncancel() == 0  # only the outer cancel was left on the counter


async def test__bind_backend__when_active__then_fence_without_backend_uses_it():
    bound = NativeBackend()

    with bind_backend(bound):
        fence = Fence()

    assert fence._backend is bound


async def test__bind_backend__when_exited__then_process_default_restored():
    before = get_default_backend()

    with bind_backend(NativeBackend()):
        pass

    assert Fence()._backend is before


async def test__bind_backend__when_task_spawned_inside__then_task_inherits_it():
    bound = NativeBackend()

    async def build() -> CancelBackend:
        return Fence()._backend

    with bind_backend(bound):
        task = asyncio.create_task(build())

    assert await task is bound


async def test__fence__when_explicit_backend_given_under_bind__then_explicit_wins():
    explicit = NativeBackend()

    with bind_backend(NativeBackend()):
        fence = Fence(backend=explicit)

    assert fence._backend is explicit
