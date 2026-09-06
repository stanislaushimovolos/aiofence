# API Guide

## Quick Start

```python
from aiofence import on_timeout, on_event

# Timeout — suppress and inspect
with on_timeout(5).move_on_cancel() as fence:
    await work()
if fence.cancelled:
    return fallback()

# Timeout — raise on cancel
with on_timeout(5).raise_on_cancel() as fence:
    await work()
# raises FenceCancelled if timed out

# Event — cancel when shutdown is signalled
shutdown = asyncio.Event()
with on_event(shutdown, code="shutdown").move_on_cancel() as fence:
    await work()
```

## Concepts

A fence has two kinds of cancellation source: one absolute **deadline** — the tightest of the timeouts and deadlines declared — and any number of **events**, each under its own `code`. You declare them once at the boundary using a **Fencing** builder, then materialize them into a context manager. Inside the block, code runs normally — no need to thread events, flags, or tokens through call signatures.

After the block, inspect `fence.cancelled`, `fence.cancel_reasons`, or `fence.cancelled_by(code)` to decide what to do.

## Creating a Fencing

Use the factory functions — each returns a `Fencing` builder:

| Factory | Condition |
|---------|-----------|
| `on_timeout(delay, *, code=None)` | Relative timeout in seconds; `None` yields an empty Fencing |
| `on_deadline(when, *, code=None)` | Absolute monotonic time (`loop.time()` based) |
| `on_event(event, *, code=None)` | Cancel when `asyncio.Event` is set |

The `code` parameter is an optional machine-readable identifier. Use it to distinguish which source fired via `fence.cancelled_by(code)`. Works well with `StrEnum` for type safety.

## Chaining Conditions

`Fencing` is immutable — every method returns a new instance. Chain freely:

```python
fencing = (
    on_timeout(30, code="budget")
    .event(shutdown, code="shutdown")
    .event(disconnect, code="disconnect")
)
with fencing.move_on_cancel() as fence:
    await work()
```

Available builder methods:

| Method | Description |
|--------|-------------|
| `.timeout(delay, *, code=None)` | Add a relative timeout; `None` adds nothing (optional timeout from config) |
| `.deadline(when, *, code=None)` | Add an absolute deadline (`loop.time()` based) |
| `.event(event, *, code=None)` | Add an event condition |
| `.guard(policy)` | Consult `policy(reason)` before delivering a cancel |
| `.unless(precondition, *, code=None)` | Decline cancellation while `precondition()` holds |

### Timeout / Deadline Merging

Time-based conditions are merged — the tightest constraint wins:

```python
ctx = on_timeout(30).timeout(5, code="db")
# 30s vs 5s → 5s wins, code="db"

ctx = on_deadline(T + 20, code="sla").timeout(5, code="db")
# T+20 vs now+5 → minimum wins
```

Under the default `AnyioBackend` the merged deadline is also what `anyio.current_effective_deadline()` reports inside the fence, so anyio-aware code below — httpx, Starlette, your own `anyio.fail_after` — sees the budget without being handed it; a tighter outer anyio deadline still wins. The fence's own timer does the cancelling, with its reason and through the policy; a declined timeout stops being advertised. `NativeBackend` has nowhere to show a deadline and ignores it.

`.timeout()` eagerly resolves to an absolute deadline and anchors the `Fencing` to the moment it was called. An anchored `Fencing` is per-operation: it can be opened as many times as needed at the call site, but `bind_fencing()` refuses it. Use `.deadline()` for a budget shared through the context.

Events are never merged — all arm independently. Registrations are deduplicated on the `(event, code)` pair, so the same event under two different codes gives you two entries and `cancelled_by()` answers `True` for both. The same event under the same code collapses to one.

### Guarding cancellation

A source firing is not always a reason to cancel. A streaming proxy wants the client's disconnect to cancel the upstream read while the answer is still being generated, but once the finish reason has arrived it wants to keep draining — clients routinely hang up the moment they have the chunk they were waiting for, and the trailing usage frame that gets billed is still on the wire. Same event, different decision, depending on state only the fence's user knows at fire time.

`.unless()` declines reasons while a precondition holds. Scope it to one code so the rest of the fence — typically the timeout — keeps cancelling:

```python
fencing = get_current_fencing().unless(generation.is_done, code=DISCONNECT_CODE)
with fencing.move_on_cancel() as fence:
    async for chunk in upstream:
        generation.observe(chunk)      # flips is_done() on the finish reason
        yield chunk

if fence.cancelled_by(DISCONNECT_CODE):
    ...  # client left before the finish reason; the upstream read was cancelled
elif fence.declined_by(DISCONNECT_CODE):
    ...  # client left after it; the drain ran to the end
```

