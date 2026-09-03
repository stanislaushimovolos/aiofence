# Architecture

## Module Layout

- **`core.py`** — abstractions and core runtime: `CancelReason`, `CancelType`, `CancelPolicy`, `Trigger`, `TriggerHandle`, `Fence`
- **`backends/`** — how a fence cancels its task, behind the `CancelBackend` / `CancelHandle` ABCs. `NativeBackend` (default) is asyncio's own `cancel()`/`uncancel()` protocol; `backends/anyio.py` holds `AnyioBackend`, an `anyio.CancelScope` per fence, importable only with the `aiofence[anyio]` extra. `set_default_backend()` selects one process-wide, `Fence(backend=...)` per fence; triggers, policy and reasons are unaffected by the choice. See [Cancel Backends](#cancel-backends)
- **`triggers/`** — built-in trigger implementations: `TimeoutTrigger`/`TimeoutHandle`, `EventTrigger`/`EventHandle`
- **`contrib/`** — optional framework integrations (Starlette / FastAPI). Never imported by the core package, so it stays dependency-free; see [api.md](api.md)
  - **`contrib/starlette/`** — the ASGI side, split by direction: one module writes, the other reads. Import from the package; the split is internal
    - **`starlette/middleware.py`** — `DisconnectMiddleware`, the single reader of a request's ASGI receive channel: `_RequestChannel` reads the channel in one task, replays body messages, records the disconnect — from the receive side or from a `send` the server refuses — and owns the event the middleware publishes; `_ResponsePhase` tracks how far the response has got and answers whether it had already ended. It also binds the event on the ambient `Fencing` under `DISCONNECT_CODE`, so anything below it that fences via `get_current_fencing()` is disconnect-aware with no wiring. Installing the middleware is the opt-in; work that must outlive the client fences on a fresh `Fencing()` instead. `watch` (`WatchPredicate`) narrows *which requests* it owns at all — asked before the app runs, because channel ownership cannot be retrofitted, and therefore above the router, where no route exists yet. Declining is total: no event, no binding, nothing below can ask. The optional `on_disconnect` (`DisconnectCallback`) is an observability hook rather than part of the mechanism: it fires only when the event is set, after the app returned and therefore after routing filled the scope in place, and its exceptions are logged, not propagated
    - **`starlette/api.py`** — what was published and how to read it back: `DISCONNECT_EVENT_SCOPE_KEY` (`"aiofence.disconnect_event"`), `DISCONNECT_CODE`, the `ContextVar`, and `get_disconnect_event` / `require_disconnect_event`, which answer with or without a scope. `_publish` is the one place that sets and drops both copies
  - **`contrib/fastapi.py`** — `disconnect_event` / `disconnect_fencing` / `disconnect_fencing_dependency` and the `DisconnectEvent` / `DisconnectFencing` aliases. Pure readers of what the middleware published; they raise `RuntimeError` when it isn't installed
- **`__init__.py`** — public re-exports

## API

For usage guide, examples, and custom trigger documentation see [api.md](api.md).

## Core Concepts

- **`Trigger`** — abstract cancellation condition. `check()` for synchronous pre-check, `arm(callback)` for async monitoring. Returns a `TriggerHandle`.
- **`TriggerHandle`** — live watch returned by `Trigger.arm()`. `disarm()` stops monitoring.
- **`Fence`** — sync context manager that arms triggers against the current task. Suppresses `CancelledError` on exit. Caller inspects `fence.suppressed` / `fence.cancel_reasons` after the block.
- **`CancelBackend`** — `enter(task)` returns a `CancelHandle`: `cancel(message)` delivers the fence's one cancel, `exit(exc_type, exc_val)` balances it and says whether the exception leaving the body is the fence's to suppress. `exit` is always called, cancel or not. `NativeBackend`'s handle encapsulates one `cancel()`/`uncancel()` cycle, tracks whether a deferred cancel fired and settles ownership by the counter.
- **`CancelReason`** — frozen dataclass with `message` and `cancel_type` (TIMEOUT or EVENT).
- **`CancelPolicy`** — `Callable[[CancelReason], bool]` consulted once per reason before the cancel is delivered. `False` routes the reason to `fence.declined_reasons` and cancels nothing; a raise is logged and counts as `True`. `Fencing.guard()` composes them with AND, `Fencing.unless()` is sugar over `guard()`.

## Cancellation Flow

0. `Fence.__enter__` requires a running task — `task.cancel()` is the only mechanism there is. Entered from a loop callback or from a worker thread (a sync FastAPI `def` handler), it raises `RuntimeError`
1. `Fence.__enter__` calls `backend.enter(task)`; the native handle snapshots `task.cancelling()` as the baseline counter
2. Runs `check()` on all triggers — each reason passes the policy first; if any is accepted, records it and calls `handle.cancel()`. Called from inside the task, the native handle defers `task.cancel()` via `call_soon`
3. If no accepted pre-triggers, arms all triggers (an already-set event arms as a no-op); when one fires, the callback passes the reason through the policy, records it and calls `handle.cancel()`, which from a loop callback is an immediate `task.cancel()`
4. Body runs. At the next `await`, `CancelledError` is raised inside the body
5. `Fence.__exit__` disarms all triggers, then calls `handle.exit()`; for the native handle:
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
- **TaskGroup cancels while Fence is active**: Fence's trigger didn't fire (no cancel was delivered), so `handle.exit()` returns `False` — `CancelledError` propagates to TaskGroup correctly
- **Both fire simultaneously**: counter protocol resolves ownership — Fence sees `remaining > baseline`, backs off, TaskGroup claims it

