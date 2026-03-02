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

After the block, inspect `fence.cancelled`, `fence.reasons`, or `fence.cancelled_by(code)` to decide what to do.

## Creating a Fencing

Use the factory functions — each returns a `Fencing` builder:

| Factory | Condition |
|---------|-----------|
| `on_timeout(delay, *, code=None)` | Relative timeout in seconds |
| `on_deadline(when, *, code=None)` | Absolute monotonic time (`loop.time()` based) |
| `on_event(event, *, code=None)` | Cancel when `asyncio.Event` is set |
| `on_trigger(trigger)` | Custom `Trigger` instance |

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
| `.trigger(trigger)` | Add a custom trigger |

### Timeout / Deadline Merging

Time-based conditions are merged — the tightest constraint wins:

```python
ctx = on_timeout(30).timeout(5, code="db")
# At entry: 30s vs 5s → 5s wins, code="db"

ctx = on_deadline(T + 20, code="sla").timeout(5, code="db")
# At entry: T+20 vs now+5 → minimum wins
```

Events and custom triggers are never merged — all arm independently.

## Entering the Fence

Two modes, both yield a `Fence`:

### `move_on_cancel()` — suppress and inspect

```python
with on_timeout(5).move_on_cancel() as fence:
    await work()

if fence.cancelled:
    print(fence.reasons)  # why were we cancelled?
```

`CancelledError` is suppressed. Code after the `with` block always runs. Check `fence.cancelled` to decide what to do.

### `raise_on_cancel()` — raise FenceCancelled

```python
try:
    with on_timeout(5).raise_on_cancel() as fence:
        await work()
except FenceCancelled as e:
    print(e.reasons)
    print(e.cancelled_by("shutdown"))
```

`CancelledError` is still suppressed inside the block, but `FenceCancelled` (a regular `Exception`, not `CancelledError`) is raised after exit. Safe to use inside `TaskGroup`.

## Inspecting Cancellation

After the block, the `Fence` has:

| Property / Method | Type | Description |
|-------------------|------|-------------|
| `fence.cancelled` | `bool` | `True` if any trigger fired |
| `fence.reasons` | `tuple[CancelReason, ...]` | All reasons that fired |
| `fence.cancelled_by(code)` | `bool` | Did a specific trigger fire? |

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

Each `move_on_cancel()` / `raise_on_cancel()` creates a fresh `Fence`:

```python
ctx = on_timeout(5)

with ctx.move_on_cancel() as f1:
    await op_a()

with ctx.move_on_cancel() as f2:
    await op_b()
```

### Multiple triggers

```python
with (
    on_timeout(30, code="timeout")
    .event(shutdown, code="shutdown")
    .trigger(CircuitBreakerTrigger(breaker))
    .move_on_cancel()
) as fence:
    await call_external()

if fence.cancelled_by("timeout"):
    log("slow response")
elif fence.cancelled_by("shutdown"):
    log("shutting down")
elif fence.cancelled:
    log("circuit breaker tripped")
```

## Context Propagation

`bind_fencing_defaults()` stores a `Fencing` in a `ContextVar`, so inner code can access it via `get_fencing_defaults()` without passing it through every call signature.

```python
from aiofence import Fencing, bind_fencing_defaults, get_fencing_defaults, on_event

# Boundary: declare the rules
fencing = on_event(disconnect, code="disconnect").timeout(30)
with bind_fencing_defaults(fencing):
    await handle_request()

# Deep inside: read and use
async def process():
    with get_fencing_defaults().move_on_cancel() as fence:
        await do_work()

# Or extend with local concerns:
async def process_with_extra():
    with get_fencing_defaults().event(other_event).move_on_cancel() as fence:
        await do_work()
```

### Semantics

- **`bind_fencing_defaults()` only stores config** — it does not create a Fence. `move_on_cancel()` / `raise_on_cancel()` materialize Fences from it.
- **Token-based set/reset** — nesting works naturally. Inner `bind_fencing_defaults()` overrides, outer is restored on exit.
- **Task inheritance** — `asyncio.create_task()` copies the `ContextVar` automatically. Child tasks inherit the boundary's config without affecting the parent.
- **`get_fencing_defaults()` with no context** — returns an empty `Fencing()`, so chaining always works: `get_fencing_defaults().timeout(5)`.

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
from fastapi import Depends, FastAPI
from aiofence import Fencing
from aiofence.contrib.starlette import disconnect_fencing

app = FastAPI()

@app.get("/work")
async def handler(fencing: Fencing = Depends(disconnect_fencing)):
    with fencing.move_on_cancel() as fence:
        await long_work()

    if fence.cancelled_by("disconnect"):
        return Response(status_code=499)
```

`disconnect_fencing` does three things:

1. Creates an `asyncio.Event` that fires on `http.disconnect`
2. Adds it to `get_fencing_defaults()` with `code="disconnect"` (or a custom code)
3. Binds the result as the active `Fencing` context via `bind_fencing_defaults()`

### Composing with other triggers

The returned `Fencing` inherits from the current context, so you can chain additional triggers:

```python
@app.get("/work")
async def handler(fencing: Fencing = Depends(disconnect_fencing)):
    with fencing.timeout(30, code="budget").move_on_cancel() as fence:
        await long_work()

    if fence.cancelled_by("budget"):
        return cached_result
    elif fence.cancelled_by("disconnect"):
        return Response(status_code=499)
```

Inner code can also access the disconnect trigger via `get_fencing_defaults()`:

```python
@app.get("/work")
async def handler(fencing: Fencing = Depends(disconnect_fencing)):
    await process()

async def process():
    with get_fencing_defaults().move_on_cancel() as fence:
        await do_work()  # cancelled if client disconnects
```

### Custom disconnect code

```python
async def handler(
    fencing: Fencing = Depends(lambda r: disconnect_fencing(r, code="client_gone")),
):
    ...
```