Without a `code`, the precondition covers every reason on the fence, timeouts included — a fence whose timeout was declined has no time bound left. A rule that spans codes uses `.guard()` directly; it receives the `CancelReason` and returns `True` to deliver the cancel:

```python
def policy(reason: CancelReason) -> bool:
    return reason.cancel_type is CancelType.TIMEOUT or not generation.is_done()

with get_current_fencing().guard(policy).move_on_cancel() as fence:
    ...
```

Semantics:

- **Consulted once per reason**, at the moment it is produced — the pre-check on entry or the live callback. A declined fire is consumed, not re-evaluated: a source fires once per arming, so "decline now, cancel later" is the caller's job, read `declined_by()` after the block.
- **A declined pre-check still arms** the other sources, so a live timeout keeps working when a pre-set event is declined.
- **Guards compose with AND** and short-circuit. A guard added to a builder that already has one can only decline more, never less.
- **Inherited by derivation**, like events and deadlines. A guard bound on the ambient fencing applies to every fence built below it, and there is no way to remove one — fence on a fresh `Fencing()` to escape it.
- **Runs inside the loop callback**, so it must be sync and cheap. A policy that raises is logged under the `aiofence` logger and treated as `True` — cancelling is the safe default.

## Entering the Fence

Two modes, both yield a `Fence`:

### `move_on_cancel()` — suppress and inspect

```python
with on_timeout(5).move_on_cancel() as fence:
    await work()

if fence.cancelled:
    print(fence.cancel_reasons)  # why were we cancelled?
```

`CancelledError` is suppressed. Code after the `with` block always runs. Check `fence.cancelled` to decide what to do.

### `raise_on_cancel()` — raise FenceCancelled

```python
try:
    with on_timeout(5).raise_on_cancel() as fence:
        await work()
except FenceCancelled as e:
    print(e.cancel_reasons)
    print(e.cancelled_by("shutdown"))
```

`CancelledError` is still suppressed inside the block, but `FenceCancelled` (a regular `Exception`, not `CancelledError`) is raised after exit. Safe to use inside `TaskGroup`.

## Inspecting Cancellation

After the block, the `Fence` has:

| Property / Method | Type | Description |
|-------------------|------|-------------|
| `fence.cancelled` | `bool` | `True` if any source fired |
| `fence.suppressed` | `bool` | `True` if `CancelledError` was caught and suppressed |
| `fence.cancel_reasons` | `tuple[CancelReason, ...]` | All reasons that fired |
| `fence.cancelled_by(code)` | `bool` | Did a specific source fire? |
| `fence.declined_reasons` | `tuple[CancelReason, ...]` | Reasons a `guard` / `unless` policy rejected; they never cancelled |
| `fence.declined_by(code)` | `bool` | Was a specific source declined? |

Most code should use `cancelled` — it tells you whether a condition was met. `suppressed` differs only when a source fires but the body completes synchronously before `CancelledError` is delivered (pre-triggered sync body). In that case `cancelled` is `True` but `suppressed` is `False`.

`declined_reasons` is a third bucket, not a variant of either: a declined reason never counts as `cancelled`. `FenceCancelled` carries `declined_reasons` / `declined_by()` alongside `cancel_reasons`, since it does not hold the fence.

A cancel the fence did not deliver — an outer anyio scope, an `asyncio.timeout()`, a `task.cancel()` from another task — propagates as it always has: `suppressed` stays `False` and code after the `with` does not run. It is still recorded, as a `CancelType.EXTERNAL` reason under the `EXTERNAL_CODE` constant, so `fence.cancelled_by(EXTERNAL_CODE)` tells an interrupted fence from one whose body completed. The policy is not consulted for it; an outer scope's cancel is not the fence's to decline. Any `CancelledError` that leaves the body without being the fence's own counts — awaiting a cancelled child task inside the body, or an outer fence's own cancel on the same task, records `EXTERNAL` on this fence.

Each `CancelReason` has:

| Field | Type | Description |
|-------|------|-------------|
| `message` | `str` | Human-readable (e.g. `"timed out after 5s"`) |
| `cancel_type` | `CancelType` | `TIMEOUT`, `EVENT`, or `EXTERNAL` |
| `code` | `str \| None` | Machine-readable identifier |

## Common Patterns

### Early exit (no await needed)

Unlike `asyncio.timeout`, cancellation state is available immediately:

```python
with on_timeout(5).move_on_cancel() as fence:
    if fence.cancelled:
        return fallback()
    await work()
```

### Incremental accumulation across layers

