<p align="center">
  <img src="docs/images/logo.png" alt="aiofence" />
</p>

# aiofence

[![codecov](https://codecov.io/gh/stanislaushimovolos/aiofence/branch/main/graph/badge.svg)](https://codecov.io/gh/stanislaushimovolos/aiofence)

Multi-reason cancellation contexts for Python asyncio. Inspired by Go's `context.Context`, `aiofence` provides a cancellation context that propagates hierarchically through your application via `ContextVar` — no need to thread events, flags, or tokens through every call signature. Declare cancellation sources once at the boundary — inner code just wraps cancellable work in a context manager and doesn't care about the actual reasons, though it can inspect them if needed.

**The flagship use case is client disconnect.** An inference or agent service burns GPU time and provider spend on requests nobody is listening to any more, and ASGI gives you exactly one shot at noticing. `DisconnectMiddleware` turns that one-shot signal into an ambient cancellation source, so any code below it can stop the work the moment the client goes away — with no `Request` in its signature and no wiring through the call stack. See [Client disconnects](#client-disconnects).

## Motivation

`asyncio` has been steadily adopting structured concurrency patterns — `TaskGroup` (3.11) and `asyncio.timeout()` (3.11) both came from `trio` and `anyio`. But one gap remains: `asyncio` can cancel tasks mechanically, but it can't tell you *why* you were cancelled, doesn't offer a non-raising timeout (`move_on_after`), and forces you to propagate cancellation sources through every call signature. When multiple sources exist (timeout, client disconnect, graceful shutdown), it gets messy fast:

```python
async def handle_request(request, shutdown_event, timeout=30):
    try:
        async with asyncio.timeout(timeout):
            while not shutdown_event.is_set():
                chunk = await get_next_chunk()
                if request.is_disconnected():
                    break
                await process(chunk)
    except TimeoutError:
        ...
    except asyncio.CancelledError:
        # shutdown? disconnect? something else?
        ...
```
For a deeper dive into the problem and design rationale, see [this Medium post](https://medium.com/p/8cdf8c5d519e).

`aiofence` solves this. Declare all cancellation sources once, composably. The callee doesn't even know cancellation exists:

```python
with (
    on_timeout(30)
    .event(shutdown, code="shutdown")
    .move_on_cancel()
) as fence:
    result = await fetch_and_transform()

if not fence.cancelled:
    await save(result)
else:
    print(fence.cancel_reasons)       # (CancelReason(message='timed out after 30s', ...),)
    print(fence.cancelled_by("shutdown"))  # True / False
```

Or raise instead of inspect:

```python
with on_timeout(30).raise_on_cancel() as fence:
    result = await fetch_and_transform()
# raises FenceCancelled if timed out
```

### What about `asyncio.shield()`?

`shield()` prevents cancellation from reaching shielded code, but it works from the opposite direction — you protect everything that *must not* be cancelled. In practice this means wrapping database writes, state transitions, logging, and cleanup individually, and each function needs to know whether it's cancel-safe.

`aiofence` comes at it differently: most code doesn't know cancellation exists. You only wrap the expensive, safely-interruptible parts — the operations you *want* to cancel. For example, in an LLM inference service, you don't want to cancel database queries or response formatting. You want to cancel the LLM call that's burning GPU time for a client that already disconnected:

```python
with (
    on_event(client_disconnect)
    .timeout(budget)
    .move_on_cancel()
) as fence:
    result = await llm.generate(prompt)  # cancellable

await db.save(result or fallback)  # always runs, no shield needed
```

### aiofence and anyio

`anyio.CancelScope` is the best cancellation *delivery* mechanism asyncio has: one scope, one deadline, one `cancel()`, shields honoured. What it does not do is the layer above delivery. It cannot say which of several sources fired, has no ambient "these are the cancellation sources for this request", no way to decline a reason under a precondition, and nothing for ASGI disconnects. `aiofence` is that layer, and it is not a replacement: on Starlette and FastAPI it cancels *through* anyio (`AnyioBackend`, the middleware's default), so the shields httpx and Starlette wrap their cleanup in hold. On plain asyncio it uses `task.cancel()` directly and needs no dependency.

The philosophies also differ, and compose. `anyio` puts one broad `CancelScope` over the operation and shields the parts that must survive. `aiofence` wraps only the expensive, safely interruptible part you *want* cancelled, and lets everything else run unaware. Inside a fence, library shields still hold.

## Features

**Composable triggers** — chain timeouts, events, deadlines, and custom triggers into a single `Fencing`. Each call returns a new immutable builder, so configs are safe to share and extend:

```python
fencing = on_timeout(30, code="budget").event(shutdown, code="shutdown")

# extend per-operation
with fencing.timeout(5, code="db").move_on_cancel() as fence:
    await query_db()
```

**Context propagation** — store a `Fencing` in a `ContextVar` at the boundary, read it anywhere with `get_current_fencing()`. No need to pass configs through every call signature:

```python
# HTTP handler boundary
with bind_fencing(on_event(disconnect, code="disconnect").timeout(30)):
    await handle_request()

# deep inside, no arguments needed
async def process():
    with get_current_fencing().move_on_cancel() as fence:
        await do_work()
```

**Typed cancellation reasons** — after cancellation, inspect *which* trigger fired. Each reason carries a machine-readable `code` for programmatic matching:

```python
if fence.cancelled_by("disconnect"):
    log("client left")
elif fence.cancelled_by("budget"):
    return cached_result
```

**Guarded cancellation** — a trigger firing is not always a reason to cancel. Decline a reason while a precondition holds, scoped to one code so the rest of the fence keeps working:

```python
with get_current_fencing().unless(generation.is_done, code="disconnect").move_on_cancel() as fence:
    async for chunk in upstream:   # keeps draining after the finish reason
        yield chunk
```

**Native asyncio, anyio optional** — the core has no dependencies and cancels through asyncio's own `cancel()`/`uncancel()` counter protocol, compatible with `TaskGroup` and `asyncio.timeout()`. How the cancel is delivered is a pluggable [backend](docs/api.md#cancel-backend): `AnyioBackend` (the `aiofence[anyio]` extra) cancels through an `anyio.CancelScope` instead, and `DisconnectMiddleware` uses it by default.

## Client disconnects

For Starlette and FastAPI, this is what `aiofence` is mainly built for. `DisconnectMiddleware` owns the request's receive channel — one reader, replayed to everything below it — and binds its disconnect event to the current `Fencing` context via `bind_fencing()` for the whole request. Installing it is the whole setup: when the client disconnects, any fence created from `get_current_fencing()` — anywhere in the call stack — is cancelled with `code="disconnect"` (`DISCONNECT_CODE`). Declaring `DisconnectFencing` on a route is optional, and only needed for a per-route code:

```python
from starlette.middleware import Middleware
from aiofence.contrib.starlette import DISCONNECT_CODE, DisconnectMiddleware

app = FastAPI(middleware=[Middleware(DisconnectMiddleware)])   # outermost, required

@app.get("/work")
async def handler():
    with get_current_fencing().timeout(30, code="budget").move_on_cancel() as fence:
        await long_work()

    if fence.cancelled_by(DISCONNECT_CODE):
        return Response(status_code=499)
```

The real value is that the binding is ambient, so service-layer code doesn't need to know about HTTP, requests, or disconnect events — it reads the cancellation context via `get_current_fencing()`:

```python
# handler — no fencing wiring at the boundary either
@app.get("/generate")
async def handler(prompt: str):
    result = await generate_response(prompt)
    return {"status": "ok", "result": result}

# service layer — no request, no fencing in the signature
async def generate_response(prompt: str) -> str:
    # canceled on timeout or global disconnect event
    with (
        get_current_fencing()
        .timeout(30, code="budget")
        .move_on_cancel()
    ) as fence:
        result = await llm.generate(prompt)

    if fence.cancelled_by("disconnect"):
        return "client disconnected, skipping"
    if fence.cancelled_by("budget"):
        return await get_cached_response(prompt)
    return result
```

Under the middleware every fence cancels through anyio, so an httpx call cut short by a disconnect finishes closing its connection instead of leaking a pool slot — the shields anyio-based libraries rely on hold. Pass `DisconnectMiddleware(backend=NativeBackend())` to opt an app out; see [which backend cancels](docs/api.md#which-backend-cancels).

Code with no dependency and no `Request` to hand can read the event straight from the ambient request:

```python
from aiofence.contrib.starlette import get_disconnect_event

async def deep_helper():
    gone = get_disconnect_event()          # None when the middleware isn't installed
```

### Why this is the hard part

An ASGI receive channel has exactly one useful reader — `receive()` is a queue pop, not a broadcast — while a request routinely has several interested parties: `StreamingResponse`'s disconnect listener, sse-starlette's, `Request.is_disconnected()`, and your own code. Three properties make the arbitration correct, and hand-rolled watchers usually miss at least one:

- **One reader, above everything else.** On hypercorn, daphne and granian `http.disconnect` is delivered exactly once, so whoever reads it first consumes it and every other listener starves. A dependency cannot arbitrate — Starlette captures the raw `receive` before any dependency runs, so there is no reference left to wrap. Only a middleware sits above all of them.

- **Replay, don't discard.** The usual watcher loop drops everything that isn't a disconnect, which steals body chunks: the loser of that race gets `{"body": b"", "more_body": False}`, and Starlette accepts it as a *complete, empty* body. Silent truncation, no exception, no log. `DisconnectMiddleware` forwards every message downstream in order and unchanged.

- **"Stream ended" is not "client left".** Per the ASGI spec `http.disconnect` means the stream ended, and every server sends it once the response is complete. A watcher that can't tell the two apart fires on every successful request — and takes `BackgroundTasks` down with it. The middleware tracks response completion in a wrapped `send`, so only a disconnect arriving *before* the response finished sets the event.

Full reasoning in [Disconnect Delivery — Design Rationale](docs/disconnect-watcher-analysis.md) and [Architecture](docs/architecture.md#disconnect-delivery-replay-and-record).

Requires `starlette` (installed with FastAPI) and `anyio>=4.11`, which Starlette already brings — `pip install aiofence[starlette]` or `aiofence[fastapi]`.

## Documentation

- [API Guide](docs/api.md) — usage, patterns, and examples
- [Architecture](docs/architecture.md) — how it works, cancellation flow, design decisions; [Cancel Backends](docs/architecture.md#cancel-backends) for native vs anyio delivery
- [Why Suppress](docs/why-suppress.md) — why `CancelledError` is suppressed instead of raised
- [Disconnect Delivery — Design Rationale](docs/disconnect-watcher-analysis.md) — why the middleware owns the receive channel, and why the dependencies have no fallback
- [CPython Task Cancellation](docs/cpython-task-cancellation.md) — how `asyncio.Task` cancellation works under the hood

## Caveats

**Nested Fences are not supported.** Entering a `Fence` while another is active on the same task raises `RuntimeError`. Use sequential fences or `get_current_fencing()` composition instead. See [#12](https://github.com/stanislaushimovolos/aiofence/issues/12) for details and progress.

**The disconnect dependencies require `DisconnectMiddleware`** and raise `RuntimeError` without it. There is no fallback on purpose — see [Why this is the hard part](#why-this-is-the-hard-part) and [the API guide](docs/api.md#why-the-middleware-is-required).

## Requirements

Python 3.12+. No dependencies.

## License

MIT
