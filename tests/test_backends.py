import asyncio
from typing import Any

import pytest

from aiofence import EXTERNAL_CODE, EventTrigger, Fence, TimeoutTrigger
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

    def enter_nested(self, task: asyncio.Task[Any]) -> CancelHandle:
        return self._inner.enter_nested(task)


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

    def enter_nested(self, *_args: object) -> CancelHandle:
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
async def test__anyio_handle__when_foreign_cancel_races_own__then_suppresses_and_absorbs_foreign():
    task = asyncio.current_task()
    assert task is not None
    event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def cut_twice() -> None:
        event.set()  # fence's cancel is delivered next tick, with anyio's message
        loop.call_soon(task.cancel, "outer")  # same tick, right after it: counter goes to 2

    loop.call_soon(cut_twice)
    with Fence(EventTrigger(event), backend=AnyioBackend()) as fence:
        await asyncio.sleep(10)

    assert fence.suppressed
    assert not fence.cancelled_by(EXTERNAL_CODE)
    await asyncio.sleep(0)  # nothing is delivered for the outer cancel
    assert task.uncancel() == 0  # only its count is left on the task


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


# --- Nesting ---


class NestingRecordingBackend(RecordingBackend):
    def enter_nested(self, task: asyncio.Task[Any]) -> CancelHandle:
        self.calls.append(("enter_nested", task))
        return RecordingHandle(self._inner.enter(task), self.calls)


@pytest.fixture(scope="session")
def untouched_default_backend() -> CancelBackend:
    return get_default_backend()


def test__default_backend__when_untouched__then_anyio(
    untouched_default_backend: CancelBackend,
) -> None:
    assert isinstance(untouched_default_backend, AnyioBackend)


def test__cancel_backend__when_enter_nested_not_implemented__then_cannot_instantiate() -> None:
    class EnterOnlyBackend(CancelBackend):
        def enter(self, *_args: object) -> CancelHandle:
            return SuppressingHandle()

    with pytest.raises(TypeError, match="enter_nested"):
        EnterOnlyBackend()  # type: ignore[abstract]


async def test__native_backend__when_nested__then_raises_naming_backend() -> None:
    class SubclassedNative(NativeBackend):
        pass

    with Fence(backend=SubclassedNative()):
        with pytest.raises(RuntimeError, match="SubclassedNative does not support nested Fences"):
            with Fence(backend=SubclassedNative()):
                pass


async def test__fence__when_nested__then_inner_backend_enters_nested(
    backend: RecordingBackend,
) -> None:
    nesting = NestingRecordingBackend()

    with Fence(backend=backend):
        with Fence(backend=nesting):
            await asyncio.sleep(0)

    assert nesting.calls[0] == ("enter_nested", asyncio.current_task())


async def test__fence__when_nested_entry_refused__then_outer_stack_unchanged() -> None:
    with Fence(TimeoutTrigger(0), backend=AnyioBackend()) as outer:
        with (
            pytest.raises(RuntimeError, match="does not support nested"),
            Fence(backend=NativeBackend()),
        ):
            pass
        await asyncio.sleep(10)

    assert outer.suppressed


async def test__fence__when_native_outer_and_anyio_inner__then_inner_nests() -> None:
    with Fence(TimeoutTrigger(10), backend=NativeBackend()) as outer:
        with Fence(TimeoutTrigger(0), backend=AnyioBackend()) as inner:
            await asyncio.sleep(10)
        await asyncio.sleep(0)

    assert inner.suppressed
    assert not outer.cancelled


async def test__fence__when_native_outer_and_anyio_inner_share_trigger__then_outer_suppresses() -> (
    None
):
    shutdown = asyncio.Event()

    with Fence(EventTrigger(shutdown), backend=NativeBackend()) as outer:
        with Fence(EventTrigger(shutdown), backend=AnyioBackend()) as inner:
            shutdown.set()
            await asyncio.sleep(10)

    assert inner.cancelled

    assert not inner.suppressed
    assert outer.suppressed
