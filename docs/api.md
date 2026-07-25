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

Every cancellation source is a **trigger**. You declare triggers once at the boundary using a **Fencing** builder, then materialize them into a context manager. Inside the block, code runs normally — no need to thread events, flags, or tokens through call signatures.

After the block, inspect `fence.cancelled`, `fence.cancel_reasons`, or `fence.cancelled_by(code)` to decide what to do.

## Creating a Fencing

Use the factory functions — each returns a `Fencing` builder:

| Factory | Condition |
|---------|-----------|
| `on_timeout(delay, *, code=None)` | Relative timeout in seconds |
| `on_deadline(when, *, code=None)` | Absolute monotonic time (`loop.time()` based) |
| `on_event(event, *, code=None)` | Cancel when `asyncio.Event` is set |

The `code` parameter is an optional machine-readable identifier. Use it to distinguish which trigger fired via `fence.cancelled_by(code)`. Works well with `StrEnum` for type safety.

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
| `.timeout(delay, *, code=None)` | Add a relative timeout |
| `.deadline(when, *, code=None)` | Add an absolute deadline (`loop.time()` based) |
| `.event(event, *, code=None)` | Add an event condition |

### Timeout / Deadline Merging

Time-based conditions are merged — the tightest constraint wins:

```python
ctx = on_timeout(30).timeout(5, code="db")
# 30s vs 5s → 5s wins, code="db"

ctx = on_deadline(T + 20, code="sla").timeout(5, code="db")
# T+20 vs now+5 → minimum wins
```

`.timeout()` eagerly resolves to an absolute deadline, making the `Fencing` **one-shot** (raises on reuse). Use `.deadline()` for reusable configs.

Events are never merged — all arm independently. Registrations are deduplicated on the `(event, code)` pair, so the same event under two different codes gives you two triggers and `cancelled_by()` answers `True` for both. The same event under the same code collapses to one.

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
| `fence.cancelled` | `bool` | `True` if any trigger fired |
| `fence.suppressed` | `bool` | `True` if `CancelledError` was caught and suppressed |
| `fence.cancel_reasons` | `tuple[CancelReason, ...]` | All reasons that fired |
| `fence.cancelled_by(code)` | `bool` | Did a specific trigger fire? |

Most code should use `cancelled` — it tells you whether a condition was met. `suppressed` differs only when a trigger fires but the body completes synchronously before `CancelledError` is delivered (pre-triggered sync body). In that case `cancelled` is `True` but `suppressed` is `False`.

Each `CancelReason` has:

| Field | Type | Description |
|-------|------|-------------|
| `message` | `str` | Human-readable (e.g. `"timed out after 5s"`) |
| `cancel_type` | `CancelType` | `TIMEOUT` or `EVENT` |
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

`Fencing` builders that use only `.deadline()` and `.event()` are reusable — each `move_on_cancel()` / `raise_on_cancel()` creates a fresh `Fence`:

```python
ctx = on_deadline(loop.time() + 30)

with ctx.move_on_cancel() as f1:
    await op_a()

with ctx.move_on_cancel() as f2:
    await op_b()
```

**Note:** `.timeout()` anchors the builder to a point in time, making it one-shot. Reusing an anchored `Fencing` raises `RuntimeError`. Call `.timeout()` fresh each time instead.

### Multiple triggers

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

# Boundary: declare the rules
fencing = on_event(disconnect, code="disconnect").timeout(30)
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
- **Token-based set/reset** — nesting works naturally. Inner `bind_fencing()` overrides, outer is restored on exit.
- **Task inheritance** — `asyncio.create_task()` copies the `ContextVar` automatically. Child tasks inherit the boundary's config without affecting the parent.
- **`get_current_fencing()` with no context** — returns an empty `Fencing()`, so chaining always works: `get_current_fencing().timeout(5)`.

## Low-Level API: Fence

`Fence` is the underlying context manager. Use it directly when you need full control over trigger instances:

```python
from aiofence import Fence, TimeoutTrigger, EventTrigger

with Fence(TimeoutTrigger(5), EventTrigger(shutdown, code="shutdown")) as fence:
    await work()
```

`Fence` always suppresses `CancelledError`. It doesn't raise `FenceCancelled` — for that, use `Fencing.raise_on_cancel()`.

## Starlette / FastAPI Integration

`aiofence.contrib.starlette` provides a FastAPI dependency that cancels the current `Fencing` when the client disconnects.

```python
from fastapi import FastAPI
from aiofence.contrib.fastapi import DisconnectFencing

app = FastAPI()

@app.get("/work")
async def handler(fencing: DisconnectFencing):
    with fencing.move_on_cancel() as fence:
        await long_work()

    if fence.cancelled_by("disconnect"):
        return Response(status_code=499)
```