```python
# Middleware: set request budget
ctx = on_deadline(loop.time() + 30, code="request")

# Handler: add shutdown listener
ctx = ctx.event(shutdown, code="shutdown")

# Inner code: per-operation timeout
with ctx.timeout(5, code="db").move_on_cancel() as fence:
    await query_db()

if fence.cancelled_by("db"):
    return cached_result
elif fence.cancelled_by("shutdown"):
    return graceful_shutdown()
```

### Reusing a Fencing

Every `Fencing` is reusable — each `move_on_cancel()` / `raise_on_cancel()` creates a fresh `Fence`:

```python
ctx = on_deadline(loop.time() + 30)

with ctx.move_on_cancel() as f1:
    await op_a()

with ctx.move_on_cancel() as f2:
    await op_b()
```

**Note:** `.timeout()` anchors the builder to the moment it was called, so every fence built from it shares that one deadline. That is what you want within a single operation, and a stale clock if the builder is kept at module level and reused across requests. Call `.timeout()` fresh each time, or use `.deadline()` where the budget is genuinely shared.

### Multiple sources

```python
with (
    on_timeout(30, code="timeout")
    .event(shutdown, code="shutdown")
    .move_on_cancel()
) as fence:
    await call_external()

if fence.cancelled_by("timeout"):
    log("slow response")
elif fence.cancelled_by("shutdown"):
    log("shutting down")
```

## Context Propagation

`bind_fencing()` stores a `Fencing` in a `ContextVar`, so inner code can access it via `get_current_fencing()` without passing it through every call signature.

```python
from aiofence import Fencing, bind_fencing, get_current_fencing, on_event

# Boundary: declare the rules — a shared budget is a deadline
fencing = on_event(disconnect, code="disconnect").deadline(loop.time() + 30)
with bind_fencing(fencing):
    await handle_request()

# Deep inside: read and use
async def process():
    with get_current_fencing().move_on_cancel() as fence:
        await do_work()

# Or extend with local concerns:
async def process_with_extra():
    with get_current_fencing().event(other_event).move_on_cancel() as fence:
        await do_work()
```

### Semantics

- **`bind_fencing()` only stores config** — it does not create a Fence. `move_on_cancel()` / `raise_on_cancel()` materialize Fences from it.
- **`bind_fencing()` refuses an anchored `Fencing`** — one built with `.timeout()` is per-operation, and the error is raised at the boundary rather than in the service code that later reads the context. Bind `.deadline()` for a request budget; inner code adds `.timeout()` for its own operation.
- **Token-based set/reset** — nesting works naturally. Inner `bind_fencing()` overrides, outer is restored on exit.
- **Task inheritance** — `asyncio.create_task()` copies the `ContextVar` automatically. Child tasks inherit the boundary's config without affecting the parent.
- **`get_current_fencing()` with no context** — returns an empty `Fencing()`, so chaining always works: `get_current_fencing().timeout(5)`.

## Low-Level API: Fence

`Fence` is the underlying context manager; `Fencing` is sugar over it. It takes one absolute deadline and `(event, code)` pairs, resolved at the call site:

```python
from aiofence import Fence

loop = asyncio.get_running_loop()
with Fence(deadline=loop.time() + 5, events=[(shutdown, "shutdown")]) as fence:
    await work()
```

`Fence` suppresses the `CancelledError` its own deadline or event caused; an external one propagates. It doesn't raise `FenceCancelled` — for that, use `Fencing.raise_on_cancel()`.

`Fence(deadline=None, deadline_code=None, events=(), policy=None, backend=None)` takes the same `CancelPolicy` that `Fencing.guard()` builds; `unless()` is builder-only sugar over it.

### Cancel backend

How a fence cancels its task is pluggable. The default, `AnyioBackend`, cancels through an `anyio.CancelScope` per fence: shields and locks in anyio-based libraries (httpx, Starlette) hold, their cleanup completes, fences nest — an inner fence backs off when an outer one has fired — and the fence's tightest timeout is the scope's deadline, visible to `anyio.current_effective_deadline()`. `NativeBackend` is asyncio's own `task.cancel()`, delivered exactly once, and refuses a second fence on the same task with `RuntimeError`:

```python
from aiofence import NativeBackend, set_default_backend

set_default_backend(NativeBackend())  # once, at startup
```

