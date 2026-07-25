# Architecture

## Module Layout

- **`core.py`** — abstractions and core runtime: `CancelReason`, `CancelType`, `Trigger`, `TriggerHandle`, `Fence`, `_CancelToken`
- **`triggers/`** — built-in trigger implementations: `TimeoutTrigger`/`TimeoutHandle`, `EventTrigger`/`EventHandle`
- **`contrib/`** — optional framework integrations (Starlette / FastAPI). Never imported by the core package, so it stays dependency-free; see [api.md](api.md)
  - **`contrib/starlette/`** — the ASGI side, split by direction: one module writes, the other reads. Import from the package; the split is internal
    - **`starlette/middleware.py`** — `DisconnectMiddleware`, the single reader of a request's ASGI receive channel: `_RequestChannel` reads the channel in one task, replays body messages, records the disconnect, and owns the event the middleware publishes; `_ResponsePhase` follows the send direction and answers whether the response had already ended. It also binds the event on the ambient `Fencing` under `DISCONNECT_CODE`, so anything below it that fences via `get_current_fencing()` is disconnect-aware with no wiring. Not a mode — installing the middleware is the opt-in; work that must outlive the client fences on a fresh `Fencing()` instead
    - **`starlette/api.py`** — what was published and how to read it back: `DISCONNECT_EVENT_SCOPE_KEY` (`"aiofence.disconnect_event"`), `DISCONNECT_CODE`, the `ContextVar`, and `get_disconnect_event` / `require_disconnect_event`, which answer with or without a scope. `_publish` is the one place that sets and drops both copies
  - **`contrib/fastapi.py`** — `disconnect_event` / `disconnect_fencing` / `disconnect_fencing_dependency` and the `DisconnectEvent` / `DisconnectFencing` aliases. Pure readers of what the middleware published; they raise `RuntimeError` when it isn't installed
- **`__init__.py`** — public re-exports

## API

For usage guide, examples, and custom trigger documentation see [api.md](api.md).

## Core Concepts

- **`Trigger`** — abstract cancellation condition. `check()` for synchronous pre-check, `arm(callback)` for async monitoring. Returns a `TriggerHandle`.
- **`TriggerHandle`** — live watch returned by `Trigger.arm()`. `disarm()` stops monitoring.
- **`Fence`** — sync context manager that arms triggers against the current task. Suppresses `CancelledError` on exit. Caller inspects `fence.suppressed` / `fence.cancel_reasons` after the block.
- **`_CancelToken`** — internal. Encapsulates one `cancel()`/`uncancel()` cycle. Tracks whether the deferred cancel fired and settles ownership in `__exit__`.
- **`CancelReason`** — frozen dataclass with `message` and `cancel_type` (TIMEOUT or EVENT).

## Cancellation Flow

0. `Fence.__enter__` requires a running task — `task.cancel()` is the only mechanism there is. Entered from a loop callback or from a worker thread (a sync FastAPI `def` handler), it raises `RuntimeError`
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

Three alternatives were considered and rejected. All lose worker control (code after the `with` block never runs), and each has a unique breakage:

1. **Raise a CancelledError subclass** — breaks TaskGroup. `TaskGroup.__aexit__` uses `et is CancelledError` (identity check, not `isinstance`), so a subclass is treated as a regular exception. This is by design — [subclassing CancelledError is not officially supported](https://discuss.python.org/t/subclassing-cancellederror/92285):

   ```python
   async with asyncio.TaskGroup() as tg:
       tg.create_task(important_work())
       with Fence(TimeoutTrigger(1)) as fence:
           await asyncio.sleep(10)
       # FenceCancelled propagates → BaseExceptionGroup([FenceCancelled])
   ```

2. **`uncancel()` + propagate CancelledError** — no protocol breakage, but a handled internal timeout leaks as `CancelledError` to the caller. The user can't distinguish Fence's cancel from external cancellation without manually tracking the counter.

3. **Don't `uncancel()` + propagate** — breaks `asyncio.timeout`. The inflated counter makes outer scopes think an additional cancel is in flight:

   ```python
   async with asyncio.timeout(5):    # baseline=0
       with Fence(TimeoutTrigger(1)) as fence:
           await asyncio.sleep(10)
       # Fence: cancel() → counter=1, no uncancel
       # timeout fires → cancel() → counter=2, uncancel() → 1
       # remaining(1) > baseline(0) → "not my cancel" → no TimeoutError
   ```

Suppression is the only approach that preserves worker control and is composable with `TaskGroup`, `asyncio.timeout`, and nested Fences.

### Pre-triggered behavior

Python sync context managers cannot skip the body without raising from `__enter__`. If `__enter__` raises, `__exit__` is never called, so counter cleanup can't happen.

Instead, pre-triggered Fences schedule `task.cancel()` via `call_soon` and let the body start. The body is interrupted at the first `await`. If the body has no awaits and completes synchronously, the pending cancel is rescinded — `fence.cancelled` is still `True` (reasons were recorded on entry, so `fence.cancelled` reflects that a trigger fired), but no `CancelledError` is ever delivered.

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
- **Single mode**: No split between "raise" and "suppress" modes. Fence always suppresses. If the caller wants to raise, they do it themselves after checking `fence.cancelled` / `fence.suppressed`.
- **No custom exception types**: No `FenceTimeout` or `FenceCancelled`. Keeps the API surface minimal and avoids `CancelledError` subclass pitfalls.
- **No scope tree / shielding**: Nesting and shielding handled by asyncio itself (`asyncio.shield()`, `uncancel()` counting).
- **Deadlines vs timeouts**: Core library works with relative timeouts (`TimeoutTrigger`). Deadlines (absolute time) are an application-layer concern — middleware converts remaining budget to `TimeoutTrigger(remaining)`.


Wire protocol is always relative duration. Each service converts to local timeout:
- Incoming: `header_seconds` -> `TimeoutTrigger(header_seconds)`
- Outgoing: `fence.remaining` -> header

## Disconnect Delivery: Replay and Record

`DisconnectMiddleware` exists because an ASGI receive channel has exactly one useful reader — `receive()` is a queue pop, not a broadcast — while a request routinely has several interested parties: `StreamingResponse`'s `listen_for_disconnect`, sse-starlette's listener, `Request.is_disconnected()`, and anything wanting to know the client left. Whoever reads first consumes the message; on hypercorn, daphne and granian it is delivered exactly once, so everyone else starves. A dependency cannot arbitrate: Starlette captures the raw `receive` before any dependency runs, so there is no reference left to wrap. Only a middleware sits above all of them.

Three properties make the arbitration correct, and each is load-bearing:

- **Replay, don't discard.** The read loop forwards every `http.request` message downstream in order and unchanged. The alternative — the dependency's watcher, which discards anything that isn't a disconnect — steals body chunks, and the loser of that race gets `{"body": b"", "more_body": False}`, which Starlette accepts as a *complete, empty* body. Silent truncation, no exception, no log.
- **Record, don't queue.** The disconnect is a terminal side channel: recorded once and answered on every later `receive()`, rather than queued as a message of its own that the first reader would consume. Buffered body messages are still handed over first; the event itself is set the moment the disconnect arrives. That turns a one-shot server delivery into a signal every reader below can observe, and it keeps the middleware from needing a bounded queue of its own. Draining the server's queue also matters in itself — hypercorn bounds its app queue at 10 messages and blocks the connection when it fills.
- **Track response completion in a wrapped `send`.** Per the ASGI spec `http.disconnect` means "the stream ended", not "the client left": every server sends it once the response is complete. The middleware flips its flag *before* handing the terminal message to the server's `send`, because the read loop can only be woken by the server having processed that message. A disconnect recorded after that point is replayed downstream but does **not** set the event — which is what keeps `BackgroundTasks` and post-response work from being cancelled on every successful request. This is the only place in the stack where the two meanings can be told apart.

"Terminal message" is not always the final body one. When `http.response.start` declares `"trailers": True`, the response is not complete until the last `http.response.trailers` message, and a client that leaves in between has genuinely left. The wrapped `send` tracks the declaration so the flag flips at the real end of the response rather than one message early.

The read loop stops as soon as it records a disconnect: uvicorn re-delivers the message immediately and forever, so reading past it would spin for the rest of the request, background-task phase included.

Replay settles that both readers are *told*, not who acts first. A fenced streaming body can therefore outlive its rival listener's cancel scope — `move_on_cancel()` suppressed the cancellation deliberately, so the generator resumes and emits its last chunk.

## Why This Complexity Is Necessary

Fence is a generalized `asyncio.timeout()`. The stdlib timeout does the same cancel/uncancel/suppress dance — but only for one trigger type and converts to `TimeoutError`. Fence supports arbitrary triggers and suppresses instead of converting.

Every piece exists because asyncio's counter protocol demands it:

- **Counter snapshot** — needed to distinguish own cancel from outer cancel. `asyncio.timeout()` does the same.
- **`call_soon` deferral** — calling `cancel()` synchronously sets `_must_cancel`, which `uncancel()` couldn't clear until 3.13. Deferring via `call_soon` ensures `cancel()` finds `_fut_waiter` set and never touches the flag.
- **`_CancelToken`** — tracks "scheduled but not delivered" vs "delivered". Without this, a sync body completing before `call_soon` fires would leave a stale cancel in flight.
- **Suppression** — the only correct exit strategy. The alternatives all cause the worker to lose control (post-block code never runs), and option 3 additionally breaks `asyncio.timeout` via counter inflation.

There is no simpler way to implement this within asyncio's cancellation model. Cooperative flags (check-in-a-loop) would work but lose the ability to interrupt arbitrary `await` points. Not calling `task.cancel()` means not solving the problem.