`DisconnectFencing` is an alias for `Annotated[Fencing, Depends(disconnect_fencing)]`, from `aiofence.contrib.fastapi` (requires `fastapi`). `aiofence.contrib.starlette` holds the dependencies themselves and works with plain Starlette.

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

**Requires `fastapi>=0.118` / `starlette>=0.42`** — install with `pip install "aiofence[fastapi]"`. FastAPI 0.106–0.117 tear yield dependencies down *before* the response is sent, which leaves the watcher dead for the whole streaming phase. The core package itself stays dependency-free.

**Install [`DisconnectMiddleware`](#disconnectmiddleware--one-reader-for-the-channel) too.** The dependencies work on their own, but only the middleware can tell "the client left" from "the response finished", and only it can share the receive channel with `StreamingResponse`, `EventSourceResponse` and raw body reads. Everything under [Known limitations](#known-limitations) is what you get without it.

`disconnect_fencing` does three things:

1. Gets the request's disconnect `asyncio.Event` — the one `DisconnectMiddleware` published, or, with no middleware installed, one it creates and caches in the ASGI scope itself
2. Adds it to `get_current_fencing()` with `code="disconnect"`
3. Binds the result as the active `Fencing` context via `bind_fencing()`

#### One reader per request

Two independent receive loops would steal each other's messages and neither would be guaranteed to see the disconnect, so there is only ever one. `disconnect_fencing`, `disconnect_event`, and any number of `disconnect_fencing_dependency(...)` variants therefore combine freely on one endpoint.

With the middleware installed the dependencies own nothing: they borrow the published event, and the middleware's pump is the single reader for the whole request. Without it, the first entrant starts a watch loop and caches it in the scope. That watch is reference counted — the last one out cancels the listener and drops the scope entry — so reuse is safe in any order: a short-lived dependency exiting under a long-lived one doesn't take the listener with it, and a dependency entering later in the same request starts a fresh watch instead of inheriting a dead event.

#### Layering codes

Each registration keeps its own code: `Fencing.event()` deduplicates on the `(event, code)` pair, not on the event alone. Two layers with different codes both report.

```python
app = FastAPI(dependencies=[Depends(disconnect_fencing)])                       # "disconnect"
router = APIRouter(
    dependencies=[Depends(disconnect_fencing_dependency(code="client_gone"))],  # "client_gone"
)

# in a handler on that router, after a disconnect:
fence.cancelled_by("disconnect")   # True
fence.cancelled_by("client_gone")  # True
```

Registering the same event under the *same* code twice collapses to a single trigger.

#### When the channel fails

If `receive()` raises — a transport error, or `BaseHTTPMiddleware` rejecting a message — the disconnect event **can no longer fire** for that request: `fence.cancelled_by("disconnect")` stays `False` however long the client has been gone. Nothing is propagated into the request's teardown either way, because by then the response has usually already been sent and Starlette could only turn it into a truncated response plus `RuntimeError("Caught handled exception, but response already started")`.

Where the error shows up depends on which reader owns the channel:

- **With the middleware** — it is re-raised from the next downstream `receive()`, at the point where the application actually reads, and it never replaces the application's own exception. If nothing ever reads again it is logged at `WARNING` on the `aiofence.contrib.middleware` logger when the request ends.
- **Without it** — the dependency's watcher logs the exception to the `aiofence.contrib.starlette` logger and stays down. Watch that logger.

### Composing with other triggers

The returned `Fencing` inherits from the current context, so you can chain additional triggers:

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

Inner code can also access the disconnect trigger via `get_current_fencing()`:

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
from aiofence.contrib.starlette import disconnect_fencing_dependency

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

### Both dependencies, both frameworks

The disconnect event — stop at your own pace:

```python
# Starlette
async def handler(gone: asyncio.Event = Depends(disconnect_event)):
    ...

# FastAPI
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
# Starlette
async def handler(fencing: Fencing = Depends(disconnect_fencing)):
    ...

# FastAPI
async def handler(fencing: DisconnectFencing):
    with fencing.timeout(30, code="budget").move_on_cancel() as fence:
        result = await render_scene()

    if fence.cancelled_by("disconnect"):
        return Response(status_code=499)
    return result
```

`DisconnectEvent` and `DisconnectFencing` come from `aiofence.contrib.fastapi`; both dependencies from `aiofence.contrib.starlette`.

### `DisconnectMiddleware` — one reader for the channel

The dependencies alone cannot fix the two channel problems below: a dependency never sees the `receive` that Starlette already handed to `StreamingResponse`, and no reader at wire level can tell a client leaving from a response finishing. `DisconnectMiddleware` sits above the whole stack, reads the channel exactly once per request, and replays what it read to everyone underneath.

```python
from starlette.middleware import Middleware
from aiofence.contrib.middleware import DisconnectMiddleware

app = FastAPI(middleware=[Middleware(DisconnectMiddleware)])
# or, equivalently — but make it the last add_middleware call:
app.add_middleware(DisconnectMiddleware)
```

**Install it outermost** — first entry of `middleware=[...]`, or the *last* `add_middleware` call, so it owns the server's own `receive` rather than another middleware's wrapper. Below a `BaseHTTPMiddleware` it still works, but it then owns that middleware's non-reentrant `wrapped_receive` instead.

Nothing else changes. `disconnect_fencing` / `disconnect_event` borrow the event the middleware published and start no watcher of their own; with no middleware installed they keep their own. Installing it is a configuration change, and so is rolling it back.

The event is published in the ASGI scope, so code with no dependency to hand can read it directly:

```python
from aiofence.contrib.middleware import get_disconnect_event

async def endpoint(request: Request) -> Response:
    gone = get_disconnect_event(request.scope)   # None when the middleware isn't installed
```

The key is removed again when the request ends, so nothing can hold a reference to an event whose reader is gone.

#### What it fixes

| | Dependency only | With the middleware |
|---|---|---|
| `BackgroundTasks` on a successful request | cancelled every time — a completed response reads as a disconnect | run normally; the event fires only while the response is unfinished |
| Raw body reads (`Request`-only handler) | hang, or silently return `b""` | exact bytes, in order, however late they are read |
| `StreamingResponse` below ASGI spec 2.4 | a race: either the fence fires or Starlette aborts the body | both readers are told |
| sse-starlette `EventSourceResponse` | `client_close_handler_callable` never runs | close handler runs *and* `cancelled_by("disconnect")` is `True` |
| hypercorn / daphne / granian | they deliver `http.disconnect` once, so the second reader starves | latched once, replayed to every later read |
| `BaseHTTPMiddleware` in the stack | masks the false disconnect; can raise into the watcher | one reader, so no reentrancy and no masking |

`http.request` messages are forwarded downstream in order and unchanged, and `http.disconnect` is treated as a terminal side channel — latched, never queued behind body chunks, and re-delivered on every later `receive()`.

What replay settles is that both readers are *told*. Which one acts first is still a scheduling matter, and a fenced body legitimately outlives its rival listener's cancel scope: `move_on_cancel()` suppressed the cancellation on purpose, so the generator resumes and emits its last chunk. An unfenced body is still torn down by the rival listener, as before.

#### Binding the fencing app-wide

```python
app = FastAPI(middleware=[Middleware(DisconnectMiddleware, fencing_code="disconnect")])
```

`fencing_code` has the middleware bind the `Fencing` itself, for the whole request, instead of leaving that to a dependency. Two consequences:

- **Exception handlers can see it.** FastAPI applies exception handling *outside* its dependency exit stacks, so a dependency-bound fencing is already gone by the time a handler runs — a middleware-bound one is not. This is the only way for a custom exception handler to ask "was this a disconnect?".
- **One code for the whole app.** Per-route codes stay the dependency's job. Layering both is safe: they share the one event, and each registration keeps its own code.

Plain ASGI and plain Starlette apps have no dependency injection at all, so `fencing_code` is the whole story there.

#### What it costs

- **One task per request** — the pump.
- **The request body is buffered in memory** for the request's lifetime, whether or not the application reads it. That is deliberate: draining the server's queue is what stops a bounded one (hypercorn's `max_app_queue_size`, default 10) from filling mid-upload and stalling the connection, and on HTTP/2 every other stream on it. The cost is that it defeats the server's read backpressure on large uploads.

### Known limitations

Two properties of the ASGI receive channel drive the first half of this list. Neither is fixable from a dependency, and both are fixed by [`DisconnectMiddleware`](#disconnectmiddleware--one-reader-for-the-channel). Full mechanics and evidence: [Disconnect Watcher Analysis](disconnect-watcher-analysis.md).

1. **The channel has one useful reader.** `receive()` is a queue pop, not a broadcast. The dependency's watcher loops on it and discards every message that isn't `http.disconnect`, so anything else in the stack that reads the channel splits the message stream with the watcher.
2. **`http.disconnect` doesn't only mean "the client left."** Per the ASGI spec it is also sent once the response has been sent. Every server collapses the two, and at wire level no reader can tell them apart.

#### Without the middleware

**`BackgroundTasks` are cancelled on every successful request.** This follows from (2). `Response.__call__` awaits `self.background()` after the final body send, while the watcher is still alive and parked in `receive()` — so it wakes with `http.disconnect` and sets the event. Any `Fence` opened after that point is cancelled immediately, and reports `cancelled_by("disconnect") == True` on a request where the client never left. Don't fence background work, and don't use `get_current_fencing()` inside a background task on a fenced route.

```python
@app.get("/work", dependencies=[Depends(disconnect_fencing)])
async def handler(bt: BackgroundTasks):
    bt.add_task(work)        # work() sees an already-cancelled fencing
    return {"ok": True}
```

**Raw body reads are unsafe.** The unsafe shape is a handler that declares **no body parameter** — FastAPI only pre-reads and caches the body when one is declared, so a `Request`-only handler is unprotected whether or not it reads raw. A raw read after any suspension point either hangs forever or, if the watcher already took a chunk, returns a **silently truncated** `b""` that Starlette accepts as a complete empty body. No exception, no log.

```python
@app.post("/upload")
async def handler(fencing: DisconnectFencing, payload: Payload, request: Request):
    await something()
    body = await request.body()      # cached before the watcher existed — safe

@app.post("/raw")
async def handler(fencing: DisconnectFencing, request: Request):
    await something()
    body = await request.body()      # may hang, or silently return b""
```

**`StreamingResponse` is a rival reader.** Starlette runs its own `listen_for_disconnect` unless the server declares ASGI `spec_version` 2.4 or higher — and **no current production server does**: uvicorn 0.32.1+ ships `2.3`, hypercorn `2.1`, granian `2.3`, daphne none at all, and the `TestClient` none. So the two-reader race is the normal path, not an edge case. Whoever wins decides: if the watcher wins, the fence fires and Starlette's listener parks forever; if Starlette wins, `cancelled_by("disconnect")` stays `False` and the body is aborted mid-stream instead. Which one wins turns on whether the handler suspended before returning the response.

```python
@app.get("/stream")
async def handler(fencing: DisconnectFencing):
    async def body():
        yield first_chunk
        with fencing.move_on_cancel() as fence:
            await slow_step()        # may or may not be cancelled on disconnect
        yield second_chunk

    return StreamingResponse(body())
```

**SSE disables sse-starlette's close handling.** `EventSourceResponse` reads the channel unconditionally and starts its listener last, so the watcher wins deterministically. `cancelled_by("disconnect")` works — but `client_close_handler_callable` never runs and pings keep going to a closed socket. If you rely on that handler, install the middleware or leave the endpoint unfenced.

```python
@app.get("/tokens")
async def handler(fencing: DisconnectFencing):
    async def events():
        with fencing.move_on_cancel() as fence:
            async for token in llm.stream(prompt):
                yield token
        # fence.cancelled_by("disconnect") is True here, but on_close never ran

    return EventSourceResponse(events(), client_close_handler_callable=on_close)
```

**Only uvicorn re-delivers `http.disconnect`.** hypercorn, daphne and granian enqueue it exactly once, so on those servers the reader that gets there first consumes it and the others never fire — the race above becomes a permanent loss for whoever came second. `Request.is_disconnected()` is not a fallback either: it is a reader too, and is independently broken under `BaseHTTPMiddleware`.

**`Depends(..., scope="function")` (FastAPI ≥ 0.121) closes before the response,** so mixing it with a request-scoped disconnect dependency inverts the ordering the shared watch relies on: the fencing stays bound to an event whose watcher is already gone. Keep all disconnect dependencies on the default request scope. With the middleware there is no watch to outlive, and the published event lives for the whole request either way.

#### With the middleware or without it

**Exception handlers see no *dependency-bound* fencing.** FastAPI applies exception handling outside the dependency exit stacks, so on the error path the stack unwinds first — and note this inverts the teardown ordering of the success path. Bind through the middleware's `fencing_code` if a custom handler needs to ask "was this a disconnect?"; the event itself is always readable from the scope with `get_disconnect_event(request.scope)`.

**Sync (`def`) handlers cannot be fenced.** See [Without a handler parameter](#without-a-handler-parameter) — the threadpool has no running event loop, so entering a fence raises regardless of who owns the channel.

**asyncio only.** Both modules use `asyncio.Event` and `asyncio.create_task` directly. Under a Trio backend (`TestClient(app, backend="trio")`) they raise `RuntimeError: no running event loop`. This is inherited from the core library — `Fencing.event()` takes an `asyncio.Event` — not specific to this integration.