Every `Fence` built afterwards without an explicit `backend=` uses it, `Fencing`-built ones included. `Fence(backend=...)` overrides per fence. `aiofence.backends.bind_backend(backend)` is the scoped form: a context manager whose backend wins over the process default in the current task and in tasks it spawns — it is how [`DisconnectMiddleware` picks anyio per request](#which-backend-cancels). The trade-offs are in [architecture.md](architecture.md#cancel-backends).

## Starlette / FastAPI Integration

The integration is two modules. `aiofence.contrib.starlette` holds [`DisconnectMiddleware`](#disconnectmiddleware--one-reader-for-the-channel), which owns the request's receive channel and publishes its disconnect event. `aiofence.contrib.fastapi` holds the dependencies that read it.

**The middleware is required.** The dependencies raise `RuntimeError` without it — see [why there is no fallback](#why-the-middleware-is-required).

```python
from fastapi import FastAPI
from starlette.middleware import Middleware
from aiofence.contrib.starlette import DisconnectMiddleware
from aiofence.contrib.fastapi import DisconnectFencing

app = FastAPI(middleware=[Middleware(DisconnectMiddleware)])   # outermost

@app.get("/work")
async def handler(fencing: DisconnectFencing):
    with fencing.move_on_cancel() as fence:
        await long_work()

    if fence.cancelled_by("disconnect"):
        return Response(status_code=499)
```

`DisconnectFencing` is an alias for `Annotated[Fencing, Depends(disconnect_fencing)]`. Declaring it is optional: the middleware already binds the same event under the same code for the whole request — see [the middleware's own binding](#the-middlewares-own-binding). Plain Starlette and raw ASGI apps, which have no dependency injection, rely on that binding or read the event directly with [`get_disconnect_event()`](#reading-the-event-directly).

There is a matching `DisconnectEvent` — `Annotated[asyncio.Event, Depends(disconnect_event)]` — for handlers that want to pick their own stopping point rather than be cancelled:

```python
from aiofence.contrib.fastapi import DisconnectEvent

@app.get("/search")
async def handler(gone: DisconnectEvent):
    hits = []
    for shard in shards:
        if gone.is_set():
            break
        hits += await query(shard)
    return hits
```

**Requires `fastapi>=0.118` / `starlette>=0.42`** — install with `pip install "aiofence[fastapi]"`. FastAPI 0.106–0.117 tear yield dependencies down *before* the response is sent, which unbinds the fencing from the context for the whole streaming phase. The core package itself stays dependency-free.

`disconnect_fencing` does three things:

1. Reads the request's disconnect `asyncio.Event` — the one `DisconnectMiddleware` published
2. Adds it to `get_current_fencing()` with `code=DISCONNECT_CODE` (`"disconnect"`)
3. Binds the result as the active `Fencing` context via `bind_fencing()`

`DISCONNECT_CODE` lives in `aiofence.contrib.starlette`; import it instead of retyping the string, so `fence.cancelled_by(DISCONNECT_CODE)` in shared code can never drift from what the middleware bound.

#### Why the middleware is required

An ASGI receive channel has exactly one useful reader, and a dependency cannot be it: Starlette hands `StreamingResponse` and `EventSourceResponse` the raw `receive` captured before any dependency runs, so there is nothing left for a dependency to wrap. A dependency that read the channel anyway would steal body chunks and streaming messages from the rest of the stack, and could not tell "the client left" from "the response finished" — which cancels `BackgroundTasks` on every successful request. Both are fixed by reading the channel once, above everything else.

So there is no watch-it-yourself fallback. The dependencies raise rather than degrade:

```
RuntimeError: aiofence disconnect signalling requires DisconnectMiddleware. Install it outermost: ...
```

#### One reader per request

The middleware's read loop is the single reader for the whole request; the dependencies own nothing and start nothing. `disconnect_fencing`, `disconnect_event`, and any number of `disconnect_fencing_dependency(...)` variants therefore combine freely on one endpoint — they all read the same published event.

#### Layering codes

Each registration keeps its own code: `Fencing.event()` deduplicates on the `(event, code)` pair, not on the event alone. Two layers with different codes both report.

```python
app = FastAPI(middleware=[Middleware(DisconnectMiddleware)])                    # "disconnect"
router = APIRouter(
    dependencies=[Depends(disconnect_fencing_dependency(code="client_gone"))],  # "client_gone"
)

# in a handler on that router, after a disconnect:
fence.cancelled_by("disconnect")   # True
fence.cancelled_by("client_gone")  # True
```

Registering the same event under the *same* code twice collapses to a single entry — which is exactly what happens when a route declares `DisconnectFencing` on top of the middleware's default binding.

#### When the channel fails

If `receive()` raises — a transport error, or `BaseHTTPMiddleware` rejecting a message — the disconnect event **can no longer fire** for that request: `fence.cancelled_by("disconnect")` stays `False` however long the client has been gone. Nothing is propagated into the request's teardown either way, because by then the response has usually already been sent and Starlette could only turn it into a truncated response plus `RuntimeError("Caught handled exception, but response already started")`.

The middleware records the exception and re-raises it from the next downstream `receive()` once the buffered body has been drained, at the point where the application actually reads, and it never replaces the application's own exception. If nothing ever reads again it is logged at `WARNING` on the `aiofence.contrib.starlette` logger when the request ends.

### Composing with other sources

The returned `Fencing` inherits from the current context, so you can chain additional sources:

```python
@app.get("/work")
async def handler(fencing: DisconnectFencing):
    with fencing.timeout(30, code="budget").move_on_cancel() as fence:
        await long_work()

    if fence.cancelled_by("budget"):
        return cached_result
    elif fence.cancelled_by("disconnect"):
        return Response(status_code=499)
```

Inner code can also access the disconnect event via `get_current_fencing()`:

```python
@app.get("/work")
async def handler(fencing: DisconnectFencing):
    await process()

async def process():
    with get_current_fencing().move_on_cancel() as fence:
        await do_work()  # cancelled if client disconnects
```

### Custom disconnect code

`disconnect_fencing_dependency` builds a dependency with a different code:

```python
from aiofence.contrib.fastapi import disconnect_fencing_dependency

ClientGone = Annotated[Fencing, Depends(disconnect_fencing_dependency(code="client_gone"))]

@app.get("/work")
async def handler(fencing: ClientGone):
    ...
```

This factory is the only way to set the code. `disconnect_fencing` deliberately takes no `code` argument: FastAPI reads a dependency's keyword arguments as request parameters, so a `code` kwarg would become a client-settable query parameter — `GET /work?code=anything` would rewrite the code and `cancelled_by("disconnect")` in shared code would stop matching.

### Without a handler parameter

Most handlers never touch the returned `Fencing` — they read it from the context with `get_current_fencing()`. Use FastAPI's `dependencies=[...]`, which runs a dependency and discards its value:

```python
from aiofence import get_current_fencing

@app.get("/work", dependencies=[Depends(disconnect_fencing)])
async def handler():
    with get_current_fencing().move_on_cancel() as fence:
        await long_work()

    if fence.cancelled_by("disconnect"):
        return Response(status_code=499)
```

The same works per-router and app-wide, which is usually what you want — declare it once at the boundary instead of on every route:

```python
app = FastAPI(dependencies=[Depends(disconnect_fencing)])

router = APIRouter(
    dependencies=[Depends(disconnect_fencing_dependency(code="client_gone"))],
)
```

Sync (`def`) handlers **cannot be fenced at all**. FastAPI runs them in a threadpool, and there is no running event loop there: `get_current_fencing()` returns the right `Fencing` (the context is copied into the thread), but entering it raises `RuntimeError: no running event loop`.

```python
@app.get("/work", dependencies=[Depends(disconnect_fencing)])
def handler():                             # note: def, not async def
    get_current_fencing()                  # fine — codes are visible
    with get_current_fencing().move_on_cancel():   # RuntimeError
        ...
```

Read the codes if you want to log them; make the handler `async def` if you want cancellation.

### Both dependencies

The disconnect event — stop at your own pace:

```python
from aiofence.contrib.fastapi import DisconnectEvent

@app.get("/search")
async def handler(gone: DisconnectEvent):
    hits = []
    for shard in shards:
        if gone.is_set():
            break
        hits += await query(shard)
    return hits
```

The fencing — be cancelled instead:

```python
from aiofence.contrib.fastapi import DisconnectFencing

@app.get("/render")
async def handler(fencing: DisconnectFencing):
    with fencing.timeout(30, code="budget").move_on_cancel() as fence:
        result = await render_scene()

    if fence.cancelled_by("disconnect"):
        return Response(status_code=499)
    return result
```

Both are `Annotated[..., Depends(...)]` aliases over `disconnect_event` / `disconnect_fencing` in the same module — use the plain dependencies with `Depends()` if you prefer.

### Plain Starlette and raw ASGI

There is no dependency injection to hook into, so the middleware does both jobs itself — installing it is the whole setup:

```python
app = Starlette(middleware=[Middleware(DisconnectMiddleware)])

async def endpoint(request: Request) -> Response:
    with get_current_fencing().move_on_cancel() as fence:   # bound by the middleware
        result = await long_work()

    gone = get_disconnect_event()                           # or read the event directly
```

### `DisconnectMiddleware` — one reader for the channel

`DisconnectMiddleware` sits above the whole stack, reads the channel exactly once per request, and replays what it read to everyone underneath. It is what makes disconnect signalling possible at all — [nothing below it can do this job](#why-the-middleware-is-required).

```python
from starlette.middleware import Middleware
from aiofence.contrib.starlette import DisconnectMiddleware

app = FastAPI(middleware=[Middleware(DisconnectMiddleware)])
# or, equivalently — but make it the last add_middleware call:
app.add_middleware(DisconnectMiddleware)
```

**Install it outermost** — first entry of `middleware=[...]`, or the *last* `add_middleware` call, so it owns the server's own `receive` rather than another middleware's wrapper. Below a `BaseHTTPMiddleware` it still works, but it then owns that middleware's non-reentrant `wrapped_receive` instead.

Installed twice, the outer instance wins: the inner one finds the scope key already published and passes the request through untouched, so there is still one reader and one event. `lifespan` and `websocket` scopes pass through as well, with the original `receive` and `send` and no scope key — a websocket scope answers `websocket.disconnect` immediately and forever, which would make the read loop spin and never equals `http.disconnect` anyway.

#### Reading the event directly

The middleware publishes the event in the ASGI scope *and* binds it for the request's context, so code with no dependency and no `Request` to hand can still read it:

```python
from aiofence.contrib.starlette import get_disconnect_event, require_disconnect_event

async def deep_helper():
    gone = get_disconnect_event()                # ambient: None outside a fenced request
    if gone is not None and gone.is_set():
        return partial_result

async def endpoint(request: Request) -> Response:
    gone = require_disconnect_event(request.scope)   # explicit scope; raises if not installed
```

`get_disconnect_event` asks — it returns `None` when the middleware isn't installed. `require_disconnect_event` demands, and raises the same `RuntimeError` the dependencies do. Both take an optional `scope`: pass one when you have it (an exception handler, or a task the request didn't spawn itself), omit it anywhere below the middleware on the request's own call stack.

Both the scope key and the context binding are dropped when the request ends, so nothing can hold a reference to an event whose reader is gone.

#### What it buys

| | If a dependency read the channel itself | With the middleware |
|---|---|---|
| `BackgroundTasks` on a successful request | cancelled every time — a completed response reads as a disconnect | run normally; the event fires only while the response is unfinished |
| Raw body reads (`Request`-only handler) | hang, or silently return `b""` | exact bytes, in order, however late they are read |
| `StreamingResponse` below ASGI spec 2.4 | a race: either the fence fires or Starlette aborts the body | both readers are told |
| `StreamingResponse` on ASGI spec 2.4 | Starlette watches nothing and waits for a `send` to fail | the fence fires as soon as the client leaves, and a failed `send` sets the event too |
| sse-starlette `EventSourceResponse` | `client_close_handler_callable` never runs | close handler runs *and* `cancelled_by("disconnect")` is `True` |
| hypercorn / daphne / granian | they deliver `http.disconnect` once, so the second reader starves | recorded once, replayed to every later read |
| `BaseHTTPMiddleware` in the stack | masks the false disconnect; can raise into the watcher | one reader, so no reentrancy and no masking |

`http.request` messages are forwarded downstream in order and unchanged, and `http.disconnect` is treated as a terminal side channel — recorded rather than queued, and answered on every later `receive()` once the buffered body has been drained. An `OSError` out of `send` — what a spec-2.4 server raises on a closed connection — is recorded the same way and re-raised, so the application still sees it.

What replay settles is that both readers are *told*. Which one acts first is a scheduling matter, and the answer depends on the [backend](#which-backend-cancels): under the default anyio delivery a fenced streaming body defers to its rival listener's cancelled task group and is torn down, so put finalisation in a `finally` rather than after the fence; under native delivery the fence's cancel lands first, is suppressed, and the generator resumes for its last chunk. An unfenced body is torn down by the rival listener either way.

##### Fencing a raw body read

A fence around `await request.body()` has two exits, so catch both:

```python
try:
    with get_current_fencing().move_on_cancel() as fence:
        payload = await request.body()
except ClientDisconnect:                       # client left before the fence was entered
    return Response(status_code=499)

if fence.cancelled_by(DISCONNECT_CODE):        # client left while the read was parked
    return Response(status_code=499)
```

Under the middleware's default anyio delivery it is `ClientDisconnect` in both cases: the reply is already on its way when the disconnect is recorded, and anyio never cancels a task whose wakeup has already resolved. Plain uvicorn behaves the same way — its `receive()` skips its own `await` once the connection is gone. Under native delivery a read that was parked when the client left is cancelled instead: recording a disconnect sets the event *before* waking the reader, so the fence's cancel is queued ahead of the reply. Entered afterwards, the fence's cancel is only scheduled (see [Deferred Cancel](architecture.md#deferred-cancel-via-call_soon)) while the buffered body and the recorded disconnect are answered from state without suspending, and Starlette raises first. Fences around anything other than a channel read are unaffected.

#### The middleware's own binding

The middleware does not only publish the event — it binds it on `get_current_fencing()` under `DISCONNECT_CODE`, for the whole request. Installing it *is* the opt-in, so there is no mode where it owns the channel but signals nothing:

```python
from aiofence.contrib.starlette import DISCONNECT_CODE, DisconnectMiddleware

app = FastAPI(middleware=[Middleware(DisconnectMiddleware)])

@app.get("/work")
async def handler():                                   # no dependency declared
    with get_current_fencing().move_on_cancel() as fence:
        result = await long_work()

    if fence.cancelled_by(DISCONNECT_CODE):
        return Response(status_code=499)
```

Three consequences:

- **Any fence built from `get_current_fencing()` is disconnect-aware.** Nothing has to be declared, injected, or threaded through — which is what makes the plain-ASGI and plain-Starlette story work at all. A directly constructed `Fence(...)` arms exactly the deadline and events it was handed and nothing else.
- **Exception handlers can see it.** FastAPI applies exception handling *outside* its dependency exit stacks, so a dependency-bound fencing is already gone by the time a handler runs — a middleware-bound one is not. This is the only way for a custom exception handler to ask "was this a disconnect?".
- **Declaring `DisconnectFencing` on top changes nothing.** Same event, same code, and `Fencing.event()` deduplicates on the `(event, code)` pair — one entry, one reason.

Per-route codes stay the dependency's job. `fencing_code` renames the app-wide one:

```python
app = FastAPI(middleware=[Middleware(DisconnectMiddleware, fencing_code="client_gone")])
```

**Work that must survive the client leaving** opts out where it lives, not app-wide — fence on a fresh `Fencing()` instead of inheriting the ambient one:

```python
async def finalize_upload(chunks):
    with Fencing().timeout(60, code="flush").move_on_cancel() as fence:   # ambient not inherited
        await storage.write(chunks)                                       # disconnect can't cancel this
```

That keeps the decision visible in the code that cares about it, and leaves every other fence in the request disconnect-aware.

#### Which backend cancels

Under the middleware every fence — ambient, dependency-built, or a bare `Fence(...)` — cancels through `AnyioBackend` by default, for the request and the tasks it spawns. Starlette runs the request inside anyio task groups and httpcore guards its connection state with anyio locks and shields; all are written to anyio's cancellation contract, and a native `task.cancel()` is a foreign signal to them — inside a cancelled task group it can be the fence's cancel that lands first and gets suppressed, leaving the body running, and inside httpcore it can land on a lock checkpoint and leave a connection stuck in a state the pool never sweeps, one slot lost for good ([why](architecture.md#cancel-backends)). Neither backend protects a *saturated* upstream-httpx pool from [httpcore#961](https://github.com/encode/httpcore/issues/961); see the README caveats. Installing the middleware is therefore also opting into anyio delivery. The `backend` parameter is the way out, per app:

```python
from aiofence import NativeBackend

app = FastAPI(middleware=[Middleware(DisconnectMiddleware, backend=NativeBackend())])
```

The middleware's backend takes precedence over `set_default_backend()` for the request; `Fence(backend=...)` still wins over both. Code outside the request — lifespan, work on a thread — keeps the process default.

#### Choosing what to watch

By default every http request is watched. `watch` is a predicate — one knob rather than an include list and an exclude list — asked once per request, before the application runs:

```python
def watch(scope: Scope) -> bool:
    return not scope["path"].startswith("/upload/")     # exempt the buffering routes

app = FastAPI(middleware=[Middleware(DisconnectMiddleware, watch=watch)])
```

It is asked *before* the router, which is the constraint to design around: **there is no route in the scope yet.** `scope["path"]` is the raw request path — `/items/42`, never the `/items/{id}` template — along with `method`, `headers`, `query_string` and the rest of the request. Only [`on_disconnect`](#reporting-a-disconnect) sees `route`, because it runs after the app returned. Matching paths here therefore means regexes that restate your route table and drift from it silently; prefer a rule that doesn't, like a path prefix you also mount under.

That timing is not an implementation gap. Ownership of the channel cannot be retrofitted — by the time a handler asks for the event, the first `receive()` may already have been consumed by someone else — so the decision has to be made before anything below runs, and there is nothing to decide later.

The predicate is only asked about requests this instance would otherwise take: non-http scopes and a request an outer instance already published are settled first, so a predicate never has to guard `scope["type"]`. Raising from it propagates — nothing has been sent yet, and guessing an answer either way is worse than a 500 that names the bug.

**Declining is total.** That request gets no event, no scope key and no fencing binding, so `get_disconnect_event()` returns `None`, `require_disconnect_event()` raises, and `get_current_fencing().move_on_cancel()` in that handler is simply not disconnect-aware — with no error to notice. The `RuntimeError` says so, but only after the fact:

```
RuntimeError: aiofence disconnect signalling requires DisconnectMiddleware. Install it
outermost: ... If it is installed, its watch predicate declined this request.
```

So two things are worth checking before reaching for it:

- **If the goal is "this work must survive the client leaving"**, you don't want `watch` — you want [a fresh `Fencing()`](#the-middlewares-own-binding) at the work that must outlive the request. That keeps the decision next to the code it concerns, and every other fence in the request stays disconnect-aware.
- **If the goal is "only a few routes should pay for this at all"**, mounting is stronger than a predicate: install the middleware on the sub-application rather than app-wide. Nothing above a `Mount` reads the channel, so ownership is intact when it runs, and the boundary is structural instead of a pattern that can drift.

  ```python
  stream = FastAPI()
  stream.add_middleware(DisconnectMiddleware)   # owns the channel for this subtree only
  app.mount("/stream", stream)
  ```

`watch` earns its place when the rule isn't a subtree — a method, a header, a content length, a feature flag read per request.

#### Reporting a disconnect

`on_disconnect` is a callback — sync or async — for logging and metrics. It is called with the ASGI scope once a request the client left has finished, and not at all on the happy path: a completed response never sets the event, so the hook needs no filter of its own.

```python
from starlette.types import Scope
from aiofence.contrib.starlette import DisconnectMiddleware

def client_left(scope: Scope) -> None:
    DISCONNECTS.labels(route=scope["route"].path, method=scope["method"]).inc()

app = FastAPI(middleware=[Middleware(DisconnectMiddleware, on_disconnect=client_left)])
```

`DisconnectCallback` — `Callable[[Scope], Awaitable[None] | None]` — is exported alongside it, for annotating a callback you pass around rather than define inline.

It runs *after* the app returned, so routing has already filled the scope in place: `endpoint` and `path_params` from Starlette, plus `route` under FastAPI — and `route.path` is the **template** (`/items/{id}`), not the request path (`/items/42`), which is the label a metric wants. `scope["path"]` is still the raw path if you want that instead; `Mount` rewrites `root_path`, never `path`, so it stays the full request path for the request's lifetime.

Two boundaries are deliberate. The hook is *not* handed the event — being called is the signal, and by then the reader is gone, so the published copies have already been dropped. And an exception out of the hook is logged at `ERROR` on the `aiofence.contrib.starlette` logger rather than propagated: the response has already gone out, and a broken metrics backend must not turn a served request into a failed one.

A channel that *failed* is not a disconnect — `receive` raising means the transport broke, which is [not evidence the client left](#when-the-channel-fails) — so the hook does not fire there. A `send` that raises `OSError` is the opposite case: the server reporting a closed connection, which sets the event and does fire the hook.

#### What it costs

- **One task per request** — the channel reader.
- **The request body is buffered in memory** for the request's lifetime, whether or not the application reads it. That is deliberate: draining the server's queue is what stops a bounded one (hypercorn's `max_app_queue_size`, default 10) from filling mid-upload and stalling the connection, and on HTTP/2 every other stream on it. The cost is that it defeats the server's read backpressure on large uploads.

### Known limitations

Why the middleware is shaped this way, and the indexed list of what breaks without it: [Disconnect Delivery — Design Rationale](disconnect-watcher-analysis.md).

**Exception handlers see no *dependency-bound* fencing.** FastAPI applies exception handling outside the dependency exit stacks, so on the error path the stack unwinds first — and note this inverts the teardown ordering of the success path. The [middleware's own binding](#the-middlewares-own-binding) is unaffected and is what a custom handler should ask; the event itself is always readable from the scope with `get_disconnect_event(request.scope)`.

**Sync (`def`) handlers cannot be fenced.** See [Without a handler parameter](#without-a-handler-parameter) — the threadpool has no running event loop, so entering a fence raises regardless of who owns the channel.

**asyncio only.** Both modules use `asyncio.Event` and `asyncio.create_task` directly. Under a Trio backend (`TestClient(app, backend="trio")`) they raise `RuntimeError: no running event loop`. This is inherited from the core library — `Fencing.event()` takes an `asyncio.Event` — not specific to this integration.
