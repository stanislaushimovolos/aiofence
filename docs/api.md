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

Events are never merged — all arm independently.

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

`disconnect_fencing` does three things:

1. Creates an `asyncio.Event` that fires on `http.disconnect`
2. Adds it to `get_current_fencing()` with `code="disconnect"` (or a custom code)
3. Binds the result as the active `Fencing` context via `bind_fencing()`

The disconnect event is created once per request and cached in the ASGI scope, so `disconnect_fencing` and `disconnect_event` can be combined on the same endpoint — they share a single receive loop. Two independent loops would steal each other's messages, and only one of them would see the disconnect.

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

Sync (`def`) handlers are not cancellable — FastAPI runs them in a threadpool. The dependency still binds and `fence.cancelled_by(...)` still reports correctly, but nothing interrupts the handler.

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

### Caveats: the watcher owns the receive channel

While a disconnect fencing is bound, its watcher loops on `receive()` and discards every message that isn't `http.disconnect`. An ASGI receive channel has only one useful reader, so anything else in the stack that reads it splits the message stream with the watcher. Three consequences:

**Don't read the raw body.** `await request.body()` or `request.stream()` inside a fenced handler races the watcher and can hang. Declare the body as a parameter instead — FastAPI caches it before dependencies run, so a later `request.body()` returns cached bytes:

```python
@app.post("/upload")
async def handler(fencing: DisconnectFencing, payload: Payload, request: Request):
    await something()
    body = await request.body()      # cached — safe

@app.post("/raw")
async def handler(fencing: DisconnectFencing, request: Request):
    await something()
    body = await request.body()      # unfenced read — may hang
```

**Streaming responses are unreliable.** Starlette's `StreamingResponse` runs its own disconnect listener when the server declares ASGI `spec_version` below `2.4`. Whether the fence or the stream sees the disconnect depends on scheduling. Servers declaring `2.4` or higher are unaffected.

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

**SSE disables sse-starlette's close handling.** `EventSourceResponse` always reads the channel. The watcher wins consistently, so `cancelled_by("disconnect")` works — but `client_close_handler_callable` never runs and pings keep going to a closed socket. If you rely on that handler, leave the endpoint unfenced.

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

All three have the same fix, which needs middleware rather than a dependency. See [Receive Channel Conflicts](receive-channel-conflicts.md) for the mechanics, evidence, and planned fix.
