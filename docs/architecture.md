# Architecture

## Module Layout

- **`core.py`** — abstractions and core runtime: `CancelReason`, `CancelType`, `CancelPolicy`, `Fence`
- **`backends/`** — how a fence cancels its task, behind the `CancelBackend` / `CancelHandle` ABCs. `AnyioBackend` (default, `backends/anyio.py`) is an `anyio.CancelScope` per fence and the only backend that nests; `NativeBackend` is asyncio's own `cancel()`/`uncancel()` protocol and refuses a second fence on one task. `set_default_backend()` selects one process-wide, `bind_backend()` for a context and the tasks it spawns, `Fence(backend=...)` per fence; deadline, events, policy and reasons are unaffected by the choice. See [Cancel Backends](#cancel-backends)
- **`contrib/`** — optional framework integrations (Starlette / FastAPI). Never imported by the core package, so it stays dependency-free; see [api.md](api.md)
  - **`contrib/starlette/`** — the ASGI side, split by direction: one module writes, the other reads. Import from the package; the split is internal
    - **`starlette/middleware.py`** — `DisconnectMiddleware`, the single reader of a request's ASGI receive channel: `_RequestChannel` reads the channel in one task, replays body messages, records the disconnect — from the receive side or from a `send` the server refuses — and owns the event the middleware publishes; `_ResponsePhase` tracks how far the response has got and answers whether it had already ended. It also binds the event on the ambient `Fencing` under `DISCONNECT_CODE`, so anything below it that fences via `get_current_fencing()` is disconnect-aware with no wiring, and binds its `backend` — `AnyioBackend` unless told otherwise — for the request, so every fence below cancels through anyio and Starlette's and httpx's shields hold. Installing the middleware is the opt-in; work that must outlive the client fences on a fresh `Fencing()` instead. `watch` (`WatchPredicate`) narrows *which requests* it owns at all — asked before the app runs, because channel ownership cannot be retrofitted, and therefore above the router, where no route exists yet. Declining is total: no event, no binding, nothing below can ask. The optional `on_disconnect` (`DisconnectCallback`) is an observability hook rather than part of the mechanism: it fires only when the event is set, after the app returned and therefore after routing filled the scope in place, and its exceptions are logged, not propagated
    - **`starlette/api.py`** — what was published and how to read it back: `DISCONNECT_EVENT_SCOPE_KEY` (`"aiofence.disconnect_event"`), `DISCONNECT_CODE`, the `ContextVar`, and `get_disconnect_event` / `require_disconnect_event`, which answer with or without a scope. `_publish` is the one place that sets and drops both copies
  - **`contrib/fastapi.py`** — `disconnect_event` / `disconnect_fencing` / `disconnect_fencing_dependency` and the `DisconnectEvent` / `DisconnectFencing` aliases. Pure readers of what the middleware published; they raise `RuntimeError` when it isn't installed
- **`__init__.py`** — public re-exports

## API

For usage guide and examples see [api.md](api.md).

## Core Concepts

