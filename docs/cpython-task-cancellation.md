# CPython Task Cancellation Internals

Reference for how `asyncio.Task` cancellation works under the hood (CPython 3.12–3.14).

## Task state

```python
Task(Future):
    _coro: Coroutine                  # the wrapped coroutine
    _fut_waiter: Future | None        # what the task is currently awaiting
    _must_cancel: bool                # deferred flag: "throw CancelledError on next __step"
    _num_cancels_requested: int       # how many cancel() calls are outstanding
    _cancel_message: object           # msg= argument from the most recent cancel()
```

## Task lifecycle: `__step` and `__wakeup`

A task is driven by two internal methods that alternate via the event loop callback queue.

`__step` is where **all** synchronous code of the task executes. Everything between two `await`s runs inside a single `__step` call.

```python
async def worker():
    x = compute()              # ┐ first __step (coro.send)
    y = transform(x)           # ┘
    await asyncio.sleep(1)     #   yields Future → __step returns → task WAITING
                               #   ... event loop runs other callbacks ...
                               #   ... timer fires → __wakeup → new __step ...
    z = finalize(y)            # ┐ second __step
    print(z)                   # ┘
    await asyncio.sleep(1)     #   yields Future → __step returns
                               #   ... __wakeup → third __step ...
    print("done")              #   third __step → StopIteration → task done
```

### `__step(exc=None)`

Runs the coroutine until it yields a Future or finishes.

```python
def __step(self, exc=None):
    # 1. Check deferred cancellation
    if self._must_cancel:
        exc = CancelledError(self._cancel_message)
        self._must_cancel = False                      # consumed

    # 2. Clear waiter — task is now EXECUTING
    self._fut_waiter = None

    # 3. Run the coroutine
    if exc is None:
        result = coro.send(None)                       # resume normally
    else:
        result = coro.throw(exc)                       # throw exception at the await point
```

After running the coroutine, four outcomes:

**A. Coroutine yields a Future** — task wants to wait:

```python
    result.add_done_callback(self.__wakeup)            # "wake me when done"
    self._fut_waiter = result                          # task is now WAITING

    # Was cancel() called DURING our execution (self-cancel)?
    if self._must_cancel:
        self._fut_waiter.cancel(msg=self._cancel_message)
        self._must_cancel = False                      # propagated to future
```

**B. Coroutine raises `StopIteration`** — returned a value:

```python
    except StopIteration as exc:
        if self._must_cancel:
            super().cancel(msg=self._cancel_message)   # mark Task as cancelled
        else:
            super().set_result(exc.value)              # mark Task as done
```

**C. Coroutine raises `CancelledError`** — cancellation escaped uncaught:

```python
    except CancelledError as exc:
        self._cancelled_exc = exc
        super().cancel()                               # mark Task as cancelled (Future.cancel)
```

**D. Any other exception** — task failed:

```python
    except BaseException as exc:
        super().set_exception(exc)
```

### `__wakeup(future)`

Done callback registered on the future the task is awaiting. Translates future outcome into `__step` input:

```python
def __wakeup(self, future):
    try:
        future.result()                    # did the future succeed?
    except BaseException as exc:
        self.__step(exc)                   # failed → pass exception to __step
    else:
        self.__step()                      # succeeded → resume normally
```

No decision-making — just plumbing between the future and `__step`.

### Execution cycle

```
__step ──► run coroutine ──► yields Future ──► WAITING
                                                  │
                                    future completes / is cancelled
                                                  │
__step ◄── throw/send ◄── __wakeup ◄──────── done callback fires
```

## `_fut_waiter`: task states

```
 ┌─────────────┐   coroutine yields a Future   ┌──────────────┐
 │  EXECUTING  │ ────────────────────────────>  │   WAITING    │
 │             │                                │              │
 │ _fut_waiter │  <──────────────────────────── │ _fut_waiter  │
 │   = None    │   future completes             │   = <Future> │
 │             │   __wakeup → __step clears it  │              │
 └─────────────┘                                └──────────────┘
```

From within the task's own code, `_fut_waiter` is **always `None`**. It is set to `None` at the top of `__step` (before your code runs) and set to a Future at the bottom (after your code suspends). Only external code running on the event loop while the task is suspended observes it as set.

## `task.cancel()`: two paths

```python
def cancel(self, msg=None):
    self._num_cancels_requested += 1               # ALWAYS incremented

    if self._fut_waiter is not None:               # task is WAITING
        self._fut_waiter.cancel(msg=msg)           # cancel the future directly
        return True                                # _must_cancel NOT set

    self._must_cancel = True                       # task is EXECUTING — defer
    self._cancel_message = msg
    return True
```

| Task state | `_fut_waiter` | What `cancel()` does | `_must_cancel` set? |
|---|---|---|---|
| **Waiting** | `<Future>` | Cancels the future → done callback → `__wakeup` → `__step(CancelledError)` | No |
| **Executing** | `None` | Sets `_must_cancel = True` — handled on next `__step` | Yes |

`cancel()` does not cancel the task. It cancels the **future** the task is waiting on. The real cancellation — the `CancelledError` thrown into the coroutine — happens later through the normal `__wakeup` → `__step` path.

`_must_cancel` is a fallback for when there is no future to cancel. It is consumed by `__step` on the next execution.

### When is `_fut_waiter` `None`?

Only two realistic cases where `cancel()` hits the `_must_cancel` path:

