# Why Fence Must Suppress CancelledError

Fence calls `task.cancel()` to interrupt the body at any `await`. Once you call `cancel()`, you own that cancellation — you must either suppress the resulting `CancelledError` or leave the caller unable to inspect why it was cancelled.

This document traces exactly what breaks for each alternative, with references to CPython's `Task` implementation and the asyncio documentation.

## Background: The Counter Protocol

From [Python docs — Task.cancel()](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.cancel):

> This arranges for a `CancelledError` exception to be thrown into the wrapped coroutine on the next cycle of the event loop.

From [Python docs — Task.uncancel()](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.uncancel):

> Decrement the count of cancellation requests to this Task.

From [Python docs — Task.cancelling()](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.cancelling):

> Return the number of pending cancellation requests to this Task, i.e., the number of calls to `cancel()` less the number of calls to `uncancel()`.

Every `cancel()` increments the counter. Every `uncancel()` decrements it. Structured concurrency primitives (`TaskGroup`, `asyncio.timeout`) snapshot this counter on entry and compare on exit to determine ownership.

From [Python docs — TaskGroup](https://docs.python.org/3/library/asyncio-task.html#asyncio.TaskGroup):

> Task groups preserve the cancellation count reported by `asyncio.Task.cancelling()`.

## Background: Task.__step and CancelledError

When `CancelledError` propagates out of a coroutine, CPython's `Task.__step` **unconditionally** puts the task into CANCELLED state:

```python
# CPython 3.12+ Task.__step_run_and_handle_result (simplified):
except CancelledError as exc:
    self._cancelled_exc = exc
    super().cancel()  # task ALWAYS enters CANCELLED state
```

There is no `_must_cancel` branching for `CancelledError`. Regardless of how `cancel()` was called (synchronously, via `call_soon`, through `_fut_waiter`), if `CancelledError` escapes the coroutine, the task is CANCELLED.

The `_must_cancel` flag only affects the `StopIteration` path — when the coroutine returns normally while a cancel is pending:

```python
except StopIteration as exc:
    if self._must_cancel:
        self._must_cancel = False
        super().cancel(msg=self._cancel_message)  # cancel wins
    else:
        super().set_result(exc.value)              # normal return
```

This means `task.cancelled()` returns `True` whenever `CancelledError` propagates out. TaskGroup ignores cancelled tasks. **The TaskGroup interaction is not the problem. The problem is worker control.**

## Background: How asyncio.timeout Determines Ownership

From [Python docs — asyncio.timeout](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout):

> Return an asynchronous context manager that can be used to limit the amount of time spent waiting on something.

`asyncio.timeout` snapshots `task.cancelling()` on entry as its baseline. On exit:

```python
# CPython Timeout.__aexit__ (simplified):
if self._state is _EXPIRING:
    self._task.uncancel()
    if remaining <= self._cancelling and exc_type is CancelledError:
        raise TimeoutError from exc_val
```

The check `remaining <= baseline` answers: "after I uncancelled, is the counter back to where it was when I entered?" If yes — the only cancel was mine, convert to `TimeoutError`. If no — someone else also cancelled, back off.

---

## Option 1: Raise a CancelledError Subclass

Approach: `uncancel()`, then raise a custom `FenceCancelled(CancelledError)`.

### What breaks

```python
async def worker():
    with Fence(TimeoutTrigger(1)) as fence:
        await asyncio.sleep(10)
    # Fence fires → cancel() → counter=1 → CancelledError
    # Fence.__exit__: uncancel() → counter=0, raise FenceCancelled(CancelledError)

    if fence.cancelled:
        return "fallback"   # never reached

async def main():
    result = await worker()  # gets CancelledError instead of "fallback"
```

### Why this is wrong

The worker intended to inspect `fence.cancelled` and return a fallback. Instead, `FenceCancelled` escaped the `with` block — the post-block code never ran.

Additional problem: `except CancelledError` in user code catches `FenceCancelled` unexpectedly, since it's a subclass. Any intermediate code between the Fence and the caller may silently swallow it.

### What breaks additionally: TaskGroup `is`-check

When `FenceCancelled` propagates out of a **child task**, `task.cancelled()` is `True` (any `CancelledError` subclass → CANCELLED state), so `TaskGroup._on_task_done` ignores it. No breakage there.

But when `FenceCancelled` propagates through the **parent task's body** into `TaskGroup.__aexit__`, it breaks. `TaskGroup` uses identity checks, not `isinstance`:

```python
# CPython TaskGroup.__aexit__ (lines 76-77, 136):
propagate_cancellation_error = \
    exc if et is exceptions.CancelledError else None
# ...
if et is not None and et is not exceptions.CancelledError:
    self._errors.append(exc)
```

`et is CancelledError` → `False` for `FenceCancelled`. TaskGroup treats it as a regular exception → appends to errors → `BaseExceptionGroup([FenceCancelled])`.

```python
async def main():
    async with asyncio.TaskGroup() as tg:
        tg.create_task(important_work())
        with Fence(TimeoutTrigger(1)) as fence:
            await asyncio.sleep(10)
        # FenceCancelled propagates into TaskGroup.__aexit__
    # Raises BaseExceptionGroup([FenceCancelled])
```

The subclass gets the worst of both worlds: `TaskGroup.__aexit__` rejects it (identity check fails), but `except CancelledError` in user code still catches it (`isinstance` → `True`).

---

## Option 2: uncancel() + Propagate CancelledError

Approach: call `uncancel()` to balance the counter, but return `False` from `__exit__` (let `CancelledError` propagate).

### What breaks

```python
async def worker():
    with Fence(TimeoutTrigger(1)) as fence:
        await asyncio.sleep(10)
    # Fence fires → cancel() → counter=1 → CancelledError
    # Fence.__exit__: uncancel() → counter=0, returns False
    # CancelledError propagates out of worker()

    if fence.cancelled:
        return "fallback"    # never reached
```

### Why this is wrong

The worker's internal 1-second timeout was handled by Fence. The worker intended to fall back to a default value. Instead, `CancelledError` propagated, the fallback code never ran, and the caller sees a `CancelledError` from what should have been a handled timeout.

Counter is balanced (good), but the worker lost control.

### TaskGroup interaction

TaskGroup is **not** broken by this option. `CancelledError` escaping the coroutine → CANCELLED state → TaskGroup ignores it.

---

## Option 3: Don't uncancel() + Propagate CancelledError

Approach: don't call `uncancel()`, return `False` from `__exit__`. Counter stays inflated.

### What breaks: worker control (same as options 1 and 2)

```python
async def worker():
    with Fence(TimeoutTrigger(1)) as fence:
        await asyncio.sleep(10)
    # Fence fires → cancel() → counter=1 → CancelledError
    # Fence.__exit__: no uncancel, counter=1, returns False
    # CancelledError propagates — fallback never runs
```

### What breaks additionally: asyncio.timeout loses ownership

Even if the worker catches the error manually, the inflated counter breaks outer scopes:

```python
async def worker():
    try:
        with Fence(TimeoutTrigger(1)) as fence:
            await asyncio.sleep(10)
    except CancelledError:
        pass  # catch but don't uncancel — counter stays 1

    print(f"counter = {asyncio.current_task().cancelling()}")  # 1
    await asyncio.sleep(10)  # timeout should fire here

async def handler():
    try:
        async with asyncio.timeout(5):   # snapshots baseline=0
            await worker()
    except TimeoutError:
        print("timed out")     # NEVER PRINTED
```

### Step-by-step counter trace

```
t=0  asyncio.timeout enters    → baseline=0, counter=0
     Fence enters               → baseline=0, counter=0

t=1  Fence fires                → cancel() → counter=1
     CancelledError raised, worker catches it
     No uncancel                → counter still 1
     Worker continues, sleeps again

t=5  asyncio.timeout fires      → cancel() → counter=2
     CancelledError raised

     asyncio.timeout.__aexit__:
       state = EXPIRING (its timer fired)
       uncancel()                → counter=1
       remaining(1) > baseline(0)
       → "someone else also cancelled, back off"
       → does NOT convert to TimeoutError
       → CancelledError escapes

except TimeoutError:  ← NEVER REACHED
```

### Why this is wrong

`asyncio.timeout` fired at 5 seconds. It should convert to `TimeoutError`. But Fence's orphaned `cancel()` inflated the counter. When timeout checks `remaining <= baseline`, it sees `1 > 0` and concludes an outer scope also cancelled. It backs off and lets `CancelledError` propagate. The timeout silently stops working.

### Which asyncio contract is violated

The counter protocol requires every `cancel()` to be paired with an `uncancel()`. From [Python docs — Task.uncancel()](https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.uncancel):

> Should the coroutine nevertheless decide to suppress the cancellation, it needs to call `Task.uncancel()` in addition to catching the exception.

Fence's `cancel()` without `uncancel()` breaks the invariant that structured concurrency primitives rely on to determine ownership.

### TaskGroup interaction

TaskGroup is **not** broken by this option. Same as options 1 and 2 — `CancelledError` escaping → CANCELLED state → ignored.

---

## Why Suppression Is the Only Correct Exit

All three alternatives share the same root cause: `CancelledError` escapes the Fence (or if caught manually by the user, requires them to handle `uncancel()` themselves).

- **The worker loses control.** Code after the `with` block never runs. The entire point of Fence — inspect `fence.cancelled` and decide what to do — is defeated.

- **The caller sees an unexpected CancelledError.** A handled internal timeout leaks as a cancellation to whoever awaits the worker. The task enters CANCELLED state, and any code awaiting it gets `CancelledError`.

- **(Option 1 only) TaskGroup `is`-check breaks.** `TaskGroup.__aexit__` uses `et is CancelledError` (identity, not `isinstance`). A `CancelledError` subclass fails this check and is treated as a regular exception → `BaseExceptionGroup`.

- **(Option 3 only) Counter inflation breaks nesting.** `asyncio.timeout` and other Fences use `remaining <= baseline` to determine ownership. An unbalanced `cancel()` makes them think an outer scope cancelled, so they back off.

With suppression, none of this happens:

```python
async def worker():
    with Fence(TimeoutTrigger(1)) as fence:
        await asyncio.sleep(10)
    # Fence fires → cancel() → CancelledError
    # __exit__: uncancel() → counter=0, suppress → return True
    # No exception propagates. Counter balanced.

    if fence.cancelled:
        return "fallback"   # runs normally

async def main():
    async with asyncio.TaskGroup() as tg:
        tg.create_task(worker())        # exits cleanly after 1s
        tg.create_task(asyncio.sleep(60))  # keeps running
    # Works correctly
```

This is the same pattern `asyncio.timeout()` uses — it calls `cancel()`, catches `CancelledError`, calls `uncancel()`, and converts (to `TimeoutError`). Fence does the same but suppresses instead of converting, because there's no single exception type to convert to (Fence supports arbitrary triggers).

---

## Summary

| Alternative | Counter | Worker control | TaskGroup | asyncio.timeout | Caller impact |
|---|---|---|---|---|---|
| 1. CancelledError subclass | Balanced | Lost | Broken — `is`-check rejects subclass | OK | `except CancelledError` catches unexpectedly |
| 2. uncancel + propagate | Balanced | Lost | OK | OK | Unexpected CancelledError |
| 3. no uncancel + propagate | Inflated | Lost | OK | Broken — can't determine ownership | Unexpected CancelledError |
| **Suppress** | **Balanced** | **Preserved** | **OK** | **OK — counter clean** | **None — no exception escapes** |