- **Sources** — a fence has one absolute `deadline` (a `loop.time()` value, reported under `deadline_code`) and any number of `(event, code)` pairs. The deadline is a single loop timer, advertised to the backend through `handle.set_deadline()`; each event is watched by a future subscribed to `Event._waiters` (see [Event Watching Without Tasks](#event-watching-without-tasks)). A source already met at `__enter__` is a pre-check; the rest are armed.
- **`Fence`** — sync context manager that arms its deadline and events against the current task. Suppresses its own `CancelledError` on exit; any other is recorded as EXTERNAL and propagates. Caller inspects `fence.suppressed` / `fence.cancel_reasons` after the block.
- **`CancelBackend`** — `enter(task)` returns a `CancelHandle`: `cancel(message)` delivers the fence's one cancel, `exit(exc_type, exc_val)` balances it and says whether the exception leaving the body is the fence's to suppress. `exit` is always called, cancel or not. `set_deadline(when)` advertises the fence's tightest pending timer, `math.inf` once none is pending; it is information, never a cancel — `AnyioBackend` sets it on the scope, `NativeBackend` ignores it. `enter_nested(task)` is called instead for a fence entered while another is active on the same task: `AnyioBackend` returns another scope, `NativeBackend` raises `RuntimeError`. `NativeBackend`'s handle encapsulates one `cancel()`/`uncancel()` cycle, tracks whether a deferred cancel fired and settles ownership by the counter.
- **`CancelReason`** — frozen dataclass with `message` and `cancel_type` (TIMEOUT, EVENT, or EXTERNAL). An EXTERNAL reason under `EXTERNAL_CODE` is recorded at exit when a `CancelledError` — plain or inside an exception group — leaves the body and the backend did not claim it; the backend decides, the fence only records. It propagates rather than being suppressed.
- **`CancelPolicy`** — `Callable[[CancelReason], bool]` consulted once per reason before the cancel is delivered. `False` routes the reason to `fence.declined_reasons` and cancels nothing; a raise is logged and counts as `True`. `Fencing.guard()` composes them with AND, `Fencing.unless()` is sugar over `guard()`.

## Cancellation Flow

0. `Fence.__enter__` requires a running task — `task.cancel()` is the only mechanism there is. Entered from a loop callback or from a worker thread (a sync FastAPI `def` handler), it raises `RuntimeError`
1. `Fence.__enter__` calls `backend.enter(task)`; the native handle snapshots `task.cancelling()` as the baseline counter
2. Checks which sources are already met — a past deadline, a set event; each reason passes the policy first; if any is accepted, records it and calls `handle.cancel()`. Called from inside the task, the native handle defers `task.cancel()` via `call_soon`
3. If no accepted pre-check, arms the rest — the deadline as a loop timer, each unset event as a waiter future; when one fires, the callback passes the reason through the policy, records it and calls `handle.cancel()`, which from a loop callback is an immediate `task.cancel()`
4. Body runs. At the next `await`, `CancelledError` is raised inside the body
5. `Fence.__exit__` cancels the timer and removes the waiters, then calls `handle.exit()`; for the native handle:
   - If cancel never fired (sync body completed first) — rescinds pending `call_soon`, returns `False`
   - If cancel fired and counter returned to baseline — `uncancel()` + suppress (`return True`)
   - If counter above baseline — outer scope also cancelled, don't suppress (`return False`)

## Cancellation Ownership

Uses asyncio's `cancel()`/`uncancel()` counter protocol. Each `Fence` snapshots `task.cancelling()` on entry as its baseline. On exit, `uncancel()` decrements the counter. If `remaining <= baseline` and the exception is `CancelledError`, this Fence owns it and suppresses. If `remaining > baseline`, an outer scope also called `cancel()` — defer to them.

## Suppression Semantics

Fence **always suppresses** the `CancelledError` its own deadline or event caused. `__exit__` raises nothing of its own; a cancel that is not the fence's propagates untouched, recorded as EXTERNAL. This follows anyio's `CancelScope` model.

### Why suppress instead of raising

Three alternatives were considered and rejected. All lose worker control (code after the `with` block never runs), and each has a unique breakage:

1. **Raise a CancelledError subclass** — breaks TaskGroup. `TaskGroup.__aexit__` uses `et is CancelledError` (identity check, not `isinstance`), so a subclass is treated as a regular exception. This is by design — [subclassing CancelledError is not officially supported](https://discuss.python.org/t/subclassing-cancellederror/92285):

   ```python
   async with asyncio.TaskGroup() as tg:
       tg.create_task(important_work())
       with Fence(deadline=loop.time() + 1) as fence:
           await asyncio.sleep(10)
       # FenceCancelled propagates → BaseExceptionGroup([FenceCancelled])
   ```

2. **`uncancel()` + propagate CancelledError** — no protocol breakage, but a handled internal timeout leaks as `CancelledError` to the caller. The user can't distinguish Fence's cancel from external cancellation without manually tracking the counter.

3. **Don't `uncancel()` + propagate** — breaks `asyncio.timeout`. The inflated counter makes outer scopes think an additional cancel is in flight:

   ```python
   async with asyncio.timeout(5):    # baseline=0
       with Fence(deadline=loop.time() + 1) as fence:
           await asyncio.sleep(10)
       # Fence: cancel() → counter=1, no uncancel
       # timeout fires → cancel() → counter=2, uncancel() → 1
       # remaining(1) > baseline(0) → "not my cancel" → no TimeoutError
   ```

Suppression is the only approach that preserves worker control and is composable with `TaskGroup` and `asyncio.timeout`. Nested Fences on one task compose the same way under `AnyioBackend`, where anyio's scope stack settles which fence a cancel belongs to. `NativeBackend` has one counter and no way to attribute a cancel to one of two fences, so it refuses a second `__enter__` with `RuntimeError` ([#12](https://github.com/stanislaushimovolos/aiofence/issues/12)).

### Pre-triggered behavior

Python sync context managers cannot skip the body without raising from `__enter__`. If `__enter__` raises, `__exit__` is never called, so counter cleanup can't happen.

Instead, pre-triggered Fences schedule `task.cancel()` via `call_soon` and let the body start. The body is interrupted at the first `await`. If the body has no awaits and completes synchronously, the pending cancel is rescinded — `fence.cancelled` is still `True` (reasons were recorded on entry, so `fence.cancelled` reflects that a source fired), but no `CancelledError` is ever delivered.

### TaskGroup compatibility

- **Fence inside TaskGroup**: suppresses, counter balanced, TaskGroup never sees `CancelledError`
- **TaskGroup cancels while Fence is active**: Fence's own sources didn't fire (no cancel was delivered), so `handle.exit()` returns `False` — `CancelledError` propagates to TaskGroup correctly
- **Both fire simultaneously**: on `NativeBackend` the counter protocol resolves ownership — Fence sees `remaining > baseline`, backs off, TaskGroup claims it. On `AnyioBackend` anyio's verdict stands and the fence suppresses; see [Cancel Backends](#cancel-backends)

## Deferred Cancel via `call_soon`

`NativeBackend`'s handle never calls `task.cancel()` synchronously from within the task's own execution. Instead it schedules via `loop.call_soon()`. This avoids setting asyncio's internal `_must_cancel` flag during synchronous code, which would force `CancelledError` at the next `await` regardless of whether `uncancel()` was called.

## Cancel Backends

`NativeBackend` calls `task.cancel()`. That is a *native* cancellation: it lands at whatever `await` the task is suspended in, and nothing in between can hold it back. Libraries written for anyio's model — httpx/httpcore, Starlette — assume cancels arrive the anyio way: httpcore guards its connection state machine with `anyio.Lock` ([`_synchronization.py#L68`](https://github.com/encode/httpcore/blob/1.0.9/httpcore/_synchronization.py#L68)) and its cleanup with `anyio.CancelScope(shield=True)` ([`AsyncShieldCancellation`](https://github.com/encode/httpcore/blob/1.0.9/httpcore/_synchronization.py#L190-L208)), and Starlette coordinates request teardown through anyio task groups ([`StreamingResponse`](https://github.com/encode/starlette/blob/1.6.0/starlette/responses.py#L273-L280), [`BaseHTTPMiddleware`](https://github.com/encode/starlette/blob/1.6.0/starlette/middleware/base.py#L117)). Shields and shielded checkpoints only hold back cancellation anyio itself delivers; against a native cancel they are inert. Usually that costs nothing — a single native cancel is absorbed by the `except` that starts httpcore's cleanup, and the cleanup then runs undisturbed. The damage happens when the cancel lands on the `sleep(0)` checkpoint inside `anyio.Lock.acquire` ([anyio 4.12.1](https://github.com/agronholm/anyio/blob/4.12.1/src/anyio/_backends/_asyncio.py#L1062)), which httpcore 1.0.9 takes before every state transition: right after connect, before the connection is marked `ACTIVE` ([`http11.py#L72-L75`](https://github.com/encode/httpcore/blob/1.0.9/httpcore/_async/http11.py#L72-L75)), or in `_response_closed`, before it is marked `IDLE` or `CLOSED` ([`#L238-L250`](https://github.com/encode/httpcore/blob/1.0.9/httpcore/_async/http11.py#L238-L250)). The transition is skipped, the connection stays `NEW` or `ACTIVE`, and the pool's sweep — which removes only closed, expired or surplus idle connections ([`_assign_requests_to_connections`](https://github.com/encode/httpcore/blob/1.0.9/httpcore/_async/connection_pool.py#L285-L293)), with `NEW` explicitly not available ([`http11.py#L267-L272`](https://github.com/encode/httpcore/blob/1.0.9/httpcore/_async/http11.py#L267-L272)) — never touches it. One slot is gone for the life of the pool, and where the shielded stream close was the thing interrupted, the pool's request entry leaks with it ([`PoolByteStream.aclose`](https://github.com/encode/httpcore/blob/1.0.9/httpcore/_async/connection_pool.py#L409-L417) removes it only after the shield). Measured with the [#961](https://github.com/encode/httpcore/issues/961) reproducer against a local server, pool of 500 so nothing ever queued, 100 concurrent requests cancelled per batch: after 12 100 native cancels, 53 connections stuck (51 `NEW`, 2 `ACTIVE`); after 18 200 anyio-delivered cancels, none. The window is one loop tick per request, so against a multi-second upstream the per-cancel odds are small, but the loss is permanent and only accumulates. httpcore2 acquires that lock with `fast_acquire=True`, no await, so the state leak is gone there; its remaining native-only leak is the request entry removed *after* a shielded close (28 in 30 s against a connection-closing server, 3 against keep-alive, 0 under anyio). Separately, and on *both* backends, upstream httpcore 1.0.9 poisons its pool when a *queued* request is cancelled while being handed a fresh, not-yet-connected connection ([#961](https://github.com/encode/httpcore/issues/961), fix open in [#986](https://github.com/encode/httpcore/pull/986)): that needs a saturated pool, and then reproduced in about 10 s native and under 1 s anyio. [httpx2](https://pypi.org/project/httpx2/) fixes it. The task-group side is the other edge of native delivery: inside a cancelled anyio task group, a native cancel can land first and be suppressed, and the body carries on — see the table below and [Disconnect Delivery](#disconnect-delivery-replay-and-record).

`AnyioBackend` enters an `anyio.CancelScope` per fence and cancels through `scope.cancel(message)`. Delivery is then anyio's ([`_deliver_cancellation`](https://github.com/agronholm/anyio/blob/4.12.1/src/anyio/_backends/_asyncio.py#L556-L594)): `task.cancel()` only while the task is suspended on a pending future, retried every loop tick until the scope exits, and skipped while a shielded child scope is active. anyio balances the counter for each cancel it issued and swallows only its own `CancelledError`. That verdict is taken as is; the handle keeps no counter of its own. The cost is one race: an asyncio-native cancel — `TaskGroup`, `asyncio.timeout()`, `task.cancel()` — landing in the same tick as the fence's own is merged by asyncio into the single `CancelledError` already in flight, anyio recognises that error as its own and the fence suppresses it. The outer cancel is lost: `task.cancelling()` stays raised, but no `CancelledError` is ever delivered for it, so an `asyncio.timeout()` that expired in that tick raises no `TimeoutError` and a peer's `task.cancel()` never reaches the task. Every canceller in the Starlette/httpx stack is anyio, so there the race cannot occur. An asyncio-native cancel that lands *first*, or alone, carries no anyio message and propagates as usual.

The scope also carries the fence's deadline. `_ScopeHandle.set_deadline` assigns `scope.deadline`, so `anyio.current_effective_deadline()` below the fence — in the body, in an anyio task group's children, up to the first shield — reports the fence's tightest timeout, and an outer anyio deadline that is tighter still wins. anyio would normally fire its own timer on that deadline and cancel the scope with no reason and no policy consulted; the scope is therefore `_AdvertisedScope`, a subclass of anyio's asyncio `CancelScope` whose `_timeout` hook is a no-op. That hook is the only place anyio acts on a deadline — `current_effective_deadline`, `checkpoint_if_cancelled` and `fail_after` merely read it — so the fence's own timer stays the one mechanism, and a declined deadline is re-advertised as `math.inf`. The subclass reaches into `anyio._backends._asyncio`; the test that an advertised deadline never cancels on its own pins that assumption.

What changes for code inside the fence, and only there:

| Inside the fence | native | anyio |
|---|---|---|
| catch `CancelledError`, `await` again | later awaits run undisturbed | re-cancelled at every await until the fence exits |
| `await` inside `anyio.CancelScope(shield=True)` | interrupted | held until the shield exits |
| the deadline or an event fires in the tick the body's last await resolves | cancel arrives instead of the result | result arrives; body completes, `suppressed` is `False` |
| fence inside a cancelled anyio task group | fence's cancel may land first and be suppressed | fence defers to the group; body is torn down |
| `asyncio.TaskGroup` / `asyncio.timeout()` cancels in the tick the fence's own timer fires | counter says the outer cancel is outstanding; fence propagates | anyio sees only its own cancel; fence suppresses, outer cancel lost |
| `anyio.current_effective_deadline()` | unaffected by the fence | the fence's tightest timeout, or a tighter outer anyio deadline |

`DisconnectMiddleware` binds `AnyioBackend` for each request it owns, since everything below it runs inside Starlette and usually calls httpx; `backend=NativeBackend()` opts an app out.

Cleanup after a suppressed cancel belongs after the `with` block on either backend; on anyio that is the only place it runs undisturbed. Pre-triggered fences need no `call_soon` deferral: `scope.cancel()` from inside the task is retried next tick, and a body that completed synchronously leaves nothing to cancel.

## Event Watching Without Tasks

The fence subscribes a `Future` per event directly to `asyncio.Event._waiters` instead of spawning a background task. The future's done callback fires the cancellation. Uses private API but mirrors what `Event.wait()` does internally.

## Design Decisions

- **Sync context manager**: `__enter__`/`__exit__` (not async). Event loop interaction happens via callbacks and `call_soon`, not awaits.
- **Single mode**: No split between "raise" and "suppress" modes. Fence always suppresses its own cancel. If the caller wants to raise, they do it themselves after checking `fence.cancelled` / `fence.suppressed`.
- **No `CancelledError` subclasses**: `Fence` itself raises nothing of its own. `Fencing.raise_on_cancel()` raises `FenceCancelled`, a plain `Exception` carrying the reasons, but only after the fence has already suppressed the `CancelledError` and the block has exited — so `TaskGroup` and `asyncio.timeout()` never see it as a cancel, and none of the subclass pitfalls above apply.
- **No scope tree / shielding of our own**: Shielding is asyncio's (`asyncio.shield()`) and ownership against outer scopes is settled by `uncancel()` counting. `AnyioBackend` opts into anyio's scope tree for delivery and for nesting; `NativeBackend` rejects a second fence on one task at `__enter__` ([#12](https://github.com/stanislaushimovolos/aiofence/issues/12)). On both, the fence records reasons and settles ownership the same way.
- **Deadlines vs timeouts**: `Fencing.timeout()` is relative and resolves eagerly, `Fencing.deadline()` is an absolute `loop.time()` value; the builder merges the tightest into the one deadline a `Fence` takes. The fence exposes no remaining budget of its own: under `AnyioBackend` it advertises that deadline as the scope deadline, so `anyio.current_effective_deadline()` is the way to read it, and under either backend the application keeps the deadline it declared and derives `deadline - loop.time()` when it needs one, e.g. to forward a relative duration to a downstream service as a header.

## Disconnect Delivery: Replay and Record

`DisconnectMiddleware` exists because an ASGI receive channel has exactly one useful reader — `receive()` is a queue pop, not a broadcast — while a request routinely has several interested parties: `StreamingResponse`'s `listen_for_disconnect`, sse-starlette's listener, `Request.is_disconnected()`, and anything wanting to know the client left. Whoever reads first consumes the message; on hypercorn, daphne and granian it is delivered exactly once, so everyone else starves. A dependency cannot arbitrate: Starlette captures the raw `receive` before any dependency runs, so there is no reference left to wrap. Only a middleware sits above all of them.

Three properties make the arbitration correct, and each is load-bearing:

- **Replay, don't discard.** The read loop forwards every `http.request` message downstream in order and unchanged. The alternative — the dependency's watcher, which discards anything that isn't a disconnect — steals body chunks, and the loser of that race gets `{"body": b"", "more_body": False}`, which Starlette accepts as a *complete, empty* body. Silent truncation, no exception, no log. Replay makes every reader *see* the disconnect; it does not make `Request.is_disconnected()` safe next to a body read. That method pops and discards whatever message is next, body chunks included, above or below the middleware — the published event is the replacement, not a companion.
- **Record, don't queue.** The disconnect is a terminal side channel: recorded once and answered on every later `receive()`, rather than queued as a message of its own that the first reader would consume. Buffered body messages are still handed over first; the event itself is set the moment the disconnect arrives. That turns a one-shot server delivery into a signal every reader below can observe, and it keeps the middleware from needing a bounded queue of its own. Draining the server's queue also matters in itself — hypercorn bounds its app queue at 10 messages and blocks the connection when it fills.
- **Track response completion in a wrapped `send`.** Per the ASGI spec `http.disconnect` means "the stream ended", not "the client left": every server sends it once the response is complete. The middleware flips its flag *before* handing the terminal message to the server's `send`, because the read loop can only be woken by the server having processed that message. A disconnect recorded after that point is replayed downstream but does **not** set the event — which is what keeps `BackgroundTasks` and post-response work from being cancelled on every successful request. This is the only place in the stack where the two meanings can be told apart.

"Terminal message" is not always the final body one. When `http.response.start` declares `"trailers": True`, the response is not complete until the last `http.response.trailers` message, and a client that leaves in between has genuinely left. The wrapped `send` tracks the declaration so the flag flips at the real end of the response rather than one message early.

The completion flag does not get to decide alone. From ASGI spec 2.4 a server may raise a subclass of `OSError` from `send` once the connection is closed, and the spec warns it can raise *before* the disconnect message reaches a reader — on the terminal message that would leave the flag saying "response complete" and the disconnect read as "stream ended". So the wrapped `send` treats an `OSError` as a disconnect in its own right: no phase check is needed, because nothing follows the terminal message and a send the server refused is a response the client never got. The error is recorded, not swallowed — Starlette's `StreamingResponse` converts it to `ClientDisconnect`, and the channel then answers `http.disconnect` to readers below so none of them park on a closed connection.

The read loop stops as soon as it records a disconnect: uvicorn re-delivers the message immediately and forever, so reading past it would spin for the rest of the request, background-task phase included.

Replay settles that both readers are *told*, not who acts first. Under the middleware's default `AnyioBackend`, a fence inside a streaming body sits within sse-starlette's own task group scope; when the rival listener cancels that group the fence defers to it and the generator is torn down mid-await, so nothing after the fence in the generator runs once the client is gone. On `NativeBackend` the fence's cancel lands first and is suppressed, and the generator resumes for its last chunk — which lands quietly on every server shipping today and, against a spec-2.4 server, is the send that raises `ClientDisconnect`.

## Why This Complexity Is Necessary

Fence is a generalized `asyncio.timeout()`. The stdlib timeout does the same cancel/uncancel/suppress dance — but only for a deadline and converts to `TimeoutError`. Fence adds events, codes and a policy, and suppresses instead of converting.

Every piece exists because asyncio's counter protocol demands it:

- **Counter snapshot** — needed to distinguish own cancel from outer cancel. `asyncio.timeout()` does the same.
- **`call_soon` deferral** — calling `cancel()` synchronously sets `_must_cancel`, which `uncancel()` couldn't clear until 3.13. Deferring via `call_soon` ensures `cancel()` finds `_fut_waiter` set and never touches the flag.
- **The native handle** — tracks "scheduled but not delivered" vs "delivered". Without this, a sync body completing before `call_soon` fires would leave a stale cancel in flight.
- **Suppression** — the only correct exit strategy. The alternatives all cause the worker to lose control (post-block code never runs), and option 3 additionally breaks `asyncio.timeout` via counter inflation.

There is no simpler way to implement this within asyncio's cancellation model. Cooperative flags (check-in-a-loop) would work but lose the ability to interrupt arbitrary `await` points. Not calling `task.cancel()` means not solving the problem.