## Deferred Cancel via `call_soon`

`NativeBackend`'s handle never calls `task.cancel()` synchronously from within the task's own execution. Instead it schedules via `loop.call_soon()`. This avoids setting asyncio's internal `_must_cancel` flag during synchronous code, which would force `CancelledError` at the next `await` regardless of whether `uncancel()` was called.

## Cancel Backends

`NativeBackend` calls `task.cancel()`. That is a *native* cancellation: it lands at whatever `await` the task is suspended in, and nothing in between can hold it back. Libraries written for anyio's model — httpx/httpcore, Starlette — wrap cleanup that must not be interrupted in `anyio.CancelScope(shield=True)`, and an anyio shield only blocks cancellation anyio itself delivers. Against a native cancel every such shield is inert, and httpcore in particular can lose a pool slot for the life of the pool when the cancel lands inside a connection close.

`AnyioBackend` enters an `anyio.CancelScope` per fence and cancels through `scope.cancel(message)`. Delivery is then anyio's: `task.cancel()` only while the task is suspended on a pending future, retried every loop tick until the scope exits, and skipped while a shielded child scope is active. anyio balances the counter for each cancel it issued and swallows only its own `CancelledError`. On top of that verdict the handle keeps the library's counter rule — if `task.cancelling()` is still above the entry baseline, an asyncio `TaskGroup` or `asyncio.timeout` has a cancel outstanding that anyio cannot see, and the fence propagates.

What changes for code inside the fence, and only there:

| Inside the fence | native | anyio |
|---|---|---|
| catch `CancelledError`, `await` again | later awaits run undisturbed | re-cancelled at every await until the fence exits |
| `await` inside `anyio.CancelScope(shield=True)` | interrupted | held until the shield exits |
| trigger fires in the tick the body's last await resolves | cancel arrives instead of the result | result arrives; body completes, `suppressed` is `False` |
| fence inside a cancelled anyio task group | fence's cancel may land first and be suppressed | fence defers to the group; body is torn down |

Cleanup after a suppressed cancel belongs after the `with` block on either backend; on anyio that is the only place it runs undisturbed. Pre-triggered fences need no `call_soon` deferral: `scope.cancel()` from inside the task is retried next tick, and a body that completed synchronously leaves nothing to cancel.

## Event Watching Without Tasks

`EventTrigger` subscribes a `Future` directly to `asyncio.Event._waiters` instead of spawning a background task. The future's done callback fires the cancellation. Uses private API but mirrors what `Event.wait()` does internally.

## Design Decisions

- **Sync context manager**: `__enter__`/`__exit__` (not async). Event loop interaction happens via callbacks and `call_soon`, not awaits.
- **Single mode**: No split between "raise" and "suppress" modes. Fence always suppresses. If the caller wants to raise, they do it themselves after checking `fence.cancelled` / `fence.suppressed`.
- **No custom exception types**: No `FenceTimeout` or `FenceCancelled`. Keeps the API surface minimal and avoids `CancelledError` subclass pitfalls.
- **No scope tree / shielding of our own**: Nesting and shielding handled by asyncio itself (`asyncio.shield()`, `uncancel()` counting). `AnyioBackend` opts into anyio's scope tree for delivery only; the fence still records reasons and settles ownership the same way.
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

The completion flag does not get to decide alone. From ASGI spec 2.4 a server may raise a subclass of `OSError` from `send` once the connection is closed, and the spec warns it can raise *before* the disconnect message reaches a reader — on the terminal message that would leave the flag saying "response complete" and the disconnect read as "stream ended". So the wrapped `send` treats an `OSError` as a disconnect in its own right: no phase check is needed, because nothing follows the terminal message and a send the server refused is a response the client never got. The error is recorded, not swallowed — Starlette's `StreamingResponse` converts it to `ClientDisconnect`, and the channel then answers `http.disconnect` to readers below so none of them park on a closed connection.

The read loop stops as soon as it records a disconnect: uvicorn re-delivers the message immediately and forever, so reading past it would spin for the rest of the request, background-task phase included.

Replay settles that both readers are *told*, not who acts first. A fenced streaming body can therefore outlive its rival listener's cancel scope — `move_on_cancel()` suppressed the cancellation deliberately, so the generator resumes and emits its last chunk. That last chunk lands quietly on every server shipping today; against a spec-2.4 server it is the send that raises, which `StreamingResponse` reports as `ClientDisconnect`.

## Why This Complexity Is Necessary

Fence is a generalized `asyncio.timeout()`. The stdlib timeout does the same cancel/uncancel/suppress dance — but only for one trigger type and converts to `TimeoutError`. Fence supports arbitrary triggers and suppresses instead of converting.

Every piece exists because asyncio's counter protocol demands it:

- **Counter snapshot** — needed to distinguish own cancel from outer cancel. `asyncio.timeout()` does the same.
- **`call_soon` deferral** — calling `cancel()` synchronously sets `_must_cancel`, which `uncancel()` couldn't clear until 3.13. Deferring via `call_soon` ensures `cancel()` finds `_fut_waiter` set and never touches the flag.
- **The native handle** — tracks "scheduled but not delivered" vs "delivered". Without this, a sync body completing before `call_soon` fires would leave a stale cancel in flight.
- **Suppression** — the only correct exit strategy. The alternatives all cause the worker to lose control (post-block code never runs), and option 3 additionally breaks `asyncio.timeout` via counter inflation.

There is no simpler way to implement this within asyncio's cancellation model. Cooperative flags (check-in-a-loop) would work but lose the ability to interrupt arbitrary `await` points. Not calling `task.cancel()` means not solving the problem.