1. **Self-cancellation** — the task calls `cancel()` on itself during its own execution (e.g., in a context manager `__enter__`).
2. **Pre-start** — the task is scheduled (`call_soon(__step)`) but hasn't run its first `__step` yet.

In normal async code, external cancellation always finds the task waiting on a future.

### Cancellation cascades through await chains

`Task` overrides `Future.cancel()`. When the `_fut_waiter` is itself a Task, cancellation propagates down:

```
task_a.cancel(sentinel)
  └── task_a._fut_waiter = task_b         (Task)
      task_b.cancel(sentinel)
        └── task_b._fut_waiter = task_c   (Task)
            task_c.cancel(sentinel)
              └── task_c._fut_waiter = TimerFuture   (plain Future)
                  TimerFuture.cancel(sentinel)
                    └── TimerFuture._state = CANCELLED   ← lands here
```

Then `CancelledError` bubbles **back up** through `__wakeup` → `__step` → `coro.throw` at each level.

## `Future.cancel()` vs `Task.cancel()`

```python
# Future.cancel — marks state, fires callbacks
def cancel(self, msg=None):
    self._state = _CANCELLED
    self._cancel_message = msg
    self.__schedule_callbacks()         # loop.call_soon for each done callback

# Task.cancel — does NOT mark state, propagates to awaited future instead
def cancel(self, msg=None):
    self._num_cancels_requested += 1
    if self._fut_waiter is not None:
        self._fut_waiter.cancel(msg=msg)
    else:
        self._must_cancel = True
    # does NOT set self._state = _CANCELLED
```

A Task only marks itself as `_CANCELLED` at the end, when the `CancelledError` escapes its coroutine uncaught (in `__step_run_and_handle_result`).

## Counter protocol: `cancel()` / `uncancel()` / `cancelling()`

The counter exists for **nested cancellation scopes**. It lets each scope determine whether a `CancelledError` belongs to it or to an outer scope.

```python
def cancel(self, msg=None):
    self._num_cancels_requested += 1       # always increment
    ...

def uncancel(self):
    if self._num_cancels_requested > 0:
        self._num_cancels_requested -= 1
        if self._num_cancels_requested == 0:
            self._must_cancel = False      # 3.13+ only
    return self._num_cancels_requested

def cancelling(self):
    return self._num_cancels_requested     # snapshot the counter
```

### How `asyncio.timeout` uses it

```python
# __enter__:
self._cancelling = task.cancelling()       # snapshot counter BEFORE my cancel()

# __exit__:
if task.uncancel() <= self._cancelling:
    # counter is back to what it was before me
    # → no OTHER scope called cancel() while I was active
    # → this CancelledError is MINE — convert to TimeoutError
    raise TimeoutError
else:
    # counter is higher than my snapshot even after my uncancel()
    # → someone ELSE also called cancel()
    # → re-raise CancelledError for them to handle
```

### Nested scopes example

```
counter = 0
                                            snapshot = 0
our scope: cancel()                         counter = 1
    inner timeout: cancel()                 counter = 2
    inner timeout __exit__: uncancel()      counter = 1
    external cancel()                       counter = 2   ← someone else!
our scope __exit__:
    uncancel()                              counter = 1
    1 > 0 (snapshot)                        → not just ours, re-raise CancelledError
```

vs the clean case:

```
counter = 0
                                            snapshot = 0
our scope: cancel()                         counter = 1
our scope __exit__:
    uncancel()                              counter = 0
    0 <= 0 (snapshot)                       → only ours, claim it → TimeoutError
```

### Suppressing `CancelledError` is legitimate

When the counter shows that no other scope has an outstanding cancellation request, the scope **owns** the `CancelledError` and is free to suppress it or convert it. From the task's perspective the books are balanced — the counter is back where it started.

This is the intended protocol. Both `asyncio.timeout` (converts to `TimeoutError`) and anyio's `CancelScope` in `move_on_after` mode (suppresses entirely) rely on it.

## The 3.12 `_must_cancel` leak

### `uncancel()` difference

| Python | `uncancel()` clears `_must_cancel` at 0? |
|--------|------------------------------------------|
| 3.12   | No                                       |
| 3.13+  | Yes                                      |

### The bug

When `task.cancel()` is called from within the task (self-cancellation during `__enter__`), `_fut_waiter` is `None`, so `_must_cancel = True`. If `__exit__` calls `uncancel()` before any `await`, the counter reaches 0 but on 3.12 `_must_cancel` stays `True`. The next `await` triggers a spurious `CancelledError`.

```python
async def worker():
    task = asyncio.current_task()
    task.cancel(msg=sentinel)        # _must_cancel = True (_fut_waiter is None)
    task.uncancel()                  # counter → 0, but _must_cancel STILL True on 3.12
    await asyncio.sleep(0)           # __step sees _must_cancel → spurious CancelledError
```

### How CPython and anyio avoid it

Both ensure `task.cancel()` is never called synchronously from within the task. They defer via `loop.call_soon()`, so the callback fires on the next event loop tick when the task is awaiting a future (`_fut_waiter` is set). The "waiting" path never sets `_must_cancel`.

**CPython `asyncio.timeout`**: even for past deadlines, defers via `loop.call_soon(self._on_timeout)`.

**anyio `CancelScope`**: `_deliver_cancellation` skips the current task (`task is not current`) and uses a `call_soon` retry loop.
