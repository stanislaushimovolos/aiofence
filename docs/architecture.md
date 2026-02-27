# Architecture

## Module Layout

- **`core.py`** — abstractions and core runtime: `CancelReason`, `CancelType`, `Trigger`, `TriggerHandle`, `Fence`, `_CancelToken`
- **`triggers/`** — built-in trigger implementations: `TimeoutTrigger`/`TimeoutHandle`, `EventTrigger`/`EventHandle`
- **`__init__.py`** — public re-exports

## Core Concepts

- **`Trigger`** — abstract cancellation condition. `check()` for synchronous pre-check, `arm(callback)` for async monitoring. Returns a `TriggerHandle`.
- **`TriggerHandle`** — live watch returned by `Trigger.arm()`. `disarm()` stops monitoring.
- **`Fence`** — sync context manager that arms triggers against the current task. Suppresses `CancelledError` on exit. Caller inspects `fence.cancelled` / `fence.reasons` after the block.
- **`_CancelToken`** — internal. Encapsulates one `cancel()`/`uncancel()` cycle. Tracks whether the deferred cancel fired and settles ownership in `__exit__`.
- **`CancelReason`** — frozen dataclass with `message` and `cancel_type` (TIMEOUT or EVENT).

## Cancellation Flow

1. `Fence.__enter__` snapshots `task.cancelling()` as the baseline counter
2. Runs `check()` on all triggers — if any pre-triggered, records reasons and schedules `task.cancel()` via `call_soon`
3. If no pre-triggers, arms all triggers; when one fires, callback records the reason and schedules `task.cancel()` via `call_soon`
4. Body runs. At the next `await`, `CancelledError` is raised inside the body
5. `Fence.__exit__` disarms all triggers, then calls `_CancelToken.resolve()`:
   - If cancel never fired (sync body completed first) — rescinds pending `call_soon`, returns `False`
   - If cancel fired and counter returned to baseline — `uncancel()` + suppress (`return True`)
   - If counter above baseline — outer scope also cancelled, don't suppress (`return False`)

## Cancellation Ownership

Uses asyncio's `cancel()`/`uncancel()` counter protocol. Each `Fence` snapshots `task.cancelling()` on entry as its baseline. On exit, `uncancel()` decrements the counter. If `remaining <= baseline` and the exception is `CancelledError`, this Fence owns it and suppresses. If `remaining > baseline`, an outer scope also called `cancel()` — defer to them.

## Suppression Semantics

Fence **always suppresses** `CancelledError`. No exceptions propagate from `__exit__`. This follows anyio's `CancelScope` model.

### Why suppress instead of raising

Three alternatives were considered and rejected:

1. **Raise a CancelledError subclass** (e.g. `FenceCancelled(CancelledError)`) — `task.cancelled()` returns `True` because the exception is a `CancelledError`, but the counter is 0. `TaskGroup` silently swallows it. `except CancelledError` catches it unexpectedly.

2. **`uncancel()` + propagate CancelledError** — produces `CancelledError` with counter at 0. Same inconsistent state as option 1. `asyncio.timeout` avoids this by converting to `TimeoutError` instead — but we don't want to force callers to catch a specific exception.

3. **Don't `uncancel()` + propagate** — counter stays inflated. Outer scopes (e.g. `asyncio.timeout`) can't determine ownership because Fence's unclaimed cancel inflated their view of the counter. Breaks nesting.

Suppression is the only approach that's both correct and composable with `TaskGroup`, `asyncio.timeout`, and nested Fences.

### Pre-triggered behavior

Python sync context managers cannot skip the body without raising from `__enter__`. If `__enter__` raises, `__exit__` is never called, so counter cleanup can't happen.

Instead, pre-triggered Fences schedule `task.cancel()` via `call_soon` and let the body start. The body is interrupted at the first `await`. If the body has no awaits and completes synchronously, the pending cancel is rescinded — `fence.cancelled` is still `True` (reasons were recorded on entry), but no `CancelledError` is ever delivered.

### TaskGroup compatibility

- **Fence inside TaskGroup**: suppresses, counter balanced, TaskGroup never sees `CancelledError`
- **TaskGroup cancels while Fence is active**: Fence's trigger didn't fire (`_cancel_token is None`), so `__exit__` returns `False` — `CancelledError` propagates to TaskGroup correctly
- **Both fire simultaneously**: counter protocol resolves ownership — Fence sees `remaining > baseline`, backs off, TaskGroup claims it

## Deferred Cancel via `call_soon`

`_CancelToken` never calls `task.cancel()` synchronously from within the task's own execution. Instead it schedules via `loop.call_soon()`. This avoids setting asyncio's internal `_must_cancel` flag during synchronous code, which would force `CancelledError` at the next `await` regardless of whether `uncancel()` was called.

## Event Watching Without Tasks

`EventTrigger` subscribes a `Future` directly to `asyncio.Event._waiters` instead of spawning a background task. The future's done callback fires the cancellation. Uses private API but mirrors what `Event.wait()` does internally.

## Design Decisions

- **Sync context manager**: `__enter__`/`__exit__` (not async). Event loop interaction happens via callbacks and `call_soon`, not awaits.
- **Single mode**: No split between "raise" and "suppress" modes. Fence always suppresses. If the caller wants to raise, they do it themselves after checking `fence.cancelled`.
- **No custom exception types**: No `FenceTimeout` or `FenceCancelled`. Keeps the API surface minimal and avoids `CancelledError` subclass pitfalls.
- **No scope tree / shielding**: Nesting and shielding handled by asyncio itself (`asyncio.shield()`, `uncancel()` counting).
- **Deadlines vs timeouts**: Core library works with relative timeouts (`TimeoutTrigger`). Deadlines (absolute time) are an application-layer concern — middleware converts remaining budget to `TimeoutTrigger(remaining)`.

## Cross-Service Deadline Propagation

```
Client (timeout=30s)
  -> Gateway: X-Request-Timeout: 30
    -> spent 2s on auth/routing
    -> Provider call: X-Request-Timeout: 28
```

Wire protocol is always relative duration. Each service converts to local timeout:
- Incoming: `header_seconds` -> `TimeoutTrigger(header_seconds)`
- Outgoing: `fence.remaining` -> header
